# 代码精读 02 · tools 工具模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/tools/` 全部 9 个文件：types.py(40) + decorator.py(144) + registry.py(249) + execution.py(122) + index.py(81) + mcp.py(128) + webfetch.py(81) + capability.py(122) + `__init__.py`(34)，共 1001 行源码。
> 总入口见 [00](./00-总入口与全局链路.md)；注入检测的细节在 [05 skills](./05-skills技能模块.md)。

---

## 0. 读前GPS

这个模块是 R5 工具域的核心：它把「一个普通 Python 函数」变成「一个可被模型调用、带安全路由、可渐进披露的工具」。

9 个文件分三层：

- **基础层**：`types.py`（枚举门面）、`decorator.py`（`@tool` 装饰器 + 待注册容器）。
- **核心层**：`registry.py`（注册 + 四级 danger 路由 + 调用，**全模块最重要**）、`execution.py`（三种高危执行类工具）、`index.py`（三级披露）。
- **接入层**：`mcp.py`（外部 MCP 工具注入）、`webfetch.py`（网页抓取 + 注入检测）、`capability.py`（装配成能力包）、`__init__.py`（包级导出）。

全模块四个核心类：

- `ToolRegistry`：注册/目录/schema/静态检查/调用的唯一入口，四级 danger 路由的落点。
- `ToolIndex`：L1 目录 / L2 schema / L3 正文三级披露。
- `ToolFn` + `tool()`：函数包装 + 装饰即自动入列。
- `MCPManager` / `WebFetchTool` / `ToolsCapability`：外部接入与能力装配。

贯穿全模块的**五条安全红线**（读到具体行会反复见到）：

1. **danger 以注册时的 spec 为准**，忽略调用方传来的 `call.danger`——防伪造降级。
2. **四级路由**：SAFE 直执行 / LOW 审计后执行 / HIGH 沙盒 / CRITICAL 审批 + 沙盒。
3. **高危无沙盒、CRITICAL 无人工直接拒绝**，绝不降级为裸奔直执行（fail closed）。
4. **L1 目录绝不披露完整 `parameters_schema`**（渐进披露红线）。
5. **工具/沙盒/审计/审批异常一律折叠为失败 `ToolResult`**，不抛穿给调用方。

一句话记忆：**注册时定死风险，调用时按风险走不同的门，异常一律吞成失败信封，披露一律最小化**。

---

## 1. types.py 逐行讲解（40 行）

基础枚举门面。本模块只放「取值可枚举」的常量（编码规则 5），供装饰器默认值和各实现共用，避免魔法字符串散落。注意 `DangerLevel` 的权威定义在 `compose`（底层跨层 DTO 的物理约束），这里只是 R5 的**统一消费入口**，从 compose 重导出。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R5 工具包基础枚举：来源、沙盒策略与风险等级的有限取值集合（规格 P5）。`
  一句话定位：三个有限取值集合——工具来源、沙盒策略、风险等级。归属 R5（工具·MCP·技能域），规格编号 P5。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `本模块只放"取值可枚举"的常量（编码规则 5），供装饰器默认值与各实现共用，`
  点明编码规则 5：有限取值集合必须用 Enum，禁止裸魔法串。并说明用途——装饰器默认值与各实现共用同一份常量。

- **第 4 行** `避免魔法字符串散落。DangerLevel/coerce 语义归属 R5（⑤Port 契约）；本模块作为`
  `DangerLevel` 和 `coerce` 的语义归属 R5。注意这里说了「语义归属 R5」——即解释权在 R5，但物理定义不在（见下两行）。

- **第 5-6 行** `R5 内部统一消费门面，从 hooks.dto 重导出（跨层 DTO 字段的物理约束使定义须留在` / `hooks 底层，避免 hooks 反向依赖 harness 成环）。`
  **关键架构说明**：`DangerLevel` 是跨层 DTO（`ToolSpec` 的字段），物理上必须定义在底层 `hooks.dto`。若定义在 harness，hooks 要读它就得反向依赖 harness，形成环。所以底层定义、上层重导出=依赖单向（harness → compose）。

- **第 7 行** `"""` docstring 结束。

### 第 8-16 行：imports 与导出

- **第 8 行** `from __future__ import annotations`
  延迟求值注解：类型注解在运行时以字符串形式保存，允许引用尚未定义的类型，降低 import 顺序耦合。

- **第 10 行** `from enum import Enum`
  为下面两个枚举提供基类。

- **第 12-13 行** 注释：`# R5 门面：DangerLevel 权威定义在 hooks.dto（跨层 DTO 字段的物理约束），` / `# 此处重导出作为 R5 内部统一消费入口（规格 P5：R5 是 danger 语义归属层）。`
  重复强调「物理定义与语义归属分离」：权威定义在底层，但消费口径统一从 R5 拿。

- **第 14 行** `from compose import DangerLevel`
  **重导出动作**：把 compose 里的 `DangerLevel` 引入本模块命名空间。外部 `from harness.tools import DangerLevel` 实际拿到的是 compose 的那个类，是同一个对象（不是拷贝）。

- **第 16 行** `__all__ = ["ToolSource", "SandboxPolicy", "DangerLevel"]`
  声明公共导出面：两个本模块定义的枚举 + 一个重导出的枚举。注意重导出的符号也列进 `__all__`，`import *` 才会带上它。

### 第 19-28 行：ToolSource 枚举

- **第 19 行** `class ToolSource(str, Enum):`
  继承 `str, Enum` 双基类：既是枚举（有限取值、可 `is` 比较），又能当字符串用（进 JSON / schema / 审计日志）。若只继承 `Enum`，枚举值进 JSON 需要额外序列化。

- **第 20-24 行** docstring + 成员说明：三个成员分别是本地 `@tool` 装饰函数、外部 MCP server 注入、技能暴露的工具。

- **第 26 行** `LOCAL = "local"`
  本地工具来源。decorator.py 第 138 行构造 spec 时就填 `ToolSource.LOCAL.value`。

- **第 27 行** `MCP = "mcp"`
  MCP 工具来源。mcp.py 的 connect 第 99 行把注入的工具 source 改成这个值。

- **第 28 行** `SKILL = "skill"`
  技能暴露的工具来源（预留）。

### 第 31-40 行：SandboxPolicy 枚举

- **第 31 行** `class SandboxPolicy(str, Enum):`
  同样是双基类枚举。

- **第 32 行** docstring 首行：`"""沙盒策略（@tool 装饰器默认值；运行期路由以 danger 为准，本枚举仅作声明）。`
  **关键提醒**：这个枚举「仅作声明」——它是 `@tool` 的参数默认值，但**运行期路由不看它**，真正决定要不要进沙盒的是 `danger`（见 registry.py 第 226-232 行）。不这么设计的话，就会出现「声明 ALWAYS 却因 danger=SAFE 被直执行」的策略与实现矛盾。

- **第 34-36 行** 成员说明：`AUTO` 由 danger 决定、`ALWAYS` 总是沙盒、`NEVER` 从不沙盒。

- **第 38 行** `AUTO = "auto"`
  默认策略：由 danger 决定（实际运行时的唯一有效口径）。

- **第 39 行** `ALWAYS = "always"`
  声明「总是沙盒」——目前只作声明，不参与路由。

- **第 40 行** `NEVER = "never"`
  声明「从不沙盒」——同样只作声明。注意 `@tool` 的 `sandbox` 参数（decorator.py 第 85 行）接收这个值，但在第 99 行被显式丢弃，不写进 ToolSpec。

**本文件设计要点**：把「来源 / 沙盒声明 / 风险等级」三个有限集合收敛到一处，且明确 `DangerLevel` 物理定义在底层、语义消费在 R5，既避免魔法串又避免层间依赖成环。

---

## 2. decorator.py 逐行讲解（144 行）

`@tool` 装饰器：把普通 Python 函数包装为带 `ToolSpec` 的 `ToolFn`，并在装饰时就把它放进模块级待注册容器，装配期由 `registry.register_pending()` 批量消费。

### 第 1-8 行：模块 docstring

- **第 1 行** `"""R5 工具装饰器：把普通 Python 函数包装为带 ToolSpec 的 ToolFn。`
  一句话定位：函数 → ToolFn 的包装器。

- **第 3 行** `- danger 必填关键字（装配期强制，规则 5 用枚举约束取值）；`
  第一条纪律：`danger` 是必填关键字参数（后面第 84 行没有默认值），强制调用方声明风险。用枚举约束取值（规则 5）。

- **第 4-5 行** `- 第一个参数为 pydantic BaseModel 时，用 model_json_schema() 生成 L2 schema，` / `  并借 pydantic 的 extra=forbid 在包装层拦截未声明参数；`
  第二条纪律：首参是 pydantic 模型时，schema 由模型生成，参数校验也借模型构造（`extra=forbid`）在**实现执行前**完成。

- **第 6-7 行** `- 无参 / 自由 **kwargs 函数生成空 properties，交给 registry 层统一做 extra=forbid。`
  第三条纪律：首参不是模型时，生成空 properties，校验下沉到 registry 层（registry.py 第 198-205 行）。

- **第 8 行** `"""` docstring 结束。

### 第 9-25 行：imports 与导出

- **第 9 行** `from __future__ import annotations` 延迟注解。

- **第 11 行** `import inspect`
  用 `inspect.signature` 解析被装饰函数的参数签名。

- **第 12 行** `from collections.abc import Callable`
  类型注解用：装饰器签名和执行体类型。

- **第 13 行** `from dataclasses import dataclass`
  为 `ToolFn` 提供 `@dataclass`。

- **第 14 行** `from typing import Protocol, cast, get_type_hints`
  - `Protocol`：定义 `_Named` 元数据视图。
  - `cast`：把函数对象断言为 `_Named` 以安全读 `__name__`/`__doc__`。
  - `get_type_hints`：解析首个参数的真实类型注解（比直接读 `__annotations__` 更可靠，能解析字符串注解和前向引用）。

- **第 16 行** `from pydantic import BaseModel`
  判断是否用模型生成 schema 与做参数校验的基准类。

- **第 18 行** `from compose import ToolSpec`
  工具目录条目的 DTO（name/description/schema/danger/source）。

- **第 20 行** `from .types import DangerLevel`
  从本包 types 门面导入风险等级（实际是 compose 的重导出）。

- **第 21 行** `from runtime import DynamicValue`
  动态值类型别名，标记执行体入参/出参是「框架不约束具体形态」的值。

- **第 23 行** `from .types import SandboxPolicy, ToolSource`
  导入沙盒策略默认值（第 85 行）和工具来源（第 138 行）。

- **第 25 行** `__all__ = ["ToolFn", "tool", "pending_tools", "clear_pending"]`
  导出面：`ToolFn` 包装器、装饰器工厂、两个容器操作函数。`_Named` 是内部协议，不导出。

### 第 27-30 行：模块级待注册容器 `_PENDING`

- **第 27-29 行** 注释：`# 【v2.2】模块级待注册容器：@tool 装饰即入列，装配期由 registry.register_pending 批量消费。` / `# 为何是模块级：零参数兼容现有全部 @tool 调用点；代价是全局状态，故提供 clear_pending 与` / `# tests/r5/conftest.py 的 autouse fixture 保证测试隔离。`
  **关键设计说明**：用模块级全局列表，是为了让现有所有 `@tool(...)` 调用点**零改动**就能自动注册（装饰器不需要持有 registry 引用）。代价是引入全局状态，所以用 `clear_pending` + 测试的 autouse fixture 来隔离，防止测试间串扰。

- **第 30 行** `_PENDING: list[ToolFn] = []`
  真正的容器。每个 `@tool` 装饰执行时往这里 append 一个 `ToolFn`（第 141 行）。装配期 `register_pending()` 统一取走并清空。

### 第 33-39 行：`pending_tools()`

- **第 33 行** `def pending_tools() -> tuple[ToolFn, ...]:`
  返回待注册项的快照。返回类型标注为 `tuple`，与内部 `list` 区分——对外暴露不可变视图。

- **第 34-38 行** docstring：返回按装饰顺序排列的 `ToolFn` 元组，无待注册项时为空元组。

- **第 39 行** `return tuple(_PENDING)`
  **防御性拷贝**：`tuple(...)` 生成新元组，外部拿到后无法 append/删除来改写内部列表。若直接返回 `_PENDING`，外部一处误改就污染全局容器。`registry.register_pending()` 第 99 行正是靠这个快照安全遍历。

### 第 42-44 行：`clear_pending()`

- **第 42 行** `def clear_pending() -> None:`
- **第 43 行** docstring：清空容器（供测试隔离与装配层显式重置），不改变已注册工具。
- **第 44 行** `_PENDING.clear()`
  就地清空（不是重新赋值 `[]`）。这点重要：`_PENDING` 的引用在模块加载时已绑定到函数闭包，若改成 `_PENDING = []` 会让持有旧引用的地方拿到陈旧列表；`clear()` 原地改则所有引用看到的都是清空后的同一容器。`register_pending()` 第 113 行注册成功后调用它。

### 第 47-51 行：`_Named` 协议

- **第 47 行** `class _Named(Protocol):`
  内部协议：描述「有 `__name__` 和 `__doc__` 的对象」。不导出（`__all__` 里没有）。

- **第 48 行** docstring：被装饰函数的元数据视图，供 cast 后安全读取。

- **第 50-51 行** `__name__: str` / `__doc__: str | None`
  声明两个属性。为什么要这个协议：普通函数对象理论上都有这两个属性，但静态类型检查器不认，所以先 `cast(_Named, original)`（第 103 行），再读 `meta.__name__` / `meta.__doc__` 就不报类型错。

### 第 54-68 行：`ToolFn` 包装器

- **第 54 行** `@dataclass(frozen=True, slots=True)`
  - `frozen=True`：实例不可变（规则 2），防止注册后 spec/fn 被改写。
  - `slots=True`：不生成 `__dict__`，省内存、防动态加属性。

- **第 55 行** `class ToolFn:`
  可调用工具包装器：携带 `ToolSpec` 的 frozen 对象。

- **第 56-61 行** docstring：字段说明——`spec` 是目录条目，`fn` 是统一签名为 `**kwargs -> DynamicValue` 的执行体。

- **第 63 行** `spec: ToolSpec`
  工具目录条目。

- **第 64 行** `fn: Callable[..., DynamicValue]`
  实际执行体。签名被统一成 `**kwargs`，无论是原始模型参数还是自由参数。

- **第 66 行** `def __call__(self, **kwargs: DynamicValue) -> DynamicValue:`
  让 `ToolFn` 实例可像函数一样被调用。registry 第 111 行把 `tool_fn`（整个 ToolFn）作为 fn 注册，调用时就是走这里的 `__call__`。

- **第 67 行** docstring：以关键字参数调用工具实现，校验由 pydantic 模型或 registry 完成。

- **第 68 行** `return self.fn(**kwargs)`
  纯转发。所有参数校验都在包装阶段（模型构造）或 registry 层完成，这里不重复校验。

### 第 71-78 行：`_first_line(doc)`

