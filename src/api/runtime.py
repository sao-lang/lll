# purity-exempt-file: 运行期装配：协议形状/动态内容边界用 object/DynamicValue，理由见逐行豁免
"""R11 运行期装配：把六项接线真正注入 Runner / ToolRegistry / ModelGateway。

不是"持有对象 + getter"——`assemble()` 构造真实 Runner（store/notify 注入）、
真实 ToolRegistry（human/audit 注入包装器）、真实 LifecycleBus（供 ModelGateway）。
全部落在 api(L3)，不改 R1/R5/R9 源码。
"""
from __future__ import annotations

from collections.abc import Callable

from compose import CallFn, LifecycleBus, PortCtx, Registry
from compose.notify import runner_notifier
from harness.durability import CheckpointManager
from harness.model import ModelGateway
from harness.observability import InMemoryEventStream
from compose import SandboxPort
from harness.security import ApprovalFlow, AuditLogger
from harness.tools import ToolsCapability, ToolRegistry
from runtime import DynamicValue, Runner, Session

from .wiring import (
    ApprovingHumanPort,
    CheckpointEventStore,
    VerifyingAuditPort,
    bind_stream_projection,
)

__all__ = ["AgentRuntime", "assemble_runtime"]


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
    checkpoint_every: int = 1,
) -> AgentRuntime:
    """真实装配：构造 Runner/Registry/三 Port 接线的运行期。

    - ①② 未注入 approval/audit 时用默认包装器（CRITICAL 触发 / 审计验链）；
    - ③ secret 由 builder.redact 在输出边界消费（不在此重复）；
    - ④ bus 订阅三相位并作为 Runner(notify=runner_notifier(bus)) 注入；
    - ⑤ CheckpointEventStore 作为 Runner(store=...) 注入；
    - ⑥ CI gate 由 CLI.eval 消费（不在此重复）。
    """
    target_stream = stream or InMemoryEventStream()
    bus = bind_stream_projection(target_stream)
    approving = approval or ApprovingHumanPort(_NoHuman(), ApprovalFlow())
    auditing = audit or VerifyingAuditPort(AuditLogger())
    mgr = manager or CheckpointManager()
    store = CheckpointEventStore(mgr)

    # ①② 真实注入 ToolRegistry 的构造参数（R5 源码不改）。
    wired_tools = tools or ToolRegistry(sandbox=None, human=approving, audit=auditing)

    runner = Runner(call, store=store, notify=runner_notifier(bus),
                    checkpoint_every=checkpoint_every)
    return AgentRuntime(runner, bus, wired_tools, approving, auditing, store, target_stream)


class _NoHuman:
    """缺 HumanPort 时的 fail-closed 占位：只返回 deny，不冒充人类。"""
    name = "no_human"

    async def ask(self, question: str, options: list[str] | None,
                  ctx: PortCtx) -> str:
        """一律 deny（fail-closed，不静默放行）。"""
        return "deny"


def capability_tools(approval: ApprovingHumanPort, audit: VerifyingAuditPort) -> ToolsCapability:
    """用已接线的三 Port 构造 ToolsCapability（生产装配入口）。"""
    reg = ToolRegistry(sandbox=None, human=approval, audit=audit)
    return ToolsCapability(registry=reg)
