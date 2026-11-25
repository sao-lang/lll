"""RED：Registry.replace 不得静默创建不存在的 name。

裂缝：register docstring 承诺"重名报错（除非用 replace）"——register 创建、
replace 更新。但 replace 实现直接 `self._providers[name] = provider`，对不存在
的 name 静默新建，让拼写错误的 replace 变成静默成功而非报错（契约裂缝）。
修后：name 未注册则 HookBusError，只有 register 能创建。
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import HookBusError, Registry


class _P:
    pass


def test_red_replace_missing_name_rejected() -> None:
    reg = Registry()
    try:
        reg.replace("ghost", _P())
    except HookBusError as exc:
        assert "not registered" in str(exc).lower() or "ghost" in str(exc), \
            f"应报未注册错误，实际 {exc}"
    else:
        raise AssertionError("replace 不存在的 name 静默成功，应报错")
    assert not reg.has("ghost"), "replace 失败不得留下注册项"


def test_red_replace_existing_name_works() -> None:
    reg = Registry()
    old, new = _P(), _P()
    reg.register("t", old)
    assert reg.replace("t", new) is reg, "replace 应返回 self 支持链式"
    assert reg.resolve("t") is new, "replace 应真替换"
