"""R7 记忆核心 DTO：双轴分类（温度三层 × 内容七类）、记忆项、召回查询与隔离域。

本模块只描述数据结构、具名常量与纯判定函数，不含存储逻辑。
secret / 注入检测在 MemoryStore 写入链执行；本模块的 is_secret_key 是第一道键名闸门。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.rag.types import TokenBudget

__all__ = [
    "MMR_LAMBDA",
    "NEAR_DUP_SIM_THRESHOLD",
    "PREFERENCE_REPEAT_THRESHOLD",
    "PROFILE_CONFIDENCE_THRESHOLD",
    "RERANK_W_CONFIDENCE",
    "RERANK_W_IMPORTANCE",
    "RERANK_W_RECENCY",
    "RERANK_W_RELEVANCE",
    "SCOPE_META_KEY",
    "SECRET_KEY_TOKENS",
    "MemoryItem",
    "MemoryKind",
    "MemoryQuery",
    "MemoryScope",
    "MemoryTier",
    "ScopeResolver",
    "is_secret_key",
    "make_item_id",
]


class MemoryTier(StrEnum):
    """温度轴：长期记忆的三个物理/检索层（均非会话内短期记忆）。

    成员：
        HOT: 近期热层，短 TTL、高频召回。
        WARM: 结构化主检索层，中 TTL。
        COLD: 全文归档层，长 TTL、低频审计。
    """

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class MemoryKind(StrEnum):
    """内容轴：长期记忆的七类语义类型（提炼阶段判定后稳定）。

    成员：
        EPISODIC: 情节，发生过什么。
        SEMANTIC: 事实，知道什么。
        PROCEDURAL: 流程，怎么做。
        PROFILE: 用户画像，稳定身份属性。
        PREFERENCE: 偏好约束，风格/格式/禁忌。
        ENTITY: 实体知识，人/项目/工具/概念。
        JOURNAL: 工作日志与决策。
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"
    PREFERENCE = "preference"
    ENTITY = "entity"
    JOURNAL = "journal"


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """记忆隔离域：tenant 必填，user/session 可选。

    由 R12 装配层经 ``PortCtx.meta["scope"]`` 注入；写入与召回双向校验。
    tenant 为空即 fail-closed（禁止静默全量可见）。
    """

    tenant: str
    user: str | None = None
    session: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """一条长期记忆条目（前五字段保留位置参数兼容旧调用方）。

    字段：
        kind: 内容轴七类之一。
        content: 正文（写入前须过递归注入检测且不含 secret）。
        importance: 重要性评分 0~1，参与召回 rerank（禁止恒定 1.0）。
        ts: 真实写入时刻；调用方未给时用当前时间（禁止硬编码 0）。
        source: 来源标识（如 ``cold:transcript`` / ``refined:<kind>``）。
        item_id: 稳定标识，由 ``make_item_id`` 生成；KV 与向量索引同源。
        confidence: 置信度 0~1，冲突消解与 rerank 依据。
        tier: 温度轴三层之一，与 kind 正交。
        scope: 隔离域，写入/召回校验。
        access_count: 命中次数，驱动晋升/降级与 importance 重算。
    """

    kind: MemoryKind
    content: str
    importance: float
    ts: int
    source: str
    item_id: str = ""
    confidence: float = 1.0
    tier: MemoryTier = MemoryTier.WARM
    # scope 缺省为 default 租户（非 None），确保跨 tenant 过滤不被 None 旁路。
    scope: MemoryScope = field(default_factory=lambda: MemoryScope(tenant="default"))
    access_count: int = 0


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """R7 自有语义召回查询 DTO（不复用 R6 Query）。

    字段：
        text: 查询文本。
        top_k: 返回条数上限。
        tier: 限定温度层（None 表示全层）。
        kind: 限定内容类（None 表示全部）。
        scope: 显式隔离域；其 tenant 必须等于 PortCtx.meta 中的 tenant，否则拒绝。
    """

    text: str
    top_k: int = 5
    tier: MemoryTier | None = None
    kind: MemoryKind | None = None
    scope: MemoryScope | None = None
    budget: TokenBudget | None = None  # 召回链预算裁剪（§12.4）：给定则按 token 贪心装入，缺省仅 top_k


