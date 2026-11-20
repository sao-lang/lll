"""R4 默认 CacheEmbedder 行为测试：验证四要素补齐（FR-1~FR-7）。"""
from __future__ import annotations

import asyncio

import json
import math
import random
import string

import pytest

from harness.model import (
    DEFAULT_CACHE_EMBED_DIM,
    CacheEmbedder,
    HashCacheEmbedder,
    InMemoryCacheStore,
    SimilarityCache,
)
from compose import LLMRequest, LLMResponse


def _req(text: str) -> LLMRequest:
    """构造单条 user 消息的查询请求。"""
    return LLMRequest(messages=[{"role": "user", "content": text}])


def _resp(text: str) -> LLMResponse:
    """构造测试响应。"""
    return LLMResponse(content=text)


def _embed(text: str, *, dim: int | None = None) -> tuple[float, ...]:
    """同步执行 embed 的测试辅助。"""
    inst = HashCacheEmbedder() if dim is None else HashCacheEmbedder(dim=dim)
    return asyncio.run(inst.embed(text))


def test_fr1_implements_protocol() -> None:
    """FR-1：默认 embedder 履约 CacheEmbedder 协议。"""
    assert isinstance(HashCacheEmbedder(), CacheEmbedder)


def test_fr2_deterministic_and_fixed_dim() -> None:
    """FR-2：同文本产出相同向量；维度固定为默认值（512，抗哈希碰撞）。"""
    a = _embed("向量召回和关键词精排")
    b = _embed("向量召回和关键词精排")
    assert a == b
    assert len(a) == DEFAULT_CACHE_EMBED_DIM == 512
    assert any(v != 0.0 for v in a)


def test_fr2_l2_normalized() -> None:
    """FR-2：向量 L2 归一（模长约等于 1）。"""
    import math

    vec = _embed("hello world")
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 1e-6


def test_fr2_different_texts_differ() -> None:
    """FR-2：不同文本产出不同向量（能支撑相似度区分）。"""
    assert _embed("向量召回") != _embed("如何烘焙面包")


def test_fr3_invalid_dim_rejected() -> None:
    """FR-3：非法维度（≤0）抛 ValueError。"""
    import pytest

    with pytest.raises(ValueError):
        HashCacheEmbedder(dim=0)


def test_fr3_custom_dim() -> None:
    """FR-3：自定义维度生效。"""
    assert len(_embed("test", dim=8)) == 8


def test_fr4_exported_from_package() -> None:
    """FR-4：默认 embedder 从 harness.model 包导出（本文件顶部 import 已验证）。"""
    assert HashCacheEmbedder.__name__ == "HashCacheEmbedder"


def test_fr5_similarity_cache_defaults_to_vector() -> None:
    """FR-5：SimilarityCache 无参构造默认用 HashCacheEmbedder；显式 None 降级 difflib。"""
    cache = SimilarityCache()
    assert isinstance(cache._embedder, HashCacheEmbedder)
    plain = SimilarityCache(embedder=None)
    assert plain._embedder is None


def test_fr6_existing_similarity_behaviour_preserved() -> None:
    """FR-6：默认改用向量后，exact/near/distant 三档阈值行为不退化。"""
    exact = SimilarityCache()
    asyncio.run(exact.put(_req("what is python"), _resp("a language")))
    assert asyncio.run(exact.get(_req("what is python"))) is not None

    near = SimilarityCache(threshold=0.7)
    asyncio.run(near.put(_req("what is the capital of france"), _resp("Paris")))
    assert asyncio.run(near.get(_req("what is capital of france"))) is not None

    distant = SimilarityCache(threshold=0.9)
    asyncio.run(distant.put(_req("what is python"), _resp("a language")))
    assert asyncio.run(distant.get(_req("how to bake bread"))) is None


def test_fr2_empty_text_is_zero_vector() -> None:
    """Should-2：空文本返回零向量，两空 query 不会互相命中。"""
    assert _embed("") == (0.0,) * DEFAULT_CACHE_EMBED_DIM


@pytest.mark.parametrize("length,kind", [
    (600, "repeat_cn"), (1000, "repeat_cn"), (600, "random_ascii"),
    (1000, "random_ascii"), (600, "random_cn"),
])
def test_fr6_long_unrelated_texts_do_not_collide(length: int, kind: str) -> None:
    """Blocker-1 回归：多种形态的不相关长文本在默认阈值下均不得误命中。"""
    random.seed(20261120)
    if kind == "repeat_cn":
        text_a = ("向量召回与关键词混合检索提升命中率，" * 90)[:length]
        text_b = ("烘焙面包需要面粉酵母和温水发酵，" * 90)[:length]
    elif kind == "random_ascii":
        alphabet = string.ascii_lowercase + " "
        text_a = "".join(random.choice(alphabet) for _ in range(length))
        text_b = "".join(random.choice(alphabet) for _ in range(length))
    else:
        alphabet = "向量召回关键词检索精排文档中文测试烘焙面包发酵面粉"
        text_a = "".join(random.choice(alphabet) for _ in range(length))
        text_b = "".join(random.choice(alphabet) for _ in range(length))
    cache = SimilarityCache(threshold=0.85)
    asyncio.run(cache.put(_req(text_a), _resp("answer-a")))
    assert asyncio.run(cache.get(_req(text_b))) is None


def test_fr7_default_embedder_vector_persisted() -> None:
    """FR-7：默认 embedder 写入的条目含定长归一化向量（供 _cosine 比较）。"""
    store = InMemoryCacheStore()
    cache = SimilarityCache(store=store)
    asyncio.run(cache.put(_req("向量召回"), _resp("answer")))
    raw = next(iter(asyncio.run(store.scan("cache:semantic:"))))[1]
    entry = json.loads(raw)
    vec = entry["vec"]
    assert vec is not None and len(vec) == DEFAULT_CACHE_EMBED_DIM
    assert any(v != 0.0 for v in vec)
    assert abs(math.sqrt(sum(v * v for v in vec)) - 1.0) < 1e-6


def test_fr5_explicit_none_still_uses_difflib() -> None:
    """FR-5：显式 embedder=None 走 difflib 降级，仍能命中近似文本。"""
    cache = SimilarityCache(embedder=None, threshold=0.7)
    asyncio.run(cache.put(_req("what is the capital of france"), _resp("Paris")))
    assert asyncio.run(cache.get(_req("what is capital of france"))) is not None


def test_nfr1_no_model_no_io() -> None:
    """NFR-1：默认 embedder 不 import 模型/网络/文件模块。"""
    import inspect

    from harness.model import cache as cache_mod

    src = inspect.getsource(cache_mod)
    for banned in ("openai", "httpx", "requests", "torch"):
        assert banned not in src
