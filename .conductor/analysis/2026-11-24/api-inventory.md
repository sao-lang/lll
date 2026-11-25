# src/ 全量公开 API 清单（introspection 穷举）

模块数=122，导入失败=0。
来源：`api_inventory.py`（AST + importlib，按定义模块去重，公开方法级）。

## `api` — src/api/__init__.py

- `__all__` (28): `PROFILES`, `profile_names`, `PortRegistry`, `PROTOCOL_NAMES`, `StreamGateway`, `static_check_api`, `KillSwitch`, `CostLimiter`, `Rollout`, `FeatureFlags`, `Governance`, `AgentBuilder`, `Agent`, `dsl`, `CLI`, `CLIResult`, `AgentRuntime`, `assemble_runtime`, `capability_tools`, `ApprovingHumanPort`, `VerifyingAuditPort`, `CheckpointEventStore`, `R1AuditEventSink`, `replay_audit_events`, `bind_stream_projection`, `redact_output`, `verify_audit_chain`, `STREAM_TOPIC`


## `api.agent` — src/api/agent.py

- `__all__` (1): `Agent`

- 类 `Agent` (bases: object) — 18 个公开方法
  - `aresume(self, answer: 'str') -> 'str'` — 异步续跑入口：answer 回填挂起会话（wait_human → resumed），跑完出结果。
  - `asay(self, text: 'str') -> 'str'` — 异步语义入口：输入经 ContextVar 按调用链绑定（并发安全）。
  - `capability(self, cap: 'Provider') -> "'Agent'"` — 注入能力（按 name 登记）；装配期真 install 进持久 Registry。
  - `clear_history(self) -> 'None'` — 清空对话历史（新话题）；不影响长期记忆。
  - `evaluate(self, draft: 'str', goal: 'str' = '') -> 'object'` — 语义入口：用注入的 ReflectionCapability 评审草稿（不跑架构）。
  - `history(self) -> 'tuple[dict[str, str], ...]'` — 跨 say 对话历史（只读快照）：每轮 say 追加 user/assistant 问答对。
  - `inject_strategy(self, strategy: 'Provider') -> "'Agent'"` — 注入策略：纯签名履约校验 + 真接进决策点（R13 第四要素，Round3 Blk-1）。
  - `on(self, phase: 'LifecyclePhase | str', fn: 'NoticeFn', *, name: 'str' = '') -> "'Agent'"` — 订阅 lifecycle 相位（一等 hook API）：say() 前注册，装配期真订阅本次运行的 bus。
  - `recognize_intent(self, text: 'str') -> 'object'` — 语义入口：用注入的 CognitionCapability 识别意图（未注入则 fail-closed）。
  - `register_port(self, port: 'str', handler: 'Callable[[Effect, DynamicValue], Awaitable[Result]]') -> "'Agent'"` — 注入自定义 port handler（Blk-2：非 llm/tool port 不再断链）。
  - `register_tool(self, name: 'str', fn: 'Callable[..., DynamicValue]', **kwargs: 'DynamicValue') -> "'Agent'"` — 注册工具到门面（tool port 走四级路由）。
  - `resume(self, answer: 'str') -> 'str'` — 同步续跑入口：HITL 闭环（say 挂起 → resume → 最终结果）。
  - `run(self, text: 'str') -> 'str'` — 语义入口：say 的同义。
  - `say(self, text: 'str') -> 'str'` — 同步语义入口：running loop 中走独立线程，只返回白名单文本。
  - `score_case(self, case_id: 'str', output: 'str') -> 'float'` — 评测语义入口：用注入的 EvalCapability 对输出打分（EvalPort）。
  - `stream(self) -> 'InMemoryEventStream'` — 可观测事件流底座（一等公开属性）：内置自建，注入则复用注入实例。
  - `trace(self) -> "'Trace'"` — 自动链路追踪（一等公开属性）：llm/tool 调用自动产 span。
  - `use(self, cap: 'Provider') -> "'Agent'"` — 能力插拔的一等 API（用户目标写法）：capability() 的链式别名。
- 类 `ArchLike` (bases: Protocol) — 0 个公开方法
- 类 `ContextVar` (bases: object) — 0 个公开方法

## `api.builder` — src/api/builder.py

- `__all__` (1): `AgentBuilder`

- 类 `AgentBuilder` (bases: object) — 16 个公开方法
  - `applied_profiles(self) -> 'tuple[str, ...]'` — 已应用 profile 快照。
  - `approve(self, port: 'ApprovingHumanPort') -> "'AgentBuilder'"` — ① 注入审批包装器（HUMAN Port 的 CRITICAL 分支真实消费）。
  - `audit(self, port: 'VerifyingAuditPort') -> "'AgentBuilder'"` — ② 注入审计包装器（每次 record 验链）。
  - `build(self) -> 'Session'` — 治理判定 + 装配期检查 + 产 Session（委托 R12 Agent）。
  - `capability(self, cap: 'Capability') -> "'AgentBuilder'"` — 注入自定义能力（覆盖同名预设）。
  - `capability_names(self) -> 'tuple[str, ...]'` — 已登记能力名快照。
  - `checkpoint(self, mgr: 'CheckpointManager | None' = None) -> "'AgentBuilder'"` — ⑤ 注入 checkpoint 适配器（交 Runner(store=...)）。
  - `checkpoint_store(self) -> 'CheckpointEventStore | None'` — ⑤ 返回 checkpoint 存储适配器（供 Runner(store=...) 注入）。
  - `govern(self, gov: 'Governance') -> "'AgentBuilder'"` — 注入治理门面；build 时真调用 gov.allow（不静默忽略）。
  - `profile(self, name: 'str') -> "'AgentBuilder'"` — 应用 profile 预设（装配期按清单装能力，未知名拒绝）。
  - `project_events(self, stream: 'InMemoryEventStream') -> "'AgentBuilder'"` — ④ 订阅三相位并发布 EventEnvelope（bus 交 Runner/ModelGateway）。
  - `redact(self, value: 'object', ctx: 'PortCtx') -> 'SafetyVerdict'` — ③ 生产路径消费：对输出执行 secret 脱敏（返回 SafetyVerdict）。
  - `registry(self) -> 'Registry'` — 返回共用 Registry（R9 三 Port 由 SecurityCapability 注册）。
  - `runtime(self, call: 'object', *, checkpoint_every: 'int' = 1) -> 'AgentRuntime'` — ⑤④②① 真实运行期装配：把持有的接线注入 Runner/ToolRegistry。
  - `secrets(self, refs: 'Sequence[str]') -> "'AgentBuilder'"` — ③ 注入 secret 引用（输出护栏 redact 用）。
  - `stream_bus(self) -> 'LifecycleBus | None'` — ④ 返回事件总线（供 Runner(notify=runner_notifier(bus)) 注入）。

## `api.cli` — src/api/cli.py

- `__all__` (2): `CLIResult`, `CLI`

- 类 `CLI` (bases: object) — 2 个公开方法
  - `commands(self) -> 'tuple[str, ...]'` — 已注册命令名。
  - `run(self, command: 'str', **kwargs: 'DynamicValue') -> 'CLIResult'` — 执行命令；未知命令返回 ok=False（不静默忽略）。
- 类 `CLIResult` (bases: object) — 0 个公开方法

## `api.dsl` — src/api/dsl.py

- `__all__` (14): `JoinMode`, `ProgramFn`, `branch`, `custom_arch`, `fallback`, `gate`, `loop`, `loop_until`, `map_effect`, `map_state`, `par`, `race`, `retry`, `seq`

- 函数 `custom_arch(factory: 'Callable[[], Program]', *, name: 'str' = 'custom', version: 'str' = '1.0.0', requires: 'tuple[str, ...]' = (), static_check: 'Callable[[], StaticCheckReport] | None' = None) -> 'Arch'` — 把用户用 FP 算子组合出的 ProgramFn 封装成内置同形的 Arch。

## `api.governance` — src/api/governance.py

- `__all__` (5): `KillSwitch`, `CostLimiter`, `Rollout`, `FeatureFlags`, `Governance`

- 类 `CostLimiter` (bases: object) — 2 个公开方法
  - `charge(self, amount: 'float') -> 'bool'` — 尝试计费；超 cap 拒绝且不入账。
  - `remaining(self) -> 'float'` — 剩余额度（下限 0）。
- 类 `FeatureFlags` (bases: object) — 2 个公开方法
  - `is_on(self, name: 'str') -> 'bool'` — 是否开启（未设置默认关）。
  - `set(self, name: 'str', value: 'bool') -> 'None'` — 设置开关。
- 类 `Governance` (bases: object) — 1 个公开方法
  - `allow(self, action: 'str', *, cost: 'float' = 0.0, key: 'str' = '') -> 'bool'` — 统一判定：熔断/灰度/成本任一拒绝即拒绝。
- 类 `KillSwitch` (bases: object) — 4 个公开方法
  - `allow(self, action: 'str' = '') -> 'bool'` — 是否放行（熔断时一律拒绝）。
  - `disable(self) -> 'None'` — 关闭熔断。
  - `enable(self, reason: 'str' = 'maintenance') -> 'None'` — 开启熔断（记原因供审计）。
  - `enabled(self) -> 'bool'` — 是否已熔断。
- 类 `Rollout` (bases: object) — 1 个公开方法
  - `enabled(self, key: 'str') -> 'bool'` — key 是否命中放量（同一 key 恒定结果）。

## `api.ports` — src/api/ports.py

- `__all__` (2): `PortRegistry`, `PROTOCOL_NAMES`

- 函数 `PROTOCOL_NAMES() -> 'tuple[str, ...]'` — 返回可注册 Port 名（∈ PORT_PROTOCOLS，单一来源）。
- 类 `PortRegistry` (bases: object) — 4 个公开方法
  - `has(self, name: 'str') -> 'bool'` — 是否已注册。
  - `register(self, name: 'str', adapter: 'Provider') -> 'None'` — 注册适配器；未知 Port 名 ValueError（D-2 不可运行时新增种类）。
  - `replace(self, name: 'str', adapter: 'Provider') -> 'None'` — 替换已注册适配器（换实现入口）。
  - `resolve(self, name: 'str') -> 'Provider'` — 解析适配器；未注册 KeyError。

## `api.profiles` — src/api/profiles.py

- `__all__` (2): `PROFILES`, `profile_names`

- 函数 `profile_names() -> 'tuple[str, ...]'` — 返回已登记 profile 名（不可变顺序快照）。

## `api.runtime` — src/api/runtime.py

- `__all__` (3): `AgentRuntime`, `assemble_runtime`, `capability_tools`

- 函数 `assemble_runtime(call: 'CallFn | None' = None, *, approval: 'ApprovingHumanPort | None' = None, audit: 'VerifyingAuditPort | None' = None, stream: 'InMemoryEventStream | None' = None, manager: 'CheckpointManager | None' = None, tools: 'ToolRegistry | None' = None, security: 'SecurityCapability | None' = None, bus: 'LifecycleBus | None' = None, store: 'EventStore | None' = None, sink: 'R1AuditEventSink | None' = None, checkpoint_every: 'int' = 1, call_factory: 'Callable[[CallFn], CallFn] | None' = None, tool_setup: 'Callable[[ToolRegistry], None] | None' = None) -> 'AgentRuntime'` — 真实装配：构造 Runner/Registry/三 Port 接线的运行期。
- 函数 `capability_tools(approval: 'ApprovingHumanPort', audit: 'VerifyingAuditPort', *, sandbox: 'SandboxPort | None' = None) -> 'ToolsCapability'` — 用已接线的三 Port 构造 ToolsCapability（生产装配入口）。
- 类 `AgentRuntime` (bases: object) — 3 个公开方法
  - `model_gateway(self, llm: 'object') -> 'ModelGateway'` — 用同一 bus 构造模型网关（after_model 相位真实接入）。
  - `resume(self, session: 'Session', answer: 'DynamicValue') -> 'object'` — 挂起后精确续跑（委托 Runner.resume）。
  - `run(self, session: 'Session') -> 'object'` — 真实执行（绑定活会话供 R1 seq 同源投影，委托 Runner.run）。

## `api.static_api` — src/api/static_api.py

- `__all__` (1): `static_check_api`

- 函数 `static_check_api(arch: 'object') -> 'StaticCheckReport'` — 聚合架构的 static_check 报告（无报告时空 ok）。

## `api.stream` — src/api/stream.py

- `__all__` (1): `StreamGateway`

- 类 `StreamGateway` (bases: object) — 3 个公开方法
  - `project(self, projection: 'Projection', topic: 'str', since: 'int | None' = None) -> 'ReadModel'` — 按 topic 投影读模型（同步快照，异步流由调用方 drain）。
  - `publish(self, event: 'EventEnvelope') -> 'None'` — 发布一个只读信封（外部系统经此接，不直接 append 内核事件）。
  - `stream(self) -> 'InMemoryEventStream'` — 返回只读订阅视图（实际是 AsyncIterator 提供者，消费方不写）。

## `api.wiring` — src/api/wiring.py

- 函数 `bind_stream_projection(stream: 'InMemoryEventStream | None' = None, *, clock: 'Callable[[], float] | None' = None, seq_source: 'R1SeqTracker | None' = None) -> 'LifecycleBus'` — ④ 事件流投影接线：LifecycleBus 订阅三相位 → publish EventEnvelope。
- 函数 `redact_output(value: 'object', ctx: 'PortCtx', *, secret_refs: 'Sequence[str]', source: 'str' = 'tool', direction: 'str' = 'output', guardrail: 'object | None' = None) -> 'SafetyVerdict'` — ③ secret 脱敏接线：构造 SecurityCtx 注入 secret_refs，过护栏得 redact 裁决。
- 函数 `replay_audit_events(sink: 'R1AuditEventSink', session_id: 'str') -> 'tuple[AuditEvent, ...]'` — 从 R1 EventStore 回放审计事件，重建 AuditEvent 序列（同源可回放）。
- 函数 `verify_audit_chain(port: 'VerifyingAuditPort') -> 'bool'` — 公开校验 API：对当前审计链验签，True/False（不抛）。
- 类 `ApprovingHumanPort` (bases: object) — 1 个公开方法
  - `ask(self, question: 'str', options: 'list[str] | None', ctx: 'PortCtx', *, danger: 'DangerLevel | None' = None) -> 'str'` — 先 should_approve，再限时等待；任何超时/异常一律 deny（fail-closed）。
- 类 `CheckpointEventStore` (bases: object) — 3 个公开方法
  - `load(self, session_id: 'str') -> 'JsonSnapshot | None'` — 加载：真委托共享的 R1 EventStore（非本地 dict 桩）。
  - `manager(self) -> 'CheckpointManager'` — 返回被消费的 CheckpointManager（供装配期核对/回放）。
  - `save(self, session_id: 'str', snapshot: 'JsonSnapshot') -> 'None'` — 保存：单次写入共享 R1 EventStore（B-1 修复）+ 更新 R1 序号追踪器（B-2）。
- 类 `R1AuditEventSink` (bases: AuditStore) — 2 个公开方法
  - `append(self, event: 'dict[str, DynamicValue]') -> 'None'` — 追加一条审计事件到该 session 的 R1 快照（append-only）。
  - `key_of(self, session_id: 'str') -> 'str'` — 审计保留 session 键（与业务 session 隔离，避免污染事件日志）。
- 类 `R1SeqTracker` (bases: object) — 3 个公开方法
  - `bind_session(self, session: 'object') -> 'None'` — AgentRuntime.run/resume 绑定当前会话；current() 从中读活事件数。
  - `current(self) -> 'int | None'` — 返回 R1 权威事件序号：活会话 len(events) 优先，否则最近 checkpoint 值。
  - `observe(self, snapshot: 'JsonSnapshot') -> 'None'` — 从 R1 规范化快照的 events 里取最后一条 Event.seq（无活会话时的兜底）。
- 类 `VerifyingAuditPort` (bases: object) — 4 个公开方法
  - `bind_sink(self, sink: 'AuditStore') -> 'None'` — 装配期把同源 sink 绑到 inner AuditLogger（外部注入审计也同源）。
  - `events(self) -> 'tuple[object, ...]'` — 不可变事件快照（供回放/核对）。
  - `record(self, event: 'dict[str, DynamicValue]', ctx: 'PortCtx') -> 'None'` — 追加事件后立刻验链（链断即抛，不进后续步骤）。
  - `verify_external(self, events: 'Sequence[object]') -> 'None'` — 校验给定事件序列；断链 ValueError（供外部注入篡改检测）。

## `archs` — src/archs/__init__.py

- `__all__` (24): `Arch`, `JoinStrategy`, `MajorityVote`, `StaticCheckIssue`, `StaticCheckReport`, `NamedStrategy`, `Planner`, `Executor`, `RePlanner`, `ReflectStrategy`, `IntentStrategy`, `IntentResult`, `ChildSpec`, `SkillSpec`, `ToolUse`, `ReAct`, `PlanExecute`, `Reflection`, `Router`, `RouterSkill`, `Supervisor`, `Handoff`, `Blackboard`, `Graph`


## `archs.base` — src/archs/base.py

- `__all__` (14): `StaticCheckIssue`, `StaticCheckReport`, `Arch`, `JoinStrategy`, `MajorityVote`, `NamedStrategy`, `Planner`, `Executor`, `RePlanner`, `ReflectStrategy`, `IntentStrategy`, `IntentResult`, `ChildSpec`, `SkillSpec`

