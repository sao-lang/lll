"""WebFetcher 默认适配器（R5 G06 / 规则 9）：真发 GET 的 HttpFetcher。

框架为 R5 的 WebFetcher 协议提供开箱可用的默认实现——此前只有协议 + 注入 fetcher
和测试 fake，协议公开却连不上任何网页。网络 IO 集中在本适配器，harness/tools
不 import httpx（规则 8）；测试注入 httpx.MockTransport 即可不联网闭环。

纪律：非 2xx / 网络故障 / 编码异常统一折叠为 RuntimeError，交由上层 WebFetchTool
再折叠为 ToolResult，不让裸 httpx 异常抛穿；正文按 max_bytes 流式截断，防止超大
响应挤爆下游模型上下文。
"""
from __future__ import annotations

import httpx

from compose import PortCtx

from harness.tools import WebFetcher

__all__ = ["HttpFetcher"]


class HttpFetcher:
    """真 HTTP 抓取后端：GET 取正文，按 max_bytes 截断，异常折叠为 RuntimeError。

    属性：name: 适配器名（可插拔物留名纪律）。
    """

    name = "http_fetch"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        max_bytes: int = 1_000_000,
    ) -> None:
        """构造：client 可注入（测试用 MockTransport 不联网），缺省自建；timeout/max_bytes 控上限。"""
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        self._timeout = timeout
        self._max_bytes = max_bytes

    async def _read_capped(self, response: httpx.Response) -> bytes:
        """流式累加正文至 max_bytes 即停（剩余响应体不读，超大响应不进内存/上下文）。"""
        parts: list[bytes] = []
        used = 0
        async for chunk in response.aiter_bytes():
            room = self._max_bytes - used
            if room <= 0:
                # 已达上限：立即停止消费，避免继续拉取超大响应体浪费带宽/内存。
                break
            piece = chunk[:room]
            parts.append(piece)
            used += len(piece)
        return b"".join(parts)

    @staticmethod
    def _decode(raw: bytes, encoding: str) -> str:
        """按响应声明的编码解码正文；非法 charset 名退回 utf-8，不让解码异常抛穿。"""
        try:
            # errors=replace 让被 max_bytes 截断在多字节字符中间的残片可读，不触发 UnicodeDecodeError。
            return raw.decode(encoding, errors="replace")
        except LookupError:
            # 服务端声明了未知 charset 名：退回 utf-8 保住正文，而非让 RuntimeError 之外的异常裸抛。
            return raw.decode("utf-8", errors="replace")

    async def fetch(self, url: str, ctx: PortCtx) -> str:
        """GET 抓取：非 2xx/网络异常折叠 RuntimeError，正文按 max_bytes 截断后返回文本。"""
        try:
            async with self._client.stream("GET", url, timeout=self._timeout) as response:
                status = response.status_code
                if status < 200 or status >= 300:
                    # 非 2xx 是抓取失败：显式带状态码报错，不让错误响应体被误当正文送进检测器。
                    raise RuntimeError(f"WebFetch HTTP {status}: {url}")
                raw = await self._read_capped(response)
                return self._decode(raw, response.encoding or "utf-8")
        except httpx.HTTPError as exc:
            # 网络层（连接/超时/DNS）异常：折叠为 RuntimeError，由 WebFetchTool 再包 ToolResult。
            raise RuntimeError(f"WebFetch network error: {url}: {exc}") from exc

    async def aclose(self) -> None:
        """仅关闭自建 client；注入的 client 归调用方所有，本适配器不越权关闭。"""
        if self._owns_client:
            await self._client.aclose()


# 履约校验：真实现必须满足 R5 的 WebFetcher 协议（装配期由 isinstance 校验，这里仅静态断言）。
_http_fetcher_is_webfetcher: type[WebFetcher] = HttpFetcher
