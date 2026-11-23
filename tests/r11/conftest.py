"""R11 测试夹具：确定性 arch/capability/governance 替身，全本地无 IO。"""
from __future__ import annotations

from archs.base import StaticCheckReport
from runtime import Terminate


class StubArch:
    """最小 Arch：compile 产 Terminate(output=tag)。"""
    name = "stub_arch"
    version = "1.0.0"

    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag

    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(arch=self.name, version=self.version)

    def compile(self):
        def program():
            yield Terminate(output=self.tag)
            return None
        return program()
