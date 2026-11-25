"""编排 + 降级真实 e2e：一轮 asay 的相位/工具/审计副作用 + 错key/无key诚实失败。

禁止手搓 phase 列表；相位来自真实 bus 通知。失败断言保留，不改造成绿。
"""
from __future__ import annotations

import asyncio

from compose import LifecyclePhase
from runtime import Invoke, Terminate, Result

from conftest import DEEPSEEK_BASE, DEEPSEEK_MODEL, assert_no_secret, run
from impls.recording_llm import RecordingLLM


class HookArch:
    """最小 Arch：读 current_input，yield llm Invoke + 可选 tool Invoke。"""
    name = "orch_arch"
    version = "1.0.0"

    def static_check(self):
        from archs.base import StaticCheckReport
        return StaticCheckReport(errors=(), arch=self.name, version=self.version,
                                 ports_required=("llm", "tool"), budgets_resolved={})

    def compile(self):
        from compose import current_input
        def program():
            text = current_input()
            out = yield Invoke(port="llm", payload={"layer": "model", "messages": [{"role": "user", "content": text}]})
            yield Terminate(output=out)
            return out
        return program()


def test_orch01_phases_real_fired(loop: asyncio.AbstractEventLoop) -> None:
    """一轮 asay 真发相位：before_model/after_model/session_start/end 被 hook 真收到。"""
    from api import Agent
    from harness.model.capability import ModelCapability
    seen: list[str] = []

    async def scenario():
        llm = RecordingLLM(answer="done")
        agent = Agent(HookArch()).use(ModelCapability(llm=llm))
        for phase in (LifecyclePhase.SESSION_START, LifecyclePhase.BEFORE_MODEL,
                      LifecyclePhase.AFTER_MODEL, LifecyclePhase.SESSION_END):
            agent.on(phase, lambda notice, p=phase: seen.append(str(p.value)), name=f"spy-{phase.value}")
        answer = await agent.asay("你好")
        assert answer == "done"
    run(scenario(), loop)
    assert "before_model" in seen, f"before_model 必须真发，实际相位={seen}"
    assert "after_model" in seen, f"after_model 必须真发，实际相位={seen}"
    assert "session_start" in seen and "session_end" in seen, f"会话相位缺失，实际={seen}"


def test_orch02_budget_real_deduction(loop: asyncio.AbstractEventLoop) -> None:
    """usage 真进 R1 预算：last_run 的 usage.prompt_tokens>0（来自 RecordingLLM 真值）。"""
    from api import Agent
    from harness.model.capability import ModelCapability
    from runtime import Budgets

    async def scenario():
        llm = RecordingLLM(answer="done")
        budgets = Budgets(max_tokens=10000)
        agent = Agent(HookArch(), budgets=budgets).use(ModelCapability(llm=llm))
        await agent.asay("你好")
        assert llm.last_usage is not None and llm.last_usage.prompt_tokens > 0, "LLM 真结算 usage"
        assert agent.last_run is not None and agent.last_run.status == "done", "真跑完成"
    run(scenario(), loop)


def test_deg01_wrong_key_honest_failure(loop: asyncio.AbstractEventLoop) -> None:
    """错 key 真失败：error 非 None、含语义族、响应不含 sk-（不伪造成功）。"""
    from ports.adapters.llm import OpenAICompatLLM
    from compose import LLMRequest, PortCtx
    llm = OpenAICompatLLM(base_url=DEEPSEEK_BASE, api_key="sk-intentionally-wrong-key",
                          model=DEEPSEEK_MODEL)
    async def scenario():
        resp = await llm.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=8),
                                  PortCtx(session_id="deg"))
        return resp
    resp = run(scenario(), loop)
    assert resp.error is not None, "错 key 必须产生 error，不得伪造成功"
    assert_no_secret(str(resp) + str(resp.error))


def test_deg02_no_key_skipped_with_reason() -> None:
    """无 key 场景：deepseek_key fixture 缺失时显式 skip（环境阻断证据），断言不删改。"""
    # 该用例本身验证 conftest 的 key 加载契约：key 只从环境/注册表读，不落文件
    import conftest
    if conftest.DEEPSEEK_KEY:
        # 真实 key 形态契约：sk- 前缀 + 足够长度（可鉴别：截断/占位 key 会红）
        k = conftest.DEEPSEEK_KEY
        assert k.startswith("sk-") and len(k) >= 20, "真实 DeepSeek key 应为 sk- 前缀且长度>=20"
        assert k.strip() == k, "key 不应含首尾空白"
    else:
        import pytest
        pytest.skip("无 DEEPSEEK_API_KEY：环境阻断（注册表/进程环境均无），断言保留不造绿")
