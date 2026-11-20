# R6 缓存按 key 失效 · 独立复审报告（fix 后 / review3）

> 复审角色：**独立复审员**，全新上下文。未参与实现、未参与上一轮审查（review.md / review2.md 均作为只读输入）。
> 复审对象：`src/harness/rag/pipeline.py` 的 R6 按 key 失效改动，经 fix-loop 后的最终状态。
> 复审方式：只读。未修改任何源码/测试/文档（本报告为唯一新增文件）。
> 复审输入：
> - `overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review.md`（2 Should + 4 Nit）
> - `docs/02-模块详设/Agent/R6-缓存按key失效-设计规格.md`（FR-1~FR-7、A1/A2、S4）
> - `git diff -- src/harness/rag/pipeline.py`
> - `tests/r6/test_cache_invalidation.py`（4 用例，含 fix-loop 补的 2 个行为级测试）
> - `overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review2.md`（实现方 fix 记录）
>
> 本轮只读复跑（均通过）：
> - `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/r6 -q` → **45 passed**
> - `PYTHONPATH=src .venv/Scripts/python.exe tests/tools/gate_check.py --module r6` → **14/14 全绿**
> - `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests -q --ignore=tests/r4/test_cache_embedder_default.py` → **510 passed**
>   - 排除项：并发会话的 RED 文件（引用未实现的 `HashCacheEmbedder`），与本轮无关，按指示排除。

---

## 一、复审重点逐项核实

### 重点 1 · Should-1「共享账本跨实例失效效果」是否真修到位 —— **已修到位**

测试：`tests/r6/test_cache_invalidation.py:95-122`，核心在 `cross_invalidate` 段（`:109-117`）。

断言逻辑核查（不看测试名，看能否抓住回归）：

| 断言 | 位置 | 能抓住的回归 |
|---|---|---|
| `q_same._cache_keys is p._cache_keys` | `:106` | 抓住「`with_` 时账本复制」（`new._cache_keys = set(self._cache_keys)`）——身份不成立即失败 |
| `q_same._cache is p._cache` | `:107` | 抓住「`with_` 换 cache / 新建 cache」 |
| `len(both) == 2`（`both = set(p._cache_keys)`，在 `q_same.query(beta)` 后抓） | `:111,:116` | 若账本在 `with_` 时被复制，则 `p._cache_keys` 只含 seed 的 `key_alpha`（`q_same.query(beta)` 写入的是副本），`len(both)==1` 失败。行为层再补一层防复制覆盖 |
| `cache.data == {}`，`q_same.ingest(d2)` 之后 | `:112,:117` | 抓住「账本共享但 ingest 只失效本实例写入的 key」（如账本带 owner 标记、仅失效 self 写入项）：`p` 写的 `key_alpha` 会残留，断言失败 |

- 前置条件成立：`seed()`（`:100-104`）让 `p` 写 `key_alpha`；`cross_invalidate()` 让 `q_same` 写 `key_beta`。两者 text 不同（`alpha` vs `beta`），`_cache_key` 含 text（`pipeline.py:291-294`），故为不同 key；`len(both)==2` 反证了两侧写入确实落在同一账本。
- 语义对齐 A2（`设计规格.md:73`）与 S4（`:78`）：新实例 ingest 能失效旧实例写入的 key。
- 关于原建议「再令 `p.query` 同一 query 验证重算（新文档可见）」未在**本用例**补齐：`cache.data == {}` 是重算的充分条件（下次 query 必 miss），且 invalidate 后重算的端到端断言已由 `tests/r6/test_cache.py:36-43`（`retriever.calls == n_first + 1`）覆盖。不构成缺口，不计 finding。

**结论**：身份断言 + 行为断言互补，覆盖「账本复制」「换 cache」「失效范围不全」三类回归，Should-1 真修到位。

### 重点 2 · Should-2「别实例 key 不被误删」是否真修到位 —— **已修到位**

测试：`tests/r6/test_cache_invalidation.py:70-92`。

断言逻辑核查：

