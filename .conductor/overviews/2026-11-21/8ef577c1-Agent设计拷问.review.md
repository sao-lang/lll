# 拷问结论：目标 Agent API vs 现状框架（2026-11-23 23:xx）

> 场景9 拷问设计 · readonly · full · 未修改任何源码/文档
> 用户目标 API（5 条语义）：① `@tool(description, danger)` + 类型注解推导 schema + 一份声明三处消费；
> ② `Agent(ReAct([fn]), system=...)` system 一等参数；③ `agent.use(ModelCapability).use(MemoryCapability)` 能力插拔；
> ④ `agent.say()` 记忆召回/写回钩子自动发生；⑤ `agent.stream` 可观测底座内置自建、外接才 use sink。

## 逐条诊断（严重度 + 证据）

| 目标语义 | 现状事实 | 证据 | 判定 |
|---|---|---|---|
| `@tool(description=...)` | `tool()` 签名无 `description`（只有 name/danger/sandbox/timeout/retries）；描述只能取 docstring 首行。照抄目标写法 → `TypeError` | `src/harness/tools/decorator.py:81-88,135` | **BLOCKER 不存在** |
| schema 从 `city: str` 类型注解推导 | 仅当首参是 pydantic `BaseModel` 才 `model_json_schema()`；逐参数注解 → 空 schema `{"type":"object","properties":{}}`，反而被 extra=forbid 拒收实参 | `decorator.py:104-131`；`src/harness/tools/registry.py:199-206` | **BLOCKER 反向有害** |
| 一份声明三处消费 | demo 里 name 重复 4 处、danger 2 处、description 2 处、schema 1 处却无消费点；`register_tool` 丢 parameters_schema；`gateway._center` 不写 tools_catalog → schema 到不了 LLM | `examples/react-chat/main.py:68-84,160-163,251-252`；`src/api/agent.py:222-223`；`src/harness/model/gateway.py:219-223`；`src/ports/adapters/llm.py:75-83` | **BLOCKER 三链断裂** |
| `system` 一等参数 | `api.Agent.__init__` 无 system；`src/api/` 全目录 `system` 零匹配；ReAct 首轮只发 user；靠用户侧 `_SystemPromptLLM` 包装器绕过（demo 自称"不改框架"） | `src/api/agent.py:185-193`；`src/archs/recipes.py:224-226`；`main.py:133-157,237` | **BLOCKER 靠 hack** |
| `use()` 链式装配 | 全项目 `def use` / `.use(` 零匹配；只有 `capability()`（返回 self） | `src` grep；`src/api/agent.py:211-215` | **BLOCKER 不存在** |
| `MemoryCapability()` | 全项目零定义；记忆只有 MemoryStore / MemoryPortWithRecall / ContextMemoryManager，无 Capability 外壳；profile 里的 rag/observability 只是占位字符串 | `src` class 穷举；`src/api/profiles.py:11-15`；`src/api/builder.py:77-79` | **BLOCKER 不存在** |
| say() 钩子自动召回/写回 | `say/asay/_arun` 全文无 before/after 钩子；`src/api` 无 recall/ingest；记忆全手动（write/recall/ingest/commit 都在 demo）；设计文档要求挂 session_end/before_model/after_tool，代码未实现 | `src/api/agent.py:383-432`；`main.py:187-189,303-304`；`AGENT-总设计.md:1327` | **BLOCKER 不存在** |
| `agent.stream` 属性 | 无该公开属性（仅私有 `_stream`）；读法为 `agent.runtime.stream`；但底座**确实内置自建**：未注入 stream 时 assemble_runtime 自动 new InMemoryEventStream + 自动 bind bus | `src/api/agent.py:201,205`；`src/api/runtime.py:71,175,177`；`src/api/stream.py:13-36` | **PARTIAL 底座符合，属性/外接 sink 协议缺** |
| `DangerLevel.LOW` / `danger=` | 单一源存在（SAFE/LOW/HIGH/CRITICAL），@tool 与门面 register_tool 都支持 danger | `src/compose/dto.py:55-81`；`decorator.py:84`；`agent.py:220` | **OK** |
| `ModelCapability(llm=llm)` | 存在且 demo 已装配成功 | `src/harness/model/capability.py:24`；`main.py:250` | **OK** |
| `ReAct([...])` | ReAct 存在，但构造吃 `list[ToolSpec]`，目标传的是 `@tool` 产出的 ToolFn 列表 → 类型不符 | `src/archs/recipes.py:227`；`main.py:246` | **PARTIAL 签名不符** |

