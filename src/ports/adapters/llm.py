"""OpenAI-compatible LLM 默认适配器（R4 §6.1）。

框架唯一的 LLMPort 默认实现：真发 HTTP 请求、真解析 SSE，不联网由注入 fake client 保证。
不属于已实现模块契约（R1~R4 锁定 runtime/compose/harness），新增为兼容扩展。

设计约束（R4 §6.1）：
- complete(): POST /chat/completions，解析 content/usage，写 last_usage。
- stream(): 带 stream_options.include_usage，逐块解析 SSE，末块 usage 写 last_usage。
- 错误折叠为 LLMResponse，不抛裸异常；api_key 只进 Authorization 头。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from compose import LLMRequest, LLMResponse, PortCtx, ToolCall, Usage

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
            base_url: OpenAI-compatible 端点根（如 https://api.example/openai）。
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
        # 逐项解析 vendor tool_calls：单项形态非法只跳过该项，保住同批其余有效调用
        # （整批失败会丢掉正文并被误标 NETWORK_ERROR 触发无谓重试/熔断）。
        for item in raw:
            # 非 dict 的项无字段可提取，跳过而非让整批解析崩掉。
            if not isinstance(item, dict):
                continue
            raw_fn = item.get("function")
            fn: dict[str, object] = raw_fn if isinstance(raw_fn, dict) else {}
            args: object = fn.get("arguments", "{}")
            # N2：坏 JSON 只降级该工具调用的参数，不让它冒泡丢弃整份有效响应
            # （否则正文丢失、整份响应被误标 NETWORK_ERROR 触发重试/熔断）。
            parsed: object = args
            # 协议里 arguments 是 JSON 字符串，需反序列化成结构化参数；
            # 若 vendor 直接给了对象则原样使用，兼容两种形态（边界条件）。
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except json.JSONDecodeError:
                    parsed = {}
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
        # N1：每次流开始重置结算口，避免复用实例时残留上次调用的 usage。
        self.last_usage = None
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
                # SSE 逐行推送，按行读才能及时把文本片段 yield 出去；
                # aiter_lines 负责 TCP 分包/粘包，调用方无需自己拼缓冲区。
                async for line in resp.aiter_lines():
                    line = line.strip()
                    # 只认 data: 行；event:/id:/重试间隔/空行等 SSE 控制行一律忽略。
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    # 流结束标记或空 data 不含增量文本，跳过（空行常作保活心跳）。
                    if not payload or payload == _SSE_DONE:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # 非 dict 的 JSON 行（如标量）不是有效 chunk，忽略而非当成文本。
                    if not isinstance(data, dict):
                        continue
                    # 末块：usage 无 choices，写入结算口后不 yield 文本。
                    usage = data.get("usage")
                    # usage 只出现在末块；出现即写入结算口，保留真实账单值。
                    if usage is not None:
                        self.last_usage = self._usage_from(usage)
                    choices = data.get("choices", [])
                    # 仅从首个 choice 取增量（本适配器只服务单主流）；
                    # choices 为空正是 usage-only 末块的标志，此时不产出文本。
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        # delta 形态可能是 dict 或缺省；非 dict 时无 content 可取，直接忽略。
                        if isinstance(delta, dict):
                            content = delta.get("content")
                            # 只 yield 非空字符串片段，空 content 不产生无意义的下游事件。
                            if isinstance(content, str) and content:
                                yield content
        except Exception:
            # N1：流中断不得把 last_usage 设成全零 Usage()——那会绕过网关
            # "usage is None → 拒答" 防线，记假零成本并把空内容写进缓存污染后续请求。
            # 保持 None（初值即 None），由网关显式拒答；若断流前已收到 usage 则保留真实值。
            return
        finally:
            if acquired:
                await client.aclose()
