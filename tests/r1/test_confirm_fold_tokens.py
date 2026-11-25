"""确认 bug #1：fold 跨 Effect 重复累计 token 预算（独立审计证据 audit_fold_tokens.py）。"""
from __future__ import annotations

from runtime import AgentState, Budgets, Invoke, Result, Usage, fold


def test_fold_deducts_each_effect_usage_token_exactly_once() -> None:
    """一批 3 个各 10 token 的 Effect，折叠后 used_tokens 必须恰好是 30。"""
    state = AgentState(budgets=Budgets(max_tokens=1000))
    effects = tuple(Invoke(port="llm", payload={}) for _ in range(3))
    results = tuple(
        Result.success(usage=Usage(prompt_tokens=6, completion_tokens=4))
        for _ in range(3)
    )

    out = fold(state, effects, results)

    assert out.budgets.used_tokens == 30, \
        f"3 个各 10 token 的 Effect 应扣 30，实际 used_tokens={out.budgets.used_tokens}"
    assert out.last_tokens == 30, \
        f"last_tokens 应记录本批 token 总和 30，实际 {out.last_tokens}"
