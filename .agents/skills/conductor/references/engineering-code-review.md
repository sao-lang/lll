# 代码评审 code-review（conductor 内置工程纪律）

> 来源致谢：提取自 obra/superpowers 上游的 `requesting-code-review/` 与 `receiving-code-review/`，本地化重述。

评审是计划与质量的守门员，分两段式进行。

## 请求评审 requesting-code-review

**激活时机**：每个任务完成、进入下一个之前。

**做法**：
- 对照**实施计划**逐条检查：实现了吗？验证过了吗？
- 按严重度报告问题：
  - **Critical（阻断）**：不符合规格 / 会炸生产 / 丢了功能 → 必须修，否则不推进。
  - **Major**：明显质量债 / 可读性差 → 建议修。
  - **Minor**：风格 / 命名 → 可后续。
- 评审结论写给用户：通过 / 带问题通过 / 打回。

## 接收评审 receiving-code-review

当**你**是被执行方、收到主代理或用户的反馈时：
- 把反馈当事实处理，不辩解。
- 区分"真的 bug"和"品味不同"，前者必改，后者可讨论。
- 改完回到对应任务的验证步骤，确认仍绿。
- 不在回复里顺手改无关代码（Surgical Changes 原则）。

## 子代理驱动开发（execute）护栏

> 完整模板（implementer-prompt / fix-loop / re-review / final-review，约 1000 行）见上游
> `obra/superpowers/skills/subagent-driven-development/`。此处保留缺了就跑不对的三条护栏。

### 派单契约

- **任务 brief 是唯一需求源**：派 implementer 前把任务的完整文本提取到唯一命名的 brief 文件，
  dispatch 里给 brief 路径（"先读这个，它是你的需求，精确值逐字使用"）+ 一句上下文 +
  前面任务的 interfaces/决策 + 你对 brief 中歧义的裁决 + 报告路径契约。
  **精确值（数字/魔法字符串/签名/测试用例）只出现在 brief，不让子代理读整个计划文件。**
- **派单前先记 `BASE=$(git rev-parse HEAD)`**，评审包和 fix 轮 diff 都依赖它。
- **implementer 不许自己派 reviewer**：它只做自己的任务、自审（读自己的 diff）、报告。
  评审是 controller 的职责——worker 自己派的 reviewer 是重复座位、批准无效，属于要标记的缺陷。
- **显式指定 model**：省略会静默继承会话模型（常常最贵）。机械任务用便宜档，
  集成/判断用标准档，架构/最终全分支评审用最强档；fix-loop 第 4-5 轮用比卡住的 implementer 高一档。
  注意"轮数胜过 token 单价"——便宜模型多步任务可能多花 2-3 倍轮次，反而更贵。

### 两段式评审（评审要求固定两步）

1. **规格符合度**：产出是否匹配任务定义？
2. **代码质量**：可读性、DRY、YAGNI、命名、是否有不必要复杂度？

两段都过，任务才算完成。

### Fix-loop 与完成判定

- 评审报规格 ❌、任何 Critical/Important、或你确认是真缺口的 ⚠️ → 触发 fix loop；
  **每轮前重读 fix-loop 规则**（轮次上限、熔断、每轮必须写的台账行，记忆扛不过上下文压缩）。
- **禁止带着未处理/未在轮次上限裁决的 Critical/Important 进入下一个任务。**
- 评审干净后写完成行到台账：`Task N: complete (commits <base7>..<head7>, review clean)`；
  熔断后写 `(<K> parked)`。台账是压缩后唯一幸存的状态——没有它，controller 会重复派发整段已完成任务。
- 所有任务完成后做**全分支最终评审**（最强档模型），含一次 fix wave。

### 常见借口（打回）

| 借口 | 真相 |
|---|---|
| "规格差不多就行" | 评审发现规格缺口 = 没完成，修或到上限裁决，只有这两个出口 |
| "我自己改更快，派单是开销" | controller 直接改会污染上下文且跳过评审；恢复 implementer |
| "再来一轮就收敛" | 过上限后不会再收敛，那是结构性问题，该裁决分流 |
| "这个 finding 明显不对，扔掉" | 只能在上限裁决，且每次裁决都进台账；静默丢弃禁止 |
| "改动小，跳过复审" | 未复审的修复是回归的来源；每轮都以限定范围复审结束 |

## 与 conductor 评审工具的分工

- 本文件定义**评审纪律与严重度口径**（何时评审、Critical 阻断、两段式）。
- `code-quality-review`（市场 skill）做**七维也审查**（正确性/错误处理/并发/资源/性能/安全/可维护性，带行号）。
- `metago-code-review-deep`（市场 skill）做**深度四级报告**（Critical/Major/Minor/Info）。
- 三者叠加：本文件定规矩，市场 skill 出证据，Critical 必须清零才进 finish。

## 在 conductor 中的挂载

由 conductor 在 **S5（review 收尾）**加载；每个任务完成后、进入下一任务前的"请求评审"也复用本文件的严重度口径。
