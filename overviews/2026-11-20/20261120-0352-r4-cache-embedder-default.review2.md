# R4 CacheEmbedder 默认实现 — fix loop 后独立复审报告（review2）

- 复审员：独立复审（全新上下文，未参与实现，未参与上一轮审查；未复用上一轮结论）
- 日期：2026-11-20
- 意图源：`docs/02-模块详设/Agent/R4-CacheEmbedder默认实现-设计规格.md`（FR-2/FR-2算法约束/FR-3/U-4/§13 已记录修复）
- 上一轮报告：`overviews/2026-11-20/20261120-0352-r4-cache-embedder-default.review.md`（含 fix loop 轮次 1 记录）
- 复审范围：`src/harness/model/cache.py`、`src/harness/model/__init__.py`、`tests/r4/test_cache_embedder_default.py`、`tests/r4/test_cache.py`、`tests/r4/test_cache_backends.py`、`tests/tools/gate_check.py`，以及设计规格同步点
- 实测环境：项目 `.venv`，直接 import 真实 `HashCacheEmbedder` / `SimilarityCache` / `_cosine`；独立探针脚本留存 `tmp/r4_review2_probe.py`、`tmp/r4_review2_cn.py`、`tmp/r4_review2_compare.py`（只读验证，未改生产代码）
- 判定口径：误命中（false positive，返回错误答案）为危险缺陷；漏命中（false negative，退化为空 cache miss）为保守行为，不计阻断

## 结论

| 项 | 数量 / 结论 |
|---|---|
| Blocker | **0** |
| Should | 2 |
| Nit | 3 |
| 是否通过 | **通过**（Blocker-1 已真清零） |

一句话判定：Blocker-1 修复经独立实测确认有效——随机不相关长文本（len 300/600/1000/2000、ascii 与中文、cosine 与端到端）在 `threshold=0.85` 下误命中 **0/200×多组**，与旧 64 维计数累加算法（复现 200/200）形成数量级反差，相对 difflib 基线不再有危险退化；`dim=512`、`MAX_EMBED_CHARS=512`、二值化（分量 0 或 1/√k）、空文本零向量、dim 自定义、跨进程确定性、三档阈值、difflib 显式降级均核对通过。残留 2 Should / 3 Nit 均为已知取舍或测试/文档缺口，其中窗口中段盲区（Should-1）是本次修复引入的新边界，需记录但不足以阻断。

---

## Findings

### [已清零] Blocker-1 独立实测复核

**实测方法**（`tmp/r4_review2_probe.py`）：每组随机生成 200 对互不相干文本，分别计算字符 bigram 余弦与 `SimilarityCache(threshold=0.85)` 端到端 `put(A)/get(B)` 命中；并用同一批文本算 `difflib.SequenceMatcher` 基线。随机种子固定 20261120 可复现。

| len | hashcos ascii mean/max | FP@0.85 (ascii) | hashcos 中文 mean/max | FP@0.85 (中文) | difflib ascii mean/max | 端到端 FP@0.85 |
|---|---|---|---|---|---|---|
| 300 | 0.4829 / 0.5450 | **0/200** | 0.4463 / 0.5220 | **0/200** | 0.0035 / 0.0267 | **0/200** |
| 600 | 0.6555 / 0.7197 | **0/200** | 0.6343 / 0.6758 | **0/200** | 0.0001 / 0.0017 | **0/200** |
| 1000 | 0.6533 / 0.7002 | **0/200** | 0.6345 / 0.6899 | **0/200** | 0.0000 / 0.0030 | **0/200** |
| 2000 | 0.6535 / 0.7141 | **0/200** | 0.6314 / 0.6751 | **0/200** | 0.0000 / 0.0005 | **0/200** |

**修复前算法对照**（`tmp/r4_review2_compare.py` 复现"64 桶 + 计数累加 + L2"）：len=600 随机 ascii mean=0.9037 / max=0.9431 / **FP=200/200**，与上一轮报告（0.8992、195/200）量级一致，确认复现准确；修复后同条件 0/200。**结论：Blocker-1 真清零，且相对 difflib（几乎完美的区分能力）不再有危险退化**——hashcos 绝对相似度（~0.65）虽高于 difflib（~0），但在 0.85 阈值下二者均正确判 miss。

