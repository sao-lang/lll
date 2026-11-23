"""R6 向量化段：Embedder 协议 + 可注入 client 的默认实现。

按总设计纪律，需要外部系统的协议必须抽象可注入 client；本模块把真实
向量化后端抽象为 ``EmbeddingClient`` 协议，默认提供确定性的哈希 client，
生产装配可注入 OpenAI-compat 后端（不联网、可测）。
"""
from __future__ import annotations

from hashlib import blake2b
from typing import Protocol, runtime_checkable

from harness.rag.types import Vector

__all__ = ["DEFAULT_DIM", "Embedder", "EmbeddingClient", "HashEmbedder"]

DEFAULT_DIM: int = 16


@runtime_checkable
class EmbeddingClient(Protocol):
    """向量化后端协议：把文本批转成向量，外部系统在此抽象可注入。"""

    name: str

    async def embed(self, texts: list[str]) -> list[Vector]:  # purity-exempt: HEAD 存量 Protocol 空体（import 行号位移，非新增）
        """把文本批转成向量列表。"""
        ...  # purity-exempt: HEAD 存量 Protocol 空体（import 行号位移，非新增）


@runtime_checkable
class Embedder(Protocol):
    """向量化协议（R6 五段之一）。"""

    name: str

    async def embed(self, texts: list[str]) -> list[Vector]:  # purity-exempt: HEAD 存量 Protocol 空体（import 行号位移，非新增）
        """把文本批转成向量列表。"""
        ...  # purity-exempt: HEAD 存量 Protocol 空体（import 行号位移，非新增）


class HashEmbedder:
    """默认向量化实现：委托可注入 client，缺省用内置哈希 client。

    哈希 client 是确定性的，便于离线评测与测试；生产用 OpenAI-compat client
    替换，上层零改动。
    """

    name: str = "hash_embedder"

    def __init__(
        self, client: EmbeddingClient | None = None, *, dim: int = DEFAULT_DIM
    ) -> None:
        """构造向量化器。

        Args:
            client: 向量化后端；None 时用内置 HashClient。
            dim: 内置 client 的向量维度。
        """
        self._client: EmbeddingClient = client if client is not None else _HashClient(dim)

    async def embed(self, texts: list[str]) -> list[Vector]:
        """委托后端向量化。"""
        return await self._client.embed(texts)


class _HashClient:
    """内置确定性向量化后端：词哈希到 dim 维单位向量。

    确定性是跨进程的：用 blake2b 稳定哈希而非内置 hash()（后者受
    PYTHONHASHSEED 每进程随机化影响，同文本跨进程向量不同，且 dim=16 时
    两个不同句子有 1/16 概率碰撞同桶致近重复误合并——r11 门禁偶发失败实证）。
    """

    name: str = "hash_client"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        """构造后端，dim 必须为正。"""
        self.dim = dim if dim > 0 else DEFAULT_DIM

    async def embed(self, texts: list[str]) -> list[Vector]:
        """把每条文本散列成 dim 维单位向量。"""
        out: list[Vector] = []
        for text in texts:
            buckets = [0.0] * self.dim
            for word in text.lower().split():
                digest = blake2b(word.encode("utf-8"), digest_size=8).digest()
                buckets[int.from_bytes(digest, "big") % self.dim] += 1.0
            norm = sum(v * v for v in buckets) ** 0.5 or 1.0
            out.append(tuple(v / norm for v in buckets))
        return out
