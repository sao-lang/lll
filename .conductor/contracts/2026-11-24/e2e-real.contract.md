# change-contract · 真实 e2e 测试补强（2026-11-24，场景13 verify / full，方案先行）

## Why
用户要求"给项目补充真实 e2e 测试，先出一个测试方案"，并给出主线判据：**每个能力包 / 工具 / 协议 / 策略都能由用户按接口自定义，且至少有一份内置实现；用户只注入自己要用的，框架按保留接口编排**。现状证据：`tests/` 以 R1~R12 单模块 mock / 内存假实现为主，无 `tests/e2e/`；真网仅 `tests/r11/test_after_model_wiring.py` 单次 smoke（只 complete，无 stream）；`tests/examples/test_otlp_e2e.py` 用离线 LLM + 假 HTTP 接收端；没有"内置实现 vs 用户自定义实现"双路径真调用矩阵，也没有"按保留接口编排"的端到端证据。因此本轮先固化测试方案的枚举真相源与证据口径，规格确认后再 TDD 落地。

## Delta
- 新增 `tests/e2e/`（真实端到端套件，按 FR 分文件，见 Acceptance）：`conftest.py`、`api_surface.py`、`test_real_llm_api.py`、`test_port_matrix.py`、`test_capability_matrix.py`、`test_tool_matrix.py`、`test_strategy_matrix.py`、`test_orchestration_e2e.py`、`test_trace_sdk_e2e.py`、`test_degradation.py`。
- 新增 `tests/e2e/impls/`（用户侧参考实现，非框架源码）：自定义 LLMPort（内部真调注入 provider 的 OpenAI 兼容 HTTP 接口，含 complete/stream）、自定义 telemetry SDK（框架自动收集的 span 装填进注入 SDK 并真上报）、自定义 Memory/Eval/Telemetry/Human/Audit 等履约实现。
- 新增 `tests/tools/e2e_preflight.py`：探测 `DEEPSEEK_API_KEY`、`podman` CLI（PATH + 常见安装路径 + Podman Desktop 配置）、容器端口；输出机器可读的可用/缺失原因，供 pytest marker 决定是否 skip。
- 新增 `tests/e2e/test_public_api_surface.py`：以 `src/api/__init__.py` 的导出集与各对象公开方法为清单，逐项真调用或显式登记"本轮不测原因"，新增公开 API 未登记即失败。
- 修改 `pyproject.toml`：注册 `e2e` / `real_llm` / `real_container` marker（只加 markers 配置，不改依赖与 src）。
- 修改 `Makefile`：新增 `e2e` 目标（等价命令 `.venv/Scripts/python -m pytest tests/e2e -q`）。
- 删除：无。无兼容层，无新旧双轨；环境缺失用 skip + 明确 reason，不写假实现冒充真链路。

## Invariants
- `src/` 零改动（尤其 `src/runtime/` 一行不改）；不改 Port 协议签名、不改 `PORT_PROTOCOLS` 集合、不改规格 D-1~D-10。
- 密钥只从环境变量读取，传入 `OpenAICompatLLM(api_key=...)`；任何 trace / 日志 / 快照 / 测试报告不得出现 `sk-`。
- "自定义实现被真消费"必须有可观测副作用（调用计数、HTTP 接收端记录、span 记录），禁止只做 `isinstance` / `registry.has` / 手搓 DTO 自证。
- 每种能力的枚举真相源固定：Port 以 `src/compose/ports.py::PORT_PROTOCOLS` 为准；能力包以 11 个 `*Capability.install` 为准；工具以 `ToolPort` + `ToolRegistry` + MCP/WebFetch 为准；策略以 `src/harness/cognition/contracts.py` 的 `NamedStrategy` 族 + R6/R7/R9 策略协议 + `src/archs/base.py::Arch/JoinStrategy` 为准。
- 用户自定义实现必须完整履约协议语义（真读写入口、真 HTTP、真 span 装填），禁止 `return 常量` 形式的测试桩。
- 真实 DeepSeek 调用上限 ≤10 次/轮（每次 max_tokens ≤64，temperature=0）；容器只在预检通过时启动，失败即 skip 并写原因。
- 环境缺失登记"环境阻断"，不修改断言制造假绿；TDD 先 RED 后 GREEN，证据为命令 + 退出码。

## Affected
- `tests/e2e/`（新增）、`tests/e2e/impls/`（新增）、`tests/tools/e2e_preflight.py`（新增）
- `pyproject.toml`（markers）、`Makefile`（e2e target）
- `.conductor/overviews/2026-11-21/8ef577c1-算子核心架构重构（阶段A设计文档-+-阶段B代码）.md`（S1 声明）
- 文档：无（未改公共契约/接口/架构决策，规格文档零影响）

