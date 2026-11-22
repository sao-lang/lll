# 代码精读 07 · memory 模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/memory/` 全部 4 个文件：`types.py`（240 行）+ `store.py`（31 行）+ `memory.py`（603 行）+ `__init__.py`（51 行）。
> 总入口见 [00](./00-总入口与全局链路.md)，上层编排见 [01 context](./01-context上下文模块.md)，注入检测器见 [05 skills](./05-skills技能模块.md)。

> 注：行号以源码实际为准。本册覆盖整个 memory 模块（含数据模型、存储实现、MemoryStore 与包级导出）。

---

## 0. 读前GPS

### 0.1 这个模块在全局的位置

memory 模块是 R7「上下文与记忆」域的**长期记忆默认实现**。它对外提供三类能力：

- **R2 `MemoryPort` 履约**：`read` / `write` / `forget` 三个简单 KV 方法（键值层）。
- **R2 `StorePort` 履约**：`InMemoryStorePort` 的 `save` / `load` 会话快照（另一个文件 `store.py`）。
- **R7 自有语义召回**：`ingest`（写入链）+ `recall`（召回链）+ 一整套分层治理方法。

它同时**横切复用**两个兄弟域：R6 的 `Embedder` / `VectorStore`（向量化与向量库），R5 的 `InjectionDetector`（注入检测）。

### 0.2 四个文件各管什么

| 文件 | 行数 | 角色 | 一句话 |
|---|---|---|---|
| `types.py` | 241 | 数据模型层 | 双轴七类 DTO + 全部阈值常量 + `ScopeResolver` + `make_item_id` + `is_secret_key` |
| `store.py` | 32 | StorePort 默认实现 | 会话快照的内存存储，深拷贝隔离 |
| `memory.py` | 604 | 核心实现 | `MemoryStore` 全部方法：两条链 + 治理 + 安全扫描 |
| `__init__.py` | 52 | 包级门面 | 统一重导出公共 API |

### 0.3 读之前先记住三个心智锚点

1. **双轴正交**：温度轴 `tier`（hot/warm/cold）× 内容轴 `kind`（七类）。两者独立，`promote`/`demote` 只动 tier 不动 kind。
2. **两条链**：写入链 `ingest` 十步，召回链 `recall` 十步。这是 `memory.py` 的主干。
3. **一道闸**：所有写入与召回输出都过递归安全扫描，命中 secret 键名或注入串即拒绝（fail-closed）。

### 0.4 核心类速览

- `MemoryTier` / `MemoryKind`：两个 `StrEnum`，双轴的两个维度。
- `MemoryScope` / `MemoryItem` / `MemoryQuery`：三个 frozen dataclass，隔离域 / 记忆项 / 查询。
- `ScopeResolver`：唯一的 tenant 入口，解析与校验隔离域。
- `MemoryStore`：核心类，`MemoryPort` 默认实现。
- `InMemoryStorePort`：`StorePort` 默认实现。

---

