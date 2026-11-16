"""默认可观测适配器：本地 JSONL trace（外部平台 = 实现 TelemetryPort 替换）。

不写墙钟时间：保证事件可确定性比对；需要时间戳由外部平台适配器自行注入。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..registry import PortCtx

__all__ = ["JsonlTelemetry", "NullTelemetry", "TraceSpan"]


@dataclass(frozen=True)
class TraceSpan:
    name: str
    session_id: str = ""


class JsonlTelemetry:
    name = "jsonl-telemetry"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def metric(self, name: str, value: float, tags: dict[str, Any] | None = None) -> None:
        self.log({"event": "metric", "name": name, "value": value, "tags": tags or {}})

    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[TraceSpan]:
        span = TraceSpan(name=name, session_id=ctx.session_id)
        self.log({"event": "span_start", "name": name, "session_id": ctx.session_id})
        yield span
        self.log({"event": "span_end", "name": name, "session_id": ctx.session_id})

    def read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [json.loads(line) for line in self._path.read_text(encoding="utf-8").splitlines() if line]


class NullTelemetry:
    name = "null-telemetry"

    def log(self, event: dict[str, Any]) -> None:
        return None

    def metric(self, name: str, value: float, tags: dict[str, Any] | None = None) -> None:
        return None

    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[TraceSpan]:
        yield TraceSpan(name=name, session_id=ctx.session_id)
