"""默认 LLM 适配器：OpenAI 兼容端点（任何厂商都能通过替换本实现接入）。

适配器职责只有"厂商格式 ⇄ 框架 DTO 的双向翻译"；不持有会话状态（M3 红线）。
换 LLM 的全部工作量 = 实现 LLMPort.complete + 填对 tool_calls / usage + 改一行注册。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TypedDict, cast

import httpx

from runtime import DynamicValue, Usage

from ..dto import LLMRequest, LLMResponse, ToolCall
from ..registry import PortCtx

__all__ = ["OpenAICompatLLM"]


class _VendorUsage(TypedDict, total=False):
    """OpenAI 响应里的 usage 对象（字段按需出现）。"""

    prompt_tokens: int  # 输入 token 数
    completion_tokens: int  # 输出 token 数


class _VendorFunctionCall(TypedDict, total=False):
    """工具调用里的 function 对象。"""

    name: str  # 工具名
    arguments: str  # JSON 编码的参数字符串


class _VendorToolCall(TypedDict, total=False):
    """OpenAI 响应里的单个 tool_call。"""

    id: str  # 调用 id
    function: _VendorFunctionCall  # 函数体


class _VendorMessage(TypedDict, total=False):
    """OpenAI 响应 choices[0].message 对象。"""

    content: str | None  # 文本响应
    tool_calls: list[_VendorToolCall]  # 工具调用列表


class _VendorResponse(TypedDict, total=False):
    """chat/completions 响应顶层结构（只声明框架消费的字段）。"""

    choices: list[dict[str, DynamicValue]]  # 候选列表，取 [0].message
    message: _VendorMessage  # 直接 message（非标准，兼容部分实现）
    usage: _VendorUsage  # token 用量
    model: str  # 实际模型名


class OpenAICompatLLM:
    """OpenAI 兼容 HTTP 端点适配器；只做厂商格式 ⇄ 框架 DTO 的翻译。"""

    name = "openai-compat-llm"  # 适配器标识

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        client: httpx.AsyncClient | None = None,
        cost_per_1k_tokens: float = 0.0,
    ) -> None:
        """装配端点参数；client 可注入以便测试时替换真实 HTTP 调用。"""
        self._base_url = base_url.rstrip("/")  # API 根地址
        self._api_key = api_key  # Bearer token
        self._model = model  # 默认模型名
        self._client = client or httpx.AsyncClient(timeout=30.0)  # HTTP 客户端
        self._cost_per_1k = cost_per_1k_tokens  # 每千 token 定价，用于折算成本

    @staticmethod
    def _to_vendor_messages(req: LLMRequest) -> list[dict[str, DynamicValue]]:
        """把框架 Message 列表转成厂商消息格式（去掉框架专用的 tool_calls 字段）。"""
        out: list[dict[str, DynamicValue]] = []
        for message in req.messages:
            item: dict[str, DynamicValue] = message.to_dict()
            item.pop("tool_calls", None)
            out.append(item)
        return out

    @staticmethod
    def _to_vendor_tools(req: LLMRequest) -> list[dict[str, DynamicValue]]:
        """把工具目录转成 OpenAI function-calling 的 tools 格式。"""
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
        """调用 chat/completions，把响应解析为 LLMResponse（含 tool_calls 与 usage）。"""
        payload: dict[str, DynamicValue] = {
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
        # 外部 JSON → 内部 TypedDict：在协议边界收窄，后续访问都有精确类型
        raw = cast(_VendorResponse, response.json())
        choices = raw.get("choices") or []
        message = cast(
            _VendorMessage,
            choices[0]["message"] if choices else raw.get("message") or {},
        )

        # 逐个解析厂商 tool_calls：arguments 是 JSON 字符串，解析失败按空字典
        tool_calls = []
        for item in message.get("tool_calls") or []:
            fn = item.get("function") or {}
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
