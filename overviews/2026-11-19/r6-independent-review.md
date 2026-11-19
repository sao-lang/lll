# R6（RAG 检索增强）独立代码审查报告

- 审查人：独立代码审查子代理（无先前会话上下文）
- 日期：2026-11-19
- 基准：`docs/02-模块详设/Agent/R6-RAG检索增强.md`（v2.1）、`docs/01-总览与设计/AGENT-总设计.md` §R6/附录 B.1.1/B.1.2、`architecture-contracts.md`、`src/hooks/protocols.py:RAGPort`
- 方法：七维（正确性/错误处理/并发/资源/性能/安全/可维护性）+ 接线四问，逐条拿「文件:行号」举证
- 纪律：门禁全绿不作为免检依据；以下结论均由实际运行 + 源码举证得出，命中的关键缺陷已用只读实验复现

---

## 一、结论

| 级别 | 数量 | 是否阻塞合入 |
|------|------|-------------|
| **Blocker（阻塞）** | **3** | 是 |
| **Should（应修）** | **8** | 否（但强烈建议修） |
| **Nit（可留）** | **3** | 否 |

### 合入判定：**不予合入**

门禁（pytest/mypy/gate_check）虽 14/14 全绿，但存在 **3 个 Blocker**，其中：

1. **检索期 query 向量化未使用流水线注入的 Embedder**，而是召回器内部另起 `HashEmbedder()`——生产默认 Embedder 为 OpenAI-compat 时，入库向量与查询向量处于**不同语义空间**，检索结果实质无效。这是典型的「功能断点 L3」：模块内单测全绿、端到端语义崩塌。
2. **混合召回的关键词通道绕过 `filters`**，且 `PostProcessor.filter` 名为「权限/时效/注入」过滤却**只做注入检测**——带 `clearance=secret` 的块在 `filters=public` 下仍进入上下文，安全红线被突破。
3. **`RAGPipeline.detector` 是死参数**——构造期接收并保存，但 `query()` 从未传给 post；自定义检测器被静默忽略，注入检测实际只跑 post 自带的默认英文关键词检测器。

最严重的三条即上述 B1/B2/B3。

---

## 二、Blocker（阻塞合入）

### B1. 检索期查询向量化与入库向量化用了两个不同的 Embedder（功能断点 L3）

**举证**
- `src/harness/rag/pipeline.py:110`：入库用 `self.embedder.embed(...)`（装配层注入的 Embedder，设计默认 OpenAI-compat）
- `src/harness/rag/retriever.py:37-45`：`VectorRetriever.__init__` **只接收 store，无 embedder 参数**；`retrieve()` 内 `embedder = _trivial_embedder()`
- `src/harness/rag/retriever.py:73-75`：`HybridRetriever.retrieve()` 同样 `embedder = _trivial_embedder()`
- `src/harness/rag/retriever.py:121-125`：`_trivial_embedder()` 硬编码 `HashEmbedder()`（dim=16、词哈希）
- `src/harness/rag/pipeline.py:158`：query() 调 `self.retriever.retrieve(sub)`，全程不传 `self.embedder`

**复现实验（只读）**：自定义 `CountingEmbedder`，`ingest` 后调用数=1，`query` 后仍=1 → 查询向量化根本没有走流水线注入的 embedder。

**后果**：入库块用 A 模型（如 1536 维 OpenAI-compat）向量化，查询用 B 模型（16 维词哈希）向量化；`store._dot` 用 `zip()` 截断到短向量，点积比较的是两个无关空间的分量。检索排序退化为近似随机，`recall/MRR` 全面崩塌。测试之所以全绿，是因为 conftest 的 `FakeEmbedder` 与 `HashEmbedder` 用了同源 `hash(word)%dim`，且断言只检查「hits 非空 / context 非空」，**从未验证召回的是正确文档**（见 S2 假绿）。demo 也恰巧两边都用 `HashEmbedder(dim=16)` 才显得正常。

