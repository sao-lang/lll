# 代码精读 01 · context 上下文模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/context/` 全部 11 个文件：`types.py`(65) + `token_counter.py`(50) + `assembler.py`(115) + `compaction.py`(155) + `reinjector.py`(88) + `offloader.py`(103) + `offload_policy.py`(68) + `observability.py`(61) + `manager.py`(227) + `ports.py`(24) + `__init__.py`(64)，共 1020 行源码。
> 总入口见 [00](./00-总入口与全局链路.md)；记忆本体见 [07 memory](./07-memory模块精读.md)。

---

## 0. 读前GPS

### 模块定位

context 模块只干一件事：**在把消息送给模型之前，把有限的上下文窗口花在刀刃上**。它属于 R7（上下文与记忆域），是「会话状态 State.messages」与「模型输入」之间的工程层。

它有四条链，全部是包内组件（⑥组件），**不是 R1 内核算子，也不是 hook 通知**：

- **组装链**（assembler）：把多个 `ContextSection` 按优先级、配额、预算拼成 `list[Message]`。
- **压缩链**（compaction）：对话历史超阈值时，钉住高价值消息，其余折叠成一条独立 role 的语义摘要。
- **重注入链**（reinjector）：把摘要、钉住片段、未完成任务按 30% 预算上限塞回下一轮。
- **卸载链**（offloader + offload_policy）：超大的工具结果落盘换 `store://<id>` 句柄，而不是截断。

四条链由 `ContextMemoryManager` 用**代码调用顺序显式串联**——先 `write_cold` 全文，再 `compact` 产视图，再 `reinject` 回注。不是 hook，不是订阅通知。这是它最重要的设计特征。

边界：短期记忆归 R1（`State.messages`），这里只做上下文工程；长期记忆的存取委托给 `MemoryStore`（07 册）。

### 核心类速览

| 类 | 文件 | 角色 |
|---|---|---|
| `ContextSection` / `TrimPolicy` | types.py | 待组装段 DTO + 裁剪策略枚举 |
| `TokenCounter` / `DefaultTokenCounter` | token_counter.py | 统一 token 口径（协议 + 默认实现） |
| `ContextAssembler` / `DefaultContextAssembler` | assembler.py | 组装链（协议 + 默认实现） |
| `CompactionPolicy` / `DefaultCompactionPolicy` | compaction.py | 压缩链（协议 + 默认实现） |
| `Reinjector` / `DefaultReinjector` | reinjector.py | 重注入链（协议 + 默认实现） |
| `BlobPort` / `InMemoryBlobStore` / `Offloader` / `DefaultOffloader` | offloader.py | 卸载存储协议 + 默认实现 |
| `OffloadPolicy` | offload_policy.py | 卸载阈值策略（截断被否决） |
| `CacheTelemetry` | observability.py | KV cache 命中率一等观测 DTO |
| `ContextMemoryManager` | manager.py | 编排器，7 依赖显式注入 |
| `MemoryPortWithRecall` | ports.py | R7 扩展协议（语义召回） |

### 贯穿全册的关键设计决策（先记住，后面逐行验证）

1. **统一 token 口径**：全模块只有 `estimate_tokens = len(text)` 一个口径（Unicode 码点数），中文不按字节折算；assembler / compaction / reinjector 三个消费者共享同一个 counter 实例（「统一口径下传」）。
2. **fail-closed**：非法策略构造即抛错（`OffloadPolicy` 拒绝 `truncate`、负值计数、负阈值）；budget=None 强制回退有限值，禁止无限绕过裁剪。
3. **摘要用独立 role**：摘要消息 `role="summary"`，不能用 `role="system"`，否则下一轮压缩会按 role 永久钉住、越滚越大（防沉淀）。
4. **system 段三种特殊待遇**：priority 最小排最前、同优先级只保留先到者（防覆盖注入）、配额上限是总预算的 50%。
5. **裁剪是降级不是丢弃**：非 system 段超预算时截到剩余额度，只有裁完为空才整段跳过。
6. **重注入 30% 上限**：回注段最多占预算 30%，摘要优先、任务次之，超预算先砍任务。
7. **落盘不截断**：大工具结果落 BlobPort 换句柄，明确否决截断（截断会丢尾部最新数据/错误堆栈）。
8. **观测一等公民**：KV cache 命中率是具名 DTO（`CacheTelemetry`），不是零散日志字符串；无访问返回 0.0 而非 NaN。
9. **四个内部协议不进 PORT_PROTOCOLS**：`TokenCounter` / `BlobPort` / `Offloader` / `Reinjector` 只是包内组件，Port 数量不变。
10. **产新不改性**：压缩、重注入、with_ 都返回新对象，绝不修改 `state.messages`（内核「只增不减」契约）。

---

## 1. types.py 逐行讲解（65 行）

本文件只描述数据结构与具名常量，**不含任何组装/压缩逻辑**。它是整个模块的「词汇表」。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 上下文工程核心 DTO：上下文段、裁剪策略与压缩阈值常量。`
  一句话定位：这里只有三类东西——上下文段（`ContextSection`）、裁剪策略（`TrimPolicy`）、压缩阈值常量。归属 R7。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `本模块只描述数据结构与具名常量，不含组装/压缩逻辑（规则 1：魔法值具名）。`
  强调本文件的职责边界：只声明数据。规则 1「魔法值具名」指所有散落的数字/字符串都要提成具名常量。

- **第 4 行** `TokenBudget 复用 R6 ``harness.rag.types.TokenBudget``，不在此重定义。`
  说明依赖方向：预算 DTO 来自 R6（rag 域），context 直接复用，不重复定义。避免两处类型不兼容。

- **第 5 行** `"""` docstring 结束。

### 第 6-9 行：imports

- **第 6 行** `from __future__ import annotations`
  延迟求值注解：类型注解里的名字在运行时不求值，允许引用尚未定义的类型，降低 import 顺序耦合。

- **第 8 行** `from dataclasses import dataclass`
  导入 dataclass 装饰器，用于把 `ContextSection` 声明成数据类。

- **第 9 行** `from enum import StrEnum`
  导入 `StrEnum`（Python 3.11+）。注意不是 `Enum`：`StrEnum` 成员本身就是字符串，既能当枚举比较，又能直接进 JSON/日志。

### 第 11-19 行：`__all__` 导出清单

- **第 11-19 行** `__all__ = [SYSTEM_SECTION_NAME, TrimPolicy, ContextSection, TRIGGER_RATIO, DEFAULT_TRIGGER_TURNS, DEFAULT_KEEP_RECENT, DEFAULT_PIN_PATTERNS]`
  声明本模块的 7 个公共符号。`__init__.py` 与门禁符号检查据此重导出。
  - 1 个段名常量 + 1 个策略枚举 + 1 个 DTO + 4 个压缩阈值常量。
  - 不这么写：外部就得知道符号定义在哪个文件，包级 API 无法收敛；`from mod import *` 也会把 `dataclass`、`StrEnum` 一起泄漏出去。

### 第 21-22 行：`SYSTEM_SECTION_NAME` 常量

- **第 21 行** `# system 段名（具名常量，禁止裸魔法串；由 assembler 重导出保持 API 兼容）。`
  注释说明两点：提常量是为规则 1（禁止裸用 `"system"` 字符串）；assembler 会重导出它以兼容旧调用方。

- **第 22 行** `SYSTEM_SECTION_NAME = "system"`
  把 system 段名提为具名常量。全模块凡是要判断 `name == "system"` 都引用它，而不是散落裸串。
  - 不这么写：某处拼错成 `"System"` 或 `"sysytem"` 不会报错，system 段的特殊待遇（排最前/去重/限长）会静默失效，极难排查。

### 第 25-38 行：`TrimPolicy` 裁剪策略枚举

- **第 25 行** `class TrimPolicy(StrEnum):`
  继承 `StrEnum`：有限取值集合用枚举（规则 5），禁止裸魔法串。成员本身就是字符串值。

- **第 26-33 行** docstring + 成员说明。
  列出 4 个成员及含义。这段是给人读的契约说明。

- **第 35 行** `PIN = "pin"`
  钉住不裁：用于 system / 高价值段。

- **第 36 行** `SUMMARY = "summary"`
  摘要后保留。

- **第 37 行** `HEAD = "head"`
  保留头部。

- **第 38 行** `TAIL = "tail"`
  保留尾部。注意：实际 `_trim` 实现里 `HEAD` 与 `PIN` 走同一分支（取头部），`TAIL` 与 `SUMMARY` 走尾部保留（见 assembler.py 第 110-115 行）。枚举成员比裸串好在：拼错 `TrimPolicy.HED` 会立刻 `AttributeError`，不会静默走错分支。

### 第 41-57 行：`ContextSection` DTO

- **第 41 行** `@dataclass(frozen=True, slots=True)`
  两个关键字都关键：
  - `frozen=True`：实例不可变（不能 `sec.content = ...`）。DTO 在多条链之间传递，不可变保证不会被某一链偷偷改写影响其他链。
  - `slots=True`：不用 `__dict__`，省内存、防动态加属性。拼错字段名 `sec.contnt` 会立刻报错，不会静默创建新属性。
  - 不这么写：可变 DTO 在被 assembler / manager 传递时可能被意外修改，导致「同一段文本在组装前后不一致」的幽灵 bug。

- **第 42 行** `class ContextSection:`
  数据类声明。

- **第 43-51 行** docstring：字段说明。
  注意第 46 行强调 `name` 的 system 值由 `SYSTEM_SECTION_NAME` 约定、不可被覆盖——这是为 assembler 的 system 特殊待遇做铺垫。

- **第 53 行** `name: str`
  段名。`"system"` 是保留名。

- **第 54 行** `content: str`
  段正文。

- **第 55 行** `quota: int`
  该段的 token 配额上限（超过就按 `trim_policy` 裁剪）。

- **第 56 行** `priority: int`
  优先级，**数字越小越先排**。system 段 priority 最小，排最前，先占用预算。

- **第 57 行** `trim_policy: TrimPolicy`
  超配额时的裁剪策略，类型是枚举而非裸 `str`，杜绝非法策略值。

### 第 60-65 行：压缩阈值常量

- **第 60 行** `# 压缩阈值（设计 §5：窗口 70% / 12 轮 / 保留最近 4 轮）。`
  注释指向设计文档 §5，说明这组常量的来源。

- **第 61 行** `TRIGGER_RATIO: float = 0.7`
  token 占比达到 70% 触发压缩。带类型注解 `: float`，读代码即知类型。

- **第 62 行** `DEFAULT_TRIGGER_TURNS: int = 12`
  轮次达到 12 触发压缩。

- **第 63 行** `DEFAULT_KEEP_RECENT: int = 4`
  压缩时保留最近 4 条消息。抗 context rot（保留最近信息）。

- **第 64 行** `# 高价值钉住特征：system 段、未完成事项标记（保留用户任务线索）。`
  注释说明钉住特征的意图：保留用户任务线索，别把「待办/未完成」压掉。

- **第 65 行** `DEFAULT_PIN_PATTERNS: tuple[str, ...] = ("system", "待办", "未完成")`
  正文含这三个特征之一即钉住。用 tuple 而非 list：不可变，防止运行期被意外修改。

---

## 2. token_counter.py 逐行讲解（50 行）

本文件解决一个历史问题：旧实现里 `len(str)` 和 `len/4` 两套口径并存，导致 assembler 和 compaction 对同一段文本算出不同 token 数。现在收敛为**唯一口径**。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 统一 token 计数：assemble 与 compaction 共用的唯一计数口径。`
  一句话定位：这里定义全模块唯一的 token 计数口径。

- **第 3-4 行** `消除旧实现 len(str) 与 len/4 两套口径并存的失真；默认用 Unicode 码点估算，中文/多字节不再按字节折算。可注入真实 tokenizer 适配器。`
  交代改造动机（两套口径失真）与默认策略（Unicode 码点）+ 可扩展点（可注入真实 tokenizer）。

- **第 5 行** `"""` docstring 结束。

### 第 6-10 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol, runtime_checkable`
  - `Protocol`：定义可注入的计数器协议。
  - `runtime_checkable`：让 `isinstance(x, TokenCounter)` 可用于装配期履约校验。