- 类 `Arch` (bases: Protocol) — 2 个公开方法
  - `compile(self) -> 'Program'` — 
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `ChildSpec` (bases: NamedStrategy, Protocol) — 0 个公开方法
- 类 `JoinStrategy` (bases: Protocol) — 1 个公开方法
  - `join(self, results: 'list[Any]') -> 'Any'` — 
- 类 `MajorityVote` (bases: object) — 1 个公开方法
  - `join(self, results: 'list[Any]') -> 'Any'` — 
- 类 `NamedStrategy` (bases: Protocol) — 0 个公开方法
- 类 `SkillSpec` (bases: NamedStrategy, Protocol) — 0 个公开方法
- 类 `StaticCheckIssue` (bases: object) — 0 个公开方法
- 类 `StaticCheckReport` (bases: object) — 1 个公开方法
  - `ok(self) -> 'bool'` — 

## `archs.recipes` — src/archs/recipes.py

- `__all__` (10): `Blackboard`, `Graph`, `Handoff`, `PlanExecute`, `ReAct`, `Reflection`, `Router`, `RouterSkill`, `Supervisor`, `ToolUse`

- 类 `Blackboard` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — Blackboard = loop_until(轮次) + par(多代理并行派发)，算子承担并行与循环。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `Graph` (bases: object) — 5 个公开方法
  - `compile(self) -> 'Program'` — Graph = loop_until(节点遍历)，算子承担图行走循环与环检测。
  - `edge(self, src: 'str', dst: 'str', *, cond: 'Callable[[AgentState], str | None] | None' = None) -> 'Graph'` — 
  - `exit(self, id: 'str') -> 'Graph'` — 
  - `node(self, id: 'str', program: 'ProgramFn | Arch', *, inputs: 'tuple[str, ...]' = (), outputs: 'tuple[str, ...]' = ()) -> 'Graph'` — 
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `Handoff` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — Handoff = loop_until(单跳角色)，算子承担跳数循环与接力决策。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `PlanExecute` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — PlanExecute = plan → loop_until(步执行 + replan 预算)，由算子承担步循环。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `ReAct` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — ReAct = loop_until(llm→tools 步) + map_effect(消息累积)，由组合算子封装。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `Reflection` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — Reflection = draft → loop_until(critique→revise)，算子承担反思循环。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `Router` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — Router = branch_value(意图源, 决策, arms)，由值驱动分支算子承担路由。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `RouterSkill` (bases: Router) — 1 个公开方法
  - `compile(self) -> 'Program'` — RouterSkill = branch_value(路由) + seq(skill.body, downstream)，算子承担路由与顺序。
- 类 `Supervisor` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — Supervisor = par(子 Agent 扇出) + join 聚合，par 承担并行批与按槽回注。
  - `static_check(self) -> 'StaticCheckReport'` — 
- 类 `ToolUse` (bases: object) — 2 个公开方法
  - `compile(self) -> 'Program'` — ToolUse = par(并行工具) / reduce(seq, 串行工具)，由组合算子承担控制流。
  - `static_check(self) -> 'StaticCheckReport'` — 

## `compose` — src/compose/__init__.py

- `__all__` (72): `seq`, `par`, `branch`, `race`, `loop`, `map_state`, `map_effect`, `fallback`, `gate`, `retry`, `current_input`, `bind_input`, `CallFn`, `build_call`, `retry_call`, `timeout`, `danger_route`, `injection_guard`, `dispatch`, `make_dispatch`, `layer_program`, `Agent`, `CallContext`, `FaultRecord`, `RoutingTarget`, `Invocation`, `Next`, `FAULT_KEY`, `ROUTING_TARGET_KEY`, `FailureClass`, `CallState`, `DangerLevel`, `DisclosurePacket`, `LLMRequest`, `LLMResponse`, `PortCtx`, `SkillManifest`, `StreamEvent`, `StreamEventKind`, `ToolCall`, `ToolResult`, `ToolSpec`, `Usage`, `make_call`, `Capability`, `HookBusError`, `Provider`, `ProviderRegistry`, `Registry`, `PORT_PROTOCOLS`, `AuditPort`, `AuditStore`, `DisclosureIndex`, `EvalPort`, `EventStreamPort`, `HumanPort`, `LLMPort`, `MCPPort`, `MemoryPort`, `RAGPort`, `SandboxPort`, `SkillPort`, `StorePort`, `TelemetryExporter`, `TelemetryPort`, `ToolPort`, `PHASE_COUNT`, `ErrorHook`, `LifecycleBus`, `LifecyclePhase`, `Notice`, `runner_notifier`


## `compose.agent` — src/compose/agent.py

- `__all__` (1): `Agent`

- 类 `Agent` (bases: object) — 4 个公开方法
  - `build(self, *, session_id: 'str' = 'agent') -> 'Session'` — 装配期校验 + 产 Session；有错误码时 ValueError 拒绝进运行期。
  - `capability(self, cap: 'Capability') -> "'Agent'"` — 装一个能力（install 注册其 Port 到共用 Registry）。
  - `registry(self) -> 'Registry'` — 返回共用 Registry（装配期核对已注册 Port）。
  - `static_check(self) -> 'object'` — 委托 Arch.static_check（无 Arch 时返回 ok 报告）。

## `compose.call` — src/compose/call.py

- `__all__` (6): `CallFn`, `build_call`, `retry_call`, `timeout`, `danger_route`, `injection_guard`

- 函数 `build_call(base: 'CallFn', *, decorators: 'Sequence[Callable[[CallFn], CallFn]]' = ()) -> 'CallFn'` — 构造注入 R1 的 call：③装饰器链（内层先注册）。
- 函数 `danger_route(base: 'CallFn', registry: 'object') -> 'CallFn'` — ③危险路由：按 payload.danger 经 registry 路由到沙箱 Port（沙箱是⑤Port）。
- 函数 `injection_guard(base: 'CallFn', detector: 'object') -> 'CallFn'` — ③注入检测：不可信输入过 detector，命中则拦截（fail-closed）。
- 函数 `retry_call(base: 'CallFn', *, attempts: 'int' = 3, backoff: 'float' = 0.0, retry_on: 'Callable[[Result], bool] | None' = None) -> 'CallFn'` — ③调用级 retry：失败可重试时循环调 base（与 A 类流程级 retry 区分，D-5）。
- 函数 `timeout(base: 'CallFn', *, seconds: 'float') -> 'CallFn'` — ③调用级 timeout：wait_for 包 base（R1 无 wait_for 限制见 D-5）。

## `compose.context` — src/compose/context.py

- `__all__` (7): `CallContext`, `Invocation`, `Next`, `FaultRecord`, `RoutingTarget`, `FAULT_KEY`, `ROUTING_TARGET_KEY`

- 类 `CallContext` (bases: object) — 6 个公开方法
  - `fault(self) -> 'FaultRecord | None'` — 读取当前故障记录；无则 None。
  - `meta_view(self) -> 'Mapping[str, object]'` — 返回 meta 只读快照（供 notify 投影，不暴露可变字典）。
  - `routing_target(self) -> 'RoutingTarget | None'` — 读取 typed 降级目标；无则 None。
  - `session_id(self) -> 'str'` — 从 session 派生会话标识（B1 选级隔离维度单一入口）。
  - `write_fault(self, fault: 'FaultRecord') -> 'None'` — 写入具名故障记录。
  - `write_routing_target(self, target: 'RoutingTarget') -> 'None'` — 写入 typed 降级目标。
- 类 `Invocation` (bases: object) — 6 个公开方法
  - `fault(self) -> 'FaultRecord | None'` — 读取当前故障记录；无则 None。
  - `meta_view(self) -> 'Mapping[str, object]'` — 返回 meta 只读快照（供 notify 投影，不暴露可变字典）。
  - `routing_target(self) -> 'RoutingTarget | None'` — 读取 typed 降级目标；无则 None。
  - `session_id(self) -> 'str'` — 从 session 派生会话标识（B1 选级隔离维度单一入口）。
  - `write_fault(self, fault: 'FaultRecord') -> 'None'` — 写入具名故障记录。
  - `write_routing_target(self, target: 'RoutingTarget') -> 'None'` — 写入 typed 降级目标。
- 类 `MappingProxyType` (bases: object) — 0 个公开方法

## `compose.dispatch` — src/compose/dispatch.py

- `__all__` (2): `dispatch`, `make_dispatch`

- 函数 `dispatch(ports: 'PortMap', effect: 'Effect', ctx: 'object') -> 'Result'` — 便捷 dispatch：直接按 ports 分派单个 Effect（make_dispatch 的无封装形态）。
- 函数 `make_dispatch(ports: 'PortMap') -> 'CallFn'` — 构造 dispatch：按 effect.port 查 Port 处理器并调用，异常折叠为 Result。

## `compose.dto` — src/compose/dto.py

- `__all__` (13): `DangerLevel`, `DisclosurePacket`, `SkillManifest`, `LLMRequest`, `LLMResponse`, `PortCtx`, `StreamEvent`, `StreamEventKind`, `ToolCall`, `ToolResult`, `ToolSpec`, `Usage`, `make_call`

- 函数 `make_call(name: 'str', args: 'dict[str, DynamicValue]', call_id: 'str' = '', *, danger: 'DangerLevel' = <DangerLevel.SAFE: 'SAFE'>, source: 'str' = 'local') -> 'ToolCall'` — 构造 ToolCall 的便捷函数。
- 类 `DangerLevel` (bases: str, Enum) — 1 个公开方法
  - `coerce(<no-signature>)` — 归一外部字符串/枚举为成员；未知或 None 回退到最严格的 CRITICAL（fail closed）。
- 类 `DisclosurePacket` (bases: object) — 0 个公开方法
- 类 `LLMRequest` (bases: _Strict) — 0 个公开方法
- 类 `LLMResponse` (bases: _Strict) — 0 个公开方法
- 类 `PortCtx` (bases: _Strict) — 0 个公开方法
- 类 `SkillManifest` (bases: object) — 0 个公开方法
- 类 `StreamEvent` (bases: object) — 0 个公开方法
- 类 `StreamEventKind` (bases: str, Enum) — 0 个公开方法
- 类 `ToolCall` (bases: _Strict) — 0 个公开方法
- 类 `ToolResult` (bases: _Strict) — 0 个公开方法
- 类 `ToolSpec` (bases: _Strict) — 0 个公开方法
- 类 `Usage` (bases: _Strict) — 0 个公开方法

## `compose.failure` — src/compose/failure.py

- `__all__` (1): `FailureClass`

- 类 `FailureClass` (bases: object) — 0 个公开方法

## `compose.guards` — src/compose/guards.py

- `__all__` (3): `FaultRecord`, `RoutingTarget`, `CallState`

- 类 `CallState` (bases: object) — 5 个公开方法
  - `fault(self) -> 'FaultRecord | None'` — 读取当前故障记录；无或类型不符返回 None。
  - `routing_target(self) -> 'RoutingTarget | None'` — 读取 typed 降级目标；无或类型不符返回 None。
  - `session_id(self) -> 'str'` — 从 ctx 派生会话标识（B1 选级隔离维度的单一入口）。
  - `write_fault(self, fault: 'FaultRecord') -> 'None'` — 写入具名故障记录（领域策略报告故障的唯一入口）。
  - `write_routing_target(self, target: 'RoutingTarget') -> 'None'` — 写入 typed 降级目标（降级选级后、网关消费前）。
- 类 `FaultRecord` (bases: object) — 0 个公开方法
- 类 `RoutingTarget` (bases: object) — 0 个公开方法

## `compose.input` — src/compose/input.py

- `__all__` (2): `current_input`, `bind_input`

- 函数 `current_input() -> 'str'` — 当前调用链的输入文本（架构内部读取；每线程/任务隔离）。
- 类 `ContextVar` (bases: object) — 0 个公开方法
- 类 `Token` (bases: object) — 0 个公开方法

## `compose.notify` — src/compose/notify.py

- `__all__` (6): `PHASE_COUNT`, `ErrorHook`, `LifecycleBus`, `LifecyclePhase`, `Notice`, `runner_notifier`

- 函数 `runner_notifier(bus: 'LifecycleBus') -> 'Callable[[str, Mapping[str, Any]], None]'` — 把 Runner 的 notify 回调桥接到 LifecycleBus（装配层接线入口）。
- 类 `LifecycleBus` (bases: object) — 3 个公开方法
  - `emit(self, notice: 'Notice') -> 'None'` — 广播通知：异常就地隔离，保证一个坏订阅者不影响其余订阅与主流程。
  - `on(self, phase: 'LifecyclePhase', fn: 'NoticeFn', *, name: 'str' = '') -> 'LifecycleBus'` — 订阅一个相位。
  - `subscribe(self, plugin: 'object') -> 'LifecycleBus'` — 注册 Lifecycle 插件（契约：name + register(bus)）。
- 类 `LifecyclePhase` (bases: StrEnum) — 0 个公开方法
- 类 `MappingProxyType` (bases: object) — 0 个公开方法
- 类 `Notice` (bases: object) — 1 个公开方法
  - `from_invocation(<no-signature>)` — 由 Invocation 投影出 Notice（上下文单一：Notice 只读，不复制可变状态）。

## `compose.ports` — src/compose/ports.py

- `__all__` (17): `PORT_PROTOCOLS`, `AuditPort`, `AuditStore`, `DisclosureIndex`, `EvalPort`, `EventStreamPort`, `HumanPort`, `LLMPort`, `MCPPort`, `MemoryPort`, `RAGPort`, `SandboxPort`, `SkillPort`, `StorePort`, `TelemetryExporter`, `TelemetryPort`, `ToolPort`

- 类 `AuditPort` (bases: Protocol) — 1 个公开方法
  - `record(self, event: 'dict[str, DynamicValue]', ctx: 'PortCtx') -> 'None'` — 追加审计事件（append-only + 哈希链，不可旁路）；内部委托 AuditStore。
- 类 `AuditStore` (bases: Protocol) — 1 个公开方法
  - `append(self, event: 'dict[str, DynamicValue]') -> 'None'` — 追加一条审计事件（append-only，写入后不可改）。
- 类 `DisclosureIndex` (bases: Protocol) — 1 个公开方法
  - `disclose(self, names: 'list[str]', skills: 'list[SkillManifest]') -> 'DisclosurePacket'` — 按命中意图名与技能清单投影披露包；超预算由实现显式拒绝。
- 类 `EvalPort` (bases: Protocol) — 1 个公开方法
  - `score(self, case_id: 'str', output: 'DynamicValue', ctx: 'PortCtx') -> 'float'` — 对单个评测用例的输出打分（0~1）。
- 类 `EventStreamPort` (bases: Protocol) — 2 个公开方法
  - `subscribe(self, topic: 'str', since: 'int | None' = None) -> 'AsyncIterator[DynamicValue]'` — 订阅事件流，从 since 序号续读；只读不侵入内核。
  - `topics(self) -> 'list[str]'` — 返回可订阅的 topic 列表。
- 类 `HumanPort` (bases: Protocol) — 1 个公开方法
  - `ask(self, question: 'str', options: 'list[str] | None', ctx: 'PortCtx') -> 'str'` — 向人提问并返回回答；挂起态须能跨周期恢复。
- 类 `LLMPort` (bases: Protocol) — 2 个公开方法
  - `complete(self, req: 'LLMRequest', ctx: 'PortCtx') -> 'LLMResponse'` — 一次性补全：返回结构化响应（含 tool_calls 与 usage）；同时写 last_usage。
  - `stream(self, req: 'LLMRequest', ctx: 'PortCtx') -> 'AsyncIterator[str]'` — 流式补全：返回异步迭代器逐块产出文本，取消由调用方 aclose() 传播。
- 类 `MCPPort` (bases: Protocol) — 3 个公开方法
  - `call_tool(self, server: 'str', tool: 'str', args: 'dict[str, DynamicValue]', ctx: 'PortCtx') -> 'DynamicValue'` — 调用 MCP 工具，返回形态由工具决定。
  - `list_tools(self, server: 'str', ctx: 'PortCtx') -> 'list[ToolSpec]'` — 列出 MCP server 的工具；L1 披露只进目录不进 schema。
  - `ping(self, server: 'str', ctx: 'PortCtx') -> 'bool'` — 健康检查：探测 server 可达（R5/R13 契约，纳入履约校验）。
- 类 `MemoryPort` (bases: Protocol) — 3 个公开方法
  - `forget(self, namespace: 'str', key: 'str', ctx: 'PortCtx') -> 'None'` — 遗忘指定记忆（TTL 或显式删除）。
  - `read(self, namespace: 'str', key: 'str', ctx: 'PortCtx') -> 'DynamicValue'` — 按命名空间与键读取记忆；不存在返回 None。
  - `write(self, namespace: 'str', key: 'str', value: 'DynamicValue', ctx: 'PortCtx') -> 'None'` — 写入记忆；内容须过注入检测且不含 secret 明文。
- 类 `RAGPort` (bases: Protocol) — 2 个公开方法
  - `ingest(self, documents: 'list[DynamicValue]', ctx: 'PortCtx') -> 'int'` — 入库文档，返回成功入库的块数。
  - `retrieve(self, query: 'str', ctx: 'PortCtx') -> 'list[DynamicValue]'` — 检索命中列表，每项带引用溯源信息。
- 类 `SandboxPort` (bases: Protocol) — 1 个公开方法
  - `run(self, fn: 'Callable[..., DynamicValue]', args: 'DynamicValue', ctx: 'PortCtx') -> 'DynamicValue'` — 在隔离环境执行函数，资源限额与网络白名单由实现保证。
