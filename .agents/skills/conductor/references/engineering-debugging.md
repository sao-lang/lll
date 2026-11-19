# 系统化调试（conductor 内置工程纪律）

> 来源致谢：提取自 obra/superpowers 的系统化调试方法论（04-systematic-debugging.md），本地化重述。

不靠猜。用 4 阶段根因流程定位问题，再修。

## 4 阶段

1. **复现**：写出能稳定触发 bug 的最小步骤 / 测试。复现不了就不算定位。
2. **根因追踪 root-cause-tracing**：沿调用链往下，用日志 / 断点确认"真相在哪一层"，而不是在现象层打补丁。
3. **纵深防御 defense-in-depth**：修根因的同时，在相邻边界加一道保护（例如输入校验），防止同类问题换皮重现。
4. **条件等待 condition-based-waiting**：对竞态 / 时序类问题，用"等到条件成立"而非"睡 N 秒"。

## 完成前验证

- 修复后，先跑**能复现原 bug 的测试**，确认它现在绿。
- 再跑**相关回归测试**，确认没引入新问题。
- 只在"真的修好了"时才宣布完成——证据是测试，不是感觉。

## 纪律

- 没调查，不修复。
- 一次只改一个变量来定位，别同时改三处。

## 在 conductor 中的挂载

由 conductor 在**场景 8（解 bug）**加载：`systematic-debugging`（4 阶段根因法）→ TDD 写复现测试（`engineering-tdd.md`）→ 代码评审（`engineering-code-review.md`）。产出根因台账 + 回归证据写入 overview。
