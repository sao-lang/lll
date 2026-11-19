"""R2 v5 原语：tapable 式 Hook 类型 + Host/RequestScope。

设计铁律（ARCH-v5）：
- 原语即调用语义：Bail / Waterfall / Loop / Parallel / Around 五种，插件不声明 mode。
- 两层：host 持扩展点与每次调用新建的 RequestScope；跨调用状态由插件实例自身持有
  （插件是注册一次、长期存活的单例），无独立 Policy 层、无 ControlState、无冒泡层。
- 四模块契约：Decision 复用 hooks.types；HookCtx/R1 Runner 不感知本模块内部实现。
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from runtime import Effect, Result, RuntimeCtx

from .types import Decision, DecisionAction, HookCtx, ProviderRegistry

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


# dispatch/call 接收的上下文：R2/R4 编排的两种真实类型（非 Any/object）。
# 生产默认 RuntimeCtx（含 degrade_target 桥接字段）；R2 桥接测试用 HookCtx（字段超集）。
# ctx_factory 的用户自定义超集按鸭子语义兼容（桥接处用 hasattr 保护）。
type HookContextArg = RuntimeCtx | HookCtx


@dataclass
class RequestScope:
    """每次调用新建的请求级协作容器（host.new_scope）。

    只放协作数据（框架与各插件在流程中产生/消费），不放插件输入参数——
    输入参数经回调签名直送（effect）或装配期构造注入（插件私有配置）。
    所有字段 typed、读写方明确：
        signals: 插件经 emit() 追加，settle 消费
        target: settle 阶段写入的降级目标，供 R4 读取
        last_result: host.call 写入的本次调用 Result，观察类插件只读
        trace: 各插件 append 的执行轨迹
        value: settle 的兜底判决载体（Decision.fallback 携带的 Result）
    """

    signals: list[Signal] = field(default_factory=list)
    target: str | None = None
    # host.call 写入的本次调用结果：生产与测试均为 Result 成功/失败信封。
    last_result: Result | None = None
    trace: list[str] = field(default_factory=list)
    # settle 兜底判决携带的 Result（decision.fallback 的 value）；None 表示无兜底。
    value: Result | None = None
    # 释放回调零参、返回值不被使用（如舱壁槽位释放），Host 每轮结束逆序兜底执行。
    releases: list[Callable[[], None]] = field(default_factory=list)

    def emit(self, signal: Signal) -> None:
        """追加一个请求内协作信号（供 settle 消费）。"""
        self.signals.append(signal)

    def release_all(self) -> None:
        """执行所有释放回调（逆序，LIFO）；幂等，清空列表。"""
        # 后注册的先释放（LIFO）：资源之间常有依赖关系，逆序归还才不会让先释放者
        # 依赖到已被回收的资源；清空列表使本方法可重复调用而不重复执行。
        for fn in reversed(self.releases):
            try:
                fn()
            except Exception:  # noqa: BLE001, S110 - 释放失败不影响主流程
                pass
        self.releases.clear()


# tap 回调的所有可能返回形态——有限、确定的联合，不是 Any：
#   Decision  守卫拦截 / 降级裁决 / retry/timeout 兜底
#   Result    AROUND 中心调用与 normalize 后的结果
#   str       WATERFALL 链式改写的上下文文本（如预算收尾提示）
#   None      放行 / 观察点 / 循环终止
type TapReturn = Decision | Result | str | None

# tap 回调签名：同步或异步均可，返回 TapReturn 联合中的具体一支。
# 参数形态按 HookKind 分派（BAIL/PARALLEL → (scope, effect)，
# WATERFALL → (value, effect)，AROUND → (next, scope, effect)）。
type TapFn = Callable[..., Awaitable[TapReturn] | TapReturn]


async def _maybe(result: Awaitable[TapReturn] | TapReturn) -> TapReturn:
    """统一等待同步/异步 tap 返回（类型收敛到 TapReturn 联合）。"""
    if inspect.isawaitable(result):
        return await result
    return result


def _coerce_result(outcome: Result | Decision | None) -> Result | None:
    """唯一的"调用结果"归一化边界（规则 6）。

    around_invoke 的 core 生产返回裸 Result（E1），部分插件用 Decision(value=Result)
    载体。此处一次判定把两种确定形态归一为 Result | None——
    输入是 Result/Decision/None 联合（非 Any），输出是确定的 Result | None。
    """
    if isinstance(outcome, Decision):
        value = outcome.value
        return value if isinstance(value, Result) else None
    return outcome


class Hook:
    """扩展点：宿主定义时选 kind，插件只 tap。"""

    def __init__(self, name: str, kind: HookKind, *, max_loops: int = 100) -> None:
        """装配扩展点：名称、调用语义与 LOOP 上限；tap 列表初始为空。

        参数：
            name: 扩展点名（宿主按此取用）。kind: 调用语义。
            max_loops: LOOP 语义的重跑硬上限，防死循环。
        """
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

    async def call(
        self, scope: RequestScope, effect: Effect | None = None,
        *args: str, **kwargs: Callable[[RequestScope], Awaitable[Result | None]],
    ) -> TapReturn:
        """按 kind 语义执行全部 tap（返回有限联合 TapReturn，非 Any）。

        effect 作为请求期输入参数直送每个回调（不进任何共享容器）：
        BAIL/LOOP/PARALLEL → fn(scope, effect)；WATERFALL → fn(value, effect)；
        AROUND → fn(next, scope, effect)。装配期配置由插件构造参数私有持有。
        """
        # R2-T06 组件留名：每次扩展点调用把已 tap 的插件名记入 trace（顺序即 tap 序）。
        # 具名留痕供审计/诊断"谁参与了本次判决"，不改变任何 tap 的返回值或短路语义。
        for plugin_name, _ in self._taps:
            scope.trace.append(f"{self.name}:{plugin_name}")

        # kind 在装配期锁定、互斥，这里仅按语义把调用路由到对应实现；
        # PARALLEL 只有观察副作用、无业务返回值，故调用后统一回 None。
        if self.kind is HookKind.BAIL:
            return await self._call_bail(scope, effect)
        if self.kind is HookKind.WATERFALL:
            return await self._call_waterfall(scope, effect, *args)
        if self.kind is HookKind.LOOP:
            return await self._call_loop(scope, effect)
        if self.kind is HookKind.PARALLEL:
            await self._call_parallel(scope, effect)
            return None
        return await self._call_around(scope, effect, **kwargs)

    async def _call_bail(self, scope: RequestScope, effect: Effect | None) -> Decision | None:
        """BAIL：首个非 CONTINUE 的 Decision 短路返回。"""
        # 按注册顺序逐个问守卫：第一个拦截者即胜出，后续 tap 不再执行（bail 语义）。
        for _, fn in self._taps:
            d = await _maybe(fn(scope, effect))
            # 仅 Decision 能拦截，且 CONTINUE 表示放行；其它返回形态在 BAIL 点无拦截含义。
            if isinstance(d, Decision) and d.action is not DecisionAction.CONTINUE:
                return d
        return None

    async def _call_waterfall(
        self, scope: RequestScope, effect: Effect | None, value: str,
    ) -> str:
        """WATERFALL：上次的字符串返回值作下次首参，逐 tap 链式改写上下文。"""
        # 链式累积：只有字符串返回才推进 value，避免某个 tap 顺手返回非字符串时把
        # 前面积累的改写文本清空（守卫型返回值在 WATERFALL 点被忽略）。
        for _, fn in self._taps:
            out = await _maybe(fn(value, effect))
            value = out if isinstance(out, str) else value
        return value

    async def _call_loop(self, scope: RequestScope, effect: Effect | None) -> str | None:
        """LOOP：非 None 返回则重跑整链，带 max_loops 硬上限。"""
        # 每轮 = 整链重跑一次；max_loops 是硬上限，防止 tap 持续返回非 None 时死循环。
        for _ in range(self.max_loops):
            # 预置 None：tap 列表为空时视为已收敛，直接落到下面的终止分支。
            result: TapReturn = None
            # result 逐 tap 覆盖，只有链上最后一个 tap 的返回决定是否再跑一轮。
            for _, fn in self._taps:
                result = await _maybe(fn(scope, effect))
            # None = 收敛/终止；任意非 None 返回都表示还需要再跑一轮。
            if result is None:
                return None
        raise RuntimeError(f"hook {self.name!r} exceeded max_loops={self.max_loops}")

    async def _call_parallel(self, scope: RequestScope, effect: Effect | None) -> None:
        """PARALLEL：全执行、异常隔离、无返回值；异常隔离但留痕（R2-T04 记 trace）。"""
        async def guarded(name: str, fn: TapFn) -> None:
            """单个观察 tap：异常就地隔离，不影响其余 tap 与主流程，但记 trace 供诊断。"""
            try:
                await _maybe(fn(scope, effect))
            except Exception as exc:  # 观察类异常隔离  # noqa: BLE001
                # R2-T04：异常不抛穿但必须留痕，否则观察插件静默失效无法察觉。
                scope.trace.append(f"{self.name}:observe_error:{name}:{type(exc).__name__}")

        await asyncio.gather(*(guarded(name, fn) for name, fn in self._taps))

    async def _call_around(
        self, scope: RequestScope, effect: Effect | None, *,
        core: Callable[[RequestScope], Awaitable[Result | None]],
    ) -> Result | Decision | None:
        """AROUND：tap 收 next 回调，层层包裹中心调用。"""
        async def layer(index: int) -> Result | Decision | None:
            """洋葱第 index 层：越界则调中心 core，否则把下一层作为 next 交给 tap。"""
            if index >= len(self._taps):
                return await core(scope)

            async def nxt() -> Result | Decision | None:
                """交给当前 tap 的下一层回调。"""
                return await layer(index + 1)

            out = await _maybe(self._taps[index][1](nxt, scope, effect))
            # 只有 Decision/Result 是有效的包裹结果；其余形态按放行(None)处理，
            # 与 TapReturn 联合在此处的窄化保持一致，不让非法返回污染上游。
            if isinstance(out, (Decision, Result)):
                return out
            return None

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

    def apply(self, host: Host) -> None:
        """插件向宿主注册自己的扩展点 tap（由 Host.plugin 在装配期调用）。"""
        ...


class Host:
    """总线（Compiler 类比）：持有扩展点与 Scope 工厂，提供 R1 注入适配器。"""

    def __init__(self) -> None:
        """装配宿主：预定义 6 个标准扩展点，冻结前可 define/plugin。"""
        self._hooks: dict[str, Hook] = {}
        self._plugins: list[Plugin] = []
        # R4 §6 两表统一：host.registry 指向统一注册表（模型网关与 hook 总线同源）。
        # 缺省 None——R12 装配层或 ModelCapability.install() 注入；非 R2 自身职责。
        self.registry: ProviderRegistry | None = None
        self._frozen = False
        # scope 桥接：按 id(ctx) 关联（框架内部，不写进 ctx 或 scope.meta）
        self._scope_bridge: dict[int, RequestScope] = {}
        # 会话级降级目标（按 session_id 隔离，消除全局单槽串味）：call 阶段
        # （timeout/retry）发的信号经 settle 选级后写对应会话槽，
        # 供该会话下一次 Effect 的 dispatch 继承——R1 每个 Effect 新建 ctx/scope。
        self._sticky_targets: dict[str, str] = {}
        # 预定义 6 个标准扩展点（ARCH-v5 §2）；宿主可用 define 覆盖语义。
        for name, kind in _STANDARD_HOOKS.items():
            self._hooks[name] = Hook(name, kind)

    def define(self, name: str, kind: HookKind, *, max_loops: int = 100) -> Hook:
        """宿主定义扩展点并选定语义；v5 冻结后不可再定义。"""
        if self._frozen:
            raise RuntimeError("host frozen: cannot define hooks")
        if name in self._hooks and self._hooks[name].names:
            raise ValueError(f"hook already has taps: {name}")
        hook = Hook(name, kind, max_loops=max_loops)
        self._hooks[name] = hook
        return hook

    def hook(self, name: str) -> Hook:
        """取已定义扩展点（供插件 tap）；未定义抛 KeyError。"""
        if name not in self._hooks:
            raise KeyError(f"undefined hook: {name}")
        return self._hooks[name]

    def plugin(self, plugin: Plugin) -> Host:
        """注册插件：校验具名后调 apply(host)，返回 self 支持链式。"""
        if not getattr(plugin, "name", None):
            raise ValueError("plugin must have a name")
        plugin.apply(self)
        self._plugins.append(plugin)
        return self

    def freeze(self) -> Host:
        """冻结宿主：此后不可再 define 扩展点（装配期校验）。"""
        self._frozen = True
        return self

    def new_scope(self, session_id: str = "") -> RequestScope:
        """新建 scope，继承该会话的 sticky target（call 阶段降级的跨 Effect 接力）。

        参数：
            session_id: 会话标识；缺省空串（无会话上下文的独立 scope）。
        """
        return RequestScope(target=self._sticky_targets.get(session_id))

    @staticmethod
    def _session_of(ctx: HookContextArg) -> str:
        """从上下文提取 session_id（规则 6 例外：鸭子读取具名字段，兼容超集 ctx）。"""
        return str(getattr(ctx, "session_id", "") or "")

    def sticky_target_of(self, session_id: str) -> str | None:
        """读取会话的 sticky 降级目标（无记录返回 None）；诊断/测试用。"""
        return self._sticky_targets.get(session_id)

    def set_sticky_target(self, session_id: str, target: str) -> None:
        """显式设置会话的 sticky 降级目标（测试/装配用）。"""
        self._sticky_targets[session_id] = target

    async def dispatch(self, effect: Effect | None, ctx: HookContextArg) -> Decision:
        """R1 注入点：before_invoke → transform → settle。

        scope 按 id(ctx) 在 Host 内部桥接到 call 阶段（不污染 ctx 与 scope）。
        effect 作为参数直送各 tap 回调；ctx 为 RuntimeCtx（生产）或 HookCtx（桥接测试）。
        """
        scope = self.new_scope(self._session_of(ctx))
        self._scope_bridge[id(ctx)] = scope
        if hasattr(ctx, "degrade_target"):
            object.__setattr__(ctx, "degrade_target", scope.target)

        before = await self.hook("before_invoke").call(scope, effect)
        if isinstance(before, Decision) and before.action is not DecisionAction.CONTINUE:
            # 守卫短路：本轮没有 call，立即释放 dispatch 阶段占的资源（舱壁槽位）。
            # C2：同时清理 scope_bridge——短路后不会有 call 来 pop，残留会导致陈旧 scope
            # 与 id(ctx) 复用串味。settle 仍用已创建的 scope（不依赖 bridge）。
            scope.release_all()
            self._scope_bridge.pop(id(ctx), None)
            return await self._settle(scope, effect, ctx, before)
        transformed = await self.hook("transform").call(scope, effect, "")
        # transform 的链式产出回填 scope.trace（避免返回值被丢弃——预算提示等）
        if transformed:
            scope.trace.append(str(transformed))
        return await self._settle(scope, effect, ctx, Decision.continue_())

    def scope_of(self, ctx: HookContextArg) -> RequestScope | None:
        """取回 dispatch 创建的 scope（call 阶段接力读取 target）。"""
        return self._scope_bridge.get(id(ctx))

    def call(
        self, inner: Callable[[Effect | None, HookContextArg], Awaitable[Result]],
    ) -> Callable[[Effect | None, HookContextArg], Awaitable[Result]]:
        """R1 注入点：AROUND 包裹真实 Port 调用，成功/失败触发观察扩展点。

        inner 是 R4 ModelGateway.as_call()（签名确定的 `(effect, ctx) -> Result`），
        wrapped 保持同签名交回 Runner。调用结果 Result 写入 scope.last_result（观察插件只读）。
        around tap 的返回经 _coerce_result 归一为 Result（规则 6 唯一边界），
        消除"outcome 是 Any"——outcome 声明为 TapReturn 联合，coerce 后确定是 Result。
        call 阶段发的降级信号在返回前经 settle 选级，写 sticky target 供下个 Effect 继承。
        """
        async def wrapped(effect: Effect | None, ctx: HookContextArg) -> Result:
            """包装真实调用：跑 AROUND 链 → 观察 → settle 降级 → 释放 → 回传 Result。"""
            scope = self.scope_of(ctx) or self.new_scope(self._session_of(ctx))
            self._scope_bridge[id(ctx)] = scope

            async def core(s: RequestScope) -> Result:
                """洋葱中心：真正调用注入的 inner（R4 网关）。"""
                return await inner(effect, ctx)

            # 把 AROUND 链与观察钩子一并纳入保护：任一步抛错都进 except 统一发
            # invoke_error、跑降级并重抛，避免 port 异常裸穿出包装器。
            try:
                # around 扩展点的返回确定是 Result/Decision/None（不含 str），
                # call() 对外统一 TapReturn，故在此 cast 到窄联合再归一化（非 Any）。
                outcome = cast(
                    Result | Decision | None,
                    await self.hook("around_invoke").call(scope, effect, core=core),
                )
                resolved = _coerce_result(outcome)
                # core 必返 Result，归一后仍为 None 说明 around 链吞掉了结果（契约违背），
                # 抛错落入 except 按失败路径处理，而非静默返回空 Result 掩盖问题。
                if resolved is None:
                    raise RuntimeError("around_invoke produced no Result")
                scope.last_result = resolved
                await self.hook("after_invoke").call(scope, effect)
                # call 阶段的 timeout/retry 降级信号：返回前跑 settle 更新 sticky target
                if any(s.kind == SignalKind.DEGRADE for s in scope.signals):
                    await self._settle(scope, effect, ctx, Decision.continue_())
                scope.release_all()
                self._scope_bridge.pop(id(ctx), None)
                return resolved
            except Exception:
                err = Result.failure("INVOKE_ERROR", "port raised")
                scope.last_result = err
                await self.hook("invoke_error").call(scope, effect)
                # 与成功路径对称：失败阶段（如 invoke_error 钩子）发出的降级信号也要 settle，
                # 保证两条路径对 sticky target 的更新行为一致。
                if any(s.kind == SignalKind.DEGRADE for s in scope.signals):
                    await self._settle(scope, effect, ctx, Decision.continue_())
                scope.release_all()
                self._scope_bridge.pop(id(ctx), None)
                raise

        return wrapped

    async def _settle(
        self, scope: RequestScope, effect: Effect | None, ctx: HookContextArg, base: Decision,
    ) -> Decision:
        """settle 扩展点：有降级信号时由降级插件选 target 并覆盖占位。"""
        if not any(s.kind == SignalKind.DEGRADE for s in scope.signals):
            return base
        verdict = await self.hook("settle").call(scope, effect)
        # 只有 settle 插件给出带 target 的 Decision 才真正改写降级目标；否则保留
        # 守卫原裁决（base），不让观察/兜底插件误覆盖上游决定。
        if isinstance(verdict, Decision) and verdict.target:
            scope.target = verdict.target
            # verdict.value 至少是 Result/None；用唯一边界归一化后写入具名字段（规则 6）。
            scope.value = _coerce_result(cast(Result | Decision | None, verdict.value))
            # 跨 Effect 继承（修 B1）：按 session_id 隔离，避免全局单槽串味。
            self._sticky_targets[self._session_of(ctx)] = verdict.target
            if hasattr(ctx, "degrade_target"):
                object.__setattr__(ctx, "degrade_target", verdict.target)
            return verdict
        return base
