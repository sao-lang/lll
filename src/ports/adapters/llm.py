"""OpenAI-compatible LLM 默认适配器（R4 §6.1）。

框架唯一的 LLMPort 默认实现：真发 HTTP 请求、真解析 SSE，不联网由注入 fake client 保证。
不属于已实现模块契约（R1~R4 锁定 runtime/compose/harness），新增为兼容扩展。

设计约束（R4 §6.1）：
- complete(): POST /chat/completions，解析 content/usage/tool_calls，写本任务 usage。
- stream()/stream_events(): 带 stream_options.include_usage，共用一条 SSE 解析链；
  流式 usage 写本任务上下文的结算口，断流/HTTP 错误一律上抛，绝不吞成成功。
- complete() 的错误折叠为 LLMResponse（R3 据此重试/熔断）；stream 路径必须上抛原错误，
  否则网关会把半截流当成功结算并写缓存。api_key 只进 Authorization 头。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from compose import (
    LLMRequest,
    LLMResponse,
    PortCtx,
    StreamEvent,
    StreamEventKind,
    ToolCall,
    Usage,
)

__all__ = ["OpenAICompatLLM"]

_SSE_DONE = "[DONE]"


class _UsageUnset:
    """哨兵：区分"本任务上下文从未设置 usage"与"显式设置为 None"。

    两者语义不同：从未设置（如 asyncio.run 结束后的新上下文）才回退兼容读取口；
    显式 None（流开头声明本流无 usage）必须保持 None，不得回退到另一个并发任务
    写入兼容口的账，否则无 USAGE 的流会串台结算别人的成本。
    """


_USAGE_UNSET = _UsageUnset()


@dataclass(frozen=True, slots=True)
class _ToolCallFragment:
    """SSE 一帧里的单片 tool_calls；index 是跨帧聚合的唯一键。"""

    index: int
    call_id: str
    name: str
    arguments_delta: str


@dataclass(frozen=True, slots=True)
class _ParsedChunk:
    """一帧 SSE 的归一化结果：文本增量 + 工具调用分片 + 可选账单。"""

    content: str = ""
    tool_calls: tuple[_ToolCallFragment, ...] = ()
    usage: Usage | None = None


@dataclass(slots=True)
class _CallAccumulator:
    """按 index 聚合的半截工具调用（arguments 逐片拼接，流末一次性解析）。"""

    call_id: str
    name: str
    argument_parts: list[str]


class OpenAICompatLLM:
    """OpenAI-compatible HTTP 后端（需 base_url + api_key + model）。

    属性：
        name: 适配器名。last_usage: 本任务上下文最近一次调用的结算 usage
        （LLMPort 协议成员；仅供兼容读取，记账以流内 USAGE / 响应 usage 为准）。
    """

    name = "openai_compat"

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
        # usage 按"实例 × 并发任务"隔离：每实例持独立 ContextVar，
        # 同一实例的并发流在各自 task 的 context 写入/读取，互不覆盖（EXP2-B）。
        self._usage_var: ContextVar[Usage | None | _UsageUnset] = ContextVar(
            "agentkit_openai_compat_last_usage", default=_USAGE_UNSET
        )
        # 跨任务边界（如 asyncio.run 结束后在主上下文读取）的兼容读取口。
        # 只供外部读取"最近一次 usage"；网关记账始终读本任务 ContextVar，不走这里。
        self._last_usage_compat: Usage | None = None

    @property
    def last_usage(self) -> Usage | None:
        """本任务上下文的结算 usage。

        仅当本上下文从未设置过 usage 时才回退兼容读取口（跨 asyncio.run 后
        的外部读取）；显式设置的 None（本流无 usage）必须保持 None，绝不回退
        到其它并发任务写入兼容口的账（否则无 USAGE 的流串台记假成本）。
        """
        scoped = self._usage_var.get()
        if isinstance(scoped, _UsageUnset):
            return self._last_usage_compat
        return scoped

    @last_usage.setter
    def last_usage(self, value: Usage | None) -> None:
        """写入本任务上下文的 usage（隔离记账），并同步兼容读取口。"""
        self._usage_var.set(value)
        self._last_usage_compat = value

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

    async def _iter_sse(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[_ParsedChunk]:
        """SSE 唯一解析链：逐帧产出归一化增量；HTTP/网络错误原样上抛。

        断流绝不吞：网关据此释放预留、不结算、不写缓存（EXP2-C/D）。
        usage 写入本任务 ContextVar，供兼容读取；结算依据是产出的 _ParsedChunk.usage。
        """
        self.last_usage = None  # 每次流开始重置本任务上下文的结算口，避免复用残留
        client = self._client or httpx.AsyncClient()
        acquired = self._client is None  # 自建客户端需自行关闭
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(req, stream=True),
            ) as resp:
                resp.raise_for_status()  # HTTP 错误原样抛出，保留状态码与响应体
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

                    # 末块 usage 无 choices；出现即写入本任务结算口并随帧产出。
                    raw_usage = data.get("usage")
                    settled: Usage | None = (
                        self._usage_from(raw_usage) if raw_usage is not None else None
                    )
                    if settled is not None:
                        self.last_usage = settled

                    content = ""
                    fragments: list[_ToolCallFragment] = []
                    choices = data.get("choices", [])
                    # 仅从首个 choice 取增量（本适配器只服务单主流）；
                    # choices 为空正是 usage-only 末块的标志，此时不产出文本。
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        # delta 形态可能是 dict 或缺省；非 dict 时无字段可取。
                        if isinstance(delta, dict):
                            raw_content = delta.get("content")
                            if isinstance(raw_content, str) and raw_content:
                                content = raw_content
                            # 流式 tool_calls：按 index 收集分片，逐片重建完整调用（EXP2-B）。
                            raw_tools = delta.get("tool_calls")
                            if isinstance(raw_tools, list):
                                for item in raw_tools:
                                    if not isinstance(item, dict):
                                        continue
                                    raw_fn = item.get("function")
                                    fn = raw_fn if isinstance(raw_fn, dict) else {}
                                    raw_index = item.get("index", 0)
                                    fragments.append(_ToolCallFragment(
                                        index=int(raw_index) if isinstance(raw_index, int) else 0,
                                        call_id=str(item.get("id", "") or ""),
                                        name=str(fn.get("name", "") or ""),
                                        arguments_delta=str(fn.get("arguments", "") or ""),
                                    ))
                    if content or fragments or settled is not None:
                        yield _ParsedChunk(
                            content=content, tool_calls=tuple(fragments), usage=settled
                        )
        finally:
            if acquired:
                await client.aclose()

    def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """流式补全：逐块产出 delta.content；末帧 usage 写本任务结算口。

        PEP525 签名（调用即得 AsyncIterator，无需 await）。取消由消费方 aclose() 传播，
        try/finally 关闭底层响应。断流/HTTP 错误上抛，由网关拒答、不结算不写缓存。
        """
        return self._stream_impl(req, ctx)

    async def _stream_impl(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """文本流实现体：只转出文本增量；结构化调用走 stream_events。"""
        async for chunk in self._iter_sse(req, ctx):
            if chunk.content:
                yield chunk.content

    def stream_events(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[StreamEvent]:
        """原生结构化事件流：与 stream() 共用解析链，额外产出工具调用生命周期事件。

        工具分片按 index 聚合：首片发 TOOL_CALL_START，每片 arguments 发 TOOL_CALL_DELTA，
        正常流末发 TOOL_CALL_END（data 含 call_id/name/arguments，供上层聚合成 ToolCall）。
        断流时异常上抛，不发 END/DONE，绝不把半截调用当可执行结果。
        """
        return self._stream_events_impl(req, ctx)

    async def _stream_events_impl(
        self, req: LLMRequest, ctx: PortCtx,
    ) -> AsyncIterator[StreamEvent]:
        """事件流实现体：透传文本/usage，按 index 聚合工具调用分片。"""
        accumulators: dict[int, _CallAccumulator] = {}
        async for chunk in self._iter_sse(req, ctx):
            if chunk.content:
                yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk.content)
            for frag in chunk.tool_calls:
                acc = accumulators.get(frag.index)
                if acc is None:
                    acc = _CallAccumulator(
                        call_id=frag.call_id, name=frag.name, argument_parts=[]
                    )
                    accumulators[frag.index] = acc
                    yield StreamEvent(kind=StreamEventKind.TOOL_CALL_START, data={
                        "index": frag.index, "call_id": frag.call_id, "name": frag.name,
                    })
                else:
                    # vendor 可能把 id/name 放在后续片，补齐以便 END 给出完整调用。
                    if frag.call_id and not acc.call_id:
                        acc.call_id = frag.call_id
                    if frag.name and not acc.name:
                        acc.name = frag.name
                if frag.arguments_delta:
                    acc.argument_parts.append(frag.arguments_delta)
                    yield StreamEvent(kind=StreamEventKind.TOOL_CALL_DELTA, data={
                        "index": frag.index, "arguments_delta": frag.arguments_delta,
                    })
            if chunk.usage is not None:
                yield StreamEvent(kind=StreamEventKind.USAGE, data=chunk.usage)

        # 只有正常流末才提交工具调用；断流时 _iter_sse 已抛异常，不会走到这里。
        for index in sorted(accumulators):
            acc = accumulators[index]
            raw_args = "".join(acc.argument_parts)
            try:
                parsed = json.loads(raw_args)
            except (ValueError, TypeError):
                parsed = {}
            yield StreamEvent(kind=StreamEventKind.TOOL_CALL_END, data={
                "index": index, "call_id": acc.call_id, "name": acc.name,
                "arguments": parsed if isinstance(parsed, dict) else {},
            })
        yield StreamEvent(kind=StreamEventKind.DONE)
