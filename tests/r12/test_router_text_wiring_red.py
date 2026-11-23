"""RED：Router 分类器必须收到真实输入文本（全模块体检 B4）。

现状 recipes.py:471/506 硬编码 text=""，分类器永远收到空串；现有测试用
忽略 text 的分类器掩盖了 bug。本测试用依赖 text 内容的分类器，当前应 FAIL。
"""
from __future__ import annotations

from api import Agent
from archs import Router
from compose.input import bind_input
from runtime import Terminate


class _TextSpyClassifier:
    """分类器：收到含'天气'的文本返回 weather 分支，否则 other（fake 仅在 tests/）。"""
    name = "text_spy"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, text: str):
        self.seen.append(text)
        from archs import IntentResult
        label = "weather" if "天气" in text else "other"
        return IntentResult(label=label, confidence=0.9)


class _Arm:
    def __init__(self, name: str) -> None:
        self.name = name

    def compile(self):
        def p():
            yield Terminate(output=self.name)
            return None
        return p()

    def static_check(self):
        class R:
            ok = True
            errors = ()
        return R()


def test_red_router_classifier_receives_real_text() -> None:
    """Router 分类器应收到 say() 的真实输入（当前 FAIL：收到空串）。"""
    spy = _TextSpyClassifier()
    agent = Agent(Router(spy, {"weather": _Arm("w"), "other": _Arm("o")}, default="other"))

    answer = agent.say("查一下深圳天气")

    assert spy.seen, "分类器应被调用"
    assert any("天气" in t for t in spy.seen), (
        f"分类器应收到含'天气'的真实输入，实得 {spy.seen!r}"
    )
    assert answer == "w", f"真实文本应路由到 weather 分支，实得 {answer!r}"