- 类 `SkillPort` (bases: Protocol) — 3 个公开方法
  - `body(self, name: 'str') -> 'str'` — 返回技能正文（L3 按需加载），正文须过注入检测。
  - `catalog(self) -> 'list[SkillManifest]'` — 返回技能目录（name/version/触发词，常驻 L1）。
  - `discover(self) -> 'list[SkillManifest]'` — 发现所有可用技能清单（具名视图，非 DynamicValue）。
- 类 `StorePort` (bases: Protocol) — 2 个公开方法
  - `load(self, session_id: 'str') -> 'dict[str, DynamicValue] | None'` — 读取会话快照；不存在返回 None。
  - `save(self, session_id: 'str', snapshot: 'dict[str, DynamicValue]') -> 'None'` — 保存会话快照；结构须可确定性 JSON 序列化。
- 类 `TelemetryExporter` (bases: Protocol) — 1 个公开方法
  - `export(self, records: 'tuple[DynamicValue, ...]') -> 'None'` — 批量导出遥测记录；失败内部消化，不抛穿到业务路径。
- 类 `TelemetryPort` (bases: Protocol) — 3 个公开方法
  - `log(self, event: 'dict[str, DynamicValue]') -> 'None'` — 记录结构化日志事件。
  - `metric(self, name: 'str', value: 'float', tags: 'dict[str, DynamicValue] | None' = None) -> 'None'` — 记录指标，tags 为可选维度标签。
  - `span(self, name: 'str', ctx: 'PortCtx') -> 'AbstractContextManager[DynamicValue]'` — 开启链路 span，返回上下文管理器。
- 类 `ToolPort` (bases: Protocol) — 3 个公开方法
  - `call(self, call: 'ToolCall', ctx: 'PortCtx') -> 'ToolResult'` — 调用工具：异常折叠为 ToolResult，不抛穿。
  - `catalog(self, level: 'int' = 1) -> 'list[ToolSpec]'` — 返回工具目录；level=1 只 name+描述，2/3 含完整 schema（渐进披露）。
  - `register(self, spec: 'ToolSpec', fn: 'Callable[..., DynamicValue]') -> 'None'` — 注册工具：spec 定义 schema，fn 是实际实现。

## `compose.program` — src/compose/program.py

- `__all__` (12): `JoinMode`, `ProgramFn`, `branch`, `fallback`, `gate`, `loop`, `map_effect`, `map_state`, `par`, `race`, `retry`, `seq`

- 函数 `branch(fn: 'Callable[[AgentState], str]', arms: 'dict[str, ProgramFn]', default: 'str | None' = None) -> 'ProgramFn'` — 条件分流：fn(state) 返回 key 选 arms[key]，缺失走 default。
- 函数 `branch_value(source: 'ProgramFn', fn: 'Callable[[DynamicValue], str]', arms: 'dict[str, ProgramFn]', *, default: 'str' = '', no_route_reason: 'str' = 'no_route') -> 'ProgramFn'` — 值驱动分支：从 source 的 Terminate.output 取值选 arm（不经 state port）。
- 函数 `fallback(p: 'ProgramFn', *, ladders: 'Sequence[ProgramFn]', refuse: 'ProgramFn') -> 'ProgramFn'` — 降级选级骨架：按序尝试 p → ladders，全失败走 refuse。
- 函数 `gate(p: 'ProgramFn', policy: 'Callable[[AgentState], object]') -> 'ProgramFn'` — 审批/澄清门：可 yield Suspend 挂起，resume 后续跑 p（D-6 唯一能挂起的 A 类算子）。
- 函数 `loop(body: 'ProgramFn', *, cond: 'Callable[[AgentState], bool]', max_rounds: 'int' = 3) -> 'ProgramFn'` — 显式循环：cond 为真时重复 body，轮次上限交 Budgets（此处仅防御性护栏）。
- 函数 `loop_until(step: 'Callable[[DynamicValue], ProgramFn]', *, done: 'Callable[[DynamicValue], bool]', max_rounds: 'int' = 30, reason_fn: 'Callable[[bool], str] | None' = None) -> 'ProgramFn'` — 回注值驱动的循环：每轮把上一步回注值交给 step 生成下一步 Program，done 为真停止。
- 函数 `map_effect(fn: 'Callable[[Effect], Effect]') -> 'Callable[[ProgramFn], ProgramFn]'` — Effect payload 变换：脱敏 / 参数规范化 / 注入标记 / 软预算注入。
- 函数 `map_state(lens: 'Callable[[AgentState], AgentState]') -> 'Callable[[ProgramFn], ProgramFn]'` — State 视图变换算子：返回可作用于一个 Program 的纯变换。
- 函数 `par(items: 'Sequence[ProgramFn]', *, join: 'str' = 'all') -> 'ProgramFn'` — 扇出并行：每轮把各子 Program 的 Effect 汇成一列表一次 yield（走 R1 列表并行），按 join 聚合。
- 函数 `race(items: 'Sequence[ProgramFn]') -> 'ProgramFn'` — 竞速：首个完成胜出，取消其余（取消走 R1 cancel 设施，算子只编排）。
- 函数 `retry(p: 'ProgramFn', *, attempts: 'int' = 3, backoff: 'float' = 0.0) -> 'ProgramFn'` — 流程级重放：失败按 program_factory 重建重跑（纯函数、状态进 x-*，D-5/D-8）。
- 函数 `seq(a: 'ProgramFn', b: 'ProgramFn') -> 'ProgramFn'` — 顺序组合：先跑 a 到 Terminate，output 写入 x-* 后跑 b，b 的 output 为整体产出。
- 类 `JoinMode` (bases: str, Enum) — 0 个公开方法

## `compose.registry` — src/compose/registry.py

- `__all__` (5): `Capability`, `HookBusError`, `Provider`, `ProviderRegistry`, `Registry`

- 类 `Capability` (bases: Protocol) — 1 个公开方法
  - `install(self, container: 'object', reg: 'Registry') -> 'None'` — 把本能力的增强器装到容器、provider 装到注册表。
- 类 `HookBusError` (bases: Exception) — 0 个公开方法
- 类 `Provider` (bases: Protocol) — 0 个公开方法
- 类 `ProviderRegistry` (bases: Protocol) — 2 个公开方法
  - `has(self, name: 'str') -> 'bool'` — 
  - `register(self, name: 'str', provider: 'Provider') -> 'object'` — Register a virtual subclass of an ABC.
- 类 `Registry` (bases: object) — 6 个公开方法
  - `freeze(self) -> 'Registry'` — 冻结注册表（装配期完成后），此后 register/replace 均拒绝。
  - `has(self, name: 'str') -> 'bool'` — 是否已注册该 name。
  - `names(self) -> 'Iterable[str]'` — 已注册名称列表（快照）。
  - `register(self, name: 'str', provider: 'Provider') -> 'Registry'` — 注册 provider；重名报错（除非用 replace）；返回 self 支持链式。
  - `replace(self, name: 'str', provider: 'Provider') -> 'Registry'` — 替换已注册的 provider；返回 self 支持链式。
  - `resolve(self, name: 'str') -> 'Provider'` — 解析 provider；缺失报错。

## `compose.tool_layer` — src/compose/tool_layer.py

- `__all__` (1): `layer_program`

- 函数 `layer_program(port: 'str', *, payload: 'Mapping[str, DynamicValue] | None' = None, output: 'DynamicValue' = None) -> 'Callable[[], Program]'` — 构造一个 ToolLayer 外壳 Program 工厂（build() 的返回值）。

## `harness._ctxview` — src/harness/_ctxview.py

- `__all__` (3): `PortCtxView`, `as_port_ctx`, `coerce_port_ctx`

- 函数 `as_port_ctx(ctx: 'object') -> 'PortCtx'` — 把 Runner 上下文投影为 PortCtx（规则 6 唯一动态读取边界）。
- 函数 `coerce_port_ctx(ctx: 'PortCtx') -> 'PortCtxView'` — PortCtx.meta 的唯一动态读取边界（规则 6），归名为成本归因维度。
- 类 `PortCtxView` (bases: object) — 0 个公开方法

## `harness.cognition` — src/harness/cognition/__init__.py

- `__all__` (22): `IntentResult`, `IntentSample`, `Critique`, `Step`, `StepResult`, `IntentStrategy`, `KeywordMatcher`, `IntentSampleStore`, `CheapIntentModel`, `PrimaryIntentModel`, `Planner`, `Executor`, `RePlanner`, `ReflectStrategy`, `CascadeIntentStrategy`, `InMemoryIntentSampleStore`, `RegexKeywordMatcher`, `intent_as_program`, `RulePlanner`, `RuleExecutor`, `RuleRePlanner`, `ThresholdReflector`


## `harness.cognition.capability` — src/harness/cognition/capability.py

- `__all__` (1): `CognitionCapability`

- 类 `CognitionCapability` (bases: object) — 2 个公开方法
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'None'` — Capability 协议：注册策略为一等 provider（名 'intent'）。
  - `recognize(self, text: 'str', ctx: 'AgentState | None' = None) -> 'IntentResult'` — 转调策略 recognize()——整包替换语义，框架不包装级联。

## `harness.cognition.cascade` — src/harness/cognition/cascade.py

- `__all__` (4): `CascadeIntentStrategy`, `InMemoryIntentSampleStore`, `RegexKeywordMatcher`, `intent_as_program`

- 函数 `intent_as_program(strategy: 'object', text: 'str' = '') -> '_ProgramFn'` — 统一桥梁：任何策略 → ProgramFn（yield from 后读 Terminate.output）。
- 类 `CascadeIntentStrategy` (bases: object) — 2 个公开方法
  - `build(self, text: 'str' = '') -> '_ProgramGen'` — 产 Program generator（yield Effect，短路，末级 Terminate）。
  - `recognize(self, text: 'str', ctx: 'object') -> 'IntentResult'` — async 单入口：逐级判定，达阈值即停（供非 Program 调用方使用）。
- 类 `InMemoryIntentSampleStore` (bases: object) — 3 个公开方法
  - `match(self, text: 'str') -> 'IntentResult | None'` — L2 判定：取最相似采样，返回 label + 相似度置信度（无命中返回 None）。
  - `search(self, text: 'str', top_k: 'int' = 5) -> 'tuple[IntentSample, ...]'` — 采样库检索：按相似度降序取 top_k（并列按插入序，确定性）。
  - `similarity(text: 'str', sample_text: 'str') -> 'float'` — 字符二元组 Jaccard 相似度（零依赖、确定性，供 L2 阈值判定复用）。
- 类 `RegexKeywordMatcher` (bases: object) — 1 个公开方法
  - `match(self, text: 'str', ctx: 'object' = None) -> 'IntentResult | None'` — 按子串匹配，命中第一个关键词返回；顺序由 dict 插入序保证确定性。

## `harness.cognition.contracts` — src/harness/cognition/contracts.py

- `__all__` (14): `IntentResult`, `IntentSample`, `Critique`, `Step`, `StepResult`, `IntentStrategy`, `KeywordMatcher`, `IntentSampleStore`, `CheapIntentModel`, `PrimaryIntentModel`, `Planner`, `Executor`, `RePlanner`, `ReflectStrategy`

- 类 `CheapIntentModel` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `recognize(self, text: 'str', ctx: 'AgentState | None') -> 'IntentResult'` — 
- 类 `Critique` (bases: object) — 0 个公开方法
- 类 `Executor` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `execute(self, step: 'Step', ctx: 'AgentState | None' = None) -> 'StepResult'` — 
- 类 `IntentResult` (bases: object) — 0 个公开方法
- 类 `IntentSample` (bases: object) — 0 个公开方法
- 类 `IntentSampleStore` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `search(self, text: 'str', top_k: 'int' = 5) -> 'tuple[IntentSample, ...]'` — 
- 类 `IntentStrategy` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `recognize(self, text: 'str', ctx: 'AgentState | None') -> 'IntentResult'` — 
- 类 `KeywordMatcher` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `match(self, text: 'str', ctx: 'AgentState | None') -> 'IntentResult | None'` — 
- 类 `NamedStrategy` (bases: Protocol) — 0 个公开方法
- 类 `Planner` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `plan(self, goal: 'str', ctx: 'AgentState | None' = None) -> 'tuple[Step, ...]'` — 
- 类 `PrimaryIntentModel` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `recognize(self, text: 'str', ctx: 'AgentState | None') -> 'IntentResult'` — 
- 类 `RePlanner` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `replan(self, goal: 'str', steps: 'tuple[Step, ...]', failures: 'tuple[str, ...]', ctx: 'AgentState | None' = None) -> 'tuple[Step, ...]'` — 
- 类 `ReflectStrategy` (bases: NamedStrategy, Protocol) — 1 个公开方法
  - `critique(self, draft: 'str', goal: 'str', ctx: 'AgentState | None' = None) -> 'Critique'` — 
- 类 `Step` (bases: object) — 0 个公开方法
- 类 `StepResult` (bases: object) — 0 个公开方法

## `harness.cognition.reflection_capability` — src/harness/cognition/reflection_capability.py

- `__all__` (1): `ReflectionCapability`

- 类 `ReflectionCapability` (bases: object) — 2 个公开方法
  - `critique(self, draft: 'str', goal: 'str' = '', ctx: 'AgentState | None' = None) -> 'Critique'` — 评审转调：三参（draft/goal/ctx）真传给注入策略。
  - `install(self, container: 'object | None' = None, reg: "'Registry | None'" = None) -> 'None'` — Capability 协议：注册反思策略为一等 provider。

## `harness.cognition.services` — src/harness/cognition/services.py

- `__all__` (4): `RulePlanner`, `RuleExecutor`, `RuleRePlanner`, `ThresholdReflector`

- 类 `RuleExecutor` (bases: object) — 1 个公开方法
  - `execute(self, step: 'Step', ctx: 'AgentState | None' = None) -> 'StepResult'` — 执行单步：输出步骤 action（无副作用，供 PlanExecute 游标推进）。
- 类 `RulePlanner` (bases: object) — 1 个公开方法
  - `plan(self, goal: 'str', ctx: 'AgentState | None' = None) -> 'tuple[Step, ...]'` — 按分隔符切分目标为具名步骤（空段跳过）。
- 类 `RuleRePlanner` (bases: object) — 1 个公开方法
  - `replan(self, goal: 'str', steps: 'tuple[Step, ...]', failures: 'tuple[str, ...]', ctx: 'AgentState | None' = None) -> 'tuple[Step, ...]'` — 保留未完成步骤并在首个步骤动作前标注失败原因。
- 类 `ThresholdReflector` (bases: object) — 1 个公开方法
  - `critique(self, draft: 'str', goal: 'str', ctx: 'AgentState | None' = None) -> 'Critique'` — 判定草稿：含通过标记或达最小长度即 passed。

## `harness.context` — src/harness/context/__init__.py

- `__all__` (30): `ContextSection`, `TrimPolicy`, `TRIGGER_RATIO`, `DEFAULT_TRIGGER_TURNS`, `DEFAULT_KEEP_RECENT`, `DEFAULT_PIN_PATTERNS`, `ContextAssembler`, `DefaultContextAssembler`, `SYSTEM_SECTION_NAME`, `CompactionPolicy`, `DefaultCompactionPolicy`, `count_tokens`, `Reinjector`, `DefaultReinjector`, `TokenCounter`, `DefaultTokenCounter`, `estimate_tokens`, `MemoryScope`, `ScopeResolver`, `make_item_id`, `ContextMemoryManager`, `MemoryCapability`, `CacheTelemetry`, `BlobPort`, `Offloader`, `InMemoryBlobStore`, `DefaultOffloader`, `BLOB_HANDLE_SCHEME`, `make_handle`, `parse_handle`


## `harness.context.assembler` — src/harness/context/assembler.py

- `__all__` (3): `SYSTEM_SECTION_NAME`, `ContextAssembler`, `DefaultContextAssembler`

- 类 `ContextAssembler` (bases: Protocol) — 1 个公开方法
  - `assemble(self, sections: 'list[ContextSection]', budget: 'TokenBudget') -> 'list[Message]'` — 按配额与预算把分段组装为消息列表。
- 类 `DefaultContextAssembler` (bases: object) — 1 个公开方法
  - `assemble(self, sections: 'list[ContextSection]', budget: 'TokenBudget') -> 'list[Message]'` — 把分段组装成消息列表。

## `harness.context.capability` — src/harness/context/capability.py

- `__all__` (1): `MemoryCapability`

- 类 `MemoryCapability` (bases: object) — 3 个公开方法
  - `install(self, container: 'object', reg: 'Registry') -> 'None'` — 把 manager.memory 注册为 memory provider（履约 Capability 协议）。
  - `recall_before(self, text: 'str', ctx: 'PortCtx') -> 'tuple[str, ...]'` — say 前召回：返回命中记忆的 content 元组（供门面注入上下文）。
  - `write_after(self, user_input: 'str', answer: 'str', ctx: 'PortCtx') -> 'None'` — say 后写回：把问答提炼进长期记忆（真实 ingest，非空桩）。

## `harness.context.compaction` — src/harness/context/compaction.py

- `__all__` (4): `SUMMARY_ROLE`, `CompactionPolicy`, `DefaultCompactionPolicy`, `count_tokens`

- 函数 `count_tokens(text: 'str') -> 'int'` — 统一 token 估算（兼容旧调用方；与 assemble 同口径）。
- 类 `CompactionPolicy` (bases: Protocol) — 2 个公开方法
  - `compact(self, messages: 'tuple[Message, ...]') -> 'tuple[Message, ...]'` — 产压缩后的消息视图（新 tuple，不改入参）。
  - `should_compact(self, state: 'AgentState') -> 'bool'` — 判定当前状态是否超压缩阈值。
- 类 `DefaultCompactionPolicy` (bases: object) — 2 个公开方法
  - `compact(self, messages: 'tuple[Message, ...]') -> 'tuple[Message, ...]'` — 产压缩视图：钉住消息原位保留，其余折叠为语义摘要。
  - `should_compact(self, state: 'AgentState') -> 'bool'` — 按 token 占比或轮次判定是否压缩。

## `harness.context.manager` — src/harness/context/manager.py

- `__all__` (1): `ContextMemoryManager`

- 类 `ContextMemoryManager` (bases: object) — 8 个公开方法
  - `assemble(self, sections: 'list[ContextSection]', budget: 'TokenBudget') -> 'list[Message]'` — 显式组装上下文（替代原 before_model hook）。
  - `build(self) -> 'Program'` — ToolLayer 外壳（规格 P8）：产出"用上下文记忆"的 Program 工厂。
  - `commit_and_compact(self, state: 'AgentState', transcript: 'str', ctx: 'PortCtx | None' = None, budget: 'TokenBudget | None' = None, tasks: 'tuple[str, ...]' = ()) -> 'tuple[Message, ...]'` — 提交会话：先写 cold 全文→压缩→按预算重注入（顺序由代码固定）。
  - `offload(self, value: 'object', ctx: 'PortCtx | None' = None) -> 'str'` — 显式大结果卸载，返回可读句柄（替代原 after_tool hook）。
  - `recall(self, query: 'object', ctx: 'PortCtx | None' = None) -> 'list[object]'` — 显式语义召回（替代原 before_model 记忆注入 hook）。
  - `record_cache_hit(self, label: 'str' = 'default') -> 'None'` — 记录一次 KV cache 命中（一等观测，纯替换不可变指标）。
  - `record_cache_miss(self, label: 'str' = 'default') -> 'None'` — 记录一次 KV cache 未命中。
  - `with_(self, assembler: 'ContextAssembler | None' = None, compaction: 'CompactionPolicy | None' = None, offloader: 'Offloader | None' = None, memory: 'MemoryStore | None' = None, blob: 'BlobPort | None' = None, reinjector: 'Reinjector | None' = None, token_counter: 'TokenCounter | None' = None) -> 'ContextMemoryManager'` — 产新实例替换指定依赖（不可变风格），并保留 trace 观测历史。

## `harness.context.observability` — src/harness/context/observability.py

- `__all__` (1): `CacheTelemetry`

- 类 `CacheTelemetry` (bases: object) — 5 个公开方法
  - `hit_rate(self) -> 'float'` — 命中率（hits / total）；无访问时为 0.0，绝不产生 NaN/None。
  - `record_hit(self) -> 'CacheTelemetry'` — 记录一次命中，返回新实例（纯函数，不改原对象）。
  - `record_miss(self) -> 'CacheTelemetry'` — 记录一次未命中，返回新实例。
  - `total(self) -> 'int'` — 总访问次数（hits + misses）。
  - `with_label(self, label: 'str') -> 'CacheTelemetry'` — 换维度标签，返回新实例（保留计数）。

## `harness.context.offloader` — src/harness/context/offloader.py

- `__all__` (7): `BLOB_HANDLE_SCHEME`, `BlobPort`, `Offloader`, `DefaultOffloader`, `InMemoryBlobStore`, `make_handle`, `parse_handle`

- 函数 `make_handle(blob_id: 'str') -> 'str'` — 把 blob id 包装为句柄字符串。
- 函数 `parse_handle(handle: 'str') -> 'str | None'` — 从句柄解析 blob id；scheme 不符返回 None。
- 类 `BlobPort` (bases: Protocol) — 3 个公开方法
  - `delete(self, handle: 'str') -> 'None'` — 按句柄删除；不存在幂等无操作。
  - `get(self, handle: 'str') -> 'DynamicValue | None'` — 按句柄读回；不存在返回 None。
  - `put(self, value: 'DynamicValue') -> 'str'` — 存入一个值，返回可读句柄。
- 类 `DefaultOffloader` (bases: object) — 1 个公开方法
  - `offload(self, value: 'DynamicValue', store: 'BlobPort') -> 'str'` — 把值交给 store 存并返回句柄。
- 类 `InMemoryBlobStore` (bases: object) — 3 个公开方法
  - `delete(self, handle: 'str') -> 'None'` — 按句柄删除；不存在幂等。
  - `get(self, handle: 'str') -> 'DynamicValue | None'` — 按句柄读回；句柄格式错或不存在返 None。
  - `put(self, value: 'DynamicValue') -> 'str'` — 存值并返回 ``store://<uuid>`` 句柄。
- 类 `Offloader` (bases: Protocol) — 1 个公开方法
  - `offload(self, value: 'DynamicValue', store: 'BlobPort') -> 'str'` — 卸载值，返回可读取的句柄。

## `harness.context.ports` — src/harness/context/ports.py

- `__all__` (1): `MemoryPortWithRecall`

- 类 `MemoryPortWithRecall` (bases: MemoryPort, Protocol) — 1 个公开方法
  - `recall(self, query: 'MemoryQuery', ctx: 'PortCtx') -> 'list[MemoryItem]'` — 语义召回：按查询返回相关记忆项。

## `harness.context.reinjector` — src/harness/context/reinjector.py

- `__all__` (3): `DefaultReinjector`, `Reinjector`, `SUMMARY_ROLE`

- 类 `DefaultReinjector` (bases: object) — 1 个公开方法
  - `reinject(self, view: 'tuple[Message, ...]', summary: 'Message | None', tasks: 'tuple[str, ...]', budget: 'TokenBudget') -> 'tuple[Message, ...]'` — 回注摘要、钉住片段与未完成任务，按预算二次裁剪。
- 类 `Reinjector` (bases: Protocol) — 1 个公开方法
  - `reinject(self, view: 'tuple[Message, ...]', summary: 'Message | None', tasks: 'tuple[str, ...]', budget: 'TokenBudget') -> 'tuple[Message, ...]'` — 按预算把摘要+钉住片段+未完成任务回注视图。

## `harness.context.token_counter` — src/harness/context/token_counter.py

- `__all__` (3): `DefaultTokenCounter`, `TokenCounter`, `estimate_tokens`

- 函数 `estimate_tokens(text: 'str') -> 'int'` — 统一 token 估算：非空文本按 Unicode 字符数计，向上取整到 token。
- 类 `DefaultTokenCounter` (bases: object) — 1 个公开方法
  - `count(self, text: 'str') -> 'int'` — 按统一口径计数。
- 类 `TokenCounter` (bases: Protocol) — 1 个公开方法
  - `count(self, text: 'str') -> 'int'` — 返回文本的 token 估算数。

## `harness.context.types` — src/harness/context/types.py

- `__all__` (7): `SYSTEM_SECTION_NAME`, `TrimPolicy`, `ContextSection`, `TRIGGER_RATIO`, `DEFAULT_TRIGGER_TURNS`, `DEFAULT_KEEP_RECENT`, `DEFAULT_PIN_PATTERNS`

- 类 `ContextSection` (bases: object) — 0 个公开方法
- 类 `TrimPolicy` (bases: StrEnum) — 0 个公开方法

## `harness.durability` — src/harness/durability/__init__.py

- `__all__` (3): `CheckpointManager`, `CheckpointSnapshot`, `replay_snapshot`


## `harness.durability.checkpoint` — src/harness/durability/checkpoint.py

- `__all__` (3): `CheckpointSnapshot`, `CheckpointManager`, `replay_snapshot`

- 函数 `replay_snapshot(snap: 'CheckpointSnapshot') -> 'AgentState'` — 从快照的 events 回放终态（复用 R1 restore_checkpoint，不重造 fold）。
- 类 `CheckpointManager` (bases: object) — 3 个公开方法
  - `load(self, session_id: 'str') -> 'CheckpointSnapshot | None'` — 从 R1 EventStore 读回快照（跨进程恢复入口）。
  - `save(self, snap: 'CheckpointSnapshot') -> 'None'` — 经 R1 EventStore 持久化（不自建存储协议）。
  - `snapshot(self, session_id: 'str', state: 'AgentState', events: 'tuple[object, ...]') -> 'CheckpointSnapshot'` — 从 state + events 产具名快照（fingerprint 由事件确定性计算）。
- 类 `CheckpointSnapshot` (bases: object) — 0 个公开方法

## `harness.eval` — src/harness/eval/__init__.py

- `__all__` (6): `EvalCase`, `Score`, `SuiteReport`, `RuleEval`, `GoldenJudge`, `EvalCapability`


## `harness.eval.capability` — src/harness/eval/capability.py

- `__all__` (1): `EvalCapability`

- 类 `EvalCapability` (bases: object) — 2 个公开方法
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'None'` — 
  - `score(self, case_id: 'str', output: 'DynamicValue', ctx: 'PortCtx') -> 'float'` — 转调注入评测器（三参真传）。