- **第 71 行** `def _first_line(doc: str | None) -> str:`
  取 docstring 首行作为一句话描述。

- **第 72 行** docstring：`"""取 docstring 首行作为一句话描述（多行正文不进 L1 目录，避免披露膨胀）。"""`
  **为什么只取首行**：L1 目录会给模型看每个工具的名字 + 描述。若把完整多行 docstring 都塞进去，token 膨胀。只取首行作为「一句话摘要」，详细正文留给 L3。

- **第 73-74 行** `if doc is None:` / `return ""`
  无 docstring 的函数返回空串，不抛异常——装饰一个没写文档的函数是合法常态。

- **第 75 行** `stripped = doc.strip()`
  去首尾空白，避免纯空白文档被当成有效描述。

- **第 76-77 行** `if not stripped:` / `return ""`
  全空白等价于无描述（与上一条分支合并处理）。

- **第 78 行** `return stripped.splitlines()[0].strip()`
  按行切分取第一行再去空白。**为什么先 strip 再 splitlines**：若 docstring 首行是换行，`strip()` 去掉后 `splitlines()[0]` 才是真正的第一行内容，否则可能拿到空串。

### 第 81-96 行：`tool()` 装饰器工厂签名与 docstring

- **第 81 行** `def tool(`
- **第 82 行** `*,`
  `*` 之后全都是关键字参数——强制调用方具名传参（`@tool(name="calc", danger=...)`），避免位置参数误传。

- **第 83 行** `name: str | None = None,`
  工具名，缺省取函数名（第 134 行 `name or meta.__name__`）。

- **第 84 行** `danger: DangerLevel,`
  **必填参数**：注意它排在有默认值的 `name` 后面，但因为没有默认值，Python 允许（关键字参数顺序不影响）。这是强制声明风险的落点——不写 danger 会在装饰时直接 `TypeError`。

- **第 85 行** `sandbox: SandboxPolicy = SandboxPolicy.AUTO,`
  沙盒策略声明，默认 AUTO。后面会被丢弃（第 99 行）。

- **第 86 行** `timeout: float = 30.0,`
  超时声明，默认 30 秒。同样被丢弃。

- **第 87 行** `retries: int = 2,`
  重试次数声明，默认 2。同样被丢弃。

- **第 88 行** `) -> Callable[[Callable[..., DynamicValue]], ToolFn]:`
  返回类型是装饰器：接收原函数、返回 `ToolFn`。

- **第 89-96 行** docstring：逐个说明参数。`danger` 是必填、决定路由；sandbox/timeout/retries 只是「声明位」，ToolSpec 不承载执行策略字段。

### 第 97-99 行：显式丢弃三个策略参数

- **第 97-98 行** 注释：`# sandbox/timeout/retries 是本装饰器的执行策略声明位，R9 执行外壳尚未定型，` / `# 故当前只接收不写入 ToolSpec（避免往 DTO 塞未定语义的字段）。`
  **为什么接收却不写入**：接口先占位（保持 API 稳定），但 R9 的执行外壳（沙盒/超时/重试具体语义）还没定。若现在就把未定语义塞进 `ToolSpec`，未来语义变了要改 DTO 契约，牵一发动全身。

- **第 99 行** `_ = (sandbox, timeout, retries)`
  **显式丢弃**：把三个值打包赋给 `_`（约定俗成的「不用」变量），明确告诉读者和 linter「这三个参数故意不使用」。相比让变量悬空未用，这样不会触发未使用变量告警，也表达清楚意图。注意：不这么写只是有个 linter 警告，不影响功能；但写出来是「意图声明」。

### 第 101-114 行：`decorate` 内部——解析签名与首参判定

- **第 101 行** `def decorate(original: Callable[..., DynamicValue]) -> ToolFn:`
  真正的装饰器函数，接收原始函数。

- **第 102 行** docstring：包装单个函数——解析首参注解，生成 schema 与统一 `**kwargs` 执行体。

- **第 103 行** `meta = cast(_Named, original)`
  把函数对象断言为 `_Named`，为后面读 `__name__`/`__doc__` 提供类型提示。`cast` 运行时无操作。

- **第 104 行** `signature = inspect.signature(original)`
  取函数签名（含参数列表）。

- **第 105 行** `params = tuple(signature.parameters.values())`
  把所有参数对象转成元组，便于按顺序取第一个。

- **第 106 行** `hints = get_type_hints(original)`
  解析类型注解。`get_type_hints` 会求值字符串注解、解析前向引用，比读 `original.__annotations__` 可靠。

- **第 107 行** `first = params[0] if params else None`
  取第一个参数对象；无参函数为 `None`。

- **第 108 行** `model_cls: type[BaseModel] | None = None`
  初始化「首参是否是 pydantic 模型」的标志，默认否。

- **第 109 行** `if first is not None:  # 有参数才需要判定是否为 pydantic 模型`
  有参数才判定，避免无参函数访问 `first.name` 出错。

- **第 110 行** `annotation = hints[first.name] if first.name in hints else None`
  从解析后的注解字典里取首参类型；没注解则为 `None`（比如自由 `**kwargs` 首参无注解）。

- **第 111 行** `if isinstance(annotation, type) and issubclass(annotation, BaseModel):`
  **两个条件缺一不可**：
  - `isinstance(annotation, type)`：确保注解是个类（不是 `typing.Optional[X]` 之类的类型别名、`list[int]` 之类的泛型别名）。
  - `issubclass(annotation, BaseModel)`：确保是 pydantic 模型子类。
  - 若不先做 `isinstance(..., type)`，对 `list[int]` 调 `issubclass` 会抛 `TypeError`。这是对注解形态的防御。

- **第 113 行** `model_cls = annotation`
  记下模型类，下面走模型路径。

### 第 115-131 行：两条 schema / 执行体生成路径

- **第 115 行** `if model_cls is not None:`
  路径 A：首参是 pydantic 模型。

- **第 116 行** `schema = model_cls.model_json_schema()`
  L2 schema 直接由模型的 `model_json_schema()` 生成——字段、类型、required 全由模型定义，单一真相源，不用手写 schema 与模型同步。

- **第 118 行** `def model_wrapper(**kwargs: DynamicValue) -> DynamicValue:`
  模型路径的执行体包装器。

- **第 119 行** 注释：`# 为何：pydantic 模型以 extra=forbid 构造，脏参数在实现执行前就被挡下。`
  **核心防御**：pydantic 模型默认（项目基类若配了 `extra="forbid"`）会在构造时拒绝未声明字段，于是「多传参数」在实现函数执行前就被挡下，根本进不了业务代码。

- **第 120 行** `return original(model_cls(**kwargs))`
  先用 `model_cls(**kwargs)` 构造模型实例（此处校验发生），再把模型实例传给原函数。原函数拿到的已是校验过的模型对象，而非裸 dict。

  - **不这么写会怎样**：若直接 `original(**kwargs)`，脏参数会透传进业务实现，可能绕过校验造成越权/异常。

- **第 122 行** `fn: Callable[..., DynamicValue] = model_wrapper`
  把包装器赋给统一的 `fn` 变量，两条路径后面共用。

- **第 123 行** `else:`
  路径 B：首参不是模型（无参 / 自由 kwargs / 普通参数）。

- **第 124 行** `schema = {"type": "object", "properties": {}}`
  生成空 properties 的 schema。**注意**：有 `"properties"` 键但为空——这会让 registry 的参数校验走到 `extra=forbid` 分支（registry.py 第 198 行检查键存在），且 allowed 集合为空，于是任何参数都被判非法。这是给「无参工具」的严格语义。

- **第 126 行** `def plain_wrapper(**kwargs: DynamicValue) -> DynamicValue:`
  普通路径的执行体包装器。

- **第 127-128 行** 注释：`# 为何：无参/自由 kwargs 工具不约束字段，透传即可；` / `# registry 层按空 properties 统一做 extra=forbid（见 registry.call）。`
  说明职责切分：这里只透传，校验下沉到 registry。

- **第 129 行** `return original(**kwargs)`
  直接把关键字参数透传给原函数，不做模型包装。

- **第 131 行** `fn = plain_wrapper`
  赋给统一的 `fn` 变量。

### 第 133-142 行：构造 ToolSpec + ToolFn + 入列 + 返回

- **第 133 行** `spec = ToolSpec(`
  构造工具目录条目 DTO。

- **第 134 行** `name=name or meta.__name__,`
  工具名：传了 `name` 就用它，否则取被装饰函数的 `__name__`。用 `or` 而非三元，空串也会回退到函数名。

- **第 135 行** `description=_first_line(meta.__doc__),`
  描述取 docstring 首行（第 71-78 行的函数）。

- **第 136 行** `parameters_schema=schema,`
  用上面两条路径生成的 schema。

- **第 137 行** `danger=danger,`
  风险等级——必填参数原样填入，决定后续路由。

- **第 138 行** `source=ToolSource.LOCAL.value,`
  来源固定为本地工具（取枚举的字符串值）。

- **第 140 行** `tool_fn = ToolFn(spec=spec, fn=fn)`
  把 spec 和执行体包成冻结的 `ToolFn`。

- **第 141 行** `_PENDING.append(tool_fn)  # 【v2.2】装配期自动注册的入列点；签名对调用方透明。`
  **自动注册的落点**：装饰时就把 `tool_fn` 塞进模块级容器。调用方写 `@tool(...)` 完全无需知道有个全局列表——这就是「零参数兼容」的实现。

- **第 142 行** `return tool_fn`
  装饰器返回 `ToolFn`。注意：被 `@tool` 装饰后，原函数名绑定的已是 `ToolFn` 实例（可调用），不再是最初的函数。

### 第 144 行：返回装饰器

- **第 144 行** `return decorate`
  工厂返回 `decorate` 函数本身。Python 的 `@tool(...)` 语法会先把参数传给 `tool()` 得到 `decorate`，再用 `decorate` 去装饰下面的函数。

**本文件设计要点**：用「首参是否为 pydantic 模型」做分叉，把参数校验要么前移到模型构造（模型路径）、要么下沉到 registry（普通路径）；用模块级 `_PENDING` 实现装饰即注册，代价是全局状态，由快照 + clear 兜住。

---

## 3. registry.py 逐行讲解（249 行）——全模块核心

这是整个 tools 模块最重要的文件：`ToolRegistry` 是 `ToolPort` 的默认实现，承担注册、目录披露、静态检查、统一调用四件事，四级 danger 路由就落在 `call()` 方法里。读完这个文件，整个模块的安全逻辑就通了。

### 第 1-8 行：模块 docstring（四条核心纪律）

- **第 1 行** `"""R5 工具注册与统一调用：ToolPort 的默认实现 ToolRegistry。`
  一句话定位。

- **第 3 行** `核心纪律（R9 §5 权威路由）：`
  下面四条是整个调用路由的设计纲领，权威出处 R9 §5。

- **第 4 行** `- danger 以注册 spec 为准（忽略 call.danger 防伪造降级）；`
  第一条红线：调用方传来的 danger 不算数，以注册时 spec 的 danger 为准。防的是「有人把高危工具伪装成 SAFE 来绕过沙盒」。

- **第 5 行** `- SAFE 直执行 / LOW 审计后直执行 / HIGH 沙盒 / CRITICAL 审批后沙盒；`
  第二条：四级路由的具体行为。

- **第 6 行** `- 工具、沙盒、审计、审批的异常一律折叠为失败 ToolResult，不抛穿；`
  第三条：异常一律吞成失败信封。

- **第 7 行** `- L1 目录绝不含完整 parameters_schema（渐进披露红线）。`
  第四条：L1 目录清空 schema。

- **第 8 行** `"""` docstring 结束。

### 第 9-24 行：imports 与导出

- **第 9 行** `from __future__ import annotations` 延迟注解。

- **第 11 行** `import inspect`
  用于第 160 行 `inspect.isawaitable` 判断执行体返回值是否可 await。

- **第 12 行** `import time`
  用于第 166/181 行 `time.perf_counter()` 计耗时（`perf_counter` 单调时钟，适合测间隔）。

- **第 13 行** `from collections.abc import Callable`
  执行体类型注解。

- **第 14 行** `from typing import Self`
  `freeze()` 第 153 行返回 `Self` 类型，表示方法返回本类实例（可链式调用）。

- **第 16-19 行** `from compose import (` 多行导入：
  - `AuditPort`：审计门（LOW 路径用）。
  - `DangerLevel`：风险等级（含 `coerce` 归一）。
  - `HumanPort`：人工审批门（CRITICAL 路径用）。
  - `PortCtx`：port 调用上下文。
  - `SandboxPort`：沙盒门（HIGH/CRITICAL 路径用）。
  - `ToolCall`：调用请求 DTO（name/args/danger/call_id/source）。
  - `ToolPort`：本类实现的协议。
  - `ToolResult`：调用结果信封（ok/value/error/meta）。
  - `ToolSpec`：目录条目。

- **第 20 行** `from runtime import DynamicValue`
  动态值类型。

- **第 22 行** `from .decorator import clear_pending, pending_tools`
  从装饰器模块导入待注册容器的快照与清空操作（`register_pending` 用）。

- **第 24 行** `__all__ = ["ToolRegistry"]`
  只导出 `ToolRegistry` 一个类。

### 第 26-35 行：错误码与比较常量

- **第 26 行** `# 稳定错误码与比较常量（规则 5：比较右侧必须是常量名而非字符串字面量）。`
  **规则 5 的落地**：比较时用常量名（如 `_ERR_NOT_FOUND`），不写裸字符串字面量（如 `"NOT_FOUND"`）。好处：改错误码只改一处；拼错常量名会被 NameError 抓到，拼错字符串则静默出错。

- **第 27 行** `_ERR_NOT_FOUND = "NOT_FOUND"`
  工具未注册错误码。

- **第 28 行** `_ERR_INVALID_ARGS = "INVALID_ARGS"`
  参数非法错误码。

- **第 29 行** `_ERR_NO_SANDBOX = "NO_SANDBOX"`
  高危工具无沙盒错误码。

- **第 30 行** `_ERR_NO_HUMAN = "NO_HUMAN"`
  CRITICAL 工具无人工审核错误码。

- **第 31 行** `_ERR_APPROVAL_DENIED = "APPROVAL_DENIED"`
  审批被拒错误码。

- **第 32 行** `_ERR_TOOL_ERROR = "TOOL_ERROR"`
  工具/沙盒/审计/审批异常的统一折叠前缀。

- **第 33 行** `_APPROVE = "approve"`
  人工审批的「同意」答案常量（第 238 行比较用）。

- **第 34 行** `_OPT_DENY = "deny"`
  人工审批的「拒绝」选项常量（第 237 行传给 `human.ask`）。

- **第 35 行** `_KEY_PROPERTIES = "properties"`
  schema 里 properties 键名常量（第 198 行检查用）。用常量避免多处重复这个字符串字面量。

### 第 38-51 行：类声明与属性注解

- **第 38 行** `class ToolRegistry:`
  工具注册表类。

