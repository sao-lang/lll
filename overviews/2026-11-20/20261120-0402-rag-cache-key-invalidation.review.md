# R6 缓存按 key 失效 · 独立代码审查报告

> 审查对象：`src/harness/rag/pipeline.py` 缓存失效改动（无参全清 → 按 key 精准失效）
> 审查基线：设计规格 §5 FR-1~FR-7、§8.3、§10 A1/A2、§11 S4 红线
> 审查方式：只读。未修改任何源码/测试/文档（本报告为唯一新增文件）
> 验证命令（均通过）：
> - `pytest tests/r6 -q --tb=short` → 43 passed
> - `pytest tests -q --ignore=tests/r4/test_cache_embedder_default.py --tb=short` → 全绿
> 排除项说明：`tests/r4/test_cache_embedder_default.py` 引用并发会话尚未实现的 `HashCacheEmbedder`，与本轮无关，按指示排除。

---

## 一、六项审查重点核实概览（非 finding）

| # | 重点 | 结论 | 关键证据 |
|---|---|---|---|
| 1 | FR-1~FR-7 逐条兑现 | **全部兑现** | `pipeline.py:147`（FR-1）、`:235-238`（FR-2）、`:279-285`（FR-3）、`:174`（FR-4）、`:269-270`（FR-5）、`:58-64`（FR-6）、测试文件（FR-7） |
| 2 | S4 红线（本实例缓存过的 query 全失效） | **成立，未发现反例** | 命中路径 `pipeline.py:209-212` 直接 return 不重新登记，但 key 在首次写入时已登记（`:238`），且账本清空（`:285`）与缓存失效（`:284`）同步成对发生，不存在"账本已清、缓存仍在"的窗口 |
| 3 | 并发边界 | **无实际竞态，但有注释误导** | `invalidate_cache`（`:273-285`）与 query 写回段（`:235-238`）均无 await，asyncio 单线程下无法交错；§8.3 的 query 写回竞态仍在，但旧/新实现的暴露窗口均为"直到下次 ingest"，**未被本改动恶化**。详见 Nit-1 |
| 4 | 测试充分性 | 禁止无参、账本身份已锁住；**行为级覆盖有缺口** | `test_cache_invalidation.py:64-66` 锁住"只传具体 key + 账本清空"；`:81-87` 锁住账本共享/重置对象身份。缺口见 Should-1/2、Nit-2/3 |
| 5 | 契约兼容 | **一致，公开 API 无变化** | `CachePort.invalidate` docstring（`:58-64`）与 `InMemoryCache.invalidate`（`:84-89`，`pop(key,None)` 幂等）及测试替身 `_SpyCache`（`test_cache_invalidation.py:31-37`）一致；构造参数与方法签名未变 |
| 6 | 已知边界 A1 | **实现与声明一致，但无测试覆盖** | 规格 §10 A1（`R6-缓存按key失效-设计规格.md:72`）明示接受；实现 `pipeline.py:283-285` 只遍历本实例账本，符合声明。测试缺口见 Should-2 |

---

## 二、Blocker（阻断交付）

**无。** 未发现破 S4、误删他人缓存、公开 API 变化或测试失败的问题。

---

## 三、Should（建议修）

### Should-1：共享账本只测了对象身份，未锁住"跨实例失效"的行为语义（A2）

- **证据**
  - 实现：`src/harness/rag/pipeline.py:269-270` —— `if new._cache is self._cache: new._cache_keys = self._cache_keys`，共享的是同一个 `set` 对象。
  - 测试：`tests/r6/test_cache_invalidation.py:80-82` —— 仅断言 `q_same._cache_keys is p._cache_keys` 与 `q_same._cache is p._cache`，**没有任何一侧 ingest 后对另一侧缓存条目的失效断言**。
  - 声明：`docs/02-模块详设/Agent/R6-缓存按key失效-设计规格.md:73`（A2）、`:78`（S4 红线）。
- **后果**：若后续实现被改成 `new._cache_keys = set(self._cache_keys)`（复制账本）或只在原实例保留账本，现有测试仍全绿，但新实例 ingest 将失效不到旧实例写入的 key，直接破 A2 与 S4——而这是 FR-5 存在的核心理由。测试只覆盖了"引用相同"，没覆盖"引用相同带来的效果"。
- **建议改法**：在 `test_with_shares_or_resets_ledger` 中补行为断言：`p` 写入 key1、`q_same` 写入 key2 → 调 `q_same.ingest()` → 断言 `_SpyCache.data` 中 key1、key2 均被移除且共享账本为空 → 再令 `p.query` 同一 query 验证重算（新文档可见）。

### Should-2：A1 明示边界（别实例写入的 key 不被本实例 ingest 失效）无测试锁定

- **证据**
  - 声明：`docs/02-模块详设/Agent/R6-缓存按key失效-设计规格.md:72` 明示接受"共享 cache 下其他实例写入的 key 不在本实例账本内，本实例 ingest 不会失效它们"。
  - 实现：`src/harness/rag/pipeline.py:283-285` 只遍历 `self._cache_keys`，符合 A1。
  - 测试缺口：`tests/r6/test_cache_invalidation.py:40-87` 全程只有一个实例 + 一个 `_SpyCache`，未在 cache 中预置"别实例的 key"；`tests/r6/test_wiring.py:101-112` 也只验证单个自定义 cache 被委托，未构造共享 cache 多实例场景。
