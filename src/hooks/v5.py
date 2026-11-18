"""R2 v5 原语：tapable 式 Hook 类型 + Host/RequestScope。

设计铁律（ARCH-v5）：
- 原语即调用语义：Bail / Waterfall / Loop / Parallel / Around 五种，插件不声明 mode。
- 两层：host 持扩展点与每次调用新建的 RequestScope；跨调用状态由插件实例自身持有
  （插件是注册一次、长期存活的单例），无独立 Policy 层、无 ControlState、无冒泡层。
- 四模块契约：Decision 复用 hooks.types；HookCtx/R1 Runner 不感知本模块内部实现。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .types import Decision, DecisionAction

__all__ = [
    "AroundHook",
    "BailHook",
    "Decision",
    "Hook",
    "HookKind",
    "Host",
    "LoopHook",
    "ParallelHook",
    "Plugin",
    "RequestScope",
    "Signal",
    "SignalKind",
    "WaterfallHook",
]


class HookKind(str, Enum):
    """Hook 调用语义（宿主在定义扩展点时选定，插件不声明）。"""

    BAIL = "bail"              # 首个非 None / 非 CONTINUE 判决即停
    WATERFALL = "waterfall"    # 上次返回值作下次首参
    LOOP = "loop"              # 非 None 返回则重跑整链（带上限）
    PARALLEL = "parallel"      # 全执行、异常隔离、返回 None
    AROUND = "around"          # tap 收 next 回调，包裹中心调用


_STANDARD_HOOKS: dict[str, HookKind] = {
    "before_invoke": HookKind.BAIL,       # killswitch/幂等/舱壁/限流/熔断
    "transform": HookKind.WATERFALL,      # 预算/上下文改写
    "around_invoke": HookKind.AROUND,     # retry/timeout 包裹真实调用
    "after_invoke": HookKind.PARALLEL,    # 观察：结果记录
    "invoke_error": HookKind.PARALLEL,    # 观察：失败记录
    "settle": HookKind.BAIL,              # 降级裁决
}


class SignalKind(str, Enum):
    """请求内协作信号的有限取值（规则 5；跨层对齐的字符串枚举）。

    成员：
        DEGRADE: 降级意图，settle 阶段据此选级。
        ABORT: 中止意图。
    """

    DEGRADE = "degrade"
    ABORT = "abort"


@dataclass(frozen=True)
class Signal:
    """请求内协作信号（RequestScope 的具名条目；无冒泡机制）。"""

    kind: SignalKind | str
    reason: str
    failed_target: str | None = None


@dataclass
class RequestScope:
    """每次调用新建的请求级协作容器（host.new_scope）。

    只放协作数据（框架与各插件在流程中产生/消费），不放插件输入参数——
    输入参数经回调签名直送（effect）或装配期构造注入（插件私有配置）。
    所有字段 typed、读写方明确：
        signals: 插件经 emit() 追加，settle 消费
        target: settle 阶段写入的降级目标，供 R4 读取
        last_result: host.call 写入的本次 Result，观察类插件只读
        trace: 各插件 append 的执行轨迹
        value: settle 的最终判决载体
    """

    signals: list[Signal] = field(default_factory=list)
    target: str | None = None
    last_result: Any = None
    trace: list[str] = field(default_factory=list)
    value: Any = None
    releases: list[Any] = field(default_factory=list)  # 资源释放回调（如舱壁槽位），Host 统一兜底执行

    def emit(self, signal: Signal) -> None:
        self.signals.append(signal)

    def release_all(self) -> None:
        """执行所有释放回调（逆序，LIFO）；幂等，清空列表。"""
        for fn in reversed(self.releases):
            try:
                fn()
            except Exception:  # noqa: BLE001, S110 - 释放失败不影响主流程
                pass
        self.releases.clear()

    def decision(self) -> Decision | None:
        """从 signals 推导是否有降级意图（供 settle 阶段读）。"""
        if any(s.kind == SignalKind.DEGRADE for s in self.signals):
            return Decision.degrade(self.target or "")
        return None


TapFn = Callable[..., Awaitable[Any] | Any]


async def _maybe(result: Awaitable[Any] | Any) -> Any:
    """同步/异步判决统一等待。"""
    return await result if asyncio.iscoroutine(result) else result


class Hook:
    """扩展点：宿主定义时选 kind，插件只 tap。"""

    def __init__(self, name: str, kind: HookKind, *, max_loops: int = 100) -> None:
        self.name = name
        self.kind = kind
        self.max_loops = max_loops
        self._taps: list[tuple[str, TapFn]] = []

    def tap(self, plugin_name: str, fn: TapFn) -> Hook:
        """挂一个插件回调；具名、有序。"""
        self._taps.append((plugin_name, fn))
        return self

    @property
    def names(self) -> tuple[str, ...]:
        """已 tap 的插件名（trace 与诊断用）。"""
        return tuple(n for n, _ in self._taps)

    async def call(self, scope: RequestScope, effect: Any = None, *args: Any, **kwargs: Any) -> Any:
        """按 kind 语义执行全部 tap。

        effect 作为请求期输入参数直送每个回调（不进任何共享容器）：
        BAIL/LOOP/PARALLEL → fn(scope, effect)；WATERFALL → fn(value, effect)；
        AROUND → fn(next, scope, effect)。装配期配置由插件构造参数私有持有。
        """
        if self.kind is HookKind.BAIL:
            return await self._call_bail(scope, effect)
        if self.kind is HookKind.WATERFALL:
            return await self._call_waterfall(scope, effect, *args)
        if self.kind is HookKind.LOOP:
            return await self._call_loop(scope, effect)
        if self.kind is HookKind.PARALLEL:
            return await self._call_parallel(scope, effect)
        return await self._call_around(scope, effect, **kwargs)

    async def _call_bail(self, scope: RequestScope, effect: Any) -> Decision | None:
        for _, fn in self._taps:
            d = await _maybe(fn(scope, effect))
            if isinstance(d, Decision) and d.action is not DecisionAction.CONTINUE:
                return d
        return None

    async def _call_waterfall(self, scope: RequestScope, effect: Any, value: Any) -> Any:
        for _, fn in self._taps:
            value = await _maybe(fn(value, effect))
        return value

    async def _call_loop(self, scope: RequestScope, effect: Any) -> Any:
        for _ in range(self.max_loops):
            result: Any = None
            for _, fn in self._taps:
                result = await _maybe(fn(scope, effect))
            if result is None:
                return None
        raise RuntimeError(f"hook {self.name!r} exceeded max_loops={self.max_loops}")

    async def _call_parallel(self, scope: RequestScope, effect: Any) -> None:
        async def guarded(fn: TapFn) -> None:
            try:
                await _maybe(fn(scope, effect))
            except Exception:  # 观察类异常隔离  # noqa: BLE001, S110
                pass

        await asyncio.gather(*(guarded(fn) for _, fn in self._taps))

    async def _call_around(self, scope: RequestScope, effect: Any, *, core: Callable[[RequestScope], Awaitable[Any]]) -> Any:
        async def layer(index: int) -> Any:
            if index >= len(self._taps):
                return await core(scope)

            async def nxt() -> Any:
                return await layer(index + 1)

            return await _maybe(self._taps[index][1](nxt, scope, effect))

        return await layer(0)


BailHook = lambda name: Hook(name, HookKind.BAIL)
WaterfallHook = lambda name: Hook(name, HookKind.WATERFALL)
ParallelHook = lambda name: Hook(name, HookKind.PARALLEL)
LoopHook = lambda name, **kw: Hook(name, HookKind.LOOP, **kw)
AroundHook = lambda name, **kw: Hook(name, HookKind.AROUND, **kw)


@runtime_checkable
class Plugin(Protocol):
    """唯一扩展点：apply(host)；不声明 mode/priority/events。"""

    name: str

    def apply(self, host: Host) -> None: ...


class Host:
    """总线（Compiler 类比）：持有扩展点与 Scope 工厂，提供 R1 注入适配器。"""

    def __init__(self) -> None:
        self._hooks: dict[str, Hook] = {}
        self._plugins: list[Plugin] = []
        self._frozen = False
        # scope 桥接：按 id(ctx) 关联（框架内部，不写进 ctx 或 scope.meta）
        self._scope_bridge: dict[int, RequestScope] = {}
        # 会话级降级目标：call 阶段（timeout/retry）发的信号经 settle 选级后写这里，
        # 供下一次 Effect 的 dispatch 继承——因为 R1 每个 Effect 新建 ctx/scope。
        self._sticky_target: str | None = None
        # 预定义 6 个标准扩展点（ARCH-v5 §2）；宿主可用 define 覆盖语义。
        for name, kind in _STANDARD_HOOKS.items():
            self._hooks[name] = Hook(name, kind)

    def define(self, name: str, kind: HookKind, **kw: Any) -> Hook:
        """宿主定义扩展点并选定语义；v5 冻结后不可再定义。"""
        if self._frozen:
            raise RuntimeError("host frozen: cannot define hooks")
        if name in self._hooks and self._hooks[name].names:
            raise ValueError(f"hook already has taps: {name}")
        hook = Hook(name, kind, **kw)
        self._hooks[name] = hook
        return hook

    def hook(self, name: str) -> Hook:
        if name not in self._hooks:
            raise KeyError(f"undefined hook: {name}")
        return self._hooks[name]

    def plugin(self, plugin: Plugin) -> Host:
        if not getattr(plugin, "name", None):
            raise ValueError("plugin must have a name")
        plugin.apply(self)
        self._plugins.append(plugin)
        return self

    def freeze(self) -> Host:
        self._frozen = True
        return self

    def new_scope(self) -> RequestScope:
        """新建 scope，继承会话级 sticky target（call 阶段降级的跨 Effect 接力）。"""
        scope = RequestScope(target=self._sticky_target)
        return scope

    async def dispatch(self, effect: Any, ctx: Any) -> Decision:
        """R1 注入点：before_invoke → transform → settle。

        scope 按 id(ctx) 在 Host 内部桥接到 call 阶段（不污染 ctx 与 scope）。
        effect 作为参数直送各 tap 回调。
        """
        scope = self.new_scope()
        self._scope_bridge[id(ctx)] = scope
        if hasattr(ctx, "degrade_target"):
            object.__setattr__(ctx, "degrade_target", scope.target)

        before = await self.hook("before_invoke").call(scope, effect)
        if isinstance(before, Decision) and before.action is not DecisionAction.CONTINUE:
            # 守卫短路：本轮没有 call，立即释放 dispatch 阶段占的资源（舱壁槽位）
            scope.release_all()
            return await self._settle(scope, effect, ctx, before)
        transformed = await self.hook("transform").call(scope, effect, "")
        # transform 的链式产出回填 scope.trace（避免返回值被丢弃——预算提示等）
        if transformed:
            scope.trace.append(str(transformed))
        return await self._settle(scope, effect, ctx, Decision.continue_())

    def scope_of(self, ctx: Any) -> RequestScope | None:
        """取回 dispatch 创建的 scope（call 阶段接力读取 target）。"""
        return self._scope_bridge.get(id(ctx))

    def call(self, inner: Callable[[Any, Any], Awaitable[Any]]) -> Callable[[Any, Any], Awaitable[Any]]:
        """R1 注入点：AROUND 包裹真实 Port 调用，成功/失败触发观察扩展点。

        Result（成功或失败信封）写入 scope.last_result（typed 字段，观察插件只读）。
        call 阶段发的降级信号在返回前经 settle 选级，写 sticky target 供下个 Effect 继承。
        """
        async def wrapped(effect: Any, ctx: Any) -> Any:
            scope = self.scope_of(ctx) or self.new_scope()
            self._scope_bridge[id(ctx)] = scope

            async def core(s: RequestScope) -> Any:
                return await inner(effect, ctx)

            try:
                outcome = await self.hook("around_invoke").call(scope, effect, core=core)
                resolved = outcome.value if isinstance(outcome, Decision) and outcome.value is not None else outcome
                scope.last_result = resolved
                await self.hook("after_invoke").call(scope, effect)
                # call 阶段的 timeout/retry 降级信号：返回前跑 settle 更新 sticky target
                if any(s.kind == SignalKind.DEGRADE for s in scope.signals):
                    await self._settle(scope, effect, ctx, Decision.continue_())
                scope.release_all()
                self._scope_bridge.pop(id(ctx), None)
                return resolved
            except Exception:
                from runtime import Result
                err = Result.failure("INVOKE_ERROR", "port raised")
                scope.last_result = err
                await self.hook("invoke_error").call(scope, effect)
                if any(s.kind == SignalKind.DEGRADE for s in scope.signals):
                    await self._settle(scope, effect, ctx, Decision.continue_())
                scope.release_all()
                self._scope_bridge.pop(id(ctx), None)
                raise

        return wrapped

    async def _settle(self, scope: RequestScope, effect: Any, ctx: Any, base: Decision) -> Decision:
        """settle 扩展点：有降级信号时由降级插件选 target 并覆盖占位。"""
        if not any(s.kind == SignalKind.DEGRADE for s in scope.signals):
            return base
        verdict = await self.hook("settle").call(scope, effect)
        if isinstance(verdict, Decision) and verdict.target:
            scope.target = verdict.target
            scope.value = verdict.value
            self._sticky_target = verdict.target  # 跨 Effect 继承（修 B1）
            if hasattr(ctx, "degrade_target"):
                object.__setattr__(ctx, "degrade_target", verdict.target)
            return verdict
        return base
