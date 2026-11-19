---
name: dev-workflow
description: 通用的 AI 开发工作流治理外壳。当收到开发、重构、修 bug、项目初始化、迭代、或"拷问代码/设计"类需求时，本 skill 应被加载：它负责场景路由、会话记录、缺口台账自省、代码规则约束与收尾交接，并显式声明每个阶段应加载哪些其他 skill。它以 superpowers 等工程方法论为流程主控，本身不重述工程步骤，只做治理与补强。适用于 Claude Code、Codex、OpenCode、Droid 等支持 SKILL.md 的通用 agent。
---

# dev-workflow · 开发工作流治理外壳

## 定位（必读）

本 skill 是**治理外壳**，不是第二个工程流程。

- **工程流程主控**交给 `superpowers`（或等价方法论）：头脑风暴 → 计划 → worktree → TDD → code-review → finish。本 skill **不重述**这些步骤细节。
- 本 skill 只做四件 superpowers 不管的事：**场景路由、会话记录（overviews）、自省补强、规则与精简治理**。
- 冲突时以工程内核（superpowers）的判断为准：它管正确性，本 skill 管治理与补充。

## 触发后必须先做的两件事

1. **建会话记录**：按 `references/overview.md` 在项目根的 `overviews/YYYY-MM-DD/` 下创建本轮会话 md（含环境、日期、场景）。
2. **场景路由 + 路径判定**：按 `references/routing.md` 的三层漏斗判定十三场景之一，**并判路径类型（改码/只读/文档/验证）**，用声明模板向用户声明"场景 / 路径 / 走的阶段 / 加载哪些 skill / 禁止项 / 下一站"。**未声明不推进。**

## 十三场景与流程裁剪

| 场景 | 交给工程内核的阶段 | 本外壳额外负责 |
|---|---|---|
| 项目初始化 | brainstorming → writing-plans（止步） | overviews 建库、技术选型拷问 |
| 架构设计 | brainstorming + 设计树（止步于架构规格） | `grill-design` 拷问 + `architecture-contracts` 约束，产出架构文档 |
| 补充设计 | 局部 brainstorm（只补缺口） | 定位已有设计 → 只补未决分支，禁止重写既有结论 |
| 项目开发 | 全程（brainstorm→plan→TDD→review→finish） | S0/S4 + ponytail/caveman/coding-rules |
| 项目迭代 | brainstorm（轻）→ plan → TDD → review | 影响面分析、回归检查 |
| 设计驱动重构 | 先核设计差异 → TDD 锁行为 → review → finish | 对照新/补充设计逐条改接线，禁夹带 |
| 项目重构 | TDD 锁行为 → review → finish | ponytail 砍复杂度、禁止夹带新功能 |
| 解 bug | systematic-debugging → TDD 复现 → review | 根因台账、回归证据 |
| 拷问代码/设计 | 不委托（纯只读） | grill-design / grill-me / code-quality-review，出清单不改码 |
| 代码库理解 | 不委托（纯只读） | `graphify` 查图 + scan_gaps + architecture-contracts，出代码地图/调用链 |
| 文档同步 | 局部（只核差异，不跑代码 TDD） | grill-design + architecture-contracts，出文档-代码差异清单 |
| 门禁验收/发布 | 不委托（只验证） | preflight + verify + scan_gaps + review 全套，出门禁报告 |
| 测试补课 | TDD 变体（只补测试，不写新实现） | test-strategy.md，出新增测试 + 覆盖率证据 |

详细步骤见 `references/scenarios.md`（十三场景）。路径与阶段的对应关系见下节矩阵。

## 阶段 → skill 加载矩阵

每一步都先声明主控，再声明加载项与禁止项。完整表见 `references/skill-matrix.md`（含各 skill 仓库地址与跨 agent 安装方式）。摘要：

| 阶段 | 主控 | 加载 / 调用 |
|---|---|---|
| S0 会话开始 | 本 skill | `scripts/preflight.py` 预检依赖 → `handoff`（读上次）→ 建 overview md |
| S1 场景路由 | 本 skill | 声明 skill 组 + 禁止项 + 下一站 |
| S2 设计计划 | 工程内核 | `brainstorming` → `writing-plans`；拷问设计加挂 `grill-design`；架构类加 `architecture-contracts` |
| S3 TDD 实现 | 工程内核 | `TDD`、`subagent-driven`；加挂 `ponytail` + `caveman` + coding-rules |
| S4 自省补强（改码变体） | 本 skill | `scripts/scan_gaps.py` 生成台账 → `scripts/verify.py` 拿退出码 → `code-quality-review`；拷问代码加挂 `grill-me` |
| S4 分析（只读变体） | 本 skill | `graphify` / `scan_gaps.py` / review，**不跑 verify**（无代码改动可验） |
| S5 review 收尾 | 工程内核 | `requesting-code-review`、`finish`；深度审查加挂 `metago-code-review-deep`；收尾 `handoff` + 写 overview |

