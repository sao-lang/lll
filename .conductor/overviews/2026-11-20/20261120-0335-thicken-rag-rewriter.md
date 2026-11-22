# 会话摘要：加厚 R6 默认查询改写器（无模型）

## L0 索引卡（续接时先读这一段，≤12 行）
- 场景 / 路径：链 ①场景3 补充设计（文档）→ ②场景6 设计驱动重构（改码） / 改码
- 当前闸门：设计闸门已过（用户拍板5决策+spec已写+详设/总设计已同步）；代码门禁未过
- 交付产物：docs/02-模块详设/Agent/R6-RAG检索增强.md §11（spec）；R6 详设 §6/§7.6/状态清单、总设计改写行已更新
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
- 独立审查报告（轮1，1 Blocker + 3 Should + 3 Nit）：`20261120-0335-thicken-rag-rewriter.review.md`（已内嵌于本文件「独立 review 报告」章节）
- 独立复审报告（轮2，Blocker=0 通过，2 Should + 1 Nit）：`20261120-0335-thicken-rag-rewriter.review2.md`（已内嵌于本文件「独立 review 报告」章节）
- 轮1 Blocker B1（默认 N=3 同义变体被子句挤出）已修：交错排序 + 整串同义保底 1 席；实测默认输出 `[原串, 同义"检索", 子句"向量召回"]`，两类策略均入窗。复审实测确认清零。
- 轮2 Should S-R1/S-R2（多子句后段被挤、大 N 增益弱）主动修复：两轮填充（基础覆盖轮 = 原串+1同义+全部子句；增益填充轮 = 同义余量+子句同义）。N=6/4子句全部入窗，N=10 再填增益。新增 `test_sr1_all_subqueries_survive_before_synonym_filler` 回归。
- Nit N1（文档 strategy 名不一致）已修：R6 详设 §7.6 参数表去掉不存在的 strategy 入参。
- 复审独立性：两轮均为全新上下文子代理，未参与实现；fix loop 未复用旧报告结论。

## L2 详情引用（用到才按路径加载）
- 独立审查报告：`overviews/2026-11-20/20261120-0335-thicken-rag-rewriter.review.md`（已内嵌于本文件「独立 review 报告」章节）
- 独立复审报告：`overviews/2026-11-20/20261120-0335-thicken-rag-rewriter.review2.md`（已内嵌于本文件「独立 review 报告」章节）
- 变更与验证证据：见本文件 L1 摘要「验证结果」

## 交接
- handoff 文档路径：待 S5 生成
- 脱敏声明：本文件不含密钥 / token / PII


---

## 独立 review 报告（原 review 文件已合并至此，多轮按 Round 分节）

### Round 1 · 20261120-0335-thicken-rag-rewriter.review

**审查日期**：2026-11-20
**审查对象**：`MultiQueryRewriter` 由"复制 N 份占位"改为"无模型规则改写（同义词等价替换 + 标点/连接词拆子句）"
**意图源**：`docs/02-模块详设/Agent/R6-RAG检索增强.md §11`（§1 / §5 FR-1~FR-8、NFR-1~3 / §13）
**审查角色**：独立审查员，未参与实现；未改动任何代码
**证据基础**：`git diff`、`pytest tests/r6 -q`（`.venv`，37 passed）、一次性只读验证脚本输出（见各 finding）

---

## 一、结论

| 项 | 数量 |
|---|---|
| **Blocker（阻断交付）** | **1** |
| **Should（建议修）** | **3** |
| **Nit（可留）** | **3** |

**判定：不通过。** 必须先修 **B1** 再复核；B1 是"功能断点（L3）"——同义词替换这一核心召回增益手段在默认参数 `n_variants=3` 下被子句拆分结构性地挤出截断窗口，实际退化为"仅按标点拆子句"，未兑现设计 §2 成功标准中"召回 union 因**词面变化**覆盖更多块"的承诺。

