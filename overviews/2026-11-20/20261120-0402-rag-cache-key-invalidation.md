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
- 独立审查报告（轮1）：`20261120-0402-rag-cache-key-invalidation.review.md`（Blocker=0 / Should=2 / Nit=4）。
- 独立复审报告（轮2，全新上下文，fix-loop 后）：`20261120-0402-rag-cache-key-invalidation.review3.md`（Blocker=0 / Should=0 / Nit=1，通过）。
- fix-loop 自核记录：`20261120-0402-rag-cache-key-invalidation.review2.md`。
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
- 独立 review 轮1：`overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review.md`
- fix-loop 自核：`overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review2.md`
- 独立复审轮2：`overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.review3.md`
- 上一轮缓存答疑图：本轮对话内联（时序竞态图）
- 命名映射：FR-7 的 `test_invalidate_by_key_not_all` 实现为 `test_ingest_invalidates_by_key_not_whole_cache`（纯命名偏差，独立复审确认不阻断）

## 交接
- handoff 文档路径：待 S5
- 脱敏声明：本文件不含密钥 / token / PII
