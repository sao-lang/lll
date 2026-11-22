# 代码精读 01 · context 上下文模块

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/context/` 全部 11 个文件。
> 总入口见 [00](./00-总入口与全局链路.md)，记忆本体见 [07 MemoryStore](./07-MemoryStore精读.md)。

---

## 目录

1. 一句话心智模型
2. 模块文件地图
3. 数据模型与全部常量（types.py）
4. 统一 token 口径（token_counter.py）
5. 组装链（assembler.py）
6. 压缩链（compaction.py）
7. 重注入链（reinjector.py）
8. 卸载链（offloader.py + offload_policy.py）
9. KV cache 观测（observability.py）
10. 编排器（manager.py）：四链的显式串联
11. Port 扩展协议（ports.py）
12. 端到端：一次 commit_and_compact
13. 细节速记卡

---

## 1. 一句话心智模型

context 模块干一件事：**在把消息送给模型前，把有限的上下文窗口花在刀刃上**。它有四条链：

- **组装**：把多个上下文段按优先级、配额、预算拼成消息列表。
- **压缩**：对话历史超阈值时，钉住高价值消息，其余折叠成一条摘要。
- **重注入**：把摘要、钉住片段、未完成任务按预算塞回下一轮。
- **卸载**：超大的工具结果落盘换句柄，而不是截断（截断会丢尾部数据）。

四条链由 `ContextMemoryManager` 用**代码调用顺序**显式编排——不是靠 hook、不是靠订阅通知。这是它最重要的设计特征。

边界：短期记忆归 R1（State.messages），这里只做上下文工程；长期记忆的存取委托给 `MemoryStore`（07 册）。

---

## 2. 模块文件地图

| 文件 | 角色 |
|---|---|
| `types.py` | DTO + 全部阈值常量 |
| `token_counter.py` | 统一 token 计数口径 |
| `assembler.py` | 组装链 |
| `compaction.py` | 压缩链 |
| `reinjector.py` | 重注入链 |
| `offloader.py` | BlobPort 协议 + 内存实现 + Offloader |
| `offload_policy.py` | 卸载策略（含「截断被否决」） |
| `observability.py` | CacheTelemetry（KV cache 命中率） |
| `manager.py` | 编排器，显式串联四链 |
| `ports.py` | MemoryPortWithRecall 扩展协议 |
| `__init__.py` | 公共 API 汇总 |

关键设计：四个内部协议（`TokenCounter` / `BlobPort` / `Offloader` / `Reinjector`）和观测 DTO **都不进 PORT_PROTOCOLS**——Port 数量不变，它们只是包内组件。

---

## 3. 数据模型与全部常量（types.py）

### ContextSection（一个待组装的上下文段）

五个字段：

- `name`：段名。`"system"` 是保留名（常量 `SYSTEM_SECTION_NAME`）。
- `content`：正文。
- `quota`：该段的 token 配额上限。
- `priority`：优先级，**数字越小越先排**，system 段排最前。
- `trim_policy`：超配额时的裁剪策略。

### TrimPolicy 四种取值

- `PIN`：钉住不裁。
- `SUMMARY`：摘要后保留。
- `HEAD`：保留头部（`content[:quota]`）。
- `TAIL`：保留尾部（`content[-quota:]`）——默认值，保留最近信息，抗 context rot。

注意：代码里 `HEAD` 和 `PIN` 在 `_trim` 里走同一分支（都取头部），`TAIL`/`SUMMARY` 走尾部保留。

### 压缩阈值常量（背下来）

- `TRIGGER_RATIO = 0.7`：token 占比达到 70% 触发压缩。
- `DEFAULT_TRIGGER_TURNS = 12`：轮次达到 12 触发压缩。
- `DEFAULT_KEEP_RECENT = 4`：保留最近 4 条消息。
- `DEFAULT_PIN_PATTERNS = ("system", "待办", "未完成")`：正文含这些特征即钉住。

---

## 4. 统一 token 口径（token_counter.py）

历史上这里有两套口径并存：`len(str)` 和 `len/4`，导致组装和压缩对同一段文本算出不同 token 数。现在统一了：

- `estimate_tokens(text)` 直接返回 `len(text)`（Unicode 码点数，中文不再按字节折算）。
- `DefaultTokenCounter.count(text)` 委托给 `estimate_tokens`。
- `TokenCounter` 协议可注入真实 tokenizer 替换。

**为什么重要**：assembler 和 compaction 构造时若未注入 counter，各自 `new` 一个 `DefaultTokenCounter()`，口径一致；`ContextMemoryManager` 注入时会把同一个 counter 传给三个消费者（assembler / compaction / reinjector），这是「统一口径下传」纪律。

---

## 5. 组装链（assembler.py）

`DefaultContextAssembler.assemble(sections, budget) -> list[Message]`。

### 5.1 常量

`_SYSTEM_BUDGET_RATIO = 0.5`：system 段最多占总预算的一半，防止超长 system 单段突破总预算。

### 5.2 完整流程

1. `cap = int(budget)`。注意 `TokenBudget.__int__` 的语义：`limit=None` 返回 **-1**（表示不限），非负整数是硬上限，0 表示无预算。
2. 按 `priority` 升序排序所有段。
3. `_dedup_system`：system 段只保留**先到者**，防止覆盖注入。
4. 逐段处理：
   - 先 `_trim(sec)`：正文长度 ≤ quota 原样返回；否则按 trim_policy 取头或取尾。
   - **system 段特殊处理**：`_cap_system(piece, cap)` 二次限长（超长 system 截断到 `cap * 0.5`，至少 1 字符），直接追加消息，累加计数，**不走通用贪心分支**。
   - 其他段走贪心装入：先算 `cost = counter.count(piece)`；若 `cap >= 0` 且 `used + cost > cap`，把 piece 截到剩余额度 `cap - used`（**降级裁剪而非整段丢弃**）；若裁完为空则跳过；否则追加并累加计数。
5. 返回消息列表，每段一条 Message（role 用段名，content 用裁剪后正文）。

### 5.3 一个容易忽略的细节

`piece = piece[:remain]` 用的是**字符切片**，而 `used += counter.count(piece)` 用的是 token 计数。默认 counter 是 `len(text)`，两者一致；但注入真实 tokenizer 后可能不一致（截了 N 个字符，token 数未必等于 N）。这是当前实现的隐含假设。

---

## 6. 压缩链（compaction.py）

`DefaultCompactionPolicy` 产**新消息序列**，绝不修改 `state.messages`（内核「只增不减」契约）。

### 6.1 三个具名常量

- `SUMMARY_ROLE = "summary"`：摘要消息的独立角色。
- `SUMMARY_PREFIX = "[摘要] "`。
- `_SUMMARY_ROLE_WINDOW = 5`：摘要里展示的角色序列长度（人类可读提示用）。

**为什么摘要要用独立 role**：如果摘要用 `role="system"`，下一轮压缩时会按 role 被永久钉住，越滚越大。独立 role 避免沉淀。

### 6.2 是否压缩：`should_compact(state)`

两个维度，任一超阈值即 True：

- token 维度：`max_tokens > 0` 且 `used / max_tokens >= 0.7`。其中 `used = max(state.budgets.used_tokens, 消息投影估算值)`——兼容「内核记录的已用」和「消息估算」两种口径，取较大者更保守。
- 轮次维度：`state.budgets.used_turns >= 12`。

`max_tokens <= 0` 时 token 维度不触发，避免除零。

### 6.3 怎么压缩：`compact(messages)`

1. 空消息 → 返回空 tuple。
2. `recent_start = max(0, len(messages) - keep_recent)`，最近 4 条的起始下标。
3. 遍历所有消息，满足任一条件就钉住（保留原位）：role 是 `"system"` / 下标 ≥ recent_start / `_is_pinned` 命中（正文含 `"system"`、`"待办"`、`"未完成"` 任一特征）。
4. 没被钉住的记作 `dropped_idx`。若为空（无可压缩）→ 返回新 tuple 的原样拷贝。
5. `_make_summary(dropped)` 生成一条摘要消息：内容形如 `"[摘要] 7 条历史 (user,assistant,user,...)"`，只列前 5 个角色。
6. 在第一个 dropped 的位置插入摘要，其余保留的消息按原顺序输出。

**结果**：原来 10 条消息，钉住 5 条，丢 5 条，输出变成 5 条保留 + 1 条摘要 = 6 条，且摘要在原来第一条被丢消息的位置。

---

## 7. 重注入链（reinjector.py）

`DefaultReinjector.reinject(view, summary, tasks, budget)` 把三种东西塞回压缩视图：语义摘要、钉住片段、未完成任务。

### 7.1 常量

- `PINNED_ROLE = "pinned"`：未完成任务回注时的 role。
- `_REINJECT_BUDGET_RATIO = 0.3`：回注段最多占总预算的 30%，防止挤占主任务。
- `SUMMARY_ROLE` 从 compaction.py **重导出**（单一定义源，避免两处字面量漂移）。

### 7.2 完整流程

1. `cap = int(budget)`（-1 表示不限）。
2. `extra_budget = cap`（不限时）或 `cap * 0.3`（有限时）。
3. 组装候选列表：摘要（若有）→ 每个未完成任务都变成一条 `role="pinned"` 的 Message。
4. 逐个候选：算 cost；若 `cap >= 0` 且 `used + cost > extra_budget` → **跳过**（超预算不透支），否则追加并累加。
5. 返回新 tuple：原视图 + 预算内装得下的回注项。

注意候选是有顺序的：摘要优先，任务次之；超预算时先砍任务。

---

## 8. 卸载链（offloader.py + offload_policy.py）

### 8.1 设计决策：落盘，不截断

大工具结果超阈值时，交给 BlobPort 落盘，返回 `store://<id>` 句柄 + 摘要占位，模型按需回读。**明确否决截断**——尾部往往是最新数据/错误堆栈，恰恰最该看到的部分。`OffloadPolicy(mode="truncate")` 构造即抛 `ValueError`，不留兼容层。

