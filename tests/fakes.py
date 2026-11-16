"""全仓库共享的 fake 外部依赖（CL-2：每个模块不依赖真实服务即可闭环）。

fake 实现携带脚本状态是允许的——红线只约束 M1 的 Program 与 M3 的适配器。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


def run(coro: Any) -> Any:
    """同步测试里跑 async 用例（不引入 pytest-asyncio 依赖）。"""
    return asyncio.run(coro)

from runtime import EffectKind, Result, Usage


def obj(**kwargs: Any) -> SimpleNamespace:
    """轻量 DTO 替身：属性访问 + canonical() 可确定性序列化。"""
    return SimpleNamespace(**kwargs)


def llm_response(content: str = "", tool_calls: list[dict] | None = None) -> SimpleNamespace:
    return obj(content=content, tool_calls=tool_calls or [], model="fake-model")


class FakeDispatch:
    """最小 dispatch：只留名、不判决 → Runner 兜底调 call。"""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def __call__(self, effect: Any, ctx: Any) -> None:
        self.seen.append(effect.kind)
        ctx.trace.append("fake-dispatch")
        return None


class ScriptedLLM:
    """按调用顺序返回预设 LLM 响应（内容 / tool_calls）。"""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def next(self) -> SimpleNamespace:
        item = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return item


class FakeCall:
    """按 Effect.kind 产出确定性 Result；llm 走脚本，其余走固定值。"""

    def __init__(
        self,
        responses: list[SimpleNamespace] | None = None,
        *,
        cost_usd: float = 0.01,
        fail_kind: EffectKind | None = None,
    ) -> None:
        self.llm = ScriptedLLM(responses or [llm_response("ok")])
        self.cost_usd = cost_usd
        self.fail_kind = fail_kind
        self.seen: list[EffectKind] = []

    async def __call__(self, effect: Any, ctx: Any) -> Result:
        self.seen.append(effect.kind)
        if self.fail_kind is not None and effect.kind is self.fail_kind:
            return Result.failure("FAKE_PORT_ERROR", "injected failure", retryable=True)
        if effect.kind is EffectKind.LLM:
            return Result.success(self.llm.next(), Usage(10, 5, self.cost_usd))
        if effect.kind is EffectKind.TOOL:
            call = effect.payload.get("call") or {}
            call_id = getattr(call, "call_id", None) or call.get("call_id", "c0")
            return Result.success(obj(call_id=call_id, ok=True, value="tool-ok", error=None, meta={}))
        if effect.kind is EffectKind.SKILL:
            return Result.success("skill body")
        if effect.kind is EffectKind.WAIT_HUMAN:
            return Result.success("approved")
        return Result.success(None)


class FakeEventStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict] = {}

    def save(self, session_id: str, snapshot: dict) -> None:
        self.snapshots[session_id] = snapshot

    def load(self, session_id: str) -> dict | None:
        return self.snapshots.get(session_id)
