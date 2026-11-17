"""R1 核心运行时：Effect 解释器 + 唯一执行循环 + 预算 + 事件溯源。

公共 API 见 docs/02-模块详设/Agent/R1-核心运行时.md §4。
"""
from __future__ import annotations

from .errors import (
    AgentKitError, BudgetExceeded, ChainAborted, PortMissing,
    ProgramCrashed, ProgramInvalid,
)
from .fold import fold, replay_events, state_append_message, state_set
from .interpreter import EffectBatch, Interpreter, InterpreterImpl
from .types import (
    DONE, AgentState, BudgetDelta, Budgets, CallFn, DispatchFn, DoneSentinel,
    DynamicValue, Effect, EffectKind, EffectPayload, Event, EventStore,
    JsonSnapshot, Message, Payload, PortError, Program, Result, RunResult,
    RuntimeCtx, Scratchpad, Session, StateSnapshot, StepReport, Usage,
    canonical, checkpoint, fingerprint, restore_checkpoint,
)
from .runner import InMemoryEventStore, Runner

__all__ = [
    "DONE", "AgentState", "Budgets", "Effect", "EffectKind", "EffectPayload",
    "Event", "EventStore", "Message", "PortError", "Program", "Result", "Usage",
    "RunResult", "RuntimeCtx", "Session", "StepReport", "JsonSnapshot", "StateSnapshot",
    "BudgetDelta", "DynamicValue", "Payload", "Scratchpad", "DoneSentinel",
    "CallFn", "DispatchFn", "canonical", "checkpoint", "fingerprint", "restore_checkpoint",
    "fold", "replay_events", "state_append_message", "state_set",
    "Interpreter", "InterpreterImpl", "EffectBatch", "Runner", "InMemoryEventStore",
    "AgentKitError", "BudgetExceeded", "ChainAborted", "PortMissing",
    "ProgramCrashed", "ProgramInvalid",
]
