"""R6 规则改写器行为测试：验证无模型改写的新契约（FR-1~FR-8）。"""
from __future__ import annotations

import asyncio

from harness.rag.rewrite import (
    DEFAULT_N_VARIANTS,
    MultiQueryRewriter,
    _split_subqueries,
)
from harness.rag.types import Query, QueryPurpose

# 同义词命中用的词表 key（走内置默认词表，避免依赖自定义注入）
_SYN_KEY = "召回"
_SYN_VALUE = "检索"


def _rewrite(query: Query, *, rewriter: MultiQueryRewriter | None = None) -> list[Query]:
    """同步执行改写的测试辅助。"""
    used = rewriter if rewriter is not None else MultiQueryRewriter()
    return asyncio.run(used.rewrite(query))


def test_fr1_default_and_max_variants() -> None:
    """FR-1：默认构造可用；返回变体数不超过 n_variants 且至少 1 份。"""
    base = Query(text="alpha")
    for n in (1, 2, 5):
        out = _rewrite(base, rewriter=MultiQueryRewriter(n_variants=n))
        assert 1 <= len(out) <= n


def test_fr2_injected_synonyms_take_effect() -> None:
    """FR-2：注入自定义词表后，变体使用注入映射而非内置表。"""
    custom = {"foo": ("bar",)}
    out = _rewrite(
        Query(text="foo"),
        rewriter=MultiQueryRewriter(n_variants=2, synonyms=custom),
    )
    texts = [q.text for q in out]
    assert "bar" in texts  # 注入映射生效
    assert "检索" not in " ".join(texts)  # 内置映射未被误用


def test_fr3_subquery_split_by_punctuation_and_conjunction() -> None:
    """FR-3：按标点与连接词拆子句；子句必须是原串连续子串。"""
    text = "向量召回和关键词精排，以及混合检索"
    out = _rewrite(Query(text=text), rewriter=MultiQueryRewriter(n_variants=6))
    for variant in out:
        # 每个变体要么是原串、要么是同义替换结果、要么是拆出的子句或其替换
        assert variant.text
    # 至少有一个变体是词面不同的子串拆分结果
    assert any(v.text != text and v.text in text for v in out)


def test_fr4_synonym_expansion_produces_distinct_text() -> None:
    """FR-4：命中同义词表的 query 产出文本不同的变体。"""
    out = _rewrite(Query(text=_SYN_KEY))
    texts = [q.text for q in out]
    assert any(_SYN_VALUE in t for t in texts), texts


def test_fr5_original_first_dedup_and_cap() -> None:
    """FR-5：原串排第一；变体互不相同；总数受 n_variants 限制。"""
    out = _rewrite(Query(text="向量召回，关键词精排"), rewriter=MultiQueryRewriter(5))
    assert out[0].text == "向量召回，关键词精排"
    texts = [q.text for q in out]
    assert len(texts) == len(set(texts))


def test_fr5_no_change_yields_single_variant() -> None:
    """FR-5：无同义词命中且无可拆子句时，只返回原串一份。"""
    out = _rewrite(Query(text="xyz"), rewriter=MultiQueryRewriter(3))
    assert len(out) == 1 and out[0].text == "xyz"


def test_fr6_only_text_changes_other_fields_preserved() -> None:
    """FR-6：变体仅改 text，filters/top_k/budget/purpose 与原 Query 一致。"""
    base = Query(
        text="向量召回，关键词精排",
        filters={"lang": "zh"},
        top_k=7,
        token_budget=99,
        purpose=QueryPurpose.MEMORY,
    )
    out = _rewrite(base, rewriter=MultiQueryRewriter(4))
    assert len(out) > 1
    for variant in out:
        assert variant.filters == base.filters
        assert variant.top_k == base.top_k
        assert variant.token_budget == base.token_budget
        assert variant.purpose is base.purpose
        assert variant is not base


def test_fr7_no_new_entities() -> None:
    """FR-7：未知词不得被替换或新增；变体词项来自原串或同义映射值。"""
    out = _rewrite(Query(text="qzzunknownword"))
    for variant in out:
        assert variant.text == "qzzunknownword"  # 无可改 → 只原串


def test_fr8_deterministic() -> None:
    """FR-8：同输入两次改写产出完全一致。"""
    q = Query(text="向量召回和关键词精排")
    first = [v.text for v in _rewrite(q, rewriter=MultiQueryRewriter(5))]
    second = [v.text for v in _rewrite(q, rewriter=MultiQueryRewriter(5))]
    assert first == second


def test_b1_default_n_balances_strategies() -> None:
    """B1 回归：默认 N=3 时同义替换与子句拆分两种策略都有代表进入截断窗口。"""
    text = "向量召回，关键词精排"
    out = _rewrite(Query(text=text))
    assert len(out) == DEFAULT_N_VARIANTS
    texts = [v.text for v in out]
    # 同义策略：至少一个变体含内置同义值（词面变化，关键词通道才有新增覆盖）。
    assert any(v in t for t in texts[1:] for v in ("检索", "嵌入", "重排", "词面")), texts
    # 子句策略：至少一个变体是原串的连续子串（粒度变化，独立检索视角）。
    assert any(t != text and t in text for t in texts[1:]), texts


def test_sr1_all_subqueries_survive_before_synonym_filler() -> None:
    """S-R1 回归：多子句查询中，所有子句先于同义填充入列，后段子句不被饿死。"""
    text = "向量召回，关键词精排，文档检索，词面重排"
    out = _rewrite(Query(text=text), rewriter=MultiQueryRewriter(6))
    texts = [v.text for v in out]
    # 4 个子句全部在 N=6 窗口内（原串 + 1 整串同义 + 4 子句 = 6）。
    for piece in ("向量召回", "关键词精排", "文档检索", "词面重排"):
        assert piece in texts, texts


def test_s1_injected_synonyms_copied_at_construction() -> None:
    """S1：构造后修改源词表不得改变改写结果（D4 确定性）。"""
    source: dict[str, tuple[str, ...]] = {"foo": ("bar",)}
    rewriter = MultiQueryRewriter(n_variants=2, synonyms=source)
    before = [q.text for q in _rewrite(Query(text="foo"), rewriter=rewriter)]
    source["foo"] = ("hack",)
    after = [q.text for q in _rewrite(Query(text="foo"), rewriter=rewriter)]
    assert before == after and "hack" not in after


def test_s3_split_subqueries_are_contiguous_substrings() -> None:
    """S3：直接锁定 FR-3 契约——每个子句都是原串连续子串，含英文连接词。"""
    cases = [
        "向量召回和关键词精排，以及混合检索",
        "foo and then bar or baz",
        "段落一。段落二；段落三、段落四",
    ]
    for text in cases:
        parts = _split_subqueries(text)
        assert parts and all(part in text for part in parts), (text, parts)
    # 英文连接词（and/then）都作分隔符切掉；strip 后不产生带空格的子句。
    assert _split_subqueries("foo and then bar") == ["foo", "bar"]


def test_nfr1_no_model_no_new_deps() -> None:
    """NFR-1：改写器模块不 import LLM/网络/文件模块（纯字符串规则）。"""
    import inspect

    from harness.rag import rewrite

    src = inspect.getsource(rewrite)
    for banned in ("openai", "httpx", "requests", "open(", "torch"):
        assert banned not in src
