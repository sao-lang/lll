"""R7 测试夹具：context + memory 的 fake 与 helper，全模块内闭环、不联网。"""
from __future__ import annotations

from hooks import PortCtx

from harness.rag.types import Chunk, Hit, Vector


def pctx(session_id: str = "s7") -> PortCtx:
    """构造 R7 用的 PortCtx。"""
    return PortCtx(session_id=session_id, run_id="run7")


def msg(role: str, content: str) -> object:
    """构造消息替身（避免测试耦合 Message 构造细节）。"""
    from runtime import Message

    return Message(role=role, content=content)


class FakeEmbedder:
    """确定性向量化替身；签名对齐 R6 Embedder 协议。"""

    name = "fake_embedder"

    async def embed(self, texts: list[str]) -> list[Vector]:
        """按词哈希生成 dim=8 的单位向量。"""
        out: list[Vector] = []
        for text in texts:
            buckets = [0.0] * 8
            for word in text.lower().split():
                buckets[hash(word) % 8] += 1.0
            norm = sum(v * v for v in buckets) ** 0.5 or 1.0
            out.append(tuple(v / norm for v in buckets))
        return out


class FakeVectorStore:
    """内存向量库替身，供 recall 断言；签名对齐 R6 VectorStore 主方法。"""

    name = "fake_vstore"

    def __init__(self) -> None:
        """初始化空库。"""
        self._chunks: list[Chunk] = []

    async def upsert(self, chunks: list[Chunk]) -> None:
        """写入块。"""
        self._chunks.extend(chunks)

    async def search(
        self, vec: Vector, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Hit]:
        """按点积返回 top_k。"""
        scored = [
            Hit(chunk=c, score=sum(x * y for x, y in zip(vec, c.vector or ())), source=c.chunk_id)
            for c in self._chunks
            if c.vector is not None
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]
