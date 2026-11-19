---
name: conductor
description: 通用的 AI 开发工作流主控 skill。当收到开发、重构、修 bug、项目初始化、迭代、架构设计、或"拷问代码/设计"类需求时，本 skill 应被加载：它是唯一的流程主控，内置完整工程纪律（头脑风暴/写计划/worktree/子代理/TDD/系统化调试/代码评审/收尾，提取自 superpowers），并负责场景路由、会话记录、缺口台账自省、代码规则约束、精简治理与收尾交接。适用于 Claude Code、Codex、OpenCode、Droid 等支持 SKILL.md 的通用 agent。
---

# conductor · 开发工作流主控

> 名字取自"指挥家"：本 skill 亲自执棒整条开发流程——从场景路由、工程纪律、
> 到自省门禁与收尾交接，一个主控贯穿始终。需要专门工具时，它调用 grill-design、
> code-quality-review、graphify、handoff 等一众 skill 各司其职。

## 定位（必读）

本 skill 是**唯一流程主控**（不再有"治理外壳 + 外部工程内核"两套工作流）。

- **工程纪律已内化**：头脑风暴 → 计划 → worktree → 子代理 → TDD → 系统化调试 → code-review → finish，
  这些步骤提取自 superpowers，固化在本 skill 的 `references/engineering-*.md`，由 conductor 自己执行。
- **一体化职责**：场景路由 + 工程纪律 + 会话记录（overviews）+ 自省门禁 + 规则与精简治理，全在本 skill。
- **外部 skill 只是工具**：grill-design / code-quality-review / handoff 等是被调用的专业工具，
  不是另一个流程主控；它们不定义阶段，只在 conductor 指定的阶段被加载。

## 触发后必须先做的两件事

1. **建会话记录**：按 `references/overview.md` 在项目根的 `overviews/YYYY-MM-DD/` 下创建本轮会话 md（含环境、日期、场景）。
2. **路由 = 规划执行计划**（`references/routing.md`）：不是匹配单个场景，而是把用户的话解析成**执行图**（节点=场景，边=链/分支，执行模式=路径）。
   - 层1 识别**有限结构算子**（顺序/条件/否定/并行/回指）定拓扑；层2 每个节点用**语义三问**（产出物/对象）定场景+路径；层3 用 **I/O 契约表**（scenarios.md）校验节点衔接，不符就停；层4 输出置信度 + **假设明示** + 待澄清；层5 高≥0.8 执行 / 中 0.5–0.8 确认 / 低<0.5 问用户。
   - 失败降级到只读场景（9/10），**绝不猜改码场景硬跑**；每个节点完成后**重规划**后续是否仍适用。
   - 声明含置信度、假设、语义依据；用户一句话纠正即可重路由。**未声明不推进。**
   - **跨 agent 复用**：`references/router-prompt.md` 是上述规划器的**自包含提示词**，可单独注入任意 agent（Codex/Claude Code/OpenCode/Droid）使其获得同款路由能力，不依赖本 skill 已加载。

## 十三场景与流程裁剪

| 场景 | 走的工程阶段（均本 skill 主控） | 额外加载的工具 |
|---|---|---|
| 项目初始化 | brainstorming → writing-plans（止步） | overviews 建库、技术选型拷问 |
| 架构设计 | brainstorming + 设计树（止步于架构规格） | `grill-design` 拷问 + `architecture-contracts` 约束，产出架构文档 |
| 补充设计 | 局部 brainstorm（只补缺口） | 定位已有设计 → 只补未决分支，禁止重写既有结论 |
| 项目开发 | 全程（brainstorm→plan→TDD→review→finish） | S0/S4 + ponytail/caveman/coding-rules |
| 项目迭代 | brainstorm（轻）→ plan → TDD → review | 影响面分析、回归检查 |
| 设计驱动重构 | 先核设计差异 → TDD 锁行为 → review → finish | 对照新/补充设计逐条改接线，禁夹带 |
| 项目重构 | TDD 锁行为 → review → finish | ponytail 砍复杂度、禁止夹带新功能 |
| 解 bug | systematic-debugging → TDD 复现 → review | 根因台账、回归证据 |
| 拷问代码/设计 | 不跑工程阶段（纯只读） | grill-design / grill-me / code-quality-review，出清单不改码 |
| 代码库理解 | 不跑工程阶段（纯只读） | `graphify` 查图 + scan_gaps + architecture-contracts，出代码地图/调用链 |
| 文档同步 | 局部（只核差异，不跑代码 TDD） | grill-design + architecture-contracts，出文档-代码差异清单 |
| 门禁验收/发布 | 不跑工程阶段（只验证） | preflight + verify + scan_gaps + review 全套，出门禁报告 |
| 测试补课 | TDD 变体（只补测试，不写新实现） | test-strategy.md，出新增测试 + 覆盖率证据 |