**具体改法**：把 Embedder 作为依赖显式注入召回器，使入库与查询共用同一向量空间。
- `VectorRetriever.__init__(self, store, embedder)`、`HybridRetriever.__init__(self, store, embedder, ...)`，`retrieve()` 改用 `self.embedder.embed([query.text])`，删除 `_trivial_embedder()`。
- `RAGPipeline.__init__` 构造/替换召回器时把 `self.embedder` 传入；或在 `query()` 里显式把 query 向量算好后透传给 retriever（改 Retriever 协议签名）。
- 增加守卫：召回前校验 query 向量维度与 store 中块维度一致，不一致即显式报错而非静默 `zip` 截断。

---

### B2. 混合召回关键词通道绕过 filters，且后处理未实现权限/时效过滤（安全红线）

**举证**
- `src/harness/rag/retriever.py:75`：向量通道 `store.search(vecs[0], query.top_k, query.filters)` —— **施加了 filters**
- `src/harness/rag/retriever.py:76-80`：关键词通道素材取自 `all_chunks()` 全量 corpus，**未带 filters**
- `src/harness/rag/retriever.py:84-97`：`_keyword_search()` 只做词面覆盖打分，无任何 `filters` 判断
- `src/harness/rag/retriever.py:99-118`：`_rrf()` 把两通道融合，被过滤掉的 secret 块被重新并回结果
- `src/harness/rag/post.py:29`：`PostProcessor.filter` 签名声明「权限/时效/注入」，docstring 亦如此承诺
- `src/harness/rag/post.py:58-69`：实际 `filter()` **只调 `detector.detect()`**，没有任何 meta/权限/时效过滤
- 设计 §3 流程图 `FL["权限/时效过滤"]`、设计 R6-红线「未过注入检测→拒绝」

**复现实验（只读）**：两个块 `clearance=public` / `clearance=secret`，`Query(filters={"clearance":"public"})` 走 `HybridRetriever`，返回 `[('pub','public'), ('sec','secret')]` —— secret 块穿透过滤。

**后果**：查询带权限/时效约束时，关键词通道可把本应被排除的文档重新引入上下文并注入 LLM，构成越权信息泄露；`PostProcessor.filter` 的「权限/时效」承诺完全落空。这是安全红线级别缺陷。

**具体改法**：
- `_keyword_search()` 接收 `filters` 并用与 `InMemoryVectorStore._matches`（`store.py:69-79`）**相同**的过滤逻辑先筛 corpus 再打分。
- `PostProcessor.filter()` 真正实现 meta/权限/时效过滤（接收 `Query.filters` 或 `filters` 参数），把它做成融合后的一道**兜底过滤**，不依赖召回器各自正确。
- 在流水线 `query()` 中把 `q.filters` 传给 `post.assemble`/`filter`，使权限过滤在拼接前统一生效。
- 补用例：`filters=public` 时 secret 块既不出现在 hits 也不出现在 context。

---

### B3. `RAGPipeline.detector` 是死参数，注入检测器替换点形同虚设（安全可替换断点）

**举证**
- `src/harness/rag/pipeline.py:62,84`：构造参数 `detector` 接收并保存为 `self.detector`
- `src/harness/rag/pipeline.py:163`：`result = await self.post.assemble(hits, ...)` —— **未传 detector**
- `src/harness/rag/post.py:54-56`：`DefaultPostProcessor.__init__` 用自己的 `detector or KeywordInjectionDetector()`
- `src/harness/rag/post.py:81`：`assemble()` 调 `self.filter(hits)`，`filter()` 用 `self._detector`
- `tests/r6/test_injection.py:22` 以为传 `detector=` 到 pipeline 即生效；实际靠 `DefaultPostProcessor()` 默认检测器恰好命中才通过（假绿）
- 设计 §6「注入检测通过 `detector=` 构造参数传入」、§9 R6-T06「复用 R5 检测器」

**复现实验（只读）**：`RAGPipeline(detector=marker)` 后 `pipeline.detector is marker == True`，但 `pipeline.post._detector is marker == False`。

**后果**：设计承诺的 8 注入点之一是「可替换注入检测器」，实际完全不可替换。默认检测器 `_DEFAULT_KEYWORDS`（`provider.py:57-66`）**全是英文串**，无法覆盖中文提示注入；若装配层按设计为 R6 注入中文/增强检测器，会被静默丢弃，安全能力远低于设计预期。虽然默认检测器仍在跑（非完全 fail-open），但「可替换的安全护栏」这一契约被破坏，属 Blocker。

