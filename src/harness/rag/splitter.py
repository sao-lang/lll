"""R6 切分段：Splitter 协议 + 递归切分默认实现。

递归切分按段落/句子边界优先，超长再按字符切；overlap 保证跨块上下文连续。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.types import Chunk, Document

__all__ = ["Splitter", "RecursiveSplitter", "DEFAULT_CHUNK_SIZE", "DEFAULT_OVERLAP"]

DEFAULT_CHUNK_SIZE: int = 512
DEFAULT_OVERLAP: int = 64

# 切分边界优先级：段落 > 句子 > 无边界（纯长度切）。
_BOUNDARIES: tuple[str, ...] = ("\n\n", "。", ". ")


@runtime_checkable
class Splitter(Protocol):
    """切分协议：把文档切成块，name 供装配期留名校验。"""

    name: str

    def split(self, doc: Document) -> list[Chunk]:
        """把文档切成块列表。"""
        ...


class RecursiveSplitter:
    """递归切分默认实现：优先按自然边界，超长按固定长度 + overlap。"""

    name: str = "recursive_splitter"

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_OVERLAP,
    ) -> None:
        """构造切分器。

        Args:
            chunk_size: 单块最大字数。
            overlap: 相邻块重叠字数（必须 < chunk_size，否则退化为 0）。

        Raises:
            ValueError: chunk_size <= 0。
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正整数")
        self.chunk_size = chunk_size
        self.overlap = overlap if 0 <= overlap < chunk_size else 0

    def split(self, doc: Document) -> list[Chunk]:
        """把文档正文切成带 id/token 估算的块。"""
        pieces = self._split_text(doc.text)
        out: list[Chunk] = []
        for i, piece in enumerate(pieces):
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

    def _split_text(self, text: str) -> list[str]:
        """递归按边界切分，返回不超过 chunk_size 的片段列表。"""
        if len(text) <= self.chunk_size:
            return [text] if text else []
        for boundary in _BOUNDARIES:
            if boundary in text:
                parts = text.split(boundary)
                out: list[str] = []
                for part in parts:
                    out.extend(self._split_text(part) if len(part) > self.chunk_size else ([part] if part else []))
                return out
        step = self.chunk_size - self.overlap
        return [text[i : i + self.chunk_size] for i in range(0, len(text), step)]
