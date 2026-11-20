"""R6 改写段：QueryRewriter 协议 + 无模型的规则改写默认实现。

默认改写器把原查询做"同义词等价替换 + 标点/连接词拆子句"，产出多个文本
不同、语义等价的查询变体，提升多路召回的词面覆盖。全程纯字符串规则：
不调用 LLM / 网络 / 文件，不引入新实体（同义词映射值定义为等价词，
拆分子串必须来自原串）。词表可注入替换，生产可注入 HyDE/step-back 策略。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.types import Query

__all__ = ["DEFAULT_N_VARIANTS", "MultiQueryRewriter", "QueryRewriter"]

DEFAULT_N_VARIANTS: int = 3

# 子句切分符：标点与连接词统一在此具名（禁魔法字符串，规则 2）。
# 按长度降序排列，确保"以及"先于"及"匹配，避免短词抢先切开。
_SPLIT_TOKENS: tuple[str, ...] = (
    "以及", "。", "？", "！", "，", ",", ";", "；", "、",
    "和", "与", "及", "并", "then", " and ", " or ",
)

# 内置精简同义词表：key 与 value 都是"等价词"，替换不引入原问题之外的新实体。
# value 用元组承载多个候选，取首个作为确定性替换（保证同输入同输出）。
_DEFAULT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "召回": ("检索",),
    "检索": ("召回",),
    "精排": ("重排",),
    "重排": ("精排",),
    "向量": ("嵌入",),
    "嵌入": ("向量",),
    "关键词": ("词面",),
    "词面": ("关键词",),
    "文档": ("语料",),
    "语料": ("文档",),
    "怎么": ("如何",),
    "如何": ("怎么",),
}


@runtime_checkable
class QueryRewriter(Protocol):
    """查询改写协议。"""

    name: str

    async def rewrite(self, query: Query) -> list[Query]:
        """把一个查询改写为多个查询。"""
        ...


def _split_subqueries(text: str) -> list[str]:
    """按标点/连接词把查询拆成连续子串子句。

     迭代扫描：逐个找切分符位置，切片后继续从符后扫描，保证每个产物
    都是原串的连续子串（守 D2 红线）。空片段丢弃，去重保序。

    Args:
        text: 原始查询文本。

    Returns:
        子句列表；无切分符时返回空列表（由调用方补原串）。
    """
    pieces: list[str] = []
    start = 0
    cursor = 0
    while cursor < len(text):
        matched = next((t for t in _SPLIT_TOKENS if text.startswith(t, cursor)), None)
        # 命中切分符：把符前片段收进结果，游标跳到符后。
        # strip 去掉英文连接词（" and "）残留的前后空格——strip 后仍是原串连续子串，
        # 不破 D2 红线，同时避免空格噪声进入 embed 与关键词通道。
        if matched is not None:
            piece = text[start:cursor].strip()
            if piece:
                pieces.append(piece)
            cursor += len(matched)
            start = cursor
        else:
            cursor += 1
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    seen: set[str] = set()
    unique: list[str] = []
    for piece in pieces:
        if piece not in seen:
            seen.add(piece)
            unique.append(piece)
    return unique


def _synonym_variants(
    text: str, synonyms: dict[str, tuple[str, ...]], *, limit: int | None = None,
) -> list[str]:
    """对文本做同义词等价替换，产出每个命中词的一个变体。

    一轮只替换一种 key 的首个出现（取 value 首个候选），避免组合爆炸；
    多种命中各自独立产出一个变体，顺序按词表插入序，保证确定性。
    limit 限制返回数量，防止整串的多个命中把子句挤出截断窗口（B1 同类问题）。

    Args:
        text: 待改写文本。
        synonyms: 同义词表；key→候选等价词元组。
        limit: 最多返回几个替换变体；None 表示不限。

    Returns:
        替换后的文本列表（均与原串不同）。
    """
    out: list[str] = []
    for key, candidates in synonyms.items():
        if limit is not None and len(out) >= limit:
            break
        if not candidates or key not in text:
            continue
        # 只替换首个命中处：单个变体只改一个词，保持与原串的最小差异。
        replaced = text.replace(key, candidates[0], 1)
        # 注入 {"k": ("k",)} 这类等值映射时结果等于原串，不入列（让 docstring 的"均不同"成立）。
        if replaced != text:
            out.append(replaced)
    return out


class MultiQueryRewriter:
    """无模型规则改写：同义词替换 + 标点/连接词拆子句，产出等价多查询。"""

    name: str = "multi_query"

    def __init__(
        self,
        n_variants: int = DEFAULT_N_VARIANTS,
        synonyms: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        """构造改写器。

        Args:
            n_variants: 最多返回的变体数（含原串），小于 1 按 1 处理。
            synonyms: 自定义同义词表；None 时用内置精简表。注入后完全替换内置表。
                等价性责任在注入方——value 必须是 key 的等价词，否则会引入新实体
                （违反红线 D2）。构造期做浅拷贝，构造后修改源 dict 不影响本实例。
        """
        self.n_variants = max(1, n_variants)
        # 浅拷贝：隔离调用方的 dict，保住 D4 确定性（构造后外部改动不改变改写结果）。
        source = synonyms if synonyms is not None else _DEFAULT_SYNONYMS
        self.synonyms: dict[str, tuple[str, ...]] = dict(source)

    async def rewrite(self, query: Query) -> list[Query]:
        """产出最多 n_variants 个文本不同的等价查询，原串排第一。

        流程：原串 + 拆分子句 + 同义替换（整串与子句都做）→ 去重保序 →
        截断到 n_variants。无任何改动时只返回原串一份。
        所有变体仅覆盖 text，其余字段用 model_copy 原样继承。

        Args:
            query: 原始查询。

        Returns:
            等价查询变体列表（非空，首个为原串）。
        """
        text = query.text
        subqueries = _split_subqueries(text)

        # 交错组装（B1 修复）：按"原串 → 整串同义 → 子句1 → 子句1同义 → 子句2…"
        # 排序，保证每种策略在截断窗口内都有代表——默认 N=3 必含至少 1 个
        # 同义替换变体，避免子句把同义变体挤出导致核心召回增益失效。
        # 第一轮"基础覆盖"（保证 N=3 时两种策略都有代表，B1）：
        # 原串 + 整串同义 1 个（词面变化）+ 全部子句（粒度变化）。
        # 子句先于子句同义入列，避免每个子句占 2 名额把后段子句饿死（S-R1）。
        text_synonyms = _synonym_variants(text, self.synonyms)
        candidates: list[str] = [text]
        if text_synonyms:
            candidates.append(text_synonyms[0])
        candidates.extend(subqueries)
        # 第二轮"增益填充"（N 较大时填剩余预算，S-R2）：
        # 整串同义余量 + 各子句同义，进一步放大关键词通道的词面覆盖。
        candidates.extend(text_synonyms[1:])
        for piece in subqueries:
            candidates.extend(_synonym_variants(piece, self.synonyms, limit=1))

        # 去重保序：首份必为原串，后续只保留文本不同的变体（FR-5）。
        seen: set[str] = set()
        texts: list[str] = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                texts.append(candidate)

        # 截断到上限后逐个派生 Query，只改 text，其余字段深拷贝继承（FR-6）。
        return [
            query.model_copy(deep=True, update={"text": variant})
            for variant in texts[: self.n_variants]
        ]
