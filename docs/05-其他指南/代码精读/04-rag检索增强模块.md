# 代码精读 04 · rag 检索增强模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/rag/` 全部 10 个文件：types.py（145 行）+ splitter.py（83 行）+ embedder.py（83 行）+ store.py（98 行）+ retriever.py（137 行）+ rerank.py（47 行）+ post.py（161 行）+ rewrite.py（193 行）+ pipeline.py（338 行）+ __init__.py（42 行）。
> 总入口见 [00](./00-总入口与全局链路.md)。

---

## 0. 读前GPS

RAG 模块是「文档进、上下文出」的完整检索链。它把链路切成**五段主协议 + 三个增强点 + 横切能力**，每一段都可独立替换、独立评测。

五段主协议（**必填**）：
- `Splitter`：文档切块。
- `Embedder`：文本向量化。
- `VectorStore`：向量存储与检索。
- `Retriever`：召回（向量 / 混合）。
- `PostProcessor`：过滤 + 预算拼接 + 引用 + 乱序修复。

三个增强点（**可缺席**）：
- `QueryRewriter`：查询改写（无模型规则）。
- `Reranker`：重排。
- `InjectionDetector`：注入检测（横切，来自 R5 skills 模块）。

核心类一览：
- `RAGPipeline`（pipeline.py）：总控，履约 `RAGPort`，编排入库与检索全链路，附带缓存。
- `_Strict` 及各 DTO（types.py）：pydantic 严格模型，描述数据结构。
- `RecursiveSplitter` / `HashEmbedder` / `InMemoryVectorStore` / `HybridRetriever` / `LexicalReranker` / `DefaultPostProcessor` / `MultiQueryRewriter`：各段默认实现。

**三条铁律先记住**（后面会在具体行反复印证）：
1. **同源 embedder（B1）**：查询向量必须由流水线注入的那个 embedder 算，与入库同源。召回器绝不自行向量化，否则入库/查询落在不同语义空间，检索直接失效。
2. **关键词通道共用 filters（B2）**：混合召回的关键词通道与向量通道走同一个 `matches_filters`，不绕过权限/时效过滤。
3. **退化必须可见（S2）**：无关键词素材时 `HybridRetriever` 显式抛错，不静默退化为纯向量。

---

## 1. types.py 逐行讲解（145 行）

纯数据结构模块：所有 DTO 都是 pydantic 严格模型，不含任何检索逻辑。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R6 检索增强核心 DTO：文档块、命中、查询、结果与预算。`
  一句话定位：本模块只描述数据结构。注意归属 R6（检索增强域）。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `本模块只描述数据结构（pydantic 严格模型 + 有限取值枚举），不含检索逻辑。`
  强调边界：这里没有算法，只有数据。为什么这样切：数据结构稳定、算法多变，分开后算法可独立替换而不动 DTO。

- **第 4 行** `向量用具名别名 ``Vector`` 承载，避免散落的裸 ``tuple[float, ...]``。`
  说明具名别名的动机：集中定义一处，签名可读，改维度时只改一处。

- **第 5 行** `"""` docstring 结束。

### 第 6-10 行：imports

- **第 6 行** `from __future__ import annotations`
  延迟求值注解，允许在类型注解里引用尚未定义的类型（这里让 `Vector`、`TokenBudget` 等可在定义前被注解引用），降低 import 顺序耦合。

- **第 8 行** `from enum import Enum`
  为 `QueryPurpose` 有限取值枚举提供基类。

- **第 10 行** `from pydantic import BaseModel, ConfigDict, Field`
  - `BaseModel`：pydantic 模型基类。
  - `ConfigDict`：配置严格模式（第 45 行）。
  - `Field`：声明字段及 description，带类型约束。

### 第 12-22 行：__all__ 导出清单

- **第 12-22 行** `__all__ = [Vector, QueryPurpose, Document, Chunk, Hit, Query, TokenBudget, RAGResult, RRF_K]`
  导出 9 个符号。 `__init__.py` 据此重导出。
  注意：这里导出的是「包内其他模块 + 外部装配」都能用的公共 API。`RRF_K` 也导出，因为 retrieval 与文档都要引用这个常量，集中一处定义。

### 第 24-28 行：Vector 别名与 RRF_K 常量

- **第 24 行** `# 具名别名：向量是定长浮点元组，集中定义一处（规则 1）。`
  注释点出纪律「规则 1：集中定义」。

- **第 25 行** `type Vector = tuple[float, ...]`
  用 PEP 695 `type` 语句定义类型别名（不是普通赋值）。Python 3.12+ 语法，`Vector` 是真正的类型别名而非变量。
  - 为什么用 `tuple` 而非 `list`：向量是定长、不可变的数学对象，tuple 语义正确且可哈希。
  - 不这么写：每个函数签名都写 `tuple[float, ...]`，冗长且容易写错（有的写 `list[float]` 导致类型不一致）。

- **第 27 行** `# RRF 融合常数（设计 §5：RRF(d)=Σ weight/(k+rank)）。`
  注释给出公式出处，方便读者对照设计文档。

- **第 28 行** `RRF_K: int = 60`
  RRF（Reciprocal Rank Fusion）融合常数，取值 60（业内常用值）。
  - 作用：在 `retriever._rrf` 中作为分母 `k + rank`，平滑不同名次的影响（k 越大，名次差异影响越小）。
  - 不这么写（不设为常量）：公式里散落裸 `60`，日后要调参就得满代码找。

### 第 31-39 行：QueryPurpose 枚举

- **第 31 行** `class QueryPurpose(str, Enum):`
  继承 `str, Enum` 双基类：既是枚举（有限取值、单例比较），又能当字符串用。
  - 为什么继承 `str`：缓存键里用 `q.purpose.value`（第 304 行），也能直接进 JSON/日志。若只继承 `Enum`，要显式转字符串。

- **第 32-36 行** docstring：说明用途——区分问答与记忆召回；列出成员 `QA`、`MEMORY`。
  为什么需要区分用途：R7 memory 模块复用本模块时，记忆召回与问答可能走不同策略（如不同预算、不同后处理），用途字段是扩展点。

- **第 38 行** `QA = "qa"`
  问答检索。

- **第 39 行** `MEMORY = "memory"`
  记忆召回。

### 第 42-45 行：_Strict 严格基类

- **第 42 行** `class _Strict(BaseModel):`
  所有 DTO 的公共基类。下划线前缀表示「包内私有基类」，不作公共 API。

- **第 43 行** `"""严格基类：拒绝多余字段，避免 DTO 被静默塞入拼错字段。"""`
  一句话点明动机。

- **第 45 行** `model_config = ConfigDict(extra="forbid")`
  核心配置：禁止额外字段。
  - 做什么：pydantic 解析时若出现未声明字段，直接抛 `ValidationError`。
  - 为什么：防止上游拼错字段名（如 `token_budge`）被静默忽略——静默忽略会导致参数「以为生效其实没生效」，极难排查。
  - 不这么写：pydantic 默认 `extra="ignore"`，拼错字段不报错，埋下隐患。

### 第 48-59 行：Document 文档 DTO

- **第 48 行** `class Document(_Strict):`
  待入库的原始文档。继承严格基类。

- **第 49-55 行** docstring：说明三个字段用途。`meta` 的键由上游决定故为动态映射（`dict[str, object]`）。

- **第 57 行** `doc_id: str = Field(description="Unique document identifier")`
  文档唯一标识，引用溯源的根基。必填（`Field` 无 default）。
  - 为什么必填：没有唯一 id 就无法生成 `chunk_id = doc_id:序号`，引用溯源断裂。

- **第 58 行** `text: str = Field(description="Raw text")`
  正文原文，必填。

- **第 59 行** `meta: dict[str, object] = Field(default_factory=dict, description="Metadata")`
  元数据（标题/页码/权限标签等）。
  - 为什么用 `default_factory=dict` 而非 `{}`：pydantic 要求可变默认值用工厂函数，否则所有实例共享同一个 dict（经典 Python 可变默认参数陷阱）。
  - 为什么值是 `object`：键由上游 schema 决定、类型不定，用 `object` 表示动态边界。

### 第 62-79 行：Chunk 文档块 DTO

- **第 62 行** `class Chunk(_Strict):`
  切分后的文档块，可携带向量。

- **第 63-72 行** docstring：逐字段说明。重点：`vector` 是入库前由 Embedder 填充的中间态。

- **第 74 行** `chunk_id: str = Field(description="Unique chunk identifier")`
  块唯一标识，形如 `doc_id:序号`。必填。

- **第 75 行** `doc_id: str = Field(description="Source document identifier")`
  来源文档标识。必填，便于从块回溯到文档。

- **第 76 行** `text: str = Field(description="Chunk text")`
  块正文。必填。

- **第 77 行** `tokens: int = Field(default=0, description="Estimated token count")`
  token 估算数（本实现按字数 `len(piece)` 计，见 splitter.py 第 65 行）。默认 0。
  - 作用：后处理按预算装入时用它判断能否装下（post.py 第 129-130 行）。

- **第 78 行** `meta: dict[str, object] = Field(default_factory=dict, description="Metadata")`
  元数据副本。同样用默认工厂。

- **第 79 行** `vector: Vector | None = Field(default=None, description="Vectorized embedding")`
  向量化产物，默认 `None`（纯文本块尚未向量化时的状态）。
  - 为什么允许 `None`：切分阶段产出块时还没有向量；入库阶段 embedder 才填。用可选类型显式表达这个「先有块后有向」的两阶段流程。
  - 注意 `Vector | None`：PEP 604 联合类型，延迟注解下可用。

### 第 82-95 行：Hit 命中 DTO

- **第 82 行** `class Hit(_Strict):`
  检索命中：块 + 相关度 + 来源。

- **第 83-90 行** docstring：逐字段说明。

- **第 92 行** `chunk: Chunk = Field(description="Matched chunk")`
  命中的块（整个 Chunk 对象）。必填。

- **第 93 行** `score: float = Field(default=0.0, description="Relevance score")`
  相关度分数，越大越相关。默认 0.0。
  - 注意：不同通道分数量纲不同（向量点积 vs 关键词覆盖率 vs RRF 融合分），但语义统一为「越大越好」，方便统一排序。

- **第 94 行** `source: str = Field(default="", description="Source identifier")`
  来源标识（召回通道名，如 `vector_retriever` / `hybrid_retriever`）。默认空串。
  - 作用：可观测性——_trace 里能看到命中来自哪个通道。

- **第 95 行** `ts: int | None = Field(default=None, description="Hit timestamp")`
  命中时间戳，可选。预留字段，当前链路未填。

### 第 98-113 行：Query 查询 DTO

- **第 98 行** `class Query(_Strict):`
  检索查询。

- **第 99-107 行** docstring：逐字段说明。

- **第 109 行** `text: str = Field(description="Query text")`
  查询文本。必填。

- **第 110 行** `filters: dict[str, object] | None = Field(default=None, description="Metadata filter")`
  元数据过滤条件，默认 `None`（不过滤）。
  - 为什么是 `dict | None` 而非 `dict`：`None` 明确表示「无过滤」；空 `{}` 也可表示，但 `None` 语义更清晰且与各处的 `if not filters` 判定兼容。

- **第 111 行** `top_k: int = Field(default=5, description="Top-k per channel")`
  每通道召回数，默认 5。
  - 注意「per channel」：混合召回里向量通道和关键词通道各取 top_k，再 RRF 融合（retriever.py 第 79、116 行）。

- **第 112 行** `token_budget: int | None = Field(default=None, description="Token budget")`
  本查询的 token 预算，默认 `None`（不限）。
  - 与 `TokenBudget.limit` 呼应：`None` 不限、非负整数硬上限、0 无预算。

- **第 113 行** `purpose: QueryPurpose = Field(default=QueryPurpose.QA, description="Query purpose")`
  查询用途，默认 `QA`。枚举类型保证有限取值。

### 第 116-127 行：TokenBudget 预算包装

- **第 116 行** `class TokenBudget(_Strict):`
  token 预算包装。

- **第 117 行** `"""token 预算包装（具名类型，避免裸 int 在签名里语义不明）。`
  说明动机：`assemble(hits, budget: TokenBudget)` 比 `assemble(hits, 1000)` 语义清晰。
  - 这也对应 pipeline.query 第 233 行把 `q.token_budget`（裸 int）重新包成 `TokenBudget(limit=...)`。

- **第 118-121 行** docstring 字段说明：重点解释 `limit` 三态。

- **第 123 行** `limit: int | None = Field(default=None, description="Budget cap; None means unlimited, 0 means no budget")`
  预算上限，默认 `None`。
  - 三态语义（务必分清）：
    - `None` = 不限。
    - 正整数 = 硬上限。
    - `0` = 无预算可分配（**不是不限**！后处理会直接返回空结果，见 post.py 第 117 行）。
  - 这是易错点：很多人把 0 当「不限」，但这里 0 明确表示「一点预算都没有」。

- **第 125 行** `def __int__(self) -> int:`
  实现 `__int__`，让 `int(TokenBudget(...))` 可用。

- **第 126 行** `"""返回预算整数值；None 按 -1 返回（调用方据此判不限）。"""`
  说明 `None → -1` 的约定。

- **第 127 行** `return self.limit if self.limit is not None else -1`
  - 做什么：有值返回值，`None` 返回 `-1`。
  - 为什么用 `-1` 哨兵：`-1` 在预算语境里是非法值（预算不可能为负），可安全用作「不限」标记。
  - 不这么写：直接 `return self.limit` 会在 `None` 时抛 `TypeError`；调用方就得自己判 None，分散逻辑。
  - 注：当前主链路 assemble 直接用 `budget.limit`（post.py 第 116 行），`__int__` 是给外部调用方的便利接口。

### 第 130-145 行：RAGResult 结果 DTO

- **第 130 行** `class RAGResult(_Strict):`
  检索最终结果（注入 LLM 上下文的产物）。

- **第 131-139 行** docstring：逐字段说明。重点：`empty=True` 时 `context` 是固定提示，模型应拒答。

- **第 141 行** `context: str = Field(default="", description="Assembled context text")`
  拼接后的上下文文本（含引用标注）。默认空串。空结果时为 `EMPTY_CONTEXT` 固定提示。

- **第 142 行** `citations: list[str] = Field(default_factory=list, description="List of citation ids")`
  引用 id 列表（形如 `doc:chunk`）。默认空列表，用工厂函数。

- **第 143 行** `hits: list[Hit] = Field(default_factory=list, description="Hits included in assembly")`
  实际参与拼接的命中列表。默认空列表。

- **第 144 行** `empty: bool = Field(default=False, description="Whether the result is empty")`
  是否空结果。默认 `False`。
  - 作用：上层据此决定是否让模型拒答（配合 `EMPTY_CONTEXT`）。

- **第 145 行** `tokens_used: int = Field(default=0, description="Tokens consumed by assembly")`
  本次拼接消耗的 token 数。默认 0。
  - 作用：可观测性——知道这次检索往上下文塞了多少 token。

---

## 2. splitter.py 逐行讲解（83 行）

切分段：递归切分，按自然边界优先，超长按固定长度 + overlap。

### 第 1-4 行：模块 docstring

- **第 1 行** `"""R6 切分段：Splitter 协议 + 递归切分默认实现。`
  一句话定位。

- **第 2 行** 空行。

- **第 3 行** `递归切分按段落/句子边界优先，超长再按字符切；overlap 保证跨块上下文连续。`
  点明核心算法：边界优先 + 超长滑窗 + overlap 连续。

- **第 4 行** `"""` docstring 结束。

### 第 5-11 行：imports 与导出

- **第 5 行** `from __future__ import annotations` 延迟注解。

- **第 7 行** `from typing import Protocol, runtime_checkable`
  - `Protocol`：定义可插拔切分协议。
  - `runtime_checkable`：让 `isinstance(x, Splitter)` 可用于装配期校验。

- **第 9 行** `from harness.rag.types import Chunk, Document`
  导入本模块要产出的 `Chunk` 与输入的 `Document`。依赖方向：splitter 依赖 types。

- **第 11 行** `__all__ = ["DEFAULT_CHUNK_SIZE", "DEFAULT_OVERLAP", "RecursiveSplitter", "Splitter"]`
  导出常量、协议、默认实现。

### 第 13-17 行：常量定义

- **第 13 行** `DEFAULT_CHUNK_SIZE: int = 512`
  默认单块最大字数。512 是常见权衡：太小上下文碎片化，太大超出模型窗口/降低检索精度。

- **第 14 行** `DEFAULT_OVERLAP: int = 64`
  默认相邻块重叠字数（512 的 1/8）。
  - 作用：防止一个完整语义单元恰好被切在边界上而丢失（跨块连续）。

- **第 16 行** `# 切分边界优先级：段落 > 句子 > 无边界（纯长度切）。`
  注释说明边界顺序的设计意图：优先在自然语义边界切。

