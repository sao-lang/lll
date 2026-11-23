"""R7 RAG 能力包：持有可注入的 RAGPipeline，作为 RAGPort 一等 provider。

接线边界（契约 6a12744）：
- 能力包 = 组合根：用户注入 pipeline（其内五段+增强都可替换）；
- 不重写 RAGPipeline 内部算法；门面 use() 后注册 RAGPort provider 真进执行链。
"""
from __future__ import annotations

from runtime import DynamicValue
from compose import PortCtx, Registry
from harness.rag import RAGPipeline

__all__ = ["RAGCapability"]


class RAGCapability:
    """RAG 能力包：持 pipeline + 转调 ingest/retrieve（RAGPort 语义）。"""

    name = "rag"
    version = "1.0.0"
    requires: tuple[str, ...] = ()
    op = "retrieve"  # port handler 入口（G-W7：provider 自声明 op）

    def __init__(self, *, pipeline: RAGPipeline) -> None:
        """构造：用户注入 RAGPipeline（内含检索工具等可插拔部件）。"""
        self.pipeline = pipeline

    async def ingest(self, documents: list[DynamicValue], ctx: PortCtx) -> int:
        """入库：委托 pipeline（真实实现，不造假）。"""
        return await self.pipeline.ingest(documents, ctx)

    async def retrieve(self, query: str, ctx: PortCtx) -> list[DynamicValue]:
        """检索：委托 pipeline 的 retrieve（真实实现）。"""
        return await self.pipeline.retrieve(query, ctx)

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:
        """Capability 协议：注册自己为 rag port provider（履约 RAGPort）。"""
        _ = container
        if reg is not None:
            reg.register("rag", self)
