"""默认可观测适配器：本地 JSONL trace（外部平台 = 实现 TelemetryPort 替换）。

不写墙钟时间：保证事件可确定性比对；需要时间戳由外部平台适配器自行注入。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from runtime import DynamicValue

from ..registry import PortCtx

__all__ = ["JsonlTelemetry", "NullTelemetry", "TraceSpan"]


@dataclass(frozen=True)
class TraceSpan:
    """追踪跨度的标识（不写墙钟，保证事件可确定性比对）。"""

    name: str  # 跨度名
    session_id: str = ""  # 所属会话


class JsonlTelemetry:
    """本地 JSONL 追踪适配器；外部平台 = 实现 TelemetryPort 整体替换。"""

    name = "jsonl-telemetry"  # 适配器标识

    def __init__(self, path: str | Path) -> None:
        """装配输出路径并创建父目录。"""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: dict[str, DynamicValue]) -> None:
        """追加一条 JSON 行（sort_keys 保证确定性）。"""
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def metric(self, name: str, value: float, tags: dict[str, DynamicValue] | None = None) -> None:
        """以 log 形式记录一个指标。"""
        self.log({"event": "metric", "name": name, "value": value, "tags": tags or {}})

    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[TraceSpan]:
        """跨度上下文管理器：yield 前后各记一条 start/end 日志。"""
        span = TraceSpan(name=name, session_id=ctx.session_id)
        self.log({"event": "span_start", "name": name, "session_id": ctx.session_id})
        yield span
        self.log({"event": "span_end", "name": name, "session_id": ctx.session_id})

    def read(self) -> list[dict[str, DynamicValue]]:
        """读取全部日志行并解析为字典列表（供测试断言）。"""
        if not self._path.exists():
            return []
        return [json.loads(line) for line in self._path.read_text(encoding="utf-8").splitlines() if line]


class NullTelemetry:
    """空实现：吞掉所有遥测调用（默认或测试场景）。"""

    name = "null-telemetry"  # 适配器标识

    def log(self, event: dict[str, DynamicValue]) -> None:
        """空操作。"""
        return None

    def metric(self, name: str, value: float, tags: dict[str, DynamicValue] | None = None) -> None:
        """空操作。"""
        return None

    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[TraceSpan]:
        """空跨度：仅 yield 一个 TraceSpan。"""
        yield TraceSpan(name=name, session_id=ctx.session_id)
