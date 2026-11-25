# 三项设计边界决策方案（2026-11-25）

## 背景与范围

三批系统性修复 + 两轮独立复审后，全量 pytest / mypy / 门禁 / 真网取证均绿。剩余三项**不是 bug**，而是"代码行为与文档契约/安全语义存在张力"的设计边界。按源码纯净铁律与"不照审计盲改"的教训（本轮已有两次盲改回退），这些需要你拍板，我不单方面改。

每项给出：现状证据（文件:行号）→ 选项权衡 → 我的推荐 → 采纳后的落地步骤。

---

## 一、RAG ingest 的租户隔离

### 现状证据

| 事实 | 出处 |
|---|---|
| `Document.doc_id` 文档明示"全局唯一" | `src/harness/rag/types.py:52` |
| `chunk_id = f"{doc_id}:{i}"` | `src/harness/rag/splitter.py:62` |
| `ingest(documents, ctx)` 收 ctx 但 `del ctx`，不消费 | `src/harness/rag/pipeline.py:151-161` |
| `retrieve(query, ctx)` 只从 ctx 取 budget；租户过滤靠调用方显式传 `Query.filters` | `src/harness/rag/pipeline.py:179-189` |
| 复现：tenant A/B 同 doc_id 入库 → B 覆盖 A；按 tenant=A 过滤召回为空 | `tmp/review2_memory_rag.py` D 段 |
| 对照：MemoryStore 用 `ScopeResolver.resolve(ctx)` 做租户隔离 | `src/harness/memory/types.py:189` |

**冲突点**：RAG 层与 Memory 层的租户纪律不一致。但 `doc_id` 全局唯一是**明示契约**，不是遗漏——上游若遵守，覆盖不会发生。

### 选项

| | 做法 | 收益 | 成本 / 风险 |
|---|---|---|---|
| **A** | 保持约定：doc_id 全局唯一由上游保证 | 零改动 | 跨租户同 doc_id 仍静默覆盖；需在文档明示"上游必须保证 doc_id 含租户维度或全局唯一" |
| **B** | chunk_id 加租户前缀 `f"{tenant}:{doc_id}:{i}"` | 彻底隔离 | **破坏 doc_id 溯源/引用语义**；存量 chunk_id 全变（引用失效）；RAGPort 契约变更 |
| **C** | ingest 把 ctx 的 scope 写进 `chunk.meta`，retrieve 自动合并 tenant 过滤（显式 filters 优先） | 不改 chunk_id、保留溯源；与 Memory 层纪律对齐 | RAG 层引入 ScopeResolver（跨模块依赖）；需定义显式 filters 与自动过滤冲突时的优先级 |
| **D** | ingest fail-closed 查重：同 doc_id 已存在且 tenant 不同则拒绝 | 不改数据格式；暴露上游错误 | 需 store 支持按 doc_id 查；合法"跨租户共享 doc_id"场景被误拒 |

### 推荐：**C（附 D 的轻量冲突校验）**

保留 doc_id 溯源语义（B 的代价不可逆），同时补齐与 Memory 层一致的租户纪律。落地：

1. `RAGPipeline.ingest` 用 `ScopeResolver.resolve(ctx)` 解析 tenant，写入每个 chunk 的 `meta["tenant"]`
2. `RAGPipeline.retrieve` 从 ctx 解析 tenant，合并进 `q.filters`；**显式 filters 优先**，但若显式 tenant 与 ctx tenant 不一致则 fail-closed 拒绝（而非静默取其一）
3. 补 RED 测试：跨租户同 doc_id 入库后各自按 ctx 召回互不污染

**残留风险**：`Document.meta` 已带 `tenant` 时与 ctx 冲突——需明确"ctx 权威，meta 仅补充"，否则双来源。

---

## 二、Watchdog 的跨 run 循环检测语义

### 现状证据

| 事实 | 出处 |
|---|---|
| Watchdog 是注入式**长生命周期**策略（非每 run 新建） | `src/harness/resilience/capability.py:114` |
| `_history: OrderedDict[session, list]`，本轮已加 LRU 上限（默认 1024） | `src/harness/resilience/defenses.py:305-311` |
| 同 session 跨 run 历史不清：run1 两次 llm + run2 首次 llm → 误判 3 连 True | `tmp/review_watchdog.py` D 段 |
| R1 有 `session_start/session_end` 相位，经 LifecycleBus 发 | `src/runtime/runner.py:89-92`、`src/compose/notify.py:42-44` |
| Watchdog 是 R3 策略，**不在 bus 上**；`detect(inv)` 在 `_emergency_preflight` 调用 | `src/harness/resilience/executor.py:146-150` |

**冲突点**：循环检测的观察窗应该按 run 还是按策略实例生命周期？当前语义下，用户第二次提问时 Agent 正常调一次工具可能被上次 run 的历史误判为循环。

