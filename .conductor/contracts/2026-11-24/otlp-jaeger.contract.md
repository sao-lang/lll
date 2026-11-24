# change-contract · 接入真实链路追踪 Jaeger/OTLP（2026-11-24，场景4 full）

## Why
用户要求"接进来真实的链路追踪，我要用平台看到"，并明确选择 **Jaeger/OTLP 标准栈**。框架内置 `TelemetryExporter` 协议零实现零消费者（台账 A5 不修），无 OTel 依赖，规格无接平台 D 决策——不能"注入现成 exporter"。真正现成的口子是 `ObservabilityCapability(telemetry=注入)`（B3 门禁验证真生效）：写一个履约 `TelemetryPort` + 框架私有 `_register/_replace` 的 telemetry，把 span 按 OTLP/HTTP 上报 Jaeger，浏览器看 Jaeger UI。

## Delta
- 新增：`examples/react_chat/otlp_telemetry.py`（OTLP/HTTP telemetry 注入实现）、`examples/trace-viewer/jaeger.sh` 或 `docker-compose`（起 Jaeger all-in-one）、OTLP 行为测试。
- 修改：`examples/react_chat/app.py::build_agent_async` 把 `NoopTelemetry()` 换成可注入的 OTLP telemetry（缺省仍 NoopTelemetry，环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 存在才启用）；README 说明 Jaeger 启动与查看。
- 删除：无（PhaseBridge 半成品已作废清理，未入库）。
- 无兼容层：环境变量缺失走 NoopTelemetry（保持离线零外部依赖），不造假——UI 明确标注"链路未上报 Jaeger"。

## Invariants
- src/ 零改动（不改内核、不改协议、不改规格 D 决策）；全部实现在 examples 层。
- 密钥只走环境变量；Jaeger endpoint 走标准 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境变量。
- 上报失败内部消化（TelemetryPort 通知语义：不拦截主流程、不抛穿），失败计数可观测、不伪造成功。
- span 结构真值：name/component/duration_ms/tokens/cost 来自框架 TraceSpan，不编造。
- 离线/无 endpoint 时不启动网络 IO（保持 demo 可离线跑）。

## Affected
- `examples/react_chat/app.py`（注入接线）
- `examples/react-chat/README.md`（Jaeger 启动与查看说明）
- `.conductor/overviews/.../8ef577c1-...md`（已声明方向）

## Acceptance
- AC-1 环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 缺失 → 用 NoopTelemetry，零网络 IO，现有门禁全绿
- AC-2 有 endpoint → 自定义 telemetry 注入生效（span 进出口径被真调用）
- AC-3 OTLP/HTTP 上报格式正确：POST 到 `<endpoint>/v1/traces`，protobuf 或 JSON body 含 resourceSpans/scopeSpans/spans，span name/duration/traceId 正确
- AC-4 上报失败（endpoint 不可达）不抛穿、主流程 chat 正常返回，失败计数可查
- AC-5 Docker 起 Jaeger all-in-one，真实跑一轮，Jaeger UI（:16686）能查到 service 与 spans
- AC-6 pytest/mypy/purity/全模块 gate 全绿，无死代码

## Open
- 不引入 opentelemetry SDK 依赖（pyproject 零改动）：用 httpx（已有依赖）手写 OTLP/HTTP JSON 协议（OTLP 支持 application/json 编码，无需 protobuf 编译器）。
- 只做 traces（span）；metrics/logs 上报属后续独立项（框架 TelemetryPort 有 metric/log，本轮 telemetry 实现内部留痕但不上报，诚实登记）。
- 规格 D 决策不新增（规格是真相源，D-1~D-10 已拍板不质疑）；本实现是 examples 层适配，非框架设计变更。
