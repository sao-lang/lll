"""ReAct 聊天助手 · AgentKit 框架能力全景 Demo（真实 DeepSeek）。

运行：
    export DEEPSEEK_API_KEY=sk-xxx        # 密钥只走环境变量，不落任何文件
    .venv/Scripts/python examples/react-chat/main.py

    # 不联网/省钱验证接线：
    CHAT_OFFLINE=1 .venv/Scripts/python examples/react-chat/main.py

设计原则：只用框架里的真实实现，不写假框架。所有"诚实边界"在报告末尾如实打印。
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
    Governance,
    StreamGateway,
    VerifyingAuditPort,
    assemble_runtime,
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
from harness.model import CostAttributor, CostGuard, ModelGateway  # noqa: E402
from harness.observability import NoopTelemetry, TimelineProjection, Trace  # noqa: E402
from harness.rag.types import TokenBudget  # noqa: E402
from harness.security import (  # noqa: E402
    RuleGuardrail,
    SecurityCtx,
    inspect_untrusted,
)
from runtime import AgentState, Budgets, Invoke, Result, Session  # noqa: E402

# ---------------------------------------------------------------- 配置
PROMPT = os.getenv("CHAT_PROMPT", "用 get_weather 查一下深圳的天气，然后用一句话告诉我。")
OFFLINE = os.getenv("CHAT_OFFLINE") == "1"
MODEL = "deepseek-chat"
SESSION_ID = "react-chat-demo"
TRACE_ID = f"react-{int(time.time())}"

# 预算：给真实调用设硬上限。
# 注意 R1 每个 Effect 都扣 1 turn（含 Terminate），2 轮 ReAct = llm+tool+llm+terminate = 4 turns，
# 留一个余量设 5；arch 层 max_turns 单独限制 LLM 轮次。
MAX_TURNS = 5
MAX_COST_USD = 0.05
COST_PER_CALL_ESTIMATE = 0.002  # 保守估算（DeepSeek 实际单价更低）；仅用于调用前硬闸

# 工具定义（SAFE，本地确定性，不联网）
# parameters_schema 是关键：OpenAI-compatible 适配器会把它转成 function calling 的 parameters，
# 缺了它 LLM 不知道工具要什么参数（框架会用空 {} 披露，工具调用概率大降）。
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
    """把各能力的可观测产物收集成一份结构化报告。"""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list[str]]] = []

    def add(self, title: str, lines: list[str]) -> None:
        """加一个分节。"""
        self.sections.append((title, lines))

    def render(self) -> str:
        """渲染完整报告。"""
        out = ["=" * 72, "ReAct 聊天助手 · AgentKit 框架能力全景报告", "=" * 72]
        for title, lines in self.sections:
            out += ["", f"【{title}】", "-" * 60, *lines]
        out += ["", "=" * 72]
        return "\n".join(out)


# ---------------------------------------------------------------- 假 LLM（离线模式）
class _FakeLLM:
    """离线用的确定性 LLM 替身（只在 tests/demo，不冒充框架实现）。"""

    name = "fake"
    last_usage: Any = None

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> Any:
        """两轮：先发工具调用，再给结论。"""
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


# ---------------------------------------------------------------- 工具实现
def _get_weather(**kwargs: Any) -> dict[str, Any]:
    """本地天气工具（确定性，无网络）——演示工具路由，非真实天气服务。"""
    city = str(kwargs.get("city") or "未知")
    return {"city": city, "temperature": 28, "condition": "晴", "source": "demo-local"}


# ---------------------------------------------------------------- 主流程
async def main() -> None:  # noqa: C901 - demo 需要线性展示所有能力
    """跑一次完整 ReAct 会话，并把每项框架的接线证据收进报告。"""
    report = Report()

    # ---- ① 安全护栏：对用户输入做注入检测（真实 RuleGuardrail）----
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

    # ---- ② 长期记忆 + 上下文管理（真实 MemoryStore / ContextMemoryManager）----
    memory = MemoryStore()
    await memory.write("profile", "language", "中文", ctx)
    recalls = await memory.recall(MemoryQuery(text=PROMPT, top_k=3), ctx)
    ctxmgr = ContextMemoryManager(memory=memory)
    sections = [
        ContextSection("system", "你是简洁的中文助手；可用工具：get_weather", 80, 0, TrimPolicy.PIN),
        ContextSection("memory", f"历史记忆命中 {len(recalls)} 条", 40, 10, TrimPolicy.SUMMARY),
        ContextSection("input", PROMPT, 80, 90, TrimPolicy.TAIL),
    ]
    assembled = ctxmgr.assemble(sections, TokenBudget(limit=200))
    report.add("② 长期记忆 + 上下文管理", [
        f"MemoryStore KV 写入后读回: {await memory.read('profile', 'language', ctx)}",
        f"recall 命中: {len(recalls)} 条",
        f"上下文组装: {len(assembled)} 条消息（预算 200 tokens，PIN/SUMMARY/TAIL 策略）",
        f"  " + " | ".join(f"{m.role}:{str(m.content)[:30]}" for m in assembled),
    ])

    # ---- ③ 链路追踪（真实 Trace / NoopTelemetry，手动埋点）----
    telemetry = NoopTelemetry()
    trace = Trace(trace_id=TRACE_ID, telemetry=telemetry)

    # ---- ④ 可观测：事件流 + 时间线投影（真实 StreamGateway）----
    stream_gateway = StreamGateway()
    stream = stream_gateway.stream()

    # ---- ⑤ 治理 + 预算（真实 Governance / Budgets / CostGuard）----
    gov = Governance(cost_cap=MAX_COST_USD, rollout_percent=100)
    gov.flags.set("react_chat", True)
    allowed = gov.allow("session.start", cost=0.0, key=SESSION_ID)
    budgets = Budgets(max_turns=MAX_TURNS, max_tokens=4000, max_cost_usd=MAX_COST_USD)
    init_state = AgentState(budgets=budgets)
    report.add("⑤ 预算管理 / 治理", [
        f"Governance.allow(session.start)={allowed}；熔断={gov.killswitch.enabled}；"
        f"特性开关 react_chat={gov.flags.is_on('react_chat')}",
        f"R1 Budgets: max_turns={MAX_TURNS}, max_tokens=4000, max_cost_usd={MAX_COST_USD}",
        f"CostGuard 调用前硬闸: max_cost_usd={MAX_COST_USD}（每次估算 ${COST_PER_CALL_ESTIMATE}）",
    ])

    # ---- ⑥ 真实 LLM 网关（DeepSeek OpenAI-compatible）----
    ledger = CostAttributor()
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
        llm = OpenAICompatLLM(
            base_url="https://api.deepseek.com/v1",
            api_key=api_key,
            model=MODEL,
        )
        cost_guard = CostGuard(max_cost_usd=MAX_COST_USD)
        llm_label = f"DeepSeek OpenAI-compatible（{MODEL}）"
    # bus 在装配运行期后接入（见 ⑦），这里先不传；after_model 由装配层 bus 承载。
    gateway = ModelGateway(
        llm=llm,
        cost_guard=cost_guard,
        cost_estimator=lambda req: COST_PER_CALL_ESTIMATE,
    )
    report.add("⑥ 真实 LLM 网关", [
        f"后端: {llm_label}",
        "ModelGateway: CostGuard 调用前预留 + CostAttributor 记账（真实现）",
    ])

    # ---- ⑦ 装配运行期（真实 Runner + 审计 + checkpoint + 事件流 bus）----
    rt = assemble_runtime(
        store=None,  # 内部共享 R1 EventStore（checkpoint 与审计同源）
        stream=stream,  # 复用同一个事件流实例
        checkpoint_every=1,
    )
    # 把真实 bus 接到网关（after_model 相位经此发事件）
    gateway._bus = rt.bus  # noqa: SLF001 - demo 显式接入装配层的同一 bus

    rt.tools.register(TOOLS[0], _get_weather)

    # ---- ⑧ 自定义 CallFn：把 llm/tool port 接到网关与工具注册表 ----
    llm_calls = {"n": 0}
    llm_errors: list[str] = []

    def _normalize_history(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 ReAct 内部消息格式归一化成 OpenAI-compatible 要求的结构。

        ReAct 产出：assistant.tool_calls=[{name,args,call_id}]、tool.content=dict；
        OpenAI/DeepSeek 要求：assistant.tool_calls=[{id,type:function,function:{name,arguments}}]，
        tool 消息带 tool_call_id 且 content 为字符串。这是 demo 层的适配，不改框架源码。
        """
        normalized: list[dict[str, Any]] = []
        # ReAct 的 tool 消息只有 name 没有 call_id；按出现顺序与最近一轮 assistant 的 calls 对齐。
        id_by_name: dict[str, str] = {}
        for m in msgs:
            role = str(m.get("role") or "user")
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    if not isinstance(c, dict):
                        continue
                    cid = str(c.get("call_id") or c.get("id") or "call")
                    name = str(c.get("name") or "")
                    id_by_name[name] = cid
                    calls.append({
                        "id": cid, "type": "function",
                        "function": {
                            "name": name,
                            "arguments": str(c.get("arguments")
                                             or __import__("json").dumps(c.get("args") or {})),
                        },
                    })
                normalized.append({"role": "assistant", "content": str(m.get("content") or ""),
                                   "tool_calls": calls})
            elif role == "tool":
                name = str(m.get("name") or "")
                normalized.append({
                    "role": "tool",
                    "tool_call_id": str(m.get("call_id") or id_by_name.get(name) or name or "call"),
                    "content": __import__("json").dumps(m.get("content"), ensure_ascii=False),
                })
            else:
                normalized.append({"role": role, "content": str(m.get("content") or "")})
        return normalized

    async def call(effect: Any, runtime_ctx: Any) -> Result:
        """真实 CallFn：llm→ModelGateway；tool→ToolRegistry（审批/沙盒/审计）。

        R1 Runner 传 RuntimeCtx（无 run_id），Port 层要 PortCtx（含 run_id）——
        demo 显式适配（这也是框架真实接线时你需要做的一步）。
        """
        if not isinstance(effect, Invoke):
            return Result.failure("INVALID_EFFECT", "demo only handles Invoke")
        pctx = PortCtx(
            session_id=str(getattr(runtime_ctx, "session_id", SESSION_ID)),
            run_id=TRACE_ID,
            meta=dict(getattr(runtime_ctx, "meta", {}) or {}),
        )
        if effect.port == "llm":
            llm_calls["n"] += 1
            with trace.span(f"llm.call#{llm_calls['n']}", component="model") as sp:
                history = [dict(m) if isinstance(m, dict)
                           else {"role": "user", "content": str(m)}
                           for m in (effect.payload.get("messages") or [])]
                # ReAct 首轮 messages 为空：注入用户输入 + 系统提示，否则真实 LLM 收到空消息会报错。
                if not history:
                    history = [
                        {"role": "system", "content": "你是简洁的中文助手。可用工具：get_weather(city)。用工具查天气后用一句话回答。"},
                        {"role": "user", "content": PROMPT},
                    ]
                req = LLMRequest(
                    messages=_normalize_history(history),
                    tools_catalog=TOOLS,  # 让适配器把工具 schema 传给 DeepSeek（原生 function calling）
                    model=str(effect.payload.get("model") or MODEL),
                    max_tokens=150,
                )
                resp = await gateway.complete(req, pctx)
                if resp.error:
                    llm_errors.append(resp.error)
                    trace.record_metric("llm.tokens", 0.0, tags={"call": llm_calls["n"]})
                    return Result.failure(resp.error.split(":")[0], resp.error)
                # 关键桥接：usage 放进 Result.usage，R1 fold 才会扣 Budgets（框架需手动搬运）
                trace.record_metric(
                    "llm.tokens",
                    float(resp.usage.prompt_tokens + resp.usage.completion_tokens),
                    tags={"call": llm_calls["n"]},
                )
                trace.record_metric("llm.cost", resp.usage.cost_usd,
                                    tags={"call": llm_calls["n"]})
                tool_calls = [
                    {"name": tc.name, "args": tc.args, "call_id": tc.call_id}
                    for tc in resp.tool_calls
                ]
                return Result.success(
                    {"content": resp.content or "", "tool_calls": tool_calls, "usage": resp.usage},
                    usage=resp.usage,
                )
        if effect.port == "tool":
            with trace.span("tool.call", component="tools") as sp:
                nested = effect.payload.get("call")
                src = nested if isinstance(nested, dict) else effect.payload
                from compose import DangerLevel as _DL, ToolCall as _TC
                tc = _TC(
                    call_id=str(src.get("call_id") or "demo-tool"),
                    name=str(src.get("name") or ""),
                    args=dict(src.get("args") or {}),
                    danger=_DL.LOW,  # 以注册 spec 为准；这里与 TOOLS 的 danger 保持一致
                )
                res = await rt.tools.call(tc, pctx)
                if res.ok:
                    return Result.success(res.value)
                return Result.failure(res.error or "TOOL_ERROR", res.error or "tool failed")
        return Result.failure("PORT_MISSING", f"no handler for port {effect.port}")

    # ---- ⑨ 跑真实 ReAct（static_check → compile → Runner）----
    arch = ReAct(TOOLS, llm=MODEL, max_turns=2)  # 架构层：最多 2 次 LLM（1 工具轮 + 1 结论轮）
    check = arch.static_check()
    if not check.ok:
        print(f"static_check 失败: {check.errors}")
        return

    with trace.span("react.session", component="agent"):
        rt.runner._call = call  # noqa: SLF001 - demo 覆盖装配默认 call，接入真实 LLM
        session = Session(session_id=SESSION_ID, program_factory=lambda: arch.compile(),
                          state=init_state)
        run = await rt.runner.run(session)

    spans = trace.spans()
    report.add("⑨ ReAct 执行结果 + 链路追踪", [
        f"static_check: ok={check.ok}（ports={len(check.ports_required)}）",
        f"run.status={run.status}, reason={run.reason or '-'}",
        f"output: {str(run.output)[:200]}",
        f"LLM 调用次数: {llm_calls['n']}（真实调用，已控制在 {MAX_TURNS} 轮内）",
        f"LLM 错误: {llm_errors or '无'}",
        f"trace spans: {len(spans)} 条，trace_id={TRACE_ID}",
        *[f"  - {s.name:20s} parent={s.parent_id or '-':20s} component={s.component:8s} "
          f"dur={s.duration_ms:.1f}ms" for s in spans],
        f"trace metrics: {telemetry.metrics()}",
        f"R1 预算消耗: turns={session.state.budgets.used_turns}, "
        f"tokens={session.state.budgets.used_tokens}, cost=${session.state.budgets.used_cost_usd:.6f}",
    ])

    # ---- ⑩ 可观测：事件流 + 时间线投影 ----
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
    snap = await rt.checkpoint.manager().load(SESSION_ID)  # R10 DTO（含 events）
    replayed = replay_snapshot(snap) if snap is not None else None
    report.add("⑪ checkpoint / 审计同源 / 回放", [
        f"终态 checkpoint: {'已落盘' if snap else '无'}，"
        f"replay 恢复 turns={replayed.budgets.used_turns if replayed else '-'}",
        f"审计链: {len(rt.audit.events())} 条，verify={verify_audit_chain(rt.audit)}",
        f"审计同源回放（R1 EventStore）: {len(audit_replay)} 条",
    ])

    # ---- ⑫ 输出脱敏 + 记忆写回 + 评测 ----
    redaction = redact_output(
        run.output, ctx,
        secret_refs=[os.getenv("DEEPSEEK_API_KEY", "")] if not OFFLINE else [],
    )
    safe_output = redaction.redacted if redaction.action == "redact" else run.output
    memory.ingest(f"用户问：{PROMPT}；助手答：{str(safe_output)[:120]}", ctx, confidence=0.8)
    ctxmgr.commit_and_compact(session.state, str(safe_output), ctx)
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
        f"成本账本快照: {gateway.ledger.snapshot()}",
    ])

    # ---- 诚实边界 ----
    report.add("诚实边界（框架现状，不美化）", [
        "· 记忆/上下文/事件流/trace 默认均为进程内内存实现，无跨进程持久化与外部导出器",
        "· trace 需手动埋点，Runner 不自动产生 span",
        "· cost_estimator 为保守估算；DeepSeek 真实单价需自行按 token 计算",
        "· LocalSandbox 为线程隔离，非 OS 强沙箱；本 demo 仅用 SAFE 工具未触发审批",
        "· ModelGateway 的 usage 需手动搬进 Result.usage，R1 Budgets 才会扣费（本 demo 已搬运）",
    ])

    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
