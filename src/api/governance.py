"""R11 治理四件：killswitch / 成本限额 / 灰度 / feature flag（真实状态机，非桩）。

治理只做判定与限额，不实现任何能力；契约见 R11 §5/§6。
"""
from __future__ import annotations

import hashlib

__all__ = ["KillSwitch", "CostLimiter", "Rollout", "FeatureFlags", "Governance"]


class KillSwitch:
    """全局熔断：enable 后所有 allow 拒绝；disable 恢复。"""
    def __init__(self) -> None:
        """初始放行。"""
        self._reason: str | None = None

    @property
    def enabled(self) -> bool:
        """是否已熔断。"""
        return self._reason is not None

    def enable(self, reason: str = "maintenance") -> None:
        """开启熔断（记原因供审计）。"""
        self._reason = reason

    def disable(self) -> None:
        """关闭熔断。"""
        self._reason = None

    def allow(self, action: str = "") -> bool:  # purity-exempt: action 为审计预留参数，熔断判定与动作无关
        """是否放行（熔断时一律拒绝）。"""
        return self._reason is None


class CostLimiter:
    """成本限额：累计 charge 不得超 cap（fail-closed）。"""
    def __init__(self, cap: float) -> None:
        """构造限额器。

        Args:
            cap: 成本上限（>0 由调用方保证，<=0 时一律拒绝）。
        """
        self.cap = cap
        self._used = 0.0

    def charge(self, amount: float) -> bool:
        """尝试计费；超 cap 拒绝且不入账。"""
        if amount < 0 or self.cap <= 0:
            return False
        if self._used + amount > self.cap + 1e-9:
            return False
        self._used += amount
        return True

    def remaining(self) -> float:
        """剩余额度（下限 0）。"""
        return max(0.0, self.cap - self._used)


class Rollout:
    """灰度：按 key 哈希稳定分桶（墙钟不介入，确定性）。"""
    def __init__(self, percent: int) -> None:
        """构造灰度器。

        Args:
            percent: 0~100 的放量比例。
        """
        self.percent = max(0, min(100, percent))

    def enabled(self, key: str) -> bool:
        """key 是否命中放量（同一 key 恒定结果）。"""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 100
        return bucket < self.percent


class FeatureFlags:
    """功能开关：具名布尔状态（默认关）。"""
    def __init__(self) -> None:
        """初始无开关。"""
        self._flags: dict[str, bool] = {}

    def set(self, name: str, value: bool) -> None:
        """设置开关。"""
        self._flags[name] = value

    def is_on(self, name: str) -> bool:
        """是否开启（未设置默认关）。"""
        return self._flags.get(name, False)


class Governance:
    """治理门面：聚合四件，统一 allow 判定。"""
    def __init__(self, *, cost_cap: float = float("inf"),
                 rollout_percent: int = 100) -> None:
        """构造治理门面。"""
        self.killswitch = KillSwitch()
        self.cost = CostLimiter(cost_cap)
        self.rollout = Rollout(rollout_percent)
        self.flags = FeatureFlags()

    def allow(self, action: str, *, cost: float = 0.0, key: str = "") -> bool:
        """统一判定：熔断/灰度/成本任一拒绝即拒绝。"""
        if not self.killswitch.allow(action):
            return False
        if key and not self.rollout.enabled(key):
            return False
        if cost <= 0:
            return True
        return self.cost.charge(cost)
