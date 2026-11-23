"""每次调用的语义输入（ContextVar 隔离并发，R11 门面内部机制）。

设计（Round2 Blk-3/4 根因修复）：
- 输入不再写 `arch.input_text` 共享实例属性（多线程 say 会互相覆盖）；
- 用 ContextVar 按调用链隔离：每个 say()/asay() 调用 set 自己的输入，架构 compile/execute
  时用 `current_input()` 读取，天然线程安全、async 任务安全。
- 位于 compose 层（archs 与 api 都可依赖）：ReAct 与用户自定义架构用 current_input() 读；
  语义门面 say()/asay() 自动 set；直接 Runner.run 的用户用 bind_input(text) 显式设置。

只暴露"当前输入文本"一个语义量，不暴露 Effect/message/payload。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

__all__ = ["current_input", "bind_input"]

_input_var: ContextVar[str] = ContextVar("agentkit_semantic_input", default="")


def current_input() -> str:
    """当前调用链的输入文本（架构内部读取；每线程/任务隔离）。"""
    return _input_var.get()


@contextmanager
def bind_input(text: str) -> Iterator[None]:
    """绑定本次调用输入（门面内部自动用；直接 Runner.run 的用户可显式用）。"""
    token: Token[str] = _input_var.set(text)
    try:
        yield
    finally:
        _input_var.reset(token)
