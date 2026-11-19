"""R6 重排段：Reranker 协议 + cross-encoder 风格默认实现。

真实 cross-encoder 属外部模型，抽象为可注入 client；默认用 query 词项
覆盖度做近似精排（离线可测），生产注入真模型零改上层。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.types import Hit, Query

__all__ = ["Reranker", "LexicalReranker"]


@runtime_checkable
class Reranker(Protocol):
    """重排协议。"""

    name: str

    def rerank(self, hits: list[Hit], query: Query) -> list[Hit]:
        """对候选命中重排。"""
        ...


class LexicalReranker:
    """默认重排：按 query 词项在块正文的覆盖度精排，取 top_n。"""

    name: str = "lexical_reranker"

    def __init__(self, top_n: int = 5) -> None:
        """构造重排器，top_n 为保留候选数。"""
        self.top_n = top_n

    def rerank(self, hits: list[Hit], query: Query) -> list[Hit]:
        """按词项覆盖度重排并返回前 top_n。"""
        terms = {w for w in query.text.lower().split() if w}
        if not terms:
            return list(hits[: self.top_n])

        def coverage(hit: Hit) -> float:
            """统计 query 词项在块正文的命中比例。"""
            body = hit.chunk.text.lower()
            return sum(1 for t in terms if t in body) / len(terms)

        ranked = sorted(hits, key=coverage, reverse=True)
        return ranked[: self.top_n]
