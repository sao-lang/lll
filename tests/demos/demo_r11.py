"""R11 独立闭环 demo：profile 装配 + 治理 + CLI 四命令 + 事件流投影。

运行：.venv/Scripts/python tests/demos/demo_r11.py
不联网、不真实 IO；治理/CLI 均为本地确定性实现。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api import AgentBuilder, CLI, Governance, StreamGateway  # noqa: E402
from archs.base import StaticCheckReport  # noqa: E402
from harness.observability import EventEnvelope, TimelineProjection  # noqa: E402


class _StubArch:
    """最小 Arch（demo 本地定义，不依赖 tests）。"""
    name = "demo_arch"
    version = "1.0.0"

    def static_check(self) -> StaticCheckReport:
        """返回 ok 报告。"""
        return StaticCheckReport(arch=self.name, version=self.version)

    def compile(self):
        """产出 Program。"""
        def program():
            from runtime import Terminate

            yield Terminate(output="ok")
            return None
        return program()


def main() -> None:
    """演示四组 API 面与治理闭环。"""
    b = AgentBuilder(_StubArch()).profile("production")
    print(f"[R11] profiles={b.applied_profiles()} caps={b.capability_names()}")
    print(f"[R11] R9 ports sandbox={b.registry().has('sandbox')} "
          f"audit={b.registry().has('audit')} human={b.registry().has('human')}")

    gov = Governance(cost_cap=1.0)
    print(f"[R11] gov allow={gov.allow('tool.call', cost=0.1, key='u1')}")

    gw = StreamGateway()
    gw.publish(EventEnvelope(seq=1, topic="trace.span", ts=1.0,
                             type="span", data={"name": "llm"}))
    model = gw.project(TimelineProjection(), "trace.span")
    print(f"[R11] projection={model.name} count={model.data['count']}")

    cli = CLI()
    print(f"[R11] cli commands={cli.commands()}")
    print("[R11] PASS: profile 装配 / R9 三 Port / 治理 / 事件流投影 / CLI 四命令闭环")


if __name__ == "__main__":
    main()