- **第 39-43 行** docstring：`"""工具注册表：注册/目录/schema/静态检查/调用四路由的唯一入口。` + 属性 `name` 说明（装配期按 name 校验）。

- **第 45 行** `name: str`
  port 名（默认 `"tools"`，见第 59 行）。

- **第 46 行** `_specs: dict[str, ToolSpec]`
  工具名 → spec 的映射（目录真相源）。

- **第 47 行** `_fns: dict[str, Callable[..., DynamicValue]]`
  工具名 → 执行体的映射。与 `_specs` 按名字一一对应，分开存是因为 spec 用于披露、fn 用于执行。

- **第 48 行** `_sandbox: SandboxPort | None`
  沙盒依赖，可缺席。

- **第 49 行** `_human: HumanPort | None`
  人工审批依赖，可缺席。

- **第 50 行** `_audit: AuditPort | None`
  审计依赖，可缺席。

- **第 51 行** `_frozen: bool`
  冻结标志，冻结后拒绝注册。

### 第 53-68 行：`__init__`

- **第 53 行** `def __init__(`
- **第 55 行** `*,` 关键字参数分隔。
- **第 56 行** `sandbox: SandboxPort | None = None,` 沙盒依赖，默认缺席。
- **第 57 行** `human: HumanPort | None = None,` 人工依赖，默认缺席。
- **第 58 行** `audit: AuditPort | None = None,` 审计依赖，默认缺席。
- **第 59 行** `name: str = "tools",` port 名默认 `"tools"`。
- **第 60 行** `) -> None:`
- **第 61 行** docstring：`"""构造注册表：三个高危路由依赖可缺省注入，缺席时按 fail-closed 处理。`
  **fail-closed**：沙盒/人工/审计缺席不是「放行」，而是让对应高危工具在调用/静态检查时报错拒绝。

- **第 62 行** `self.name = name`
- **第 63 行** `self._specs = {}`
- **第 64 行** `self._fns = {}`
- **第 65 行** `self._sandbox = sandbox`
- **第 66 行** `self._human = human`
- **第 67 行** `self._audit = audit`
- **第 68 行** `self._frozen = False`
  初始未冻结。

### 第 70-83 行：`register(spec, fn)`

- **第 70 行** `def register(self, spec: ToolSpec, fn: Callable[..., DynamicValue]) -> None:`
- **第 71 行** docstring 首行：`"""注册工具（重名拒绝）；danger=None 允许注册，由 static_check 暴露问题。`
  注意：danger 为 None 不在这里拒绝，留给静态检查统一报告（见第 81 行注释）。

- **第 72-75 行** 参数说明。

- **第 76 行** `if self._frozen:  # 冻结后目录不再变更，防止运行期工具集漂移。`
  **冻结检查**：冻结后拒绝注册，防止运行期工具集被偷偷改动（工具集漂移会破坏可审计性与披露一致性）。

- **第 77 行** `raise RuntimeError(f"registry {self.name} is frozen")`
  冻结时抛 `RuntimeError`。

- **第 78 行** `if spec.name in self._specs:`
  重名检查。

- **第 79-80 行** 注释 + `raise ValueError(f"duplicate tool: {spec.name}")`
  **重名拒绝**：两个同名工具在调用路由时会有二义性（无法区分该调哪个），所以注册期就挡住。

- **第 81 行** `# 不在 register 拒绝 danger=None：装配期需要先注册再由 static_check 统一报告。`
  **为什么不在这里拒绝 danger=None**：装配期希望一次 `static_check()` 拿到所有问题（danger 缺失、无沙盒、无人工），而不是在注册时就中断。注册阶段放行、检查阶段统一报告，体验更好。

- **第 82 行** `self._specs[spec.name] = spec`
- **第 83 行** `self._fns[spec.name] = fn`
  写入两个映射。

### 第 85-114 行：`register_pending()`——先全量预检再批量注册

- **第 85 行** `def register_pending(self) -> list[str]:`
  批量注册装饰器装饰的待注册项。

- **第 86 行** docstring 首行：`"""装配期批量注册 @tool 待注册项，成功即清空容器（v2.2 自动注册）。`

- **第 88-90 行** docstring 核心说明：`为何先全量预检再批量注册：预检覆盖 register 的全部失败条件（freeze 状态与重名），` / `确认整批可落地后才逐个写入，避免"注册一半抛错"留下半状态；预检通过后批量阶段` / `不会再抛，故无需回滚。失败时容器保留，修正后可重试。`
  **这是本方法最重要的设计**：
  - 若边检查边注册，注册到第 3 个发现重名抛错，前 2 个已经写进 registry，留下「注册了一半」的脏状态，且 `_PENDING` 没清空（因为提前抛错）。
  - 所以先做一遍**全量预检**，确认整批都能落地，再一次性写入。预检覆盖了 register 的全部失败条件（冻结、重名），所以批量阶段保证不会再抛，不需要事务回滚。
  - 失败时容器保留，修正后可重试。

- **第 92-98 行** docstring 的 Returns / Raises：返回按装饰顺序注册的工具名列表；冻结抛 `RuntimeError`；重名抛 `ValueError`。

- **第 99 行** `pending = pending_tools()`
  取不可变快照（decorator.py 第 33-39 行），遍历期间不受外部改写影响。

- **第 100-101 行** `if not pending:` / `return []`
  空容器直接返回空列表，不做任何操作。

- **第 102-103 行** `if self._frozen:` / `raise RuntimeError(f"registry {self.name} is frozen")`
  冻结检查**先于重名检查**——与 `register` 的纪律一致（先判能不能注册，再判注册什么）。

- **第 104 行** `seen: set[str] = set(self._specs)  # 已有工具名参与重名判定。`
  用集合记录已占用的名字，初始化为 registry 已有工具名。集合查询是 O(1)。

- **第 105 行** `for tool_fn in pending:  # 全量预检：容器内部重名与外部重名一并暴露。`
  遍历所有待注册项做预检。

- **第 106-107 行** `if tool_fn.spec.name in seen:` / `raise ValueError(f"duplicate tool: {tool_fn.spec.name}")`
  **预检的核心**：名字已在 `seen` 中（无论是 registry 已有的，还是前面待注册项刚加的）就抛错，整批拒绝。

- **第 108 行** `seen.add(tool_fn.spec.name)`
  把当前名字加入 seen，供后续项检测「容器内部重名」。

- **第 109 行** `names: list[str] = []`
  收集成功注册的名字。

- **第 110 行** `for tool_fn in pending:  # 预检已排除所有失败条件，批量阶段不会再抛。`
  第二遍遍历，真正注册。注释点明「不会再抛」的原因。

- **第 111 行** `self.register(tool_fn.spec, tool_fn)`
  调用 `register`。注意第二个参数传的是 `tool_fn` 本身（它是可调用的，见 decorator.py 第 66 行 `__call__`），作为执行体。

- **第 112 行** `names.append(tool_fn.spec.name)`
  记录名字。

- **第 113 行** `clear_pending()  # 成功即清空；失败在上面已提前返回/抛出，不会走到这里。`
  全部成功后清空容器。注释说明：失败路径已在上面提前 return/raise，能走到这里就一定是全成功了。

- **第 114 行** `return names`
  返回注册的名字列表。

### 第 116-121 行：`bind(name, fn)`——延迟替换执行体

- **第 116 行** `def bind(self, name: str, fn: Callable[..., DynamicValue]) -> None:`
- **第 117 行** docstring：`"""延迟绑定/替换已注册工具的执行体；未注册则 KeyError。"""`
- **第 118 行** `if name not in self._fns:`
- **第 119-120 行** 注释 + `raise KeyError(name)`
  **只允许替换已知工具**：未注册的名字抛 KeyError，防止有人绕过 `register` 偷偷注入一个执行体（那样 spec 目录里没有，披露和审计都会漏）。

- **第 121 行** `self._fns[name] = fn`
  只替换执行体，**不动 spec**。所以 danger/schema/来源保持不变，路由语义不变。

  - **关键**：`bind` 不受冻结影响——冻结只挡 `register`。这意味着冻结后仍可用 `bind` 热更执行体（比如替换成优化实现），而工具集结构不变。

### 第 123-128 行：`catalog(level)`——渐进披露的目录

- **第 123 行** `def catalog(self, level: int = 1) -> list[ToolSpec]:`
- **第 124 行** docstring：`"""按注册顺序返回目录；level=1 清空 schema，level>=2 返回完整 spec。"""`
- **第 125 行** `if level <= 1:`
  L1（含 level 0 或负数）走清空 schema 分支。

- **第 126 行** 注释：`# L1 红线：浅拷贝并清空 parameters_schema，避免披露完整 schema。`
  **L1 红线**：L1 目录只给模型看「有哪些工具 + 一句话描述」，绝不给完整参数 schema（那会占用大量 token，且没必要——模型先看目录再按需取 schema）。

- **第 127 行** `return [s.model_copy(update={"parameters_schema": {}}) for s in self._specs.values()]`
  **关键行**：用 pydantic 的 `model_copy(update=...)` 生成浅拷贝，把 `parameters_schema` 置为 `{}`。
  - **为什么拷贝而不是直接改**：`self._specs` 里的原 spec 是真相源，L2 披露要用完整 schema。若直接把原 spec 的 schema 清空，L2 就拿不到了。
  - **为什么是浅拷贝**：`model_copy` 默认浅拷贝，只替换 update 里的字段，其余字段共享引用——足够这里用（只改 schema 字段）。
  - **不这么做（直接返回原 spec）**：L1 泄露完整 schema，违背渐进披露，token 膨胀。

- **第 128 行** `return list(self._specs.values())`
  L2（level>=2）返回完整 spec 列表，按 dict 插入顺序（即注册顺序）。

### 第 130-134 行：`schema_of(name)`

- **第 130 行** `def schema_of(self, name: str) -> dict[str, DynamicValue]:`
- **第 131 行** docstring：`"""按名返回完整 parameters_schema（未注册 KeyError）；返回副本防外部改写。"""`
- **第 132-133 行** `if name not in self._specs:` / `raise KeyError(name)`
  未注册抛 KeyError。
- **第 134 行** `return dict(self._specs[name].parameters_schema)`
  **返回副本**：`dict(...)` 浅拷贝，防止外部拿到内部 schema 的引用后改写，污染注册表。与 catalog 的拷贝纪律一致。

### 第 136-151 行：`static_check()`——装配期静态检查

- **第 136 行** `def static_check(self) -> list[str]:`
- **第 137 行** docstring：`"""装配期静态检查：danger 缺失、高危无沙盒、CRITICAL 无人工，逐条列出。"""`
- **第 138 行** `problems: list[str] = []`
  收集所有问题。

- **第 139 行** `for name, spec in self._specs.items():  # 逐工具检查，收集全部问题而非首个即止。`
  **收集全部而非遇错即停**：装配时希望一次拿到所有问题清单，而不是修一个跑一次。

- **第 140 行** `if spec.danger is None:`
  问题一：danger 未声明。

- **第 141-142 行** `problems.append(f"{name}: danger not declared")` / `continue`
  记录后 continue——danger 都没声明就没法判断后续的高危条件，跳过后续检查。

- **第 143 行** `resolved = DangerLevel.coerce(spec.danger)`
  用 `coerce` 把 danger 归一成 `DangerLevel` 枚举（兼容字符串传入）。

- **第 144 行** `if resolved in (DangerLevel.HIGH, DangerLevel.CRITICAL):`
  只对高危等级检查沙盒。

- **第 145 行** 注释：`# 高危工具没有沙盒等于裸奔，必须在装配期暴露。`
- **第 146-147 行** `if self._sandbox is None:` / `problems.append(f"{name}: {resolved.value} tool missing sandbox")`
  问题二：高危工具缺沙盒。

- **第 148-150 行** 注释 + `if resolved is DangerLevel.CRITICAL and self._human is None:` / `problems.append(f"{name}: CRITICAL tool missing human reviewer")`
  问题三：CRITICAL 工具缺人工审核。**注意沙盒在场也不能免人工**——CRITICAL 需要审批 + 沙盒两道门。

- **第 151 行** `return problems`
  返回全部问题列表（空列表表示通过）。

### 第 153-156 行：`freeze()`

- **第 153 行** `def freeze(self) -> Self:`
- **第 154 行** docstring：`"""冻结目录（之后 register 抛 RuntimeError）；bind 仍可用于热更执行体。"""`
- **第 155 行** `self._frozen = True`
  置冻结标志。
- **第 156 行** `return self`
  返回 self，支持链式调用（如 `registry.freeze().static_check()`）。

### 第 158-162 行：`_maybe_await(value)`

- **第 158 行** `async def _maybe_await(self, value: DynamicValue) -> DynamicValue:`
  同步/异步执行体归一。

- **第 159 行** docstring：`"""同步/异步执行体归一：awaitable 才 await（避免 asyncio.iscoroutine 推 Any）。"""`
  用 `inspect.isawaitable` 而非 `asyncio.iscoroutine`：后者类型标注推不出具体类型（推成 Any），前者对任意 awaitable（含自定义 `__await__` 对象）都成立。

- **第 160 行** `if inspect.isawaitable(value):`
  判断是否可 await。

- **第 161 行** `return await value`
  可 await 就 await 拿结果。

- **第 162 行** `return value`
  否则原样返回（同步函数的返回值）。

  **为什么需要它**：`@tool` 装饰的函数可能是同步的也可能是异步的。统一走 `await self._maybe_await(...)`，调用方代码不用区分——同步函数 `fn(**args)` 直接返回值（非 awaitable），不 await；异步函数返回协程（awaitable），await 之。

### 第 164-186 行：`call()` 准备阶段 + `finish()` 闭包

- **第 164 行** `async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:`
  统一调用入口。

- **第 165 行** docstring：`"""统一调用入口：四级 danger 路由 + 参数校验 + 异常折叠 + 耗时记录。"""`
- **第 166 行** `started = time.perf_counter()`
  记录开始时间（单调时钟）。

- **第 167 行** `initial = DangerLevel.coerce(call.danger)`
  归一调用方传来的 danger。**注意**：这个值现在只用于 meta 的初步记录，后面第 193 行会以注册 spec 的 danger **覆盖**它。所以 `call.danger` 最终不参与路由决策。

- **第 168-175 行** 构造 `meta` 字典：
  - 第 169 行 `"call_id": call.call_id`：调用 ID。
  - 第 170 行 `"danger": initial.value`：初始 danger（会被覆盖）。
  - 第 171 行 `"source": call.source`：初始来源（会被覆盖）。
  - 第 172 行 `"sandbox": False`：默认未走沙盒，走到沙盒分支才置 True。
  - 第 173 行 `"approved": False`：默认未审批，CRITICAL 通过才置 True。
  - 第 174 行 `"retryable": True`：默认可重试，`finish` 会按 ok 刷新。
  - 第 175 行 `}` 字典结束。

- **第 177-179 行** `def finish(` 内部闭包：
  - 第 178 行 `value: DynamicValue = None, *, ok: bool = True, error: str | None = None,`
  - 第 179 行 `) -> ToolResult:`
  所有出口统一通过它构造 `ToolResult`。