**维度/窗口/二值化核验**（`cache.py:43-45`、`:184-240`）：`DEFAULT_CACHE_EMBED_DIM=512`、`MAX_EMBED_CHARS=512`；非空文本向量长度 512，非零分量唯一取值 = 0.25 = 1/√16（k=命中桶数），L2 模长 1.0，全部分量属于 `{0, 1/√k}` ✓。跨进程确定性：PYTHONHASHSEED=0/1/random 三次运行向量 sha256 完全一致（`519b0d68…d58c9`）✓。

---

### [Should-1] 窗口截断的中段盲区：首尾相同、中间不同的长文本 100% 误命中

**位置**：`src/harness/model/cache.py:228-231`（`source[:half] + source[-half:]`）；设计规格 `FR-2` / §4（`MAX_EMBED_CHARS=512`）。

**实测**（`tmp/r4_review2_probe.py` §副作用 B）：构造两段长度 1012 的文本，前 256 字符完全相同、后 256 字符完全相同，仅中间 500 字符不同 → cosine = **1.0000**，`threshold=0.85` 误命中。相对地，仅头部不同的长文本 cosine = 0.7659 < 0.85，能正确区分。即窗口策略对"首尾差异"敏感、对"中段差异"完全失明。

**后果**：若长 prompt 的首尾模板固定、差异集中在中段（例如 RAG 场景中替换中间注入的检索文档，或长代码/长描述只在中间段落变化），两段语义不同的文本会得到完全相同的向量，语义缓存静默命中并返回错误的历史响应。风险低于旧 Blocker-1（旧算法对任意长文本误命中），但属于本次"取头尾各半"修复引入的新边界，设计规格已将其选为取舍但未记载该盲区。

**改法**：二选一——(a) 文档层：在 R4 详设/规格明确"默认 embedder 仅保证首尾差异的长文本可区分；中段差异长文本须注入真向量模型或降低 threshold"，并对长查询给出提示；(b) 算法层：把"取头尾"改为"全文均匀采样/分段哈希后拼接"，使中段也参与向量（需重新实测不相关文本误命中率与 REGRESSION 阈值）。不阻断合入，但应登记为已知限制。

---

### [Should-2] 中文近义召回在 threshold=0.7 下 0/10 命中，规格所载验收数值不可复现

**位置**：设计规格 `docs/02-模块详设/Agent/R4-CacheEmbedder默认实现-设计规格.md:52`（FR-2 算法约束声称"中文近义 0.727≥0.7"）；实现 `src/harness/model/cache.py:197-240`（字符 bigram 哈希）。

**实测**（`tmp/r4_review2_cn.py`，10 组常见中文近义/改写对）：threshold=0.7 下命中 **0/10**，例如"机器学习神经网络"vs"深度学习神经元网络"=0.6325、"如何烘焙面包"vs"怎样制作面包"=0.2857、"总结这篇文章"vs"帮我概括这段文字"=0.0000。复核"修复前后对比"（`tmp/r4_review2_compare.py`）：旧 64 维计数累加算法在同样文本对上同样基本 <0.7（0.6674/0.5040/0.6396/0.5000/0.2857/0.0000）。

**后果**：默认 embedder 对中文近义查询几乎只做字面 bigram 重叠匹配，非真语义（设计规格 §6 方案 A 取舍已承认"同义不等价"）。这是**固有局限而非本次修复引入的退化**——修复后 distant 相似度从旧算法 0.4339 量级降到 0.1879，行为更保守，漏命中只会退化为 cache miss，不会返回错误答案。真正的问题在规格：把单组特定文本（0.727）写为修复验收证据，会误导后续维护者相信中文近义默认可用。

**改法**：把规格 FR-2 算法约束的"中文近义 0.727≥0.7"改为结论式表述（如"中文近义受字符 bigram 字面重叠限制，默认阈值下通常 miss；需真语义模型注入或调低 threshold"），或注明该数值对应的具体文本对，避免以特例代一般。不阻断合入。

---

### [Nit-1] 规格 FR-6 数值未同步，与 FR-2 算法约束自相矛盾

**位置**：`docs/02-模块详设/Agent/R4-CacheEmbedder默认实现-设计规格.md:56`（FR-6）。

**实测**：FR-6 仍写"near 0.886≥0.7、distant 0.084<0.9、exact 1.0"，而本次实测为 near=0.9285 / distant=0.1879 / exact=1.0；FR-2 算法约束（:52）已更新为"near 0.928 / distant 0.188"，与实测吻合。即规格内部同一指标两套数值，FR-6 是上一轮 Nit-3 未修的残留。

