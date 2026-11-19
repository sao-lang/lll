# Skill 矩阵 · 阶段加载表与仓库地址

> 本文件回答两个问题：**每步加载哪个 skill**、**这些 skill 从哪来 / 怎么装**。
> 主控原则：conductor 是唯一流程主控；工程纪律已内化为 `engineering-*.md`，外部 skill 只是被调用的工具。

## 1. 完整阶段矩阵

| 阶段 | 主控 | 加载 / 调用 | 本步禁止 | 出口条件 |
|---|---|---|---|---|
| S0 会话开始 | conductor | `scripts/preflight.py` 预检 → `handoff`（读上次交接）、建 overview md | 禁止直接写代码 | 预检完成 + overview 文件已建 |
| S1 场景路由 | conductor | 声明 skill 组 + 禁止项 + 下一站 | 禁止跳过声明 | 路由已向用户声明 |
| S2 设计计划 | conductor | 读 `references/02-engineering/engineering-workflow.md`（brainstorming + writing-plans）；拷问设计加挂 `grill-design`；架构/设计驱动场景加挂 `references/04-guards/architecture-contracts.md` | 规格未确认不动手 | 设计文档 + 计划就绪 |
| S3 TDD 实现 | conductor | 读 `references/02-engineering/engineering-tdd.md` + `references/02-engineering/engineering-workflow.md`（worktree/subagent）；加挂 `ponytail` + `caveman` + coding-rules | 无失败测试不写实现 | 红转绿、全量测试绿 |
| S4 自省补强 | conductor | `scripts/scan_gaps.py` → `scripts/verify.py --lang` → `code-quality-review` + 缺口台账法；拷问代码加挂 `grill-me`；安全核对 `references/04-guards/security-redlines.md`、测试核对 `references/04-guards/test-strategy.md` | 未达标不许进 review | 遗漏/断点/覆盖率/通过率/安全五项达标 |
| S5 review 收尾 | conductor | 读 `references/02-engineering/engineering-code-review.md` + `references/02-engineering/engineering-workflow.md`（finish）、解 bug 读 `references/02-engineering/engineering-debugging.md`；深度审查加挂 `metago-code-review-deep`；收尾 `handoff` + 写 overview | Critical 未清零不许收尾 | 全绿 + 交接文档已写 |

### 确定性脚本（scripts/，非 skill，直接执行）

| 脚本 | 用途 | 调用 |
|---|---|---|
| `preflight.py` | 预检外部 skill + 语言验证工具是否就位，缺失给安装命令 | `python scripts/preflight.py --project-root .` |
| `verify.py` | 执行 `coding-rules-<lang>.md` 的验证命令，汇总退出码为证据 | `python scripts/verify.py --lang <lang>` |
| `scan_gaps.py` | 扫 TODO/pass/fake/协议无实现/install 无调用，生成缺口台账草稿 | `python scripts/scan_gaps.py --path src` |

## 2. Skill 清单与仓库地址

> **工程纪律已内置**：原 `superpowers` 的方法论已提取为 conductor 自己的
> `references/engineering-{workflow,tdd,debugging,code-review}.md`，由 conductor 直接执行，
> 不再依赖外部 superpowers skill——**没有第二套工作流**。
> 下表是 conductor 会调用的**外部工具 skill**，已全部随项目放在 `.agents/skills/`（项目自包含），
> 无需单独安装。用 `preflight.py` 可随时复核。

| Skill | 用途 | 仓库 / 来源 | 备注 |
|---|---|---|---|
| **ponytail** | 精简代码（YAGNI 七级梯） | `https://github.com/DietrichGebert/ponytail`（`skills/ponytail/SKILL.md`） | 砍过度设计，不动安全/校验/测试 |
| **caveman** | 精简输出（省 token） | `https://github.com/JuliusBrussee/caveman`（`skills/caveman/SKILL.md`） | 代码/命令/报错 byte-exact，不压缩 |
| **handoff** | 会话压缩交接 | WorkBuddy 市场 `handoff`（原 `mattpocock/skills`） | 写交接文档 + suggested skills；S0 关键依赖 |
| **grill-design** | 拷问设计（设计树+前沿） | WorkBuddy 市场 | 每分支必须有结论、不许静默假设 |
| **grill-me** | 深度追问方案/代码 | WorkBuddy 市场 | 逐层拆解直到共识 |
| **code-quality-review** | 代码七维审查 | WorkBuddy 市场 | 带行号、结论分级、可直接应用的改法 |
| **metago-code-review-deep** | 深度代码审查（四级报告） | WorkBuddy 市场 | Critical/Major/Minor/Info |
| **graphify** | 代码知识图谱（本地 tree-sitter 建图，查询卡 2000 token） | WorkBuddy 市场 `graphify`；开源 `pip install graphifyy` | 场景 10/9 用；替代 grep/read 循环；冷启动有成本，短任务慎用 |

配套（非主流程必需）：`ponytail-review`、`caveman-compress` 也随项目放在 `.agents/skills/`。

## 3. 跨 agent 安装方式

通用 skills CLI（支持 Claude Code / Codex / Cursor / OpenCode 等）：

```bash
# 通用安装（把仓库 skill 装到当前项目的 .agents/skills/ 或用户级）
npx skills add <repo> --skill <skill-name>

# 示例
npx skills add DietrichGebert/ponytail --skill ponytail
npx skills add DietrichGebert/ponytail --skill caveman
npx skills add mattpocock/skills --skill handoff
```

按 agent 的目录约定（手动放置时）：

| Agent | Skill 目录 |
|---|---|
| Claude Code | `.claude/skills/<name>/SKILL.md` |
| Codex | `.codex/skills/<name>/SKILL.md` |
| OpenCode | `.opencode/skill/<name>/SKILL.md` 或 `.config/opencode/skill/` |
| 通用 CLI | `.agents/skills/<name>/SKILL.md` |

本 skill（`conductor`）位于项目根 `.agents/skills/conductor/`，为通用结构，可被上述任一 agent 加载。

## 4. 加载原则（单一主控）

1. **唯一主控**：conductor 自己执行全部工程阶段（brainstorm/TDD/review/debugging/finish），
   步骤细节在 `engineering-*.md`，不再加载任何其他流程 skill。**没有第二套工作流**。
2. **工程纪律内化**：原 superpowers 方法论已提取为内部 reference（含来源致谢），
   不依赖外部 skill 是否存在；外部工具 skill 缺失只影响该工具，不影响流程本身。
3. **工具是配角**：grill-design / code-quality-review / handoff / graphify 等只在指定阶段被调用，
   它们不定义阶段、不争夺主控；conductor 决定何时加载谁。
4. **事件触发规则加载**：coding-rules 靠 S1 种下的会话级约束 + 写码事件触发读取，不在 S3 动态注入。
