"""R4 测试夹具：fake LLM / 样本 schema / 请求构造器（模块内闭环，不联网）。"""
from __future__ import annotations

from compose import LLMRequest, LLMResponse, PortCtx, Usage


def sample_schema() -> dict:
    """最小 JSON Schema：对象含 name(string,必填) 与 age(integer)。"""
    return {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer", "minimum": 0},
        },
    }


def resp(content: str, model: str = "m1", cost: float = 0.01) -> LLMResponse:
    """构造 LLM 响应，带成本元数据。"""
    return LLMResponse(
        content=content, model=model,
        usage=Usage(prompt_tokens=10, completion_tokens=5, cost_usd=cost),
    )


def req(messages: list[dict] | None = None, model: str | None = None) -> LLMRequest:
    """构造 LLM 请求。"""
    return LLMRequest(
        messages=messages or [{"role": "user", "content": "hi"}], model=model
    )


def pctx(run_id: str = "run1", user_id: str = "u1") -> PortCtx:
    """构造 PortCtx。"""
    return PortCtx(session_id="s4", run_id=run_id, meta={"user_id": user_id})
