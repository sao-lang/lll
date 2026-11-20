# R4 CacheEmbedder 默认实现 · 设计规格（补充设计）

> 派生自 `overviews/2026-11-20/20261120-0352-r4-cache-embedder-default.md`（用户拍板 3 决策 + 两轮独立 review Blocker 清零）。主详设：`R4-模型与推理.md` §5 CachePort/SimilarityCache。状态：已实现（tests/r4/test_cache_embedder_default.py）。

## 1. 背景与问题
- `CacheEmbedder`（cache.py）原本只有 Protocol、全 src 零实现类；`SimilarityCache` 缺省用 difflib 旁路顶替"默认"名分；测试用 FakeEmbedder。
- 四要素（协议+默认+注入入口+装配）缺"默认"——向量相似度要用户自写 embedder。
- 纠正：R4 详设/总设计/协议审查/R13 中"difflib 是默认"的错误表述已在本次同步修订。

## 2. 用户拍板的 3 项决策
| 项 | 决策 |
|---|---|
| 默认算法 | 字符 bigram 哈希向量，定长 512 维（初版 64 维，review 后改），L2 归一，二值化 + 512 字符头尾窗口 |
| 默认值切换 | `SimilarityCache` 无参默认改用向量 embedder；difflib 降级为显式 `embedder=None`（哨兵区分未传/显式 None） |
| 类位置/命名 | `cache.py` 内 `HashCacheEmbedder` + `__all__` + `model/__init__.py` 导出 |

## 3. 功能需求
- **FR-1** 具名常量：维度 512、窗口 512 字符（头尾各 256），禁魔法值。
- **FR-2** `HashCacheEmbedder`：字符 bigram 哈希 → 512 维二值向量 → L2 归一；禁进程随机 `hash()`（用确定性哈希）。
- **FR-3** `_Unset` 哨兵：区分"未传 embedder"（用默认 HashCacheEmbedder）与"显式 embedder=None"（difflib 兼容路径）。
- **FR-4** `SimilarityCache` 缺省注入 `HashCacheEmbedder`。
- **FR-5** 导出：`cache.py.__all__` + `model/__init__.py` 导出 `HashCacheEmbedder`。
- **FR-6** 规格数值用结论式表述（不写死易失同步的特例浮点数值）。
- **FR-7** 默认 embedder 的向量必须真的被持久化用于相似度（`test_fr7_default_embedder_vector_persisted` 直接断言）。

## 4. 非功能需求
- **NFR-1** 中文友好：字符 bigram 对中文有效。
- **NFR-2** 零依赖、确定性、L2 归一。
- **NFR-3** 长文本不退化：len ≤2000 不相关文本误命中 0/100（实测）。

## 5. 算法选型实测（Blocker 修复依据）
- 初版 64 维计数累加：长文本（len=600）向量收敛常量，195/200 不相关文本误命中，较 difflib 显著退化。
- 实测多方案（count/binary/log × 维数 × 窗口）后选定 **512 维二值化 + 512 字符头尾窗口**：len ≤2000 误命中 0/100，对现有英文测试阈值仍满足（near≥0.7 / distant<0.9 / exact=1.0）。
- 二值化单独用更糟（长文本覆盖所有桶→cosine=1.0），必须高维 + 窗口 + 二值化三者组合。

## 6. 已知限制（登记，非本次引入）
- **窗口中段盲区**：超长文本取头尾各 256 字符，"首尾相同仅中段不同"的长文本会误命中。需中段差异的长查询注入真向量模型或降 threshold。

## 7. 变更记录
- v1.0（2026-11-20）：3 决策 + FR-1~7 + NFR-1~3。两轮独立 review：轮1 Blocker-1（长文本收敛退化）→ 512维二值化+窗口修复；轮2 规格数值失同步/中文近义数值/FR-7 直接断言/长文本负向测试均修，中段盲区登记为已知限制。Blocker 清零。
