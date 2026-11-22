# R7 记忆与上下文设计/实现审查问题清单（场景9 · 只读 · 2026-11-20）

> 审查对象：`docs/02-模块详设/Agent/R7-上下文与记忆.md`（下称「详设」）、`docs/01-总览与设计/AGENT-总设计.md` R7/R1/R10 章节（下称「总设」）、`docs/01-总览与设计/能力域全景.md` F/G 域（下称「全景」）、`src/harness/context/` + `src/harness/memory/` 实现与 `tests/r7/`。
> 用户诉求：需要明确的「短期记忆 + 长期记忆（多分类）」及相应优化；上下文管理要有比较完整的优化链。
> 方法：两个独立子代理分别审文档/审源码，主控复核关键断点（调用点 grep、`PortCtx`/`VectorStore` 源码逐行）。全部只读，未改任何文档或代码。

---

## 结论摘要

现实现是「协议/测试齐备、语义空壳」：分层只是标签、召回是临时索引、提炼/去重/冲突/评分无实现、短期记忆无实体、上下文链多处空转、生产零接线。缺口是**数据模型级**的，不是补几个函数能解决。设计文档本身也不自洽：短期记忆三种归属打架、能力全景与详设互相矛盾、重注入只在图上。

---

## Blocker（不改则无法支撑用户诉求）

### B1 短期记忆没有权威定义，三种归属打架
- 全景 `G1:115`：短期记忆＝会话 scratch + checkpoint，L0 内核，映射 M1/M7。
- 总设 `R1.5:488-489`：messages 只增不减、scratch 为用户/插件键；`R10:1487`：checkpoint 是短期 thread 内恢复，memory 是长期跨 thread 知识。
- 详设 `§2:20`、`§5:98`：hot＝「会话内」，却放在「长期记忆 hot/warm/cold 三层」里。
- 源码：`PortCtx`（`hooks/dto.py:147-166`）只有 session_id/run_id；`State.messages/scratch`（`runtime/types.py`）是唯一会话态；`src/harness/` 无 SessionBuffer/短期记忆实体。
- 后果：用户问「短期记忆」时，文档给出三个互相冲突的答案；R7 的 hot 层既像短期记忆又属长期记忆模块，checkpoint 与 memory 边界被模糊。
- 方向：确立唯一口径——**短期记忆 = State.messages + scratch + last_*（R1 内核，checkpoint 可恢复）；R7 对其只读投影产短期视图，不另造竞争者**；R7 hot 层改称「长期记忆的近期热层」，与会话内短期记忆明确切开。

### B2 长期记忆分层是标签，不是生命周期
- 源码 `memory.py:61-64`：`_kv`、`_items`、`_tiers`、`_item_times` 四个扁平 dict；`promote/demote`（`:148-179`）只改 `_tiers` 里的字符串标签，不迁移存储、不改 TTL、不改检索路径；`forget_expired`（`:156-166`）对所有层用同一个 ttl。
- 详设 `§11.7.3:325` 承诺「三 dict（hot/warm/cold）」，与实现不符；`§5:98` 写的 hot=Redis / warm=PG+pgvector / cold=对象存储是目标形态，实现全为内存 dict。
- 方向：分层必须是**可观测的物理/检索差异**：独立存储桶 + 各自 TTL/容量上限 + 晋升/降级触发条件（访问频次、importance、时效）+ 检索优先级；文档同步标注「目标后端 vs 当前内存默认」。

### B3 `write_cold` 时间戳与重要性反常，TTL 定时炸弹
- `memory.py:116-125`：`ts=0`、`importance=1.0` 硬编码；`put_item`（`:127-135`）无 now 时用 `item.ts` 作写入时刻；`forget_expired`（`:156-166`）判 `now - 0 > ttl` → 正常时间戳下所有 cold 全文归档会被一次遗忘清空。测试 `tests/r7/test_memory.py:92` 还把这个反常当默认前提。
- 语义反向：全文归档 importance 最高（1.0），而 importance 全程只存不算、不参与召回排序。
- 方向：写入时刻用真实时钟/显式 now；importance 由评分器计算并参与召回排序；cold 归档设长 TTL 或永久，与 hot 短 TTL 分开。

### B4 KV 记忆与 item 召回是两座孤岛
- `memory.py:61` `_kv` 由 `read/write/forget`（`:67-81`）操作；`:63` `_items` 由 `put_item/recall`（`:83-166`）操作，二者无互写；`tests/demos/demo_r7.py:70-71` write 后只读回、永不进 recall。
- 方向：统一数据模型（稳定 item_id + 一份实体），`read/write` 与 `recall` 走同一存储与索引。

### B5 写入提炼/去重/冲突消解/重要性评分全部缺失
- 详设 `§3:39`、`FR-9:217`、总设 `R7.2:1188` 都声称有；源码 `src/harness/memory/` 零 dedup/conflict/score 实现（全仓仅 `assembler.py:65` 的 system 段去重与 R6 的 dedup）。
- 方向：补可测的提炼流水线：规范化 → secret/PII 脱敏 → 注入检测 → 去重（hash + 相似阈值）→ 冲突消解（来源/置信度/时序裁决）→ importance 评分 → 分层落库 → 向量索引增量 upsert。**这是「长期记忆有分类且有优化」的核心。**