用户点名的两个重点怀疑项已实测核实：**FR-3 含空格连接词（`" and "` / `" or "`）不会产生非子串**（所有拆分结果均为原串连续子串）；**FR-2 注入词表确实完全替换内置表**。这两点实现正确，不构成 finding。

---

## 二、审查依据速览（供 findings 参照）

### 接线四问（pipeline.py:206-219）

| 问题 | 结论 |
|---|---|
| 谁调用它 | `RAGPipeline.query()` 第 207-210 行：`rewritten = await self.rewrite.rewrite(q)` → `queries = list(rewritten)`。改动后返回 `list[Query]`，协议签名未变，兼容。 |
| 它调用谁 | 无外部依赖；纯字符串 + `Query.model_copy`。符合 D1/NFR-1。 |
| 变体 text 是否真进入检索 | 是。第 213 行 `embed([sub.text for sub in queries])`，第 216 行逐 `sub` 调 `retriever.retrieve(sub, vec)`。变体词面真实参与向量召回与关键词通道。 |
| 去重是否配套 | 是。第 217 行 `_dedup(hits)` 按 `chunk_id` 保留最高分，多 query 命中不重复引用。**注意**：_dedup 是 query 间命中去重，与 rewrite 层"变体文本去重"是两层，均齐备。 |

**接线结论：闭环，无兼容性断点。** `pipeline.py` 本次 diff 仅为 import/`__all__` 顺序调整，不涉及接线逻辑。

### FR/NFR 逐条核对

| 需求 | 状态 | 依据 / 证据 |
|---|---|---|
| FR-1 签名兼容、n_variants 语义 | ✅ 通过 | `__init__(n_variants=3, synonyms=None)`；`max(1, n)`；`test_fr1` |
| FR-2 内置≥8组 + 注入完全替换 | ✅ 通过 | 内置 12 组（rewrite.py:27-40）；`self.synonyms = synonyms if ...`（:131）；`test_fr2` 验证注入生效且内置未误用 |
| FR-3 标点/连接词拆子句且为连续子串 | ⚠️ 实现通过、测试未锁定 | 切片实现（:69-82）保证子串；实测 `foo and bar`→`['foo','bar']` 等均为子串；但 `test_fr3` 未直接断言此契约 → 见 S3 |
| FR-4 同义展开产文本不同变体 | ❌ **默认路径失效** | 单无子句输入可用（`召回`→`['召回','检索']`）；含标点输入默认 N=3 时全部同义变体被截断 → 见 B1 |
| FR-5 去重保序/原串第一/上限/无可改返1 | ✅ 通过 | `test_fr5_*`；实测 `xyz`→1 份、`len<=N`、互不相同 |
| FR-6 仅改 text、其余字段深拷贝继承 | ✅ 通过 | `model_copy(deep=True, update=...)`（:165）；`test_fr6` 逐字段断言 |
| FR-7 红线：不引入新实体 | ⚠️ 内置表通过、注入场景可破 | 内置表词项均来自原串或映射值；注入无等价约束 → 见 S2 |
| FR-8 确定性 | ⚠️ 表层通过、别名可破 | dict 插入序固定；但注入 dict 被按引用持有 → 见 S1 |
| NFR-1 无新依赖/无 IO | ✅ 通过 | 仅 `typing`/`harness.rag.types`；`test_nfr1`（方法脆弱见"未决"） |
| NFR-2 旧行为锁更新 | ✅ 通过 | `test_components.py:84-105` 已替换为新契约断言 |
| NFR-3 协议与 pipeline 不变 | ✅ 通过 | Protocol 未改；接线四问闭环；pytest 37 passed |

---

## 三、Findings

### Blocker

#### B1. 默认 `n_variants=3` 下同义替换被子句挤出截断窗口，FR-4 核心召回增益结构性失效

