# R4 CacheEmbedder 默认实现 — 独立代码审查报告

- 审查员：独立审查（全新上下文，未参与实现）
- 日期：2026-11-20
- 意图源：`docs/02-模块详设/Agent/R4-CacheEmbedder默认实现-设计规格.md`（§1、§5 FR-1~FR-7、§13）
- 审查范围：`src/harness/model/cache.py`、`src/harness/model/__init__.py`、`tests/r4/test_cache_embedder_default.py`、`tests/tools/gate_check.py`，以及 4 处文档同步
- 实测环境：`.venv/Scripts/python.exe`（Python 3.13.14），直接 import 真实 `HashCacheEmbedder` / `SimilarityCache` / `_cosine`；探针脚本留存于 `tmp/r4_embedder_probe.py`、`tmp/r4_embedder_probe2.py`

## 结论

| 项 | 数量 / 结论 |
|---|---|
| Blocker | **1** |
| Should | 2 |
| Nit | 3 |
| 是否通过 | **不通过**（Blocker 未清零） |

一句话判定：协议补齐、哨兵区分、确定性、L2、跨进程一致、导出与门禁接线均正确；但**默认向量在中长文本上大面积误命中，且相对被替换的 difflib 路径显著退化**，与 FR-6"既有相似度行为不退化"直接冲突，且现有测试因只覆盖短文本而完全未暴露该问题。

### 核对概览

**接线四问**

1. 谁调用 `HashCacheEmbedder`：仅 `SimilarityCache.__init__`（`src/harness/model/cache.py:371`）无参默认构造；`src` 全仓无其他构造点，装配层默认仍为 `ExactCache`，故影响面限定为"显式使用 `SimilarityCache()` 无参构造的用户"。
2. `SimilarityCache` 如何消费向量：`_vector_of`（:382-384）→ `embed`；`put`（:417）写 `entry["vec"]`；`get`/`invalidate`（:398-401、:437-440）读 `tuple(entry["vec"])`，双方都有向量才走余弦，否则降级 difflib。链路完整。
3. `_cosine` 维度一致性（:234-241）：长度不等或零向量返回 0.0，保守不误命中；同实例内 `dim` 固定故一致；跨实例不同 `dim` 共享 store 会静默全 miss（见 Nit-3）。
4. 写入/读取口径一致：三处均用同一"有向量则余弦、否则 difflib"口径 ✓；旧 difflib 写入条目（`vec=None`）与新向量读取会降级 difflib，向后兼容 ✓。

**七维扫描**：正确性 ✗（见 Blocker-1、Should-2）｜错误处理 ✓（dim≤0 抛错、坏 JSON 跳过、零向量防御）｜并发 ✓（`embed` 纯函数无共享态）｜资源 ✓（无新依赖/网络/文件）｜性能 ✓（`embed` O(n)、`_cosine` O(64)，无新增瓶颈；但 O(n) bigram 数是 Blocker-1 的成因）｜安全 ✓（无外部输入执行）｜可维护性 ✗（docstring 与实现矛盾、测试语义脱节，见 Should/Nit）。

---

## Findings

### [Blocker-1] 默认向量对中长文本大规模误命中，且较 difflib 显著退化

**位置**：`src/harness/model/cache.py:188-220`（`_bigrams` + `_hash_bucket` + `embed`）；消费端 `src/harness/model/cache.py:382-401`。

**实测证据**（随机 ascii 不同文本，每组 200 对）：

| 文本长度 | bigram 数 | cosine 均值 | 最大 | threshold=0.85 下误命中 |
|---|---|---|---|---|
| 20 | ~21 | 0.2561 | 0.5217 | 0/200 |
| 100 | ~101 | 0.6164 | 0.7446 | 0/200 |
| 200 | ~201 | 0.7650 | 0.8635 | 1/200 |
| 500 | ~501 | 0.8899 | 0.9371 | **195/200** |
| 1000 | ~1001 | 0.9416 | 0.9662 | **200/200** |
| 2000 | ~2001 | 0.9695 | 0.9794 | **200/200** |

