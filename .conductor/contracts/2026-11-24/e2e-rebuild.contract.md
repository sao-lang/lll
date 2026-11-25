# change-contract · 真实 e2e 测试从零重建（2026-11-24，场景13 verify / full，方案先行）

## Why
用户接手 E:\vsc-workspace\lania-zip\lll 的 Python Agent 框架，要求从零补齐真实 e2e 测试（前任实现已全部删除），并明确列出验收标准与"前任踩过的坑"。本轮**只出方案**：全量 API 清单 + 逐用例效果表 + 覆盖矩阵，用户确认后才进 S3 写用例。核心诉求逐条：①introspection 穷举 src/ 导出符号与方法、逐条 e2e、0 skip（真环境不具备需显式证据）；②每种能力包/工具/协议/策略都有内置实现 + 用户自定义实现两条真路径，用户只注入所需对象时框架按保留接口真编排，证据必须是真调用 + 可观察副作用（调用计数/HTTP 接收端/span/LLM 实收 messages），禁止 isinstance/registry.has/手搓 DTO；③LLM complete/stream 走真 DeepSeek、链路追踪起真 Jaeger 容器经 OTLP 上报后 Jaeger API 查同 trace_id span、MCP/WebFetch 起真本地 server；④每用例写清输入→真实入口→精确断言→失败判据，禁止无鉴别力断言；⑤至少覆盖记忆 write→recall、门面 recall 注入 LLM、门面 write_after、LLM stream、自定义 SDK 装填五个反证变异（临时改坏被测行必须 rc=1，恢复 rc=0，命令+退出码留痕）；⑥按 conductor 契约六段+覆盖矩阵 rc=0，用户确认后 TDD，全量证据 + 独立 review Blocker=0。

现状核验（本轮独立实测，不采信前任）：前任方案 `.conductor/contracts/2026-11-24/e2e-real.contract.md` 把 Podman 判成"CLI 缺失=环境阻断"，但**命名管道 `\\.\pipe\podman-machine-default` 实测 `GET /_ping` = 200 OK（Libpod 6.2.0 / Api-Version 1.44 / Docker-Experimental true）**，容器内已有 15 镜像、2 运行容器；因此"真容器"路径不能用 CLI 缺失当理由降级。能力包数量经全 src grep `def install(` 复核为 **11 个**（cognition / reflection / memory / eval / model / observability / prompt / rag / resilience / security / tools；前任 11 正确，我初版误数 9 已纠正；另有 `ports.adapters.provider.LLMProviderCapability.install` 属 provider 适配器、不计入 harness 能力包，但纳入覆盖矩阵）。`git status` 中 `src/api/agent.py` 显示为 M 但 `git diff` 与 `git diff --cached` 均为空（mtime 抖动，内容零差异），本轮不改 src。

## Delta
- 新增 `tests/e2e/`（真实端到端套件，方案确认后落地）：`conftest.py`（真入口 fixture、进程隔离）、`api_surface.py`（全量 API 清单的机器可读真相源）、以及按 FR 分文件的用例集。
- 新增 `tests/e2e/impls/`（用户侧参考实现，**非框架源码**）：自定义 LLMPort（真发 HTTP 到注入 provider，含 SSE）、自定义 Telemetry SDK（履约 TelemetryPort + `_register/_replace`，span 真装填、真 OTLP 上报）、自定义 Memory/Eval/Human/Audit/RAG/Strategy 等履约实现（完整语义 + 调用计数，禁止 return 常量桩）。
- 新增 `tests/tools/e2e_preflight.py`：探测 DEEPSEEK_API_KEY（进程环境 → 注册表 User 区读取到内存，不落文件）、Podman 命名管道 API（`/_ping`）、Jaeger 镜像与容器；输出机器可读的可用/阻断原因，供 marker 决定是否 skip。**镜像拉取失败 = 环境阻断，显式报命令+响应，不静默 skip、不降级成进程内 HTTP 接收端冒名顶替。**
- 新增 `tests/e2e/test_public_api_surface.py`：以 introspection 清单（`.conductor/analysis/2026-11-24/api-inventory.json`，122 模块/309 类/514 方法/74 模块函数）为清单，逐项真调用或显式登记"本轮不测原因"；新增公开 API 未登记即失败。
- 修改 `pyproject.toml`：注册 `e2e`/`real_llm`/`real_container` marker（只加 markers，不改依赖与 src）。
- 修改 `Makefile`：新增 `e2e` 目标（`.venv/Scripts/python -m pytest tests/e2e -q`）。
- 本轮（S2）新增的方案产物：`.conductor/contracts/2026-11-24/e2e-rebuild.contract.md`（本文件）、`e2e-rebuild-coverage.md`（覆盖矩阵）、`.conductor/overviews/2026-11-21/8ef577c1-e2e-rebuild.details.md`（逐用例效果表）、`.conductor/analysis/2026-11-24/api-inventory.{json,md}`（全量 API 清单）。
- 删除：无。无兼容层，无新旧双轨；环境缺失登记"环境阻断"并给证据，不写假实现冒充真链路。

