# 代码精读 · archs 内置架构模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/archs/base.py`（165 行）+ `src/archs/recipes.py`（694 行）+ `__init__.py`（23 行）全部代码。
> 归属内核层的第三册（R12 内置架构）。前两册讲 harness 能力层，本册讲「架构工厂」——把 A 类算子组合成 Program 的预设配方。
> 总入口见 [00](./00-总入口与全局链路.md)。

---

## 0. 读前GPS

`src/archs/` 是**内核层的架构工厂**：它不执行任何业务逻辑，只做两件事——

1. `compile()` 产出 `Program`（一个 generator 函数），交给 R1 解释器执行。
2. `static_check()` 在装配期产出结构化报告（`StaticCheckReport`），把配置错误挡在运行期之前。

全模块三个文件，职责分层：

- **base.py**：定义 `Arch` 协议、`StaticCheckReport`/`StaticCheckIssue` 报告类型，以及一整套可插拔策略契约（`Planner`/`Executor`/`RePlanner`/`ReflectStrategy`/`IntentStrategy`/`JoinStrategy`/`ChildSpec`/`SkillSpec`）。
- **recipes.py**：10 个内置架构的全部实现（ToolUse/ReAct/PlanExecute/Reflection/Router/RouterSkill/Supervisor/Handoff/Blackboard/Graph），外加私有 helper。这是本册重头戏。
- **__init__.py**：包级 API 收敛，统一重导出。

### 关键设计决策（读懂这些，全模块贯通）

- **决策 1｜架构 = 产出 Program 的工厂**：`compile()` 返回一个 generator（`Program`），R1 只认 Program。换架构时上层装配零改动，因为所有架构共用 R1 的解释循环。
- **决策 2｜static_check 装配期给结构化报告**：错误码 R12-E01~E17，问题进运行期前就报错，而不是等到运行时崩溃。
- **决策 3｜Program 拿不到 fold 后的 State**：AgentState 只由 R1 的 fold 内部生成，架构 generator 拿不到。所以架构内所有决策（tool_calls/label/passed/next 节点）都从 **`send` 回注值**读。
- **决策 4｜多步进度用 `x-arch.*` 扩展键随 Effect 传递**（D-2 约定）：不依赖 fold 回注完整 State，把进度塞进 `Invoke.payload` 的扩展键里。
- **决策 5｜`_run_child` 拦截子 Program 的 Terminate**：子 Program 的 `Terminate` 代表「该子流程结束」，转成返回值提取 output，**不提前结束整个 run**（否则会终止全部）。
- **决策 6｜`_batch` 的 `calls` 由解释器展开**：一次 batch Invoke，解释器把它展开成多个独立 tool Effect，回填 `list[dict]`。
- **决策 7｜预算是 Budgets 维度**：generator 内的 while/for 用构造参数作**防御性上限**（防止 port 永不收敛时死循环），真实约束仍由 R1 Budgets 兜底。
- **决策 8｜Graph 的 static_check 查环/可达/悬空边/多出口**：E01 悬空边、E02 环、E03 不可达、E04 多出口、E06 cond 目标重复。
- **决策 9｜MajorityVote 平票取首个**：确定性，墙钟不介入。
- **决策 10｜IntentStrategy 异步协议用 asyncio.run 取结果**：`recognize` 是 async，装配期同步调用时用 `asyncio.run` 取协程结果。

### 依赖方向

```
archs ──依赖──> runtime（AgentState/Program/Effect/Invoke/...）
      ──依赖──> compose（ToolSpec/ProgramFn）
      └─内部 base <── recipes（import 策略契约与报告类型）
```

注意：`runtime` 是内核更底层，archs 依赖它；`compose` 提供 DTO。archs 不反向依赖 harness。

---

## 1. base.py 逐行讲解（165 行）