- **位置**：`src/harness/rag/rewrite.py:147-166`（组装顺序 :149 / :151 / :152-153，截断 :166）
- **现象（实测）**：
  - `rewrite("向量召回，关键词精排")` 默认 N=3 → `['向量召回，关键词精排', '向量召回', '关键词精排']` ——**无任何同义替换变体**。
  - 同输入 N=6 → 才出现 `'向量检索，关键词精排'`、`'向量召回，关键词重排'`、`'嵌入召回，关键词精排'`。
  - `rewrite("召回")`（无子句）→ `['召回', '检索']`，故 `test_fr4` 用无子句输入恰好通过，掩盖了该缺陷。
- **后果**：
  1. 变体顺序为 `[原串, 全部子句, 整串同义替换, 子句同义替换]`（:147-153），截断（:166）总在尾部切。只要子句数 ≥ 2，默认 N=3 就全部被子句占满，同义替换永远排不进结果。
  2. 子句与原串**词项完全相同**（仅少标点/组合）。项目关键词通道是"词面覆盖扫描"，子句变体对它几乎零新增覆盖；唯一能带来"词面变化"的是同义替换词（检索/重排/嵌入/词面），而它被截掉。结果是**多路召回退化为近似占位实现**，直接违背设计 §1 意图与 §2 成功标准。
  3. 这是典型的 L3 功能断点：FR-4 代码存在、单测通过，但默认配置的用户路径上不生效。
- **具体改法**（任选其一，推荐组合前两项）：
  1. **交错排序**保证每类策略都有代表进入前 N：`candidates = [原串] + interleave(同义替换(整串), 子句, 同义替换(子句))`。例如按"原串 → 整串同义替换 → 子句1 → 子句1同义替换 → …"组装，使 N=3 至少含 1 个同义替换变体。
  2. **策略保底**：在预算内先为每种策略预留 1 个名额（原串 + 至少 1 个同义替换 + 至少 1 个子句），剩余名额再按原顺序填满；不足时 Give Way 给增益更高的策略。
  3. 配套新增回归测试：`rewrite("向量召回，关键词精排")` 默认 N=3 时断言 `any(任一内置同义值 in v.text for v in out[1:])`。

### Should

#### S1. 注入词表被按引用直接持有，外部修改可破坏 FR-8 确定性（D4）

- **位置**：`src/harness/rag/rewrite.py:131`
- **现象（实测）**：构造 `r = MultiQueryRewriter(synonyms=d)`（初始 `d={'foo':('bar',)}`）输出 `['foo','bar']`；随后外部执行 `d['foo']=('hack',)`，同一实例输出变为 `['foo','hack']`。
- **后果**：`self.synonyms = synonyms` 保存的是调用方 dict 的别名。装配层若复用/修改该 dict，改写行为在两次 `rewrite()` 之间静默改变，违反 D4"相同输入恒定产出相同变体"，也使 `test_fr8` 的确定性保证在真实装配场景下失效。并发下若另一方在 `_synonym_variants` 遍历 `synonyms.items()`（:106）时修改 dict，还会抛 `RuntimeError: dictionary changed size during iteration`。
- **具体改法**：构造期拷贝为内部不可变视图，例如 `self.synonyms = dict(synonyms) if synonyms is not None else dict(_DEFAULT_SYNONYMS)`；或进一步冻结为 `MappingProxyType`。同时补一条"构造后修改源 dict 不影响输出"的测试。

#### S2. 注入词表无等价性约束，红线 FR-7 在注入场景可被直接破坏

- **位置**：`src/harness/rag/rewrite.py:106-110`、`:131`
- **现象（实测）**：注入 `{'苹果': ('微软',)}` 后，`rewrite("苹果公司")` → `['苹果公司', '微软公司']`，凭空引入原问题没有的实体"微软"。
- **后果**：设计 D2 把"同义词映射值"定义为等价词，前提是注入词表本身确实等价。代码对 value 不做任何校验，FR-7 红线完全依赖注入者自觉；一旦注入错误/恶意映射，改写器成为实体注入通道（虽下游有 R5 注入检测，但那是 prompt 注入范畴，挡不住业务实体污染）。规格 §11 把"不可信 query 只做字符串处理"写得很清楚，但对"不可信词表"没有防护。
- **具体改法**：至少在 docstring/文档显式声明"注入词表的 value 必须为 key 的等价词，等价性责任在注入方"；更稳妥可在构造期提供可选校验钩子，或限制 value 必须能在原 query 词项中找到（但这会削弱 FR-2 的领域词表语义，需权衡）。建议先落文档责任声明 + 一条"错误注入示例"的反面测试记录边界。