详细步骤见 `references/scenarios.md`（十三场景）。路径与阶段的对应关系见下节矩阵。

> **references 导航**：25 份知识文档的"何时读哪份"索引见 `references/README.md`（按流程/工程纪律/编码规则/门禁/元信息分五类）——
> 不要一次性全读，按索引指定的阶段与触发条件渐进加载。

## 阶段 → skill 加载矩阵

每一步都先声明主控，再声明加载项与禁止项。完整表见 `references/skill-matrix.md`（含各 skill 仓库地址与跨 agent 安装方式）。摘要：

| 阶段 | 主控 | 加载 / 调用 |
|---|---|---|
| S0 会话开始 | 本 skill | `scripts/preflight.py` 预检依赖 → **确认加载 `handoff` 并调用** → 建 overview md |
| S1 场景路由 | 本 skill | 按 `routing.md` 规划执行图 + 声明 skill 组 + 禁止项 + 下一站；**识别样例触发信号**（用户举例/参考X那样），命中则声明启用 `examples-to-spec.md` |
| S2 设计计划 | 本 skill | 读 `engineering-workflow.md`（brainstorming + writing-plans）；拷问设计加挂 `grill-design`；架构类加 `architecture-contracts`；**按 `examples-to-spec.md` 四步法补全样例外覆盖** |
| S3 TDD 实现 | 本 skill | 读 `engineering-tdd.md`（RED-GREEN-REFACTOR）+ `engineering-workflow.md`（worktree/subagent）；加挂 `ponytail` + `caveman` + coding-rules；**按补全清单核关联项/同模式代码**；每个绿循环按 `git-commit-rules.md` 提交 |
| S4 自省补强（改码变体） | 本 skill | `scripts/scan_gaps.py` 生成台账 → `scripts/verify.py` 拿退出码 → `code-quality-review`；拷问代码加挂 `grill-me`；**核对样例补全项是否都落地且有证据** |
| S4 分析（只读变体） | 本 skill | `graphify` / `scan_gaps.py` / review，**不跑 verify**（无代码改动可验） |
| S5 review 收尾 | 本 skill | 读 `engineering-code-review.md` + `engineering-workflow.md`（finish）；解 bug 读 `engineering-debugging.md`；深度审查加挂 `metago-code-review-deep`；按 `git-commit-rules.md` 提交；收尾 `handoff` + 写 overview |

确定性脚本（scripts/，非 skill）：`preflight.py`（依赖预检）、`verify.py --lang <lang>`（验证命令汇总）、`scan_gaps.py`（缺口台账草稿）。脚本输出即证据，直接写入 overview。

### 工程能力目录（superpowers 整合结果）

原 superpowers 的 15 个 skill 已内化为 conductor 的 10 个**具名工程能力**（`brainstorm`/`plan`/`worktree`/
`execute`/`tdd`/`debug`/`review`/`receive-review`/`verify`/`finish`），3 个舍弃、1 个并入场景链并行算子、
1 个被 conductor 入口取代。逐个来源、去向、输入输出契约见 `references/engineering-catalog.md`。
**场景流程 = 能力链**：`scenarios.md` 每个场景写成 `能力A → 能力B`，按契约逐级传递——
这就是"按 superpowers 流程工作"在 conductor 里的落地，不再加载任何外部流程 skill。

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
3. 工程纪律（`engineering-tdd.md`）与规则**正交**：前者管"先写测试再写实现"，规则管"实现写成什么样"，两者同时遵守。
4. S4 是本 skill 真正"用"规则的环节：跑验证命令拿退出码。

规则文件：`references/coding-rules-common.md` + `references/coding-rules-{python,typescript,react,vue,go,rust,dart}.md`。

**样例补全规则**（`references/examples-to-spec.md`）同属知识文档：用户举例只覆盖部分时，
按"归纳意图→推导覆盖面→分类确认→落地核对"四步补全，防止"用户举 3 个就只做 3 个"。
命中触发信号（比如/例如/参考 X 那样/改这里但同模块有同类位置）时启用，补全项先分类确认再落地。