**具体改法**：二选一——
1. 让 `self.detector` 真正生效：`DefaultPostProcessor.assemble(hits, budget, detector=None)` 接收 detector，`filter()` 用「传入 detector > post 自带 detector > 默认」优先级；`pipeline.query()` 传 `self.detector`。
2. 或明确 detector 只在 `post=` 注入点配置，删除 pipeline 的死参数 `detector`，并同步修订设计 §6/§9 与测试（不推荐，削弱契约）。

---

## 三、Should（应修）

### S1. `with_` 不可变替换丢失全部 4 个可选注入点

**举证**：`pipeline.py:168-182`。`current` 只填了 5 个必填段（splitter/embedder/store/retriever/post），随后 `type(self)(**current)`；`rewrite/rerank/cache/detector` 均未从 `self` 复制。

**复现实验**：`p.with_(post=...)` 后 `q.rewrite/rerank/detector` 全为 None、`q._cache_on=False`。

**后果**：设计 §4「不可变替换」、§9 R6-T01「换任一注入点上层零改」不成立。换一个段会**静默丢掉**重排/改写/缓存/检测器，既改变行为又可能削弱安全（detector 丢失）。未知参数处理（`pipeline.py:174-176`）倒是正确抛 `ValueError`，这点 OK。

**改法**：`current` 初始化时纳入全部 8 个注入点的当前值（含 `rewrite/rerank/cache` 与 `detector`，cache 需保留 `_cache_on` 及缓存内容或显式约定重建），再 `current.update(replacements)`。补断言：替换 splitter 后其余 7 点身份不变。

### S2. 混合检索的高召回测试是「假绿」，G3/验收标准 #2 未被真正验证

**举证**：`tests/r6/conftest.py:103-131` 的 `InMemoryVectorStore` **没有 `all_chunks()` 方法**；`retriever.py:76-80` 鸭子探测失败 → `corpus=[]` → 关键词通道为空 → 退化为纯向量。`tests/r6/test_hybrid.py:36-46` 断言的 d1 靠向量点积也能命中，`assert len(vhits)==len(hhits)` 反而印证退化；`test_components.py:98-113` 注释「鸭子取 store.all_chunks()」但同样取不到。

**后果**：设计验收 #2「混合检索召回率高于纯向量基线」、R6-G3、R6-T03 从未被真正测试；`HybridRetriever` 面对不带 `all_chunks` 的自定义 store 会静默降级为纯向量（设计未说明该退化是被接受的）。

**改法**：给 conftest fake store 加 `all_chunks()`；构造「向量召回排不进前 k、但关键词能命中」的区分性语料，断言混合命中包含纯向量未命中的 doc；补一例验证「自定义 store 无 all_chunks 时的退化行为」是显式预期的（记录日志或抛错），不要静默。

### S3. 预算派生与耗尽边界漏洞

**举证**：`pipeline.py:193-205` 从 `ctx.budget_left` 取属性 `("tokens","token_budget","total_tokens")`；而 R1 `Budgets`（`src/runtime/types.py:204-222`）字段是 `max_tokens`/`used_tokens`，无名 `tokens` 者；`TokenBudget.limit<=0 视为不限`（`types.py:120,123`、`post.py:92`）。

**复现实验**：`PortCtx(budget_left=Budgets(max_tokens=100,used_tokens=100))` → 派生 `None`；`budget_left=0`（耗尽）→ 派生 `0` → `TokenBudget(limit=0)` → **不限**。

**后果**：接入真实 R1 Budgets 投影时预算读取恒失败→不限；更严重的是「预算耗尽=0」被解释为不限，形成超预算漏洞。`tokens_used` 用的是 `chunk.tokens=len(piece)`（字符估算，设计接受），但拼接时追加的引用串 `[doc:x]` 与换行未计入（`post.py:94-108`），统计偏低。

**改法**：`_budget_from_ctx` 按 R1 语义算 `max_tokens-used_tokens`（兼容 int 与具名投影）；明确区分「None=不限」与「0=无预算可分配→空结果/拒绝」，不要把 0 当不限；把引用串长度计入 tokens_used 或改名为「估算 token」。

