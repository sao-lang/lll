# purity-exempt-file: InMemoryIntentSampleStore 为 B.1.2 指定默认部件命名；component ctx 为协议透传参数
"""意图四级级联默认实现 + Program 桥梁（R13 §4.1 / R8-T08，D-级联-1）。

短路在 generator 层兑现：L1/L2 本地算不 yield Effect；L3/L4 各 yield 一次
Invoke(port="llm")，收到回注后判定阈值；达阈值即停止 yield 后续级。
最终以 Terminate(output=dict) 传出 IntentResult 观察形态（label/confidence/
gaps/trace/hit_stage），供 Router 单入口消费。

复杂度留在策略层：不新增 R1 Effect 种类、不新增 Lifecycle 相位、不下沉内核 DTO。
用户铁律：默认部件确定性、不联网、不调真实 LLM；L3/L4 的 LLM 路径以 Invoke
Effect 表达，由调用方注入的 call（录制/假 LLMPort）兑现。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator, Mapping, Sequence
from typing import Final

from runtime import DynamicValue, Effect, Invoke, Terminate

from .contracts import (
    CheapIntentModel,
    IntentResult,
    IntentSample,
    IntentSampleStore,
    KeywordMatcher,
    PrimaryIntentModel,
)

def _await_sync(factory: Callable[[], object]) -> object:
    """在同步 Program generator 内执行 async 部件（无 loop 直接 run，有 loop 走独立线程）。

    避免 asyncio.run 在活事件循环中抛 RuntimeError；与门面 say 的同款适配纪律。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())  # type: ignore[arg-type]
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()  # type: ignore[arg-type]


__all__ = [
    "CascadeIntentStrategy",
    "InMemoryIntentSampleStore",
    "RegexKeywordMatcher",
    "intent_as_program",
]

# 默认阈值：L1 零成本高置信、L2 语义中置信、L3/L4 模型判定。
_DEFAULT_THRESHOLDS: Final[dict[str, float]] = {
    "L1": 0.7, "L2": 0.6, "L3": 0.6, "L4": 0.5,
}

type _ProgramGen = Generator[Effect, DynamicValue, None]
type _ProgramFn = Callable[[], _ProgramGen]


def _stage_trace(stage: str, result: IntentResult | None) -> str:
    """格式化单级观察记录（stage:label:confidence）。"""
    if result is None:
        return f"{stage}:none:0.0"
    return f"{stage}:{result.label}:{result.confidence:.2f}"


class RegexKeywordMatcher:
    """L1 默认部件：子串关键词表，命中返回高置信度 IntentResult。"""
    name = "keyword"

    def __init__(self, table: Mapping[str, str] | None = None) -> None:
        """构造关键词表（关键词 → label）。

        Args:
            table: 关键词到 label 的映射；None 用空表（恒不命中）。
        """
        self._table = dict(table or {})

    def match(self, text: str, ctx: object = None) -> IntentResult | None:  # purity-exempt: 协议 ctx 只透传不读，Object 为 R1 契约既有形态
        """按子串匹配，命中第一个关键词返回；顺序由 dict 插入序保证确定性。"""
        lowered = text.lower()
        for keyword, label in self._table.items():
            if keyword.lower() in lowered:
                return IntentResult(label=label, confidence=0.9)
        return None


class InMemoryIntentSampleStore:
    """L2 默认部件：内存采样库 + 内置字符二元组相似度（零依赖、确定性）。"""
    name = "samples"

    def __init__(self, samples: Sequence[IntentSample] = ()) -> None:
        """构造采样库。"""
        self._samples = tuple(samples)

    @staticmethod
    def similarity(text: str, sample_text: str) -> float:
        """字符二元组 Jaccard 相似度（零依赖、确定性，供 L2 阈值判定复用）。"""
        def bigrams(value: str) -> set[str]:
            normal = "".join(value.lower().split())
            return {normal[i:i + 2] for i in range(max(0, len(normal) - 1))}

        a, b = bigrams(text), bigrams(sample_text)
        union = a | b
        return len(a & b) / len(union) if union else 0.0

    def search(self, text: str, top_k: int = 5) -> tuple[IntentSample, ...]:
        """采样库检索：按相似度降序取 top_k（并列按插入序，确定性）。"""
        if not self._samples:
            return ()
        ranked = sorted(enumerate(self._samples),
                        key=lambda pair: (-self.similarity(text, pair[1].text), pair[0]))
        return tuple(sample for _, sample in ranked[:top_k]
                     if self.similarity(text, sample.text) > 0.0)

    def match(self, text: str) -> IntentResult | None:
        """L2 判定：取最相似采样，返回 label + 相似度置信度（无命中返回 None）。"""
        hits = self.search(text, top_k=1)
        if not hits:
            return None
        head = hits[0]
        return IntentResult(label=head.label, confidence=self.similarity(text, head.text))


