# 需求覆盖矩阵 · 真实 e2e 测试从零重建（2026-11-24，含红队补漏）

> 需求原话取自用户本轮 query；决策列按源码/实测裁决。FR 共 38 条；红队补漏 13 行；Open 1 行（U-1，实测代理/镜像源细节已并入）。数字经红队复审后修订对齐。

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R1 用 introspection 穷举 src/ 导出符号与方法，形成全量 API 清单 | 采用：AST+importlib 双源清单 122 模块/309 类/514 方法/74 函数，逐项登记 | FR-1 | AC-1 |
| R2 全量 API 逐条有 e2e 用例，0 skip（真环境不具备除外且显式证据） | 采用：逐项真调用 + 显式豁免登记；skip 必须带证据 | FR-2 | AC-1/AC-11 |
| R3 每种能力包有内置实现 + 用户自定义实现两条真路径 | 采用：11 个 harness Capability.install 双路径矩阵（含 reflection/prompt）+ LLMProviderCapability 单列 | FR-3 | AC-6 |
| R4 每种工具有内置 + 自定义实现两条真路径 | 采用：ToolPort/@tool/ToolRegistry/MCP/WebFetch 双路径 | FR-4 | AC-7 |
| R5 每种协议有内置 + 自定义实现两条真路径 | 采用：14 个 PORT_PROTOCOLS 键双路径 | FR-5 | AC-5 |
| R6 每种策略有内置 + 自定义实现两条真路径 | 采用：NamedStrategy 全族 + Arch/Join/MajorityVote + R6/R7/R9 策略 | FR-6 | AC-8 |
| R7 用户只注入所需对象时框架按保留接口真编排 | 采用：最小注入 Agent.asay 真执行 + 相位/审计/存档/预算副作用断言 | FR-7 | AC-9 |
| R8 证据必须是真调用+可观察副作用（调用计数/HTTP 接收端/span/LLM messages） | 采用：副作用断言硬门禁，禁 isinstance/has/手搓 DTO | FR-8 | AC-5/AC-6/AC-8 |
| R9 LLM complete 走真 DeepSeek | 采用：OpenAICompatLLM.complete 真网，断言正文/usage/无 sk- | FR-9 | AC-2 |
| R10 LLM stream 走真 DeepSeek | 采用：真网 SSE 消费，chunk/usage/aclose | FR-10 | AC-3 |
| R11 链路追踪起真 Jaeger 容器，OTLP 上报后 Jaeger API 查同 trace_id span | 采用：管道 API 起 all-in-one；镜像失败=环境阻断附命令+响应，不降级 | FR-11 | AC-10 |
| R12 MCP 起真本地 server | 采用：真 stdio 子进程 + HTTP 本地 server | FR-12 | AC-7 |
| R13 WebFetch 起真本地 server | 采用：真本地 HTTP server 接收端记录请求 | FR-13 | AC-7 |
| R14 每用例写清输入→真实入口→精确断言→失败判据 | 采用：逐用例五栏（含反证变异）写进 details | FR-14 | AC-14 |
| R15 禁止无鉴别力断言（is not None/callable/or True） | 采用：断言必须是精确值/计数/内容匹配 | FR-15 | AC-14 |
| R16 反证变异：记忆 write→recall 改坏必须 rc=1 | 采用：删 memory.py:160 ingest → MEM-01 红→恢复绿 | FR-16 | AC-14 |
| R17 反证变异：门面 recall 注入 LLM 改坏必须 rc=1 | 采用：删 agent.py:520 memory_text → MEM-10 红→恢复绿 | FR-17 | AC-14 |
| R18 反证变异：门面 write_after 改坏必须 rc=1 | 采用：删 agent.py:757 write_after → MEM-11 红→恢复绿 | FR-18 | AC-14 |
| R19 反证变异：LLM stream 改坏必须 rc=1 | 采用：吞 llm.py SSE 增量 → LLM-02 红→恢复绿 | FR-19 | AC-14 |
| R20 反证变异：自定义 SDK 装填改坏必须 rc=1 | 采用：空 _register/_replace → TRACE-01 红→恢复绿 | FR-20 | AC-14 |
| R21 按 conductor：契约六段 + 覆盖矩阵 rc=0 | 采用：contract_check/coverage_check 退出 0 | FR-21 | AC-12 |
| R22 用户确认后才 TDD | 采用：AC-0 硬门禁，本轮只出方案 | FR-22 | AC-0 |
| R23 全量证据 pytest/mypy strict/source_purity strict/全模块 gate | 采用：命令+退出码留痕 | FR-23 | AC-12 |
| R24 独立 review Blocker=0 | 采用：review_gate 独立子代理复核 | FR-24 | AC-12 |
| R25 真网调用预算 ≤10 次/轮 | 采用：max_tokens≤64、temperature=0、计数上限 | FR-25 | AC-2/AC-3 |
| R26 DeepSeek key 仅从环境变量读，不写文件 | 采用：注册表读到内存注入进程，禁落文件 | FR-26 | AC-2/AC-11 |
| R27 断言响应/日志不含 sk- | 采用：全链路日志/响应/报告扫描 | FR-27 | AC-2/AC-11 |
| R28 Podman 已启动，用 API 驱动真容器 | 采用：命名管道 API（实测 /_ping=200）驱动，不依赖 CLI | FR-28 | AC-10 |
| R29 别用反射自动生成几百条契约用例凑数 | 采用：清单是索引，用例按语义分组真调用，禁止自动模板用例 | FR-29 | AC-1/AC-14 |
| R30 别把容器用例降级成进程内 HTTP 接收端后声称通过 | 采用：容器失败=环境阻断显式报，进程内接收端只作协议预检不作容器替代 | FR-30 | AC-10 |
| R31 别把"需业务上下文"的方法 skip 掉说成显式覆盖 | 采用：要么真跑通要么登记环境阻断+证据，禁口头豁免 | FR-31 | AC-1/AC-11 |
| R32 别只测 api.__all__ 就宣称全 API 测完 | 采用：introspection 方法级清单，类公开方法逐条 | FR-32 | AC-1 |
| R33 别跨用例共享实例（proactor loop 失效） | 采用：每用例独立 fixture + 独立 loop | FR-33 | AC-14 |
| R34 别为凑门禁数字改 gate_check.py 放水 | 采用：门禁脚本零改动，失败如实报 | FR-34 | AC-12 |
| R35 红队：Podman CLI 缺失≠环境阻断（管道 API 实测 200） | 采用：纠正前任结论，用管道 API 起容器 | FR-11/FR-28 | AC-10 |
| R36 红队：能力包数量按源码全 grep（11 个，我初版误数 9 已纠正；含 reflection/prompt） | 采用：全 src grep `def install(` 裁决，前任 11 正确 | FR-3 | AC-6 |
| R37 红队：R1 一行不改、src 纯净 | 采用：所有新增落 tests/e2e 与 .conductor，git diff src/runtime 空 | FR-34 | AC-13 |
| R38 红队：环境缺失时改断言造绿 | 采用：断言原样保留，skip 带证据，不改断言 | FR-31 | AC-11 |
| R39 用户给代理 61303 实测不通、当前 shell 54802、machine HTTPProxy=None | 进 Open(U-1)：需确认镜像源/代理端口，否则登记环境阻断；探测留痕 podman-probe-evidence.md | — | — |
| R40 真网成本与限流可控 | 采用：≤10 次/轮 + 短输出 + 固定 temperature | FR-25 | AC-2/AC-12 |
| R41 用户本轮只要方案，确认后再动手写用例 | 采用：本轮零代码改动（src/tests/pyproject/Makefile 不动），只出方案产物 | FR-22 | AC-0 |
| R42 红队：514 方法 + 74 函数需逐条路径与 EXEMPT 准入标准，禁"登记即覆盖" | 采用：208 含方法类 + 74 模块函数逐条映射真调用用例或 EXEMPT（逐条理由）；准入=纯 Protocol `...` 或纯数据 accessor | FR-1/FR-2 | AC-1 |
| R43 红队：compose.program 12 算子 / api.dsl 14 导出 / runtime 52 导出无专门用例 | 采用：新增 OPS-xx / DSL-xx / RT-xx 三组逐条真调用 | FR-35 | AC-9b |
| R44 红队：关键用例（真网/五变异/双实现差分）不得整体 skip | 采用：skip 只允许依赖真缺失且附证据；核心验收零 skip 覆盖 | FR-36 | AC-14b |
| R45 红队：变异点 4 行号缺失、变异点 5 对象选错（NoopTelemetry≠注入 SDK） | 采用：llm.py:219 yield content；SDK 变异改注入对象 `_register/_replace` | FR-19/FR-20 | AC-14 |
| R46 红队：探测结论无留痕无法复核 | 采用：podman-probe-evidence.md 记录命令+原始 stdout（不含密钥明文/前缀） | FR-28 | AC-10 |

FR 数 = 38；红队补漏 13 行（R29~R38 中 9 行 + R42~R46 五行，去重后 13）；Open 1 行（U-1）。