**后果**：调参依据失真、规格自相矛盾，削弱验收文档可信度（验收结论本身不变）。

**改法**：把 FR-6 数值改为与 FR-2 一致的 0.928/0.188/1.0，或统一改为只记结论"near≥0.7 命中 / distant<0.9 未命中 / exact=1.0"而不写浮点值。

---

### [Nit-2] FR-7"写入条目含向量"仍缺直接断言（上一轮 Nit-2 未修）

**位置**：`tests/r4/test_cache_backends.py:121-126`。

**实测**：`test_store_records_embeddings` 仍仅 `assert len(store.data) == 1`，未验证 `vec` 字段内容与维度。经探针确认功能正确（写入条目 keys=['text','vec','resp']，`vec` 长度 512、非零 15 个），但核心验收点无自动化防护。

**后果**：后续若有人误改 `put` 逻辑导致 `vec=None` 或维度错误，现有测试无法拦截。

**改法**：追加 `entry=json.loads(...)` 后断言 `entry["vec"] is not None and len(entry["vec"])==DEFAULT_CACHE_EMBED_DIM and any(entry["vec"])`，并断言 L2 模长≈1.0。

---

### [Nit-3] 长文本负向回归测试仅用一对固定构造文本，覆盖强度偏弱

**位置**：`tests/r4/test_cache_embedder_default.py:105-112`（`test_fr6_long_unrelated_texts_do_not_collide`）。

**实测/判断**：Should-1 要求的负向测试已新增，但只测一对重复词根文本（`"向量召回…" *40` vs `"烘焙面包…" *40`，各 600 字符）。这类文本 bigram 种类少、周期性强，属于相对"容易"的负例；本次独立实测表明随机 ascii/中文在 len 300~2000 下误命中 0/200，防线其实有更大余量，但测试本身未覆盖随机文本与中文长文本。

**后果**：回归防护覆盖面窄，无法拦截"仅特定文本形态安全"的算法退化。

**改法**：把该用例参数化，加入随机 ascii、随机中文、len 1000/2000 等组合（可固定随机种子），并保留 difflib 对照。

---

### 逐条核上一轮修复项（fix loop 轮次 1）

| 项 | 是否真修到位 | 证据 |
|---|---|---|
| Blocker-1 长文本误命中 | **是** | 独立实测 0/200×多组（见上）；旧算法复现 200/200 |
| Should-1 长文本负向测试 | **是** | `test_cache_embedder_default.py:105-112` 新增负向断言（强度见 Nit-3） |
| Should-2 空文本单位向量矛盾 | **是** | `cache.py:224-225` 开头 `if not text: return (0.0,)*dim`；docstring 同步；`test_fr2_empty_text_is_zero_vector`（:100-102）；端到端两空 query 实测 miss ✓ |
| Nit-1 测试语义脱节 | **是** | `test_cache_backends.py:115-119` 构造改为 `SimilarityCache(embedder=None, threshold=0.7)`，docstring 同步 |
| （上一轮 Nit-2 FR-7 断言） | 否 | 见本轮 Nit-2 |
| （上一轮 Nit-3 规格数值） | 部分 | FR-2 已更新，FR-6 未更新，见本轮 Nit-1 |

### 既有行为与副作用核对（复审要求 2/3）

| 核对项 | 实测结果 |
|---|---|
| exact | cos=1.0000 ≥0.85 命中 ✓ |
| near | cos=0.9285 ≥0.7 命中 ✓ |
| distant | cos=0.1879 <0.9 未命中 ✓（较旧算法 0.4339 更保守） |
| 中文近义 | threshold=0.7 下 0/10（固有局限，见 Should-2；非退化） |
| difflib 显式降级 | `SimilarityCache(embedder=None, threshold=0.7)` near 命中 ✓；`_embedder is None` ✓ |
| 512 维存储成本 | 单条 JSON 2981 bytes（旧 64 维约 373 bytes，约 8×）；无新依赖/网络/文件 ✓ |
| 窗口截断首尾敏感 | 中段不同 100% 误命中（见 Should-1）；首尾不同能区分 ✓ |
| 空文本零向量 | `embed("")` 全零、长度 512；两空 query 不互相命中 ✓ |
| dim 自定义 | dim=8/64/128/512 长度与二值形态均正确；dim=0 抛 ValueError ✓ |
| 跨 embedder 维度混用 | dim=512 写 / dim=8 读 → 全 miss（保守，无静默错误） ✓ |