#### S3. FR-3 测试未真正锁定"每个子句是连续子串"契约

- **位置**：`tests/r6/test_rewrite.py:40-48`
- **现象**：`test_fr3` 只断言 `assert any(v.text != text and v.text in text for v in out)`，且 `out` 被 `n_variants=6` 截断；它既未直接调用 `_split_subqueries`，也未断言"每个子句都在原串中"。按 B1 的截断顺序，n=6 时子句恰好排在前面，测试才碰巧通过。
- **后果**：FR-3 的核心验收（"断言每个子句 `in query.text`"，规格 §5 原文）未被精确锁定。未来若改动切分逻辑（如给子句加上下文、拼接、strip 过度）产生非子串，或 B1 修复改变排序，该测试不会报警，红线 D2 失去测试防护。
- **具体改法**：直接 `from harness.rag.rewrite import _split_subqueries`，对多组输入（中英文标点、`和/与/及/以及/并`、`and/or/then`、连接词相邻如 `foo and then bar`）断言 `all(p in text for p in _split_subqueries(text))`，并对至少一例断言精确分片列表。

### Nit

#### N1. 文档参数表 `strategy=rule_expand` 与实现 `name="multi_query"` 且无 `strategy` 入参不一致

- **位置**：`docs/02-模块详设/Agent/R6-RAG检索增强.md:170`（对照 `docs/01-总览与设计/AGENT-总设计.md:1038` 的改写行用词尚可）
- **后果**：文档写 `strategy / n_variants / synonyms` 默认 `rule_expand / 3 / 内置精简词表可注入`，但代码 `MultiQueryRewriter.name == "multi_query"`（`rewrite.py:117`），构造函数也没有 `strategy` 参数。读者会误以为存在可切换的策略枚举或配置入口。
- **具体改法**：把默认值改为 `multi_query`（与 `name` 对齐），或注明 `rule_expand` 只是逻辑策略描述而非可注入参数。

#### N2. `_split_subqueries(text)` 在同一方法内被调用两次

- **位置**：`src/harness/rag/rewrite.py:149` 与 `:152`
- **后果**：对同一文本重复 O(len(text)×token数) 的线性扫描；当前规模下开销可忽略，但属于明显冗余，也让"子句来源"出现两处需同步维护的点。
- **具体改法**：`subqueries = _split_subqueries(text)` 计算一次，:149 用 `candidates.extend(subqueries)`，:152 改为 `for piece in subqueries`。

#### N3. 英文连接词切分产生带前导空格的子句，污染变体文本

- **位置**：`src/harness/rag/rewrite.py:72-77`（配合 :20-23 的 `" and "` / `" or "`  token）
- **现象（实测）**：`'foo and then bar'` → `['foo', ' bar']`（`then` 前的空格残留为尾片段的前导空格）。
- **后果**：`" bar"` 仍是原串连续子串（不违反 FR-3），但该空格随变体 text 进入 embed 与关键词通道，对分词/词面匹配引入噪声，降低变体质量。
- **具体改法**：切片入列前 `.strip()`（strip 后仍是原串子串，红线不破），同时跳过 strip 后为空的片段。

---

## 五、Fix loop 轮次 1（2026-11-20，执行者修复后）

执行者已针对本报告全项修复，证据如下（行号为修复后）：