- **第 180 行** 注释：`# 为何：所有出口统一补耗时；复制 meta 防止调用方改动注册表内部状态。`
  **统一出口的意义**：无论走哪个路由分支、成功还是失败，都经过 `finish`，保证耗时、retryable、meta 副本这些后处理不遗漏。

- **第 181 行** `meta["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)`
  计算耗时：结束减开始，乘 1000 转毫秒，`round(..., 3)` 保留 3 位小数。

- **第 182-183 行** 注释 + `meta["retryable"] = ok`
  **retryable 跟着 ok 走**：成功（ok=True）可重试，失败（ok=False）不可盲目重试。这样上层调度器看到 retryable=False 就不会无脑重放失败调用。

- **第 184-186 行** 构造 `ToolResult`：
  - 第 185 行 `call_id=call.call_id, ok=ok, value=value, error=error, meta=dict(meta),`
  - **第 185 行关键**：`meta=dict(meta)` 又做了一次拷贝——`finish` 每次返回都给调用方一个独立的 meta 副本，防止调用方改动这个副本影响 registry 内部或后续调用。

### 第 188-205 行：`call()` 路由准备——未注册检查、spec 为准、参数校验

- **第 188 行** `if call.name not in self._specs:`
- **第 189-190 行** 注释 + `return finish(ok=False, error=_ERR_NOT_FOUND)`
  未注册折叠为 `NOT_FOUND` 失败信封，不抛 KeyError（异常不抛穿纪律）。

- **第 192 行** `spec = self._specs[call.name]`
  取注册时的 spec（路由真相源）。

- **第 193 行** `level = DangerLevel.coerce(spec.danger)  # 以注册 spec 为准，忽略伪造的 call.danger。`
  **第一安全红线**：danger 以注册 spec 为准，彻底忽略 `call.danger`。这是防伪造降级的核心行——即使调用方把 danger 伪装成 SAFE，这里仍按 spec（比如 HIGH）路由，强制进沙盒。

- **第 194 行** `meta["danger"] = level.value`
  把 meta 里的 danger 刷新为 spec 的真实值（审计记录用）。

- **第 195 行** `meta["source"] = spec.source`
  把 meta 里的 source 也刷新为 spec 的真实值。

- **第 196 行** `fn = self._fns[call.name]`
  取执行体。

- **第 198 行** `if _KEY_PROPERTIES in spec.parameters_schema:`
  **参数校验的开关**：仅当 schema 显式含 `"properties"` 键时才做 `extra=forbid` 校验。

- **第 199-200 行** 注释：`# 仅当 schema 显式声明 properties 才做 extra=forbid：` / `# 无该键（如 MCP 的 {"type":"object"}）视为不约束，避免误伤外部工具。`
  **为什么不无条件校验**：外部 MCP 工具的 schema 可能只是 `{"type": "object"}`，没有 properties 键。若强行按空 allowed 集合校验，会拒绝所有参数，误伤外部工具。所以「无 properties 键 = 不约束」。

- **第 201 行** `props = spec.parameters_schema[_KEY_PROPERTIES]`
  取 properties 定义。

- **第 202 行** `allowed: set[str] = set(props) if isinstance(props, dict) else set()`
  构造允许的参数名集合：properties 是 dict 就取它的键，否则空集合。用 `isinstance` 防御异常形态（防止非 dict 导致 `set(props)` 行为异常）。

- **第 203 行** `for key in call.args:  # 命中第一个非法键即拒绝，错误信息保持稳定。`
  遍历调用方传来的所有参数名。

- **第 204-205 行** `if key not in allowed:` / `return finish(ok=False, error=_ERR_INVALID_ARGS)`
  任一参数名不在 allowed 集合里，立即返回 `INVALID_ARGS`。`extra=forbid` 语义——不接受未声明参数。

### 第 207-249 行：四级 danger 路由 + 异常折叠

- **第 207 行** `try:`
  包住整个路由分派，任何异常都在第 247 行被折叠。

#### SAFE 分支（第 208-210 行）

- **第 208 行** `if level is DangerLevel.SAFE:`
- **第 209 行** 注释：`# SAFE：无副作用顾虑，直接执行（同步函数经 _maybe_await 归一）。`
- **第 210 行** `return finish(await self._maybe_await(fn(**call.args)))`
  无副作用顾虑，直接执行。`fn(**call.args)` 传参，`_maybe_await` 归一同步/异步，结果交 `finish`。**不审计、不沙盒、不审批**——SAFE 就是最轻的路径。

#### LOW 分支（第 211-225 行）

- **第 211 行** `if level is DangerLevel.LOW:`
- **第 212 行** 注释：`# LOW：先记审计再直执行；审计缺席降级执行但显式标记，不静默放行。`
- **第 213 行** `if self._audit is not None:`
- **第 214-222 行** `await self._audit.record(` 记录审计：
  - 第 216 行 `"danger": level.value`：风险等级。
  - 第 217 行 `"source": spec.source`：来源。
  - 第 218 行 `"tool": call.name`：工具名。
  - 第 219 行 `"call_id": call.call_id`：调用 ID。
  - 第 220 行 `},` 字典结束。
  - 第 221 行 `ctx,` 传上下文。
  - 第 222 行 `)` 调用结束。

- **第 223 行** `else:`
- **第 224 行** `meta["audit_missing"] = True`
  **审计缺席时降级执行但显式标记**：不拒绝执行（LOW 风险可控），但在 meta 里标记 `audit_missing=True`，让下游审计系统知道这次调用没留痕。不是「静默放行」——有标记可追查。

- **第 225 行** `return finish(await self._maybe_await(fn(**call.args)))`
  审计后直接执行（不经沙盒）。

#### HIGH 分支（第 226-232 行）

- **第 226 行** `if level is DangerLevel.HIGH:`
- **第 227 行** 注释：`# HIGH：必须沙盒；缺席即拒绝，绝不降级为直执行。`
- **第 228-229 行** `if self._sandbox is None:` / `return finish(ok=False, error=_ERR_NO_SANDBOX)`
  **第二安全红线**：无沙盒直接拒绝，返回 `NO_SANDBOX`。绝不降级为直执行——高危工具裸奔的后果远大于拒绝调用的代价。

- **第 230 行** `out = await self._sandbox.run(fn, call.args, ctx)`
  有沙盒则把执行体和参数交给沙盒隔离运行。

- **第 231 行** `meta["sandbox"] = True`
  标记本次走了沙盒（供审计/指标）。

- **第 232 行** `return finish(out)`
  返回沙盒执行结果。

#### CRITICAL 分支（第 233-246 行）——审批 + 沙盒两道门

- **第 233 行** 注释：`# CRITICAL：审批 + 沙盒两道门缺一不可。`
- **第 234-235 行** `if self._human is None:` / `return finish(ok=False, error=_ERR_NO_HUMAN)`
  **先检查人工门**：无人工审核直接拒绝（`NO_HUMAN`）。

- **第 236 行** `question = f"Approve execution of tool {call.name} (call_id={call.call_id})?"`
  构造审批问题文本，含工具名和调用 ID（让审核人知道批的是什么）。

- **第 237 行** `answer = await self._human.ask(question, [_APPROVE, _OPT_DENY], ctx)`
  调用人工门询问，选项是常量 `_APPROVE`("approve") 和 `_OPT_DENY`("deny")。

- **第 238 行** `if answer.strip().lower() != _APPROVE:`
  **归一化比较**：答案去首尾空白 + 转小写后与 `_APPROVE` 常量比较。这样 "Approve"、" APPROVE "、"approve" 都算同意。
  - 右侧用常量 `_APPROVE` 而非字面量 `"approve"`（规则 5）。

- **第 239-240 行** 注释 + `return finish(ok=False, error=_ERR_APPROVAL_DENIED)`
  **不是 approve 就拒绝**（包括 deny 和任何其它回答）——审批默认拒绝（fail closed）。返回 `APPROVAL_DENIED`。

- **第 241-242 行** `if self._sandbox is None:` / `return finish(ok=False, error=_ERR_NO_SANDBOX)`
  审批通过后**再检查沙盒门**：无沙盒仍然拒绝。审批通过不能替代沙盒。

- **第 243 行** `out = await self._sandbox.run(fn, call.args, ctx)`
  两道门都过，沙盒执行。

- **第 244 行** `meta["approved"] = True`
  标记已审批。

- **第 245 行** `meta["sandbox"] = True`
  标记已沙盒。

- **第 246 行** `return finish(out)`
  返回结果。

#### 异常折叠（第 247-249 行）

- **第 247 行** `except Exception as exc:`
  捕获路由分派中抛出的任何异常——工具执行异常、沙盒异常、审计 `record` 异常、人工 `ask` 异常，全都在这里兜住。

- **第 248 行** 注释：`# 工具/沙盒/审计/审批任一异常都折叠为失败信封，不抛穿到调用方。`
  **第三安全红线**：异常不抛穿。调用方永远拿到 `ToolResult`，而不是需要处理到处乱飞的异常。

- **第 249 行** `return finish(ok=False, error=f"{_ERR_TOOL_ERROR}: {exc}")`
  折叠为 `TOOL_ERROR: <异常信息>` 的失败信封，异常详情拼进 error 字段供排查。

**本文件设计要点**：
- **danger 以 spec 为准**（第 193 行）是防伪造降级的命门。
- **四级路由**用连续的 `if` + `return`，每条路径提前返回，结构扁平无嵌套。
- **fail closed** 贯穿：高危无沙盒、CRITICAL 无人工/未批准一律拒绝，绝不降级。
- **统一出口 `finish`** 保证耗时、retryable、meta 副本不遗漏。
- **异常折叠**让调用方只面对 `ToolResult` 一种返回形态。

---

## 4. execution.py 逐行讲解（122 行）

三种执行类工具：代码执行（D10）、Shell（D11）、文件（D12）。共同特点是 **danger 一律 HIGH**，强制走 registry 的沙盒路由。职责切分：R5 管校验和白名单/虚拟根，R9 的 `SandboxPort` 管隔离。

### 第 1-10 行：模块 docstring

- **第 1 行** `"""R5 执行类工具（D10 代码 / D11 Shell / D12 文件）：R5 管工具面与校验，R9 管隔离。`
  一句话定位 + 职责切分。

- **第 3 行** `职责切分（能力域全景 D6/D10-D12）：`
- **第 4 行** `- R5：命令白名单、虚拟根/路径白名单、danger 分级（执行类一律 HIGH → 强制沙盒）；`
  R5 侧做三件事：Shell 命令白名单、文件路径白名单（虚拟根）、danger 定为 HIGH。

- **第 5 行** `- R9：SandboxPort 提供进程/容器隔离（默认实现归 R9，见 GAP-LIST G04）。`
  R9 侧提供真正的隔离实现。GAP-LIST G04 表明默认沙盒实现尚未落地——这是有意的缺口标记。

- **第 7-9 行** `为何不是裸执行：工具本体只做校验然后委托 SandboxPort.run；sandbox 缺席时返回失败` / `（缺席即拒绝），绝不退回裸奔直执行。SandboxPort.run 的可序列化限制（B.1.3）` / `归 R9 后续改签名，本模块不伪造隔离承诺。`
  **关键纪律**：沙盒缺席就拒绝，不裸奔。且本模块明确「不伪造隔离承诺」——不假装自己做了隔离，隔离完全委托 `SandboxPort`。

- **第 10 行** `"""` docstring 结束。

### 第 11-24 行：imports、导出、`_APPROVE` 常量

- **第 11 行** `from __future__ import annotations` 延迟注解。
- **第 13 行** `import os`
  路径校验用（`abspath` / `path.join` / `sep`）。
- **第 14 行** `from collections.abc import Callable` 执行体类型。
- **第 15 行** `from dataclasses import dataclass` 为 `_ExecOutcome` 提供装饰器。
- **第 17 行** `from compose import PortCtx, SandboxPort, ToolSpec` 导入上下文、沙盒门、spec DTO。
- **第 19 行** `from .types import DangerLevel` 风险等级。
- **第 20 行** `from runtime import DynamicValue` 动态值类型。
- **第 22 行** `__all__ = ["CodeExecTool", "FileExecTool", "ShellExecTool"]`
  只导出三个具体执行工具，基类 `_ExecTool` 和 `_ExecOutcome` 是内部实现。
- **第 24 行** `_APPROVE = True  # 稳定布尔常量（规则 5：避免魔法值散落）。`
  用常量True表示成功，而非到处写裸 `True`。

### 第 27-32 行：`_ExecOutcome`——执行结果内部形态

- **第 27 行** `@dataclass(frozen=True, slots=True)`
  冻结 + slots，内部 DTO 不可变、省内存。

- **第 28 行** `class _ExecOutcome:`
- **第 29 行** docstring：`"""执行结果的内部归一形态（ok + 可选值 + 错误码）。"""`
- **第 30 行** `ok: bool` 是否成功。
- **第 31 行** `value: DynamicValue = None` 成功时的值，默认 None。
- **第 32 行** `error: str = ""` 失败时的错误码/信息，默认空串。

  **为什么需要这个内部形态**：`_run` 统一返回它（成功带 value，失败带 error），子类 `execute` 再把它折叠成 `bool` 返回给调用方。若直接返回 bool 就丢了错误信息；直接抛异常又违背「异常折叠」纪律。

### 第 35-61 行：`_ExecTool` 基类

- **第 35 行** `class _ExecTool:`
- **第 36 行** docstring：`"""执行类工具基类：持有 sandbox 与 spec，统一路由到 SandboxPort。"""`
- **第 38 行** `danger = DangerLevel.HIGH  # 执行类一律 HIGH：内核 registry 会强制沙盒路由。`
  **类级常量**：所有执行类工具 danger 都是 HIGH。这是「执行类一律高危」的落点——registry 看到 HIGH 会强制沙盒（registry.py 第 226-232 行），无法绕过。

- **第 40-41 行** `__init__` 签名：
  - 第 40 行 `def __init__(self, *, sandbox: SandboxPort | None = None, name: str, description: str,`
  - 第 41 行 `properties: dict[str, DynamicValue], required: tuple[str, ...] = ()) -> None:`
  - sandbox 可缺席；name/description/properties 由子类给定；required 默认空元组。

- **第 42 行** docstring：`"""构造：sandbox 可缺席（缺席即拒绝执行）；spec 由子类给定 schema。"""`
- **第 43 行** `self._sandbox = sandbox` 保存沙盒依赖。
- **第 44-48 行** 构造 `ToolSpec`：
  - 第 45 行 `name=name, description=description,`
  - 第 46 行 `parameters_schema={"type": "object", "properties": properties, "required": list(required)},`
    schema 含 `properties` 键 —— 这会触发 registry 的 `extra=forbid` 参数校验（registry.py 第 198 行）。`required` 从 tuple 转成 list（JSON schema 用 list）。
  - 第 47 行 `danger=self.danger, source="local",`
    danger 取类级常量 HIGH，source 用裸字符串 `"local"`（这里没用 `ToolSource.LOCAL.value`，是直写字符串——与 decorator.py 第 138 行用枚举相比略不一致，但值相同）。
  - 第 48 行 `)` 构造结束。