- **第 17 行** `_BOUNDARIES: tuple[str, ...] = ("\n\n", "。", ". ")`
  边界符元组，按优先级排列：双换行（段落）> 中文句号 > 英文句点+空格（句子）。
  - 为什么用 tuple：不可变，防止运行期被改。
  - 不这么写：散落在 `_split_text` 里的裸字符串列表，难维护。

### 第 20-28 行：Splitter 协议

- **第 20 行** `@runtime_checkable`
  允许运行时 `isinstance()` 检查。代价：只检查成员存在，不检查方法签名。

- **第 21 行** `class Splitter(Protocol):`
  切分协议。五段之一，可插拔。

- **第 22 行** `"""切分协议：把文档切成块，name 供装配期留名校验。"""`
  说明 `name` 的用途：装配期留名校验 + trace 归因。

- **第 24 行** `name: str`
  协议属性声明。注意 Protocol 里只声明不赋值。

- **第 26 行** `def split(self, doc: Document) -> list[Chunk]:`
  协议方法签名：输入文档，输出块列表。

- **第 27-28 行** docstring + `...`
  `...` 是协议方法体，表示接口声明而非实现（Python 协议语法，不是代码省略）。

### 第 31-53 行：RecursiveSplitter 构造

- **第 31 行** `class RecursiveSplitter:`
  递归切分默认实现。
  - 注意它没有显式继承 `Splitter`——Protocol 是结构化类型（鸭子类型），有 `name` 和 `split` 就算履约。

- **第 32 行** `"""递归切分默认实现：优先按自然边界，超长按固定长度 + overlap。"""`
  一句话概括。

- **第 34 行** `name: str = "recursive_splitter"`
  具名标识。

- **第 36-40 行** 构造函数签名：
  `chunk_size` 与 `overlap` 都是位置或关键字参数，有默认值。

- **第 41-49 行** 构造函数 docstring：说明参数与异常。

- **第 50 行** `if chunk_size <= 0:`
  校验块大小必须为正。

- **第 51 行** `raise ValueError("chunk_size must be a positive integer")`
  - 做什么：非法值立即报错。
  - 为什么：0 或负数会导致后面 `range(0, len, step)` 中 step 非正抛 `ValueError`，或切出空块。尽早失败（fail fast）在构造期暴露，比在运行期崩溃好排查。

- **第 52 行** `self.chunk_size = chunk_size`
  保存块大小。

- **第 53 行** `self.overlap = overlap if 0 <= overlap < chunk_size else 0`
  **关键防御行**：overlap 只在 `[0, chunk_size)` 区间内才采用，否则退化为 0。
  - 为什么不报错而是退化：overlap ≥ chunk_size 会让 `step = chunk_size - overlap ≤ 0`（第 82 行），导致 `range` 抛错或死循环般重复切片。退化为 0 保证 `step = chunk_size > 0`。
  - 为什么区间上界是严格 `< chunk_size`：等于时 step=0 同样出问题。

### 第 55-69 行：split 方法

- **第 55 行** `def split(self, doc: Document) -> list[Chunk]:`
  协议方法实现。

- **第 56 行** `"""把文档正文切成带 id/token 估算的块。"""` 说明输出。

- **第 57 行** `pieces = self._split_text(doc.text)`
  调用递归切分得到片段列表。把「怎么切」委托给 `_split_text`，本方法只负责包装成 Chunk。

- **第 58 行** `out: list[Chunk] = []`
  结果容器。

- **第 59 行** `for i, piece in enumerate(pieces):`
  枚举片段，`i` 用作块序号。用 `enumerate` 而非手动计数，避免 off-by-one。

- **第 60-68 行** 构造 Chunk：
  - **第 62 行** `chunk_id=f"{doc.doc_id}:{i}"`
    **引用溯源的基础**：块 id = 文档 id + 冒号 + 序号。为什么这样设计：可从 `chunk_id` 反查是哪个文档的第几块，citations 才有意义。
  - **第 63 行** `doc_id=doc.doc_id`：保留来源文档 id。
  - **第 64 行** `text=piece`：块正文。
  - **第 65 行** `tokens=len(piece)`：**按字数近似 token 数**。为什么不精确算 token：本实现无 tokenizer 依赖，字数在低精度预算下够用；精确 token 化要引入 tiktoken 等依赖。
  - **第 66 行** `meta=dict(doc.meta)`：**复制元数据**，不是引用。防止后续改动源 doc.meta 影响块，也便于块独立传递。
- **第 69 行** `return out`
  返回全部块。

### 第 71-83 行：_split_text 递归切分核心

- **第 71 行** `def _split_text(self, text: str) -> list[str]:`
  递归函数：输入文本，返回不超过 `chunk_size` 的片段列表。

- **第 72 行** `"""递归按边界切分，返回不超过 chunk_size 的片段列表。"""` 说明契约。

- **第 73 行** `if len(text) <= self.chunk_size:`
  递归终止条件：文本已够短，无需再切。

- **第 74 行** `return [text] if text else []`
  够短时返回单片；空串返回空列表（不入列空块）。
  - 为什么不返回空块：空块向量化后是零向量，召回无意义，还浪费预算。

- **第 75 行** `for boundary in _BOUNDARIES:`
  按优先级尝试每种边界。先试 `\n\n`（段落），再试 `。`，再试 `. `。

- **第 76 行** `if boundary in text:`
  当前边界在文本中存在才用它切。
  - 为什么按优先级「第一个命中就用」：段落边界语义最强，能切就优先用；只有段落切不开才退而求其次用句子边界。

- **第 77 行** `parts = text.split(boundary)`
  按边界切开。注意 `split` 会丢弃边界符本身（段落/句子分隔符不需要进块正文）。

- **第 78 行** `out: list[str] = []`
  本次切分的结果容器。

- **第 79 行** `for part in parts:`
  遍历每片。

- **第 80 行** `out.extend(self._split_text(part) if len(part) > self.chunk_size else ([part] if part else []))`
  **递归核心**：
  - 若 `part` 仍超长 → 递归再切（可能用更低优先级边界或滑窗）。
  - 否则 → 若非空则原样收（`[part]`），空则丢弃（`[]`）。
  - 用 `extend` 而非 `append`：递归可能返回多片，要展开加入。
  - 为什么不直接收超长 part：那会产生超过 chunk_size 的块，破坏「每块 ≤ chunk_size」的契约。

- **第 81 行** `return out`
  找到边界并完成切分后返回，不再尝试更低优先级边界（首个命中即定值）。

- **第 82 行** `step = self.chunk_size - self.overlap`
  无自然边界时的滑窗步长。
  - 为什么是 `chunk_size - overlap`：相邻窗口重叠 `overlap` 个字，保证跨块上下文连续（一个词不会被切在两块中间完全丢失）。
  - 构造期已保证 `overlap < chunk_size`，所以 `step > 0`。

- **第 83 行** `return [text[i : i + self.chunk_size] for i in range(0, len(text), step)]`
  按 step 滑窗切片：从 0 开始，每步前进 step，每片长 chunk_size。
  - 列表推导式一次性生成所有片。
  - 最后一片可能短于 chunk_size，正常。
  - 不这么写（用 step=chunk_size 即无 overlap）：切在边界的语义单元会断裂，检索时该块可能缺失关键上下文。

---

## 3. embedder.py 逐行讲解（83 行）

向量化段：可注入 client 的默认实现，内置确定性哈希 client 供离线评测。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R6 向量化段：Embedder 协议 + 可注入 client 的默认实现。`
  一句话定位。

- **第 2-5 行** 说明设计纪律：需要外部系统的协议必须抽象可注入 client；默认提供确定性哈希 client，生产可注入 OpenAI-compat 后端（不联网、可测）。
  这段是**为什么有两层协议**的核心解释：`EmbeddingClient`（真实后端）与 `Embedder`（五段协议）分离，让上层不绑死具体实现。

- **第 6 行** `"""` docstring 结束。

### 第 7-13 行：imports 与导出

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `from typing import Protocol, runtime_checkable` 协议与运行时检查装饰器。

- **第 11 行** `from harness.rag.types import Vector` 导入向量别名。

- **第 13 行** `__all__ = ["DEFAULT_DIM", "Embedder", "EmbeddingClient", "HashEmbedder"]`
  导出维度常量、两层协议、默认实现。注意 `_HashClient` 是私有的（下划线前缀），不在导出清单。

### 第 15 行：DEFAULT_DIM 常量

- **第 15 行** `DEFAULT_DIM: int = 16`
  默认向量维度。16 维足够离线评测区分词哈希，又不至于稀疏。
  - 生产用真实模型时维度由模型决定（通常 768/1536），这里只是默认 client 的参数。

### 第 18-26 行：EmbeddingClient 协议（后端层）

- **第 18 行** `@runtime_checkable` 运行时可检查。

- **第 19 行** `class EmbeddingClient(Protocol):`
  向量化后端协议：把真实向量化服务抽象成可注入接口。

- **第 20 行** `"""向量化后端协议：把文本批转成向量，外部系统在此抽象可注入。"""`
  点明用途：外部系统在此抽象。

- **第 22 行** `name: str` 具名属性。

- **第 24 行** `async def embed(self, texts: list[str]) -> list[Vector]:`
  协议方法签名：`async` 因为真实后端可能走网络 IO。
  - 为什么用 `async` 而非同步：OpenAI-compat 调用是网络请求，异步避免阻塞事件循环；内置哈希 client 虽是纯计算，也实现 `async` 以统一签名。

- **第 25-26 行** docstring + `...` 接口声明。

### 第 29-37 行：Embedder 协议（五段层）

- **第 29 行** `@runtime_checkable` 运行时可检查。

- **第 30 行** `class Embedder(Protocol):`
  向量化协议（R6 五段之一）。

- **第 31 行** `"""向量化协议（R6 五段之一）。"""` 定位说明。

- **第 33 行** `name: str` 具名属性。

- **第 35 行** `async def embed(self, texts: list[str]) -> list[Vector]:`
  与 `EmbeddingClient` 同签名。

- **第 36-37 行** docstring + `...`。
  - **为什么要两层协议**：`EmbeddingClient` 是「怎么算向量」（策略，可注入后端），`Embedder` 是「流水线拿到的向量化能力」（五段之一）。`HashEmbedder` 同时履约两者——它既是一个 Embedder，又内部委托一个 EmbeddingClient。这样 pipeline 只依赖 `Embedder`，而真实后端可经 `HashEmbedder(client=真实后端)` 注入。

### 第 40-62 行：HashEmbedder 默认实现

- **第 40 行** `class HashEmbedder:`
  默认向量化实现：委托可注入 client，缺省用内置哈希 client。

- **第 41-45 行** docstring：说明哈希 client 确定性便于离线评测，生产可替换，上层零改动。

- **第 47 行** `name: str = "hash_embedder"` 具名标识。

- **第 49-51 行** 构造函数签名：`client` 可选，`dim` 关键字参数。

- **第 52-57 行** 构造函数 docstring。

- **第 58 行** `self._client: EmbeddingClient = client if client is not None else _HashClient(dim)`
  **关键防御行**：传 `None` 时用默认 `_HashClient`，不存在「无 client 直接失败」。
  - 为什么不报错：默认实现要开箱可用（规则 9：每个协议有默认实现）。
  - `dim` 只作用于内置 client；若注入自定义 client，`dim` 被忽略（因为维度由 client 决定）——这是合理的，docstring 已说明 `dim` 是「内置 client 的维度」。

- **第 60 行** `async def embed(self, texts: list[str]) -> list[Vector]:` 协议方法实现。

- **第 61 行** `"""委托后端向量化。"""` 说明纯委托。

- **第 62 行** `return await self._client.embed(texts)`
  **纯委托，零逻辑**。做什么：把文本交给后端，原样返回结果。
  - 为什么不在这里加逻辑：保持策略与委托分离。要换向量化算法只需换 client，不动 HashEmbedder。
  - 注意 `await`：`embed` 是协程，必须等待。

### 第 65-83 行：_HashClient 内置哈希后端

- **第 65 行** `class _HashClient:`
  内置确定性向量化后端（私有，下划线前缀）。

- **第 66 行** `"""内置确定性向量化后端：词哈希到 dim 维单位向量。"""` 一句话算法。

- **第 68 行** `name: str = "hash_client"` 具名标识。

- **第 70 行** `def __init__(self, dim: int = DEFAULT_DIM) -> None:` 构造函数。

- **第 71 行** `"""构造后端，dim 必须为正。"""` 说明。

- **第 72 行** `self.dim = dim if dim > 0 else DEFAULT_DIM`
  防御：`dim <= 0` 回退默认值。
  - 为什么：`dim` 用作 `[0.0] * self.dim` 的长度（第 78 行）和取模基数（第 80 行），非正会导致空向量或除零（`% 0`）。回退默认值保证可用。

- **第 74 行** `async def embed(self, texts: list[str]) -> list[Vector]:` 实现后端协议。

- **第 75 行** `"""把每条文本散列成 dim 维单位向量。"""` 说明。

- **第 76 行** `out: list[Vector] = []` 结果容器。

- **第 77 行** `for text in texts:` 逐条文本处理。

- **第 78 行** `buckets = [0.0] * self.dim`
  初始化 dim 维全零向量（词袋计数器）。
  - 列表乘法创建 dim 个独立 float 引用（float 不可变，安全）。

- **第 79 行** `for word in text.lower().split():`
  文本小写化后按空白切词。
  - 为什么 `lower()`：让大小写不敏感（"RAG" 与 "rag" 视为同一词），提升词面一致性。注意这里用 `lower()` 而非 `casefold()`——哈希词袋是近似语义，不需要 skills 模块那种严格大小写折叠。

- **第 80 行** `buckets[hash(word) % self.dim] += 1.0`
  **核心散列逻辑**：用 Python 内置 `hash()` 把词映射到 `[0, dim)` 的桶，命中桶计数 +1。
  - 做什么：把词袋压成定长向量（每个词对对应维度贡献 1）。
  - 为什么用 `hash()`：零依赖、快、分布均匀。
  - **关键纪律（与 model/cache 的哈希不能混用）**：这里用的是**内置 `hash()`**，它受 `PYTHONHASHSEED` 影响——**同一进程内确定，跨进程会变**。而 model/cache 里的 `HashCacheEmbedder` 刻意用确定性 31 进制哈希（跨进程稳定）。两者是**独立实现、语义空间不同，绝不能混用**：如果用不同哈希方案分别入库和查询，同一词会落到不同维度，向量点积接近 0，检索直接失效。这是与 B1「同源 embedder」配套的纪律——不只是同一个 embedder 实例，连哈希方案都得一致。

- **第 81 行** `norm = sum(v * v for v in buckets) ** 0.5 or 1.0`
  计算 L2 范数，并用 `or 1.0` 防空向量除零。
  - 做什么：`sqrt(Σ v²)`。
  - 为什么 `or 1.0`：空文本（无词）时 buckets 全 0，norm=0；不处理会导致下一行除零。`or 1.0` 让空文本得到零向量（全 0/1=0），安全。
  - 注意：短路求值——norm=0 时取 1.0。

- **第 82 行** `out.append(tuple(v / norm for v in buckets))`
  归一化成单位向量（各维除以范数）并转 tuple。
  - 为什么归一化：向量点积作为相似度时，单位向量让点积等于余弦相似度，不受文本长度影响（长文本不会因词多而虚高）。
  - 转 tuple：`Vector` 类型要求 tuple，且不可变。

- **第 83 行** `return out`
  返回全部向量。

---

## 4. store.py 逐行讲解（98 行）

存储段：向量存于 Chunk.vector，点积排序，元数据过滤唯一判定。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R6 存储段：VectorStore 协议 + 内存默认实现。`
  一句话定位。

- **第 2-4 行** 说明：向量存放于 `Chunk.vector`（入库前由 Embedder 填充），search 用点积排序，filters 按 `Chunk.meta` 键值对精确匹配。

- **第 5 行** `"""` docstring 结束。

### 第 6-12 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol, runtime_checkable` 协议工具。

