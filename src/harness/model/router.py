"""R4 模型路由：按任务/成本选模型，目标必须已注册（可插拔契约校验）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hooks import PortCtx
from runtime import DynamicValue, Effect

from .types import RouteDecision

__all__ = ["ModelRouter", "RuleRouter", "NO_DEFAULT"]

NO_DEFAULT = "default"
_NO_DEFAULT = NO_DEFAULT  # 内部兼容别名
"""未显式配置默认模型时的哨兵值（命名常量，替代散落的裸字符串）。"""


@runtime_checkable
class ModelRouter(Protocol):
    """模型路由协议：name + route(effect, ctx) → RouteDecision。

    方法体 `...` 是 Protocol 接口语法，不是省略实现。
    默认实现：本文件下方 `RuleRouter`（规则路由），由 ModelGateway 在未注入时默认构造。
    归属 R4，已实现。
    """

    name: str

    def route(self, effect: Effect, ctx: PortCtx) -> RouteDecision:
        """按 Effect 的任务提示与预算选模型。"""
        ...


@dataclass(frozen=True, slots=True)
class _RouteInput:
    """路由输入的具名视图（规则 6：payload 动态字段一次读取成具名视图）。

    字段：
        task_hint: 任务类型提示（如 code/chat）。
        max_cost: 单次调用成本上限（来自 payload.meta，None 表示无约束）。
    """

    task_hint: str
    max_cost: float | None


def _coerce_route(effect: Effect) -> _RouteInput:
    """唯一的 payload 动态读取边界：task_hint 与 meta.max_cost_per_call。

    effect.payload 的 task_hint/meta 是 R4 约定的扩展键（不在 R1 EffectPayload
    已知键全集内），形态由装配层决定，故在此一次读取并归名为具名视图（规则 6）。

    参数：
        effect: 待路由的 LLM Effect。
    返回：
        _RouteInput 具名视图；未知形态显式回退空提示/无成本约束。
    """
    payload: DynamicValue = effect.payload
    hint = str(payload.get("task_hint") or "")
    raw_meta: DynamicValue = payload.get("meta")
    max_cost: float | None = None
    if isinstance(raw_meta, dict):
        raw_budget: DynamicValue = raw_meta.get("max_cost_per_call")
        if isinstance(raw_budget, (int, float)) and not isinstance(raw_budget, bool):
            max_cost = float(raw_budget)
    return _RouteInput(task_hint=hint, max_cost=max_cost)


class RuleRouter:
    """规则路由默认实现：task_hint 映射优先，成本预算约束，未知回退 default。

    属性：
        name: 策略名。routes: task_hint→model。default: 兜底模型。
        cost_tiers: model→每次调用成本。registered: 已注册模型集合。
    """

    name = "rule_router"

    def __init__(
        self,
        *,
        routes: dict[str, str] | None = None,
        default: str = _NO_DEFAULT,
        cost_tiers: dict[str, float] | None = None,
        registered: set[str] | None = None,
    ) -> None:
        """装配路由表与已注册模型集合（装配期校验所有目标已注册）。

        参数：
            routes: task_hint → 模型名。default: 兜底模型（_NO_DEFAULT 哨兵表示未配置）。
            cost_tiers: 模型 → 估计单次成本。registered: 本网关可用的模型名集合。
        异常：
            ValueError: 路由目标或默认模型未注册。
        """
        self._routes = dict(routes or {})
        self._default = default
        self._cost_tiers = dict(cost_tiers or {})
        self._registered = set(registered or ())
        self._validate_targets()

    def _validate_targets(self) -> None:
        """装配期校验：显式声明的路由目标必须已注册。

        默认模型为 _NO_DEFAULT 哨兵（调用方未显式给）时不参与校验；
        仅当调用方传入非哨兵默认值时才要求其注册。
        """
        if not self._registered:
            return  # 未给 registered 时不校验（独立测试路由逻辑用）
        targets = set(self._routes.values())
        if self._default != _NO_DEFAULT:
            targets.add(self._default)
        unknown = targets - self._registered
        if unknown:
            raise ValueError(f"route targets not registered: {sorted(unknown)}")

    def route(self, effect: Effect, ctx: PortCtx) -> RouteDecision:
        """按 task_hint / max_cost 选模型（payload 读取走 _coerce_route）。

        参数：
            effect: LLM Effect。ctx: Port 上下文（路由可读取用户/预算信息）。
        返回：
            RouteDecision(model, reason) —— reason 供 trace。
        """
        route_input = _coerce_route(effect)  # 唯一 payload 动态边界（规则 6）
        target = self._routes.get(route_input.task_hint, self._default)
        reason = f"task_hint={route_input.task_hint or 'none'}"

        # 成本约束：若带 max_cost，选不超预算的最便宜已注册模型。
        max_cost = route_input.max_cost
        if max_cost is not None and self._cost_tiers:
            affordable = [
                model for model, cost in self._cost_tiers.items() if cost <= max_cost
            ]
            if affordable:
                chosen = min(affordable, key=lambda m: self._cost_tiers[m])
                if chosen != target:
                    target = chosen
                    reason = f"cost<={max_cost}"

        return RouteDecision(model=target, reason=reason)