### S4. 缓存键忽略 filters/purpose，且 ingest 后缓存不失效

**举证**：`pipeline.py:189-191` `_cache_key` 仅 `text|top_k|token_budget`；`pipeline.py:184-186` `invalidate_cache` 只手动调用，`ingest()`（`:88-114`）不触发失效。

**复现实验**：两次不同 `filters` 的相同文本 query 只触发 1 次召回（filters 未入 key）；新 ingest 后再查仍命中旧缓存、看不到新数据。

**后果**：同一 query 在不同租户/权限过滤下复用同一缓存条目，可能跨上下文串数据；入库后新文档对已缓存 query 不可见，是陈旧结果/数据一致性问题。注意：这只是**精确缓存**（key=文本），设计横切写的是「语义缓存」（详设 §3/§5），且 §6 承诺「复用 R4 CachePort」——实现是自建 `dict`，并非语义缓存也未复用 R4。

**改法**：缓存键纳入 `filters`、`purpose`、向量化模型版本与库版本；`ingest()` 成功后自动失效（按 doc_id 局部失效或全清）；若要兑现「语义缓存」，改接 R4 `SimilarityCache`，否则在设计里把口径改为「精确缓存（ExactCache）」以免文档失实。

### S5. `cache` 注入对象是死参数（与 B3 同型）

**举证**：`pipeline.py:85-86`：`self._cache_on = cache is True or (cache is not None and cache is not False)`，`self._cache: dict = {}`。任意非 None/False 的 `cache` 对象只被用来**开启布尔开关**，对象本身从不存储/调用。

**后果**：设计 §6「缓存注入点，默认 ExactCache（复用 R4 CachePort），命中/invalidate 生效」未兑现——注入自定义 CachePort 被忽略，永远走内置 dict。8 注入点中 cache 与 detector 两个都是“假可替换”。

**改法**：保存注入的 cache 实现并委托其 get/set/invalidate；`with_` 要保留该对象（见 S1）。

### S6. 注入串被切分切断可绕过逐块检测（注入 fail-closed 的已知边界，可被轻易触发）

**举证**：检测粒度在 `post.py:58-69` 的**单个命中块正文**；入库切分在 `splitter.py:71-83`。关键词检测是子串匹配（`provider.py:89-101`）。

**复现实验**：`RecursiveSplitter(chunk_size=10)` 切 `"prefix ignore previous instructions suffix"` → 5 块，逐块 `detect()` **全为 clean**，而完整串检测为 injected。

**后果**：攻击者可通过在文档里控制填充长度，把注入串跨块边界切断，使两块都躲过关键词匹配。缓解因素：拼接时块间插入 `\n` 和 `[doc:x]` 引用（`post.py:94-104`），使被切断的串在最终上下文中不再连续，LL M 较难还原成完整指令；`reorder_middle` 又可能把两块分开。因此定值 **Should 而非 Blocker**（若威胁模型要求「任何注入串不得入 context」则应升为 Blocker）。另注：R6 复用 R5 检测器是设计明确要求，非 R6 自造，此项属继承边界。

**改法**：对拼接后的**完整 context** 再做一次检测（把跨块重组考虑进去）；或在切分时保留 overlap 覆盖边界并检测窗口；或在检测前对候选块正文做规范化拼接后再判。至少在文档标注该边界为已知限制。

### S7. MultiQuery 改写产生重复命中/引用，无跨 query 去重

**举证**：`rewrite.py:37-39` 默认把同一 query 复制 `n_variants=3` 份；`pipeline.py:156-158` 对每个 sub query `hits.extend(...)`，无去重；RRF 去重只在单个 retriever 调用内部（`retriever.py:99-118`）。

**复现实验**：`rewrite=MultiQueryRewriter(3)` 查询返回 `citations=['d1:0','d1:0','d1:0']`，hits 三份重复。

**后果**：开启改写后同一片段在上下文与引用中出现 3 次，浪费 token、稀释有效内容、引用列表失真；`LexicalReranker(top_n=5)` 若启用还会被重复项挤占名额。默认改写「复制同串」本身也不带来召回增益（设计 §3/§5 承诺 multi-query/HyDE/step-back 语义改写）。

