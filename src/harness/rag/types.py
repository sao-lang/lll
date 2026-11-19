"""R6 检索增强核心 DTO：文档块、命中、查询、结果与预算。

本模块只描述数据结构（pydantic 严格模型 + 有限取值枚举），不含检索逻辑。
向量用具名别名 ``Vector`` 承载，避免散落的裸 ``tuple[float, ...]``。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Vector",
    "QueryPurpose",
    "Document",
    "Chunk",
    "Hit",
    "Query",
    "TokenBudget",
    "RAGResult",
    "RRF_K",
]

# 具名别名：向量是定长浮点元组，集中定义一处（规则 1）。
type Vector = tuple[float, ...]

# RRF 融合常数（设计 §5：RRF(d)=Σ weight/(k+rank)）。
RRF_K: int = 60


class QueryPurpose(str, Enum):
    """查询用途（有限取值集合）；区分问答与记忆召回。

    成员：
        QA: 问答检索。MEMORY: 记忆召回（R7 复用本模块时）。
    """

    QA = "qa"
    MEMORY = "memory"


class _Strict(BaseModel):
    """严格基类：拒绝多余字段，避免 DTO 被静默塞入拼错字段。"""

    model_config = ConfigDict(extra="forbid")


class Document(_Strict):
    """待入库的原始文档。

    字段：
        doc_id: 文档唯一标识（引用溯源用）。
        text: 正文。
        meta: 元数据（标题/页码/权限标签等，键由上游决定故为动态映射）。
    """

    doc_id: str = Field(description="文档唯一标识")
    text: str = Field(description="正文")
    meta: dict[str, object] = Field(default_factory=dict, description="元数据")


class Chunk(_Strict):
    """切分后的文档块，可携带向量。

    字段：
        chunk_id: 块唯一标识，形如 ``doc_id:序号``（引用溯源用）。
        doc_id: 来源文档标识。
        text: 块正文。
        tokens: 块 token 估算数（本实现按字数计）。
        meta: 元数据副本。
        vector: 向量化产物（入库前由 Embedder 填充；纯文本块为 None）。
    """

    chunk_id: str = Field(description="块唯一标识")
    doc_id: str = Field(description="来源文档标识")
    text: str = Field(description="块正文")
    tokens: int = Field(default=0, description="token 估算数")
    meta: dict[str, object] = Field(default_factory=dict, description="元数据")
    vector: Vector | None = Field(default=None, description="向量化产物")


class Hit(_Strict):
    """检索命中：块 + 相关度 + 来源。

    字段：
        chunk: 命中的块。
        score: 相关度分数（越大越相关）。
        source: 来源标识（召回通道名或 doc_id）。
        ts: 命中时间戳（可选）。
    """

    chunk: Chunk = Field(description="命中的块")
    score: float = Field(default=0.0, description="相关度分数")
    source: str = Field(default="", description="来源标识")
    ts: int | None = Field(default=None, description="命中时间戳")


class Query(_Strict):
    """检索查询。

    字段：
        text: 查询文本。
        filters: 元数据过滤条件（键由上游 schema 决定，故为动态映射）。
        top_k: 每通道召回数。
        token_budget: 本查询的 token 预算（None 表示不限制）。
        purpose: 查询用途（区分问答/记忆召回）。
    """

    text: str = Field(description="查询文本")
    filters: dict[str, object] | None = Field(default=None, description="元数据过滤")
    top_k: int = Field(default=5, description="每通道召回数")
    token_budget: int | None = Field(default=None, description="token 预算")
    purpose: QueryPurpose = Field(default=QueryPurpose.QA, description="查询用途")


class TokenBudget(_Strict):
    """token 预算包装（具名类型，避免裸 int 在签名里语义不明）。

    字段：
        limit: 预算上限；None 表示不限制，非负整数表示硬上限（0 = 无预算可分配）。
    """

    limit: int | None = Field(default=None, description="预算上限；None 不限制，0 无预算")

    def __int__(self) -> int:
        """返回预算整数值；None 按 -1 返回（调用方据此判不限）。"""
        return self.limit if self.limit is not None else -1


class RAGResult(_Strict):
    """检索最终结果（注入 LLM 上下文的产物）。

    字段：
        context: 拼接后的上下文文本（含引用标注；空结果时为固定提示）。
        citations: 引用 id 列表（形如 ``doc:chunk``，逐条可在 hits 中溯源）。
        hits: 参与拼接的命中列表。
        empty: 是否空结果（True 时 context 为固定提示，模型应拒答）。
        tokens_used: 本次拼接消耗的 token 数。
    """

    context: str = Field(default="", description="拼接后的上下文文本")
    citations: list[str] = Field(default_factory=list, description="引用 id 列表")
    hits: list[Hit] = Field(default_factory=list, description="参与拼接的命中")
    empty: bool = Field(default=False, description="是否空结果")
    tokens_used: int = Field(default=0, description="拼接消耗的 token 数")
