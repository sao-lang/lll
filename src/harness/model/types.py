"""R4 模型与推理共享类型：结构化规格 / 缓存键 / 路由决策 / 成本归因账本。

全部只依赖 R1（runtime）与 R2（hooks, 复用其 LLMRequest/LLMResponse/Usage DTO），
墙钟/随机不隐式进入；成本与 token 数据来自 LLMPort 响应，不自行估算。
公共 API 见 docs/02-模块详设/Agent/R4-模型与推理.md §4。
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from compose import LLMPort, LLMRequest, Usage
from runtime import DynamicValue, Result

__all__ = [
    "BackendResolution",
    "CacheKey",
    "CostAttributor",
    "CostGuard",
    "CostRecord",
    "RefuseExecution",
    "Reservation",
    "RouteDecision",
    "StructuredOutputSpec",
    "TargetUnresolved",
    "UsageLedger",
    "build_cache_key",
    "cache_key_string",
]


# ---- R4 §2.2 / §5.4 降级解析三态（唯一决策点返回值，不回退默认模型）----
# 稳定错误码集中为模块常量（规则 5 禁魔法串），R4-T09/T10 断言这两个 code。
REFUSED_CODE = "REFUSED"
TARGET_UNRESOLVED_CODE = "TARGET_UNRESOLVED"


@dataclass(frozen=True, slots=True)
class RefuseExecution:
    """降级末级拒答：产出 REFUSED 失败，绝不调用任何 LLMPort。"""

    reason: str

    def to_result(self) -> Result:
        """折叠为稳定失败信封（ok=False，retryable=False）。"""
        return Result.failure(REFUSED_CODE, self.reason)


@dataclass(frozen=True, slots=True)
class TargetUnresolved:
    """降级目标非空但未注册：显式失败，绝不静默回退默认模型。"""

    target: str

    def to_result(self) -> Result:
        """折叠为稳定失败信封（带未解析 target 便于诊断）。"""
        return Result.failure(
            TARGET_UNRESOLVED_CODE, f"degrade target not registered: {self.target}"
        )


# 三态联合：LLMPort（切后端）/ RefuseExecution（拒答）/ TargetUnresolved（显式失败）。
type BackendResolution = LLMPort | RefuseExecution | TargetUnresolved


class StructuredOutputSpec(BaseModel):
    """结构化输出规格（R4 §4）：schema + 校验/修复策略。

    字段：
        schema: JSON Schema（dict，描述期望输出结构）。
        mode: constrained（模型侧约束解码）/ prompt（仅提示）/ repair（校验失败后修复重试）。
        max_repair: 最大修复重试次数（仅 repair 模式生效）。
        strict: 严格模式——未过 schema 的输出绝不标记为合法（红线）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # 对外契约字段名为 "schema"（R4 §4）；内部属性用别名 json_schema，
    # 避免遮蔽 BaseModel.schema() 方法的告警。populate_by_name 让 schema=/json_schema= 均可构造。
    # schema 形态由外部 JSON Schema 决定（规则 3 动态边界），用 DynamicValue 具名别名。
    json_schema: dict[str, DynamicValue] = Field(
        default_factory=dict, alias="schema", description="JSON Schema"
    )
    mode: Literal["constrained", "prompt", "repair"] = Field(
        default="repair", description="Structured output mode"
    )
    max_repair: int = Field(default=2, ge=0, description="Maximum repair retries")
    strict: bool = Field(default=True, description="Strict mode: invalid output is rejected")


@dataclass(frozen=True, slots=True)
class CacheKey:
    """缓存键快照：value 为规范化哈希，version 为 prompt 版本。"""

    value: str
    version: str = "current"


