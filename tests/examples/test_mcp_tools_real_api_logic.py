"""FR-4/5 真实工具逻辑：用可控 HTTP 替身验证天气/搜索 MCP server 的请求与解析。

沙箱无外网（SSL 超时）→ 用 urllib.request.urlopen 替身注入，验证：
- 天气：用城市坐标请求 open-meteo 正确 URL，解析 temperature_2m；
- 搜索：请求 DDG lite，解析 title/href；
- 失败：返回 status=error 与原始异常，不伪造数据。
真实联网由用户在有网环境执行 `python examples/react-chat/mcp_servers/weather_server.py` 验证。
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MCP = ROOT / "examples" / "react-chat" / "mcp_servers"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, MCP / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_weather_parses_real_api_shape(monkeypatch: object) -> None:
    weather = _load("weather_server")

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a: object) -> None: pass

    captured: list[str] = []
    def fake_urlopen(url: str, timeout: float = 8.0):
        captured.append(url)
        return Resp(json.dumps({"current": {
            "temperature_2m": 26.3, "weather_code": 0, "time": "2026-11-24T09:00"}}
        ).encode())
    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)  # type: ignore[attr-defined]
    monkeypatch.delenv("CHAT_OFFLINE", raising=False)  # type: ignore[attr-defined]

    text = weather.get_weather({"city": "深圳"})["content"][0]["text"]
    data = json.loads(text)
    assert data["status"] == "ok" and data["temperature"] == 26.3
    assert "api.open-meteo.com" in captured[0] and "latitude=22.5431" in captured[0]


def test_search_parses_titles_and_links(monkeypatch: object) -> None:
    search = _load("search_server")

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a: object) -> None: pass

    html = '<a class="result__a" href="https://ex.com/a">AgentKit <b>框架</b></a>'
    monkeypatch.setattr(search.urllib.request, "urlopen",  # type: ignore[attr-defined]
                        lambda *a, **kw: Resp(html.encode()))
    monkeypatch.delenv("CHAT_OFFLINE", raising=False)  # type: ignore[attr-defined]

    text = search.search({"query": "AgentKit"})["content"][0]["text"]
    data = json.loads(text)
    assert data["status"] == "ok" and data["results"][0]["url"] == "https://ex.com/a"
    assert "框架" in data["results"][0]["title"]


def test_failure_returns_error_not_fabricated(monkeypatch: object) -> None:
    weather = _load("weather_server")
    def boom(*a: object, **kw: object):
        raise OSError("network down")
    monkeypatch.setattr(weather.urllib.request, "urlopen", boom)  # type: ignore[attr-defined]
    monkeypatch.delenv("CHAT_OFFLINE", raising=False)  # type: ignore[attr-defined]
    data = json.loads(weather.get_weather({"city": "深圳"})["content"][0]["text"])
    assert data["status"] == "error" and "network down" in data["message"]
