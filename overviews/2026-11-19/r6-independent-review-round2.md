# R6（RAG 检索增强）交付前独立复审报告 · Round 2

- 复审日期：2026-11-19
- 复审方式：全新上下文独立子代理，未阅读任何既有 review 报告；结论全部来自源码通读 + 自写只读实验
- 复审范围：3 个已知问题的真实修复状态、新增 Blocker 扫描、回归测试质量抽查、设计文档 v2.1 覆盖状态核对
- 环境：Python 3.13.14 / `.venv`；所有实验 `PYTHONPATH=src`，只读，未改任何源码

---

## 一、结论速览

| 编号 | 问题 | 判定 | 关键证据 |
|------|------|------|----------|
| 1 | 检索语义断点：query 是否用注入的同一 embedder | **PASS** | CountingEmbedder：ingest 后 calls=1，query 后 calls=2，query 入参=`['alpha beta']` 确系注入器收到 |
| 2 | 权限过滤绕过：双通道 + 后处理是否都施加 filters | **PASS** | public/secret 对照实验：无 filters 时 secret 入 hits；加 filters 后 secret 在向量通道/关键词通道/后处理三层均被剔除，context 不含 secret |
| 3 | 死注入点 detector：pipeline.detector 是否生效 | **PASS** | 恒 INJECTED 检测器：detect 被调 1 次，empty=True，hits=[]，context 为固定空提示 |

- **新增 Blocker：无**（详见第四节；若干 Nit/Should 不阻塞）
- **是否可合入：可以合入**。三问题全 PASS，无新增 Blocker，三条验证命令退出码均为 0。

---

## 二、三个实验的实际观察值

### 实验 1：查询 embedder 一致性（问题 1）

自写 `CountingEmbedder`（继承确定性哈希逻辑，累计 `calls` 与每次 `embed` 的入参文本），注入 `RAGPipeline`，ingest 一个文档后 query 一次。

代码要点：
```python
emb = CountingEmbedder()
p = RAGPipeline(..., embedder=emb, store=store, retriever=VectorRetriever(store), post=DefaultPostProcessor())
await p.ingest([Document(doc_id="d1", text="alpha beta gamma delta")], pctx())
r = await p.query(Query(text="alpha beta", top_k=2))
```
源码对照：`pipeline.py:165`（ingest 用 `self.embedder.embed`）、`pipeline.py:216`（query 用 `self.embedder.embed([sub.text for sub in queries])`）——两处均为构造时注入的同一 `self.embedder`。

**实际输出：**
```
ingest 前 calls = 0
ingest 返回块数 = 1 | ingest 后 calls = 1
ingest 的 embed 入参 = ['alpha beta gamma delta']
query 后 calls = 2
query 的 embed 入参 = ['alpha beta']
hits 数 = 1  empty = False
判定 calls==2 ? True => PASS
```
分析：查询文本 `alpha beta` 确实出现在注入器的 `arg_log[1]`，说明查询向量由注入 embedder 计算，而非内部另起 HashEmbedder。文档注释 `retriever.py:3-5`/`pipeline.py:215` 所述 B1 修复属实。

### 实验 2：权限过滤（问题 2）

两块均含 `alpha beta`，保证若不过滤会被双通道召回。先做无 filters 对照组，再用 `Query(filters={"clearance":"public"})` 走完整 `pipeline.query()`，并单独抽查 `matches_filters` 与 `PostProcessor.filter`。

代码要点：
```python
docs = [
  Document(doc_id="pub", text="alpha beta public content", meta={"clearance":"public"}),
  Document(doc_id="sec", text="alpha beta secret content", meta={"clearance":"secret"}),
]
p = RAGPipeline(..., retriever=HybridRetriever(store), post=DefaultPostProcessor())
r = await p.query(Query(text="alpha beta", top_k=5, filters={"clearance":"public"}))
kept = await post.filter(all_hits, filters={"clearance":"public"})
```
源码对照：`retriever.py:78`（向量通道 `store.search(..., query.filters)`）、`retriever.py:103`（关键词通道 `_keyword_search` 内 `matches_filters(chunk, query.filters)`）、`post.py:90`（`filter` 内 `matches_filters(hit.chunk, filters)`）——三层均施加 filters。

