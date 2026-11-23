"""R8 独立闭环 demo：意图四级级联 + Router 单入口 + 规划/反思确定性服务。

运行：.venv/Scripts/python tests/demos/demo_r8.py
不联网、不调真实 LLM；L3/L4 经本地 call 回注模拟。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from archs import Router  # noqa: E402
from harness.cognition import (
    CascadeIntentStrategy,
    RegexKeywordMatcher,
    RuleExecutor,
    RulePlanner,
    ThresholdReflector,
)
from runtime import Result, Runner, Session, Terminate


class _Arm:
    """Router 命中臂：产出 Terminate(output=标签)。"""

    def __init__(self, label: str) -> None:
        """保存臂标签。"""
        self.label = label

    def compile(self):
        """产出 Program。"""
        def program():
            yield Terminate(output=f"handled:{self.label}")
            return None
        return program()


def main() -> None:
    """演示级联短路命中 → Router 走对应臂；规划/反思确定性产出。"""
    # L1 命中：零 LLM 调用
    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({"weather": "weather"}))
    arch = Router(strategy, {"weather": _Arm("weather")}, default="weather")
    def call(effect, ctx):
        return Result.success({"label": "weather", "confidence": 0.9})
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="demo-r8", program_factory=lambda: arch.compile())))
    print(f"[R8] Router output={res.output!r} status={res.status}")

    steps = RulePlanner().plan("collect; analyze; report")
    print(f"[R8] plan={[s.action for s in steps]}")
    print(f"[R8] exec={RuleExecutor().execute(steps[0]).output!r}")
    print(f"[R8] reflect={ThresholdReflector().critique('ok PASS', 'goal')}")
    print("[R8] PASS: 四级级联短路 / Router 单入口 / 规划反思确定性闭环")


if __name__ == "__main__":
    main()
