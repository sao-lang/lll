"""R7 记忆核心 DTO：三层/三类枚举、记忆项与召回查询。

本模块只描述数据结构、具名常量与 secret 键判定，不含存储逻辑。
secret 判定用于写入 fail-closed（红线：secret 不进记忆）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "MemoryTier",
    "MemoryKind",
    "MemoryItem",
    "MemoryQuery",
    "SECRET_KEY_TOKENS",
    "is_secret_key",
]


class MemoryTier(StrEnum):
    """记忆分层（有限取值集合）。

    成员：
        HOT: 会话内，短 TTL。
        WARM: 结构化摘要。
        COLD: 全文归档。
    """

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class MemoryKind(StrEnum):
    """记忆类型（有限取值集合）。

    成员：
        EPISODIC: 情节（发生过什么）。
        SEMANTIC: 事实（知道什么）。
        PROCEDURAL: 流程（怎么做）。
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """一条记忆条目。

    字段：
        kind: 记忆类型。
        content: 正文（写入前须过注入检测且不含 secret）。
        importance: 重要性评分（0~1）。
        ts: 时间戳（整数，写入时刻）。
        source: 来源标识（如 session_end）。
    """

    kind: MemoryKind
    content: str
    importance: float
    ts: int
    source: str


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """R7 自有语义召回查询 DTO（不复用 R6 Query）。

    字段：
        text: 查询文本。
        top_k: 返回条数上限。
        tier: 限定分层（None 表示全层）。
        kind: 限定类型（None 表示全部）。
    """

    text: str
    top_k: int = 5
    tier: MemoryTier | None = None
    kind: MemoryKind | None = None


# secret 键名特征（大小写不敏感子串匹配；规则 2：具名常量集中定义）。
SECRET_KEY_TOKENS: tuple[str, ...] = ("secret", "token", "password", "api_key", "apikey")


def is_secret_key(key: str) -> bool:
    """判定键名是否疑似 secret（写入 fail-closed 的第一道闸）。

    Args:
        key: 待写入的记忆键名。

    Returns:
        命中任一 secret 特征子串返回 True（大小写不敏感），否则 False。
    """
    folded = key.casefold()
    return any(token in folded for token in SECRET_KEY_TOKENS)