**改法**：对多 query 合并后的 hits 按 `chunk_id` 做去重（保留最高分）；默认改写实现替换为真正产生语义变体的策略，或明确标注「默认复制仅为离线占位、不提升召回」。

### S8. 设计 20 项能力中多项「设计承诺、代码零实现」（区分可选与缺失）

详见第五节核对表。属于设计明确列为能力/关键参数但代码完全无对应实现的有：**MRR 去冗余**（详设 §3/§5/§7.6）、**语义缓存**（实为精确缓存，见 S4）、**增量更新**（`store.py:41-43` 的 upsert 只是 `extend`，无按 chunk_id/doc_id 去重或覆盖）、**自检索**、**RAG 评测 Recall/MRR/NDCG**（R6 内无任何评测代码，EvalPort 归 R10，口径需澄清）、**RRF weights 可配**（硬编码 `_VECTOR_WEIGHT/_KEYWORD_WEIGHT`，`retriever.py:16-18,102-105`）、**reorder_middle 可配**（`post.py:86,112-123` 恒开启）、**引用 verify 校验 ID 存在性**（无此步骤，citations 直接由 hits 生成）。

**后果**：设计 v2.1 §5「20 项能力已落地」与实现不符，验收/追责时无法据文档核对。部分确属设计允许「可开关/可替换」或跨模块归属（如评测归 R10），应如实标注归属，而非笼统宣称已实现。

**改法**：逐项在代码或文档标注「已实现/设计可选未启用/归其他模块」；对承诺为红线或默认开启的能力（MRR、增量去重）补实现，或在 v2.1 里下调为可选并说明。

---

## 四、Nit（可留，≤3）

### N1. `HashEmbedder` 的「确定性」仅在同一进程成立

`embedder.py:80` 用内建 `hash(word)`，CPython 对 str 哈希加随机盐（PYTHONHASHSEED），`conftest.py:68` 同源。`embedder.py:43` docstring 称「确定性，便于离线评测」。跨进程/重启后同一文本向量不同；当前 InMemory 存储无持久化故未暴雷。建议改静态哈希（如 hashlib）。

### N2. `tokens_used` 统计偏低

`post.py:92-108`：`tokens_used` 只累加 `chunk.tokens`（=字数），未计拼接追加的引用串 `[doc:x]`（`:94`）与换行符（`:104`）。属估算口径，设计接受字数估算，但需明确该字段是「估算」而非真实 tokenizer。

### N3. `RAGPort.retrieve()` 溯源信息偏薄 / `filter` 签名与设计漂移

`pipeline.py:131-138` 返回的 DynamicValue 仅含 `citation`（即 `chunk_id`=`doc_id:序号`）/`text`/`score`，可回溯到 doc:chunk，但缺独立 `doc_id`、标题/页码等结构化溯源字段（存在于 `Chunk.meta` 却未投影）。另 `post.py:29` 的 `filter` 是 `async def`，设计 §4 API 为 `def`；不影响运行（mypy 通过），记 Nit。

---

## 五、接线四问核对结果（architecture-contracts.md）

| 问 | 结论 | 证据 |
|----|------|------|
| **1. 谁调用它？** | **生产侧零调用者（跨模块断点，登记不虚标）**。`RAGPipeline` 在 `src/` 中除自身包外无任何真实调用/装配；仅出现在 tests/demo/gate_check 与设计协议定义。设计 §4 约定由 R12 装配层 `registry.register("rag", ...)` 注入，而 R12 尚未实现。按契约 §4.3，这是跨模块归属缺口，不列为 R6 单模块 Blocker，但**当前端到端系统不可用**。 | `grep -rn RAGPipeline src/` 排除 rag 包后无结果；`protocols.py:132,330` 仅定义；`AGENT-总设计.md:1017` 注明供 R12 |
| **2. 它调用谁？** | 五段依赖均已构造注入；但 **Embedder 在检索链未被真正调用**（B1）、**detector/cache 注入点未接到下游**（B3/S5）。关键词通道鸭子取 `store.all_chunks()`，自定义 store 无该方法即静默退化（S2）。 | `pipeline.py:77-86,158,163`；`retriever.py:43,73,78-79` |
| **3. 信号谁消费？** | R6 无 Signal/Event emit；不适用（port 直接返回 DTO，无订阅模型）。 | `src/harness/rag/` 无 emit/hook 调用 |
| **4. 数据流闭环？** | **部分断裂**：ingest 产物（chunk.vector）与 query 向量空间不一致（B1）；`filters` 未贯穿关键词通道与后处理（B2）；`retrieve()` 投影丢弃结构化溯源（N3）；缓存未按 ingest 失效（S4）。`ctx.budget_left` 派生对真实 R1 Budgets 失效（S3）。 | 见 B1/B2/S3/S4 各行号 |

