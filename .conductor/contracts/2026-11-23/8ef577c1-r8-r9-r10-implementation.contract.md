# Change: 8ef577c1-R8R9R10 / 2026-11-23 / confirmed

## Why
用户要快速落地 R8（认知与多Agent编排）/R9（安全与HITL）/R10（可观测评测与持久化）三个模块。三份详设是设计定稿时的快照，与当前代码事实严重脱节（仍称 hooks/AOP 存在、R2 是插件总线、组合算子含 budget、端口签名与源码不同）。若照字面实现会产生死代码/桩/双轨兼容层，违反用户 2026-11-23 明确的源码纯净铁律。因此先拷问设计、按源码事实裁决冲突、用户拍板四个方向后固化本规格。

## Delta
- 新增 `src/harness/cognition/`（认知服务：Intent 四级级联默认策略 + 四部件 + intent_as_program 桥梁 + Planner/Executor/Reflect/RePlan 确定性默认实现）。
- 新增 `src/harness/security/`（安全域 DTO + Guardrail + 注入检测复用 + SandboxPort→LocalSandbox / AuditPort→AuditLogger / HumanPort→ConsoleHumanPort 三个默认实现 + ApprovalFlow）。
- 新增 `src/harness/observability/` + `src/harness/eval/` + `src/harness/durability/`（TelemetryPort→NoopTelemetry + Trace、EventStreamPort→InMemoryEventStream + EventEnvelope、EvalPort→RuleEval、Projection、CheckpointSnapshot 复用 R1 EventStore）。
- 替换 `src/archs/recipes.py::Router._recognize()`：单次 asyncio.run 裂缝 → 经 `intent_as_program` 单入口消费级联结果（文档 2147 承诺但代码未落地）。
- 接线 `src/harness/security/capability.py::SecurityCapability`：三 Port 默认实现由 `install(reg)` 注册进 Registry；`ToolRegistry.__init__(sandbox/human/audit)` 是已存在的注入缝（R5 现状），保持 fail-closed 语义不改 R5 源码（工具层互不直连）。默认装配（把 SecurityCapability 三 Port 传入 ToolsCapability）属 R11 装配器职责——R11 未实现，在其详设留功能接线备注，不留假实现。
- 新增 `tests/r8/`、`tests/r9/`、`tests/r10/` 测试与 `tests/demos/demo_r8|r9|r10.py` 独立闭环 demo。
- 扩展 `tests/tools/gate_check.py::MODULES` 与 `tests/tools/ast_scan.py::TARGETS` 注册 r8/r9/r10 门禁。
- 同步文档：R8/R9/R10 详设实现状态与 AOP/hooks 表述、AGENT-总设计 §R8/R9/R10 与附录 B.1.2 三行移到 B.1.1、`src/compose/ports.py` 归属表待实现→已实现、R13 §4.1 级联代码落地注记、Makefile demo/gate/test 目标。
- 删除：`- 删除 \`Router._recognize\` 内的 \`asyncio.run(cls.recognize())\` 单次直调（因 \`intent_as_program\` 统一单入口而消失）`；`- 删除 \`src/compose/ports.py\` 归属表 R9/R10 六项的"（待实现）"标记（因六个默认实现类落地而消失）`。

## Invariants
- R1（src/runtime/）一行不改（D-7 硬约束）；所有新能力产出 Program，运行时对其零领域感知。
- 依赖方向恒为 工具层 → 组合层 → R1；R1 零依赖上层。新模块只用现有 PORT_PROTOCOLS 键与 compose 原语，不新增 Port 种类。
- 端口签名以源码为准：`AuditPort.record(dict,ctx) async`（ports.py:231）、`EvalPort.score(case_id,output,ctx)->float`（:241）、`EventStreamPort.subscribe->AsyncIterator[DynamicValue]`（:318）、`TelemetryPort.span/metric/log` 同步（:270-278）、`SandboxPort.run(fn,args,ctx)`、`HumanPort.ask(question,options,ctx)`。
- DangerLevel 单一源 = `compose/dto.py:55`；coerce fail-closed 回退 CRITICAL。
- 源码纯净：禁止死代码、打补丁、兼容层、测试桩/fake（用户 2026-11-23 铁律）；fake 只允许在 tests/ 下作测试替身。
- 通知只观察不拦截：`before_model` 为 Lifecycle 通知点（notify.py:46）；真正拦截走 `compose/call.py::injection_guard` 装饰器（:85）与 R9 Guardrail。
- 注入检测单一源：复用 `harness/skills/provider.py::KeywordInjectionDetector`，R9 `SafetyReport` 从 `InjectionVerdict` 派生，不造第二套检测器。
- Checkpoint 复用 R1 `EventStore`（runtime/types.py:728）+ `replay_events`（fold.py:208），R10 不重复声明 EventStore。
- 确定性默认：认知策略默认纯函数（不联网、不调真实 LLM），LLM 路径经注入/Effect 交给调用方。
- 失败测试先行（TDD RED→GREEN）；证据只认命令+退出码；source_purity --strict + HEAD 基线 rc=0（见 AC-8 直接命令）。

