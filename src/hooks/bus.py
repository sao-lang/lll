"""R2 HookBus：唯一扩展点的编排器。

按 event 选 hook → matcher 过滤 → 按 mode 编排：
- observe：并行执行、异常隔离、返回忽略
- guard：串行按 priority，首个非 continue 短路
- transform：串行链式改写 payload/context
- around：洋葱包裹 invoke，拿 next 回调

总线自己不碰 IO：洋葱中心调用 ctx.invoke（由 R1 Runner 注入）。
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from runtime import DynamicValue, Effect, Session

from .types import (
    Decision,
    DecisionAction,
    Hook,
    HookBusError,
    HookCtx,
    HookMode,
    Registry,
)

__all__ = ["DispatchResult", "HookBus"]


@dataclass(frozen=True)
class DispatchResult:
    """一次事件分发的打包结果（emit 目前直接返回 Decision，此类供外层封装用）。

    字段：
        event: 事件名。decision: 合并后的裁决。
        trace: 执行轨迹。aborted: 是否短路终止。
    """

    event: str
    decision: Decision
    trace: tuple[str, ...]
    aborted: bool = False


@dataclass
class _Registered:
    """已注册的 hook 条目（一次注册可对应多个事件，各自持有一条）。

    字段：
        hook: 插件实例。priority: 生效优先级。
        order: 注册序号，保证同优先级稳定顺序。
        mode: 生效的执行语义（可被 on() 覆盖）。
    """

    hook: Hook
    priority: int
    order: int  # 注册序号，保证同优先级稳定顺序
    mode: HookMode


async def _maybe(value: DynamicValue) -> DynamicValue:
    """统一等待：hook 既可同步返回 Decision 也可 awaitable。

    参数：
        value: 同步值或 Awaitable。
    返回：
        解包后的实际值。
    """
    return await value if inspect.isawaitable(value) else value


class HookBus:
    """钩子总线：注册 + 编排 + 分发。对外唯一入口是 on()。"""

    def __init__(self) -> None:
        """初始化空总线：钩子表按事件名分组，注册表独立可替换。"""
        self._hooks: dict[str, list[_Registered]] = defaultdict(list)  # event → 条目列表
        self._order = 0  # 全局注册序号（稳定排序用）
        self._frozen = False  # 冻结标志（装配完成后置 True）
        self.registry = Registry()  # Provider 注册表

    def on(
        self,
        hook: Hook,
        *,
        events: tuple[str, ...] | None = None,
        priority: int | None = None,
        mode: HookMode | None = None,
    ) -> HookBus:
        """注册 hook（唯一扩展入口）；可覆盖声明的 event/priority/mode。

        参数：
            hook: 插件实例（必须有非空 name）。
            events: 覆盖 hook.events；None 表示用插件声明值。
            priority: 覆盖 hook.priority；None 表示用插件声明值（缺省 100）。
            mode: 覆盖 hook.mode；None 表示用插件声明值（缺省 GUARD）。
        返回：
            self（链式调用）。
        异常：
            HookBusError: 已冻结或 hook 无 name。
        """
        if self._frozen:
            raise HookBusError("hook bus is frozen")
        name = getattr(hook, "name", None)
        if not name:
            raise HookBusError(f"hook must have a non-empty name: {hook!r}")
        # 覆盖值优先，否则回退到插件自身声明（用 getattr 容错鸭子类型插件）。
        h_events = events if events is not None else tuple(getattr(hook, "events", ()))
        h_priority = (
            priority if priority is not None else int(getattr(hook, "priority", 100))
        )
        h_mode = (
            mode
            if mode is not None
            else HookMode(getattr(hook, "mode", HookMode.GUARD))
        )
        entry = _Registered(
            hook=hook, priority=h_priority, order=self._order, mode=h_mode
        )
        self._order += 1
        for event in h_events:
            # 同一条目登记到每个订阅事件；不改用户插件对象（mode 覆盖只体现在 entry）。
            self._hooks[event].append(entry)
        return self

    def freeze(self, required_ports: tuple[str, ...] = ()) -> HookBus:
        """装配期校验：留名、依赖端口齐备，然后冻结。

        参数：
            required_ports: 预留参数（hook 自带 requires_ports，此处暂不额外要求）。
        返回：
            self（已冻结）。
        异常：
            HookBusError: 某 hook 依赖的 port 未注册。
        """
        seen: set[int] = set()  # 用 id 去重：同一插件可能订阅多个事件
        for entries in self._hooks.values():
            for entry in entries:
                if id(entry.hook) in seen:
                    continue
                seen.add(id(entry.hook))
                for port in getattr(entry.hook, "requires_ports", ()):
                    if not self.registry.has(port):
                        raise HookBusError(
                            f"hook {entry.hook.name} requires port {port!r}, not registered"
                        )
        self.registry.freeze()
        self._frozen = True
        return self

    def ctx_factory(self) -> Callable[[Effect, Session], HookCtx]:
        """给 R1 Runner 注入的上下文工厂：产出 HookCtx。

        返回：
            (effect, session) → HookCtx 的工厂函数。
        """
        def factory(effect: Effect, session: Session) -> HookCtx:
            """按 effect 产出 HookCtx。

            参数：
                effect: 当前 Effect。session: 当前会话。
            返回：
                新 HookCtx（budget_left 取 session.state.budgets）。
            """
            budgets = session.state.budgets  # AgentState 是确定类型，budgets 是具名字段
            return HookCtx(
                effect=effect,
                state=session.state,
                session_id=session.session_id,
                event="",
                budget_left=budgets,
            )

        return factory

    async def emit(self, event: str, ctx: HookCtx) -> Decision:
        """对某事件分发全部 hook，返回合并后的 Decision。

        编排顺序：① observe（并行）→ ② guard（串行可短路）→
        ③ transform（串行改写）→ ④ around（洋葱包裹 invoke）。

        参数：
            event: 事件名。ctx: 钩子上下文（event 字段会被覆写）。
        返回：
            合并后的 Decision；guard 短路时立即返回。
        """
        ctx.event = event
        entries = sorted(
            self._hooks.get(event, []), key=lambda e: (e.priority, e.order)
        )  # 优先级优先，同优先级按注册序
        final = Decision.continue_()

        # 1) observe：并行执行、异常隔离、返回忽略（不影响 final）
        observers = [e for e in entries if e.mode is HookMode.OBSERVE]
        if observers:
            await asyncio.gather(*(self._observe(e.hook, ctx) for e in observers))

        # 2) guard：串行按优先级，首个非 continue 短路
        guards = [e for e in entries if e.mode is HookMode.GUARD]
        for entry in guards:
            d = await self._call_hook(entry.hook, ctx, entry.mode)
            final = self._merge(final, d)
            if d.action is not DecisionAction.CONTINUE:
                return final  # 短路：deny/ask/abort/retry/degrade/fallback 直接返回

        # 3) transform：串行链式改写 payload/context（改写立即作用到 ctx）
        transforms = [e for e in entries if e.mode is HookMode.TRANSFORM]
        for entry in transforms:
            d = await self._call_hook(entry.hook, ctx, entry.mode)
            final = self._merge(final, d)
            self._apply_rewrite(ctx, d)

        # 4) around：洋葱包裹，核心是 ctx.invoke
        arounds = [e for e in entries if e.mode is HookMode.AROUND]
        if arounds:
            d = await self._around(arounds, ctx)
            final = self._merge(final, d)
        return final

    async def _observe(self, hook: Hook, ctx: HookCtx) -> None:
        """观察 hook：异常隔离，只记 trace，不影响主流程。

        参数：
            hook: 观察插件。ctx: 钩子上下文。
        """
        try:
            await _maybe(hook(ctx))
            ctx.trace.append(f"observe:{hook.name}")
        except Exception as exc:  # 观察者故障绝不影响主流程  # noqa: BLE001
            ctx.trace.append(f"observe:{hook.name}:error:{type(exc).__name__}")

    async def _call_hook(self, hook: Hook, ctx: HookCtx, mode: HookMode) -> Decision:
        """调用单个 hook，异常折叠为 continue（不打断主流程）。

        参数：
            hook: 插件。ctx: 上下文。mode: 执行语义（仅用于 trace 标记）。
        返回：
            插件返回的 Decision；非 Decision 或异常时返回 continue。
        """
        try:
            d = await _maybe(hook(ctx))
            ctx.trace.append(f"{mode.value}:{hook.name}")
            return d if isinstance(d, Decision) else Decision.continue_()
        except Exception as exc:  # noqa: BLE001
            ctx.trace.append(f"hook:{hook.name}:error:{type(exc).__name__}")
            return Decision.continue_()

    async def _around(self, entries: list[_Registered], ctx: HookCtx) -> Decision:
        """洋葱调度：around hook 通过 ctx.meta['next'] 拿下一层回调，中心调 ctx.invoke。

        参数：
            entries: 本事件的 around 条目（已排序）。ctx: 钩子上下文。
        返回：
            最外层返回的 Decision。
        """

        async def core() -> Decision:
            """洋葱中心：调用 ctx.invoke（R1 注入的 Port 调用），把 Result 装进 Decision.value。

            关键：必须把 invoke 的 Result 回填到 Decision，让 R1 直接采用、不再重复调用
            Port（否则插件在 around 里改的 effect 会被 R1 第二次原始调用覆盖，且副作用执行两次）。
            """
            if ctx.invoke is None:
                return Decision.continue_()  # 无 invoke：只能放行
            result = await ctx.invoke(ctx.effect, ctx)
            from runtime import Result as _Result

            if isinstance(result, _Result):
                # 用 Decision.value（统一裁决载体字段）装 Result，R1 靠它避免重复调 Port
                return Decision(action=DecisionAction.CONTINUE, value=result)
            return Decision.continue_()

        async def layer(index: int) -> Decision:
            """递归执行第 index 层 around hook，末层进入 core。

            参数：
                index: 当前层下标；>= len(entries) 时进入洋葱中心。
            返回：
                该层（或其内层）的 Decision。
            """
            if index >= len(entries):
                return await core()
            hook = entries[index].hook

            async def nxt() -> Decision:
                """下一层回调，供 around hook 通过 ctx.meta['next'] 调用。"""
                return await layer(index + 1)

            # around hook 统一签名 __call__(ctx)，从 ctx.meta 取 next 回调
            ctx.meta["next"] = nxt
            try:
                d = await _maybe(hook(ctx))
                ctx.trace.append(f"around:{hook.name}")
                if isinstance(d, Decision) and d.action is not DecisionAction.CONTINUE:
                    return d  # 短路（如 fallback/deny）
                return await nxt()  # 未显式短路则继续下一层
            except Exception as exc:  # noqa: BLE001
                ctx.trace.append(f"around:{hook.name}:error:{type(exc).__name__}")
                return Decision.continue_()
            finally:
                ctx.meta.pop("next", None)  # 清理，避免回调泄漏到后续 hook

        return await layer(0)  # 从最外层开始

    @staticmethod
    def _merge(base: Decision, new: Decision) -> Decision:
        """合并两个 Decision：后者覆盖前者（transform 链式改写累加在 ctx 上）。

        带 value（around 中心 invoke 的 Result）的 continue 必须保留——R1 靠它避免重复调 Port。

        参数：
            base: 累计裁决。new: 本 hook 裁决。
        返回：
            合并后的裁决（new 为空操作 continue 时保留 base）。
        """
        if new.action is DecisionAction.CONTINUE and not (
            new.payload_patch
            or new.context_inject
            or new.state_patch
            or new.value is not None
        ):
            return base
        return new

    @staticmethod
    def _apply_rewrite(ctx: HookCtx, d: Decision) -> None:
        """把 rewrite 决策应用到 ctx（payload_patch → effect.payload，context_inject → meta）。

        参数：
            ctx: 上下文（原地改写）。d: rewrite 裁决。
        """
        if d.payload_patch:
            merged = dict(ctx.effect.payload)  # 复制只读 payload
            merged.update(d.payload_patch)
            from dataclasses import replace
            from typing import cast

            from runtime import EffectPayload

            ctx.effect = replace(ctx.effect, payload=cast(EffectPayload, merged))
        if d.context_inject:
            # 注入文本累加成列表，多次 rewrite 不互相覆盖。
            ctx.meta.setdefault("injected_context", []).append(d.context_inject)
