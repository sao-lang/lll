"""记忆管理真实 e2e：store.write/recall 真入口 + Agent.asay 门面注入。

反证变异点：
- MEM-01 ↔ memory.py:160 ingest（删则 recall 空）
- MEM-10 ↔ agent.py:520 memory_text（删则 LLM system 无召回文本）
- MEM-11 ↔ agent.py:757 write_after（删则跨轮记忆断裂）
"""
from __future__ import annotations

import asyncio

import pytest

from compose import PortCtx
from harness.context.manager import ContextMemoryManager
from harness.memory.memory import MemoryStore
from harness.memory.types import MemoryQuery, MemoryScope
from harness.model.capability import ModelCapability

from conftest import run
from impls.recording_llm import RecordingLLM
from impls.user_memory import UserMemory


def _ctx(tenant: str = "t1") -> PortCtx:
    return PortCtx(session_id="e2e-mem", run_id="run-mem",
                   meta={"scope": MemoryScope(tenant=tenant)})


def test_mem01_write_recall_same_channel(loop: asyncio.AbstractEventLoop) -> None:
    """write 的内容必须经 ingest 进召回链；只写 KV 会让 recall 空。"""
    async def scenario() -> None:
        store = MemoryStore()
        value = "用户偏好中文回答"
        await store.write("prefs", "lang", value, _ctx())
        assert await store.read("prefs", "lang", _ctx()) == value
        recalled = await store.recall(MemoryQuery(text="用户偏好中文回答"), _ctx())
        assert len(recalled) >= 1, "write 的值必须能被 recall 召回（ingest 断链则空）"
        assert any(value in item.content for item in recalled), "召回项须含写入原文"
        assert store.tier_count() >= 1, "ingest 后温度层须有条目"
    run(scenario(), loop)


def test_mem03_secret_key_rejected(loop: asyncio.AbstractEventLoop) -> None:
    """secret 键名必须 fail-closed 拒绝且不落库。"""
    async def scenario() -> None:
        store = MemoryStore()
        with pytest.raises(ValueError, match="secret key rejected"):
            await store.write("api_key", "k", "sk-should-never-land", _ctx())
        assert await store.read("api_key", "k", _ctx()) is None
        assert store.tier_count() == 0
    run(scenario(), loop)


def test_mem08_tenant_isolation(loop: asyncio.AbstractEventLoop) -> None:
    """tenant 过滤必须真隔离；越权 query 必须 fail-closed。"""
    async def scenario() -> None:
        store = MemoryStore()
        await store.write("note", "t1", "t1专属内容alpha", _ctx("t1"))
        await store.write("note", "t2", "t2专属内容beta", _ctx("t2"))
        r1 = await store.recall(MemoryQuery(text="专属内容"), _ctx("t1"))
        assert all("t1专属内容alpha" in i.content or "t2专属内容beta" in i.content for i in r1)
        assert any("t1专属内容alpha" in i.content for i in r1), "t1 须召回自己的内容"
        assert not any("t2专属内容beta" in i.content for i in r1), "t1 不得召回 t2 内容"
        with pytest.raises(ValueError, match="tenant mismatch"):
            await store.recall(MemoryQuery(text="专属内容", scope=MemoryScope(tenant="t2")), _ctx("t1"))
    run(scenario(), loop)


def test_mem10_facade_recall_injected_into_llm(loop: asyncio.AbstractEventLoop) -> None:
    """门面 asay 必须把 recall 的文本注入 LLM 实收的 system messages。"""
    from api import Agent
    from harness.context.capability import MemoryCapability

    async def scenario() -> None:
        store = MemoryStore()
        await store.write("prefs", "lang", "用户偏好中文回答", PortCtx(session_id="e2e-mem"))
        llm = RecordingLLM(answer="好的")
        arch = _arch()
        agent = Agent(arch).use(ModelCapability(llm=llm)).use(
            MemoryCapability(manager=ContextMemoryManager(memory=store))
        )
        await agent.asay("我的偏好是什么")
        assert llm.complete_calls >= 1, "LLM 必须真被调用"
        system = llm.system_text()
        assert "相关记忆：用户偏好中文回答" in system, f"LLM 实收 system 须含召回文本，实际={system!r}"
    run(scenario(), loop)


def test_mem11_facade_write_after_cross_turn(loop: asyncio.AbstractEventLoop) -> None:
    """门面 asay 必须把本轮写入长期记忆，下一轮能召回。"""
    from api import Agent
    from harness.context.capability import MemoryCapability

    async def scenario() -> None:
        store = MemoryStore()
        llm = RecordingLLM(answer="记住了")
        agent = Agent(_arch()).use(ModelCapability(llm=llm)).use(
            MemoryCapability(manager=ContextMemoryManager(memory=store))
        )
        await agent.asay("请记住我喜欢蓝色")
        assert store.tier_count() > 0, "write_after 必须写入长期记忆"
        r = await store.recall(MemoryQuery(text="喜欢蓝色"), PortCtx(session_id="e2e-mem"))
        assert any("喜欢蓝色" in i.content for i in r), "写入内容须可召回"
        await agent.asay("我喜欢什么颜色")
        system = llm.system_text()
        assert "喜欢蓝色" in system, f"第二轮 LLM system 须含首轮记忆，实际={system!r}"
    run(scenario(), loop)


def test_mem12_custom_memory_backend_injected(loop: asyncio.AbstractEventLoop) -> None:
    """用户自定义记忆后端必须被门面真消费：计数>0 且内置 store 零落库。"""
    from api import Agent
    from harness.context.capability import MemoryCapability

    async def scenario() -> None:
        custom = UserMemory()
        llm = RecordingLLM(answer="好的")
        agent = Agent(_arch()).use(ModelCapability(llm=llm)).use(
            MemoryCapability(manager=ContextMemoryManager(memory=custom))  # type: ignore[arg-type]
        )
        await agent.asay("请记住我喜欢蓝色")
        await agent.asay("我喜欢什么颜色")
        assert custom.ingest_calls > 0, "自定义后端的 ingest 必须被真调用"
        assert custom.recall_calls > 0, "自定义后端的 recall 必须被真调用"
        assert "喜欢蓝色" in llm.system_text(), "第二轮 system 须含自定义后端召回的文本"
    run(scenario(), loop)


class MinimalArch:
    """最小 Arch：compile 产一个 yield llm Invoke 的 Program（真走 llm 端口）。"""

    name = "e2e_min_arch"
    version = "1.0.0"

    def static_check(self) -> object:
        from archs.base import StaticCheckReport
        return StaticCheckReport(errors=(), arch=self.name, version=self.version,
                                 ports_required=("llm",), budgets_resolved={})

    def compile(self) -> object:
        from runtime import Invoke, Terminate
        from compose import current_input

        def program():
            text = current_input()
            messages = [{"role": "user", "content": text}]
            out = yield Invoke(port="llm", payload={"layer": "model", "messages": messages})
            yield Terminate(output=out)
            return out
        return program()


def _arch(seq_fn: object = None) -> MinimalArch:
    return MinimalArch()