## `harness.eval.rules` — src/harness/eval/rules.py

- `__all__` (2): `RuleEval`, `GoldenJudge`

- 类 `GoldenJudge` (bases: object) — 1 个公开方法
  - `score(self, case: 'EvalCase', output: 'DynamicValue', ctx: 'PortCtx') -> 'Score'` — 轨迹全等得 1.0，缺步/多步按比例扣（交集长度 / 并集长度）。
- 类 `RuleEval` (bases: EvalPort) — 3 个公开方法
  - `add_case(self, case: 'EvalCase') -> 'None'` — 注册评测用例（golden 集）。
  - `regression(self, suite: 'str', ctx: 'PortCtx') -> 'SuiteReport'` — 回归套件：用录制/采样得到的 case.actual 评分，产结构化报告（CI gate 消费）。
  - `score(self, case_id: 'str', output: 'DynamicValue', ctx: 'PortCtx') -> 'float'` — 评分：should_refuse 用例判拒答；否则 output == expected 得 1.0。

## `harness.eval.types` — src/harness/eval/types.py

- `__all__` (3): `EvalCase`, `Score`, `SuiteReport`

- 类 `EvalCase` (bases: object) — 0 个公开方法
- 类 `Score` (bases: object) — 0 个公开方法
- 类 `SuiteReport` (bases: object) — 0 个公开方法

## `harness.memory` — src/harness/memory/__init__.py

- `__all__` (20): `MemoryItem`, `MemoryKind`, `MemoryQuery`, `MemoryScope`, `MemoryTier`, `ScopeResolver`, `SECRET_KEY_TOKENS`, `SCOPE_META_KEY`, `NEAR_DUP_SIM_THRESHOLD`, `PREFERENCE_REPEAT_THRESHOLD`, `PROFILE_CONFIDENCE_THRESHOLD`, `RERANK_W_RELEVANCE`, `RERANK_W_IMPORTANCE`, `RERANK_W_RECENCY`, `RERANK_W_CONFIDENCE`, `MMR_LAMBDA`, `is_secret_key`, `make_item_id`, `MemoryStore`, `InMemoryStorePort`


## `harness.memory.memory` — src/harness/memory/memory.py

- `__all__` (1): `MemoryStore`

- 类 `MemoryStore` (bases: object) — 17 个公开方法
  - `default_tier_for(self, kind: 'MemoryKind') -> 'MemoryTier'` — 查询内容类的初始层默认落点（维度正交，仅默认非强制）。
  - `demote(self, item: 'MemoryItem') -> 'None'` — 降级/沉降一级：hot→warm→cold（低频/低分者归档）；已在 cold 不动。
  - `forget(self, namespace: 'str', key: 'str', ctx: 'PortCtx') -> 'None'` — 遗忘指定键值；不存在幂等。
  - `forget_expired(self, ttl: 'int | None' = None, *, now: 'int | None' = None) -> 'list[MemoryItem]'` — 遗忘过期项；ttl 缺省按各层独立 TTL，now 缺省为当前时刻。
  - `importance_of(self, item: 'MemoryItem') -> 'float'` — 读取当前 importance（命中重算后可能高于初始值）。
  - `ingest(self, text: 'str', ctx: 'PortCtx', *, kind: 'MemoryKind | None' = None, confidence: 'float' = 1.0, source: 'str' = 'refined', now: 'int | None' = None) -> 'MemoryItem | None'` — 完整写入链：判定 kind → 评分 → 去重/冲突消解 → 默认层落库。
  - `promote(self, item: 'MemoryItem') -> 'None'` — 晋升/回温一级：cold→warm→hot（高访问/高 importance 更易被召回）；已在 hot 不动。
  - `put_item(self, item: 'MemoryItem', tier: 'MemoryTier', *, now: 'int | None' = None) -> 'None'` — 登记一条记忆项；同 item_id 覆盖不追加（稳定 upsert）。
  - `read(self, namespace: 'str', key: 'str', ctx: 'PortCtx') -> 'DynamicValue'` — 按 namespace/key 读取；不存在返回 None（键值层不做 tenant 隔离）。
  - `recall(self, query: 'MemoryQuery', ctx: 'PortCtx') -> 'list[MemoryItem]'` — 完整召回链：tenant 过滤→混合召回→rerank 加权和→MMR→注入复检。
  - `record_access(self, item: 'MemoryItem') -> 'MemoryItem | None'` — 记录命中：access_count+1 且 importance 按 log 增长重算。
  - `refine_kind(text: 'str', confidence: 'float', *, preference_hits: 'int' = 0) -> 'MemoryKind'` — 按 §12.2 互斥优先级判定内容类型（命中即停）。
  - `score_importance(text: 'str', kind: 'MemoryKind', source: 'str') -> 'float'` — importance 评分：0.35·信息量 + 0.25·复用度 + 0.20·时效 + 0.20·来源权重。
  - `tier_count(self) -> 'int'` — 返回已登记记忆项总数（诊断/测试用）。
  - `tier_of(self, item: 'MemoryItem') -> 'MemoryTier | None'` — 查当前层；先按稳定 id，回退对象身份/值比对（兼容旧调用方）。
  - `write(self, namespace: 'str', key: 'str', value: 'DynamicValue', ctx: 'PortCtx') -> 'None'` — 写入键值记忆；递归 secret/注入检测命中即拒绝（fail-closed）。
  - `write_cold(self, transcript: 'str', ctx: 'PortCtx | None' = None) -> 'None'` — 先落盘原文全文（commit_and_compact 第一步），不经过提炼链。

## `harness.memory.store` — src/harness/memory/store.py

- `__all__` (1): `InMemoryStorePort`

- 类 `InMemoryStorePort` (bases: object) — 2 个公开方法
  - `load(self, session_id: 'str') -> 'dict[str, DynamicValue] | None'` — 读取快照副本；不存在返回 None。
  - `save(self, session_id: 'str', snapshot: 'dict[str, DynamicValue]') -> 'None'` — 保存会话快照（深拷贝隔离）。

## `harness.memory.types` — src/harness/memory/types.py

- `__all__` (18): `MMR_LAMBDA`, `NEAR_DUP_SIM_THRESHOLD`, `PREFERENCE_REPEAT_THRESHOLD`, `PROFILE_CONFIDENCE_THRESHOLD`, `RERANK_W_CONFIDENCE`, `RERANK_W_IMPORTANCE`, `RERANK_W_RECENCY`, `RERANK_W_RELEVANCE`, `SCOPE_META_KEY`, `SECRET_KEY_TOKENS`, `MemoryItem`, `MemoryKind`, `MemoryQuery`, `MemoryScope`, `MemoryTier`, `ScopeResolver`, `is_secret_key`, `make_item_id`

- 函数 `is_secret_key(key: 'str') -> 'bool'` — 判定键名是否疑似 secret（fail-closed 第一道闸）。
- 函数 `make_item_id(tenant: 'str', kind: 'MemoryKind | str', content: 'str') -> 'str'` — 由 tenant + kind + 规范化内容生成稳定 item_id。
- 类 `MemoryItem` (bases: object) — 0 个公开方法
- 类 `MemoryKind` (bases: StrEnum) — 0 个公开方法
- 类 `MemoryQuery` (bases: object) — 0 个公开方法
- 类 `MemoryScope` (bases: object) — 0 个公开方法
- 类 `MemoryTier` (bases: StrEnum) — 0 个公开方法
- 类 `ScopeResolver` (bases: object) — 2 个公开方法
  - `check(query_scope: 'MemoryScope | None', ctx_scope: 'MemoryScope') -> 'None'` — 校验显式 query scope 与 ctx scope 的 tenant 一致，防止从 Query 侧绕过。
  - `resolve(ctx: 'object') -> 'MemoryScope'` — 从 ``ctx.meta["scope"]`` 解析 MemoryScope，缺失降级为 default。

## `harness.model` — src/harness/model/__init__.py

- `__all__` (33): `BackendResolution`, `CacheKey`, `CostAttributor`, `CostGuard`, `CostRecord`, `RefuseExecution`, `TargetUnresolved`, `Reservation`, `CachePort`, `CacheStore`, `InMemoryCacheStore`, `CacheEmbedder`, `HashCacheEmbedder`, `DEFAULT_CACHE_EMBED_DIM`, `ModelEndpoint`, `ModelRegistry`, `InMemoryModelRegistry`, `RouteDecision`, `ExactCache`, `SimilarityCache`, `ModelCapability`, `ModelGateway`, `ModelRouter`, `RouteDecision`, `RuleRouter`, `StructuredOutputSpec`, `SchemaValidator`, `MinimalSchemaValidator`, `StructuredOutputValidator`, `UsageLedger`, `build_cache_key`, `cache_key_string`, `validate_json_schema`


## `harness.model.cache` — src/harness/model/cache.py

