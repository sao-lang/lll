"""R6 测试夹具：五段 fake 实现，全模块内闭环，不联网、不依赖真实模型。"""
from __future__ import annotations

from compose import PortCtx

from harness.rag.retriever import Retriever
from harness.rag.types import Chunk, Document, Hit, Query, Vector

def pctx(budget: object = None) -> PortCtx:
    """构造 R6 用的 PortCtx，budget_left 可注入 TokenBudget 快照。"""
    return PortCtx(session_id="s6", run_id="run6", budget_left=budget)


def doc(doc_id: str, text: str, *, title: str = "") -> Document:
    """构造测试文档。"""
    return Document(doc_id=doc_id, text=text, meta={"title": title})


def query(text: str, *, top_k: int = 3, budget: int | None = None) -> Query:
    """构造查询。"""
    return Query(text=text, top_k=top_k, token_budget=budget)


class FakeSplitter:
    """按固定字数切分的切分器替身；name 供留名断言。"""

    name = "fake_splitter"

    def __init__(self, size: int = 8) -> None:
        """设定切片大小。"""
        self.size = size

    def split(self, doc: Document) -> list[Chunk]:
        """把正文按 size 切片，chunk_id 带序号。"""
        out: list[Chunk] = []
        text = doc.text
        for i, start in enumerate(range(0, len(text), self.size)):
            piece = text[start : start + self.size]
            out.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}:{i}",
                    doc_id=doc.doc_id,
                    text=piece,
                    tokens=len(piece),
                    meta=dict(doc.meta),
                )
            )
        return out


class FakeEmbedder:
    """确定性向量化替身：按词哈希生成固定维度单位向量。"""

    name = "fake_embedder"

    def __init__(self, dim: int = 8) -> None:
        """设定向量维度。"""
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[Vector]:
        """生成确定性向量（同一文本恒定、可被测试预测）。"""
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> Vector:
        """把文本散列成 dim 维单位化向量。"""
        buckets = [0.0] * self.dim
        for word in text.lower().split():
            buckets[(hash(word) % self.dim)] += 1.0
        norm = sum(v * v for v in buckets) ** 0.5 or 1.0
        return tuple(v / norm for v in buckets)


class CountingEmbedder(FakeEmbedder):
    """带 embed 计数的向量化替身，供缓存测试断言是否重算。"""

    def __init__(self, dim: int = 8) -> None:
        """初始化计数器。"""
        super().__init__(dim)
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[Vector]:
        """记录调用次数后委托父类。"""
        self.calls += 1
        return await super().embed(texts)


class CountingRetriever:
    """带 retrieve 计数的召回器替身，供缓存测试断言是否跳过召回。"""

    name = "counting_retriever"

    def __init__(self, inner: Retriever) -> None:
        """包装真实召回器。"""
        self._inner = inner
        self.calls = 0

    async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:
        """记录调用次数后委托内部召回器。"""
        self.calls += 1
        return await self._inner.retrieve(query, vec)


class InMemoryVectorStore:
    """内存向量库：存带向量的 Chunk，search 用点积相似度排序。"""

    name = "fake_store"

    def __init__(self) -> None:
        """初始化空库。"""
        self._chunks: list[Chunk] = []

    async def upsert(self, chunks: list[Chunk]) -> None:
        """写入块（向量在 chunk.vector 上）。"""
        self._chunks.extend(chunks)

    async def delete(self, chunk_ids: list[str]) -> None:
        """按 chunk_id 删除块（幂等）。"""
        doomed = set(chunk_ids)
        self._chunks = [c for c in self._chunks if c.chunk_id not in doomed]

    def all_chunks(self) -> list[Chunk]:
        """返回全部块（供混合召回关键词通道）。"""
        return list(self._chunks)

    async def search(
        self, vec: Vector, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Hit]:
        """按点积相似度返回 top_k 命中，并施加 filters。"""
        from harness.rag.store import matches_filters

        scored = [
            Hit(chunk=chunk, score=self._dot(vec, chunk.vector), source=chunk.doc_id)
            for chunk in self._chunks
            if chunk.vector is not None and matches_filters(chunk, filters)
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    @staticmethod
    def _dot(a: Vector, b: Vector) -> float:
        """向量点积。"""
        return sum(x * y for x, y in zip(a, b))
