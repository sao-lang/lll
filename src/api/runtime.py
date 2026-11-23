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

from compose import CallFn, LifecycleBus, PortCtx, Registry, SandboxPort
from compose.notify import runner_notifier
from harness.durability import CheckpointManager
from harness.model import ModelGateway
from harness.observability import InMemoryEventStream
from harness.security import ApprovalFlow, AuditLogger, SecurityCapability
from harness.tools import ToolsCapability, ToolRegistry
from runtime import DynamicValue, InMemoryEventStore, Runner, Session

from .wiring import (
    ApprovingHumanPort,
    CheckpointEventStore,
    R1AuditEventSink,
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
                 checkpoint: CheckpointEventStore, stream: InMemoryEventStream | None = None) -> None:
        """构造运行期（由 assemble_runtime 统一创建）。"""
        self.runner = runner
        self.bus = bus
        self.tools = tools
        self.approval = approval
        self.audit = audit
        self.checkpoint = checkpoint
        self.stream = stream

    async def run(self, session: Session) -> object:
        """真实执行（委托 Runner.run）。"""
        return await self.runner.run(session)

    async def resume(self, session: Session, answer: DynamicValue) -> object:
        """挂起后精确续跑（委托 Runner.resume）。"""
        return await self.runner.resume(session, answer)

    def model_gateway(self, llm: object) -> ModelGateway:
        """用同一 bus 构造模型网关（after_model 相位真实接入）。"""
        return ModelGateway(llm=llm, bus=self.bus)  # type: ignore[arg-type]


def assemble_runtime(
    call: CallFn,
    *,
    approval: ApprovingHumanPort | None = None,
    audit: VerifyingAuditPort | None = None,
    stream: InMemoryEventStream | None = None,
    manager: CheckpointManager | None = None,
    tools: ToolRegistry | None = None,
    security: SecurityCapability | None = None,
    bus: LifecycleBus | None = None,
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
    mgr = manager or CheckpointManager(store=InMemoryEventStore())
    shared_store = mgr.store
    target_stream = stream or InMemoryEventStream()
    active_bus = bus or bind_stream_projection(target_stream)

    approving = approval or ApprovingHumanPort(sec.human, sec.approval)
    audit_sink = R1AuditEventSink(shared_store)
    auditing = audit or VerifyingAuditPort(AuditLogger(sink=audit_sink))
    store = CheckpointEventStore(mgr, inner=shared_store)

    # ①② B2 修复：真实注入 SecurityCapability 的 sandbox/human/audit（消除 sandbox=None 断链）。
    wired_tools = tools or ToolRegistry(
        sandbox=sec.sandbox, human=approving, audit=auditing,
    )

    runner = Runner(call, store=store, notify=runner_notifier(active_bus),
                    checkpoint_every=checkpoint_every)
    return AgentRuntime(runner, active_bus, wired_tools, approving, auditing, store, target_stream)


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