### 8.2 句柄

- `BLOB_HANDLE_SCHEME = "store://"`。
- `make_handle(blob_id)` → `"store://" + blob_id`。
- `parse_handle(handle)` → 解析 blob id，scheme 不符或 id 为空返回 None。

### 8.3 三个协议/实现

- `BlobPort`：三个 async 方法 `put(value)->句柄`、`get(handle)->值或None`、`delete(handle)`（不存在幂等）。
- `InMemoryBlobStore`：进程内 dict，句柄用 `uuid4().hex`，按句柄 get/delete。
- `Offloader`：一个 async 方法 `offload(value, store)->句柄`。`DefaultOffloader` 直接 `await store.put(value)`。

### 8.4 OffloadPolicy

- `DEFAULT_OFFLOAD_THRESHOLD = 8192`（8 KiB 字符数，按 Unicode 码点口径）。
- 构造校验：`threshold <= 0` 抛错；`mode != "offload"` 抛错（拒绝截断）。
- `should_offload(value)`：`len(str(value)) > threshold`。
- `apply(value, store)`：超阈值 → `await store.put(value)` 回句柄；否则原值返回。

---

## 9. KV cache 观测（observability.py）

`CacheTelemetry` 是不可变 DTO（`frozen=True, slots=True`），把 KV cache 命中率做成**一等观测指标**而不是零散日志字符串。

