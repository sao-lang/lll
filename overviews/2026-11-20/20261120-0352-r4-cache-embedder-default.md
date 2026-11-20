# 会话摘要：补 R4 CacheEmbedder 默认实现

## L0 索引卡（续接时先读这一段，≤12 行）
- 场景 / 路径：链 ①场景3 补充设计（文档）→ 闸门 → ②场景6 设计驱动重构（改码） / 改码
- 当前闸门：设计闸门已过（用户拍板3决策+spec已写+4份文档已同步）；代码门禁未过
- 交付产物：docs/02-模块详设/Agent/R4-CacheEmbedder默认实现-设计规格.md；R4详设(4处)/总设计§728/协议审查§60/R13§110 已同步
- 未完成项：①HashCacheEmbedder 未实现 ②SimilarityCache 默认未改 ③测试/门禁/review 未动
- 下一步：S3 TDD（读编码规则→写失败测试→实现→门禁）

## 环境
- 日期时间：2026-11-20 03:52 GMT+8
- 项目：AgentKit (lll) / 工作区：E:/vsc-workspace/lania-zip/lll
- OS / Shell：Windows / Git Bash
- 运行时：Python 3.13（项目 .venv 含 pytest/mypy/pyright，验证一律用 .venv）
- IDE / Agent：WorkBuddy / conductor 主控

## 场景与 skill
- 场景：3 补充设计 → 6 设计驱动重构（用户要求先改设计后改代码）
- 加载的 skill：conductor（主控）；S2 grill-design；S3 coding-rules-python
- 主控：conductor

## 用户诉求
- "R4 的 CacheEmbedder 好像没有默认实现，你仔细看下，如果真的没有，继续按照 conductor 先改设计方案，再改代码"

## S0 事实调查（只读，已确认）
- `CacheEmbedder`（src/harness/model/cache.py:144）仅有 Protocol，全 src 无任何实现类（grep `class .*(CacheEmbedder)` 零命中）。
- `SimilarityCache` 缺省用 difflib.SequenceMatcher 文本相似度（cache.py:261/314），开箱即用，但**绕过协议**：`if self._embedder is not None: 余弦 else: difflib`。
- 测试用的是 `FakeEmbedder`（tests/r4/test_cache_backends.py:48），非框架默认件。
- 文档把 difflib 当成默认："协议可插拔完备性审查:60 默认 InMemory/difflib"、"R4 详设:390/429 缺省 difflib/无(difflib)"、"R13:110 ExactCache+SimilarityCache/difflib"。
- **定性**：可插拔四要素（协议+默认+注入入口+装配注册）中"默认实现"缺失——与 R5 反思的"协议+门面+fake 是最危险的假完成形态"同类。功能可用但纪律违背，且用户要向量相似度必须自己写 embedder，门槛不合理。与上一轮 R6 改写器"占位不算真实现"同源问题。

## L1 摘要（需要背景时读）
### 会话摘要
1. 协议存在、默认缺失、difflib 旁路兜底——需补一个零依赖的确定性默认 embedder。
2. 补法候选：字符/词 n-gram 哈希向量（类似 R6 HashEmbedder）/ 词袋 one-hot / 复用 R6 HashEmbedder（跨模块依赖需评估）。
3. 必须同步文档：R4 详设 §209/390/429、总设计 §728、协议审查 §60、R13 §110（把"difflib 是默认 embedder"改为"默认 X embedder + difflib 是无向量时的降级路径"）。
4. 影响面：cache.py、model/__init__.py 导出、tests/r4/test_cache_backends.py、gate_check r4 符号清单、4 份文档。

### 关键决策与理由（grill-design 收尾，用户拍板）
- 默认算法：字符 n-gram（bigram）哈希向量，定长 64 维，L2 归一 — 中文友好、零依赖、确定性（禁进程随机 hash()）
- 默认路径：SimilarityCache() 无参改用默认 embedder 走向量余弦；difflib 降级为显式 embedder=None（用户选"默认改用向量"）
- 类位置/命名：cache.py 内 HashCacheEmbedder + __all__ + model/__init__ 导出
- 事实（自查）：R6 HashEmbedder 签名 list[str]→list[Vector] 与本协议 str→Vector 不符，且 R4→R6 跨模块依赖破坏分层 → 否决复用，R4 独立实现
- 实测（自查）：字符 bigram 余弦对现有测试阈值全满足（near 0.886≥0.7 / distant 0.084<0.9 / exact 1.0）→ 默认改用向量不破坏既有测试
- 明示默认：dim 非法值抛 ValueError（与 RecursiveSplitter 一致）；difflib 路径保留为显式降级

### 验证结果（S3 已跑，S4 复核中）
- 测试：`PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/r4` — 143 passed；全量 r1..r6+ports — 519 passed
- 覆盖率：gate_r4 G1 — 88%（阈值 85%）
- mypy strict：0 error（55 文件）；pyright：0 error
- gate_r4：13/13 全绿（G0 19 符号含 HashCacheEmbedder；G2 56 测试含新测试文件）
- 独立 review：待 S5 派全新子代理

### 未完成项与下一步（完整）
- S1 路由 → S2 设计 → S3 TDD → S4 自省 → S5 独立 review + 收尾。
- suggested skills：grill-design（S2）、coding-rules-python（S3）。

## 独立 review 结果（S5 门禁证据）
- **Blocker = 0，无阻塞项，允许交付。**
- 独立审查报告（轮1，1 Blocker）：`20261120-0352-r4-cache-embedder-default.review.md`
- 独立复审报告（轮2，Blocker=0 通过，2 Should + 3 Nit）：`20261120-0352-r4-cache-embedder-default.review2.md`
- 轮1 Blocker-1（64维计数累加致长文本 100% 误命中，较 difflib 退化）已修：改为 512维二值化 + 512字符头尾窗口；独立复审实测 len 300~2000 中英文不相关文本误命中 0/200（旧算法复现 200/200），Blocker 真清零。
- 轮2 残留处理：Should-2（中文近义数值误导）/ Nit-1（FR-6 数值失同步）→ 规格改为实测一致的结论式表述；Nit-2（FR-7 缺直接断言）→ 新增 `test_fr7_default_embedder_vector_persisted`；Nit-3（长文本负向测试弱）→ 参数化 5 组（重复中文/随机ascii/随机中文 × 多长度）。
- **登记为已知限制（未改代码，非本次引入）**：Should-1 窗口中段盲区——超长文本取头尾各256字符，"首尾相同仅中段不同"的长文本会误命中；已在规格「已知限制」节登记，须中段差异的长查询注入真向量模型或降 threshold。
- 复审独立性：两轮均全新上下文子代理；fix loop 未复用旧报告结论。

## L2 详情引用
- 独立审查报告：`20261120-0352-r4-cache-embedder-default.review.md`
- 独立复审报告：`20261120-0352-r4-cache-embedder-default.review2.md`

## 交接
- handoff 文档路径：待 S5
- 脱敏声明：本文件不含密钥 / token / PII