**实际输出：**
```
[对照] 无 filters 的 citations = ['pub:0', 'sec:0']
[对照] secret 是否出现 = True
[实验] filters = {'clearance': 'public'}
[实验] hits 的 doc_id = ['pub']
[实验] citations = ['pub:0']
[实验] context 片段 = 'alpha beta public content [pub:0]'
[实验] context 含 'secret' 字样 = False
secret 在 hits = False | secret 在 context/citations = False
[抽查] corpus 全块 = ['pub:0', 'sec:0']
[抽查] matches_filters(public) 通过的块 = ['pub:0']
[抽查] PostProcessor.filter(public) 保留 = ['pub']
判定 全链路过滤 secret 且保留 public ? True => PASS
```
分析：对照组证明 secret 块在无过滤时确实会被召回（测试非假绿）；加 filters 后 secret 在向量通道、关键词通道、后处理兜底三层均被拦截。B2 修复属实。`PostProcessor.filter` 不只做注入检测，确实做了 meta/权限过滤（`post.py:90`）。

### 实验 3：自定义 detector 注入（问题 3）

自写 `AlwaysInjected.detect()` 恒返回 `InjectionVerdict.INJECTED` 并计数，注入 `RAGPipeline(detector=...)`；另建默认检测器流水线作对照。

代码要点：
```python
class AlwaysInjected:
    def detect(self, text): self.called += 1; return InjectionVerdict.INJECTED
p = RAGPipeline(..., post=DefaultPostProcessor(), detector=det)
r = await p.query(Query(text="clean normal", top_k=2))
```
源码对照：`pipeline.py:227`（query 把 `detector=self.detector` 传入 `post.assemble`）、`post.py:87`（`used = detector if detector is not None else self._detector`）、`post.py:92-94`（INJECTED 即剔除）。链路完整。

**实际输出：**
```
[对照] 默认检测器 hits = 1 empty = False
[实验] 自定义 detector.detect 调用次数 = 1
[实验] hits = []  empty = True  citations = []
[实验] context = '未检索到相关文档，请依据常识谨慎作答或拒绝回答。'
判定 empty=True 且 hits=[] 且 detector 被调 ? True => PASS
```
分析：自定义检测器被真实调用且使结果 fail-closed 为空。B3 修复属实。

---

## 三、附带检查

### 3.1 设计文档 v2.1「实现覆盖状态」是否如实

核对 `docs/02-模块详设/Agent/R6-RAG检索增强.md:115-121`：

- ✅ 完整实现（向量化、RRF、预算拼接、引用溯源、乱序修复、空结果降级、注入检测接入、查询/入库向量同源）——经实验 1/2/3 与源码逐项确认，属实。
- 🟡 简化/近似实现（切分、存储点积+meta 精确匹配、词面覆盖关键词通道、词面重排、多查询复制改写、精确缓存）——与源码一致，属实。
- ⚪ 未实现/归其他模块（查询路由、MMR、权限/时效**语义判定**、自检索、评测、增量去重）——文档明确承认「当前仅做 meta 键值精确匹配与注入检测，复杂时效/权限规则归 R9」，与 `matches_filters` 只做 `chunk.meta.get(key)==value` 精确匹配的现实一致，**没有把未实现说成已实现**。
- 红线保障四项（引用溯源/预算不超/空结果确定/注入 fail-closed）——均真实实现且有测试。
- 结论：覆盖状态一节如实；仅发现两处**非阻塞数字/措辞瑕疵**（见第四节 Nit-4、Nit-5）。

### 3.2 gate_check 门禁真实性

`tests/tools/gate_check.py` 为真实执行门禁（调用 pytest/coverage/mypy 并读 returncode 与覆盖率行，`gate_check.py:178/191-202`），非假绿。实测 14/14 全绿，覆盖率 94%。

---

## 四、新增 Blocker

**无。** 独立通读 `src/harness/rag/` 全部 10 个源文件，未发现新的功能断点、安全红线突破或语义错误。若干非阻塞观察（Nit/Should）记录如下，不阻塞合入：

- **Should-1（既有设计边界，非新增）** `pipeline.py:266-270` 缓存键只含 `text/top_k/token_budget/purpose/filters`，不含 `embedder/detector/rewrite/rerank` 配置；而 `with_`（`pipeline.py:246/255`）按 S1 设计**共享同一 CachePort 实例**（`test_wiring.py:77` 明确断言 `q._cache is cache`）。因此用 `with_` 更换 detector/embedder 后的变体，可能对同一 query 命中旧配置缓存。属既有设计权衡，建议后续在缓存键纳入配置摘要或在 `with_` 中新建空缓存，**不作为本次 Blocker**。
- **Nit-1** `pipeline.py:246` 注释「缓存内容不迁移（新实例空缓存）」与 `pipeline.py:255` 实际传 `self._cache`（同一实例、内容共享）矛盾；行为经测试确认是有意为之，仅注释误导。
- **Nit-2** `store.py:62-64` `upsert` docstring 称「写入块副本」，实际 `self._chunks.extend(chunks)` 存引用；流水线内块由 splitter 现造、无外部改写，故无实际危害。
- **Nit-3** `test_components.py:99` docstring 写「空 corpus 降级为向量」，但 `test_components.py:109` 实际传了 `all_chunks=[c]`（非空），措辞与用例不符（不影响断言有效性）。
- **Nit-4** 设计文档 `R6-RAG检索增强.md:3` 与 `AGENT-总设计.md:1634` 声称覆盖率 95%，门禁实测 94%，数字略有出入。
- **Nit-5** 设计文档 `R6-RAG检索增强.md:121` 措辞「detector=None 时由装配层注入默认检测器」，实际由 `DefaultPostProcessor.__init__`（`post.py:67-69`）内置 `KeywordInjectionDetector` 兜底；`test_injection.py:36-44` 确认缺席即 fail-closed，语义无误仅措辞偏差。