## 1. types.py 逐行讲解（240 行）

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 记忆核心 DTO：双轴分类（温度三层 × 内容七类）、记忆项、召回查询与隔离域。`
  一句话定位：本文件是**纯数据模型层**——只定义结构、常量、纯函数，不含任何存储逻辑。开篇即点明「双轴」。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `本模块只描述数据结构、具名常量与纯判定函数，不含存储逻辑。`
  强调职责边界：存储逻辑全在 `memory.py`，本文件保持「无状态、可独立测试」。

- **第 4 行** `secret / 注入检测在 MemoryStore 写入链执行；本模块的 is_secret_key 是第一道键名闸门。`
  说明安全职责分工：完整递归检测在 `MemoryStore._scan_value`，本文件只提供**键名闸门** `is_secret_key`。称它为「第一道闸」是因为 `write` 一进来就先查键名。

- **第 5 行** `"""` docstring 结束。

### 第 6-14 行：imports 与条件导入

- **第 6 行** `from __future__ import annotations`
  延迟注解求值：允许在类型注解里写尚未定义的类型（如第 134 行的 `TokenBudget`），降低 import 顺序耦合。这是全模块统一纪律。

- **第 7 行** 空行。

- **第 8 行** `from dataclasses import dataclass, field`
  - `dataclass`：三个 DTO 都用 `@dataclass(frozen=True, slots=True)`。
  - `field`：仅 `MemoryItem.scope` 用 `field(default_factory=...)`（第 113 行），因为默认值需要每次构造新对象。

- **第 9 行** `from enum import StrEnum`
  Python 3.11+ 的 `StrEnum`：成员既是枚举（有限取值、可比），又是字符串（可直接进 JSON / 日志 / f-string）。比旧版 `class X(str, Enum)` 更现代，且 `str(MemoryTier.HOT)` 直接得 `"hot"`。

- **第 10 行** 空行。

- **第 11 行** `from typing import TYPE_CHECKING`
  类型检查期常量：运行时为 `False`，静态检查器视为 `True`。用来避免循环导入。

- **第 12 行** 空行。

- **第 13 行** `if TYPE_CHECKING:`
  仅在静态检查时进入这个分支，运行时跳过。

- **第 14 行** `from harness.rag.types import TokenBudget`
  `MemoryQuery.budget` 的类型来自 R6。为什么放在 `TYPE_CHECKING` 里：若顶层 import，`memory` 依赖 `rag`，而 `rag` 某些模块可能反向依赖，会造成 import 成环。延迟到类型检查期即可拿到注解，运行时不真正导入。
  - 不这么写会怎样：顶层直接 `from harness.rag.types import TokenBudget` 可能在特定装配顺序下触发循环导入，导致模块加载失败。

### 第 16-35 行：__all__ 导出清单

- **第 16-35 行** `__all__ = [...]`
  共 20 个符号，分三类：
  - 常量（10 个）：`MMR_LAMBDA`、`NEAR_DUP_SIM_THRESHOLD`、`PREFERENCE_REPEAT_THRESHOLD`、`PROFILE_CONFIDENCE_THRESHOLD`、四个 `RERANK_W_*`、`SCOPE_META_KEY`、`SECRET_KEY_TOKENS`。
  - DTO / 枚举（5 个）：`MemoryItem`、`MemoryKind`、`MemoryQuery`、`MemoryScope`、`MemoryTier`。
  - 工具（3 个）：`ScopeResolver`、`is_secret_key`、`make_item_id`。

  `__all__` 有两个作用：一是 `from .types import *` 的显式白名单；二是声明本模块的公共面——`__init__.py` 据此重导出。注意 `_DEFAULT_TENANT`（第 155 行）**不在** `__all__`：它是内部细节，不对外承诺。

### 第 38-49 行：MemoryTier（温度轴）

- **第 38 行** `class MemoryTier(StrEnum):`
  温度轴枚举，三个物理/检索层。继承 `StrEnum` 而非 `Enum`：这样 `tier` 字段既能当枚举比对（`is`），又能直接序列化成字符串。

- **第 39-45 行** docstring：说明三层均**非会话内短期记忆**（短期归 R1），并列出成员含义：`HOT` 近期热层短 TTL 高频召回、`WARM` 结构化主检索层中 TTL、`COLD` 全文归档层长 TTL 低频审计。

- **第 47 行** `HOT = "hot"`
  热层。字符串值 `"hot"` 是落库与日志里的实际表示。

- **第 48 行** `WARM = "warm"`
  温层，也是 `MemoryItem.tier` 的默认值（第 111 行）。

- **第 49 行** `COLD = "cold"`
  冷层，归档用。

**为什么不直接存裸字符串**：枚举收敛了取值集合，拼错 `"hott"` 会立刻暴露；裸字符串会静默写入一个永远匹配不到的层。

### 第 52-71 行：MemoryKind（内容轴）

- **第 52 行** `class MemoryKind(StrEnum):`
  内容轴枚举，七个语义类型。七类是提炼阶段判定后稳定的分类。

- **第 53-63 行** docstring：逐类说明含义——`EPISODIC` 情节（发生过什么）、`SEMANTIC` 事实（知道什么）、`PROCEDURAL` 流程（怎么做）、`PROFILE` 用户画像（稳定身份属性）、`PREFERENCE` 偏好约束（风格/格式/禁忌）、`ENTITY` 实体知识（人/项目/工具/概念）、`JOURNAL` 工作日志与决策。

- **第 65 行** `EPISODIC = "episodic"` 情节类。
- **第 66 行** `SEMANTIC = "semantic"` 事实类，也是 `refine_kind` 的兜底类（第 320 行）。
- **第 67 行** `PROCEDURAL = "procedural"` 流程类，importance 复用度给满分（第 335 行）。
- **第 68 行** `PROFILE = "profile"` 画像类，需 `confidence >= 0.9` 才判（第 307 行）。
- **第 69 行** `PREFERENCE = "preference"` 偏好类，同样复用度满分。
- **第 70 行** `ENTITY = "entity"` 实体类，`refine_kind` 里优先于 procedural/journal（第 312 行）。
- **第 71 行** `JOURNAL = "journal"` 日志类。

**双轴正交的第一处体现**：`kind`（这 7 个）与 `tier`（上节 3 个）是两个独立维度。`MemoryItem` 同时持有两者（第 104、111 行），互不推导、互不约束。

### 第 74-84 行：MemoryScope（隔离域）

- **第 74 行** `@dataclass(frozen=True, slots=True)`
  两个关键字：
  - `frozen=True`：不可变。隔离域一旦构造不可改，防止写入后 tenant 被篡改导致隔离失效。
  - `slots=True`：不生成 `__dict__`，省内存、防动态加属性。DTO 类统一用这两个参数，是全模块纪律。

- **第 75 行** `class MemoryScope:`
  「谁的数据」——tenant 必填，user/session 可选。

- **第 76-80 行** docstring：说明由 R12 装配层经 `PortCtx.meta["scope"]` 注入（第 78 行），写入与召回双向校验；`tenant` 为空即 fail-closed（第 79 行），禁止静默全量可见。

- **第 82 行** `tenant: str`
  租户，必填。它是隔离的主键——`make_item_id`（第 185 行）与召回 tenant 过滤（第 167 行）都靠它。

- **第 83 行** `user: str | None = None`
  用户，可选。当前代码只在 DTO 层保留，过滤逻辑只用 tenant。

- **第 84 行** `session: str | None = None`
  会话，可选。同样保留字段，未参与当前过滤。

**为什么不把 tenant 设为可选**：若 `tenant: str | None = None`，忘记注入时会静默退化成「全租户可见」，是最危险的隔离漏洞。设为必填 + 空白校验（第 222 行）把问题提前暴露。

### 第 87-114 行：MemoryItem（记忆项）

- **第 87 行** `@dataclass(frozen=True, slots=True)`
  同样不可变 + slots。不可变在这里尤其重要：`record_access` 更新 importance 时必须**构造新对象**（第 439 行），不能原地改——这正是「调用方必须用返回值」的根源。

- **第 88 行** `class MemoryItem:`
  一条长期记忆条目。

- **第 89-102 行** docstring：字段清单逐条说明。第 94 行强调 importance「禁止恒定 1.0」（要参与 rerank 有区分度）；第 95 行强调 ts 用真实时间「禁止硬编码 0」（否则时效衰减全乱）；第 97 行说明 item_id 由 `make_item_id` 生成，KV 与向量索引同源。

- **第 104 行** `kind: MemoryKind`
  内容轴。**前五个字段（104-108）保留位置参数**——旧调用方按位置传 `MemoryItem(kind, content, importance, ts, source)` 仍可用，这是向后兼容的关键布局。

- **第 105 行** `content: str`
  正文。写入前须过递归注入检测且不含 secret（docstring 第 93 行）。

- **第 106 行** `importance: float`
  重要性 0~1，参与 rerank（权重 0.2，见 `types.py` 第 147 行 / `memory.py` 第 477 行）。

- **第 107 行** `ts: int`
  写入时刻（纳秒时间戳）。`_recency` 用它算半衰期衰减。

- **第 108 行** `source: str`
  来源标识，如 `cold:transcript` / `refined:<kind>`。以 `explicit` 开头的来源在 importance 中 weighting 满分（第 337 行）。

- **第 109 行** `item_id: str = ""`
  稳定标识，有默认值（属于**后五个默认字段**）。空串表示「未显式给出」，`put_item`（第 366 行）会按需补算。

- **第 110 行** `confidence: float = 1.0`
  置信度，默认满分。冲突裁决时「低置信不覆盖高置信」（第 260 行）。

- **第 111 行** `tier: MemoryTier = MemoryTier.WARM`
  温度轴，默认 WARM。与第 104 行的 kind 正交——这里给默认值只是便利，不代表 kind 决定 tier。

- **第 112 行** `# scope 缺省为 default 租户（非 None），确保跨 tenant 过滤不被 None 旁路。`
  关键设计注释。解释见下一行。

- **第 113 行** `scope: MemoryScope = field(default_factory=lambda: MemoryScope(tenant="default"))`
  **防 None 旁路的核心行**。scope 默认是 `tenant="default"` 的合法 Scope，而不是 `None`。
  - 为什么用 `default_factory` 而不是直接 `scope = MemoryScope(...)`：dataclass 不允许可变默认值，且每次构造应得独立对象；factory 保证这一点。
  - 为什么不默认 `None`：若默认 None，召回过滤 `item.scope is not None and item.scope.tenant == scope.tenant`（第 167 行）会把这些项**全部跳过**——看起来「安全」，但旧调用方造的 item 会莫名消失；更糟的是若某处写成 `item.scope is None or ...` 就会全量放行。给一个确定的 default tenant，让行为可预测、可测试。
  - 与 `resolve` 缺省 tenant 呼应：`ScopeResolver.resolve` 无 meta 时也返回 `tenant="default"`（第 210 行），两者同口径。

- **第 114 行** `access_count: int = 0`
  命中次数，驱动 importance 重算（第 437 行）。

### 第 117-134 行：MemoryQuery（查询 DTO）

- **第 117 行** `@dataclass(frozen=True, slots=True)`
  查询也不可变。

- **第 118 行** `class MemoryQuery:`
  第 119 行 docstring 明确：这是 **R7 自有语义召回查询 DTO，不复用 R6 Query**——因为 R7 多了 tier/kind/scope/budget 四个维度，复用会污染 R6。

- **第 120-127 行** docstring：字段说明。第 126 行强调 scope 的 tenant 必须等于 `PortCtx.meta` 的 tenant，否则拒绝（防 Query 侧绕过隔离）。

- **第 129 行** `text: str`
  查询文本，必填。

- **第 130 行** `top_k: int = 5`
  返回条数上限，默认 5。

- **第 131 行** `tier: MemoryTier | None = None`
  限定温度层，None 表示全层。

- **第 132 行** `kind: MemoryKind | None = None`
  限定内容类，None 表示全部。

- **第 133 行** `scope: MemoryScope | None = None`
  显式隔离域。None 表示「取 ctx 的」；非 None 时走 `ScopeResolver.check`（第 163 行）校验 tenant 一致。

- **第 134 行** `budget: TokenBudget | None = None  # 召回链预算裁剪（§12.4）：给定则按 token 贪心装入，缺省仅 top_k`
  **旧版文档遗漏的字段，源码真实存在**。给定 budget 则召回链按 token 贪心装入（第 187-193 行），缺省只按 top_k 截断。类型 `TokenBudget` 来自 R6，通过第 13-14 行的 `TYPE_CHECKING` 延迟导入。

### 第 137-155 行：全部具名常量

- **第 137 行** `# secret 键名特征（大小写不敏感子串匹配；键与值双向检测的第一道闸）。`
  注释说明威胁模型：键名里带这些词即疑似 secret。

- **第 138-140 行** `SECRET_KEY_TOKENS: tuple[str, ...] = (...)`
  7 个特征串：`secret` / `token` / `password` / `api_key` / `apikey` / `private_key` / `credential`。
  - 用 tuple 不用 list：不可变，防运行期被篡改。
  - 大小写不敏感匹配在第 168 行通过 `casefold()` 实现，所以表里全小写即可。

- **第 141 行** `# 冲突消解/分类阈值（具名常量，禁止裸魔法值）。`
  下面三个阈值都具名，禁止在逻辑里散落裸数字。

- **第 142 行** `NEAR_DUP_SIM_THRESHOLD: float = 0.92`
  近重复相似度阈值。写入链去重（第 354 行）用同一个值，语义统一。

- **第 143 行** `PREFERENCE_REPEAT_THRESHOLD: int = 3`
  「同类约束累计 3 次判偏好」阈值。这是曾被纠正的历史点之一：旧实现没引用它，本版通过 `pref_hits` 落实（第 305 行）。

- **第 144 行** `PROFILE_CONFIDENCE_THRESHOLD: float = 0.9`
  高置信画像推断阈值。`refine_kind` 判 PROFILE 要求 confidence ≥ 0.9（第 307 行）。

- **第 145 行** `# rerank 加权和权重（和为 1.0；连乘会把四因子 0.5 压到 0.06 失去区分度）。`
  **关键设计注释**：解释为什么用「加权和」而非「连乘」。若四因子连乘，0.5×0.2×0.15×0.15 ≈ 0.0023（注释说 0.06 是示意量级），分数被压扁到几乎无区分度；加权和保持各因子贡献可比。

- **第 146 行** `RERANK_W_RELEVANCE: float = 0.5`
  相关性权重，最大头。

- **第 147 行** `RERANK_W_IMPORTANCE: float = 0.2`
  重要性权重。

- **第 148 行** `RERANK_W_RECENCY: float = 0.15`
  时效权重。

- **第 149 行** `RERANK_W_CONFIDENCE: float = 0.15`
  置信度权重。

  **四个权重和为 1.0**（0.5+0.2+0.15+0.15=1.0）。这是被纠正的历史点之一：旧版用连乘，本版改加权和且和为 1。不这么做会怎样：权重和不为 1 虽不致命，但语义不再是「加权平均」，分数尺度漂移，难以解释与调参。

- **第 150 行** `# MMR 去冗余：lambda 偏向相关性，(1-lambda) 偏向多样性。`
  说明 MMR 公式的两项分工。

- **第 151 行** `MMR_LAMBDA: float = 0.7`
  MMR 的相关性偏好 λ=0.7，多样性占 0.3。偏向「留住相关项」，但仍惩罚与已选项的相似。

- **第 152 行** `# PortCtx.meta 中承载隔离域的键（D-2 x-* 扩展口径，不改 R2 DTO 结构）。`
  说明为什么用 meta 扩展而非改 `PortCtx` DTO：R2 不动，通过 `meta["scope"]` 挂扩展。

- **第 153 行** `SCOPE_META_KEY: str = "scope"`
  meta 里承载隔离域的键名。集中成一个常量，避免 `"scope"` 散落各处拼错。

- **第 154 行** `# 缺省 tenant（仅 demo/测试向后兼容；生产由 R12 强制注入真实 tenant）。`
  明确 default tenant 的定位：兼容用途，不是生产隔离策略。

- **第 155 行** `_DEFAULT_TENANT: str = "default"`
  缺省租户值。下划线前缀表示内部私有，不进 `__all__`。生产环境由 R12 装配层注入真实 tenant。

### 第 158-168 行：is_secret_key（键名闸门）

- **第 158 行** `def is_secret_key(key: str) -> bool:`
  纯函数：输入键名，输出是否疑似 secret。

- **第 159-166 行** docstring：说明这是「fail-closed 第一道闸」，命中任一特征子串返回 True（大小写不敏感）。

- **第 167 行** `folded = key.casefold()`
  大小写归一。用 `casefold()` 而非 `lower()`：`casefold` 是 Unicode 全折叠（如德语 ß→ss），防通过大小写变换绕过黑名单。
  - 不这么写会怎样：`API_KEY` 直接匹配小写表会漏，secret 值被静默写入。

- **第 168 行** `return any(token in folded for token in SECRET_KEY_TOKENS)`
  生成器 + `any`：任一特征是键名子串即命中；`any` 短路，命中即停。
  - 注意只做**子串匹配**：`my_api_key_v2` 也会命中，宁可误杀不放过（fail-closed 的取舍）。

### 第 171-186 行：make_item_id（稳定 id 生成）

- **第 171 行** `def make_item_id(tenant: str, kind: MemoryKind | str, content: str) -> str:`
  输入 tenant + kind + 内容，输出稳定 id。`kind` 允许 `str` 是为了兼容测试里传裸字符串。

- **第 172-181 行** docstring：第 175 行点明关键——**tenant 参与哈希，防止跨租户同内容撞 id**。

- **第 182 行** `from hashlib import sha256`
  函数内导入，保持模块顶层轻量（标准库 import 很便宜，但统一风格）。

- **第 184 行** `normalized = " ".join(content.split()).casefold()`
  内容规范化两步：`split()` 把任意空白（空格/换行/制表符）拆成词，`" ".join` 用单空格重连；再 `casefold()` 大小写折叠。
  - 为什么：同一段话不同空白/大小写应得同一 id，实现「稳定 upsert」。
  - 不这么写会怎样：`"hello  world"` 与 `"hello world"` 会生成两个 id，同一条记忆被重复存储。

- **第 185 行** `payload = f"{tenant}|{kind}|{normalized}".encode("utf-8")`
  用 `|` 拼接三元组并编码为 UTF-8 字节。显式 `utf-8` 不依赖系统默认编码（Windows 可能是 GBK）。
  - tenant 在第一位参与哈希：A 租户和 B 租户写同样内容，id 不同，天然隔离。

- **第 186 行** `return sha256(payload).hexdigest()[:16]`
  SHA-256 取前 16 位十六进制（64 bit）。
  - 为什么截断：完整 64 字符 hash 太长，16 位已足够低碰撞概率（作为内存内 id 与 chunk_id 后缀）。
  - 为什么不用 Python `hash()`：`hash()` 进程间不稳定（随机盐），重启后 id 全变，无法稳定 upsert；`sha256` 跨进程跨重启稳定。

### 第 189-224 行：ScopeResolver.resolve（隔离域解析）

- **第 189 行** `class ScopeResolver:`
  唯一 tenant 入口。docstring（第 190 行）强调「R1/R2 零改」——不改上游 DTO，只在解析层做文章。

- **第 192 行** `@staticmethod`
  无状态工具方法，不需要实例。

- **第 193 行** `def resolve(ctx: object) -> MemoryScope:`
  参数类型是 `object` 而非 `PortCtx`：鸭子类型——只要对象有 `meta` 属性即可，避免 memory 对 hooks/compose 的强类型反向依赖。

- **第 194-207 行** docstring：说明缺失降级为 default（第 194 行）、生产由 R12 强制注入（第 196 行）、鸭子类型避免反向依赖（第 200 行）、异常条件（第 205-206 行）。

- **第 208 行** `meta = getattr(ctx, "meta", None)`
  鸭子取 `meta`，没有就给 None。用 `getattr` 的默认值而非直接 `ctx.meta`，容忍任意对象（含 None）。

- **第 209 行** `if not isinstance(meta, dict) or SCOPE_META_KEY not in meta:`
  两个降级条件：meta 不是 dict，或 dict 里没有 `"scope"` 键。

- **第 210 行** `return MemoryScope(tenant=_DEFAULT_TENANT)`
  降级为 default 租户。仅供 demo/测试向后兼容（旧调用方不传 meta）。
  - 不这么写会怎样：直接抛错会让所有旧调用方崩溃；但注意生产环境必须注入真 tenant，否则所有数据落进 default 域——这是文档明确标注的兼容取舍，不是安全缺口（因为生产由 R12 保证）。

- **第 211 行** `raw = meta.get(SCOPE_META_KEY)`
  取出 scope 原始值，准备判定形态。

- **第 212 行** `if isinstance(raw, MemoryScope):`
  情形一：装配层直接注入了 DTO 实例。

- **第 213 行** `scope = raw`
  直接采用（DTO frozen，安全）。

- **第 214 行** `elif isinstance(raw, dict) and isinstance(raw.get("tenant"), str):`
  情形二：注入的是 dict，且 tenant 是字符串。必须校验 tenant 类型——防止 `{"tenant": 123}` 这种脏数据混进来。

- **第 215-219 行** `scope = MemoryScope(tenant=raw["tenant"], user=raw.get("user"), session=raw.get("session"))`
  从 dict 构造 DTO。user/session 用 `.get` 缺省 None（可选字段）。

- **第 220 行** `else:`
  情形三：以上都不匹配。

- **第 221 行** `raise ValueError("PortCtx.meta['scope'] invalid; fail-closed")`
  **fail-closed**：显式给了 scope 但格式非法 → 拒绝，不降级为 default。
  - 为什么不降级：显式声明了隔离域却写错，说明调用方有 bug；静默降级会掩盖错误并可能把数据写进错误租户。

- **第 222 行** `if not scope.tenant.strip():`
  最后一道：tenant 是空白串（`"  "` 或 `""`）也拒绝。`.strip()` 去掉首尾空白后判空。

- **第 223 行** `raise ValueError("empty tenant rejected")`
  空 tenant → 拒绝。与 `MemoryScope` docstring（第 79 行）呼应。
  - 不这么写会怎样：空 tenant 会让所有租户的过滤条件 `tenant == ""` 失效或误匹配，隔离形同虚设。

- **第 224 行** `return scope`
  返回合法 scope。

### 第 226-240 行：ScopeResolver.check（Query 侧防绕过）

- **第 226 行** `@staticmethod`
  同为静态方法。

- **第 227 行** `def check(query_scope: MemoryScope | None, ctx_scope: MemoryScope) -> None:`
  输入 query 显式 scope 与 ctx 可信 scope，不一致就抛错。

- **第 228 行** docstring 首句（第 228 行）点明目的：**防止从 Query 侧绕过隔离**。

- **第 229-236 行** docstring：参数说明——`query_scope` 为 None 表示取 ctx 的（第 231 行）；`ctx_scope` 是可信来源（第 232 行）；异常条件（第 234-235 行）。

- **第 237 行** `if query_scope is None:`
  Query 没显式带 scope。

- **第 238 行** `return`
  直接放行——用 ctx 的 scope 即可，无需校验。

- **第 239 行** `if not query_scope.tenant.strip() or query_scope.tenant != ctx_scope.tenant:`
  两个拒绝条件：显式 tenant 为空，或与 ctx tenant 不一致。
  - 为什么必须比对 tenant：否则调用方可以传 `MemoryQuery(scope=MemoryScope(tenant="victim"))` 来查别人租户的数据——这是典型的「Query 侧越权」。

- **第 240 行** `raise ValueError("query scope tenant mismatch; fail-closed")`
  不一致 → 拒绝。与 `resolve` 的 fail-closed 口径一致。

---

## 2. store.py 逐行讲解（31 行）

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 StorePort 默认实现：内存会话快照存取（B.1.2 勾销项）。`
  一句话定位：这是 R2 `StorePort` 的默认实现，存的是**会话快照**，不是 memory 的长期记忆项。注意与 `memory.py` 的 `MemoryPort` 区分——两个不同的 Port，两个不同的存储。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `签名对齐 R2 ``StorePort``（async save/load）；快照须可确定性 JSON 序列化，`
  两个约束：一是方法签名 async（与 R2 协议一致）；二是快照必须可确定性 JSON 序列化（DynamicValue 的契约）。

- **第 4 行** `内部做深拷贝隔离，避免调用方与存储共享可变状态。`
  点出本文件最重要的设计：**深拷贝**。存取都拷贝，外部改自己的对象不会影响存储，反之亦然。

- **第 5 行** `"""` docstring 结束。

### 第 6-12 行：imports 与导出

- **第 6 行** `from __future__ import annotations`
  延迟注解，全模块统一。

- **第 7 行** 空行。

- **第 8 行** `from copy import deepcopy`
  深拷贝工具，第 26、31 行使用。为什么必须 `deepcopy` 而非 `copy`：快照是嵌套 dict，浅拷贝只复制外层，内层仍是共享引用，隔离失效。

- **第 9 行** 空行。

- **第 10 行** `from runtime import DynamicValue`
  快照值的类型——R2 定义的动态 JSON 值联合类型（dict/list/str/int/float/bool/None）。

- **第 11 行** 空行。

- **第 12 行** `__all__ = ["InMemoryStorePort"]`
  导出面只有一个类。`__init__.py` 据此重导出。

### 第 15-31 行：InMemoryStorePort 类

- **第 15 行** `class InMemoryStorePort:`
  默认实现。注意它**没有显式继承** `StorePort`——同 skills 模块的 detector 一样，R2 Port 是结构化协议（鸭子类型），有 `name`/`save`/`load` 即履约。

- **第 16 行** `"""内存会话快照存储：进程内字典，session_id 为键。"""`
  一句话说明存储介质（进程内字典）与键（session_id）。进程内意味着不跨进程持久化——这是「内存实现」的定位。

- **第 18 行** `name: str = "in_memory_store_port"`
  具名标识，用于装配期履约校验与 trace 归因。可插拔物必须具名，是全框架纪律。

- **第 20 行** `def __init__(self) -> None:`
  构造函数无参数——内存实现不需要配置。

- **第 21 行** `"""初始化空快照表。"""`
  构造说明。

- **第 22 行** `self._snapshots: dict[str, dict[str, DynamicValue]] = {}`
  核心存储：外层键是 `session_id`，值是该会话的快照 dict。
  - 类型注解 `[str, dict[str, DynamicValue]]` 明确两层结构。
  - 用普通 dict 而非 `defaultdict`：读取不存在会话要返回 None（第 31 行），不是自动建空快照。

- **第 24 行** `async def save(self, session_id: str, snapshot: dict[str, DynamicValue]) -> None:`
  保存方法，签名对齐 R2 `StorePort.save`。`async` 是为了与协议一致（未来可换真实异步后端），当前实现是同步内存操作。

- **第 25 行** `"""保存会话快照（深拷贝隔离）。"""`
  强调深拷贝。

- **第 26 行** `self._snapshots[session_id] = deepcopy(snapshot)`
  **存入即深拷贝**：
  - 做什么：把调用方的快照完整复制一份再存。
  - 为什么：调用方存完后再改 snapshot 里的嵌套结构，不会污染存储里的值。
  - 不这么写会怎样：若直接 `= snapshot`，外部与存储共享同一对象，外部改动会「隔空」改变已存快照，造成难查的数据污染。

- **第 28 行** `async def load(self, session_id: str) -> dict[str, DynamicValue] | None:`
  读取方法，返回可空 dict。

- **第 29 行** `"""读取快照副本；不存在返回 None。"""`
  两个语义：返回副本；不存在返回 None。

- **第 30 行** `snap = self._snapshots.get(session_id)`
  用 `.get` 取，不存在得 None（而非 `[]` 抛 KeyError）。

- **第 31 行** `return deepcopy(snap) if snap is not None else None`
  **取出也深拷贝**：
  - 做什么：命中则深拷贝返回，未命中返回 None。
  - 为什么：与 save 对称——读取方拿到副本后随意改，不影响存储本体。
  - 不这么写会怎样：若直接返回 `snap`，调用方改返回值就等于改了存储，破坏隔离。注意条件必须是 `is not None` 而不是裸 `if snap`：若快照是空 dict `{}`（falsy），裸判断会误判为「不存在」而返回 None。

### store.py 小结

`store.py` 是四个文件里最小的，但纪律密度高：**async 签名对齐协议 + 具名 + 双向深拷贝 + 空 dict 与 None 严格区分**。它与 `memory.py` 的 `MemoryStore` 是两套独立存储（快照 vs 记忆项），不要混淆。

---

## 3. memory.py 逐行讲解（603 行）

这是全模块的核心与主体。建议按「模块级定义 → 构造 → Port 三方法 → 召回链 → 写入链 → 治理 → 内部算法 → 安全扫描」的顺序读，与本文件源码顺序一致。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R7 MemoryPort 默认实现：双轴分类 + 四条优化链 + 递归安全检测。`
  一句话定位：MemoryPort 默认实现，三大特征——双轴、优化链、安全检测。