- **第 50-52 行** `_run` 签名与 docstring：
  - 第 50 行 `async def _run(self, fn: Callable[..., DynamicValue], args: dict[str, DynamicValue],`
  - 第 51 行 `ctx: PortCtx) -> _ExecOutcome:`
  - 第 52 行 docstring：`"""统一执行入口：sandbox 缺席即拒绝；异常折叠为失败结果不抛穿。"""`

- **第 53 行** `if self._sandbox is None:`
- **第 54 行** 注释：`# 无隔离不可用：高危执行绝不裸奔直跑（fail closed）。`
- **第 55 行** `return _ExecOutcome(ok=False, error="NO_SANDBOX")`
  **fail closed**：无沙盒返回失败（`NO_SANDBOX`），绝不直接执行 fn。

- **第 56 行** `try:`
- **第 57 行** `value = await self._sandbox.run(fn, args, ctx)`
  委托沙盒执行。
- **第 58 行** `return _ExecOutcome(ok=_APPROVE, value=value)`
  成功返回（ok 为常量 True）。

- **第 59-61 行** 异常折叠：
  - 第 59 行 `except Exception as exc:`
  - 第 60 行 注释：`# 沙盒异常折叠为失败，交由上层 ToolResult 承载，不裸抛。`
  - 第 61 行 `return _ExecOutcome(ok=False, error=f"EXEC_ERROR: {exc}")`
  - 沙盒异常折叠为 `EXEC_ERROR: <异常>` 的失败结果，不抛穿。

  **为什么不在这里直接构造 ToolResult**：执行类工具的 `execute()` 是给调用方直接用的便捷方法（返回 bool），而 spec/fn 又会被注册进 registry 走统一 `call()`。内部用 `_ExecOutcome` 便于两条路复用。

### 第 64-77 行：`CodeExecTool`（D10 代码执行）

- **第 64 行** `class CodeExecTool(_ExecTool):`
- **第 65 行** docstring：`"""代码执行工具（D10）：模型生成的代码经 SandboxPort 隔离运行。"""`
- **第 66 行** `def __init__(self, *, sandbox: SandboxPort | None = None) -> None:`
- **第 67 行** docstring：`"""构造：code 参数为字符串源码。"""`
- **第 68-72 行** `super().__init__(` 调用基类：
  - 第 70 行 `sandbox=sandbox, name="code_exec", description="Execute code in isolation",`
  - 第 71 行 `properties={"code": {"type": "string"}}, required=("code",),`
  - 工具名 `code_exec`，唯一参数 `code`（字符串源码），必填。

- **第 74 行** `async def execute(self, code: str, ctx: PortCtx) -> bool:`
- **第 75 行** docstring：`"""执行源码字符串；返回是否成功（校验经 spec 由调用方负责）。"""`
- **第 76 行** `outcome = await self._run(lambda code: code, {"code": code}, ctx)`
  **执行体是 `lambda code: code`**——一个恒等函数。为什么：真正的沙盒执行由 `SandboxPort.run` 负责，这里传进去的 fn 只是「沙盒要执行的目标」。D10 的真实语义（怎么跑代码）在 R9 沙盒实现里，本模块只提供占位执行体和参数。

- **第 77 行** `return outcome.ok`
  折叠成 bool 返回。

### 第 80-99 行：`ShellExecTool`（D11 Shell）——命令白名单

- **第 80 行** `class ShellExecTool(_ExecTool):`
- **第 81 行** docstring：`"""Shell 工具（D11）：仅白名单命令可执行，危险命令在 R5 侧就拦截。"""`
- **第 83-84 行** `__init__` 签名：
  - 第 83 行 `def __init__(self, *, sandbox: SandboxPort | None = None,`
  - 第 84 行 `allowlist: tuple[str, ...] = ()) -> None:`
  - `allowlist` 是允许的命令首词元组，默认空元组。

- **第 85 行** docstring：`"""构造：allowlist 是允许的命令首词元组（空=拒绝一切，fail closed）。"""`
  **空白名单 = 拒绝一切**——fail closed 的又一体现。

- **第 86 行** `self._allowlist = allowlist` 保存白名单。
- **第 87-90 行** `super().__init__(` 调用基类：
  - 第 88 行 `sandbox=sandbox, name="shell_exec", description="Execute allowlisted commands",`
  - 第 89 行 `properties={"command": {"type": "string"}}, required=("command",),`
  - 工具名 `shell_exec`，参数 `command`（字符串命令），必填。

- **第 92 行** `async def execute(self, command: str, ctx: PortCtx) -> bool:`
- **第 93 行** docstring：`"""校验命令首词在白名单内再交 sandbox；非白名单零执行。"""`
- **第 94 行** `head = command.strip().split(" ", 1)[0] if command.strip() else ""`
  **取命令首词**：先 `strip()` 去首尾空白，再 `split(" ", 1)[0]` 取第一个空格前的部分（命令名）。空命令则 head 为空串。
  - 注意：`split(" ", 1)` 只切一次，取 `[0]` 是首词。这里对 `\t` 分隔不生效（只按空格切），是简化实现。

- **第 95 行** `if head not in self._allowlist:`
- **第 96 行** 注释：`# 白名单外命令直接拒绝，连 sandbox 都不惊动（最小攻击面）。`
- **第 97 行** `return False`
  **最小攻击面**：白名单外命令直接返回 False，**连沙盒都不调用**。这样危险命令根本不会进入执行通道，比「交给沙盒但沙盒拦」更保守。

- **第 98 行** `outcome = await self._run(lambda command: command, {"command": command}, ctx)`
  白名单内才交沙盒，执行体同样是恒等 lambda。

- **第 99 行** `return outcome.ok`
  折叠成 bool。

### 第 102-122 行：`FileExecTool`（D12 文件）——虚拟根 + 路径逃逸校验

- **第 102 行** `class FileExecTool(_ExecTool):`
- **第 103 行** docstring：`"""文件工具（D12）：虚拟根 + 路径白名单，越界路径（../）在 R5 侧拦截。"""`
- **第 105 行** `def __init__(self, *, sandbox: SandboxPort | None = None, root: str = "") -> None:`
- **第 106 行** docstring：`"""构造：root 为虚拟根（空串=不开放任何路径，fail closed）。"""`
  空 root = 不开放任何路径——fail closed。

- **第 107 行** `self._root = os.path.abspath(root) if root else ""`
  **把 root 转成绝对路径**：`abspath` 消除 `.` / `..` 和相对路径歧义，得到规范的绝对根。若 root 为空串则保持空串（表示未开放）。

- **第 108-111 行** `super().__init__(` 调用基类：
  - 第 109 行 `sandbox=sandbox, name="file_exec", description="Read files within virtual root",`
  - 第 110 行 `properties={"path": {"type": "string"}}, required=("path",),`
  - 工具名 `file_exec`，参数 `path`（字符串路径），必填。

- **第 113 行** `async def read(self, path: str, ctx: PortCtx) -> bool:`
- **第 114 行** docstring：`"""校验路径在虚拟根内再交 sandbox；越界/空根直接拒绝。"""`
- **第 115 行** `if not self._root:`
- **第 116 行** `return False`
  空根直接拒绝（没有开放任何路径）。

- **第 117 行** `candidate = os.path.abspath(os.path.join(self._root, path))`
  **拼接并规范化候选路径**：`join(root, path)` 拼上相对路径，再 `abspath` 规范化。若 path 含 `../`，abspath 会把它解析掉——所以第 118 行必须检查解析后的结果是否还在根内。

- **第 118 行** `if not (candidate == self._root or candidate.startswith(self._root + os.sep)):`
  **路径逃逸检查**（本模块最关键的安全行）：候选路径必须满足以下之一才算安全：
  1. `candidate == self._root`：正好等于根路径本身。
  2. `candidate.startswith(self._root + os.sep)`：以「根路径 + 路径分隔符」开头，即是根的子路径。
  - **为什么要加 `os.sep`**：若只判断 `candidate.startswith(self._root)`，`/home/user` 会被认为在 `/home/u` 根内（前缀误判）。加上分隔符确保是「路径段」级别的包含关系。
  - **为什么用 abspath 后的 candidate**：`../` 已被 abspath 解析，逃逸路径会变成根外的绝对路径，被这个检查挡下。绝对路径 path（如 `/etc/passwd`）在 `join(root, "/etc/passwd")` 中会直接变成 `/etc/passwd`，也被挡下。
  - **不这么写会怎样**：路径遍历攻击（`../../etc/passwd`）可读取沙盒外的文件。

- **第 119 行** 注释：`# 路径逃逸虚拟根：拒绝（阻断 ../ 与绝对路径绕过）。`
- **第 120 行** `return False`
  越界直接拒绝，不惊动沙盒（与 Shell 白名单同样的最小攻击面策略）。

- **第 121 行** `outcome = await self._run(lambda path: path, {"path": candidate}, ctx)`
  通过校验才交沙盒。注意传进去的是**规范化后的 `candidate`** 而非原始 `path`——沙盒拿到的是已确认在根内的安全路径，杜绝二次解析歧义。

- **第 122 行** `return outcome.ok`
  折叠成 bool。

**本文件设计要点**：
- **执行类一律 HIGH**（第 38 行），强制沙盒，无法绕过。
- **沙盒缺席即拒绝**（第 55 行），从不裸奔。
- **R5 侧前置校验**：Shell 命令白名单（第 95 行）、文件虚拟根逃逸检查（第 118 行）都在进沙盒前完成，最小攻击面。
- **路径检查用 abspath + sep 前缀**（第 117-118 行），防 `../` 和绝对路径逃逸。
- **内部 `_ExecOutcome` 统一成功/失败形态**，子类折叠成 bool 给调用方。

---

## 5. index.py 逐行讲解（81 行）——三级披露

`ToolIndex` 实现 `DisclosureIndex`，把 `ToolRegistry` 投影成三级披露包：L1 目录（无完整 schema）/ L2 命中项的完整 spec / L3 技能正文。Token 预算是硬约束：超限抛错；工具总数超阈值且无命中意图时强制 L1-only。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R5 三级披露索引：ToolIndex 把 ToolRegistry 投影为 L1 目录 / L2 schema / L3 正文。`
  一句话定位。

- **第 3 行** `Token 预算是硬约束：超限显式拒绝；合集超过阈值且无命中意图时强制 L1-only。`
  第一条纪律：预算超限抛错，超阈值无命中强制 L1-only。

- **第 4-5 行** `L3 技能正文需要 SkillPort 参与：注入后按技能清单加载正文（命中注入折叠为空串），` / `未注入时 l3 留空——协议签名不变，ToolIndex 作为增强实现承载正文加载。`
  第二条纪律：L3 需要注入 `SkillPort` 才加载；命中注入检测抛 ValueError 时折叠为空串（不抛穿）。协议签名不变（disclose 仍收 names + skills），ToolIndex 作为增强实现承载正文加载。

- **第 6 行** `"""` docstring 结束。

### 第 7-13 行：imports 与导出

- **第 7 行** `from __future__ import annotations` 延迟注解。
- **第 9 行** `from compose import DisclosureIndex, DisclosurePacket, SkillManifest, SkillPort, ToolSpec`
  - `DisclosureIndex`：实现的协议。
  - `DisclosurePacket`：返回的披露包 DTO（l1/l2/l3/token_cost）。
  - `SkillManifest`：技能清单 DTO。
  - `SkillPort`：技能能力门（用于加载 L3 正文）。
  - `ToolSpec`：工具 spec。
- **第 11 行** `from .registry import ToolRegistry` 持有 registry 引用。
- **第 13 行** `__all__ = ["ToolIndex"]` 只导出一个类。

### 第 16-37 行：类属性 + `__init__`（两个关键常量）

- **第 16 行** `class ToolIndex:`
- **第 17 行** docstring：`"""披露索引：DisclosureIndex 的默认实现（name="disclosure"）。"""`
- **第 19 行** `name: str = "disclosure"`
  port 名（与 capability.py 第 30 行的 `_PORT_DISCLOSURE` 对应）。
- **第 20 行** `_registry: ToolRegistry` 注册表引用。
- **第 21 行** `_skills: SkillPort | None` 技能门（可缺席）。
- **第 22 行** `_l1_token_budget: int` L1 token 预算。
- **第 23 行** `_force_threshold: int` 强制 L1-only 的工具数阈值。

- **第 25-32 行** `__init__` 签名：
  - 第 25 行 `def __init__(`
  - 第 27 行 `registry: ToolRegistry,` 必填位置参数。
  - 第 29 行 `skills: SkillPort | None = None,` 技能门可缺席。
  - **第 30 行** `l1_token_budget: int = 2048,` **常量一**：默认 token 预算 2048。
  - **第 31 行** `force_threshold: int = 20,` **常量二**：默认阈值 20（工具总数超过 20 且无命中意图时强制 L1-only）。
  - 第 32 行 `) -> None:`

- **第 33 行** docstring：`"""构造索引：注入 registry 与可选 SkillPort，配置 L1 token 预算与强制披露阈值。"""`
- **第 34 行** `self._registry = registry`
- **第 35 行** `self._skills = skills`
- **第 36 行** `self._l1_token_budget = l1_token_budget`
- **第 37 行** `self._force_threshold = force_threshold`

### 第 39-81 行：`disclose(names, skills)`——生成披露包

- **第 39 行** `def disclose(self, names: list[str], skills: list[SkillManifest]) -> DisclosurePacket:`
- **第 40 行** docstring 首行：`"""按命中名与技能清单生成披露包；超预算抛 ValueError。`
- **第 42-46 行** 参数/返回说明：names 是命中意图的工具名，skills 用于按 name 加载 L3 正文。

#### L1 目录（第 48 行）

- **第 48 行** `l1: tuple[ToolSpec, ...] = tuple(self._registry.catalog(level=1))`
  **总是取全量 L1 目录**（catalog(level=1) 已清空 schema，见 registry.py 第 127 行）。转成 tuple 保证不可变。

#### L2 命中项（第 49-55 行）

- **第 49 行** `if names:`
  仅当有命中意图（names 非空）才展开 L2。

- **第 50 行** 注释：`# 有命中才展开 L2：整目录过滤，保持注册顺序。`
- **第 51-53 行** `l2: tuple[ToolSpec, ...] = tuple(` / `spec for spec in self._registry.catalog(level=2) if spec.name in names` / `)`
  从完整目录（level=2）里过滤出名字在 `names` 里的 spec，保持注册顺序（catalog 返回顺序）。

- **第 54 行** `else:`
- **第 55 行** `l2 = ()`
  无命中意图则 L2 为空元组——**不预先把全部 schema 塞给模型**。

#### L3 技能正文（第 56-65 行）

- **第 56 行** 注释：`# 注入 SkillPort 才加载 L3：按技能清单逐个 name 取正文。`
- **第 57 行** `l3: dict[str, str] = {}`
  技能名 → 正文的字典。
