# R7 设计审查问题清单（场景9 · 只读核对 · 2026-11-20）

> 对照：R7 模块详设 `docs/02-模块详设/Agent/R7-上下文与记忆.md`（下称「详设」）、总设计 R7 章节 `docs/01-总览与设计/AGENT-总设计.md:1055-1147`（下称「总设」）。
> 所有结论经源码逐行核实（两个独立子代理核对 + 主控打开源码复核）。

## Blocker（致命，不改无法落地）

- **B1 生命周期 hook 零 dispatch**。详设流程图:27/30/32/35 与总设:1073/1078/1080/1083 声称由 `before_model`/`after_compact`/`after_tool`/`session_end` 触发组装/重注入/卸载/提炼；但这四个点只是 `src/hooks/types.py:72-106` 的 `LifecycleEvent` 枚举常量，**全 src 零消费、零 dispatch**。Runner 唯一 dispatch 是 `src/runtime/runner.py:346` 对 `Effect` 的判决；`Host.dispatch`/`Host.call`（`src/hooks/v5.py:372-455`）只跑 invoke 级 6 个 hook（`:49-56`）。后果：验收标准 1/3（详设:104/106）、门禁 R7-G2（after_compact 重注入）全部悬空。〔需决策：改内核加 dispatch vs R7 自闭环显式调用〕
- **B2 "hook priority 保证先写 cold 再压缩"无实现基础**。详设:20、总设:1066 靠 priority 保证顺序；但 `src/hooks/v5.py:292` 明确 Plugin「不声明 mode/priority/events」，`Hook.tap`（`:174-177`）仅 list append，无 priority 概念。后果：验收标准 3 / 门禁 R7-G3「顺序确定性」无载体。〔随 B1 决策〕
- **B3 Offloader 依赖 StorePort 语义错位**。详设:56 `offload(value, store: StorePort) -> str`、:79/89「句柄 store://id + 摘要」「句柄必须可读」；但 `StorePort.save/load`（`src/hooks/protocols.py:190-196`）是 `session_id → 整体快照 dict` 的存取，无「存单值→返 id→按 id 读回」能力；且 `Offloader` 协议本身不在源码。后果：T03 句柄可读、R7-G2 卸载门禁无法实现。〔需决策：专用 BlobPort vs 复用 StorePort〕
- **B4 MemoryPort 签名逐字不符**。详设:58-60、总设:1110-1112 写 `read(self, ns, key, ctx)` 等；源码 `src/hooks/protocols.py:168-178` 是 `namespace: str` + 完整类型标注（`ctx: PortCtx`）。`@runtime_checkable` Protocol 参数名具契约意义——按设计实现的 MemoryStore 经关键字调用即失败。

## Should（重要，影响自洽/可用性）

- **S1 语义召回无接口落点**。详设:7/96 声称复用 R6 Embedder/Store 做 before_model 语义召回，但 `MemoryPort.read/write/forget` 无任何 embedding/query 参数；R6 仅以 `QueryPurpose.MEMORY`（`src/harness/rag/types.py:31-39`）预留。语义召回在接口层缺失。〔需决策〕
- **S2 warm(PG+pgvector) 无实现**。源码只有 `InMemoryVectorStore`（`src/harness/rag/store.py:53`），无 PG/pgvector 实现与注入装配点；设计未声明默认用内存、PG 为可注入点。
- **S3 TokenBudget 归属不明**。真实定义在 R6（`src/harness/rag/types.py:116`），设计依赖表（详设:7/96）未说明从 R6 导入；R1 另有不同构的 `Budgets`（`src/runtime/types.py:204-222`）。
- **S4 assemble 返回类型不自洽**。详设:49 `assemble -> list[Message]` 与 `AgentState.messages: tuple[Message,...]`（`src/runtime/types.py:310`，只增不减）无法直接衔接；「裁剪是视图层」但接口未表达视图返回。
- **S5 门禁/测试基建整体缺失**。`tests/r7/`、`make demo-r7`、`gate-r7`、`MODULES["r7"]`（`gate_check.py:45-123`）、`TARGETS["r7"]`（`ast_scan.py:370-371`）全不存在；详设 header「门禁 key：无（设计态）」与 §7~§10 假定入口存在自相矛盾。〔实现阶段补齐，非设计缺陷〕

## Nit（次要）

- N1 详设:58-60 的 `ctx` 缺类型标注（源码为 `PortCtx`）。
- N2 "HookPoint 枚举"表述不准，实为 `LifecycleEvent`。
- N3 详设:3「当前仓库已实现 R1~R4」过时（实际已到 R6，R5/R6 均已勾销）。

## 闸门结论
设计不能原样落地：B1~B4 必须在补充设计阶段解决；其中 B1/B2 是架构方向决策（是否动 R1/R2 稳定内核），B3/S1 是契约设计决策（Port 划分）。已停下请用户拍板，未写任何生产代码。