### 选项

| | 做法 | 收益 | 成本 / 风险 |
|---|---|---|---|
| **A** | 保持：历史跨 run 累积 | 零改动 | 新 run 首批调用可能被误拒（静默） |
| **B** | 加 `Watchdog.reset(session_id=None)` 显式 API，由装配层在 run 边界调用 | 干净；不改 R1、不改 Invocation 契约 | 需在门面加调用点；裸用 Runner 的用户需自己调（docstring 明示） |
| **C** | Watchdog 订阅 bus 的 session_start/end 自动清 | 全自动 | 需把 R3 策略接进 R11 总线，**破坏依赖方向**（工具层 → 组合层 → 内核）；侵入大 |
| **D** | 历史项带时间戳，只统计时间窗内 | 不依赖边界信号 | 语义变化大；可能漏检"慢循环" |

### 推荐：**B**

在 `Watchdog` 加 `reset(session_id: str | None = None)`（不传清全部，传则清单桶），由 `api/agent.py` 在 `_arun` 装配后、run 前调用——门面已持有 capabilities 与 bus，能拿到策略实例。不改 R1、不改 bus 契约。

**残留风险**：裸用 `Runner` + 手动注入 Watchdog 的调用方不在门面覆盖内——在 `Watchdog.detect` docstring 明示"跨 run 需调用方 reset"。

---

## 三、danger_route 的死打标

### 现状证据

| 事实 | 出处 |
|---|---|
| docstring 承诺"按 payload.danger 经 registry 路由到沙箱 Port"，实现只写 `ctx.meta` 后 `await base()` | `src/compose/call.py:75-81` |
| 全 src 零消费方（`x-compose.danger_route` 无读取处） | 三路复审一致 |
| 真路由在 ToolRegistry，以**注册 spec.danger**为准，明确忽略伪造的 `call.danger` | `src/harness/tools/registry.py:194` |
| 引用面：src + tests + docs + gate_check 共 **72 处** | `grep -r danger_route` |
| 测试固化打标语义 | `tests/r12/test_call.py:99`、`tests/r2/test_compose_call_guards.py:216`、`tests/e2e/test_coverage_compose_context_tools.py:186` |

**冲突点**：docstring 撒谎（承诺路由，实际打标）+ 死代码（打标无消费方）。但"照审计改成真路由"会用 LLM/用户可控的 `payload.danger` 作权威 → **引入 fail-open 安全漏洞**，绝不可取。

### 选项

| | 做法 | 收益 | 成本 / 风险 |
|---|---|---|---|
| **A** | 照审计改：以 payload.danger 路由沙箱 | "兑现 docstring" | **fail-open**：LLM 可控字段变权威，安全退化 |
| **B** | 保持现状 | 零改动 | docstring 继续误导；死代码违反源码纯净铁律 |
| **C** | 诚实化：docstring 改为"危险级别标注（供观测/审计），权威路由在 ToolRegistry"；`_Keys.ROUTE`→`DANGER_MARK` | 消除误导；零行为变化；零 API 破坏 | 打标本身仍是死代码，但语义诚实 |
| **D** | 删除整个装饰器，同步 72 处引用 + gate_check + 文档 | 彻底清死代码 | **破坏公开 API**；大工程；多个测试要重写 |

### 推荐：**C**

安全上 A 直接排除。D 的收益（清一个死打标）不足以覆盖 72 处改动的回归风险与公开 API 破坏。C 是最小完整变更：让代码说实话，保留"外部观测可读取标注"的合法用途。若后续确认无外部用户，再单独走 D（届时作为独立的 API 废弃需求，配迁移说明）。

---

## 决策请求

请对三项各给一个选择（A/B/C/D 或其它方向）。未拍板前我不改这三处代码——现状全绿，不存在需要立即修的故障。

| 项 | 我的推荐 | 你的决定 |
|---|---|---|
| 1 RAG 租户隔离 | C（meta 注入 + 自动过滤，显式优先、冲突 fail-closed） | ? |
| 2 Watchdog 跨 run | B（显式 reset API + 门面在 run 边界调用） | ? |
| 3 danger_route | C（docstring 诚实化 + 内部常量改名） | ? |

---

## 附：为什么这些不在本轮直接改

- 三项都涉及**公开 API 或跨层依赖或安全语义**的契约变更，属产品/架构决策，不是可以 TDD 红→绿收口的 bug。
- 本轮已有两次"照审计/探针盲改"回退（say 的 static_check、approval 阈值）——教训是：看似明显的"不一致"，往往是刻意的设计分工。
- 源码纯净铁律禁止兼容层与假实现；上述推荐方案都已排除"加一层兜底"的短路做法。
