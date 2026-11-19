# Git 提交规则（conductor 内置规则文档）

> 本文件是 conductor 的**提交纪律**，由 S3（TDD 循环）与 S5（收尾）加载。
> 它不是 skill、不占能力链节点——提交是贯穿各阶段的动作，规则管"何时提交、怎么写、提交什么"。
> 与 `engineering-tdd.md`（绿后提交）、`engineering-workflow.md` 的 `finish`（交付决策）正交，可同时遵守。

## 1. 何时提交

| 时机 | 要求 |
|---|---|
| 每个 TDD 循环转绿后 | RED→GREEN→REFACTOR 完成、测试全绿即可提交；一个循环一个 commit |
| 每个计划任务完成 | `review`（两段式评审）通过后提交，不攒多个任务一起提 |
| 收尾前（S5） | 全量测试绿 + S4 自省门禁通过（scan_gaps / verify 无阻塞项）才提交 |
| 文档/设计变更 | 与对应代码改动同批提交，或单独成 commit，禁止"以后再补" |

**禁止**：测试未绿就提交、自省未过就提交、把多个不相关逻辑塞进一个 commit。

## 2. Commit message 格式

采用 Conventional Commits + 中文描述：

```
<type>(<scope>): <中文简述，一句话>

- <要点 1>
- <要点 2>
```

- **type**：`feat` 新功能 / `fix` 修复 / `refactor` 重构（不改行为）/ `docs` 文档 /
  `test` 测试 / `chore` 杂务 / `perf` 性能 / `ci` 构建 / `revert` 回滚。
- **scope**：模块或能力名（如 `skill`、`runtime`、`tools`、`docs`、`overview`），无合适 scope 可省略括号。
- **简述**：祈使句、中文、不加句号；body 用 `-` 列关键改动，解释"为什么"而非只列"改了什么"。
- 简述 ≤ 72 字符；body 每行 ≤ 100 字符。

示例：
```
fix(skill): 补齐 superpowers 的 git 纪律（worktree/finish 防呆细节）

- 核查发现 git 纪律藏在两个 skill 里，上一轮被压成一句话丢失
- worktree 补 submodule 陷阱/gitignore 先验/原生工具优先
- finish 补 force-push 禁令/discard 确认/清理所有权判断
```

## 3. 暂存纪律（最高优先级 · 本项目的血泪点）

**这是最容易犯错、后果最严重的一条。工作区里常混有其他会话/其他来源的改动，盲加会污染提交。**

1. **禁止 `git add -A` / `git add .` 盲目全加** —— 它们会把无关改动、历史遗留、临时文件一起暂存。
2. **只显式 add 本次任务相关的文件/目录**，一次 add 一组，逐个核对。
3. **提交前必须两步核对**：
   ```bash
   git status --short                 # 看工作区：哪些是本次的，哪些是别人的
   git diff --cached --stat           # 看暂存区：确认只有本次文件
   ```
4. **发现暂存区混入无关文件** → `git restore --staged <file>` 撤出，绝不"顺手一起提交"。
5. **对破坏性改动保持警惕**：看到删除生产代码（`D src/...`）、删独立文档而无替代，先判断是否有意；
   无法确认就不纳入本次提交，向用户说明并等待指示（参见 `architecture-contracts.md`）。
6. **提交后核对**：`git log --oneline -1` 确认 HEAD 内容正确（不仅看消息，必要时
   `git show HEAD:<path>` 核对实际文件——避免"只改了路径没改内容"这类假提交）。

## 4. 禁止事项（红线）

| 禁止 | 原因 |
|---|---|
| `git add -A` / `git add .` 盲加 | 混入无关改动、误提交历史遗留 |
| `git commit --no-verify` | 跳过 hooks 检查，绕过质量门禁 |
| 擅自 `git push --force` | 覆盖他人提交；远端被拒先调查，强推需用户明确要求 |
| 擅自 `git commit --amend` | 改写已存在的提交历史；除非用户明确要求 |
| 提交密钥 / token / `.env` / 凭证文件 | 泄露敏感信息；提交前按 `security-redlines.md` 核对 |
| 提交 `__pycache__` / `.pyc` / 构建产物 / 临时备份 | 污染仓库；用 `.gitignore` 排除 |
| 未通过 S4 验证就提交 | 门禁形同虚设（证据优于声称） |
| 一次提交混合多个不相关主题 | 破坏原子性，回滚困难 |

## 5. 提交前检查清单（S3/S5 逐条过）

- [ ] 相关测试全绿（S4 的 verify 退出码为 0）
- [ ] 本次改动只有一个逻辑主题
- [ ] commit message 符合 `<type>(<scope>):` 格式，中文简述
- [ ] `git status --short` 已看，工作区里的其他改动已识别
- [ ] `git diff --cached --stat` 已看，暂存区只有本次文件
- [ ] 无密钥 / token / 凭证 / 构建产物 / 临时文件
- [ ] 未使用 `--no-verify` / `--force` / 擅自 `--amend`
- [ ] 提交后已 `git log --oneline -1`（必要时核对 HEAD 文件内容）

## 6. 在 conductor 流程中的挂载

- **S3 TDD 实现**：每个 `tdd` 循环绿后提交一次（规则 §1）；`execute` 每个任务评审通过后提交。
- **S5 review 收尾**：`finish` 交付决策前，确认全量绿 + S4 通过 + 本清单过，再提交。
- **overview 记录**：每次提交把 commit hash 与简述写入本轮 overview 的"变更文件清单/验证结果"。