### 七维扫描

正确性 △（Blocker-1 清零；但窗口中段盲区见 Should-1，中文近义弱见 Should-2）｜错误处理 ✓（dim≤0 ValueError、坏 JSON 跳过、零向量防御、threshold 范围校验；实测空文本/跨维均保守）｜并发 ✓（`embed` 纯函数、无共享可变态）｜资源 △（512 维使单条 JSON 约 8× 膨胀，属 dim=512 设计的预期代价；无新依赖/网络/文件）｜性能 ✓（`embed` O(n) 且窗口封顶 512 字符，`_cosine` O(512)，无新瓶颈）｜安全 ✓（无外部输入执行，不可信文本仅做字符串哈希）｜可维护性 △（规格 FR-6 数值失真、FR-7 测试缺断言、中文近义验收声称不可复现）。

### 接线四问复核

1. **谁调用 `HashCacheEmbedder`**：仅 `SimilarityCache.__init__`（`cache.py:390-393`）在无参/未传时默认构造；`src` 全仓无其他构造点。装配层默认仍为 `ExactCache`，影响面限定为"显式使用 `SimilarityCache()` 且未传 embedder 的用户"。
2. **如何消费向量**：`_vector_of`（`:402-404`）→ `embed`；`put`（`:431-446`）写 `entry["vec"]`；`get`/`invalidate`（`:406-429`、`:448-462`）读 `tuple(entry["vec"])`，双方都有向量走余弦，否则 difflib。链路完整 ✓。
3. **维度一致性**：`_cosine`（`:254-261`）对长度不等或零向量返回 0.0，保守不误命中；同实例 dim 固定故一致；跨实例不同 dim 共享 store 静默全 miss（实测确认，延续上一轮未决项）。
4. **写入/读取口径一致**：`get`/`put`/`invalidate` 三处均为"有向量则余弦、否则 difflib" ✓；旧 difflib 条目（`vec=None`）与新向量读取降级 difflib，向后兼容 ✓。

### 门禁与回归证据

`pytest tests/r4 -q` 全绿（145 passed）；`gate_check.py --module r4` **13/13 PASS**（G0 公共 API 19 符号含 `HashCacheEmbedder`、G1 覆盖率 88% / mypy strict 0 error / 语法编译 / docstring、G2 行为断言、G3 上下游契约与 demo、G4 文档八要素与版本登记）；跨进程确定性 sha256 三 seed 一致。

---

## 未决与建议

1. **窗口中段盲区（Should-1）需产品确认适用边界**：建议在 R4 详设补充"默认 embedder 对超长文本仅保证首尾差异可区分"的明确声明；若长 RAG 上下文替换是主场景，应优先推进算法层均匀采样/分段哈希，或在装配层对长查询注入真向量模型。
2. **中文近义能力边界（Should-2）需在文档如实标注**：默认字符 bigram 不做真语义等价，中文近义改写基本 miss；建议文档明示并给出"调低 threshold 仍有限 / 注入真向量模型"两条出路，同时修正规格 0.727 的特例表述。
3. **存储成本容量评估**：单条向量 JSON 由旧 64 维约 373 B 增至 512 维约 2981 B（约 8×）。InMemory 默认无容量上限，若缓存条目量大或换 Redis/DB，需评估内存/带宽与 TTL 策略；必要时提供"维度可配（已有 `dim` 参数）+ 长文本降级"的容量开关。
4. **跨 embedder 维度混用**：实测 dim=512 写入 / dim=8 读取全 miss（保守）。建议在设计规格补充"同一 store 必须固定 embedder 与 dim"的约定；如需可观测性，可在维度不符时记 warning 或计数指标。
5. **临时探针脚本**：`tmp/r4_review2_probe.py`、`tmp/r4_review2_cn.py`、`tmp/r4_review2_compare.py` 为本次独立实测留存的可复现证据，非产品代码；若仓库不允许 `tmp` 下留临时脚本，可在归档本报告后删除。
6. **未发现问题的部分**：FR-1（协议履约）、FR-3（非法 dim 抛错/自定义生效）、FR-4（包导出）、FR-5（哨兵区分默认与显式 None）、NFR-1（无新依赖、无 I/O、grep import 扫描）、NFR-2（门禁符号）、跨进程确定性、三档阈值、空文本零向量、difflib 降级全部核对通过。
