"""MCP server · 真实天气（open-meteo 免费 API，无密钥）。

离线（CHAT_OFFLINE=1）或网络失败时返回明确 `offline` 标记，不伪造天气数据。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _server import MCPServer, text_result

TOOLS = [{
    "name": "get_weather",
    "description": "查询指定城市的当前真实天气（温度、天气状况），来源 open-meteo",
    "inputSchema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名，如深圳/Beijing"}},
        "required": ["city"],
    },
}]

# 少量常见城市坐标（离线兜底不做假天气，仅用于在线请求）；未知城市走开放地理编码。
_CITY_COORDS = {
    "深圳": (22.5431, 114.0579), "shenzhen": (22.5431, 114.0579),
    "北京": (39.9042, 116.4074), "beijing": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737), "shanghai": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644), "guangzhou": (23.1291, 113.2644),
}


def _geocode(city: str) -> tuple[float, float] | None:
    if city in _CITY_COORDS:
        return _CITY_COORDS[city]
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"})
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results") or []
    if not results:
        return None
    return float(results[0]["latitude"]), float(results[0]["longitude"])


def get_weather(args: dict) -> dict:
    city = str(args.get("city") or "").strip()
    if not city:
        return text_result("error: city 不能为空")
    if os.getenv("CHAT_OFFLINE") == "1":
        return text_result(json.dumps(
            {"city": city, "status": "offline",
             "message": "离线模式：不伪造天气数据，请设 DEEPSEEK_API_KEY 并关闭 CHAT_OFFLINE"},
            ensure_ascii=False))
    try:
        coords = _geocode(city)
        if coords is None:
            return text_result(json.dumps(
                {"city": city, "status": "not_found", "message": "未找到城市坐标"},
                ensure_ascii=False))
        lat, lon = coords
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code", "timezone": "auto"})
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cur = data.get("current") or {}
        return text_result(json.dumps({
            "city": city, "status": "ok",
            "temperature": cur.get("temperature_2m"),
            "weather_code": cur.get("weather_code"),
            "source": "open-meteo", "time": cur.get("time"),
        }, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 真实错误上送，不伪造数据
        return text_result(json.dumps(
            {"city": city, "status": "error", "message": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False))


def main() -> None:
    server = MCPServer("weather", TOOLS)
    server.handlers["get_weather"] = get_weather
    server.run()


if __name__ == "__main__":
    main()
