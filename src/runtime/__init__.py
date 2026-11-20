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
    DONE, AgentState, BudgetDelta, Budgets, CallFn, CancellationToken, DispatchFn,
    Disposition, DoneSentinel, DynamicValue, Effect, EffectPayload, Event, EventStore,
    Invoke, JsonSnapshot, Message, Payload, PortError, PortName, Program, Result, RunResult,
    RuntimeCtx, Scratchpad, Session, Spawn, StateSnapshot, StepReport, Suspend, Terminate, Usage,
    canonical, checkpoint, disposition_of, fingerprint, restore_checkpoint,
)
from .runner import InMemoryEventStore, Runner

__all__ = [
    "DONE", "AgentState", "Budgets", "Effect", "EffectPayload",
    "Event", "EventStore", "Message", "PortError", "Program", "Result", "Usage",
    "RunResult", "RuntimeCtx", "Session", "StepReport", "JsonSnapshot", "StateSnapshot",
    "BudgetDelta", "DynamicValue", "Payload", "Scratchpad", "DoneSentinel",
    "Disposition", "PortName", "Invoke", "Spawn", "Suspend", "Terminate", "disposition_of",
    "CallFn", "CancellationToken", "DispatchFn", "canonical", "checkpoint", "fingerprint", "restore_checkpoint",
    "fold", "replay_events", "state_append_message", "state_set",
    "Interpreter", "InterpreterImpl", "EffectBatch", "Runner", "InMemoryEventStore",
    "AgentKitError", "BudgetExceeded", "ChainAborted", "PortMissing",
    "ProgramCrashed", "ProgramInvalid",
]