- **第 58 行** `if self._skills is not None:`
  只有注入了 SkillPort 才加载正文。
- **第 59 行** `for manifest in skills:`
  遍历技能清单。
- **第 60 行** `try:`
- **第 61 行** `body = self._skills.body(manifest.name)`
  按技能名加载正文（FileSkillProvider.body 会做注入检测，见 05-skills 文档第 176-179 行）。
- **第 62 行** `except ValueError:`
  **捕获 ValueError**：`body()` 在正文命中注入时抛 ValueError（见 skills 模块）。
- **第 63-64 行** 注释 + `body = ""`
  `# 正文命中注入检测：留空串而非抛穿，避免不可信正文进入披露包。`
  **关键降级**：命中注入不抛穿给用户，而是把该技能正文留空串。这样不可信正文不会进披露包，同时披露流程不中断（一个技能的问题不影响其它技能）。
- **第 65 行** `l3[manifest.name] = body`
  记录正文（可能是空串）。

#### 成本计算（第 66-71 行）

- **第 66 行** 注释：`# 粗略成本：L1 算 name+description，L2 算 schema，L3 计入已加载正文长度。`
- **第 67-71 行** `token_cost = (` 求和：
  - 第 68 行 `sum(len(s.name) + len(s.description) for s in l1)`：L1 成本 = 各工具「名字长度 + 描述长度」之和。
  - 第 69 行 `+ sum(len(str(s.parameters_schema)) for s in l2)`：L2 成本 = 各命中项 schema 字符串长度之和。
  - 第 70 行 `+ sum(len(body) for body in l3.values())`：L3 成本 = 各技能正文长度之和。
  - 第 71 行 `)` 结束。

  **为什么是「粗略」**：用字符长度近似 token 数，不是精确分词。够用作预算守门，避免引入 tokenizer 依赖。

#### 强制 L1-only（第 72-75 行）

- **第 72 行** `total = len(self._registry.catalog())`
  取工具总数（catalog() 默认 level=1，但这里只要长度，用哪个 level 一样）。
- **第 73 行** `if total > self._force_threshold and not names:`
  条件：工具总数超过阈值（默认 20）**且**无命中意图（names 为空）。
- **第 74 行** 注释：`# 合集过大且无命中意图：强制 L1-only，L2 保持空（不报错、不全量注入）。`
- **第 75 行** `l2 = ()`
  L2 强制清空。注意此时 L2 本来就是空（因为 names 空时第 55 行已设 `l2=()`），这行是**显式兜底**——强调「工具太多时绝不因无命中而全量注入 schema」。

  - **为什么要这个阈值**：工具很多时，若模型没表达明确意图就给一大堆 schema 会严重膨胀上下文。强制只看 L1 目录，让模型先收窄意图。

#### 预算检查（第 76-80 行）

- **第 76 行** `if token_cost > self._l1_token_budget:`
- **第 77 行** 注释：`# 预算超限必须显式拒绝，避免静默把超量上下文塞给模型。`
- **第 78-80 行** `raise ValueError(` / `f"Disclosure token budget exceeded: {token_cost} > {self._l1_token_budget}"` / `)`
  **超预算抛 ValueError**：显式拒绝，不静默塞超量上下文。调用方（通常是 capability.build 的编排层）需要捕获它并决定怎么降级。

#### 返回（第 81 行）

- **第 81 行** `return DisclosurePacket(l1=l1, l2=l2, l3=l3, token_cost=token_cost)`
  构造披露包返回。

**本文件设计要点**：
- **三级披露**：L1 全目录（无 schema）/ L2 命中项完整 spec / L3 技能正文（需注入 SkillPort）。
- **L3 命中注入降级为空串**（第 64 行），不抛穿——一个技能的问题不拖垮整个披露。
- **成本用字符长度粗估**（第 67-71 行），够守门且无额外依赖。
- **超阈值无命中强制 L1-only**（第 73-75 行），防上下文膨胀。
- **超预算抛 ValueError**（第 78 行），不静默塞超量上下文。

---

## 6. mcp.py 逐行讲解（128 行）

MCP 合并：定义传输层协议 `MCPTransport`（用户扩展点）与门面 `MCPManager`。MCP 工具以 `mcp__<server>__<tool>` 全名注入同一个 `ToolRegistry`，使 catalog/call 与本地工具无差别。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R5 MCP 合并：本地传输协议 MCPTransport 与 MCPPort 实现 MCPManager。`
  一句话定位：协议 + 门面。

- **第 3 行** `MCP 工具以 mcp__<server>__<tool> 全名注入同一 ToolRegistry，`
  说明全名格式 `mcp__<server>__<tool>`，注入目标是同一个 registry。

- **第 4 行** `使 catalog/call 与本地工具无差别；health 走 transport.ping。`
  **关键设计**：MCP 工具与本地工具在目录/调用层无差别——统一走 registry 的四级路由；health 单独走 transport 的 ping。

- **第 5 行** `"""` docstring 结束。

### 第 6-17 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。
- **第 8 行** `from collections.abc import Callable` 闭包返回类型。
- **第 9 行** `from typing import Protocol, runtime_checkable`
  定义可 `isinstance` 检查的协议。
- **第 11 行** `from compose import MCPPort, PortCtx, ToolSpec`
  - `MCPPort`：框架消费的门面（本模块 Manager 实现它）。
  - `PortCtx`：上下文。
  - `ToolSpec`：工具 spec。
- **第 12 行** `from runtime import DynamicValue` 动态值。
- **第 14 行** `from .registry import ToolRegistry` 注入目标。
- **第 15 行** `from .types import ToolSource` 来源枚举（改 spec.source 用）。
- **第 17 行** `__all__ = ["MCPTransport", "MCPManager"]` 导出协议与门面。

### 第 20-58 行：`MCPTransport` 协议——框架唯一的 MCP 扩展点

- **第 20 行** `@runtime_checkable`
  让 `isinstance(client, MCPTransport)` 可用。**代价**：只能检查成员存在，不能检查方法签名。

- **第 21 行** `class MCPTransport(Protocol):`
  传输层协议定义。

- **第 22-25 行** docstring 开头：`"""**用户实现自定义 MCP 客户端的接口**（框架唯一的 MCP 客户端扩展点）。` + `只需实现本协议的三个方法 + name 属性，就能把自己的 MCP 客户端接进框架：` + `MCPManager(registry, 你的客户端)。框架自带的 stdio/http 实现也遵守本协议。`
  **扩展点说明**：用户实现三个 async 方法 + `name` 属性即可接入自己的 MCP 客户端。框架内置实现也遵守本协议。

- **第 27-36 行** 三方法契约（实现前必读）——这段 docstring 全是硬约束：
  - **第 28-31 行** `list_tools(server)`：返回工具清单。**工具名用原始名，不要加 `mcp__` 前缀**——前缀由 `MCPManager.connect` 统一加，重复加会导致双前缀。ToolSpec 需填 name/description/parameters_schema/danger/source；danger 必填，决定后续沙盒/审批路由。
  - **第 32-34 行** `call_tool(server, tool, args)`：tool 是原始名；args 为参数字典；返回值形态由工具决定（DynamicValue），框架包成 ToolResult。**异常不要吞**——抛出后由上层折叠为失败信封（registry.py 第 247 行）。
  - **第 35-36 行** `ping(server)`：可达性探测，返回 bool；不要抛异常（health 会折叠，但返回 False 更直接）。

- **第 38-39 行** 与 MCPPort 的分工：`MCPPort（R2，带 ctx）是框架消费的能力门面，由 MCPManager 实现；` / `本协议是**传输层**，不带 ctx，由用户/内置适配器实现。两者别混。`
  **关键区分**：
  - `MCPPort`（compose 层，带 ctx）：框架消费门面，由 Manager 实现。
  - `MCPTransport`（本模块，不带 ctx）：传输层，由用户/内置适配器实现。
  - 为什么带不带 ctx 有区别：门面层有调用上下文（trace/审计归因），传输层只管网络协议。

- **第 41 行** `如何验证实现合规：isinstance(你的客户端, MCPTransport) 为 True（runtime_checkable）。`

- **第 44 行** `name: str` 协议要求实现的具名属性。

- **第 46-48 行** `list_tools` 声明：
  - 第 46 行 `async def list_tools(self, server: str) -> list[ToolSpec]:`
  - 第 47 行 docstring：`"""列出 server 暴露的工具（原始名，未加前缀）。"""`
  - 第 48 行 `...` Protocol 方法体用省略号（接口声明，非代码省略）。

- **第 50-54 行** `call_tool` 声明：
  - 第 50-52 行签名（多行）。
  - 第 53 行 docstring：`"""调用工具，返回形态由工具决定（动态边界）。"""`
  - 第 54 行 `...`

- **第 56-58 行** `ping` 声明：
  - 第 56 行 `async def ping(self, server: str) -> bool:`
  - 第 57 行 docstring：`"""探测 server 可达性。"""`
  - 第 58 行 `...`

### 第 61-81 行：`MCPManager` 构造 + `_invoker`

- **第 61 行** `class MCPManager:`
- **第 62 行** docstring：`"""MCP 能力包：把外部工具注入 registry，并提供带 ctx 的 MCPPort 门面。"""`
- **第 64 行** `name: str = "mcp"` port 名。
- **第 65 行** `_registry: ToolRegistry` 注入目标。
- **第 66 行** `_transport: MCPTransport` 底层客户端。
- **第 67 行** `_servers: list[str]` 已连接 server 列表（供 health 用）。
- **第 68-73 行** `__init__`：
  - 第 69 行 `def __init__(self, registry: ToolRegistry, transport: MCPTransport) -> None:`
  - 第 70 行 docstring：`"""构造门面：registry 为注入目标，transport 为底层 MCP 客户端。"""`
  - 第 71 行 `self._registry = registry`
  - 第 72 行 `self._transport = transport`
  - 第 73 行 `self._servers = []`

- **第 75-81 行** `_invoker(server, tool_name)`——闭包绑定参数：
  - **第 75 行** `def _invoker(self, server: str, tool_name: str) -> Callable[..., DynamicValue]:`
  - **第 76 行** docstring：`"""构造绑定到 (server, tool_name) 的调用闭包（参数绑定避免循环晚绑定）。"""`
    **为什么需要闭包**：registry 存的 fn 签名固定为 `**kwargs -> DynamicValue`，但调用 MCP 工具需要知道是哪个 server、哪个 tool。用闭包把这两个值「冻结」在函数里，返回的 `invoke` 只需接收 args。
    - 若不用闭包而在循环里用 lambda 直接引用循环变量，会遭遇 Python 的**循环晚绑定**——所有 lambda 都引用最后一次循环的值。闭包工厂把值作为默认参数/外层变量绑定，规避此问题。
  - **第 77 行** `async def invoke(**args: DynamicValue) -> DynamicValue:`
  - **第 78 行** 注释：`# 为何：transport 签名不含 ctx，这里只转发参数；ctx 由上层 port 负责。`
  - **第 79 行** `return await self._transport.call_tool(server, tool_name, args)`
    转发给 transport，server/tool_name 来自闭包绑定，args 来自调用。
  - **第 81 行** `return invoke`
    返回闭包。

### 第 83-103 行：`connect(server, ctx)`——发现并注入工具（MCP 全名前缀由 Manager 加）

- **第 83 行** `async def connect(self, server: str, ctx: PortCtx) -> list[ToolSpec]:`
- **第 84 行** docstring 首行：`"""发现并注入 server 的工具，返回加前缀后的 spec。`
- **第 86-90 行** 参数/返回说明：server 是标识；ctx 保留签名对齐（本协议不需要）；返回已注入 registry 的全名 ToolSpec 列表。

- **第 91 行** `if server not in self._servers:`
- **第 92-93 行** 注释 + `self._servers.append(server)`
  `# 记录已连接 server 供 health 使用；重复 connect 不重复记账。`
  **幂等记账**：同一个 server 重复 connect 不会重复加入列表（health 不会重复 ping）。

- **第 94 行** `specs = await self._transport.list_tools(server)`
  从 transport 拿原始 spec（原始工具名，未加前缀）。

- **第 95 行** `injected: list[ToolSpec] = []`
  收集注入后的 spec。

- **第 96 行** `for orig in specs:  # 逐条改全名、改来源并注册，保持 transport 返回顺序。`
  遍历原始 spec。

- **第 97 行** `fqname = f"mcp__{server}__{orig.name}"`
  **加全名前缀**：`mcp__<server>__<原始名>`。防不同 server 的同名工具冲突，也标识这是 MCP 工具。
  - 呼应 transport 协议第 28-29 行的约定：transport 返回原始名，**前缀只在这里加**，重复加会双前缀。

- **第 98-100 行** `spec = orig.model_copy(` / `update={"name": fqname, "source": ToolSource.MCP.value}` / `)`
  用 `model_copy` 浅拷贝原 spec，改两处：
  - `name` → 全名 `fqname`。
  - `source` → `ToolSource.MCP.value`（标记为 MCP 来源）。
  - **为什么不改原对象**：原 spec 来自 transport，拷贝后注入避免污染 transport 的数据。

- **第 101 行** `self._registry.register(spec, self._invoker(server, orig.name))`
  **注入同一 Registry**：用全名 spec + 闭包执行体注册。注意注册用的是**原始名** `orig.name`（闭包里传给 `call_tool` 的也是原始名），因为 transport 认原始名。这样 MCP 工具与本地工具在 registry 里无差别，走同一套四级路由。

- **第 102 行** `injected.append(spec)` 记录注入后的 spec。
- **第 103 行** `return injected` 返回。

### 第 105-117 行：带 ctx 的门面方法（list_tools / call_tool / ping）

- **第 105-107 行** `list_tools`：
  - 第 105 行 `async def list_tools(self, server: str, ctx: PortCtx) -> list[ToolSpec]:`
  - 第 106 行 docstring：`"""委托 transport 列出原始工具（带 ctx 门面，不改全名）。"""`
  - 第 107 行 `return await self._transport.list_tools(server)`
  - 直接委托 transport，返回原始工具（不改全名）。注意这里 ctx 没用——只为门面签名对齐。

- **第 109-113 行** `call_tool`：
  - 第 109-111 行签名（多行，带 ctx）。
  - 第 112 行 docstring：`"""委托 transport 调用原始工具（带 ctx 门面）。"""`
  - 第 113 行 `return await self._transport.call_tool(server, tool, args)`
  - 委托 transport 调原始工具。

- **第 115-117 行** `ping`：
  - 第 115 行 `async def ping(self, server: str, ctx: PortCtx) -> bool:`
  - 第 116 行 docstring：`"""委托 transport 探测 server 可达性。"""`
  - 第 117 行 `return await self._transport.ping(server)`
  - 委托 transport。

  **这三个门面方法的意义**：它们实现 `MCPPort`（带 ctx），是框架消费的统一入口；而 transport 是底层（不带 ctx）。Manager 在两者间做适配。

### 第 119-128 行：`health(ctx)`——健康检查（单点异常记 False）

