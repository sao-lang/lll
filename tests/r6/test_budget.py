"""R6-T04：预算拼接不超 TokenBudget 且每片段带引用。"""
from __future__ import annotations

import asyncio

from harness.rag.post import DefaultPostProcessor
from harness.rag.types import Chunk, Hit, TokenBudget


def hit(doc_id: str, i: int, text: str, score: float) -> Hit:
    """构造命中。"""
    chunk = Chunk(
        chunk_id=f"{doc_id}:{i}", doc_id=doc_id, text=text,
        tokens=len(text.split()), meta={},
    )
    return Hit(chunk=chunk, score=score, source=doc_id)


def test_assemble_respects_budget_and_cites() -> None:
    """拼接结果 tokens_used 不超预算，且每个引用在 hits 中存在。"""
    post = DefaultPostProcessor()
    hits = [hit("d", i, "word " * 3, 1.0 - i * 0.1) for i in range(5)]

    out = asyncio.run(post.assemble(hits, TokenBudget(limit=8)))
    assert out.tokens_used <= 8
    ids = {h.chunk.chunk_id for h in hits}
    for cite in out.citations:
        assert cite in ids


def test_tiny_budget_yields_empty() -> None:
    """预算小于最小块时不装入任何片段，返回确定性空结果。"""
    post = DefaultPostProcessor()
    hits = [hit("d", 0, "a b c d e", 1.0)]
    out = asyncio.run(post.assemble(hits, TokenBudget(limit=1)))
    assert out.empty is True
