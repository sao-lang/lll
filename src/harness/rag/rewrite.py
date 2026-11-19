"""R6 改写段：QueryRewriter 协议 + 多查询默认实现。

默认多查询把原查询复制到 n_variants 份（守"不引入原问题没有的实体"校验）；
生产可注入 HyDE/step-back 策略替换，上层零改。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.types import Query

__all__ = ["QueryRewriter", "MultiQueryRewriter", "DEFAULT_N_VARIANTS"]

DEFAULT_N_VARIANTS: int = 3


@runtime_checkable
class QueryRewriter(Protocol):
    """查询改写协议。"""

    name: str

    async def rewrite(self, query: Query) -> list[Query]:
        """把一个查询改写为多个查询。"""
        ...


class MultiQueryRewriter:
    """默认改写：复制原查询为多份（不引入新实体）。"""

    name: str = "multi_query"

    def __init__(self, n_variants: int = DEFAULT_N_VARIANTS) -> None:
        """构造改写器，n_variants 为改写份数。"""
        self.n_variants = max(1, n_variants)

    async def rewrite(self, query: Query) -> list[Query]:
        """返回 n_variants 份等价查询（复用同一 Query 结构）。"""
        return [query.model_copy(deep=True) for _ in range(self.n_variants)]