- **第 2 行** 空行。

- **第 3 行** `- 温度轴 tier（hot/warm/cold）× 内容轴 kind（七类）正交；各层独立 TTL。`
  双轴正交 + 各层独立 TTL（对应 `_TIER_TTL_NS`，第 49 行）。「正交」意味着 tier 不决定 kind，kind 也不锁死 tier。

- **第 4 行** `- 写入链：规范化→递归脱敏/注入→去重→冲突消解→评分→分层落库→稳定 id upsert。`
  写入链的七个环节概述。注意顺序在源码 `ingest` 里是「判定 kind → 规范化 → 冲突裁决 → 近重复去重 → 评分 → 落库」，docstring 是概念性罗列，逐行以源码为准。

- **第 5 行** `- 召回链：tenant 过滤→混合召回→rerank 加权和→MMR 去冗余→预算裁剪→注入复检。`
  召回链六环节概述，与 `recall` 源码（第 160 行起）一一对应。

- **第 6 行** `- read/write/forget 保持 R2 MemoryPort 签名；旧调用方五字段 MemoryItem 兼容。`
  两个兼容承诺：Port 签名不变、五字段位置参数兼容（见 `types.py` 第 104-108 行）。

- **第 7 行** `"""` docstring 结束。

### 第 8-23 行：标准库与外部 imports

- **第 8 行** `from __future__ import annotations`
  延迟注解。

- **第 9 行** 空行。

- **第 10 行** `from asyncio import get_running_loop`
  `get_running_loop` 用于检测当前线程是否已有事件循环（`_run_sync` 第 85 行）。

- **第 11 行** `from asyncio import run as asyncio_run`
  `asyncio.run` 的别名，起一个新事件循环跑协程。用别名是为了在第 87、89 行简洁调用。

- **第 12 行** `from collections.abc import Coroutine`
  协程类型，用于 `_run_sync` / `_run_typed` 的参数注解。

- **第 13 行** `from concurrent.futures import ThreadPoolExecutor`
  线程池，在已有事件循环的线程里开新线程跑协程（第 88 行）。

- **第 14 行** `from math import exp, log`
  - `exp`：`_recency` 的指数衰减（第 565 行）。
  - `log`：`record_access` 的 importance log 增长（第 438 行）。

- **第 15 行** `from time import time_ns`
  纳秒时间戳，写入/时效/过期判定都用它。用 `time_ns` 而非 `time.time` 是为了纳秒精度与单调语义。

- **第 16 行** `from typing import TypeVar`
  泛型变量 `_T`，用于 `_run_sync` 返回类型推断（第 72 行）。

- **第 17 行** 空行。

- **第 18 行** `from compose import PortCtx`
  Port 上下文类型，提供 `meta["scope"]`（第 162 行 `resolve` 用）。注意依赖方向：harness → compose，不反向。

- **第 19 行** `from harness.rag.embedder import Embedder`
  R6 向量化后端协议。`MemoryStore` 构造函数接收它（第 115 行）。

- **第 20 行** `from harness.rag.store import VectorStore`
  R6 向量库协议。

- **第 21 行** `from harness.rag.types import Chunk, Vector`
  - `Chunk`：写向量库时的块结构（第 529 行）。
  - `Vector`：向量类型，用于 `_run_typed` 返回注解（第 92 行）。

- **第 22 行** `from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector`
  复用 R5 的注入检测三件套：协议、裁决枚举、默认实现。`_scan_text`（第 603 行）用 `InjectionVerdict.INJECTED` 判定。这正对应 skills 模块「被 memory 复用」的说明。

- **第 23 行** `from runtime import DynamicValue`
  KV 层的值类型（第 136 行 `_kv` 的值）。

### 第 25-44 行：types 导入与 __all__

- **第 25-42 行** `from .types import (...)`
  从同包 `types.py` 导入 18 个符号，分三组：
  - 常量（第 26-33 行）：`MMR_LAMBDA`、`NEAR_DUP_SIM_THRESHOLD`、`PREFERENCE_REPEAT_THRESHOLD`、`PROFILE_CONFIDENCE_THRESHOLD`、四个 `RERANK_W_*`——全部阈值集中在本文件顶部使用处，不散落裸值。
  - DTO / 枚举（第 34-39 行）：`MemoryItem`、`MemoryKind`、`MemoryQuery`、`MemoryScope`、`MemoryTier`、`ScopeResolver`。
  - 工具函数（第 40-41 行）：`is_secret_key`、`make_item_id`。

- **第 44 行** `__all__ = ["MemoryStore"]`
  本模块只对外导出 `MemoryStore` 一个类。其他都是内部实现细节或经 `__init__.py` 从 `types.py` 重导出。

### 第 46-72 行：模块级常量与泛型

- **第 46 行** `# 分层顺序：索引越小越热；越大越冷。`
  关键约定：`_TIER_ORDER` 的索引语义——越小越热。promote/demote 的 ±1 方向依赖它。

- **第 47 行** `_TIER_ORDER: tuple[MemoryTier, ...] = (MemoryTier.HOT, MemoryTier.WARM, MemoryTier.COLD)`
  温度轴的顺序表：HOT(0) → WARM(1) → COLD(2)。
  - 用 tuple：不可变，顺序是结构性事实。
  - 不这么写会怎样：顺序若散落在各处 if 判断，promote/demote 方向容易写反（这正是历史陷阱之一）。

- **第 48 行** `# 各层默认 TTL（纳秒）：hot 1 小时 / warm 30 天 / cold 365 天。`

- **第 49-53 行** `_TIER_TTL_NS: dict[MemoryTier, int] = {...}`
  各层 TTL（纳秒）：
  - 第 50 行 `HOT: 3_600_000_000_000` = 1 小时（3.6e12 ns）。
  - 第 51 行 `WARM: 2_592_000_000_000_000` = 30 天。
  - 第 52 行 `COLD: 31_536_000_000_000_000` = 365 天。

  用下划线分隔大数字提升可读性。`forget_expired`（第 407 行）按当前层取对应 TTL。

- **第 54 行** `# kind → 初始层默认落点（不破坏正交：任何 kind 都可因访问升迁/沉降）。`
  强调：这只是**初始落点**，不是永久绑定。正交性靠「可升迁/沉降」维持。

- **第 55-63 行** `_KIND_DEFAULT_TIER: dict[MemoryKind, MemoryTier] = {...}`
  七类各自的默认层：
  - 第 56-60 行：PROFILE / PREFERENCE / ENTITY / SEMANTIC / PROCEDURAL → WARM（结构化主检索层）。
  - 第 61 行：EPISODIC → HOT（情节有时效性，近期热）。
  - 第 62 行：JOURNAL → COLD（日志归档）。
  - 第 63 行 `}` 字典结束。

  `default_tier_for`（第 454 行）查这张表，缺省 WARM。

- **第 64 行** `_RECALL_CHUNK_PREFIX = "mem:"`
  向量库 chunk_id 前缀。chunk_id 形如 `mem:<item_id>`（第 530 行）。集中成常量避免散落。

- **第 65 行** `# importance 命中增量上限与系数（命中后 log 增长，避免线性膨胀）。`

- **第 66 行** `_IMPORTANCE_BOOST = 0.02`
  命中增量系数。`record_access` 用 `0.02 * log(1+count)`（第 438 行）。

- **第 67 行** `_MAX_IMPORTANCE_GAIN = 0.2`
  单次命中增量上限。`min(0.2, ...)` 防止高频命中把 importance 无限推高。

- **第 68 行** `# 时效半衰期（30 天，纳秒）。`

- **第 69 行** `_HALF_LIFE_NS = 2_592_000_000_000_000`
  30 天的纳秒数。`_recency` 用 `exp(-age/半衰期)`（第 565 行）：30 天前的项衰减到约 0.37。

- **第 71 行** `# 协程返回值泛型变量（供 _run_sync 类型推断）。`

- **第 72 行** `_T = TypeVar("_T")`
  泛型变量，让 `_run_sync` 的返回类型与传入协程的返回类型一致（第 75 行签名）。

### 第 75-94 行：_run_sync / _run_typed（同步桥）

- **第 75 行** `def _run_sync(coro: Coroutine[object, object, _T]) -> _T:`
  同步上下文里运行异步协程的桥接函数。签名用泛型 `_T`：传入返回 `_T` 的协程，返回 `_T`。

- **第 76-83 行** docstring：说明「已有事件循环时新建线程避免冲突」。

- **第 84 行** `try:`
  开始探测当前线程是否有运行中的事件循环。

- **第 85 行** `get_running_loop()`
  尝试获取当前线程的事件循环。

- **第 86 行** `except RuntimeError:`
  `get_running_loop()` 在没有循环时会抛 `RuntimeError`（这是它的规定行为）。

- **第 87 行** `return asyncio_run(coro)`
  无事件循环 → 直接 `asyncio.run(coro)`，在当前线程新建并运行循环，跑完关闭。
  - 这是最常见情形（同步调用方），最省事。

- **第 88 行** `with ThreadPoolExecutor(max_workers=1) as pool:`
  已有事件循环 → 开一个只有 1 个 worker 的线程池。
  - 为什么 `max_workers=1`：只为跑这一个协程，单 worker 足够；`with` 保证退出时线程被回收。

- **第 89 行** `return pool.submit(asyncio_run, coro).result()`
  把 `asyncio.run(coro)` 提交到新线程，`.result()` 阻塞等待并返回结果。
  - **为什么不直接 `asyncio.run`**：在已有运行循环里（如 async 框架内同步调用记忆 API），直接 `asyncio.run` 会抛 `RuntimeError: This event loop is already running`。新线程有独立事件循环，绕开冲突。
  - 为什么不返回 Future 而是 `.result()`：本函数是同步桥，调用方要的是同步结果，必须阻塞等完成。
  - 为什么不用 `nest_asyncio`：那会污染全局事件循环行为，开线程更隔离可控。

