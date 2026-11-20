# R6 缓存按 key 失效 · Fix-loop 复审报告

**复审日期**：2026-11-20
**复审对象**：独立审查报告（`20261120-0402-rag-cache-key-invalidation.review.md`）提出的 2 Should + 4 Nit 的修复
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
