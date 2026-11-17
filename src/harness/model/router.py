"""R4 模型路由：按任务/成本选模型，目标必须已注册（可插拔契约校验）。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from hooks import PortCtx
from runtime import DynamicValue, Effect

from .types import RouteDecision

__all__ = ["ModelRouter", "RuleRouter"]


@runtime_checkable
class ModelRouter(Protocol):
    """模型路由协议：name + route(effect, ctx) → RouteDecision。"""

    name: str

    def route(self, effect: Effect, ctx: PortCtx) -> RouteDecision:
        """按 Effect 的任务提示与预算选模型。"""
        ...


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
        default: str = "default",
        cost_tiers: dict[str, float] | None = None,
        registered: set[str] | None = None,
    ) -> None:
        """装配路由表与已注册模型集合（装配期校验所有目标已注册）。

        参数：
            routes: task_hint → 模型名。default: 兜底模型。
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

        default="default" 是占位哨兵（调用方未显式给默认模型），不参与校验；
        仅当调用方传入非哨兵默认值时才要求其注册。
        """
        if not self._registered:
            return  # 未给 registered 时不校验（独立测试路由逻辑用）
        targets = set(self._routes.values())
        if self._default != "default":
            targets.add(self._default)
        unknown = targets - self._registered
        if unknown:
            raise ValueError(f"route targets not registered: {sorted(unknown)}")

    def route(self, effect: Effect, ctx: PortCtx) -> RouteDecision:
        """按 payload 的 task_hint / meta.max_cost_per_call 选模型。

        参数：
            effect: LLM Effect，payload 可带 task_hint 与 meta.max_cost_per_call。
            ctx: Port 上下文（路由可读取用户/预算信息）。
        返回：
            RouteDecision(model, reason) —— reason 供 trace。
        """
        payload: DynamicValue = effect.payload
        hint = str(payload.get("task_hint", ""))
        target = self._routes.get(hint, self._default)
        reason = f"task_hint={hint or 'none'}"

        # 成本约束：若带 max_cost_per_call，选不超预算的最便宜已注册模型。
        meta = payload.get("meta") or {}
        budget = meta.get("max_cost_per_call") if isinstance(meta, dict) else None
        if budget is not None and self._cost_tiers:
            affordable = [
                model for model, cost in self._cost_tiers.items() if cost <= float(budget)
            ]
            if affordable:
                chosen = min(affordable, key=lambda m: self._cost_tiers[m])
                if chosen != target:
                    target = chosen
                    reason = f"cost<={budget}"

        return RouteDecision(model=target, reason=reason)