| 项 | 修复动作 | 验证证据 |
|---|---|---|
| B1 | rewrite.py 改交错排序：`[原串, 整串同义(限1), 子句1, 子句1同义(限1), 子句2…]`；整串同义限 1 做策略保底 | 实测 N=3 输出 `['向量召回，关键词精排', '向量检索，关键词精排', '向量召回']` = 原串+同义+子句，两策略都有代表；`test_b1_default_n_balances_strategies` 断言两类策略均入窗 |
| S1 | 构造器 `dict(source)` 浅拷贝，隔离外部 dict | `test_s1_injected_synonyms_copied_at_construction` |
| S2 | 构造器 docstring + 设计规格 FR-2 补充：注入词表等价性责任在注入方 | 文档已更新 |
| S3 | 新增直接测 `_split_subqueries` 的精确断言（中英文标点/连接词） | `test_s3_split_subqueries_are_contiguous_substrings` |
| N1 | 参数表 `rule_expand` 保留为逻辑策略描述（name 仍 multi_query，无 strategy 入参）——执行者判断不必改代码，仅文档表述，见复审确认 | 待复审 |
| N2 | `subqueries = _split_subqueries(text)` 只算一次 | rewrite.py rewrite 方法 |
| N3 | 子句切片 `.strip()` 去空格；`_SPLIT_TOKENS` 中 then 本身是分隔符，测试期望修正为 `['foo','bar']` | `test_s3` 含 `foo and then bar` 用例 |
| 未决4 | `_synonym_variants` 过滤 replaced==text 的等值映射 | rewrite.py |

**修复后验证**：`pytest tests/r6` 40 passed；全量 505 passed；`mypy --strict src` 0 error（55 文件）；pyright 0 error；`gate_check --module r6` 14/14 全绿（覆盖率 94%）。

> 复审须由**新的独立子代理**执行，禁止复用本报告结论。

## 四、未决与建议

1. **B1 修复优先级最高**：它使改动的核心价值在默认配置下落空。修复时务必保留"原串第一 + 去重保序 + 上限截断"（FR-5），并新增"默认 N 下同义替换至少出现一次"的回归断言，防止排序调整再次把它挤掉。
2. **测试覆盖口径**：本次仅执行 `pytest tests/r6 -q`（37 passed）。规格 §12 还要求 `.venv` 下 `mypy strict` + `pyright` + `gate_check.py --module r6` + `verify.py` + 覆盖率≥95%；这些门禁本次审查未执行，建议实现方在修完 Blocker 后补齐证据，尤其是 NFR-3 的 mypy strict 0 error。
3. **NFR-1 测试方法脆弱**：`test_rewrite.py:106-114` 用 `inspect.getsource` + 字符串包含匹配来禁用 `open(`/`requests` 等，既无法识别动态导入，也可能误伤注释/docstring 中的字样（当前 docstring 含"文件"等词但不含禁用字面量故通过）。建议改用 AST 扫描 `Import`/`ImportFrom`/`Call` 节点。
4. **docstring 小瑕疵**：`_synonym_variants` 声明"均与原串不同"（:103），但当注入 `{"k": ("k",)}` 时 `replace` 结果等于原串（虽被去重吞掉）。可加 `if new != text` 过滤或修正措辞。
5. **已核实无需修改的点**（避免后续误改）：① FR-3 空格连接词切分不会产生非子串——切片 `text[start:cursor]` 始终跳过分隔符本身，所有产物均为原串连续子串；② FR-2 注入词表为完全替换，内置表不会残留参与改写。
6. **范围提示**：`git status` 显示工作区另有 `pipeline.py`（仅 import/`__all__` 顺序）及多处文档/目录变更；本报告仅覆盖用户指定的审查范围，未对上述额外改动做功能审查。

### Round 2 · 20261120-0335-thicken-rag-rewriter.review2

