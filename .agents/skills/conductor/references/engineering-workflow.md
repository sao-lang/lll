# 工程工作流（conductor 内置工程纪律）

> 来源致谢：本文件的方法论提取自 obra/superpowers（公开工程方法论），为其 7 步工作流、
> 写计划纪律与 4 条核心理念的本地化重述。提取目的：让 conductor 成为**单一主控**，
> 不再依赖外部 superpowers skill，避免两套工作流并存。原方法论版权归原作者。

## 核心理念（4 条，贯穿所有工程阶段）

1. **测试驱动** —— 永远先写测试，没有失败测试不算实现。
2. **系统化优先于临场发挥** —— 用流程取代猜测。
3. **降低复杂度是首要目标** —— 简单优于聪明（配合 `ponytail` 的 YAGNI 梯）。
4. **证据优于声称** —— 宣布完成前先验证，拿不出证据不得标完成。

## 7 步基础工作流

工程纪律是一条"强制按顺序走"的链。每一步的详细规则见本节与相邻 engineering-* 文件。

| 步 | 名称 | 激活时机 | 核心动作 | 出口 |
|---|---|---|---|---|
| 1 | 头脑风暴 brainstorming | 有开发需求、无明确规格 | 苏格拉底式提问逼清需求；一次一小块设计逐块确认；至少 2 个替代方案说明取舍 | 用户签字确认的**设计文档**，无它不进下一步 |
| 2 | 隔离工作区 git worktree | 设计确认后 | 独立分支/worktree 工作，不污染主分支；跑初始化、确认测试基线干净（全绿或全空） | 干净基线 + 隔离区就绪 |
| 3 | 写实施计划 writing-plans | 有规格后 | 拆成 2-5 分钟小任务，每个含精确文件路径、完整改法、验证步骤 | 计划呈现用户确认；计划变更回头改计划，不悄悄改 |
| 4 | 子代理驱动开发 subagent-driven | 计划就绪 | 每个任务派全新上下文子代理（任务说明+计划片段+两段式评审）；主代理只编排审查 | 两段评审都过（规格符合度 + 代码质量） |
| 5 | 测试驱动开发 TDD | 实现中 | RED→GREEN→REFACTOR→提交，见 `engineering-tdd.md` | 每个循环测试全绿 |
| 6 | 请求代码评审 code-review | 每任务完成、进入下一任务前 | 对照计划逐条检查，按严重度报告，见 `engineering-code-review.md` | Critical 清零才推进 |
| 7 | 收尾 finish | 所有任务完成 | 跑完整测试套件；给用户 4 选项（合并/开 PR/保留/丢弃）说明后果；清理 worktree | 全绿 + 用户决策 + 已清理 |

**备选执行模式**：`executing-plans` —— 分批执行 + 人工检查点，适合需要人在场的场景。

## `worktree` 能力详解（隔离工作区，提取自 using-git-worktrees）

**核心原则**：先检测已有隔离 → 用原生工具 → 无原生工具才回退 git → 不与宿主环境对抗。

### 步骤

0. **先检测是否已隔离**（不要凭肉眼判断）：
   ```bash
   GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
   GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
   ```
   - `GIT_DIR != GIT_COMMON` → 已在 linked worktree，跳过创建。
   - **submodule 陷阱**：submodule 里 `GIT_DIR != GIT_COMMON` 也成立，需先排除：
     `git rev-parse --show-superproject-working-tree` 有输出则是 submodule，按普通仓库处理。
1. **优先原生 worktree 工具**（`EnterWorktree`/`/worktree`/`--worktree` flag）——原生工具自动管目录、
   分支、清理；绕过它用 `git worktree add` 会产生宿主看不见的 phantom state（头号错误）。
   无原生工具才用 `git worktree add "$path" -b "$BRANCH"` 回退。
2. **目录选择优先级**：用户显式声明 > 项目内已有 `.worktrees/`/`worktrees/`（`.worktrees` 优先）> 默认 `.worktrees/`。
3. **必须先验 gitignore**：`git check-ignore -q .worktrees` —— 未忽略则先加 `.gitignore` **并提交**再建 worktree，
   否则会把整个工作树误提交进仓库。
4. **项目初始化 + 干净基线**：按语言自动装依赖（package.json→npm install / Cargo.toml→cargo build /
   requirements.txt→pip / pyproject.toml→poetry / go.mod→go mod download），跑测试确认基线（全绿或全空）。
5. **权限被沙箱拒绝**：告知用户并改为在当前目录工作，跑 setup 和基线测试。

> 创建前若用户未表态是否要隔离，先征求同意："要建隔离 worktree 吗？它保护当前分支不受改动影响。"

## `finish` 能力详解（收尾，提取自 finishing-a-development-branch）

