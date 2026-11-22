# 代码精读 04 · rag 检索增强模块

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/rag/` 全部 10 个文件。
> 你说的「tag 模块」按确认为本模块。总入口见 [00](./00-总入口与全局链路.md)。

---

## 目录

1. 一句话心智模型
2. 模块文件地图
3. 核心 DTO（types.py）
4. 流水线总控（pipeline.py）
5. 切分段（splitter.py）
6. 向量化段（embedder.py）
7. 存储段（store.py）
8. 召回段（retriever.py）：向量 + 混合 RRF
9. 重排段（rerank.py）
10. 后处理段（post.py）
11. 改写段（rewrite.py）
12. 缓存与失效
13. 端到端：一次 query
14. 细节速记卡

---

## 1. 一句话心智模型

RAG 模块把「文档进、答案上下文出」拆成五段主链 + 三个增强点：

```
入库：Document → 切分 → 向量化 → 存 VectorStore
检索：Query → 改写 → 向量化 → 召回(向量+关键词 RRF) → 重排 → 后处理 → RAGResult
```

五段主协议：`Splitter` / `Embedder` / `VectorStore` / `Retriever` / `PostProcessor`——**全部必填**。
三个增强注入点：`QueryRewriter` / `Reranker` / `InjectionDetector`——**可缺席**。

**最关键的一致性纪律**：查询向量必须由流水线里**注入的那个 embedder** 算，与入库同源。召回器绝不自行向量化，否则入库/查询落在不同语义空间，检索直接失效（这是历史 B1 修复）。

---

## 2. 模块文件地图

| 文件 | 角色 |
|---|---|
| `types.py` | DTO + RRF_K 常量 |
| `pipeline.py` | RAGPipeline 总控 + 缓存（338 行，核心） |
| `splitter.py` | 递归切分 |
| `embedder.py` | Embedder / EmbeddingClient + 哈希默认 |
| `store.py` | VectorStore + 内存实现 + matches_filters |
| `retriever.py` | 向量召回 / 混合召回 RRF |
| `rerank.py` | 词面覆盖度重排 |
| `post.py` | 过滤 + 预算拼接 + 引用 + 乱序修复 |
| `rewrite.py` | 无模型规则改写 |
| `__init__.py` | 公共 API |

---

## 3. 核心 DTO（types.py）

全部是 pydantic 严格模型（继承 `_Strict`，`extra="forbid"`，防止拼错字段被静默塞入）。

- `Vector = tuple[float, ...]`：具名别名，避免散落的裸元组。
- `RRF_K = 60`：RRF 融合常数。
- `QueryPurpose`：`QA`（问答）/ `MEMORY`（记忆召回）。
- `Document`：`doc_id` / `text` / `meta`。
- `Chunk`：`chunk_id`（形如 `doc_id:序号`）/ `doc_id` / `text` / `tokens`（字数估算）/ `meta` / `vector`（入库前由 Embedder 填）。
- `Hit`：`chunk` / `score` / `source`（召回通道名）/ `ts`。
- `Query`：`text` / `filters` / `top_k`（默认 5）/ `token_budget` / `purpose`。
- `TokenBudget`：`limit`（None 不限，0 无预算）。`__int__()`：None 返回 **-1**，其余返回 limit。
- `RAGResult`：`context`（拼接文本）/ `citations`（引用 id 列表）/ `hits` / `empty` / `tokens_used`。

---

## 4. 流水线总控（pipeline.py）

### 4.1 两个入口

- **RAGPort 入口**：`ingest(documents, ctx) -> int` 和 `retrieve(query, ctx) -> list[dict]`。DTO 是 DynamicValue（框架装配用）。
- **富入口**：`query(q: Query) -> RAGResult`，返回结构化结果（单测与上层精细消费用）。

### 4.2 构造：五必填 + 四可选

必填：`splitter` / `embedder` / `store` / `retriever` / `post`。
可选：`rewrite` / `rerank` / `cache` / `detector`。

`cache` 参数三种形态归一为 `CachePort | None`：`True` → 内置 InMemoryCache；`False`/`None` → 关闭；实例 → 直接用。

还有一个重要内部字段：`_cache_keys`——本实例写入缓存的 key 账本，ingest 后按 key 精准失效。

### 4.3 ingest（入库链）

1. 逐个 document：是 `Document` 直接用，否则 `Document.model_validate`（鸭子兼容）。
2. `splitter.split(doc)` 收集所有 chunk。
3. 无 chunk → 返回 0。
4. `await embedder.embed([c.text for c in chunks])` 批量向量化，逐 chunk 写回 `chunk.vector`。
5. `await store.upsert(chunks)`。
6. `await invalidate_cache()`——新文档对已缓存 query 可见。
7. 返回入库块数。

### 4.4 retrieve（Port 入口）

1. 从 `ctx.budget_left` 派生 `TokenBudget`（见 4.7）。
2. 造 `Query(text, token_budget)`。
3. `await query(q)`。
4. 把 hits 投影为 dict：`citation`（chunk_id）/ `doc_id` / `text` / `score` / `meta`。

### 4.5 query（富入口）：完整检索链

```
cache_key → 命中缓存? 返回
queries = [q]; 有 rewrite → rewritten 替换
vecs = await embedder.embed([sub.text for sub in queries])   # 同源 embedder
for (sub, vec): hits += await retriever.retrieve(sub, vec)
hits = _dedup(hits)                                          # 按 chunk_id 去重保最高分
有 rerank → hits = rerank.rerank(hits, q)
result = await post.assemble(hits, TokenBudget(limit=q.token_budget), detector, filters)
写缓存 + 登记 key
return result
```

**S7 去重**：多 query 改写会产生重复命中，按 `chunk_id` 去重，保留最高分，再按分数降序。

### 4.6 build

`layer_program("rag", payload={"layer":"rag"})()`——外壳 Program，供 R12 试点。

### 4.7 with_：不可变替换

允许替换 9 个注入点，未知替换点抛 ValueError。返回新实例，原实例不变。

**账本归属细节（FR-5）**：若新实例的 cache 与旧实例是同一个对象（含都未传的情况）→ 共享同一个 `_cache_keys` 账本；若 cache 被换 → 新实例用空账本。否则新实例 ingest 失效不到旧实例写入的 key。

### 4.8 _budget_from_ctx

从 `ctx.budget_left` 派生预算：

- None → None（不限）。
- int → `max(0, raw)`（0 表示耗尽，**不当成不限**）。
- 有 max_tokens/used_tokens 的投影 → `max(0, max - used)`。
- 有 tokens/token_budget/total_tokens 属性 → 取第一个 int。
- 都不识别 → None。

---

## 5. 切分段（splitter.py）

`RecursiveSplitter` 默认：`chunk_size=512`、`overlap=64`。

构造校验：`chunk_size <= 0` 抛错；`overlap` 不在 `[0, chunk_size)` 时退化为 0。

`split(doc)`：切分后每个 piece 生成一个 Chunk，`chunk_id = f"{doc.doc_id}:{i}"`，`tokens = len(piece)`，meta 复制。

`_split_text` 递归逻辑：

1. 长度 ≤ chunk_size → 返回单片（空串返回空列表）。
2. 按边界优先级 `("\n\n", "。", ". ")` 找第一个命中的边界 → split → 每个 part 递归（过长继续切）或原样收。
3. 无边界 → 按 `step = chunk_size - overlap` 固定步长滑窗切片，保证跨块上下文连续。

---

## 6. 向量化段（embedder.py）

两层协议：

- `Embedder`（五段之一）：`async embed(texts) -> list[Vector]`。
- `EmbeddingClient`（真实后端）：同样签名。需要外部系统的协议必须抽象可注入 client。

`HashEmbedder` 委托 client（缺省内置 `_HashClient`）。

`_HashClient`：把每个词 `hash(word) % dim` 落到桶里累加，再 L2 归一成单位向量。`DEFAULT_DIM = 16`，dim ≤ 0 回退默认。

注意：这里用的是内置 `hash()`（进程随机化）——与 model/cache 里 HashCacheEmbedder 刻意用确定性 31 进制哈希不同。这是两个独立实现，语义空间也不同，**不能混用**。

---

## 7. 存储段（store.py）

`matches_filters(chunk, filters)`：向量通道与关键词通道**共用的唯一过滤判定**。

- filters 空 → True。
- 键是 `chunk_id`/`doc_id` → 直查块字段；否则查 `chunk.meta.get(key)`。
- 全部门槛命中才通过（AND 语义）。

`InMemoryVectorStore`：

- `upsert`：追加块引用（**是对象引用不是深拷贝**，调用方入库后不应再改这些 Chunk；当前未实现按 chunk_id 覆盖的去重增量）。
- `all_chunks()`：返回全部块，供混合召回的关键词通道。
- `search(vec, top_k, filters)`：遍历，跳过 vector=None 和不过滤的，算点积分数，降序取 top_k。
- `_dot`：点积，`zip` 逐项相乘求和（维度不等时 zip 会截断，当前无显式校验）。

---

## 8. 召回段（retriever.py）：向量 + 混合 RRF

### 8.1 核心纪律

查询向量由 pipeline 算好传入 `retrieve(query, vec)`，召回器**不自行向量化**。

### 8.2 VectorRetriever

最简单：`await store.search(vec, top_k, filters)`。

### 8.3 HybridRetriever：向量 + 关键词 + RRF

构造：`store` + 可选 `all_chunks`（关键词素材）。若两者都无 → `_corpus()` **显式抛 ValueError**（S2：退化必须可见，不允许静默退化为纯向量）。`all_chunks` 未传时从 `store.all_chunks()` 鸭子取用。

`retrieve` 流程：

1. 向量通道：`store.search(vec, top_k, filters)`。
2. 关键词通道：`_keyword_search(query, corpus)`。
3. `_rrf([vector_hits, keyword_hits])` 融合。
4. 取前 top_k。

`_keyword_search`：

- query 文本按空白切成词项集合（小写），先过滤 corpus 里的每个 chunk（**两通道共用 matches_filters，B2 修复：关键词通道不再绕过权限/时效过滤**）。
- 词面覆盖度 = 命中词项数 / 总词项数；>0 才成为 Hit。
- 降序取 top_k。

`_rrf`（Reciprocal Rank Fusion）：

```
RRF(d) = Σ weight / (k + rank)      k = RRF_K = 60, rank 从 1 起
```

- 通道权重：向量 1.0、关键词 1.0（具名常量）。
- 按 `chunk_id` 累加分数去重。
- 分数写回 Hit.score，降序返回。

**为什么用 RRF**：两通道分数量纲不同（点积 vs 覆盖率），按名次融合可比，且能去重。

---

## 9. 重排段（rerank.py）

`LexicalReranker` 默认 `top_n=5`。真实 cross-encoder 是外部模型，抽象为可注入 client；默认用词面覆盖度近似精排（离线可测）。

`rerank(hits, query)`：query 词项集合；每个 hit 算覆盖度 `命中词项/总词项`；降序取 top_n。query 无词项时直接返回前 top_n（不改序）。

---

## 10. 后处理段（post.py）

`DefaultPostProcessor` 把命中整理成可注入 LLM 的 RAGResult。

常量：

- `EMPTY_CONTEXT`：空结果固定提示——「No relevant documents were found. Answer cautiously from general knowledge or decline to answer.」
- `_CITATION_FMT = "[{cid}]"`。

`filter(hits, detector, filters)`：兜底过滤——`matches_filters` + 注入检测（命中 `INJECTED` 剔除）。这是**融合后的统一兜底**，不依赖各召回通道各自正确（B2）。

`assemble(hits, budget, detector, filters)`：

1. `limit == 0` → 直接返回 empty=True（预算为 0 不能当不限）。
2. `safe = filter(...)`；空 → empty。
3. `ordered = _reorder_middle(safe)`——抗 lost-in-the-middle。
4. 贪心装入：逐 hit，`need = chunk.tokens`；`used + need > limit` → **跳过**（继续试更小的）；否则追加，文本拼 `[cid]` 引用，累加 used。
5. 一个都装不下 → empty。
6. 返回 RAGResult：context 用换行拼、citations 是 chunk_id 列表、empty=False、tokens_used=used。

`_reorder_middle`（乱序修复）：把高相关片段**交错放两端**、低相关居中——偶数下标进 left，奇数下标进 right，最后 `left + reversed(right)`。降低长上下文中部被忽略的风险。长度 ≤ 2 原样返回。

---

## 11. 改写段（rewrite.py）

`MultiQueryRewriter`：无模型的规则改写——同义词等价替换 + 标点/连接词拆子句，产出多个语义等价的查询变体，提升词面覆盖。

常量：

- `DEFAULT_N_VARIANTS = 3`。
- `_SPLIT_TOKENS`：按长度降序的切分符（`"以及"` 在 `"及"` 前，避免短词抢先切开）。
- `_DEFAULT_SYNONYMS`：精简同义词表（召回↔检索、精排↔重排、向量↔嵌入、关键词↔词面、文档↔语料、怎么↔如何）。key 和 value 都是等价词，**不引入原问题之外的新实体**（D2 红线）。

构造：`n_variants`（<1 按 1）、`synonyms`（注入后完全替换内置表，构造期浅拷贝防外部改动）。

`rewrite(query)` 的**两轮组装**（B1 修复核心）：

1. 第一轮基础覆盖：`[原串, 整串同义第1个, *全部子句]`——保证 N=3 时同义策略和子句策略都有代表，避免子句把同义变体挤出。子句先于子句同义入列（S-R1：避免每个子句占 2 名额饿死后段）。
2. 第二轮增益填充：整串同义余量 + 各子句同义（`limit=1`）——N 较大时进一步放大覆盖。

最后：去重保序 → 截断到 n_variants → 每个变体 `query.model_copy(deep=True, update={"text": variant})`（**只改 text，其余字段深拷贝继承**）。

`_split_subqueries` 保证每个产物都是原串的**连续子串**（D2 红线）；英文连接词 `" and "` 切片后 strip 仍是连续子串。

`_synonym_variants` 一轮只替换一种 key 的**首个出现**，避免组合爆炸；`limit` 防止把子句挤出窗口。

---

## 12. 缓存与失效

`CachePort` 协议：`get(key)` / `set(key, value)` / `invalidate(key=None)`，全同步（内存操作无 IO）。`invalidate(key)` 对不存在 key 幂等；pipeline **只依赖单键失效**，不依赖 None 全清（避免共享缓存被误清空）。

`InMemoryCache`：进程内 dict。

`_cache_key(q)`：`f"{text}|{top_k}|{token_budget}|{purpose}|{filters_repr}"`——纳入**全部影响检索的参数**，filters 用 `repr(sorted(items))` 保证确定性。

`invalidate_cache()`（S4/FR-3）：对 `_cache_keys` 里每个 key 调 `invalidate(key)`，然后 `difference_update(snapshot)` 而非 clear——仅移除已失效的 key，循环期间新登记的 key 保留到下次（防「有缓存无账本」泄漏）。

---

## 13. 端到端：一次 query

假设 Query(text="怎么优化召回", top_k=5, token_budget=1000)，装配了 MultiQueryRewriter + HybridRetriever + LexicalReranker + DefaultPostProcessor：

1. `_cache_key` 算出键 → 未命中。
2. rewrite：原串 + 同义「怎么优化检索」+ 子句（若无切分符则无子句）→ 得 2 个 Query。
3. `embedder.embed([两个文本])` → 2 个向量（**同一个 embedder**）。
4. 每个子 Query 走 HybridRetriever：向量通道 store.search + 关键词通道词面覆盖 → RRF 融合 → 各取 top 5。
5. `_dedup`：两路命中按 chunk_id 去重保最高分，降序。
6. rerank：按「怎么/优化/召回」词项覆盖度重排，取 top 5。
7. `post.assemble`：先 filter（过滤 + 注入检测）→ `_reorder_middle`（高相关放两端）→ 贪心装入（chunk.tokens 累计 ≤ 1000）→ 拼文本+引用。
8. 返回 RAGResult（context 含 `[doc:chunk]` 引用、citations 列表、empty=False、tokens_used）。
9. 写缓存 + 登记 key。

若随后 ingest 了新文档：向量化新 chunk → store.upsert → `invalidate_cache()` 按账本逐个失效旧 query 的缓存 → 新文档对查询可见。

---

## 14. 细节速记卡

- **同源 embedder 是铁律**：查询向量必须由 pipeline 注入的 embedder 算，召回器不自行向量化（B1）。
- **五段必填，三增强可选**：splitter/embedder/store/retriever/post 必填；rewrite/rerank/detector 可缺席。
- **关键词通道共用 filters**：B2 修复，不绕过权限/时效过滤。
- **无关键词素材显式抛错**：S2，不静默退化为纯向量。
- **RRF_K=60，rank 从 1 起**：按名次融合不同量纲的通道分数。
- **改写保证连续子串**：D2 红线，不引入新实体。
- **改写两轮组装**：先基础覆盖（含同义+子句）再增益填充，防子句挤出同义（B1）。
- **overlap 保证跨块连续**：step = chunk_size - overlap。
- **InMemoryVectorStore 存引用不深拷贝**：入库后别改 Chunk。
- **TokenBudget limit=0 是「无预算」不是「不限」**：assemble 直接返回 empty。
- **预算装入是贪心跳过**：装不下当前项就跳，继续试更小的，不是整段丢弃。
- **乱序修复抗 lost-in-the-middle**：高相关放两端，低相关居中。
- **注入检测 fail-closed**：命中 INJECTED 剔除，检测器缺省也有默认值。
- **缓存键含全部检索参数**：text/top_k/budget/purpose/filters。
- **失效按 key 精准删**：不用 None 全清，共享缓存不误删他人条目。
- **with_ 换 cache 才换账本**：同 cache 实例共享账本（FR-5）。
- **HashEmbedder 用内置 hash()**：与 model/cache 的确定性哈希不同，两空间不能混用。
- **DTO 全部 extra=forbid**：拼错字段直接报错，不静默。
- **chunk_id 形如 doc_id:序号**：引用溯源的基础。

---

*下一册：[05 skills 技能模块](./05-skills技能模块.md)。技能的注入检测同时被 tools/webfetch 和 rag/post 复用。*