difflib 基线对照（12 对/档）：len=100 时 hash-cos 0.6487 / difflib 0.1525；len=300 时 0.8253 / 0.0039；len=600 时 **0.8992 / 0.0000**。即被替换掉的 difflib 几乎能完美区分中长文本，新默认向量却给出 0.82~0.90 的高相似度。

**根因**：`_hash_bucket` 仅 64 个桶，`embed`（:215）用 `+= 1.0` 累加 bigram 计数再 L2 归一。文本越长，bigram 数 O(n) 越大，各桶计数越趋近 `n/64`，归一化向量收敛到近似常量向量 `(1/8,…,1/8)`，于是**任意两段长文本的 cosine 都趋近 1.0**。这是哈希 trick 的结构性缺陷，不是参数问题。

**后果**：
- 语义缓存对长 prompt（>~300 字符，RAG/长描述/粘贴代码很常见）会静默命中**不相关历史响应**，返回错误答案；且无任何告警。
- 直接推翻设计规格 `FR-6`（`设计规格.md:54`）"既有相似度行为不退化"的验收声称，属于 L3 功能断点。
- 风险被测试盲区掩盖：`test_cache.py:67-75`、`test_cache_embedder_default.py:80-92` 的 near/distant 文本均 <30 字符，恰好落在"短文本尚安全"的区间。

**具体改法**（建议组合使用）：
1. 改累加为**二值/次线性**（如 `buckets[b] = 1.0` 或 `min(1.0 + math.log(cnt), K)`），阻断计数随长度线性膨胀导致的均匀化；
2. 给 `embed` 增加输入窗口上限（如 `max_chars=512`，超长取头尾窗口），从源头限制 bigram 数；
3. 在文档中明确"默认 embedder 仅适用于短查询；长查询须注入真向量模型"；
4. 补长文本负向测试（见 Should-1）。
   备选（更强但更复杂）：改用 minhash/simhash 等顺序敏感签名。不建议为规避此问题而动态调高长文本 threshold。

---

### [Should-1] FR-6 验证存在测试盲区：无"不相关长文本必须 miss"负向测试

**位置**：`tests/r4/test_cache_embedder_default.py:80-92`；`tests/r4/test_cache.py:67-75`。

**后果**：FR-6 只在短文本三档（exact/near/distant）上验证，而 Blocker-1 的缺陷恰好只在中长文本显现；回归防护形同虚设，后续任何人调参或换算法都无法拦截该退化。

**具体改法**：新增一条测试——两段完全不同主题、长度 ≥500 字符的文本，以 `threshold=0.85` 构造 `SimilarityCache()`，`put` 其一后 `get` 另一必须返回 `None`。把 FR-6 的验证矩阵从"短文本三档"扩展为"短/中/长三档"，长文本档以 difflib 结果为对照基线。

---

### [Should-2] 空文本产出单位向量而非零向量，与 docstring 及零向量分支矛盾

**位置**：`src/harness/model/cache.py:189-195`（`_bigrams`）、`src/harness/model/cache.py:210-219`（`embed` docstring 与注释）。

**实测**：`embed("")` 的向量有 1 个非零分量、L2 模长 = 1.0，并非 docstring 所写的"空文本返回零向量"。

**后果**：
- `_bigrams("")` 返回 `["^$"]`，计数恒为 1，故 `norm==0.0` 分支（:218-219）实际不可达——"零向量边界"从未被真正处理或测试，注释"空文本碰撞抵消"不成立。
- 边缘 false positive：两个空 query 的 cosine = 1.0，会互相命中。

**具体改法**：二选一并明示——(a) 若语义要求空文本为零向量，在 `embed` 开头加 `if not text: return (0.0,) * self.dim`；(b) 若接受单位向量语义，则修正 docstring/注释并删除或改写零向量分支。无论哪种，补 `test_embed_empty_text` 断言，覆盖 FR-2 提到的零向量边界。

---

### [Nit-1] `test_without_embedder_uses_text_similarity` 语义已脱节

**位置**：`tests/r4/test_cache_backends.py:115-119`。

**后果**：该用例用无参 `SimilarityCache(threshold=0.7)`，docstring 声称"不注入 embedder 仍用 difflib"，但当前无参默认已是 `HashCacheEmbedder`，实际测的是向量路径；真正的 difflib 降级未被它覆盖。

