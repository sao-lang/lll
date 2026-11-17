"""R4 模型与推理能力：多 Provider / 模型路由 / 缓存 / 结构化输出 / 成本归因。

全部基于 R1（runtime）+ R2（hooks，复用其 LLMRequest/LLMResponse/Usage/LLMPort）构建。
公共 API 见 docs/02-模块详设/Agent/R4-模型与推理.md §4。
"""
from __future__ import annotations

from .adapter import RecordingLLM, StreamLLM
from .cache import ExactCache, SemanticCache
from .capability import ModelCapability
from .gateway import ModelGateway
from .router import ModelRouter, RuleRouter
from .structured import StructuredOutputValidator, validate_json_schema
from .types import (
    CacheKey,
    CostAttributor,
    CostGuard,
    CostRecord,
    RouteDecision,
    StructuredOutputSpec,
    UsageLedger,
    build_cache_key,
    cache_key_string,
)

__all__ = [
    "StructuredOutputSpec", "CacheKey", "RouteDecision", "CostRecord",
    "UsageLedger", "CostAttributor", "CostGuard",
    "build_cache_key", "cache_key_string",
    "ModelRouter", "RuleRouter", "SemanticCache", "ExactCache",
    "validate_json_schema", "StructuredOutputValidator",
    "RecordingLLM", "StreamLLM", "ModelGateway", "ModelCapability",
]