- **第 10 行** `from harness.rag.types import Chunk, Hit, Vector` 导入 DTO。

- **第 12 行** `__all__ = ["InMemoryVectorStore", "VectorStore", "matches_filters"]`
  导出协议、默认实现、共享过滤函数。注意 `matches_filters` 是**跨模块共享**的关键函数（retriever/post 都用它），所以导出。

### 第 14-15 行：_CHUNK_FIELDS 常量

- **第 14 行** `# 元数据过滤中可直查的块字段（具名常量，避免裸串散落）。`
  注释点出纪律。

- **第 15 行** `_CHUNK_FIELDS: frozenset[str] = frozenset({"chunk_id", "doc_id"})`
  可直接查 Chunk 属性的过滤键集合。
  - 为什么用 `frozenset`：不可变、成员测试 O(1)（`key in _CHUNK_FIELDS`）。
  - 作用：过滤时区分「块字段」与「meta 键」——`chunk_id`/`doc_id` 直查 Chunk 属性，其余查 meta。

### 第 18-33 行：matches_filters 共享过滤函数

- **第 18 行** `def matches_filters(chunk: Chunk, filters: dict[str, object] | None) -> bool:`
  **向量通道与关键词通道共用的唯一过滤判定**（B2 的关键落点）。

- **第 19-23 行** docstring：强调「唯一判定」、AND 语义、None 不过滤。

- **第 24 行** `if not filters:`
  `None` 或空 `{}` 都视为「不过滤」。

- **第 25 行** `return True`
  无过滤时直接通过。

- **第 26 行** `for key, value in filters.items():`
  逐个检查过滤条件。

- **第 27 行** `if key in _CHUNK_FIELDS:`
  判断是块字段还是 meta 键。

- **第 28 行** `actual = chunk.chunk_id if key == "chunk_id" else chunk.doc_id`
  取对应块字段值。因 `_CHUNK_FIELDS` 只有两个键，非 `chunk_id` 即 `doc_id`。

- **第 29-30 行** `if actual != value: return False`
  块字段不匹配 → 整个过滤失败（AND 语义，任一不满足即拒）。

- **第 31 行** `elif chunk.meta.get(key) != value:`
  非块字段 → 查 `chunk.meta`，取不到（None）与目标不等则拒。
  - 为什么用 `.get(key)` 而非 `[key]`：键不存在时 `.get` 返回 None 而非抛 `KeyError`。目标 value 不可能是 None（filters 来自上游语义），所以键缺失自然判为不匹配。

- **第 32 行** `return False`
  同上，拒绝。

- **第 33 行** `return True`
  所有条件都通过才返回 True。

  **B2 修复的意义**：在混合召回里，关键词通道（retriever.py 第 108 行）与向量通道（第 83 行）都调用这同一个函数，保证权限/时效过滤不会被关键词通道绕过。不这么做（关键词通道自己实现过滤或直接不过滤）会导致越权文档被关键词召回注入 LLM，是安全漏洞。

### 第 36-50 行：VectorStore 协议

- **第 36 行** `@runtime_checkable` 运行时可检查。

- **第 37 行** `class VectorStore(Protocol):`
  向量存储协议（R6 五段之一）。

- **第 38 行** `"""向量存储协议（R6 五段之一）。"""` 定位。

- **第 40 行** `name: str` 具名属性。

- **第 42 行** `async def upsert(self, chunks: list[Chunk]) -> None:`
  写入块（含向量）。`async` 因为真实存储可能是数据库 IO。

- **第 43-44 行** docstring + `...`。

- **第 46-48 行** `async def search(self, vec, top_k, filters=None) -> list[Hit]:`
  按向量相似度返回 top_k 命中。注意 `filters` 是可选参数，默认 None。

- **第 49-50 行** docstring + `...`。

### 第 53-73 行：InMemoryVectorStore 构造与写/读

- **第 53 行** `class InMemoryVectorStore:`
  内存向量库默认实现。注意它没有显式继承 `VectorStore`（Protocol 结构化类型）。

- **第 54 行** `"""内存向量库默认实现：进程内列表存储，点积相似度。"""` 一句话概括。

- **第 56 行** `name: str = "in_memory_store"` 具名标识。

- **第 58 行** `def __init__(self) -> None:` 构造函数。

- **第 59 行** `"""初始化空库。"""` 说明。

- **第 60 行** `self._chunks: list[Chunk] = []`
  进程内列表存储所有块。为什么用 list：内存实现，简单；顺序即插入顺序。

- **第 62 行** `async def upsert(self, chunks: list[Chunk]) -> None:` 写入实现。

- **第 63-68 行** docstring：特别声明「当前为追加语义，按 chunk_id 覆盖的增量去重未实现」以及「存的是对象引用而非深拷贝」。
  - 这两个声明重要：使用者须知入库后不要再改这些 Chunk（会污染库内数据）；重复入库同一文档会产生重复块（当前无去重）。

- **第 69 行** `self._chunks.extend(chunks)`
  **追加块引用**（不是深拷贝）。
  - 做什么：把传入的块对象引用加入列表。
  - 为什么不深拷贝：省内存、快；代价是调用方改动块会影响库内数据——docstring 已警示。
  - 当前局限：不按 chunk_id 去重，重复 ingest 同文档会产生重复块（`_dedup` 在检索侧缓解引用重复，但库内确实冗余）。

- **第 71 行** `def all_chunks(self) -> list[Chunk]:`
  返回库内全部块（供混合召回关键词通道）。这个方法让 store 可被 `HybridRetriever` 鸭子取用（retriever.py 第 89 行）。

- **第 72 行** `"""返回库内全部块（供混合召回的关键词通道做词面打分）。"""` 说明用途。

- **第 73 行** `return list(self._chunks)`
  **返回浅拷贝列表**（`list(...)`）而非内部列表本身。
  - 为什么：防止调用方 `append`/`remove` 直接改动库内存储（结构层面的隔离）。注意块对象本身仍是共享引用（浅拷贝）。

### 第 75-93 行：search 检索

- **第 75-77 行** `async def search(self, vec, top_k, filters=None) -> list[Hit]:` 协议实现。

- **第 78 行** `"""点积相似度排序，按 meta 过滤后取 top_k。"""` 说明。

- **第 79 行** `scored: list[Hit] = []` 结果容器。

- **第 80 行** `for chunk in self._chunks:` 遍历全部块。

- **第 81-82 行** `if chunk.vector is None: continue`
  跳过未向量化的块（纯文本块）。入库前 vector 必填，但防御性跳过。

- **第 83-84 行** `if not matches_filters(chunk, filters): continue`
  **调用共享过滤函数**（B2）。不过滤的块跳过。

- **第 85-91 行** 构造 Hit：
  - **第 87 行** `chunk=chunk`：命中块本身。
  - **第 88 行** `score=self._dot(vec, chunk.vector)`：点积分数。
  - **第 89 行** `source=self.name`：标记来源通道（`in_memory_store`），可观测。

- **第 92 行** `scored.sort(key=lambda h: h.score, reverse=True)`
  按分数降序排序（越大越相关）。

- **第 93 行** `return scored[:top_k]`
  取前 top_k。

### 第 95-98 行：_dot 点积

- **第 95 行** `@staticmethod` 静态方法（不依赖实例）。

- **第 96 行** `def _dot(a: Vector, b: Vector) -> float:`
  计算两个向量点积。

- **第 97 行** `"""计算两个向量的点积。"""` 说明。

- **第 98 行** `return sum(x * y for x, y in zip(a, b))`
  `zip` 逐项相乘求和。
  - 为什么用 `zip`：两个向量按位置配对。
  - 注意（当前局限）：`zip` 在长度不等时**静默截断到较短者**，无显式维度校验。若入库与查询向量维度不同（例如混用了不同哈希方案——见 embedder.py 第 80 行的纪律），点积会算错而不报错。这从侧面进一步说明「同源 embedder、同哈希方案」为何是铁律。

---

## 5. retriever.py 逐行讲解（137 行）

召回段：向量召回 / 混合召回（RRF 融合）。本文件是 B1、B2、S2 三条纪律的集中落点。

### 第 1-9 行：模块 docstring

- **第 1 行** `"""R6 召回段：Retriever 协议 + 向量召回 / 混合召回（RRF 融合）默认实现。`
  一句话定位。

- **第 2-5 行** 说明 B1 与 B2 修复：
  - **B1（第 3-5 行）**：查询向量由 pipeline 用与入库**同一 embedder** 算好后经 `retrieve(query, vec)` 注入，召回器绝不自行向量化，避免入库/查询落在不同语义空间。
  - **B2（第 5 行）**：关键词通道与向量通道共用 `matches_filters`。
  这段是本模块最重要的设计声明，务必逐字理解。

- **第 7-8 行** RRF 公式说明：`RRF(d) = Σ weight / (k + rank)`，k=60。
  给出公式，方便对照 `_rrf` 实现。

- **第 9 行** `"""` docstring 结束。

### 第 10-18 行：imports 与导出

- **第 10 行** `from __future__ import annotations` 延迟注解。

- **第 12 行** `from collections.abc import Callable`
  为 `_corpus` 中 `cast(Callable[...], getter)` 提供类型。

- **第 13 行** `from typing import Protocol, cast, runtime_checkable`
  - `cast`：鸭子取用 `all_chunks` 时显式还原类型（第 94 行）。
  - 其余协议工具。

- **第 15 行** `from harness.rag.store import VectorStore, matches_filters`
  导入 store 协议与**共享过滤函数**（B2 依赖）。

- **第 16 行** `from harness.rag.types import RRF_K, Chunk, Hit, Query, Vector`
  导入常量与各 DTO。注意 `RRF_K` 从 types 导入（集中定义）。

- **第 18 行** `__all__ = ["HybridRetriever", "Retriever", "VectorRetriever"]`
  导出协议与两个实现。

### 第 20-22 行：通道权重常量

- **第 20 行** `# 混合召回的通道权重（具名常量，避免裸数字散落）。`
  纪律注释。

- **第 21 行** `_VECTOR_WEIGHT: float = 1.0`
  向量通道权重，默认 1.0。

- **第 22 行** `_KEYWORD_WEIGHT: float = 1.0`
  关键词通道权重，默认 1.0。
  - 为什么要权重：RRF 可对不同通道给不同权重（如更信任向量通道就调大 `_VECTOR_WEIGHT`）。具名常量便于调参。

### 第 25-36 行：Retriever 协议

- **第 25 行** `@runtime_checkable` 运行时可检查。

- **第 26-30 行** `class Retriever(Protocol):` + docstring：强调「查询向量由 pipeline 用注入的 embedder 预算好传入，召回器不自行向量化」（B1）。

- **第 32 行** `name: str` 具名属性。

- **第 34 行** `async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:`
  **协议签名关键**：同时接收 `query` 与 `vec`。
  - 为什么单独传 `vec`：把「算向量」的职责从召回器剥离——召回器只消费向量，不生产向量（B1 的签名层落地）。
  - 为什么不把 vec 放进 Query：vec 是本次检索即时计算的中间产物，不属于查询的持久语义。

- **第 35-36 行** docstring + `...`。

### 第 39-50 行：VectorRetriever 纯向量召回

- **第 39 行** `class VectorRetriever:`
  最简单的召回器。

- **第 40 行** `"""纯向量召回：用 pipeline 传入的查询向量走 store.search。"""` 一句话。

- **第 42 行** `name: str = "vector_retriever"` 具名标识（会写进 Hit.source）。

- **第 44 行** `def __init__(self, store: VectorStore) -> None:` 构造，可注入 store。

- **第 45 行** `"""构造召回器，可注入 store。"""` 说明。

- **第 46 行** `self._store = store` 保存 store。

- **第 48 行** `async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:` 协议实现。

- **第 49 行** `"""用查询向量召回 top_k，filters 在 store 层生效。"""` 说明。

- **第 50 行** `return await self._store.search(vec, query.top_k, query.filters)`
  **纯粹委托 store.search**：传入 vec、top_k、filters。
  - 自己不算向量（B1）、自己不做过滤（过滤下沉到 store 的 `matches_filters`）。
  - 不这么写（召回器内自己调 embedder）：又回到「召回器自行向量化」的 B1 老问题。

### 第 53-99 行：HybridRetriever 混合召回

- **第 53 行** `class HybridRetriever:`
  混合召回：向量通道 + 词面关键词通道，RRF 融合。