- `__all__` (9): `CacheEmbedder`, `CachePort`, `CacheStore`, `DEFAULT_CACHE_EMBED_DIM`, `ExactCache`, `HashCacheEmbedder`, `InMemoryCacheStore`, `MAX_EMBED_CHARS`, `SimilarityCache`

- 类 `CacheEmbedder` (bases: Protocol) — 1 个公开方法
  - `embed(self, text: 'str') -> 'tuple[float, ...]'` — 文本 → 向量（维度由实现决定，同一实现内须一致）。
- 类 `CachePort` (bases: Protocol) — 3 个公开方法
  - `get(self, request: 'LLMRequest', *, version: 'str' = 'current') -> 'LLMResponse | None'` — 命中返回响应，未命中 None。
  - `invalidate(self, request: 'LLMRequest', *, version: 'str' = 'current') -> 'None'` — 失效该请求对应的缓存条目。
  - `put(self, request: 'LLMRequest', resp: 'LLMResponse', *, version: 'str' = 'current') -> 'None'` — 写入响应。
- 类 `CacheStore` (bases: Protocol) — 4 个公开方法
  - `delete(self, key: 'str') -> 'None'` — 删除 key；不存在静默。
  - `get(self, key: 'str') -> 'str | None'` — 读 key → JSON 字符串；不存在 None。
  - `put(self, key: 'str', value: 'str', *, ttl: 'int | None' = None) -> 'None'` — 写 key → JSON 字符串；ttl 秒（None 表示永不过期）。
  - `scan(self, prefix: 'str') -> 'list[tuple[str, str]]'` — 按前缀遍历返回 (key, value) 列表（语义缓存匹配候选集用）。
- 类 `ExactCache` (bases: object) — 3 个公开方法
  - `get(self, request: 'LLMRequest', *, version: 'str' = 'current') -> 'LLMResponse | None'` — 命中 +1 返回响应；未命中 +1 返回 None。
  - `invalidate(self, request: 'LLMRequest', *, version: 'str' = 'current') -> 'None'` — 删除精确键。
  - `put(self, request: 'LLMRequest', resp: 'LLMResponse', *, version: 'str' = 'current') -> 'None'` — 写入精确键 → 响应 JSON。
- 类 `HashCacheEmbedder` (bases: object) — 1 个公开方法
  - `embed(self, text: 'str') -> 'tuple[float, ...]'` — 文本 → L2 归一化的定长向量（二值化 + 头尾窗口截断）。
- 类 `InMemoryCacheStore` (bases: object) — 4 个公开方法
  - `delete(self, key: 'str') -> 'None'` — 删除 key 及其 TTL。
  - `get(self, key: 'str') -> 'str | None'` — 读 key；TTL 过期则删除并返回 None。
  - `put(self, key: 'str', value: 'str', *, ttl: 'int | None' = None) -> 'None'` — 写 key；ttl 给定时设置过期时刻。
  - `scan(self, prefix: 'str') -> 'list[tuple[str, str]]'` — 返回所有 prefix 开头的 (key, value)（跳过已过期项）。
- 类 `SimilarityCache` (bases: object) — 3 个公开方法
  - `get(self, request: 'LLMRequest', *, version: 'str' = 'current') -> 'LLMResponse | None'` — 按相似度找最匹配条目；≥ threshold 才命中。
  - `invalidate(self, request: 'LLMRequest', *, version: 'str' = 'current') -> 'None'` — 删除相似度 ≥ threshold 的条目（语义失效）。
  - `put(self, request: 'LLMRequest', resp: 'LLMResponse', *, version: 'str' = 'current') -> 'None'` — 写入条目（原文 + 可选向量 + 响应 JSON）到存储工具。

## `harness.model.capability` — src/harness/model/capability.py

- `__all__` (1): `ModelCapability`

- 类 `ModelCapability` (bases: object) — 7 个公开方法
  - `add_backend(self, name: 'str', llm: 'LLMPortWithUsage') -> 'None'` — 注册降级目标后端（如 "backup"），供网关按 typed 目标切换。
  - `build(self) -> 'Program'` — ToolLayer 外壳（规格 P4）：产出"用模型"的 Program 工厂。
  - `dispatch(self) -> 'CallFn'` — P4 dispatch 入口（§2.5 形态）：直接吃 (effect, ctx)，不构造 Invocation。
  - `gateway(self) -> 'ModelGateway'` — 构造模型网关（传入共用 registry 与通知总线）。
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'Registry'` — 统一装配：把 llm/router/cache 注册进共用 Provider 注册表。
  - `llm(self) -> 'LLMPortWithUsage'` — 默认后端实例（装配核对用）。
  - `registry(self) -> 'ModelRegistry'` — 本能力持有的注册表（装配核对用）。

## `harness.model.gateway` — src/harness/model/gateway.py

- `__all__` (1): `ModelGateway`

- 类 `ModelGateway` (bases: object) — 7 个公开方法
  - `as_call(self) -> 'Callable[[Invocation], Awaitable[Result]]'` — 兼容入口（可靠性执行器 core）：Invocation → _center。
  - `as_dispatch(self) -> 'Callable[[Effect, object], Awaitable[Result]]'` — §2.5 dispatch 形态（规格 P4）：直接吃 (effect, ctx)，不构造 Invocation。
  - `attach_bus(self, bus: 'LifecycleBus') -> 'None'` — 装配期总线共享（幂等）：把 lifecycle 总线接进网关通知通道。
  - `complete(self, request: 'LLMRequest', ctx: 'PortCtx', *, degrade_target: 'str | None' = None) -> 'LLMResponse'` — 一次完整模型调用：缓存命中直接返回，否则路由→调用→校验→记账。
  - `registry(self) -> 'ModelRegistry | None'` — 装配核对用：网关解析降级目标所用的注册表（与共用容器同源）。
  - `stream(self, request: 'LLMRequest', ctx: 'PortCtx', *, degrade_target: 'str | None' = None) -> 'AsyncIterator[str]'` — 流式补全（R4 §4.1）：缓存命中吐缓存，否则逐块转发并在结束时结算。
  - `stream_events(self, request: 'LLMRequest', ctx: 'PortCtx', *, degrade_target: 'str | None' = None) -> 'AsyncIterator[StreamEvent]'` — 结构化流式事件：透传后端事件，遇 USAGE 结算成本。

## `harness.model.ports` — src/harness/model/ports.py

- `__all__` (1): `LLMPortWithUsage`

- 类 `LLMPortWithUsage` (bases: LLMPort, Protocol) — 0 个公开方法

## `harness.model.registry` — src/harness/model/registry.py

- `__all__` (3): `ModelEndpoint`, `ModelRegistry`, `InMemoryModelRegistry`

- 类 `InMemoryModelRegistry` (bases: object) — 7 个公开方法
  - `has(self, name: 'str') -> 'bool'` — 名字是否已注册（端点/后端/provider 任一）。
  - `register(self, name: 'str', provider: 'Provider | None' = None, *, base_url: 'str | None' = None, api_key: 'str | None' = None, model: 'str | None' = None) -> 'None'` — 注册 provider（同名幂等/异实例拒绝）或端点连接信息（同名覆盖）。
  - `register_llm(self, name: 'str', llm: 'LLMPort') -> 'None'` — 注册后端实例：同名同实例幂等，同名异实例拒绝（R4 §5.3）。
  - `register_provider(self, name: 'str', provider: 'Provider') -> 'None'` — 注册具名 provider（llm/router/cache）：同名同实例幂等 / 异实例拒绝。
  - `resolve(self, name: 'str') -> 'Provider | ModelEndpoint | None'` — 解析 provider（优先）或端点；未注册返回 None。
  - `resolve_llm(self, name: 'str') -> 'LLMPort | None'` — 解析后端实例；未注册返回 None。
  - `resolve_provider(self, name: 'str') -> 'Provider | None'` — 解析具名 provider；未注册返回 None。
- 类 `ModelEndpoint` (bases: object) — 0 个公开方法
- 类 `ModelRegistry` (bases: Protocol) — 6 个公开方法
  - `has(self, name: 'str') -> 'bool'` — 名字是否已注册（端点/后端/provider 任一）。
  - `register(self, name: 'str', provider: 'Provider | None' = None, *, base_url: 'str | None' = None, api_key: 'str | None' = None, model: 'str | None' = None) -> 'None'` — 注册具名 provider（设计 §5.3）或端点连接信息（既有调用形态）。
  - `register_llm(self, name: 'str', llm: 'LLMPort') -> 'None'` — 注册已构造好的后端实例（供降级直接切换，跳过重建适配器）。
  - `register_provider(self, name: 'str', provider: 'Provider') -> 'None'` — 注册具名 provider（llm/router/cache），供两表统一；同名幂等/异实例拒绝。
  - `resolve(self, name: 'str') -> 'Provider | ModelEndpoint | None'` — 按名字解析 provider（优先）或端点；未注册返回 None。
  - `resolve_llm(self, name: 'str') -> 'LLMPort | None'` — 按名字解析后端实例；未注册返回 None。

## `harness.model.router` — src/harness/model/router.py

- `__all__` (3): `NO_DEFAULT`, `ModelRouter`, `RuleRouter`

- 类 `ModelRouter` (bases: Protocol) — 1 个公开方法
  - `route(self, effect: 'Effect', ctx: 'PortCtx') -> 'RouteDecision'` — 按 Effect 的任务提示与预算选模型。
- 类 `RuleRouter` (bases: object) — 1 个公开方法
  - `route(self, effect: 'Effect', ctx: 'PortCtx') -> 'RouteDecision'` — 按 task_hint / max_cost 选模型（payload 读取走 _coerce_route）。

## `harness.model.stream` — src/harness/model/stream.py

- `__all__` (2): `has_stream_events`, `wrap_text_stream`

- 函数 `has_stream_events(llm: 'LLMPort') -> 'bool'` — 后端是否已实现 stream_events()（具名能力探测，非动态 getattr 链）。
- 函数 `wrap_text_stream(llm: 'LLMPort', req: 'LLMRequest', ctx: 'PortCtx') -> 'AsyncIterator[StreamEvent]'` — 把文本流后端包成事件流：每块 TEXT_DELTA，末了 USAGE/DONE。

## `harness.model.structured` — src/harness/model/structured.py

- `__all__` (4): `MinimalSchemaValidator`, `SchemaValidator`, `StructuredOutputValidator`, `validate_json_schema`

- 函数 `validate_json_schema(value: 'object', schema: 'dict[str, DynamicValue]') -> 'list[str]'` — 校验一个值是否符合最小 JSON Schema，返回错误列表（空表示合法）。
- 类 `MinimalSchemaValidator` (bases: object) — 1 个公开方法
  - `validate(self, value: 'object', schema: 'dict[str, DynamicValue]') -> 'list[str]'` — 调用最小 JSON Schema 校验，返回错误列表。
- 类 `SchemaValidator` (bases: Protocol) — 1 个公开方法
  - `validate(self, value: 'object', schema: 'dict[str, DynamicValue]') -> 'list[str]'` — 返回错误描述列表；空列表表示合法。
- 类 `StructuredOutputValidator` (bases: object) — 6 个公开方法
  - `build_repair_prompt(self, bad_output: 'str', errors: 'list[str]') -> 'str'` — 生成修复提示：附上原输出与校验错误，让模型纠偏。
  - `check(self, text: 'str') -> 'list[str]'` — 用当前校验器校验输出文本，返回错误列表（供网关修复循环共用同一口径）。
  - `max_repair(self) -> 'int'` — 最大修复次数。
  - `schema_validator(self) -> 'SchemaValidator'` — 当前生效的校验器（默认或注入），供外部读取。
  - `should_repair(self) -> 'bool'` — 是否允许自然语言修复重试（枚举比较，非裸字符串）。
  - `validate(self, text: 'str') -> 'tuple[dict[str, DynamicValue] | list[DynamicValue] | str | int | None, bool]'` — 校验模型输出：解析 + schema 校验。

## `harness.model.types` — src/harness/model/types.py

- `__all__` (13): `BackendResolution`, `CacheKey`, `CostAttributor`, `CostGuard`, `CostRecord`, `RefuseExecution`, `Reservation`, `RouteDecision`, `StructuredOutputSpec`, `TargetUnresolved`, `UsageLedger`, `build_cache_key`, `cache_key_string`

- 函数 `build_cache_key(request: 'LLMRequest', *, prompt_version: 'str' = 'current') -> 'CacheKey'` — 构造 CacheKey 值对象（键含版本，供缓存隔离）。
- 函数 `cache_key_string(request: 'LLMRequest', *, prompt_version: 'str' = 'current') -> 'str'` — 构造缓存键字符串：hash(messages + model + params + prompt_version)。
- 类 `CacheKey` (bases: object) — 0 个公开方法
- 类 `CostAttributor` (bases: UsageLedger) — 4 个公开方法
  - `model_cost(self, model: 'str') -> 'float'` — 某 model 累计成本。
  - `run_cost(self, run_id: 'str') -> 'float'` — 某 run 累计成本。
  - `snapshot(self) -> 'dict[str, float | int]'` — 返回成本快照（总额 + 记录数），供 trace/评测。
  - `user_cost(self, user_id: 'str') -> 'float'` — 某 user 累计成本。
- 类 `CostGuard` (bases: object) — 6 个公开方法
  - `allow(self, estimated_cost: 'float') -> 'bool'` — （旧 API）预计成本加上已花是否仍在预算内；不含未结清预留。
  - `cancel(self, reservation: 'Reservation') -> 'None'` — 调用失败取消：释放预留且不记支出。
  - `charge(self, cost: 'float') -> 'None'` — （旧 API）扣减预算（调用后按实际成本）。
  - `commit(self, reservation: 'Reservation', actual: 'float') -> 'None'` — 调用后结清：释放预留、按实际成本扣减（差额自动退还/补扣）。
  - `remaining(self) -> 'float'` — 剩余可用预算（扣除已花与未结清预留）。
  - `reserve(self, estimated_cost: 'float') -> 'Reservation | None'` — 调用前预留额度：原子检查并占用，超预算返回 None（硬闸拒绝）。
- 类 `CostRecord` (bases: object) — 0 个公开方法
- 类 `RefuseExecution` (bases: object) — 1 个公开方法
  - `to_result(self) -> 'Result'` — 折叠为稳定失败信封（ok=False，retryable=False）。
- 类 `Reservation` (bases: object) — 0 个公开方法
- 类 `RouteDecision` (bases: object) — 0 个公开方法
- 类 `StructuredOutputSpec` (bases: BaseModel) — 0 个公开方法
- 类 `TargetUnresolved` (bases: object) — 1 个公开方法
  - `to_result(self) -> 'Result'` — 折叠为稳定失败信封（带未解析 target 便于诊断）。
- 类 `UsageLedger` (bases: object) — 5 个公开方法
  - `by(self, dim: 'str', value: 'str') -> 'float'` — 按维度取成本（如 by("model", "m1")）。
  - `count(self) -> 'int'` — 记录条数。
  - `record(self, rec: 'CostRecord') -> 'None'` — 记录一条成本（各维度同步累计）。
  - `tokens_by(self, dim: 'str', value: 'str') -> 'int'` — 按维度取 token 数。
  - `total(self) -> 'float'` — 累计总成本。

## `harness.observability` — src/harness/observability/__init__.py

- `__all__` (9): `TraceSpan`, `EventEnvelope`, `ReadModel`, `NoopTelemetry`, `Trace`, `InMemoryEventStream`, `Projection`, `TimelineProjection`, `ObservabilityCapability`


## `harness.observability.capability` — src/harness/observability/capability.py

- `__all__` (1): `ObservabilityCapability`

- 类 `ObservabilityCapability` (bases: object) — 1 个公开方法
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'None'` — 

## `harness.observability.stream` — src/harness/observability/stream.py

- `__all__` (3): `InMemoryEventStream`, `Projection`, `TimelineProjection`

- 类 `InMemoryEventStream` (bases: EventStreamPort) — 4 个公开方法
  - `dropped_count(self) -> 'int'` — 返回因背压丢弃的事件数。
  - `publish(self, event: 'EventEnvelope') -> 'None'` — 发布事件到其 topic（发布端做 R1 Event → EventEnvelope 转换的落点）。
  - `subscribe(self, topic: 'str', since: 'int | None' = None) -> 'AsyncIterator[DynamicValue]'` — 订阅 topic，从 since 序号之后开始（只读，不侵入内核）。
  - `topics(self) -> 'list[str]'` — 返回可订阅 topic 列表。
- 类 `Projection` (bases: object) — 1 个公开方法
  - `project(self, events: 'Iterable[EventEnvelope]') -> 'ReadModel'` — 从事件投影读模型（基类返回计数，子类覆写具体语义）。
- 类 `TimelineProjection` (bases: Projection) — 1 个公开方法
  - `project(self, events: 'Iterable[EventEnvelope]') -> 'ReadModel'` — 按事件序列产出时间线读模型。

## `harness.observability.telemetry` — src/harness/observability/telemetry.py

- `__all__` (2): `NoopTelemetry`, `Trace`

- 类 `NoopTelemetry` (bases: TelemetryPort) — 6 个公开方法
  - `log(self, event: 'dict[str, DynamicValue]') -> 'None'` — 记录结构化日志事件。
  - `logs(self) -> 'tuple[dict[str, DynamicValue], ...]'` — 返回日志快照。
  - `metric(self, name: 'str', value: 'float', tags: 'dict[str, DynamicValue] | None' = None) -> 'None'` — 记录指标（同步入队）。
  - `metrics(self) -> 'tuple[tuple[str, float, dict[str, DynamicValue] | None], ...]'` — 返回指标快照。
  - `span(self, name: 'str', ctx: 'PortCtx') -> 'AbstractContextManager[DynamicValue]'` — 开启 span（返回上下文管理器，供 with 使用）。
  - `spans(self) -> 'tuple[TraceSpan, ...]'` — 返回 span 快照。