## 强制机制（规则从建议变门禁）

四道锁，缺一不可：

- **锁 1 · 写码前**：未读规则文件禁止产生代码；违反则该步无效、回退重做。
- **锁 2 · 写码中**：每写一个函数/类/循环，边写边对照注释模板、类型原则、禁魔法字符串、长度基线。
- **锁 3 · 写码后**：跑 formatter/linter/type-checker/注释检查，**退出码非 0 → 禁止进 review、禁止标完成**（客观强制，不靠自觉）。
- **锁 4 · 提交前**：按 `references/git-commit-rules.md` 核对——测试绿、暂存区只有本次文件、commit message 合规；
  **禁止 `git add -A`/`git add .` 盲加混入他人改动**（本项目反复踩过的坑），提交后核对 HEAD 内容。

例外必须显式声明并记入 overview（如生成代码、第三方文件、vendor 边界），禁止以"技术债"名义静默放行。

## 代码理解工具（graphify）

场景 10（代码库理解）与场景 9（拷问）用 `graphify` 替代 grep/read 循环——本地 tree-sitter 建图，查询默认卡 2000 token，边标 `EXTRACTED`/`INFERRED` 可追溯。

- **安装**：`pip install graphifyy`（双 y）→ `graphify install` 注册 skill/MCP → `/graphify .` 建图。市场 skill 名 `graphify`（代码知识图谱问答）。
- **何时用**：跨文件问题（谁调用 / 改了波及什么 / A 怎么到 B）、陌生代码库初次理解。
- **何时不用**：单文件小问题、短会话——工具定义有冷启动成本，可能 net-negative。
- **纪律**：graphify 给的是**上下文**（带源码位置）不是已验证答案；关键边必须打开源码核对。它不替代 `scan_gaps.py`（一个查结构，一个查约定违反）。

## skill 加载纪律（防"声明了但没加载"）

本 skill 声明"加载 X"时（尤其 S0 的 `handoff`），执行方必须**真的调用该 skill**，并遵守：

1. **先确认存在**：S0 跑 `preflight.py`，它检查外部 skill 是否安装。缺失项必须显式处理。
2. **失败要出声**：若某 skill 加载失败（未安装 / 工具不可用），**禁止静默跳过**——必须在 overview 写"缺失：X，影响：Y，已降级为 Z"，并向用户声明降级方案。
3. **不可假装加载**：没实际调用就不得声称"已加载 handoff"。证据是 skill 的调用动作，不是文字声明。
4. **关键依赖缺失要问**：`handoff`（S0）若缺失，必须先告知用户并给出安装命令，不可直接开跑。工程纪律已内置（`engineering-*.md`），不再有外部主控依赖。
5. **路径相关**：只读路径（9/10）不强依赖 handoff——无上次交接也能做理解/拷问，但需在 overview 标注"无交接上下文"。

## 铁律（贯穿全程）

1. 规格未确认不动手。
2. 无失败测试不算实现。
3. 自省未过不进 review。
4. 拿不出证据（测试行号 / linter 退出码 / 覆盖率）不得标完成。
5. 禁魔法字符串：语义字符串一律用枚举或具名常量（见 common 规则 §2）。
6. 工程纪律已内置：S2/S3/S5 读 `engineering-*.md` 执行，不加载外部流程 skill。

## 会话记录（overviews）

每轮会话一个 md，按日期分目录，存于项目根 `overviews/YYYY-MM-DD/`。格式与字段见 `references/overview.md`。写入前脱敏（无密钥 / PII / token）。

## 跨 agent 使用说明

本 skill 目录为通用结构，各 agent 按自身约定纳入即可：

- Claude Code：将本目录放到 `.claude/skills/conductor/`，或用 skills CLI `npx skills add <repo> --skill conductor`。
- Codex：放到 `.codex/skills/conductor/`。
- OpenCode：放到 `.opencode/skill/conductor/` 或 `.config/opencode/skill/`。
- 其他：只要 agent 支持读取 SKILL.md 的 `name`/`description` 作为触发元数据，即可加载本目录。

本 skill 不依赖任何特定 agent 的内置工具名；"加载 skill""读文件""跑命令"等动作在所有 agent 中等价表达。
