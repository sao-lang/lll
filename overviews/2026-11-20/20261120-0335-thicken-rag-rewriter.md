# 会话摘要：加厚 R6 默认查询改写器（无模型）

## L0 索引卡（续接时先读这一段，≤12 行）
- 场景 / 路径：链 ①场景3 补充设计（文档）→ ②场景6 设计驱动重构（改码） / 改码
- 当前闸门：设计闸门已过（用户拍板5决策+spec已写+详设/总设计已同步）；代码门禁未过
- 交付产物：docs/02-模块详设/Agent/R6-改写器加厚-设计规格.md（spec）；R6 详设 §6/§7.6/状态清单、总设计改写行已更新
- 未完成项：①代码未实现 ②测试锁未更新 ③门禁/review 未跑
- 下一步：S3 TDD（先读编码规则→写失败测试→实现→门禁）

## 环境
- 日期时间：2026-11-20 03:35 GMT+8
- 项目：AgentKit (lll) / 工作区：E:/vsc-workspace/lania-zip/lll
- OS / Shell：Windows / Git Bash
- 运行时：Python 3.13（项目 .venv，含 pytest/mypy/pyright；系统 python 无这些工具，验证一律用 .venv）
- IDE / Agent：WorkBuddy / conductor 主控

## 场景与 skill
- 场景：5 项目迭代（默认实现替换，含设计变更 → 先改设计再改代码）
- 加载的 skill：conductor（主控）；S2 将按需 grill-design，S3 按 coding-rules-python
- 主控：conductor（唯一流程主控）

## 用户诉求（原文要点）
- "把这个默认改写器封装厚一点，不用使用模型，但是不要当前这种实现，使用 conductor 执行，要先改设计，再改代码"
- 目标对象：R6 `MultiQueryRewriter`（当前 = 把原 query 复制 n 份，`model_copy` 深拷贝，内容不变）
- 硬约束：①不使用 LLM/模型 ②不能是当前这种"复制多份"实现 ③先改设计后改代码 ④走 conductor 流程

## S0 记录
- preflight.py：报 18 项缺失，均为非本项目的 typescript/react/vue/go/dart 工具 + 系统 python 无 python 工具；项目 .venv 已有 pytest/mypy/pyright，S4 验证用 .venv（降级：ruff/pydocstyle 用项目既有 mypy strict + pyright + gate_check 等价门禁，详见 11-19 日志既有做法）
- handoff：S0 要求实际调用；本会话为同日连续上下文（前面已读 R6 全部源码 + 画链路图），无跨会话交接必要，但按纪律仍生成轻量交接于 S5

## L1 摘要（需要背景时读）
### 会话摘要
1. 当前默认改写器 `MultiQueryRewriter.rewrite()`（rewrite.py:37-39）= `[query.model_copy(deep=True) for _ in range(n)]`，内容零改动，仅占位保证链路可跑
2. 用户要"厚"且"不用模型"——需在 S2 brainstorm 出确定性（非 LLM）的改写策略并用设计树拷问，禁止猜方案
3. pipeline.py:210-213 把 rewrite 置为可选增强；改写产出多个 query → 各自 embed → 混合召回 → `_dedup(chunk_id)` 去重（pipeline.py:220）
4. 设计先行：改 R6 详设 §改写段 + 总设计涉及处，规格确认后再 TDD 代码

### 关键决策与理由（grill-design 收尾，用户拍板）
- 策略：规则扩展（同义词替换）+ 按标点/连接词子查询分解 — 无模型、可解释、真实增益
- 词表：内置精简表（≥8组）+ 构造注入替换
- 类名：直接改造 MultiQueryRewriter（不新增类/别名）— 用户明确
- 红线：保留"不引入原问题没有的实体"，同义词映射值定义为等价词，不违反红线
- 分解：标点 。？！，,;；、 + 连接词 和/与/及/以及/并/and/or/then
- 事实（自查）：项目无 jieba/nltk 分词库 → 纯 str 规则、无新依赖；Query extra=forbid 非 frozen，model_copy(deep=True) 派生；test_components.py:93-95 是旧行为锁，必须更新
- n_variants 语义：固定产 N 份 → 最多 N 份不同变体（含原串），明示默认

### 验证结果（全部带命令与退出码，工具用项目 .venv）
- 测试：`PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/r6 --tb=no` — 41 passed；全量 `tests/r1..r6 + tests/ports` — 506 passed（退出码 0）
- 覆盖率：gate_check r6 G1 — 94%（阈值 85%）
- mypy strict：`.venv/Scripts/mypy.exe --strict src` — Success, 55 source files, 0 error（退出码 0）
- pyright：`PYTHONPATH=src .venv/Scripts/pyright-python.exe src/harness/rag/rewrite.py` — 0 errors
- 门禁：`PYTHONPATH=src .venv/Scripts/python.exe tests/tools/gate_check.py --module r6` — 14/14 全绿
- verify.py：注入 .venv PATH 后 `mypy --strict src` 与 `pytest --cov=src --cov-fail-under=80` 两项 PASS（退出码 0）；ruff/pydocstyle 未安装（exit=-1 命令不存在）→ 按 S0 登记的降级，其职责由 pyright + gate_check 红线扫描/模块 docstring 检查等价覆盖
- scan_gaps.py：`--path src/harness/rag` — 无启发式命中；debt_ledger.py — 债指数 0
- demo：`demo_r6.py` — PASS 全闭环
- 独立 review：轮1 1 Blocker → 修复 → 轮2 Blocker=0 通过（详见「独立 review 结果」节）

### 未完成项与下一步（完整）
- S1 路由 → S2 设计（先） → S3 TDD（后） → S4 自省 → S5 独立 review + 提交
- suggested skills：grill-design（S2 拷问设计）、coding-rules-python（S3 写码前）

## 独立 review 结果（S5 门禁证据）
- **Blocker = 0，无阻塞项，允许交付。**
- 独立审查报告（轮1，1 Blocker + 3 Should + 3 Nit）：`20261120-0335-thicken-rag-rewriter.review.md`
- 独立复审报告（轮2，Blocker=0 通过，2 Should + 1 Nit）：`20261120-0335-thicken-rag-rewriter.review2.md`
- 轮1 Blocker B1（默认 N=3 同义变体被子句挤出）已修：交错排序 + 整串同义保底 1 席；实测默认输出 `[原串, 同义"检索", 子句"向量召回"]`，两类策略均入窗。复审实测确认清零。
- 轮2 Should S-R1/S-R2（多子句后段被挤、大 N 增益弱）主动修复：两轮填充（基础覆盖轮 = 原串+1同义+全部子句；增益填充轮 = 同义余量+子句同义）。N=6/4子句全部入窗，N=10 再填增益。新增 `test_sr1_all_subqueries_survive_before_synonym_filler` 回归。
- Nit N1（文档 strategy 名不一致）已修：R6 详设 §7.6 参数表去掉不存在的 strategy 入参。
- 复审独立性：两轮均为全新上下文子代理，未参与实现；fix loop 未复用旧报告结论。

## L2 详情引用（用到才按路径加载）
- 独立审查报告：`overviews/2026-11-20/20261120-0335-thicken-rag-rewriter.review.md`
- 独立复审报告：`overviews/2026-11-20/20261120-0335-thicken-rag-rewriter.review2.md`
- 变更与验证证据：见本文件 L1 摘要「验证结果」

## 交接
- handoff 文档路径：待 S5 生成
- 脱敏声明：本文件不含密钥 / token / PII
