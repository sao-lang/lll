"""Port 注册表与调用上下文（M3 §4）。

命名空间固定：内置 12 个 port 名；用户自定义必须 `x-<名字>`。
装配期 freeze() 缺 port 直接报错（含缺失清单）——绝不进运行期才炸。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from runtime import PortMissing

__all__ = ["BUILTIN_PORTS", "PortCtx", "PortRegistry", "PortContractError", "port_ctx_from"]

BUILTIN_PORTS = frozenset({
    "llm", "tool", "skill", "memory", "store", "sandbox", "audit",
    "telemetry", "eval", "human", "mcp", "rag",
})


@dataclass(frozen=True)
class PortCtx:
    """适配器获取会话上下文的唯一途径（适配器禁止持有会话状态）。"""

    session_id: str
    trace_span: str = ""
    budget_left: Any = None
    run_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class PortContractError(Exception):
    """适配器未实现声明的 Protocol。"""


def port_ctx_from(ctx: Any) -> PortCtx:
    """把 M1 RuntimeCtx / M2 ChainCtx 折叠为 PortCtx（duck typing，不 import 上层）。"""
    meta = dict(getattr(ctx, "meta", {}) or {})
    return PortCtx(
        session_id=getattr(ctx, "session_id", ""),
        trace_span="|".join(list(getattr(ctx, "trace", []) or [])[-3:]),
        budget_left=getattr(ctx, "budget_left", None),
        run_id=meta.get("run_id", ""),
        meta=meta,
    )


class PortRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, name: str, adapter: Any, *, protocol: type | None = None) -> "PortRegistry":
        if name not in BUILTIN_PORTS and not name.startswith("x-"):
            raise PortContractError(
                f"custom port must use 'x-<name>' prefix, got {name!r}"
            )
        if protocol is not None and not isinstance(adapter, protocol):
            raise PortContractError(
                f"adapter {type(adapter).__name__} does not implement {protocol.__name__}"
            )
        self._adapters[name] = adapter
        return self

    def resolve(self, name: str) -> Any:
        if name not in self._adapters:
            raise PortMissing(f"port {name!r} is not registered")
        return self._adapters[name]

    def try_resolve(self, name: str) -> Any | None:
        return self._adapters.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def missing(self, required: Iterable[str] = ("llm", "tool", "telemetry")) -> list[str]:
        """返回缺失 port 清单（供装配器打印）。"""
        return [p for p in required if p not in self._adapters]

    def freeze(self, required: Iterable[str] = ("llm", "tool", "telemetry")) -> "PortRegistry":
        """装配期检查；缺 port 抛 PortMissing（含清单）。返回 self 以支持链式调用。"""
        missing = self.missing(required)
        if missing:
            raise PortMissing(f"missing ports: {', '.join(sorted(missing))}")
        return self
