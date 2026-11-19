# conductor references 索引

> 25 份知识文档（含本索引），按需加载（渐进式披露）。本索引回答"什么情况读哪份"。
> 加载纪律：只在本表指定的阶段/触发条件下读对应文件，不要一次性全读。

## A. 流程与路由（S0/S1 必读）

| 文件 | 何时读 | 职责 |
|---|---|---|
| `routing.md` | S1 | 场景路由规划器——把用户的话解析成执行图（结构算子→语义三问→I/O 契约→置信度） |
| `router-prompt.md` | 需跨 agent 复用路由时 | routing 的自包含提示词版，可注入 Codex/CC/OpenCode/Droid |
| `scenarios.md` | S1 之后 | 13 场景详细流程 + 工程能力链 + 场景 I/O 契约表 |
| `overview.md` | S0 / 收尾 | 会话记录格式（overviews/日期/文件），写入前脱敏 |
| `skill-matrix.md` | 需查阶段加载表/工具来源时 | 阶段→skill 加载矩阵 + 外部工具清单 + 跨 agent 安装 |

## B. 工程纪律（superpowers 内化，S2–S5 按需读）

| 文件 | 何时读 | 职责 |
|---|---|---|
| `engineering-catalog.md` | 想知道某个 superpowers skill 去哪了 | 15 个 skill 逐个去向 + 10 个具名能力的输入输出契约 |
| `engineering-workflow.md` | S2 / 隔离 / 收尾 | brainstorm（先调查再开口+回述+红旗）、writing-plans（Interfaces+Self-Review 五问）、worktree、finish 详解 |
| `engineering-tdd.md` | S3 | RED-GREEN-REFACTOR + Verify RED/GREEN 强制细节 + 5 借口 + 红旗 |
| `engineering-debugging.md` | 场景 8 解 bug | 4 阶段根因法 + 完成前验证 |
| `engineering-code-review.md` | S5 / 派子代理时 | 评审严重度分级 + subagent 派单契约/两段式/fix-loop 护栏 |

## C. 编码规则（写码事件触发，非全程加载）

| 文件 | 何时读 | 职责 |
|---|---|---|
| `coding-rules-common.md` | 任何写码动作前 | 通用铁律：类型、禁魔法字符串、注释模板、长度基线、三道锁 |
| `coding-rules-<lang>.md` | 写对应语言代码时 | 该语言注释方案/语法/禁魔法字符串载体/长度基线/验证命令清单 |
| | | `<lang>` ∈ python, typescript, react, vue, go, rust, dart |

> 7 个语言文件靠通配符引用（不逐个列名），这是刻意的渐进式披露——只读当前语言那份。

## D. 规则与门禁（特定场景触发）

| 文件 | 何时读 | 职责 |
|---|---|---|
| `examples-to-spec.md` | 用户举例/参考 X 那样/改这里时 | 样例→完整规格：归纳意图→6 维外推→分类确认→落地核对 |
| `git-commit-rules.md` | S3 绿后 / S5 提交前 | commit 时机、message 格式、暂存纪律（禁 add -A）、红线、检查清单 |
| `security-redlines.md` | S4 安全核对 / 涉及密钥注入时 | 密钥脱敏、不可信输入 fail-closed、危险路由、成本闸门 |
| `test-strategy.md` | S4 / 测试相关场景 | 测试金字塔、Mock 边界、覆盖率达标口径、假绿识别 |
| `architecture-contracts.md` | 设计/改码/接线核对时 | 接线四问 + 断点模式清单，专治"内部通外部断" |

## E. 项目元信息（维护 skill 自身时读）

| 文件 | 何时读 | 职责 |
|---|---|---|
| `roadmap.md` | 想知道某能力为何这样设计 / 加新场景前 | 已激活能力、P2 候选、激活原则、各次改造记录 |

## 加载优先级与冲突

1. **工程纪律 vs 编码规则正交**：前者管"先写测试再实现"，后者管"实现写成什么样"，同时遵守。
2. **流程主控唯一**：conductor 自己执行全部工程阶段，A/B 类文档是它的步骤说明，不是外部 skill。
3. **工具是配角**：grill-design/graphify/code-quality-review 等只在指定阶段被调用，不占能力链节点。
4. **拿不出证据不标完成**：D 类门禁的退出码/行号是硬证据，写在 overview。
