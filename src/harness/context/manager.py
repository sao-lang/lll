"""R7 显式编排器：统一装配入口（7 依赖注入），顺序由代码结构保证。

commit_and_compact() 方法体内固定"先 write_cold 全文，再 compact 产视图"，
随后经 Reinjector 把摘要+钉住+未完成任务按预算回注（不绕过预算）。
trace 仅记 write_cold/compact 两个顺序事件（兼容既有断言）；
with_ 不可变替换时保留 trace 观测历史。通知点纯通知，副作用在订阅者内经本类方法完成。
"""
from __future__ import annotations

from harness.memory import MemoryStore
from harness.rag.types import TokenBudget
from compose import PortCtx
from compose.tool_layer import layer_program
from runtime import AgentState, Message, Program

from .assembler import ContextAssembler, DefaultContextAssembler
from .compaction import CompactionPolicy, DefaultCompactionPolicy, SUMMARY_ROLE
from .observability import CacheTelemetry
from .offloader import BlobPort, DefaultOffloader, InMemoryBlobStore, Offloader
from .reinjector import DefaultReinjector, Reinjector
from .token_counter import DefaultTokenCounter, TokenCounter
from .types import ContextSection

__all__ = ["ContextMemoryManager"]

# trace 事件名（具名常量；仅两项，保持顺序断言稳定）。
_TRACE_WRITE_COLD = "write_cold"
_TRACE_COMPACT = "compact"
# 默认重注入预算占总预算比例（传给 Reinjector 的 TokenBudget 由调用方传入）。
_DEFAULT_REINJECT_TASKS: tuple[str, ...] = ()