- **第 54-60 行** docstring：
  - 第 56 行：两路都施加同一 filters（B2）。
  - 第 57-59 行：关键词素材来源优先级（显式 all_chunks → store.all_chunks 鸭子取用 → 都没有则显式抛错，S2）。

- **第 62 行** `name: str = "hybrid_retriever"` 具名标识。

- **第 64 行** `def __init__(self, store, *, all_chunks=None) -> None:`
  构造：`store` 必填位置参数，`all_chunks` 关键字参数可选。

- **第 65-73 行** 构造函数 docstring：说明参数与 `ValueError`（无关键词素材来源）。

- **第 74 行** `self._store = store` 保存 store。

- **第 75 行** `self._all_chunks = all_chunks` 保存显式素材（可能为 None，表示「运行时再解析来源」）。

- **第 77 行** `async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:` 协议实现。

- **第 78 行** `"""两路召回后 RRF 融合去重，两路都施加 filters。"""` 说明。

- **第 79 行** `vector_hits = await self._store.search(vec, query.top_k, query.filters)`
  **向量通道**：用 pipeline 传入的 vec，带 filters。

- **第 80 行** `corpus = self._corpus()`
  解析关键词通道素材来源（见下方 `_corpus`）。若无从解析，这里抛错。

- **第 81 行** `keyword_hits = self._keyword_search(query, corpus)`
  **关键词通道**：词面覆盖度打分（见下方），内部也施加 filters（B2）。

- **第 82 行** `fused = self._rrf([vector_hits, keyword_hits])`
  RRF 融合两通道，按 chunk_id 去重。

- **第 83 行** `return fused[: query.top_k]`
  融合后取前 top_k。
  - 为什么融合后再截 top_k：两通道各 top_k，融合后有最多 2×top_k 候选，取全局最优 top_k。

- **第 85-99 行**：_corpus 素材解析（S2 落点）

  - **第 85 行** `def _corpus(self) -> list[Chunk]:`
  - **第 86 行** `"""解析关键词通道素材来源；缺失则显式报错（不允许静默退化）。"""` 点明 S2。
  - **第 87-88 行** `if self._all_chunks is not None: return self._all_chunks`
    优先用构造期显式传入的素材。
  - **第 89 行** `getter = getattr(self._store, "all_chunks", None)`
    鸭子取用 store 的 `all_chunks` 方法（若存在）。
    - 为什么用 `getattr` 而非直接 `self._store.all_chunks()`：`all_chunks` 不在 `VectorStore` 协议里（协议只有 upsert/search），是内存实现额外提供的能力。用 `getattr` 避免对不实现它的 store 报错。
  - **第 90 行** `if callable(getter):`
    检查取到的确实是可调用对象（而非其他同名属性）。
  - **第 91-93 行** 注释：解释为何需要 `cast`——`getattr` 的静态类型是 `object`，`callable()` 只收窄到 `Callable[..., object]`，返回值仍是 `object`，传给 `list()` 会因不满足 `Iterable` 报错。真实契约由 `store.all_chunks() -> list[Chunk]` 保证，`cast` 显式还原类型。
  - **第 94 行** `corpus_getter = cast(Callable[[], list[Chunk]], getter)`
    类型断言：告诉类型检查器 getter 是「无参返回 list[Chunk] 的可调用对象」。
    - 注意 `cast` 运行时无操作，纯粹是类型提示。
  - **第 95 行** `return list(corpus_getter())`
    调用并再包一层 `list()`（防御性拷贝，与 store.all_chunks 第 73 行呼应）。
  - **第 96-99 行** `raise ValueError(...)`
    **S2 核心**：两种素材来源都没有时显式抛错，错误信息给出两种解决方式（传 all_chunks 或让 store 实现 all_chunks）。
    - 为什么不静默退化为纯向量：静默退化会让人以为在用混合召回，实际只有向量通道——词面召回的增益悄悄消失，且难以察觉。显式抛错让配置错误立刻暴露。

- **第 101-116 行**：_keyword_search 关键词通道

  - **第 101 行** `def _keyword_search(self, query: Query, corpus: list[Chunk]) -> list[Hit]:`
  - **第 102 行** `"""词面覆盖度打分：先按 filters 过滤，再算 query 词项命中比例。"""` 说明两步。
  - **第 103 行** `terms = {w for w in query.text.lower().split() if w}`
    查询文本小写切词成集合（set）。
    - 为什么用 set：自动去重，避免重复词项抬高覆盖率分母。
    - `if w` 过滤空串（split 连续空格会产生空串）。
  - **第 104-105 行** `if not terms or not corpus: return []`
    空查询词或无素材 → 无命中（除零保护）。
  - **第 106 行** `hits: list[Hit] = []` 结果容器。
  - **第 107 行** `for chunk in corpus:` 遍历素材块。
  - **第 108-109 行** `if not matches_filters(chunk, query.filters): continue`
    **B2 关键行**：关键词通道复用与向量通道完全相同的 `matches_filters`。
    - 不这么写（关键词通道自己过滤或不过滤）：关键词通道会成为权限/时效过滤的旁路——无权文档只要词面匹配就被召回，造成越权注入。
  - **第 110 行** `body = chunk.text.lower()` 块正文小写化，与词项大小写一致。
  - **第 111 行** `covered = sum(1 for t in terms if t in body)`
    统计有多少查询词项出现在正文中。
    - 注意是子串匹配（`t in body`），不是精确词匹配——词面近似，接受词形包含。
  - **第 112 行** `score = covered / len(terms)`
    覆盖率 = 命中词项数 / 总词项数，取值 (0, 1]。
  - **第 113-114 行** `if score > 0: hits.append(Hit(chunk=chunk, score=score, source=self.name))`
    只有覆盖率 > 0（至少一个词命中）才成为 Hit。source 标记为 `hybrid_retriever`。
    - 为什么不收 score=0 的：零覆盖块与查询无关，召回无意义。
  - **第 115 行** `hits.sort(key=lambda h: h.score, reverse=True)` 按覆盖率降序。
  - **第 116 行** `return hits[: query.top_k]` 取前 top_k。

- **第 118-137 行**：_rrf RRF 融合核心

  - **第 118 行** `@staticmethod` 静态方法。
  - **第 119 行** `def _rrf(channels: list[list[Hit]]) -> list[Hit]:`
    输入多个通道的命中列表，输出融合列表。
  - **第 120 行** `"""按 RRF 融合多通道，按 chunk_id 去重，分数写回 score。"""` 说明。
  - **第 121 行** `weights = (_VECTOR_WEIGHT, _KEYWORD_WEIGHT)`
    通道权重元组，顺序与 channels 对应（向量、关键词）。
  - **第 122 行** `acc: dict[str, tuple[Hit, float]] = {}`
    累加器：chunk_id → (命中对象, 累加分数)。用 dict 实现按 id 去重。
  - **第 123 行** `for idx, hits in enumerate(channels):`
    遍历每个通道，`idx` 用于取权重。
  - **第 124 行** `weight = weights[idx] if idx < len(weights) else _VECTOR_WEIGHT`
    按通道索引取权重，超出预设通道数时回退到向量权重。
    - 为什么有回退：方法签名允许多于 2 个通道（灵活），但权重表只定义了 2 个；回退保证不越界。
  - **第 125 行** `for rank, hit in enumerate(hits, start=1):`
    遍历通道内命中，`rank` 从 **1** 起（不是 0）。
    - **为什么从 1 起**：RRF 公式 `weight/(k+rank)`，rank=1 对应第一名。若从 0 起，第一名分母为 k+0，与 k=60 的设计（平滑名次差异）语义不符；且 rank=0 会让第一名分数虚高。
  - **第 126 行** `cid = hit.chunk.chunk_id` 取块 id 作去重键。
  - **第 127 行** `contribution = weight / (RRF_K + rank)`
    **RRF 核心公式**：本通道本名次的贡献 = 权重 / (60 + 名次)。
    - 第 1 名贡献 1/61，第 2 名 1/62……名次越靠后贡献越小，且衰减平缓（k=60 起平滑作用）。
  - **第 128 行** `if cid in acc:` 该块已在其他通道出现过。
  - **第 129-130 行** `prev_hit, prev_score = acc[cid]; acc[cid] = (prev_hit, prev_score + contribution)`
    累加分数（同名块在多通道命中，分数叠加）。保留先出现的 Hit 对象（source 记为首次出现的通道）。
  - **第 131-132 行** `else: acc[cid] = (hit, contribution)`
    首次出现的块，记录贡献分。
  - **第 133-135 行** 重建 Hit 列表：
    `merged = [Hit(chunk=h.chunk, score=s, source=h.source) for h, s in acc.values()]`
    把累加结果（(Hit, 分数)）转成新的 Hit，分数写回 `score`。
    - 为什么要重建而非原地改：Hit 是 pydantic 模型，且这里想保留首次命中对象的 source；重建更清晰，避免修改累加器里的原对象。
  - **第 136 行** `merged.sort(key=lambda h: h.score, reverse=True)` 按融合分降序。
  - **第 137 行** `return merged` 返回。

  **为什么用 RRF（而非直接比较原始分数）**：向量通道是点积（量纲取决于向量归一化，约 [-1,1]），关键词通道是覆盖率（[0,1]），**量纲与分布都不同**，直接相加或比较没有意义。RRF 只看「名次」——把每个通道的排序名次转成可比的分值再融合，既跨量纲可比，又天然按 chunk_id 去重。这是信息检索领域的经典做法。

---

## 6. rerank.py 逐行讲解（47 行）

重排段：用词面覆盖度近似 cross-encoder 精排（离线可测）。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R6 重排段：Reranker 协议 + cross-encoder 风格默认实现。`
  一句话定位。

- **第 2-4 行** 说明：真实 cross-encoder 是外部模型，抽象为可注入 client；默认用 query 词项覆盖度近似精排（离线可测），生产注入真模型零改上层。

- **第 5 行** `"""` docstring 结束。

### 第 6-12 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol, runtime_checkable` 协议工具。

- **第 10 行** `from harness.rag.types import Hit, Query` 导入 DTO。

- **第 12 行** `__all__ = ["LexicalReranker", "Reranker"]` 导出协议与实现。

### 第 15-23 行：Reranker 协议

- **第 15 行** `@runtime_checkable` 运行时可检查。

- **第 16 行** `class Reranker(Protocol):` 重排协议。

- **第 17 行** `"""重排协议。"""` 定位。

- **第 19 行** `name: str` 具名属性。

- **第 21 行** `def rerank(self, hits: list[Hit], query: Query) -> list[Hit]:`
  **注意是同步方法**（非 async）。为什么：词面重排是纯 CPU 计算；真实 cross-encoder 若需网络则在自己实现里按需处理（协议不强制 async）。
  - 注意第二个参数是完整 `Query` 对象（不只文本），便于未来用 purpose/filters 等做精细重排。

- **第 22-23 行** docstring + `...`。

### 第 26-33 行：LexicalReranker 构造

- **第 26 行** `class LexicalReranker:`
  默认重排：按 query 词项在块正文的覆盖度精排。

- **第 27 行** `"""默认重排：按 query 词项在块正文的覆盖度精排，取 top_n。"""` 一句话。

- **第 29 行** `name: str = "lexical_reranker"` 具名标识。

- **第 31 行** `def __init__(self, top_n: int = 5) -> None:` 构造，top_n 默认 5。

- **第 32 行** `"""构造重排器，top_n 为保留候选数。"""` 说明。

- **第 33 行** `self.top_n = top_n` 保存。
  - 注意：这里没有像 splitter 那样校验 `top_n > 0`。`top_n=0` 时 `ranked[:0]` 返回空列表（合法，表示不要候选），负数会从尾部切——这是当前实现的宽松之处，但调用方通常传正数。

### 第 35-47 行：rerank 方法

- **第 35 行** `def rerank(self, hits: list[Hit], query: Query) -> list[Hit]:` 协议实现。

- **第 36 行** `"""按词项覆盖度重排并返回前 top_n。"""` 说明。

- **第 37 行** `terms = {w for w in query.text.lower().split() if w}`
  查询词项集合（小写、去重、去空）。与 retriever._keyword_search 第 103 行同款逻辑。

- **第 38-39 行** `if not terms: return list(hits[: self.top_n])`
  查询无词项时**不改序**，直接返回前 top_n。
  - 为什么不排序：无词项时所有覆盖度都是 0，排序无意义；保留召回阶段的原有顺序（通常已按相关度排过）。
  - `list(...)` 做浅拷贝，避免返回原列表切片视图被外部改（切片本身已是新列表，这里更明确）。

- **第 41-44 行** 内部函数 coverage：
  - **第 41 行** `def coverage(hit: Hit) -> float:` 定义打分函数。
  - **第 42 行** `"""统计 query 词项在块正文的命中比例。"""` 说明。
  - **第 43 行** `body = hit.chunk.text.lower()` 块正文小写化。
  - **第 44 行** `return sum(1 for t in terms if t in body) / len(terms)`
    覆盖率 = 命中词项数 / 总词项数。作为排序键。
    - 定义在函数内的好处：闭包捕获 `terms`，无需额外传参。

- **第 46 行** `ranked = sorted(hits, key=coverage, reverse=True)`
  按覆盖率降序重排。
  - 注意 `sorted` 是稳定排序：覆盖率相同的命中保持召回阶段的相对顺序。

- **第 47 行** `return ranked[: self.top_n]` 取前 top_n。

  **与 retriever 关键词通道的区别**：两者都用覆盖率，但用途不同——retriever 用它**召回**（从全库中找候选），rerank 用它**精排**（在已召回的候选里重新排序）。rerank 更靠后，处理的是更小的候选集。

---

## 7. post.py 逐行讲解（161 行）

后处理段：过滤（含注入 fail-closed）+ 预算拼接 + 引用 + 乱序修复。B2、B3、S2 的兜底落点。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R6 后处理段：过滤 + 预算拼接 + 引用溯源 + 注入检测接入。`
  一句话定位。

- **第 2-4 行** 说明流程：先过滤（含注入 fail-closed），再按 token 预算贪心装入，最后生成引用并做乱序修复。

- **第 5 行** `"""` docstring 结束。

### 第 6-14 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol, runtime_checkable` 协议工具。

- **第 10 行** `from harness.rag.store import matches_filters`
  **导入共享过滤函数**（B2）——后处理是融合后的统一兜底过滤点。

- **第 11 行** `from harness.rag.types import Hit, RAGResult, TokenBudget` 导入 DTO。

- **第 12 行** `from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector`
  **横切复用 R5 skills**：三个符号分别是检测器协议、裁决枚举、默认检测器。这印证了读前 GPS 里说的「skills 的 detector 被 rag/post 复用」。

- **第 14 行** `__all__ = ["EMPTY_CONTEXT", "DefaultPostProcessor", "PostProcessor"]`
  导出空提示常量、协议、实现。

### 第 16-20 行：常量

- **第 16 行** `# 空结果固定提示文本（具名常量，禁魔法字符串）。` 纪律注释。

