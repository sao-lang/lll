"""M1 核心运行时：State / Effect / Program / 解释循环 / 事件溯源。

本包不依赖 pydantic、不依赖任何具体 IO，可独立安装与测试（CL-1）。
"""
from .errors import (
    AgentKitError,
    BudgetExceeded,
    ChainAborted,
    PortMissing,
    ProgramCrashed,
    ProgramInvalid,
)
from .fold import fold, fold_tool_calls, fold_tool_results, replay_events, state_set
from .interpreter import EffectBatch, Interpreter, InterpreterImpl
from .runner import InMemoryEventStore, Runner
from .types import (
    DONE,
    AgentState,
    Budgets,
    Effect,
    EffectKind,
    Event,
    Message,
    PortError,
    Program,
    Replayed,
    Result,
    restore_state,
    revive,
    RunResult,
    RuntimeCtx,
    Session,
    StepReport,
    Usage,
    canonical,
    checkpoint,
    fingerprint,
    restore_checkpoint,
)

__all__ = [
    "AgentState", "Budgets", "DONE", "Effect", "EffectKind", "Event", "Message",
    "PortError", "Program", "Result", "RunResult", "RuntimeCtx", "Session",
    "StepReport", "Usage", "Runner", "InMemoryEventStore", "Interpreter",
    "InterpreterImpl", "EffectBatch", "fold", "fold_tool_calls", "fold_tool_results",
    "replay_events", "state_set", "canonical", "checkpoint", "restore_checkpoint",
    "restore_state", "revive", "Replayed",
    "fingerprint", "AgentKitError", "BudgetExceeded", "ProgramInvalid", "PortMissing",
    "ProgramCrashed", "ChainAborted",
]