## Acceptance
- AC-0 用户确认本方案后才进入 S3 编码（conductor 硬门禁：规格未确认不动手）。
- AC-1 `test_real_llm_api.py`：真实 DeepSeek `LLMPort.complete` 返回非空正文、`error is None`、`usage.prompt_tokens>0`、`last_usage` 同步；断言响应/日志不含 `sk-`。
- AC-2 `test_real_llm_api.py`：真实 DeepSeek `LLMPort.stream` 收到 ≥1 个 chunk、拼接非空、`last_usage.prompt_tokens>0`；覆盖取消（`aclose()`）不抛穿。
- AC-3 `test_real_llm_api.py`：`ModelGateway.complete` / `stream` / `stream_events` 真路径可用，`before_model` / `after_model` 相位真发出；usage 进入 R1 预算扣费。
- AC-4 `test_public_api_surface.py`：`api` 导出面 + `Agent` / `AgentBuilder` / `AgentRuntime` / `StreamGateway` / `PortRegistry` / `CLI` / governance / wiring / `static_check_api` / `custom_arch` 等公开 API 全部有真调用用例或显式豁免登记；无遗漏。
- AC-5 `test_port_matrix.py`：`PORT_PROTOCOLS` 每个协议跑"内置实现 + 用户自定义实现"两条路径，自定义实现被框架在 `assemble_runtime` / `Agent.asay` 中真调用并留下副作用；协议方法逐个验证（如 ToolPort.register/catalog/call、TelemetryPort.span/metric/log、EventStreamPort.subscribe）。
- AC-6 `test_capability_matrix.py`：11 个 Capability 的 `install` 真注册 + 用户注入实例不被默认实现替换（回归 B5 类缺陷）；只注入所需能力时其余走内置且工作正常。
- AC-7 `test_tool_matrix.py`：`@tool` schema 推导、ToolRegistry 注册/目录/调用、MCP（stdio 真子进程）、WebFetcher、`ToolPort` 自定义实现替换后真被 ReAct 调用。
- AC-8 `test_strategy_matrix.py`：R8 `NamedStrategy` 全族（IntentStrategy/KeywordMatcher/Planner/Executor/RePlanner/ReflectStrategy 等）、R6/R7/R9 策略协议、`Arch`/`JoinStrategy` 均有内置实现 + 用户自定义实现，且自定义策略在真实 Agent 执行中生效（不是只取 prompt）。
- AC-9 `test_orchestration_e2e.py`：一轮真实 `Agent.asay` 同时验证用户只注入 LLM/Telemetry 时框架按保留接口完成编排：16 相位通知、工具调用、审计验链、checkpoint save/restore、预算扣费，全部来自真执行。
- AC-10 `test_trace_sdk_e2e.py`：用户自定义 SDK 履约 `TelemetryPort` + `_register/_replace`；框架自动收集 llm/tool/phase span 真装填进 SDK；有 Podman CLI 时起 Jaeger all-in-one，经 Jaeger API `GET /api/traces?service=...` 查到同 trace_id 的 span；无 CLI 时起进程内 OTLP HTTP 接收端并 skip 容器断言（理由写清）。
- AC-11 `test_degradation.py`：错 key / 不可达 endpoint / 无 key / 无容器等场景，框架诚实失败或明确 skip，不伪造成功；失败响应不含密钥。
- AC-12 全量门禁：`.venv/Scripts/python -m pytest tests/ -q`、`python -m mypy --strict src`、`source_purity.py check --strict`、`tests/tools/gate_check.py --module ...`、覆盖矩阵 `coverage_check.py` 与 `contract_check.py` 退出码 0、`review_gate` 无 Blocker。
- AC-13 源码纯净：测试中的自定义实现全部在 `tests/e2e/impls/`，`src/` 无 fake/stub/占位、无死代码；`git diff src/runtime/` 为空。
- AC-14 用例效果可判：逐用例五栏（前置/真实入口/预期效果/失败判据/反证变异）写进 `.conductor/overviews/2026-11-21/8ef577c1-e2e-real.details.md`；每个用例落地时至少一个反证变异实测变红（命令+退出码留痕），不红判无效重写；MEM-01/10/11/12 必须走 `store.write/recall` 或 `agent.asay` 真入口，禁止直调内部方法自证。

## Open
- U-1 Podman CLI 不在当前 PATH 与常见安装目录（Podman Desktop 进程在运行）；需用户给出 CLI 路径或授权启用，否则 AC-10 降级为进程内接收端 + 容器用例 skip，不伪造"真 Jaeger UI 已验证"。
- U-2 真实 DeepSeek 调用按 ≤10 次/轮上限执行；若用户要求更多真实场景需另行确认成本。
- U-3 "每种"按源码真相源全枚举（见 Invariants）；若用户希望只覆盖代表性子集，需在 S2 确认后缩减矩阵并显式登记被缩减项。