- 类 `Trace` (bases: object) — 4 个公开方法
  - `record_log(self, event: 'dict[str, DynamicValue]') -> 'None'` — 记录日志到遥测后端。
  - `record_metric(self, name: 'str', value: 'float', tags: 'dict[str, DynamicValue] | None' = None) -> 'None'` — 记录指标到遥测后端。
  - `span(self, name: 'str', *, component: 'str' = '') -> 'Iterator[TraceSpan]'` — 开启嵌套 span：自动接 parent_id，退出后恢复父级并记录真实耗时。
  - `spans(self) -> 'tuple[TraceSpan, ...]'` — 返回本 trace 的全部 span。
- 类 `TracebackType` (bases: object) — 0 个公开方法
- 类 `count` (bases: object) — 0 个公开方法

## `harness.observability.types` — src/harness/observability/types.py

- `__all__` (3): `TraceSpan`, `EventEnvelope`, `ReadModel`

- 类 `EventEnvelope` (bases: object) — 0 个公开方法
- 类 `ReadModel` (bases: object) — 0 个公开方法
- 类 `TraceSpan` (bases: object) — 0 个公开方法

## `harness.prompt` — src/harness/prompt/__init__.py

- `__all__` (3): `PromptTemplate`, `PromptCapability`, `DEFAULT_TEMPLATES`

- 类 `PromptCapability` (bases: object) — 3 个公开方法
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'None'` — Capability 协议：把模板注册表登记进通用 Registry。
  - `register(self, template: 'PromptTemplate') -> 'None'` — 注册模板（同名覆盖——显式注入优先于默认）。
  - `render(self, name: 'str', **variables: 'DynamicValue') -> 'list[dict[str, DynamicValue]]'` — 按模板名渲染为 role/content 消息；未注册模板 fail-closed 报错。
- 类 `PromptTemplate` (bases: object) — 1 个公开方法
  - `render(self, **variables: 'DynamicValue') -> 'list[dict[str, DynamicValue]]'` — 渲染为 role/content 消息列表（缺变量由 string.Template 抛 KeyError）。

## `harness.rag` — src/harness/rag/__init__.py

- `__all__` (28): `RAGPipeline`, `CachePort`, `InMemoryCache`, `RAGCapability`, `Document`, `Chunk`, `Hit`, `Query`, `QueryPurpose`, `RAGResult`, `TokenBudget`, `Vector`, `Splitter`, `RecursiveSplitter`, `Embedder`, `EmbeddingClient`, `HashEmbedder`, `VectorStore`, `InMemoryVectorStore`, `Retriever`, `VectorRetriever`, `HybridRetriever`, `PostProcessor`, `DefaultPostProcessor`, `QueryRewriter`, `MultiQueryRewriter`, `Reranker`, `LexicalReranker`


## `harness.rag.capability` — src/harness/rag/capability.py

- `__all__` (1): `RAGCapability`

- 类 `RAGCapability` (bases: object) — 3 个公开方法
  - `ingest(self, documents: 'list[DynamicValue]', ctx: 'PortCtx') -> 'int'` — 入库：委托 pipeline（真实实现，不造假）。
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'None'` — Capability 协议：注册自己为 rag port provider（履约 RAGPort）。
  - `retrieve(self, query: 'str', ctx: 'PortCtx') -> 'list[DynamicValue]'` — 检索：委托 pipeline 的 retrieve（真实实现）。

## `harness.rag.embedder` — src/harness/rag/embedder.py

- `__all__` (4): `DEFAULT_DIM`, `Embedder`, `EmbeddingClient`, `HashEmbedder`

- 类 `Embedder` (bases: Protocol) — 1 个公开方法
  - `embed(self, texts: 'list[str]') -> 'list[Vector]'` — 把文本批转成向量列表。
- 类 `EmbeddingClient` (bases: Protocol) — 1 个公开方法
  - `embed(self, texts: 'list[str]') -> 'list[Vector]'` — 把文本批转成向量列表。
- 类 `HashEmbedder` (bases: object) — 1 个公开方法
  - `embed(self, texts: 'list[str]') -> 'list[Vector]'` — 委托后端向量化。
- 类 `blake2b` (bases: object) — 0 个公开方法

## `harness.rag.pipeline` — src/harness/rag/pipeline.py

- `__all__` (4): `PIPELINE_NAME`, `CachePort`, `InMemoryCache`, `RAGPipeline`

- 类 `CachePort` (bases: Protocol) — 3 个公开方法
  - `get(self, key: 'str') -> 'RAGResult | None'` — 按键取缓存，未命中返回 None。
  - `invalidate(self, key: 'str | None' = None) -> 'None'` — 失效单个键；None 表示清空全部。
  - `set(self, key: 'str', value: 'RAGResult') -> 'None'` — 写入缓存。
- 类 `InMemoryCache` (bases: object) — 3 个公开方法
  - `get(self, key: 'str') -> 'RAGResult | None'` — 按键取缓存。
  - `invalidate(self, key: 'str | None' = None) -> 'None'` — 失效单个键或全部。
  - `set(self, key: 'str', value: 'RAGResult') -> 'None'` — 写入缓存。
- 类 `RAGPipeline` (bases: object) — 6 个公开方法
  - `build(self) -> 'Program'` — ToolLayer 外壳（规格 P7）：产出"用 RAG"的 Program 工厂。
  - `ingest(self, documents: 'list[DynamicValue]', ctx: 'PortCtx') -> 'int'` — RAGPort 入口：切分→向量化→入库，返回成功入库块数。
  - `invalidate_cache(self) -> 'None'` — 按 key 失效本实例写入的检索缓存（不无参全表清空）。
  - `query(self, q: 'Query') -> 'RAGResult'` — 富入口：完整检索链，返回结构化 RAGResult。
  - `retrieve(self, query: 'str', ctx: 'PortCtx') -> 'list[DynamicValue]'` — RAGPort 入口：str → Query（预算取 ctx）→ query() → 引用投影。
  - `with_(self, **replacements: 'object') -> 'RAGPipeline'` — 不可变替换：返回注入点被替换的新实例，原实例不变（S1）。

## `harness.rag.post` — src/harness/rag/post.py

- `__all__` (3): `EMPTY_CONTEXT`, `DefaultPostProcessor`, `PostProcessor`

- 类 `DefaultPostProcessor` (bases: object) — 2 个公开方法
  - `assemble(self, hits: 'list[Hit]', budget: 'TokenBudget', *, detector: 'InjectionDetector | None' = None, filters: 'dict[str, object] | None' = None) -> 'RAGResult'` — 过滤后按预算贪心装入，生成带引用的上下文。
  - `filter(self, hits: 'list[Hit]', detector: 'InjectionDetector | None' = None, filters: 'dict[str, object] | None' = None) -> 'list[Hit]'` — 兜底过滤：元数据/权限/时效 + 注入检测，两者任一不过即剔除。
- 类 `PostProcessor` (bases: Protocol) — 2 个公开方法
  - `assemble(self, hits: 'list[Hit]', budget: 'TokenBudget', *, detector: 'InjectionDetector | None' = None, filters: 'dict[str, object] | None' = None) -> 'RAGResult'` — 按预算拼接为 RAGResult。
  - `filter(self, hits: 'list[Hit]', detector: 'InjectionDetector | None' = None, filters: 'dict[str, object] | None' = None) -> 'list[Hit]'` — 过滤命中（权限/时效/注入）。

## `harness.rag.rerank` — src/harness/rag/rerank.py

- `__all__` (2): `LexicalReranker`, `Reranker`

- 类 `LexicalReranker` (bases: object) — 1 个公开方法
  - `rerank(self, hits: 'list[Hit]', query: 'Query') -> 'list[Hit]'` — 按词项覆盖度重排并返回前 top_n。
- 类 `Reranker` (bases: Protocol) — 1 个公开方法
  - `rerank(self, hits: 'list[Hit]', query: 'Query') -> 'list[Hit]'` — 对候选命中重排。

## `harness.rag.retriever` — src/harness/rag/retriever.py

- `__all__` (3): `HybridRetriever`, `Retriever`, `VectorRetriever`

- 类 `HybridRetriever` (bases: object) — 1 个公开方法
  - `retrieve(self, query: 'Query', vec: 'Vector') -> 'list[Hit]'` — 两路召回后 RRF 融合去重，两路都施加 filters。
- 类 `Retriever` (bases: Protocol) — 1 个公开方法
  - `retrieve(self, query: 'Query', vec: 'Vector') -> 'list[Hit]'` — 用给定查询向量召回命中列表。
- 类 `VectorRetriever` (bases: object) — 1 个公开方法
  - `retrieve(self, query: 'Query', vec: 'Vector') -> 'list[Hit]'` — 用查询向量召回 top_k，filters 在 store 层生效。

## `harness.rag.rewrite` — src/harness/rag/rewrite.py

- `__all__` (3): `DEFAULT_N_VARIANTS`, `MultiQueryRewriter`, `QueryRewriter`

- 类 `MultiQueryRewriter` (bases: object) — 1 个公开方法
  - `rewrite(self, query: 'Query') -> 'list[Query]'` — 产出最多 n_variants 个文本不同的等价查询，原串排第一。
- 类 `QueryRewriter` (bases: Protocol) — 1 个公开方法
  - `rewrite(self, query: 'Query') -> 'list[Query]'` — 把一个查询改写为多个查询。

## `harness.rag.splitter` — src/harness/rag/splitter.py

- `__all__` (4): `DEFAULT_CHUNK_SIZE`, `DEFAULT_OVERLAP`, `RecursiveSplitter`, `Splitter`

- 类 `RecursiveSplitter` (bases: object) — 1 个公开方法
  - `split(self, doc: 'Document') -> 'list[Chunk]'` — 把文档正文切成带 id/token 估算的块。
- 类 `Splitter` (bases: Protocol) — 1 个公开方法
  - `split(self, doc: 'Document') -> 'list[Chunk]'` — 把文档切成块列表。

## `harness.rag.store` — src/harness/rag/store.py

- `__all__` (3): `InMemoryVectorStore`, `VectorStore`, `matches_filters`

- 函数 `matches_filters(chunk: 'Chunk', filters: 'dict[str, object] | None') -> 'bool'` — 检查块是否满足过滤条件——向量通道与关键词通道共用的唯一判定。
- 类 `InMemoryVectorStore` (bases: object) — 3 个公开方法
  - `all_chunks(self) -> 'list[Chunk]'` — 返回库内全部块（供混合召回的关键词通道做词面打分）。
  - `search(self, vec: 'Vector', top_k: 'int', filters: 'dict[str, object] | None' = None) -> 'list[Hit]'` — 点积相似度排序，按 meta 过滤后取 top_k。
  - `upsert(self, chunks: 'list[Chunk]') -> 'None'` — 追加块引用到库中（当前为追加语义，按 chunk_id 覆盖的增量去重未实现）。
- 类 `VectorStore` (bases: Protocol) — 2 个公开方法
  - `search(self, vec: 'Vector', top_k: 'int', filters: 'dict[str, object] | None' = None) -> 'list[Hit]'` — 按向量相似度返回 top_k 命中。
  - `upsert(self, chunks: 'list[Chunk]') -> 'None'` — 写入块（含其向量）。

## `harness.rag.types` — src/harness/rag/types.py

- `__all__` (9): `Vector`, `QueryPurpose`, `Document`, `Chunk`, `Hit`, `Query`, `TokenBudget`, `RAGResult`, `RRF_K`

- 类 `Chunk` (bases: _Strict) — 0 个公开方法
- 类 `Document` (bases: _Strict) — 0 个公开方法
- 类 `Hit` (bases: _Strict) — 0 个公开方法
- 类 `Query` (bases: _Strict) — 0 个公开方法
- 类 `QueryPurpose` (bases: str, Enum) — 0 个公开方法
- 类 `RAGResult` (bases: _Strict) — 0 个公开方法
- 类 `TokenBudget` (bases: _Strict) — 0 个公开方法

## `harness.resilience` — src/harness/resilience/__init__.py

- `__all__` (30): `FailureClass`, `FailureClassifier`, `DimensionKey`, `DEFAULT_FAILURE_TABLE`, `backoff_delay`, `classify_failure`, `normalize_code`, `validate_retry_spec`, `validate_ladder`, `DegradeMode`, `DegradeStep`, `DegradeLadder`, `Jitter`, `RetrySpec`, `BreakerStatus`, `BreakerState`, `BreakerStore`, `InMemoryBreakerStore`, `RetryPolicy`, `CircuitBreaker`, `Degradation`, `RateLimit`, `Bulkhead`, `Idempotency`, `Watchdog`, `KillSwitch`, `ResilienceCapability`, `Timeout`, `RatePermit`, `ReliabilityExecutor`


## `harness.resilience.breaker` — src/harness/resilience/breaker.py

- `__all__` (3): `BreakerStore`, `CircuitBreaker`, `InMemoryBreakerStore`

- 类 `BreakerStore` (bases: Protocol) — 2 个公开方法
  - `load(self, dim: 'Dimension') -> 'BreakerState | None'` — 
  - `save(self, dim: 'Dimension', state: 'BreakerState') -> 'None'` — 
- 类 `CircuitBreaker` (bases: object) — 6 个公开方法
  - `check(self, dimension: 'Dimension | None') -> 'str | None'` — 返回降级原因（OPEN 冷却内 / 半开超额），健康返回 None。
  - `maybe_half_open(self, key: 'object') -> 'None'` — 公开 API：冷却期满 OPEN→HALF_OPEN（带锁）。
  - `observe_outcome(self, inv: 'Invocation', result: 'Result') -> 'None'` — 把一次调用结果记入熔断三态机（领域语义）。
  - `permit(self, inv: 'Invocation') -> 'str | None'` — 熔断许可判定（领域语义）：返回降级原因表示不放行，健康返回 None。
  - `record(self, result: 'Result', dimension: 'Dimension | None' = None, *, count: 'bool' = True) -> 'None'` — 记录一次调用结果并推进三态机（锁内 load→decide→save）。
  - `state(self, key: 'object') -> 'BreakerState'` — 读取维度当前状态（无记录视为 CLOSED 默认态）。
- 类 `InMemoryBreakerStore` (bases: object) — 2 个公开方法
  - `load(self, dim: 'Dimension') -> 'BreakerState | None'` — 读取维度快照；无记录返回 None。
  - `save(self, dim: 'Dimension', state: 'BreakerState') -> 'None'` — 写入维度快照（覆盖）。

## `harness.resilience.capability` — src/harness/resilience/capability.py

- `__all__` (1): `ResilienceCapability`

- 类 `ResilienceCapability` (bases: object) — 3 个公开方法
  - `install(self, container: 'object | None' = None, reg: 'object | None' = None) -> 'None'` — Capability 协议装配（install(container, reg)）：注册执行器工厂。
  - `plugins(self) -> 'tuple[object, ...]'` — 已选领域策略（内置 + extra），顺序确定（供装配核验）。
  - `wrap(self, core: 'Callable[[Invocation], Result | Awaitable[Result]]', *, registry: 'object | None' = None, bus: 'object | None' = None, notify: 'Callable[[str, Mapping[str, object]], None] | None' = None) -> 'ReliabilityExecutor'` — 把中心 Port 调用包装进可靠性执行器（原 install 的包装语义）。

## `harness.resilience.defenses` — src/harness/resilience/defenses.py

- `__all__` (8): `Bulkhead`, `Degradation`, `Idempotency`, `KillSwitch`, `RatePermit`, `RateLimit`, `Watchdog`, `failed_target_of`

- 函数 `failed_target_of(inv: 'Invocation') -> 'str'` — 从 Invocation 派生失败目标注册名（B2：阶梯后端注册名，非维度二元组）。
- 类 `Bulkhead` (bases: object) — 3 个公开方法
  - `acquire(self, pool: 'str') -> 'bool'` — 占用一个槽；成功 True，池满 False。
  - `release(self, pool: 'str') -> 'None'` — 释放一个槽（下限 0，防重复释放）。
  - `run_in_slot(self, inv: 'Invocation', next: 'NextFn') -> 'Result'` — 舱位隔离执行（领域语义）：占槽 → 调下一跳 → finally 释放。
- 类 `Degradation` (bases: object) — 2 个公开方法
  - `pick(self, available: 'set[str] | None' = None) -> 'DegradeStep'` — 从给定（或初始）可用集里选 quality 最高级，全无则 REFUSE。
  - `select(self, fault: 'FaultRecord', *, session_id: 'str') -> 'str'` — 唯一选级裁决（四坑落点）。
- 类 `Idempotency` (bases: object) — 1 个公开方法
  - `check_and_mark(self, key: 'str') -> 'bool'` — 返回 True 表示窗口内已见过（命中去重），False 表示首次（并登记）。
- 类 `KillSwitch` (bases: object) — 1 个公开方法
  - `is_tripped(self, session_id: 'str') -> 'bool'` — 紧急开关是否置位（领域语义）：全局标志或本作用域标志为真即置位。
- 类 `RateLimit` (bases: object) — 1 个公开方法
  - `acquire(self) -> 'RatePermit'` — 取一个令牌（限流领域语义）。
