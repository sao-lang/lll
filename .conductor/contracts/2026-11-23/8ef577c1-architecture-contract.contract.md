# Change: 8ef577c1-arch-contract / 2026-11-23 / confirmed

## Why
用户要评估框架是否可用,发现框架对外宣称的"组合式 API 快速搭 agent、内置架构由算子封装、可插拔四要素齐全"在代码里没兑现:0/10 架构复用算子、履约校验全缺、无语义门面、零胶水搭不出结果。用户明确:"message 不暴露、ReAct 由组合式 API 封装、组合 API 可封装自定义架构要 FP、能力/策略/工具三层可注入且接口都有默认实现(Registry Capability)"。前一轮"全代码检测"只按内部 AC 验,漏掉了这条用户路径,这次按用户契约四层重做架构层。

## Delta
- 新增 `api.Agent`（语义门面）：`say(text)/run(text)` 直接出结果，用户零 message/effect/payload/ctx；内部完成文本注入、ctx 转换、消息归一、usage 搬运。位于 api 层，不改依赖方向。
- 用户用 compose 的 FP 组合算子（`seq`/`loop_until`/`branch` 等，签名 `ProgramFn→ProgramFn`）直接封装自定义架构，与内置架构同权；Effect 作为组合单元对用户公开（B3 方案 A，用户 15:39 确认），仅 message/payload 数据由语义门面内部注入不暴露。
- 替换 10 内置架构的 `compile()`：由手搓 `yield Invoke/_batch/_end` 改为复用组合层算子（`ReAct`→loop 语义、`Router`→branch 语义等），行为等价。
- 新增可插拔履约校验：所有注入点（Capability/策略/工具/Port）装配期 `isinstance` Protocol 校验，非法实现拒绝，兑现 R13 第四要素。
- 补齐默认实现缺口：`CheapIntentModel`/`PrimaryIntentModel` 等无默认协议给默认实现或在 Open 说明。
- 保留（分层，非双轨）：`AgentBuilder.runtime(call)` 必填 call 与 `redact(value, ctx)` 是**底层装配器**入口（高级用户显式控制调用链/脱敏）；`api.Agent` 语义门面是默认用户路径（零胶水）。两者位于不同层、面向不同用户，非新旧双轨。契约③的『不暴露 message/effect』约束的是**默认门面**，不约束显式底层装配器。
- 删除 10 架构 compile 内手搓的**控制流循环/顺序 yield 序列**（`for _ in range` 驱动的工具/反思/交接/图遍历、裸 `yield spawns` 并行），改由 `loop_until`/`par`/`seq`/`branch_value` 承担。
- 保留（非双轨）：`_invoke`/`_batch`/`_end` 是**叶子 Effect 构造器**，`_run_child` 是子 Program 驱动器，`_X` 是 x-arch.* 数据键工具——它们是算子内部与架构共用的基础设施，不承担控制流，删除它们等于重写 R1 Effect 协议，超出契约范围。
- 删除测试 `tests/r12/test_tool_layers.py::test_p6_arch_compiles_to_program_via_operators` 的空壳断言（因改为真实断言算子被复用而消失）。
- 删除语义块式 `api/dsl.py` 的 `llm_step/tool_step/terminate_step/Sequence/LoopStep`（因用户改选方案 A：用户直接用 FP 算子而非语义块而消失）。

## Invariants
- R1（`src/runtime/`）一行不改（D-7 硬约束）；内核只认 Program，零领域语义。
- 依赖方向恒为 工具层(R3~R9) → 组合层(compose) → 内核 R1；compose 不 import harness/api。
- 零死代码/兼容层/测试桩；改契约同次删旧路径，禁双轨。
- 现有 10 算子的对外行为（ProgramFn→ProgramFn 工厂延迟、resume 重建）不变；语义 DSL 是新增层，不改算子签名。
- 现有 868 测试行为不回归（除被替换的空壳断言）。

## Affected
- 模块：`src/api/`（新语义门面）、`src/archs/recipes.py`（10 架构算子化）、`src/compose/`（语义 DSL + 履约校验）、`src/harness/`（默认实现补齐）。
- 测试：`tests/r12/`、`tests/r11/`、`tests/r2/`；新增用户路径零胶水实测。
- 设计文档（S5 必同步）：`docs/02-模块详设/Agent/R12-内置架构与组合.md`、`R13-可插拔与协议契约.md`、`R11-集成接入与治理.md`、`R2-算子与装配.md`、`docs/01-总览与设计/AGENT-总设计.md`、`docs/README.md`、`docs/05-其他指南/算子与组合代码阅读指南.md`、`代码精读/10-archs内置架构精读.md`。
- 需 conductor 编排影响面核实。

## Acceptance
- AC-1~AC-21 对应 `.coverage.md` 的 21 行；每行为一条命令+退出码或文件:行号证据。
- 核心用户路径（FR-6/AC-6）：`api.Agent(ReAct(tools)).capability(...).say("...")` 一条链出结果，贴真实输出。
- 10 架构算子化（AC-7/18）：源码断言 10/10 compile 复用算子 + 真实 Runner（非 fake state port）行为等价测试 PASS。
- 可插拔四要素（AC-11/20）：协议约30个逐条存在校验 + 注入点 isinstance 履约校验，非法实现装配期拒绝。
- 删除零残留（AC-19）：`removal_check` 对 `call` 必填参数/`redact(ctx)`/`_invoke`/`_batch`/`_end`/`_run_child` 零残留。
- 全量门禁：`pytest tests/ -q` rc=0；`mypy --strict src` rc=0；12 `gate_check` 全绿；`ast_scan` PASS；`source_purity --strict` rc=0；`git diff src/runtime/` 空。
- 独立 review Blocker=0。

## Open
- 无。契约②与③的张力（红队 B3）：用户 15:39 确认方案 A（用户直接用 FP 算子、碰 Effect；仅 message 数据不暴露）；门面分层（红队 B4）：用户 15:39 确认保持 api 层。两项均已拍板。
- 无。
