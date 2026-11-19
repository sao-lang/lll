"""R6 检索增强包：可分层评测的 RAG 流水线（五段 + 三增强 + 横切）。

公共 API 与各段协议从本包导出，装配层（R12）按 ``RAGPipeline(...)`` 构造并
以 ``PORT_PROTOCOLS["rag"]`` 校验履约。
"""
from __future__ import annotations

from harness.rag.embedder import Embedder, EmbeddingClient, HashEmbedder
from harness.rag.pipeline import CachePort, InMemoryCache, RAGPipeline
from harness.rag.post import DefaultPostProcessor, PostProcessor
from harness.rag.retriever import HybridRetriever, Retriever, VectorRetriever
from harness.rag.rewrite import MultiQueryRewriter, QueryRewriter
from harness.rag.rerank import LexicalReranker, Reranker
from harness.rag.splitter import RecursiveSplitter, Splitter
from harness.rag.store import InMemoryVectorStore, VectorStore
from harness.rag.types import (
    Chunk,
    Document,
    Hit,
    Query,
    QueryPurpose,
    RAGResult,
    TokenBudget,
    Vector,
)

__all__ = [
    # 流水线
    "RAGPipeline", "CachePort", "InMemoryCache",
    # 核心 DTO
    "Document", "Chunk", "Hit", "Query", "QueryPurpose",
    "RAGResult", "TokenBudget", "Vector",
    # 五段协议与默认实现
    "Splitter", "RecursiveSplitter",
    "Embedder", "EmbeddingClient", "HashEmbedder",
    "VectorStore", "InMemoryVectorStore",
    "Retriever", "VectorRetriever", "HybridRetriever",
    "PostProcessor", "DefaultPostProcessor",
    # 增强协议与默认实现
    "QueryRewriter", "MultiQueryRewriter",
    "Reranker", "LexicalReranker",
]