- **第 119 行** `async def health(self, ctx: PortCtx) -> dict[str, bool]:`
- **第 120 行** docstring：`"""对所有已连接 server 逐一 ping，返回可达性字典。"""`
- **第 121 行** `result: dict[str, bool] = {}` 结果字典。
- **第 122 行** `for server in self._servers:  # 逐个探测，单点异常记为 False 而非中断整体健康检查。`
  遍历已连接 server。
- **第 123 行** `try:`
- **第 124 行** `result[server] = await self._transport.ping(server)`
  正常记录 ping 结果。
- **第 125 行** `except Exception:`
- **第 126 行** 注释：`# ping 失败即视为不可达，健康接口不抛穿到调用方。`
- **第 127 行** `result[server] = False`
  **单点异常记 False**：某个 server ping 抛异常，记为不可达，不中断整体健康检查，也不抛穿。这样一个 server 挂了不影响其它 server 的健康报告。
- **第 128 行** `return result`

**本文件设计要点**：
- **MCPTransport 是唯一扩展点**（第 21 行），用户实现三方法 + name 即可接入。
- **前缀由 Manager 加**（第 97 行），transport 返回原始名——避免双前缀。
- **MCP 工具注入同一 Registry**（第 101 行），与本地工具在 catalog/call 层无差别。
- **闭包绑定 server/tool**（第 75-81 行），规避循环晚绑定，让 registry 的统一 fn 签名能携带 MCP 路由信息。
- **health 单点异常记 False**（第 127 行），不中断整体。

---

## 7. webfetch.py 逐行讲解（81 行）

`WebFetchTool`（D13 网页访问）：抓取网页正文，一律视为不可信输入，L3 披露前必须过注入检测；命中注入则**拒载（抛 ValueError）而非净化**，并记审计。网络 IO 由注入的 `WebFetcher` 承担，本模块不 import IO SDK。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R5 工具封装：WebFetchTool（D13 浏览器/Web 访问）。`
  一句话定位。

- **第 3-4 行** `纪律：抓取正文一律视为不可信输入，L3 披露前必须过 InjectionDetector 检测；` / `命中注入则拒载并记审计。网络 IO 由注入的 WebFetcher 承担，本模块不 import IO SDK`
  第一条纪律：不可信输入必须过检测，命中拒载 + 审计。网络 IO 靠注入，不 import SDK。

- **第 4-5 行** `（规则 8：能力插件不直接依赖 httpx/requests），单测用 fake fetcher 闭环。`
  规则 8：能力插件不直接依赖具体 IO 库（httpx/requests）。好处：单测用 fake fetcher 就能闭环，不需要真实网络。

- **第 6 行** `"""` docstring 结束。

### 第 7-19 行：imports 与导出

- **第 7 行** `from __future__ import annotations` 延迟注解。
- **第 9 行** `from typing import Protocol, runtime_checkable` 定义可 isinstance 的抓取协议。
- **第 11 行** `from compose import AuditPort, PortCtx, ToolSpec`
  - `AuditPort`：审计门（命中/干净都留痕）。
  - `PortCtx`：上下文。
  - `ToolSpec`：工具 spec。
- **第 13 行** `from .types import DangerLevel` 风险等级。
- **第 15 行** `from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector`
  **横切复用**：从 skills 模块导入注入检测协议、裁决枚举、默认检测器。这正是 05-skills 文档说的「detector 被 tools/rag/memory 三处复用」的 tools 这一处。
- **第 17 行** `from .types import ToolSource` 来源枚举。
- **第 19 行** `__all__ = ["WebFetcher", "WebFetchTool"]` 导出抓取协议与工具。

### 第 22-29 行：`WebFetcher` 协议

- **第 22 行** `@runtime_checkable` 支持 isinstance 检查。
- **第 23 行** `class WebFetcher(Protocol):`
- **第 24 行** docstring：`"""网页抓取后端（用户可注入真实 HTTP/浏览器客户端；R5 不内置 IO SDK）。"""`
- **第 25 行** `name: str` 具名属性（可插拔物必须具名的纪律）。
- **第 27-29 行** `fetch` 声明：
  - 第 27 行 `async def fetch(self, url: str, ctx: PortCtx) -> str:`
  - 第 28 行 docstring：`"""抓取给定 URL 的原始正文（形态为文本，交由检测器净化）。"""`
  - 第 29 行 `...` Protocol 方法体省略号。

### 第 32-62 行：`WebFetchTool` 类 + `__init__`

- **第 32 行** `class WebFetchTool:`
- **第 33-37 行** docstring：
  - 第 33 行 `"""WebFetchTool：抓取 + 注入检测 + 审计，命中注入拒载。`
  - 第 35-36 行 字段说明：`name: 工具名。spec: 供统一 Registry 注册的目录条目（danger=SAFE，检测在工具内强制）。`
    - **注意**：这行 docstring 写的是 `danger=SAFE`，但下面第 60 行实际代码是 `danger=DangerLevel.LOW`。**以代码为准，实际是 LOW**。这是文档注释与实现不一致的旧注释，阅读时别被误导。
- **第 39 行** `name = "web_fetch"` 工具名。

- **第 41-47 行** `__init__` 签名：
  - 第 42 行 `fetcher: WebFetcher,` 必填：抓取后端。
  - 第 45 行 `detector: InjectionDetector | None = None,` 检测器可缺省。
  - 第 46 行 `audit: AuditPort | None = None,` 审计可缺省。
  - 第 47 行 `) -> None:`

- **第 48 行** docstring：`"""装配抓取后端、注入检测器与可选审计 Port。"""`
- **第 49 行** `self._fetcher = fetcher` 保存抓取后端。
- **第 50 行** `self._detector = detector or KeywordInjectionDetector()`
  **缺默认检测器**：传 None 或假值都用默认检测器。用 `or` 而非 `if is not None`——检测器对象通常为真值，效果等价但更简洁。保证任何情况下都有检测器（fail closed），不存在「未注入就不检测」的放行分支。

  - 对比 skills 模块第 131 行用 `detector if detector is not None else ...`：这里用 `or` 更简短，但若有人传入一个 `__bool__` 返回 False 的检测器对象会被误替换——实际不会发生。

- **第 51 行** `self._audit = audit` 保存审计门。

- **第 52-62 行** 构造 `ToolSpec`：
  - 第 53 行 `name=self.name,`
  - 第 54 行 `description="Fetch web page body (untrusted input, injection-checked)",`
  - 第 55-59 行 参数 schema：
    - 第 57 行 `"properties": {"url": {"type": "string"}},`
    - 第 58 行 `"required": ["url"],`
    - 参数 `url`（字符串），必填。
  - **第 60 行** `danger=DangerLevel.LOW,  # 外部内容风险：走审计路径（R9 §5 LOW=审计）`
    **实际 danger 是 LOW**：外部网页内容有风险，走审计路径（LOW = 审计后执行）。检测在工具内部强制（第 67-68 行），不依赖 registry 的路由。
    - 注意 docstring 第 36 行说 SAFE 是旧注释，以这里的 LOW 为准。
  - 第 61 行 `source=ToolSource.LOCAL.value,` 来源本地。

### 第 64-81 行：`fetch(url, ctx)`——抓取 + 检测 + 拒载

- **第 64 行** `async def fetch(self, url: str, ctx: PortCtx) -> str:`
- **第 65 行** docstring：`"""抓取并检测：命中注入则拒载（ValueError）并审计；干净正文放行。"""`
- **第 66 行** `raw = await self._fetcher.fetch(url, ctx)`
  抓取原始正文（文本）。

- **第 67 行** `verdict = self._detector.detect(raw)`
  过注入检测，拿裁决枚举（`CLEAN` 或 `INJECTED`，见 05-skills 文档第 26-35 行）。

- **第 68 行** `if verdict is InjectionVerdict.INJECTED:`
  用 `is` 比较枚举成员（单例）。

- **第 69 行** 注释：`# 不可信正文命中注入：拒载而非净化，避免把带攻击载荷的文本送进模型上下文。`
  **拒载不净化**：命中注入就拒绝加载，不做「删掉可疑片段后放行」的净化。因为净化可能不彻底，带攻击载荷的文本一旦进模型上下文就可能成功的 prompt injection。

- **第 70 行** `if self._audit is not None:`
- **第 71-73 行** `await self._audit.record(` 命中也记审计：
  - 第 72 行 `{"tool": self.name, "url": url, "verdict": verdict.value}, ctx`
  - 记录工具名、URL、裁决值——安全事件留痕。
- **第 74 行** `raise ValueError(f"Injection detected in fetched body; refusing to load: {url}")`
  **抛 ValueError 拒载**：命中注入抛异常，由上层（比如 ToolIndex 第 62 行 L3 加载，或 registry.call 的第 247 行异常折叠）决定怎么降级。

- **第 76 行** `if self._audit is not None:`
- **第 77 行** 注释：`# 干净正文也留痕，满足 R9「网页视为不可信」的可审计要求。`
- **第 78-80 行** `await self._audit.record(` 干净也记审计：
  - 第 79 行 `{"tool": self.name, "url": url, "verdict": verdict.value}, ctx`
  - 即便干净也留痕，因为网页本就不可信，所有抓取都该可审计。

  - **对比命中与干净两处审计**：命中时 verdict 是 `INJECTED`，干净时是 `CLEAN`，审计记录靠 verdict 字段区分。两处都留痕，区别只在是否抛异常。

- **第 81 行** `return raw`
  干净正文原样返回（不净化、不改写）。

**本文件设计要点**：
- **抓取正文一律不可信**，必过检测（第 67 行）。
- **命中注入拒载不净化**（第 69、74 行），抛 ValueError 由上层降级。
- **命中与干净都留审计**（第 71、78 行），verdict 字段区分。
- **检测器缺省必填**（第 50 行），无缺席放行分支。
- **网络 IO 靠注入 WebFetcher**（第 43 行），不 import IO SDK，规则 8。
- **danger 实际是 LOW**（第 60 行），走审计路由（docstring 第 36 行的 SAFE 是旧注释，以代码为准）。

---

## 8. capability.py 逐行讲解（122 行）

`ToolsCapability`：把 registry / mcp / skills / disclosure 组装成默认能力包（规则 9——协议可注入则必须有开箱默认实现）。`name="tools"`、`version="5.0.0"`、`requires=()`。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R5 能力装配：ToolsCapability 把 registry/mcp/skills/disclosure 组装成默认能力包。`
  一句话定位。

- **第 3-4 行** `缺席即构造内置默认实现（规则 9）：registry 缺则用 R9 三 Port 建 ToolRegistry，` / `disclosure 缺则用该 registry 建 ToolIndex；mcp/skills 可缺席为 None。`
  规则 9 落地：registry 缺席用三 Port 建默认 `ToolRegistry`；disclosure 缺席用该 registry 建默认 `ToolIndex`；mcp/skills 可缺席为 None（它们是增强项，不是必需）。

- **第 5 行** `"""` docstring 结束。

### 第 6-26 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。
- **第 8 行** `from compose.tool_layer import layer_program` 由 build() 构造 ToolLayer Program。
- **第 9 行** `from runtime import Program` Program 类型。
- **第 11-20 行** `from compose import (` 多行导入：
  - `AuditPort` / `HumanPort` / `SandboxPort`：R9 三 Port（建默认 registry 用）。
  - `DisclosureIndex` / `DisclosurePacket`：披露协议与包。
  - `Registry`：共用 Provider 注册表（install 用）。
  - `SkillManifest` / `SkillPort`：技能清单与门。
- **第 22 行** `from .index import ToolIndex` 默认披露实现。
- **第 23 行** `from .mcp import MCPManager` MCP 门面。
- **第 24 行** `from .registry import ToolRegistry` 默认注册表。
- **第 26 行** `__all__ = ["ToolsCapability"]` 只导出能力包类。

### 第 28-32 行：端口名常量

- **第 28 行** `# R2 规范端口名（与 PORT_PROTOCOLS 键对齐）：install 据此把各 port 注册进 Registry。`
  这些名字必须与 R2 的 PORT_PROTOCOLS 键一致，install 按名字注册。
- **第 29 行** `_PORT_TOOL = "tool"` 工具 port 名。
- **第 30 行** `_PORT_DISCLOSURE = "disclosure"` 披露 port 名（对应 ToolIndex.name）。
- **第 31 行** `_PORT_MCP = "mcp"` MCP port 名。
- **第 32 行** `_PORT_SKILL = "skill"` 技能 port 名。

### 第 35-49 行：类属性（Capability 三要素 + 持有字段）

- **第 35 行** `class ToolsCapability:`
- **第 36-40 行** docstring：持有默认注册表/披露索引与可替换的 MCP、技能实现；属性 name/version/requires 是 R13 Capability 三要素。
- **第 42 行** `name = "tools"` 能力名。
- **第 43 行** `version = "5.0.0"` 版本。
- **第 44 行** `requires: tuple[str, ...] = ()` 依赖的能力列表（空=无依赖）。三要素都用类属性声明，框架据此识别能力。
- **第 46 行** `_registry: ToolRegistry` 持有注册表。
- **第 47 行** `_mcp: MCPManager | None` MCP 门面（可缺席）。
- **第 48 行** `_skills: SkillPort | None` 技能门（可缺席）。
- **第 49 行** `_disclosure: DisclosureIndex` 披露索引。

### 第 51-75 行：`__init__`——默认构造（规则 9）

- **第 51-61 行** 签名：
  - 关键字参数：`registry` / `mcp` / `skills` / `disclosure` / `sandbox` / `human` / `audit`，全部可缺省。
- **第 62 行** docstring：`"""装配能力：registry/disclosure 缺席时建默认实现，mcp/skills 可缺席。"""`

- **第 63 行** `if registry is None:`
- **第 64 行** 注释：`# 规则 9：协议可注入则必须有开箱默认实现，三 Port 透传给默认注册表。`
- **第 65 行** `self._registry = ToolRegistry(sandbox=sandbox, human=human, audit=audit)`
  **默认注册表**：把外层三个 Port 透传进去。这样调用方只构造 `ToolsCapability(sandbox=..., human=..., audit=...)` 就能拿到配好高危路由的 registry。
- **第 66 行** `else:`
- **第 67 行** 注释：`# 自定义注册表已自带路由依赖，忽略外层 sandbox/human/audit 避免语义歧义。`
- **第 68 行** `self._registry = registry`
  **关键决策**：传了自定义 registry 就忽略外层三 Port。因为自定义 registry 内部已配好自己的 sandbox/human/audit，若外层再传一套会造成「到底用哪套」的语义歧义。

- **第 69 行** `self._mcp = mcp`
- **第 70 行** `self._skills = skills`

- **第 71 行** `if disclosure is None:`
- **第 72 行** 注释：`# 规则 9：披露策略可替换，但默认必须可用；透传 skills 以启用 L3 正文加载。`
- **第 73 行** `self._disclosure = ToolIndex(self._registry, skills=self._skills)`
  **默认披露索引**：用当前 registry + skills 构造 ToolIndex。**透传 skills 是关键**——只有传了 skills，L3 正文加载才会启用（见 index.py 第 58 行）。
- **第 74 行** `else:`
- **第 75 行** `self._disclosure = disclosure`
  传了自定义披露策略就用它。

### 第 77-79 行：`build()`——产出 ToolLayer Program

- **第 77 行** `def build(self) -> Program:`
- **第 78 行** docstring：`"""ToolLayer 外壳（规格 P5）：产出"用工具"的 Program 工厂（内部不改）。"""`
- **第 79 行** `return layer_program("tool", payload={"layer": "tools"})()`
  调用 `layer_program` 工厂构造一个「使用工具」的 Program，payload 标记 layer 为 tools。这是能力域的标准外壳。

### 第 81-98 行：`install(reg)`——注册 Port（先 flush 再注册）

- **第 81 行** `def install(self, reg: Registry) -> None:`
- **第 82 行** docstring 首行：`"""把持有的 port 注册进共用 Provider 注册表。`
- **第 84-86 行** 参数说明：reg 是共用 Provider 注册表，按 port 名注册四个 port。

- **第 87 行** 注释：`# 先装配期自动注册 @tool 待注册项，再注册各 Port；`
- **第 88 行** 注释：`# 为何在前：flush 可能抛重名/freeze，须在 Port 注册前暴露，避免半装配。`
- **第 89 行** `self._registry.register_pending()`
  **关键顺序**：先 flush 待注册项。
  - **为什么必须在注册 Port 之前**：`register_pending()` 可能抛重名/freeze 错误。若先注册了 Port 再 flush 抛错，就留下「Port 已注册但工具没注册成功」的半装配状态。先 flush，失败就完全没注册任何 Port，干净利落。

