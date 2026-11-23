"""R8 测试夹具：确定性意图部件与 fake LLM 回注，全部本地无联网。"""
from __future__ import annotations

from harness.cognition.contracts import IntentResult


def keyword_hit(*labels: str, confidence: float = 0.8):
    """构造关键词部件：文本含任一 label 即命中。

    Args:
        *labels: 触发关键词。
        confidence: 命中置信度。

    Returns:
        部件对象（match 签名）。
    """

    class Matcher:
        name = "kw"

        def match(self, text: str, ctx: object) -> IntentResult | None:
            for label in labels:
                if label in text:
                    return IntentResult(label=label, confidence=confidence)
            return None

    return Matcher()


def stage_model(label: str, confidence: float):
    """构造 recognize-only 级联部件：固定返回 label/confidence。

    Args:
        label: 命中标签。
        confidence: 置信度。

    Returns:
        recognize 部件对象。
    """

    class Model:
        name = "model"

        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label=label, confidence=confidence)

    return Model()