字段：`hits`（命中数）、`misses`（未命中数）、`label`（维度标签，默认 `"default"`，可按模型/前缀归因）。

方法：

- `__post_init__`：计数不得为负（fail-closed）。
- `total` = hits + misses。
- `hit_rate`：无访问时返回 **0.0**（不是 NaN/None，trace 序列化不炸），否则 hits/total。
- `record_hit()` / `record_miss()` / `with_label(label)`：都返回**新实例**（纯函数，可重放）。

`ContextMemoryManager` 持有一个 `cache_telemetry`，对外暴露 `record_cache_hit(label)` / `record_cache_miss(label)` 两个方法更新它。

---

## 10. 编排器（manager.py）：四链的显式串联

这是整个模块的调度中心。记住一句话：**7 个依赖显式注入，顺序与预算由代码结构保证**。

### 10.1 构造：七个依赖 + 统一口径下传

构造参数：`assembler` / `compaction` / `offloader` / `memory` / `blob` / `reinjector` / `token_counter`，全部可缺省。

缺省链的关键点：`token_counter` 先确定（缺省 `DefaultTokenCounter`），然后传给 assembler、compaction、reinjector 三个消费者——保证「同一个口径贯穿三链」。memory 缺省 `MemoryStore()`，blob 缺省 `InMemoryBlobStore()`。

还初始化：`trace` 列表（只记两个顺序事件）、`cache_telemetry`。

### 10.2 六个对外方法

**`build()`**：返回 `layer_program("memory", payload={"layer": "context"})()`——一个「用上下文记忆」的外壳 Program。注意 port 名叫 `"memory"`（不是 `"context"`），内部四链仍是组件调用，不是算子。

**`assemble(sections, budget)`**：直接委托 `assembler.assemble`。

**`commit_and_compact(state, transcript, ctx, budget, tasks)`**：最核心的方法，固定三步：

