"""默认人工在环适配器：终端问答（生产环境替换为 IM / 审批流实现）。"""
from __future__ import annotations

from ..registry import PortCtx

__all__ = ["ConsoleHuman"]


class ConsoleHuman:
    name = "console-human"

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        suffix = f" [{'/'.join(options)}]" if options else ""
        return input(f"{question}{suffix}\n> ")
