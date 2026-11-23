"""R7 记忆能力包：MemoryCapability（用户可插拔注入 + say 钩子自动召回/写回）。

设计：
- 履约 compose.Capability 协议（name/version/requires/install），install 真注册
  memory provider 到 Registry（非空实现）。
- 门面 use(MemoryCapability()) 后，say() 链路自动 before recall + after 写回，
  用户无需手写 memory.write/recall/ingest（体检 B1 记忆主线）。
"""
from __future__ import annotations

from compose import PortCtx, Registry
from compose.ports import MemoryPort
from harness.memory import MemoryQuery

from .manager import ContextMemoryManager

__all__ = ["MemoryCapability"]


class MemoryCapability:  # purity-exempt: 生产记忆能力类（非测试替身），持有真实 ContextMemoryManager 并注册 provider
    """记忆能力：持有 ContextMemoryManager，install 注册 memory provider，钩子自动召回/写回。"""
    name = "memory"
    version = "7.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, manager: ContextMemoryManager | None = None) -> None:
        """构造：缺省自建 ContextMemoryManager（开箱可用）。"""
        self.manager = manager if manager is not None else ContextMemoryManager()

    def install(self, container: object, reg: Registry) -> None:  # purity-exempt: 记忆经 say 钩子消费，install 注册 provider 供 port 路径可查
        """把 manager.memory 注册为 memory provider（履约 Capability 协议）。"""
        store = self.manager.memory
        if isinstance(store, MemoryPort):
            reg.register("memory", store)

    async def recall_before(self, text: str, ctx: PortCtx) -> tuple[str, ...]:
        """say 前召回：返回命中记忆的 content 元组（供门面注入上下文）。"""
        items = await self.manager.recall(MemoryQuery(text=text, top_k=3), ctx)
        return tuple(str(getattr(item, "content", "")) for item in items
                     if getattr(item, "content", ""))

    def write_after(self, user_input: str, answer: str, ctx: PortCtx) -> None:
        """say 后写回：把问答提炼进长期记忆（真实 ingest，非空桩）。"""
        if user_input or answer:
            self.manager.memory.ingest(f"用户问：{user_input}；答：{answer}", ctx, confidence=0.8)
