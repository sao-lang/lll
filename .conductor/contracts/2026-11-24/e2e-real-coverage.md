# 需求覆盖矩阵 · 真实 e2e 测试补强（2026-11-24，含红队补漏）

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R1 用户原话："给项目补充真实 e2e 测试，你要先出一个测试方案" | 采用：方案先行，规格确认后再 TDD | FR-1 | AC-0 |
| R2 "你要保证每个 api 都可用" | 采用：公开 API 清单 + 逐项真调用 + 新增 API 未登记即失败 | FR-2 | AC-4 |
| R3 "比如 llm 的 complete 必须可用" | 采用：真实 DeepSeek complete 用例 | FR-3 | AC-1 |
| R4 "比如 llm 的 stream 必须可用" | 采用：真实 DeepSeek stream + 末块 usage + 取消 | FR-4 | AC-2 |
| R5 模型编排层 API 可用性（gateway complete/stream/stream_events） | 采用：ModelGateway 真路径用例 | FR-5 | AC-3 |
| R6 "每种能力包至少有一份内置实现" | 采用：11 个 Capability.install 真注册矩阵 | FR-6 | AC-6 |
| R7 "每种能力包都有用户自定义实现" | 采用：用户注入实例被框架真消费并留副作用 | FR-7 | AC-6 |
| R8 "每种工具和协议、策略都有自定义实现" | 采用：ToolPort/@tool/MCP/WebFetch 双路径 | FR-8 | AC-7 |
| R9 "每个协议有内置 + 自定义实现" | 采用：PORT_PROTOCOLS 全枚举双路径 | FR-9 | AC-5 |
| R10 "每种策略有内置 + 自定义实现" | 采用：NamedStrategy 全族 + Arch/JoinStrategy 双路径 | FR-10 | AC-8 |
| R11 "llm 用户可自定义实现，内部直接调用注入 provider 的 api 即可" | 采用：自定义 LLMPort 真发 HTTP 到本地 provider（含 SSE） | FR-11 | AC-5/AC-1 |
| R12 "链路追踪可视化平台用户借用任意 SDK，框架自动收集数据，注入 SDK 直接装填" | 采用：自定义 SDK 履约 TelemetryPort + _register/_replace，真收 span | FR-12 | AC-10 |
| R13 "用户只注入自己要使用的东西" | 采用：最小注入编排用例，其余能力走内置 | FR-13 | AC-6/AC-9 |
| R14 "框架内部会按照保留的接口进行编排" | 采用：Agent.asay 真执行 + 相位/审计/存档/预算断言 | FR-14 | AC-9 |
| R15 "podman 是打开的，你可以用" | 采用：预检 CLI 后起 Jaeger all-in-one，API 查同 trace span | FR-15 | AC-10 |
| R16 "这是 deepseek 的 apikey，你可以测试" | 采用：密钥仅走环境变量，真网调用 | FR-16 | AC-1/AC-2 |
| R17 密钥安全（响应/日志/快照不得泄露） | 采用：断言全链路无 sk- 前缀，密钥仅环境变量 | FR-17 | AC-1/AC-11 |
| R18 门禁证据（pytest/mypy/purity/gate/review） | 采用：命令 + 退出码留痕 | FR-18 | AC-12 |
| R19 红队："每种"边界可判 | 采用：枚举真相源固定（PORT_PROTOCOLS / Capability / contracts / Arch / ToolPort） | FR-19 | AC-5/AC-8 |
| R20 红队：只登记未真调用属假绿 | 采用：自定义实现必须有可观测副作用断言 | FR-20 | AC-5/AC-6 |
| R21 红队：默认新建替换用户注入（B5 类回归） | 采用：注入隔离与 replace 语义用例 | FR-21 | AC-6 |
| R22 红队：真网成本与限流可控 | 采用：≤10 次/轮、max_tokens≤64、temperature=0 | FR-22 | AC-1/AC-12 |
| R23 红队：stream 断流/取消/末块 usage | 采用：aclose 与 last_usage 断言 | FR-23 | AC-2 |
| R24 红队：容器端口冲突/镜像拉取失败 | 采用：预检 + 明确 skip reason | FR-24 | AC-10/AC-11 |
| R25 红队：R1 一行不改、src 纯净 | 采用：全部新增落在 tests/e2e，git diff src/runtime 为空 | FR-25 | AC-13 |
| R26 红队：环境缺失时改断言造绿 | 采用：环境阻断登记 + skip，断言原样保留 | FR-26 | AC-11 |
| R27 Podman CLI 路径缺失（当前仅 Desktop GUI 在跑） | 进 Open(U-1) | — | — |
| R28 真网调用预算上限确认 | 进 Open(U-2) | — | — |
| R29 全枚举 vs 代表性子集确认 | 进 Open(U-3) | — | — |
| R30 用户二次拷问："没列每个测试用例要达到什么效果，比如记忆管理怎么保证测的对" | 采用：逐用例五栏 + 记忆模块 13 用例样板 + 反证变异机制 | FR-27 | AC-14 |
| R31 红队：用例只查符号/isinstance 属假绿 | 采用：可观察副作用断言 + 双实现计数差分 + 负向 fail-closed | FR-28 | AC-14/AC-5 |

FR 数 = 28；红队补漏 10 行（R19~R26、R31）；用户追加 1 行（R30）；Open 3 行。
