# 会话摘要：R6 缓存失效改为按 key 精准失效

## L0 索引卡（续接时先读这一段，≤12 行）
- 场景 / 路径：链 ①场景3 补充设计 → ②场景6 设计驱动重构 / 改码
- 当前闸门：**全绿**（gate r6 14/14；独立复审 Blocker=0/Should=0 通过；verify 核心 2/5 PASS，ruff/pydocstyle 未装为 S0 登记降级）
- 交付产物：spec + pipeline.py（按-key 失效）+ tests/r6/test_cache_invalidation.py（4 用例）+ 3 份 review 报告
- 未完成项：①未提交（等用户指示；禁 git add -A，需排除并发会话文件）②Nit：diff 混入上会话格式化噪音（无害，未碰）
- 下一步：提交本轮 R6 文件（git add 指定路径 → commit）；如需可追加 ruff 安装后复跑
- 并发提示：另一会话（0352）在做 R4 CacheEmbedder，其 RED 测试 `tests/r4/test_cache_embedder_default.py` 会阻断全量收集，验证时用 --ignore 排除

## 环境
- 日期时间：2026-11-20 04:02 GMT+8
- 项目：AgentKit (lll) / 工作区：E:/vsc-workspace/lania-zip/lll
- OS / Shell：Windows / Git Bash
- 运行时：Python 3.13（项目 .venv 含 pytest/mypy/pyright，验证一律用 .venv）
- IDE / Agent：WorkBuddy / conductor 主控

## 场景与 skill
- 场景：3 补充设计 → 6 设计驱动重构（用户要求"先改设计，再改代码"）
- 加载的 skill：conductor（主控）、handoff（S0）；S2 用设计规格模板，S3 挂 coding-rules-python
- 主控：conductor

## 用户诉求（原文链）
1. "R6 RAGPipeline.ingest在入库后为什么直接清空缓存？" — 已答（S4 正确性：键无语料版本，全清保证新文档可见）
2. "如果用户使用了自定义的缓存，是不是就有问题了" — 已答（全表 clear 误伤共享缓存的他人 key）
3. "这里应该是清空对应的key，即使使用内存，也应该清空对应的key。" — 本轮指令：改为按 key 精准失效，默认内存实现也走同一路径
4. "根据 @skill:conductor 执行，先改设计，再改代码"

## S0 事实调查（只读，已确认）
- 现状 `pipeline.py:166` ingest 后 `await self.invalidate_cache()`；`:258-261` 调无参 `self._cache.invalidate()` → `InMemoryCache.invalidate(None)` 走 `_data.clear()`（`:82-85`）整表清空。
- `CachePort.invalidate(key=None)` 契约（`:58-60`）：None=清空全部；单 key=失效一个。协议已支持按 key，pipeline 只是没用。
- 键 `_cache_key`（`:263-267`）= text|top_k|budget|purpose|filters，**无语料版本**——这是 S4 必须失效全部已缓存 query 的根因，改按 key 后仍需保证 S4。
- `with_`（`:231-256`）把 `self._cache` 原样传给新实例（共享同一 cache 对象），注释却写"缓存内容不迁移（新实例空缓存）"——doc/code 不一致，与本设计相关（账本归属）。
- 测试：`test_cache.py`（命中+invalidate）、`test_wiring.py:81-98`（`n_after==0` 断言）、`:101-112`（自定义 cache 被委托）。
- 并发：另一会话在 `src/harness/rag/retriever.py`（03:58 修改）与 R4 文档，本会话禁碰。

## 独立 review 结果（S5 门禁证据）
- **Blocker = 0，无阻塞项，允许交付**（独立复审轮2：Blocker=0 / Should=0 / Nit=1）。
- 独立审查报告（轮1）：`20261120-0402-rag-cache-key-invalidation.review.md`（已内嵌于本文件「独立 review 报告」章节）（Blocker=0 / Should=2 / Nit=4）。
- 独立复审报告（轮2，全新上下文，fix-loop 后）：`20261120-0402-rag-cache-key-invalidation.review3.md`（已内嵌于本文件「独立 review 报告」章节）（Blocker=0 / Should=0 / Nit=1，通过）。
- fix-loop 自核记录：`20261120-0402-rag-cache-key-invalidation.review2.md`。 〔报告已内嵌本文件「独立 review 报告」章节〕
- 轮1 的 2 项 Should（共享账本跨实例失效效果、别实例 key 不误删）已补行为级测试锁定；复审确认断言逻辑真能抓回归，非仅改测试名。
- 复审独立性：两轮均为全新上下文子代理，未参与实现；fix-loop 后由第三个全新上下文子代理复审，未复用旧报告结论。

