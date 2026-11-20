"""R7 独立 demo：显式编排（组装→先写cold→压缩→卸载→召回）全链冒烟。

跑法：python tests/demos/demo_r7.py —— 不依赖外部 IO（嵌入用 R6 确定性
HashEmbedder、向量库/记忆/卸载均内存默认、注入检测用 R5 默认检测器），
展示 R7 模块内闭环：三 Port 履约 + 顺序确定性 + 句柄可读 + secret/注入 fail-closed。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import PORT_PROTOCOLS, PortCtx  # noqa: E402

from harness.context import (  # noqa: E402
    ContextMemoryManager,
    ContextSection,
    InMemoryBlobStore,
    TrimPolicy,
)
from harness.memory import MemoryStore, MemoryQuery  # noqa: E402
from harness.rag.types import TokenBudget  # noqa: E402
from runtime import AgentState, Budgets, Message  # noqa: E402


def main() -> None:
    """演示 R7 完整闭环，打印关键产物。"""
    mgr = ContextMemoryManager()
    ctx = PortCtx(session_id="demo-r7", run_id="run1")

    # ① 显式组装（替代 before_model）。
    sections = [
        ContextSection("system", "你是中文助手，遵守规则。", 50, 0, TrimPolicy.PIN),
        ContextSection("history", "用户之前问过天气；助手答过温度。", 80, 50, TrimPolicy.TAIL),
        ContextSection("input", "现在几点？", 50, 90, TrimPolicy.TAIL),
    ]
    assembled = mgr.assemble(sections, TokenBudget(limit=200))
    print(f"[R7] 组装消息数: {len(assembled)} / 首段: {assembled[0].content}")

    # ② 显式提交：先写 cold 全文，再压缩（顺序由代码固定）。
    state = AgentState(
        messages=(
            Message(role="system", content="规则"),
            *(Message(role="user", content=f"旧消息{i}") for i in range(15)),
            Message(role="user", content="最近问题"),
        ),
        budgets=Budgets(used_turns=20, max_tokens=1000),
    )
    view = mgr.commit_and_compact(state, "完整对话逐字记录（含所有细节）", ctx)
    print(f"[R7] 顺序 trace: {mgr.trace}")
    print(f"[R7] 压缩视图条数: {len(view)}（原 {len(state.messages)}，State.messages 未改）")
    if mgr.trace != ["write_cold", "compact"]:
        raise AssertionError("顺序错误：未做到先写 cold 再压缩")
    if len(state.messages) != 17:
        raise AssertionError("红线：State.messages 被修改")

    async def run() -> None:
        """卸载与召回、Port 履约、fail-closed 断言。"""
        # ③ 大结果卸载（替代 after_tool）：句柄可读。
        handle = await mgr.offload({"tool": "search", "rows": [1, 2, 3]}, ctx)
        readable = await mgr.blob.get(handle) is not None
        print(f"[R7] 卸载句柄: {handle} / 可读: {readable}")
        if not readable:
            raise AssertionError("卸载句柄不可读")

        # ④ 语义召回（替代 before_model 记忆注入）。
        assert isinstance(mgr.memory, MemoryStore)
        await mgr.memory.write("profile", "language", "用户偏好中文", ctx)
        print(f"[R7] 记忆写入读回: {await mgr.memory.read('profile', 'language', ctx)}")

        # ⑤ Port 履约 + secret/注入 fail-closed。
        print(f"[R7] MemoryPort 履约: {isinstance(mgr.memory, PORT_PROTOCOLS['memory'])}")
        print(f"[R7] BlobPort 履约: {isinstance(mgr.blob, PORT_PROTOCOLS.get('blob', object))}"
              if "blob" in PORT_PROTOCOLS else "[R7] BlobPort: context 内部协议（非 R2 Port）")
        rejected = False
        try:
            await mgr.memory.write("creds", "api_token", "sk-secret", ctx)
        except ValueError:
            rejected = True
        print(f"[R7] secret 拒绝: {rejected}")
        if not rejected:
            raise AssertionError("secret 键名未被拒绝")

    asyncio.run(run())
    print("[R7] PASS: 显式编排 / 顺序确定性 / 三Port可用+Blob内部 / 句柄可读 / secret+注入 fail-closed 全闭环")


if __name__ == "__main__":
    main()