- **第 92 行** `def _run_typed(coro: Coroutine[object, object, list[Vector]]) -> list[Vector]:`
  返回类型收窄到 `list[Vector]` 的包装——因为 `Embedder.embed` 契约返回向量列表，但 `_run_sync` 的泛型推断在某些类型检查器下不够明确。

- **第 93 行** docstring：说明这是类型收窄包装。

- **第 94 行** `return _run_sync(coro)`
  直接委托 `_run_sync`，运行时行为完全一致，只是静态类型更精确。

### 第 97-140 行：MemoryStore 类定义、docstring 与构造

- **第 97 行** `class MemoryStore:`
  核心类。同样不显式继承 `MemoryPort`——结构化协议，有 `read`/`write`/`forget`/`recall` 即履约。

- **第 98-109 行** 类 docstring：
  - 第 98 行：一句话定位。
  - 第 101-103 行：`_embedder` / `_vector_store` / `_detector` 三个可注入依赖及其默认实现（R6 HashEmbedder / R6 InMemoryVectorStore / R5 KeywordInjectionDetector）。
  - 第 104-108 行：**五个内部字典的说明**——理解它们就理解整个类：
    - `_kv`：`(namespace, key) → 值`，KV 记忆本体（Port 三方法操作）。
    - `_items`：`item_id → MemoryItem`，记忆项实体（召回与治理操作）。
    - `_tiers`：`item_id → 当前温度层`。
    - `_item_times`：`item_id → 写入时刻(ns)`，独立 TTL 判定。
    - `_vectors`：`item_id → 已 embed 的向量`，稳定增量召回。

- **第 111 行** `name: str = "memory_store"`
  具名标识。

- **第 113-118 行** 构造函数签名：
  - **第 113 行** `def __init__(`
  - **第 114 行** `self,`
  - **第 115 行** `embedder: Embedder | None = None,`
  - **第 116 行** `vector_store: VectorStore | None = None,`
  - **第 117 行** `detector: InjectionDetector | None = None,`
  - **第 118 行** `) -> None:`

  三个依赖都是可选关键字参数（实际是位置或关键字均可），便于测试时注入桩，也便于装配层替换真实后端。

- **第 119-125 行** 构造 docstring：逐项说明 None 时的默认降级。

- **第 126 行** `from harness.rag.embedder import HashEmbedder`
  **延迟导入**——在函数内 import，避免顶层 import 成环（R6 的某些模块可能反向依赖 memory）。

- **第 127 行** `from harness.rag.store import InMemoryVectorStore`
  同上，向量库的默认实现延迟导入。

- **第 129 行** `self._embedder = embedder if embedder is not None else HashEmbedder()`
  **fail-closed 式依赖兜底**：显式 None 也用默认实现，不存在「没有 embedder 就不做向量化」的分支。用 `is not None` 而非裸真值判断，防止传入一个自定义对象恰好 `__bool__` 为 False 被误替换。

- **第 130-132 行** `self._vector_store = (vector_store if vector_store is not None else InMemoryVectorStore())`
  同上，向量库兜底。

- **第 133-135 行** `self._detector = (detector if detector is not None else KeywordInjectionDetector())`
  **安全兜底**： detector 永远存在。若允许 None 导致「不检测直接放行」，注入防护就出现缺口。
  - 不这么写会怎样：传 `detector=None` 时 `_scan_text`（第 603 行）会 `None.detect(...)` 抛 AttributeError，或更糟——被改成跳过检测，注入内容长驱直入。

- **第 136 行** `self._kv: dict[tuple[str, str], DynamicValue] = {}`
  初始化 KV 存储。键是 `(namespace, key)` 二元组，值 DynamicValue。

- **第 137 行** `self._items: dict[str, MemoryItem] = {}`
  初始化记忆项存储。

- **第 138 行** `self._tiers: dict[str, MemoryTier] = {}`
  初始化层存储。

- **第 139 行** `self._item_times: dict[str, int] = {}`
  初始化时间存储。

- **第 140 行** `self._vectors: dict[str, tuple[float, ...]] = {}`
  初始化向量缓存。向量用 tuple 存（不可变，放入 dict 值安全）。

**五个字典的关系**：`_items` / `_tiers` / `_item_times` / `_vectors` 四个都以 `item_id` 为键，是同一条记忆项的四个侧面（实体/层/时间/向量）；`_kv` 独立，是另一套简单 KV 存储。`put_item`（第 358 行）同时写前三个，`_embed_sync_batch`（第 521 行）写第四个。

### 第 142-158 行：R2 MemoryPort 履约三方法

- **第 142 行** `# ---------- R2 MemoryPort 履约（保持签名） ----------`
  分节注释。

- **第 143 行** `async def read(self, namespace: str, key: str, ctx: PortCtx) -> DynamicValue:`
  读 KV。`ctx` 参数保留是为了签名对齐 R2，但**未使用**。

- **第 144 行** docstring：`按 namespace/key 读取；不存在返回 None（键值层不做 tenant 隔离）。`
  **明确说明 KV 层不做 tenant 隔离**——隔离只在记忆项链路（ingest/recall）上。这是设计取舍：KV 是底层原语，与 port 调用方自己的 namespace 约定绑定。

- **第 145 行** `return self._kv.get((namespace, key))`
  用二元组键查表，不存在返回 None（`.get` 的默认行为）。
  - 不这么写会怎样：用 `self._kv[(namespace, key)]` 会在不存在时抛 KeyError，而 Port 契约要求返回 None。

- **第 147-149 行** `async def write(self, namespace: str, key: str, value: DynamicValue, ctx: PortCtx) -> None:`
  写 KV，签名对齐 R2。

- **第 150 行** docstring：递归 secret/注入检测命中即拒绝（fail-closed）。

- **第 151 行** `if is_secret_key(key) or is_secret_key(namespace):`
  **键名闸门**：key 和 namespace 都查（两边都可能泄露敏感语义，如 namespace="api_keys"）。

- **第 152 行** `raise ValueError(f"secret key rejected: {key}")`
  命中 → 拒绝写入。

- **第 153 行** `self._scan_value(value, key)`
  **值闸门**：递归扫描任意嵌套结构（见第 584 行 `_scan_value`）。把 key 作为 label 传入，便于报错定位。
  - 注意：`_scan_value` 内部命中就抛 ValueError，不返回布尔——所以这行不需要检查返回值。

- **第 154 行** `self._kv[(namespace, key)] = value`
  两道闸都通过，才真正写入。顺序上是「先检测后写入」，绝不存在「先写后检测」。

- **第 156 行** `async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:`
  删除 KV。

- **第 157 行** docstring：不存在幂等。

- **第 158 行** `self._kv.pop((namespace, key), None)`
  `.pop(key, None)` 的第二个参数保证不存在时不抛错——幂等语义。
  - 不这么写会怎样：用 `del self._kv[...]` 会对不存在的键抛 KeyError，破坏「遗忘不存在项也应成功」的幂等约定。

---

### 第 160-194 行：recall（召回链，十步主干）

- **第 160 行** `async def recall(self, query: MemoryQuery, ctx: PortCtx) -> list[MemoryItem]:`
  R7 语义召回主入口。

- **第 161 行** docstring：完整召回链六环节概述。

- **第 162 行** `scope = ScopeResolver.resolve(ctx)`
  **第 1 步**：从 ctx 解析隔离域（tenant 来源可信）。

- **第 163 行** `ScopeResolver.check(query.scope, scope)`
  **第 2 步**：Query 侧防绕过——显式 scope 的 tenant 必须与 ctx 一致，否则抛错（见 `types.py` 第 227 行）。
  - 这一步在过滤之前：非法 query 直接拒绝，不进入后续计算。

- **第 164-168 行** `candidates = [item for item in self._items.values() if item.scope is not None and item.scope.tenant == scope.tenant]`
  **第 3 步**：tenant 过滤。
  - `item.scope is not None` 是防御性检查（虽然默认 scope 非 None，见 `types.py` 第 113 行）。
  - `item.scope.tenant == scope.tenant` 只比 tenant，不比 user/session——当前隔离粒度是租户级。
  - 不这么做会怎样：跨租户数据进入候选集，轻则污染召回结果，重则泄露他人记忆。

- **第 169-170 行** `if query.tier is not None: candidates = [it for it in candidates if self.tier_of(it) is query.tier]`
  **第 4 步**：tier 过滤。用 `is` 比枚举成员（单例）。`tier_of`（第 372 行）兼容旧调用方无 id 的情况。

- **第 171-172 行** `if query.kind is not None: candidates = [it for it in candidates if it.kind is query.kind]`
  **第 5 步**：kind 过滤。同样用 `is`。

- **第 173-174 行** `if not candidates: return []`
  **第 6 步**：无候选快速返回，避免对空集做 embedding / rerank（省算力，也避免 `max()` 空序列等边界）。

- **第 175 行** `scored = self._score_candidates(query.text, candidates)`
  **第 7 步**：混合召回 + rerank 打分（见第 459 行）。返回 `(item, score)` 列表。

- **第 176 行** `ranked = sorted(scored, key=lambda pair: pair[1], reverse=True)`
  **第 8 步**：按 score 降序排序。`reverse=True` 高分在前。

- **第 177 行** `selected = self._mmr(ranked)`
  **第 9 步**：MMR 贪心去冗余（见第 484 行）。返回 `(item, mmr_score)` 序列，注意分数已被换成 mmr_score。

- **第 178 行** `out: list[MemoryItem] = []`
  **第 10 步**准备：结果容器。

- **第 179-180 行** 注释：`§12.4 预算裁剪：给定 budget 时按 token 贪心装入（超出丢弃低分项，非整段丢弃），否则退回 top_k 截断（指南 §7-9 陷阱修复）。两条路径都先过 top_k 上限。`
  说明两条输出路径及其共同前置（top_k）。这是历史陷阱修复的记录——旧实现或整段丢弃、或忽略 budget。

- **第 181 行** `budget_limit = query.budget.limit if query.budget is not None else None`
  提前取预算上限，避免循环里反复判空。无 budget 时 `budget_limit=None`，后续所有预算判断自动跳过。

- **第 182 行** `used = 0`
  已用预算累计器。

- **第 183 行** `for item, _ in selected[: query.top_k]:`
  两条路径的共同前置：先按 `top_k` 截断。注意 `_` 丢弃 mmr_score——输出阶段只关心 item 本身。
  - 为什么先 top_k 再预算：top_k 限制的是「返回条数」，预算限制的是「总 token」，两者都要满足，先截取候选能减少无效计算。

- **第 184 行** `if self._scan_text(item.content):`
  注入复检：对**已经入选**的项再查一次注入串。
  - 为什么需要：写入时已检测过，但历史脏数据可能是在检测规则完善前写入的；召回是数据离开系统的最后一环，必须再查（fail-closed 双保险）。

- **第 185 行** `continue  # 历史脏数据注入复检：命中即跳过（fail-closed）`
  命中注入 → 跳过该项（不是抛错中断整个召回，而是静默剔除）。

- **第 186 行** `cost = len(item.content)  # R7 统一口径（Unicode 码点，同 token_counter.estimate_tokens）`
  用 `len(str)` 作为 token 成本的近似（Unicode 码点数）。与 R6 `token_counter` 口径一致，避免两套计量标准打架。

- **第 187 行** `if budget_limit is not None and used + cost > budget_limit:`
  预算判断：加上当前项会不会超上限。

- **第 188 行** `continue  # 贪心：当前项放不下就跳过，继续尝试更小项（非整段丢弃）`
  **关键：贪心跳过而非 `break`**。当前项太大装不下，跳过它继续试后面的小项。
  - 不这么写会怎样：若用 `break`，一个超大项会堵死后面所有能装下的小项，召回结果骤减。
  - 注意 `used` 不变（没装入就不计费）。

- **第 189 行** `fresh = self.record_access(item)`
  命中记账：access_count+1、importance 按 log 增长（见第 424 行）。返回更新后的新对象。

- **第 190 行** `out.append(fresh if fresh is not None else item)`
  加入结果。用 `record_access` 的返回值（新对象）；若它返回 None（未登记）则退回原 item。
  - 为什么必须用返回值：`MemoryItem` frozen，原对象不会被改；忽略返回值就拿不到更新后的 importance/access_count。

- **第 191 行** `used += cost`
  只有真正装入才累加预算。

- **第 192-193 行** `if budget_limit is not None and used >= budget_limit: break`
  预算用尽 → 停止循环（剩余项即使更小也装不下了，因为 `used` 已达上限）。
  - 用 `>=` 而非 `>`：恰好等于上限也应停止（`used + cost > limit` 的判断已保证不会超，等于时正好装满）。

- **第 194 行** `return out`
  返回最终记忆项列表。

### 第 196-214 行：write_cold（全文保底落盘）

- **第 196 行** `# ---------- 分层与治理 ----------`
  分节注释：标志从 R2 Port 履约方法转入分层治理方法。源码用这种分隔注释划分功能区，阅读时可作为导航锚点。

- **第 197 行** `def write_cold(self, transcript: str, ctx: PortCtx | None = None) -> None:`
  同步方法。ctx 可空（调用方可能没有上下文）。

- **第 198 行** docstring：`先落盘原文全文（commit_and_compact 第一步），不经过提炼链。`
  **意义**：先保存完整原文，再做摘要/提炼——防止提炼过程丢细节。这是「保底」语义。

- **第 199-203 行** `scope = (ScopeResolver.resolve(ctx) if ctx is not None else MemoryScope(tenant="default"))`
  解析 scope。ctx 为 None 时直接用 default tenant（兼容老调用）。
  - 注意这里**没有走 `ScopeResolver.resolve(None)`**：`resolve(None)` 内部 `getattr(None, "meta", None)` 也能得到 default，但显式判断更清晰、也避免未来 resolve 行为变化影响此路径。