## 根因诊断（5 条）

1. **双 Agent 类分裂**：`api.Agent`（门面，say/run）与 `compose.Agent`（装配器，capability/rag/memory/telemetry/event_stream + build）同名并存；后者的 memory/rag/telemetry/event_stream 是**死注入**——写入字段后 build() 从不读取。用户目标里的 `Agent` 语义落不到唯一载体上。（`src/api/agent.py:174` / `src/compose/agent.py:29,52-78,89-122`）
2. **Capability 协议缺 build()**：协议只有 `install/name/version/requires`；设计文档却写 `build()->Program`；门面 Program 恒为 `arch.compile()`，能力永远进不了 Program。ResilienceCapability.install 签名还独立于协议。（`src/compose/registry.py:44-62` / `AGENT-总设计.md:95` / `api/agent.py:408` / `harness/resilience/capability.py:71-102`）
3. **schema 文化是手写 dict**：无 type→JSON Schema 通用推导；且推导/传递三处断裂（@tool 逐参数空 schema / register_tool 丢 schema / gateway 丢 tools_catalog），导致 schema 看起来传了实际没到 LLM（离线 FakeLLM 硬编码 tool_calls 掩盖了断点）。
4. **记忆有组件、无能力壳、无钩子**：ContextMemoryManager 有 recall()/commit_and_compact()，但门面从不构造它，也无生命周期通知点订阅。
5. **设计哲学冲突（最本质）**：用户要的是 agent-centric（主体 Agent + 能力插拔 + say 一条链自动回写）；现状是 arch+capability+manual-hook-centric（工具/记忆/可观测散落 5 个子系统，用户手动装配）。不是补几个方法能搞定，是公共契约层重构。

## 铁律约束（不可绕）

- R1（`src/runtime/`）零改动（D-7）；依赖方向 R3~R9 → compose → R1，R1 零依赖上层。
- 禁兼容层/双轨，改契约同次删旧路径；禁 fake/stub 冒充实现；每个功能失败测试先行。
- 本设计触及 Agent/tool/Capability 三个公共契约 → full 档，S2 需需求覆盖矩阵 + coverage_check rc=0；规格未确认不动手。
- 本轮只读，未改码。

## 待用户拍板的 frontier（下一轮 AskUserQuestion）

- Q1 双 Agent 归宿：合并到单一 api.Agent 门面 + 删 compose.Agent 死链（推荐）？还是保留分层？
- Q2 schema 推导：逐参数注解推导替换现有 pydantic 模型二选一（推荐）？还是保留模型单参、改用户写法？
- Q3 MemoryCapability 形态：新建 Capability 壳挂 say 钩子（before_model 召回 / session_end 写回，推荐）？还是做进 arch？
- Q4 system 落点：Agent 一等参数、内部落 context PIN 段（推荐）？还是纯 prompt 段？

## 门禁登记

- coverage_matrix：路由卡判定"不需要"（未命中 FR≥8 等 big-requirement 触发；本轮只读拷问不产契约）。
- commitment_ledger check --strict：rc=1，60 项 pending 全为阶段A/B 历史存量承诺，与本轮只读拷问无新增关系，登记为历史存量、非本轮 Blocker。
- 证据口径：所有判定均带文件:行号，关键断言已由主 agent 二次 grep/read 核验。