1. `memory.write_cold(transcript, port_ctx)`——**先把全文落 cold 层**（细节必须先落盘才能提炼），trace 记 `"write_cold"`。
2. `compaction.compact(state.messages)` 产压缩视图——不改 State.messages，trace 记 `"compact"`。
3. 从视图里找 `role == "summary"` 的摘要消息，算有效预算 `cap`（传了 budget 用它的 limit；**没传或显式 None 都强制回退到 `state.budgets.max_tokens`**，禁止无限绕过裁剪），包成 `TokenBudget(limit=cap)`，调 `reinjector.reinject`。

返回压缩并重注入后的消息 tuple。

**`recall(query, ctx)`**：校验 query 必须是 `MemoryQuery`（否则 TypeError），委托 `memory.recall`。

**`offload(value, ctx)`**：委托 `offloader.offload(value, blob)`，返回 `store://` 句柄。

**`with_(...)`**：不可变替换依赖，返回新实例，原实例不变。两个细节：

- 换 `token_counter` 时，如果 assembler/compaction/reinjector 仍是默认实现且调用方没显式替换它们，会同步重建这三个消费者为带新 counter 的默认实现——避免「换了 counter 但消费者还在用旧的」。
- 新实例保留 `trace` 的历史拷贝和同一个 `cache_telemetry`（不可变 DTO 直接共享）。

---

## 11. Port 扩展协议（ports.py）

`MemoryPortWithRecall(MemoryPort, Protocol)` 是 R7 扩展协议：在基础 `MemoryPort`（read/write/forget）之上加一个 async `recall(query, ctx) -> list[MemoryItem]`。

**设计理由**：基础 Port 只声明通用记忆能力；语义召回是 R7 领域语义，不该反向污染 R2 的 compose 层。DTO（`MemoryQuery`/`MemoryItem`）也由 R7/memory 自有类型承载。

---

## 12. 端到端：一次 commit_and_compact

假设 State.messages 有 10 条、其中 2 条含「待办」，本轮 transcript 是一段长对话，budget.limit=1000：

1. `write_cold`：把完整 transcript 作为 `EPISODIC` 项写进 cold 层（07 册讲细节），trace += `write_cold`。
2. 判断压缩阈值：假设 used_tokens/max_tokens = 0.75 ≥ 0.7 → 该压缩。
3. `compact`：保留 system（1 条）+ 最近 4 条 + 含「待办」的 2 条 = 7 条钉住，丢 3 条，生成一条 `role="summary"` 的摘要插到第一条被丢消息的位置，输出 8 条。trace += `compact`。
4. 找摘要（找到），算 cap=1000，`TokenBudget(limit=1000)`。
5. `reinject`：extra_budget = 300。摘要 cost=30 装下；tasks 里若有两个未完成任务，cost 各 50/100，都装下（总 180 < 300），追加为 `role="pinned"` 消息。
6. 返回 8 + 3 = 11 条消息的新 tuple。State.messages 原封不动。

---

## 13. 细节速记卡

- **四链靠代码顺序编排**，不是 hook——`commit_and_compact` 方法体里写死「先写 cold，再压缩，再重注入」。
- **system 段三种特殊待遇**：priority 最小排最前、同优先级只留先到者、配额上限是总预算的 50%。
- **裁剪是降级不是丢弃**：非 system 段超预算时截到剩余额度，只有裁完为空才跳过。
- **TAIL 是默认裁剪策略**：保留尾部，抗 context rot。
- **摘要用独立 role**：避免被下一轮按 `role=system` 永久钉住。
- **钉住三条件**：system 角色 / 最近 N 条 / 正文命中 pin_patterns。
- **压缩取两个 used 口径的较大值**：内核 `used_tokens` 与消息估算，取大更保守。
- **budget=None 也会回退有限值**：`commit_and_compact` 里显式 None 强制用 max_tokens，禁止无限绕过。
- **重注入上限 30%**：摘要优先，任务次之，超预算先砍任务。
- **截断被明确否决**：`OffloadPolicy(mode="truncate")` 构造即抛错，没有兼容开关。
- **句柄 scheme 是 `store://`**，`parse_handle` 对非法前缀返回 None。
- **hit_rate 无访问返回 0.0**，绝不 NaN；record_* 返回新实例。
- **with_ 换 counter 会重建三个默认消费者**：这是「统一口径下传」的落地细节。
- **四个内部协议不进 PORT_PROTOCOLS**：Port 数量不变。
- **token 口径是 Unicode 码点数**：中文不再按字节折算。

---

*下一册：[02 tools 工具模块](./02-tools工具模块.md)。想深入记忆本体读 [07 MemoryStore](./07-MemoryStore精读.md)。*