## Invariants
- `src/` 零改动（尤其 `src/runtime/` 一行不改）；不改 Port 协议签名、不改 `PORT_PROTOCOLS` 集合、不改规格 D-1~D-10。S2 阶段仅写 `.conductor/` 方案产物与 `tmp/` 临时探测脚本（tmp 不入交付）。
- 密钥只从环境变量读取（进程 `DEEPSEEK_API_KEY` 缺失时从注册表 User 区 `HKCU\Environment` 读到内存注入进程），禁止写入任何文件；断言响应/日志/快照/测试报告不含 `sk-`。真网调用 ≤10 次/轮（每次 max_tokens≤64、temperature=0）。
- "自定义实现被真消费"必须有可观察副作用（调用计数、HTTP 接收端记录、span 内容、LLM 实收 messages），禁止只做 `isinstance` / `registry.has` / `hasattr` / 手搓 DTO 自证。
- 枚举真相源固定：Port 以 `src/compose/ports.py::PORT_PROTOCOLS` 的 **14 个键**为准（llm/tool/skill/rag/memory/store/sandbox/audit/eval/telemetry/human/mcp/event_stream/disclosure）；能力包以 **11 个** `harness.*Capability.install` 为准（含 reflection / prompt，另有 `LLMProviderCapability.install` 单列）；工具以 ToolPort + ToolRegistry + MCP/WebFetch 为准；策略以 `src/harness/cognition/contracts.py` 的 `NamedStrategy` 族（IntentStrategy/KeywordMatcher/IntentSampleStore/CheapIntentModel/PrimaryIntentModel/Planner/Executor/RePlanner/ReflectStrategy）+ R6/R7/R9 策略协议 + `src/archs/base.py::Arch/JoinStrategy/MajorityVote` 为准；算子以 `compose/program.py` 的 12 个 + `api/dsl.py` 的 14 个导出为准；运行时以 `runtime/__init__.py` 的 52 个导出（Runner/Interpreter/fold/replay/revive/checkpoint/Budgets/Suspend 等）为准。
- API 覆盖登记硬规则：清单中 208 个含方法的类 + 74 个模块函数，每个公开方法必须映射到①具体真调用用例 或②`EXEMPT` 条目且带逐条理由（如纯 Protocol 方法体 `...`、纯数据 accessor）；EXEMPT 必须有准入标准且逐条列明，禁止"登记即覆盖"的口头豁免。清单计数以 JSON 为准：514 公开方法、74 模块函数、309 类（其中 208 类含方法）。
- 每个用例必须有①输入 ②真实入口（用户可见 API）③精确断言 ④失败判据 ⑤反证变异五栏；禁止 `assert x is not None` / `assert callable` / `or True` 类无鉴别力断言。
- 反证变异硬门禁：①MEM write→recall（删 `memory.py:160` ingest）；②门面 recall 注入 LLM（删 `agent.py:520` memory_text）；③门面 write_after（删 `agent.py:757`）；④LLM stream（删 `llm.py:219` 的 `yield content`，精确行号已核实）；⑤自定义 SDK 装填（把**注入的自定义 SDK 对象**的 `_register/_replace` 改空，非 NoopTelemetry 自身——调用链是 `telemetry.py:36/52/131/140` 调注入对象；变异对象选错则 TRACE-01 不会变红）；五处落地时临时改坏→目标用例 rc=1→恢复→rc=0，命令+退出码留痕；不红判假绿重写。本轮五变异均无实测留痕（承诺 S3 落地时补），方案阶段不谎称已验证。
- 用例间不共享实例（Windows proactor 下 asyncio.run 换 loop 会持有失效资源）；每个用例独立 fixture 建实例、独立 loop。
- 不改 `tests/tools/gate_check.py` / `ast_scan.py` 放水；门禁失败如实报告。
- 真环境优先：Podman 管道 API 可用即走真容器；镜像拉取失败如实报"环境阻断"+命令+响应，禁止降级成进程内 HTTP 接收端后声称真 Jaeger 通过。

