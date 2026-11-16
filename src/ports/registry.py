"""Port 注册表与调用上下文（M3 §4）。

命名空间固定：内置 12 个 port 名；用户自定义必须 `x-<名字>`。
装配期 freeze() 缺 port 直接报错（含缺失清单）——绝不进运行期才炸。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from runtime import DynamicValue, PortMissing

__all__ = ["BUILTIN_PORTS", "PortCtx", "PortRegistry", "PortContractError", "port_ctx_from"]

BUILTIN_PORTS = frozenset({
    "llm", "tool", "skill", "memory", "store", "sandbox", "audit",
    "telemetry", "eval", "human", "mcp", "rag",
})


@dataclass(frozen=True)
class PortCtx:
    """适配器获取会话上下文的唯一途径（适配器禁止持有会话状态）。"""

    session_id: str  # 会话标识
    trace_span: str = ""  # 最近若干 trace 片段的拼接，供链路追踪
    budget_left: DynamicValue = None  # 剩余预算（Budgets 实例；不 import 上层以免循环依赖）
    run_id: str = ""  # 跨周期运行标识
    meta: dict[str, DynamicValue] = field(default_factory=dict)  # 从上层 ctx 折叠来的元数据


class PortContractError(Exception):
    """适配器未实现声明的 Protocol（装配期契约错误）。"""


def port_ctx_from(ctx: DynamicValue) -> PortCtx:
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
    """port 名到适配器的注册表；装配期 freeze() 校验必需 port 齐备。"""

    def __init__(self) -> None:
        self._adapters: dict[str, DynamicValue] = {}  # port 名 → 适配器实例

    def register(self, name: str, adapter: DynamicValue, *, protocol: type | None = None) -> PortRegistry:
        """注册适配器；自定义 port 必须以 x- 开头，可选校验实现的 Protocol。"""
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

    def resolve(self, name: str) -> DynamicValue:
        """按名取适配器；未注册抛 PortMissing（运行期缺 port 的硬失败）。"""
        if name not in self._adapters:
            raise PortMissing(f"port {name!r} is not registered")
        return self._adapters[name]

    def try_resolve(self, name: str) -> DynamicValue | None:
        """按名取适配器；未注册返回 None（供可选 port 探测）。"""
        return self._adapters.get(name)

    def names(self) -> tuple[str, ...]:
        """返回已注册 port 名的排序元组。"""
        return tuple(sorted(self._adapters))

    def missing(self, required: Iterable[str] = ("llm", "tool", "telemetry")) -> list[str]:
        """返回缺失 port 清单（供装配器打印）。"""
        return [p for p in required if p not in self._adapters]

    def freeze(self, required: Iterable[str] = ("llm", "tool", "telemetry")) -> PortRegistry:
        """装配期检查；缺 port 抛 PortMissing（含清单）。返回 self 以支持链式调用。"""
        missing = self.missing(required)
        if missing:
            raise PortMissing(f"missing ports: {', '.join(sorted(missing))}")
        return self