- **第 204-213 行** 构造 `MemoryItem`：
  - **第 205 行** `kind=MemoryKind.EPISODIC`：全文按情节类落。
  - **第 206 行** `content=transcript`：**全文原文**，不提炼、不截断。
  - **第 207 行** `importance=0.5`：中等重要性（全文保底，不抢摘要的召回权重）。
  - **第 208 行** `ts=time_ns()`：真实写入时间。
  - **第 209 行** `source="cold:transcript"`：来源标识，表明是冷层全文。
  - **第 210 行** `item_id=make_item_id(scope.tenant, MemoryKind.EPISODIC, transcript)`：稳定 id，tenant 参与哈希。
  - **第 211 行** `tier=MemoryTier.COLD`：落冷层（归档）。
  - **第 212 行** `scope=scope`：写入解析出的 scope。
  - **第 213 行** `)` 构造结束。

- **第 214 行** `self.put_item(item, MemoryTier.COLD, now=item.ts)`
  落库，层显式指定 COLD，时间与 item.ts 一致。
  - 注意：**没有做注入检测**——write_cold 的调用方（commit_and_compact）应保证 transcript 已经过安全处理；且召回时仍会 `_scan_text` 复检（第 184 行）。这是分层信任的设计。

### 第 216-279 行：ingest（写入链，十步主干）

- **第 216-225 行** 函数签名：
  - **第 216 行** `def ingest(`
  - **第 217 行** `self,`
  - **第 218 行** `text: str,`
  - **第 219 行** `ctx: PortCtx,`
  - **第 220 行** `*`：之后都是关键字参数。
  - **第 221 行** `kind: MemoryKind | None = None,`：显式内容类，None 时自动判定。
  - **第 222 行** `confidence: float = 1.0,`：来源置信度，冲突裁决依据。
  - **第 223 行** `source: str = "refined",`：来源标识。
  - **第 224 行** `now: int | None = None,`：写入时刻，便于测试注入。
  - **第 225 行** `) -> MemoryItem | None:` 返回落库项，或被合并时返回已存项。

- **第 226-238 行** docstring：逐参数说明。第 229 行注明「text 已由调用方确保不含 secret」——与 write 不同，ingest 不在本方法里做递归检测，信任调用方（但召回端有复检兜底）。

- **第 239 行** `scope = ScopeResolver.resolve(ctx)`
  **写入第 1 步**：解析隔离域。

- **第 240-241 行** 注释：`同类约束历史累计（§12.2 "≥3 次"）：同 tenant 已存的 preference 约束项数。显式 kind 时无需统计（调用方已裁定）。`

- **第 242 行** `pref_hits = 0`
  累计计数器初始化。

- **第 243 行** `if kind is None:`
  仅当调用方没指定 kind 时才统计——指定了就不需要 refine_kind，统计是浪费。

- **第 244-249 行** `pref_hits = sum(1 for stored in self._items.values() if stored.scope.tenant == scope.tenant and stored.kind is MemoryKind.PREFERENCE)`
  **写入第 2 步**：统计同 tenant 已存的 PREFERENCE 项数。
  - `stored.scope.tenant == scope.tenant`：只算本租户，防止跨租户计数污染。
  - 这是「≥3 次同类约束判偏好」的关键落地（历史纠正点之一：旧实现漏了这段）。

- **第 250-254 行** `chosen = (kind if kind is not None else self.refine_kind(text, confidence, preference_hits=pref_hits))`
  **写入第 3 步**：确定内容类。显式优先；否则调 `refine_kind` 自动判定，把累计次数传进去。

- **第 255 行** `normalized = " ".join(text.split())`
  **写入第 4 步**：规范化空白（与 `make_item_id` 内部第 184 行的规范一致）。

- **第 256 行** `item_id = make_item_id(scope.tenant, chosen, normalized)`
  **写入第 5 步**：生成稳定 id，tenant + kind + 规范内容三元组哈希。

- **第 257 行** `timestamp = now if now is not None else time_ns()`
  **写入第 6 步**：确定时间戳。`is not None` 保证显式传 0 也被尊重（测试用）。
  - 不这么写会怎样：用 `now or time_ns()` 会把显式传入的 0 当成「没给」而替换，测试无法固定时间。

- **第 258 行** 注释：`冲突消解：同 id 已存在时按 confidence 裁决，低置信不覆盖高置信。`

- **第 259 行** `existing = self._items.get(item_id)`
  **写入第 7 步**：查同 id 的已存项。

- **第 260-261 行** `if existing is not None and existing.confidence > confidence: return existing`
  已存项置信度**严格大于**本次 → 保留旧项，拒绝低置信覆盖。
  - 用 `>` 而非 `>=`：相等时允许新项覆盖（后写胜），让更新能生效。
  - 不这么做会怎样：低置信来源（如猜测）会覆盖高置信来源（如用户显式声明），记忆质量退化。

- **第 262 行** 注释：`近重复检测：同 tenant 内高相似先合并（写入链也用 0.92 阈值）。`

- **第 263 行** `dup = self._find_near_duplicate(normalized, scope.tenant)`
  **写入第 8 步**：查语义近重复（见第 340 行）。

- **第 264-265 行** `if dup is not None and dup.confidence >= confidence: return dup`
  找到近重复且它置信度**大于或等于**本次 → 合并到已存项。
  - 注意这里用 `>=`（与第 260 行的 `>` 不同）：置信度相等时也合并（去重优先），因为语义已高度相似，没必要再存一条。

- **第 266 行** `importance = self.score_importance(normalized, chosen, source)`
  **写入第 9 步**：四因子评分（见第 322 行）。

- **第 267-277 行** 构造 MemoryItem：
  - **第 268 行** `kind=chosen`：判定出的内容类。
  - **第 269 行** `content=normalized`：规范化正文。
  - **第 270 行** `importance=importance`：评分结果。
  - **第 271 行** `ts=timestamp`：写入时刻。
  - **第 272 行** `source=f"{source}:{chosen.value}"`：来源拼接 kind，如 `refined:preference`，便于追溯。
  - **第 273 行** `item_id=item_id`：稳定 id。
  - **第 274 行** `confidence=confidence`：本次置信度。
  - **第 275 行** `tier=self.default_tier_for(chosen)`：**初始层由 kind 查默认表**（WARM 为主）。
  - **第 276 行** `scope=scope`：隔离域。

- **第 278 行** `self.put_item(item, item.tier, now=timestamp)`
  **写入第 10 步**：落库——同时写 `_items` / `_tiers` / `_item_times` 三个字典（见第 358 行）。

- **第 279 行** `return item`
  返回新落库的项。

**写入链一句话总结**：解析 scope → 统计偏好累计 → 判 kind → 规范化 → 算 id → 定时戳 → 同 id 置信裁决 → 近重复去重 → 评分 → 分层落库。两步「拦截」（置信裁决、近重复）都在评分落库之前——不通过就不浪费评分与存储。

---

### 第 281-320 行：refine_kind（互斥优先级判定）

- **第 281 行** `@staticmethod`
  纯判定函数，无需实例状态。

- **第 282-284 行** 签名：`def refine_kind(self 不需要, text, confidence, *, preference_hits=0) -> MemoryKind`
  - **第 283 行** `text: str, confidence: float, *, preference_hits: int = 0`
  - `preference_hits` 是关键字参数，缺省 0。

- **第 285-299 行** docstring：第 287-289 行明确**权威序**：preference → profile → entity → procedural → journal → episodic → semantic，并记载历史纠正——旧实现把 journal 提到 entity 前、procedural 又先于 entity，与详设 §12.2 冲突，本版已纠正。第 294-295 行说明 `preference_hits` 用于落实「≥3 次」规则（历史纠正点之二）。

- **第 300 行** `folded = text.casefold()`
  一次折叠，后面所有子串判断复用。中文无大小写问题，但英文关键词（prefer/must/recipe）需要。

- **第 301-303 行** `explicit_pref = any(tok in folded for tok in ("要", "不要", "总是", "禁止", "prefer", "must"))`
  显式偏好词检测：6 个特征词，中英文混合。
  - `any` 短路：命中一个即停。
  - 注意 `"要"` 是 `"不要"` 的子串——这里有冗余但无害（两个词都在表里，命中任一都得 True）。

- **第 304 行** 注释：`显式偏好词，或同类约束累计命中达阈值（历史重复）→ preference。`

- **第 305-306 行** `if explicit_pref or preference_hits >= PREFERENCE_REPEAT_THRESHOLD: return MemoryKind.PREFERENCE`
  **最高优先级**：显式偏好词 **或** 历史累计 ≥3 次都判偏好。
  - 这是历史纠正点之二：旧实现只看显式词，从不读 `preference_hits`，「≥3 次累计」形同虚设。

- **第 307-310 行** `if confidence >= PROFILE_CONFIDENCE_THRESHOLD and any(tok in folded for tok in ("我是", "角色", "职业", "领域", "工程师", "身份")): return MemoryKind.PROFILE`
  画像类需**两个条件同时满足**：confidence ≥ 0.9 且含身份词。
  - 为什么加置信度门槛：「我是工程师」可能是玩笑或转述，低置信不该写成用户画像。

- **第 311 行** 注释：`entity 在 procedural/journal 之前（具名实体+属性优先于事件/步骤）。`
  **历史纠正点之一**：entity 必须先于 procedural/journal。

- **第 312-313 行** `if any(tok in folded for tok in ("项目", "工具", "实体", "实体卡", "Alpha", "Beta")): return MemoryKind.ENTITY`
  实体类判定。包含 `Alpha` / `Beta`（示例项目名）与「实体卡」。
  - 若放在 procedural 之后：「关于 Alpha 项目的步骤」会被误判成流程而非实体。

- **第 314-315 行** `if any(tok in folded for tok in ("步骤", "流程", "先", "然后", "recipe")): return MemoryKind.PROCEDURAL`
  流程类判定。

- **第 316-317 行** `if any(tok in folded for tok in ("决定了", "因为", "选择", "决策")): return MemoryKind.JOURNAL`
  日志类判定。

- **第 318-319 行** `if any(tok in folded for tok in ("发生", "上次", "昨天", "参加了")): return MemoryKind.EPISODIC`
  情节类判定。

- **第 320 行** `return MemoryKind.SEMANTIC`
  **兜底**：都不命中就归事实类。保证函数总有返回值。

**为什么是「互斥优先级」而非多标签**：一条记忆只归一类，判定顺序即重要性顺序；命中即停（`return`），不做累积打分。这让分类结果稳定、可解释、易测试。

### 第 322-338 行：score_importance（四因子加权）

- **第 322 行** `@staticmethod`
  纯函数。

- **第 323 行** `def score_importance(text: str, kind: MemoryKind, source: str) -> float:`

- **第 324 行** docstring 公式：`0.35·信息量 + 0.25·复用度 + 0.20·时效 + 0.20·来源权重`。四个系数和为 1.0。

- **第 325-333 行** 参数与返回说明。

- **第 334 行** `info = min(1.0, len(set(text.split())) / 20.0)`
  信息量 = **不同词数 / 20**，封顶 1.0。
  - 用 `set` 去重：重复词不增加信息量。
  - 20 是经验阈值：约 20 个不同词视为满信息量。
  - 不这么写会怎样：用原始词数会让啰嗦重复的文本得高分。

- **第 335 行** `reuse = 1.0 if kind in (MemoryKind.PROCEDURAL, MemoryKind.PREFERENCE) else 0.6`
  复用度：流程类与偏好类会被反复用到，给满分；其他类给 0.6 基准。

- **第 336 行** `recency = 1.0  # 新写入项时效满分；随时间衰减由 _recency 在召回期计算`
  写入时时效恒为 1.0——因为刚写入当然最新。衰减是召回期用 `_recency`（第 562 行）按 ts 算的，不在这里。
  - 若在这里写死衰减，则 importance 会随时间变小且无法恢复；放在召回期算才反映「当前」时效。

- **第 337 行** `weight = 1.0 if source.startswith("explicit") else 0.7`
  来源权重：显式来源（如用户明确要求）满分，其他（如推断提炼）0.7。
  - 用 `startswith("explicit")`：兼容 `explicit` / `explicit:user` 等变体。

- **第 338 行** `return min(1.0, 0.35 * info + 0.25 * reuse + 0.20 * recency + 0.20 * weight)`
  四因子加权求和，再封顶 1.0。系数 0.35+0.25+0.20+0.20=1.0，与 docstring 一致。
  - 与 rerank 权重的区别：这是**写入时**的 importance 公式；rerank（第 475 行）是**召回时**把 importance 与其他三因子再加权。两套权重不要混。

### 第 340-356 行：_find_near_duplicate（近重复检测）

- **第 340 行** `def _find_near_duplicate(self, text: str, tenant: str) -> MemoryItem | None:`

- **第 341-349 行** docstring：同 tenant 内相似度 ≥ 阈值即视为重复，返回首个高相似项。

- **第 350 行** `vec = self._embed_sync(text)`
  先向量化待检测文本（失败返回空向量，第 537 行）。

- **第 351 行** `for item in self._items.values():`
  遍历所有已存项。

- **第 352-353 行** `if item.scope.tenant != tenant: continue`
  **跳过不同租户**——近重复判定必须在隔离域内进行，否则 A 租户写的话会被 B 租户的相似项「合并」掉。

- **第 354-355 行** `if self._dot(vec, self._vector_of(item)) >= NEAR_DUP_SIM_THRESHOLD: return item`
  相似度（点积）≥ 0.92 → 返回该项。
  - `_vector_of`（第 545 行）惰性 embed 并缓存，避免重复计算。
  - 返回**首个**命中项：遍历顺序是 dict 插入序，语义上「先到先得」。

- **第 356 行** `return None`
  无高相似项。

