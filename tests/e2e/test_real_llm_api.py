"""真实 LLM e2e：DeepSeek 真网 + 用户自定义 LLMPort 真 HTTP。

纪律：
- 密钥只从 tests.e2e.conftest.deepseek_key fixture 读，只进 Authorization 头；
  绝不 print、绝不写文件、绝不出现在断言消息里；
- 每用例独立 loop（conftest.loop + run），真网调用仅 4 次（LLM-01/02/03/04）；
- 断言必须有鉴别力：精确文本 / token 计数 / 相位顺序 / 服务端实收，禁用 is not None
  或 or True 充数；
- 真网失败如实红，绝不改 mock/fake 冒充通过。
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from compose import LifecycleBus, LifecyclePhase, LLMRequest, LLMResponse, PortCtx, Usage
from harness.model import ModelGateway
from ports.adapters.llm import OpenAICompatLLM
from runtime import Invoke

from tests.e2e.conftest import DEEPSEEK_BASE, DEEPSEEK_MODEL, assert_no_secret, run

_PROMPT = "Reply with exactly: OK"


def test_llm01_complete_real_deepseek(
    loop: asyncio.AbstractEventLoop, deepseek_key: str
) -> None:
    """LLM-01 complete 真网。

    输入：DEEPSEEK_BASE/DEEPSEEK_MODEL + fixture 密钥；"Reply with exactly: OK"，
          max_tokens=32，temperature=0。
    真实入口：OpenAICompatLLM.complete → 真 POST api.deepseek.com/v1/chat/completions。
    精确断言：error is None；content.strip()=="OK"；usage.prompt_tokens>0；
              last_usage is usage；str(response) 不含 "sk-"。
    失败判据：网络/鉴权/限流使 error 非 None、正文非精确 "OK"、账单 token 为 0、
              last_usage 与响应 usage 非同一对象、或响应串泄漏密钥，任一即红。
    """

    async def scenario() -> None:
        llm = OpenAICompatLLM(
            base_url=DEEPSEEK_BASE, api_key=deepseek_key, model=DEEPSEEK_MODEL
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": _PROMPT}],
            max_tokens=32,
            temperature=0.0,
        )
        resp = await llm.complete(req, PortCtx(session_id="e2e-llm01", run_id="run-llm01"))

        assert resp.error is None, f"真实 complete 失败：error={resp.error!r}"
        assert resp.content is not None and resp.content.strip() == "OK", (
            f"真网返回正文须精确为 OK，实际={resp.content!r}"
        )
        assert resp.usage.prompt_tokens > 0, "真实账单须有 prompt token，0 表示 usage 未解析"
        assert llm.last_usage is resp.usage, "last_usage 必须是本次响应同一个 usage 对象"
        assert_no_secret(str(resp))

    run(scenario(), loop)


def test_llm02_stream_real_deepseek(
    loop: asyncio.AbstractEventLoop, deepseek_key: str
) -> None:
    """LLM-02 stream 真网并消费全部 chunk。

    输入：同 LLM-01 的请求；.stream(req, ctx) 调用即得 PEP525 迭代器。
    真实入口：OpenAICompatLLM.stream → SSE 真流（含 stream_options.include_usage）。
    精确断言：chunk 数>=1；拼接正文 strip().upper()=="OK"；
              last_usage.prompt_tokens>0；正文与 chunk 列表均不含 "sk-"。
    失败判据：零 chunk、正文非精确 OK、末块 usage 未写入结算口、或密钥泄漏，任一即红。
    """

    async def scenario() -> None:
        llm = OpenAICompatLLM(
            base_url=DEEPSEEK_BASE, api_key=deepseek_key, model=DEEPSEEK_MODEL
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": _PROMPT}],
            max_tokens=32,
            temperature=0.0,
        )
        stream = llm.stream(req, PortCtx(session_id="e2e-llm02", run_id="run-llm02"))
        chunks = [chunk async for chunk in stream]

        assert len(chunks) >= 1, "真实 SSE 必须产出至少一个文本 chunk"
        full = "".join(chunks)
        assert full.strip().upper() == "OK", f"流式全文须精确为 OK，实际={full!r}"
        assert llm.last_usage is not None, "流末必须写入 last_usage（否则网关拒答）"
        assert llm.last_usage.prompt_tokens > 0, "真实流式账单 prompt_tokens 须大于 0"
        assert_no_secret(full)
        assert_no_secret(str(chunks))

    run(scenario(), loop)


def test_llm03_stream_cancel_after_one_chunk(
    loop: asyncio.AbstractEventLoop, deepseek_key: str
) -> None:
    """LLM-03 stream 消费 1 个 chunk 后取消。

    输入：同 LLM-01 请求；anext 取一个真 chunk 后对异步生成器 aclose()。
    真实入口：OpenAICompatLLM.stream 的异步生成器 + GeneratorExit → finally 关闭连接。
    精确断言：首 chunk 为非空 str 且不含 "sk-"；aclose() 不抛异常；再次 anext 抛
              StopAsyncIteration（证明生成器确已关闭，而不是吞掉取消继续跑）。
    失败判据：首 chunk 空/含凭据、aclose 抛穿异常、或关闭后仍能取出内容，任一即红。
    """

    async def scenario() -> None:
        llm = OpenAICompatLLM(
            base_url=DEEPSEEK_BASE, api_key=deepseek_key, model=DEEPSEEK_MODEL
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": _PROMPT}],
            max_tokens=64,
            temperature=0.0,
        )
        stream = llm.stream(req, PortCtx(session_id="e2e-llm03", run_id="run-llm03"))
        first = await anext(stream)
        assert isinstance(first, str) and first != "", "首 chunk 必须是真实非空文本"
        assert_no_secret(first)
        await stream.aclose()
        # 取消会弃掉 httpx 内部的 SSE async generator；显式 await 标准清理钩子把
        # 它的 aclose 真跑完，避免 per-case loop 关闭时留下未 await 的 coroutine。
        await asyncio.get_running_loop().shutdown_asyncgens()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    run(scenario(), loop)


def test_llm04_gateway_notices_and_usage(
    loop: asyncio.AbstractEventLoop, deepseek_key: str
) -> None:
    """LLM-04 ModelGateway + LifecycleBus 真编排。

    输入：真实 OpenAICompatLLM + 空 LifecycleBus（订阅 BEFORE_MODEL/AFTER_MODEL）；
          Invoke(port="llm", payload={"messages":[{"role":"user","content":_PROMPT}]})。
    真实入口：ModelGateway.as_dispatch()(effect, PortCtx) → _center → complete →
              真 DeepSeek 调用；_notify_model 发相位；_record_cost 进 ledger。
    精确断言：result.ok is True 且 data["content"].strip()=="OK"；
              data["usage"].prompt_tokens>0；相位序列精确为
              ["before_model","after_model"] 且 after.payload.error==""；
              ledger.count==1 且 tokens_by("run_id","run-llm04")>0；结果无 "sk-"。
    失败判据：相位缺失/乱序、after 带错、usage 未入账、正文非 OK、凭据泄漏，任一即红。
    """

    async def scenario() -> None:
        llm = OpenAICompatLLM(
            base_url=DEEPSEEK_BASE, api_key=deepseek_key, model=DEEPSEEK_MODEL
        )
        bus = LifecycleBus()
        phases: list[tuple[str, str]] = []
        bus.on(
            LifecyclePhase.BEFORE_MODEL,
            lambda notice: phases.append((notice.phase.value, "")),
        )
        bus.on(
            LifecyclePhase.AFTER_MODEL,
            lambda notice: phases.append(
                (notice.phase.value, str(notice.payload.get("error", "")))
            ),
        )
        gw = ModelGateway(llm=llm, bus=bus)
        effect = Invoke(port="llm", payload={
            "messages": [{"role": "user", "content": _PROMPT}],
        })
        ctx = PortCtx(session_id="e2e-llm04", run_id="run-llm04")

        result = await gw.as_dispatch()(effect, ctx)

        assert result.ok is True, f"网关真调用应成功，实际 error={result.error!r}"
        data = result.data
        assert isinstance(data, dict), f"成功信封 data 须为 dict，实际={type(data)}"
        assert data["content"].strip() == "OK", f"网关返回正文须为 OK，实际={data['content']!r}"
        assert data["usage"].prompt_tokens > 0, "网关必须带回真账单 token"
        assert [phase for phase, _ in phases] == ["before_model", "after_model"], (
            f"相位须精确 before→after 各一次，实际={phases!r}"
        )
        assert phases[-1][1] == "", f"成功路径 after_model.error 须为空，实际={phases[-1][1]!r}"
        assert gw.ledger.count == 1, "一次真调用必须恰好记一笔成本"
        assert gw.ledger.tokens_by("run_id", "run-llm04") > 0, "真 usage 必须按 run_id 入账"
        assert_no_secret(str(result))
        assert_no_secret(str(phases))

    run(scenario(), loop)


class _EchoProviderHandler(BaseHTTPRequestHandler):
    """本地 provider：记录收到的 messages，回 OpenAI-compatible 响应，日志全静默。"""

    def log_message(self, fmt: str, *args: object) -> None:
        """静默：请求头/凭据绝不落日志。"""
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        received: list[list[dict[str, Any]]] = getattr(self.server, "received")
        received.append([dict(m) for m in messages if isinstance(m, dict)])

        user_text = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                user_text = str(message.get("content") or "")
                break
        content = f"SERVER_ECHO: {user_text}"

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for piece in ("SERVER_ECHO: ", user_text):
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            tail = {
                "choices": [],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            }
            self.wfile.write(f"data: {json.dumps(tail)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        payload = {
            "model": "local-provider",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ProviderBackedLLM:
    """用户自定义 LLMPort：内部用 httpx 真 POST 到本地 provider，非框架默认适配器。"""

    name = "provider_backed"
    last_usage: Usage | None = None

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _body(self, req: LLMRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model or self._model,
            "messages": [dict(m) for m in req.messages],
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=self._body(req, stream=False),
            )
            resp.raise_for_status()
            data = resp.json()
        raw_usage = data["usage"]
        usage = Usage(
            prompt_tokens=int(raw_usage["prompt_tokens"]),
            completion_tokens=int(raw_usage["completion_tokens"]),
        )
        self.last_usage = usage
        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            usage=usage,
            model=data["model"],
        )

    def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        return self._stream_impl(req, ctx)

    async def _stream_impl(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        self.last_usage = None
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=self._body(req, stream=True),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    data = json.loads(payload)
                    if data.get("usage") is not None:
                        raw_usage = data["usage"]
                        self.last_usage = Usage(
                            prompt_tokens=int(raw_usage["prompt_tokens"]),
                            completion_tokens=int(raw_usage["completion_tokens"]),
                        )
                    for choice in data.get("choices", []):
                        piece = choice.get("delta", {}).get("content")
                        if piece:
                            yield piece


def test_llm05_custom_llm_consumed_via_real_http(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """LLM-05 用户自定义 LLMPort 被框架真消费（本地真 HTTP，不走 DeepSeek）。

    输入：http.server 起的本地 provider（127.0.0.1 随机端口，记录实收 messages）
          + 本文件定义的 ProviderBackedLLM（httpx.AsyncClient 真 POST）
          + ModelGateway(llm=custom)；两条不同 user 文本分别走 complete 与 stream。
    真实入口：gateway.complete → custom.complete → 真 POST → 本地 server；
              custom.stream → 真 POST → 本地 server 的 SSE。
    精确断言：server.received 恰有 2 条且首条/次条 content 分别等于两条输入文本；
              complete 返回的 content=="SERVER_ECHO: e2e-custom-provider-payload"；
              usage.prompt_tokens==11；stream 拼接=="SERVER_ECHO: e2e-custom-stream-payload"；
              stream 的 last_usage.prompt_tokens==11；响应无 "sk-"。
    失败判据：server 没收到/收到错误文本、返回内容不是服务端回显、usage 非服务端值、
              stream 未消费自定义实现、或凭据泄漏，任一即红。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoProviderHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        async def scenario() -> None:
            custom = ProviderBackedLLM(
                base_url=base_url, api_key="local-provider-no-key", model="local-provider"
            )
            gw = ModelGateway(llm=custom)
            ctx = PortCtx(session_id="e2e-llm05", run_id="run-llm05")

            resp = await gw.complete(
                LLMRequest(messages=[
                    {"role": "user", "content": "e2e-custom-provider-payload"}
                ]),
                ctx,
            )
            assert resp.error is None, f"自定义后端经网关调用失败：{resp.error!r}"
            assert resp.content == "SERVER_ECHO: e2e-custom-provider-payload", (
                f"返回正文必须来自 server 回显，实际={resp.content!r}"
            )
            assert resp.usage.prompt_tokens == 11, "usage 必须来自 server 真账单"
            received = getattr(server, "received")
            assert len(received) == 1, f"server 应收到恰好 1 条请求，实际={len(received)}"
            assert received[0][0]["content"] == "e2e-custom-provider-payload", (
                f"server 实收用户文本须一致，实际={received[0]!r}"
            )

            chunks = [
                chunk async for chunk in custom.stream(
                    LLMRequest(messages=[
                        {"role": "user", "content": "e2e-custom-stream-payload"}
                    ]),
                    ctx,
                )
            ]
            assert "".join(chunks) == "SERVER_ECHO: e2e-custom-stream-payload", (
                f"流式全文须来自 server 回显，实际={''.join(chunks)!r}"
            )
            assert len(received) == 2, f"server 应收到恰好 2 条请求，实际={len(received)}"
            assert received[1][0]["content"] == "e2e-custom-stream-payload", (
                f"server 第二次实收须一致，实际={received[1]!r}"
            )
            assert custom.last_usage is not None, "自定义 stream 末块必须写 last_usage"
            assert custom.last_usage.prompt_tokens == 11, "流式 usage 须来自 server 真账单"
            assert_no_secret(str(resp))
            assert_no_secret("".join(chunks))

        run(scenario(), loop)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
