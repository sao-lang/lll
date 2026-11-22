# 代码精读 07 · MemoryStore 精读（603 行逐方法拆解）

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/memory/memory.py` 全部 603 行 + `types.py` 常量。
> 这是你最想搞懂的部分：不跳读、不省略，每个方法讲透。
> 总入口见 [00](./00-总入口与全局链路.md)，上层编排见 [01 context](./01-context上下文模块.md)。

---

## 目录

1. 一句话心智模型
2. 先看数据模型：双轴七类 + 全部常量
3. 构造与五个内部字典
4. R2 Port 履约三方法：read / write / forget
5. 同步桥：_run_sync
6. 写入链总览：ingest 的 10 步
7. 写入链逐个方法拆解
8. 召回链总览：recall 的 10 步
9. 召回链逐个方法拆解
10. 分层治理方法
11. 安全检测：递归扫描
12. 四个历史陷阱与纠正
13. 端到端：写一条偏好、召回一次
14. 细节速记卡

---

## 1. 一句话心智模型

MemoryStore 是 R7 长期记忆的默认实现。记住「两条链 + 一个双轴 + 一道安全闸」：

- **双轴**：温度层（hot/warm/cold）× 内容类（七类），正交。
- **写入链**：判定 kind → 规范化 → 冲突裁决 → 近重复去重 → importance 评分 → 分层落库。
- **召回链**：tenant 过滤 → tier/kind 过滤 → 混合召回 + rerank → MMR 去冗余 → 预算裁剪 → 注入复检。
- **安全闸**：所有写入的值递归扫 secret 键名和注入串，命中即拒绝（fail-closed）。

它同时履约 R2 的 `MemoryPort`（read/write/forget 三个简单 KV 方法）和 R7 的 `recall`（语义召回）。

---

## 2. 先看数据模型：双轴七类 + 全部常量

### 2.1 温度轴 MemoryTier

- `HOT = "hot"`：近期热层，短 TTL、高频召回。
- `WARM = "warm"`：结构化主检索层，中 TTL。
- `COLD = "cold"`：全文归档层，长 TTL、低频审计。

**三者都是长期记忆，都不是会话内短期记忆**（短期归 R1）。

### 2.2 内容轴 MemoryKind（七类）

- `EPISODIC`：情节，发生过什么。
- `SEMANTIC`：事实，知道什么。
- `PROCEDURAL`：流程，怎么做。
- `PROFILE`：用户画像，稳定身份属性。
- `PREFERENCE`：偏好约束，风格/格式/禁忌。
- `ENTITY`：实体知识，人/项目/工具/概念。
- `JOURNAL`：工作日志与决策。

### 2.3 MemoryItem 的十个字段

前五个是位置参数（兼容旧调用方）：`kind` / `content` / `importance` / `ts` / `source`。
后五个有默认值：`item_id`（稳定 id）、`confidence`（默认 1.0）、`tier`（默认 WARM）、`scope`（默认 tenant="default"）、`access_count`（默认 0）。

**scope 默认 tenant="default" 而不是 None**——这是为了防止跨 tenant 过滤被 None 旁路。

### 2.4 MemoryScope 与隔离

`tenant` 必填，`user` / `session` 可选。由装配层经 `PortCtx.meta["scope"]` 注入。

`ScopeResolver.resolve(ctx)`：

- meta 不存在或无 scope 键 → 返回 `MemoryScope(tenant="default")`（仅供 demo/测试向后兼容）。
- scope 是 MemoryScope 实例 → 直接用。
- scope 是 dict 且 tenant 是 str → 构造 MemoryScope。
- 其他 → 抛 ValueError（fail-closed）。
- tenant 是空白串 → 抛 ValueError。

`ScopeResolver.check(query_scope, ctx_scope)`：显式 query scope 的 tenant 必须与 ctx 的 tenant 一致，否则抛错——**防止从 Query 侧绕过隔离**。

### 2.5 全部阈值常量（背下来）

| 常量 | 值 | 用途 |
|---|---|---|
| `NEAR_DUP_SIM_THRESHOLD` | 0.92 | 近重复去重 / 写入链合并 |
| `PREFERENCE_REPEAT_THRESHOLD` | 3 | 同类约束累计 3 次判偏好 |
| `PROFILE_CONFIDENCE_THRESHOLD` | 0.9 | 高置信画像推断阈值 |
| `RERANK_W_RELEVANCE` | 0.5 | rerank 相关性权重 |
| `RERANK_W_IMPORTANCE` | 0.2 | rerank 重要性权重 |
| `RERANK_W_RECENCY` | 0.15 | rerank 时效权重 |
| `RERANK_W_CONFIDENCE` | 0.15 | rerank 置信度权重 |
| `MMR_LAMBDA` | 0.7 | MMR 相关性偏好 |
| `SCOPE_META_KEY` | `"scope"` | meta 承载隔离域的键 |

四个 rerank 权重**和为 1.0**——源码注释明确：连乘会把四因子 0.5 压到 0.06 失去区分度，所以用加权和。

模块内部常量：

- `_TIER_TTL_NS`：hot 1 小时 / warm 30 天 / cold 365 天（纳秒）。
- `_KIND_DEFAULT_TIER`：profile/preference/entity/semantic/procedural → WARM；episodic → HOT；journal → COLD。注意这只是**默认落点，不破坏正交**——任何 kind 都可因访问升迁/沉降。
- `_IMPORTANCE_BOOST = 0.02`、`_MAX_IMPORTANCE_GAIN = 0.2`：命中后 importance 按 log 增长的上限与系数。
- `_HALF_LIFE_NS` = 30 天（时效半衰期）。
- `_RECALL_CHUNK_PREFIX = "mem:"`：向量库 chunk_id 前缀。

`is_secret_key(key)`：大小写不敏感子串匹配 7 个特征——secret / token / password / api_key / apikey / private_key / credential。

`make_item_id(tenant, kind, content)`：`sha256(f"{tenant}|{kind}|{规范化content}").hexdigest()[:16]`。规范化是 `" ".join(content.split()).casefold()`。**tenant 参与哈希**，防跨租户同内容撞 id。

---

## 3. 构造与五个内部字典

构造收三个可选依赖：

- `embedder`：缺省 R6 `HashEmbedder()`。
- `vector_store`：缺省 R6 `InMemoryVectorStore()`。
- `detector`：缺省 R5 `KeywordInjectionDetector()`。

三个依赖都是**延迟导入**（在函数内 `from harness.rag...`），避免顶层 import 成环。

五个内部字典（理解它们就理解了整个类）：

| 字典 | 用途 |
|---|---|
| `_kv` | `(namespace, key) → 值`，KV 记忆本体（read/write/forget 操作） |
| `_items` | `item_id → MemoryItem`，长期记忆项实体（召回与治理操作） |
| `_tiers` | `item_id → 当前温度层` |
| `_item_times` | `item_id → 写入时刻(ns)`，供各层独立 TTL 判定 |
| `_vectors` | `item_id → 已 embed 的向量`，供稳定增量召回 |

**注意 `_kv` 和 `_items` 是两套存储**：read/write/forget 操作简单 KV，ingest/recall 操作记忆项。二者互不干扰。

---

## 4. R2 Port 履约三方法

### read(namespace, key, ctx)

`return self._kv.get((namespace, key))`。**KV 层不做 tenant 隔离**——注释明确说明。

### write(namespace, key, value, ctx)

1. `is_secret_key(key)` 或 `is_secret_key(namespace)` → 抛 ValueError，拒绝。
2. `_scan_value(value, key)`——递归扫值和注入串（见第 11 节）。
3. 通过才 `self._kv[(namespace, key)] = value`。

### forget(namespace, key, ctx)

`self._kv.pop((namespace, key), None)`——不存在幂等。

---

## 5. 同步桥：_run_sync

记忆模块很多方法是同步的，但 embedder / vector_store 是异步后端。

`_run_sync(coro)`：

1. 试 `get_running_loop()`。
2. 抛 RuntimeError（说明当前无事件循环）→ 直接 `asyncio_run(coro)`。
3. 有事件循环 → 开 `ThreadPoolExecutor(max_workers=1)`，在新线程里 `asyncio_run(coro)` 并 `.result()` 等待。

**为什么**：在已有事件循环的线程里直接 `asyncio.run` 会报「事件循环已在运行」。开新线程绕开。

`_run_typed(coro)` 是收窄返回类型的包装（明确 `list[Vector]`），供类型推断用。

---

## 6. 写入链总览：ingest 的 10 步

`ingest(text, ctx, kind=None, confidence=1.0, source="refined", now=None) -> MemoryItem | None`

按顺序：

1. `scope = ScopeResolver.resolve(ctx)`。
2. 同类约束历史累计：若 `kind is None`，统计本 tenant 已存的 PREFERENCE 项数 → `pref_hits`（显式 kind 时跳过统计，调用方已裁定）。
3. `chosen = kind if 显式 else refine_kind(text, confidence, preference_hits=pref_hits)`。
4. `normalized = " ".join(text.split())`（规范化空白）。
5. `item_id = make_item_id(scope.tenant, chosen, normalized)`。
6. `timestamp = now or time_ns()`。
7. **冲突裁决**：`existing = _items.get(item_id)`；若存在且 `existing.confidence > confidence` → 返回旧项（低置信不覆盖高置信）。
8. **近重复检测**：`dup = _find_near_duplicate(normalized, scope.tenant)`；若 dup 存在且 `dup.confidence >= confidence` → 返回 dup。
9. `importance = score_importance(normalized, chosen, source)`。
10. 构造 MemoryItem（tier 用 `default_tier_for(chosen)`）→ `put_item(item, item.tier, now=timestamp)` → 返回 item。

**一句话**：同 id 先按置信度裁决，再按相似度去重，都通过才评分落库。

---

## 7. 写入链逐个方法拆解

### 7.1 refine_kind：互斥优先级判定

按**详设 §12.2 权威序**，命中即停：

```
preference → profile → entity → procedural → journal → episodic → semantic(兜底)
```

具体条件（`folded = text.casefold()`）：

1. **preference**：显式偏好词（`"要"/"不要"/"总是"/"禁止"/"prefer"/"must"` 任一）**或** `preference_hits >= 3`（落实「≥3 次同类约束」）。
2. **profile**：`confidence >= 0.9` 且含身份词（`"我是"/"角色"/"职业"/"领域"/"工程师"/"身份"`）。
3. **entity**：含 `"项目"/"工具"/"实体"/"实体卡"/"Alpha"/"Beta"`。**在 procedural/journal 之前**——具名实体优先于事件/步骤。
4. **procedural**：含 `"步骤"/"流程"/"先"/"然后"/"recipe"`。
5. **journal**：含 `"决定了"/"因为"/"选择"/"决策"`。
6. **episodic**：含 `"发生"/"上次"/"昨天"/"参加了"`。
7. **semantic**：兜底。

### 7.2 score_importance：四因子加权

```
info    = min(1.0, len(set(text.split())) / 20.0)     # 信息量：不同词数/20
reuse   = 1.0 if kind in (PROCEDURAL, PREFERENCE) else 0.6
recency = 1.0                                          # 新写入满分，衰减在召回期算
weight  = 1.0 if source.startswith("explicit") else 0.7
importance = min(1.0, 0.35·info + 0.25·reuse + 0.20·recency + 0.20·weight)
```

### 7.3 _find_near_duplicate：近重复检测

1. `vec = _embed_sync(text)`。
2. 遍历 `_items`，跳过不同 tenant 的。
3. `_dot(vec, _vector_of(item)) >= 0.92` → 返回该项（首个高相似）。
4. 无 → None。

### 7.4 put_item：稳定 upsert

1. `item_id = item.item_id or make_item_id(...)`。
2. `ts = now or item.ts`。
3. 三处赋值：`_items[item_id] = item`、`_tiers[item_id] = tier`、`_item_times[item_id] = ts`。

**同 item_id 覆盖不追加**——这是「稳定 upsert」。

### 7.5 write_cold：全文保底落盘

`commit_and_compact` 第一步调用。不经过任何提炼链：

1. scope 从 ctx 解析（ctx None 时用 `tenant="default"`）。
2. 构造 MemoryItem：`kind=EPISODIC`、`content=transcript` 全文、`importance=0.5`、`source="cold:transcript"`、`tier=COLD`、稳定 id。
3. `put_item(item, COLD, now=item.ts)`。

**意义**：先保全文，再提炼摘要——避免提炼过程丢细节。

---

## 8. 召回链总览：recall 的 10 步

`recall(query: MemoryQuery, ctx) -> list[MemoryItem]`

1. `scope = ScopeResolver.resolve(ctx)`。
2. `ScopeResolver.check(query.scope, scope)`——显式 scope 必须一致。
3. tenant 过滤：只保留 `item.scope.tenant == scope.tenant` 的项。
4. tier 过滤（query.tier 非 None 时）：`tier_of(it) is query.tier`。
5. kind 过滤（query.kind 非 None 时）。
6. 无候选 → 返回空。
7. `scored = _score_candidates(query.text, candidates)`——混合召回+rerank。
8. `ranked = sorted(scored, key=score, reverse=True)`。
9. `selected = _mmr(ranked)`——MMR 贪心去冗余。
10. 遍历 `selected[:top_k]`：注入复检 → 预算裁剪 → record_access → 加入结果。

---

## 9. 召回链逐个方法拆解

### 9.1 _score_candidates：混合召回 + rerank

```
query_lower = text.casefold()
lexical[i]  = 命中词面数（query 每个词是否 in item.content.casefold()）
max_lex     = max(lexical, default=1.0) or 1.0
vectors     = _ensure_vectors(items)
qvec        = _embed_sync(text)