**复审日期**：2026-11-20
**复审对象**：fix loop 轮次 1 之后的 `MultiQueryRewriter` 改动（交错排序 + `limit=1` + 构造器拷贝 + strip）
**意图源**：`docs/02-模块详设/Agent/R6-RAG检索增强.md §11`（§5 FR-1~FR-8、§1 价值排序、§13）
**上一轮报告**：`overviews/2026-11-20/20261120-0335-thicken-rag-rewriter.review.md`（三、Findings + 五、Fix loop 轮次 1） 〔报告已内嵌本文件「独立 review 报告」章节〕
**复审角色**：独立复审员，全新上下文，未参与实现、未参与上一轮审查；**未改动任何代码**
**证据基础**：用户指定 B1 实测命令原样输出；补充边界实测脚本；`pytest tests/r6 -q`（40 passed）；`mypy --strict src`（55 文件 0 error）；`gate_check.py --module r6`（14/14）；`git diff` / `git show HEAD`

---

## 一、复审结论表

| 项 | 数量 | 说明 |
|---|---|---|
| **Blocker（阻断交付）** | **0** | 上轮唯一 B1 已实测清零，默认 N=3 两类策略均入窗 |
| **Should（建议修）** | **2** | 均为 fix 引入的"策略次优分配"，非功能断点，不阻断交付 |
| **Nit（可留）** | **1** | 上轮遗留 N1 文档表述，未修 |

**判定：通过（Blocker = 0）。** B1 核心召回增益在默认配置下已真正兑现；但交错排序 + 固定 `limit=1` 引入了 2 个 Should 级的策略失衡（多子句后半被挤、大 N 时词面增益被过度削弱），建议在本轮或下轮一并修掉。

**最严重一条：S-R1（多子句时后半子句被子句同义变体挤掉）**——见第五节。

---

## 二、B1 实测复核（首要项）

用户指定命令原样执行，输出：

```
['向量召回，关键词精排', '向量检索，关键词精排', '向量召回']
```

逐项判定：

| 变体 | 判定 | 依据 |
|---|---|---|
| `'向量召回，关键词精排'` | 原串，排第一 | FR-5 `result[0].text == query.text` ✓ |
| `'向量检索，关键词精排'` | **同义替换变体**（召回→检索，词面变化） | 核心召回增益入窗 ✓ |
| `'向量召回'` | **子句变体**（原串连续子串，粒度变化） | FR-3 独立检索视角 ✓ |

**结论：默认 N=3 同时含同义替换变体与子句变体，B1 真修好，Blocker 清零。**

机制确认（`rewrite.py:164-190`）：组装顺序为 `[原串] + 整串同义(limit=1) + 每子句[子句, 子句同义(limit=1)]`，整串同义被提到子句之前，故不再被子句挤出截断窗口；去重保序（`:178-184`）与上限截断（`:187-190`）保留，FR-5 未被破坏。

---

## 三、接线四问复核（`pipeline.py:206-219` / `:270-277`）

| 问题 | 结论 |
|---|---|
| 谁调用它 | `RAGPipeline.query()` `:207-210`：`rewritten = await self.rewrite.rewrite(q)` → `queries = list(rewritten)`。协议签名未变，兼容 ✓ |
| 它调用谁 | 无外部依赖；纯字符串 + `Query.model_copy`。符合 D1 / NFR-1 ✓ |
| 变体 text 是否真进入检索 | 是。`:213 embed([sub.text for sub in queries])`、`:216 retriever.retrieve(sub, vec)`。词面变化真实参与向量与关键词通道 ✓ |
| 去重是否配套 | 是。`:217 _dedup(hits)` 按 `chunk_id` 保留最高分（`:270-277`），处理 query 间命中重复；与 rewrite 层"变体文本去重"是两层，均齐备 ✓ |

**接线结论：闭环，无兼容性断点。** NFR-2 行为锁已更新（`test_components.py:84-105`，已核对 diff）：旧断言"3 份且 text 全等"替换为新契约（无可改只产 1 份、有命中产文本不同变体、逐字段继承）。

---

## 四、上一轮 S1/S2/S3/N2/N3 + 未决 4 修复确认