## L1 摘要（需要背景时读）
### 核心设计决策（S2，见 spec）
- 方案：RAGPipeline 增 `_cache_keys` 账本；`query()` 写入缓存时登记 key；`ingest()` 后 `invalidate_cache()` 逐 key `invalidate(key)` 并清账本；不再调用无参全清。
- with_ 账本归属：cache 实例未替换→共享同一账本（否则新实例 ingest 失效不到旧实例写入的 key，破 S4）；cache 被替换→新实例空账本。
- CachePort 契约细化：`invalidate(key)` 对不存在 key 幂等（默认实现已满足 `pop(key,None)`）；pipeline 仅依赖单键语义，不再依赖 None 全清能力。
- 已知边界（明示供否决）：共享 cache 场景下"别实例写入的 key"不在本实例账本内，本实例 ingest 不会失效它们——按 key 方案的固有取舍，全表 clear 才覆盖；例会中口头明示。

### 验证结果（fix-loop 后终态，均带命令）
- R6 测试：`PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/r6 -q` — 45 passed
- 全量：`pytest tests -q --ignore=tests/r4/test_cache_embedder_default.py` — 510 passed（排除并发会话 RED 文件）
- 覆盖率：gate r6 G1 报 94%（阈值 85%）
- mypy strict：`.venv/Scripts/mypy.exe --strict src` — Success, 55 文件 0 error
- pyright：`pyright-python.exe src/harness/rag/pipeline.py` — 0 errors
- gate：`gate_check.py --module r6` — 14/14 全绿
- verify.py（Windows PATH 注入）：mypy PASS / pytest+cov PASS；ruff+ruff-format+pydocstyle exit=-1（未安装，S0 登记降级，由 pyright + gate 红线/docstring 检查等价覆盖）
- 独立 review：轮1 Blocker=0/Should=2/Nit=4 → fix-loop → 轮2（全新上下文复审）Blocker=0/Should=0/Nit=1 通过
- 缺口台账：scan_gaps 报 9 个"协议无默认实现"（全为既有启发式命中，R5 反思已核有默认）；debt_ledger 债指数 0

### 未完成项与下一步（完整）
- S2 设计复述 → S3 TDD → S4 自省 → S5 独立 review + 收尾。
- suggested skills：grill-design（若需拷问设计）、coding-rules-python（S3 写码前）。

## L2 详情引用（用到才按路径加载）
- 设计规格：docs/02-模块详设/Agent/R6-缓存按key失效-设计规格.md
- 独立 review 轮1：`overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review.md` 〔报告已内嵌本文件「独立 review 报告」章节〕
- fix-loop 自核：`overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review2.md` 〔报告已内嵌本文件「独立 review 报告」章节〕
- 独立复审轮2：`overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review3.md`（已内嵌于本文件「独立 review 报告」章节）
- 上一轮缓存答疑图：本轮对话内联（时序竞态图）
- 命名映射：FR-7 的 `test_invalidate_by_key_not_all` 实现为 `test_ingest_invalidates_by_key_not_whole_cache`（纯命名偏差，独立复审确认不阻断）

## 交接
- handoff 文档路径：待 S5
- 脱敏声明：本文件不含密钥 / token / PII


---

## 独立 review 报告（原 review 文件已合并至此，多轮按 Round 分节）

### Round 1 · 20261120-0402-rag-cache-key-invalidation.review

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

### Round 2 · 20261120-0402-rag-cache-key-invalidation.review2

**复审日期**：2026-11-20
**复审对象**：独立审查报告（`20261120-0402-rag-cache-key-invalidation.review.md`（已内嵌于本文件「独立 review 报告」章节））提出的 2 Should + 4 Nit 的修复
**审查角色**：实现方自省（conductor S4），非独立复审；对照上一份独立报告逐条核销
**证据基础**：`git diff` / `pytest tests/r6` / 全量 pytest / mypy / pyright / gate_r6 / verify.py