# secret 键名特征（大小写不敏感子串匹配；键与值双向检测的第一道闸）。
SECRET_KEY_TOKENS: tuple[str, ...] = (
    "secret", "token", "password", "api_key", "apikey", "private_key", "credential",
)
# 冲突消解/分类阈值（具名常量，禁止裸魔法值）。
NEAR_DUP_SIM_THRESHOLD: float = 0.92
PREFERENCE_REPEAT_THRESHOLD: int = 3
PROFILE_CONFIDENCE_THRESHOLD: float = 0.9
# rerank 加权和权重（和为 1.0；连乘会把四因子 0.5 压到 0.06 失去区分度）。
RERANK_W_RELEVANCE: float = 0.5
RERANK_W_IMPORTANCE: float = 0.2
RERANK_W_RECENCY: float = 0.15
RERANK_W_CONFIDENCE: float = 0.15
# MMR 去冗余：lambda 偏向相关性，(1-lambda) 偏向多样性。
MMR_LAMBDA: float = 0.7
# PortCtx.meta 中承载隔离域的键（D-2 x-* 扩展口径，不改 R2 DTO 结构）。
SCOPE_META_KEY: str = "scope"
# 缺省 tenant（仅 demo/测试向后兼容；生产由 R12 强制注入真实 tenant）。
_DEFAULT_TENANT: str = "default"


def is_secret_key(key: str) -> bool:
    """判定键名是否疑似 secret（fail-closed 第一道闸）。

    Args:
        key: 待写入的记忆键名。

    Returns:
        命中任一 secret 特征子串返回 True（大小写不敏感），否则 False。
    """
    folded = key.casefold()
    return any(token in folded for token in SECRET_KEY_TOKENS)


def make_item_id(tenant: str, kind: MemoryKind | str, content: str) -> str:
    """由 tenant + kind + 规范化内容生成稳定 item_id。

    Args:
        tenant: 隔离域租户（参与哈希，防止跨租户同内容撞 id）。
        kind: 内容类型。
        content: 已规范化正文。

    Returns:
        sha256 前 16 位十六进制字符串作为稳定标识。
    """
    from hashlib import sha256

    normalized = " ".join(content.split()).casefold()
    payload = f"{tenant}|{kind}|{normalized}".encode("utf-8")
    return sha256(payload).hexdigest()[:16]


class ScopeResolver:
    """从 PortCtx.meta 解析隔离域（R1/R2 零改，唯一 tenant 入口）。"""

    @staticmethod
    def resolve(ctx: object) -> MemoryScope:
        """从 ``ctx.meta["scope"]`` 解析 MemoryScope，缺失降级为 default。

        生产环境由 R12 装配层强制注入真实 tenant；缺省 tenant 仅供 demo/测试
        向后兼容（旧调用方不传 meta）。fail-closed 仍适用于显式错误 scope。

        Args:
            ctx: 具备 meta 映射属性的 PortCtx（鸭子类型，避免 DTO 反向依赖 hooks）。

        Returns:
            解析出的 MemoryScope；缺失时用 tenant=DEFAULT_SCOPE_TENANT。

        Raises:
            ValueError: meta["scope"] 存在但格式非法或 tenant 为空。
        """
        meta = getattr(ctx, "meta", None)
        if not isinstance(meta, dict) or SCOPE_META_KEY not in meta:
            return MemoryScope(tenant=_DEFAULT_TENANT)
        raw = meta.get(SCOPE_META_KEY)
        if isinstance(raw, MemoryScope):
            scope = raw
        elif isinstance(raw, dict) and isinstance(raw.get("tenant"), str):
            scope = MemoryScope(
                tenant=raw["tenant"],
                user=raw.get("user"),
                session=raw.get("session"),
            )
        else:
            raise ValueError("PortCtx.meta['scope'] invalid; fail-closed")
        if not scope.tenant.strip():
            raise ValueError("empty tenant rejected")
        return scope

    @staticmethod
    def check(query_scope: MemoryScope | None, ctx_scope: MemoryScope) -> None:
        """校验显式 query scope 与 ctx scope 的 tenant 一致，防止从 Query 侧绕过。

        Args:
            query_scope: MemoryQuery 显式携带的隔离域，None 表示取 ctx 的。
            ctx_scope: ScopeResolver.resolve(ctx) 的结果（可信来源）。

        Raises:
            ValueError: 显式 tenant 与 ctx tenant 不一致，或显式 tenant 为空。
        """
        if query_scope is None:
            return
        if not query_scope.tenant.strip() or query_scope.tenant != ctx_scope.tenant:
            raise ValueError("query scope tenant mismatch; fail-closed")
