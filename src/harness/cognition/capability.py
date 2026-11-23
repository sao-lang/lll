"""R11 认知能力包：意图识别策略的可插拔装配（use() / 架构 / 组合 API 三路）。

本包不做识别逻辑（在 CascadeIntentStrategy 等实现），只做：
- 履约 Capability 协议（name/version/requires/install）；
- 把注入策略注册进通用 Registry（意图策略为一等 provider）；
- 提供 recognize/build 转调，供门面与 Router 架构消费。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from runtime import AgentState
from compose import Registry

from .contracts import IntentResult, IntentStrategy
from .cascade import CascadeIntentStrategy, intent_as_program

__all__ = ["CognitionCapability"]


class CognitionCapability:
    """认知能力包：持有 IntentStrategy（缺省四级级联），三路可组装。"""

    name = "cognition"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, *, strategy: IntentStrategy | CascadeIntentStrategy | None = None) -> None:
        """构造：缺省 CascadeIntentStrategy（本地确定性，可离线跑）。"""
        self.strategy = strategy if strategy is not None else CascadeIntentStrategy()

    async def recognize(self, text: str, ctx: AgentState | None = None) -> IntentResult:
        """异步识别：优先策略 recognize()，其余经 intent_as_program 统一桥。"""
        recognize = getattr(self.strategy, "recognize", None)
        if callable(recognize):
            result = await recognize(text, ctx)
            if isinstance(result, IntentResult):
                return result
            return IntentResult(label=str(getattr(result, "label", "unknown")),
                                confidence=float(getattr(result, "confidence", 0.0)))
        # 纯 Callable 策略：intent_as_program 桥（同步适配）
        program = intent_as_program(self.strategy, text)
        gen = program()
        try:
            effect = next(gen)
            # 仅接受 Terminate 输出（无 IO 的同步策略）；有 IO 请用 recognize 协议
            from runtime import Terminate
            if isinstance(effect, Terminate):
                out = dict(effect.output or {})
                return IntentResult(label=str(out.get("label", "unknown")),
                                    confidence=float(out.get("confidence", 0.0)))
        except StopIteration:
            pass
        return IntentResult(label="unknown", confidence=0.0)

    def build(self, text: str = ""):  # type: ignore[no-untyped-def]
        """Program 路径：委托策略 build() 或统一桥（Router 经 intent_as_program 消费）。"""
        build = getattr(self.strategy, "build", None)
        if callable(build):
            return build(text)
        return intent_as_program(self.strategy, text)()

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:
        """Capability 协议：注册策略为一等 provider（名 'intent'）。"""
        if reg is not None:
            reg.register("intent", self)
