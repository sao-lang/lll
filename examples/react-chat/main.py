"""ReAct 聊天助手 · AgentKit 框架能力全景 Demo（真实 DeepSeek）。

运行：
    export DEEPSEEK_API_KEY=sk-xxx        # 密钥只走环境变量，不落任何文件
    .venv/Scripts/python examples/react-chat/main.py

    # 不联网/省钱验证接线：
    CHAT_OFFLINE=1 .venv/Scripts/python examples/react-chat/main.py

设计原则：
- 主流程只用**语义门面** api.Agent：say() 一条链出结果，零 message/effect/ctx 胶水；
  旧 demo 的 _normalize_history / 自定义 CallFn / PortCtx 构造 / usage 搬运 / runner._call
  覆盖全部删除——这些框架内部已做（见 api.agent）。
- 仍展示框架全部真实能力：门面可选注入 stream/store/budgets + capability(bus/cost_guard)，
  从 agent.runtime / agent.last_run 读可观测底座（诊断，不改 say 返回语义）。
- 所有"诚实边界"在报告末尾如实打印，不写假框架。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api import (  # noqa: E402
    Agent,
    Governance,
    StreamGateway,
    VerifyingAuditPort,
    redact_output,
    verify_audit_chain,
)
from api.wiring import STREAM_TOPIC  # noqa: E402
from archs import ReAct  # noqa: E402
from compose import DangerLevel, LLMRequest, PortCtx, ToolSpec  # noqa: E402
from harness.context import (  # noqa: E402
    ContextMemoryManager,
    ContextSection,
    TrimPolicy,
)
from harness.eval import EvalCase, RuleEval  # noqa: E402
from harness.memory import MemoryQuery, MemoryStore  # noqa: E402
from harness.model import CostGuard  # noqa: E402
from harness.observability import NoopTelemetry, TimelineProjection, Trace  # noqa: E402
from harness.rag.types import TokenBudget  # noqa: E402
from harness.security import (  # noqa: E402
    RuleGuardrail,
    SecurityCtx,
    inspect_untrusted,
)
from runtime import Budgets  # noqa: E402

# ---------------------------------------------------------------- 配置
PROMPT = os.getenv("CHAT_PROMPT", "用 get_weather 查一下深圳的天气，然后用一句话告诉我。")
OFFLINE = os.getenv("CHAT_OFFLINE") == "1"
MODEL = "deepseek-chat"
SESSION_ID = "react-chat-demo"
TRACE_ID = f"react-{int(time.time())}"

MAX_TURNS = 5
MAX_COST_USD = 0.05
COST_PER_CALL_ESTIMATE = 0.002

SYSTEM_PROMPT = (
    "你是简洁的中文助手。可用工具：get_weather(city)。"
    "用工具查天气后用一句话回答。"
)

TOOLS = [
    ToolSpec(
        name="get_weather",
        description="查询指定城市的当前天气（温度、天气状况）",
        parameters_schema={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名，如 深圳"}},
            "required": ["city"],
        },
        danger=DangerLevel.LOW,  # LOW：直执行但必写审计（demo 借此展示审计链+同源回放）
    ),
]


# ---------------------------------------------------------------- 报告工具
class Report:
    def __init__(self) -> None:
        self.sections: list[tuple[str, list[str]]] = []

    def add(self, title: str, lines: list[str]) -> None:
        self.sections.append((title, lines))

    def render(self) -> str:
        out = ["=" * 72, "ReAct 聊天助手 · AgentKit 框架能力全景报告", "=" * 72]
        for title, lines in self.sections:
            out += ["", f"【{title}】", "-" * 60, *lines]
        out += ["", "=" * 72]
        return "\n".join(out)


# ---------------------------------------------------------------- 假 LLM（离线）
class _FakeLLM:
    """离线确定性 LLM 替身（只在 demo，不冒充框架实现）。"""
    name = "fake"
    last_usage: Any = None

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> Any:
        from compose import LLMResponse, ToolCall, Usage
        self.n += 1
        if self.n == 1:
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


class _SystemPromptLLM:
    """用户侧适配：给首轮补 system 提示（ReAct 只发 user 消息；不改框架）。

    包装任意 LLMPort，在 messages 无 system 时前置一条。业务侧常见适配，非框架胶水。
    """
    def __init__(self, inner: Any, system: str) -> None:
        self._inner = inner
        self._system = system
        self.name = getattr(inner, "name", "wrapped")
        self.last_usage = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> Any:
        from compose import LLMRequest as _Req
        msgs = [dict(m) if isinstance(m, dict) else {"role": "user", "content": str(m)}
                for m in (req.messages or [])]
        if not any(str(m.get("role")) == "system" for m in msgs):
            msgs.insert(0, {"role": "system", "content": self._system})
        wrapped = _Req(messages=msgs, tools_catalog=req.tools_catalog,
                       model=req.model, max_tokens=req.max_tokens)
        resp = await self._inner.complete(wrapped, ctx)
        self.last_usage = getattr(self._inner, "last_usage", None) or getattr(resp, "usage", None)
        return resp

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> Any:  # pragma: no cover
        return await self._inner.stream(req, ctx)


def _get_weather(**kwargs: Any) -> dict[str, Any]:
    """本地天气工具（确定性，无网络）。"""
    city = str(kwargs.get("city") or "未知")
    return {"city": city, "temperature": 28, "condition": "晴", "source": "demo-local"}


async def main() -> None:  # noqa: C901 - demo 线性展示所有能力
    report = Report()

    # ---- ① 安全护栏 ----
    ctx = PortCtx(session_id=SESSION_ID, run_id=TRACE_ID,
                  meta={"scope": {"tenant": "demo"}, "secret_refs": []})
    sec_ctx = SecurityCtx(session_id=SESSION_ID, run_id=TRACE_ID,
                          source="input", direction="input", trust=False)
    injection = inspect_untrusted(PROMPT, sec_ctx)
    verdict = RuleGuardrail().check(PROMPT, sec_ctx)
    report.add("① 安全护栏 / 注入检测", [
        f"用户输入: {PROMPT}",
        f"注入检测等级: {injection.level}（rules_hit={injection.rules_hit}）",
        f"护栏裁决: {verdict.action}（rule={verdict.rule or '-'}）",
    ])
    if verdict.action == "deny":
        report.add("结果", ["输入被护栏拒绝，未调用 LLM（fail-closed）"])
        print(report.render())
        return

    # ---- ② 长期记忆 + 上下文管理 ----
    memory = MemoryStore()
    await memory.write("profile", "language", "中文", ctx)
    recalls = await memory.recall(MemoryQuery(text=PROMPT, top_k=3), ctx)
    ctxmgr = ContextMemoryManager(memory=memory)
    sections = [
        ContextSection("system", SYSTEM_PROMPT, 80, 0, TrimPolicy.PIN),
        ContextSection("memory", f"历史记忆命中 {len(recalls)} 条", 40, 10, TrimPolicy.SUMMARY),
        ContextSection("input", PROMPT, 80, 90, TrimPolicy.TAIL),
    ]
    assembled = ctxmgr.assemble(sections, TokenBudget(limit=200))
    report.add("② 长期记忆 + 上下文管理", [
        f"MemoryStore KV 写入后读回: {await memory.read('profile', 'language', ctx)}",
        f"recall 命中: {len(recalls)} 条",
        f"上下文组装: {len(assembled)} 条消息（预算 200 tokens，PIN/SUMMARY/TAIL 策略）",
        "  " + " | ".join(f"{m.role}:{str(m.content)[:30]}" for m in assembled),
    ])

    # ---- ③ 链路追踪（手动埋点，Runner 不自动产 span）----
    telemetry = NoopTelemetry()
    trace = Trace(trace_id=TRACE_ID, telemetry=telemetry)

    # ---- ④/⑤ 事件流 + 治理 + 预算 ----
    stream_gateway = StreamGateway()
    stream = stream_gateway.stream()
    gov = Governance(cost_cap=MAX_COST_USD, rollout_percent=100)
    gov.flags.set("react_chat", True)
    allowed = gov.allow("session.start", cost=0.0, key=SESSION_ID)
    budgets = Budgets(max_turns=MAX_TURNS, max_tokens=4000, max_cost_usd=MAX_COST_USD)
    report.add("⑤ 预算管理 / 治理", [
        f"Governance.allow(session.start)={allowed}；熔断={gov.killswitch.enabled}；"
        f"特性开关 react_chat={gov.flags.is_on('react_chat')}",
        f"R1 Budgets: max_turns={MAX_TURNS}, max_tokens=4000, max_cost_usd={MAX_COST_USD}",
        f"CostGuard 调用前硬闸: max_cost_usd={MAX_COST_USD}（估算 ${COST_PER_CALL_ESTIMATE}/次）",
    ])

    # ---- ⑥ 真实/离线 LLM ----
    if OFFLINE:
        raw_llm: Any = _FakeLLM()
        cost_guard = None
        llm_label = "本地假 LLM（CHAT_OFFLINE=1，不联网）"
    else:
        from ports.adapters.llm import OpenAICompatLLM
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            print("错误：需要 DEEPSEEK_API_KEY 环境变量（或设 CHAT_OFFLINE=1 离线演示）")
            return
        raw_llm = OpenAICompatLLM(base_url="https://api.deepseek.com/v1",
                                  api_key=api_key, model=MODEL)
        cost_guard = CostGuard(max_cost_usd=MAX_COST_USD)
        llm_label = f"DeepSeek OpenAI-compatible（{MODEL}）"
    llm = _SystemPromptLLM(raw_llm, SYSTEM_PROMPT)
    report.add("⑥ 真实 LLM 网关", [
        f"后端: {llm_label}",
        "ModelGateway: CostGuard 调用前预留 + CostAttributor 记账（真实现）",
        "用户侧适配: _SystemPromptLLM 首轮补 system 提示（不改框架）",
    ])

    # ---- ⑦/⑨ 语义门面一条链：装配 + ReAct + 出结果（零胶水）----
    from harness.model import ModelCapability
    arch = ReAct(TOOLS, llm=MODEL, max_turns=2)
    check = arch.static_check()

    agent = Agent(arch, session_id=SESSION_ID, stream=stream, budgets=budgets)
    agent.capability(ModelCapability(llm=llm, cost_guard=cost_guard))
    agent.register_tool("get_weather", _get_weather, danger=DangerLevel.LOW,
                        description=TOOLS[0].description)

    with trace.span("react.session", component="agent"):
        answer = agent.say(PROMPT)  # 零胶水：say() 一条链出结果

    rt = agent.runtime
    run = agent.last_run
    assert rt is not None and run is not None, "门面应暴露诊断运行期"
    # bus 同源：after_model 相位经装配层 bus；门面 ModelCapability 未注入 bus 时网关自建，
    # 这里把 rt.bus 展示出来（ AFTER_MODEL 订阅见诚实边界）。
    report.add("⑨ ReAct 执行结果（语义门面 say）", [
        f"static_check: ok={check.ok}（ports={len(check.ports_required)}）",
        f"say() 返回: {answer[:200]}",
        f"run.status={run.status}, reason={run.reason or '-'}, session_id={run.session_id}",
        f"trace spans: {len(trace.spans())} 条，trace_id={TRACE_ID}",
        f"R1 预算消耗: turns={run.state.budgets.used_turns}, "
        f"tokens={run.state.budgets.used_tokens}, cost=${run.state.budgets.used_cost_usd:.6f}",
    ])

    # ---- ⑩ 可观测：事件流投影 ----
    async def drain() -> list[Any]:
        return [e async for e in stream.subscribe(STREAM_TOPIC)]
    envelopes = await drain()
    timeline = TimelineProjection().project(envelopes)
    report.add("⑩ 可观测 / 事件流投影", [
        f"事件流 topic={STREAM_TOPIC}：{len(envelopes)} 条",
        *[f"  seq={e.seq} type={e.type} ts={e.ts:.3f} synthetic={e.data.get('seq_synthetic', False)}"
          for e in envelopes],
        f"TimelineProjection: {timeline.data}",
    ])

    # ---- ⑪ checkpoint + 审计同源 + 回放 ----
    from api.wiring import R1AuditEventSink, replay_audit_events
    from harness.durability import replay_snapshot

    audit_replay = await replay_audit_events(R1AuditEventSink(rt.checkpoint.inner), SESSION_ID)
    snap = await rt.checkpoint.manager().load(SESSION_ID)
    replayed = replay_snapshot(snap) if snap is not None else None
    report.add("⑪ checkpoint / 审计同源 / 回放", [
        f"终态 checkpoint: {'已落盘' if snap else '无'}，"
        f"replay 恢复 turns={replayed.budgets.used_turns if replayed else '-'}",
        f"审计链: {len(rt.audit.events())} 条，verify={verify_audit_chain(rt.audit)}",
        f"审计同源回放（R1 EventStore）: {len(audit_replay)} 条",
    ])

    # ---- ⑫ 输出脱敏 + 记忆写回 + 评测 ----
    redaction = redact_output(
        answer, ctx,
        secret_refs=[os.getenv("DEEPSEEK_API_KEY", "")] if not OFFLINE else [],
    )
    safe_output = redaction.redacted if redaction.action == "redact" else answer
    memory.ingest(f"用户问：{PROMPT}；助手答：{str(safe_output)[:120]}", ctx, confidence=0.8)
    ctxmgr.commit_and_compact(run.state, str(safe_output), ctx)
    evaluator = RuleEval(cases={
        "has_answer": EvalCase(case_id="has_answer", input=PROMPT,
                               expected="non-empty", actual="non-empty" if safe_output else ""),
    })
    eval_report = await evaluator.regression("react-chat-v1", ctx)
    report.add("⑫ 输出脱敏 / 记忆写回 / 评测", [
        f"脱敏后输出: {str(safe_output)[:120]}",
        f"记忆写回: MemoryStore 现有 {memory.tier_count()} 条记忆项",
        f"上下文压缩 trace: {ctxmgr.trace}",
        f"RuleEval 回归: {eval_report.passed}/{eval_report.total} passed, "
        f"average={eval_report.average:.2f}",
        f"成本账本: ModelGateway 内部 CostAttributor 记账（R1 扣费 ${run.state.budgets.used_cost_usd:.6f}）",
    ])

    # ---- 诚实边界 ----
    report.add("诚实边界（框架现状，不美化）", [
        "· 主流程只用语义门面 say()；_normalize_history/自定义CallFn/PortCtx/usage搬运已由框架内部完成",
        "· 门面 last_run/runtime 仅作可观测诊断；say() 仍只返回白名单文本，不泄漏内部消息",
        "· 记忆/上下文/事件流/trace 默认进程内内存实现，无跨进程持久化与外部导出器",
        "· trace 需手动埋点，Runner 不自动产 span",
        "· LocalSandbox 线程隔离非 OS 强沙箱；本 demo 用 LOW 工具（直执行但必写审计）未触发审批",
        "· ModelGateway 的 usage 由门面内部搬进 Result.usage（demo 不再手动搬运）",
        "· 用户侧 _SystemPromptLLM 补 system 提示是业务适配；ReAct 默认首轮只发 user 消息",
    ])

    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
