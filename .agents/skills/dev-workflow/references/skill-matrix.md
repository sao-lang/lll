# Skill 矩阵 · 阶段加载表与仓库地址

> 本文件回答两个问题：**每步加载哪个 skill**、**这些 skill 从哪来 / 怎么装**。
> 主控原则：工程流程以 superpowers 为准；本表只声明"加载/调用"，不重述工程步骤。

## 1. 完整阶段矩阵

| 阶段 | 主控 | 加载 / 调用 | 本步禁止 | 出口条件 |
|---|---|---|---|---|
| S0 会话开始 | dev-workflow | `scripts/preflight.py` 预检 → `handoff`（读上次交接）、建 overview md | 禁止直接写代码 | 预检完成 + overview 文件已建 |
| S1 场景路由 | dev-workflow | 声明 skill 组 + 禁止项 + 下一站 | 禁止跳过声明 | 路由已向用户声明 |
| S2 设计计划 | superpowers | `brainstorming` → `writing-plans`；拷问设计加挂 `grill-design`；架构/设计驱动场景加挂 `architecture-contracts.md` | 规格未确认不动手 | 设计文档 + 计划就绪 |
| S3 TDD 实现 | superpowers | `TDD`、`subagent-driven`；加挂 `ponytail` + `caveman` + coding-rules | 无失败测试不写实现 | 红转绿、全量测试绿 |
| S4 自省补强 | dev-workflow | `scripts/scan_gaps.py` → `scripts/verify.py --lang` → `code-quality-review` + 缺口台账法；拷问代码加挂 `grill-me`；安全核对 `security-redlines.md`、测试核对 `test-strategy.md` | 未达标不许进 review | 遗漏/断点/覆盖率/通过率/安全五项达标 |
| S5 review 收尾 | superpowers | `requesting-code-review`、`finish`；深度审查加挂 `metago-code-review-deep`；收尾 `handoff` + 写 overview | Critical 未清零不许收尾 | 全绿 + 交接文档已写 |

### 确定性脚本（scripts/，非 skill，直接执行）

| 脚本 | 用途 | 调用 |
|---|---|---|
| `preflight.py` | 预检外部 skill + 语言验证工具是否就位，缺失给安装命令 | `python scripts/preflight.py --project-root .` |
| `verify.py` | 执行 `coding-rules-<lang>.md` 的验证命令，汇总退出码为证据 | `python scripts/verify.py --lang <lang>` |
| `scan_gaps.py` | 扫 TODO/pass/fake/协议无实现/install 无调用，生成缺口台账草稿 | `python scripts/scan_gaps.py --path src` |

## 2. Skill 清单与仓库地址

| Skill | 用途 | 仓库 / 来源 | 备注 |
|---|---|---|---|
| **superpowers** | 工程方法论总框架（主控） | `https://github.com/obra/superpowers` | 工程流程唯一主控；不修改本体 |
| **ponytail** | 精简代码（YAGNI 七级梯） | `https://github.com/DietrichGebert/ponytail` | 砍过度设计，不动安全/校验/测试 |
| **caveman** | 精简输出（省 token） | `https://github.com/JuliusBrussee/caveman`（或 ponytail 仓库 `benchmarks/arms/caveman`） | 代码/命令/报错 byte-exact，不压缩 |
| **handoff** | 会话压缩交接 | `https://github.com/mattpocock/skills`（skill: handoff） | 写交接文档 + suggested skills |
| **grill-design** | 拷问设计（设计树+前沿） | WorkBuddy 市场 | 每分支必须有结论、不许静默假设 |
| **grill-me** | 深度追问方案/代码 | WorkBuddy 市场 | 逐层拆解直到共识 |
| **code-quality-review** | 代码七维审查 | WorkBuddy 市场 | 带行号、结论分级、可直接应用的改法 |
| **metago-code-review-deep** | 深度代码审查（四级报告） | WorkBuddy 市场 | Critical/Major/Minor/Info |
| **graphify** | 代码知识图谱（本地 tree-sitter 建图，查询卡 2000 token） | WorkBuddy 市场 `graphify`；开源 `pip install graphifyy` | 场景 10/9 用；替代 grep/read 循环；冷启动有成本，短任务慎用 |

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

本 skill（`dev-workflow`）位于项目根 `.agents/skills/dev-workflow/`，为通用结构，可被上述任一 agent 加载。

## 4. 加载原则（防冲突）

1. **单一主控**：工程步骤以 superpowers 为准，dev-workflow 不重述 brainstorm/TDD/review 步骤细节。
2. **单向声明优先级**：dev-workflow 的 description 写明"以 superpowers 为流程主控"，不修改 superpowers 本体。
3. **只补空白**：superpowers 已有的系统化调试、完成前验证、代码 review 直接复用；本 skill 只做会话记录、自省补强、精简治理、编码规则。
4. **事件触发规则加载**：coding-rules 靠 S1 种下的会话级约束 + 写码事件触发读取，不在 S3 动态注入（避免主控冲突）。