- **第 10 行** `__all__ = ["DefaultTokenCounter", "TokenCounter", "estimate_tokens"]`
  导出三个符号：协议、默认实现、估算函数。

### 第 13-21 行：`TokenCounter` 协议

- **第 13 行** `@runtime_checkable`
  允许运行时 `isinstance()` 检查成员存在。代价：只检查成员名，不检查方法签名。

- **第 14 行** `class TokenCounter(Protocol):`
  可插拔协议定义。注意它**不进 PORT_PROTOCOLS**——只是 context 包内部协议。

- **第 15 行** `"""token 计数协议（context 包内部协议，不进 PORT_PROTOCOLS）。"""`
  明确它不增加系统 Port 数量。

- **第 17 行** `name: str`
  具名标识：「可插拔物必须具名」纪律的落地，装配期按 name 校验履约、进 trace 归因。

- **第 19 行** `def count(self, text: str) -> int:`
  协议方法签名：输入文本，输出 token 估算数。

- **第 20 行** `"""返回文本的 token 估算数。"""` 方法说明。

- **第 21 行** `...`
  Protocol 方法体用省略号，是接口声明语法，不是「代码被省略」。

### 第 24-33 行：`estimate_tokens` 函数

- **第 24 行** `def estimate_tokens(text: str) -> int:`
  统一估算函数。它是全模块口径的**单一定义源**。

- **第 25-32 行** docstring：`统一 token 估算：非空文本按 Unicode 字符数计，向上取整到 token。` + Args/Returns 说明。

- **第 33 行** `return len(text)`
  **核心行**：直接返回字符串长度（Unicode 码点数）。
  - 为什么：默认口径要确定、可复现、零依赖。`len` 对中文按 1 个码点计，不再按 UTF-8 的 3 字节折算，消除了与字符切片 `piece[:remain]` 的不一致。
  - 不这么写：若回到 `len(text)/4` 这类估算，中文被严重低估（一个中文字符≈1 token，不是 0.25），预算会被撑爆。
  - 隐含假设：assembler 里用字符切片裁剪、用 `len` 计数，二者才对齐；若注入真实 tokenizer，切片 N 字符的 token 数未必等于 N（见 assembler.py 第 70 行说明）。这是当前实现的已知边界。

### 第 36-50 行：`DefaultTokenCounter` 类

- **第 36 行** `class DefaultTokenCounter:`
  默认计数器实现。它没有显式继承 `TokenCounter`——Protocol 是结构化类型（鸭子类型），有 `name` 和 `count` 即履约，不需要继承。

- **第 37 行** `"""默认 token 计数器：单一口径，可被真实 tokenizer 替换。"""`
  一句话定位。

- **第 39 行** `name: str = "default_token_counter"`
  具名标识。

- **第 41 行** `def count(self, text: str) -> int:`
  履约方法。

- **第 42-49 行** docstring：Args/Returns 说明。

- **第 50 行** `return estimate_tokens(text)`
  委托给 `estimate_tokens`，确保默认实现与单一定义源一致。
  - 不这么写：若这里重复实现一遍 `len(text)`，将来改口径时要改两处，容易漂移。委托是「单一定义源」纪律的落地。

---

## 3. assembler.py 逐行讲解（115 行）

组装链：把分段投影成消息列表。核心是**优先级排序 + 配额截断 + 贪心预算 + system 保护**。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 默认上下文组装器：分段排序、配额截断、贪心预算装入与 system 保护。`
  一句话点出四个关键动作。

- **第 3-4 行** `按 priority 升序排列各段，逐段按 quota 截断；统一 TokenCounter 计数，超预算段降级裁剪而非整段丢弃；system 段保留先到者且设上限（不突破总预算）。`
  概括完整算法与两条特殊规则（降级裁剪、system 保护）。

- **第 5 行** `"""` docstring 结束。

### 第 6-16 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol` 定义组装协议。

- **第 10 行** `from harness.rag.types import TokenBudget`
  复用 R6 的预算 DTO。依赖方向：harness 内部依赖 rag 的类型。

- **第 11 行** `from runtime import Message`
  产出的是内核 `Message`（两段式 role/content）。

- **第 13 行** `from .token_counter import DefaultTokenCounter, TokenCounter`
  统一口径的协议 + 默认实现。

- **第 14 行** `from .types import ContextSection, SYSTEM_SECTION_NAME, TrimPolicy`
  输入 DTO + system 常量 + 裁剪策略枚举。

- **第 16 行** `__all__ = ["SYSTEM_SECTION_NAME", "ContextAssembler", "DefaultContextAssembler"]`
  注意 `SYSTEM_SECTION_NAME` 被**重导出**（它本定义在 types.py）——为兼容旧调用方 `from assembler import SYSTEM_SECTION_NAME`。

### 第 18-19 行：`_SYSTEM_BUDGET_RATIO` 常量

- **第 18 行** `# system 段配额上限（占预算比例；防止超长 system 单段突破总预算）。`
  注释说明动机：防止一段超长 system 吃掉全部预算。

- **第 19 行** `_SYSTEM_BUDGET_RATIO = 0.5`
  system 段最多占总预算的一半。具名常量（规则 1）。

### 第 22-29 行：`ContextAssembler` 协议

- **第 22 行** `class ContextAssembler(Protocol):`
  组装协议，可注入自定义实现。

- **第 23 行** docstring：`上下文组装协议：把分段投影为消息列表。`

- **第 25 行** `name: str` 具名标识。

- **第 27 行** `def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:`
  协议方法签名：分段 + 预算 → 消息列表。

- **第 28 行** `"""按配额与预算把分段组装为消息列表。"""` 方法说明。

- **第 29 行** `...` 接口声明。

### 第 32-43 行：`DefaultContextAssembler` 类与构造

- **第 32 行** `class DefaultContextAssembler:`
  默认组装器实现，不显式继承协议（鸭子类型）。

- **第 33 行** `"""默认组装器：优先级排序 + 配额截断 + 贪心预算 + system 保护。"""` 一句话定位。

- **第 35 行** `name: str = "default_assembler"` 具名标识。

- **第 37 行** `def __init__(self, counter: TokenCounter | None = None) -> None:`
  构造函数。`counter` 可缺省。

- **第 38-42 行** docstring：第 41 行强调 `None` 用 `DefaultTokenCounter`（与压缩同口径）——这是「统一口径」在缺省路径上的保证。

- **第 43 行** `self._counter = counter if counter is not None else DefaultTokenCounter()`
  **fail-closed 落地行**：counter 永远存在。传 `None` 也用默认值，不存在「无计数器」状态。
  - 不这么写：若 `self._counter = counter` 直接赋值，传 None 后 `self._counter.count()` 会 `AttributeError`。

### 第 45-75 行：`assemble` 方法（核心）

- **第 45 行** `def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:`
  主方法。

- **第 46-54 行** docstring：第 49 行强调 `sections` 顺序无关（内部按 priority 排序），第 50 行说明 `limit=None` 表示不限。

- **第 55 行** `cap = int(budget)  # None → -1（R6 TokenBudget.__int__ 语义，表示不限）`
  **关键行**：把预算转成整数上限。三态语义（来自 R6 `TokenBudget.__int__`）：`limit=None` → `-1`（不限）；非负整数 → 硬上限；`0` → 无预算。
  - 不这么写：若直接读 `budget.limit` 得到 `None`，后面 `used + cost > None` 会 `TypeError`。转成 `cap` 后用 `cap >= 0` 统一判断，干净利落。

- **第 56 行** `ordered = self._dedup_system(sorted(sections, key=lambda s: s.priority))`
  两步合一：先按 `priority` 升序排序（数字小的 system 段排最前），再去重 system 段。
  - 为什么排序：确定性——输入顺序无关，输出稳定可复现；且高优先级段先占预算。

- **第 57 行** `out: list[Message] = []` 结果容器。

- **第 58 行** `used = 0` 已用 token 计数。

- **第 59 行** `for sec in ordered:` 逐段处理。

- **第 60 行** `piece = self._trim(sec)`
  先按配额裁剪正文（见第 105-115 行）。

- **第 61 行** `if sec.name == SYSTEM_SECTION_NAME:`
  **system 段特殊待遇入口**：命中后走独立分支，不参与通用贪心。

- **第 62 行** `piece = self._cap_system(piece, cap)`
  二次限长：system 段再截到 `cap * 0.5`（见第 77-90 行）。这是 system 的第三重特殊待遇。

- **第 63 行** `out.append(Message(role=sec.name, content=piece))`
  直接追加消息，role 用段名（system）。

- **第 64 行** `used += self._counter.count(piece)`
  累加 token 计数。

- **第 65 行** `continue`
  **关键**：跳过后面的贪心分支。system 段不受总预算贪心约束（但已被 `_cap_system` 限到 50%）。
  - 不这么写：若 system 也走贪心，它虽排最前，但超长时会被整段按剩余额度截断甚至丢弃，破坏「system 必在前且完整到 50%」的语义。

- **第 66 行** `# 贪心装入：预算内先整段；超预算时降级裁剪到剩余额度（非整段丢弃）。`
  注释点明「降级裁剪而非丢弃」的设计。

- **第 67 行** `cost = self._counter.count(piece)`
  算本段裁剪后正文的 token 成本。统一口径。

- **第 68 行** `if cap >= 0 and used + cost > cap:`
  判断超预算。`cap >= 0` 排除「不限」（`-1`）情形。

- **第 69 行** `remain = max(0, cap - used)`
  剩余额度。`max(0, ...)` 防止已超额时为负数。

- **第 70 行** `piece = piece[:remain]`
  **降级裁剪**：把正文截到剩余额度，而不是整段丢弃。
  - 不这么写：若 `continue` 整段丢弃，会丢失本段全部信息；降级裁剪至少保留能装下的部分。
  - 隐含边界：这里是**字符切片**，`remain` 来自 token 计数。默认 counter 是 `len`，二者一致；注入真实 tokenizer 后可能不一致（截 N 字符≠N token）。这是已知假设，不是 bug。

- **第 71-72 行** `if not piece: continue`
  裁完为空则跳过（避免产生空 content 消息）。

- **第 73 行** `out.append(Message(role=sec.name, content=piece))`
  追加消息。

- **第 74 行** `used += self._counter.count(piece)`
  累加裁剪后的实际计数。

- **第 75 行** `return out`
  返回消息列表，每段一条。

### 第 77-90 行：`_cap_system` 方法

- **第 77 行** `def _cap_system(self, piece: str, cap: int) -> str:`
  system 段限长方法。

- **第 78-86 行** docstring：`system 段设上限：超长系统段也不得突破预算。` + Args/Returns。

- **第 87 行** `if cap < 0:`
  若预算不限（`-1`）。

- **第 88 行** `return piece`
  不限时原样返回，不截断。

- **第 89 行** `limit = max(1, int(cap * _SYSTEM_BUDGET_RATIO))`
  **核心行**：上限 = 预算 × 0.5，且至少 1 字符。
  - `max(1, ...)` 防极端情况（cap 很小如 0 或 1）算出 0，导致 system 被裁成空串。
  - 不这么写：若无 50% 上限，一段 10 万字符的 system 会吃光整个窗口，模型看不到任何其他上下文。

- **第 90 行** `return piece[:limit]`
  截断到上限。

### 第 92-103 行：`_dedup_system` 方法

- **第 92 行** `@staticmethod` 静态方法（不依赖实例状态）。

- **第 93 行** `def _dedup_system(sections: list[ContextSection]) -> list[ContextSection]:`
  去重方法。

- **第 94 行** `"""同 priority 下 system 段只保留先到者，防止覆盖注入。"""`
  **关键设计**：防覆盖注入。system 提示是最高信任级别，若允许多个 system 段，后到的可能覆盖先到的安全指令，构成 prompt injection 风险。

- **第 95 行** `seen_system = False` 标记是否见过 system 段。

- **第 96 行** `kept: list[ContextSection] = []` 保留结果。

- **第 97 行** `for sec in sections:` 遍历（此时已按 priority 排序）。

- **第 98 行** `if sec.name == SYSTEM_SECTION_NAME:` 命中 system 段。

- **第 99-100 行** `if seen_system: continue`
  见过就跳过后续所有 system 段（只留先到者）。

- **第 101 行** `seen_system = True` 标记已见。

- **第 102 行** `kept.append(sec)` 非重复段加入结果。

- **第 103 行** `return kept` 返回去重后列表。

### 第 105-115 行：`_trim` 方法

- **第 105 行** `@staticmethod` 静态方法。

- **第 106 行** `def _trim(sec: ContextSection) -> str:`
  按策略裁剪段正文。

- **第 107 行** `"""按 trim_policy 与 quota 截断段正文。"""` 说明。

- **第 108 行** `if len(sec.content) <= sec.quota:`
  若正文长度不超配额。

- **第 109 行** `return sec.content`
  原样返回（不裁剪）。

- **第 110 行** `if sec.trim_policy is TrimPolicy.HEAD:`
  头部策略。用 `is` 比较枚举成员（单例）。

- **第 111 行** `return sec.content[: sec.quota]`
  取头部前 quota 字符。

- **第 112 行** `if sec.trim_policy is TrimPolicy.PIN:`
  钉住策略。

- **第 113 行** `return sec.content[: sec.quota]`
  **注意**：PIN 与 HEAD 走同一分支（取头部）。这是实现选择——「钉住」在组装层等价于「保头部」。

- **第 114 行** `# TAIL / SUMMARY 默认保留尾部（保留最近信息，抗 context rot）。`
  注释说明默认分支（TAIL/SUMMARY）的动机：保留最近信息，抗上下文腐化。

- **第 115 行** `return sec.content[-sec.quota :]`
  取尾部后 quota 字符。这是**默认裁剪策略**（TAIL 是 types.py 里的默认语义）。
  - 不这么写：若默认取头部，最新/最相关的信息（往往在尾部）会被丢掉，加剧 context rot。

---

## 4. compaction.py 逐行讲解（155 行）

压缩链：判定是否压缩 + 产压缩视图。**产新消息序列，绝不修改 `state.messages`**（内核只增不减契约）。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R7 默认压缩策略：阈值触发 + 钉住保留 + 语义摘要（不沉淀）。`
  一句话点出三要素与关键特性「不沉淀」。

- **第 3 行** `压缩产新消息序列（视图层），绝不修改 State.messages（内核只增不减契约）。`
  强调「产新不改性」的硬约束。

- **第 4-5 行** `钉住 system、最近 N 轮与高价值消息；其余折叠为独立 role 的语义摘要，防止摘要 role=system 被下轮按 role 永久钉住。token 计数统一走 TokenCounter。`
  说明钉住三条件、摘要独立 role 的反沉淀设计、统一口径。

- **第 6 行** `"""` docstring 结束。

### 第 7-19 行：imports

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `from typing import Protocol` 定义压缩协议。

- **第 11 行** `from runtime import AgentState, Message`
  输入状态与消息类型。

- **第 13 行** `from .token_counter import DefaultTokenCounter, TokenCounter, estimate_tokens`
  `estimate_tokens` 用于 `count_tokens` 兼容函数。

- **第 14-19 行** `from .types import (DEFAULT_KEEP_RECENT, DEFAULT_PIN_PATTERNS, DEFAULT_TRIGGER_TURNS, TRIGGER_RATIO)`
  从 types.py 引入四个阈值常量。压缩阈值不在本文件重定义（单一定义源）。

### 第 21-26 行：`__all__` 导出

- **第 21-26 行** `__all__ = ["SUMMARY_ROLE", "CompactionPolicy", "DefaultCompactionPolicy", "count_tokens"]`
  导出四个符号。注意 `SUMMARY_ROLE` 是跨模块共享的摘要角色常量（reinjector 会重导出它）。

### 第 28-33 行：三个具名常量

- **第 28 行** `# 摘要消息角色（独立于 system，防沉淀）。`
  注释点明设计动机：防沉淀。

