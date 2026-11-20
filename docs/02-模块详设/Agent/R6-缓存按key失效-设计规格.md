# R6 缓存按 key 精准失效 · 设计规格（补充设计）

> 派生自 `overviews/2026-11-20/20261120-0402-rag-cache-key-invalidation.md`（用户拍板"清空对应的 key，即使内存也应清空对应 key" + 两轮独立 review Blocker 清零）。主详设：`R6-RAG检索增强.md` §6 缓存行。状态：已实现（tests/r6/test_cache_invalidation.py）。

## 1. 背景与问题
- 现状（改造前）：`pipeline.ingest` 后 `invalidate_cache()` 调无参 `CachePort.invalidate()` → `InMemoryCache.invalidate(None)` 整表 `_data.clear()`，误伤共享缓存中他人 key。
- `CachePort.invalidate(key=None)` 契约本就支持单 key，只是 pipeline 没用。
- `with_` 注释称"缓存内容不迁移（新实例空缓存）"与实际共享 cache 对象不一致（账本归属问题）。

## 2. 设计决策（§6 Approaches 四选一）
- **选定：账本 + 逐 key 失效**。`RAGPipeline` 增 `_cache_keys` 账本，登记本实例写入的 key；ingest 后逐个 `invalidate(key)`，不再全清。
- 否决：全表 clear（误伤共享 key）、前缀匹配（无统一前缀）、版本号（键无语料版本维度）。

## 3. 功能需求
- **FR-1** 构造建 `_cache_keys` 账本（set）。
- **FR-2** `query()` 写入缓存时登记 key 到账本。
- **FR-3** `invalidate_cache()` 逐 key `invalidate(key)`，并用差集清账本。
- **FR-4** `with_` 账本归属：cache 实例未替换 → 新实例共享同一账本（否则新实例 ingest 失效不到旧实例写入的 key，破 S4 新文档可见）；cache 被替换 → 新实例空账本。
- **FR-5** `CachePort.invalidate(key)` 对不存在 key 幂等（默认实现 `pop(key, None)` 已满足）；pipeline 仅依赖单键语义，不再依赖 None 全清能力。
- **FR-6** 门禁与测试：`tests/r6/test_cache_invalidation.py`（命中/invalidate 按 key、共享账本、幂等）。
- **FR-7** 命名：`test_invalidate_by_key_not_all` 落地为 `test_ingest_invalidates_by_key_not_whole_cache`（纯命名偏差，复审确认不阻断）。

## 4. 明示假设（供否决，未被否决）
- **A1**：共享 cache 场景下"别实例写入的 key"不在本实例账本内，本实例 ingest 不会失效它们——按 key 方案的固有取舍（全表 clear 才覆盖）。接受。
- **A2**：`with_` 未换 cache 时共享账本（FR-4）。接受。

## 5. 已知边界
- A1 的跨实例 key 不失效是有意取舍；若要求跨实例全覆盖需注入共享账本或全清（违背本次用户决策）。

## 6. 变更记录
- v1.0（2026-11-20）：账本+逐key 方案，FR-1~7。两轮独立 review：轮1 2 项 Should（共享账本行为、别实例 key 不误删）补行为级测试；轮2 Blocker 0 通过。
