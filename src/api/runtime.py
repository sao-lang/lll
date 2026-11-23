# purity-exempt-file: 运行期装配：协议形状/动态内容边界用 object/DynamicValue，理由见逐行豁免
"""R11 运行期装配：把六项接线真正注入 Runner / ToolRegistry / ModelGateway。

不是"持有对象 + getter"——`assemble()` 构造真实 Runner（store/notify 注入）、
真实 ToolRegistry（sandbox/human/audit 注入）、真实 LifecycleBus（供 ModelGateway）。
全部落在 api(L3)，不改 R1/R5/R9 源码。

B2 修复：装配链唯一——从 SecurityCapability 取三 Port（sandbox/human/audit），
不再自造 sandbox=None + _NoHuman，profile 装配与运行装配汇成一条链。
"""
from __future__ import annotations

from collections.abc import Callable

from compose import (
    CallFn,
    DangerLevel,
    LifecycleBus,
    PortCtx,
    Registry,
    SandboxPort,
    ToolCall,
)
from compose.notify import runner_notifier
from harness.durability import CheckpointManager
from harness.model import ModelGateway
from harness.observability import InMemoryEventStream
from harness.security import ApprovalFlow, AuditLogger, SecurityCapability
from harness.tools import ToolsCapability, ToolRegistry
from runtime import (
    DynamicValue,
    Effect,
    EventStore,
    InMemoryEventStore,
    Invoke,
    Result,
    Runner,
    Session,
)

from .wiring import (
    ApprovingHumanPort,
    CheckpointEventStore,
    R1AuditEventSink,
    R1SeqTracker,
    VerifyingAuditPort,
    bind_stream_projection,
)

__all__ = ["AgentRuntime", "assemble_runtime", "capability_tools"]


class AgentRuntime:
    """运行期聚合：真实 Runner + 六个接线端口（生产路径消费方）。

    字段公开供调用方直接跑/恢复；不是数据 DTO，持有真实执行器。
    """
    name = "agent_runtime"

    def __init__(self, runner: Runner, bus: LifecycleBus, tools: ToolRegistry | None,
                 approval: ApprovingHumanPort | None, audit: VerifyingAuditPort | None,
                 checkpoint: CheckpointEventStore, stream: InMemoryEventStream | None = None,
                 seq_tracker: R1SeqTracker | None = None) -> None:
        """构造运行期（由 assemble_runtime 统一创建）。"""
        self.runner = runner
        self.bus = bus
        self.tools = tools
        self.approval = approval
        self.audit = audit
        self.checkpoint = checkpoint
        self.stream = stream
        self._seq_tracker = seq_tracker

    async def run(self, session: Session) -> object:
        """真实执行（绑定活会话供 R1 seq 同源投影，委托 Runner.run）。"""
        if self._seq_tracker is not None:
            self._seq_tracker.bind_session(session)
        return await self.runner.run(session)

    async def resume(self, session: Session, answer: DynamicValue) -> object:
        """挂起后精确续跑（委托 Runner.resume）。"""
        if self._seq_tracker is not None:
            self._seq_tracker.bind_session(session)
        return await self.runner.resume(session, answer)

    def model_gateway(self, llm: object) -> ModelGateway:
        """用同一 bus 构造模型网关（after_model 相位真实接入）。"""
        return ModelGateway(llm=llm, bus=self.bus)  # type: ignore[arg-type]


def _tool_call_handler(registry: ToolRegistry) -> CallFn:
    """把 Invoke(port=tool) 桥接成 ToolRegistry.call（B-3 生产最后一公里）。

    R1 只认 CallFn(effect, ctx)；工具层 ToolRegistry.call(ToolCall, PortCtx)。
    本桥接在装配层做 DTO 转换，不侵入 R1/工具层源码：payload.name/args/call_id/danger
    映射到 ToolCall，ToolResult.ok/value/error 映射回 Result。审批+沙盒+审计全在
    registry.call 内真实执行（生产调用链不再是死代码）。
    """
    async def handler(effect: Effect, ctx: object) -> Result:
        if not isinstance(effect, Invoke):
            return Result.failure("INVALID_EFFECT", "tool dispatch only handles Invoke")
        payload = effect.payload
        ctx_obj = ctx if isinstance(ctx, PortCtx) else PortCtx()
        raw_args = payload.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        raw_danger = payload.get("danger")
        danger = (DangerLevel.coerce(raw_danger)
                  if isinstance(raw_danger, (str, DangerLevel))
                  else DangerLevel.CRITICAL)  # 未知/缺 danger 按最严格处理（fail-closed）
        call = ToolCall(
            call_id=str(payload.get("call_id") or effect.trace_id or "tool-call"),
            name=str(payload.get("name") or ""),
            args=args,
            danger=danger,
            source=str(payload.get("source") or "local"),
        )
        result = await registry.call(call, ctx_obj)
        if result.ok:
            return Result.success(result.value)
        return Result.failure(result.error or "TOOL_ERROR",
                              result.error or "tool call failed")
    return handler