def _result_from_injection(data: DynamicValue) -> IntentResult | None:
    """把 L3/L4 Invoke 回注归一为 IntentResult（dict 形态）。"""
    if not isinstance(data, Mapping):
        return None
    label = data.get("label")
    confidence = data.get("confidence", 0.0)
    if not isinstance(label, str):
        return None
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.0
    return IntentResult(label=label, confidence=conf)


class CascadeIntentStrategy:
    """默认整体策略 = L1 关键词 → L2 采样库 → L3 便宜模型 → L4 主模型。

    四部件可各自注入（构造参数），未注入用本文件的确定性默认实现。
    build(text) 返回 Program generator，每次调用新产（满足 D-8 resume 重建）。
    """
    name = "cascade"

    def __init__(
        self,
        *,
        keyword: object | None = None,
        samples: object | None = None,
        cheap: CheapIntentModel | None = None,
        primary: PrimaryIntentModel | None = None,
        thresholds: Mapping[str, float] | None = None,
    ) -> None:
        """构造级联策略：注入部件替换对应级，thresholds 覆写各级阈值。"""
        self.keyword = keyword if keyword is not None else RegexKeywordMatcher()
        self.samples = samples if samples is not None else InMemoryIntentSampleStore()
        self.cheap = cheap
        self.primary = primary
        self.thresholds = {**_DEFAULT_THRESHOLDS, **(dict(thresholds) if thresholds else {})}

    async def recognize(self, text: str, ctx: object) -> IntentResult:
        """async 单入口：逐级判定，达阈值即停（供非 Program 调用方使用）。

        逐级 await（而非在同步 generator 内 asyncio.run），避免嵌套事件循环与
        coroutine 泄漏；与 build() 的 Effect 版语义一致。
        """
        trace: list[str] = []

        l1 = getattr(self.keyword, "match", None)
        r1 = l1(text, None) if callable(l1) else None
        trace.append(_stage_trace("L1", r1))
        if r1 is not None and r1.confidence >= self.thresholds["L1"]:
            return IntentResult(label=r1.label, confidence=r1.confidence,
                                gaps=r1.gaps, trace=tuple(trace))

        match = getattr(self.samples, "match", None)
        r2 = match(text) if callable(match) else None
        trace.append(_stage_trace("L2", r2))
        if r2 is not None and r2.confidence >= self.thresholds["L2"]:
            return IntentResult(label=r2.label, confidence=r2.confidence,
                                gaps=r2.gaps, trace=tuple(trace))

        if self.cheap is not None:
            r3 = await self.cheap.recognize(text, None)
            trace.append(_stage_trace("L3", r3))
            if r3.confidence >= self.thresholds["L3"]:
                return IntentResult(label=r3.label, confidence=r3.confidence,
                                    gaps=r3.gaps, trace=tuple(trace))

        if self.primary is not None:
            r4 = await self.primary.recognize(text, None)
            trace.append(_stage_trace("L4", r4))
            if r4.confidence >= self.thresholds["L4"]:
                return IntentResult(label=r4.label, confidence=r4.confidence,
                                    gaps=r4.gaps, trace=tuple(trace))

        return IntentResult(label="unknown", confidence=0.0, trace=tuple(trace))

    def build(self, text: str = "") -> _ProgramGen:
        """产 Program generator（yield Effect，短路，末级 Terminate）。

        L3/L4 的 LLM 调用以 Invoke(port="llm") 表达；即便注入了 cheap/primary
        部件，Program 路径也统一走 Effect（由 call 兑现），保证策略层不直接 IO。
        """
        trace: list[str] = []
        # L1：本地关键词
        l1 = getattr(self.keyword, "match", None)
        r1 = l1(text, None) if callable(l1) else None
        trace.append(_stage_trace(f"L1[{getattr(self.keyword, 'name', 'kw')}]", r1))
        if r1 is not None and r1.confidence >= self.thresholds["L1"]:
            yield Terminate(output=self._output(r1, "L1", trace))
            return
        # L2：本地采样库相似度
        match = getattr(self.samples, "match", None)
        if callable(match):
            r2 = match(text)
        else:
            search = getattr(self.samples, "search", None)
            hits = search(text) if callable(search) else ()
            r2 = IntentResult(label=hits[0].label, confidence=0.6) if hits else None
        trace.append(_stage_trace(f"L2[{getattr(self.samples, 'name', 'samples')}]", r2))
        if r2 is not None and r2.confidence >= self.thresholds["L2"]:
            yield Terminate(output=self._output(r2, "L2", trace))
            return
        # L3：注入的 cheap 部件优先（同步适配 async recognize，与 recognize() 路径
        # 语义一致）；未注入才走 Invoke(port=llm) 由 call 兑现——两路同一判定。
        cheap = self.cheap
        if cheap is not None:
            raw3 = _await_sync(lambda: cheap.recognize(text, None))
            r3 = raw3 if isinstance(raw3, IntentResult) else None
            trace.append(_stage_trace("L3", r3))
            if r3 is not None and r3.confidence >= self.thresholds["L3"]:
                yield Terminate(output=self._output(r3, "L3", trace))
                return
        else:
            injected = yield Invoke(port="llm").with_payload(
                task="intent", tier="cheap", text=text)
            r3 = _result_from_injection(injected)
            trace.append(_stage_trace("L3", r3))
            if r3 is not None and r3.confidence >= self.thresholds["L3"]:
                yield Terminate(output=self._output(r3, "L3", trace))
                return
        # L4：注入的 primary 部件优先；未注入走 Invoke 由 call 兑现
        primary = self.primary
        if primary is not None:
            raw4 = _await_sync(lambda: primary.recognize(text, None))
            r4 = raw4 if isinstance(raw4, IntentResult) else None
            trace.append(_stage_trace("L4", r4))
            if r4 is not None and r4.confidence >= self.thresholds["L4"]:
                yield Terminate(output=self._output(r4, "L4", trace))
                return
        else:
            injected = yield Invoke(port="llm").with_payload(
                task="intent", tier="primary", text=text)
            r4 = _result_from_injection(injected)
            trace.append(_stage_trace("L4", r4))
            if r4 is not None and r4.confidence >= self.thresholds["L4"]:
                yield Terminate(output=self._output(r4, "L4", trace))
                return
        # 全不满足：default/unknown 兜底
        yield Terminate(output=self._output(
            IntentResult(label="unknown", confidence=0.0), "default", trace))

    @staticmethod
    def _output(result: IntentResult, hit_stage: str,
                trace: Sequence[str]) -> dict[str, DynamicValue]:
        """把 IntentResult 归一为观察 dict（Terminate.output 载荷）。"""
        return {
            "label": result.label,
            "confidence": result.confidence,
            "gaps": list(result.gaps),
            "trace": tuple(trace),
            "hit_stage": hit_stage,
        }


