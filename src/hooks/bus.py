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
    Decision, DecisionAction, Hook, HookCtx, HookMode, HookBusError, Registry,
)

__all__ = ["HookBus", "DispatchResult"]


@dataclass(frozen=True)
class DispatchResult:
    """一次事件分发的打包结果。"""
    event: str
    decision: Decision
    trace: tuple[str, ...]
    aborted: bool = False


@dataclass
class _Registered:
    """已注册的 hook 条目。"""
    hook: Hook
    priority: int
    order: int  # 注册序号，保证同优先级稳定顺序
    mode: HookMode


async def _maybe(value: DynamicValue) -> DynamicValue:
    """统一等待：hook 既可同步返回 Decision 也可 awaitable。"""
    return await value if inspect.isawaitable(value) else value


class HookBus:
    """钩子总线：注册 + 编排 + 分发。对外唯一入口是 on()。"""
    def __init__(self) -> None:
        """初始化空总线：钩子表按事件名分组，注册表独立可替换。"""
        self._hooks: dict[str, list[_Registered]] = defaultdict(list)
        self._order = 0
        self._frozen = False
        self.registry = Registry()

    def on(
        self, hook: Hook, *, events: tuple[str, ...] | None = None,
        priority: int | None = None, mode: HookMode | None = None,
    ) -> HookBus:
        """注册 hook（唯一扩展入口）；可覆盖声明的 event/priority/mode。"""
        if self._frozen:
            raise HookBusError("hook bus is frozen")
        name = getattr(hook, "name", None)
        if not name:
            raise HookBusError(f"hook must have a non-empty name: {hook!r}")
        h_events = events if events is not None else tuple(getattr(hook, "events", ()))
        h_priority = priority if priority is not None else int(getattr(hook, "priority", 100))
        h_mode = mode if mode is not None else HookMode(getattr(hook, "mode", HookMode.GUARD))
        entry = _Registered(hook=hook, priority=h_priority, order=self._order, mode=h_mode)
        self._order += 1
        for event in h_events:
            # 用浅包装让 mode 覆盖生效（不改用户对象）
            self._hooks[event].append(entry)
        return self

    def freeze(self, required_ports: tuple[str, ...] = ()) -> HookBus:
        """装配期校验：留名、依赖端口齐备，然后冻结。"""
        seen: set[int] = set()
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
        """给 R1 Runner 注入的上下文工厂：产出 HookCtx。"""
        from runtime import Budgets

        def factory(effect: Effect, session: Session) -> HookCtx:
            """按 effect 产出 HookCtx。"""
            budgets = getattr(session.state, "budgets", Budgets())
            return HookCtx(
                effect=effect, state=session.state, session_id=session.session_id,
                event="", budget_left=budgets,
            )
        return factory

    async def emit(self, event: str, ctx: HookCtx) -> Decision:
        """对某事件分发全部 hook，返回合并后的 Decision。"""
        ctx.event = event
        entries = sorted(
            self._hooks.get(event, []), key=lambda e: (e.priority, e.order)
        )
        final = Decision.continue_()

        # 1) observe：并行执行、异常隔离、返回忽略
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

        # 3) transform：串行链式改写 payload/context
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
        """观察 hook：异常隔离，只记 trace，不影响主流程。"""
        try:
            await _maybe(hook(ctx))
            ctx.trace.append(f"observe:{hook.name}")
        except Exception as exc:  # 观察者故障绝不影响主流程  # noqa: BLE001
            ctx.trace.append(f"observe:{hook.name}:error:{type(exc).__name__}")

    async def _call_hook(self, hook: Hook, ctx: HookCtx, mode: HookMode) -> Decision:
        """调用单个 hook，异常折叠为 continue（不打断主流程）。"""
        try:
            d = await _maybe(hook(ctx))
            ctx.trace.append(f"{mode.value}:{hook.name}")
            return d if isinstance(d, Decision) else Decision.continue_()
        except Exception as exc:  # noqa: BLE001
            ctx.trace.append(f"hook:{hook.name}:error:{type(exc).__name__}")
            return Decision.continue_()

    async def _around(self, entries: list[_Registered], ctx: HookCtx) -> Decision:
        """洋葱调度：around hook 通过 ctx.meta['next'] 拿下一层回调，中心调 ctx.invoke。"""
        async def core() -> Decision:
            """洋葱中心：调用 ctx.invoke（R1 注入的 Port 调用），无 invoke 则放行。"""
            if ctx.invoke is None:
                return Decision.continue_()
            await ctx.invoke(ctx.effect, ctx)
            return Decision.continue_()

        async def layer(index: int) -> Decision:
            """递归执行第 index 层 around hook，末层进入 core。"""
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
                ctx.meta.pop("next", None)

        return await layer(0)

    @staticmethod
    def _merge(base: Decision, new: Decision) -> Decision:
        """合并两个 Decision：后者覆盖前者（transform 链式改写的累加在 ctx 上）。"""
        if new.action is DecisionAction.CONTINUE and not (
            new.payload_patch or new.context_inject or new.state_patch
        ):
            return base
        return new

    @staticmethod
    def _apply_rewrite(ctx: HookCtx, d: Decision) -> None:
        """把 rewrite 决策应用到 ctx（payload_patch/context_inject）。"""
        if d.payload_patch:
            merged = dict(ctx.effect.payload)
            merged.update(d.payload_patch)
            from dataclasses import replace
            from runtime import EffectPayload
            from typing import cast
            ctx.effect = replace(ctx.effect, payload=cast(EffectPayload, merged))
        if d.context_inject:
            ctx.meta.setdefault("injected_context", []).append(d.context_inject)