确定性脚本（scripts/，非 skill）：`preflight.py`（依赖预检）、`verify.py --lang <lang>`（验证命令汇总）、`scan_gaps.py`（缺口台账草稿）。脚本输出即证据，直接写入 overview。

### 场景 × 阶段矩阵（S1 硬输出，决定跳过哪些阶段）

| 路径 | 场景 | S0 | S1 | S2 | S3 | S4 | S5 |
|---|---|---|---|---|---|---|---|
| 改码 | 1/4/5/6/7/8 | ✓ | ✓ | ✓ | ✓ | 改码变体 | ✓ |
| 只读 | 9/10 | ✓ | ✓ | — | — | 只读变体 | —（写 overview 后结束） |
| 文档 | 2/3/11 | ✓ | ✓ | 核差异 | S3' 只改文档 | 核签名 | ✓ |
| 验证 | 12/13 | ✓ | ✓ | — | 13 补测试 / 12 跳过 | ✓ | ✓ |

**只读出口**：只读路径的出口是"发现清单 / 代码地图 / 证据台账已交付且可追溯"，**不是"测试全绿"**。禁止对无代码改动的场景套用"未达标不许进 review"的门禁，否则空转。

## 代码规则如何生效（关键机制）

规则**不是第二个 skill**，是按需加载的知识文档，靠"会话级约束 + 写码事件触发"生效：

1. S1 时本 skill 在会话中种下一条**常驻约束**："任何写码动作前，先读 `references/coding-rules-common.md` 与当前语言的 `coding-rules-<lang>.md`"。
2. 常驻的是**一句话指针**（省 token），规则**正文**直到写码那一刻才读取——与渐进式披露同构。
3. 工程内核（superpowers）**无需感知**该规则：它管"先写测试再写实现"，规则管"实现写成什么样"，两者正交。
4. S4 是本 skill 真正"用"规则的环节：跑验证命令拿退出码。

规则文件：`references/coding-rules-common.md` + `references/coding-rules-{python,typescript,react,vue,go,rust,dart}.md`。

## 强制机制（规则从建议变门禁）

三道锁，缺一不可：

- **锁 1 · 写码前**：未读规则文件禁止产生代码；违反则该步无效、回退重做。
- **锁 2 · 写码中**：每写一个函数/类/循环，边写边对照注释模板、类型原则、禁魔法字符串、长度基线。
- **锁 3 · 写码后**：跑 formatter/linter/type-checker/注释检查，**退出码非 0 → 禁止进 review、禁止标完成**（客观强制，不靠自觉）。

例外必须显式声明并记入 overview（如生成代码、第三方文件、vendor 边界），禁止以"技术债"名义静默放行。

## 代码理解工具（graphify）

场景 10（代码库理解）与场景 9（拷问）用 `graphify` 替代 grep/read 循环——本地 tree-sitter 建图，查询默认卡 2000 token，边标 `EXTRACTED`/`INFERRED` 可追溯。

- **安装**：`pip install graphifyy`（双 y）→ `graphify install` 注册 skill/MCP → `/graphify .` 建图。市场 skill 名 `graphify`（代码知识图谱问答）。
- **何时用**：跨文件问题（谁调用 / 改了波及什么 / A 怎么到 B）、陌生代码库初次理解。
- **何时不用**：单文件小问题、短会话——工具定义有冷启动成本，可能 net-negative。
- **纪律**：graphify 给的是**上下文**（带源码位置）不是已验证答案；关键边必须打开源码核对。它不替代 `scan_gaps.py`（一个查结构，一个查约定违反）。

## 铁律（贯穿全程）

1. 规格未确认不动手。
2. 无失败测试不算实现。
3. 自省未过不进 review。
4. 拿不出证据（测试行号 / linter 退出码 / 覆盖率）不得标完成。
5. 禁魔法字符串：语义字符串一律用枚举或具名常量（见 common 规则 §2）。
6. 只补空白：superpowers 已有的能力直接复用，不重复造流程。

## 会话记录（overviews）

每轮会话一个 md，按日期分目录，存于项目根 `overviews/YYYY-MM-DD/`。格式与字段见 `references/overview.md`。写入前脱敏（无密钥 / PII / token）。

## 跨 agent 使用说明

本 skill 目录为通用结构，各 agent 按自身约定纳入即可：

- Claude Code：将本目录放到 `.claude/skills/dev-workflow/`，或用 skills CLI `npx skills add <repo> --skill dev-workflow`。
- Codex：放到 `.codex/skills/dev-workflow/`。
- OpenCode：放到 `.opencode/skill/dev-workflow/` 或 `.config/opencode/skill/`。
- 其他：只要 agent 支持读取 SKILL.md 的 `name`/`description` 作为触发元数据，即可加载本目录。

本 skill 不依赖任何特定 agent 的内置工具名；"加载 skill""读文件""跑命令"等动作在所有 agent 中等价表达。
