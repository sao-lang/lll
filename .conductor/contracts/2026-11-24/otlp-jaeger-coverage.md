# 需求覆盖矩阵 · 接入 Jaeger/OTLP 链路追踪（2026-11-24）

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R1 接真实平台（用户选 Jaeger/OTLP） | 采用：examples 层 OTLP/HTTP telemetry 注入 | FR-1 | AC-2/5 |
| R2 用框架内置能力包 | 采用：ObservabilityCapability(telemetry=) 现成口子（B3 门禁路径） | FR-2 | AC-2 |
| R3 span 数据真实 | 采用：TraceSpan 真值转 OTLP（name/duration/traceId/component） | FR-3 | AC-3 |
| R4 上报失败不影响聊天 | 采用：TelemetryPort 通知语义，失败内部消化+计数 | FR-4 | AC-4 |
| R5 离线零外部依赖 | 采用：无 endpoint → NoopTelemetry，无网络 IO | FR-5 | AC-1 |
| R6 不引入新依赖 | 采用：httpx（已有）手写 OTLP JSON；pyproject 零改 | FR-6 | AC-6 |
| R7 平台可查看 | 采用：Jaeger all-in-one（Docker）+ UI :16686 | FR-7 | AC-5 |
| R8 src 零改 | 采用：全部在 examples 层 | FR-8 | AC-6 |
| R9 无死代码 | 采用：PhaseBridge 作废清理；exporter 有真消费者 | FR-9 | AC-6 |

FR 数 = 9。