### B6 召回实现会跨轮污染，且降级承诺虚假
- `memory.py:95` 每轮对全量 items 重新 embed；`:98,103,108` 用 `enumerate` 临时索引做 `chunk_id="mem:i"`；R6 `InMemoryVectorStore.upsert`（`rag/store.py:62-69`）是追加语义、按 id 覆盖未实现 → 多轮后同名 chunk 堆积、item 增删后旧向量映射到新 item、重复 chunk 挤占 top_k。
- `recall` docstring（`:86`）承诺「无 embedder 时按 importance 降级」，但构造器 `:58-60` 永远注入默认实现，无此分支；详设 `§11.8.3:326` 同样落空。
- 方向：稳定 chunk_id（item_id + embedding 版本）、增量 upsert（新库语义或记忆库自带替换）、独立作用域的向量库；降级分支真实实现并加测试。

### B7 上下文优化链多处空转，压缩视图不供 assemble
- `assembler.py:46,55,57,61,80` 用 `len(str)` 当 token 配额，与 `compaction.py:25-27` 的 `len/4` 两套口径；中文/多字节严重失真。
- `assembler.py:57-59` 超预算整段丢弃（高价值大段被丢、低价值小段保留）；`:52-56` system 段不受 cap 约束却计入 used，超长 system 可突破总预算。
- `compaction.py:118-123` 摘要只拼角色名、无语义；摘要 `role="system"`，下轮 `compact:99` 按 role 钉住 → 摘要永久累积不可再压。
- 详设 `§3:34`、`总设:1202` 的「重注入」只有流程图节点，无协议/实现/测试/门禁。
- 源码 grep：`commit_and_compact`/`ContextMemoryManager` 在生产 `src/` 零调用，仅 demo/测试显式调用；`manager.py:52-76` 的 `assemble` 与 `commit_and_compact` 无任何衔接，详设 `§8:129`/`§11.7.4:311`「压缩视图供 assemble 使用」未兑现；`with_`（`:110-116`）新实例不传 trace，观测丢历史。
- 方向：真实 token 计数（或统一估算口径）+ 预算贪心降级裁剪 + system 上限；语义摘要（摘要用独立 role 或 pinned flag，避免 role 误钉）；补 Reinjector 契约与用例；生产装配（R12）把 assemble/recall/offload/commit 四环真正接线；视图作为纯函数投影可重放。

### B8 安全/隔离维度薄，租户无落点
- `memory/types.py:86,98-99`：`is_secret_key` 仅 5 个英文子串；`memory.py:73-77` 注入检测仅对 `str` 值生效，而 `DynamicValue=Any`，dict/list 内嵌注入串或 secret 可绕过。
- `PortCtx`（`hooks/dto.py:147-166`）无 tenant/acl/principal；`MemoryQuery`（`memory/types.py:68-82`）无租户字段；namespace 仅键空间 → 跨租户记忆串味无防线。
- 方向：递归扫描值（含容器）+ 正则/模式化 secret 检测；MemoryItem/MemoryQuery 增 scope/tenant/acl 字段并在写入/召回双向校验；全景 K8 数据隔离补映射。

---

## Should（重要，不完整或口径缺失）

- **S1 分类粒度不足**：当前只有 episodic/semantic/procedural 三类，无 profile/preference/entity/journal（工作日志/决策）。全景 `G9:123`「跨会话历史检索/工作日志沉淀」标 `—`。用户要的「长期记忆好几种分类」需要显式扩展，且每类给提炼规则与召回场景。
- **S2 全景与详设互相矛盾**：全景 `F4:103` 大结果卸载＝`—`、`G3:117`/`G8:122` 分层与晋升降级＝`—`、`:299` 缺口汇总列为完全缺失；但 `:315` §2 又称「R7 覆盖 F、G 全域」，详设 `FR-5/FR-9` 声称已实现。需统一为「R7 已覆盖」并更新映射与缺口汇总。
- **S3 压缩视图与 checkpoint/回放一致性未定**：视图是否进 fingerprint、重放时如何重建同一视图未说明（总设 `R10:1568` 要求确定性）；需明确视图为纯函数投影（state → 视图）且不写回 messages。
- **S4 门禁/测试缺口**：详设 `§9:144` 要求 `tests/r7/test_ordering.py`，实际不存在（顺序断言塞在 `test_wiring.py:23-28`）；`demo_r7.py:75` 用 `PORT_PROTOCOLS.get('blob', object)` 做履约断言，`isinstance(x, object)` 恒真（'blob' 不在协议表）→ 假测试。
- **S5 上下文优化链缺「相关性筛选/MMR 去冗余」**：全景 `F8:107` 标「部分」，详设无相关性打分/去冗余落点；R6 已有 H11 MMR/H10 rerank 可复用，需显式接入。
- **S6 预算「预留输出」无量化口径**：详设 `§3:31`、总设 `R7.3:1198` 只有文字，无预留比例/固定值/触发降级规则。
- **S7 短期记忆与 checkpoint 的消费关系未写**：总设 `R10:1487` 只说两者分离，未说 R7 压缩视图在恢复后如何重建。

