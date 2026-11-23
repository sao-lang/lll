# 需求覆盖矩阵 · 可插拔注入主线重构（场景7 full）

> 需求列逐条抄用户原话，防止"翻译时丢需求"。决策四类：采用/否决/进Open/范围外。
> 8 个 Blocker 来自全模块接线体检（`.review.md`）；4 个 frontier 来自 23:36 AskUserQuestion 用户已拍板。

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R1 "用户可插拔注入，里面的东西都是自动编排的" | 采用 注入即生效真相源 | FR-1 | AC-1 注入能力/策略/工具后断言进执行链 |
| R2 "其他所有的可注入模块肯定都有问题"——全模块修 | 采用 6 大模块群全扫 | FR-2 | AC-2 8 Blocker 各有失败测试→GREEN |
| R3 "实现一个 agent，初始代码应该不需要多少" | 采用 单一语义门面零胶水 | FR-3 | AC-3 5 行目标 demo 真跑出结果 |
| R4 "策略/工具这些用户可以自己决定" | 采用 策略真接决策点 | FR-4 | AC-4 注入策略改变输出的测试 PASS |
| R5 "不能做兼容，不能打补丁" | 采用 删旧路径零残留 | FR-5 | AC-5 removal_check + purity --strict rc=0 |
| R6 "HITL 这些核心功能都会有问题" | 采用 门面注入安全链 | FR-6 | AC-6 say() 注入 SecurityCapability 真生效 |
| R7 （已拍板）两个 Agent 合并到单一门面 | 采用 api.Agent 唯一 | FR-7 | AC-7 compose.Agent 死注入字段删除零残留 |
| R8 （已拍板）schema 逐参数推导 + 修三链 | 采用 @tool 推导 | FR-8 | AC-8 city:str 生成 schema 且到 LLM |
| R9 （已拍板）新建 MemoryCapability 挂钩子 | 采用 能力类+钩子 | FR-9 | AC-9 say 自动 recall/写回真发生 |
| R10 （已拍板）出完整规格再动手 | 采用 本契约 | FR-10 | AC-10 coverage_check rc=0 |
| B1 10 算子 8 个死 + state-port 必 PORT_MISSING | 采用 删死算子/修 state port | FR-11 | AC-11 真实 Runner 下每算子行为测试 |
| B2 Planner/Executor/Reflect 只取 prompt | 采用 真调方法 | FR-12 | AC-12 注入策略方法被调用断言 |
| B3 inject_strategy 被跳过 | 采用 接进决策点 | FR-13 | AC-13 注入改变输出 |
| B4 Router text="" 裂缝 | 采用 接 current_input | FR-14 | AC-14 分类器收到真实文本 |
| B5 门面 Security 被默认新建替换 | 采用 门面传 security | FR-15 | AC-6 覆盖 |
| B6 replay_snapshot 静默丢状态 | 采用 restore_checkpoint | FR-16 | AC-16 load→replay 恢复 output/messages |
| B7 MemoryStore write≠recall | 采用 统一存储通道 | FR-17 | AC-17 write 后 recall 命中 |
| B8 guardrail 假审批 + 沙盒非 OS 隔离 | 采用 接真审批/诚实标注 | FR-18 | AC-18 审批分支真触发 |
| B9 Trace/CacheTelemetry 恒 0 | 采用 真实埋点接自动链 | FR-19 | AC-19 span duration>0 / hit rate 真统计 |
| B10 RAG/Context build() 死外壳 + OffloadPolicy 死 | 采用 删死壳或接线 | FR-20 | AC-20 build() 被消费或删除零残留 |
| B11 测试反向固化错误行为 | 采用 改期望为真行为 | FR-21 | AC-21 三处 defect 测试改写 |
| B12 三套 Agent 主体互不通 | 采用 合并（=FR-7） | FR-7 | AC-7 覆盖 |
| 沙盒 OS 隔离 / 外部导出器 / 跨进程持久化 | 范围外 | — | — | 诚实边界，非本次主线；demo 已披露 |
| 意图四级级联 L3/L4 的 task/tier 透传 | 进Open(U-1) | — | — | 待确认是否本轮，属 B4 同源 |
