"""B1 回归：start.py 必须从注册表兜底读 OTEL_EXPORTER_OTLP_ENDPOINT。

否则旧终端 setx 后启动 → endpoint 丢失 → 静默退化 NoopTelemetry（零上报）。
用 monkeypatch 替换 _read_user_env 与 subprocess.call，不真起服务。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples" / "react-chat"))


def test_red_start_reads_otlp_endpoint_from_registry(monkeypatch: object) -> None:
    import start
    import os
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # type: ignore[attr-defined]

    def fake_read(name: str) -> str | None:
        return {"DEEPSEEK_API_KEY": "sk-test",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4318"}.get(name)
    monkeypatch.setattr(start, "_read_user_env", fake_read)  # type: ignore[attr-defined]
    captured: dict[str, object] = {}
    monkeypatch.setattr(start.subprocess, "call",  # type: ignore[attr-defined]
                        lambda cmd: captured.setdefault("env", dict(os.environ)) or 0)

    try:
        start.main()
    except SystemExit:
        pass
    env = captured.get("env", {})
    assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://jaeger:4318", (
        "start.py 未从注册表加载 OTLP endpoint → 链路零上报")
