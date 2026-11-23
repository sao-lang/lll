"""ReAct 聊天助手 · 语义门面最小 Demo（真实 DeepSeek / 离线双模）。

运行：
    export DEEPSEEK_API_KEY=sk-xxx        # 密钥只走环境变量，不落任何文件
    .venv/Scripts/python examples/react-chat/main.py

    # 不联网/省钱验证接线：
    CHAT_OFFLINE=1 .venv/Scripts/python examples/react-chat/main.py

设计原则（注入接线重构后的目标 API 形态）：
- 核心路径 5 行：@tool 声明工具（schema 从类型注解推导）→ Agent(ReAct, system=)
  → .use(模型).use(安全).use(记忆) → say() 出结果 → agent.stream 可观测。
- 零胶水：无 _SystemPromptLLM 包装、无手写 ToolSpec、无手动 recall/ingest、
  无 StreamGateway/PortCtx/usage 搬运——全部由框架自动编排。
- 能力插拔即生效：每个注入的能力在报告里有"生效证据"，不是登记名字。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api import Agent, verify_audit_chain
from api.wiring import STREAM_TOPIC
from archs import ReAct
from compose import (
    DangerLevel,
    LifecyclePhase,
    LLMRequest,
    LLMResponse,
    PortCtx,
    ToolCall,
    Usage,
)
from harness.context import MemoryCapability
from harness.model import CostGuard, ModelCapability
from harness.observability import TimelineProjection
from harness.resilience import ResilienceCapability, Timeout
from harness.security import SecurityCapability
from harness.tools import tool
from runtime import Budgets

# ---------------------------------------------------------------- 配置
PROMPT = os.getenv("CHAT_PROMPT", "用 get_weather 查一下深圳的天气，然后用一句话告诉我。")
OFFLINE = os.getenv("CHAT_OFFLINE") == "1"
MODEL = "deepseek-chat"
SESSION_ID = "react-chat-demo"
TRACE_ID = f"react-{int(time.time())}"
MAX_COST_USD = 0.05

SYSTEM_PROMPT = "你是简洁的中文助手。用工具查天气后用一句话回答。"


# ---------------------------------------------------------------- ① 工具：@tool 一行声明
# schema 从类型注解逐参数推导（city: str → {"type": "string"}，required 自动），
# 装饰即入列待注册容器，Agent 装配期自动消费——无需手写 ToolSpec、无需 register_tool。
@tool(description="查询指定城市的当前天气（温度、天气状况）", danger=DangerLevel.LOW)
def get_weather(city: str) -> dict[str, object]:
    """本地确定性天气查询（demo 用，无网络）。"""
    return {"city": city, "temperature": 28, "condition": "晴", "source": "demo-local"}


# ---------------------------------------------------------------- 报告工具
class Report:
    def __init__(self) -> None:
        self.sections: list[tuple[str, list[str]]] = []

    def add(self, title: str, lines: list[str]) -> None:
        self.sections.append((title, lines))

    def render(self) -> str:
        out = ["=" * 72, "ReAct 聊天助手 · 语义门面最小 Demo 报告", "=" * 72]
        for title, lines in self.sections:
            out += ["", f"【{title}】", "-" * 60, *lines]
        out += ["", "=" * 72]
        return "\n".join(out)


# ---------------------------------------------------------------- 假 LLM（离线）
class _FakeLLM:
    """离线确定性 LLM 替身（只在 demo，不冒充框架实现）；捕获收到的消息做注入取证。"""
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls.append([dict(m) for m in req.messages])
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(call_id="c1", name="get_weather", args={"city": "深圳"})],
                usage=Usage(prompt_tokens=30, completion_tokens=10, cost_usd=0.0001),
                model="fake",
            )
        return LLMResponse(
            content="深圳今天 28°C，晴。（离线假 LLM 的结论）",
            tool_calls=[],
            usage=Usage(prompt_tokens=60, completion_tokens=20, cost_usd=0.0002),
            model="fake",
        )

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> Any:  # pragma: no cover
        raise NotImplementedError


async def main() -> None:
    report = Report()

    # ---- ② LLM 后端（真实 DeepSeek 或离线假 LLM）----
    if OFFLINE:
        llm: Any = _FakeLLM()
        cost_guard = None
        llm_label = "本地假 LLM（CHAT_OFFLINE=1，不联网）"
    else:
        from ports.adapters.llm import OpenAICompatLLM
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            print("错误：需要 DEEPSEEK_API_KEY 环境变量（或设 CHAT_OFFLINE=1 离线演示）")
            return
        llm = OpenAICompatLLM(base_url="https://api.deepseek.com/v1",
                              api_key=api_key, model=MODEL)
        cost_guard = CostGuard(max_cost_usd=MAX_COST_USD)
        llm_label = f"DeepSeek OpenAI-compatible（{MODEL}）"

    # ---- ③ 记忆能力：预置两条长期记忆（生产里来自历史 say() 的自动写回）----
    mem_cap = MemoryCapability()
    preload_ctx = PortCtx(session_id=SESSION_ID, run_id="preload")
    mem_cap.manager.memory.ingest("用户偏好：喝咖啡不加糖", preload_ctx)
    mem_cap.manager.memory.ingest("用户当前所在城市：深圳", preload_ctx)
    mem_before = mem_cap.manager.memory.tier_count()

    # ---- ④ 核心一条链：Agent + 能力插拔 + say（全部自动编排）----
    arch = ReAct([get_weather], llm=MODEL, max_turns=2)
    check = arch.static_check()
    agent = Agent(arch, session_id=SESSION_ID, system=SYSTEM_PROMPT,
                  budgets=Budgets(max_turns=5, max_tokens=4000, max_cost_usd=MAX_COST_USD))
    agent.use(ModelCapability(llm=llm, cost_guard=cost_guard))
    agent.use(SecurityCapability())   # 沙盒/审计/审批默认实现 + say 输入过 guardrail
    agent.use(mem_cap)                # say() 自动 recall（前）+ 自动写回（后）
    agent.use(ResilienceCapability(timeout=Timeout(timeout=30.0)))
    # ↑ 可靠性九策略真包装 llm 链（timeout/retry/breaker/限流/舱位/幂等/降级/看门狗/killswitch）

    # lifecycle hook 一等订阅：say 前注册，装配期真订阅本次运行的 bus。
    # Notice 只读投影（phase/port/payload/meta），异常被总线就地隔离（通知语义）。
    hook_log: list[str] = []
    agent.on(LifecyclePhase.BEFORE_MODEL,
             lambda n: hook_log.append(f"{n.phase.value}"))
    agent.on(LifecyclePhase.AFTER_TOOL,
             lambda n: hook_log.append(f"{n.phase.value}:{n.port}"))
    agent.on(LifecyclePhase.SESSION_END,
             lambda n: hook_log.append(f"{n.phase.value}"))

    answer = agent.say(PROMPT)

    run = agent.last_run
    assert run is not None, "门面应暴露 last_run 诊断"
    report.add("④ 语义门面一条链（say）", [
        f"ReAct([get_weather], llm={MODEL}, max_turns=2) + Agent(system=…) + use×3",
        f"static_check: ok={check.ok}（ports={len(check.ports_required)}）",
        f"say() 返回: {answer[:200]}",
        f"run.status={run.status}, reason={run.reason or '-'}",
        (f"R1 预算消耗: turns={run.state.budgets.used_turns}, "
        f"tokens={run.state.budgets.used_tokens}, cost=${run.state.budgets.used_cost_usd:.6f}"),
    ])

    # ---- ⑤ 注入生效证据（不是登记名字）----
    sys_msg = next((m for m in llm.calls[0] if m.get("role") == "system"), None) \
        if isinstance(llm, _FakeLLM) and llm.calls else None
    mem_recalled = sys_msg is not None and "相关记忆" in str(sys_msg.get("content", ""))
    mem_after = mem_cap.manager.memory.tier_count()
    schema = get_weather.spec.parameters_schema
    report.add("⑤ 注入生效证据", [
        f"@tool schema 推导: {schema}（city:str → string，required 自动）",
        (f"system= 一等参数: 首轮 LLM 消息 roles="
        f"{[m.get('role') for m in llm.calls[0]] if isinstance(llm, _FakeLLM) and llm.calls else '（真实 LLM 由框架 _call_llm 前置）'}"),
        f"记忆自动召回: 命中注入={'是' if mem_recalled else '否（本次输入未命中预置记忆）'}",
        f"记忆自动写回: say 前 {mem_before} 条 → say 后 {mem_after} 条"
        + ("（+1 问答对自动入库）" if mem_after > mem_before else "（写回应发生，属异常）"),
    ])

    # ---- ⑥ 可观测：agent.stream 内置事件流 + TimelineProjection + 自动 trace ----
    envelopes = [e async for e in agent.stream.subscribe(STREAM_TOPIC)]
    timeline = TimelineProjection().project(envelopes)
    report.add("⑥ 可观测（agent.stream + agent.trace 内置）", [
        f"事件流 topic={STREAM_TOPIC}：{len(envelopes)} 条（自动发布，无需 StreamGateway）",
        *[f"  seq={e.seq} type={e.type} synthetic={e.data.get('seq_synthetic', False)}"
          for e in envelopes],
        f"TimelineProjection: {timeline.data}",
        (f"agent.trace 自动 span: {len(agent.trace.spans())} 条 "
        f"({sorted({s.component for s in agent.trace.spans()})})——llm/tool 调用自动埋点"),
    ])

    # ---- ⑦ 治理诊断：审计同源链 ----
    rt = agent.runtime
    audit_count = len(rt.audit.events()) if rt is not None else 0
    report.add("⑦ 治理诊断（只读）", [
        (f"审计链: {audit_count} 条，verify={verify_audit_chain(rt.audit) if rt is not None else '-'}"
        "（LOW 工具直执行但必写审计）"),
        ("SecurityCapability 默认实现: LocalSandbox + AuditLogger + ApprovalFlow"
        "（CRITICAL 工具才触发审批）"),
    ])

    # ---- ⑧ Lifecycle hook（agent.on）----
    report.add("⑧ Lifecycle hook（agent.on 一等订阅）", [
        f"本次运行收到的相位通知: {hook_log or '（无）'}",
        "16 相位见 compose.LifecyclePhase；Notice 只读、hook 异常就地隔离不阻断主流程",
    ])

    # ---- ⑨ 四能力包随意组装（意图/反思/模板/Provider）----
    from harness.cognition import CascadeIntentStrategy, RegexKeywordMatcher, ThresholdReflector
    from harness.cognition.capability import CognitionCapability
    from harness.cognition.reflection_capability import ReflectionCapability
    from harness.prompt import PromptCapability, PromptTemplate
    from string import Template

    intent_cap = CognitionCapability(strategy=CascadeIntentStrategy(
        keyword=RegexKeywordMatcher({"天气": "weather", "订票": "booking"})))
    reflect_cap = ReflectionCapability(reflector=ThresholdReflector(min_len=1))
    prompt_cap = PromptCapability(templates=(PromptTemplate(
        name="weather.system", system=Template("你是${role}。"), user=Template("${input}")),))
    intent_result = await intent_cap.recognize("帮我查天气")
    critique = reflect_cap.critique("深圳今天28度晴", goal="一句话回答")
    rendered = prompt_cap.render("weather.system", role="简洁中文助手", input="深圳天气？")
    report.add("⑨ 认知/反思/模板/Provider 四能力包组装（离线）", [
        f"意图识别（CognitionCapability→CascadeIntentStrategy L1）: "
        f"label={intent_result.label}, confidence={intent_result.confidence:.2f}",
        f"反思（ReflectionCapability→ThresholdReflector）: passed={critique.passed}, "
        f"score={critique.score}",
        f"提示词模板（system/user 双段渲染）: {rendered}",
        "Provider 能力包: LLMProviderCapability.register(base_url/api_key/model, "
        "factory=OpenAI-compatible) → install_into(ModelRegistry) → ModelCapability 消费",
        "组装路径: 内置架构 classifier=cap / 组合式 intent_as_program(cap) / "
        "门面 use(cap) 三路同一策略对象",
    ])

    # ---- 诚实边界 ----
    report.add("诚实边界（框架现状，不美化）", [
        "· say() 只返回白名单文本；last_run/runtime/trace 是只读诊断，不泄漏内部消息",
        "· 记忆/事件流/审计/trace 默认进程内内存实现，无跨进程持久化与外部导出器",
        "· 外接 sink：stream= 注入自定义 EventStreamPort；或 agent.on 订阅 16 相位",
        "· LocalSandbox 线程隔离非 OS 强沙箱；本 demo 用 LOW 工具未触发审批",
        "· HITL 闭环：gate 算子挂起后 agent.resume(答复) 续跑（本 demo 工具为 LOW 不触发）",
        "· 记忆召回是哈希向量+词面混合（默认 HashEmbedder，离线确定性）；生产注入真实 embedder",
    ])

    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