### 第 358-370 行：put_item（稳定 upsert）

- **第 358-364 行** 签名：`def put_item(self, item, tier, *, now=None) -> None`
  第 362 行 `*` 之后 `now` 是关键字参数。

- **第 365 行** docstring：`同 item_id 覆盖不追加（稳定 upsert）。`

- **第 366 行** `item_id = item.item_id or make_item_id(item.scope.tenant, item.kind, item.content)`
  id 兜底：item 自带 id 就用，否则按 tenant + kind + 内容算。
  - 用 `or`：空串 `""`（默认 id）会被视为假值而重算——正是期望行为。

- **第 367 行** `ts = now if now is not None else item.ts`
  时间戳兜底，同样用 `is not None` 尊重显式 0。

- **第 368 行** `self._items[item_id] = item`
  写实体。

- **第 369 行** `self._tiers[item_id] = tier`
  写层。

- **第 370 行** `self._item_times[item_id] = ts`
  写时间。

**为什么是三处一起赋值**：`_items` / `_tiers` / `_item_times` 是同一条记忆的三个侧面。若漏写某个字典（比如忘了 `_tiers`），召回 tier 过滤会拿不到层、过期判定会回退 COLD TTL——数据不一致。三者必须在同一函数里原子式更新。
- 注意 `_vectors` **不在这里写**：向量由召回链的 `_embed_sync_batch` 惰性填充（第 518 行），写入时不一定需要做 embedding。

### 第 372-379 行：tier_of（查当前层）

- **第 372 行** `def tier_of(self, item: MemoryItem) -> MemoryTier | None:`

- **第 373 行** docstring：先按稳定 id，回退对象身份/值比对（兼容旧调用方）。

- **第 374-375 行** `if item.item_id and item.item_id in self._tiers: return self._tiers[item.item_id]`
  快路径：有 id 且 id 在 `_tiers` 里，直接返回。O(1)。

- **第 376-378 行** `for item_id, stored in self._items.items(): if stored is item or stored == item: return self._tiers.get(item_id)`
  慢路径：遍历 `_items`，按对象身份（`is`）**或值相等**（`==`）找匹配项，返回其层。
  - 为什么有这条：旧调用方构造的 `MemoryItem` 可能 item_id 为空（第 109 行默认 `""`），无法走快路径。
  - `is or ==`：`is` 覆盖同一对象（最快）；`==` 覆盖内容相同但对象不同的情形（dataclass 自动生成 `__eq__`）。
  - 不这么做会怎样：旧调用方的 item 在 tier 过滤时拿不到层，直接被排除在召回之外。

- **第 379 行** `return None`
  都找不到 → None。召回的 tier 过滤（第 170 行）此时 `None is query.tier` 为 False，该项被过滤掉。

### 第 381-383 行：tier_count（诊断计数）

- **第 381 行** `def tier_count(self) -> int:`
- **第 382 行** docstring：诊断/测试用。
- **第 383 行** `return len(self._items)`
  返回记忆项总数。名字虽叫 tier_count，实际是 item 数（每个 item 恰有一层，所以等价）。

### 第 385-395 行：promote / demote（升迁与沉降）

- **第 385-390 行** `promote`：
  - **第 385 行** `def promote(self, item: MemoryItem) -> None:`
  - **第 386-390 行** docstring：晋升/回温一级 cold→warm→hot，已在 hot 不动。第 389 行明确记载历史纠正——旧实现的 promote 方向反了（实为沉降），本版纠正。
  - **第 391 行** `self._shift(item, -1)`
    **delta = -1**：索引越小越热（第 46 行约定），所以 -1 是升温。

- **第 393-395 行** `demote`：
  - **第 393 行** `def demote(self, item: MemoryItem) -> None:`
  - **第 394 行** docstring：降级/沉降一级 hot→warm→cold，已在 cold 不动。
  - **第 395 行** `self._shift(item, +1)`
    **delta = +1**：索引增大变冷。

**方向直觉**：promote = 提升热度 = 索引 -1；demote = 降低热度 = 索引 +1。这是历史纠正点之三——旧版写反导致「越常访问的记忆越沉到冷层」，完全背离设计。

**双轴正交的体现**：两个方法只调 `_shift` 改 tier，**从不碰 `kind`**。一条 PREFERENCE 记忆可以从 WARM 升到 HOT，但它仍是 PREFERENCE——温度与内容互不干扰。

### 第 397-416 行：forget_expired（过期清理）

- **第 397-399 行** 签名：`def forget_expired(self, ttl: int | None = None, *, now: int | None = None) -> list[MemoryItem]:`
  ttl 可显式统一指定；now 可注入便于测试。

- **第 400 行** docstring：ttl 缺省按各层独立 TTL，now 缺省当前时刻。

- **第 401 行** `moment = now if now is not None else time_ns()`
  确定判定基准时刻。

- **第 402 行** `expired: list[MemoryItem] = []`
  被删项的收集列表（返回给调用方，便于审计）。

- **第 403 行** `for item_id, ts in list(self._item_times.items()):`
  遍历时间字典的**快照**（`list(...)` 包一层）。
  - 为什么用快照：循环体内要 `pop` 修改 `_item_times`，直接迭代原 dict 会抛 `RuntimeError: dictionary changed size during iteration`。

- **第 404-408 行** `effective = (ttl if ttl is not None else _TIER_TTL_NS.get(self._tiers[item_id], _TIER_TTL_NS[MemoryTier.COLD]))`
  确定该项的有效 TTL：
  - 显式传了 ttl → 统一用它。
  - 否则按该项当前层的 TTL 查表（第 49 行）。
  - 查不到层 → 回退 COLD 的 TTL（最长，保守——不轻易删）。

- **第 409 行** `if moment - ts > effective:`
  过期判定：**严格大于** `>`。恰好等于 TTL 不算过期。
  - 为什么用 `>` 而非 `>=`：边界时刻项仍有效，避免误删。

- **第 410 行** `item = self._items.pop(item_id, None)`
  从 `_items` 删除，用 `.pop(..., None)` 防缺失。

- **第 411 行** `self._tiers.pop(item_id, None)`
  从 `_tiers` 删除。

- **第 412 行** `self._item_times.pop(item_id, None)`
  从 `_item_times` 删除。

- **第 413 行** `self._vectors.pop(item_id, None)`
  从 `_vectors` 删除。

  **四个字典全删**：items / tiers / times / vectors 一个不留。
  - 为什么必须全删：只删 `_items` 会在其他三个字典留下孤儿条目——内存泄漏，且 `_vectors` 里的陈旧向量会干扰后续 MMR。

- **第 414-415 行** `if item is not None: expired.append(item)`
  只把真正存在的项加入返回列表。

- **第 416 行** `return expired`
  返回被删除的项列表。

### 第 418-422 行：importance_of（读当前重要性）

- **第 418 行** `def importance_of(self, item: MemoryItem) -> float:`
- **第 419 行** docstring：命中重算后可能高于初始值。
- **第 420-421 行** `if item.item_id and item.item_id in self._items: return self._items[item.item_id].importance`
  从存储读**当前** importance（可能已被 `record_access` 提高）。
- **第 422 行** `return item.importance`
  存储里没有就返回 item 自带值（初始评分）。

**为什么不直接用 `item.importance`**：MemoryItem frozen，`record_access` 更新的是存储里的新对象；调用方手里的旧 item 分数是过期的。这个方法保证读到最新值。

### 第 424-452 行：record_access（命中记账）

- **第 424 行** `def record_access(self, item: MemoryItem) -> MemoryItem | None:`

- **第 425-432 行** docstring：第 431 行特别强调「caller 应使用返回值」——这是 frozen DTO 的直接后果。

- **第 433 行** `item_id = item.item_id or make_item_id(item.scope.tenant, item.kind, item.content)`
  id 兜底，同 `put_item`（第 366 行）。

- **第 434 行** `stored = self._items.get(item_id)`
  取存储中的项。

- **第 435-436 行** `if stored is None: return None`
  未登记 → 返回 None（调用方 recall 第 190 行会退回原 item）。

- **第 437 行** `count = stored.access_count + 1`
  命中次数 +1。

- **第 438 行** `boost = min(_MAX_IMPORTANCE_GAIN, _IMPORTANCE_BOOST * log(1 + count))`
  **log 增长 + 上限**：
  - `0.02 * log(1+count)`：第 1 次命中 boost≈0.014，第 10 次≈0.048，第 100 次≈0.092——增长越来越慢。
  - `min(0.2, ...)` 封顶 0.2，无论命中多少次，单次增量不超过 0.2。
  - 为什么不用线性 `0.02 * count`：高频项 importance 会飞速冲破 1.0 并被反复 clamp，失去区分度；log 增长让「常访问」有奖励但不失控。
  - 为什么 `log(1+count)` 而非 `log(count)`：count=1 时 `log(1)=0` 没有奖励；`log(2)` 才有正增量，避免首次命中无变化。

- **第 439-450 行** 构造新 MemoryItem（**除两项外全复制 stored**）：
  - **第 440 行** `kind=stored.kind`
  - **第 441 行** `content=stored.content`
  - **第 442 行** `importance=min(1.0, stored.importance + boost)`：**变化点 1**，加 boost 并封顶 1.0。
  - **第 443 行** `ts=stored.ts`：注意**不更新时间**——ts 是「写入时刻」，不是「访问时刻」，访问不该重置时效。
  - **第 444 行** `source=stored.source`
  - **第 445 行** `item_id=stored.item_id`
  - **第 446 行** `confidence=stored.confidence`
  - **第 447 行** `tier=stored.tier`：注意**不改层**——升迁由 `promote` 显式触发，记账不自动改层（保持职责单一）。
  - **第 448 行** `scope=stored.scope`
  - **第 449 行** `access_count=count`：**变化点 2**，更新命中次数。
  - **第 450 行** `)` 构造结束。

- **第 451 行** `self._items[item_id] = updated`
  用新对象覆盖存储中的旧对象（frozen DTO 的唯一更新方式）。

- **第 452 行** `return updated`
  返回新对象——调用方必须用这个返回值，否则拿到的是旧 importance。

### 第 454-456 行：default_tier_for

- **第 454 行** `def default_tier_for(self, kind: MemoryKind) -> MemoryTier:`
- **第 455 行** docstring：查初始层默认落点（维度正交，仅默认非强制）。
- **第 456 行** `return _KIND_DEFAULT_TIER.get(kind, MemoryTier.WARM)`
  查表（第 55 行），查不到回退 WARM。
  - 用 `.get` 带默认值：即使 `_KIND_DEFAULT_TIER` 漏配某个 kind 也不崩，安全落到主检索层。

---

### 第 458-482 行：_score_candidates（混合召回 + rerank）

- **第 458 行** `# ---------- 内部：召回链 ----------`
  分节注释。

- **第 459-461 行** 签名：`def _score_candidates(self, text: str, items: list[MemoryItem]) -> list[tuple[MemoryItem, float]]:`

- **第 462 行** docstring：混合召回打分（词面重合 + 向量相似）+ rerank 加权和。

- **第 463 行** `query_lower = text.casefold()`
  查询文本大小写折叠，供词面匹配用。

- **第 464-467 行** `lexical = [sum(1.0 for word in query_lower.split() if word in it.content.casefold()) for it in items]`
  **词面命中数**：对每项，统计查询词中有多少个是该 item 内容的子串。
  - 用 `sum(1.0 ...)` 得浮点，便于后面除法。
  - 用 `word in it.content.casefold()` 子串匹配而非全词匹配——容忍词形变化。
  - 注意 `it.content.casefold()` 在生成器里对每个 word 都重算一次——有性能浪费，但 item 数量小、可读性优先。

- **第 468 行** `max_lex = max(lexical, default=1.0) or 1.0`
  取最大命中数作为归一化分母。
  - `default=1.0`：空列表（理论上不会发生，recall 第 173 行已挡）时给 1.0。
  - `or 1.0`：**双保险**——若 `max_lex` 算出来是 0.0（所有项词面命中都是 0），也强制为 1.0。
  - 不这么做会怎样：所有命中数为 0 时除以 0 抛 ZeroDivisionError，召回直接崩。

- **第 469 行** `vectors = self._ensure_vectors(items)`
  确保所有候选项的向量已缓存并返回（见第 516 行）。

- **第 470 行** `qvec = self._embed_sync(text)`
  查询文本向量化（失败得空向量，第 537 行）。

- **第 471 行** `scored: list[tuple[MemoryItem, float]] = []`
  结果容器。

- **第 472 行** `for idx, item in enumerate(items):`
  遍历候选项，`idx` 对应 lexical 与 vectors 的下标。

- **第 473 行** `sim = self._dot(qvec, vectors[idx])`
  **向量相似度**：查询向量与项向量的点积。空向量或维度不等返回 0.0（第 557 行）。

- **第 474 行** `relevance = 0.6 * sim + 0.4 * (lexical[idx] / max_lex)`
  **混合相关度**：0.6 向量相似 + 0.4 归一化词面命中。
  - 为什么混合：纯向量对关键词匹配不敏感，纯词面抓不住语义；二者互补。
  - 词面项除以 `max_lex` 归一到 0~1，与 sim 尺度一致。

- **第 475-480 行** `score = (RERANK_W_RELEVANCE * relevance + RERANK_W_IMPORTANCE * item.importance + RERANK_W_RECENCY * self._recency(item.ts) + RERANK_W_CONFIDENCE * item.confidence)`
  **rerank 加权和**：0.5·相关度 + 0.2·重要性 + 0.15·时效 + 0.15·置信度。
  - 四个权重来自 `types.py` 第 146-149 行，**和为 1.0**（历史纠正点之四：旧版用连乘把分数压扁，本版改加权和）。
  - `_recency(item.ts)` 在召回期实时算（第 562 行），反映「当前」时效。
  - 不这么写会怎样：用连乘则四因子中任一接近 0 就把总分拖到接近 0，区分度尽失。