- **第 17 行** `EMPTY_CONTEXT: str = "No relevant documents were found. Answer cautiously from general knowledge or decline to answer."`
  空结果时注入 LLM 的固定提示，让模型「谨慎作答或拒答」。
  - 为什么不返回空串：空上下文会让模型完全凭空编造（幻觉）；固定提示明确告知「没找到相关资料」，引导模型拒答。
  - 为什么用英文：LLM 提示词常用英文，稳定且不易被误判为文档内容。

- **第 19-20 行** `_CITATION_FMT: str = "[{cid}]"`
  引用标注模板。私有常量。`format(cid=...)` 生成如 `[doc1:0]` 的引用。

### 第 23-47 行：PostProcessor 协议

- **第 23 行** `@runtime_checkable` 运行时可检查。

- **第 24 行** `class PostProcessor(Protocol):` 后处理协议（五段之一）。

- **第 25 行** `"""后处理协议（R6 五段之一）。"""` 定位。

- **第 27 行** `name: str` 具名属性。

- **第 29-36 行** `async def filter(self, hits, detector=None, filters=None) -> list[Hit]:`
  过滤方法协议。`async` 因为真实实现可能做异步检测。
  - 参数：`detector` 可覆盖检测器，`filters` 过滤条件。

- **第 38-47 行** `async def assemble(self, hits, budget, *, detector=None, filters=None) -> RAGResult:`
  装配方法协议。`budget` 必填位置参数，`detector`/`filters` 关键字参数。

### 第 50-68 行：DefaultPostProcessor 构造

- **第 50 行** `class DefaultPostProcessor:`
  默认后处理：注入 fail-closed + 贪心预算拼接 + 引用溯源 + 乱序修复。

- **第 51-55 行** docstring：重点第 53-54 行说明乱序修复（anti lost-in-the-middle）——相关度最高的放两端，次高的居中。

- **第 57 行** `name: str = "default_post"` 具名标识。

- **第 59 行** `def __init__(self, detector: InjectionDetector | None = None) -> None:` 构造，detector 可选。

- **第 60-65 行** docstring：第 63-64 行强调「None 时用 R5 默认检测器（fail-closed，不存在缺席放行）」。

- **第 66-68 行** `self._detector: InjectionDetector = (detector if detector is not None else KeywordInjectionDetector())`
  **fail-closed 落地行**：任何情况下检测器都存在。
  - 做什么：显式传入就用它，否则用默认关键词检测器。
  - 为什么：正文注入检测是披露前最后一环，绝不能因「没配检测器」而跳过。传 None 不会导致放行。
  - 与 skills.provider 第 131 行同款纪律。

### 第 70-95 行：filter 兜底过滤

- **第 70-75 行** `async def filter(self, hits, detector=None, filters=None) -> list[Hit]:` 协议实现。

- **第 76-85 行** docstring：
  - 第 76 行：「元数据/权限/时效 + 注入检测，两者任一不过即剔除」。
  - 第 80 行：`detector` 覆盖本次检测（为 B3 服务）。
  - 第 84 行：**「这是融合后的统一兜底过滤，不依赖各召回通道各自正确（B2）」**——关键设计声明。

- **第 86 行** `used = detector if detector is not None else self._detector`
  选定本次检测器：方法参数优先，否则用实例自带。
  - **B3 落地**：pipeline 可以把自己的 detector 传进来（通过 assemble → filter），覆盖 post 构造时的默认值，让 `RAGPipeline(detector=...)` 真正生效。

- **第 87 行** `kept: list[Hit] = []` 结果容器。

- **第 88 行** `for hit in hits:` 逐个命中检查。

- **第 89-90 行** `if not matches_filters(hit.chunk, filters): continue`
  **兜底施加元数据过滤**（B2）：即便各召回通道已过滤，这里再过滤一次，防止遗漏。

- **第 91 行** `verdict = used.detect(hit.chunk.text)`
  对块正文做注入检测，得到裁决枚举。

- **第 92-93 行** `if verdict is InjectionVerdict.INJECTED: continue`
  **注入 fail-closed 核心行**：命中注入就剔除该块。
  - 用 `is` 比较枚举成员（单例），正确。
  - 不这么写（放行或净化）：不可信正文进入 LLM 上下文可能触发提示注入攻击，危害系统。这里选择剔除（fail closed）而非净化。

- **第 94 行** `kept.append(hit)` 两项都通过的命中保留。

- **第 95 行** `return kept` 返回。

### 第 97-147 行：assemble 装配

- **第 97-104 行** `async def assemble(self, hits, budget, *, detector=None, filters=None) -> RAGResult:` 协议实现。

- **第 105-115 行** docstring：
  - 第 109 行：预算三态（None 不限、0 无预算→空结果）。
  - 第 110 行：**B3**——detector 覆盖让 pipeline.detector 生效。
  - 第 111 行：**B2**——filters 兜底施加。

- **第 116 行** `limit = budget.limit` 取出预算数字（可能是 None/0/正整数）。

- **第 117-118 行** `if limit == 0: return RAGResult(empty=True, context=EMPTY_CONTEXT, hits=[], citations=[])`
  **`limit=0` 是「无预算」不是「不限」**（易错点）：直接返回空结果。
  - 为什么：0 表示一点预算都没有，不能装任何块；若当成不限会往上下文塞满内容，违背预算约束。
  - 注意 `limit=0` 的判断用 `== 0`，而 `None` 不会等于 0，所以 `None`（不限）走正常流程。

- **第 120 行** `safe = await self.filter(hits, detector=detector, filters=filters)`
  先过滤（元数据 + 注入）。把 detector/filters 透传下去（B2/B3）。

- **第 121-122 行** `if not safe: return RAGResult(empty=True, ...)`
  过滤后无有效命中 → 空结果。

- **第 124 行** `ordered = self._reorder_middle(safe)`
  **乱序修复**：重排命中顺序，抗 lost-in-the-middle（见下方 _reorder_middle）。

- **第 125 行** `kept: list[Hit] = []` 实际装入的命中。

- **第 126 行** `parts: list[str] = []` 各块文本片段（含引用）。

- **第 127 行** `used_tokens = 0` 已用 token 累计。

- **第 128 行** `for hit in ordered:` 贪心装入循环。

- **第 129 行** `need = hit.chunk.tokens` 本块所需 token。

- **第 130-131 行** `if limit is not None and used_tokens + need > limit: continue`
  **贪心跳过核心**：
  - 做什么：若装下本块会超预算，就**跳过本块**，继续试下一块（可能更小）。
  - 为什么 `limit is not None` 守卫：`limit=None` 表示不限，跳过此判断。
  - **为什么是 `continue` 而非 `break`**：这是「贪心跳过」，不是「整段丢弃」。当前块装不下不代表后面的小块也装不下；跳过能装下更多块，提高预算利用率。
  - 不这么写（break）：一旦遇到大块就停止，后面能装的小块全丢，浪费预算。

- **第 132 行** `cite = _CITATION_FMT.format(cid=hit.chunk.chunk_id)`
  生成引用标注，如 `[doc1:0]`。

- **第 133 行** `parts.append(f"{hit.chunk.text} {cite}")`
  块正文后接引用标注，加入片段列表。

- **第 134 行** `kept.append(hit)` 记录装入的命中。

- **第 135 行** `used_tokens += need` 累加已用 token。

- **第 137-138 行** `if not kept: return RAGResult(empty=True, ...)`
  一块都没装下（例如所有块 tokens 都超预算）→ 空结果。
  - 注意：limit=0 已在第 117 行提前返回；这里是「预算虽非 0 但装不下任何块」的情况（如 limit=10 但每块 100 token）。

- **第 140 行** `citations = [h.chunk.chunk_id for h in kept]`
  引用列表 = 实际装入块的 chunk_id（逐条可在 hits 中溯源）。

- **第 141-147 行** 构造 RAGResult：
  - **第 142 行** `context="\n".join(parts)`：片段用换行拼接。
  - **第 143 行** `citations=citations`。
  - **第 144 行** `hits=kept`：只含实际装入的命中。
  - **第 145 行** `empty=False`：有内容。
  - **第 146 行** `tokens_used=used_tokens`：消耗的 token。

### 第 149-161 行：_reorder_middle 乱序修复

- **第 149 行** `@staticmethod` 静态方法。

- **第 150 行** `def _reorder_middle(hits: list[Hit]) -> list[Hit]:`
  把高相关片段交错放两端，低相关居中。

- **第 151 行** `"""把高相关片段交错放两端，低相关居中（抗 lost-in-the-middle）。"""`
  点明目的：抗 lost-in-the-middle。

- **第 152-153 行** `if len(hits) <= 2: return list(hits)`
  长度 ≤ 2 时原样返回（ reorder 无意义——就两三个位置，中间丢失问题不明显）。

- **第 154 行** `left: list[Hit] = []` 放左端（开头）的片段。

- **第 155 行** `right: list[Hit] = []` 放右端（结尾）的片段。

- **第 156 行** `for i, hit in enumerate(hits):`
  遍历，用下标奇偶分组。输入 `hits` 已按相关度降序（前面的更相关）。

- **第 157-160 行** `if i % 2 == 0: left.append(hit) else: right.append(hit)`
  偶数下标（第 1、3、5… 相关）进 left，奇数下标（第 2、4、6… 次相关）进 right。
  - 效果：最相关的（下标 0）在 left 开头 → 最终在上下文最前；第二相关（下标 1）进 right → 最终在末尾附近；越靠后的低相关片段越往中间挤。

- **第 161 行** `return left + list(reversed(right))`
  **关键拼接**：`left` 正序 + `right` 反转。
  - 为什么 `reversed(right)`：right 里是按相关度降序收集的（越靠前越相关），反转后最相关的（right 第一个）出现在拼接结果的**末尾**（紧邻 left 之后？不——反转让 right 末尾的原低相关项来到拼接中部，而 right 里较相关的项靠近末尾）。
  - 实际效果举例：输入相关度 [A(1.0), B(0.9), C(0.8), D(0.7), E(0.6)]：
    - left = [A, C, E]，right = [B, D]，reversed(right)=[D, B]
    - 输出 = [A, C, E, D, B]
    - 位置：开头 A（最相关）、末尾 B（第二相关）、中间 E/D/C（较低相关）。
  - 这正是 lost-in-the-middle 防护：LLM 对长上下文**开头和结尾**最敏感、中部最易忽略，所以把最相关的放两端、次相关/低相关放中间。
  - 不这么写（直接按相关度顺序拼）：最相关的在开头没问题，但第二、第三相关紧随其后，中部塞满中等相关，末尾是最不相关的——浪费了「末尾高敏感区」。

---

## 8. rewrite.py 逐行讲解（193 行）

