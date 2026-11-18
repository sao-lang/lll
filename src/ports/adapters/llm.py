"""OpenAI-compatible LLM 默认适配器（R4 §6.1）。

框架唯一的 LLMPort 默认实现：真发 HTTP 请求、真解析 SSE，不联网由注入 fake client 保证。
不属于已实现模块契约（R1~R4 锁定 runtime/hooks/harness），新增为兼容扩展。

设计约束（R4 §6.1）：
- complete(): POST /chat/completions，解析 content/usage，写 last_usage。
- stream(): 带 stream_options.include_usage，逐块解析 SSE，末块 usage 写 last_usage。
- 错误折叠为 LLMResponse，不抛裸异常；api_key 只进 Authorization 头。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from hooks import LLMRequest, LLMResponse, PortCtx, ToolCall, Usage

__all__ = ["OpenAICompatLLM"]

_SSE_DONE = "[DONE]"


class OpenAICompatLLM:
    """OpenAI-compatible HTTP 后端（需 base_url + api_key + model）。

    属性：
        name: 适配器名。last_usage: 最近一次调用的结算 usage（LLMPort 协议成员）。
    """

    name = "openai_compat"
    last_usage: Usage | None = None

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """装配适配器。

        参数：
            base_url: OpenAI-compatible 端点根（如 https://api.x/v1）。
            api_key: 密钥，仅进 Authorization 头，不进 trace/日志/快照。
            model: 默认模型名（请求未覆盖时使用）。
            client: 可注入 httpx 客户端（测试用 MockTransport 避免真联网）；缺省新建。
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client
        self.last_usage = None

    def _headers(self) -> dict[str, str]:
        """构造请求头；密钥只在此处进入 Authorization（K3 凭据管理）。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, req: LLMRequest, *, stream: bool) -> dict[str, object]:
        """把 LLMPort 请求翻译为 OpenAI-compatible JSON body。"""
        body: dict[str, object] = {
            "model": req.model or self._model,
            "messages": [dict(m) for m in req.messages],
            "stream": stream,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.tools_catalog:
            body["tools"] = [
                {"type": "function", "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters_schema,
                }}
                for t in req.tools_catalog
            ]
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _usage_from(self, raw: object) -> Usage:
        """从 vendor usage JSON 提取具名 Usage；字段缺失填 0（真实账单缺失如实为 0）。"""
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(raw.get("completion_tokens", 0) or 0),
            cost_usd=float(raw.get("cost_usd", 0.0) or 0.0),
        )

    def _tool_calls_from(self, raw: object) -> list[ToolCall]:
        """解析 vendor tool_calls（缺省空）。"""
        calls: list[ToolCall] = []
        if not isinstance(raw, list):
            return calls
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_fn = item.get("function")
            fn: dict[str, object] = raw_fn if isinstance(raw_fn, dict) else {}
            args: object = fn.get("arguments", "{}")
            parsed = json.loads(args) if isinstance(args, str) else args
            calls.append(ToolCall(
                call_id=str(item.get("id", "")),
                name=str(fn.get("name", "")),
                args=parsed if isinstance(parsed, dict) else {},
            ))
        return calls

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次性补全：真 POST，解析响应；错误折叠为零 usage 响应，不抛裸异常。"""
        try:
            client = self._client or httpx.AsyncClient()
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(req, stream=False),
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", []) if isinstance(data, dict) else []
            first = choices[0] if choices and isinstance(choices[0], dict) else {}
            message = first.get("message", {}) if isinstance(first.get("message"), dict) else {}
            usage = self._usage_from(data.get("usage") if isinstance(data, dict) else None)
            self.last_usage = usage
            return LLMResponse(
                content=message.get("content"),
                tool_calls=self._tool_calls_from(message.get("tool_calls")),
                usage=usage,
                model=str(data.get("model", self._model)) if isinstance(data, dict) else self._model,
            )
        except httpx.HTTPStatusError as exc:
            # HTTP 错误：折叠为带 error 的 LLMResponse（不抛裸异常），R3 据此重试/熔断。
            # code 用纯 HTTP 状态码（如 "500"），与 R3 normalize_code(^5\d\d$) 对齐。
            code = str(exc.response.status_code)
            self.last_usage = Usage()
            return LLMResponse(content="", tool_calls=[], usage=Usage(),
                               model=self._model, error=f"{code}: {exc}")
        except Exception as exc:
            # 网络/解析错误：归一到 "NETWORK_ERROR" 语义族（可重试、可熔断）。
            self.last_usage = Usage()
            return LLMResponse(content="", tool_calls=[], usage=Usage(),
                               model=self._model, error=f"NETWORK_ERROR: {exc}")

    def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """流式补全：逐块解析 SSE data 行的 delta.content；末块 usage 写 last_usage。

        PEP525 签名（调用即得 AsyncIterator，无需 await）。取消由消费方 aclose() 传播，
        try/finally 关闭底层响应。
        """
        return self._stream_impl(req, ctx)

    async def _stream_impl(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """异步生成器实现体；SSE 断流与取消均在 finally 关闭连接。"""
        client = self._client or httpx.AsyncClient()
        acquired = self._client is None  # 自建客户端需自行关闭
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(req, stream=True),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == _SSE_DONE:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    # 末块：usage 无 choices，写入结算口后不 yield 文本。
                    usage = data.get("usage")
                    if usage is not None:
                        self.last_usage = self._usage_from(usage)
                    choices = data.get("choices", [])
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        if isinstance(delta, dict):
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                yield content
        except Exception:
            # 流中错误：折叠为无输出，usage 如实为零（不造假账单）。
            self.last_usage = Usage()
            return
        finally:
            if acquired:
                await client.aclose()
