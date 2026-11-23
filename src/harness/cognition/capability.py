"""R11 认知能力包：意图策略持有者（不是架构桥梁）。

接线边界（契约 6a12744 §1 C4）：
- 能力包 = 策略持有 + install 注册 + recognize 转调；
- **不持有 intent_as_program、不暴露 build()**——Program 桥梁留给 archs.Router
  直接用 cap.strategy（架构构造器收策略对象，不收能力包）。
- 整包替换：传任意 IntentStrategy（recognize 协议），框架不包装级联。
- 部件替换：默认 CascadeIntentStrategy 内部换 keyword/samples/cheap/primary。
"""
from __future__ import annotations

from runtime import AgentState
from compose import Registry

from .contracts import IntentResult, IntentStrategy
from .cascade import CascadeIntentStrategy

__all__ = ["CognitionCapability"]


class CognitionCapability:
    """认知能力包：持有 IntentStrategy，门面语义入口直接消费策略。"""

    name = "cognition"
    version = "2.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, *, strategy: IntentStrategy | CascadeIntentStrategy | None = None) -> None:
        """构造：缺省四级级联策略（本地确定性，可离线跑）。"""
        self.strategy = strategy if strategy is not None else CascadeIntentStrategy()

    async def recognize(self, text: str, ctx: AgentState | None = None) -> IntentResult:
        """转调策略 recognize()——整包替换语义，框架不包装级联。

        fail-closed：策略不履约 recognize 协议时明确报错，不伪造 unknown 结果，
        也不偷偷套 Program 桥梁（那是架构层职责）。
        """
        recognize = getattr(self.strategy, "recognize", None)
        if not callable(recognize):
            raise TypeError(
                f"意图策略 {type(self.strategy).__name__} 未履约 IntentStrategy.recognize")
        result = await recognize(text, ctx)
        if isinstance(result, IntentResult):
            return result
        # 鸭子形态归一（允许返回等价 DTO，不伪造缺失字段）
        if hasattr(result, "label"):
            return IntentResult(
                label=str(result.label),
                confidence=float(getattr(result, "confidence", 0.0)),
                gaps=tuple(getattr(result, "gaps", ())),
                trace=tuple(getattr(result, "trace", ())),
            )
        raise TypeError(f"意图策略返回非法结果：{type(result).__name__}")

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:
        """Capability 协议：注册策略为一等 provider（名 'intent'）。"""
        _ = container  # 协议入参：策略经 recognize 消费，不持有容器
        if reg is not None:
            reg.register("intent", self.strategy)
