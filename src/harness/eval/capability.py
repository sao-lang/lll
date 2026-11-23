"""R10 评测能力包：持 EvalPort，供门面 score_case() 真调。

接线边界：EvalPort 协议现成（compose/ports.py:237），能力包是组合根持有
可替换评测器；门面 evaluate() 保留反思语义不动，另开 score_case()。
"""
from __future__ import annotations

from runtime import DynamicValue
from compose import PortCtx, Registry
from harness.eval.rules import RuleEval

__all__ = ["EvalCapability"]


class EvalCapability:
    """评测能力包：持有 EvalPort（缺省 RuleEval）。"""

    name = "eval"
    version = "1.0.0"
    requires: tuple[str, ...] = ()
    op = "score"

    def __init__(self, *, evaluator: object | None = None) -> None:  # purity-exempt: 鸭子协议评测器注入边界
        self.evaluator = evaluator if evaluator is not None else RuleEval()

    async def score(self, case_id: str, output: DynamicValue,
                    ctx: PortCtx) -> float:
        """转调注入评测器（三参真传）。"""
        result = await self.evaluator.score(case_id, output, ctx)  # type: ignore[attr-defined]
        return float(result)

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:
        _ = container
        if reg is not None:
            reg.register("eval", self)
