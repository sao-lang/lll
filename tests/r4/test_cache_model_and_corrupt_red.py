"""RED：语义缓存必须按 model/temperature/工具隔离；损坏条目视为 miss 并删除。

独立审计 EXP3-A/B 证据：
- 同 query 不同 model 命中同一响应（cheap 的回答被 expensive 命中）。
- 脏 JSON 条目让 ExactCache.get 抛 ValidationError 穿透调用方。
"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, LLMResponse, PortCtx, Usage
from harness.model.cache import ExactCache, InMemoryCacheStore, SimilarityCache



def test_red_similarity_cache_isolated_by_model() -> None:
    """cheap-model 写入后，expensive-model 同 query 不得命中（精确断言 response.model）。"""
    async def run() -> None:
        store = InMemoryCacheStore()
        cache = SimilarityCache(store=store)
        cheap = LLMRequest(messages=[{"role": "user", "content": "same question"}], model="cheap-model")
        expensive = LLMRequest(messages=[{"role": "user", "content": "same question"}], model="expensive-model")
        answer = LLMResponse(content="ANSWER-FROM-CHEAP-MODEL", usage=Usage(cost_usd=0.01), model="cheap-model")
        await cache.put(cheap, answer)

        hit = await cache.get(expensive)
        assert hit is None, f"跨 model 不得命中：expensive-model 命中了 {hit.content if hit else None}"

        # 同 model 仍必须能正常命中，证明不是一刀切禁用缓存。
        same = await cache.get(cheap)
        assert same is not None and same.content == "ANSWER-FROM-CHEAP-MODEL"

    asyncio.run(run())


def test_red_similarity_cache_isolated_by_temperature() -> None:
    """temperature 不同视为不同响应，不得命中。"""
    async def run() -> None:
        cache = SimilarityCache(store=InMemoryCacheStore())
        warm = LLMRequest(messages=[{"role": "user", "content": "q"}], temperature=0.2)
        cold = LLMRequest(messages=[{"role": "user", "content": "q"}], temperature=0.9)
        await cache.put(warm, LLMResponse(content="TEMP-02", model="m"))
        assert await cache.get(cold) is None

    asyncio.run(run())


def test_red_corrupt_exact_cache_entry_is_miss_and_deleted() -> None:
    """store 中脏 JSON → get 视为 miss、返回 None，并删除坏条目。"""
    async def run() -> None:
        store = InMemoryCacheStore()
        cache = ExactCache(store=store)
        req = LLMRequest(messages=[{"role": "user", "content": "q"}])
        key = "cache:exact:current:" + cache._identity(req, "current")
        await store.put(key, "{not-json")

        got = await cache.get(req)
        assert got is None, f"坏条目应视为 miss，实际返回 {got!r}"
        assert await store.get(key) is None, "坏条目必须从 store 删除，防止反复炸调用"
        assert cache.misses == 1

    asyncio.run(run())


def test_red_corrupt_similarity_cache_entry_is_skipped_and_deleted() -> None:
    """语义缓存条目的 resp 字段损坏 → 跳过并删除，不影响后续正确条目。"""
    async def run() -> None:
        store = InMemoryCacheStore()
        cache = SimilarityCache(store=store)
        good_req = LLMRequest(messages=[{"role": "user", "content": "good question"}])
        await cache.put(good_req, LLMResponse(content="GOOD-ANSWER", model="m"))

        keys = [k for k, _ in await store.scan("cache:semantic:current:")]
        assert len(keys) == 1
        import json
        raw = json.loads(await store.get(keys[0]) or "{}")  # type: ignore[arg-type]
        raw["resp"] = "{bad-json"
        await store.put(keys[0], json.dumps(raw))

        got = await cache.get(good_req)
        assert got is None, f"resp 损坏条目应视为 miss，实际返回 {got!r}"
        assert await store.get(keys[0]) is None, "损坏的语义条目必须被删除"

    asyncio.run(run())
