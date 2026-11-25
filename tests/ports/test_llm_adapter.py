"""OpenAICompatLLM 默认适配器测试（R4 §6.1）：complete / stream / last_usage / 凭据不泄漏 / 错误折叠。

用 httpx.MockTransport 注入 fake 传输，全程不联网。
"""
from __future__ import annotations

import asyncio
import json

import httpx

from compose import LLMPort, LLMRequest, PortCtx
from ports.adapters.llm import OpenAICompatLLM


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ctx() -> PortCtx:
    return PortCtx(session_id="s", run_id="r")


def _req() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hi"}])


class TestComplete:
    def test_complete_parses_content_and_usage(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            body = json.loads(request.content)
            seen["body"] = body
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                "model": "gpt-test",
            })

        llm = OpenAICompatLLM(base_url="https://api.test/openai", api_key="SK-SECRET",
                              model="gpt-test", client=_client(handler))
        resp = asyncio.run(llm.complete(_req(), _ctx()))
        assert resp.content == "hello"
        assert resp.usage.prompt_tokens == 10 and resp.usage.completion_tokens == 3
        assert resp.model == "gpt-test"
        assert llm.last_usage is not None and llm.last_usage.prompt_tokens == 10
        assert seen["url"] == "https://api.test/openai/chat/completions"
        assert seen["auth"] == "Bearer SK-SECRET"
        assert seen["body"]["model"] == "gpt-test"
        assert seen["body"]["stream"] is False

    def test_implements_llm_port(self) -> None:
        llm = OpenAICompatLLM(base_url="x", api_key="k", model="m", client=_client(lambda r: httpx.Response(200)))
        assert isinstance(llm, LLMPort)

    def test_bad_tool_json_keeps_response_body(self) -> None:
        """N2：工具 arguments 坏 JSON 不得丢弃整份有效响应（正文保留、不误标网络错误）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant",
                    "content": "here is your answer",
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {"name": "do_thing", "arguments": "{bad json"},
                    }],
                }}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                "model": "m",
            })

        llm = OpenAICompatLLM(base_url="https://api.test/openai", api_key="k",
                              model="m", client=_client(handler))
        resp = asyncio.run(llm.complete(_req(), _ctx()))
        assert resp.error is None, f"坏 JSON 不该误标网络错误：{resp.error}"
        assert resp.content == "here is your answer", "有效正文不能丢"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "do_thing"
        assert resp.tool_calls[0].args == {}, "坏 JSON 参数降级为空字典"


class TestStream:
    def test_stream_parses_sse_and_sets_usage(self) -> None:
        sse = (
            'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n'
            'data: [DONE]\n\n'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["stream"] is True
            assert body["stream_options"] == {"include_usage": True}
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

        llm = OpenAICompatLLM(base_url="https://api.test/openai", api_key="k",
                              model="m", client=_client(handler))
        chunks = asyncio.run(_collect(llm.stream(_req(), _ctx())))
        assert chunks == ["he", "llo"]
        assert llm.last_usage is not None and llm.last_usage.completion_tokens == 2

    def test_stream_disconnect_raises_and_fakes_no_usage(self) -> None:
        """断流必须上抛原始错误，且不得写入 usage 伪装成功结算。

        早期实现吞掉异常并让网关看到 None，虽不记假账却把原始错误丢失；
        现在错误原样上抛，网关据此释放预留、不结算、不写缓存。
        """
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset mid-stream")

        llm = OpenAICompatLLM(base_url="https://api.test/openai", api_key="k",
                              model="m", client=_client(handler))
        try:
            asyncio.run(_collect(llm.stream(_req(), _ctx())))
        except httpx.ReadError as exc:
            assert "connection reset mid-stream" in str(exc)
        else:
            raise AssertionError("断流必须上抛 httpx.ReadError，不得吞成正常结束")
        assert llm.last_usage is None, \
            f"断流后 last_usage 应保持 None，实际 {llm.last_usage!r}"


class TestErrors:
    def test_http_error_folded_with_error_marker(self) -> None:
        """HTTP 错误折叠为带 error 的响应（不抛裸异常），R3 据此重试/熔断。

        E3 修复：不能再返回"空内容但看起来成功"的响应——那会让 R3 全线失明。
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom"}})

        llm = OpenAICompatLLM(base_url="https://api.test/openai", api_key="k", model="m", client=_client(handler))
        resp = asyncio.run(llm.complete(_req(), _ctx()))
        assert resp.error is not None and "500" in resp.error
        assert resp.usage.prompt_tokens == 0  # 错误不产生 usage，非假数据

    def test_api_key_never_in_response_or_usage(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}], "usage": {}, "model": "m",
            })

        llm = OpenAICompatLLM(base_url="x", api_key="SK-SECRET", model="m", client=_client(handler))
        resp = asyncio.run(llm.complete(_req(), _ctx()))
        assert "SK-SECRET" not in str(resp.model)
        assert "SK-SECRET" not in (resp.content or "")


async def _collect(it) -> list[str]:
    return [item async for item in it]
