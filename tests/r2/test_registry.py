"""R2-T07：Registry 缺 provider 报错、重名报错、替换、freeze。"""
import pytest

from hooks import Registry, HookBusError


class P:
    def __init__(self, name):
        self.name = name


def test_register_resolve():
    r = Registry()
    r.register("llm", P("llm"))
    assert r.resolve("llm").name == "llm"


def test_missing_raises():
    r = Registry()
    with pytest.raises(HookBusError):
        r.resolve("nope")


def test_duplicate_raises():
    r = Registry()
    r.register("x", P("x"))
    with pytest.raises(HookBusError):
        r.register("x", P("x"))


def test_replace():
    r = Registry()
    r.register("x", P("a"))
    r.replace("x", P("b"))
    assert r.resolve("x").name == "b"


def test_freeze_blocks_mutation():
    r = Registry()
    r.register("x", P("x")).freeze()
    with pytest.raises(HookBusError):
        r.register("y", P("y"))