- **后果**：本轮改动的**核心卖点**是"不误删共享缓存中他人的 key"，但没有任何测试证明这一点。若有人回退全表 clear、或在 `with_` 中错误扩大账本范围，A1 契约漂移不会被测试发现；同时 review 无法用测试证据确认 S4 之外的"不误伤"承诺。
- **建议改法**：新增测试——两个 `RAGPipeline` 共享同一个 `_SpyCache`（**不要**通过 `with_` 派生，以模拟"别实例"），A 写入 keyA、B 写入 keyB；B.ingest() 后断言：① keyA 仍在 `cache.data`；② keyB 被删；③ `cache.invalidate_calls` 只含 keyB、无 `None`。

---

## 四、Nit（可留）

### Nit-1：`invalidate_cache` 的 tuple 注释与 `clear()` 不自洽；单线程下 tuple 无实际保护作用

- **证据**：`src/harness/rag/pipeline.py:281-285` —— 注释称"invalidate 期间若并发 query 登记新 key，留待下次 ingest 处理，不在本轮循环内被 clear 吞掉"，但 `:285` 的 `self._cache_keys.clear()` **无条件清空整个集合**，并发新增的 key 会被一并删除，并非"留待下次"。因 `CachePort` 契约为同步（`:45`）且 `invalidate_cache`、query 写回段均无 await，asyncio 单线程下该交错窗口实际不存在。
- **后果**：注释误导维护者以为存在并发保护；若未来 CachePort 换成含 await 或跨线程实现，会真实出现"缓存有数据、账本无记录"的 key 泄漏，导致该 key 永远不再被 ingest 失效（S4 破口）。
- **建议改法**：把 `:285` 改为差集删除 `self._cache_keys.difference_update(snapshot)`（`snapshot = tuple(self._cache_keys)`），使代码与注释意图一致；或保留现状但把注释改为"同步路径无 await，循环期间不会被任务切换插入登记"。

### Nit-2：`cache=False/None` 与"多次 ingest"路径无测试覆盖

- **证据**：FR-1 要求 `cache` 关闭时也建空账本（`设计规格.md:23`），实现位于 `src/harness/rag/pipeline.py:141-147`；但 `tests/r6/test_cache.py:25-28`、`tests/r6/test_wiring.py:83` 均用 `cache=True`，`tests/r6/test_cache_invalidation.py:40-46` 注入 spy——**无 `cache=False/None` 的账本断言**。多次 ingest 场景（第二次账本已空、不应产生失效调用）同样未被显式测试。
- **后果**：若回归为"无 cache 仍持有 stale 账本"或误调 `invalidate`，测试不会发现；风险低（实现本身正确）。
- **建议改法**：补一条参数化测试：`cache=False`/`None` 时 `p._cache_keys == set()`，ingest 后无失效调用；并在按-key 测试中加第二次 ingest 断言 `invalidate_calls` 数量不增加。

### Nit-3：`ingest` 空 chunks 不触发失效，S4 字面与 FR-4 存在阅读歧义，且无测试固定

- **证据**：`src/harness/rag/pipeline.py:168-169` —— `if not chunks: return 0` 提前返回，未走到 `:174` 的 `invalidate_cache()`；FR-4 表述为"成功 upsert 后"（`设计规格.md:26`），而 S4 红线表述为"ingest 后所有本实例缓存过的 query 必须失效"（`:78`）。
- **后果**：语义上无新文档入库故无需失效，且这是**既有行为**（非本轮引入）；但字面易被误读为破 S4，缺乏测试固定"空 ingest 不清缓存"的预期。
- **建议改法**：补一例测试断言空 chunks 返回 0 且不产生 invalidate 调用；或在 S4 描述中澄清为"成功入库后"。

### Nit-4：新测试函数名与 FR-7 约定不一致

- **证据**：FR-7 约定 `test_invalidate_by_key_not_all`（`设计规格.md:32`），实际为 `test_ingest_invalidates_by_key_not_whole_cache`（`tests/r6/test_cache_invalidation.py:49`）。
- **后果**：纯命名偏差，不影响覆盖强度，可留。
- **建议改法**：如需严格可追溯，统一命名或在 overview 中记录映射关系。

---

## 五、结论与判定

- **实现质量**：FR-1~FR-7 逐条兑现；S4 红线成立且未发现反例；CachePort 契约细化与默认/自定义实现一致，公开 API 无变化；已知边界 A1、A2 与规格声明一致。§8.3 的 query 写回竞态仍在但未被本改动恶化。
- **验证结果**：R6 测试 43 项全绿，全量 pytest（排除并发会话文件）全绿，gate/mypy/pyright 本轮未由审查方复跑（按证据要求仅做只读验证）。
- **遗留风险**：集中在测试的行为级覆盖——共享账本的跨实例失效效果（Should-1）与"不误删他人 key"的核心承诺（Should-2）尚无测试锁定；`invalidate_cache` 注释与 `clear()` 语义不自洽（Nit-1）。

**判定：Blocker = 0 → 通过。** 建议合入前优先处理 Should-1、Should-2 两项测试补强，Nit 可按迭代节奏处理。