def assemble_runtime(
    call: CallFn | None = None,
    *,
    approval: ApprovingHumanPort | None = None,
    audit: VerifyingAuditPort | None = None,
    stream: InMemoryEventStream | None = None,
    manager: CheckpointManager | None = None,
    tools: ToolRegistry | None = None,
    security: SecurityCapability | None = None,
    bus: LifecycleBus | None = None,
    store: EventStore | None = None,
    sink: R1AuditEventSink | None = None,
    checkpoint_every: int = 1,
) -> AgentRuntime:
    """真实装配：构造 Runner/Registry/三 Port 接线的运行期。

    - ①② 未注入 approval/audit 时用 SecurityCapability 的真实默认（CRITICAL 触发 /
      审计验链 + R1 同源落盘）；
    - ③ secret 由 builder.redact 在输出边界消费（不在此重复）；
    - ④ bus 订阅三相位并作为 Runner(notify=runner_notifier(bus)) 注入；
      可注入 bus 以复用 builder.project_events() 建的总线（消除 S1 双 bus）；
    - ⑤ CheckpointEventStore 作为 Runner(store=...) 注入，与 audit sink 共享同一 R1 store；
    - ⑥ CI gate 由 CLI.eval 消费（不在此重复）。
    """
    sec = security or SecurityCapability()
    # 单底座：manager 自带 store 时复用它，否则新建；audit sink/checkpoint 共用同一实例。
    # 单底座：显式 store > manager 自带 store > 新建；audit sink/checkpoint 共用同一实例。
    mgr = manager or CheckpointManager(store=store or InMemoryEventStore())
    shared_store = store if store is not None else mgr.store
    mgr.store = shared_store
    target_stream = stream or InMemoryEventStream()
    seq_tracker = R1SeqTracker()
    active_bus = bus or bind_stream_projection(target_stream, seq_source=seq_tracker)

    approving = approval or ApprovingHumanPort(sec.human, sec.approval)
    audit_sink = sink or R1AuditEventSink(shared_store)
    if audit is not None:
        # 外部注入审计也同源：装配期把同一 sink 绑到其 inner（已有 sink 不覆盖）。
        audit.bind_sink(audit_sink)
        auditing = audit
    else:
        auditing = VerifyingAuditPort(AuditLogger(sink=audit_sink))
    checkpoint_store = CheckpointEventStore(mgr, inner=shared_store, seq_tracker=seq_tracker)

    # ①② B2 修复：真实注入 SecurityCapability 的 sandbox/human/audit（消除 sandbox=None 断链）。
    wired_tools = tools or ToolRegistry(
        sandbox=sec.sandbox, human=approving, audit=auditing,
    )

    # B-3 修复：未注入 call 时用已接线的 ToolRegistry 构造默认 CallFn——真实
    # Agent 跑 Invoke(port=tool) 会经此进审批+沙盒+审计链，而非悬空死代码。
    tool_handler = _tool_call_handler(wired_tools)

    async def default_call(effect: Effect, ctx: object) -> Result:
        if not isinstance(effect, Invoke):
            return Result.failure("INVALID_EFFECT", "default_call only handles Invoke")
        if effect.port == "tool":
            return await tool_handler(effect, ctx)
        # 非 tool port 无默认实现：显式失败而非静默放行（fail-closed）。
        return Result.failure("PORT_MISSING", f"no default handler for port: {effect.port}")

    active_call = call if call is not None else default_call
    runner = Runner(active_call, store=checkpoint_store, notify=runner_notifier(active_bus),
                    checkpoint_every=checkpoint_every)
    return AgentRuntime(runner, active_bus, wired_tools, approving, auditing, checkpoint_store,
                        target_stream, seq_tracker=seq_tracker)


def capability_tools(
    approval: ApprovingHumanPort, audit: VerifyingAuditPort,
    *, sandbox: SandboxPort | None = None,
) -> ToolsCapability:
    """用已接线的三 Port 构造 ToolsCapability（生产装配入口）。

    Args:
        approval: 审批包装器。
        audit: 审计包装器。
        sandbox: SandboxPort 实现；None 时用 LocalSandbox 默认（生产真执行）。
    """
    from harness.security import LocalSandbox

    reg = ToolRegistry(sandbox=sandbox or LocalSandbox(allow_sync=True),
                       human=approval, audit=audit)
    return ToolsCapability(registry=reg)