- **第 29 行** `SUMMARY_ROLE = "summary"`
  **核心设计行**：摘要消息的独立 role。
  - 为什么不用 `role="system"`：若摘要用 system role，下一轮压缩时会在第 121 行 `msg.role == "system"` 被永久钉住，摘要越滚越大、永不淘汰。独立 role 让它能被后续压缩正常处理。
  - 不这么写：用 system role 会导致「摘要沉淀」——上下文里堆积越来越多的摘要，反而撑爆窗口。

- **第 31 行** `SUMMARY_PREFIX = "[摘要] "`
  摘要正文前缀（具名常量，禁止裸串）。

- **第 33 行** `_SUMMARY_ROLE_WINDOW = 5`
  摘要里展示的角色序列长度（人类可读提示用）。下划线前缀表示包内私有。

### 第 36-45 行：`count_tokens` 兼容函数

- **第 36 行** `def count_tokens(text: str) -> int:`
  兼容旧调用方的计数函数。

- **第 37 行** `"""统一 token 估算（兼容旧调用方；与 assemble 同口径）。"""`
  说明它是兼容层，口径与组装一致。

- **第 38-44 行** docstring：Args/Returns。

- **第 45 行** `return estimate_tokens(text)`
  委托给统一口径函数。不重复实现，避免漂移。

### 第 48-59 行：`CompactionPolicy` 协议

- **第 48 行** `class CompactionPolicy(Protocol):` 压缩协议。

- **第 49 行** docstring：`压缩策略协议：判定是否压缩 + 产压缩视图。`

- **第 51 行** `name: str` 具名标识。

- **第 53 行** `def should_compact(self, state: AgentState) -> bool:` 判定方法签名。

- **第 54 行** docstring。

- **第 57 行** `def compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:` 压缩方法签名。注意入参/返回都是 `tuple`（不可变序列）——呼应「产新不改性」。

- **第 58 行** docstring：`产压缩后的消息视图（新 tuple，不改入参）。`

- **第 59 行** `...` 接口声明。

### 第 62-88 行：`DefaultCompactionPolicy` 类与构造

- **第 62 行** `class DefaultCompactionPolicy:` 默认压缩实现。

- **第 63 行** `"""阈值语义摘要压缩器：钉住 system/最近 N 轮/高价值，其余摘要。"""` 一句话定位。

- **第 65 行** `name: str = "default_compaction"` 具名标识。

- **第 67-74 行** 构造函数签名：五个可配置参数，全部有默认值（来自 types.py 常量），counter 可缺省。
  - `trigger_tokens`: token 占比阈值（0~1）。
  - `trigger_turns`: 轮次阈值。
  - `keep_recent`: 保留最近消息条数。
  - `pin_patterns`: 命中即钉住的内容特征。
  - `counter`: token 计数器。

- **第 75-83 行** docstring：参数说明。第 82 行强调 counter 缺省用 `DefaultTokenCounter`（与 assemble 同口径）。

- **第 84 行** `self._trigger_tokens = trigger_tokens` 保存阈值。

- **第 85 行** `self._trigger_turns = trigger_turns` 保存轮次阈值。

- **第 86 行** `self._keep_recent = keep_recent` 保存保留条数。

- **第 87 行** `self._pin_patterns = pin_patterns` 保存钉住特征。

- **第 88 行** `self._counter = counter if counter is not None else DefaultTokenCounter()`
  **fail-closed 行**：counter 永远存在，传 None 用默认值。

### 第 90-105 行：`should_compact` 方法

- **第 90 行** `def should_compact(self, state: AgentState) -> bool:`
  判定是否超阈值。

- **第 91-98 行** docstring：第 97 行说明「任一维度超阈值返回 True；max_tokens<=0 时该维度不触发」。

- **第 99 行** `max_tokens = state.budgets.max_tokens`
  取预算上限。

- **第 100 行** `# 兼容两种口径：预算已记录的 used_tokens（内核权威）与消息投影估算取较大者。`
  注释说明「取较大者」的兼容性设计。

- **第 101 行** `projected = self._counter.count("".join(m.content for m in state.messages))`
  用统一 counter 估算所有消息正文的总 token 数（消息投影口径）。

- **第 102 行** `used = max(state.budgets.used_tokens, projected)`
  **关键行**：取内核记录的 `used_tokens` 与消息估算的**较大者**。
  - 为什么：兼容两种口径，取大更保守（宁可早压缩，不撑爆窗口）。

- **第 103 行** `token_hit = max_tokens > 0 and used / max_tokens >= self._trigger_tokens`
  token 维度判定：`max_tokens > 0` 防除零（且 0 表示无预算，不该触发）；占比 ≥ 0.7 命中。

- **第 104 行** `turn_hit = state.budgets.used_turns >= self._trigger_turns`
  轮次维度判定：轮次 ≥ 12 命中。

- **第 105 行** `return token_hit or turn_hit`
  两个维度任一命中即 True（或短路）。

### 第 107-134 行：`compact` 方法（核心）

- **第 107 行** `def compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:`
  压缩主方法。

- **第 108-115 行** docstring：第 114 行「无消息可压缩时原样返回新 tuple」。

- **第 116 行** `if not messages:`
  空消息序列。

- **第 117 行** `return ()`
  空序列直接返回空 tuple。

- **第 118 行** `recent_start = max(0, len(messages) - self._keep_recent)`
  计算「最近 N 条」的起始下标。`max(0, ...)` 防消息数不足 N 时为负。

- **第 119 行** `kept_idx: set[int] = set()` 钉住消息的下标集合。

- **第 120 行** `for i, msg in enumerate(messages):` 遍历所有消息（带下标）。

- **第 121 行** `if msg.role == "system" or i >= recent_start or self._is_pinned(msg):`
  **钉住三条件**（任一命中即钉住、保留原位）：
  1. `role == "system"`：system 消息必留。
  2. `i >= recent_start`：最近 N 条消息必留（抗 context rot）。
  3. `self._is_pinned(msg)`：正文命中高价值特征（见第 136-138 行）。

- **第 122 行** `kept_idx.add(i)` 记录钉住下标。

- **第 123 行** `dropped_idx = [i for i in range(len(messages)) if i not in kept_idx]`
  未被钉住的下标 = 可压缩/丢弃的消息。

- **第 124 行** `if not dropped_idx:`
  没有可压缩消息。

- **第 125 行** `return tuple(messages)`
  **产新不改性**：即使内容没变，也返回**新 tuple**（`tuple(messages)`），而不是原对象。这样调用方拿到的永远是独立视图，不会被后续对原序列的操作影响。

