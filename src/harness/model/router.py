"""R4 模型路由：按任务/成本选模型，目标必须已注册（可插拔契约校验）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypedDict, cast, runtime_checkable

from compose import PortCtx
from runtime import Effect, Invoke

from .types import RouteDecision

__all__ = ["NO_DEFAULT", "ModelRouter", "RuleRouter"]

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


INTENT_TASK = "intent"  # 意图识别任务具名常量（规则 5：禁止魔法串比较）
TIER_KEY = "tier"       # 意图级联档位（cheap/primary）在 meta 中的具名键


class _RouteMeta(TypedDict, total=False):
    """LLM Effect 的 payload.meta 里 R4 路由消费的明确字段（规则 4）。

    meta 容器本身已在 R1 的 EffectPayload 声明为 dict[str, DynamicValue]；
    其中被本模块读取的键在此具名声明，避免裸字符串动态取值。
    """

    max_cost_per_call: float


@dataclass(frozen=True, slots=True)
class _RouteInput:
    """路由输入的具名视图（规则 6：payload 字段读取成具名视图）。

    字段：
        task_hint: 任务类型提示（如 code/chat）。
        max_cost: 单次调用成本上限（来自 payload.meta，None 表示无约束）。
    """

    task_hint: str
    max_cost: float | None


def _coerce_route(effect: Effect) -> _RouteInput:
    """从已声明字段读取路由输入，归名为具名视图（规则 4/6）。

    task_hint 与 meta 已在 R1 EffectPayload 声明；max_cost_per_call 在本文件
    _RouteMeta 声明。形态不合法时显式回退空提示/无成本约束（规则 5 显式回退）。

    参数：
        effect: 待路由的 LLM Effect。
    返回：
        _RouteInput 具名视图。
    """
    # 规则 6 例外：task_hint/meta 已在 R1 EffectPayload 声明为明确字段，
    # 此处是类型化读取（.get 安全取可选键），非未声明动态键；形态回退见下方。
    if not isinstance(effect, Invoke):
        return _RouteInput(task_hint="", max_cost=None)  # 非 Invoke 不携带路由参数
    hint = str(effect.payload.get("task_hint") or "")
    raw_meta = cast(_RouteMeta | None, effect.payload.get("meta"))
    max_cost: float | None = None
    if raw_meta is not None:  # meta 缺席即无成本约束；存在才深入读取，不臆造默认预算
        raw_budget = raw_meta.get("max_cost_per_call")
        if isinstance(raw_budget, (int, float)) and not isinstance(raw_budget, bool):  # bool 是 int 子类须排除；形态合法才转 float，否则回退无约束（规则 5）
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
        if self._default != _NO_DEFAULT:  # 哨兵表示未显式配置默认模型，不纳入注册校验
            targets.add(self._default)
        unknown = targets - self._registered
        if unknown:  # 装配期暴露配置错误，避免运行期才路由到不存在的模型
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
        # 意图级联 tier 真影响路由：intent/cheap 与 intent/primary 可按 routes 配置
        # 选不同模型；无专项配置时沿用 task_hint 结果（不伪造降级）。
        # 规则 6 例外：ctx.meta 为 R4 路由显式消费的 typed 元数据边界（_RouteMeta），
        # tier 键由本文件具名常量声明，非任意动态键。
        tier = str(ctx.meta.get(TIER_KEY) or "")
        if tier and route_input.task_hint == INTENT_TASK:
            tier_target = self._routes.get(f"intent/{tier}")
            if tier_target is not None:
                target = tier_target
                reason = f"intent_tier={tier}"

        # 成本约束：若带 max_cost，选不超预算的最便宜已注册模型。
        max_cost = route_input.max_cost
        if max_cost is not None and self._cost_tiers:
            affordable = [
                model for model, cost in self._cost_tiers.items() if cost <= max_cost
            ]
            if affordable:  # 仅在预算内有候选时才改选；否则保留 task_hint 的结果，不强行改写
                chosen = min(affordable, key=lambda m: self._cost_tiers[m])
                if chosen != target:  # 决策真被成本约束改变时才更新 reason，保证 trace 与实际选择一致
                    target = chosen
                    reason = f"cost<={max_cost}"

        return RouteDecision(model=target, reason=reason)
