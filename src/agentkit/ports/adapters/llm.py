"""默认 LLM 适配器：OpenAI 兼容端点（任何厂商都能通过替换本实现接入）。

适配器职责只有"厂商格式 ⇄ 框架 DTO 的双向翻译"；不持有会话状态（M3 红线）。
换 LLM 的全部工作量 = 实现 LLMPort.complete + 填对 tool_calls / usage + 改一行注册。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from agentkit.runtime import Usage

from ..dto import LLMRequest, LLMResponse, ToolCall
from ..registry import PortCtx

__all__ = ["OpenAICompatLLM"]


class OpenAICompatLLM:
    name = "openai-compat-llm"

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        client: httpx.AsyncClient | None = None,
        cost_per_1k_tokens: float = 0.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._cost_per_1k = cost_per_1k_tokens

    @staticmethod
    def _to_vendor_messages(req: LLMRequest) -> list[dict[str, Any]]:
        out = []
        for message in req.messages:
            item: dict[str, Any] = message.to_dict()
            item.pop("tool_calls", None)
            out.append(item)
        return out

    @staticmethod
    def _to_vendor_tools(req: LLMRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters_schema,
                },
            }
            for spec in (req.tools_catalog or [])
        ]

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": req.model or self._model,
            "messages": self._to_vendor_messages(req),
        }
        tools = self._to_vendor_tools(req)
        if tools:
            payload["tools"] = tools
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = await self._client.post(
            f"{self._base_url}/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()
        raw = response.json()
        message = raw["choices"][0]["message"]

        tool_calls = []
        for item in message.get("tool_calls") or []:
            fn = item.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(call_id=item.get("id", ""), name=fn.get("name", ""), args=args)
            )

        usage_raw = raw.get("usage") or {}
        prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
        completion_tokens = int(usage_raw.get("completion_tokens", 0))
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=round(
                    (prompt_tokens + completion_tokens) / 1000 * self._cost_per_1k, 8
                ),
            ),
            model=raw.get("model", req.model or self._model),
        )

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """P0 最小实现：非流式端点退化为一次性 yield（真实流式可整体替换本适配器）。"""
        response = await self.complete(req, ctx)
        if response.content:
            yield response.content