| 项 | 位置 | 确认结论 |
|---|---|---|
| S1 注入词表被按引用持有 | `rewrite.py:145-146` | `source = synonyms or _DEFAULT_SYNONYMS`；`self.synonyms = dict(source)` 浅拷贝隔离外部 dict。实测：外部改 `d['foo']=('hack',)` 后输出仍 `['foo','bar']`；`test_s1` 通过 ✓ **修到位** |
| S2 注入词表无等价约束 | `rewrite.py:140-141` + 规格 `:57` | docstring 与 FR-2 补充均显式声明"等价性责任在注入方，value 须为 key 等价词，否则引入新实体违反 D2" ✓ **修到位**（责任边界声明） |
| S3 FR-3 测试未锁定 | `test_rewrite.py:132-143` | 新增 `test_s3` 直接测 `_split_subqueries`，含中英文标点/连接词精确断言。实测 `foo and then bar`→`['foo','bar']` ✓ **修到位** |
| N2 `_split_subqueries` 调用两次 | `rewrite.py:162, 174` | `subqueries = _split_subqueries(text)` 只算一次，两处复用 ✓ **修到位** |
| N3 英文连接词前导空格 | `rewrite.py:75, 82, 143` | 切片入列前 `.strip()`，空片段跳过；测试期望修正为 `['foo','bar']`。实测 ✓ **修到位** |
| 未决 4 等值映射产生非变体 | `rewrite.py:120` | `if replaced != text` 过滤；实测 `{"k":("k",)}` → `['k']` ✓ **修到位** |

全部六项均真修到位，无遗漏。

---

## 五、Findings

### Should（2）

#### S-R1. 多子句查询中后半子句被"子句同义变体"挤掉（fix 引入的策略失衡）

- **位置**：`src/harness/rag/rewrite.py:174-176`（每个子句后插入 `limit=1` 的同义变体）+ `:187-190`（截断）。
- **实测**：N=6、4 子句 `向量召回，关键词精排，文档检索，词面重排` →
  `[原串, 向量检索…, 向量召回, 向量检索, 关键词精排, 关键词重排]`。
  子句 3「文档检索」、子句 4「词面重排」**完全丢失**。对比上一轮报告记录的修复前组装顺序 `[原串, 各子句, 各同义]`（`review.md:68`），彼时 N=6 下 4 个子句可全部入窗。
- **后果**：交错结构让每个子句占用 2 个名额（子句 + 子句同义），当子句数增长而 N 未同步放大时，靠后的子句被饿死。多子句查询常把关键限定条件放在后段，丢失这些子句即丢失独立检索视角，收窄召回覆盖。需注意：这与 B1 修复本身无关（B1 只要求整串同义入窗），是"每个子句都附带一个同义变体"这一附加策略的副作用，可通过调整组装顺序避免。
- **改法**：让基础变体（原串 + 全部子句）优先入列，同义替换作为第二轮填充。例如 `candidates = [text] + subqueries + <同义变体>`；或对子句同义设条件——仅当"剩余预算 > 尚未入列的子句数"时才插入。这样 N=6 / 4 子句可得 `[原串, 4 子句, 整串同义]`，既不丢子句又保留 B1 要求的同义代表。

#### S-R2. 固定 `limit=1` 在 N 较大时过度削弱整串词面增益（fix 引入）

- **位置**：`src/harness/rag/rewrite.py:171`（`_synonym_variants(text, self.synonyms, limit=1)`）。
- **实测**：N=10、`向量召回，关键词精排` → 仅 6 个变体，整串同义只有「向量检索，关键词精排」（召回→检索）。该整串实际命中 4 个 key（召回/精排/向量/关键词），因 `limit=1` 丢失了「嵌入」「词面」「重排」三个整串词面变体。
- **后果**：`limit=1` 的初衷是 B1 的"策略保底"（防止整串多命中挤掉子句）。但 N 充足时（10 > 基础变体数 6）根本不存在挤掉风险，固定 `limit=1` 仍砍掉 3 个能扩大关键词通道覆盖的词面变体，违背设计 §1"红线 D2 > 召回增益 > 变体数量"的价值排序——牺牲召回增益去保一个本就不短缺的变体数量。
- **改法**：limit 随预算自适应，例如整串同义 `limit = max(1, self.n_variants - 1 - len(subqueries))`；N=3 时仍为 1（保底不变），N 大时放开。子句同义可按剩余名额同理分配。