- **第 481 行** `scored.append((item, score))`
  收集 `(项, 分数)` 对。

- **第 482 行** `return scored`
  返回未排序的打分列表（排序由 recall 第 176 行做）。

### 第 484-514 行：_mmr（真贪心去冗余）

- **第 484-486 行** 签名：`def _mmr(self, ranked: list[tuple[MemoryItem, float]]) -> list[tuple[MemoryItem, float]]:`

- **第 487-494 行** docstring：每轮选 `λ·相关 - (1-λ)·最大相似度` 最优者；返回的是**真贪心顺序**，非输入顺序。

- **第 495 行** `remaining = list(ranked)`
  复制候选列表（不修改入参）。

- **第 496 行** `selected: list[tuple[MemoryItem, float]] = []`
  已选列表。

- **第 497 行** `while remaining:`
  直到候选耗尽。

- **第 498 行** `best_idx = 0`
  本轮最优下标。

- **第 499 行** `best_score = float("-inf")`
  本轮最优分数初始化为负无穷——保证任何候选的 mmr_score 都会大于它。
  - 不这么写会怎样：初始化 0 的话，当所有 mmr_score 都为负时永远选不中，死循环。

- **第 500 行** `for idx, (item, score) in enumerate(remaining):`
  遍历本轮所有剩余候选。

- **第 501-508 行** `max_sim = (max(self._dot(self._vector_of(item), self._vector_of(other)) for other, _ in selected) if selected else 0.0)`
  计算当前 item 与**所有已选项**的最大相似度：
  - 若 `selected` 非空：`max(...)` 取与已选项的最大点积。
  - 若 `selected` 为空（第一轮）：`0.0`——没有已选项，无冗余可惩罚。
  - 用 `max` 而非平均：只要与某一已选项高度相似就重罚，确保多样性。

- **第 509 行** `mmr_score = MMR_LAMBDA * score - (1 - MMR_LAMBDA) * max_sim`
  **MMR 公式**：`0.7·score - 0.3·max_sim`。
  - 第一项奖励相关性，第二项惩罚与已选项的相似。
  - 与已选内容越像，分越低——这就是去冗余的机制。

- **第 510-511 行** `if mmr_score > best_score: best_score, best_idx = mmr_score, idx`
  记录本轮最优。用 `>` 而非 `>=`：并列时保留先出现的，保持确定性。

- **第 512 行** `chosen = remaining.pop(best_idx)`
  从候选移除最优项。

- **第 513 行** `selected.append((chosen[0], best_score))`
  加入已选。**注意记录的是 `best_score`（mmr_score），不是原来的 rerank score**——返回值里的分数语义是 MMR 分。

- **第 514 行** `return selected`
  返回去冗余后的序列。

**为什么是「真贪心」**：每轮 `while` 都重新遍历所有剩余项、重新计算它们与当前已选集的相似度——选择顺序由 mmr_score 动态决定，**不是按输入顺序逐个判断**。若错误地按输入顺序一次遍历，就退化成「阈值过滤」，无法全局优化多样性。

### 第 516-519 行：_ensure_vectors（惰性向量缓存）

- **第 516 行** `def _ensure_vectors(self, items: list[MemoryItem]) -> list[tuple[float, ...]]:`

- **第 517 行** docstring：按稳定 item_id 惰性 embed 并缓存（禁止全量重嵌/临时索引）。

- **第 518 行** `self._embed_sync_batch([it for it in items if it.item_id not in self._vectors])`
  挑出**尚未缓存**的项（`item_id not in _vectors`），只 embedding 这些。
  - 为什么按 item_id 判：稳定 id 是缓存键，同一条记忆只嵌一次。
  - 不这么做会怎样：每次召回都全量重嵌所有项，O(n) embedding 调用，性能随记忆量线性恶化。

- **第 519 行** `return [self._vector_of(it) for it in items]`
  返回所有项的向量（已缓存的直接取，第 545 行）。

### 第 521-535 行：_embed_sync_batch（批量写向量库）

- **第 521 行** `def _embed_sync_batch(self, items: list[MemoryItem]) -> None:`

- **第 522 行** docstring：把未缓存项 embed 后写入向量库（chunk_id 为稳定 item_id）。

- **第 523-524 行** `if not items: return`
  空列表快速返回，避免无谓调用 embedder。

- **第 525 行** `vectors = _run_typed(self._embedder.embed([it.content for it in items]))`
  批量 embedding：把所有内容组成列表一次传入，通过同步桥 `_run_typed`（第 92 行）执行。
  - 批量而非逐条：减少后端往返开销。

- **第 526 行** `for item, vec in zip(items, vectors):`
  `zip` 一一配对（假定 embedder 返回顺序与输入一致，这是 Embedder 契约）。

- **第 527-528 行** `if item.item_id: self._vectors[item.item_id] = tuple(vec)`
  有 id 才缓存（空 id 无法作为键）。转 tuple 防外部修改。

- **第 529-534 行** 构造 `Chunk`：
  - **第 530 行** `chunk_id=f"{_RECALL_CHUNK_PREFIX}{item.item_id}"`：块 id = `mem:` + item_id（前缀常量第 64 行）。
  - **第 531 行** `doc_id="memory"`：所有记忆项归到同一 doc 下。
  - **第 532 行** `text=item.content`：正文。
  - **第 533 行** `vector=tuple(vec)`：向量。
  - **第 534 行** `)` 结束。

- **第 535 行** `_run_sync(self._vector_store.upsert([chunk]))`
  通过同步桥把 chunk 写入向量库（upsert = 有则更新无则插入）。
  - 用 chunk_id = 稳定 item_id：同一项重复 embedding 是更新而非新增，不产生重复 chunk。
  - 不这么做会怎样：用随机 chunk_id 会导致每次召回都往向量库塞新块，存储膨胀。

### 第 537-543 行：_embed_sync（单文本向量化，带兜底）

- **第 537 行** `def _embed_sync(self, text: str) -> tuple[float, ...]:`

- **第 538 行** docstring：查询文本 embed（同步包装）；失败返回空向量走词面兜底。

- **第 539 行** `try:`
  开始保护 embedding 调用。

- **第 540 行** `vectors = _run_typed(self._embedder.embed([text]))`
  同步桥执行单文本 embedding。

- **第 541 行** `return tuple(vectors[0]) if vectors else ()`
  有结果取第一条转 tuple；空列表返回空 tuple。
  - `if vectors` 防 IndexError（后端返回空列表的边界）。

- **第 542-543 行** `except (IndexError, ValueError, TypeError): return ()`
  **安全降级**：三类常见后端异常一律吞掉，返回空向量。
  - 为什么吞：embedding 失败不该让整个召回崩掉——空向量会让 `_dot` 返回 0（第 557 行），相关度退化为纯词面匹配（第 474 行仍有 0.4 的词面项），召回仍可用。
  - 为什么不捕获 `Exception` 全吞：只吞预期的三种，其他异常（如网络错误的具体类型）应暴露以便排查。这里按源码原样记录其取舍。

### 第 545-552 行：_vector_of（取项向量）

- **第 545 行** `def _vector_of(self, item: MemoryItem) -> tuple[float, ...]:`

- **第 546 行** docstring：取项向量；未缓存则即时 embed。

- **第 547-548 行** `if item.item_id and item.item_id in self._vectors: return self._vectors[item.item_id]`
  命中缓存直接返回（快路径）。

- **第 549 行** `vec = self._embed_sync(item.content)`
  未命中则即时 embedding。

- **第 550-551 行** `if item.item_id: self._vectors[item.item_id] = vec`
  有 id 就写回缓存，下次不再重算。
  - 注意即使 embedding 失败（`vec=()`）也缓存空 tuple——避免对失败项反复重试。

- **第 552 行** `return vec`
  返回向量。

### 第 554-559 行：_dot（点积相似度）

- **第 554 行** `@staticmethod`
  纯计算。

- **第 555 行** `def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:`

- **第 556 行** docstring：点积相似度；维度不等返回 0（安全降级）。

- **第 557-558 行** `if not a or not b or len(a) != len(b): return 0.0`
  三个安全条件：a 空 / b 空 / 维度不等 → 0.0。
  - 不这么写会怎样：维度不等时 `zip` 会静默截断到较短长度，得到一个无意义的点积；空向量时结果 0 虽对但显式判断更清晰。

- **第 559 行** `return sum(x * y for x, y in zip(a, b))`
  逐元素相乘求和（标准点积）。
  - 未做归一化（非余弦相似度）——假定 embedder 输出已归一化（HashEmbedder 等通常如此），直接点积即可。

### 第 561-565 行：_recency（时效分数）

- **第 561 行** `@staticmethod`
- **第 562 行** `def _recency(ts: int) -> float:`
- **第 563 行** docstring：30 天半衰期归一到 0~1。
- **第 564 行** `age_ns = max(0, time_ns() - ts)`
  计算年龄，`max(0, ...)` 防**未来时间戳**（测试注入或时钟偏差）导致负年龄。
- **第 565 行** `return exp(-age_ns / _HALF_LIFE_NS)`
  指数衰减：`exp(-age/半衰期)`。
  - age=0 → 1.0；age=30 天 → exp(-1)≈0.37；age=60 天 → exp(-2)≈0.14。
  - 用 `exp` 而非线性衰减：近期衰减快、远期平缓，符合记忆遗忘曲线。

### 第 567-582 行：_shift（层移动的底层实现）

- **第 567 行** `# ---------- 内部：治理与安全 ----------`
  分节注释。

- **第 568 行** `def _shift(self, item: MemoryItem, delta: int) -> None:`

- **第 569 行** docstring：按 delta 在温度层序上移动一级，不改内容轴 kind。

- **第 570 行** `item_id = item.item_id or make_item_id("default", item.kind, item.content)`
  id 兜底。这里 tenant 写死 `"default"`——因为 `_shift` 收到的 item 可能无 id 也无 scope 信息（治理路径），用默认租户算一个可定位的 key。
  - 这是一个保守近似：若 item 实际属于其他租户且无 id，会算错 id → 后面遍历回退（第 573 行）兜底。

- **第 571 行** `current = self._tiers.get(item_id)`
  查当前层。

- **第 572-576 行** `if current is None: for mid, stored in self._items.items(): if stored is item or stored == item: item_id, current = mid, self._tiers.get(mid); break`
  查不到则遍历 `_items` 按身份/值匹配回退（兼容无 id 的旧 item），找到后更新 item_id 与 current。

- **第 577-578 行** `if current is None: return`
  仍查不到 → 该项未登记，什么都不做（幂等）。

- **第 579 行** `idx = _TIER_ORDER.index(current)`
  取当前层在顺序表（HOT, WARM, COLD）中的索引：0/1/2。

- **第 580-582 行** `self._tiers[item_id] = _TIER_ORDER[min(max(idx + delta, 0), len(_TIER_ORDER) - 1)]`
  **带 clamp 的移动**：
  - `idx + delta`：promote(-1) 或 demote(+1)。
  - `max(..., 0)`：不小于 0（已在 HOT 的 promote 不动）。
  - `min(..., len-1)`：不大于 2（已在 COLD 的 demote 不动）。
  - 用 `min(max(...))` 而不是条件判断：简洁且无边界遗漏。
  - 不这么写会怎样：不加 clamp 会索引越界（IndexError），或绕回另一端（Python 负索引让 HOT 的 promote 跳到 COLD，完全反直觉）。

**只改 `_tiers`，不碰 `_items` / kind / 内容**：这是「双轴正交」的落地——层移动是纯温度轴操作。

### 第 584-599 行：_scan_value（递归安全扫描）

- **第 584 行** `def _scan_value(self, value: DynamicValue, label: str) -> None:`

- **第 585 行** docstring：递归扫描任意值（含 dict/list/tuple 嵌套字符串）的 secret/注入串。

- **第 586 行** `if isinstance(value, str):`
  分支一：字符串。

- **第 587 行** `if is_secret_key(label) or self._scan_text(value):`
  两个检查：
  - `is_secret_key(label)`：键名是否是 secret（如 `api_key`）。
  - `self._scan_text(value)`：值里是否含注入串（见第 601 行）。
  - `or` 短路：键名命中就不必再查值。

- **第 588 行** `raise ValueError(f"secret/injection rejected in value: {label}")`
  命中即抛错，带 label 便于定位是哪个字段出错。

- **第 589-591 行** `elif isinstance(value, dict): for key, sub in value.items(): self._scan_value(sub, str(key))`
  分支二：dict——逐项递归，**用子键的字符串作为新 label**。
  - 关键：这样嵌套 `{"config": {"api_key": "x"}}` 里，内层 `api_key` 会成为 label 被 `is_secret_key` 命中。
  - 为什么用 `str(key)`：键可能不是字符串（JSON 键总是 str，但 DynamicValue 更宽）。

- **第 592-594 行** `elif isinstance(value, (list, tuple, set, frozenset)): for sub in value: self._scan_value(sub, label)`
  分支三：四种序列/集合——逐项递归，**沿用当前 label**（集合元素没有自己的键名）。

- **第 595-597 行** `elif hasattr(value, "model_dump"):`
  分支四：**pydantic 模型**——检测 `model_dump` 方法存在（鸭子类型，不依赖具体 pydantic 类）。
  - **第 596 行** 注释：展开为 dict 后递归，防止嵌套 secret 绕过。
  - **第 597 行** `self._scan_value(value.model_dump(), label)`

- **第 598-599 行** `elif isinstance(value, bytes): self._scan_value(value.decode("utf-8", errors="replace"), label)`
  分支五：bytes——`decode` 成字符串后按分支一处理。`errors="replace"` 保证非法字节不抛错（替换成替换字符），扫描继续。
  - 为什么要有 bytes 分支：否则字节流里的 secret/注入串能绕过字符串检测。