def cache_key_string(request: LLMRequest, *, prompt_version: str = "current") -> str:
    """构造缓存键字符串：hash(messages + model + params + prompt_version)。

    参数：
        request: LLM 请求（messages/model/temperature/max_tokens 参与键）。
        prompt_version: prompt 模板版本（变化即失效）。
    返回：
        确定性哈希字符串（stable JSON + sha256 前 32 位）。
    """
    import hashlib

    payload = {
        "messages": [dict(m) for m in request.messages],
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "prompt_version": prompt_version,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_cache_key(request: LLMRequest, *, prompt_version: str = "current") -> CacheKey:
    """构造 CacheKey 值对象（键含版本，供缓存隔离）。"""
    return CacheKey(value=cache_key_string(request, prompt_version=prompt_version), version=prompt_version)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """路由决策：选中的模型 + 进 trace 的理由。"""

    model: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CostRecord:
    """一次模型调用的成本记录（多维归因载体）。

    字段：
        run_id / agent_id / user_id / model: 归因维度。
        usage: 来自 LLMPort 响应的 token/成本元数据。
    """

    run_id: str
    agent_id: str
    user_id: str
    model: str
    usage: Usage


@dataclass
class UsageLedger:
    """成本/token 账本：按多维累计（per-run/agent/user/model）。"""

    _cost: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    _total_cost: float = 0.0
    _tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _count: int = 0

    def record(self, rec: CostRecord) -> None:
        """记录一条成本（各维度同步累计）。"""
        cost = float(rec.usage.cost_usd)
        tokens = int(rec.usage.prompt_tokens) + int(rec.usage.completion_tokens)
        self._total_cost = round(self._total_cost + cost, 10)
        self._count += 1
        for dim, value in (
            ("run_id", rec.run_id), ("agent_id", rec.agent_id),
            ("user_id", rec.user_id), ("model", rec.model),
        ):
            self._cost[f"{dim}:{value}"] = round(self._cost[f"{dim}:{value}"] + cost, 10)
            self._tokens[f"{dim}:{value}"] += tokens

    def total(self) -> float:
        """累计总成本。"""
        return self._total_cost

    def by(self, dim: str, value: str) -> float:
        """按维度取成本（如 by("model", "m1")）。"""
        return self._cost.get(f"{dim}:{value}", 0.0)

    def tokens_by(self, dim: str, value: str) -> int:
        """按维度取 token 数。"""
        return self._tokens.get(f"{dim}:{value}", 0)

    @property
    def count(self) -> int:
        """记录条数。"""
        return self._count


class CostAttributor(UsageLedger):
    """成本归因器：UsageLedger 的语义化子类，提供具名维度快捷查询。"""

    def run_cost(self, run_id: str) -> float:
        """某 run 累计成本。"""
        return self.by("run_id", run_id)

    def user_cost(self, user_id: str) -> float:
        """某 user 累计成本。"""
        return self.by("user_id", user_id)

    def model_cost(self, model: str) -> float:
        """某 model 累计成本。"""
        return self.by("model", model)

    def snapshot(self) -> dict[str, float | int]:
        """返回成本快照（总额 + 记录数），供 trace/评测。"""
        return {"total": self.total(), "records": self.count}


@dataclass(frozen=True, slots=True)
class Reservation:
    """成本预留凭证（P2b）：调用前 reserve 得到，调用后 commit/cancel 结清。

    字段：
        id: 预留唯一标识（防重复结清）。amount: 预留金额（预估成本）。
    """

    id: int
    amount: float


@dataclass
class CostGuard:
    """成本硬闸：调用前 reserve 预留（硬闸），调用后 commit 按实际结算（P2b）。

    并发下"先调用后扣费"会让硬闸失效（预算 0.5、20 并发全通过）；reserve 在读改写
    原子操作里立即占用额度，超预算的调用在发起前就被拒。allow/charge 保留供
    简单场景与向后兼容，但生产链路（gateway）走 reserve/commit。
    """

    max_cost_usd: float
    spent: float = 0.0
    _reserved: float = field(default=0.0, repr=False)
    _seq: int = field(default=0, repr=False)
    _active: dict[int, float] = field(default_factory=dict, repr=False)

    def allow(self, estimated_cost: float) -> bool:
        """（旧 API）预计成本加上已花是否仍在预算内；不含未结清预留。"""
        return round(self.spent + self._reserved + estimated_cost, 10) <= self.max_cost_usd

    def charge(self, cost: float) -> None:
        """（旧 API）扣减预算（调用后按实际成本）。"""
        self.spent = round(self.spent + cost, 10)

    def reserve(self, estimated_cost: float) -> Reservation | None:
        """调用前预留额度：原子检查并占用，超预算返回 None（硬闸拒绝）。

        参数：
            estimated_cost: 预估成本（>=0；负数视为 0）。
        返回：
            Reservation 凭证（供 commit/cancel）；预算不足返回 None。
        """
        amount = max(0.0, estimated_cost)
        if not self.allow(amount):
            return None
        self._seq += 1
        self._reserved = round(self._reserved + amount, 10)
        self._active[self._seq] = amount
        return Reservation(id=self._seq, amount=amount)

    def commit(self, reservation: Reservation, actual: float) -> None:
        """调用后结清：释放预留、按实际成本扣减（差额自动退还/补扣）。

        未登记的凭证（重复/伪造）被忽略，防止重复扣减。

        参数：
            reservation: reserve 返回的凭证。actual: 实际成本。
        """
        if reservation.id not in self._active:
            return
        del self._active[reservation.id]
        self._reserved = round(self._reserved - reservation.amount, 10)
        self.spent = round(self.spent + max(0.0, actual), 10)

    def cancel(self, reservation: Reservation) -> None:
        """调用失败取消：释放预留且不记支出。"""
        if reservation.id not in self._active:
            return
        del self._active[reservation.id]
        self._reserved = round(self._reserved - reservation.amount, 10)

    def remaining(self) -> float:
        """剩余可用预算（扣除已花与未结清预留）。"""
        return round(self.max_cost_usd - self.spent - self._reserved, 10)
