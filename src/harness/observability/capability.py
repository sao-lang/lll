"""R10 可观测能力包：持 telemetry，供门面 trace 使用（可插拔后端）。

接线边界：
- 能力包 = 组合根：用户注入 telemetry（可替换为 OTel 适配）；
- 不重写 Trace 的 span/metric/log 语义；门面 trace 属性从能力包取 telemetry。
- Trace 依赖 telemetry._register/_replace 内部注册协议（NoopTelemetry 实现），
  注入实现须提供这两个方法——是本框架可观测后端的明确契约。
"""
from __future__ import annotations

from compose import Provider, Registry
from compose.ports import TelemetryPort
from harness.observability.telemetry import NoopTelemetry

__all__ = ["ObservabilityCapability"]


class ObservabilityCapability:
    """可观测能力包：持有 telemetry（缺省 NoopTelemetry）。"""

    name = "observability"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, *, telemetry: TelemetryPort | object | None = None) -> None:
        self.telemetry = telemetry if telemetry is not None else NoopTelemetry()

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:
        _ = container
        if reg is not None:
            reg.register("telemetry", self.telemetry)  # type: ignore[arg-type]  # purity-exempt: telemetry 鸭子履约
