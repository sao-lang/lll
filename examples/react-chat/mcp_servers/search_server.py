"""MCP server · 资料搜索（公开搜索端点，无密钥；失败返回原始错误不伪造结果）。

用 DuckDuckGo HTML Lite（无需 API key）；若网络/格式变化导致失败，
返回 status=error 与原始错误——绝不编造标题/链接。
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _server import MCPServer, text_result

TOOLS = [{
    "name": "search",
    "description": "搜索互联网资料，返回标题与链接（DuckDuckGo HTML，无密钥）",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索关键词"}},
        "required": ["query"],
    },
}]

_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return text_result("error: query 不能为空")
    if os.getenv("CHAT_OFFLINE") == "1":
        return text_result(json.dumps(
            {"query": query, "status": "offline",
             "message": "离线模式：不伪造搜索结果，请关闭 CHAT_OFFLINE 后重试"},
            ensure_ascii=False))
    try:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AgentKit-demo"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="replace")
        hits = []
        for href, title in _RESULT_RE.findall(page)[:5]:
            clean = html.unescape(_TAG_RE.sub("", title)).strip()
            hits.append({"title": clean, "url": html.unescape(href)})
        if not hits:
            return text_result(json.dumps(
                {"query": query, "status": "empty",
                 "message": "未解析到结果（搜索页结构可能变化）"}, ensure_ascii=False))
        return text_result(json.dumps(
            {"query": query, "status": "ok", "results": hits}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 真实错误上送，不包装成成功
        return text_result(json.dumps(
            {"query": query, "status": "error", "message": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False))


def main() -> None:
    server = MCPServer("search", TOOLS)
    server.handlers["search"] = search
    server.run()


if __name__ == "__main__":
    main()