## Affected
- 源码新增：`src/harness/{cognition,security,observability,eval,durability}/`、`tests/{r8,r9,r10}/`、`tests/demos/demo_r8|r9|r10.py`。
- 源码改动（接线）：`src/archs/recipes.py`（Router/RouterSkill）、`src/compose/ports.py`（归属表注释）、`tests/tools/gate_check.py`、`tests/tools/ast_scan.py`、`Makefile`。R9 三 Port 装配接线点留给 R11（未实现，见 R11 详设备注），本轮不改 R5（保持工具层互不直连 + fail-closed）。
- 设计文档：`docs/02-模块详设/Agent/R8-认知与多Agent编排.md`、`R9-安全与HITL.md`、`R10-可观测评测与持久化.md`、`R13-可插拔与协议契约.md`、`docs/01-总览与设计/AGENT-总设计.md`（§R8/R9/R10 + 附录 B.1.2 + §R10.4 EventStore 去重）、`docs/03-流程与规范/门禁体系.md`、`docs/README.md`。
- 需 conductor 编排影响面核实：改 Router._recognize 影响 R12 archs 行为；改 ToolsCapability 构造影响 R5 装配；gate_check/ast_scan 扩展影响全门禁链。

## Acceptance
- AC-1 `pytest tests/r8/ -q` 退出 0；四级级联逐级短路/兜底/四部件注入/strategy 整体替换全绿（R8-T01/T08）。
- AC-2 `pytest tests/r8/ -q` 规划/反思轮换用例全绿（R8-T02/T03，max_rounds 处结束）。
- AC-3 `pytest tests/r12/test_archs_semantics.py -q` 全绿且 Router 走 intent_as_program 单入口（R8-T04/T06 嵌套）。
- AC-4 `pytest tests/r9/ -q` 全绿：注入串拦截、沙盒路由、审批挂起恢复、审计哈希链、secret 隔离（R9-T01~T06）。
- AC-5 `pytest tests/r10/ -q` 全绿：trace 串接、事件流从 seq 续读、回放 State 全等、checkpoint 跨进程一致、eval 三层不调真实 LLM（R10-T01~T05）。
- AC-6 `pytest tests/ -q` 全量退出 0（无回归）；`mypy --strict src` 退出 0。
- AC-7 `python tests/tools/gate_check.py --module r8|r9|r10` 各退出 0；`python tests/tools/ast_scan.py --module r8|r9|r10` 各 PASS。
- AC-8 直接命令 `.venv/Scripts/python "C:/Users/30895/.workbuddy/skills/conductor/organs/conductor-review/scripts/source_purity.py" check --baseline tests/tools/baselines/purity-HEAD-20261121.json --strict` 退出 0（Makefile `purity` target 同命令；源码零新增 fake/占位/双轨，基线锁 2026-11-21 HEAD）。
- AC-9 独立 review Blocker=0；`git status --short src/runtime/` 为空。
- AC-10 文档同步核对：R8/R9/R10 详设 + 总设 §R8/R9/R10 + 附录 B.1.2 已移行 + ports.py 归属表已改，0 死链。

## Open
- 无（四个方向性分叉用户已全选推荐：确定性纯函数默认 / 复用单一检测器 / 复用 R1 EventStore / 全量实现全绿）。
- 被否决方案（记于此防复活）：R8 默认直接 yield Invoke(llm)（违 D-8 与不调真实 LLM 铁律）；R9 新建独立检测器（造第二套）；R10 自建 EventStore（与 R1 重复）；先核心闭环再补边界（用户要全量）。