- **第 126 行** `summary = self._make_summary([messages[i] for i in dropped_idx])`
  为被丢弃的消息生成一条摘要（见第 140-155 行）。

- **第 127 行** `insert_at = min(dropped_idx)`
  摘要插入位置 = 第一条被丢消息的位置。这样摘要在时序上处于被压缩历史的最前位置。

- **第 128 行** `out: list[Message] = []` 新视图容器。

- **第 129 行** `for i, msg in enumerate(messages):` 重新遍历原消息。

- **第 130 行** `if i == insert_at:`
  到插入点。

- **第 131 行** `out.append(summary)`
  先插入摘要（在第一条被丢消息位置）。

- **第 132 行** `if i in kept_idx:`
  若当前下标是钉住的。

- **第 133 行** `out.append(msg)`
  保留该消息（原位顺序）。

- **第 134 行** `return tuple(out)`
  返回新 tuple。
  - 结果示例：10 条消息，钉住 5 条、丢 5 条 → 输出 5 条保留 + 1 条摘要 = 6 条，摘要在第一条被丢消息的位置。

### 第 136-138 行：`_is_pinned` 方法

- **第 136 行** `def _is_pinned(self, msg: Message) -> bool:`
  高价值判定。

- **第 137 行** docstring：`高价值判定：正文命中 pin_patterns 任一特征。`

- **第 138 行** `return any(p in msg.content for p in self._pin_patterns)`
  生成器 + `any`：任一特征（如 `"system"`、`"待办"`、`"未完成"`）是正文子串即钉住。`any` 短路，命中即停。
  - 不这么写：若不用 `any` 而用循环全查，性能略差且代码冗长；更重要的是漏掉钉住会丢失用户任务线索。

### 第 140-155 行：`_make_summary` 方法

- **第 140 行** `@staticmethod` 静态方法。

- **第 141 行** `def _make_summary(dropped: list[Message]) -> Message:`
  折叠历史为摘要。

- **第 142 行** `"""折叠历史为语义摘要占位（独立 role，防 system 沉淀）。"""`
  强调独立 role 的反沉淀设计。

- **第 143-149 行** docstring：Args/Returns。

- **第 150 行** `roles = [msg.role for msg in dropped]`
  提取被丢消息的 role 列表。

- **第 151 行** `detail = ",".join(roles[:_SUMMARY_ROLE_WINDOW])`
  只取前 5 个 role 拼成可读提示（如 `user,assistant,user,...`），避免摘要过长。

- **第 152-155 行** 构造 `Message`：
  - **第 153 行** `role=SUMMARY_ROLE`：独立 role（`"summary"`），防沉淀的关键。
  - **第 154 行** `content=f"{SUMMARY_PREFIX}{len(dropped)} 条历史 ({detail})"`：内容形如 `"[摘要] 5 条历史 (user,assistant,...)"`。
  - 这是「语义摘要」的当前实现——目前是数量 + 角色分布的占位提示，不是真调用 LLM 摘要。结构已就位，可替换为真实摘要器。

---

## 5. reinjector.py 逐行讲解（88 行）

重注入链：把语义摘要、钉住片段与未完成任务回注下一轮上下文。**必须按预算二次裁剪，30% 上限，不得绕过预算**。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 重注入：把语义摘要、钉住片段与未完成任务回注下一轮上下文。`
  一句话定位：回注三种东西。

- **第 3-4 行** `重注入是 R7 编排内部步骤（无通知点），必须按 budget 二次裁剪，不得绕过预算导致组装后超窗口；产物作为下一轮 assemble 的输入段。`
  强调两点：无通知点（纯内部步骤）+ 必须按预算二次裁剪（防超窗口）。

- **第 5 行** `"""` docstring 结束。

### 第 6-18 行：imports、重导出与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol, runtime_checkable`
  `Protocol` 定义协议；`runtime_checkable` 让 `isinstance` 可校验。

- **第 10 行** `from harness.rag.types import TokenBudget` 预算 DTO。

- **第 11 行** `from runtime import Message` 消息类型。

- **第 13-14 行** 注释：`# 摘要消息角色单一定义源在 compaction.py（指南 §7-11 双源修复）；此处重导出供调用方按同一语义引用，避免两处字面量漂移。`
  交代为什么从 compaction 重导出而不是重新定义——单一定义源，防漂移。

- **第 15 行** `from .compaction import SUMMARY_ROLE`
  **重导出**：摘要角色只有 compaction.py 一处定义（`"summary"`），这里导入后对外暴露。

- **第 16 行** `from .token_counter import DefaultTokenCounter, TokenCounter` 统一口径。

- **第 18 行** `__all__ = ["DefaultReinjector", "Reinjector", "SUMMARY_ROLE"]`
  导出三个符号，含重导出的 `SUMMARY_ROLE`。

### 第 20-23 行：常量

- **第 20 行** `# 钉住消息的角色标记。` 注释。

- **第 21 行** `PINNED_ROLE = "pinned"`
  未完成任务回注时的 role。与 `summary` 区分——任务用 pinned 标记。

- **第 22 行** `# 回注段配额占预算的比例上限（防止重注入挤占主任务）。`
  注释说明动机：不能让回注把主任务的窗口挤没。

- **第 23 行** `_REINJECT_BUDGET_RATIO = 0.3`
  **核心设计行**：回注段最多占总预算 30%。具名常量（规则 1）。

### 第 26-40 行：`Reinjector` 协议

- **第 26 行** `@runtime_checkable` 运行时可校验。

- **第 27 行** `class Reinjector(Protocol):`
  重注入协议。注意它**不进 PORT_PROTOCOLS**——包内组件。

- **第 28 行** docstring：明确「context 包内部协议，不进 PORT_PROTOCOLS」。

- **第 30 行** `name: str` 具名标识。

- **第 32-38 行** `def reinject(self, view, summary, tasks, budget) -> tuple[Message, ...]:`
  协议方法签名：压缩视图 + 摘要 + 任务 + 预算 → 新消息 tuple。

- **第 39 行** docstring：`按预算把摘要+钉住片段+未完成任务回注视图。`

- **第 40 行** `...` 接口声明。

### 第 43-54 行：`DefaultReinjector` 类与构造

- **第 43 行** `class DefaultReinjector:` 默认重注入实现（鸭子类型履约）。

- **第 44 行** `"""默认重注入器：按 token 预算贪心装入，超出丢弃低优先级回注项。"""`
  一句话定位：贪心装入 + 超预算丢低优先级项。

- **第 46 行** `name: str = "default_reinjector"` 具名标识。

- **第 48 行** `def __init__(self, counter: TokenCounter | None = None) -> None:` 构造函数，counter 可缺省。

- **第 49-53 行** docstring：第 52 行说明 None 用 `DefaultTokenCounter`。

- **第 54 行** `self._counter = counter if counter is not None else DefaultTokenCounter()`
  fail-closed 行：counter 永远存在。

### 第 56-88 行：`reinject` 方法（核心）

- **第 56-62 行** 方法签名：四个参数 + 返回新 tuple。

- **第 63-73 行** docstring：第 66 行「view 不被修改」、第 67 行「summary None 表示无摘要」、第 69 行「limit=None 不限」、第 72 行「返回原视图 + 可装入的回注消息」。

- **第 74 行** `cap = int(budget)`
  预算转整数（-1 表示不限，与 assembler 同一语义）。

- **第 75 行** `extra_budget = cap if cap < 0 else int(cap * _REINJECT_BUDGET_RATIO)`
  **核心行**：计算回注专用额度。
  - 不限（cap<0）时：回注额度也不限。
  - 有限时：回注额度 = 总预算 × 0.3（30% 上限）。
  - 不这么写：若用全部 cap 做回注额度，回注会挤占主任务窗口；30% 是设计上的硬约束。

- **第 76 行** `out = list(view)`
  先把原视图拷进新列表（**产新不改性**的起点）。

- **第 77 行** `used = 0` 回注已用计数。

- **第 78 行** `candidates: list[Message] = []` 候选回注项。

- **第 79 行** `if summary is not None:` 有摘要才加入。

- **第 80 行** `candidates.append(Message(role=SUMMARY_ROLE, content=summary.content))`
  摘要作为**第一个候选**（最高优先级）。用独立 role（SUMMARY_ROLE，防沉淀）。

- **第 81 行** `candidates.extend(Message(role=PINNED_ROLE, content=text) for text in tasks)`
  每个未完成任务变成一条 `role="pinned"` 的 Message，追加在摘要之后。
  - 候选顺序：摘要 → 任务。超预算时先砍任务（后加入的），保摘要。

- **第 82 行** `for msg in candidates:` 逐个候选处理。

- **第 83 行** `cost = self._counter.count(msg.content)`
  算候选 token 成本（统一口径）。

- **第 84 行** `if cap >= 0 and used + cost > extra_budget:`
  超回注额度判定。`cap >= 0` 排除不限情形（不限时 extra_budget 也是 -1，不会被这个条件拦）。

- **第 85 行** `continue  # 超预算跳过低优先级回注项，不透支`
  **关键**：超预算直接跳过，不透支。因为 candidates 有序，跳过的总是低优先级项（任务）。

- **第 86 行** `out.append(msg)` 装得下就追加。

- **第 87 行** `used += cost` 累加。

- **第 88 行** `return tuple(out)`
  返回新 tuple：原视图 + 预算内装得下的回注项。
  - 与 assembler 的差异：assembler 超预算会**降级裁剪**（截到剩余额度），reinjector 超预算直接**跳过整项**——因为回注项是可有可无的增强信息，而组装段的正文是主内容。

---

## 6. offloader.py 逐行讲解（103 行）

卸载链的存储层：`BlobPort` 协议 + 内存默认实现 + `Offloader`。句柄形如 `store://<id>`，可按句柄读回。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R7 大结果卸载：BlobPort 协议 + 内存默认实现 + Offloader。`
  一句话定位三个组件。

- **第 3 行** `句柄形如 ``store://<id>``，可按句柄读回（修复原设计误用 StorePort 的语义错位）。`
  说明句柄格式与历史修复（原来误用 StorePort，语义错位）。

- **第 4 行** `BlobPort 是 context 包内部协议（仿 R6 CachePort），不进 PORT_PROTOCOLS。`
  明确不增加 Port 数量。

- **第 5 行** `"""` docstring 结束。

### 第 6-21 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from typing import Protocol` 定义两个协议。

- **第 9 行** `from uuid import uuid4` 生成唯一 blob id。

- **第 11 行** `from runtime import DynamicValue`
  被卸载的值类型（动态值）。

- **第 13-21 行** `__all__`：导出 7 个符号——句柄 scheme 常量、两个协议、两个默认实现、句柄生成/解析函数。

### 第 23-24 行：`BLOB_HANDLE_SCHEME` 常量

- **第 23 行** `# 句柄 scheme（具名常量，禁止裸魔法串）。` 注释。

- **第 24 行** `BLOB_HANDLE_SCHEME = "store://"`
  句柄前缀（scheme）。具名常量，禁止散落裸串。

### 第 27-29 行：`make_handle` 函数

- **第 27 行** `def make_handle(blob_id: str) -> str:`
  id → 句柄。

- **第 28 行** `"""把 blob id 包装为句柄字符串。"""` 说明。

- **第 29 行** `return f"{BLOB_HANDLE_SCHEME}{blob_id}"`
  拼接前缀 + id，如 `"store://abc123"`。
  - 不这么写：若前缀散落在多处，改 scheme 时要改多处，且可能出现两种前缀不兼容。

### 第 32-36 行：`parse_handle` 函数

- **第 32 行** `def parse_handle(handle: str) -> str | None:`
  句柄 → id，解析失败返回 None。