改写段：无模型规则改写（同义词 + 拆子句），两轮组装（B1 修复核心）。D2 红线（连续子串）落点。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R6 改写段：QueryRewriter 协议 + 无模型的规则改写默认实现。`
  一句话定位。

- **第 2-6 行** 说明：
  - 第 3-5 行：默认改写器做「同义词等价替换 + 拆子句」，产出文本不同、语义等价的变体，提升词面覆盖；全程纯字符串规则，不调 LLM/网络/文件，不引入新实体。
  - 第 6 行：词表可注入替换，生产可注入 HyDE/step-back 策略。
  - **D2 红线**：同义词映射值是等价词、拆分子串必须来自原串——不引入原问题之外的新实体。

- **第 7 行** `"""` docstring 结束。

### 第 8-14 行：imports 与导出

- **第 8 行** `from __future__ import annotations` 延迟注解。

- **第 10 行** `from typing import Protocol, runtime_checkable` 协议工具。

- **第 12 行** `from harness.rag.types import Query` 导入 Query DTO。

- **第 14 行** `__all__ = ["DEFAULT_N_VARIANTS", "MultiQueryRewriter", "QueryRewriter"]`
  导出常量、协议、实现。

### 第 16 行：DEFAULT_N_VARIANTS 常量

- **第 16 行** `DEFAULT_N_VARIANTS: int = 3`
  默认最多返回 3 个变体（含原串）。权衡：太少增益有限，太多增加向量化/召回开销。

### 第 18-23 行：_SPLIT_TOKENS 切分符

- **第 18-19 行** 注释：切分符统一具名（禁魔法串）；**按长度降序排列**，确保「以及」先于「及」匹配，避免短词抢先切开。
  这是关键设计说明。

- **第 20-23 行** `_SPLIT_TOKENS: tuple[str, ...] = (...)`
  切分符元组：中文标点（"。？！，、；"）、中文连接词（"以及/和/与/及/并"）、英文（"then"/" and "/" or "）。
  - 为什么「以及」在「及」前、`" and "` 带空格：优先匹配长符，防止短符把词切碎。例如「A以及B」若先按「及」切会得「A以及」残留，不对；按「以及」切干净。
  - 英文连接词带两侧空格（`" and "`）：避免把 "and" 从单词中间切开（如 "android" 不该被 "and" 切）。

### 第 25-40 行：_DEFAULT_SYNONYMS 同义词表

- **第 25-26 行** 注释：key 与 value 都是「等价词」，替换不引入新实体；value 用元组承载多个候选，取首个作确定性替换（同输入同输出）。

- **第 27-40 行** `_DEFAULT_SYNONYMS: dict[str, tuple[str, ...]] = {...}`
  6 组等价词对（互为 key/value）：召回↔检索、精排↔重排、向量↔嵌入、关键词↔词面、文档↔语料、怎么↔如何。
  - 为什么互为键值：保证任一方向都能替换（「召回→检索」和「检索→召回」）。
  - **D2 红线**：value 都是 key 的等价词（同义），不引入原问题之外的新实体。若注入方给了非等价词（如「RAG→区块链」）就违反红线，责任在注入方（构造 docstring 第 140-141 行声明）。
  - 为什么 value 用元组而非字符串：预留多候选扩展（未来一个词可映射多个等价词），当前取首个 `candidates[0]` 保证确定性。

### 第 43-51 行：QueryRewriter 协议

- **第 43 行** `@runtime_checkable` 运行时可检查。

- **第 44 行** `class QueryRewriter(Protocol):` 查询改写协议。

- **第 45 行** `"""查询改写协议。"""` 定位。

- **第 47 行** `name: str` 具名属性。

- **第 49 行** `async def rewrite(self, query: Query) -> list[Query]:`
  **输入一个 Query，输出多个 Query**。注意返回的是 Query 对象列表（非纯文本），以便继承 filters/top_k 等字段。
  - `async` 因为生产可注入 HyDE（需调 LLM）。

- **第 50-51 行** docstring + `...`。

### 第 54-91 行：_split_subqueries 拆子句

- **第 54 行** `def _split_subqueries(text: str) -> list[str]:`
  按标点/连接词把查询拆成连续子串子句。

- **第 55-65 行** docstring：
  - 第 57-58 行：迭代扫描，逐个找切分符位置，切片后从符后继续扫，**保证每个产物都是原串连续子串（守 D2 红线）**；空片段丢弃，去重保序。
  - 第 64 行：无切分符时返回空列表（由调用方补原串）。

- **第 66 行** `pieces: list[str] = []` 子句容器。

- **第 67 行** `start = 0` 当前子句起点。

- **第 68 行** `cursor = 0` 扫描游标。

- **第 69 行** `while cursor < len(text):` 扫描循环。

- **第 70 行** `matched = next((t for t in _SPLIT_TOKENS if text.startswith(t, cursor)), None)`
  在游标位置按 `_SPLIT_TOKENS` 顺序（长符在前）找第一个匹配的切分符。
  - `next(..., None)`：找不到返回 None。
  - 为什么用 `startswith(t, cursor)` 而非 `t in text[cursor:]`：精确判断「游标处」是否以切分符开头，避免匹配到更后面的符。

- **第 71-73 行** 注释：解释命中切分符时的处理，以及 strip 的合理性（英文连接词残留空格，strip 后仍是连续子串，不破 D2）。

- **第 74 行** `if matched is not None:` 命中切分符分支。

- **第 75 行** `piece = text[start:cursor].strip()`
  取符前片段并 strip 去首尾空格。
  - **为什么 strip 不破 D2**：strip 只去首尾空白（原串切分符附近本就有空格），剩下的仍是原串的连续子串。

- **第 76-77 行** `if piece: pieces.append(piece)` 非空才收（丢弃空片段）。

- **第 78 行** `cursor += len(matched)` 游标跳过切分符。

- **第 79 行** `start = cursor` 新子句起点设为符后。

- **第 80-81 行** `else: cursor += 1`
  未命中切分符 → 游标前进 1 字符继续扫。

- **第 82 行** `tail = text[start:].strip()`
  循环结束后收尾片段（最后一个切分符之后的部分）。

- **第 83-84 行** `if tail: pieces.append(tail)` 非空则收。

- **第 85-90 行** 去重保序：
  - **第 85 行** `seen: set[str] = set()` 已见集合。
  - **第 86 行** `unique: list[str] = []` 结果。
  - **第 87-90 行** 遍历 pieces，未在 seen 中的加入 unique。
  - 为什么去重：重复子句（如「A和A」）产生相同变体，浪费窗口名额。

- **第 91 行** `return unique` 返回子句列表。

### 第 94-122 行：_synonym_variants 同义替换

- **第 94-96 行** 函数签名：`text`、`synonyms`、关键字参数 `limit=None`。

- **第 97-110 行** docstring：
  - 第 99-100 行：一轮只替换一种 key 的首个出现（取 value 首个候选），避免组合爆炸；多命中各自独立产出变体，顺序按词表插入序，保证确定性。
  - 第 101 行：`limit` 防止整串多个命中把子句挤出截断窗口（B1 同类问题）。

- **第 111 行** `out: list[str] = []` 结果容器。

- **第 112 行** `for key, candidates in synonyms.items():`
  遍历同义词表（dict 保持插入序，保证确定性）。

- **第 113-114 行** `if limit is not None and len(out) >= limit: break`
  达到数量上限就停（limit 生效）。

- **第 115 行** `if not candidates or key not in text: continue`
  无候选或文本不含该 key → 跳过。

- **第 117 行** 注释：只替换首个命中处，单个变体只改一个词，保持与原串最小差异。

- **第 118 行** `replaced = text.replace(key, candidates[0], 1)`
  **关键**：`replace(key, 值, 1)` 的第三个参数 1 表示只替换**第一次出现**。
  - 为什么不全部替换：一次只改一个词，保持最小差异；且避免组合爆炸（全替换会产生组合）。

- **第 119 行** 注释：解释等值映射（如 `{"k": ("k",)}`）时结果等于原串，不入列。

- **第 120-121 行** `if replaced != text: out.append(replaced)`
  只收与原串不同的变体，兑现 docstring「均不同」承诺。

- **第 122 行** `return out` 返回。

### 第 125-146 行：MultiQueryRewriter 构造

- **第 125 行** `class MultiQueryRewriter:` 无模型规则改写器。

- **第 126 行** `"""无模型规则改写：同义词替换 + 标点/连接词拆子句，产出等价多查询。"""` 一句话。

- **第 128 行** `name: str = "multi_query"` 具名标识。

- **第 130-134 行** 构造签名：`n_variants=3`、`synonyms=None`。

- **第 135-142 行** docstring：
  - 第 138 行：n_variants 小于 1 按 1 处理。
  - 第 139-141 行：自定义同义词表 None 时用内置；**注入后完全替换内置表**；等价性责任在注入方（value 必须是等价词，否则违反 D2）；构造期浅拷贝，构造后改源 dict 不影响本实例。

- **第 143 行** `self.n_variants = max(1, n_variants)`
  下限保护：至少 1（原串总要在）。

- **第 144 行** 注释：浅拷贝隔离调用方 dict，保住 D4 确定性。

- **第 145 行** `source = synonyms if synonyms is not None else _DEFAULT_SYNONYMS`
  选自定义表或内置表。

- **第 146 行** `self.synonyms: dict[str, tuple[str, ...]] = dict(source)`
  **浅拷贝**：`dict(source)` 复制一份。
  - 为什么：防止构造后外部修改源 dict 改变本实例行为（确定性 D4）。
  - 注意是浅拷贝：value（元组）本身不可变，所以浅拷贝足够——元组不可变，共享无风险。

### 第 148-193 行：rewrite 两轮组装（B1 修复核心）

- **第 148 行** `async def rewrite(self, query: Query) -> list[Query]:` 协议实现。

- **第 149-160 行** docstring：
  - 第 151-153 行：流程（原串 + 拆分子句 + 同义替换 → 去重保序 → 截断）；无改动时只返回原串；所有变体仅覆盖 text，其余字段 model_copy 继承。
  - 第 159 行：返回非空，首个为原串。

- **第 161 行** `text = query.text` 取原串。

- **第 162 行** `subqueries = _split_subqueries(text)` 拆子句。

- **第 164-169 行** 注释：**交错组装（B1 修复）**详细说明——按「原串 → 整串同义 → 子句1 → 子句1同义 → 子句2…」排，保证每种策略在截断窗口内都有代表；默认 N=3 必含至少 1 个同义替换变体，避免子句把同义挤出导致核心召回增益失效；第一轮「基础覆盖」子句先于子句同义入列，避免每个子句占 2 名额饿死后段（S-R1）。

- **第 170 行** `text_synonyms = _synonym_variants(text, self.synonyms)`
  算出整串的同义变体列表。

- **第 171 行** `candidates: list[str] = [text]`
  **候选起点：原串排第一**（保证首个变体是原串）。

- **第 172-173 行** `if text_synonyms: candidates.append(text_synonyms[0])`
  加入整串同义的**第一个**（词面变化代表）。

- **第 174 行** `candidates.extend(subqueries)`
  加入**全部子句**（粒度变化代表）。
  - **为什么子句先于子句同义入列（S-R1）**：若先放「子句1 + 子句1同义 + 子句2 + 子句2同义…」，N 小时后段子句根本进不了窗口。先放全部子句，保证粒度策略均衡覆盖。

- **第 175-176 行** 注释：第二轮「增益填充」（N 较大时填剩余预算，S-R2）——整串同义余量 + 各子句同义，放大词面覆盖。

- **第 177 行** `candidates.extend(text_synonyms[1:])`
  加入整串同义的余量（第 2 个及以后）。

- **第 178-179 行** `for piece in subqueries: candidates.extend(_synonym_variants(piece, self.synonyms, limit=1))`
  各子句各取 1 个同义变体。
  - `limit=1`：每个子句只献 1 个同义，防止某子句的多个同义挤占其他子句名额（与 _synonym_variants 的 limit 设计呼应）。

- **第 181 行** 注释：去重保序，首份必为原串（FR-5）。

- **第 182-187 行** 去重保序：
  - **第 182 行** `seen: set[str] = set()`
  - **第 183 行** `texts: list[str] = []`
  - **第 184-187 行** 遍历 candidates，未见的加入。
  - 为什么去重：两轮组装可能产生重复文本（如子句与原串相同、同义与外部重复），去重避免重复向量化与重复召回。

- **第 189 行** 注释：截断后派生 Query，只改 text，其余字段深拷贝继承（FR-6）。

- **第 190-193 行** 派生 Query：
  - **第 191 行** `query.model_copy(deep=True, update={"text": variant})`
    **只改 text，其余字段深拷贝继承**。
    - 为什么 `model_copy` 而非重建 Query：自动继承 filters/top_k/token_budget/purpose 等所有字段，无需逐一复制，且 DTO 新增字段时自动跟随。
    - 为什么 `deep=True`：Query 的 `filters` 是 dict（可变），深拷贝确保各变体不共享同一 filters 对象，避免一个变体改动影响其他。
    - `update={"text": variant}`：只覆盖 text。
  - **第 192 行** `for variant in texts[: self.n_variants]`
    截断到 n_variants（含原串）。
  - **第 193 行** `]` 列表推导结束。

  **两轮组装为什么能防「子句挤出同义」（B1 修复）**：如果简单地把「原串 + 所有子句 + 所有同义」按某顺序一股脑塞进窗口再截断，N=3 时很可能前 3 个全是子句，同义策略完全缺席——而同义替换是词面覆盖的核心增益。第一轮强制「原串 + 1 个整串同义 + 全部子句」进入候选前部，保证 N=3 时同义必有代表；第二轮才在 N 较大时补余量。这就是「基础覆盖 + 增益填充」两轮设计的价值。

---

## 9. pipeline.py 逐行讲解（338 行）

核心总控：履约 RAGPort，编排入库与检索全链路，附带缓存与失效。本文件是缓存纪律（S4/FR-1~5）、同源 embedder（B1）、预算派生（S3）的集中落点。

### 第 1-10 行：模块 docstring

- **第 1 行** `"""R6 流水线：RAGPipeline 履约 RAGPort，编排入库与检索全链路。`
  一句话定位。

- **第 2-5 行** 说明双轨入口：
  - `ingest`/`retrieve` 对齐 RAGPort（框架装配用，DTO 为 DynamicValue）。
  - `query` 为富入口（返回结构化 RAGResult，R6 单测与 R12 精细消费用）。
  - 为什么双轨：框架装配层只认 RAGPort 契约（DynamicValue），而单测/上层需要结构化结果——两套入口覆盖两种消费场景。

- **第 6-9 行** 说明检索链：改写 → 召回 → RRF（在 Retriever 内）→ 重排 → 后处理；以及三条纪律：查询向量由注入 embedder 预算（同源）、detector/filters 贯穿后处理兜底、缓存键含全部影响检索的参数。

- **第 10 行** `"""` docstring 结束。

### 第 11-35 行：imports 与导出

- **第 11 行** `from __future__ import annotations` 延迟注解。

- **第 13 行** `from typing import Any, Protocol, runtime_checkable`
  - `Any`：定义 DynamicValue 别名。
  - 其余协议工具。

- **第 15-21 行** 从各段模块导入协议：
  - 第 15 行 `Embedder`、第 16 行 `PostProcessor`、第 17 行 `Reranker`、第 18 行 `Retriever`、第 19 行 `QueryRewriter`、第 20 行 `Splitter`、第 21 行 `VectorStore`。
  - 依赖方向：pipeline 依赖所有段的协议，是编排者。

- **第 22-29 行** `from harness.rag.types import (...)` 导入 DTO：`Chunk`、`Document`、`Hit`、`Query`、`RAGResult`、`TokenBudget`。

- **第 30 行** `from harness.skills import InjectionDetector`
  横切：导入注入检测器协议（用于构造参数类型）。

- **第 31 行** `from compose.tool_layer import layer_program`
  为 `build()` 的外壳 Program 提供工厂。

- **第 32 行** `from compose import PortCtx`
  Port 上下文类型（ingest/retrieve 签名履约用）。

- **第 33 行** `from runtime import Program`
  Program 返回类型。

- **第 35 行** `__all__ = ["PIPELINE_NAME", "CachePort", "InMemoryCache", "RAGPipeline"]`
  导出流水线名、缓存协议、默认缓存、流水线类。

### 第 37-40 行：常量与别名

- **第 37 行** `PIPELINE_NAME: str = "rag_pipeline"`
  流水线名，也是 `RAGPipeline.name` 的值（装配期留名校验）。

- **第 39 行** `# 动态值别名：RAGPort 契约要求 DynamicValue（=Any），此处集中定义一处。`
  纪律注释。

- **第 40 行** `type DynamicValue = Any`
  动态值别名。为什么 `Any`：RAGPort 边界的外部值类型由框架决定，用 `Any` 表示动态边界，管道内部用 `isinstance`/`model_validate` 收敛为具体 DTO（见 ingest 第 164-168 行）。

### 第 43-66 行：CachePort 协议

- **第 43 行** `@runtime_checkable` 运行时可检查。

- **第 44-48 行** `class CachePort(Protocol):` + docstring：
  - 第 45 行：按字符串键存取决结果，可注入替换。
  - 第 47 行：**get/set/invalidate 全为同步（内存操作，无 IO）**——这是 `invalidate_cache` 里「循环期间无 await 不会被任务切换打断」的理论依据（第 291-294 行）。
  - 第 47 行：失败返回 None 视为未命中。

- **第 50 行** `name: str` 具名属性。

- **第 52-54 行** `def get(self, key: str) -> RAGResult | None:`
  按键取缓存，未命中返回 None。同步方法。

- **第 56-58 行** `def set(self, key: str, value: RAGResult) -> None:`
  写缓存。同步方法。

- **第 60-66 行** `def invalidate(self, key: str | None = None) -> None:`
  失效单个键；None 表示清空全部。
  - 第 63-64 行 docstring 关键：**invalidate(key) 对不存在 key 幂等**（默认实现用 pop 容错）；调用方 RAGPipeline **仅依赖单键失效，不依赖 None 全清**——避免共享缓存被误清空。
  - 为什么这条重要：若 pipeline 用 `invalidate()`（无参）全清，共享同一个 CachePort 的其他实例缓存会被误删。精准单键失效避免这个问题（S4）。

### 第 69-91 行：InMemoryCache 默认缓存

- **第 69 行** `class InMemoryCache:`
  默认精确缓存实现：进程内 dict，满足 CachePort 协议（结构化类型，无需显式继承）。

- **第 70 行** `"""默认精确缓存实现：进程内 dict，满足 CachePort 协议。"""` 定位。

- **第 72 行** `name: str = "rag_in_memory_cache"` 具名标识。

- **第 74 行** `def __init__(self) -> None:` 构造。

- **第 75 行** `"""初始化空缓存。"""` 说明。

- **第 76 行** `self._data: dict[str, RAGResult] = {}`
  进程内 dict 存储。

- **第 78-80 行** `get`：`return self._data.get(key)`
  `.get` 未命中返回 None（幂等）。

- **第 82-84 行** `set`：`self._data[key] = value` 直接写。

- **第 86-91 行** `invalidate`：
  - **第 88-89 行** `if key is None: self._data.clear()`：无参清空全部。
  - **第 90-91 行** `else: self._data.pop(key, None)`：**单键失效用 `pop(key, None)`——对不存在 key 幂等**（兑现 CachePort 契约）。
    - 为什么不 `del self._data[key]`：key 不存在时 `del` 抛 `KeyError`；`pop(key, None)` 容错，符合幂等承诺。

### 第 94-149 行：RAGPipeline 类与构造

- **第 94 行** `class RAGPipeline:`
  检索增强流水线，履约 RAGPort。

- **第 95-102 行** docstring：
  - 第 97-101 行 属性说明：五段主协议（公开以便替换断言）、三个增强注入点、`_cache_keys`（本实例写入缓存的 key 账本，ingest 后按 key 精准失效，S4）。

- **第 104 行** `name: str = PIPELINE_NAME` 具名标识 = "rag_pipeline"。

- **第 106-118 行** 构造函数签名：
  - **第 108-112 行** 五个必填位置参数：`splitter`/`embedder`/`store`/`retriever`/`post`。
  - **第 113 行** `*` 之后四个增强可选关键字参数：`rewrite`/`rerank`/`cache`/`detector`。
  - **第 116 行** `cache: CachePort | bool | None = None`：三种形态。
  - 为什么五段必填、三个增强可选：五段是链路骨架（缺一不可），增强点是锦上添花（可缺席降级）。

- **第 119-131 行** 构造函数 docstring：
  - 第 123 行：`embedder` 入库与查询共用，保证同一语义空间（B1）。
  - 第 129 行：cache 三形态说明（实例/True/False或None）。
  - 第 130 行：detector None 时由 post 自带检测器兜底。

- **第 132-136 行** 保存五段：
  `self.splitter`/`embedder`/`store`/`retriever`/`post` = 对应参数。公开属性（无下划线）便于外部替换断言。

- **第 137-139 行** 保存增强点：`self.rewrite`/`rerank`/`detector`。

- **第 140-146 行** cache 三态归一（S5：注入对象真正生效）：
  - **第 141-142 行** `if cache is True: self._cache = InMemoryCache()`：`True` → 内置缓存。
  - **第 143-144 行** `elif cache is False or cache is None: self._cache = None`：`False`/`None` → 关闭。
  - **第 145-146 行** `else: self._cache = cache`：实例直接用。
  - **为什么归一**：后续代码只处理 `CachePort | None` 两种形态，无需到处判断 bool。S5 指注入的实例真正被使用，而非被 bool 分支吞掉。

- **第 147-149 行** `self._cache_keys: set[str] = set()`
  **key 账本**：记录本实例写入缓存的 key。
  - 第 147-148 行注释：ingest 后按 key 精准失效而非全表清空，共享 CachePort 时不会误删其他实例条目（FR-1）。
  - 为什么用 set：key 唯一、频繁做成员判断与差集。

### 第 151-177 行：ingest 入库链

- **第 151 行** `async def ingest(self, documents: list[DynamicValue], ctx: PortCtx) -> int:`
  RAGPort 入口，返回成功入库块数。

- **第 152-160 行** docstring：第 159 行强调入库成功后按 key 失效缓存（S4：新文档对已缓存 query 可见）。

- **第 161 行** `del ctx`
  **显式删除 ctx 引用**。为什么：签名履约 RAGPort（必须有 ctx 参数），但当前不消费；`del ctx` 明确告诉读者与 linter「故意不用」，避免「未使用参数」警告，也防止误用。

- **第 162 行** `chunks: list[Chunk] = []` 收集全部块。

- **第 163 行** `for document in documents:` 逐个处理文档。

- **第 164-168 行** DTO 归一（鸭子兼容）：
  - `document if isinstance(document, Document) else Document.model_validate(document)`
  - 做什么：已是 Document 直接用，否则按 pydantic 校验转换。
  - 为什么：RAGPort 边界是 DynamicValue，上游可能传 dict 或鸭子对象；`model_validate` 在边界处收敛为严格 DTO。
  - 配合 types.py 第 45 行的 `extra="forbid"`：拼错字段会在 `model_validate` 时报错。

- **第 169 行** `chunks.extend(self.splitter.split(doc))`
  切分并收集（用 extend 展开多块）。

- **第 170-171 行** `if not chunks: return 0`
  无块（如所有文档为空）→ 返回 0，不向量化、不入库、不失效缓存。

- **第 172 行** `vectors = await self.embedder.embed([c.text for c in chunks])`
  **批量向量化**：一次性把所有块文本交给 embedder。
  - 为什么批量：真实后端批量调用比逐条高效；且与查询侧用同一个 `self.embedder`（B1）。

- **第 173-174 行** `for chunk, vec in zip(chunks, vectors): chunk.vector = vec`
  逐块写回向量（按位置配对）。
  - 为什么用 `zip`：embed 返回顺序与输入顺序一致，按位置对应。

- **第 175 行** `await self.store.upsert(chunks)`
  写入库。

- **第 176 行** `await self.invalidate_cache()`
  **入库后失效缓存**（S4）：让新文档对已缓存 query 可见。
  - 为什么不立刻重算缓存：失效比重算廉价；下次查询自然重新计算并缓存。

- **第 177 行** `return len(chunks)` 返回入库块数。

### 第 179-201 行：retrieve Port 入口

- **第 179 行** `async def retrieve(self, query: str, ctx: PortCtx) -> list[DynamicValue]:`
  RAGPort 入口：接收 str 查询。

- **第 180-188 行** docstring：第 187 行说明返回投影字段（citation/doc_id/text/score/meta）。

- **第 189 行** `budget = self._budget_from_ctx(ctx)`
  从 ctx 派生预算（见 `_budget_from_ctx`）。

- **第 190 行** `q = Query(text=query, token_budget=budget)`
  造 Query（只填 text 与 token_budget，其余用默认）。

- **第 191 行** `result = await self.query(q)`
  委托富入口 query，拿结构化结果。

- **第 192-201 行** 引用投影：
  - 把 `result.hits` 投影为 dict 列表，每项含：
    - 第 194 行 `citation`：chunk_id。
    - 第 195 行 `doc_id`。
    - 第 196 行 `text`。
    - 第 197 行 `score`。
    - 第 198 行 `meta`：`dict(h.chunk.meta)` 转普通 dict（DynamicValue 边界要求）。
  - 为什么投影：RAGPort 契约要求返回 DynamicValue（dict），不适合直接返回 pydantic 模型；投影成 dict 供框架装配层消费。

### 第 203-241 行：query 富入口（完整检索链）

- **第 203 行** `async def query(self, q: Query) -> RAGResult:`
  富入口，返回结构化结果。

- **第 204-209 行** docstring：
  - 第 206-207 行：流程（改写 → 用注入 embedder 预算向量 → 召回 → 重排 → 后处理）。
  - 第 208 行：跨改写 query 的命中按 chunk_id 去重（S7）；缓存命中直接返回。

- **第 210 行** `cache_key = self._cache_key(q)`
  算缓存键（含全部检索参数，见 `_cache_key`）。

- **第 211-214 行** 缓存读：
  - `if self._cache is not None: cached = self._cache.get(cache_key); if cached is not None: return cached`
  - 命中缓存直接返回，跳过整条检索链。
  - 为什么用 `is not None` 判命中：缓存值只可能是 RAGResult 或 None，None 明确表示未命中。

- **第 216 行** `queries = [q]` 默认单查询（原串）。

- **第 217-220 行** 改写：
  - `if self.rewrite is not None: rewritten = await self.rewrite.rewrite(q); if rewritten: queries = list(rewritten)`
  - 有改写器则替换 queries。
  - **为什么 `if rewritten` 守卫**：改写器理论上可能返回空列表（异常实现），此时保留原串，避免 `queries=[]` 导致整条链无输入。
  - `list(...)` 转列表，防改写器返回其他可迭代对象。

- **第 222-223 行** `vecs = await self.embedder.embed([sub.text for sub in queries])`
  **B1 核心行**：用 `self.embedder`（与 ingest 第 172 行同一个）批量算所有子 query 的向量。
  - 为什么必须与入库同源：入库时块向量由这个 embedder 算，查询向量也必须由它算，才能落在同一语义空间。若换成另一个 embedder（或召回器自己算），两空间不一致，点积无意义，检索失效。
  - 批量一次算：所有改写变体一起 embed，高效。

- **第 224 行** `hits: list[Hit] = []` 汇总命中容器。

- **第 225-226 行** `for sub, vec in zip(queries, vecs): hits.extend(await self.retriever.retrieve(sub, vec))`
  每个子 query 用对应向量召回，汇总命中。
  - 注意传入的是 `sub`（改写后的 Query）与 `vec`（对应向量），召回器不自行向量化（B1）。

- **第 227 行** `hits = self._dedup(hits)`
  **S7 去重**：多 query 改写会产生重复命中，按 chunk_id 去重保最高分（见 `_dedup`）。

- **第 228-229 行** `if self.rerank is not None: hits = self.rerank.rerank(hits, q)`
  有重排器则重排。
  - 注意用**原查询 `q`** 而非改写后的 sub 重排：重排针对原始意图。

- **第 231-236 行** 后处理装配：
  - `result = await self.post.assemble(hits, TokenBudget(limit=q.token_budget), detector=self.detector, filters=q.filters)`
  - 第 233 行：把裸 `q.token_budget`（int|None）重新包成 `TokenBudget`（具名类型，语义清晰）。
  - 第 234 行：`detector=self.detector` 透传流水线级检测器（B3：让 `RAGPipeline(detector=...)` 生效，覆盖 post 默认）。
  - 第 235 行：`filters=q.filters` 透传过滤条件（B2：兜底过滤）。

- **第 237-240 行** 缓存写 + 登记：
  - `if self._cache is not None: self._cache.set(cache_key, result); self._cache_keys.add(cache_key)`
  - 第 240 行：**登记 key 到账本**（S4/FR-2），供 ingest 后逐 key 失效。

- **第 241 行** `return result` 返回结果。

### 第 243-249 行：build 外壳

- **第 243 行** `def build(self) -> Program:` 产 Program 工厂。

- **第 244-248 行** docstring：ToolLayer 外壳（规格 P7），产出「用 RAG」的 Program 工厂；九注入点内部不改；外壳给 R1 领域无知的 Program，供试点验证（tests/r12/test_r6_pilot.py）。
  - 注意 docstring 说「九注入点」而 allowed 集合是 9 个（第 256-259 行：splitter/embedder/store/retriever/post/rewrite/rerank/cache/detector）——与 with_ 的允许替换点一致。

- **第 249 行** `return layer_program("rag", payload={"layer": "rag"})()`
  构造外壳 Program。不碰内部 query 链，只为满足「R1 只认 Program」的试点约束。

### 第 251-281 行：with_ 不可变替换（FR-5 账本归属）

- **第 251 行** `def with_(self, **replacements: object) -> RAGPipeline:`
  不可变替换：返回新实例，原实例不变（S1）。

- **第 252-255 行** docstring：第 254 行说「全部 8 个注入点」——注意这与实际 9 个允许点略有出入（docstring 笔误，代码 allowed 是 9 个：第 256-259 行）。以代码为准。

- **第 256-259 行** `allowed = {...}`
  9 个允许替换点：splitter/embedder/store/retriever/post/rewrite/rerank/cache/detector。

- **第 260 行** `unknown = set(replacements) - allowed`
  算出未知替换点（差集）。

- **第 261-262 行** `if unknown: raise ValueError(f"Unknown replacement points: {sorted(unknown)}")`
  **未知替换点直接报错**。为什么不静默忽略：拼错替换点名（如 `embeder`）若被忽略，用户以为换了实际没换，隐蔽 bug。

- **第 263 行** 注释：cache 归一，账本归属见下方 FR-5。

- **第 264-274 行** `current` 字典：收集当前全部 9 个注入点的实例值。
  - 第 272 行 `"cache": self._cache`：注意放的是**归一后的 `_cache`**（CachePort|None），不是原始 `cache` 参数——这样传给构造函数走 else 分支直接用实例。

- **第 275 行** `current.update(replacements)`
  用替换值覆盖。

- **第 276 行** `new = type(self)(**current)`（带 type ignore）
  用当前类构造新实例（而非硬编码 `RAGPipeline`，便于子类）。`# type: ignore[arg-type]` 因 current 值是 object，类型检查器无法确认签名匹配。

- **第 277-278 行** 注释：S4 账本归属（FR-5）——cache 实例未变（含未传）→ 共享账本；cache 被换 → 新实例空账本。

- **第 279-280 行** `if new._cache is self._cache: new._cache_keys = self._cache_keys`
  **FR-5 关键**：
  - 做什么：若新旧实例的 cache 是**同一个对象**（用 `is` 判同一性），则让新实例共享旧实例的账本对象。
  - **为什么要共享账本**：若换掉其他注入点但 cache 没变，新实例与旧实例共用同一个缓存。此时新实例 ingest 必须能失效旧实例写入的 key——共享账本才能保证「账本里有所有 key」。否则新实例账本为空，ingest 失效不到旧 key，旧缓存脏数据继续被命中（破 S4）。
  - **为什么 cache 被换时用空账本**：cache 换了意味着新缓存里没有旧实例的条目，新实例也不该去失效旧缓存（那不是它的缓存）——构造时 `self._cache_keys = set()` 已是空账本，无需额外处理。
  - 不这么写（总是新空账本）：with_ 换非 cache 注入点后，共享缓存的失效链断裂，是隐蔽的缓存一致性 bug。

- **第 281 行** `return new` 返回新实例。

### 第 283-298 行：invalidate_cache 精准失效

- **第 283 行** `async def invalidate_cache(self) -> None:` 按 key 失效。

- **第 284-288 行** docstring：
  - 第 284 行：不无参全表清空。
  - 第 286-287 行 FR-3：对账本中每个 key 调 `invalidate(key)` 后清空账本；默认内存与自定义共享缓存走同一路径，避免误删他人条目。

- **第 289-290 行** `if self._cache is None or not self._cache_keys: return`
  无缓存或账本空 → 直接返回。

- **第 291-294 行** 注释（重要）：
  - 先固定本次失效集合：CachePort 契约是同步、本方法内无 await，asyncio 单线程下循环期间不会被任务切换插入登记，`snapshot == 当前账本`。
  - **用 `difference_update` 而非 `clear`**：仅移除已失效的 key；若未来换成可挂起/跨线程实现，循环期间新登记的 key 会保留到下次 ingest，不会「有缓存无账本」泄漏。
  - 这段解释了为什么用 snapshot + difference_update 而非直接遍历 + clear。

- **第 295 行** `snapshot = tuple(self._cache_keys)`
  固定账本快照（转 tuple 防止遍历期间集合被改导致 `RuntimeError`）。

- **第 296-297 行** `for key in snapshot: self._cache.invalidate(key)`
  逐 key 精准失效（单键失效，不用 None 全清）。

- **第 298 行** `self._cache_keys.difference_update(snapshot)`
  从账本移除本次失效的 key（差集更新，保留循环期间可能新登记的 key）。

### 第 300-304 行：_cache_key 缓存键

- **第 300 行** `@staticmethod` 静态方法。

- **第 301 行** `def _cache_key(q: Query) -> str:` 生成缓存键。

- **第 302 行** `"""缓存键纳入全部影响检索的参数（S4）：文本/top_k/预算/用途/过滤条件。"""`
  点明设计：键要包含所有影响结果的参数。
  - 为什么：若漏掉某参数（如 filters），不同过滤条件的查询会命中同一缓存，返回错误结果。

- **第 303 行** `filters_repr = repr(sorted((q.filters or {}).items()))`
  - `q.filters or {}`：None 视为空 dict。
  - `.items()` 取键值对，`sorted` 排序（dict 顺序无关，排序保证确定性——同样内容不同插入序得到相同键）。
  - `repr` 转成字符串表示。
  - **为什么要排序 + repr**：filters 是 dict，直接 `str(dict)` 受插入顺序影响；排序后相同语义的 filters 得到相同键，避免缓存碎片。

- **第 304 行** `return f"{q.text}|{q.top_k}|{q.token_budget}|{q.purpose.value}|{filters_repr}"`
  用 `|` 拼接五个参数：文本、top_k、预算、用途（`.value` 取枚举字符串值）、filters。
  - 为什么用 `.value`：`purpose` 是枚举，取字符串值保证键稳定可读。

### 第 306-314 行：_dedup 去重

- **第 306 行** `@staticmethod` 静态方法。

- **第 307 行** `def _dedup(hits: list[Hit]) -> list[Hit]:` 去重。

- **第 308 行** `"""按 chunk_id 去重保留最高分（S7：多 query 改写不产生重复引用）。"""`
  点明 S7。

- **第 309 行** `best: dict[str, Hit] = {}`
  按 chunk_id 存最高分命中。

- **第 310 行** `for hit in hits:` 遍历。

- **第 311 行** `cid = hit.chunk.chunk_id` 取块 id。

- **第 312-313 行** `if cid not in best or hit.score > best[cid].score: best[cid] = hit`
  **保留最高分**：新块或未见过则记录；已见过且当前分更高则替换。
  - 为什么要保最高分：同一块被多个改写 query 命中时，分数可能不同；取最高分代表最佳相关度。

- **第 314 行** `return sorted(best.values(), key=lambda h: h.score, reverse=True)`
  按分数降序返回去重后的命中。
  - 为什么排序：去重后顺序被 dict 打乱，恢复按分数降序，供后续 rerank/post 消费。

### 第 316-338 行：_budget_from_ctx 预算派生（S3）

- **第 316 行** `@staticmethod` 静态方法。

- **第 317 行** `def _budget_from_ctx(ctx: PortCtx) -> int | None:` 从 ctx 派生预算。

- **第 318-324 行** docstring：逐条说明四种来源：
  - None → 不限（返回 None）。
  - int → 余量（≥0）；**0 表示耗尽，返回 0（后处理转空结果，绝不当不限）**。
  - R1 Budgets 投影（含 max_tokens/used_tokens）→ 计算余量。
  - 无法识别 → None（保守放行，由装配层保证传入正确投影）。

- **第 325 行** `raw = ctx.budget_left` 取出原始预算值。

- **第 326-327 行** `if raw is None: return None`
  None → 不限。

- **第 328-329 行** `if isinstance(raw, int): return max(0, raw)`
  int → 余量，`max(0, ...)` 保证非负。
  - **关键**：0 原样返回 0（不是不限），负值钳到 0。这与 TokenBudget「0 无预算」语义一致。
  - 为什么 `isinstance(raw, int)` 要注意 bool：`bool` 是 `int` 子类，`True` 会被当 1——但预算字段一般不会是 bool，当前实现接受这个宽松。

- **第 330 行** `max_tokens = getattr(raw, "max_tokens", None)`
  鸭子取用 `max_tokens` 属性（R1 Budgets 投影）。

- **第 331 行** `used_tokens = getattr(raw, "used_tokens", None)`
  鸭子取用 `used_tokens` 属性。

- **第 332-333 行** `if isinstance(max_tokens, int) and isinstance(used_tokens, int): return max(0, max_tokens - used_tokens)`
  两者都是 int → 返回余量（最大 - 已用），钳非负。
  - 为什么先判这一种投影：这是 R1 Budgets 的标准形态，优先精确计算。

- **第 334 行** `for attr in ("tokens", "token_budget", "total_tokens"):`
  尝试其他可能的预算属性名（兼容不同投影形态）。

- **第 335-337 行** `value = getattr(raw, attr, None); if isinstance(value, int): return max(0, value)`
  取第一个是 int 的属性值作预算，钳非负。
  - 为什么多个候选名：框架的预算投影可能有多种字段名，逐个兼容，提高装配宽容度。

- **第 338 行** `return None`
  都不识别 → None（不限）。
  - 为什么是保守放行 None：无法理解预算结构时，选择「不限」而非「0」，避免因解析失败导致所有查询空结果；正确性由装配层保证传入正确投影（docstring 第 323 行声明）。

---

## 10. __init__.py 逐行讲解（42 行）

包级 API 收敛：把各段的协议与默认实现集中导出，外部只需 `from harness.rag import ...`。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R6 检索增强包：可分层评测的 RAG 流水线（五段 + 三增强 + 横切）。`
  一句话定位：五段 + 三增强 + 横切。

- **第 2-4 行** 说明：公共 API 与各段协议从本包导出，装配层（R12）按 `RAGPipeline(...)` 构造并以 `PORT_PROTOCOLS["rag"]` 校验履约。
  - 指路：装配层怎么用本包。

- **第 5 行** `"""` docstring 结束。

### 第 6 行：future import

- **第 6 行** `from __future__ import annotations` 延迟注解。

### 第 8-25 行：重导出 imports

- **第 8 行** `from harness.rag.embedder import Embedder, EmbeddingClient, HashEmbedder`
  向量化段：两层协议 + 默认实现。

- **第 9 行** `from harness.rag.pipeline import CachePort, InMemoryCache, RAGPipeline`
  流水线 + 缓存。

- **第 10 行** `from harness.rag.post import DefaultPostProcessor, PostProcessor`
  后处理段。

- **第 11 行** `from harness.rag.retriever import HybridRetriever, Retriever, VectorRetriever`
  召回段：协议 + 两个实现。

- **第 12 行** `from harness.rag.rewrite import MultiQueryRewriter, QueryRewriter`
  改写段。

- **第 13 行** `from harness.rag.rerank import LexicalReranker, Reranker`
  重排段。

- **第 14 行** `from harness.rag.splitter import RecursiveSplitter, Splitter`
  切分段。

- **第 15 行** `from harness.rag.store import InMemoryVectorStore, VectorStore`
  存储段。

- **第 16-25 行** `from harness.rag.types import (...)`：导入 8 个 DTO（Chunk/Document/Hit/Query/QueryPurpose/RAGResult/TokenBudget/Vector）。
  - **包级 API 收敛纪律**：外部装配层只需知道 `harness.rag` 这个包，不必知道内部文件路径（如 `harness.rag.pipeline`）。这样内部重构文件结构不破坏外部导入。

### 第 27-42 行：__all__ 公共导出面

- **第 27 行** `__all__ = [` 开始导出清单。

- **第 28-29 行** `# 流水线` + `"RAGPipeline", "CachePort", "InMemoryCache"`：流水线三件套。

- **第 30-32 行** `# 核心 DTO` + 8 个 DTO。

- **第 33-38 行** `# 五段协议与默认实现`：
  - 第 34 行 Splitter / RecursiveSplitter。
  - 第 35 行 Embedder / EmbeddingClient / HashEmbedder。
  - 第 36 行 VectorStore / InMemoryVectorStore。
  - 第 37 行 Retriever / VectorRetriever / HybridRetriever。
  - 第 38 行 PostProcessor / DefaultPostProcessor。

- **第 39-41 行** `# 增强协议与默认实现`：
  - 第 40 行 QueryRewriter / MultiQueryRewriter。
  - 第 41 行 Reranker / LexicalReranker。

- **第 42 行** `]` 结束。

  **注意**：`__all__` 不导出 `RRF_K`（types 模块有导出，但包级没有）。若需 RRF_K 要走 `from harness.rag.types import RRF_K`。这是公共面的有意收窄——常量属内部实现细节，一般外部不需要。

---

## 11. 逐行讲解后的整体心智图

```
入库（ingest）：
  Document（鸭子兼容 model_validate）
    → splitter.split          递归切分（边界优先 + overlap 滑窗）
    → embedder.embed[批量]     ←── 同一个 embedder（B1）
    → chunk.vector = vec
    → store.upsert            追加块引用（对象引用，非深拷贝）
    → invalidate_cache        按账本逐 key 精准失效（S4）

检索（query）：
  cache_key（text|top_k|budget|purpose|filters_repr）
    → 命中缓存? → 直接返回
  queries = [q] → rewrite（两轮组装：原串+同义+子句，保连续子串 D2）
    → embedder.embed[批量]     ←── 同一个 embedder（B1，与入库同源）
  for (sub, vec): retriever.retrieve(sub, vec)
    ├─ VectorRetriever: store.search(vec, top_k, filters)
    └─ HybridRetriever:
         ├─ 向量通道: store.search（filters）
         ├─ 关键词通道: _keyword_search（matches_filters，B2）
         └─ _rrf 融合: Σ weight/(60+rank)，按 chunk_id 去重
  _dedup（按 chunk_id 保最高分，S7）
    → rerank（词面覆盖度，取 top_n）
    → post.assemble:
         ├─ limit==0 → empty（0 是无预算不是不限）
         ├─ filter（matches_filters + 注入 fail-closed，B2/B3）
         ├─ _reorder_middle（高相关放两端，抗 lost-in-the-middle）
         ├─ 贪心装入（装不下就 continue 跳过，非 break 整段丢）
         └─ 拼文本 + [cid] 引用 → RAGResult
    → 写缓存 + 登记 key 到账本

with_ 不可变替换（FR-5）：
  cache 同实例 → 共享账本；cache 被换 → 空账本
```

---

## 12. 细节速记卡（逐行精华）

**一致性铁律**
- **同源 embedder 是铁律（B1）**：查询向量必须由 pipeline 注入的 embedder 算，与入库同一实例同一空间；召回器绝不自行向量化（pipeline.py 第 223 行 / retriever.py 第 34 行）。
- **HashEmbedder 用内置 hash()**：与 model/cache 里确定性 31 进制哈希不同，两空间不能混用（embedder.py 第 80 行）——否则同词落不同维度，点积失真。
- **关键词通道共用 filters（B2）**：retriever 关键词通道与向量通道都走 `matches_filters`（retriever.py 第 108 行）+ post 统一兜底过滤（post.py 第 89 行），不绕过权限/时效。
- **无关键词素材显式抛错（S2）**：`_corpus` 两来源皆无则 raise ValueError，不静默退化为纯向量（retriever.py 第 96 行）。

**召回与融合**
- **RRF_K=60，rank 从 1 起**：`weight/(60+rank)`，按名次融合不同量纲通道并去重（retriever.py 第 125-127 行）。
- **RRF 按 chunk_id 累加去重**：同名块多通道命中分数叠加（retriever.py 第 128-132 行）。
- **store 存引用不深拷贝**：入库后别改 Chunk；重复 ingest 不按 id 去重（store.py 第 69 行）。
- **_dot 用 zip 不校验维度**：长度不等静默截断——再次印证同源 embedder 必要（store.py 第 98 行）。

**后处理与预算**
- **TokenBudget limit=0 是「无预算」不是「不限」**：assemble 第 117 行直接返回 empty；`None` 才是不限。
- **预算装入是贪心跳过**：装不下当前项 `continue` 跳，继续试更小的，不是 `break` 整段丢（post.py 第 130-131 行）。
- **乱序修复抗 lost-in-the-middle**：偶数下标放左端、奇数放右端，`left + reversed(right)`，高相关在两端（post.py 第 161 行）。
- **注入 fail-closed**：命中 INJECTED 剔除；检测器缺省也有默认值，无「缺席放行」（post.py 第 66-68、92 行）。
- **filter 是融合后统一兜底**：不依赖各召回通道各自正确（post.py 第 84 行）。
- **空结果固定提示**：`EMPTY_CONTEXT` 引导模型谨慎/拒答，不返回空串（post.py 第 17 行）。
- **引用标注 `[{cid}]`**：context 里块文本后接引用，citations 是 chunk_id 列表（post.py 第 20、140 行）。

**改写纪律**
- **改写保证连续子串（D2）**：`_split_subqueries` 迭代切片，strip 后仍是原串连续子串，不引入新实体（rewrite.py 第 75 行）。
- **改写两轮组装防子句挤出同义（B1）**：第一轮「原串+1同义+全子句」保 N=3 有代表，第二轮余量增益；子句先于子句同义防饿死（rewrite.py 第 170-179 行）。
- **同义词只替首个出现**：`replace(key, value, 1)` 避免组合爆炸（rewrite.py 第 118 行）。
- **切分符按长度降序**：「以及」在「及」前，`" and "` 带空格（rewrite.py 第 20-23 行）。
- **变体只改 text**：`model_copy(deep=True, update={"text":...})` 继承其余字段，深拷贝防共享 filters（rewrite.py 第 191 行）。

**缓存与失效**
- **缓存键含全部检索参数**：`text|top_k|token_budget|purpose|filters_repr`，filters 排序去重保确定性（pipeline.py 第 303-304 行）。
- **失效按 key 精准删**：逐 key `invalidate(key)`，不用 None 全清，共享缓存不误删他人（pipeline.py 第 296-297 行）。
- **difference_update 而非 clear**：仅移除已失效 key，防「有缓存无账本」泄漏（pipeline.py 第 298 行）。
- **with_ 换 cache 才换账本**：同 cache 实例用 `is` 判，共享账本；换 cache 用空账本（FR-5，pipeline.py 第 279-280 行）。
- **cache 三态归一**：True→内置、False/None→关、实例→用（pipeline.py 第 141-146 行）。
- **CachePort 全同步**：invalidate 幂等，pipeline 只依赖单键失效（pipeline.py 第 47、63-64 行）。

**入口与装配**
- **双轨入口**：ingest/retrieve 对齐 RAGPort（DynamicValue），query 富入口返回 RAGResult（pipeline.py 第 1-5 行）。
- **五段必填 + 三增强可选**：splitter/embedder/store/retriever/post 必填；rewrite/rerank/detector 可缺席（pipeline.py 第 106-118 行）。
- **未知替换点抛 ValueError**：with_ 拼错点名立即暴露（pipeline.py 第 261-262 行）。
- **DTO 全部 extra=forbid**：拼错字段直接报错不静默（types.py 第 45 行）。
- **TokenBudget.__int__ 的 None→-1**：-1 作「不限」哨兵（types.py 第 127 行）。
- **预算派生 S3**：ctx.budget_left 四种来源，int 原样保 0（耗尽≠不限），无法识别回 None（pipeline.py 第 325-338 行）。
- **chunk_id = doc_id:序号**：引用溯源基础（splitter.py 第 62 行）。
- **overlap 保证跨块连续**：`step = chunk_size - overlap`（splitter.py 第 82 行）。
- **all_chunks 鸭子取用 + cast 还原类型**：getattr 检查 callable，cast 显式还原 list[Chunk]（retriever.py 第 89-95 行）。
- **包级 API 收敛**：外部只认 `harness.rag`，不依赖内部文件路径（__init__.py 第 8-25 行）。

---

*这是逐行讲解版的 rag 册。格式与 [05 skills 技能模块](./05-skills技能模块.md) 一致：「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」。*
