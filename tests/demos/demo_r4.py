"""R4 独立 demo：模型路由 / 缓存 / 结构化修复 / 成本归因 / 换后端。

不联网（RecordingLLM fake），全程确定性可回放。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import HookBus, LLMRequest, LLMResponse, PortCtx, Usage  # noqa: E402

from harness.model import (  # noqa: E402
    CostGuard,
    ModelCapability,
    ModelGateway,
    RecordingLLM,
    RuleRouter,
    StructuredOutputSpec,
    build_cache_key,
    validate_json_schema,
)


def _req() -> LLMRequest:
    """最小请求。"""
    return LLMRequest(messages=[{"role": "user", "content": "hi"}])


def _resp(content: str, cost: float = 0.01) -> LLMResponse:
    """最小响应（带成本）。"""
    return LLMResponse(content=content, usage=Usage(prompt_tokens=10, completion_tokens=5, cost_usd=cost))


def _ctx() -> PortCtx:
    """最小 PortCtx。"""
    return PortCtx(session_id="s4", run_id="r1", meta={"user_id": "u1"})


SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}, "age": {"type": "integer", "minimum": 0}},
}


async def main() -> None:
    """跑通 R4 全流程并打印摘要。"""
    # 1) 缓存键含版本
    r = _req()
    assert build_cache_key(_req(), prompt_version="v1").value != build_cache_key(_req(), prompt_version="v2").value

    # 2) 缓存命中省调用
    llm = RecordingLLM([_resp("cached")])
    gw = ModelGateway(llm=llm)
    await gw.complete(r, _ctx())
    await gw.complete(r, _ctx())
    assert llm.call_count == 1

    # 3) 结构化修复：非法 → 合法
    llm2 = RecordingLLM([_resp('{"age": -1}'), _resp('{"name": "Zoe", "age": 20}')])
    gw2 = ModelGateway(llm=llm2, spec=StructuredOutputSpec(schema=SCHEMA))
    out = await gw2.complete(r, _ctx())
    assert "Zoe" in (out.content or "") and llm2.call_count == 2

    # 4) 成本归因
    assert gw2.ledger.total() > 0

    # 5) 成本闸
    guard = CostGuard(max_cost_usd=0.001)
    gw3 = ModelGateway(llm=RecordingLLM([_resp("x", cost=0.5)]), cost_guard=guard)
    try:
        await gw3.complete(r, _ctx())
    except ValueError:
        pass
    else:
        raise AssertionError("cost guard should block")

    # 6) 路由 + 能力装配
    router = RuleRouter(routes={"code": "gpt-4"}, registered={"gpt-4", "default"})
    cap = ModelCapability(llm=RecordingLLM([]), router=router)
    bus = HookBus()
    cap.install(bus)
    assert bus.registry.has("llm")

    # 7) schema 校验独立
    assert validate_json_schema('{"name": "A"}', SCHEMA) == []

    print("[R4] PASS: 缓存键/命中 / 结构化修复 / 成本归因+硬闸 / 路由 / 能力装配 / schema 校验")


if __name__ == "__main__":
    asyncio.run(main())
