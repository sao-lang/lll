"""R4 独立 demo：自定义缓存后端（Redis 风格异步 store + 向量 embedding）。

演示用户如何把缓存落到 Redis/DB、如何换 embedding 模型——用 fake async client，
不引入真实 redis/向量库依赖，全程离线可跑。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import LLMRequest, LLMResponse, PortCtx, Usage  # noqa: E402

from harness.model import (  # noqa: E402
    CacheEmbedder,
    CacheStore,
    ExactCache,
    ModelGateway,
    SimilarityCache,
)


class FakeRedisClient:
    """模拟 redis.asyncio：get/set/delete/scan 都是 async。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value  # ex(TTL) 此处忽略，真实 Redis 会设置

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def scan_keys(self, prefix: str) -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]


class RedisCacheStore:
    """把 CacheStore 落到 Redis：直接 await redis 客户端，不阻塞事件循环。"""

    name = "redis_cache_store"

    def __init__(self, client: FakeRedisClient) -> None:
        self._client = client

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def put(self, key: str, value: str, *, ttl: int | None = None) -> None:
        await self._client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def scan(self, prefix: str) -> list[tuple[str, str]]:
        keys = await self._client.scan_keys(prefix)
        return [(k, await self._client.get(k) or "") for k in keys]


class FakeVectorEmbedder:
    """模拟本地/OpenAI embedding：关键词→固定向量（真实实现换成 API 调用即可）。"""

    name = "fake_vector_embedder"

    async def embed(self, text: str) -> tuple[float, ...]:
        t = text.lower()
        if "python" in t:
            return (1.0, 0.0, 0.0)
        if "java" in t:
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)


class FakeLLM:
    name = "fake_llm"
    last_usage = Usage()

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="answer-from-llm", usage=Usage(cost_usd=0.1))

    def stream(self, req: LLMRequest, ctx: PortCtx):
        async def gen():
            yield "answer"
        return gen()


async def main() -> None:
    ctx = PortCtx(session_id="s-demo", run_id="r-demo")

    # --- 场景 1：精确缓存 + Redis store ---
    redis = FakeRedisClient()
    exact = ExactCache(store=RedisCacheStore(redis), ttl=3600)
    gw = ModelGateway(llm=FakeLLM(), cache=exact)
    req = LLMRequest(messages=[{"role": "user", "content": "hello"}])
    r1 = await gw.complete(req, ctx)
    r2 = await gw.complete(req, ctx)
    print("[精确+Redis] 首次:", r1.content, "| 二次:", r2.content)
    print("[精确+Redis] Redis keys:", len(redis.data), "| 命中:", exact.hits)

    # --- 场景 2：语义缓存 + Redis store + 向量 embedding ---
    redis2 = FakeRedisClient()
    sem = SimilarityCache(
        store=RedisCacheStore(redis2),
        embedder=FakeVectorEmbedder(),
        threshold=0.9,
    )
    gw2 = ModelGateway(llm=FakeLLM(), cache=sem)
    await gw2.complete(
        LLMRequest(messages=[{"role": "user", "content": "tell me about python"}]), ctx
    )
    hit = await gw2.complete(
        LLMRequest(messages=[{"role": "user", "content": "python programming language"}]), ctx
    )
    print("[语义+Redis+向量] 字面不同但语义命中:", hit.content)
    print("[语义+Redis+向量] Redis keys:", len(redis2.data), "| 命中:", sem.hits)

    # 契约校验
    assert isinstance(RedisCacheStore(redis), CacheStore)
    assert isinstance(FakeVectorEmbedder(), CacheEmbedder)
    print("[PASS] 自定义 store/embedder 满足协议；全程离线、异步不阻塞")


if __name__ == "__main__":
    asyncio.run(main())