## Affected
- `tests/e2e/`（新增）、`tests/e2e/impls/`（新增）、`tests/tools/e2e_preflight.py`（新增）
- `pyproject.toml`（markers）、`Makefile`（e2e target）
- `.conductor/contracts/2026-11-24/e2e-rebuild.contract.md`（本契约）、`e2e-rebuild-coverage.md`（覆盖矩阵）
- `.conductor/overviews/2026-11-21/8ef577c1-e2e-rebuild.details.md`（逐用例效果表）
- `.conductor/analysis/2026-11-24/api-inventory.{json,md}`（全量 API 清单，introspection 生成）
- 文档：本轮不改设计规格文档（未改公共契约/接口/架构决策）；前任契约与 details 保留作对照，不删除不覆盖。

## Acceptance
- AC-0 用户确认本方案后才进入 S3 编码（conductor 硬门禁：规格未确认不动手）。
- AC-1 全量 API 清单：`.conductor/analysis/2026-11-24/api-inventory.json` 穷举 122 模块/309 类/514 公开方法/74 模块函数/0 导入失败；`test_public_api_e2e.py` + `api_surface_map.py` 逐条真调用或带准入理由的 EXEMPT，新增未登记 API 即失败。
- AC-15 真实覆盖率硬指标（用户 2026-11-25 追加）：以 `coverage run --source=src -m pytest tests/e2e` 实测，具名函数/方法执行率必须 ≥95%，且未执行项只能是私有 helper / 嵌套函数 / Protocol 占位（公开 API 必须全部真执行）。实测 56.5%（446 未执行）→ 补测后 **98.2%（1007/1025，剩 18 个均为私有 helper/嵌套函数）**。清单 `.conductor/analysis/2026-11-24/uncovered-public-api.json`。
- AC-16 已知缺陷回归（用户 2026-11-25 追加）：overview 登记的三处历史缺陷（ToolCall 归一丢 danger/source、replay_snapshot plain dict 崩、外部 audit 不同源）源码已修，且必须有 e2e 真入口回归 + 反证变异红→绿证据（`test_known_defects_regression.py`）。
- AC-2 真网 LLM complete：真实 DeepSeek `OpenAICompatLLM.complete` 返回非空正文、`error is None`、`usage.prompt_tokens>0`、`last_usage` 同步；断言响应/日志无 `sk-`。
- AC-3 真网 LLM stream：真实 DeepSeek `.stream` 收到 ≥1 chunk、拼接非空、`last_usage.prompt_tokens>0`；`aclose()` 取消不抛穿。
- AC-4 ModelGateway 编排：`complete`/`stream`/`stream_events` 真路径可用，`before_model`/`after_model` 相位真发，usage 进 R1 预算扣费。
- AC-5 Port 矩阵：14 个 `PORT_PROTOCOLS` 键各跑内置 + 用户自定义两条路径，自定义实现经 `assemble_runtime`/`Agent.asay` 真调用并留副作用；协议方法逐个真调用（如 ToolPort.register/catalog/call、TelemetryPort.span/metric/log、EventStreamPort.subscribe），14 键逐条成行不合并。
- AC-6 Capability 矩阵：11 个 harness Capability 的 `install` 真注册 + 用户注入实例不被默认替换（含 reflection / prompt）；`LLMProviderCapability.install` 单列覆盖；只注入所需能力时其余走内置且工作正常。
- AC-7 Tool 矩阵：`@tool` schema 推导、ToolRegistry 注册/目录/调用、MCP（真本地 server：stdio 子进程 + HTTP）、WebFetch（真本地 server）、自定义 ToolPort 替换后被 ReAct 真调用。
- AC-8 Strategy 矩阵：NamedStrategy 全族 + Arch/JoinStrategy/MajorityVote + R6/R7/R9 策略协议，内置 + 用户自定义两条路径，自定义策略在真 Agent 执行中生效（计数>0 且结果反映自定义输出）。
- AC-9 全 API 编排：一轮真 `Agent.asay` 验证用户只注入 LLM/Telemetry 时框架按保留接口编排——16 相位通知、工具调用、审计验链、checkpoint save/restore、预算扣费全部来自真执行。
- AC-9b 算子与运行时族：`compose/program.py` 12 算子（seq/par/branch/race/loop/loop_until/map_state/map_effect/fallback/gate/retry/JoinMode）、`api/dsl.py` 14 导出、`runtime/` 52 导出的公开执行 API（Runner.run/resume、Interpreter.interpret、fold、replay/revive、Budgets 扣费）逐个有真调用用例，禁止只靠 ORCH-01 间接覆盖。
- AC-14b 关键用例不得 skip：真网 LLM（LLM-01/02）、五变异点关联用例、双实现差分用例不得以环境理由整体 skip；skip 只允许出现在其依赖的环境确实缺失且附证据，且不得使核心验收零覆盖；`pytest` 全绿但核心用例 skipped = 验收失败。
- AC-10 自定义 SDK 装填：自定义 SDK 履约 TelemetryPort + `_register/_replace`，框架自动收集的 llm/tool/phase span 真装填进 SDK；有真 Jaeger 容器时经 OTLP 上报后用 Jaeger API `GET /api/traces?service=...` 查到同 trace_id 的 span；镜像拉取/容器失败 = 环境阻断（附命令+响应），不静默 skip、不伪装进程内接收端为容器验证。
- AC-11 降级与诚实：错 key/不可达 endpoint/无 key/无容器场景诚实失败或显式环境阻断，不伪造成功；失败响应不含密钥。
- AC-12 全量门禁：pytest / mypy strict / source_purity --strict / 全模块 gate / contract_check / coverage_check / review_gate 无 Blocker，全部命令+退出码留痕。
- AC-13 源码纯净：自定义实现全在 `tests/e2e/impls/`；`src/` 无 fake/stub/占位/死代码；`git diff src/runtime/` 为空。
- AC-14 逐用例五栏：`.conductor/overviews/2026-11-21/8ef577c1-e2e-rebuild.details.md` 每用例填输入/真实入口/精确断言/失败判据/反证变异；五个关键变异落地时实测红→绿，命令+退出码留痕；MEM 系列必须走 `store.write/recall` 或 `agent.asay` 真入口，禁直调内部方法自证。

## Open
- U-1 Jaeger 镜像拉取：**已解除（2026-11-24 晚）**。根因是 tag 写成 `1.62` 而 quay.io 真实 tag 为 `1.62.0`。`POST /images/create?fromImage=quay.io/jaegertracing/all-in-one&tag=1.62.0` + `X-Registry-Auth=e30=` → HTTP 200 Download complete；真容器 CREATE 201 / START 204 / `GET /api/services` 200。TRACE-02 真跑通并查到同 trace_id span，0 skip。历史失败证据保留在 `podman-probe-evidence.md`。代理 61303 不通、shell 动态端口的问题由 conftest 同步小写 `no_proxy` 根治（本地回环不误走代理）。
- U-2 DeepSeek key：当前进程环境缺失，注册表 User 区存在（35 字符，`sk-` 前缀）；S3 从注册表读到内存注入，禁止落文件。真网调用 ≤10 次/轮已按用户约束。
- U-3 "每种"按源码真相源全枚举（14 Port / 9 Capability / NamedStrategy 全族 / 12+14 算子 / ToolPort+MCP+WebFetch）；用户若希望只覆盖代表性子集，需在确认时明确缩减并登记被缩减项。
