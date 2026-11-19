# 会话摘要：补全 superpowers 提取细节 + 整理 references

## 环境
- 日期时间：2026-11-19 18:59 GMT+8
- 项目：lll / 工作区：E:/vsc-workspace/lania-zip/lll
- OS / Shell：Windows / Git Bash
- IDE / Agent：WorkBuddy

## 场景与 skill
- 场景：项目重构（conductor skill 自身）
- 主控：conductor（本轮即维护它自身）

## 会话摘要（≤10 条要点）
1. 用户质疑：brainstorming/writing-plans 在 superpowers 是完整 skill，压成能力只留简单表述，是否影响功能。
2. 拉取原始仓库逐段对比：11 skill ~4400 行 vs 提取 245 行，压缩 95%；确认丢失的是"执行细节+防呆层"而非步骤。
3. 修正上一轮两处误判：subagent-driven 不是废了而是缺三条护栏；brainstorming 致命缺失是"先调查再开口"而非 Red Flags。
4. 补 engineering-tdd.md：Verify RED(fail vs error)/Verify GREEN(全项目套件、漏报即失真)/5 借口/红旗。
5. 补 engineering-workflow.md：brainstorm 首条消息+回述+两红旗；writing-plans Interfaces+Self-Review 五问。
6. 补 engineering-code-review.md：subagent 派单契约+fix-loop+完成台账+借口表。
7. 整理：新增 references/README.md 索引（25 份分五类+加载优先级+冲突规则）；来源致谢的上游文件名加限定词；SKILL.md 挂索引指针。
8. 校验：quick_validate 通过、补充关键字全部命中、索引文件数与声明一致(25)。

## 关键决策与理由
- 决策：按修正清单补而非全搬 4400 行 — 理由：全搬违背省 token 初衷；只补"缺了就做错"的执行细节。
- 决策：不物理移动 references 到子目录 — 理由：会破坏所有交叉引用路径；加索引即可导航（最小改动）。
- 决策：TDD 借口表只补最狠 5 条（完整 13 条留链接到上游）— 理由：性价比，最狠的挡住 90% 找借口场景。

## 变更文件清单
- references/engineering-tdd.md — Verify RED/GREEN + 5 借口 + 红旗
- references/engineering-workflow.md — brainstorm + writing-plans 详解
- references/engineering-code-review.md — subagent 护栏
- references/README.md — 新增索引
- references/roadmap.md — 记录
- SKILL.md — 索引指针 + 文件数

## 验证结果
- skill 格式：quick_validate 通过
- 补充关键字：Verify RED/沉没成本/Interfaces/Self-Review/look before you ask/显式指定 model/fix-loop 全部命中
- 索引一致性：25 份 = 声明 25 份

## 交接
- 待办：完整 13 条 TDD 借口表、brainstorming 的 visual companion 未搬（按需再取）。
- 脱敏声明：本文件不含密钥 / token / PII
