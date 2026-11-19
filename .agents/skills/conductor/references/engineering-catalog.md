# 工程能力目录 · superpowers 全景整合表

> 本文件回答两个问题：**superpowers 的每个 skill 去哪了？**、**conductor 的流程靠哪些能力跑起来？**
> 来源：obra/superpowers（15 个 skill）+ 本地压缩重制版（5 份 reference）。
> 整合原则：不是把 skill 文件塞进项目，而是把每个 skill **内化为 conductor 的具名工程能力**，
> 挂进 13 场景的具体阶段。能力是 conductor 流程里的节点，有名字、有输入输出契约、有详情文件。

## 1. 全景盘点（15 个 skill 逐个交代去向）

| # | superpowers skill | 去向 | conductor 能力名 | 挂载场景 / 阶段 | 详情文件 |
|---|---|---|---|---|---|
| 1 | brainstorming | ✅ 整合 | `brainstorm` | 场景 1/2/3/4/5 的 S2 | engineering-workflow.md |
| 2 | writing-plans | ✅ 整合 | `plan` | 场景 1/4/5/6 的 S2 | engineering-workflow.md |
| 3 | using-git-worktrees | ✅ 整合 | `worktree` | 所有改码场景 S2 后 | engineering-workflow.md（含完整隔离/gitignore 纪律） |
| 4 | subagent-driven-development | ✅ 整合 | `execute`（subagent 模式） | 场景 4/5 S3 | engineering-workflow.md + engineering-code-review.md |
| 5 | executing-plans | ✅ 整合 | `execute`（分批模式） | 场景 4/5 S3 备选（需人在场） | engineering-workflow.md |
| 6 | test-driven-development | ✅ 整合 | `tdd` | 场景 4/5/6/7/8/13 的 S3 | engineering-tdd.md |
| 7 | requesting-code-review | ✅ 整合 | `review` | 所有改码场景（每任务间 + S5） | engineering-code-review.md |
| 8 | receiving-code-review | ✅ 整合 | `receive-review` | 横切：收到反馈时 | engineering-code-review.md |
| 9 | finishing-a-development-branch | ✅ 整合 | `finish` | 所有改码场景 S5 | engineering-workflow.md（含菜单/合并/清理红线） |
| 10 | systematic-debugging | ✅ 整合 | `debug` | 场景 8；场景 6 接线失败时 | engineering-debugging.md |
| 11 | verification-before-completion | ✅ 整合 | `verify` | 场景 12 门禁 + 所有改码场景收尾 | engineering-workflow.md + scripts/verify.py |
| 12 | using-superpowers | ❌ 舍弃 | — | 它是 superpowers 的入口/触发说明；conductor 自己就是入口，不需要 | — |
| 13 | diagnosing-superpowers | ❌ 舍弃 | — | superpowers 自检工具，诊断"superpowers 是否装好"；与 conductor 无关 | — |
| 14 | writing-skills | ❌ 舍弃 | — | "怎么写 skill"元能力，非开发流程；造 skill 用 skill-creator 即可 | — |
| 15 | dispatching-parallel-agents | ♻️ 并入 | 场景链并行算子 | 独立派单能力已由 conductor 的 `routing.md` 并行算子覆盖（语句含"分别/同时"→多实例） | routing.md §1.5 |

**结论**：11 个整合为具名能力，1 个并入已有机制，3 个舍弃——没有遗漏，也没有无理由丢弃。

## 2. 具名工程能力契约（conductor 流程的节点）

这些能力是 conductor 场景链里真正被调用的节点。每个都有输入/输出契约——场景衔接时按契约传递，与 `scenarios.md` 的场景 I/O 契约表同构。

| 能力名 | 一句话职责 | 输入契约 | 输出契约 | 入口阶段 |
|---|---|---|---|---|
| `brainstorm` | 苏格拉底式提问逼清需求，逐块确认设计 | 粗糙需求 | 用户签字的设计文档 | S2 |
| `plan` | 拆成 2-5 分钟、精确到文件与验证的任务 | 设计文档 | 可执行实施计划 | S2 |
| `worktree` | 隔离分支 + 干净测试基线 | 已确认设计 + 干净工作区 | 隔离工作区 + 基线证据 | S2 后 |
| `execute` | 逐任务执行（subagent 两段评审 / 分批人工检查点） | 实施计划 | 任务产出（评审通过） | S3 |
| `tdd` | RED→GREEN→REFACTOR，测试前代码一律删 | 期望行为描述 | 通过测试的实现 + 证据 | S3（每任务内） |
| `debug` | 4 阶段根因：复现→追踪→纵深防御→条件等待 | 故障现象 | 根因 + 修复 + 回归测试 | 场景 8 主体 |
| `review` | 对照计划逐条查，按严重度分级 | 任务产出 + 计划 | 分级评审结论（Critical 阻断） | 每任务间 + S5 |
| `receive-review` | 把反馈当事实，改完回到验证仍绿 | 评审反馈 | 修改后的产出（仍绿） | 横切 |
| `verify` | 完成前跑真实验证，拿退出码/测试证据 | 完成声明 | 验证证据（退出码/覆盖率） | S4 + S5 前 |
| `finish` | 全绿后给合并/PR/保留/丢弃四选项，清理 | 全绿变更 | 用户决策 + 已清理工作区 | S5 |

## 3. 能力编排规则

1. **场景即能力链**：每个场景在 `scenarios.md` 里写成 `能力A → 能力B → ...`，能力名必须在本表可查。
2. **契约传递**：前一能力的输出契约必须满足后一能力的输入契约；不满足则停下问用户（与 routing 的 I/O 校验同构）。
3. **按需裁剪**：只读/文档/验证路径的场景只调用子集（如场景 9/10 不调用 `tdd`/`review` 的改码变体）。
4. **外部工具 ≠ 工程能力**：grill-design / graphify / code-quality-review 等是被能力调用的工具，
   它们不占据能力链节点；例如 `brainstorm` 可调用 grill-design 做设计拷问。
5. **缺失降级**：某能力的详情文件缺失 → 按该能力"一句话职责 + 契约"最小执行，并在 overview 标注，禁止静默跳过。