- 类 `RatePermit` (bases: str, Enum) — 0 个公开方法
- 类 `Watchdog` (bases: object) — 2 个公开方法
  - `detect(self, inv: 'Invocation') -> 'bool'` — 从 Invoke.port 派生观测标识并检测循环（执行器入口）。
  - `inspect(self, kind: 'str') -> 'bool'` — 记录一个观测点并检测循环（看门狗领域语义）。

## `harness.resilience.executor` — src/harness/resilience/executor.py

- `__all__` (1): `ReliabilityExecutor`

- 类 `ReliabilityExecutor` (bases: object) — 2 个公开方法
  - `as_call(self) -> 'CallFn'` — 产出符合 R1 CallFn 签名的 call（构造 Invocation，R1 零感知）。
  - `run_invocation(self, inv: 'Invocation') -> 'Result'` — 直接对给定 Invocation 跑完整链（测试入口）。

## `harness.resilience.retry` — src/harness/resilience/retry.py

- `__all__` (1): `RetryPolicy`

- 类 `RetryPolicy` (bases: object) — 1 个公开方法
  - `run_with_retries(self, inv: 'Invocation', next: 'NextFn') -> 'Result'` — 重试执行（领域语义）：失败退避重放；耗尽写 fault。

## `harness.resilience.timeout` — src/harness/resilience/timeout.py

- `__all__` (1): `Timeout`

- 类 `Timeout` (bases: object) — 1 个公开方法
  - `run_with_deadline(self, inv: 'Invocation', next: 'NextFn') -> 'Result'` — 时限执行（领域语义）：超时则写 fault + TIMEOUT 失败，否则透传。

## `harness.resilience.types` — src/harness/resilience/types.py

- `__all__` (16): `DEFAULT_FAILURE_TABLE`, `BreakerState`, `BreakerStatus`, `DegradeLadder`, `DegradeMode`, `DegradeStep`, `DimensionKey`, `FailureClass`, `FailureClassifier`, `Jitter`, `RetrySpec`, `backoff_delay`, `classify_failure`, `normalize_code`, `validate_ladder`, `validate_retry_spec`

- 函数 `backoff_delay(attempt: 'int', spec: 'RetrySpec', rng: 'Random', *, prev_delay: 'float' = 0.0, retry_after: 'float | None' = None) -> 'float'` — 计算第 attempt 次重试的退避秒数（attempt 从 1 起）。
- 函数 `classify_failure(result: 'Result', ctx: 'object', *, classifier: 'FailureClassifier | None' = None) -> 'FailureClass'` — 统一分类入口：注入自定义分类器则委托，否则用默认表。
- 函数 `normalize_code(raw: 'str') -> 'str'` — 把错误码归一到语义族（HTTP 码与符号码等价）。
- 函数 `validate_ladder(ladder: 'DegradeLadder') -> 'None'` — 装配期强校验降级阶梯（R3 红线）。
- 函数 `validate_retry_spec(spec: 'RetrySpec') -> 'None'` — 装配期校验重试参数完整性。
- 类 `BreakerState` (bases: object) — 1 个公开方法
  - `failure_rate(self) -> 'float'` — 滑窗失败率；空窗返回 0。
- 类 `BreakerStatus` (bases: str, Enum) — 0 个公开方法
- 类 `DegradeLadder` (bases: object) — 1 个公开方法
  - `pick(self, available: 'AbstractSet[str]') -> 'DegradeStep'` — 从可用目标集合里选 quality 最高的一级；无可用则落到拒答级。
- 类 `DegradeMode` (bases: str, Enum) — 0 个公开方法
- 类 `DegradeStep` (bases: object) — 0 个公开方法
- 类 `DimensionKey` (bases: object) — 1 个公开方法
  - `as_tuple(self) -> 'tuple[str, str]'` — 转为二元组表示（便于做 store key）。
- 类 `FailureClass` (bases: object) — 0 个公开方法
- 类 `FailureClassifier` (bases: Protocol) — 1 个公开方法
  - `classify(self, result: 'Result', ctx: 'object') -> 'FailureClass'` — 对一次 Port 结果分类；异常由调用方折叠，不抛穿。
- 类 `Jitter` (bases: str, Enum) — 1 个公开方法
  - `coerce(<no-signature>)` — 把字符串归一为枚举成员；未知值显式回退到 FULL（规则 5）。
- 类 `RetrySpec` (bases: object) — 0 个公开方法

## `harness.security` — src/harness/security/__init__.py

- `__all__` (14): `SecurityCtx`, `SafetyVerdict`, `SafetyReport`, `AuditEvent`, `verify_hash_chain`, `Guardrail`, `RuleGuardrail`, `inspect_untrusted`, `redact_secret`, `LocalSandbox`, `AuditLogger`, `ConsoleHumanPort`, `ApprovalFlow`, `SecurityCapability`


## `harness.security.approval` — src/harness/security/approval.py

- `__all__` (1): `ApprovalFlow`

- 类 `ApprovalFlow` (bases: object) — 2 个公开方法
  - `decide_timeout(self) -> 'str'` — 超时裁决：一律 deny（不静默放行）。
  - `should_approve(self, danger: 'DangerLevel') -> 'bool'` — 危险级别达到触发线才审批（CRITICAL 默认；HIGH 只沙盒不审批）。

## `harness.security.capability` — src/harness/security/capability.py

- `__all__` (1): `SecurityCapability`

- 类 `SecurityCapability` (bases: object) — 2 个公开方法
  - `build(self) -> 'Program'` — ToolLayer 外壳：安全能力的 Program 入口。
  - `install(self, container: 'object', reg: 'Registry') -> 'None'` — 把三 Port 注册进共用 Provider 注册表（对齐 compose.Capability 协议）。

## `harness.security.guard` — src/harness/security/guard.py

- `__all__` (4): `Guardrail`, `RuleGuardrail`, `inspect_untrusted`, `redact_secret`

- 函数 `inspect_untrusted(text: 'str', ctx: 'SecurityCtx', *, detector: 'KeywordInjectionDetector | None' = None) -> 'SafetyReport'` — 注入检测单一入口：trust=False 过复用检测器；trust=True 返回 safe。
- 函数 `redact_secret(value: 'object', secrets: 'Sequence[str]') -> 'SafetyVerdict'` — 敏感串脱敏：把 secrets 明文替换为 ***，返回 redact 裁决（不复用组合层 Verdict）。
- 类 `Guardrail` (bases: Protocol) — 1 个公开方法
  - `check(self, data: 'DynamicValue', ctx: 'SecurityCtx') -> 'SafetyVerdict'` — 
- 类 `RuleGuardrail` (bases: object) — 1 个公开方法
  - `check(self, data: 'object', ctx: 'SecurityCtx') -> 'SafetyVerdict'` — 护栏：不可信内容命中注入串 → deny；含 secret 明文 → redact（fail closed）。

## `harness.security.ports` — src/harness/security/ports.py

- `__all__` (3): `LocalSandbox`, `AuditLogger`, `ConsoleHumanPort`

- 类 `AuditLogger` (bases: AuditPort) — 3 个公开方法
  - `bind_sink(self, sink: 'AuditStore') -> 'None'` — 装配期绑定审计落盘后端（外部注入审计器也能接入同源底座）。
  - `events(self) -> 'tuple[AuditEvent, ...]'` — 返回 append-only 快照（tuple 不可变）。
  - `record(self, event: 'dict[str, DynamicValue]', ctx: 'PortCtx') -> 'None'` — 追加一条审计事件（hash 链，不可旁路）；有 sink 则同源落底座。
- 类 `ConsoleHumanPort` (bases: HumanPort) — 1 个公开方法
  - `ask(self, question: 'str', options: 'list[str] | None', ctx: 'PortCtx') -> 'str'` — 向人提问并返回回答；挂起态跨周期恢复由 Runner.suspend/resume 承担。
- 类 `LocalSandbox` (bases: SandboxPort) — 1 个公开方法
  - `run(self, fn: 'Callable[..., DynamicValue]', args: 'DynamicValue', ctx: 'PortCtx') -> 'DynamicValue'` — 在隔离线程执行 fn(**args)；awaitable 结果归一。

## `harness.security.types` — src/harness/security/types.py

- `__all__` (4): `SecurityCtx`, `SafetyVerdict`, `SafetyReport`, `AuditEvent`

- 函数 `verify_hash_chain(events: 'tuple[AuditEvent, ...]') -> 'bool'` — 校验哈希链：首条 prev_hash 为空，每条 hash 与字段重算一致且接上一条。
- 类 `AuditEvent` (bases: object) — 1 个公开方法
  - `sealed(self, prev_hash: 'str') -> 'AuditEvent'` — 返回接上前序 hash 的封印条目（不可变原对象保持不变）。
- 类 `SafetyReport` (bases: object) — 1 个公开方法
  - `from_verdict(verdict: 'InjectionVerdict', *, rule: 'str' = '', evidence: 'str' = '') -> 'SafetyReport'` — 单一检测器裁决 → 安全域报告（复用 skills 检测器，不造第二套）。
- 类 `SafetyVerdict` (bases: object) — 0 个公开方法
- 类 `SecurityCtx` (bases: object) — 2 个公开方法
  - `from_invocation(<no-signature>)` — 从 Invocation 投影（不改原对象，避免安全域依赖全量组合层字段语义）。
  - `from_port_ctx(<no-signature>)` — 从 PortCtx 投影（R9 三 Port 的消费侧便捷入口）。

## `harness.skills` — src/harness/skills/__init__.py

- `__all__` (4): `InjectionVerdict`, `InjectionDetector`, `KeywordInjectionDetector`, `FileSkillProvider`


## `harness.skills.provider` — src/harness/skills/provider.py

- `__all__` (4): `InjectionVerdict`, `InjectionDetector`, `KeywordInjectionDetector`, `FileSkillProvider`

- 类 `FileSkillProvider` (bases: object) — 3 个公开方法
  - `body(self, name: 'str') -> 'str'` — 按需加载技能正文（L3），命中注入则拒绝披露。
  - `catalog(self) -> 'list[SkillManifest]'` — 返回技能目录（L1 常驻披露，与 discover 同形）。
  - `discover(self) -> 'list[SkillManifest]'` — 扫描磁盘发现所有可用技能的具名清单。
- 类 `InjectionDetector` (bases: Protocol) — 1 个公开方法
  - `detect(self, text: 'str') -> 'InjectionVerdict'` — 检测文本是否含注入特征。
- 类 `InjectionVerdict` (bases: str, Enum) — 0 个公开方法
- 类 `KeywordInjectionDetector` (bases: object) — 1 个公开方法
  - `detect(self, text: 'str') -> 'InjectionVerdict'` — 大小写不敏感地查找任一关键词。

## `harness.tools` — src/harness/tools/__init__.py

- `__all__` (17): `CodeExecTool`, `DangerLevel`, `FileExecTool`, `MCPManager`, `MCPTransport`, `SandboxPolicy`, `ShellExecTool`, `ToolFn`, `ToolIndex`, `ToolRegistry`, `ToolSource`, `ToolsCapability`, `WebFetcher`, `WebFetchTool`, `clear_pending`, `pending_tools`, `tool`


## `harness.tools.capability` — src/harness/tools/capability.py

- `__all__` (1): `ToolsCapability`

- 类 `ToolsCapability` (bases: object) — 9 个公开方法
  - `bind_registry(self, registry: 'ToolRegistry') -> 'None'` — 把披露索引绑定到执行链的同一 registry（A4 同源修复）。
  - `build(self) -> 'Program'` — ToolLayer 外壳（规格 P5）：产出"用工具"的 Program 工厂（内部不改）。
  - `connect_mcp(self, registry: 'ToolRegistry', ctx: 'PortCtx', *, servers: 'tuple[str, ...] | None' = None) -> 'tuple[str, ...]'` — 把注入的 MCP transport 接进指定 registry（原设计 mcp.py:83 connect）。
  - `disclose(self, names: 'list[str]') -> 'DisclosurePacket'` — 委托披露索引生成披露包，技能清单从 SkillPort.catalog() 取。
  - `index(self) -> 'DisclosureIndex'` — 返回本能力持有的披露索引。
  - `install(self, container: 'object', reg: 'Registry') -> 'None'` — 把持有的 port 注册进共用 Provider 注册表（对齐 Capability 两参协议）。
  - `manager(self) -> 'MCPManager | None'` — 返回 MCP 管理器；未装配时为 None。
  - `registry(self) -> 'ToolRegistry'` — 返回本能力持有的工具注册表。
  - `skills(self) -> 'SkillPort | None'` — 返回技能 Port；未装配时为 None。

## `harness.tools.decorator` — src/harness/tools/decorator.py

- `__all__` (4): `ToolFn`, `tool`, `pending_tools`, `clear_pending`

- 函数 `clear_pending() -> 'None'` — 清空待注册容器（供测试隔离与装配层显式重置）；不改变已注册工具。
- 函数 `pending_tools() -> 'tuple[ToolFn, ...]'` — 返回待注册工具的不可变快照（防外部改写内部容器）。
- 函数 `tool(*, name: 'str | None' = None, description: 'str | None' = None, danger: 'DangerLevel', sandbox: 'SandboxPolicy' = <SandboxPolicy.AUTO: 'auto'>, timeout: 'float' = 30.0, retries: 'int' = 2) -> 'Callable[[Callable[..., DynamicValue]], ToolFn]'` — 装饰器工厂：生成带 ToolSpec 的 ToolFn。
- 类 `ToolFn` (bases: object) — 1 个公开方法
  - `__call__(self, **kwargs: 'DynamicValue') -> 'DynamicValue'` — 以关键字参数调用工具实现（参数校验由 pydantic 模型或 registry 完成）。

## `harness.tools.execution` — src/harness/tools/execution.py

- `__all__` (3): `CodeExecTool`, `FileExecTool`, `ShellExecTool`

- 类 `CodeExecTool` (bases: _ExecTool) — 1 个公开方法
  - `execute(self, code: 'str', ctx: 'PortCtx') -> 'bool'` — 执行源码字符串；返回是否成功（校验经 spec 由调用方负责）。
- 类 `FileExecTool` (bases: _ExecTool) — 1 个公开方法
  - `read(self, path: 'str', ctx: 'PortCtx') -> 'bool'` — 校验路径在虚拟根内再交 sandbox；越界/空根直接拒绝。
- 类 `ShellExecTool` (bases: _ExecTool) — 1 个公开方法
  - `execute(self, command: 'str', ctx: 'PortCtx') -> 'bool'` — 校验命令首词在白名单内再交 sandbox；非白名单零执行。

## `harness.tools.index` — src/harness/tools/index.py

- `__all__` (1): `ToolIndex`

- 类 `ToolIndex` (bases: object) — 1 个公开方法
  - `disclose(self, names: 'list[str]', skills: 'list[SkillManifest]') -> 'DisclosurePacket'` — 按命中名与技能清单生成披露包；超预算抛 ValueError。

## `harness.tools.mcp` — src/harness/tools/mcp.py

- `__all__` (2): `MCPTransport`, `MCPManager`

- 类 `MCPManager` (bases: object) — 5 个公开方法
  - `call_tool(self, server: 'str', tool: 'str', args: 'dict[str, DynamicValue]', ctx: 'PortCtx') -> 'DynamicValue'` — 委托 transport 调用原始工具（带 ctx 门面）。
  - `connect(self, server: 'str', ctx: 'PortCtx') -> 'list[ToolSpec]'` — 发现并注入 server 的工具，返回加前缀后的 spec。
  - `health(self, ctx: 'PortCtx') -> 'dict[str, bool]'` — 对所有已连接 server 逐一 ping，返回可达性字典。
  - `list_tools(self, server: 'str', ctx: 'PortCtx') -> 'list[ToolSpec]'` — 委托 transport 列出原始工具（带 ctx 门面，不改全名）。
  - `ping(self, server: 'str', ctx: 'PortCtx') -> 'bool'` — 委托 transport 探测 server 可达性。
- 类 `MCPTransport` (bases: Protocol) — 3 个公开方法
  - `call_tool(self, server: 'str', tool: 'str', args: 'dict[str, DynamicValue]') -> 'DynamicValue'` — 调用工具，返回形态由工具决定（动态边界）。
  - `list_tools(self, server: 'str') -> 'list[ToolSpec]'` — 列出 server 暴露的工具（原始名，未加前缀）。
  - `ping(self, server: 'str') -> 'bool'` — 探测 server 可达性。

## `harness.tools.registry` — src/harness/tools/registry.py

- `__all__` (1): `ToolRegistry`

- 类 `ToolRegistry` (bases: object) — 8 个公开方法
  - `bind(self, name: 'str', fn: 'Callable[..., DynamicValue]') -> 'None'` — 延迟绑定/替换已注册工具的执行体；未注册则 KeyError。
  - `call(self, call: 'ToolCall', ctx: 'PortCtx') -> 'ToolResult'` — 统一调用入口：四级 danger 路由 + 参数校验 + 异常折叠 + 耗时记录。
  - `catalog(self, level: 'int' = 1) -> 'list[ToolSpec]'` — 按注册顺序返回目录；level=1 清空 schema，level>=2 返回完整 spec。
  - `freeze(self) -> 'Self'` — 冻结目录（之后 register 抛 RuntimeError）；bind 仍可用于热更执行体。
  - `register(self, spec: 'ToolSpec', fn: 'Callable[..., DynamicValue]') -> 'None'` — 注册工具（重名拒绝）；danger=None 允许注册，由 static_check 暴露问题。
  - `register_pending(self) -> 'list[str]'` — 装配期批量注册 @tool 待注册项，成功即清空容器（v2.2 自动注册）。
  - `schema_of(self, name: 'str') -> 'dict[str, DynamicValue]'` — 按名返回完整 parameters_schema（未注册 KeyError）；返回副本防外部改写。
  - `static_check(self) -> 'list[str]'` — 装配期静态检查：danger 缺失、高危无沙盒、CRITICAL 无人工，逐条列出。

