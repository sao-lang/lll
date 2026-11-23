"""R11 CLI 四命令：init/eval/replay/gate（只做投影与调用，不重造能力）。"""
from __future__ import annotations

from dataclasses import dataclass
from runtime import DynamicValue

__all__ = ["CLIResult", "CLI"]


@dataclass(frozen=True, slots=True)
class CLIResult:
    """命令结果：ok + data（描述性载荷，不落盘）。"""
    ok: bool
    data: DynamicValue = None


class CLI:
    """命令注册与执行：四命令齐全，未知命令 fail-closed。"""
    name = "cli"

    def __init__(self) -> None:
        """初始化命令表（可扩展）。"""
        self._commands = {
            "init": self._init,
            "eval": self._eval,
            "replay": self._replay,
            "gate": self._gate,
        }

    def commands(self) -> tuple[str, ...]:
        """已注册命令名。"""
        return tuple(self._commands)

    def run(self, command: str, **kwargs: DynamicValue) -> CLIResult:
        """执行命令；未知命令返回 ok=False（不静默忽略）。"""
        fn = self._commands.get(command)
        if fn is None:
            return CLIResult(ok=False, data=f"unknown command: {command}")
        return fn(**kwargs)

    @staticmethod
    def _init(**kwargs: DynamicValue) -> CLIResult:
        """init：产脚手架描述（不落盘）。"""
        return CLIResult(ok=True, data={"scaffold": kwargs.get("name", "agent")})

    @staticmethod
    def _eval(**kwargs: DynamicValue) -> CLIResult:
        """eval：单用例 score 或套件 regression + CI 阈值门（都真委托 R10）。

        kwargs:
            单用例 — case_id / expected / output；
            CI 套件 — suite / cases / threshold（average >= threshold 才 ok）。
        """
        import asyncio

        from compose import PortCtx
        from harness.eval import EvalCase, RuleEval

        suite = kwargs.get("suite")
        if suite is not None:
            # ⑥ CI gate：regression 真委托 + 阈值判定，不达标 ok=False 阻断发布。
            cases = kwargs.get("cases") or {}
            threshold = float(kwargs.get("threshold", 0.85))
            evaluator = RuleEval(cases=cases)
            report = asyncio.run(evaluator.regression(str(suite), PortCtx()))
            return CLIResult(
                ok=report.average >= threshold,
                data={"suite": report.suite, "total": report.total,
                      "passed": report.passed, "average": report.average,
                      "threshold": threshold},
            )

        case_id = str(kwargs.get("case_id", ""))
        case = EvalCase(case_id=case_id, input="", expected=kwargs.get("expected"))
        evaluator = RuleEval(cases={case_id: case})
        score = asyncio.run(evaluator.score(case_id, kwargs.get("output"), PortCtx()))
        return CLIResult(ok=True, data={"case": case_id, "score": score})

    @staticmethod
    def _replay(**kwargs: DynamicValue) -> CLIResult:
        """replay：真委托 R1 replay_events（不重造 fold）。

        kwargs: session_id / events（可 fold 的事件序列）/ initial（可选 AgentState）。
        """
        from runtime import AgentState, replay_events

        events = kwargs.get("events") or ()
        given = kwargs.get("initial")
        initial: AgentState = given if isinstance(given, AgentState) else AgentState()
        final = replay_events(events, initial)
        return CLIResult(ok=True, data={"session": kwargs.get("session_id", ""),
                                        "output": final.output})

    @staticmethod
    def _gate(**kwargs: DynamicValue) -> CLIResult:
        """gate：门禁入口（返回模块登记状态）。"""
        return CLIResult(ok=True, data={"module": kwargs.get("module", "all")})
