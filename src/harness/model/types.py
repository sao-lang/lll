"""R4 模型与推理共享类型：结构化规格 / 缓存键 / 路由决策 / 成本归因账本。

全部只依赖 R1（runtime）与 R2（hooks, 复用其 LLMRequest/LLMResponse/Usage DTO），
墙钟/随机不隐式进入；成本与 token 数据来自 LLMPort 响应，不自行估算。
公共 API 见 docs/02-模块详设/Agent/R4-模型与推理.md §4。
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from hooks import LLMRequest, Usage
from runtime import DynamicValue

__all__ = [
    "CacheKey",
    "ChatMessage",
    "CostAttributor",
    "CostGuard",
    "CostRecord",
    "RouteDecision",
    "StructuredOutputSpec",
    "UsageLedger",
    "build_cache_key",
    "cache_key_string",
]


class ChatMessage(TypedDict):
    """LLM 对话消息的具名结构（规则 3：明确字段不写动态 dict）。

    字段：
        role: 角色（system/user/assistant/tool）。
        content: 正文。
    """

    role: str
    content: str


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
    # schema 形态由外部 JSON Schema 决定（规则 4 动态边界），用 DynamicValue 具名别名。
    json_schema: dict[str, DynamicValue] = Field(
        default_factory=dict, alias="schema", description="JSON Schema"
    )
    mode: Literal["constrained", "prompt", "repair"] = Field(
        default="repair", description="结构化输出模式"
    )
    max_repair: int = Field(default=2, ge=0, description="最大修复重试次数")
    strict: bool = Field(default=True, description="严格模式：非法输出不进业务")


@dataclass(frozen=True, slots=True)
class CacheKey:
    """缓存键快照：value 为规范化哈希，version 为 prompt 版本。"""

    value: str
    version: str = "v1"


def cache_key_string(request: LLMRequest, *, prompt_version: str = "v1") -> str:
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


def build_cache_key(request: LLMRequest, *, prompt_version: str = "v1") -> CacheKey:
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


@dataclass
class CostGuard:
    """成本硬闸：调用前 allow 检查，调用后 charge 扣减；超限拒绝（红线：不得绕过 Budgets）。"""

    max_cost_usd: float
    spent: float = 0.0

    def allow(self, estimated_cost: float) -> bool:
        """预计成本加上已花是否仍在预算内。"""
        return round(self.spent + estimated_cost, 10) <= self.max_cost_usd

    def charge(self, cost: float) -> None:
        """扣减预算（调用后按实际成本）。"""
        self.spent = round(self.spent + cost, 10)

    def remaining(self) -> float:
        """剩余预算。"""
        return round(self.max_cost_usd - self.spent, 10)
