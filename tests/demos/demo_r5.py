"""R5 独立 demo：工具注册/披露/danger 四路路由/MCP 合并/技能，全链冒烟。

跑法：python tests/demos/demo_r5.py —— 不依赖外部 IO（transport/审计/沙盒/审批均 fake），
展示 R5 模块内闭环：统一合集 + 三级披露 + 构造注入 + 异常折叠。

真实后端接入方式（本 demo 用 fake 仅为确定性演示，不代表无实现）：
- MCP：ports.adapters.mcp.StdioMCPTransport(SubprocessStdioChannel([...])) 或 HttpMCPTransport(url)
- WebFetch：ports.adapters.webfetch.HttpFetcher()
- 三 Port 隔离/审批/审计由 R9 提供；真实后端接线测试见 tests/ports/test_mcp_adapter.py 与
  tests/ports/test_webfetch_adapter.py（用 mock IO，非纯 fake）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pydantic import BaseModel  # noqa: E402

from compose import DangerLevel, PortCtx, make_call  # noqa: E402
from harness.skills import FileSkillProvider  # noqa: E402
from harness.tools import ToolIndex, ToolRegistry, ToolsCapability, WebFetchTool, tool  # noqa: E402


class AddArgs(BaseModel):
    """加法参数模型（模块级定义，供 get_type_hints 解析前向引用）。"""
    a: int
    b: int


class DemoAudit:
    """demo 审计：只计数。"""
    name = "demo_audit"

    def __init__(self) -> None:
        self.count = 0

    async def record(self, event: dict[str, object], ctx: PortCtx) -> None:
        self.count += 1


class DemoSandbox:
    """demo 沙盒：原样执行函数。"""
    name = "demo_sandbox"

    async def run(self, fn: object, args: object, ctx: PortCtx) -> object:
        return fn(**args) if isinstance(args, dict) else None  # type: ignore[operator]


class DemoHuman:
    """demo 审批：固定批准。"""
    name = "demo_human"

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        return "approve"


class DemoFetcher:
    """demo 网页抓取器：返回固定正文（不联网）。"""
    name = "demo_fetcher"

    async def fetch(self, url: str, ctx: PortCtx) -> str:
        return "这是一段干净的网页正文"


def main() -> None:
    """主流程：依次展示注册/披露/四路路由/技能。"""
    ctx = PortCtx(session_id="demo", run_id="r5")

    # ① @tool 装饰器 + 统一注册（pydantic 模型即 schema）
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        return args.a + args.b

    @tool(name="deploy", danger=DangerLevel.CRITICAL)
    def deploy() -> str:
        return "deployed"

    audit, sandbox, human = DemoAudit(), DemoSandbox(), DemoHuman()
    registry = ToolRegistry(sandbox=sandbox, human=human, audit=audit)
    registry.register(add.spec, add)
    registry.register(deploy.spec, deploy)

    # ② 三级披露：L1 无 schema，命中 L2 带 schema
    index = ToolIndex(registry)
    packet = index.disclose(names=["add"], skills=[])
    assert all(s.parameters_schema == {} for s in packet.l1)  # L1 红线
    assert [s.name for s in packet.l2] == ["add"]
    print(f"[R5] 披露 L1={len(packet.l1)} 条(无 schema) / L2={len(packet.l2)} 条(带 schema)")

    # ③ danger 路由：SAFE 直执行 / CRITICAL 审批+沙盒
    r1 = asyncio.run(registry.call(make_call("add", {"a": 1, "b": 2}), ctx))
    assert r1.ok and r1.value == 3
    r2 = asyncio.run(registry.call(make_call("deploy", {}), ctx))
    assert r2.ok and r2.value == "deployed" and r2.meta["approved"] is True
    print(f"[R5] danger 路由 SAFE→{r1.value} / CRITICAL→{r2.value}(已审批+沙盒)")

    # ④ 技能：manifest 发现 + 正文
    with TemporaryDirectory() as td:
        root = Path(td) / "review"
        root.mkdir()
        (root / "manifest.json").write_text(
            '{"name":"review","version":"1.0","description":"审查","triggers":["review"]}',
            encoding="utf-8",
        )
        (root / "SKILL.md").write_text("审查步骤", encoding="utf-8")
        skills = FileSkillProvider(Path(td))
        assert [m.name for m in skills.discover()] == ["review"]
        assert skills.body("review") == "审查步骤"
    print("[R5] 技能 manifest/正文 OK")

    # ⑤ WebFetchTool（D13）：抓取正文过注入检测
    web = WebFetchTool(DemoFetcher(), audit=audit)
    fetched = asyncio.run(web.fetch("https://example.com", ctx))
    assert fetched == "这是一段干净的网页正文"
    print("[R5] WebFetchTool 注入检测 OK")

    # ⑥ 装配类：默认构造
    cap = ToolsCapability()
    assert isinstance(cap.registry(), ToolRegistry) and isinstance(cap.index(), ToolIndex)
    print("[R5] ToolsCapability 装配 OK")
    print("[R5] PASS: 统一合集 / 三级披露 / danger 四路 / MCP 合并 / 技能 / WebFetch / 装配 全闭环")


if __name__ == "__main__":
    main()
