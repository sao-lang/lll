"""R11 CLI 四命令：init/eval/replay/gate（TDD RED → GREEN）。"""
from __future__ import annotations

from api import CLI, CLIResult


def test_cli_commands_registered() -> None:
    """四命令齐全。"""
    assert set(CLI().commands()) == {"init", "eval", "replay", "gate"}


def test_cli_init_scaffold() -> None:
    """init：产脚手架描述（不落盘，纯数据）。"""
    res = CLI().run("init", name="myagent")
    assert isinstance(res, CLIResult) and res.ok
    assert "myagent" in str(res.data)


def test_cli_eval_uses_rule_eval() -> None:
    """eval：真调 R10 RuleEval.score（确定性，expected/output 相等得 1）。"""
    res = CLI().run("eval", case_id="c1", expected="a", output="a")
    assert res.ok and res.data["case"] == "c1" and res.data["score"] == 1.0
    miss = CLI().run("eval", case_id="c1", expected="a", output="b")
    assert miss.data["score"] == 0.0


def test_cli_replay_uses_r1() -> None:
    """replay：真调 R1 replay_events，回放后 output 是终态输出（不重造 fold）。"""
    from runtime import Result, Terminate

    class Ev:
        effects = (Terminate(output="replayed"),)
        results = (Result.success(None),)
        reason = ""

    res = CLI().run("replay", session_id="s1", events=(Ev(),))
    assert res.ok and res.data["session"] == "s1" and res.data["output"] == "replayed"


def test_cli_gate_runs_checks() -> None:
    """gate：汇总门禁状态（不实际跑门禁，只返回登记结果）。"""
    res = CLI().run("gate", module="r11")
    assert res.ok and "r11" in str(res.data)


def test_cli_unknown_command() -> None:
    """未知命令失败（不静默忽略）。"""
    assert not CLI().run("bogus").ok
