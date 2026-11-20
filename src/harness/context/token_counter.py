"""R7 统一 token 计数：assemble 与 compaction 共用的唯一计数口径。

消除旧实现 len(str) 与 len/4 两套口径并存的失真；默认用 Unicode 码点估算，
中文/多字节不再按字节折算。可注入真实 tokenizer 适配器。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["DefaultTokenCounter", "TokenCounter", "estimate_tokens"]


@runtime_checkable
class TokenCounter(Protocol):
    """token 计数协议（context 包内部协议，不进 PORT_PROTOCOLS）。"""

    name: str

    def count(self, text: str) -> int:
        """返回文本的 token 估算数。"""
        ...


def estimate_tokens(text: str) -> int:
    """统一 token 估算：非空文本按 Unicode 字符数计，向上取整到 token。

    Args:
        text: 待计数文本。

    Returns:
        token 数；空串返回 0。
    """
    return len(text)


class DefaultTokenCounter:
    """默认 token 计数器：单一口径，可被真实 tokenizer 替换。"""

    name: str = "default_token_counter"

    def count(self, text: str) -> int:
        """按统一口径计数。

        Args:
            text: 待计数文本。

        Returns:
            token 估算数。
        """
        return estimate_tokens(text)
