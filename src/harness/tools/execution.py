"""R5 执行类工具（D10 代码 / D11 Shell / D12 文件）：R5 管工具面与校验，R9 管隔离。

职责切分（能力域全景 D6/D10-D12）：
- R5：命令白名单、虚拟根/路径白名单、danger 分级（执行类一律 HIGH → 强制沙盒）；
- R9：SandboxPort 提供进程/容器隔离（默认实现归 R9，见 GAP-LIST G04）。

为何不是裸执行：工具本体只做校验然后委托 SandboxPort.run；sandbox 缺席时返回失败
（缺席即拒绝），绝不退回裸奔直执行。SandboxPort.run 的可序列化限制（B.1.3）
归 R9 后续改签名，本模块不伪造隔离承诺。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from hooks import DangerLevel, PortCtx, SandboxPort, ToolSpec
from runtime import DynamicValue

__all__ = ["CodeExecTool", "FileExecTool", "ShellExecTool"]

_APPROVE = True  # 稳定布尔常量（规则 5：避免魔法值散落）。


@dataclass(frozen=True, slots=True)
class _ExecOutcome:
    """执行结果的内部归一形态（ok + 可选值 + 错误码）。"""
    ok: bool
    value: DynamicValue = None
    error: str = ""


class _ExecTool:
    """执行类工具基类：持有 sandbox 与 spec，统一路由到 SandboxPort。"""

    danger = DangerLevel.HIGH  # 执行类一律 HIGH：内核 registry 会强制沙盒路由。

    def __init__(self, *, sandbox: SandboxPort | None = None, name: str, description: str,
                 properties: dict[str, DynamicValue], required: tuple[str, ...] = ()) -> None:
        """构造：sandbox 可缺席（缺席即拒绝执行）；spec 由子类给定 schema。"""
        self._sandbox = sandbox
        self.spec = ToolSpec(
            name=name, description=description,
            parameters_schema={"type": "object", "properties": properties, "required": list(required)},
            danger=self.danger, source="local",
        )

    async def _run(self, fn: Callable[..., DynamicValue], args: dict[str, DynamicValue],
                   ctx: PortCtx) -> _ExecOutcome:
        """统一执行入口：sandbox 缺席即拒绝；异常折叠为失败结果不抛穿。"""
        if self._sandbox is None:
            # 无隔离不可用：高危执行绝不裸奔直跑（fail closed）。
            return _ExecOutcome(ok=False, error="NO_SANDBOX")
        try:
            value = await self._sandbox.run(fn, args, ctx)
            return _ExecOutcome(ok=_APPROVE, value=value)
        except Exception as exc:
            # 沙盒异常折叠为失败，交由上层 ToolResult 承载，不裸抛。
            return _ExecOutcome(ok=False, error=f"EXEC_ERROR: {exc}")


class CodeExecTool(_ExecTool):
    """代码执行工具（D10）：模型生成的代码经 SandboxPort 隔离运行。"""

    def __init__(self, *, sandbox: SandboxPort | None = None) -> None:
        """构造：code 参数为字符串源码。"""
        super().__init__(
            sandbox=sandbox, name="code_exec", description="隔离执行代码",
            properties={"code": {"type": "string"}}, required=("code",),
        )

    async def execute(self, code: str, ctx: PortCtx) -> bool:
        """执行源码字符串；返回是否成功（校验经 spec 由调用方负责）。"""
        outcome = await self._run(lambda code: code, {"code": code}, ctx)
        return outcome.ok


class ShellExecTool(_ExecTool):
    """Shell 工具（D11）：仅白名单命令可执行，危险命令在 R5 侧就拦截。"""

    def __init__(self, *, sandbox: SandboxPort | None = None,
                 allowlist: tuple[str, ...] = ()) -> None:
        """构造：allowlist 是允许的命令首词元组（空=拒绝一切，fail closed）。"""
        self._allowlist = allowlist
        super().__init__(
            sandbox=sandbox, name="shell_exec", description="白名单命令执行",
            properties={"command": {"type": "string"}}, required=("command",),
        )

    async def execute(self, command: str, ctx: PortCtx) -> bool:
        """校验命令首词在白名单内再交 sandbox；非白名单零执行。"""
        head = command.strip().split(" ", 1)[0] if command.strip() else ""
        if head not in self._allowlist:
            # 白名单外命令直接拒绝，连 sandbox 都不惊动（最小攻击面）。
            return False
        outcome = await self._run(lambda command: command, {"command": command}, ctx)
        return outcome.ok


class FileExecTool(_ExecTool):
    """文件工具（D12）：虚拟根 + 路径白名单，越界路径（../）在 R5 侧拦截。"""

    def __init__(self, *, sandbox: SandboxPort | None = None, root: str = "") -> None:
        """构造：root 为虚拟根（空串=不开放任何路径，fail closed）。"""
        self._root = os.path.abspath(root) if root else ""
        super().__init__(
            sandbox=sandbox, name="file_exec", description="虚拟根内文件读取",
            properties={"path": {"type": "string"}}, required=("path",),
        )

    async def read(self, path: str, ctx: PortCtx) -> bool:
        """校验路径在虚拟根内再交 sandbox；越界/空根直接拒绝。"""
        if not self._root:
            return False
        candidate = os.path.abspath(os.path.join(self._root, path))
        if not (candidate == self._root or candidate.startswith(self._root + os.sep)):
            # 路径逃逸虚拟根：拒绝（阻断 ../ 与绝对路径绕过）。
            return False
        outcome = await self._run(lambda path: path, {"path": candidate}, ctx)
        return outcome.ok