## Nit

- N1 `DEFAULT_PIN_PATTERNS` 含 `"system"`，但 `compact:99` 已按 role 钉住，pattern 冗余且正文子串易误钉。
- N2 `write_cold(ctx=None)`/`commit_and_compact(ctx=None)` 静默造空 PortCtx，丢审计上下文。
- N3 `tier_of/_shift` 用对象身份 `is` 比对（`memory.py:139,170`），copy/反序列化后失效；应用稳定 item_id。

---

## 改进方向草案（供拍板，未落盘到设计文档）

### 记忆目标模型

| 层 | 归属 | 内容 | 生命周期 | 谁维护 |
|---|---|---|---|---|
| 短期记忆 | L0 内核 | `State.messages`（只增）+ `scratch` + `last_*`；可选会话级 buffer（工具大结果句柄） | 单 run/session，checkpoint 可恢复 | R1/R10；R7 只读投影 |
| 长期记忆 | L1 R7 | 跨会话事实/经历/偏好/流程/实体/日志 | 跨 session，分层 TTL | R7 MemoryPort |

**长期记忆双轴分类（关键）**：`kind`（内容类型，正交于温度）
- `episodic` 情节：发生了什么（会话/任务事件）
- `semantic` 事实：知道什么（可复用知识）
- `procedural` 流程：怎么做（成功路径/工具序列）
- 建议新增：`profile` 用户画像、`preference` 偏好与约束、`entity` 实体/项目知识、`journal` 工作日志与决策
- `tier`（温度/物理层）：hot 近期热层（短 TTL、高频召回）、warm 结构化层（主检索、向量索引）、cold 全文归档（长 TTL、低频）

**写入优化链**：规范化 → 脱敏/注入检测（递归）→ 去重（hash + 相似）→ 冲突消解（置信度/时序/来源）→ importance 评分（信息量×复用度×时效×来源权重）→ 分层落库 → 向量增量 upsert
**召回优化链**：scope/tenant 过滤 → kind/tier 过滤 → 向量+关键词混合召回 → rerank（相关性×importance×recency×置信度）→ MMR 去冗余 → 预算裁剪 → 注入检测 → 注入上下文段

### 上下文目标优化链

1. 分段注册与优先级：system / task / tool catalog / retrieval / memory / history / input
2. 预算分配：预留输出 token + 按优先级/价值贪心装入；system 硬保留但设上限；超预算段**降级裁剪**而非整段丢弃
3. 相关性筛选：检索/记忆按 rerank 分数取 top；历史按滑窗 + 重要性保留（不写回 messages）
4. 压缩：token/轮次/时效阈值触发 → 钉住 system + 未完成事项 + 最近 N 轮 + 高价值 → 语义摘要（独立 role 或 pinned flag）→ 纯函数投影可重放
5. 重注入：摘要 + 钉住片段 + 未完成任务显式回注下一轮 assemble（补 Reinjector 契约）
6. 大结果卸载：BlobPort 句柄 + 摘要，按需再读，句柄有效性校验
7. 渐进披露：工具/技能 L1 目录 / L2 schema / L3 正文（与 R5 对齐）
8. 可观测：每段 token 用量、裁剪/丢弃原因、压缩前后 token、召回命中率进 trace

### 设计文档改动清单（待拍板后执行）

| 文档 | 章节 | 改动 |
|---|---|---|
| 总设 | R1.5 / R10.1 | 确立短期记忆唯一口径、与 checkpoint 边界 |
| 总设 | R7.1~R7.7 | hot 层定性、双轴分类、重注入落点、租户/脱敏、优化链、生产接线口径 |
| 详设 | §1/§2/§5/§11.5/§11.7.3/§9/§10 | 数据模型、kind 扩展、7 条优化链各给协议/DTO/用例/门禁 |
| 全景 | F4/F8、G1/G3/G7/G8/G9、§1 缺口汇总、§2 索引 | 统一映射口径，补新分类与优化项落点 |

---

## 待拍板（闸门）

1. 本轮范围：只改设计文档（R7 详设 + 总设 + 能力全景），还是文档 + 代码一起改？
2. 短期记忆归属：R1/R10 内核持有 + R7 只读投影（推荐）？还是 R7 新建 SessionBuffer 实体接管？
3. 长期记忆分类粒度：扩到 7 类（推荐）/ 3 类 + subtype / 保持 3 类只加强优化链？
4. 优化链覆盖范围（多选）：写入提炼去重冲突评分、召回 rerank/MMR/预算、压缩语义摘要 + 重注入、租户隔离 + 脱敏、R12 生产装配接线。

*本次为只读审查，未修改任何设计文档或生产代码。*