### Nit（1）

#### N1（上轮遗留，未修）. 文档参数表仍写不存在的 `strategy=rule_expand`

- **位置**：`docs/02-模块详设/Agent/R6-RAG检索增强.md:170`。
- **现状**：参数表写 `strategy / n_variants / synonyms`，默认 `rule_expand / 3 / 内置精简词表可注入`；但 `MultiQueryRewriter.name == "multi_query"`（`rewrite.py:128`）且构造器无 `strategy` 入参（`:130-134`）。上一轮执行者判断"保留为逻辑策略描述，不必改代码"，但未在文档补注，复审确认表述仍不一致。
- **后果**：读者会误以为存在可切换的策略枚举入口。
- **改法**：从参数表移除 `strategy` 列，或加注"`rule_expand` 为逻辑策略描述，非构造参数（构造参数仅 `n_variants` / `synonyms`）"。

---

## 六、七维扫描小结

| 维度 | 结论 |
|---|---|
| 1 功能正确性（FR-1~FR-8） | B1 已修；FR-1/2/5/6/7/8 保持通过，FR-3/4 默认路径恢复 ✓ |
| 2 红线与安全（D2 / FR-7） | 切分均为连续子串；同义值皆为等价词；等值映射已过滤；不引入新实体 ✓ |
| 3 确定性（D4 / FR-8） | 构造期 dict 拷贝保 D4；实测两次 rewrite 输出一致（`True`） ✓ |
| 4 召回有效性（核心意图） | 默认 N=3 词面增益确已入窗；但 S-R1 / S-R2 在多子句与大 N 下次优 ⚠ |
| 5 兼容性 / 接线（NFR-3） | 协议未变、接线闭环；`mypy --strict` 0 error、pyright 0 error、门禁 14/14 ✓ |
| 6 测试充分性（NFR-2 / S3） | `tests/r6` 40 passed；B1/S1/S3 回归齐备；NFR-1 的 inspect 字符串法仍脆弱（见未决 3） ⚠ |
| 7 文档一致性 | S2 责任声明已补齐；N1 遗留 ⚠ |

---

## 七、未决与建议

1. **Should 优先级**：S-R1 > S-R2。S-R1 影响默认或接近默认预算下的多子句查询，建议优先按"基础变体优先入列"重构组装顺序；S-R2 影响主动调大 N 的用户，可用自适应 limit 解决。两者均不阻断交付。
2. **覆盖率口径不一致**：门禁 G1 报 94%（门禁口径 ≥85% 通过），但设计规格 §12 要求 ≥95%。建议要么补测试到 95%，要么统一规格表述。此为规格与门禁脚本口径差异，非本次 fix 引入，故未计入 findings。
3. **NFR-1 测试方法脆弱**（沿用上轮未决）：`test_rewrite.py:146-154` 用 `inspect.getsource` + 字符串匹配禁用 `open(` / `requests` 等，无法识别动态导入，也可能误伤注释或 docstring 中的字样。建议改用 AST 扫描 `Import` / `ImportFrom` / `Call` 节点。
4. **改动范围提示**：`git status` 显示工作区另有 `pipeline.py`（仅 import / `__all__` 顺序调整）、`test_components.py`（行为锁更新，已核对）及多处文档/目录变更；本报告仅覆盖用户指定审查范围，未对上述其余改动做功能审查。
5. **基线说明**：`git show HEAD` 中 `rewrite.py` 仍是"复制 N 份"占位实现，规则改写版从未提交；本轮 S-R1 的"修复前顺序"依据上一轮报告记录（`review.md:68`），未做 git 历史实跑对比。

---

> 复审结论：**Blocker = 0，通过。** 建议在合入前处理 S-R1（并顺带 S-R2），以兑现多子句与大 N 场景下的完整召回增益。