**核心原则**：验测试 → 探环境 → 给选项 → 执行选择 → 清理。

### 步骤

1. **验测试**：跑**即将集成的那棵树**的完整测试套件（"本会话早些时候绿过"不算数）。失败则停下报告，菜单在绿灯之后。
2. **探环境**（在切目录前先抓 `GIT_DIR`/`GIT_COMMON`/`WORKTREE_PATH`，清理时要用）：
   | 状态 | 菜单 | 清理 |
   |---|---|---|
   | 普通仓库（GIT_DIR==GIT_COMMON） | 3 选项 | 无 worktree 可清 |
   | worktree + 命名分支 | 3 选项 | 按来源判断（见下） |
   | worktree + detached HEAD | 2 选项（无合并） | 宿主管理，原地保留 |
3. **确认 base branch**：从计划/对话/upstream 推断后**向用户确认**——合并到错误分支代价极高。
4. **给菜单**（普通仓库与命名分支给 exactly 这 3 个；detached HEAD 给 2 个）：
   ```
   1. 本地合并回 <base>
   2. 推送并创建 PR
   3. 保留分支（我之后自己处理）
   ```
   丢弃**只在用户明确要求时**才提供，且要求输入 `discard` 精确确认。
5. **执行选择**：本地合并先合并→验合并结果绿→再清理 worktree 和删分支；push 用 `git push -u`；
   **push 被拒先调查远端变化，绝不擅自 force-push**（仅用户明确要求才强推）。
6. **清理**（仅合并与确认丢弃时）：
   - `GIT_DIR==GIT_COMMON` → 普通仓库，无需清理。
   - worktree 在 `.worktrees/`/`worktrees/` 下 → superpowers 建的，可 `git worktree remove` + `git worktree prune`。
   - 其他位置 → 宿主环境拥有，**原地保留**，有 workspace-exit 工具就用它。
   - **移除被拒**（含未提交文件）→ 绝不擅自 `--force`，展示 `git status` 让用户选：提交/移走/删除。

### git 操作红线（防呆）

| 借口 | 真相 |
|---|---|
| "测试刚才绿过" | 绿只证明跑测试的那棵树；集成前必须在当前树重跑 |
| "显然想合并" | 集成是用户的决定，给菜单并等待 |
| "PR 提了，worktree 是垃圾" | PR feedback 在那个 worktree 里改，保留到落地 |
| "清理被拒，--force 就行" | 拒绝意味着文件只存在于该 worktree，force 会永久销毁 |
| "base 显然是 main" | 确认分叉点，"显然"合并错代价极高 |
| "push 被拒，强推修复" | 被拒说明远端变了，先调查，强推需用户明确要求 |

## 写实施计划（writing-plans）硬约束

计划是给"热情但品味差、没判断力、没项目上下文、且讨厌写测试"的初级工程师看的——必须清楚到照做不跑偏。

```
### 任务 N：<一句话目标>
- 文件路径：src/foo/bar.ts（精确到文件）
- 要做的：<完整可粘贴代码或明确改法>
- 验证：<具体命令或断言，例如 npm test foo 应通过>
```

- **YAGNI**：只写被要求的，不提前造"以后可能用到"的。
- **DRY**：重复逻辑抽出来，但不过度抽象。
- **可验证**：每个任务都有"怎么知道做完了"的办法。
- **无歧义**：文件路径、函数名、改哪几行全部写死。

反例："优化一下性能""把那块逻辑重构好"（无文件路径、无验证、无改法）。
正例："给 calculateTotal 加折扣分支：第 42 行后插入 `if (coupon) total *= 1 - coupon.rate;`；验证：新增测试应通过"。

## 完成前验证（verification-before-completion）

- 宣布完成前，先跑**能证明真的完成**的命令，而不是"看起来完成了"。
- 证据是测试输出 / linter 退出码 / 覆盖率数字，不是感觉或声称。
- 与 conductor 的 S4 自省门禁（scan_gaps + verify）叠加执行，不重复造流程。

## 哲学底线

- 测试驱动：先写测试，永远。
- 系统化优于临场：流程取代猜测。
- 降低复杂度：简单是首要目标。
- 证据优于声称：宣布成功前先验证。

## 在 conductor 中的挂载

本文件由 conductor 在这些阶段加载（见 SKILL.md 阶段矩阵）：
- **S2 设计计划** → 头脑风暴 + 写实施计划 + 架构类加 `architecture-contracts.md`
- **隔离工作区** → 改码类场景的设计确认后执行
- **收尾** → S5 finish

系统化调试见 `engineering-debugging.md`，TDD 见 `engineering-tdd.md`，评审见 `engineering-code-review.md`。