- **第 90 行** 注释：`# reg 提供 register(name, provider) 现成 API，直接注册持有的四个 port。`
- **第 91 行** `reg.register(_PORT_TOOL, self._registry)` 注册工具 port（必注册）。
- **第 92 行** `reg.register(_PORT_DISCLOSURE, self._disclosure)` 注册披露 port（必注册）。
- **第 93 行** `if self._mcp is not None:`
- **第 94 行** 注释：`# MCPPort 缺席为 None，跳过注册（缺席即降级）。`
- **第 95 行** `reg.register(_PORT_MCP, self._mcp)` MCP 缺席跳过。
- **第 96 行** `if self._skills is not None:`
- **第 97 行** 注释：`# SkillPort 缺席为 None，跳过注册（缺席即降级）。`
- **第 98 行** `reg.register(_PORT_SKILL, self._skills)` 技能缺席跳过。

  **必注册 vs 可选**：tool 和 disclosure 是核心能力，必须注册；mcp 和 skill 是增强项，缺席就跳过（降级工作）——这与 `__init__` 里它们可为 None 一致。

### 第 100-114 行：四个 Getter

- **第 100-102 行** `registry()`：返回 `self._registry`。
- **第 104-106 行** `index()`：返回 `self._disclosure`（披露索引也叫 index）。
- **第 108-110 行** `manager()`：返回 `self._mcp`（可能 None）。
- **第 112-114 行** `skills()`：返回 `self._skills`（可能 None）。

  这些 Getter 供装配层/测试获取内部持有的实现。

### 第 116-122 行：`disclose(names)`——对外披露入口

- **第 116 行** `def disclose(self, names: list[str]) -> DisclosurePacket:`
- **第 117 行** docstring：`"""委托披露索引生成披露包，技能清单从 SkillPort.catalog() 取。"""`
- **第 118 行** `manifests: list[SkillManifest] = []`
  初始化空技能清单。
- **第 119 行** `if self._skills is not None:`
- **第 120 行** 注释：`# 技能缺席时传空清单，披露仍能按工具维度降级工作。`
- **第 121 行** `manifests = list(self._skills.catalog())`
  从 SkillPort 取技能清单（catalog() 是 L1 常驻披露）。
  - **为什么要从 skills.catalog() 取**：ToolIndex.disclose 需要 skills 清单来加载 L3 正文（index.py 第 59 行）。这里作为桥梁把 skills 的能力接进披露流程。
- **第 122 行** `return self._disclosure.disclose(names, manifests)`
  委托披露索引生成包。技能缺席时传空清单，披露按工具维度降级仍能工作。

**本文件设计要点**：
- **规则 9**：registry/disclosure 缺席建默认实现，mcp/skills 可缺席为 None。
- **传自定义 registry 时忽略外层三 Port**（第 68 行），避免双套依赖的语义歧义。
- **默认披露透传 skills**（第 73 行），启用 L3 正文加载。
- **install 先 flush 待注册项再注册 Port**（第 89 行），避免半装配。
- **tool/disclosure 必注册，mcp/skill 缺席跳过**（第 91-98 行），核心与增强分层。
- **disclose 桥接 SkillPort.catalog()**（第 121 行），让披露能加载技能正文。

---

## 9. __init__.py 逐行讲解（34 行）

包级公共 API：把内部各文件的关键符号重导出，外部只需 `from harness.tools import ...`，不必知道内部文件路径。`__all__` 即 tests/r5 契约面。

### 第 1-4 行：模块 docstring

- **第 1 行** `"""R5 工具包公共 API：装饰器、注册表、披露索引、MCP 门面与能力装配。`
  一句话定位：包级出口，涵盖五大块。
- **第 3 行** `导出面即 tests/r5 契约；内部实现细节不进 __all__。`
  **关键**：`__all__` 就是对外契约（tests/r5 据此测试），内部实现（如 `_ExecTool`、`_Named`、`_PENDING`）不导出。
- **第 4 行** `"""` docstring 结束。

### 第 5-14 行：从各子模块导入

- **第 5 行** `from __future__ import annotations` 延迟注解。
- **第 7 行** `from .capability import ToolsCapability` 能力装配类。
- **第 8 行** `from .decorator import ToolFn, clear_pending, pending_tools, tool` 装饰器四符号。
- **第 9 行** `from .execution import CodeExecTool, FileExecTool, ShellExecTool` 三执行工具（注意 `_ExecTool` 基类不导出）。
- **第 10 行** `from .index import ToolIndex` 披露索引。
- **第 11 行** `from .mcp import MCPManager, MCPTransport` MCP 门面与协议。
- **第 12 行** `from .registry import ToolRegistry` 注册表。
- **第 13 行** `from .types import DangerLevel, SandboxPolicy, ToolSource` 三个枚举（`DangerLevel` 是重导出的 compose 符号，这里再重导出一层，外部只认 `harness.tools`）。
- **第 14 行** `from .webfetch import WebFetcher, WebFetchTool` 抓取协议与工具。

  **包级 API 收敛的意义**：外部统一从 `harness.tools` 导入，内部文件结构调整不影响调用方。例如 webfetch.py 第 15 行从 `harness.skills` 导入检测器，也是同样的「从包门面导入」纪律。

### 第 16-34 行：`__all__`——公共导出面

- **第 16 行** `__all__ = [` 开始列表。
- **第 17-30 行** 类与枚举符号：
  - 第 17 行 `"CodeExecTool",`
  - 第 18 行 `"DangerLevel",`
  - 第 19 行 `"FileExecTool",`
  - 第 20 行 `"MCPManager",`
  - 第 21 行 `"MCPTransport",`
  - 第 22 行 `"SandboxPolicy",`
  - 第 23 行 `"ShellExecTool",`
  - 第 24 行 `"ToolFn",`
  - 第 25 行 `"ToolIndex",`
  - 第 26 行 `"ToolRegistry",`
  - 第 27 行 `"ToolSource",`
  - 第 28 行 `"ToolsCapability",`
  - 第 29 行 `"WebFetcher",`
  - 第 30 行 `"WebFetchTool",`
- **第 31-33 行** 函数符号：
  - 第 31 行 `"clear_pending",`
  - 第 32 行 `"pending_tools",`
  - 第 33 行 `"tool",`
- **第 34 行** `]` 结束。

  **`__all__` 的作用**：界定 `from harness.tools import *` 会导入哪些符号，也是文档化「哪些是公共 API」。共 18 个符号，全部在第 7-14 行导入过——`__all__` 与导入一一对应，没有遗漏也没有多余。

**本文件设计要点**：纯粹的「门面收敛」——把 8 个内部文件的关键符号按 R5 统一出口重导出，`__all__` 即对外契约，内部实现细节不外泄。

---

## 10. 逐行讲解后的整体心智图

```
【装配期】
模块导入 → @tool 装饰 → ToolFn 入 _PENDING(decorator 第141行)
                                    │
ToolsCapability.__init__ (capability 第51行)
  ├─ registry 缺 → 用 sandbox/human/audit 建 ToolRegistry(规则9)
  └─ disclosure 缺 → 用 registry+skills 建 ToolIndex(规则9)
        │
install(reg) (capability 第81行)
  ① register_pending() 先全量预检再批量注册(registry 第85行) ← 失败则零 Port 注册
  ② 注册 port: tool/disclosure 必注册; mcp/skill 缺席跳过
        │
static_check() 报告 danger 缺失/高危无沙盒/CRITICAL 无人工

【运行期一次工具调用】 registry.call (registry 第164行)
ToolCall → 未注册? → NOT_FOUND
         → danger 以 spec 为准覆盖 call.danger(第193行) ← 防伪造降级
         → 参数校验(有 properties 键才做 extra=forbid, 第198行)
         → 四级路由:
             SAFE     → 直接执行(第210行)
             LOW      → 审计(缺席标 audit_missing)后执行(第211-225行)
             HIGH     → 无沙盒 NO_SANDBOX; 有则 sandbox.run(第226-232行)
             CRITICAL → 无人工 NO_HUMAN; 审批非 approve→APPROVAL_DENIED;
                        再查沙盒→sandbox.run(第233-246行)
         → 任何异常 → TOOL_ERROR 折叠(第247行)
         → finish() 统一补 duration_ms/retryable/meta 副本

【模型看工具列表】 ToolIndex.disclose (index 第39行)
L1 全目录(无 schema) + L2 命中项完整 schema + L3 技能正文(需 SkillPort)
  → L3 命中注入 → 正文留空串(第64行)
  → 超阈值无命中 → 强制 L1-only(第73行)
  → 超 token 预算 → 抛 ValueError(第78行)

【接入层】
MCPManager.connect → transport 原始名 → 加 mcp__<server>__<tool> 前缀 → 注入同一 Registry
WebFetchTool.fetch → 抓正文 → detector 检测 → 命中注入抛 ValueError 拒载 + 审计
执行类工具(Code/Shell/File) → danger=HIGH 强制沙盒; R5 侧白名单/虚拟根前置校验
```

---

## 11. 细节速记卡（逐行精华）

**danger 与安全路由**
- **danger 以注册 spec 为准**：第 193 行覆盖 `call.danger`，防伪造降级。
- **四级路由**：SAFE 直执行 / LOW 审计后执行 / HIGH 沙盒 / CRITICAL 审批 + 沙盒。
- **HIGH 无沙盒、CRITICAL 无人工/未批准直接拒绝**，绝不降级直执行（第 228、234、238、241 行）。
- **SandboxPolicy 只是声明**：运行期路由看 danger，不看 policy（types.py 第 32 行）。
- **danger 必填**：`@tool(danger=...)` 无默认值，强制声明（decorator.py 第 84 行）。
- **异常一律折叠为 ToolResult**：不抛穿（registry.py 第 247 行、execution.py 第 59 行）。

**注册与装配**
- **先全量预检再批量注册**：register_pending 避免"注册一半抛错"半状态（第 85-114 行）。
- **install 先 flush 待注册项再注册 Port**：避免半装配（capability.py 第 89 行）。
- **bind 只换执行体不换 spec**：且冻结后仍可 bind（热更）（registry.py 第 116 行）。
- **冻结后 register 抛错**：防运行期工具集漂移（registry.py 第 76 行）。
- **规则 9 默认实现**：registry 缺用三 Port 建、disclosure 缺用 registry 建（capability.py 第 63-73 行）。
- **传自定义 registry 忽略外层三 Port**：避免双套依赖歧义（capability.py 第 68 行）。

**披露**
- **L1 目录清空 schema**：`model_copy` 浅拷贝后置空（registry.py 第 127 行）。
- **schema_of 返回副本**：防外部改写内部（registry.py 第 134 行）。
- **三级披露**：L1 全目录 / L2 命中项 / L3 技能正文（index.py 第 48-65 行）。
- **L3 命中注入留空串**：不抛穿，一技能问题不拖垮全局（index.py 第 64 行）。
- **默认披露透传 skills**：启用 L3（capability.py 第 73 行）。
- **超预算抛 ValueError**：不静默塞超量上下文（index.py 第 78 行）。
- **工具总数 > 20 且无命中强制 L1-only**：防上下文膨胀（index.py 第 73 行）。

**参数与 schema**
- **参数校验只在 schema 有 properties 键时做**：无该键视为不约束，兼容 MCP（registry.py 第 198 行）。
- **首参是 pydantic 模型走模型校验**：`extra=forbid` 在实现执行前挡脏参数（decorator.py 第 120 行）。
- **docstring 只取首行进 L1 目录**：避免披露膨胀（decorator.py 第 72 行）。
- **@tool 的 timeout/retries 当前不生效**：只接收不写 ToolSpec（decorator.py 第 99 行）。

**MCP / WebFetch / 执行类**
- **MCP 工具全名是 `mcp__<server>__<tool>`**：transport 返回原始名，Manager 统一加前缀（mcp.py 第 97 行）。
- **MCP 工具注入同一 Registry**：catalog/call 与本地无差别（mcp.py 第 101 行）。
- **_invoker 闭包绑定 server/tool**：规避循环晚绑定（mcp.py 第 75-81 行）。
- **health 单点异常记 False**：不中断整体（mcp.py 第 127 行）。
- **WebFetch 命中注入抛 ValueError 拒载，不净化**：干净正文也留审计（webfetch.py 第 69、74、78 行）。
- **WebFetch 检测器缺省必填**：无缺席放行分支（webfetch.py 第 50 行）。
- **WebFetch danger 实际是 LOW**：走审计路由（docstring 第 36 行的 SAFE 是旧注释，以第 60 行代码为准）。
- **执行类一律 HIGH 强制沙盒**：sandbox 缺席即拒绝（execution.py 第 38、55 行）。
- **Shell 命令白名单**：非白名单零执行，连沙盒都不惊动（execution.py 第 95-97 行）。
- **文件虚拟根路径校验**：abspath + `root + os.sep` 前缀，阻断 `../` 与绝对路径逃逸（execution.py 第 117-118 行）。

**模块结构**
- **包级门面收敛**：`__init__.py` 的 `__all__` 即 tests/r5 契约，内部实现不外泄。
- **_PENDING 模块级容器**：装饰即入列，零参数兼容现有调用点（decorator.py 第 30 行）。

---

*这是 tools 模块的逐行讲解版。格式与 [05 skills 样板册](./05-skills技能模块.md) 一致：「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」。*

