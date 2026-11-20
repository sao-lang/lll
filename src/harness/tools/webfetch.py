"""R5 工具封装：WebFetchTool（D13 浏览器/Web 访问）。

纪律：抓取正文一律视为不可信输入，L3 披露前必须过 InjectionDetector 检测；
命中注入则拒载并记审计。网络 IO 由注入的 WebFetcher 承担，本模块不 import IO SDK
（规则 8：能力插件不直接依赖 httpx/requests），单测用 fake fetcher 闭环。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from compose import AuditPort, PortCtx, ToolSpec

from .types import DangerLevel

from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector

from .types import ToolSource

__all__ = ["WebFetcher", "WebFetchTool"]


@runtime_checkable
class WebFetcher(Protocol):
    """网页抓取后端（用户可注入真实 HTTP/浏览器客户端；R5 不内置 IO SDK）。"""
    name: str

    async def fetch(self, url: str, ctx: PortCtx) -> str:
        """抓取给定 URL 的原始正文（形态为文本，交由检测器净化）。"""
        ...


class WebFetchTool:
    """WebFetchTool：抓取 + 注入检测 + 审计，命中注入拒载。

    字段：
        name: 工具名。spec: 供统一 Registry 注册的目录条目（danger=SAFE，检测在工具内强制）。
    """

    name = "web_fetch"

    def __init__(
        self,
        fetcher: WebFetcher,
        *,
        detector: InjectionDetector | None = None,
        audit: AuditPort | None = None,
    ) -> None:
        """装配抓取后端、注入检测器与可选审计 Port。"""
        self._fetcher = fetcher
        self._detector = detector or KeywordInjectionDetector()
        self._audit = audit
        self.spec = ToolSpec(
            name=self.name,
            description="Fetch web page body (untrusted input, injection-checked)",
            parameters_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            danger=DangerLevel.LOW,  # 外部内容风险：走审计路径（R9 §5 LOW=审计）
            source=ToolSource.LOCAL.value,
        )

    async def fetch(self, url: str, ctx: PortCtx) -> str:
        """抓取并检测：命中注入则拒载（ValueError）并审计；干净正文放行。"""
        raw = await self._fetcher.fetch(url, ctx)
        verdict = self._detector.detect(raw)
        if verdict is InjectionVerdict.INJECTED:
            # 不可信正文命中注入：拒载而非净化，避免把带攻击载荷的文本送进模型上下文。
            if self._audit is not None:
                await self._audit.record(
                    {"tool": self.name, "url": url, "verdict": verdict.value}, ctx
                )
            raise ValueError(f"Injection detected in fetched body; refusing to load: {url}")

        if self._audit is not None:
            # 干净正文也留痕，满足 R9「网页视为不可信」的可审计要求。
            await self._audit.record(
                {"tool": self.name, "url": url, "verdict": verdict.value}, ctx
            )
        return raw
