---
name: code-quality-review
display_name: 代码质量审查
display_name_en: Code Quality Review
description: Reviews a diff, a pull request, or a single file the way a careful teammate would, and returns findings that are ranked, located and actionable instead of a list of stylistic opinions. It separates blocking defects from suggestions, cites the exact file and line for each finding, explains why it matters in this codebase, and proposes a concrete diff. A runnable pass grades the change on seven dimensions and refuses to ship a review that contains only vague praise or unlocated complaints. Use when the user asks 代码审查、审一下这段代码、review 一下、看看这个 PR、这段代码有问题吗、帮我看看改动、代码质量、有没有 bug、逻辑对不对、边界情况、空指针、异常处理、并发问题、资源泄露、性能隐患、命名规范、重复代码、可读性、可维护性、圈复杂度、函数太长、耦合、硬编码、魔法数字、日志缺失、错误码、单元测试、回归风险、上线风险、重构建议、diff 审查、分支对比、提交前检查, or says code review, review this PR, review my code, check this diff, is this correct, any bugs, edge cases, null check, error handling, race condition, resource leak, performance issue, naming convention, duplicated code, readability, maintainability, cyclomatic complexity, too long, coupling, hardcoded value, magic number, missing logs, unit test coverage, regression risk, refactor suggestion, pre-commit check.
description_zh: 按七维（正确性、错误处理、并发、资源、性能、安全、可维护性）扫描代码或 PR，结论按阻塞/应该改/可选三级排序，每条挂文件与行号、说清后果并给出可直接应用的改法。
description_en: Reviews code or a PR across seven dimensions and ranks findings blocker/should/nit, with every finding located to file:line, explained by consequence, and shipped with a concrete diff.
category: engineering
version: 1.0.0
author: 昆仑增长
allowed-tools: Read, Write, Bash, Glob, Grep
---

# Code Quality Review

把「帮我 review 一下」变成一份**能直接照着改**的清单：每条结论都有位置、有理由、有改法，
并且**按严重程度排序**。

这个技能刻意不做「风格挑刺机」。一份满篇都是「建议改成 const」的 review
是负价值的 —— 它会把真正的问题淹没掉。所以流程的第一步是**分级**。

## 何时使用

- 用户贴了一段代码或一个文件 → 走「单文件」分支
- 用户给了 PR / diff / 分支 → 走「变更集」分支
- 用户问「有没有 bug」→ 走「缺陷优先」分支，跳过风格与命名
- 用户问「能不能上线」→ 走「风险清单」分支，只输出阻塞项与回归风险

## 工作流

### 第 1 步：先搞清楚这次改动想干什么

在读代码之前，先回答：

1. **意图** —— 这次改动要解决什么问题？（commit message / PR 描述 / 用户说明）
2. **范围** —— 改了哪些文件？有没有动公共接口或数据 schema？
3. **风险面** —— 这些文件被谁依赖？有没有测试覆盖？

**不知道意图就 review，一定会产生误报** —— 你会把刻意为之的取舍当成缺陷提出来。

### 第 2 步：七维扫描

按 `references/review-dimensions.md` 逐维过。每个维度都规定了对应用法：

| 维度 | 一句话判据 |
|---|---|
| 正确性 | 逻辑在所有分支上对吗？边界值呢？ |
| 错误处理 | 失败路径被吞掉了吗？ |
| 并发与时序 | 共享状态有没有竞态？ |
| 资源生命周期 | 打开的东西都关了吗？ |
| 性能 | 有没有 N+1、循环内重活、无界增长？ |
| 安全与数据 | 输入可信吗？敏感信息落地了吗？ |
| 可维护性 | 半年后别人改得动吗？ |

### 第 3 步：分级（这一步决定 review 有没有用）

- **阻塞（Blocker）** —— 会出错、会丢数据、会引入安全问题的。必须改。
- **应该改（Should）** —— 不改会积累成问题，但本次不改也不炸。
- **可选（Nit）** —— 风格与偏好。**最多列 3 条**，多了就删。

🔴 硬性要求：**如果一次 review 里 Blocker 和 Should 都是 0，
必须在结论里明确写「本次未发现缺陷级问题」，然后用 Nit 补全** ——
不能因为没问题就返回一篇含糊的好评。

### 第 4 步：每条结论都写成四段

```
[级别] 文件:行号
现象：这段代码现在是什么样
为什么重要：在这个代码库里会导致什么后果（具体到场景，不要说「可能有问题」）
改法：给出可直接应用的 diff 或替换代码
```

缺任何一段的结论都要补上或删掉。特别是**「改法」** ——
只说「这里不太好」不说怎么改的结论是无效的。

### 第 5 步：跑一遍自检

交付前过 `references/output-contract.md` 的闸门，拦下：

- 没有位置的结论（`文件:行号` 缺失）
- 没有改法的结论
- Nit 超过 3 条
- 通篇只有风格意见却标了 Blocker
- 把用户刻意为之的取舍当成缺陷（第 1 步没做实的典型症状）

## 硬约束

- 每条结论必须能定位到 `文件:行号`；定位不到的，归入「需要更多上下文」而不是硬凑
- 不给出「没问题」这类结论 —— 只能说「本次扫描在七个维度上未发现 X 类问题」
- 不给没读过的代码下判断；读不到的部分显式说明「未覆盖」
- 涉及安全问题时说清影响面，不做夸大，也不淡化
- Blocker 必须给出可执行的改法，不能只报警

## References

- `references/review-dimensions.md` —— 七维判据与典型反例
- `references/output-contract.md` —— 输出格式、分级规则与交付闸门