- **第 33 行** `"""从句柄解析 blob id；scheme 不符返回 None。"""` 说明。

- **第 34 行** `if not handle.startswith(BLOB_HANDLE_SCHEME):`
  校验前缀。

- **第 35 行** `return None`
  scheme 不符（如传入 `"http://..."`）返回 None，**fail-closed**——不把非法句柄当有效 id 用。

- **第 36 行** `return handle[len(BLOB_HANDLE_SCHEME) :] or None`
  去掉前缀取 id；若 id 为空串（句柄只有 `"store://"`），`or None` 把它转成 None。
  - 不这么写：若返回空串 `""`，后续 `self._data.get("")` 可能取到错误数据；返回 None 明确表示「无效句柄」。

### 第 39-54 行：`BlobPort` 协议

- **第 39 行** `class BlobPort(Protocol):`
  大结果卸载存储协议。包内协议（仿 R6 CachePort），不进 PORT_PROTOCOLS。

- **第 40 行** `"""大结果卸载存储协议：存单值→返句柄→按句柄读回/删除。"""` 说明三方法。

- **第 42 行** `name: str` 具名标识。

- **第 44 行** `async def put(self, value: DynamicValue) -> str:`
  异步存入，返回句柄。

- **第 45 行** docstring。

- **第 46 行** `...` 接口声明。

- **第 48 行** `async def get(self, handle: str) -> DynamicValue | None:`
  异步按句柄读回，不存在返回 None。

- **第 49-50 行** docstring + `...`。

- **第 52 行** `async def delete(self, handle: str) -> None:`
  异步按句柄删除。

- **第 53 行** docstring：`按句柄删除；不存在幂等无操作。`

- **第 54 行** `...` 接口声明。

### 第 57-64 行：`Offloader` 协议

- **第 57 行** `class Offloader(Protocol):`
  卸载协议（与 BlobPort 分开：BlobPort 是存储，Offloader 是「把值交给存储」的动作）。

- **第 58 行** docstring：`卸载协议：把大值交给 BlobPort 存并返回句柄。`

- **第 60 行** `name: str` 具名标识。

- **第 62 行** `async def offload(self, value, store: BlobPort) -> str:`
  异步方法签名：值 + 存储后端 → 句柄。

- **第 63-64 行** docstring + `...`。

### 第 67-93 行：`InMemoryBlobStore` 类

- **第 67 行** `class InMemoryBlobStore:`
  内存 Blob 存储默认实现。

- **第 68 行** `"""内存 Blob 存储：进程内字典，句柄 uuid 唯一。"""` 一句话定位。

- **第 70 行** `name: str = "in_memory_blob"` 具名标识。

- **第 72 行** `def __init__(self) -> None:` 构造函数。

- **第 73 行** `"""初始化空存储。"""` 说明。

- **第 74 行** `self._data: dict[str, DynamicValue] = {}`
  用进程内 dict 存 `id → 值`。注意：进程重启即丢失（内存实现，适合 demo/测试；生产可换持久化 BlobPort）。

- **第 76 行** `async def put(self, value: DynamicValue) -> str:`
  存入。

- **第 77 行** docstring：`存值并返回 ``store://<uuid>`` 句柄。`

- **第 78 行** `blob_id = uuid4().hex`
  生成 32 位十六进制唯一 id。用 uuid 而非自增：分布式/并发下不冲突。

- **第 79 行** `self._data[blob_id] = value` 存值。

- **第 80 行** `return make_handle(blob_id)` 返回句柄。

- **第 82 行** `async def get(self, handle: str) -> DynamicValue | None:`
  读回。

- **第 83 行** docstring。

- **第 84 行** `blob_id = parse_handle(handle)`
  解析句柄，非法得 None。

- **第 85 行** `if blob_id is None: return None`
  非法句柄直接返回 None（fail-closed）。

- **第 87 行** `return self._data.get(blob_id)`
  dict.get：不存在返回 None（不会 KeyError）。

- **第 89 行** `async def delete(self, handle: str) -> None:`
  删除。

- **第 90 行** docstring：`按句柄删除；不存在幂等。`

- **第 91 行** `blob_id = parse_handle(handle)` 解析。

- **第 92-93 行** `if blob_id is not None: self._data.pop(blob_id, None)`
  `pop(key, None)`：不存在也不报错（**幂等**）。删两次不会第二次抛异常。

### 第 96-103 行：`DefaultOffloader` 类

- **第 96 行** `class DefaultOffloader:` 默认卸载器。

- **第 97 行** `"""默认卸载器：委托任意 BlobPort 存值并回传句柄。"""` 说明。

- **第 99 行** `name: str = "default_offloader"` 具名标识。

- **第 101 行** `async def offload(self, value: DynamicValue, store: BlobPort) -> str:`
  履约方法。

- **第 102 行** docstring。

- **第 103 行** `return await store.put(value)`
  纯委托：把值交给传入的 BlobPort 存，回传句柄。
  - 为什么 `store` 是参数而非内部固定：卸载器与存储解耦，可注入不同后端（内存/磁盘/对象存储）。

---

## 7. offload_policy.py 逐行讲解（68 行）

卸载策略：**超阈值落盘、否则原样返回；截断模式被明确否决**。这是「规格外新增设计决策 D4」。

### 第 1-14 行：模块 docstring（含完整决策说明）

- **第 1 行** `"""R7 新增方案 D4：大 tool 结果落盘而非截断（规格外新增设计决策）。`
  一句话定位：这是新增设计决策 D4。

- **第 3-6 行** `**决策**：工具结果超过阈值时，交给 BlobPort 落盘并回"store://<id>"句柄 + 摘要占位，按需再读；**明确否决"截断"（truncate）**——截断会静默丢失工具结果尾部内容（数据/错误/证据），属不可接受的语义损失，故 `mode="truncate"` 构造即 ValueError，不留兼容层（对齐 D-10 纪律）。`
  **核心设计说明**：落盘保真 vs 截断失真；截断构造即抛错，不留兼容层（fail-closed）。

- **第 8-11 行** `**为何落盘而非截断**：` 三条理由：
  - 落盘保真：原文完整存于 BlobPort，摘要进上下文，模型按需 get 回读。
  - 截断失真：尾部常被截掉的是最新数据/错误堆栈，恰恰最该被看到的部分。
  - 小结果直接返回：低于阈值不落盘，避免无谓句柄开销（句柄本身也占 token）。

- **第 13 行** `阈值是 R7 编排策略，属 ⑥组件；不放进 R1 Budgets（内核零领域语义）。`
  强调职责边界：阈值属于 R7 组件，不污染 R1 内核（内核零领域语义）。

- **第 14 行** `"""` docstring 结束。

### 第 15-23 行：imports 与导出

- **第 15 行** `from __future__ import annotations` 延迟注解。

- **第 17 行** `from dataclasses import dataclass` 数据类装饰器。

- **第 19 行** `from runtime import DynamicValue` 值类型。

- **第 21 行** `from .offloader import BlobPort` 存储协议（apply 的参数类型）。

- **第 23 行** `__all__ = ["DEFAULT_OFFLOAD_THRESHOLD", "OffloadPolicy"]` 导出两个符号。

### 第 26-28 行：`DEFAULT_OFFLOAD_THRESHOLD` 常量

- **第 26 行** `# 默认阈值：8 KiB 字符数（默认 TokenCounter 口径为 Unicode 码点）。`
  注释说明单位是字符数（码点），8 KiB = 8192。

- **第 27 行** `# 具名常量，禁止裸魔法串（编码规则 5）。` 注释。

- **第 28 行** `DEFAULT_OFFLOAD_THRESHOLD = 8192`
  默认阈值 8192 字符。具名常量。

### 第 31-51 行：`OffloadPolicy` 数据类与校验

- **第 31 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots，同 `ContextSection` 的理由（策略对象不可被运行期篡改）。

- **第 32 行** `class OffloadPolicy:` 策略类。

- **第 33-38 行** docstring：第 37 行强调 mode 唯一合法值 `"offload"`，`"truncate"` 等一律拒绝（fail-closed）。

- **第 39 行** `threshold: int = DEFAULT_OFFLOAD_THRESHOLD` 阈值字段，默认 8192。

- **第 40 行** `mode: str = "offload"` 模式字段，默认 `"offload"`。

- **第 42 行** `def __post_init__(self) -> None:`
  dataclass 构造后钩子——在对象诞生瞬间做校验。

- **第 43 行** `"""校验阈值与模式（装配期 fail-closed，不把非法策略带进运行期）。"""`
  **关键**：校验在装配期（构造时）完成，不是等到运行时才发现非法策略。

- **第 44 行** `if self.threshold <= 0:`
  阈值必须为正。

- **第 45 行** `raise ValueError(f"offload threshold must be positive, got {self.threshold}")`
  非正阈值抛错。0 或负阈值会让 `should_offload` 对所有值都返回 True，失去意义。

- **第 46 行** `if self.mode != "offload":`
  模式必须是 `"offload"`。

- **第 47 行** `# 截断是被明确否决的方案（会静默丢数据），不给兼容开关。` 注释强调否决态度。

- **第 48-51 行** `raise ValueError(f"mode={self.mode!r} rejected: truncation silently drops tool-result tail (data/errors/evidence); use offload-to-blob instead")`
  **fail-closed 核心行**：任何非 `"offload"` 的模式（尤其是 `"truncate"`）构造即抛错，错误信息说明原因（截断会静默丢尾部数据/错误/证据）。
  - 不这么写：若留 `mode="truncate"` 兼容分支，调用方可能误用它，导致工具结果尾部数据被静默丢弃且无人察觉。这里选择「构造即炸」把问题暴露在源头。

### 第 53-55 行：`should_offload` 方法

- **第 53 行** `def should_offload(self, value: DynamicValue) -> bool:`
  判定是否需要落盘。

- **第 54 行** docstring：`是否超过阈值需要落盘（纯函数，无副作用）。`

- **第 55 行** `return len(str(value)) > self.threshold`
  把值转字符串算长度，超阈值即 True。
  - 注意口径：`str(value)` 的长度是字符数（码点），与默认 TokenCounter 一致。
  - 不这么写：若用 `len(value)` 而 value 是 dict/list，长度是元素个数而非文本大小，判断会失真。

### 第 57-68 行：`apply` 方法

- **第 57 行** `async def apply(self, value: DynamicValue, store: BlobPort) -> DynamicValue:`
  应用策略（异步，因为落盘是异步 IO）。

- **第 58-65 行** docstring：第 64 行强调「大值 → 句柄；小值 → 原值。绝不截断」。

- **第 66 行** `if not self.should_offload(value):`
  未超阈值。

- **第 67 行** `return value`
  小值原样返回（避免无谓句柄开销）。

- **第 68 行** `return await store.put(value)`
  大值落盘，返回 `store://` 句柄。
  - 不这么写：若这里做 `value[:threshold]` 截断，就会静默丢失尾部数据——这正是本文件明确否决的方案。

---

## 8. observability.py 逐行讲解（61 行）

KV cache 命中率作为一等可观测指标。这是「新增方案 D5」。

### 第 1-14 行：模块 docstring

- **第 1 行** `"""R7 新增方案 D5：KV cache 命中率作为一等可观测指标。`
  一句话定位：新增设计决策 D5。

- **第 3-5 行** `**决策**：KV cache 的 hit/miss 不是零散 trace 字符串，而是具名 DTO（``CacheTelemetry``），提供一等字段 ``hit_rate``（hits / total），可进 R10 trace、可按维度标签归因（如 "llm:primary"、"prefix:system"）。`
  说明决策：具名 DTO + 一等字段 + 可归因。