**具体改法**：把构造改为 `SimilarityCache(embedder=None, threshold=0.7)`，或删除该用例（已由 `test_cache_embedder_default.py:95-99` 的 `test_fr5_explicit_none_still_uses_difflib` 覆盖）。

---

### [Nit-2] FR-7"写入条目含向量"缺少直接断言

**位置**：`tests/r4/test_cache_backends.py:121-127`；`tests/r4/test_cache_embedder_default.py`（无覆盖）。

**后果**：`test_store_records_embeddings` 仅断言 `len(store.data)==1`，未验证 `vec` 字段内容与维度。实测写入条目确为 `['text','vec','resp']`、`vec` 长度 64、非零 14 个——功能正确，但 FR-7 的核心验收点没有自动化防护。

**具体改法**：追加断言 `entry["vec"] is not None and len(entry["vec"]) == DEFAULT_CACHE_EMBED_DIM and any(v for v in entry["vec"])`，并顺带断言 L2 模长 ≈ 1.0。

---

### [Nit-3] 设计规格 FR-6 所载实测数值与真实实现不符

**位置**：`docs/02-模块详设/Agent/R4-CacheEmbedder默认实现-设计规格.md:54`。

**后果**：规格写"near 0.886、distant 0.084"，真实实现实测为 near 0.9491、distant 0.4339（exact 1.0 一致）。验收结论虽不变，但作为调参依据的规格数值失真，易误导后续维护。

**具体改法**：用当前实现重测并更新数值，或改为只记结论（"near≥阈值命中、distant<阈值未命中"）而不写死浮点值。

---

## 未决与建议

1. **跨 embedder 维度混用**：实测 `dim=8` 的实例读取 `dim=64` 写入的条目时，`_cosine` 因长度不符返回 0.0、全部 miss（保守但静默）。建议在设计规格补充"同一 store 必须固定 embedder/dim"的约定；如需可观测性，可在维度不符时记 warning 或计数指标。
2. **默认适用边界需产品确认**：在 Blocker-1 修复前，建议至少在 R4 详设门禁符号表处加粗提示"默认 embedder 仅短查询";若长查询是主场景，建议重新评估"默认走向量"的决策（设计规格 U-3）是否应回退为 difflib、向量仅显式注入。
3. **探针脚本**：`tmp/r4_embedder_probe.py` / `tmp/r4_embedder_probe2.py` 为本次实测留存的可复现证据，非产品代码；若仓库不允许 `tmp` 下留临时脚本，可在修复 Blocker-1 并补测试后删除。
4. **未发现的问题**：FR-1/FR-3/FR-4/FR-5、NFR-1/NFR-2、门禁符号、4 处文档同步（协议审查:60、总设计:730、R13:110、R4 详设 209/391/430/494）、跨进程确定性（PYTHONHASHSEED=0/1/random 输出 sha256 完全一致）均通过核对。

---

## Fix loop 轮次 1（执行者修复后）

| 项 | 修复动作 | 证据 |
|---|---|---|
| Blocker-1 长文本误命中 | 算法从"64维计数累加"改为"512维二值化 + 512字符头尾窗口"；实测 dim=64/256/512 多方案对比后选定 512+二值+窗口，len≤2000 不相关文本误命中 0/100，相似度稳定 0.65 以下；对既有阈值仍满足（near 0.928≥0.7 / distant 0.188<0.9 / 中文近义 0.727≥0.7） | `test_fr6_long_unrelated_texts_do_not_collide`（600字符负向断言）；spec FR-2/U-4/§13 已更新 |
| Should-1 长文本负向测试缺失 | 新增 `test_fr6_long_unrelated_texts_do_not_collide` | 测试绿 |
| Should-2 空文本单位向量矛盾 | `embed` 开头 `if not text: return (0.0,)*dim`，docstring 同步 | `test_fr2_empty_text_is_zero_vector` |
| Nit-1 测试语义脱节 | `test_cache_backends.py:115` 构造改 `embedder=None` | 测试绿 |

**修复后验证**：R4 145 passed；全量 523 passed；mypy strict 0 error（55 文件）；pyright 0；gate_r4 13/13。

> 复审须新独立子代理，禁止复用本报告结论。
