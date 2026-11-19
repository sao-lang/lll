# 会话摘要：设计 dev-workflow 工作流 skill

## 环境
- 日期时间：2026-11-19 16:19 GMT+8
- 项目：AgentKit（工作区 `E:/vsc-workspace/lania-zip/lll`）
- OS / Shell：Windows / Git Bash
- 运行时：Python 3.13.14
- Agent：WorkBuddy（Claude）

## 场景与 skill
- 场景：项目初始化（新建 skill 资产）
- 本轮未加载外部 skill；按对话设计产出
- 主控：dev-workflow（本轮即设计它本身）

## 会话摘要
1. 调研确认 skill 仓库：superpowers(obra)、ponytail(DietrichGebert)、caveman(JuliusBrussee)、handoff(mattpocock)；拷问类 grill-design/grill-me/code-quality-review/metago-code-review-deep。
2. 确认分层：superpowers 独占工程流程主控；dev-workflow 降级为治理外壳（路由/会话记录/自省/规则治理）。
3. 解决"代码规则谁加载"：S1 种会话级约束，写码事件触发按需读取，不在 S3 动态注入。
4. 设计三道锁强制机制：写码前读规则 / 写码中自检 / 写码后 linter 退出码门禁。
5. 编码规则覆盖 7 语言 + 通用铁律，新增禁魔法字符串（枚举/常量）硬规则。
6. 目录按跨 agent 通用约定放在项目根 `.agents/skills/dev-workflow/`。
7. 产出 12 个文件（SKILL.md + 11 references），官方 quick_validate.py 校验通过。

## 关键决策与理由
- 不修改 superpowers 本体 — 市场更新会覆盖；只在 dev-workflow description 单向声明优先级。
- coding-rules 做成 reference 而非 skill — 知识不是流程，避免主控冲突；渐进式披露省 token。
- 规则正文按需加载 — 与项目 R5 的 L1/L2/L3 披露同构。
- 机器门禁为唯一客观强制 — prompt 自律可能打折，退出码不可打折。

## 变更文件清单
- `.agents/skills/dev-workflow/SKILL.md` — 入口，定位/六场景/矩阵/强制机制/跨 agent 说明
- `.agents/skills/dev-workflow/references/coding-rules-common.md` — 通用铁律（含禁魔法字符串 §2、三道锁 §6）
- `.agents/skills/dev-workflow/references/coding-rules-{python,typescript,react,vue,go,rust,dart}.md` — 7 语言规则 + 验证命令
- `.agents/skills/dev-workflow/references/skill-matrix.md` — 阶段加载表 + 仓库地址 + 跨 agent 安装
- `.agents/skills/dev-workflow/references/scenarios.md` — 六场景流程 + 缺口台账法
- `.agents/skills/dev-workflow/references/overview.md` — 本记录规范

## 验证结果
- skill 格式校验：`python quick_validate.py .agents/skills/dev-workflow` — Skill is valid（退出码 0）
- 文件数：12；总行数：767
- 本轮为文档型产出，无单元测试适用

## 未完成项与下一步
- 外部 skill（superpowers/ponytail/caveman/handoff/grill-*/code-quality-review）未在本轮安装，仅提供仓库地址与安装命令。
- suggested skills：下次开发任务时按 skill-matrix.md 加载 superpowers + ponytail + caveman + coding-rules。

## 交接
- handoff 文档路径：无（本轮为设计会话）
- 脱敏声明：本文件不含密钥 / token / PII