- **第 7-11 行** `**为何是一等**：` 四条理由：
  - 可断言：结构化字段可被单测/门禁直接断言，不是「从日志里 grep」。
  - 可归因：label 维度区分不同模型/前缀的命中率，定位缓存失效根因。
  - 安全默认：无访问时 hit_rate=0.0（不是 NaN/None），trace 序列化不炸。
  - 纯函数：record_hit/record_miss 返回新实例，可重放（对齐 R7 §12.5 可重放纪律）。

- **第 13 行** `本模块是 ⑥组件（观测数据载体），不进 PORT_PROTOCOLS（Port 数量不变）。`
  强调不增加 Port 数量。

- **第 14 行** `"""` docstring 结束。

### 第 15-19 行：imports 与导出

- **第 15 行** `from __future__ import annotations` 延迟注解。

- **第 17 行** `from dataclasses import dataclass` 数据类装饰器。

- **第 19 行** `__all__ = ["CacheTelemetry"]` 只导出一个符号。

### 第 22-32 行：`CacheTelemetry` 数据类与字段

- **第 22 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots。不可变是「纯函数、可重放」的基础——计数更新返回新实例而非改原对象。

- **第 23 行** `class CacheTelemetry:` 观测 DTO。

- **第 24-29 行** docstring：第 28 行说明 label 缺省 `"default"` 便于归因。

- **第 30 行** `hits: int = 0` 命中次数，默认 0。

- **第 31 行** `misses: int = 0` 未命中次数，默认 0。

- **第 32 行** `label: str = "default"` 维度标签，默认 `"default"`。

### 第 34-37 行：`__post_init__` 校验

- **第 34 行** `def __post_init__(self) -> None:`
  构造后钩子。

- **第 35 行** `"""计数不得为负（fail-closed）。"""` 说明。

- **第 36 行** `if self.hits < 0 or self.misses < 0:`
  任一计数为负。

- **第 37 行** `raise ValueError("cache hit/miss counts must be non-negative")`
  抛错。负计数会让 `hit_rate` 算出无意义值（如 -0.5 或 >1），装配期就拦截。

### 第 39-42 行：`total` 属性

- **第 39 行** `@property` 只读属性。

- **第 40 行** `def total(self) -> int:` 总访问次数。

- **第 41 行** `"""总访问次数（hits + misses）。"""` 说明。

- **第 42 行** `return self.hits + self.misses`
  命中 + 未命中。

### 第 44-49 行：`hit_rate` 属性

- **第 44 行** `@property` 只读属性。

- **第 45 行** `def hit_rate(self) -> float:` 命中率。

- **第 46 行** `"""命中率（hits / total）；无访问时为 0.0，绝不产生 NaN/None。"""`
  **关键设计**：无访问返回 0.0，不是 NaN/None。

- **第 47 行** `if self.total == 0:`
  总访问为 0。

- **第 48 行** `return 0.0`
  **安全默认**：返回 0.0。
  - 不这么写：若直接 `hits / total` 会因除零抛 `ZeroDivisionError`；若返回 `NaN`，JSON 序列化（`json.dumps`）会产生非法 JSON `NaN`，trace 落盘/传输时炸。

- **第 49 行** `return self.hits / self.total`
  正常命中率。

### 第 51-53 行：`record_hit` 方法

- **第 51 行** `def record_hit(self) -> CacheTelemetry:`
  记录一次命中。

- **第 52 行** `"""记录一次命中，返回新实例（纯函数，不改原对象）。"""`
  强调返回新实例。

- **第 53 行** `return CacheTelemetry(hits=self.hits + 1, misses=self.misses, label=self.label)`
  构造新实例（hits+1，其余不变）。
  - 不这么写：若 `self.hits += 1` 改原对象，会破坏 frozen 约束（且不可重放——同一历史状态被就地改写，无法回放）。

### 第 55-57 行：`record_miss` 方法

- **第 55 行** `def record_miss(self) -> CacheTelemetry:` 记录未命中。

- **第 56 行** docstring。

- **第 57 行** `return CacheTelemetry(hits=self.hits, misses=self.misses + 1, label=self.label)`
  构造新实例（misses+1）。

### 第 59-61 行：`with_label` 方法

- **第 59 行** `def with_label(self, label: str) -> CacheTelemetry:`
  换维度标签。

- **第 60 行** docstring：`换维度标签，返回新实例（保留计数）。`

- **第 61 行** `return CacheTelemetry(hits=self.hits, misses=self.misses, label=label)`
  构造新实例（只换 label，计数不变）。
  - 用途：manager.py 的 `record_cache_hit(label)` 先 `with_label(label)` 切维度，再 `record_hit()`，实现按模型/前缀归因。

---

## 9. manager.py 逐行讲解（227 行）

