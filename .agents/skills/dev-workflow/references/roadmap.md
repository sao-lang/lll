# 路线图 · 场景与流程增强记录

> 记录已激活与候选的场景/机制，便于追踪演进。
> 状态：`🟢 已激活`（已进 SKILL.md/routing.md/scenarios.md 生效）/ `🟡 候选`（已识别未实现）。
> 当前生效工作流：**十三场景 + 四路径 + S0–S5**。

## 一、已激活（🟢，方案 A 全部落地于 2026-11-19）

### 场景 10 · 代码库理解（只读路径）
- 定位：补"先搞懂"缺口；加载 `graphify` + `scan_gaps.py` + `architecture-contracts.md`。
- 产出：模块职责 + 调用链 + 断点清单 + 理解笔记 → overview。
- 已写入：`scenarios.md` 场景 10、`routing.md` 判定表、`SKILL.md` 场景表。

### 场景 11 · 文档同步（文档路径）
- 定位：治本项目 G07/G13/G15 类"文档-代码不一致"；加载 `grill-design` + `architecture-contracts`。
- 产出：文档-代码差异清单逐条勾销，拿行号证据。
- 已写入：同上三处。

### 场景 12 · 门禁验收 / 发布（验证路径）
- 定位：独立化散落在 S5 的门禁；加载 preflight + verify + scan_gaps + review 全套。
- 出口：门禁全绿 + 证据入 overview，否则禁止合并/发布。
- 已写入：同上三处。

### 场景 13 · 测试补课（验证路径，TDD 变体）
- 定位：给已有代码加 characterization test，区别于 TDD 写新实现；加载 `test-strategy.md`。
- 禁止：无断言凑覆盖率、借机改实现行为。
- 已写入：同上三处。

### 机制 1 · 路径字段（改码/只读/文档/验证）
- S1 除判场景外必判路径，决定后续阶段。已写入 `routing.md` §2.5、`SKILL.md` 场景×阶段矩阵。

### 机制 2 · 只读出口
- 只读路径出口 = "清单已交付 + 证据可追溯"，非"测试全绿"；S4 拆改码/只读两变体。
- 已写入 `SKILL.md` 与 `scenarios.md` 的"只读出口"。

### 机制 3 · 场景 × 阶段矩阵
- 散落的裁剪规则收敛成统一矩阵，作为 S1 硬输出。已写入 `SKILL.md` 与 `routing.md`。

### graphify 接入（场景 10/9）
- 市场 skill `graphify`（代码知识图谱问答）+ 开源 `pip install graphifyy`。
- 已写入 `SKILL.md` 的"代码理解工具"段、`skill-matrix.md` 的 skill 清单、场景 10 步骤。
- 纪律：只给带源码位置的上下文非验证答案；冷启动有成本，短任务不用；不替代 scan_gaps。

### 依赖安装 + skill 加载纪律（2026-11-19）
- 背景：其他会话用 dev-workflow 时 AI 未加载 handoff。根因是 handoff 当时未安装（只给了仓库地址）。
- 已装齐 11 个 skill：superpowers / grill-design / grill-me / code-quality-review /
  handoff / metago-code-review-deep / graphify（市场）+ ponytail / ponytail-review /
  caveman / caveman-compress（从 GitHub 仓库复制到用户级目录）。
- 修复软约束缺陷：SKILL.md 新增"skill 加载纪律"段——先 preflight 确认存在、
  加载失败必须显式降级并写 overview、禁止假装加载、关键依赖缺失要问用户。
- preflight 复核：8 个 skill 缺失已清零；剩余 18 项为语言验证工具缺失（另案）。

## 二、P2 候选（🟡，有真实需求再加）

- 安全审计（只读，加载 `security-redlines.md`）
- 技术债治理（针对已知欠账清单批量处理）
- 依赖升级 / 迁移（运行时/框架升级 + 废弃 API 排查）
- 独立 code review（只要分级清单，不进修复）
- 会话续接（读 handoff + overview 恢复上下文）
- 原型 / POC（明确标记丢弃 or 转正式，禁止混入生产）
- `knowledge-graph-rebuilder`：把 overviews 会话记录做成可查询实体-关系图谱

## 三、激活原则

新增场景前先判：它走哪条路径？现有三机制是否够用？若不够，先补流程机制再加场景——
否则新场景会在 S3/S4 卡死或空跑（这是方案 A 的教训）。