def intent_as_program(strategy: object, text: str = "") -> _ProgramFn:
    """统一桥梁：任何策略 → ProgramFn（yield from 后读 Terminate.output）。

    分支：
        - 有 build()（CascadeIntentStrategy）：直接调 build(text)，短路在 generator 层；
        - 仅 recognize() 的 IntentStrategy：以 asyncio.run 做 async→sync 适配，
          兑现策略自身逻辑（整体替换语义）；Router 不再各自内联 recognize 解析；
        - 纯 Callable（R12 兼容路径）：同步调用 + 归一；
        - 非上述（None/非法）：产 unknown 兜底 Terminate。

    Args:
        strategy: IntentStrategy / 级联策略 / Callable / None。
        text: 待识别文本。

    Returns:
        ProgramFn（每次调用产新 generator，满足 D-8）。
    """
    # 能力包解包（用户主线：注入的是能力包，能力包是策略+工具的容器）：
    # 桥梁层识别 .strategy，架构/门面无需知道能力包内部形态。能力包自身
    # 不持有 build()——Program 桥梁的职责始终在本函数，不下沉到能力包。
    inner = getattr(strategy, "strategy", None)
    if inner is not None and inner is not strategy:
        return intent_as_program(inner, text)

    build = getattr(strategy, "build", None)
    if callable(build):
        def from_build() -> _ProgramGen:
            yield from build(text)
        return from_build

    recognize = getattr(strategy, "recognize", None)
    if callable(recognize):
        def adapted() -> _ProgramGen:
            result: DynamicValue = asyncio.run(recognize(text, None))
            normalized = (result if isinstance(result, IntentResult)
                          else _result_from_injection(result))
            if normalized is None:
                normalized = IntentResult(label="unknown", confidence=0.0)
            yield Terminate(output=CascadeIntentStrategy._output(
                normalized, "strategy", ("strategy:adapted",)))
        return adapted

    if callable(strategy):
        def callable_adapter() -> _ProgramGen:
            raw: DynamicValue = strategy(text)
            if hasattr(raw, "__await__"):
                raw = asyncio.run(raw)
            result = raw if isinstance(raw, IntentResult) else _result_from_injection(raw)
            if result is None:
                result = IntentResult(label="unknown", confidence=0.0)
            yield Terminate(output=CascadeIntentStrategy._output(
                result, "strategy", ("strategy:callable",)))
        return callable_adapter

    def fallback() -> _ProgramGen:
        yield Terminate(output=CascadeIntentStrategy._output(
            IntentResult(label="unknown", confidence=0.0), "default", ()))
    return fallback