这是整个模块的**调度中心**。记住一句话：**7 个依赖显式注入，顺序与预算由代码结构保证**——不是 hook，不是订阅通知。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R7 显式编排器：统一装配入口（7 依赖注入），顺序由代码结构保证。`
  一句话定位：显式编排 + 7 依赖注入 + 顺序由代码保证。

- **第 3-4 行** `commit_and_compact() 方法体内固定"先 write_cold 全文，再 compact 产视图"，随后经 Reinjector 把摘要+钉住+未完成任务按预算回注（不绕过预算）。`
  把核心方法的三步固定顺序写进 docstring——这是本模块最重要的契约。

- **第 5 行** `trace 仅记 write_cold/compact 两个顺序事件（兼容既有断言）；`
  说明 trace 只记两个事件，且是为兼容既有断言（不随意增加事件名）。

- **第 6 行** `with_ 不可变替换时保留 trace 观测历史。通知点纯通知，副作用在订阅者内经本类方法完成。`
  说明 with_ 保留观测历史，以及通知点无副作用的定位。

- **第 7 行** `"""` docstring 结束。

### 第 8-24 行：imports 与导出

- **第 8 行** `from __future__ import annotations` 延迟注解。

- **第 10 行** `from compose import PortCtx` Port 上下文类型。

- **第 11 行** `from compose.tool_layer import layer_program`
  ToolLayer 外壳工厂，用于 `build()`。

- **第 12 行** `from harness.memory import MemoryStore`
  长期记忆存储（07 册）。

- **第 13 行** `from harness.rag.types import TokenBudget` 预算 DTO。

- **第 14 行** `from runtime import AgentState, Message, Program`
  状态、消息、程序类型。

- **第 16 行** `from .assembler import ContextAssembler, DefaultContextAssembler` 组装链。

- **第 17 行** `from .compaction import SUMMARY_ROLE, CompactionPolicy, DefaultCompactionPolicy`
  压缩链 + 摘要角色常量（用于从压缩视图里找摘要）。

- **第 18 行** `from .observability import CacheTelemetry` KV cache 观测 DTO。

- **第 19 行** `from .offloader import BlobPort, DefaultOffloader, InMemoryBlobStore, Offloader` 卸载链。

- **第 20 行** `from .reinjector import DefaultReinjector, Reinjector` 重注入链。

- **第 21 行** `from .token_counter import DefaultTokenCounter, TokenCounter` 统一口径。

- **第 22 行** `from .types import ContextSection` 段 DTO。

- **第 24 行** `__all__ = ["ContextMemoryManager"]`
  包级只导出编排器这一个符号——它是 context 模块的统一门面。

### 第 26-30 行：常量

- **第 26 行** `# trace 事件名（具名常量；仅两项，保持顺序断言稳定）。`
  注释说明：只两项，为保持既有顺序断言稳定。

- **第 27 行** `_TRACE_WRITE_COLD = "write_cold"` 写 cold 事件名。

- **第 28 行** `_TRACE_COMPACT = "compact"` 压缩事件名。

- **第 29 行** `# 默认重注入预算占总预算比例（传给 Reinjector 的 TokenBudget 由调用方传入）。`
  注释说明默认任务常量用途。

- **第 30 行** `_DEFAULT_REINJECT_TASKS: tuple[str, ...] = ()`
  `tasks` 参数默认值：空 tuple。用不可变 tuple 作默认参数（避免可变默认值陷阱）。

### 第 33-81 行：`ContextMemoryManager` 类与构造函数

- **第 33 行** `class ContextMemoryManager:` 编排器类。

- **第 34 行** `"""R7 统一编排器：7 依赖显式注入，顺序与预算都由代码保证。"""` 一句话定位。

- **第 36-45 行** 构造函数签名：7 个可缺省依赖——
  `assembler` / `compaction` / `offloader` / `memory` / `blob` / `reinjector` / `token_counter`。
  - 全部可缺省：开箱即用（用默认实现），也可逐个替换（可插拔）。

- **第 46-56 行** docstring：7 个参数说明。第 54-55 行标注 `reinjector` 和 `token_counter` 是 FR-11 install 入口。

- **第 57-59 行** `self.token_counter = (token_counter if token_counter is not None else DefaultTokenCounter())`
  **关键：counter 先确定**。它是后面三个组件的共享依赖，必须先定下来。

- **第 60 行** `# 统一口径下传：注入的 counter 必须传给三个消费者（FR-11 install 四要素）。`
  **核心设计注释**：「统一口径下传」——同一个 counter 传给三个消费者。

- **第 61-65 行** `self.assembler = (assembler if assembler is not None else DefaultContextAssembler(counter=self.token_counter))`
  assembler 缺省时用**注入的 self.token_counter** 构造，而不是 `new` 一个新的。
  - 不这么写：若各处 `new DefaultTokenCounter()`，虽然默认口径相同，但替换 counter 时无法贯穿，失去统一口径能力。

- **第 66-70 行** `self.compaction = (compaction if compaction is not None else DefaultCompactionPolicy(counter=self.token_counter))`
  压缩器同样用共享 counter。

- **第 71 行** `self.offloader = offloader if offloader is not None else DefaultOffloader()`
  卸载器缺省实现。无 counter 依赖。

- **第 72 行** `self.memory = memory if memory is not None else MemoryStore()`
  记忆存储缺省 `MemoryStore()`。

- **第 73 行** `self.blob = blob if blob is not None else InMemoryBlobStore()`
  卸载后端缺省内存实现。

- **第 74-78 行** `self.reinjector = (reinjector if reinjector is not None else DefaultReinjector(counter=self.token_counter))`
  重注入器同样用共享 counter。**三个消费者（assembler/compaction/reinjector）口径就此统一**。

- **第 79 行** `self.trace: list[str] = []`
  顺序事件轨迹（只记 write_cold/compact）。

- **第 80 行** `# R7 新增方案 D5：KV cache 命中率一等观测面（可进 R10 trace、可按 label 归因）。`
  注释说明观测面用途。

- **第 81 行** `self.cache_telemetry = CacheTelemetry()`
  初始化空观测指标（hits=0, misses=0, label="default"）。

### 第 83-89 行：KV cache 记录方法

- **第 83 行** `def record_cache_hit(self, label: str = "default") -> None:`
  记录命中，label 可指定维度。

- **第 84 行** `"""记录一次 KV cache 命中（一等观测，纯替换不可变指标）。"""`
  说明「纯替换」——不可变 DTO 只能整体替换。

- **第 85 行** `self.cache_telemetry = self.cache_telemetry.with_label(label).record_hit()`
  两步链式：先切到指定 label 维度（`with_label`），再记一次命中（`record_hit`），结果整体替换回实例属性。
  - 不这么写：若 DTO 可变，就地 `self.cache_telemetry.hits += 1` 会破坏不可变/可重放纪律。

- **第 87 行** `def record_cache_miss(self, label: str = "default") -> None:`
  记录未命中。

- **第 88 行** docstring。

- **第 89 行** `self.cache_telemetry = self.cache_telemetry.with_label(label).record_miss()`
  同上，记录未命中。

### 第 91-97 行：`build` 方法

- **第 91 行** `def build(self) -> Program:`
  产出 Program 外壳。

- **第 92-96 行** docstring：第 94-95 行说明内部四链保持显式编排不变；外壳只给 R1 领域无知的 Program；上下文四链属 ⑥组件 + ④notify，不是算子。

- **第 97 行** `return layer_program("memory", payload={"layer": "context"})()`
  **关键行**：产出一个「用上下文记忆」的 Program 工厂。
  - 注意 port 名叫 `"memory"`（**不是** `"context"`）——外壳复用 memory 这个 ToolLayer 入口，payload 里标 `"layer": "context"` 区分。
  - 内部四链仍是组件调用，不是算子——外壳只是给 R1 一个无知的外壳。

### 第 99-111 行：`assemble` 方法

- **第 99-101 行** 方法签名：`assemble(sections, budget) -> list[Message]`。

- **第 102-110 行** docstring：第 102 行「显式组装上下文（替代原 before_model hook）」。

- **第 111 行** `return self.assembler.assemble(sections, budget)`
  纯委托给 assembler。编排器只做门面，不重复实现组装逻辑。

### 第 113-147 行：`commit_and_compact` 方法（最核心）

- **第 113-120 行** 方法签名：`state` / `transcript` / `ctx=None` / `budget=None` / `tasks=_DEFAULT_REINJECT_TASKS`。

- **第 121-131 行** docstring：
  - 第 121 行：`提交会话：先写 cold 全文→压缩→按预算重注入（顺序由代码固定）。`
  - 第 124 行：`state` 只读，不改 messages。
  - 第 125 行：`transcript` 本轮完整对话（写 cold 全文，避免细节丢失）。
  - 第 127 行：`budget` None 时不限（**注意 docstring 这么写，但实现强制回退有限值，见第 142-145 行**——这是 docstring 与实现的细微不一致，以实现为准）。
  - 第 131 行：返回新 tuple，State.messages 不变。

- **第 133 行** `port_ctx = ctx if ctx is not None else PortCtx()`
  ctx 缺省用空 `PortCtx()`（仅 demo/测试）。

- **第 134 行** `# ① 先写 cold 全文（细节必须先落盘才能提炼）。`
  **步骤①注释**：先落盘全文。这是关键顺序——细节必须先落盘才能提炼，否则压缩丢掉的细节永久丢失。

- **第 135 行** `self.memory.write_cold(transcript, port_ctx)`
  把完整 transcript 写进 cold 层（07 册讲细节）。

- **第 136 行** `self.trace.append(_TRACE_WRITE_COLD)`
  记 `write_cold` 事件。

- **第 137 行** `# ② 再压缩产视图（不改 state.messages）。`
  步骤②注释。

- **第 138 行** `view = self.compaction.compact(state.messages)`
  压缩产视图。**不改 state.messages**（内核只增不减）。

- **第 139 行** `self.trace.append(_TRACE_COMPACT)`
  记 `compact` 事件。

- **第 140 行** `# ③ 重注入：语义摘要 + 钉住 + 未完成任务，按预算二次裁剪。`
  步骤③注释。

- **第 141 行** `summary = next((m for m in view if m.role == SUMMARY_ROLE), None)`
  从压缩视图里找 `role == "summary"` 的摘要消息，找不到返回 None。
  - 用 `next(..., None)` 而非循环：简洁且安全（无摘要时给 None，reinjector 会跳过）。

- **第 142 行** `# 默认预算取 state.max_tokens（有限）；显式 None 也强制回退有限值，禁止无限绕过裁剪。`
  **关键设计注释**：fail-closed——不允许「不限预算」绕过裁剪。

- **第 143 行** `cap = budget.limit if budget is not None else None`
  取传入预算的 limit；没传则暂为 None。

- **第 144-145 行** `if cap is None: cap = state.budgets.max_tokens`
  **fail-closed 核心行**：cap 为 None（没传 budget 或显式 None）时，强制回退到 `state.budgets.max_tokens`（有限值）。
  - 不这么写：若允许 cap 为 None 传入 reinjector，`int(TokenBudget(limit=None))` = -1（不限），回注就不会裁剪，可能撑爆窗口。这里堵死「无限绕过」。

- **第 146 行** `effective = TokenBudget(limit=cap)`
  用确定的 cap 造一个新预算对象。

- **第 147 行** `return self.reinjector.reinject(view, summary, tasks, effective)`
  调重注入器，返回压缩并重注入后的新 tuple。

### 第 149-166 行：`recall` 方法

- **第 149 行** `async def recall(self, query: object, ctx: PortCtx | None = None) -> list[object]:`
  显式语义召回（替代原 before_model 记忆注入 hook）。

- **第 150-161 行** docstring：第 153 行「query 必须是 MemoryQuery 实例（运行时校验类型）」、第 160 行「Raises: TypeError」。

- **第 162 行** `from harness.memory.types import MemoryQuery`
  函数内导入（延迟导入，避免顶部循环依赖或减轻启动开销）。

- **第 164 行** `if not isinstance(query, MemoryQuery):`
  运行时类型校验。

- **第 165 行** `raise TypeError("recall expects MemoryQuery")`
  **fail-closed**：类型不符抛 TypeError，不静默接受错误类型。
  - 不这么写：若直接把任意 object 传给 `memory.recall`，内部按 `query.text` 等属性访问时会在更深处抛 `AttributeError`，难以定位。

- **第 166 行** `return list(await self.memory.recall(query, ctx if ctx is not None else PortCtx()))`
  委托 memory.recall，结果转 list（协议返回 list，这里保险转换）。

### 第 168-178 行：`offload` 方法

- **第 168 行** `async def offload(self, value: object, ctx: PortCtx | None = None) -> str:`
  显式大结果卸载（替代原 after_tool hook）。

- **第 169-177 行** docstring：第 173 行说明 `ctx` 保留签名兼容、当前未消费。

- **第 178 行** `return await self.offloader.offload(value, self.blob)`
  委托 offloader，用内置的 `self.blob` 作为存储后端，返回 `store://` 句柄。

### 第 180-227 行：`with_` 方法（不可变替换）

- **第 180-189 行** 方法签名：与构造函数一样的 7 个可缺省依赖。

- **第 190-203 行** docstring：第 190 行「产新实例替换指定依赖（不可变风格），并保留 trace 观测历史」；第 202 行「返回新实例；当前实例不变」。

- **第 204-214 行** `new = ContextMemoryManager(...)`：用「传入值优先，否则沿用 self 的」构造新实例：
  - **第 205 行** `assembler=assembler if assembler is not None else self.assembler`
  - **第 206 行** `compaction=...else self.compaction`
  - **第 207 行** `offloader=...else self.offloader`
  - **第 208 行** `memory=...else self.memory`
  - **第 209 行** `blob=...else self.blob`
  - **第 210 行** `reinjector=...else self.reinjector`
  - **第 211-213 行** `token_counter=token_counter if token_counter is not None else self.token_counter`
  - 不这么写：若就地改 self 的属性，会破坏不可变风格——已有对旧实例的引用会看到被改后的状态，难以推理。

- **第 215 行** `# 换 counter 时同步重建仍为默认的三个消费者，避免注入失效。`
  **关键设计注释**：换 counter 要同步重建三个默认消费者。

- **第 216 行** `if token_counter is not None:`
  只在确实换了 counter 时处理。

- **第 217-218 行** `if assembler is None and isinstance(self.assembler, DefaultContextAssembler): new.assembler = DefaultContextAssembler(counter=token_counter)`
  若调用方没显式替换 assembler、且当前 assembler 是默认实现 → 用新 counter 重建它。
  - **为什么加 `assembler is None` 条件**：若调用方同时传了新的 assembler，应尊重调用方的选择，不用 counter 覆盖它。
  - **为什么加 `isinstance(..., DefaultContextAssembler)` 条件**：若当前是自定义 assembler（非默认），不能假设它的构造签名接受 counter，重建会破坏它。只对默认实现重建。
  - 不这么写：若只换 counter 不重建消费者，会出现「manager.token_counter 是新的，但 assembler 内部还是旧 counter」的分裂状态——这正是「统一口径下传」要避免的。

- **第 219-222 行** compaction 同理：`if compaction is None and isinstance(self.compaction, DefaultCompactionPolicy): new.compaction = DefaultCompactionPolicy(counter=token_counter)`。

- **第 223-224 行** reinjector 同理：`if reinjector is None and isinstance(self.reinjector, DefaultReinjector): new.reinjector = DefaultReinjector(counter=token_counter)`。

- **第 225 行** `new.trace = list(self.trace)`
  拷贝 trace 历史（**拷贝而非共享**——list 可变，共享会让新旧实例互相影响）。

- **第 226 行** `new.cache_telemetry = self.cache_telemetry  # 保留 KV cache 一等观测历史（不可变 DTO 直接共享）`
  cache_telemetry 是**不可变 DTO，直接共享**（与 trace 的可变拷贝形成对比，注释点明原因）。

- **第 227 行** `return new`
  返回新实例。

---

## 10. ports.py 逐行讲解（24 行）

R7 扩展协议：在基础 `MemoryPort` 之上加语义召回能力。**不反向污染 R2 的 compose 层**。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R7 Port 扩展协议（规格 P8）：MemoryPortWithRecall 承载语义召回。`
  一句话定位：P8 规格的扩展协议。

- **第 3-5 行** `recall 从 R2 基础 Port（hooks.MemoryPort）归位到 R7 能力层扩展协议：基础 Port 只声明 read/write/forget 的通用记忆能力；语义召回（按查询返回相关项）属 R7 领域语义，不该反向污染 R2。DTO（MemoryQuery/MemoryItem）由 R7 自有类型承载。`
  **关键设计说明**：职责分层——基础能力在 R2，领域语义在 R7；DTO 也由 R7 自有类型承载，不污染 R2。

- **第 6 行** `"""` docstring 结束。

### 第 7-15 行：imports 与导出

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `from typing import Protocol, runtime_checkable`
  `Protocol` 定义扩展协议；`runtime_checkable` 让 `isinstance` 可校验。

- **第 11 行** `from compose import MemoryPort, PortCtx`
  继承 R2 的基础 `MemoryPort` + Port 上下文。

- **第 13 行** `from harness.memory.types import MemoryItem, MemoryQuery`
  R7 自有的 DTO（查询与召回项）。

- **第 15 行** `__all__ = ["MemoryPortWithRecall"]` 只导出一个符号。

### 第 18-24 行：`MemoryPortWithRecall` 协议

- **第 18 行** `@runtime_checkable` 运行时可校验（装配期 `isinstance(backend, MemoryPortWithRecall)`）。

- **第 19 行** `class MemoryPortWithRecall(MemoryPort, Protocol):`
  **关键**：同时继承 `MemoryPort`（R2 基础能力）和 `Protocol`（标记这是协议）。
  - 含义：实现这个协议的对象必须既有基础 read/write/forget，又有下面的 recall。
  - 不这么写：若不继承基础 `MemoryPort`，扩展协议就丢了基础能力契约，装配期无法保证后端同时具备两类能力。

- **第 20 行** `"""R7 扩展：实现 MemoryPort 且支持语义召回的后端。"""` 一句话定位。

- **第 22 行** `async def recall(self, query: MemoryQuery, ctx: PortCtx) -> list[MemoryItem]:`
  新增方法签名：查询 + 上下文 → 召回项列表。异步。

- **第 23 行** `"""语义召回：按查询返回相关记忆项。"""` 说明。

- **第 24 行** `...` 接口声明。

---

## 11. __init__.py 逐行讲解（64 行）

包级 API 汇总：把 11 个文件里的公共符号收敛到一处，外部只需 `from harness.context import X`。

### 第 1-4 行：模块 docstring

- **第 1 行** `"""R7 上下文工程：分段组装、阈值压缩、重注入、大结果卸载与显式编排。`
  一句话列出模块五大能力。

- **第 3 行** `公共 API 见 docs/02-模块详设/Agent/R7-上下文与记忆.md。`
  指路：详设文档在哪。

- **第 4 行** `"""` docstring 结束。

### 第 5-32 行：imports 汇总

- **第 5 行** `from __future__ import annotations` 延迟注解。

- **第 7 行** `from .assembler import ContextAssembler, DefaultContextAssembler, SYSTEM_SECTION_NAME`
  从 assembler 导入协议、默认实现、system 常量（该常量在 assembler 里被重导出）。

- **第 8 行** `from .compaction import CompactionPolicy, DefaultCompactionPolicy, count_tokens`
  压缩链三个符号。

- **第 9 行** `from .manager import ContextMemoryManager`
  编排器（门面）。

- **第 10 行** `from .observability import CacheTelemetry` 观测 DTO。

- **第 11 行** `from .offload_policy import DEFAULT_OFFLOAD_THRESHOLD, OffloadPolicy` 卸载策略。

- **第 12-20 行** `from .offloader import (BLOB_HANDLE_SCHEME, BlobPort, DefaultOffloader, InMemoryBlobStore, Offloader, make_handle, parse_handle)`
  卸载链 7 个符号（多行括号导入）。

- **第 21 行** `from .reinjector import DefaultReinjector, Reinjector` 重注入链。

- **第 22 行** `from .token_counter import DefaultTokenCounter, TokenCounter, estimate_tokens` 统一口径三符号。

- **第 23 行** `# 记忆 DTO 重导出：供 ContextMemoryManager 消费者与门禁符号检查可见（源仍在 harness.memory）。`
  注释说明重导出的目的与源头。

