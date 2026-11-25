"""确认 bug #2：负预算增量绕过硬上限（独立审计证据 audit_negative_budget.py）。"""
from __future__ import annotations

import asyncio

import pytest

from runtime import (
    AgentState,
    Budgets,
    Invoke,
    ProgramInvalid,
    Result,
    Runner,
    Session,
    Terminate,
    Usage,
    fold,
)


def _declared_effects() -> list[Invoke]:
    return [
        Invoke(port="llm", payload={"budget": {"tokens": -1000, "turns": -100}})
        for _ in range(3)
    ]


def test_fold_rejects_negative_budget_delta() -> None:
    """Program 声明负增量必须被拒绝，不得产生负的 used_* 预算。"""
    state = AgentState(budgets=Budgets(max_tokens=10, max_turns=2))
    results = tuple(Result.success(usage=Usage(prompt_tokens=5)) for _ in range(3))

    with pytest.raises(ProgramInvalid):
        fold(state, _declared_effects(), results)


def test_run_folds_negative_budget_into_error_not_bypass() -> None:
    """端到端：负增量不得绕过硬上限，run 必须返回 error/PROGRAM_INVALID。"""
    async def run() -> None:
        def program(state):
            for _ in range(3):
                yield Invoke(port="llm", payload={"budget": {"tokens": -1000, "turns": -100}})
            yield Terminate(output="X")

        session = Session(
            session_id="neg",
            program_factory=lambda: program(AgentState(budgets=Budgets(max_tokens=10, max_turns=2))),
            state=AgentState(budgets=Budgets(max_tokens=10, max_turns=2)),
        )
        runner = Runner(call=lambda effect, ctx: _ok())

        result = await runner.run(session)

        assert result.status == "error", f"status={result.status}, 期望 error"
        assert result.error == "PROGRAM_INVALID", f"error={result.error!r}"
        assert result.state.budgets.used_tokens == 0, \
            f"used_tokens={result.state.budgets.used_tokens}, 不得出现负值"
        assert result.state.budgets.used_turns == 0, \
            f"used_turns={result.state.budgets.used_turns}, 不得出现负值"
        assert result.state.budgets.exceeded() is None

    async def _ok():
        return Result.success("ok", usage=Usage(prompt_tokens=5))

    asyncio.run(run())
