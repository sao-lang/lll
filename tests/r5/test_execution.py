"""G02：code/shell/file 执行工具——R5 侧白名单/虚拟根校验 + SandboxPort 路由。

R5 职责：工具面、命令白名单、路径白名单、danger 分级；R9 职责：进程隔离（SandboxPort）。
缺席即拒绝：无 sandbox 时返回失败信封，不裸奔直执行。
"""
from __future__ import annotations

import asyncio

from compose import DangerLevel, PortCtx, ToolResult
from harness.tools import CodeExecTool, FileExecTool, ShellExecTool

from .conftest import FakeSandbox


def test_shell_whitelist_accepts_allowed() -> None:
    """shell 白名单放行：allowed 命令才交给 sandbox。"""
    sandbox = FakeSandbox()
    tool = ShellExecTool(sandbox=sandbox, allowlist=("ls", "cat"))
    result = asyncio.run(tool.execute("ls -la", PortCtx()))
    assert result and sandbox.calls == ["run"]


def test_shell_whitelist_rejects_dangerous() -> None:
    """shell 危险命令拦截：未白名单命令不进 sandbox（致命动作零执行）。"""
    sandbox = FakeSandbox()
    tool = ShellExecTool(sandbox=sandbox, allowlist=("ls",))
    result = asyncio.run(tool.execute("rm -rf /", PortCtx()))
    assert not result and sandbox.calls == []


def test_code_exec_routes_through_sandbox() -> None:
    """代码执行经 sandbox；无 sandbox 返回失败（缺席即拒绝）。"""
    sandbox = FakeSandbox()
    tool = CodeExecTool(sandbox=sandbox)
    assert asyncio.run(tool.execute("print(1)", PortCtx()))
    assert asyncio.run(CodeExecTool().execute("print(1)", PortCtx())) is False


def test_file_virtual_root_blocks_traversal() -> None:
    """文件工具虚拟根：../ 越界路径拒绝，不进 sandbox。"""
    sandbox = FakeSandbox()
    tool = FileExecTool(sandbox=sandbox, root="/workspace")
    assert not asyncio.run(tool.read("../../etc/passwd", PortCtx()))
    assert sandbox.calls == []


def test_file_within_root_is_allowed() -> None:
    """虚拟根内路径放行并交给 sandbox 执行。"""
    sandbox = FakeSandbox()
    tool = FileExecTool(sandbox=sandbox, root="/workspace")
    assert asyncio.run(tool.read("data.txt", PortCtx()))
    assert sandbox.calls == ["run"]


def test_exec_tools_are_high_danger() -> None:
    """执行类工具 danger=HIGH，强制走沙盒路由（设计 D10~D12 形态）。"""
    from pathlib import Path
    assert CodeExecTool(sandbox=FakeSandbox()).danger is DangerLevel.HIGH
    assert ShellExecTool(sandbox=FakeSandbox(), allowlist=("ls",)).danger is DangerLevel.HIGH
    assert FileExecTool(sandbox=FakeSandbox(), root="/w").danger is DangerLevel.HIGH


def test_sandbox_exception_folded() -> None:
    """沙盒执行抛异常 → 折叠为失败（不抛穿），工具返回 False。"""
    class BoomSandbox(FakeSandbox):
        async def run(self, fn, args, ctx):  # type: ignore[no-untyped-def]
            raise RuntimeError("sandbox exploded")

    tool = CodeExecTool(sandbox=BoomSandbox())
    assert asyncio.run(tool.execute("x", PortCtx())) is False


def test_file_empty_root_refuses() -> None:
    """虚拟根为空（未配置）→ 拒绝一切文件访问（fail closed）。"""
    tool = FileExecTool(sandbox=FakeSandbox(), root="")
    assert asyncio.run(tool.read("anything", PortCtx())) is False