**为什么必须递归**：secret / 注入串常藏在任意深度的嵌套结构里（如 `{"config": {"tokens": [{"api_key": "..."}]}}`）。浅层扫描只查顶层字符串，深层完全漏检。递归 + 每层的 label 传递，让键名闸门在每一层都生效。
- 注意 `elif` 的**顺序**：dict / list 等先判，pydantic 用 `hasattr` 在后面——若某个 dict 也有 `model_dump` 属性，会先按 dict 处理（正确）。

### 第 601-603 行：_scan_text（注入检测）

- **第 601 行** `def _scan_text(self, text: str) -> bool:`
- **第 602 行** docstring：复用 R5 检测器判定注入串。
- **第 603 行** `return self._detector.detect(text) is InjectionVerdict.INJECTED`
  - 复用 R5 的 `KeywordInjectionDetector`（或注入的自定义检测器）——**全框架统一检测逻辑**，memory 不自己实现一套。
  - 用 `is` 比枚举成员（单例，第 22 行导入 `InjectionVerdict`）。
  - 返回 bool，供 `_scan_value`（第 587 行）与 recall 复检（第 184 行）使用。

---

## 4. __init__.py 逐行讲解（51 行）

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 长期记忆：MemoryPort / StorePort 默认实现与双轴分类 DTO。`
  包级定位：同时点出两个 Port 的默认实现与双轴 DTO。
- **第 2 行** 空行。
- **第 3 行** `公共 API 见 docs/02-模块详设/Agent/R7-上下文与记忆.md。`
  指路详设文档。
- **第 4 行** `"""` docstring 结束。
- **第 5 行** `from __future__ import annotations`
  延迟注解，全模块统一。

### 第 7-28 行：重导入（包级 API 收敛）

- **第 7 行** `from .memory import MemoryStore`
  从核心实现导入 `MemoryStore`。

- **第 8 行** `from .store import InMemoryStorePort`
  从快照实现导入 `InMemoryStorePort`。

- **第 9-28 行** `from .types import (...)`
  从数据模型层导入 18 个符号：
  - 第 10-19 行：10 个常量（`MMR_LAMBDA`、近重复/偏好/画像阈值、四个 rerank 权重、`SCOPE_META_KEY`、`SECRET_KEY_TOKENS`）。
  - 第 20-25 行：5 个 DTO/枚举（`MemoryItem`、`MemoryKind`、`MemoryQuery`、`MemoryScope`、`MemoryTier`）。
  - 第 26-27 行：`ScopeResolver`、`is_secret_key`。
  - 第 28 行：`make_item_id`、`)` 结束。

**为什么要集中重导出**：外部只需 `from harness.memory import MemoryStore, MemoryKind, ScopeResolver`，不必知道内部有 `memory.py` / `types.py` / `store.py` 三个文件。这是「包级 API 收敛」纪律——内部重构文件名不影响外部调用方。
- 注意 `_DEFAULT_TENANT`（内部私有）没有重导出，符合「只暴露公共面」原则。

### 第 30-51 行：__all__（公共导出面）

- **第 30 行** `__all__ = [`
  声明本包的公共符号白名单，共 21 项。

- **第 31-36 行** 六个 DTO/工具：`MemoryItem`、`MemoryKind`、`MemoryQuery`、`MemoryScope`、`MemoryTier`、`ScopeResolver`。

- **第 37-38 行** `SECRET_KEY_TOKENS`、`SCOPE_META_KEY`：常量。

- **第 39-41 行** 三个分类阈值：`NEAR_DUP_SIM_THRESHOLD`、`PREFERENCE_REPEAT_THRESHOLD`、`PROFILE_CONFIDENCE_THRESHOLD`。

- **第 42-45 行** 四个 rerank 权重：`RERANK_W_RELEVANCE`、`RERANK_W_IMPORTANCE`、`RERANK_W_RECENCY`、`RERANK_W_CONFIDENCE`。

- **第 46 行** `MMR_LAMBDA`：MMR 参数。

- **第 47-48 行** `is_secret_key`、`make_item_id`：两个工具函数。

- **第 49-50 行** `MemoryStore`、`InMemoryStorePort`：两个 Port 默认实现。

- **第 51 行** `]` 结束。

**导入清单与 `__all__` 的一致性**：第 7-28 行导入的符号与第 30-51 行列出的符号一一对应（除内部私有项）。这种「导入即导出」的写法让公共面一目了然，也是其他兄弟模块（skills/rag）的统一风格。

---

## 5. 逐行讲解后的整体心智图

```
memory 模块 = types.py（数据） + store.py（快照） + memory.py（核心） + __init__.py（门面）

types.py
  ├─ 双轴：MemoryTier(hot/warm/cold) × MemoryKind(七类)         ← 正交
  ├─ DTO：MemoryScope(tenant必填) / MemoryItem(frozen,10字段) / MemoryQuery(含budget)
  ├─ ScopeResolver.resolve(ctx)  → meta["scope"] 或 default（空白tenant拒绝）
  │           .check(query_scope, ctx_scope) → tenant 必须一致（防Query侧绕过）
  ├─ is_secret_key(key)      → 7特征子串, casefold 匹配
  └─ make_item_id(tenant,kind,content) → sha256(f"{tenant}|{kind}|{规范content}")[:16]
                                          tenant 参与哈希，防跨租户撞 id

store.py  InMemoryStorePort（StorePort 默认）
  └─ _snapshots[session_id] = snapshot   ← 双向 deepcopy 隔离

memory.py  MemoryStore（MemoryPort 默认）
  ├─ 构造：embedder/vector_store/detector 三依赖延迟导入+fail-closed 兜底
  ├─ 五字典：_kv(Port三方法) | _items/_tiers/_item_times/_vectors(召回与治理)
  │
  ├─ 写入链 ingest（10步）
  │   resolve scope → pref_hits累计 → refine_kind → 规范化 → item_id
  │   → 时间戳 → 同id置信裁决(低不盖高) → 近重复去重(0.92) → score_importance → put_item
  │
  ├─ 召回链 recall（10步）
  │   resolve+check → tenant过滤 → tier/kind过滤 → 空返回
  │   → _score_candidates(混合0.6向量+0.4词面 → rerank加权和0.5/0.2/0.15/0.15)
  │   → 降序排序 → _mmr(0.7·score-0.3·max_sim 真贪心) → 输出循环
  │       输出循环：注入复检(continue) → 预算贪心(装不下continue, 满则break)
  │                → record_access(log增长, 上限0.2) → 加入结果
  │
  ├─ 治理：promote(-1升热)/demote(+1降温)/_shift(clamp)  ← 只动tier不动kind
  │        forget_expired(四字典全删, > 不算等于) / record_access / importance_of
  │
  ├─ 向量：_ensure_vectors(惰性) → _embed_sync_batch(写mem:chunk) / _embed_sync(失败空向量)
  │        _vector_of(缓存) / _dot(维度不等返0) / _recency(30天半衰期exp衰减)
  │
  ├─ 同步桥：_run_sync(无循环直接asyncio.run; 有循环开线程) / _run_typed(收窄类型)
  │
  └─ 安全闸：_scan_value 递归扫 str/dict/list/tuple/set/pydantic/bytes
             → is_secret_key(键名) or _scan_text(复用R5检测器, is 比枚举)

__init__.py  统一重导出 21 个公共符号 → from harness.memory import ...
```

---

## 6. 细节速记卡（逐行精华）

### 双轴与数据模型

- **双轴正交**：tier(hot/warm/cold) × kind(七类) 独立；`promote`/`demote` 只动 tier 不动 kind。
- **两个 StrEnum**：成员既是枚举（可比）又是字符串（可序列化），比裸字符串收敛取值。
- **三个 frozen dataclass**：`MemoryScope` / `MemoryItem` / `MemoryQuery`，不可变 + slots。
- **scope 默认 tenant="default" 而非 None**：防跨 tenant 过滤被 None 旁路（`types.py` 第 113 行）。
- **MemoryItem 前五字段位置参数**：兼容旧调用方（第 104-108 行）；后五字段有默认值。
- **MemoryQuery 有 budget 字段**（旧册遗漏）：给定则按 token 贪心，缺省仅 top_k（第 134 行）。
- **TokenBudget 延迟导入**：放 `TYPE_CHECKING` 里（第 13-14 行），避免 import 成环。

### 阈值与安全

- **四个 rerank 权重和为 1**：0.5/0.2/0.15/0.15，**加权和不是连乘**（连乘把 0.5 压到无区分度，`types.py` 第 145 行）。
- **tenant 参与 item_id 哈希**：`sha256(f"{tenant}|{kind}|{规范content}")[:16]`，防跨租户同内容撞 id（第 185 行）。
- **item_id 用 sha256 不用 hash()**：后者进程间不稳定，重启后 id 全变。
- **内容规范化**：`" ".join(content.split()).casefold()`，空白/大小写不敏感（第 184 行）。
- **is_secret_key**：7 特征子串 + `casefold()` 大小写不敏感（第 167-168 行）。
- **fail-closed 三处**：secret 键名（第 151 行）、注入串（第 587/603 行）、空 tenant（第 222/239 行）。
- **递归安全扫描覆盖**：str / dict（子键当 label）/ list / tuple / set / frozenset / pydantic(`model_dump`) / bytes（第 586-599 行）。
- **detector 永远存在**：传 None 也用默认实现，无「缺席放行」分支（第 133-135 行）。

### 写入链

- **写入链 10 步**：resolve → pref_hits → refine_kind → 规范化 → item_id → 时间戳 → 置信裁决 → 近重复 → 评分 → 落库（第 239-278 行）。
- **refine_kind 权威序**：preference → profile → entity → procedural → journal → episodic → semantic，命中即停（第 305-320 行）。
- **profile 需 confidence ≥ 0.9** 且含身份词（第 307 行）。
- **preference 看累计 3 次**：显式词或历史 `pref_hits >= 3`（第 305 行，历史纠正点）。
- **entity 先于 procedural/journal**：具名实体优先于事件/步骤（第 311-313 行，历史纠正点）。
- **冲突裁决 `>`、近重复 `>=`**：同 id 低置信不盖高；近重复置信相等也合并（第 260、264 行）。
- **importance 公式**：`0.35·信息量 + 0.25·复用度 + 0.20·时效 + 0.20·来源`，和为 1（第 338 行）。
- **信息量按不同词数/20**：`set(text.split())` 去重，防啰嗦得高分（第 334 行）。
- **五字典关系**：`_items`/`_tiers`/`_item_times` 同键三侧面由 `put_item` 同时写（第 368-370 行）；`_vectors` 由召回惰性写；`_kv` 独立。

### 召回链

- **召回链 10 步**：resolve+check → tenant → tier/kind → 空返回 → 打分 → 排序 → MMR → 输出循环（第 162-194 行）。
- **KV 层不做 tenant 隔离**：源码明确，隔离只在记忆项链路（第 144 行）。
- **混合相关度 0.6 向量 + 0.4 词面**：词面按 `max_lex` 归一，`max(...) or 1.0` 防除零（第 468、474 行）。
- **MMR = 0.7·score − 0.3·max_sim**：真贪心（每轮重算全部剩余项），不是输入顺序阈值过滤（第 497-513 行）。
- **MMR 用 `best_score=-inf` 初始化**：防全负分数时死循环（第 499 行）。
- **时效是 30 天半衰期指数衰减**：`exp(-age/半衰期)`，30 天→0.37、60 天→0.14（第 565 行）。
- **预算裁剪是贪心跳过**：装不下 `continue` 试下一个小的（非整段丢弃），`used >= limit` 才 `break`（第 187-193 行）。
- **召回末尾注入复检**：历史脏数据命中也 `continue` 剔除（第 184-185 行）。
- **record_access 用返回值**：item frozen，原对象没被改；log 增长且有上限 0.2（第 438、452 行）。
- **record_access 不更新 ts / tier**：ts 是写入时刻，升迁由 promote 显式触发（第 443、447 行）。

### 治理与工程细节

- **promote = -1 升热，demote = +1 降温**：索引越小越热（第 46、391、395 行，历史纠正点）。
- **_shift 用 `min(max(idx+delta,0),2)` clamp**：边界不动，不越界、不绕回（第 580-582 行）。
- **forget_expired 四字典全删**：items/tiers/times/vectors 一个不留（第 410-413 行）。
- **过期判断是 `>` 不是 `>=`**：恰好等于 TTL 不算过期（第 409 行）。
- **遍历 `_item_times` 用 `list(...)` 快照**：防迭代中 pop 抛 "dictionary changed size"（第 403 行）。
- **_run_sync 在有事件循环时开线程**：避免「循环已在运行」错误；无循环直接 `asyncio.run`（第 84-89 行）。
- **默认依赖全部延迟导入**：构造内 import HashEmbedder / InMemoryVectorStore，避免顶层成环（第 126-127 行）。
- **embed 失败返回空向量**：`_dot` 维度不等返 0，召回退化为词面兜底，不中断（第 542-543、557 行）。
- **chunk_id = `mem:` + 稳定 item_id**：重复 embedding 是 upsert 不新增，防向量库膨胀（第 530 行）。
- **store.py 双向 deepcopy**：save 存时拷、load 取时拷，防调用方与存储共享可变状态（第 26、31 行）。
- **load 判 `is not None` 而非裸真值**：空 dict 也是有效快照（第 31 行）。

### 四个历史纠正（读旧文档注意口径）

1. **kind 顺序**：entity 提到 procedural/journal 之前（第 311 行）。
2. **偏好累计**：`pref_hits >= 3` 落实「≥3 次」（第 305 行）。
3. **promote 方向**：promote = 升热(-1)、demote = 降温(+1)（第 391/395 行）。
4. **rerank 权重**：改连乘为加权和，权重和为 1（第 475-480 行）。

---

*四个文件逐行读完。建议回 [00 第 8 节](./00-总入口与全局链路.md) 再看端到端链路，此时每个环节的细节都已在脑中。涉及注入检测的段落可对照 [05 skills](./05-skills技能模块.md) 复核检测器实现。*