这是「协议与契约层」：不含任何架构实现，只定义类型。读懂它，recipes.py 里 10 个架构的类签名和返回类型就都认识了。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""内置架构统一协议与静态检查报告（R12 §5/§5.1，规格 P6）。`
  一句话定位：统一协议 + 静态检查报告。归属 R12（内置架构域），规格编号 P6。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `架构 = 产出 Program 的工厂：compile() 生成 generator（经 A 类算子配方组合），`
  点明核心心智：架构不是「运行器」，是「工厂」。`compile()` 返回一个 generator（就是 Program），内容是 A 类算子的配方组合。

- **第 4 行** `static_check() 在装配期给结构化报告（错误码 R12-E01~E17），问题进运行期前报错。`
  第二个核心方法：`static_check()`。强调「装配期」+「结构化报告」+「错误码 E01~E17」，目标是 fail fast（问题进运行期前就报）。

- **第 5 行** `所有架构共用 R1 的解释循环，换架构上层装配零改动。`
  解释为什么能「换架构零改动」：架构只是数据（Program），执行引擎只有一套（R1 解释循环）。

- **第 6 行** `"""` docstring 结束。

### 第 7-12 行：imports

- **第 7 行** `from __future__ import annotations`
  延迟求值注解：允许在类型注解里引用尚未定义的类型（如 `Arch` 协议方法返回 `Program`），降低 import 顺序耦合。

- **第 9 行** `from dataclasses import dataclass, field`
  - `dataclass`：声明 `MajorityVote`/`IntentResult`/`StaticCheckIssue`/`StaticCheckReport` 四个不可变数据类。
  - `field`：给 `StaticCheckReport.budgets_resolved` 提供 `default_factory`（可变默认值必须用它）。

- **第 10 行** `from typing import Any, Protocol, runtime_checkable`
  - `Any`：结果合并策略里结果类型未知（工具调用的返回包罗万象）。
  - `Protocol`：定义可插拔协议（结构化类型 / 鸭子类型）。
  - `runtime_checkable`：让协议支持 `isinstance()` 运行时校验（装配期履约检查要用）。

- **第 12 行** `from runtime import AgentState, Program`
  - `AgentState`：Graph 的条件边 `cond` 签名需要它；`IntentStrategy.recognize` 的 ctx 类型。
  - `Program`：`Arch.compile()` 的返回类型。
  依赖方向：archs → runtime（向下依赖内核底层）。

### 第 14-18 行：__all__

- **第 14-18 行** `__all__ = [...]`
  声明公共导出面，共 14 个符号，分三类：
  - 报告类型：`StaticCheckIssue`、`StaticCheckReport`。
  - 协议与核心类：`Arch`、`JoinStrategy`、`MajorityVote`、`NamedStrategy`、`Planner`、`Executor`、`RePlanner`、`ReflectStrategy`、`IntentStrategy`、`IntentResult`、`ChildSpec`、`SkillSpec`。
  `__init__.py` 据此重导出。注意 `base.py` 的 `__all__` 不含任何架构实现——那些在 recipes.py。

### 第 21-26 行：JoinStrategy 协议

- **第 21 行** `@runtime_checkable`
  运行时可检查装饰器：允许 `isinstance(x, JoinStrategy)`。代价：只能检查成员存在，不能校验方法签名。

- **第 22 行** `class JoinStrategy(Protocol):`
  定义「子代理结果合并策略」协议。Supervisor 用它把多个子 Agent 的结果合并成一个。

- **第 23 行** `"""子代理结果合并策略（Supervisor.join，R12 §4）。"""`
  点明用途：Supervisor 的 join 注入点。

- **第 24 行** `name: str`
  具名契约：「可插拔物必须具名」纪律的落地，进 trace 归因、装配期校验。

- **第 26 行** `def join(self, results: list[Any]) -> Any: ...`
  协议方法签名：输入结果列表，输出合并结果。`...` 是 Protocol 的接口声明语法，不是代码省略。
  - 为什么返回 `Any`：各架构的结果形态各异（dict、文本、列表），无法统一成具体类型。

### 第 29-40 行：MajorityVote 默认合并策略

- **第 29 行** `@dataclass(frozen=True, slots=True)`
  - `frozen=True`：不可变，策略对象可安全共享。
  - `slots=True`：无 `__dict__`，省内存、防动态加属性。

- **第 30 行** `class MajorityVote:`
  默认合并策略实现。**没有显式继承** `JoinStrategy`——Protocol 是结构化类型，有 `name` 和 `join` 就算履约。

- **第 31 行** `"""默认合并策略：多数投票；平票取首个（确定性，墙钟不介入）。"""`
  算法一句话：多数投票；平票取首个。**确定性**是关键——不靠时间/随机，保证可复现。

- **第 32 行** `name: str = "majority_vote"`
  具名标识，默认值。

- **第 34 行** `def join(self, results: list[Any]) -> Any:`
  合并方法实现。

- **第 35-36 行** `if not results: return None`
  空列表边界：没有结果就返回 `None`。不这么写的话，下面 `max()` 对空序列会抛 `ValueError`。

- **第 37 行** `counts: dict[Any, int] = {}`
  计数字典：结果值 → 出现次数。

- **第 38-39 行** `for r in results: counts[r] = counts.get(r, 0) + 1`
  逐结果计数。`counts.get(r, 0)` 处理首次出现的键（缺省 0）。
  - 隐含约束：结果值 `r` 必须可哈希（能做 dict 键）。这是多数投票的前提。

- **第 40 行** `return max(counts, key=lambda k: (counts[k], -list(results).index(k)))`
  **核心确定性行**：`max` 的排序键是二元组 `(次数, -首次出现位置)`。
  - 第一优先：出现次数多者胜（多数投票）。
  - 第二优先（平票时）：`-index(k)`，首次出现位置越靠前，`-index` 越大，所以**平票取首个**。
  - 为什么不写 `key=counts.get`：那样平票时 `max` 取 dict 插入顺序的第一个键——虽然 Python 3.7+ dict 保序，但语义不直观；这里显式用 `index` 表达「取 results 中首次出现者」，意图清晰且墙钟不介入。
  - `list(results)` 每次构造：因为 `results` 可能是不可重复的映射视图，转 list 保证 `index` 可用。

### 第 43-51 行：可插拔策略契约说明注释

- **第 43-44 行** 分隔注释 + 标题：`可插拔策略契约（R12 §4 构造签名 + R13 §2 协议目录 + R8 §4/§5）`
  说明这段契约的规格来源。

- **第 46-50 行** 注释正文：
  - 规格用 `Planner`/`Executor`/.../`SkillSpec` 命名这些注入点，但**旧实现全写成裸 `Any`**。这里把协议名落到类型层。
  - 每个策略的装配期契约是「具名（`name` 非空）」——`static_check` 据此判 **E15「策略未履约：缺 name」**。
  - 方法签名只在规格明确给出时声明（`IntentStrategy`），其余不编造规格未定的方法，避免空头契约挡住未来实现。
  - 这是重要的设计克制：不为了「看起来完整」而捏造方法签名，否则未来实现稍有不同就会被协议挡住。

### 第 54-61 行：NamedStrategy 协议

- **第 54 行** `@runtime_checkable` 运行时可检查。

- **第 55 行** `class NamedStrategy(Protocol):`
  所有可插拔策略的**最小公共契约**：具名即可被装配期履约校验（E15）。

- **第 56-60 行** docstring：说明字段 `name` 是策略注册名，`static_check` 只读此字段判「策略未履约」。

- **第 61 行** `name: str`
  唯一的契约字段。其他所有策略协议都继承它，自动获得「具名」要求。

### 第 64-69 行：Planner 协议

- **第 64 行** `@runtime_checkable` 运行时可检查。

- **第 65 行** `class Planner(NamedStrategy, Protocol):`
  规划策略，注入点：PlanExecute / Supervisor（R13 §5）。继承 `NamedStrategy` 获得 `name` 要求。

- **第 66-69 行** docstring：把任务分解为可执行步骤；分解逻辑由实现方持有，架构只消费产出的步骤。
  - 关键分工：架构不关心「怎么规划」，只消费「规划出的步骤」。

### 第 72-77 行：Executor 协议

- **第 72 行** `@runtime_checkable` 运行时可检查。

- **第 73 行** `class Executor(NamedStrategy, Protocol):`
  步骤执行策略，注入点：PlanExecute（R13 §5）。

- **第 74-77 行** docstring：执行单个已规划步骤；执行细节由实现方持有。

### 第 80-85 行：RePlanner 协议

- **第 80 行** `@runtime_checkable` 运行时可检查。

- **第 81 行** `class RePlanner(NamedStrategy, Protocol):`
  重规划策略，注入点：PlanExecute 的可选注入点（R13 §5）。

- **第 82-85 行** docstring：失败或环境变化时产出新步骤序列。

### 第 88-93 行：ReflectStrategy 协议

- **第 88 行** `@runtime_checkable` 运行时可检查。

- **第 89 行** `class ReflectStrategy(NamedStrategy, Protocol):`
  反思评判策略，注入点：Reflection（R13 §5）。

- **第 90-93 行** docstring：对草稿产出评判（`passed`/`score`/`comments`，见 R8 §5 Critique）。

### 第 96-109 行：IntentResult 数据类

- **第 96 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots。作为「返回信封」，不可变能保证结果在传递中不被意外修改。

- **第 97 行** `class IntentResult:`
  意图识别结果，单入口 `IntentStrategy.recognize` 的返回信封（R8 §5）。

- **第 98-105 行** docstring 字段说明：
  - `label`：命中的意图标签（对应 Router.arms 的 key）。
  - `confidence`：置信度（0~1），低于 `min_confidence` 走 default。
  - `gaps`：信息缺口，触发澄清（R8 §5）。
  - `trace`：观察语义的命中级/逐级 label+confidence，非内核必填。

- **第 106 行** `label: str`
  必填：意图标签。

- **第 107 行** `confidence: float`
  必填：置信度。

- **第 108 行** `gaps: tuple[str, ...] = ()`
  可选：信息缺口，默认空元组。用 tuple 保证不可变。

- **第 109 行** `trace: tuple[str, ...] = ()`
  可选：观察轨迹，默认空元组。

### 第 112-119 行：IntentStrategy 协议

- **第 112 行** `@runtime_checkable` 运行时可检查（recipes.py 的 `_recognize` 用 `isinstance(cls, IntentStrategy)` 判断）。

- **第 113 行** `class IntentStrategy(NamedStrategy, Protocol):`
  意图识别整体策略，注入点：Router / RouterSkill（R13 §2 注入点①）。

- **第 114-118 行** docstring：
  - 传入即**整包替换**默认四级级联（`CascadeIntentStrategy`，见 R13 §4.1）。
  - Router 只消费单次 recognize 结果，对级联内部级数/阈值无感。
  - 这是「策略整包替换」纪律：注入一个对象，替换整套算法，架构不关心内部实现。

- **第 119 行** `async def recognize(self, text: str, ctx: AgentState) -> IntentResult: ...`
  **唯一声明了方法签名的协议**（因为规格明确给出）。`async` 是关键——它是异步协议，recipes.py 里要用 `asyncio.run` 取结果。
  - `ctx: AgentState`：上下文状态。
  - 返回 `IntentResult` 信封。

### 第 122-128 行：ChildSpec 协议

- **第 122 行** `@runtime_checkable` 运行时可检查。

- **第 123 行** `class ChildSpec(NamedStrategy, Protocol):`
  子 Agent 规格，注入点：Supervisor（R12 §4）。

- **第 124-128 行** docstring：
  - 声明子 Agent 类型；并发上限由 `max_children` 承载、预算由 Budgets 分账。
  - 二者都**不在架构 generator 内自数**（铁律）——计数交给 R1 的 Budgets 统一扣减。

### 第 131-136 行：SkillSpec 协议

- **第 131 行** `@runtime_checkable` 运行时可检查。

- **第 132 行** `class SkillSpec(NamedStrategy, Protocol):`
  技能规格，RouterSkill 的路由目标之一（R12 §4）。

- **第 133-136 行** docstring：`name` 为技能名，正文（`part:body`）加载由 R5 SkillPort 履约，架构不实现注册。
  - 分工：架构负责「路由到技能并请求加载」，实际加载由 R5 的技能提供者完成。

### 第 139-143 行：StaticCheckIssue 数据类

- **第 139 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots。问题条目一旦产生不再修改。

- **第 140 行** `class StaticCheckIssue:`
  单个静态检查问题。

- **第 141 行** `code: str`
  错误码（如 `R12-E07`）。必填。

- **第 142 行** `message: str`
  人类可读的错误描述。必填。

- **第 143 行** `node: str | None = None`
  可选：关联的节点/字段名（如 `"tools"`、`"max_turns"`），便于定位。默认 `None`。

### 第 146-156 行：StaticCheckReport 数据类

- **第 146 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots。报告作为值对象传递。

- **第 147 行** `class StaticCheckReport:`
  静态检查报告：装配期的结构化产物。

- **第 148 行** `errors: tuple[StaticCheckIssue, ...] = ()`
  问题列表，用 tuple（不可变），默认空元组。

- **第 149 行** `arch: str = ""`
  架构名（如 `"ReAct"`），默认空串。

- **第 150 行** `version: str = ""`
  架构版本（如 `"12.0.0"`），默认空串。

- **第 151 行** `ports_required: tuple[str, ...] = ()`
  该架构需要的端口列表（如 `("llm", "tool")`），供装配期核对依赖是否齐备。

- **第 152 行** `budgets_resolved: dict[str, int | float] = field(default_factory=dict)`
  **可变默认值必须用 `field(default_factory=dict)`**。
  - 不这么写（直接 `= {}`）：所有实例共享同一个 dict，一处修改污染全部。
  - 记录解析后的预算维度（如 `{"turns": 30}`），供装配期核对。

- **第 154-156 行** `ok` 属性：
  - **第 154 行** `@property` 装饰器：当属性访问（`report.ok`）。
  - **第 155 行** `def ok(self) -> bool:`
  - **第 156 行** `return not self.errors`
    语义：没有问题 → `ok` 为 `True`。调用方只需 `if not report.ok: ...` 即可 fail fast。

### 第 159-165 行：Arch 协议（核心）

- **第 159 行** `@runtime_checkable` 运行时可检查。

- **第 160 行** `class Arch(Protocol):`
  **全模块的核心协议**：所有 10 个内置架构都实现它。

- **第 161 行** `name: str`
  架构名。

- **第 162 行** `version: str`
  架构版本。

- **第 164 行** `def static_check(self) -> StaticCheckReport: ...`
  装配期检查方法，返回结构化报告。`...` 是协议声明。

- **第 165 行** `def compile(self) -> Program: ...`
  编译方法，返回 `Program`（generator）。这是「架构 = 工厂」的落地点。
  - 两个方法一静（static_check 装配期）一动（compile 运行期产出 Program），构成架构的完整契约。

---

## 2. recipes.py 逐行讲解（694 行）

这是本册的重头戏：10 个内置架构的全部实现，外加私有 helper。建议先读完本节开头的「运行时建模」说明（docstring 第 7-15 行），否则下面每个架构的 `yield` / `yield from` 都会看不懂。

### 第 1-16 行：模块 docstring（含运行时建模，最重要）

- **第 1 行** `"""10 内置架构 = A 类算子的预设配方（R12 §6，规格 P6）。`
  一句话定位：架构是 A 类算子（seq/par/branch/loop 等）的预设组合配方。归属 R12 §6。

- **第 2 行** 空行（docstring 内分段）。

- **第 3-5 行** `每个架构实现 Arch 协议（name/version/static_check/compile）...R1 只认 Program，架构知识全在装配期。构造签名对齐 R12 §4「对外 API 不变」。`
  三重定位：① 实现 Arch 协议；② `compile()` 产出**真实语义**的 Program（phase 机、反思循环、条件路由、分支跳转）；③ 构造签名对外不变（R12 §4）。
  - 关键句「R1 只认 Program，架构知识全在装配期」：R1 解释器不知道自己在跑 ReAct 还是 Router，它只看到 Program。

- **第 7-11 行** `运行时建模（已用 Runner 实测确认）：program yield Effect 后 send 回填的是 port 的 Result.data——单个 Invoke 收 dict，batch Invoke 收 list[dict]；AgentState 只由 R1 fold 内部生成、program 拿不到。故架构内所有决策（tool_calls/label/passed/next 节点等）都从回注值读，多步进度用 Invoke.payload 的 x-arch.* 扩展键随 Effect 传递（D-2 约定），不依赖 fold 回注完整 State。`
  **全模块最关键的 5 行**，逐条拆：
  - `yield Effect` 后 `send` 回填的是 `Result.data`：generator 的 `yield` 把 Effect 交给解释器，解释器执行后把结果 `.data` 通过 `send` 送回 generator。**注意：送回的是 data，不是完整 State**。
  - 单个 Invoke 收 dict，batch Invoke 收 list[dict]：决定了后面 `_dict`/`_tool_calls` 等 helper 要同时处理两种形态。
  - AgentState 只由 R1 fold 内部生成、program 拿不到：这是决策 3。架构**不能**直接读 State。
  - 所有决策都从回注值读：所以你会看到大量 `_get(_dict(out), "xxx")` 的写法。
  - 多步进度用 `x-arch.*` 扩展键随 Effect 传递（D-2 约定）：决策 4。进度不靠 State，靠塞进 payload 的扩展键。

- **第 13-15 行** `预算（max_turns/max_rounds/max_hops）是 Budgets 维度（R1 强制扣减）：generator 内的 while 循环用构造参数作防御性上限（防止 port 永不收敛时死循环），真实约束仍由 R1 Budgets 兜底；static_check 把预算记入 budgets_resolved 供装配期核对。`
  决策 7 的说明：预算有**两层**——generator 里的 for/while 用构造参数是「防御性上限」（兜底防死循环），R1 的 Budgets 才是「真实约束」（强制扣减）。`static_check` 把预算记入 `budgets_resolved` 供装配期核对。

- **第 16 行** `"""` docstring 结束。

### 第 17-32 行：imports

- **第 17 行** `from __future__ import annotations` 延迟求值注解。

- **第 19 行** `import asyncio`
  Router 的 `_recognize` 用它 `asyncio.run()` 驱动异步的 `IntentStrategy.recognize`。

- **第 20 行** `from collections.abc import Callable, Generator, Mapping, Sequence`
  - `Callable`：策略/条件函数的类型（如 Graph 的 `cond`、Router 的 classifier）。
  - `Generator`：`_run_child` 的返回类型注解。
  - `Mapping`：`_get`/`_next` 等只读映射参数类型（比 dict 更宽松）。
  - `Sequence`：`_batch` 的 calls、`Blackboard.agents` 的序列类型。

- **第 21 行** `from dataclasses import dataclass`
  定义 `_Edge`（Graph 的有向边）数据类。

- **第 22 行** `from typing import Final`
  给 `_X.PREFIX` 标注 `Final`（常量，不可重新赋值）。

- **第 24 行** `from compose import ToolSpec`
  ToolUse/ReAct 的工具规格类型（来自 compose DTO）。

- **第 25 行** `from compose.program import ProgramFn`
  Program 工厂函数类型（Generator 函数），Graph 的节点、Router 的 arm 用它。

- **第 26 行** `from runtime import AgentState, DynamicValue, Effect, Invoke, Program, Spawn, Terminate`
  从内核 runtime 导入全部运行时类型：
  - `AgentState`：Graph 条件边 `cond` 的入参。
  - `DynamicValue`：回注值的动态类型（架构内所有 data 都是它）。
  - `Effect`：generator yield 的产出类型。
  - `Invoke`：调用单个 port 的 Effect。
  - `Program`：generator 函数产出物。
  - `Spawn`：派生子 Agent 的 Effect（Supervisor/Blackboard 用）。
  - `Terminate`：终止 Effect（`_end` 构造，`_run_child` 拦截）。

- **第 28-32 行** `from .base import (...)`
  从协议层导入契约与报告类型：`Arch`、`ChildSpec`、`Executor`、`IntentResult`、`IntentStrategy`、`JoinStrategy`、`MajorityVote`、`Planner`、`RePlanner`、`ReflectStrategy`、`SkillSpec`、`StaticCheckIssue`、`StaticCheckReport`。
  - 注意 recipes 只**消费**这些类型，不改它们——分层清晰。

### 第 34-45 行：__all__

- **第 34-45 行** `__all__ = [...]`
  导出 10 个内置架构类：`Blackboard`、`Graph`、`Handoff`、`PlanExecute`、`ReAct`、`Reflection`、`Router`、`RouterSkill`、`Supervisor`、`ToolUse`。按字母序排列。`__init__.py` 据此重导出。

### 第 48-51 行：_report 工厂函数

- **第 48-49 行** `def _report(name: str, errors: tuple[StaticCheckIssue, ...], ports: tuple[str, ...], budgets: dict[str, int | float] | None = None) -> StaticCheckReport:`
  统一的报告构造函数。参数：架构名、问题元组、所需端口元组、可选预算字典。

- **第 50-51 行** `return StaticCheckReport(errors=errors, arch=name, version="12.0.0", ports_required=ports, budgets_resolved=budgets or {})`
  构造报告，统一填 `version="12.0.0"`（所有架构同版本），预算用 `budgets or {}` 归一（传 None 时给空 dict）。
  - 为什么不每个架构各自构造报告：`_report` 保证 `version` 一致、缺省值一致，避免 10 处重复且易漂移的代码。

### 第 54-56 行：helper 区注释

- **第 54-56 行** 分隔注释 + 标题：`内部 helper：回注值归一 + Effect 构造 + x-arch.* 进度传递（R12 §6.1.1）`
  下面 6 个 helper 服务于「从回注值读数据」和「构造 Effect」两件事，是读懂 10 个架构的基础。

### 第 57-64 行：_X 类（扩展键前缀）

- **第 57 行** `class _X:`
  架构私有 scratch 键前缀工具。

- **第 58 行** `"""架构私有 scratch 键前缀（集中定义禁用魔法串，R12 §6.1.1）。"""`
  集中定义前缀，禁止在各处散落魔法串 `"x-arch."`。

- **第 59 行** `PREFIX: Final[str] = "x-arch."`
  前缀常量。`Final` 标注不可重新赋值。

- **第 61-62 行** `@staticmethod def of(arch: str, key: str) -> str:`
  静态方法，接收架构名与键名。

- **第 63 行** `"""返回 `x-arch.<arch>.<key>` 键名。"""` 说明返回格式。

- **第 64 行** `return f"{_X.PREFIX}{arch}.{key}"`
  拼出形如 `x-arch.handoff.role`、`x-arch.bb.round` 的键。架构用它在 Effect 间传递进度（决策 4）。

### 第 67-72 行：_dict 归一函数

- **第 67 行** `def _dict(value: DynamicValue) -> dict[str, DynamicValue]:`
  把回注值归一为可读写 dict。

- **第 68-71 行** docstring：`Runner 实测：单 Invoke 回填 Result.data（通常 dict），batch 回填 list[dict]。`
  说明为什么需要归一：回注值形态不定。

- **第 72 行** `return dict(value) if isinstance(value, Mapping) else {}`
  核心防御行：
  - 若是映射 → `dict(value)` 转成普通 dict（顺带拷贝，避免改到原对象）。
  - 否则（如 list、None、标量）→ 返回空 dict `{}`。
  - 不这么写：对 list 调用 `.get()` 会 `AttributeError`；对 None 会崩。归一后下游可安全地 `_dict(out).get(...)`。

### 第 75-78 行：_get 读取函数

- **第 75 行** `def _get(data: Mapping[str, DynamicValue], key: str, default: DynamicValue = None) -> DynamicValue:`
  从回注映射读字段，带默认值。

- **第 76 行** `"""从回注映射读字段，None 视为缺失回退 default。"""` 说明语义。

- **第 77 行** `value = data.get(key)`
  读取字段。

- **第 78 行** `return default if value is None else value`
  **关键防御行**：`None` 视为缺失，回退默认值。
  - 为什么不用 `data.get(key, default)`：因为若字段存在但值显式为 `None`，`.get` 会返回 `None` 而非 `default`。这里显式把 `None` 也当缺失，更稳健。

### 第 81-86 行：_invoke 构造函数

- **第 81-82 行** `def _invoke(port: str, payload: dict[str, DynamicValue] | None = None, **extra: DynamicValue) -> Invoke:`
  构造单个 port 的 Invoke。参数：port 名、可选 payload、额外关键字参数（覆盖 payload 同名键）。

- **第 83 行** `"""构造 Invoke（extra 覆盖 payload 同名键）。"""` 说明覆盖语义。

- **第 84 行** `data: dict[str, DynamicValue] = dict(payload or {})`
  拷贝 payload（None 时给空 dict）。拷贝避免改动传入对象。

- **第 85 行** `data.update(extra)`
  用 extra 覆盖同名键——所以 `extra` 优先级高于 payload。

- **第 86 行** `return Invoke(port=port, payload=data)  # type: ignore[arg-type]`
  构造 Invoke。`type: ignore` 是因为 `DynamicValue` 的静态类型与 Invoke.payload 期望类型不完全匹配，运行时没问题。

### 第 89-93 行：_batch 构造函数

- **第 89-90 行** `def _batch(port: str, calls: Sequence[Mapping[str, DynamicValue]], **extra: DynamicValue) -> Invoke:`
  构造批量 Invoke。

- **第 91 行** `"""构造批量 Invoke（payload.calls 由解释器展开为独立 Effect）。"""`
  决策 6 的说明：`calls` 不是架构自己循环调用，而是**交给解释器展开**成多个独立 Effect。

- **第 92 行** `data: dict[str, DynamicValue] = {"calls": [dict(c) for c in calls], **extra}`
  把 calls 归一成 list[dict]（每项 `dict(c)` 拷贝），连同 extra 一起放进 payload。

- **第 93 行** `return Invoke(port=port, batch=True, payload=data)  # type: ignore[arg-type]`
  **关键行**：`batch=True` 标记这是批量调用，解释器据此展开。不这么写（用普通 Invoke）：解释器不会展开，calls 只是一坨数据，工具根本不会被调用。

### 第 96-99 行：_tool_calls 提取函数

- **第 96 行** `def _tool_calls(value: DynamicValue) -> list[dict[str, DynamicValue]]:`
  从 LLM 回注值提取 tool_calls 列表。

- **第 97 行** `"""从 LLM 回注值提取 tool_calls 列表（每项归一为 dict）。"""` 说明用途。

- **第 98 行** `calls = _get(_dict(value), "tool_calls", []) or []`
  先 `_dict(value)` 归一 → `_get` 取 `tool_calls` 键（缺省 `[]`）→ `or []` 再兜底一次（防止取到 None）。

- **第 99 行** `return [c for c in (_dict(item) for item in calls) if c]`
  生成器 + 列表推导：每项用 `_dict(item)` 归一为 dict，`if c` 过滤掉空 dict。
  - 为什么过滤空 dict：空调用项没有意义，过滤后 `zip(calls, results)` 才不会错位。

### 第 102-104 行：_end 构造函数

- **第 102 行** `def _end(value: DynamicValue = None, reason: str = "") -> Terminate:`
  构造终止 Effect。参数：输出值、原因。

- **第 103 行** `"""构造 Terminate Effect。"""` 说明用途。

- **第 104 行** `return Terminate(output=value, reason=reason)`
  构造 Terminate。`reason` 用于区分正常结束与各种耗尽（`turns_exhausted`/`rounds_exhausted`/`no_route` 等），进 trace 便于诊断。

### 第 107-125 行：_run_child 子程序驱动器（决策 5，核心）

- **第 107 行** `def _run_child(child: Program) -> Generator[Effect, DynamicValue, DynamicValue]:`
  驱动子 Program 的 generator。类型注解：产出 Effect、`send` 进 DynamicValue、最终返回 DynamicValue。

- **第 108-117 行** docstring（含参数/返回说明）：
  - 转发子 Effect，拦截 Terminate 提取 output（不提前结束整个 run）。
  - 子 Program 内的 Terminate 代表「该子流程结束」，不能直接 yield 给 R1（会终止全部）；本驱动把它转成返回值。
  - 与 `compose._run_child` 语义一致，但架构层自包含（不依赖 compose 的实现）。

- **第 118 行** `try:`
  捕获 `StopIteration`（子 generator 自然结束但没 yield Terminate 的情况）。

- **第 119 行** `effect: Effect = next(child)`
  启动子 generator，拿到第一个 Effect。

- **第 120 行** `while not isinstance(effect, Terminate):`
  **核心循环条件**：只要不是 Terminate 就继续转发。Terminate 是「子流程结束信号」，不转发给 R1。

- **第 121 行** `sent: DynamicValue = yield effect`
  把 Effect 转发给外层 R1，暂停；外层 `send` 回注值给 `sent`。

- **第 122 行** `effect = child.send(sent)`
  把回注值送进子 generator，拿下一个 Effect。

- **第 123 行** `return effect.output`
  **决策 5 的落地点**：循环退出说明当前 effect 是 Terminate，返回它的 `output` 作为子流程结果。
  - 为什么不 `yield effect`（把 Terminate 交给 R1）：那会终止整个 run，子流程结束不该结束全部。转成返回值后，父架构可以继续往下走。

- **第 124-125 行** `except StopIteration: return None`
  子 generator 若直接结束（没 yield Terminate），返回 `None`。这是防御：有些 ProgramFn 可能 `return` 而非 `yield _end(...)`。

### 第 128-133 行：_Edge 数据类

- **第 128 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots。图边一旦定义不改。

- **第 129 行** `class _Edge:`
  Graph 的有向边。

- **第 130 行** `"""Graph 的有向边：src→dst，可带 cond(state)->目标 id 的条件分支。"""`
  说明：一条边从 src 到 dst，可选地带条件函数（返回目标 id 或 None）。

- **第 131 行** `src: str`
  起点节点 id。

- **第 132 行** `dst: str`
  终点节点 id。

- **第 133 行** `cond: Callable[[AgentState], str | None] | None = None`
  可选条件函数：入参 AgentState，返回目标 id（命中）或 None（不命中）。默认 None 表示无条件边。
  - 条件边允许「同一 src 按状态跳不同 dst」，这是 Graph 实现分支的关键。

---

### 第 136-172 行：① ToolUse（一次性并行工具调用）

- **第 136 行** `class ToolUse:`
  架构①，最简单的一个：一次性调用一组工具。

- **第 137 行** `"""① 一次性并行工具调用（parallel=True 用 par，否则 seq）。"""`
  `parallel=True` 用 par（并行算子），否则 seq（串行算子）。

- **第 138 行** `name = "ToolUse"; version = "12.0.0"`
  类属性实现 Arch 协议的 `name`/`version`。

#### ToolUse.__init__（第 140-141 行）

- **第 140 行** `def __init__(self, tools: list[ToolSpec], *, parallel: bool = True) -> None:`
  `tools` 必填位置参数；`parallel` 关键字参数（`*` 之后），默认 True。

- **第 141 行** `self.tools = tools; self.parallel = parallel`
  保存两个参数。构造签名对齐 R12 §4「对外 API 不变」。

#### ToolUse.static_check（第 143-151 行）

- **第 143 行** `def static_check(self) -> StaticCheckReport:` 检查方法。

- **第 144 行** `errs = []`
  问题收集列表。

- **第 145-146 行** `if not self.tools: errs.append(StaticCheckIssue("R12-E07", "tools 为空", "tools"))`
  **E07 检查**：工具列表不能为空。空的架构跑起来没有意义，装配期就拦。

- **第 147-150 行** `for t in self.tools:` + 注释 + `if not t.name: errs.append(StaticCheckIssue("R12-E09", "ToolSpec 缺 name", t.name))`
  **E09 检查**：每个 ToolSpec 必须有 `name`。
  - 第 148 行注释很关键：`L1 披露只有 name+description，parameters_schema 缺省空 dict 合法（L2/L3 才披露）`——所以这里**只查 name**，不查 `parameters_schema`（它缺省是合法的）。

- **第 151 行** `return _report(self.name, tuple(errs), ("tool",))`
  返回报告：问题列表转 tuple，所需端口 `("tool",)`。无预算维度（一次性调用无循环预算）。

#### ToolUse.compile（第 153-172 行）

- **第 153 行** `def compile(self) -> Program:`
  编译方法，返回 Program。

- **第 154 行** `def program() -> Program:`
  内部 generator 函数（ProgramFn）。注意：返回的是函数对象调用结果 `program()`，即 generator 实例。

- **第 155-156 行** `calls = [{"name": t.name, "call": {"call_id": t.name, "name": t.name, "args": {}}} for t in self.tools]`
  为每个工具构造调用描述：外层 `name`，内层 `call` 含 `call_id`/`name`/`args`（此处 args 为空 `{}`）。
  - `call_id` 用 `t.name`：工具唯一标识，回填时靠它对齐。

- **第 157-159 行** `if not calls: yield _end([], "tool_all_failed"); return`
  防御分支：calls 为空时（虽然 static_check 已拦，但 compile 独立可调用）直接结束，输出空列表，原因 `tool_all_failed`。

- **第 160 行** `if self.parallel:`
  按并行/串行分支。

- **第 161-163 行** 并行分支：
  - 第 161 行注释：`一次 batch Invoke，解释器展开为多个独立 tool Effect；回填 list[data]。`——决策 6。
  - **第 162 行** `results = yield _batch("tool", calls)`
    一次批量调用，解释器展开。yield 暂停，回填 `list[data]` 给 `results`。
  - **第 163 行** `yield _end(list(results) if isinstance(results, list) else [results])`
    终止并输出全部结果。归一成 list（单结果包成单元素列表）。

- **第 164-170 行** 串行分支：
  - 第 165 行注释：`串行：逐个调用，收集每次回填的 data。`
  - **第 166 行** `results_list: list[DynamicValue] = []` 结果容器。
  - **第 167 行** `for call in calls:` 逐个调用。
  - **第 168 行** `data = yield _invoke("tool", call)`
    单个 Invoke（非 batch），回填单个 data。
  - **第 169 行** `results_list.append(data)` 收集。
  - **第 170 行** `yield _end(results_list)` 终止并输出列表。

- **第 171 行** `return None`
  generator 自然结束。

- **第 172 行** `return program()`
  返回 generator 实例（调用内部函数得到）。**所有架构 compile 都是这个模式**：定义内部 `program()` 再 `return program()`。

### 第 175-219 行：② ReAct（推理-行动自循环）

- **第 175 行** `class ReAct:`
  架构②，经典的 Reasoning-Acting 循环。

- **第 176 行** `"""② 推理-行动自循环（loop 承载；轮次交 Budgets）。"""`
  loop 算子承载；轮次交给 Budgets（决策 7）。

- **第 177 行** `name = "ReAct"; version = "12.0.0"`
  协议属性。

#### ReAct.__init__（第 179-180 行）

- **第 179 行** `def __init__(self, tools: list[ToolSpec], *, llm: str | None = None, max_turns: int = 30) -> None:`
  `tools` 必填；`llm` 可选模型名；`max_turns` 默认 30（防御性上限）。

- **第 180 行** `self.tools = tools; self.llm = llm; self.max_turns = max_turns`
  保存三个参数。

#### ReAct.static_check（第 182-188 行）

- **第 183 行** `errs = []` 收集器。

- **第 184-185 行** `if not self.tools: errs.append(StaticCheckIssue("R12-E07", "tools 为空", "tools"))`
  E07：工具不能为空。

- **第 186-187 行** `if self.max_turns <= 0: errs.append(StaticCheckIssue("R12-E17", "max_turns <= 0", "max_turns"))`
  **E17 检查**：轮次上限必须为正。非正的循环上限无意义。

- **第 188 行** `return _report(self.name, tuple(errs), ("llm", "tool"), {"turns": self.max_turns})`
  报告：所需端口 `("llm", "tool")`（既调 LLM 又调工具），预算 `{"turns": 30}` 供装配期核对。

#### ReAct.compile（第 190-219 行）

- **第 190 行** `def compile(self) -> Program:` 编译方法。

- **第 191 行** `def program() -> Program:` 内部 generator。

- **第 192 行** `messages: list[DynamicValue] = []`
  对话历史列表——ReAct 靠它把历史消息带到下一轮。

- **第 193-196 行** `payload: dict[str, DynamicValue] = {"messages": messages, "tools": [t.model_dump() if hasattr(t, "model_dump") else t for t in self.tools]}`
  初始 payload：
  - `messages`：对话历史（初始空）。
  - `tools`：工具列表，逐个转 dict——有 `model_dump` 就调（pydantic 模型），否则原样（鸭子类型兼容）。
  - 为什么不直接放 ToolSpec 对象：`payload` 要能随 Effect 传递，转成可序列化 dict 更稳。

- **第 197-198 行** `if self.llm is not None: payload["model"] = self.llm`
  指定模型时加入 payload，否则用端口默认模型。

- **第 199 行** `for _ in range(self.max_turns):  # 防御上限；真实轮次约束由 R1 Budgets.turns 兜底`
  循环上限 = `max_turns`（决策 7：防御性上限，真实约束靠 Budgets）。

- **第 200 行** `llm_out = yield _invoke("llm", payload)`
  调 LLM，回填输出给 `llm_out`。

- **第 201 行** `assistant = _dict(llm_out)` 归一 LLM 输出为 dict。

- **第 202 行** `calls = _tool_calls(llm_out)` 提取工具调用列表（见第 96-99 行）。

- **第 203-205 行** 注释 + `messages.append({"role": "assistant", "content": _get(assistant, "content", ""), "tool_calls": calls})`
  把本轮 assistant 消息追加进历史：`content` 缺省空串，`tool_calls` 带上调用。

- **第 206 行** `payload["messages"] = list(messages)`
  更新 payload 的历史。**为什么要 `list(messages)` 拷贝**：避免后续 append 改动到已 yield 出去的 payload 引用（Effect 是异步传递的，共享同一 list 会有副作用）。

- **第 207-209 行** `if not calls: yield _end(_get(assistant, "content")); return`
  **循环退出条件**：没有工具调用说明 LLM 给出最终答案 → 终止并输出 content。

- **第 210-212 行** 注释 + `tool_results = yield _batch("tool", calls)` + `results = tool_results if isinstance(tool_results, list) else [tool_results]`
  有工具调用 → 批量调用工具（决策 6），回填结果归一成 list。

- **第 213-216 行** `for call, data in zip(calls, results):` + `messages.append({"role": "tool", "name": _get(call, "name", ""), "content": data})` + `payload["messages"] = list(messages)`
  把每个工具结果作为 `tool` 消息注入历史，`name` 从 call 读（缺省空串）。`zip` 按位置对齐 calls 与 results——这就是为什么 `_tool_calls` 要过滤空项（保证位置对齐）。

- **第 217 行** `yield _end(messages[-1] if messages else None, "turns_exhausted")`
  循环正常跑完（没提前退出）→ 轮次耗尽，输出最后一条消息，原因 `turns_exhausted`。

- **第 218 行** `return None` generator 结束。

- **第 219 行** `return program()` 返回 generator。

### 第 222-272 行：③ PlanExecute（规划-执行状态机）

- **第 222 行** `class PlanExecute:`
  架构③，先规划再逐步执行，失败可重规划。

- **第 223 行** `"""③ 规划-执行状态机（seq(plan, execute, [replan])）。"""`
  用 seq 串接：plan → execute →（可选）replan。

- **第 224 行** `name = "PlanExecute"; version = "12.0.0"` 协议属性。

#### PlanExecute.__init__（第 226-228 行）

- **第 226-227 行** `def __init__(self, planner: Planner, executor: Executor, *, replanner: RePlanner | None = None) -> None:`
  `planner`/`executor` 必填（注入的策略），`replanner` 可选。

- **第 228 行** `self.planner = planner; self.executor = executor; self.replanner = replanner`
  保存三个策略。

#### PlanExecute.static_check（第 230-235 行）

- **第 231 行** `errs = []` 收集器。

- **第 232-234 行** `for role, obj in (("planner", self.planner), ("executor", self.executor)):` + `if obj is None or not getattr(obj, "name", None): errs.append(StaticCheckIssue("R12-E15", f"{role} 未履约", role))`
  **E15 检查（策略履约）**：planner 和 executor 都必须有非空 `name`。
  - `getattr(obj, "name", None)` 安全读取（obj 可能是任意对象）。这正是 base.py 第 48-50 行注释说的「装配期只读 name 判 E15」的落地。
  - 注意这里**不检查 replanner**——它是可选注入点，缺席合法。

- **第 235 行** `return _report(self.name, tuple(errs), ("llm",))`
  报告：所需端口 `("llm",)`。注意没有 tool 端口——步骤执行通过 `port` 字段动态指定（见第 248 行），不在这里声明。

#### PlanExecute.compile（第 237-262 行）

- **第 237 行** `def compile(self) -> Program:` 编译方法。

- **第 238 行** `def program() -> Program:` 内部 generator。

- **第 239 行** 注释：`规划：LLM 产出步骤（约定回注 {"steps": [...]} 或 {"content": ...}）。`

- **第 240-241 行** `plan_out = yield _invoke("llm", {"messages": [{"role": "system", "content": getattr(self.planner, "prompt", "plan")}]})`
  调 LLM 做规划。system 内容从 `planner.prompt` 读（缺省 `"plan"`）——策略持有自己的 prompt，架构不硬编码。

- **第 242 行** `steps = self._parse_steps(plan_out)` 用 helper 解析步骤（见第 264-272 行）。

- **第 243 行** `last: DynamicValue = None` 记录最后一步输出。

- **第 244 行** `replan_budget = self.max_replans if hasattr(self, "max_replans") else len(steps) + 1`
  重规划预算：若构造时设了 `max_replans` 用它，否则用 `len(steps) + 1` 作防御上限。
  - `hasattr` 检查：构造签名里没有 `max_replans`（对外 API 不变），但允许动态注入；没有时用步骤数+1 兜底。
  - 这是防御性上限（决策 7），防止 replan 无限循环。

- **第 245 行** `for step in steps:` 逐个执行步骤。

- **第 246 行** 注释：`执行：按步骤构造 tool/llm 调用（约定 step={"port","payload"}）。`

- **第 247 行** `step_map = _dict(step)` 归一步骤为 dict。

- **第 248 行** `port = str(_get(step_map, "port", "tool"))`
  从步骤读目标端口，缺省 `"tool"`。这就是上面 static_check 不声明 tool 端口的原因——端口由步骤动态指定。

- **第 249 行** `payload = _dict(_get(step_map, "payload", {}))`
  从步骤读 payload，缺省空 dict 并归一。

- **第 250 行** `out = yield _invoke(port, payload)` 执行该步骤，回填输出。

- **第 251 行** `last = out` 记录最后输出。

- **第 252 行** `failed = bool(_get(_dict(out), "ok", True) is False or _get(_dict(out), "error"))`
  **失败判定**：两个条件之一即失败——
  - `ok` 字段显式为 `False`（注意用 `is False`，只有布尔假才算，避免 `0`/`""` 误判）。
  - 或有 `error` 字段（非空即视为有错误）。
  - 注意 `_dict(out)` 调用两次：一次查 ok，一次查 error（每次都归一，稳健）。

- **第 253-259 行** 失败 + 可重规划分支：
  - **第 253 行** `if failed and self.replanner is not None and replan_budget > 0:`
    三个条件：失败、有 replanner、还有预算。
  - **第 254-257 行** `replan_out = yield _invoke("llm", {"messages": [system(replanner.prompt), user({"steps": steps, "failed": step, "last": out})]})`
    调 LLM 重规划，传入当前步骤、失败步骤、最后输出。system 内容从 `replanner.prompt` 读（缺省 `"replan"`）。
  - **第 258 行** `steps = self._parse_steps(replan_out)` 解析新步骤。
  - **第 259 行** `replan_budget -= 1` 扣减重规划预算。
  - 注意：替换了 `steps` 但 `for step in steps` 用的是原迭代器——Python 中 for 循环迭代器在循环开始时固定，替换 `steps` 变量**不会**改变当前正在迭代的序列。这意味着重规划的新步骤不会在本轮被立即执行（这是一个实现细节/已知限制，但文档按源码如实记录）。

- **第 260 行** `yield _end(last, "replan_exhausted" if replan_budget <= 0 else "")`
  终止，输出最后结果。预算耗尽时原因 `replan_exhausted`。

- **第 261 行** `return None` generator 结束。

- **第 262 行** `return program()` 返回 generator。

#### PlanExecute._parse_steps（第 264-272 行，静态方法）

- **第 264 行** `@staticmethod` 静态方法（不依赖实例）。

- **第 265 行** `def _parse_steps(value: DynamicValue) -> list[DynamicValue]:`
  从 LLM 回注解析步骤列表。

- **第 266 行** docstring：`约定 steps 键；退而取 content 文本为单步`。

- **第 267 行** `data = _dict(value)` 归一。

- **第 268 行** `steps = _get(data, "steps")` 取 `steps` 键。

- **第 269-270 行** `if isinstance(steps, list): return steps`
  是 list 直接返回（约定格式）。

- **第 271-272 行** `content = _get(data, "content")` + `return [{"port": "llm", "payload": {"messages": [{"role": "user", "content": content}]}}] if content else []`
  **降级路径**：没有 steps 但有 content 文本 → 构造成一个单步（port=llm，payload 是 user 消息）。既没有 steps 也没有 content → 返回空列表。
  - 这个降级让「LLM 只回了一段文本」也能被当作一步执行，而不是直接失败。

### 第 275-314 行：④ Reflection（起草-反思-修订）

- **第 275 行** `class Reflection:`
  架构④，产出草稿后反复反思修订直到通过或轮次耗尽。

- **第 276 行** `"""④ 起草-反思-修订（seq(draft, reflect, revise)；轮次交 Budgets）。"""`
  seq 串接 draft → reflect → revise；轮次交 Budgets。

- **第 277 行** `name = "Reflection"; version = "12.0.0"` 协议属性。

#### Reflection.__init__（第 279-280 行）

- **第 279 行** `def __init__(self, reflector: ReflectStrategy, *, max_rounds: int = 3) -> None:`
  `reflector` 必填（反思策略），`max_rounds` 默认 3。

- **第 280 行** `self.reflector = reflector; self.max_rounds = max_rounds` 保存。

#### Reflection.static_check（第 282-288 行）

- **第 283 行** `errs = []` 收集器。

- **第 284-285 行** `if self.max_rounds <= 0: errs.append(StaticCheckIssue("R12-E17", "max_rounds <= 0", "max_rounds"))`
  E17：反思轮次必须为正。

- **第 286-287 行** `if not getattr(self.reflector, "name", None): errs.append(StaticCheckIssue("R12-E15", "reflector 未履约", "reflector"))`
  E15：reflector 必须具名履约。

- **第 288 行** `return _report(self.name, tuple(errs), ("llm",), {"rounds": self.max_rounds})`
  报告：端口 `("llm",)`，预算 `{"rounds": 3}`。

#### Reflection.compile（第 290-314 行）

- **第 290 行** `def compile(self) -> Program:` 编译方法。

- **第 291 行** `def program() -> Program:` 内部 generator。

- **第 292-294 行** 注释 + `draft_out = yield _invoke("llm", {"messages": [{"role": "system", "content": "draft"}]})`
  起草：调 LLM，system 内容固定 `"draft"`。

- **第 295 行** `best = _get(_dict(draft_out), "content", "") or ""`
  取草稿文本，`content` 缺省空串，`or ""` 再兜底（None → 空串）。`best` 保存当前最佳草稿。

- **第 296 行** `for _ in range(self.max_rounds):  # 防御上限；真实轮次由 R1 Budgets.rounds 兜底`
  反思循环，防御上限 `max_rounds`。

- **第 297-301 行** 注释 + `critique_out = yield _invoke("llm", {"messages": [{"role": "system", "content": getattr(self.reflector, "prompt", "critique")}, {"role": "user", "content": best}]})`
  反思：调 LLM，system 从 `reflector.prompt` 读（缺省 `"critique"`），user 内容是当前草稿 `best`。

- **第 302 行** `critique = _dict(critique_out)` 归一评判结果为 dict。

- **第 303-305 行** `if bool(_get(critique, "passed", False)): yield _end(best); return`
  **通过条件**：`passed` 字段为真（缺省 False）→ 终止并输出当前草稿。
  - 缺省 False 很重要：拿不到明确通过信号时，不轻易认为通过，继续修订（fail-closed 思路）。

- **第 306-310 行** 注释 + `revise_out = yield _invoke("llm", {"messages": [{"role": "system", "content": "revise"}, {"role": "user", "content": {"draft": best, "critique": critique}}]})`
  修订：调 LLM，system 固定 `"revise"`，user 内容是 `{draft, critique}` 结构。

- **第 311 行** `best = _get(_dict(revise_out), "content", best) or best`
  更新最佳草稿：取修订结果的 content，缺省/为假时保留原 `best`（`or best` 防止把草稿清空）。

- **第 312 行** `yield _end(best, "rounds_exhausted")`
  循环跑完仍未通过 → 输出当前草稿，原因 `rounds_exhausted`。

- **第 313 行** `return None` generator 结束。

- **第 314 行** `return program()` 返回 generator。

---

### 第 317-369 行：⑤ Router（意图路由）

- **第 317 行** `class Router:`
  架构⑤，按意图标签把请求路由到不同的 arm（子架构/技能/ProgramFn）。

- **第 318 行** `"""⑤ 意图路由（branch(fn, arms, default)）。"""`
  用 branch 算子：`fn` 是识别函数，`arms` 是分支表，`default` 是兜底。

- **第 319 行** `name = "Router"; version = "12.0.0"` 协议属性。

#### Router.__init__（第 321-325 行）

- **第 321-323 行** `def __init__(self, classifier: IntentStrategy | Callable[..., IntentResult] | None, arms: Mapping[str, Arch | SkillSpec], *, default: str | None = None, min_confidence: float = 0.0) -> None:`
  - `classifier`：三种形态——`IntentStrategy`（异步协议）、`Callable`（同步函数）、`None`（不注入，走兜底）。
  - `arms`：标签 → 目标（Arch 或 SkillSpec）的映射。
  - `default`：兜底标签，关键字参数，默认 None。
  - `min_confidence`：置信度阈值，默认 0.0。

- **第 324-325 行** `self.classifier = classifier; self.arms = arms` + `self.default = default; self.min_confidence = min_confidence`
  保存四个参数。

#### Router.static_check（第 327-333 行）

- **第 328 行** `errs = []` 收集器。

- **第 329-330 行** `if not self.arms: errs.append(StaticCheckIssue("R12-E07", "arms 为空", "arms"))`
  E07：分支表不能为空，空了无处可路由。

- **第 331-332 行** `if self.default is not None and self.default not in self.arms: errs.append(StaticCheckIssue("R12-E08", "default 不在 arms", "default"))`
  **E08 检查**：兜底标签必须存在于 arms 中。否则运行时 `self.arms[target]` 会 `KeyError`。
  - `is not None` 守卫：允许不设 default（此时 default 为 None 是合法的）。

- **第 333 行** `return _report(self.name, tuple(errs), ("llm",))`
  报告：端口 `("llm",)`（意图识别通常靠 LLM）。

#### Router.compile（第 335-349 行）

- **第 335 行** `def compile(self) -> Program:` 编译方法。

- **第 336 行** `def program() -> Program:` 内部 generator。

- **第 337 行** `intent = self._recognize()`
  调 `_recognize` 得到 `IntentResult`（见第 351-369 行）。注意这是**同步调用**，但内部可能驱动异步协议。

- **第 338 行** `label, conf = intent.label, intent.confidence`
  拆出标签与置信度。

- **第 339 行** `target = label if conf >= self.min_confidence and label in self.arms else self.default`
  **路由决策**：置信度达标 **且** 标签在 arms 中 → 用 label；否则走 default。
  - 两个条件都必要：置信度低不能路由（即使标签存在），标签不存在也不能路由（即使置信度高）。

- **第 340-342 行** `if target is None: yield _end(None, "no_route"); return`
  没有目标（既没命中也没 default）→ 终止，原因 `no_route`。

- **第 343 行** `arm = self.arms[target]` 取目标 arm。

- **第 344-345 行** 注释 + `factory: ProgramFn = arm.compile if hasattr(arm, "compile") else arm  # type: ignore[assignment]`
  **鸭子类型分发**：arm 有 `compile` 方法（Arch）就取它；否则 arm 本身当作 ProgramFn。
  - 这让 arm 既可以是架构对象，也可以是裸 generator 函数。

- **第 346 行** `out = yield from _run_child(factory())`
  **决策 5 的落地**：`yield from _run_child(...)` 驱动子 Program，转发其 Effect，拦截 Terminate 拿 output。
  - `factory()` 调用得到 generator（Program）。

- **第 347 行** `yield _end(out)` 用子流程的输出作为自己的输出终止。

- **第 348 行** `return None` generator 结束。

- **第 349 行** `return program()` 返回 generator。

#### Router._recognize（第 351-369 行，决策 10 核心）

- **第 351-356 行** docstring：
  - 调 classifier 得 `IntentResult`。
  - 注入 `IntentStrategy` 时以默认文本走 `recognize()`（异步协议，此处取协程结果）。
  - 注入 `Callable` 时直接调用；未注入时按 label 为空走兜底。

- **第 357 行** `cls = self.classifier` 局部变量。

- **第 358-359 行** `if cls is None: return IntentResult(label="", confidence=0.0)`
  未注入分类器 → 返回空标签、零置信度的结果，让上层走 default 分支（因为空标签不在 arms 中，且 conf=0 < 阈值……注意若 min_confidence=0，则 `conf >= 0` 成立但 `"" in arms` 通常不成立，仍走 default）。

- **第 360-361 行** `if isinstance(cls, IntentStrategy):` + `result: DynamicValue = asyncio.run(cls.recognize("", None))  # type: ignore[arg-type]`
  **决策 10 的落地点**：`IntentStrategy` 的 `recognize` 是 `async def`，返回协程。这里用 `asyncio.run()` 同步驱动它取结果。
  - 注意传的是空文本 `""` 和 `None` ctx——因为架构层拿不到真实输入文本（文本在 Effect 里，这里只是识别策略的装配期占位调用）。这是实现的一个简化点。
  - `type: ignore`：ctx 传 `None` 与声明的 `AgentState` 类型不符。

- **第 362-364 行**  else 分支（Callable）：
  - **第 363 行** `raw: DynamicValue = cls("")  # Callable[[str], ...]`
    直接调用同步函数，传空文本。
  - **第 364 行** `result = raw if not hasattr(raw, "__await__") else asyncio.run(raw)`
    **兼容异步 Callable**：若返回值有 `__await__`（是 coroutine），也用 `asyncio.run` 驱动；否则直接用。
    - 这行让 classifier 无论是同步函数、异步函数、还是返回协程的对象都能工作。

- **第 365-366 行** `if isinstance(result, IntentResult): return result`
  结果已是 `IntentResult` 信封 → 直接返回。

- **第 367-369 行** 降级构造：
  - **第 367 行** `data = _dict(result)` 归一为 dict。
  - **第 368-369 行** `return IntentResult(label=str(_get(data, "label", "")), confidence=float(_get(data, "confidence", 0.0)))`
    从 dict 读 `label`/`confidence`，分别用 `str()`/`float()` 强转（防类型不符），构造信封返回。

### 第 372-408 行：⑥ RouterSkill（技能路由，Router 变体）

- **第 372 行** `class RouterSkill(Router):`
  架构⑥，继承 Router，路由目标可以是 SkillSpec（技能）。

- **第 373 行** `"""⑥ 技能路由（Router 变体，routes 可含 SkillSpec）。"""` 说明用途。

- **第 374 行** `name = "RouterSkill"`
  覆盖 `name`，但**没有覆盖 `version`**——继承 Router 的 `"12.0.0"`。

#### RouterSkill.__init__（第 376-380 行）

- **第 376-377 行** `def __init__(self, classifier: IntentStrategy | Callable[..., IntentResult] | None, routes: Mapping[str, Arch | SkillSpec], default: str) -> None:`
  与 Router 类似，但参数名是 `routes`（保留 R12 §4 签名），且 `default` 是**必填**（无默认值）。

- **第 378-379 行** 注释：`签名按 R12 §4 保留 routes 名；值类型与基类 arms 一致，只读映射可直接向上传递。`

- **第 380 行** `super().__init__(classifier, routes, default=default)`
  委托父类构造——`routes` 传给基类的 `arms`。所以 RouterSkill 复用基类的 `self.arms`、`_recognize`、`static_check`。

#### RouterSkill.compile（第 382-408 行）

- **第 382 行** `def compile(self) -> Program:` 覆盖父类的 compile。

- **第 383 行** `def program() -> Program:` 内部 generator。

- **第 384 行** `intent = self._recognize()` 复用父类的识别逻辑。

- **第 385 行** `route = self.arms.get(intent.label) or self.arms.get(self.default or "")`
  路由选择：先按 `intent.label` 查；查不到（或假值）再按 `default` 查。`self.default or ""` 防止 default 为 None。
  - 注意：**没有用 `min_confidence` 判断**（父类 Router 用了）。RouterSkill 简化为「标签命中即路由」。

- **第 386-388 行** `if route is None: yield _end(None, "no_route"); return`
  没有路由目标 → 终止，原因 `no_route`。

- **第 389-397 行** SkillSpec 分支：
  - **第 389 行** 注释：`SkillSpec：先加载技能正文（R5 SkillPort 履约），再执行下游。`
  - **第 390 行** `if isinstance(route, SkillSpec):`
    - **第 391 行** `yield _invoke("skill", {"name": route.name, "part": "body"})`
      先发一个 `skill` 端口的 Invoke 加载技能正文（`part: "body"`）——实际加载由 R5 SkillPort 完成（见 base.py 第 133-136 行注释）。
      - 注意这里 `yield` 了但没用返回值——加载完即继续。
    - **第 392 行** `downstream = getattr(route, "downstream", None)` 取技能的下游（若有）。
    - **第 393-397 行** `if downstream is not None:` + `factory = downstream.compile if isinstance(downstream, Arch) else downstream` + `out = yield from _run_child(factory())` + `yield _end(out)` + `return`
      有下游 → 鸭子类型取 compile，`_run_child` 驱动，输出子结果并终止返回。

- **第 398-401 行** Arch 分支：
  - **第 398 行** `elif isinstance(route, Arch):`
  - **第 399 行** `out = yield from _run_child(route.compile())` 驱动架构子流程。
  - **第 400 行** `yield _end(out)` 输出结果。
  - **第 401 行** `return` 返回。

- **第 402-405 行** ProgramFn 分支（else）：
  - **第 402 行** `else:  # ProgramFn`
  - **第 403 行** `out = yield from _run_child(route())` 直接调用裸函数。
  - **第 404 行** `yield _end(out)` 输出结果。
  - **第 405 行** `return` 返回。

- **第 406 行** `yield _end(_get(_dict(route), "name", intent.label))`
  兜底：若 route 既不是 SkillSpec 带下游、也不是 Arch、也不是 ProgramFn（如占位对象），输出它的 name（缺省用 intent.label）。

- **第 407 行** `return None` generator 结束。

- **第 408 行** `return program()` 返回 generator。

### 第 411-458 行：⑦ Supervisor（监督者-子代理）

- **第 411 行** `class Supervisor:`
  架构⑦，LLM 拆分任务 → 并行 Spawn 子 Agent → 合并结果。

- **第 412 行** `"""⑦ 监督者-子代理（par(planner, children)；子数交 Budgets）。"""`
  par 算子；子数交 Budgets（决策 7）。

- **第 413 行** `name = "Supervisor"; version = "12.0.0"` 协议属性。

#### Supervisor.__init__（第 415-418 行）

- **第 415-416 行** `def __init__(self, planner: Planner, child_spec: ChildSpec, *, max_children: int = 8, join: JoinStrategy | None = None) -> None:`
  `planner`/`child_spec` 必填；`max_children` 默认 8；`join` 可选。

- **第 417 行** `self.planner = planner; self.child_spec = child_spec` 保存策略。

- **第 418 行** `self.max_children = max_children; self.join = join or MajorityVote()`
  **关键行**：`join or MajorityVote()`——未注入合并策略时用默认的多数投票（协议+默认实现成对纪律）。
  - 注意 `or` 语义：若传入 falsy 的 join 对象（一般不会）也会用默认；这里依赖 JoinStrategy 实例为真值。

#### Supervisor.static_check（第 420-426 行）

- **第 421 行** `errs = []` 收集器。

- **第 422-423 行** `if not getattr(self.planner, "name", None): errs.append(StaticCheckIssue("R12-E15", "planner 未履约", "planner"))`
  E15：planner 必须具名。

- **第 424-425 行** `if self.max_children <= 0: errs.append(StaticCheckIssue("R12-E17", "max_children <= 0", "max_children"))`
  E17：子代理上限必须为正。

- **第 426 行** `return _report(self.name, tuple(errs), ("llm",), {"parallel_children": self.max_children})`
  报告：端口 `("llm",)`，预算 `{"parallel_children": 8}`。

#### Supervisor.compile（第 428-444 行）

- **第 428 行** `def compile(self) -> Program:` 编译方法。

- **第 429 行** `def program() -> Program:` 内部 generator。

- **第 430-432 行** 注释 + `split_out = yield _invoke("llm", {"messages": [{"role": "system", "content": getattr(self.planner, "prompt", "split")}]})`
  拆分任务：调 LLM，system 从 `planner.prompt` 读（缺省 `"split"`）。

- **第 433 行** `tasks = _get(_dict(split_out), "tasks", []) or []`
  取 `tasks` 列表，缺省空，`or []` 兜底。

- **第 434 行** `tasks = tasks[:self.max_children]  # 并发上限（Budgets.parallel_children 再校验）`
  **防御性上限**：切片截到 `max_children`。真实并发约束由 Budgets 再校验（决策 7）。

- **第 435-437 行** `if not tasks: yield _end([], "join_timeout"); return`
  无任务 → 终止，输出空列表，原因 `join_timeout`。

- **第 438-440 行** 注释 + `spawns = [Spawn(program_factory=self._child_factory(t)) for t in tasks]` + `results = yield spawns  # type: ignore[misc]`
  **并行派生**：为每个任务构造一个 `Spawn` Effect（携带子 Program 工厂），一次性 `yield` 整个列表。
  - 解释器支持 `yield list[Effect]` 作为「并行声明」，并发执行后回填 `list[data]`。
  - 这是真正的并行派发——不是架构自己 for 循环调 `yield from`。

- **第 441 行** `values = results if isinstance(results, list) else [results]`
  归一回填结果为 list。

- **第 442 行** `yield _end(self.join.join(list(values)))`
  **合并**：用注入的 join 策略（默认 MajorityVote）合并所有子结果，输出。

- **第 443 行** `return None` generator 结束。

- **第 444 行** `return program()` 返回 generator。

#### Supervisor._child_factory（第 446-458 行）

- **第 446 行** `def _child_factory(self, task: DynamicValue) -> ProgramFn:`
  按 `child_spec` 与单个 task 构造子 Program 工厂。

- **第 447 行** docstring：`无参、可重建（D-8）`——工厂无参数、可重复调用重建 generator，保证幂等（D-8 纪律）。

- **第 448 行** `spec = self.child_spec` 局部变量。

- **第 449 行** `def child() -> Program:` 内部子 generator 函数。

- **第 450-451 行** `factory = getattr(spec, "program_factory", None) or getattr(spec, "build", None)`
  **鸭子类型取工厂**：优先 `spec.program_factory`，否则 `spec.build`。
  - 策略持有自己的构造方式，架构不硬编码。

- **第 452-454 行** `if callable(factory):` + `out = yield from _run_child(factory(task)())` + `yield _end(out)`
  工厂可调用 → 调用它拿子 Program（注意 `factory(task)()` 两次调用：先 `factory(task)` 拿 generator 函数，再 `()` 得 generator），`_run_child` 驱动，输出结果。

- **第 455-456 行** `else:` + `yield _invoke("llm", {"messages": [{"role": "user", "content": task}]})`
  工厂不可调用（child_spec 没提供构造方式）→ 降级：直接把 task 当 user 消息调一次 LLM。

- **第 457 行** `return None` child generator 结束。

- **第 458 行** `return child` 返回子工厂。

---

### 第 461-505 行：⑧ Handoff（角色交接）

- **第 461 行** `class Handoff:`
  架构⑧，多个角色按顺序接力（或按回注指定的角色跳转）。

- **第 462 行** `"""⑧ 角色交接（seq 串接角色 Program；跳数交 Budgets）。"""`
  seq 串接角色 Program；跳数交 Budgets。

- **第 463 行** `name = "Handoff"; version = "12.0.0"` 协议属性。

#### Handoff.__init__（第 465-466 行）

- **第 465 行** `def __init__(self, roles: Mapping[str, Arch], *, max_hops: int = 5) -> None:`
  `roles` 必填（角色名 → Arch 的映射）；`max_hops` 默认 5。

- **第 466 行** `self.roles = roles; self.max_hops = max_hops` 保存。

#### Handoff.static_check（第 468-474 行）

- **第 469 行** `errs = []` 收集器。

- **第 470-471 行** `if not self.roles: errs.append(StaticCheckIssue("R12-E07", "roles 为空", "roles"))`
  E07：角色表不能为空。

- **第 472-473 行** `if self.max_hops <= 0: errs.append(StaticCheckIssue("R12-E17", "max_hops <= 0", "max_hops"))`
  E17：跳数上限必须为正。

- **第 474 行** `return _report(self.name, tuple(errs), ("llm",), {"hops": self.max_hops})`
  报告：端口 `("llm",)`，预算 `{"hops": 5}`。

#### Handoff.compile（第 476-505 行）

- **第 476 行** `def compile(self) -> Program:` 编译方法。

- **第 477 行** `def program() -> Program:` 内部 generator。

- **第 478-480 行** `if not self.roles: yield _end(None, "no_route"); return`
  防御分支：角色表为空直接终止（static_check 已拦，但 compile 独立可调用）。

- **第 481 行** `role_order = list(self.roles)  # 声明顺序即默认接力顺序`
  **关键**：`dict` 保序（Python 3.7+），`list(self.roles)` 拿到键的声明顺序，作为默认接力顺序（决策：配置即顺序）。

- **第 482 行** `role = role_order[0]  # 起始角色` 从第一个角色开始。

- **第 483 行** `last: DynamicValue = None` 记录最后输出。

- **第 484 行** `for _ in range(self.max_hops):  # 防御上限；真实跳数由 R1 Budgets.hops 兜底`
  接力循环，防御上限 `max_hops`。

- **第 485 行** `role_program = self.roles[role]` 取当前角色的 Program。

- **第 486-492 行** 三种角色形态的分发：
  - **第 486-487 行** `if isinstance(role_program, Arch):` + `out = yield from _run_child(role_program.compile())`
    是 Arch → 编译并驱动子流程。
  - **第 488-489 行** `elif callable(role_program):` + `out = yield from _run_child(role_program())`
    是可调用对象（ProgramFn）→ 直接驱动。
  - **第 490-492 行** `else:` + 注释 + `out = yield _invoke("llm", {"name": role})`
    非 Program 值（占位对象/角色名）→ 发一个带 `name` 的具名 Invoke 表示「交接到达该角色」。
    - 注释（第 491 行）：`非 Program 值（如占位对象/角色名）：发一个具名 Invoke 表示交接到达该角色。`

- **第 493 行** `last = out` 记录输出。

- **第 494-502 行** 交接目标决策：
  - **第 494 行** 注释：`交接目标从回注读（x-arch.handoff.role / handoff）；无显式目标则按声明顺序接力。`
  - **第 495 行** `nxt = _get(_dict(out), _X.of("handoff", "role")) or _get(_dict(out), "handoff")`
    **决策 4 的落地**：从回注读 `x-arch.handoff.role` 扩展键，没有则读普通 `handoff` 键。`_X.of("handoff", "role")` 生成 `x-arch.handoff.role`。
  - **第 496-497 行** `if nxt is not None and str(nxt) in self.roles: role = str(nxt)`
    有显式目标且是合法角色名 → 跳到该角色。
  - **第 498-502 行** else（按声明顺序接力）：
    - **第 499 行** `pos = role_order.index(role)` 当前角色位置。
    - **第 500-501 行** `if pos + 1 >= len(role_order): break`
      已是末位角色 → 交接完成，跳出循环。
    - **第 502 行** `role = role_order[pos + 1]` 否则前进到下一个角色。

- **第 503 行** `yield _end(last, "hops_exhausted" if len(role_order) > self.max_hops else "")`
  终止，输出最后结果。若角色数超过跳数上限，原因 `hops_exhausted`（说明没走完所有角色）。
  - 注意判断依据是 `len(role_order) > self.max_hops`（角色数 vs 上限），而非是否真的耗尽。

- **第 504 行** `return None` generator 结束。

- **第 505 行** `return program()` 返回 generator。

### 第 508-551 行：⑨ Blackboard（黑板协作）

- **第 508 行** `class Blackboard:`
  架构⑨，多代理共享一块「黑板」，每轮并行写入，冲突键首个胜出。

- **第 509 行** `"""⑨ 黑板协作（race 多代理竞速；轮次交 Budgets）。"""`
  race 算子（竞速）；轮次交 Budgets。

- **第 510 行** `name = "Blackboard"; version = "12.0.0"` 协议属性。

#### Blackboard.__init__（第 512-513 行）

- **第 512 行** `def __init__(self, agents: Sequence[Arch], *, rounds: int = 3) -> None:`
  `agents` 必填（Arch 序列）；`rounds` 默认 3。

- **第 513 行** `self.agents = agents; self.rounds = rounds` 保存。

#### Blackboard.static_check（第 515-521 行）

- **第 516 行** `errs = []` 收集器。

- **第 517-518 行** `if not self.agents: errs.append(StaticCheckIssue("R12-E07", "agents 为空", "agents"))`
  E07：代理列表不能为空。

- **第 519-520 行** `if self.rounds <= 0: errs.append(StaticCheckIssue("R12-E17", "rounds <= 0", "rounds"))`
  E17：轮次必须为正。

- **第 521 行** `return _report(self.name, tuple(errs), ("llm",), {"rounds": self.rounds})`
  报告：端口 `("llm",)`，预算 `{"rounds": 3}`。

#### Blackboard.compile（第 523-540 行）

- **第 523 行** `def compile(self) -> Program:` 编译方法。

- **第 524 行** `def program() -> Program:` 内部 generator。

- **第 525 行** `board: dict[str, DynamicValue] = {}`
  **共享黑板**：跨轮次累积的键值存储。

- **第 526 行** `for _ in range(self.rounds):  # 防御上限；真实轮次由 R1 Budgets.rounds 兜底`
  竞速循环，防御上限 `rounds`。

- **第 527-529 行** 注释 + `spawns = [Spawn(program_factory=self._bb_agent(a, board)) for a in self.agents]` + `results = yield spawns  # type: ignore[misc]`
  和 Supervisor 一样：为每个 agent 构造 Spawn，一次性 yield 列表并行派生。
  - 注意 `_bb_agent(a, board)` 把**当前 board 快照**传进子 agent。

- **第 530 行** `values = results if isinstance(results, list) else [results]` 归一结果。

- **第 531-535 行** 注释 + 合并写入：
  - **第 531 行** 注释：`合并各 agent 的写入（约定回注 {"writes": {...}}）；冲突键按首个胜出仲裁。`
  - **第 532 行** `for value in values:` 逐个 agent 的结果。
  - **第 533 行** `writes = _dict(_get(_dict(value), "writes", {}))` 取 `writes` 信封并归一为 dict。
  - **第 534-535 行** `for key, val in writes.items(): board.setdefault(key, val)`
    **冲突仲裁关键行**：`board.setdefault(key, val)`——只在 key 不存在时写入。
    - 所以多个 agent 写同一 key 时，**第一个写入者胜出**（确定性，与 MajorityVote 平票取首个同理）。

- **第 536 行** `board[_X.of("bb", "round")] = int(board.get(_X.of("bb", "round"), 0)) + 1`
  **决策 4 落地**：用 `x-arch.bb.round` 扩展键记录轮次（读当前值或 0，+1）。进度存在 board 里随结果传递。

- **第 537 行** `board[_X.of("bb", "board")] = dict(board)`
  用 `x-arch.bb.board` 扩展键存一份黑板快照。

- **第 538 行** `yield _end(board)` 终止并输出整块黑板。

- **第 539 行** `return None` generator 结束。

- **第 540 行** `return program()` 返回 generator。

#### Blackboard._bb_agent（第 542-551 行）

- **第 542 行** `def _bb_agent(self, agent: Arch, board: Mapping[str, DynamicValue]) -> ProgramFn:`
  按单个 agent 与共享 board 构造子 Program 工厂。

- **第 543 行** docstring：`按单个 agent 与共享 board 构造子 Program 工厂。`

- **第 544 行** `def child() -> Program:` 内部子 generator。

- **第 545 行** `factory = agent.compile if isinstance(agent, Arch) else agent`
  鸭子类型取工厂（Arch 取 compile，否则裸函数）。

- **第 546 行** `out = yield from _run_child(factory())` 驱动子流程，拿输出。

- **第 547-548 行** 注释 + `writes = _dict(_get(_dict(out), "writes", {})) or {"result": out}`
  **输出归一为 write 信封**：取 `writes`，若为空则用 `{"result": out}` 兜底——保证每个 agent 至少有一条写入。

- **第 549 行** `yield _end({"writes": writes})`
  终止，输出 `{"writes": ...}` 信封，供上层合并。

- **第 550 行** `return None` child generator 结束。

- **第 551 行** `return child` 返回子工厂。

### 第 554-694 行：⑩ Graph（声明式图，最复杂）

- **第 554 行** `class Graph:`
  架构⑩，声明式有向图：`.node()` 定义节点、`.edge()` 定义边、`.exit()` 定义出口、`.compile()` 编译。

- **第 555 行** `"""⑩ 声明式图（.node/.edge/.exit/.compile；node 用 seq 串接）。"""`
  node 用 seq 串接。Graph 是唯一有「构建期 API」的架构（其他架构构造即完成配置）。

- **第 556 行** `name = "Graph"; version = "12.0.0"` 协议属性。

#### Graph.__init__（第 558-563 行）

- **第 558 行** `def __init__(self) -> None:` 无参构造，节点/边/出口靠后续方法添加。

- **第 559 行** `self._nodes: dict[str, ProgramFn] = {}`
  节点表：id → ProgramFn（工厂函数，不是 generator 实例）。

- **第 560 行** `self._inputs: dict[str, tuple[str, ...]] = {}   # node id → 声明的 inputs`
  输入声明表（目前只记录，未用于执行）。

- **第 561 行** `self._outputs: dict[str, tuple[str, ...]] = {}  # node id → 声明的 outputs`
  输出声明表（同上）。

- **第 562 行** `self._edges: list[_Edge] = []`
  边列表。

- **第 563 行** `self._exits: set[str] = set()`
  出口集合（用 set，但 static_check 要求恰好 1 个）。

#### Graph.node（第 565-571 行）

- **第 565-566 行** `def node(self, id: str, program: ProgramFn | Arch, *, inputs: tuple[str, ...] = (), outputs: tuple[str, ...] = ()) -> Graph:`
  添加节点：id、program（ProgramFn 或 Arch），可选 inputs/outputs 声明。返回 `Graph` 支持链式调用。

- **第 567 行** 注释：`保存 compile 工厂本身（不是一次性 generator），保证二次 compile/resume 幂等（D-8）。`
  **D-8 纪律**：存工厂而非 generator 实例，这样每次 compile 都能重新生成，幂等。

- **第 568 行** `self._nodes[id] = program.compile if isinstance(program, Arch) else program`
  鸭子类型：Arch 取 `compile` 工厂，否则存原 ProgramFn。**存的是函数本身**。

- **第 569 行** `self._inputs[id] = tuple(inputs)` 记录输入声明（转 tuple）。

- **第 570 行** `self._outputs[id] = tuple(outputs)` 记录输出声明。

- **第 571 行** `return self` 返回自身，支持 `.node(...).edge(...).exit(...)` 链式。

#### Graph.edge（第 573-575 行）

- **第 573 行** `def edge(self, src: str, dst: str, *, cond: Callable[[AgentState], str | None] | None = None) -> Graph:`
  添加边：src → dst，可选条件函数。

- **第 574 行** `self._edges.append(_Edge(src=src, dst=dst, cond=cond))` 构造 `_Edge` 并加入列表。

- **第 575 行** `return self` 链式返回。

#### Graph.exit（第 577-578 行）

- **第 577 行** `def exit(self, id: str) -> Graph:` 声明出口节点。

- **第 578 行** `self._exits.add(id); return self` 加入出口集合并链式返回。

#### Graph.static_check（第 580-608 行，决策 8 核心）

这是全模块最完整的静态检查：查环、可达性、悬空边、多出口、cond 目标重复。

- **第 581 行** `errs: list[StaticCheckIssue] = []` 收集器（显式类型）。

- **第 582 行** `known = set(self._nodes) | self._exits`
  合法 id 集合 = 节点 id ∪ 出口 id。边端点必须在这个集合里。
  - 出口也要算「已知」——因为边可以指向 exit（图要能走到出口）。

- **第 583-585 行** E04（出口数量检查）：
  - **第 583 行** 注释：`E04：恰好一个 exit（0 或多个都拒绝）。`
  - **第 584 行** `if len(self._exits) != 1:`
  - **第 585 行** `errs.append(StaticCheckIssue("R12-E04", f"exit 数量为 {len(self._exits)}（须恰好 1 个）", "exit"))`
    出口必须恰好 1 个。0 个（无出口）或多个（多出口歧义）都拒绝。

- **第 586-589 行** E01（悬空边检查）：
  - **第 586 行** 注释：`E01：悬空边。`
  - **第 587 行** `for e in self._edges:` 遍历每条边。
  - **第 588 行** `if e.src not in known or e.dst not in known:`
  - **第 589 行** `errs.append(StaticCheckIssue("R12-E01", f"悬空边 {e.src}->{e.dst}", e.src))`
    边的src 或 dst 不在已知集合 → 悬空边。不拦的话运行时会跳到不存在的节点。

- **第 590-597 行** E06（cond 目标重复检查）：
  - **第 590 行** 注释：`E06：同一 src 的多条 cond 边目标重复。`
  - **第 591-594 行** `by_src: dict[str, list[str]] = {}` + `for e in self._edges:` + `if e.cond is not None: by_src.setdefault(e.src, []).append(e.dst)`
    按 src 分组收集所有**条件边**的目标。
  - **第 595-597 行** `for src, targets in by_src.items():` + `if len(set(targets)) != len(targets):` + `errs.append(StaticCheckIssue("R12-E06", f"cond 目标重复于 {src}", src))`
    `set` 去重后长度变短 → 有重复目标。重复会导致条件路由歧义（同一状态匹配多个相同目标）。

- **第 598-600 行** E02（环检查）：
  - **第 598 行** 注释：`E02：环（DFS 检测，回边即报）。`
  - **第 599 行** `if self._has_cycle():`
  - **第 600 行** `errs.append(StaticCheckIssue("R12-E02", "图中存在环", "graph"))`
    调用 `_has_cycle`（见第 679-694 行）。有环会导致执行无限跳转。

- **第 601-607 行** E03（不可达节点检查）：
  - **第 601 行** 注释：`E03：从 entry 不可达的节点。`
  - **第 602 行** `entry = self._entry()` 取入口节点。
  - **第 603 行** `if entry is not None:` 有入口才检查。
  - **第 604 行** `reachable = self._reachable(entry)` 算可达集合（见第 666-677 行）。
  - **第 605-607 行** `for nid in self._nodes:` + `if nid not in reachable:` + `errs.append(StaticCheckIssue("R12-E03", f"不可达节点 {nid}", nid))`
    每个节点都必须在可达集合中，否则是死代码（永远跑不到）。

- **第 608 行** `return _report(self.name, tuple(errs), ("llm",))`
  返回报告：端口 `("llm",)`（图节点默认用 LLM，虽然具体端口由节点决定）。

#### Graph.compile（第 610-635 行）

- **第 610 行** `def compile(self) -> Program:` 编译方法。

- **第 611 行** `def program() -> Program:` 内部 generator。

- **第 612 行** `entry = self._entry()` 取入口节点。

- **第 613 行** `exit_id = next(iter(self._exits)) if self._exits else None`
  取唯一出口（set 无序，但 static_check 保证恰好 1 个，所以 `next(iter(...))` 确定）。

- **第 614-616 行** `if entry is None or exit_id is None: yield _end(None, "graph_invalid"); return`
  无入口或无出口 → 终止，原因 `graph_invalid`。

- **第 617 行** `cur: str | None = entry` 当前节点从入口开始。

- **第 618 行** `visited: set[str] = set()` 已访问集合（防环，双保险）。

- **第 619 行** `state_out: DynamicValue = None` 记录节点输出（作为下一节点的状态）。

- **第 620 行** `for _ in range(max(1, len(self._nodes) + len(self._edges))):  # 防御上限（static_check 已拦截环）`
  防御上限 = 节点数 + 边数（至少 1）。static_check 已拦环，这里只是兜底。

- **第 621-623 行** `if cur is None or cur in visited or cur not in self._nodes:` + `yield _end(None, "cycle" if cur in visited else "graph_invalid")` + `return`
  **执行期防环**：当前节点为空、已访问（环）、或不存在 → 终止。原因区分 `cycle`（访问过）和 `graph_invalid`（不存在）。

- **第 624 行** `visited.add(cur)` 标记已访问。

- **第 625-626 行** 注释 + `state_out = yield from _run_child(self._nodes[cur]())`
  - 第 625 行注释：`执行当前节点；exit 节点也执行一次（单节点图 entry==exit 时保证节点被跑）。`
  - **第 626 行** `self._nodes[cur]()` 调用工厂得到 generator（D-8 幂等），`_run_child` 驱动，输出存 `state_out`。

- **第 627-628 行** `if cur == exit_id: break`
  到达出口节点 → 跳出循环（图执行完成）。

- **第 629 行** `nxt = self._next(cur, _dict(state_out))`
  调 `_next` 按状态选下一节点（见第 649-664 行）。`_dict` 归一节点输出为 state 映射。

- **第 630-631 行** `if nxt is None or nxt not in self._nodes: break`
  没有下一节点或下一节点不存在 → 跳出。

- **第 632 行** `cur = nxt` 前进到下一节点。

- **第 633 行** `yield _end(state_out)` 终止，输出最后节点的输出。

- **第 634 行** `return None` generator 结束。

- **第 635 行** `return program()` 返回 generator。

#### Graph 内部纯函数（第 637-694 行，不进公共 API）

- **第 637 行** `# ---- 内部纯函数（不进公共 API） ----` 分隔注释。

##### _entry（第 638-640 行）

- **第 638 行** `def _entry(self) -> str | None:` 取入口节点。

- **第 639 行** docstring：`入口 = 第一个声明的 node（保持向后兼容）；无边时即该节点。`

- **第 640 行** `return next(iter(self._nodes), None)`
  返回第一个节点 id（dict 保序），无节点返回 None。直接用声明顺序当入口——简单、向后兼容。

##### _adjacency（第 642-647 行）

- **第 642 行** `def _adjacency(self) -> dict[str, list[str]]:` 构造邻接表。

- **第 643 行** docstring：`src → 目标列表（cond 边取其 dst；_next 另行按 state 选）。`
  - 条件边在这里也只取 dst 加入邻接表（用于可达性/环检测）；实际路由选择由 `_next` 按 state 做。

- **第 644 行** `adj: dict[str, list[str]] = {}` 邻接表容器。

- **第 645-646 行** `for e in self._edges:` + `adj.setdefault(e.src, []).append(e.dst)`
  逐边填入邻接表。

- **第 647 行** `return adj` 返回邻接表。

##### _next（第 649-664 行）

- **第 649 行** `def _next(self, cur: str, state: Mapping[str, DynamicValue]) -> str | None:`
  按状态选下一节点。

- **第 650 行** docstring：`返回首个非 None 的条件目标；否则走无条件 dst。`
  选择优先级：条件边 > 无条件边。

- **第 651 行** `outgoing = [e for e in self._edges if e.src == cur]` 取当前节点的所有出边。

- **第 652-660 行** 第一轮：条件边：
  - **第 652 行** `for e in outgoing:` 遍历出边。
  - **第 653 行** `if e.cond is not None:` 只看条件边。
  - **第 654 行** 注释：`cond 签名收 AgentState；此处回注是 dict，构造只读 State 视图供其判定。`
  - **第 655-658 行** `try:` + `target = e.cond(AgentState(scratch=dict(state)))` + `except Exception:  # cond 异常按无匹配处理，走无条件边` + `target = None`
    **关键防御**：把回注 dict 包装成 `AgentState(scratch=...)` 供 cond 使用（因为 cond 签名要 AgentState）。
    - `try/except Exception`：cond 出错不崩图，按「无匹配」处理，继续走无条件边。
  - **第 659-660 行** `if target is not None: return target` 首个非 None 的条件目标立即返回（短路）。

- **第 661-663 行** 第二轮：无条件边：
  - **第 661 行** `for e in outgoing:` 再遍历一遍。
  - **第 662 行** `if e.cond is None:` 只看无条件边。
  - **第 663 行** `return e.dst` 返回第一条无条件边的目标。

- **第 664 行** `return None` 没有任何出边 → None。

##### _reachable（第 666-677 行）

- **第 666 行** `def _reachable(self, start: str) -> set[str]:` 从 start 出发的可达节点集合。

- **第 667 行** docstring：`从 start 出发按邻接表可达的节点集合。`

- **第 668 行** `adj = self._adjacency()` 取邻接表。

- **第 669 行** `seen: set[str] = set()` 已见集合。

- **第 670 行** `stack = [start]` 栈（DFS 用），从 start 开始。

- **第 671-676 行** 迭代式 DFS：
  - **第 671 行** `while stack:` 栈非空则继续。
  - **第 672 行** `node = stack.pop()` 弹出栈顶。
  - **第 673-674 行** `if node in seen: continue` 已见则跳过（防重复入栈）。
  - **第 675 行** `seen.add(node)` 标记已见。
  - **第 676 行** `stack.extend(adj.get(node, ()))` 把邻居入栈。
  - 用栈而非递归：避免深图递归爆栈。

- **第 677 行** `return seen` 返回可达集合。

##### _has_cycle（第 679-694 行，DFS 三色标记）

- **第 679 行** `def _has_cycle(self) -> bool:` 检测有向环。

- **第 680 行** docstring：`DFS 三色标记检测有向环。`

- **第 681 行** `adj = self._adjacency()` 邻接表。

- **第 682 行** `color: dict[str, int] = {}  # 0=未访问 1=在栈 2=完成`
  三色标记：
  - 0（未访问 / 默认 `get` 返回 0）：还没探索。
  - 1（在栈 / 灰色）：正在递归栈中。
  - 2（完成 / 黑色）：已探索完。
  - **核心原理**：DFS 中遇到颜色为 1 的节点 = 遇到回边 = 有环。

- **第 684-692 行** 内部递归函数 `visit`：
  - **第 684 行** `def visit(node: str) -> bool:` 返回「从 node 出发是否发现环」。
  - **第 685 行** `color[node] = 1` 标记在栈（灰）。
  - **第 686 行** `for nxt in adj.get(node, ()):` 遍历邻居。
  - **第 687-688 行** `if color.get(nxt, 0) == 1: return True`
    **回边检测**：邻居颜色为 1（在栈中）→ 有环，立即返回 True。
  - **第 689-690 行** `if color.get(nxt, 0) == 0 and visit(nxt): return True`
    邻居未访问 → 递归探索；递归发现环 → 向上传播 True。
  - **第 691 行** `color[node] = 2` 探索完成，标记黑。
  - **第 692 行** `return False` 从 node 出发无环。

- **第 694 行** `return any(color.get(node, 0) == 0 and visit(node) for node in self._nodes)`
  **最终判定**：对每个未访问节点跑 DFS，任何一个发现环就返回 True（`any` 短路）。
  - 为什么遍历所有节点而非只从 entry 开始：图可能不连通，环可能存在于与入口不连通的子图里——虽然 E03 会报不可达，但 E02 仍要独立查出环。

---

## 3. __init__.py 逐行讲解（23 行）

包级 API 收敛层：把 base 的契约类型与 recipes 的 10 个架构统一重导出，外部只需 `from archs import Xxx`，不必知道内部文件路径。

### 第 1-4 行：模块 docstring

- **第 1 行** `"""R12 内置架构：10 种 A 类算子预设配方（规格 P6）。`
  一句话定位：R12 的 10 种架构配方。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `对外 API 不变（R12 §4 构造签名）；每个架构 compile() -> Program，R1 只认 Program。`
  两条纪律：① 构造签名对外不变（R12 §4）；② 架构 compile 产出 Program，R1 只认 Program。

- **第 4 行** `"""` docstring 结束。

### 第 5 行：imports 起点

- **第 5 行** `from __future__ import annotations` 延迟求值注解。

### 第 7-11 行：从 .base 导入契约类型

- **第 7-11 行** `from .base import (Arch, ChildSpec, Executor, IntentResult, IntentStrategy, JoinStrategy, MajorityVote, NamedStrategy, Planner, RePlanner, ReflectStrategy, SkillSpec, StaticCheckIssue, StaticCheckReport)`
  从 base 导入全部 14 个公共符号：协议（Arch/JoinStrategy/NamedStrategy/Planner/Executor/RePlanner/ReflectStrategy/IntentStrategy/ChildSpec/SkillSpec）、数据类（MajorityVote/IntentResult/StaticCheckIssue/StaticCheckReport）。
  - 注意这里比 base.py 的 `__all__` 多了 `NamedStrategy`（base 的 `__all__` 也含它，第 16 行——一致）。

### 第 12-15 行：从 .recipes 导入架构

- **第 12-15 行** `from .recipes import (Blackboard, Graph, Handoff, PlanExecute, ReAct, Reflection, Router, RouterSkill, Supervisor, ToolUse)`
  从 recipes 导入全部 10 个内置架构类。

### 第 17-23 行：__all__

- **第 17-23 行** `__all__ = [...]`
  声明包级公共导出面，共 24 个符号，分三组：
  - 报告与合并类：`Arch`、`JoinStrategy`、`MajorityVote`、`StaticCheckIssue`、`StaticCheckReport`。
  - 策略契约：`NamedStrategy`、`Planner`、`Executor`、`RePlanner`、`ReflectStrategy`、`IntentStrategy`、`IntentResult`、`ChildSpec`、`SkillSpec`。
  - 10 个架构：`ToolUse`、`ReAct`、`PlanExecute`、`Reflection`、`Router`、`RouterSkill`、`Supervisor`、`Handoff`、`Blackboard`、`Graph`。
  - 这是 archs 包的「门面」：上层装配代码（compose/runtime 侧）只通过这里拿架构与契约，完全不接触 base.py / recipes.py 的内部路径。换内部实现不影响外部调用。

---

## 4. 逐行讲解后的整体心智图

```
┌──────────────────────────── archs 包 ────────────────────────────┐
│                                                                   │
│  __init__.py（门面）                                               │
│    └─ 统一导出 24 符号：契约类型 + 10 架构                          │
│                                                                   │
│  ┌─ base.py（协议层，不含实现）─────────────────────────────────┐  │
│  │  Arch 协议：name/version + static_check() + compile()         │  │
│  │  StaticCheckReport：errors/arch/version/ports/budgets + ok    │  │
│  │  StaticCheckIssue：code(R12-E01~E17)/message/node             │  │
│  │  策略契约（具名即履约，判 E15）：                              │  │
│  │    NamedStrategy → Planner/Executor/RePlanner/                │  │
│  │                    ReflectStrategy/IntentStrategy(async)/     │  │
│  │                    ChildSpec/SkillSpec                        │  │
│  │  JoinStrategy + MajorityVote（平票取首个，确定性）             │  │
│  │  IntentResult 信封：label/confidence/gaps/trace               │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                            ▲ import                                │
│  ┌─ recipes.py（实现层）────┴────────────────────────────────────┐  │
│  │  helpers：                                                     │  │
│  │    _X（x-arch.* 扩展键）  _dict/_get（回注归一）                │  │
│  │    _invoke/_batch（Effect 构造）  _tool_calls  _end             │  │
│  │    _run_child（拦截子 Terminate 提取 output，不结束整个 run）  │  │
│  │    _Edge（Graph 有向边）                                       │  │
│  │                                                                │  │
│  │  10 架构（每个 = static_check + compile）：                    │  │
│  │    ① ToolUse      一次性工具调用（batch/逐调用）               │  │
│  │    ② ReAct        LLM↔工具自循环（messages 累积历史）          │  │
│  │    ③ PlanExecute  plan→execute→[replan] 状态机                │  │
│  │    ④ Reflection   draft→reflect→revise 反思循环               │  │
│  │    ⑤ Router       branch：confidence + label → arm            │  │
│  │    ⑥ RouterSkill  Router 变体：SkillSpec 先加载 body           │  │
│  │    ⑦ Supervisor   par：Spawn 子 Agent + join 合并              │  │
│  │    ⑧ Handoff      seq：角色接力（x-arch.handoff.role 跳转）    │  │
│  │    ⑨ Blackboard   race：多 Agent 写共享板，首个胜出            │  │
│  │    ⑩ Graph        声明式图：static_check 查环/可达/悬空/多出口  │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
                              │ compile() 产出
                              ▼
                         Program（generator）
                              │ R1 解释循环执行（换架构上层零改动）
                              ▼
              yield Effect → 解释器执行 → send 回填 Result.data
                              │
                              └─ program 从回注值读决策（拿不到 fold 后 State）
```

### 一次 compile→执行→检查的完整链路

```
装配期：Arch.static_check() ──> StaticCheckReport
                                   ├─ ok=True  → 放行
                                   └─ ok=False → fail fast（E01~E17）
运行期：Arch.compile() ──> Program generator
        R1 驱动 generator：
          yield Invoke(port,payload) ──> 端口执行 ──> send(Result.data) 回填
          yield list[Effect]         ──> 并行展开（_batch / Spawn）
          yield from _run_child(...) ──> 子流程 Effect 转发 + Terminate 拦截
          yield Terminate(output)    ──> 整个 run 结束
```

### 10 个架构的静态检查错误码速查

| 错误码 | 含义 | 触发架构 |
|---|---|---|
| E01 | 悬空边（src/dst 不在已知集合） | Graph |
| E02 | 图中存在环（DFS 回边） | Graph |
| E03 | 不可达节点 | Graph |
| E04 | exit 数量不是恰好 1 | Graph |
| E06 | 同一 src 的 cond 目标重复 | Graph |
| E07 | 集合为空（tools/arms/roles/agents） | ToolUse/ReAct/Router/RouterSkill/Handoff/Blackboard |
| E08 | default 不在 arms | Router |
| E09 | ToolSpec 缺 name | ToolUse |
| E15 | 策略未履约：缺 name | PlanExecute/Reflection/RouterSkill/Supervisor |
| E17 | 预算上限 <= 0（turns/rounds/hops/max_children） | ReAct/Reflection/Supervisor/Handoff/Blackboard |

（E05/E10~E14/E16 在本模块 3 个文件中未出现，属于规格保留码位。）

---

## 5. 细节速记卡（逐行精华）

### 决策类（全局贯通）

- **架构 = 工厂**：`compile()` 返回 generator（Program），R1 只认 Program；换架构上层零改动（base.py 第 3、165 行；__init__.py 第 3 行）。
- **static_check 装配期报告**：E01~E17 错误码，问题进运行期前 fail fast（base.py 第 4 行）。
- **Program 拿不到 fold 后的 State**：所有决策从 `send` 回注值读（recipes.py 第 9-10 行）。
- **x-arch.* 扩展键传进度**：`_X.of(arch, key)` 生成 `x-arch.<arch>.<key>`（第 57-64 行）；用在 Handoff 角色（第 495 行）、Blackboard 轮次（第 536-537 行）。
- **_run_child 拦截 Terminate**：转成 output 返回，不提前结束整个 run（第 107-125 行）。
- **_batch 的 calls 由解释器展开**：`batch=True` 是关键标记（第 89-93 行）。
- **预算双保险**：构造参数是防御性上限，R1 Budgets 是真实约束（第 13-15 行）。
- **Graph 静态检查**：E01 悬空边 / E02 环（三色 DFS）/ E03 不可达 / E04 多出口 / E06 cond 重复（第 580-608、679-694 行）。
- **MajorityVote 平票取首个**：`(counts[k], -index(k))` 排序键，确定性（base.py 第 40 行）。
- **IntentStrategy 异步**：`asyncio.run(cls.recognize(...))` 同步取结果（第 360-361 行）。

### 实现技巧类

- **回注值先归一**：`_dict(value)` 非映射返回 `{}`，`_get` 把 None 当缺失（第 67-78 行）。
- **向历史 append 前拷贝**：`payload["messages"] = list(messages)`，避免共享引用副作用（第 206、216 行）。
- **`is False` 判失败**：避免 `0`/`""` 被误判为失败（第 252 行）。
- **`or` 兜底空串**：`_get(...) or ""` 防止把草稿/内容清空（第 295、311 行）。
- **`or` 注入默认策略**：`join or MajorityVote()`（第 418 行）。
- **`setdefault` 冲突仲裁**：黑板写入首个胜出（第 535 行）。
- **`dict` 保序即声明顺序**：Handoff 接力顺序、Graph entry 都靠它（第 481、640 行）。
- **存工厂不存 generator**：Graph.node 存 `compile` 函数，保证二次 compile 幂等 D-8（第 567-568 行）。
- **`isinstance` 鸭子分发**：Arch 取 `.compile`，否则当 ProgramFn（第 394、398、486、545、568 行；另有 345 行用 `hasattr` 判 compile）。
- **`__all__` 门面收敛**：包外只用 `from archs import X`，不碰内部路径（__init__.py 第 17-23 行）。

### 纠错备忘（易踩坑）

- `static_check` 不声明 tool 端口的架构：PlanExecute（端口由 `step["port"]` 动态指定，第 235/248 行）。
- `Router.static_check` 用 `min_confidence`，但 `RouterSkill.compile` 不用（第 339 行 vs 第 385 行）。
- `IntentStrategy` 只传空文本 `""` 和 `None` ctx（第 361 行），是装配期占位调用。
- `_run_child` 的 `except StopIteration` 返回 None（第 124-125 行），防御没 yield Terminate 的 Program。
- `cond` 异常按无匹配处理（第 657-658 行），图不崩。
- 错误码 E05/E10~E14/E16 本模块未用（规格保留位）。

---

*本册是内核层三册的第三册（R12 内置架构）。逐行讲解版统一格式：「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」。*

