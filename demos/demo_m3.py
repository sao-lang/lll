"""M3 独立闭环 demo：DTO / 协议族 / 注册表 / 错误信封 / 四默认适配器 / 替换 / child / 集成。

运行：make demo-m3
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from chains import Chains, MountPoint  # noqa: E402
from chains.builtins import AllowFilter  # noqa: E402
from ports import (  # noqa: E402
    DangerLevel, LLMRequest, LLMResponse, PortContractError, PortCtx,
    PortRegistry, ToolCall, ToolSpec, call, make_call,
)
from ports.adapters import (  # noqa: E402
    InMemoryToolRegistry, JsonlTelemetry, OpenAICompatLLM,
)
from ports.adapters.fakes import (  # noqa: E402
    FailingAdapter, ScriptedLLM, StaticHuman, StaticSkillPort,
)
from ports.protocols import PORT_PROTOCOLS, LLMPort, ToolPort  # noqa: E402
from runtime import (  # noqa: E402
    AgentState, Effect, EffectKind, PortMissing, Runner, Session, Usage,
)


def ctx():
    from chains import ChainCtx
    return ChainCtx(effect=Effect(EffectKind.LLM), state_view=AgentState(), session_id="demo-m3")


async def main() -> None:
    # ① DTO 与协议族
    spec = ToolSpec(name="echo", description="回显", parameters_schema={"type": "object"}, danger=DangerLevel.SAFE)
    req = LLMRequest.model_validate({"messages": [{"role": "user", "content": "hi"}], "tools_catalog": [spec.model_dump()]})
    assert req.messages[0].content == "hi" and len(PORT_PROTOCOLS) == 12
    print(f"[M3] DTO ok messages={len(req.messages)} ports={len(PORT_PROTOCOLS)}")

    # ② 注册表：命名空间 + 装配期缺 port
    registry = PortRegistry()
    try:
        registry.register("my_llm", object())
        raise AssertionError("应拒绝非法命名空间")
    except PortContractError:
        pass
    try:
        registry.freeze(("llm", "tool", "telemetry"))
        raise AssertionError("应报缺失清单")
    except PortMissing as exc:
        print(f"[M3] freeze → {exc}")
    registry.register("x-my-llm", object())  # 自定义 port 前缀合法
    print(f"[M3] registered = {registry.names()}")

    # ③ 错误信封：异常 100% 折叠、retryable 标注
    failing = PortRegistry().register("llm", FailingAdapter(TimeoutError("upstream timeout")))
    res = await call(Effect(EffectKind.LLM, {"messages": []}), ctx(), failing)
    assert not res.ok and res.error.retryable and res.error.code == "TimeoutError"
    bad = PortRegistry().register("tool", InMemoryToolRegistry())
    res2 = await call(Effect(EffectKind.TOOL, {"call": {"name": "x"}}), ctx(), bad)
    assert not res2.ok and res2.error.code == "PORT_INVALID" and not res2.error.retryable
    print("[M3] 错误信封 → TimeoutError(retryable=True) / PORT_INVALID(retryable=False)")

    # ④ 四个默认适配器
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "demo-model"
        return httpx.Response(200, json={
            "model": "demo-model",
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "echo", "arguments": '{"text":"hi"}'}}],
            }}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAICompatLLM(base_url="http://fake/v1", api_key="k", model="demo-model", client=client, cost_per_1k_tokens=0.01)
        resp = await llm.complete(req, PortCtx("demo-m3"))
        assert resp.tool_calls[0].name == "echo" and resp.usage.prompt_tokens == 12
        print(f"[M3] OpenAICompatLLM → tool_calls={len(resp.tool_calls)} cost={resp.usage.cost_usd}")

    tools = InMemoryToolRegistry()
    tools.register(spec, lambda args, c: f"echo:{args['text']}")
    tr = await tools.call(ToolCall(call_id="c1", name="echo", args={"text": "hi"}), PortCtx("demo-m3"))
    assert tr.ok and tr.value == "echo:hi"
    print(f"[M3] InMemoryToolRegistry → {tr.value}")

    with tempfile.TemporaryDirectory() as tmp:
        tele = JsonlTelemetry(Path(tmp) / "trace.jsonl")
        r3 = PortRegistry().register("llm", ScriptedLLM([LLMResponse(content="hello", usage=Usage(1, 1, 0.0))])).register("telemetry", tele)
        c = ctx()
        c.trace.extend(["pipe:a", "filter:b", "middleware:c"])
        result = await call(Effect(EffectKind.LLM, {"messages": []}), c, r3)
        assert result.ok and any("filter:b" in e["components"] for e in tele.read())
        print(f"[M3] JsonlTelemetry → {len(tele.read())} 条，含链路组件名")

        human = StaticHuman(["approve"])
        r4 = PortRegistry().register("human", human)
        answer = await call(Effect(EffectKind.WAIT_HUMAN, {"question": "允许执行?"}), ctx(), r4)
        assert answer.data == "approve" and human.questions == ["允许执行?"]
        r5 = PortRegistry().register("skill", StaticSkillPort())
        body = await call(Effect(EffectKind.SKILL, {"name": "python-expert", "part": "body"}), ctx(), r5)
        print(f"[M3] StaticHuman/StaticSkillPort → {answer.data} / {body.data[:8]}…")

    # ⑤ 替换适配器：上层 call 零改动
    reg = PortRegistry().register("llm", ScriptedLLM([LLMResponse(content="A")]), protocol=LLMPort)
    one = await call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg)
    reg.register("llm", ScriptedLLM([LLMResponse(content="B")]), protocol=LLMPort)
    two = await call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg)
    assert one.data.content == "A" and two.data.content == "B"
    print("[M3] 换 LLMPort 实现 → 只改一行注册，上层零改动")

    # ⑥ child 递归会话：子 Session trace 挂到父事件日志
    def child_program(state):
        yield Effect(EffectKind.END, {"output": "child-done"})

    def parent_program(state):
        answer = yield Effect(EffectKind.CHILD, {
            "program_factory": lambda: child_program(AgentState()),
            "state": AgentState(),
        })
        yield Effect(EffectKind.END, {"output": answer})

    runner = Runner(dispatch=lambda e, c: None, call=make_call(reg))
    session = Session("m3-parent", lambda: parent_program(AgentState()))
    out = await runner.run(session)
    assert out.output == "child-done" and any(":child:" in e.session_id for e in session.events)
    print(f"[M3] child 递归 → output={out.output!r} 父日志含子事件={sum(':child:' in e.session_id for e in session.events)}")

    # ⑦ 全链路集成：M1 Runner + M2 Chains + M3 Port
    def react(state):
        resp = yield Effect(EffectKind.LLM, {"messages": [m.to_dict() for m in state.messages]})
        if not resp.tool_calls:
            yield Effect(EffectKind.END, {"output": resp.content})
            return
        results = yield Effect(EffectKind.TOOL, {"calls": [
            {"call_id": tc.call_id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls
        ]}, batch=True)
        yield Effect(EffectKind.END, {"output": results[0].value})

    chain = Chains().filter(AllowFilter(), at=MountPoint.BEFORE_LLM).freeze(("llm", "tool"))
    integrated = (PortRegistry()
                  .register("llm", ScriptedLLM([LLMResponse(
                      content=None,
                      tool_calls=[ToolCall(call_id="c9", name="echo", args={"text": "hi"})],
                      usage=Usage(5, 2, 0.001),
                  )]), protocol=LLMPort)
                  .register("tool", tools, protocol=ToolPort))
    integrated.freeze(("llm", "tool"))
    r = Runner(dispatch=chain.dispatch, call=make_call(integrated), ctx_factory=chain.ctx_factory())
    final = await r.run(Session("m3-react", lambda: react(AgentState())))
    assert final.status == "done" and final.output == "echo:hi"
    print(f"[M3+M1+M2] ReAct → {final.output!r}，预算已扣 cost={final.state.budgets.used_cost_usd}")

    print("[M3] PASS: 契约 / DTO / 注册表 / 错误信封 / 四默认适配器 / 替换 / child / 集成")


if __name__ == "__main__":
    asyncio.run(main())