- 场景构造正确：`a = _pipeline(cache)`、`b = _pipeline(cache)`（`:73-74`）**独立构造**、共享同一 `_SpyCache`、**不经 `with_` 派生**——精确模拟 A1（`设计规格.md:72`）的「共享 cache 下别实例写入的 key」。各自 `__init__` 建独立账本（`pipeline.py:145-147`）。
- `before == key_a | key_b`（`:83,:89`）：锁定前置状态——cache 中确有 A、B 两实例写入的 key，使后续断言有意义。
- `key_a <= set(cache.data)`（`:90`）：**核心断言**。若 `b.ingest` 回退为全表清空（无参 `invalidate()`），`key_a` 被抹掉即失败。
- `not (key_b & set(cache.data))`（`:91`）：确保 B 自己的 key 确实被失效（避免「啥也不删」假通过）。
- `None not in cache.invalidate_calls`（`:92`）：锁住「全程无无参全清」。
- `key_a.isdisjoint(key_b)`（`:88`）：确认两实例登记的是不同 key（`text` 分别为 `alpha`/`gamma`）。

覆盖判断：能抓住「回退全表 clear」「把 cache 全 key 登记进本实例账本」「无参误删」等 A1 契约漂移。Should-2 真修到位。

### 重点 3 · Nit-1 的 `difference_update(snapshot)` 改法 —— **正确、注释自洽、无新问题**

实现：`src/harness/rag/pipeline.py:273-288`，注释 `:281-284`。

- 正确性：`snapshot = tuple(self._cache_keys)`（`:285`）固定失效集合 → 逐个 `invalidate(key)`（`:286-287`）→ `difference_update(snapshot)`（`:288`）。
  - 同步语义下（`CachePort` 契约为同步，`pipeline.py:44-45`；本方法无 await）：`snapshot == 当前账本`，`difference_update(snapshot)` 与 `clear()` 等价，FR-3（`:25`）兑现。
  - 未来若换可挂起/跨线程实现：循环期间新登记的 key 不在 snapshot 中，不会被 `difference_update` 删掉，避免「缓存有数据、账本无记录」泄漏——与原报告建议意图一致。
- 注释自洽：先述「同步无 await → snapshot == 当前账本」，再解释用 `difference_update` 而非 `clear` 是为此后可能的可挂起实现做防护；「当前等价、为未来防护」逻辑一致，无自相矛盾。
- 早退 `if self._cache is None or not self._cache_keys: return`（`:279-280`）与 FR-3 等价（账本空时原实现循环 0 次、`clear()` 空集，均无副作用）。
- 异常路径：§8.3（`设计规格.md:65`）明示异常上抛、本轮不改；`invalidate` 抛异常时 `difference_update` 不执行，账本保留、下次 ingest 重试（幂等），与改前 `clear()` 行为一致，未引入新泄漏。
- 未发现新问题。

### 重点 4 · 全量门禁 —— **仍绿**

见顶部三条复跑结果：R6 45 passed、gate r6 14/14、全量（排除并发 RED）510 passed。

---

## 二、其他观察（不构成回归风险）

- **Nit（改动聚焦）**：`git diff` 中除核心失效逻辑外，还含与 R6 需求无直接关系的格式化/清理调整——`import` 重排、`Vector` 导入移除、`__all__` 重新排序（`pipeline.py:13-33`）、`with_` 返回注解由 `"RAGPipeline"` 改为 `RAGPipeline`（`:241`）。已核对：`Vector` 在全文件未被使用，移除正确；`from __future__ import annotations`（`:11`）下注解去引号等价；`__all__` 仅字母序调整。均无害，但增加了 diff 噪音，建议与核心改动分离提交。级别 Nit。

---

## 三、Findings 汇总

| 级别 | 数量 | 明细 |
|---|---|---|
| Blocker | **0** | 无 |
| Should | **0** | 上轮 2 Should 均真修到位（断言逻辑可抓回归，非仅测试名） |
| Nit | **1** | 改动含与核心需求无关的格式化/清理调整，无害但增 diff 噪音 |

---

## 四、结论

**Blocker = 0 / Should = 0 / Nit = 1 → 通过。**

- 上轮 2 个 Should 的修复经断言逻辑层核实均到位：Should-1 用「身份断言 + `cache.data == {}` 行为断言」锁住 A2 跨实例失效；Should-2 用「两独立实例共享 cache + `key_a` 残留断言 + 无 None 断言」锁住 A1 不误删。
- Nit-1 的 `difference_update(snapshot)` 改法正确、注释自洽、未引入新问题。
- 全量门禁复跑全绿（R6 45、gate 14/14、全量 510）。
- 唯一 Nit 为改动聚焦问题（格式化噪音），不影响正确性与契约，**不阻断交付**。建议合入即可。