class ContextMemoryManager:
    """R7 统一编排器：7 依赖显式注入，顺序与预算都由代码保证。"""

    def __init__(
        self,
        assembler: ContextAssembler | None = None,
        compaction: CompactionPolicy | None = None,
        offloader: Offloader | None = None,
        memory: MemoryStore | None = None,
        blob: BlobPort | None = None,
        reinjector: Reinjector | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        """构造编排器；None 的依赖用默认实现（开箱可用）。

        Args:
            assembler: 上下文组装器。
            compaction: 压缩策略。
            offloader: 大结果卸载器。
            memory: 长期记忆存储。
            blob: 卸载存储后端。
            reinjector: 重注入器（FR-11 install 入口）。
            token_counter: 统一 token 计数器（FR-11 install 入口）。
        """
        self.token_counter = (
            token_counter if token_counter is not None else DefaultTokenCounter()
        )
        # 统一口径下传：注入的 counter 必须传给三个消费者（FR-11 install 四要素）。
        self.assembler = (
            assembler
            if assembler is not None
            else DefaultContextAssembler(counter=self.token_counter)
        )
        self.compaction = (
            compaction
            if compaction is not None
            else DefaultCompactionPolicy(counter=self.token_counter)
        )
        self.offloader = offloader if offloader is not None else DefaultOffloader()
        self.memory = memory if memory is not None else MemoryStore()
        self.blob = blob if blob is not None else InMemoryBlobStore()
        self.reinjector = (
            reinjector
            if reinjector is not None
            else DefaultReinjector(counter=self.token_counter)
        )
        self.trace: list[str] = []
        # R7 新增方案 D5：KV cache 命中率一等观测面（可进 R10 trace、可按 label 归因）。
        self.cache_telemetry = CacheTelemetry()

    def record_cache_hit(self, label: str = "default") -> None:
        """记录一次 KV cache 命中（一等观测，纯替换不可变指标）。"""
        self.cache_telemetry = self.cache_telemetry.with_label(label).record_hit()

    def record_cache_miss(self, label: str = "default") -> None:
        """记录一次 KV cache 未命中。"""
        self.cache_telemetry = self.cache_telemetry.with_label(label).record_miss()

    def build(self) -> Program:
        """ToolLayer 外壳（规格 P8）：产出"用上下文记忆"的 Program 工厂。

        内部四链（组装/压缩/重注入/卸载）保持显式编排不变；外壳只给 R1 领域无知
        的 Program。上下文四链属 ⑥组件 + ④notify，不是算子。
        """
        return layer_program("memory", payload={"layer": "context"})()

    def assemble(
        self, sections: list[ContextSection], budget: TokenBudget
    ) -> list[Message]:
        """显式组装上下文（替代原 before_model hook）。

        Args:
            sections: 上下文分段。
            budget: token 预算。

        Returns:
            组装后的消息列表。
        """
        return self.assembler.assemble(sections, budget)

    def commit_and_compact(
        self,
        state: AgentState,
        transcript: str,
        ctx: PortCtx | None = None,
        budget: TokenBudget | None = None,
        tasks: tuple[str, ...] = _DEFAULT_REINJECT_TASKS,
    ) -> tuple[Message, ...]:
        """提交会话：先写 cold 全文→压缩→按预算重注入（顺序由代码固定）。

        Args:
            state: 当前状态（只读，不改 messages）。
            transcript: 本轮完整对话记录（写 cold 全文，避免细节丢失）。
            ctx: Port 上下文；None 时用空上下文（仅 demo/测试）。
            budget: 重注入预算；None 时不限（不裁剪回注项）。
            tasks: 未完成任务，供重注入保留进度。

        Returns:
            压缩并重注入后的消息视图（新 tuple，State.messages 不变）。
        """
        port_ctx = ctx if ctx is not None else PortCtx()
        # ① 先写 cold 全文（细节必须先落盘才能提炼）。
        self.memory.write_cold(transcript, port_ctx)
        self.trace.append(_TRACE_WRITE_COLD)
        # ② 再压缩产视图（不改 state.messages）。
        view = self.compaction.compact(state.messages)
        self.trace.append(_TRACE_COMPACT)
        # ③ 重注入：语义摘要 + 钉住 + 未完成任务，按预算二次裁剪。
        summary = next((m for m in view if m.role == SUMMARY_ROLE), None)
        # 默认预算取 state.max_tokens（有限）；显式 None 也强制回退有限值，禁止无限绕过裁剪。
        cap = budget.limit if budget is not None else None
        if cap is None:
            cap = state.budgets.max_tokens
        effective = TokenBudget(limit=cap)
        return self.reinjector.reinject(view, summary, tasks, effective)

    async def recall(self, query: object, ctx: PortCtx | None = None) -> list[object]:
        """显式语义召回（替代原 before_model 记忆注入 hook）。

        Args:
            query: MemoryQuery 实例（运行时校验类型）。
            ctx: Port 上下文。

        Returns:
            召回的 MemoryItem 列表。

        Raises:
            TypeError: query 非 MemoryQuery。
        """
        from harness.memory.types import MemoryQuery

        if not isinstance(query, MemoryQuery):
            raise TypeError("recall expects MemoryQuery")
        return list(await self.memory.recall(query, ctx if ctx is not None else PortCtx()))

    async def offload(self, value: object, ctx: PortCtx | None = None) -> str:
        """显式大结果卸载，返回可读句柄（替代原 after_tool hook）。

        Args:
            value: 待卸载的大结果。
            ctx: Port 上下文（保留签名兼容，当前未消费）。

        Returns:
            store:// 句柄字符串。
        """
        return await self.offloader.offload(value, self.blob)

    def with_(
        self,
        assembler: ContextAssembler | None = None,
        compaction: CompactionPolicy | None = None,
        offloader: Offloader | None = None,
        memory: MemoryStore | None = None,
        blob: BlobPort | None = None,
        reinjector: Reinjector | None = None,
        token_counter: TokenCounter | None = None,
    ) -> ContextMemoryManager:
        """产新实例替换指定依赖（不可变风格），并保留 trace 观测历史。

        Args:
            assembler: 新组装器；None 沿用。
            compaction: 新压缩策略；None 沿用。
            offloader: 新卸载器；None 沿用。
            memory: 新记忆存储；None 沿用。
            blob: 新卸载存储；None 沿用。
            reinjector: 新重注入器；None 沿用。
            token_counter: 新 token 计数器；None 沿用。

        Returns:
            替换依赖后的新实例；当前实例不变。
        """
        new = ContextMemoryManager(
            assembler=assembler if assembler is not None else self.assembler,
            compaction=compaction if compaction is not None else self.compaction,
            offloader=offloader if offloader is not None else self.offloader,
            memory=memory if memory is not None else self.memory,
            blob=blob if blob is not None else self.blob,
            reinjector=reinjector if reinjector is not None else self.reinjector,
            token_counter=(
                token_counter if token_counter is not None else self.token_counter
            ),
        )
        # 换 counter 时同步重建仍为默认的三个消费者，避免注入失效。
        if token_counter is not None:
            if assembler is None and isinstance(self.assembler, DefaultContextAssembler):
                new.assembler = DefaultContextAssembler(counter=token_counter)
            if compaction is None and isinstance(
                self.compaction, DefaultCompactionPolicy
            ):
                new.compaction = DefaultCompactionPolicy(counter=token_counter)
            if reinjector is None and isinstance(self.reinjector, DefaultReinjector):
                new.reinjector = DefaultReinjector(counter=token_counter)
        new.trace = list(self.trace)
        new.cache_telemetry = self.cache_telemetry  # 保留 KV cache 一等观测历史（不可变 DTO 直接共享）
        return new