for each item:
    sim        = _dot(qvec, vectors[i])                # 向量相似
    relevance  = 0.6·sim + 0.4·(lexical[i]/max_lex)    # 混合相关度
    score      = 0.5·relevance + 0.2·importance
               + 0.15·recency(ts) + 0.15·confidence
```

**混合召回**：0.6 向量 + 0.4 词面。词面用归一化的命中比例（除以 max_lex）。

### 9.2 _mmr：贪心去冗余

MMR（Maximal Marginal Relevance）解决「召回结果高度相似、占满 top_k」的问题。

```
remaining = list(ranked)   # 已按 score 降序
selected = []
while remaining:
    for each (item, score) in remaining:
        max_sim = max(_dot(vector(item), vector(other)) for other in selected) or 0
        mmr_score = 0.7·score - 0.3·max_sim
    选 mmr_score 最大的，从 remaining 移除，加入 selected（记录的是 mmr_score）
return selected
```

**真贪心**：每轮重新计算所有剩余项，不是按输入顺序。`λ=0.7` 偏相关性，`(1-λ)=0.3` 偏多样性——与已选内容越像，分越低。

### 9.3 _recency：时效分数

`exp(-age_ns / _HALF_LIFE_NS)`，`age_ns = max(0, time_ns() - ts)`，半衰期 30 天。

含义：刚写入接近 1.0，30 天前降到 ~0.37，60 天前 ~0.14。

### 9.4 _ensure_vectors / _embed_sync_batch / _embed_sync / _vector_of / _dot

- `_ensure_vectors(items)`：对 `item_id` 不在 `_vectors` 的项调 `_embed_sync_batch`，然后返回每项向量。
- `_embed_sync_batch(items)`：`_run_typed(embedder.embed([contents]))`，把向量写进 `_vectors[item_id]`；同时构造 `Chunk(chunk_id=f"mem:{item_id}", doc_id="memory", text, vector)` 并 `_run_sync(vector_store.upsert([chunk]))`。**按稳定 item_id 增量，禁止全量重嵌**。
- `_embed_sync(text)`：查询文本向量化；异常（IndexError/ValueError/TypeError）→ 返回空向量 `()`，走词面兜底。
- `_vector_of(item)`：命中 `_vectors` 直接取；否则即时 embed 并缓存。
- `_dot(a, b)`：点积；**维度不等或任一为空返回 0.0**（安全降级）。

### 9.5 recall 的输出循环（三个细节）

遍历 `selected[:top_k]` 时：

1. `_scan_text(item.content)` 命中注入 → `continue`（**历史脏数据的注入复检，fail-closed**）。
2. `cost = len(item.content)`（R7 统一口径，同 token_counter 的 Unicode 码点）。若 `query.budget` 存在且 `used + cost > budget.limit` → `continue`（贪心跳过，继续试更小的，**不是整段丢弃**）。
3. `fresh = record_access(item)`——命中记账，加入 `fresh`（若 None 则原 item）；`used += cost`；若 `used >= budget.limit` → break。

**两条路径**：有 budget 走 token 贪心，无 budget 仅 top_k 截断；两条都先过 top_k 上限。

---

## 10. 分层治理方法

### tier_of(item)

先按 `item.item_id` 查 `_tiers`；查不到则遍历 `_items` 按对象身份或值相等回退（兼容旧调用方无 id 的情况）。

### tier_count()

`len(_items)`，诊断/测试用。

### promote(item) / demote(item)

- `promote` → `_shift(item, -1)`：cold→warm→hot（**晋升=提升热度**，索引越小越热）。
- `demote` → `_shift(item, +1)`：hot→warm→cold。

`_shift(item, delta)`：

1. 解析 item_id（无 id 则按内容算，tenant 用 "default"；再回退对象身份查找）。
2. `current = _tiers.get(item_id)`，None 则 return。
3. `idx = _TIER_ORDER.index(current)`（顺序是 HOT, WARM, COLD）。
4. `new = _TIER_ORDER[min(max(idx + delta, 0), 2)]`——**clamp 到边界**，已在 hot 的 promote 不动，已在 cold 的 demote 不动。

**只改温度层，不改内容轴 kind**——维护双轴正交。

### forget_expired(ttl=None, now=None)

1. `moment = now or time_ns()`。
2. 遍历 `_item_times` 的快照：有效 TTL = 传入 ttl，否则按该项当前层的 `_TIER_TTL_NS`（查不到层则回退 COLD 的）。
3. `moment - ts > effective` → 从**四个字典全删**（_items / _tiers / _item_times / _vectors），加入 expired 列表。
4. 返回被删的 item 列表。

**注意是 `>` 不是 `>=`**——恰好等于 TTL 不算过期。

### record_access(item)：命中记账

1. `item_id`，取 `stored = _items.get(item_id)`；无则 None。
2. `count = stored.access_count + 1`。
3. `boost = min(0.2, 0.02 × log(1 + count))`——**log 增长，有上限 0.2**，避免线性膨胀。
4. 构造新 MemoryItem（除 importance 和 access_count 外全复制 stored），`importance = min(1.0, stored.importance + boost)`。
5. `_items[item_id] = updated`，返回 updated。

**调用方必须用返回值**——原 item 对象没被改（DTO frozen）。

### importance_of(item) / default_tier_for(kind)

- `importance_of`：从 `_items` 读当前 importance（可能已因命中重算高于初始值）；无则 item 自带值。
- `default_tier_for`：查 `_KIND_DEFAULT_TIER`，缺省 WARM。

---

## 11. 安全检测：递归扫描

### _scan_value(value, label)：递归扫描任意值

按类型分派：

- **str**：`is_secret_key(label)` 或 `_scan_text(value)` → 抛 `ValueError("secret/injection rejected in value: <label>")`。
- **dict**：逐个 `key, sub`，递归 `_scan_value(sub, str(key))`——用子键作新 label。
- **list/tuple/set/frozenset**：逐项递归，沿用 label。
- **有 model_dump 的对象**（pydantic 模型）：展开为 dict 后递归——防嵌套 secret 绕过。
- **bytes**：`decode("utf-8", errors="replace")` 后按 str 递归。

**递归的意义**：secret 可能藏在任意深度的嵌套结构里（如 `{"config": {"api_key": "..."}}`），浅层扫描会漏。

### _scan_text(text)

`self._detector.detect(text) is InjectionVerdict.INJECTED`——复用 R5 的注入检测器（见 [05 skills](./05-skills技能模块.md)）。

---

## 12. 四个历史陷阱与纠正

源码注释明确记录了几个曾被纠正的问题（读旧文档可能遇到旧口径）：

1. **refine_kind 顺序纠正**：旧实现把 journal 提到 entity 前、procedural 又先于 entity，与详设 §12.2 冲突。**本版权威序是 preference → profile → entity → procedural → journal → episodic → semantic**。
2. **PREFERENCE_REPEAT_THRESHOLD 曾未被引用**：旧实现只看显式偏好词，没落实「≥3 次累计」。**本版通过 `pref_hits` 参数落实**。
3. **promote 方向反转**：旧实现 promote 实为沉降。**本版 promote=升热（-1），demote=降温（+1）**，对齐词面直觉。
4. **rerank 权重口径**：四因子曾用连乘，把 0.5 压到 0.06 失去区分度。**本版改为加权和，权重和=1**。

---

## 13. 端到端：写一条偏好、召回一次

**写入**：

调用 `ingest("我总是用 Python 写代码", ctx)`（ctx.meta["scope"]=tenant="alice"）：

1. resolve scope → tenant="alice"。
2. kind=None → 统计 alice 现有 preference 项数（假设 0）。
3. `refine_kind`：含「总是」→ PREFERENCE。
4. 规范化 → `"我总是用 python 写代码"`（原文 casefold 只在 hash 里做，这里只压空白）。
5. `item_id = sha256("alice|preference|...")[:16]`。
6. 无同 id、无近重复（假设）。
7. `score_importance`：info = 不同词数/20、reuse=1.0（PREFERENCE）、recency=1、weight=0.7（source="refined" 非 explicit）→ 计算得分。
8. `default_tier_for(PREFERENCE)` = WARM。
9. put_item 写四个字典。

**召回**：

调用 `recall(MemoryQuery(text="python 偏好", top_k=5, scope=MemoryScope(tenant="alice")), ctx)`：

1. resolve + check：tenant 一致，通过。
2. tenant 过滤：只留 alice 的项。
3. 无 tier/kind 限定 → 全留。
4. `_score_candidates`：query 向量 + 词面命中数 → relevance → 四因子加权 score。
5. 降序排序 → `_mmr` 去冗余。
6. 遍历前 5：`_scan_text` 干净 → 无 budget 跳过裁剪 → `record_access`（importance 微涨、access_count=1）→ 加入结果。
7. 返回 MemoryItem 列表（每项 importance 已更新）。

---

## 14. 细节速记卡

- **两条链**：写入 ingest 10 步、召回 recall 10 步，是全文主干。
- **双轴正交**：tier×kind 独立；promote/demote 只动 tier 不动 kind。
- **item_id 含 tenant**：防跨租户同内容撞 id，也是向量库 chunk_id 的基础（`mem:` 前缀）。
- **_kv 与 _items 是两套存储**：Port 的 read/write/forget 操作前者，ingest/recall 操作后者。
- **KV 层不做 tenant 隔离**：源码明确说明；隔离只在记忆项链路上。
- **冲突裁决**：同 id 时低 confidence 不覆盖高 confidence。
- **近重复阈值 0.92**：写入和召回共用同一阈值语义（写入合并、去重）。
- **refine_kind 权威序**：preference → profile → entity → procedural → journal → episodic → semantic。
- **profile 需 confidence ≥ 0.9**：高置信才判画像。
- **preference 看累计 3 次**：显式词或历史重复都算。
- **importance 公式**：0.35·信息量 + 0.25·复用度 + 0.20·时效 + 0.20·来源权重。
- **record_access 是 log 增长且有上限 0.2**：防线性膨胀。
- **rerank 四因子加权和**：0.5/0.2/0.15/0.15，和为 1（不是连乘）。
- **混合相关度 0.6 向量 + 0.4 词面**。
- **MMR = 0.7·score − 0.3·max_sim**：真贪心，与已选越像分越低。
- **时效是 30 天半衰期的指数衰减**。
- **预算裁剪是贪心跳过**：装不下就试下一个小的，不整段丢；然后 `>= limit` break。
- **召回末尾做注入复检**：历史脏数据命中也跳过（fail-closed）。
- **forget_expired 四个字典全删**：items/tiers/times/vectors 一个不留。
- **过期判断是 `>` 不是 `>=`**。
- **递归安全扫描覆盖 dict/list/tuple/set/pydantic/bytes**：防嵌套 secret。
- **_run_sync 在有事件循环时开线程**：避免「循环已在运行」错误。
- **embed 失败返回空向量**：走词面兜底，不中断召回。
- **默认依赖全部延迟导入**：避免顶层 import 成环。
- **四个历史纠正**：kind 顺序、偏好累计、promote 方向、rerank 权重口径。

---

*七册读完。建议回 [00 第 8 节](./00-总入口与全局链路.md) 再看一次端到端链路，此时每个环节的细节都已在脑中。*