- **第 24 行** `from harness.memory import MemoryScope, ScopeResolver, make_item_id`
  **跨模块重导出**：记忆 DTO 源在 `harness.memory`，这里重导出让消费者与门禁检查可见。

- **第 25-32 行** `from .types import (DEFAULT_KEEP_RECENT, DEFAULT_PIN_PATTERNS, DEFAULT_TRIGGER_TURNS, TRIGGER_RATIO, ContextSection, TrimPolicy)`
  types.py 的 6 个符号（4 常量 + DTO + 枚举）。

### 第 34-64 行：`__all__` 公共导出面

- **第 34-64 行** `__all__ = [...]`
  声明 31 个公共符号，分组如下：
  - **types（6 个）**：`ContextSection`、`TrimPolicy`、`TRIGGER_RATIO`、`DEFAULT_TRIGGER_TURNS`、`DEFAULT_KEEP_RECENT`、`DEFAULT_PIN_PATTERNS`（第 35-40 行）。
  - **assembler（3 个）**：`ContextAssembler`、`DefaultContextAssembler`、`SYSTEM_SECTION_NAME`（第 41-43 行）。
  - **compaction（3 个）**：`CompactionPolicy`、`DefaultCompactionPolicy`、`count_tokens`（第 44-46 行）。
  - **reinjector（2 个）**：`Reinjector`、`DefaultReinjector`（第 47-48 行）。
  - **token_counter（3 个）**：`TokenCounter`、`DefaultTokenCounter`、`estimate_tokens`（第 49-51 行）。
  - **memory DTO（3 个）**：`MemoryScope`、`ScopeResolver`、`make_item_id`（第 52-54 行）。
  - **manager（1 个）**：`ContextMemoryManager`（第 55 行）。
  - **observability + offload_policy（3 个）**：`CacheTelemetry`、`OffloadPolicy`、`DEFAULT_OFFLOAD_THRESHOLD`（第 56 行，写在同一行）。
  - **offloader（7 个）**：`BlobPort`、`Offloader`、`InMemoryBlobStore`、`DefaultOffloader`、`BLOB_HANDLE_SCHEME`、`make_handle`、`parse_handle`（第 57-63 行）。

  **为什么集中导出**：外部（如 manager 的消费者、门禁符号检查、其他模块）只需 `from harness.context import X`，不必知道 X 定义在哪个内部文件。这是「包级 API 收敛」纪律。
  - 注意：`MemoryPortWithRecall`（ports.py）**没有**出现在这个 `__all__` 里——它是独立协议，由需要它的地方直接 `from harness.context.ports import ...`。

---

## 12. 逐行讲解后的整体心智图

```
                    ContextMemoryManager（manager.py，7 依赖显式注入）
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   commit_and_compact    assemble()            with_()（不可变替换）
        │                     │                     │
   ①write_cold           assembler.assemble    换 counter 时重建
   ②compact              （组装链）             3 个默认消费者
   ③reinject                  │
        │                     │
   ┌────┴────┐                ├─ 排序(priority 升序)
   │         │                ├─ _dedup_system（只留先到者）
compaction  reinjector       ├─ _trim（HEAD/PIN 头部，TAIL/SUMMARY 尾部）
（压缩链）   （重注入链）       └─ 贪心装入 + system 限长 50%
   │              │
   ├ should_compact          ├ candidates: 摘要优先 → 任务次之
   │  (ratio≥0.7 或 turns≥12)├ extra_budget = cap × 0.3（30% 上限）
   └ compact                 └ 超预算跳过（不裁剪整项）
      ├ 钉住: system/最近4/命中pin_patterns
      └ 其余 → role="summary" 摘要（防沉淀）

   统一口径：estimate_tokens = len(text)（Unicode 码点）
        └─ 同一个 TokenCounter 传给 assembler/compaction/reinjector

   卸载链：OffloadPolicy.should_offload(len>8192)
        └─ apply → store.put → "store://<uuid>" 句柄（截断被否决）

   观测：CacheTelemetry（frozen DTO）
        └─ hit_rate 无访问返回 0.0；record_* 返回新实例
```

### 一次 `commit_and_compact` 的完整时序（把四链串起来）

假设 `State.messages` 有 10 条、其中 2 条含「待办」，本轮 transcript 是一段长对话，`budget.limit=1000`：

1. `write_cold`：把完整 transcript 写进 cold 层，trace += `write_cold`。
2. 压缩判定（若单独调用 should_compact）：假设 `used_tokens/max_tokens = 0.75 ≥ 0.7` → 该压缩。
3. `compact`：保留 system(1) + 最近 4 条 + 含「待办」2 条 = 7 条钉住，丢 3 条，生成一条 `role="summary"` 摘要插到第一条被丢消息位置，输出 8 条。trace += `compact`。
4. 从视图找到摘要，算 `cap=1000`（传了 budget 用它），`TokenBudget(limit=1000)`。
5. `reinject`：`extra_budget = 300`。摘要 cost=30 装下；两个未完成任务 cost 各 50/100 都装下（总 180 < 300），追加为 `role="pinned"` 消息。
6. 返回 8 + 3 = 11 条消息的新 tuple。`State.messages` 原封不动。

---

## 13. 细节速记卡（逐行精华）

- **四链靠代码顺序编排**，不是 hook——`commit_and_compact` 方法体写死「先 write_cold，再 compact，再 reinject」（manager.py 第 135-147 行）。
- **统一 token 口径**：`estimate_tokens = len(text)`，Unicode 码点数，中文不按字节折算（token_counter.py 第 33 行）。
- **统一口径下传**：同一个 counter 传给 assembler/compaction/reinjector 三个消费者（manager.py 第 57-78 行）；with_ 换 counter 会重建仍为默认的三个消费者（第 216-224 行）。
- **system 段三种特殊待遇**：priority 最小排最前、同优先级只留先到者（防覆盖注入）、配额上限是总预算 50%（assembler.py 第 56、94-102、89 行）。
- **裁剪是降级不是丢弃**：非 system 段超预算时截到剩余额度，只有裁完为空才跳过（assembler.py 第 68-72 行）。
- **TAIL 是默认裁剪策略**：保留尾部，抗 context rot（assembler.py 第 115 行）。
- **PIN 与 HEAD 同分支**：都取头部（assembler.py 第 112-113 行）。
- **降级裁剪 vs 整项跳过**：assembler 超预算降级裁剪（保主内容），reinjector 超预算整项跳过（回注是增强项）——两者策略不同（assembler.py 第 70 行 vs reinjector.py 第 85 行）。
- **摘要用独立 role**：`role="summary"`，避免被下一轮按 `role=system` 永久钉住（compaction.py 第 29 行）。
- **SUMMARY_ROLE 单一定义源**：定义在 compaction.py，reinjector 重导出，防字面量漂移（reinjector.py 第 15 行）。
- **钉住三条件**：system 角色 / 最近 N 条 / 正文命中 pin_patterns（compaction.py 第 121 行）。
- **压缩取两个 used 口径的较大值**：内核 `used_tokens` 与消息估算，取大更保守（compaction.py 第 102 行）。
- **max_tokens≤0 不触发 token 维度**：防除零（compaction.py 第 103 行）。
- **budget=None 也会回退有限值**：commit_and_compact 里显式 None 强制用 max_tokens，禁止无限绕过裁剪（manager.py 第 144-145 行）。
- **重注入上限 30%**：`extra_budget = cap × 0.3`，摘要优先、任务次之，超预算先砍任务（reinjector.py 第 23、75 行）。
- **截断被明确否决**：`OffloadPolicy(mode="truncate")` 构造即抛 ValueError，没有兼容开关（offload_policy.py 第 46-51 行）。
- **落盘不截断**：大结果交 BlobPort 换句柄，保真且模型可按需回读（offload_policy.py 第 68 行）。
- **句柄 scheme 是 `store://`**：`parse_handle` 对非法前缀或空 id 返回 None（offloader.py 第 24、34-36 行）。
- **delete 幂等**：`pop(key, None)`，删不存在句柄不报错（offloader.py 第 93 行）。
- **hit_rate 无访问返回 0.0**，绝不 NaN/None；record_* / with_label 返回新实例（observability.py 第 47-48、53、57、61 行）。
- **trace 只记两个事件**：`write_cold` / `compact`，为兼容既有顺序断言（manager.py 第 27-28 行）。
- **with_ 保留观测历史**：trace 用拷贝（可变 list），cache_telemetry 直接共享（不可变 DTO）（manager.py 第 225-226 行）。
- **四个内部协议不进 PORT_PROTOCOLS**：TokenCounter / BlobPort / Offloader / Reinjector 只是包内组件，Port 数量不变。
- **产新不改性**：压缩、重注入、with_ 都返回新对象，绝不修改 `state.messages`（内核只增不减契约）。
- **fail-closed 诸处**：非法策略构造即抛错（OffloadPolicy 两处校验、CacheTelemetry 负计数）、counter 永不缺席、budget=None 强制有限值、recall 类型不符抛 TypeError。
- **包级 API 收敛**：`__init__.py` 汇总 31 个符号；`MemoryPortWithRecall` 不在其中，需从 ports.py 单独导入。

---

*本册逐行覆盖 `src/harness/context/` 全部 11 个文件（1020 行源码），按「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」格式编写。下一册：[02 tools 工具模块](./02-tools工具模块.md)。想深入记忆本体读 [07 memory](./07-memory模块精读.md)。*