**综合**：模块内闭环（CL-1~CL-5）在模块视角成立，`isinstance(p, PORT_PROTOCOLS["rag"])` 也真过（`tests/r6/test_pipeline.py:46-60` 实测通过）；但「注入依赖真正生效」「数据流全链一致」两项有实质断裂，正是架构契约专治的「模块自洽可测但语义断点」。

---

## 六、设计 20 项能力覆盖核对表

口径：入库 5 + 检索 9 + 横切 6 = 20（详设 §1/§5；总设计 §R6.5）。

| # | 能力 | 类别 | 状态 | 证据 / 说明 |
|---|------|------|------|-------------|
| 1 | 摄取（多格式解析/去重） | 入库 | **部分** | `pipeline.py:105-113` 仅 Document 投影+切分；无多格式解析；`upsert` 无去重（S8） |
| 2 | 切分（递归/语义/父子块） | 入库 | 部分 | `splitter.py:31-83` 仅递归+overlap；无语义/父子块（parent_child 参数缺失） |
| 3 | 向量化 | 入库 | 已实现(入库侧) | `embedder.py`；但检索侧未复用（B1） |
| 4 | 存储（向量/全文/元数据索引） | 入库 | 部分 | `store.py` 向量点积+meta 精确匹配；无真正全文/倒排索引 |
| 5 | 索引 | 入库 | 未实现/简化 | 无独立索引结构；关键词通道是扫描 corpus（`retriever.py:84-97`） |
| 6 | 查询改写（多查询/HyDE/step-back） | 检索 | 部分 | `rewrite.py` 仅复制同串，非语义改写（S7） |
| 7 | 查询路由（是否检索/检哪个库） | 检索 | **未实现** | 无路由协议/实现，代码零对应 |
| 8 | 多路召回（向量+BM25+关键词） | 检索 | 部分 | `retriever.py` 向量+词面覆盖（非 BM25）；关键词通道可被绕过（B2/S2） |
| 9 | RRF 融合 | 检索 | 已实现 | `retriever.py:99-118`；weights 不可配（S8） |
| 10 | 重排 cross-encoder | 检索 | 部分 | `rerank.py` 词覆盖近似，非 cross-encoder（设计允许可注入替换） |
| 11 | MMR 去冗余 | 检索 | **未实现** | 代码零对应；`_reorder_middle` 是抗 lost-in-the-middle，非 MMR（S8） |
| 12 | 权限/时效过滤 | 检索 | **未实现/承诺落空** | `post.filter` 只做注入检测（B2） |
| 13 | 预算拼接 | 检索 | 已实现 | `post.py:85-109`；预算派生/边界有缺陷（S3） |
| 14 | 引用溯源 [doc:chunk] | 检索 | 已实现 | `post.py:94,102`；verify 未实现，投影偏薄（S8/N3） |
| 15 | 乱序修复 | 检索(横切) | 已实现 | `post.py:112-123` 恒开启；reorder_middle 不可配（S8） |
| 16 | 语义缓存 | 横切 | **名不符实** | 精确缓存自建 dict（S4/S5），未复用 R4 CachePort，非语义 |
| 17 | 空结果降级 | 横切 | 已实现 | `post.py:82-83,99-100` EMPTY_CONTEXT；`test_fallback.py` 真测 |
| 18 | 自检索 | 横切 | **未实现** | 代码零对应（S8） |
| 19 | RAG 评测 Recall/MRR/NDCG | 横切 | **未实现(R6 内)** | R6 无评测代码；EvalPort 归 R10（`AGENT-总设计.md:1643`），口径需澄清（S8） |
| 20 | 注入检测接入 | 横切 | 部分 | 复用 R5 检测器属实；但 pipeline 注入点死（B3）、逐块绕过（S6） |
| 21 | 增量更新 | 横切 | **未实现** | `upsert` 仅 extend，无去重/覆盖（S8） |

