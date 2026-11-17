"""R3 可靠性共享类型：失败分类表 / 维度键 / 退避算法 / 降级阶梯 / 熔断状态。

职责（R3 §4/§5.5）：
- 失败分类是重试与熔断的共享设施：一次分类同时给出 retryable 与 count_toward_breaker，
  避免重试插件与熔断插件各自解释错误码导致语义漂移。
- 退避算法为纯函数（随机源显式注入），墙钟/随机不隐式进入，保证可测试、可回放。
- 降级阶梯装配期强校验（无环、末级必须拒答、quality 严格递减），防止编造与死循环。

分层纪律：只依赖 R1（runtime）与 R2（hooks），不 import 任何 IO SDK（编码规则 6）。
公共 API 见 docs/02-模块详设/Agent/R3-可靠性.md §4。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
from random import Random
from typing import Protocol, runtime_checkable

from hooks import RetrySpec
from runtime import Result

__all__ = [
    "DEFAULT_FAILURE_TABLE",
    "BreakerState",
    "BreakerStatus",
    "DegradeLadder",
    "DegradeMode",
    "DegradeStep",
    "DimensionKey",
    "FailureClass",
    "FailureClassifier",
    "Jitter",
    "backoff_delay",
    "classify_failure",
    "normalize_code",
    "validate_ladder",
    "validate_retry_spec",
]


class Jitter(str, Enum):
    """退避抖动策略（有限取值集合，规则 7）。

    成员：
        NONE: 无抖动（base_delay=0 时禁用，防重试风暴）。
        FULL: uniform(0, raw)，最常用。
        EQUAL: raw/2 + uniform(0, raw/2)。
        DECORRELATED: min(max, prev × uniform(1,3))（AWS 推荐）。
    """

    NONE = "none"
    FULL = "full"
    EQUAL = "equal"
    DECORRELATED = "decorrelated"

    @classmethod
    def coerce(cls, raw: Jitter | str) -> Jitter:
        """把字符串归一为枚举成员；未知值显式回退到 FULL（规则 7）。

        参数：
            raw: 枚举成员或等价字符串（跨层 RetrySpec.jitter 为 str）。
        返回：
            Jitter 成员；无法识别时 FULL（安全防风暴）。
        """
        if isinstance(raw, cls):
            return raw
        try:
            return cls(raw)
        except ValueError:
            return cls.FULL


# --------------------------------------------------------------------------- #
# 失败分类：共享设施，重试与熔断共用一次分类结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FailureClass:
    """一次失败的分类结论。

    字段：
        retryable: 是否可重试（瞬态错误为真）。
        count_toward_breaker: 是否计入熔断滑窗（对方正常保护如 429 不计）。
        suggested_delay: 服务端建议的延迟秒数（Retry-After），None 表示无建议。
        code: 归一化后的失败码。
    """

    retryable: bool
    count_toward_breaker: bool
    suggested_delay: float | None
    code: str


type _Triple = tuple[bool, bool, str]
"""失败规则三元组：(retryable, count_toward_breaker, 归一化码)。"""

# 默认失败分类表（共享给 retry / breaker）：归一化族 → 语义三元组。
_DEFAULT_RULES: Mapping[str, _Triple] = {
    # 对方限流：重试但不算熔断（是正常保护，不是对方宕机）
    "RATE_LIMITED": (True, False, "RATE_LIMITED"),
    # 服务端错误 / 超时 / 连接重置：重试且计入熔断
    "SERVER_ERROR": (True, True, "SERVER_ERROR"),
    "TIMEOUT": (True, True, "TIMEOUT"),
    "CONN_RESET": (True, True, "CONN_RESET"),
    # 确定性错误：不重试、不计熔断
    "BAD_REQUEST": (False, False, "BAD_REQUEST"),
    "UNAUTHORIZED": (False, False, "UNAUTHORIZED"),
    "FORBIDDEN": (False, False, "FORBIDDEN"),
    "PORT_MISSING": (False, False, "PORT_MISSING"),
    # schema 非法：重试（可能模型偶发），但不算熔断
    "SCHEMA_INVALID": (True, False, "SCHEMA_INVALID"),
}

# HTTP/符号码 → 归一化族。字符串前缀匹配，覆盖 5xx 段。
_CODE_ALIASES: Mapping[str, str] = {
    "429": "RATE_LIMITED",
    "RATE_LIMITED": "RATE_LIMITED",
    "TOO_MANY_REQUESTS": "RATE_LIMITED",
    "500": "SERVER_ERROR",
    "502": "SERVER_ERROR",
    "503": "SERVER_ERROR",
    "SERVER_ERROR": "SERVER_ERROR",
    "SERVICE_UNAVAILABLE": "SERVER_ERROR",
    "INTERNAL_ERROR": "SERVER_ERROR",
    "TIMEOUT": "TIMEOUT",
    "DEADLINE_EXCEEDED": "TIMEOUT",
    "CONN_RESET": "CONN_RESET",
    "CONNECTION_RESET": "CONN_RESET",
    "400": "BAD_REQUEST",
    "401": "UNAUTHORIZED",
    "403": "FORBIDDEN",
    "PORT_MISSING": "PORT_MISSING",
    "SCHEMA_INVALID": "SCHEMA_INVALID",
    "SCHEMA_VALIDATION": "SCHEMA_INVALID",
}

# 成功/未知的默认分类（成功不可重试；未知由分类兜底，尊重 port 显式标记）。
_SUCCESS_CLASS = FailureClass(False, False, None, "OK")
_UNKNOWN_CLASS = FailureClass(False, False, None, "UNKNOWN")

# 导出默认表（归一化族 → FailureClass），供校验与注入方查询覆盖完整。
DEFAULT_FAILURE_TABLE: Mapping[str, FailureClass] = {
    family: FailureClass(
        retryable=triple[0],
        count_toward_breaker=triple[1],
        suggested_delay=None,
        code=triple[2],
    )
    for family, triple in _DEFAULT_RULES.items()
}

_HTTP_5XX = re.compile(r"^5\d\d$")


def normalize_code(raw: str) -> str:
    """把错误码归一到语义族（HTTP 码与符号码等价）。

    参数：
        raw: 原始错误码（如 "429" / "SERVICE_UNAVAILABLE"）。
    返回：
        归一化族名；无法识别时原样返回（规则 7：未知值不静默改写语义，由分类兜底）。
    """
    upper = raw.strip().upper()
    if upper in _CODE_ALIASES:
        return _CODE_ALIASES[upper]
    if _HTTP_5XX.match(upper):
        return "SERVER_ERROR"
    return raw


@runtime_checkable
class FailureClassifier(Protocol):
    """可注入的失败分类策略（可插拔契约：默认表 / 自定义实现）。

    属性：
        name: 策略名（一切可插拔物强制留名）。
    """

    name: str

    def classify(self, result: Result, ctx: object) -> FailureClass:
        """对一次 Port 结果分类；异常由调用方折叠，不抛穿。"""
        ...


class _DefaultClassifier:
    """内置默认分类器（基于静态规则表，无状态）。"""

    name = "default_failure"

    def classify(self, result: Result, ctx: object) -> FailureClass:
        """成功→终态；失败→查表；未知码按确定性错误（不重试、不计熔断）。"""
        if result.ok:
            return _SUCCESS_CLASS
        error = result.error
        if error is None:  # pragma: no cover - Result.failure 必带 error，防御性兜底
            return _UNKNOWN_CLASS
        family = normalize_code(error.code)
        triple = _DEFAULT_RULES.get(family)
        if triple is None:
            # 尊重 PortError.retryable 显式标记，但保守地不计熔断（未知码不误伤熔断统计）。
            return FailureClass(error.retryable, False, None, family or "UNKNOWN")
        return FailureClass(triple[0], triple[1], None, family)


def classify_failure(
    result: Result, ctx: object, *, classifier: FailureClassifier | None = None
) -> FailureClass:
    """统一分类入口：注入自定义分类器则委托，否则用默认表。

    参数：
        result: 待分类的 Port 结果。ctx: HookCtx（分类器可选读取）。
        classifier: 自定义分类器；None 用内置默认。
    返回：
        FailureClass 分类结论。
    """
    active = classifier or _DefaultClassifier()
    return active.classify(result, ctx)


# --------------------------------------------------------------------------- #
# 维度键：按 provider/model/tool/tenant 隔离熔断/限流计数
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DimensionKey:
    """隔离计数维度（如 ("provider", "openai")）。

    字段：
        dim_type: 维度类型（provider/model/tool/tenant）。
        name: 目标名。
    """

    dim_type: str
    name: str

    def as_tuple(self) -> tuple[str, str]:
        """转为二元组表示（便于做 store key）。"""
        return (self.dim_type, self.name)


# --------------------------------------------------------------------------- #
# 退避算法：纯函数，随机源显式注入（墙钟/随机不隐式进入）
# --------------------------------------------------------------------------- #
def validate_retry_spec(spec: RetrySpec) -> None:
    """装配期校验重试参数完整性。

    校验：
        base_delay=0 且 jitter=none → 拒绝（无退避的重试会形成风暴）。
        max_attempts<1 / max_delay<0 / multiplier<1 → 拒绝。
    """
    if spec.max_attempts < 1:
        raise ValueError("RetrySpec.max_attempts must be >= 1")
    if spec.max_delay < 0:
        raise ValueError("RetrySpec.max_delay must be >= 0")
    if spec.multiplier < 1:
        raise ValueError("RetrySpec.multiplier must be >= 1")
    if spec.base_delay == 0 and Jitter.coerce(spec.jitter) is Jitter.NONE:
        raise ValueError("base_delay=0 with jitter=none would cause a retry storm")


def backoff_delay(
    attempt: int,
    spec: RetrySpec,
    rng: Random,
    *,
    prev_delay: float = 0.0,
    retry_after: float | None = None,
) -> float:
    """计算第 attempt 次重试的退避秒数（attempt 从 1 起）。

    算法（R3 §5.5）：
        raw = min(max_delay, base_delay × multiplier^(attempt-1))
        none         → raw
        full         → uniform(0, raw)                       （最常用，防重试风暴）
        equal        → raw/2 + uniform(0, raw/2)
        decorrelated → min(max_delay, prev × uniform(1,3))   （prev=0 时回退到 base_delay）
        若给 Retry-After：取 max(结果, 服务端提示)。

    参数：
        attempt: 当前重试序号（≥1）。spec: 重试参数。rng: 注入的随机源。
        prev_delay: 上一次退避值（decorrelated 用）。retry_after: 服务端建议延迟。
    返回：
        本次应等待的秒数（≥0）。
    """
    raw = min(spec.max_delay, spec.base_delay * (spec.multiplier ** max(0, attempt - 1)))
    jitter = Jitter.coerce(spec.jitter)  # 未知值显式回退 FULL（规则 7）
    if jitter is Jitter.NONE:
        delay = raw
    elif jitter is Jitter.FULL:
        delay = rng.uniform(0.0, raw)
    elif jitter is Jitter.EQUAL:
        half = raw / 2
        delay = half + rng.uniform(0.0, half)
    else:  # Jitter.DECORRELATED
        base = prev_delay if prev_delay > 0 else spec.base_delay
        delay = min(spec.max_delay, base * rng.uniform(1.0, 3.0))
    if spec.respect_retry_after and retry_after is not None:
        delay = max(delay, retry_after)
    return max(0.0, delay)


# --------------------------------------------------------------------------- #
# 降级阶梯：无环 + 末级拒答 + quality 递减
# --------------------------------------------------------------------------- #
class DegradeMode(str, Enum):
    """降级方式（有限取值集合，规则 7）。"""

    REROUTE = "reroute"                   # 换备用模型/port
    CACHE = "cache"                       # 用语义缓存答案
    FALLBACK_VALUE = "fallback_value"     # 用静态兜底值
    REFUSE = "refuse"                     # 明确拒答（禁止编造）


@dataclass(frozen=True, slots=True)
class DegradeStep:
    """降级阶梯的一级。

    字段：
        target: 目标名（模型/port/缓存键；拒答级为空）。
        mode: 降级方式。
        quality: 质量分（1.0 最优 → 0.0 拒答），进 trace 供评测。
        max_latency_ms: 可选延迟预算。condition: 可选触发条件表达式。
    """

    target: str
    mode: DegradeMode
    quality: float
    max_latency_ms: int | None = None
    condition: str | None = None


@dataclass(frozen=True, slots=True)
class DegradeLadder:
    """降级阶梯（主→备→便宜→缓存→拒答）。

    字段：
        steps: 有序步骤元组，quality 严格递减，末级必须 REFUSE。
    """

    steps: tuple[DegradeStep, ...]

    def pick(self, available: AbstractSet[str]) -> DegradeStep:
        """从可用目标集合里选 quality 最高的一级；无可用则落到拒答级。

        参数：
            available: 当前可用的目标名集合。
        返回：
            选定的 DegradeStep（REROUTE/CACHE 仅当 target 在 available；REFUSE 总可用）。
        """
        candidates = [
            step for step in self.steps
            if step.mode is DegradeMode.REFUSE
            or (step.target and step.target in available)
        ]
        if not candidates:
            # 防御：合法阶梯必有拒答级；若被绕过校验，返回一个临时拒答级（不编造）。
            return DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0)
        return max(candidates, key=lambda step: step.quality)


def validate_ladder(ladder: DegradeLadder) -> None:
    """装配期强校验降级阶梯（R3 红线）。

    校验：
        steps 非空；末级必须 REFUSE；非拒答级 target 不重复（无环）；quality 严格递减。
    """
    steps = ladder.steps
    if not steps:
        raise ValueError("degrade ladder must not be empty")
    if steps[-1].mode is not DegradeMode.REFUSE:
        raise ValueError("degrade ladder must end with a REFUSE step (no fabrication)")
    targets = [step.target for step in steps if step.mode is not DegradeMode.REFUSE]
    if len(set(targets)) != len(targets):
        raise ValueError("degrade ladder contains a cycle (duplicate target)")
    for prev, nxt in zip(steps, steps[1:]):
        if nxt.quality >= prev.quality:
            raise ValueError(
                f"degrade ladder quality must strictly decrease: "
                f"{prev.quality} -> {nxt.quality}"
            )


# --------------------------------------------------------------------------- #
# 熔断状态：三态机的不可变快照
# --------------------------------------------------------------------------- #
class BreakerStatus(str, Enum):
    """熔断器三态（有限取值，规则 7）。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class BreakerState:
    """熔断器在某维度上的不可变状态快照。

    字段：
        status: 当前三态之一（字符串值，与 BreakerStatus 对齐）。
        failures: 累计失败数（用于样本量判定）。
        window: 滑窗（True=失败, False=成功），计算失败率。
        half_open_successes: 半开态连续成功次数。
        opened_at: 进入 open 的时钟值（注入 clock 的返回类型；秒或步号），None 表示非 open。
    """

    status: str = BreakerStatus.CLOSED.value
    failures: int = 0
    window: tuple[bool, ...] = field(default_factory=tuple)
    half_open_successes: int = 0
    opened_at: float | None = None

    def failure_rate(self) -> float:
        """滑窗失败率；空窗返回 0。"""
        if not self.window:
            return 0.0
        return sum(1 for ok_flag in self.window if ok_flag) / len(self.window)