---

## 五、测试质量抽查

### 5.1 `tests/r6/test_wiring.py`（重点）

6 个测试全部为**真断言**，无一恒真/假绿：

| 测试 | 覆盖项 | 断言要点 | 评价 |
|------|--------|----------|------|
| `test_query_uses_injected_embedder` | 问题1/B1 | `embedder.calls == 2` | 真断言，且能区分新旧实现（旧实现只会是 1） |
| `test_pipeline_detector_takes_effect` | 问题3/B3 | `out.empty is True and out.hits == []` | 真断言，能区分接线/未接线 |
| `test_with_preserves_all_injection_points` | S1 | `q.rewrite is rw and q.rerank is rr and q._cache is cache` 等 | 真断言，身份比较有效 |
| `test_cache_key_includes_filters_and_ingest_invalidates` | S4 | `n_before >= 2`、`n_after == 0` | 真断言，覆盖 filters 入键与 ingest 失效 |
| `test_custom_cache_object_is_used` | S5 | `p._cache is cache`、`len(cache._data)==1` | 真断言 |
| `test_multi_query_dedup` | S7 | `len(cites)==len(set(cites))` | 真断言 |

**覆盖缺口说明**：`test_wiring.py` 断言了问题 1、3 及 S1/S4/S5/S7，但**未包含问题 2（权限过滤）**——该文件 docstring 也只列 B1/B3/S1/S4/S5/S7。问题 2 由 `tests/r6/test_hybrid.py:68-75::test_keyword_channel_respects_filters` 独立覆盖（`assert all(... == "public")` + `assert all(doc_id != "d3")`），属真断言。因此三问题在整个 `tests/r6/` 套件中均有回归保护，只是不全集中在 `test_wiring.py`。此为组织性观察，非缺陷。

### 5.2 其余测试文件

抽查 `test_hybrid.py`/`test_injection.py`/`test_pipeline.py`/`test_cache.py`/`test_budget.py`/`test_fallback.py`/`test_components.py`：均为真断言，无 `assert True`、`pass`-only 或恒等式假绿。

- 唯一**弱断言**（Should，非阻塞）：`test_hybrid.py:64` `assert "d3" in hybrid_ids or len(hybrid_ids) >= len(vector_ids)`——因混合召回通道数通常 ≥ 纯向量，`len(...) >= ...` 分支几乎恒真，该断言约束力偏弱；但同文件 `:36-45/68-75` 有其他强断言兜底，不影响 B2 保护。
- `test_fallback.py:14-27`、`test_budget.py:19-37` 的空结果/预算边界断言真实有效。

---

## 六、验证命令退出码

```
$ PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/r6 -q
...........................                                              [100%]   # 27 passed
PYTEST_EXIT=0

$ PYTHONPATH=src .venv/Scripts/python.exe -m mypy --strict src/harness/rag
Success: no issues found in 10 source files
MYPY_EXIT=0

$ PYTHONPATH=src .venv/Scripts/python.exe tests/tools/gate_check.py --module r6
=== 结果：14/14 项通过 ===
R6 门禁全绿：允许合入。
GATE_EXIT=0
```

（说明：题目要求命令未带 PYTHONPATH，本仓为 src 布局且未启用可编辑安装，裸命令会 `ModuleNotFoundError: No module named 'harness'`；故三条命令均显式加 `PYTHONPATH=src`，属环境路径设置，不改变被测对象。）

---

## 七、最终判定

- 问题 1（embedder 一致性）：**PASS**
- 问题 2（权限过滤）：**PASS**
- 问题 3（detector 注入）：**PASS**
- 新增 Blocker：**无**（Nit/Should 见第四节，不阻塞）
- 合入结论：**可以合入 R6**