> 注：计数按详设 §5「横切 6」= 语义缓存/空结果降级/自检索/评测/增量/注入检测接入；上表多列了「乱序修复」并单列「摄取去重」，是为暴露实现细节，不改变 5+9+6 口径。

**小结**：完整/基本实现约 7 项（3/9/9/13/14/15/17），部分实现约 7 项，明确未实现约 6 项（7/11/12/18/19/21）+ 名不符实 1 项（16）。设计 v2.1 声称「20 项已落地」与实际有差距。

---

## 七、验证证据（实际运行命令与退出码）

工作目录：`E:/vsc-workspace/lania-zip/lll`，解释器 `.venv/Scripts/python.exe`（Python 3.13）。

| 命令 | 退出码 | 关键输出 |
|------|--------|---------|
| `.venv/Scripts/python.exe -m pytest tests/r6 -q` | **0** | `18 passed` |
| `.venv/Scripts/python.exe -m mypy --strict src/harness/rag` | **0** | `Success: no issues found in 10 source files` |
| `.venv/Scripts/python.exe tests/tools/gate_check.py --module r6` | **0** | `14/14 项通过`，`R6 门禁全绿：允许合入` |

**独立复现实验（均为只读 `python - <<'PY'`，未改任何文件）**：
- A：`pipeline.detector is marker=True` 但 `post._detector is marker=False` → B3
- B：`with_` 后 rewrite/rerank/detector 全 False、cache 关闭 → S1
- C：`ingest` 后 embed 调用=1，`query` 后仍=1 → B1
- D：`filters=public` 混合召回仍返回 `clearance=secret` → B2
- F：`chunk_size=10` 切分注入串后 5 块全 clean → S6
- G：不同 filters 相同文本仅 1 次召回；ingest 后仍命中旧缓存 → S4
- H：`rewrite=3` 返回 3 份重复 citation/hits → S7
- I：`Budgets(max=100,used=100)` 派生 None；`budget_left=0` 派生 0→不限 → S3

**门禁为何没发现**：gate_check 的 `scan_r6` 是结构扫描（检查 token 存在），G1 覆盖率统计行数执行而非行为正确性，G3 demo 恰巧两边都用 HashEmbedder 且注入串未被切断；测试断言普遍只查「非空/结构正确」，不查「召回正确 / 过滤生效 / 检测器被替换」。这正是独立审查需要补的缺口。

---

## 八、审查边界与「待核」声明

- **装配层缺位**（第四节问 1）属跨模块归属，按架构契约 §4.3 只登记、不虚标为 R6 单模块 Blocker；端到端可用性需 R12 落地后复核。
- `dict[str, object]`（`types.py:59,78,110`）用于 `meta`/`filters`：coding-rules-common §1 禁 `Any`/`DynamicValue` 作公开类型，`object` 比 `Any` 严格且 mypy strict 通过，设计亦明确标注为「键由上游决定的动态映射边界」。该项处于规则 §1.3 具名别名要求的灰色地带，**记 Nit 不记 Blocker**；若团队严格执行「动态映射必须用具名 JsonValue 别名」，可单独提应修。
- `type DynamicValue = Any`（`pipeline.py:37`）用于 RAGPort 入口签名：RAGPort 契约本身在 `protocols.py:136,140` 使用 `DynamicValue`（=Any，`runtime/types.py:32`），属既有框架事实与边界具名别名，**有据可依**，不作为违规。
- 「语义缓存 vs 精确缓存」「BM25」「cross-encoder」等设计用词与实现的近似关系，部分属设计允许「可注入替换/离线近似」，已逐项区分标注，未一律判为缺失。

---
*独立审查报告结束 · 结论：3 Blocker / 8 Should / 3 Nit · 不予合入*
