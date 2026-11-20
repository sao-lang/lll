"""R7 显式编排器：统一装配入口，顺序由代码结构保证（不依赖内核 hook）。

commit_and_compact() 方法体内固定"先 write_cold 全文，再 compact 产视图"，
这是 B1/B2 断点的落地方案。trace 记录事件顺序供单测快照断言。
"""
from __future__ import annotations

from hooks import PortCtx
from harness.memory import MemoryStore
from harness.rag.types import TokenBudget
from runtime import AgentState, Message

from .assembler import ContextAssembler, DefaultContextAssembler
from .compaction import CompactionPolicy, DefaultCompactionPolicy
from .offloader import BlobPort, DefaultOffloader, InMemoryBlobStore, Offloader
from .types import ContextSection

__all__ = ["ContextMemoryManager"]

# trace 事件名（具名常量）。
_TRACE_WRITE_COLD = "write_cold"
_TRACE_COMPACT = "compact"


class ContextMemoryManager:
    """R7 统一编排器：显式调用各能力，装配与顺序都在本类。"""

    def __init__(
        self,
        assembler: ContextAssembler | None = None,
        compaction: CompactionPolicy | None = None,
        offloader: Offloader | None = None,
        memory: MemoryStore | None = None,
        blob: BlobPort | None = None,
    ) -> None:
        """构造编排器；None 的依赖用默认实现（开箱可用）。

        Args:
            assembler: 上下文组装器。
            compaction: 压缩策略。
            offloader: 大结果卸载器。
            memory: 记忆存储。
            blob: 卸载存储后端。
        """
        self.assembler = assembler if assembler is not None else DefaultContextAssembler()
        self.compaction = compaction if compaction is not None else DefaultCompactionPolicy()
        self.offloader = offloader if offloader is not None else DefaultOffloader()
        self.memory = memory if memory is not None else MemoryStore()
        self.blob = blob if blob is not None else InMemoryBlobStore()
        self.trace: list[str] = []

    def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:
        """显式组装上下文（替代原 before_model hook）。"""
        return self.assembler.assemble(sections, budget)

    def commit_and_compact(
        self, state: AgentState, transcript: str, ctx: PortCtx | None = None
    ) -> tuple[Message, ...]:
        """提交会话：先写 cold 全文，再压缩产视图（顺序不可反转）。

        Args:
            state: 当前状态（仅读取，不修改 messages）。
            transcript: 本轮完整对话记录（写 cold 全文，避免细节丢失）。
            ctx: Port 上下文；None 时用空上下文。

        Returns:
            压缩后的消息视图（新 tuple，State.messages 不变）。
        """
        port_ctx = ctx if ctx is not None else PortCtx()
        # 顺序由代码结构固定：① 先写 cold 全文（细节必须先落盘才能提炼）。
        self.memory.write_cold(transcript, port_ctx)
        self.trace.append(_TRACE_WRITE_COLD)
        # ② 再压缩历史产视图（不改 State.messages）。
        view = self.compaction.compact(state.messages)
        self.trace.append(_TRACE_COMPACT)
        return view

    async def recall(self, query: object, ctx: PortCtx | None = None) -> list[object]:
        """显式语义召回（替代原 before_model 记忆注入 hook）。"""
        from harness.memory.types import MemoryQuery

        if not isinstance(query, MemoryQuery):
            raise TypeError("recall expects MemoryQuery")
        return list(await self.memory.recall(query, ctx if ctx is not None else PortCtx()))

    async def offload(self, value: object, ctx: PortCtx | None = None) -> str:
        """显式大结果卸载，返回可读句柄（替代原 after_tool hook）。"""
        return await self.offloader.offload(value, self.blob)

    def with_(
        self,
        assembler: ContextAssembler | None = None,
        compaction: CompactionPolicy | None = None,
        offloader: Offloader | None = None,
        memory: MemoryStore | None = None,
        blob: BlobPort | None = None,
    ) -> ContextMemoryManager:
        """产新实例替换指定依赖（不可变风格，仿 R6）。

        Args:
            assembler: 新组装器；None 表示沿用当前实例。
            compaction: 新压缩策略；None 表示沿用。
            offloader: 新卸载器；None 表示沿用。
            memory: 新记忆存储；None 表示沿用。
            blob: 新卸载存储；None 表示沿用。

        Returns:
            替换依赖后的新实例；当前实例不变。
        """
        return ContextMemoryManager(
            assembler=assembler if assembler is not None else self.assembler,
            compaction=compaction if compaction is not None else self.compaction,
            offloader=offloader if offloader is not None else self.offloader,
            memory=memory if memory is not None else self.memory,
            blob=blob if blob is not None else self.blob,
        )