## `harness.tools.types` — src/harness/tools/types.py

- `__all__` (3): `ToolSource`, `SandboxPolicy`, `DangerLevel`

- 类 `SandboxPolicy` (bases: str, Enum) — 0 个公开方法
- 类 `ToolSource` (bases: str, Enum) — 0 个公开方法

## `harness.tools.webfetch` — src/harness/tools/webfetch.py

- `__all__` (2): `WebFetcher`, `WebFetchTool`

- 类 `WebFetchTool` (bases: object) — 1 个公开方法
  - `fetch(self, url: 'str', ctx: 'PortCtx') -> 'str'` — 抓取并检测：命中注入则拒载（ValueError）并审计；干净正文放行。
- 类 `WebFetcher` (bases: Protocol) — 1 个公开方法
  - `fetch(self, url: 'str', ctx: 'PortCtx') -> 'str'` — 抓取给定 URL 的原始正文（形态为文本，交由检测器净化）。

## `ports.adapters` — src/ports/adapters/__init__.py

- `__all__` (6): `OpenAICompatLLM`, `HttpMCPTransport`, `StdioChannel`, `StdioMCPTransport`, `SubprocessStdioChannel`, `HttpFetcher`


## `ports.adapters.llm` — src/ports/adapters/llm.py

- `__all__` (1): `OpenAICompatLLM`

- 类 `OpenAICompatLLM` (bases: object) — 2 个公开方法
  - `complete(self, req: 'LLMRequest', ctx: 'PortCtx') -> 'LLMResponse'` — 一次性补全：真 POST，解析响应；错误折叠为零 usage 响应，不抛裸异常。
  - `stream(self, req: 'LLMRequest', ctx: 'PortCtx') -> 'AsyncIterator[str]'` — 流式补全：逐块解析 SSE data 行的 delta.content；末块 usage 写 last_usage。

## `ports.adapters.mcp` — src/ports/adapters/mcp.py

- `__all__` (7): `MCP_CLIENT_NAME`, `MCP_CLIENT_VERSION`, `MCP_PROTOCOL_VERSION`, `HttpMCPTransport`, `StdioChannel`, `StdioMCPTransport`, `SubprocessStdioChannel`

- 类 `HttpMCPTransport` (bases: object) — 5 个公开方法
  - `aclose(self) -> 'None'` — 关闭自有的 httpx client（注入的 client 由调用方负责关闭）。
  - `call_tool(self, server: 'str', tool: 'str', args: 'dict[str, DynamicValue]') -> 'DynamicValue'` — 调用工具：text 内容拼接（动态值由工具决定，交给上层信封承载）。
  - `list_tools(self, server: 'str') -> 'list[ToolSpec]'` — 发现工具：与 stdio 同构（同一帧助手，保证两种 transport 行为一致）。
  - `ping(self, server: 'str') -> 'bool'` — 健康检查：标准 ping，异常折叠为 False。
  - `reconnect(self) -> 'None'` — 显式重连：复位握手状态，下次调用重新 initialize（不做副作用重放）。
- 类 `StdioChannel` (bases: Protocol) — 3 个公开方法
  - `close(self) -> 'None'` — 关闭通道（终止子进程/释放管道）。
  - `recv(self) -> 'dict[str, DynamicValue]'` — 从 server stdout 读一行并解析为 JSON 帧。
  - `send(self, message: 'dict[str, DynamicValue]') -> 'None'` — 向 server stdin 写一行 JSON 帧。
- 类 `StdioMCPTransport` (bases: object) — 5 个公开方法
  - `aclose(self) -> 'None'` — 关闭底层 stdio 通道（进程/管道资源释放）。
  - `call_tool(self, server: 'str', tool: 'str', args: 'dict[str, DynamicValue]') -> 'DynamicValue'` — 调用工具：tools/call 的 text 内容拼成返回值（形态由工具决定）。
  - `list_tools(self, server: 'str') -> 'list[ToolSpec]'` — 发现工具：tools/list 的 inputSchema 映射为 ToolSpec（同 Registry 目录形态）。
  - `ping(self, server: 'str') -> 'bool'` — 健康检查：标准 ping 成功即 True；异常折叠为 False 不抛穿。
  - `reconnect(self) -> 'None'` — 显式重连：关闭旧通道 + 复位握手状态（不自动重试 call，防副作用重复执行）。
- 类 `SubprocessStdioChannel` (bases: object) — 3 个公开方法
  - `close(self) -> 'None'` — 关闭 stdin 并终止子进程（幂等；未启动过则无需处理）。
  - `recv(self) -> 'dict[str, DynamicValue]'` — 读一帧：stdout 空行即 EOF，显式报错不伪装成功。
  - `send(self, message: 'dict[str, DynamicValue]') -> 'None'` — 写一帧：JSON + 换行分隔（MCP stdio 的帧定界约定）。

## `ports.adapters.provider` — src/ports/adapters/provider.py

- `__all__` (3): `LLMProviderFactory`, `LLMProviderCapability`, `openai_http`

- 函数 `openai_http(*, base_url: 'str', api_key: 'str', model: 'str') -> 'LLMPort'` — 默认工厂：OpenAI-compatible HTTP 适配器（DeepSeek/Moonshot/OpenAI/vLLM）。
- 类 `LLMProviderCapability` (bases: object) — 5 个公开方法
  - `install(self, container: 'object | None' = None, reg: 'Registry | None' = None) -> 'None'` — Capability 协议：把构造好的 provider 注册进通用 Registry。
  - `install_into(self, registry: 'ModelRegistry') -> 'None'` — 把 provider 实例登记进 ModelRegistry（供网关多后端降级真消费）。
  - `names(self) -> 'tuple[str, ...]'` — 已注册 provider 名（按注册顺序）。
  - `register(self, name: 'str', *, factory: 'Callable[..., LLMPort] | None' = None, base_url: 'str', api_key: 'str', model: 'str') -> 'LLMPort'` — 注册端点配置；同名幂等（返回已有实例），异配置拒绝（fail-closed）。
  - `resolve(self, name: 'str') -> 'LLMPort | None'` — 解析已构造的 provider 实例（未注册返回 None）。
- 类 `LLMProviderFactory` (bases: Protocol) — 1 个公开方法
  - `__call__(self, *, base_url: 'str', api_key: 'str', model: 'str') -> 'LLMPort'` — 按端点配置构造 provider 实例（履约 LLMPort：complete/stream）。

## `ports.adapters.webfetch` — src/ports/adapters/webfetch.py

- `__all__` (1): `HttpFetcher`

- 类 `HttpFetcher` (bases: object) — 2 个公开方法
  - `aclose(self) -> 'None'` — 仅关闭自建 client；注入的 client 归调用方所有，本适配器不越权关闭。
  - `fetch(self, url: 'str', ctx: 'PortCtx') -> 'str'` — GET 抓取：非 2xx/网络异常折叠 RuntimeError，正文按 max_bytes 截断后返回文本。

## `runtime` — src/runtime/__init__.py

- `__all__` (52): `DONE`, `AgentState`, `Budgets`, `Effect`, `EffectPayload`, `Event`, `EventStore`, `Message`, `PortError`, `Program`, `Result`, `Usage`, `RunResult`, `RuntimeCtx`, `Session`, `StepReport`, `JsonSnapshot`, `StateSnapshot`, `BudgetDelta`, `DynamicValue`, `Payload`, `Scratchpad`, `DoneSentinel`, `Disposition`, `PortName`, `Invoke`, `Spawn`, `Suspend`, `Terminate`, `disposition_of`, `CallFn`, `CancellationToken`, `DispatchFn`, `canonical`, `checkpoint`, `fingerprint`, `restore_checkpoint`, `fold`, `replay_events`, `state_append_message`, `state_set`, `Interpreter`, `InterpreterImpl`, `EffectBatch`, `Runner`, `InMemoryEventStore`, `AgentKitError`, `BudgetExceeded`, `ChainAborted`, `PortMissing`, `ProgramCrashed`, `ProgramInvalid`


## `runtime.errors` — src/runtime/errors.py

- 类 `AgentKitError` (bases: Exception) — 0 个公开方法
- 类 `BudgetExceeded` (bases: AgentKitError) — 0 个公开方法
- 类 `ChainAborted` (bases: AgentKitError) — 0 个公开方法
- 类 `PortMissing` (bases: AgentKitError) — 0 个公开方法
- 类 `ProgramCrashed` (bases: AgentKitError) — 0 个公开方法
- 类 `ProgramInvalid` (bases: AgentKitError) — 0 个公开方法

## `runtime.fold` — src/runtime/fold.py

- `__all__` (5): `field`, `fold`, `replay_events`, `state_append_message`, `state_set`

- 函数 `field(obj: 'DynamicValue', name: 'str', default: 'DynamicValue' = None) -> 'DynamicValue'` — 读取对象字段，兼容三种形态：pydantic 模型 / dict / dataclass / Replayed。
- 函数 `fold(state: 'AgentState', effects: 'Sequence[Effect]', results: 'Sequence[Result]', reason: 'str' = '') -> 'AgentState'` — 把一批 Effect + Result 折叠为新 State（消息/预算/托管字段全产生新对象）。
- 函数 `replay_events(events: 'Iterable[DynamicValue]', initial_state: 'AgentState') -> 'AgentState'` — 事件溯源重放：逐步 fold 得到终态。
- 函数 `state_append_message(state: 'AgentState', message: 'Message') -> 'AgentState'` — 向对话历史追加一条消息，返回新 State（messages 重建为 tuple）。
- 函数 `state_set(state: 'AgentState', key: 'str', value: 'DynamicValue') -> 'AgentState'` — Program 用的不可变 scratch 写入（phase/cursor/steps…），返回新 State。

## `runtime.interpreter` — src/runtime/interpreter.py

- `__all__` (4): `EffectBatch`, `Interpreter`, `InterpreterImpl`, `SendResult`

- 类 `EffectBatch` (bases: object) — 0 个公开方法
- 类 `Interpreter` (bases: Protocol) — 1 个公开方法
  - `send(self, value: 'DynamicValue') -> 'SendResult'` — 推进一步。
- 类 `InterpreterImpl` (bases: object) — 2 个公开方法
  - `send(self, value: 'DynamicValue') -> 'SendResult'` — 推进一步：首次 next() 启动，其后 send(value) 回填上次结果。
  - `started(self) -> 'bool'` — generator 是否已被首次推进（True 表示后续应走 send 而非 next）。

## `runtime.runner` — src/runtime/runner.py

- `__all__` (2): `InMemoryEventStore`, `Runner`

- 类 `InMemoryEventStore` (bases: EventStore) — 2 个公开方法
  - `load(self, session_id: 'str') -> 'JsonSnapshot | None'` — 按会话 id 读取快照；不存在返回 None。
  - `save(self, session_id: 'str', snapshot: 'JsonSnapshot') -> 'None'` — 把快照按会话 id 存入内存字典。
- 类 `Runner` (bases: object) — 6 个公开方法
  - `cancel(self, reason: 'str') -> 'None'` — 运行中请求取消：下一 step 检查到后停止执行任何 Effect。
  - `load(self, session_id: 'str', program_factory: 'Callable[[], Program]') -> 'Session'` — 从 checkpoint 恢复 Session。
  - `replay(self, session: 'Session', initial_state: 'AgentState') -> 'AgentState'` — 事件溯源重放：从初始 State 逐步 fold 得到终态。
  - `resume(self, session: 'Session', answer: 'DynamicValue') -> 'RunResult'` — 从挂起断点继续：答案 send 回 Program，事件日志不动。
  - `run(self, session: 'Session') -> 'RunResult'` — 跑到 done / suspended / error。
  - `step(self, session: 'Session') -> 'StepReport'` — 执行一个 step（供调试 / HITL 单步使用）。

## `runtime.types` — src/runtime/types.py

- `__all__` (36): `DONE`, `AgentState`, `BudgetDelta`, `Budgets`, `CallFn`, `CancellationToken`, `DispatchFn`, `Disposition`, `DoneSentinel`, `DynamicValue`, `Effect`, `EffectPayload`, `Event`, `EventStore`, `Invoke`, `JsonSnapshot`, `Message`, `Payload`, `PortError`, `PortName`, `Program`, `Result`, `RunResult`, `RuntimeCtx`, `Scratchpad`, `Session`, `Spawn`, `StateSnapshot`, `StepReport`, `Suspend`, `Terminate`, `Usage`, `canonical`, `checkpoint`, `fingerprint`, `restore_checkpoint`

- 函数 `canonical(obj: 'DynamicValue') -> 'str'` — 确定性 JSON：sort_keys + 紧凑分隔符，事件哈希与快照比对的基础。
- 函数 `checkpoint(session: 'Session') -> 'JsonSnapshot'` — 把会话折叠为可 JSON 持久化的快照（不含 program，因其不可序列化）。
- 函数 `disposition_of(effect: 'Effect') -> 'Disposition'` — 按 Effect 具体类型返回判别标签（checkpoint 持久化与分派用）。
- 函数 `fingerprint(events: 'Sequence[Event]') -> 'str'` — 事件日志指纹（确定性门禁比对它）；对规范化列表取 SHA256。
- 函数 `restore_checkpoint(snapshot: 'JsonSnapshot', program_factory: 'Callable[[], Program]') -> 'Session'` — 从快照重建 State 与事件日志；Program 纯函数 + 重放定位断点。
- 函数 `restore_state(data: 'StateSnapshot') -> 'AgentState'` — 从规范化快照还原 AgentState（兼容旧快照：缺字段回退 scratch）。
- 函数 `revive(obj: 'DynamicValue') -> 'DynamicValue'` — 把规范化后的纯 dict/list 递归还原为可属性访问的对象。
- 类 `AgentState` (bases: object) — 3 个公开方法
  - `get(self, key: 'str', default: 'DynamicValue' = None) -> 'DynamicValue'` — 统一读：框架托管键读显式字段，其余读 scratch。
  - `set(self, key: 'str', value: 'DynamicValue') -> 'AgentState'` — 写入单键，返回新对象（不可变语义）。
  - `with_(self, **changes: 'DynamicValue') -> 'AgentState'` — 唯一状态变更入口：返回新对象，托管键改写到显式字段。
- 类 `BudgetDelta` (bases: dict) — 0 个公开方法
- 类 `Budgets` (bases: object) — 3 个公开方法
  - `check_parallel(self, count: 'int') -> 'str | None'` — 检查并行数是否超限；不产生副作用。
  - `consume(self, *, turns: 'int' = 0, hops: 'int' = 0, rounds: 'int' = 0, cost_usd: 'float' = 0.0, tokens: 'int' = 0, parallel_children: 'int' = 0) -> 'Budgets'` — 按各维度增量生成新 Budgets（成本 round 到 10 位避免浮点累积）。
  - `exceeded(self) -> 'str | None'` — 按固定优先级检查所有硬上限，返回首个超限维度名。
- 类 `CancellationToken` (bases: object) — 1 个公开方法
  - `cancel(self, reason: 'str') -> 'CancellationToken'` — 生成已取消的新令牌（原令牌不变）。
- 类 `Disposition` (bases: str, Enum) — 0 个公开方法
- 类 `EffectPayload` (bases: dict) — 0 个公开方法
- 类 `Event` (bases: object) — 0 个公开方法
- 类 `EventStore` (bases: Protocol) — 2 个公开方法
  - `load(self, session_id: 'str') -> 'JsonSnapshot | None'` — 读取会话快照；不存在返回 None。
  - `save(self, session_id: 'str', snapshot: 'JsonSnapshot') -> 'None'` — 持久化会话快照。参数：session_id 会话标识，snapshot 可 JSON 序列化快照信封。
- 类 `Invoke` (bases: object) — 1 个公开方法
  - `with_payload(self, **extra: 'DynamicValue') -> 'Invoke'` — 在副本上增量更新 payload，返回新 Invoke（供组合层改写消费）。
- 类 `JsonSnapshot` (bases: dict) — 0 个公开方法
- 类 `MappingProxyType` (bases: object) — 0 个公开方法
- 类 `Message` (bases: object) — 1 个公开方法
  - `to_dict(self) -> 'dict[str, DynamicValue]'` — 转为可序列化字典，供 vendor payload 与快照复用。
- 类 `PortError` (bases: object) — 0 个公开方法
- 类 `PortName` (bases: str, Enum) — 0 个公开方法
- 类 `Replayed` (bases: dict) — 0 个公开方法
- 类 `Result` (bases: object) — 2 个公开方法
  - `failure(<no-signature>)` — 构造失败信封，折叠异常为 PortError。
  - `success(<no-signature>)` — 构造成功信封。
- 类 `RunResult` (bases: object) — 0 个公开方法
- 类 `RuntimeCtx` (bases: object) — 0 个公开方法
- 类 `Session` (bases: object) — 0 个公开方法
- 类 `Spawn` (bases: object) — 0 个公开方法
- 类 `StateSnapshot` (bases: dict) — 0 个公开方法
- 类 `StepReport` (bases: object) — 0 个公开方法
- 类 `Suspend` (bases: object) — 0 个公开方法
- 类 `Terminate` (bases: object) — 0 个公开方法
- 类 `Usage` (bases: object) — 1 个公开方法
  - `from_mapping(<no-signature>)` — 从外部形态恢复 Usage 的唯一归一化边界（规则 6）。
