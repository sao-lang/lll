# R6 改写器加厚改动 · 独立代码审查报告

**审查日期**：2026-11-20
**审查对象**：`MultiQueryRewriter` 由"复制 N 份占位"改为"无模型规则改写（同义词等价替换 + 标点/连接词拆子句）"
**意图源**：`docs/02-模块详设/Agent/R6-改写器加厚-设计规格.md`（§1 / §5 FR-1~FR-8、NFR-1~3 / §13）
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