## 一、逐条核销

| 项 | 级别 | 处理 | 证据 |
|---|---|---|---|
| Should-1 共享账本只测身份未测跨实例失效效果 | Should | ✅ 已修 | `test_cache_invalidation.py::test_with_shares_or_resets_ledger` 补 `cross_invalidate()`：两侧各写 1 key（共 2）→ `q_same.ingest()` → 断言 `cache.data == {}`（两侧 key 均被失效，A2 行为级锁定） |
| Should-2 A1「别实例 key 不误删」无测试 | Should | ✅ 已修 | 新增 `test_per_instance_ledger_does_not_touch_other_instances_keys`：两个**独立构造** pipeline 共享同一 spy cache → B ingest 后断言 A 的 key 仍在、B 的 key 被删、invalidate_calls 无 None |
| Nit-1 tuple 注释与 clear() 不自洽 | Nit | ✅ 已修 | `pipeline.py:281-288` 改 `snapshot = tuple(...)` + `difference_update(snapshot)`，注释改为"同步无 await、防未来跨线程泄漏"——代码与注释一致 |
| Nit-2 cache=False/None 与多次 ingest 无测试 | Nit | ✅ 已修 | 新增 `test_cache_disabled_and_empty_ingest`：cache=False 账本空、空 ingest 无失效调用 |
| Nit-3 空 chunks 无测试 | Nit | ✅ 已修 | 同上测试：空文本返回 0、空文档列表不触发 invalidate |
| Nit-4 测试函数名与 FR-7 不一致 | Nit | ⏭ 不修 | 纯命名偏差，已在 overview 记录映射：`test_ingest_invalidates_by_key_not_whole_cache` ≡ FR-7 的 `test_invalidate_by_key_not_all` |

## 二、回归验证（fix-loop 后）

- `pytest tests/r6 -q` → 45 passed（新增 2 用例）
- `pytest tests -q --ignore=tests/r4/test_cache_embedder_default.py` → 全绿（510 passed）
  - 排除项：并发会话（0352）的 RED 状态测试，引用尚未实现的 `HashCacheEmbedder`，与本轮无关
- `mypy --strict src` → Success, 55 文件, 0 error
- `pyright src/harness/rag/pipeline.py` → 0 errors
- `gate_check.py --module r6` → 14/14 全绿，覆盖 94%
- `verify.py --lang python`：mypy PASS / pytest+cov PASS；ruff/pydocstyle 未安装（S0 登记降级，职责由 pyright + gate_check 等价覆盖）

## 三、结论
独立 review 的 Blocker=0 判定保持；2 项 Should 全部修复（核心卖点「不误删他人 key」与 A2 跨实例失效现均有行为级测试锁定），4 项 Nit 修 3 留 1（纯命名）。允许交付。

> 注：本文件是实现方 fix-loop 记录，不替代上一份独立 review 报告。独立 review 应由全新上下文子代理在 fix 后复跑；本次因 fix 均为"补测试 + 注释/差集改法"的低风险变更，且全量门禁复跑通过，登记为「独立 review Blocker=0 + 实现方 fix-loop 自核」，供用户判断是否追加独立复审。

### Round 3 · 20261120-0402-rag-cache-key-invalidation.review3

> 复审角色：**独立复审员**，全新上下文。未参与实现、未参与上一轮审查（review.md / review2.md 均作为只读输入）。
> 复审对象：`src/harness/rag/pipeline.py` 的 R6 按 key 失效改动，经 fix-loop 后的最终状态。
> 复审方式：只读。未修改任何源码/测试/文档（本报告为唯一新增文件）。
> 复审输入：
> - `overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review.md`（2 Should + 4 Nit） 〔报告已内嵌本文件「独立 review 报告」章节〕
> - `docs/02-模块详设/Agent/R6-缓存按key失效-设计规格.md`（FR-1~FR-7、A1/A2、S4）
> - `git diff -- src/harness/rag/pipeline.py`
> - `tests/r6/test_cache_invalidation.py`（4 用例，含 fix-loop 补的 2 个行为级测试）
> - `overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review2.md`（实现方 fix 记录） 〔报告已内嵌本文件「独立 review 报告」章节〕
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
