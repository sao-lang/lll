# 代码精读 · runtime 内核模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/runtime/` 全部 6 个文件：types.py（955 行）+ fold.py（223 行）+ interpreter.py（166 行）+ runner.py（718 行）+ errors.py（46 行）+ __init__.py（33 行），合计 2141 行源码。
> 这是「代码精读」系列的内核三册之一（runtime / compose / harness 能力层）。总入口见 [00](./00-总入口与全局链路.md)。

---

## 0. 读前GPS

### 模块定位

runtime 是整个框架的**内核**：它不认识任何具体业务——不 import pydantic、不 import 任何 IO/DB/HTTP 实现、不 import hooks/aop。它只提供一套「状态 + 效果 + 事件」的确定性状态机。

一句话记住主链路：

```
Program(generator) --yield--> Effect --Interpreter 展开--> batch --Runner 调用--> Result
        ^                                                                       |
        |------------------------- send(结果) <----------------------------------+
                                       |
                              fold 纯函数回填 State
                                       |
                           Event append + checkpoint
```

六个文件的分工：

| 文件 | 行数 | 角色 |
|---|---|---|
| types.py | 955 | 全框架唯一类型来源：所有 DTO、Effect 四成员、序列化/快照/恢复 |
| fold.py | 223 | 唯一状态变更入口，纯函数，只增不减 |
| interpreter.py | 166 | generator 解释器，只做确定性推进 + batch 展开，零 IO |
| runner.py | 718 | 唯一的 async IO 编排点，预算/并行/取消/子会话/事件/通知全在这里 |
| errors.py | 46 | 框架异常分类 + 稳定错误码 |
| __init__.py | 33 | 包级 API 收敛 |

### 核心类速览

- `AgentState`：一次会话的全部进度，frozen，任何修改产生新对象。
- `Effect = Invoke | Spawn | Suspend | Terminate`：tagged union 四成员，只有 Invoke 走 AOP 链和 Port。
- `Result`：Port 调用统一信封，业务失败也算 `ok=True`，只有 Port 异常 `ok=False`。
- `Budgets`：6 维预算（turns/hops/rounds/cost_usd/tokens/parallel_children），内核硬强制。
- `Event`：事件溯源最小单元，append-only，`ts=step` 逻辑时钟，无墙钟。
- `InterpreterImpl`：把 generator 一次推进翻译成下一批 Effect 或 DONE。
- `Runner`：唯一 IO 编排点；call / store / ctx_factory / notify 全部构造注入。
- `Session`：`program_factory` + state + event_log 聚合根；用工厂而非 generator，因为 generator 不可序列化。

### 关键设计决策（先记住，后面逐行验证）

1. **内核零领域策略**：types.py 不 import 任何实现；hooks/aop 由装配层包进 call 槽，内核只认 Callable。
2. **Effect 四成员 tagged union**：Invoke（唯一走 Port）/ Spawn（递归子 Agent）/ Suspend（挂起等人工）/ Terminate（结束产出）。
3. **唯一 IO 点在 Runner**：只有 runner.py 有 `async` + `await`；interpreter/fold 是纯函数。
4. **Interpreter 纯推进 + batch 展开传幂等键**：一个 batch Invoke 展开成多个独立 Invoke，trace_id 与 idempotency_key 原样透传。
5. **fold 纯函数只增不减**：messages 从旧副本起步追加，绝不删改；所有返回新对象。
6. **取消用可变 holder**：frozen `CancellationToken` 无法在运行中被感知（`cancel()` 返回新对象，Runner 拿不到），故 Runner 内部用 `_CancellationHolder` 原地置位。
7. **失败回填 Result 信封**：Program 收到的是 Result 对象本身（而非 `data=None`），能读到错误码。
8. **预算硬检查无法绕过**：`exceeded()` 在 step 最前面检查；超限强制 Terminate，Program 没有旁路。
9. **Terminate/Suspend 必须整批**：混合批含可执行 Effect 时不能被吞，只有整批都是才走快路径。
10. **trace_id 格式**：`<session>:<step:04d>:<index:02d>`，由 Runner 按 step/index 注入。
11. **逻辑时钟 ts=step**：Event.ts 取 step_no，无墙钟，保证确定性回放。
12. **checkpoint 终态总落**：运行中按 `checkpoint_every` 节流（最小 1），终态无条件落。
13. **终态幂等**：run() 遇到 done/error/suspended 直接返回，不重复执行。
14. **子会话 id 格式**：`<parent>:child:<seq>`，seq 按父 session 独立递增；同批并发 Spawn 拿到唯一 id。
15. **resume 回放**：重建 generator，按事件日志重放到断点，answer 作为末 suspended 事件的回填值。
16. **13 个通知相位**：`_Phase` 枚举具名对齐 `hooks.LifecyclePhase`，内核不 import hooks。
17. **事件溯源**：Event append-only；`replay_events` / `Runner.replay` 从初始 State 逐步 fold 得终态。
18. **稳定原因码**：`_REASON_*` 常量值一旦发布不可更改，进 `Event.reason` / `RunResult.reason`。
19. **x-* 扩展键不进内核结构**：Effect.payload 的 x-* 键由消费模块声明，内核只在 payload 里透传，不提升为字段/枚举。

---

## 1. types.py 逐行讲解（955 行）

types.py 是全框架**唯一自包含的类型来源**。955 行看着吓人，其实结构是「docstring → imports → __all__ → 动态别名 → 一组 DTO → 序列化/快照/恢复函数」。逐块啃。

### 第 1-8 行：模块 docstring

- **第 1 行** `"""R1 核心运行时：全框架唯一自包含的类型来源。`
  一句话定位：这里是类型的「唯一真相源」。强调「自包含」——不依赖任何其他模块实现。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `契约纪律（见 docs/02-模块详设/Agent/R1-核心运行时.md §4）：`
  指路：完整契约在详设文档 §4。下面四条是纪律摘要。

- **第 4 行** `- 本模块不 import 任何具体 IO / pydantic / 其他模块实现。`
  **内核零依赖纪律**。不这么写（比如 import 了 pydantic 或某个 DB 客户端），内核就被绑死具体实现，无法独立闭环、无法替换后端。

- **第 5 行** `- 上层只 import 本文件；跨模块信封（Result/Usage/PortError）定义在此。`
  依赖方向恒定：装配层/harness → 内核，不反向。信封放这里，跨模块传递不用各自定义一套。

- **第 6 行** `- 所有状态变更产生新对象（frozen dataclass + MappingProxyType）。`
  不可变纪律。frozen 防字段改写，MappingProxyType 防 dict 就地改。不这么做，并发/回放时状态会被悄悄污染。

- **第 7 行** `- 墙钟不进运行时：Event.ts 用逻辑时钟（=step），真实时间由外壳注入。`
  **逻辑时钟纪律**。墙钟（datetime.now）是非确定性的，同样的输入两次运行 ts 不同，事件溯源无法逐字节比对。

- **第 8 行** `"""` docstring 结束。

### 第 9-19 行：imports（延迟注解 + 标准库）

- **第 9 行** `from __future__ import annotations`
  延迟求值注解，允许注解里引用尚未定义的类型（如 `AgentState` 在 `Session` 之前用），降低 import 顺序耦合。

- **第 11 行** `import dataclasses`
  用 `dataclasses.fields` / `is_dataclass` 在 `_plain` 里递归规范化 dataclass（见第 776 行）。

- **第 12 行** `import hashlib`
  `fingerprint()` 用 SHA256 给事件日志算指纹（见第 809 行）。

- **第 13 行** `import json`
  `canonical()` 用 `json.dumps` 做确定性序列化（见第 804 行）。

- **第 14 行** `import types`
  `_plain` 里判断 `types.SimpleNamespace`（见第 773 行）。注意这里 import 的是标准库 types，与本模块同名但不同物。

- **第 15 行** `from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence`
  - `Awaitable`/`Callable`：回调签名注解（CallFn/DispatchFn/EventStore）。
  - `Generator`：Program 类型别名。
  - `Mapping`：只读视图类型（Payload/Scratchpad）。
  - `Sequence`：`canonical` 接收事件序列。

- **第 16 行** `from dataclasses import dataclass, field, replace`
  - `dataclass`：所有 DTO 的装饰器。
  - `field`：`default_factory` 与 frozen 下的可变默认值。
  - `replace`：不可变更新的核心工具（Budgets.consume / AgentState.with_ / Invoke.with_payload）。

- **第 17 行** `from enum import Enum`
  定义 `Disposition` / `PortName` 枚举。

- **第 18 行** `from types import MappingProxyType`
  冻结映射为只读视图，实现不可变 scratch/payload。

- **第 19 行** `from typing import Any, Protocol, TypedDict, cast`
  - `Any`：`DynamicValue` 别名的基础。
  - `Protocol`：`EventStore` 可插拔协议。
  - `TypedDict`：快照/负载的字段声明。
  - `cast`：动态边界处收敛类型（不运行时校验，只给类型检查器看）。

### 第 21-58 行：__all__ 导出清单

- **第 21-58 行** `__all__ = [...]`
  列出 36 个公共符号，分三组：
  - **DTO 类**：`AgentState`/`Message`/`Result`/`Usage`/`PortError`/`Budgets`/`Event`/`Session`/`StepReport`/`RunResult`/`RuntimeCtx`/`EventStore` 等。
  - **Effect 四成员 + 判别**：`Invoke`/`Spawn`/`Suspend`/`Terminate`/`Effect`/`Disposition`/`disposition_of`。
  - **函数工具**：`canonical`/`fingerprint`/`checkpoint`/`restore_checkpoint`/`DONE` 等。
  - 作用：`__init__.py` 据此重导出；外部 `from runtime import Result` 不必知道内部文件。注意 `revive`/`restore_state`/`_effect_from_plain`/`_plain` 等内部函数**不在**导出面——它们是恢复链路的内部零件。

### 第 60-68 行：动态边界别名（把不可避免的 Any 收敛为具名名）

- **第 60-62 行** 注释分隔符：`# 动态边界：把不可避免的 Any 收敛为具名别名`。
  设计意图：动态值不可消灭，但给它起名字，让类型语义可读。

- **第 63 行** `type DynamicValue = Any`
  **单个动态值别名**。Result.data / scratch 条目 / 待规范化对象都用它。形态由 port 决定，内核不解释。

- **第 64 行** docstring：说明 DynamicValue 的语义边界。

- **第 65 行** `type Payload = Mapping[str, DynamicValue]`
  **Effect 参数负载的只读视图**。用 Mapping（只读）而非 dict，防 payload 被就地改。

- **第 66 行** docstring。

- **第 67 行** `type Scratchpad = Mapping[str, DynamicValue]`
  **AgentState.scratch 只读视图**。与 Payload 结构完全相同但语义不同（一个是 Effect 入参，一个是状态命名空间），所以独立命名——不复用别名，避免语义混淆。

- **第 68 行** docstring 点明「结构同但语义不同，故独立命名」。

### 第 71-85 行：JsonSnapshot TypedDict

- **第 71 行** `class JsonSnapshot(TypedDict):`
  checkpoint 顶层快照类型，五个固定键，可 JSON 持久化。

- **第 72-80 行** docstring：逐字段说明。强调五个固定键。

- **第 81 行** `session_id: str`：会话标识。

- **第 82 行** `version: str`：框架/状态版本，用于迁移（旧快照升级时有据可依）。

- **第 83 行** `state: StateSnapshot`：已规范化的 AgentState 快照。

- **第 84 行** `events: list[DynamicValue]`：已规范化的事件列表（每个事件是 dict）。

- **第 85 行** `fingerprint: str`：事件日志的 SHA256，防篡改/漂移。恢复时可比对指纹确认日志未被改。

### 第 88-112 行：StateSnapshot TypedDict

- **第 88 行** `class StateSnapshot(TypedDict):`
  AgentState 的规范化快照，**键即 AgentState 字段名**——这是 restore_state 能一一回填的前提。

- **第 89-101 行** docstring：列出各字段语义。

- **第 102 行** `messages: list[DynamicValue]`：规范化后的消息列表（dict 形态）。

- **第 103 行** `scratch: dict[str, DynamicValue]`：用户自定义键值命名空间。

- **第 104 行** `budgets: dict[str, DynamicValue]`：规范化后的预算字典（12 个字段）。

- **第 105 行** `version: str`：状态版本。

- **第 106 行** `output: DynamicValue`：最终输出。

- **第 107 行** `last_skill: DynamicValue`：最近一次 SKILL Effect 的结果。

- **第 108 行** `last_child: DynamicValue`：最近一次 Spawn（子 Agent）的结果。

- **第 109 行** `last_wait_human: DynamicValue`：最近一次 Suspend 的结果。

- **第 110 行** `last_step_kinds: list[str]`：最近一步的 Effect kind 序列。

- **第 111 行** `last_tokens: int`：最近一步累计 token。

- **第 112 行** `last_reason: str`：最近一次结束/挂起原因。

### 第 115-127 行：Disposition 枚举（tagged union 判别标签）

- **第 115 行** `class Disposition(str, Enum):`
  继承 `str, Enum`：既是枚举（有限取值、可比较），又能当字符串用（进 JSON/快照）。

- **第 116-123 行** docstring：说明这是 tagged union 的判别标签，内核按此 match 分派。四个成员各自说明。

- **第 124 行** `INVOKE = "invoke"`：唯一走 AOP 增强链与 Port 调用的 Effect。

- **第 125 行** `SPAWN = "spawn"`：递归创建子 Agent（内核路径，不进 AOP）。

- **第 126 行** `SUSPEND = "suspend"`：挂起等人工输入（内核快路径，不进 AOP）。

- **第 127 行** `TERMINATE = "terminate"`：结束会话并产出 output（内核快路径，不进 AOP）。

**为什么不只用 isinstance**：checkpoint 落盘后 Effect 变成纯 dict，类型信息丢失；需要一个显式标签（disposition 字段）才能无损重建（见第 767-769 行）。

### 第 130-139 行：PortName 枚举（Invoke.port 取值域）

- **第 130 行** `class PortName(str, Enum):`
  Invoke.port 的**具名取值域**，装配期校验用。

- **第 131-134 行** docstring：关键点——Port 种类**不可运行时新增**；`x-*` 只用于 Effect.payload 扩展键，不作为 Port 种类。这正是「x-* 扩展键不进内核结构」决策的落点。

- **第 135 行** `LLM = "llm"`：大模型 Port。

- **第 136 行** `TOOL = "tool"`：工具 Port。

- **第 137 行** `SKILL = "skill"`：技能 Port。

- **第 138 行** `RAG = "rag"`：检索 Port。

- **第 139 行** `MEMORY = "memory"`：记忆 Port。

**为什么是闭集**：内核按 Port 种类有不同处理（如 tool 发通知、skill 写 last_skill 托管字段）。开放种类会让分派逻辑失效，且 x-* 扩展键已从 payload 层提供了扩展口，无须开放 Port 枚举。

### 第 142-169 行：Message DTO

- **第 142 行** `@dataclass(frozen=True)`
  不可变消息单元。对话历史只增不减，frozen 保证已有消息不被改写。

- **第 143 行** `class Message:`

- **第 144-151 行** docstring：定位「对话历史最小单元；只增不减（裁剪属于视图层，R7）」。裁剪不在这里做——内核只负责追加，上下文窗口管理是上层视图层职责。

- **第 153 行** `role: str`：角色（system/user/assistant/tool）。

- **第 154 行** `content: str = ""`：正文。tool 消息里放规范化结果的 canonical JSON。

- **第 155 行** `name: str | None = None`：可选名称（tool 消息里存 call_id，见 fold.py 第 128 行）。

- **第 156 行** `tool_calls: tuple[DynamicValue, ...] = ()`：助手消息携带的工具调用。用 tuple 不用 list，不可变。

- **第 158 行** `def to_dict(self) -> dict[str, DynamicValue]:`
  转可序列化字典，供 vendor payload 与快照复用。

- **第 159-163 行** docstring：返回 role/content/name/tool_calls 四键。

- **第 164-169 行** 返回字典：
  - **第 168 行** `"tool_calls": [_plain(tc) for tc in self.tool_calls]`：每个 tool_call 经 `_plain` 规范化，确保可 JSON 序列化。

### 第 172-182 行：PortError DTO

- **第 172 行** `@dataclass(frozen=True)`
  结构化错误信封，不可变。

- **第 173 行** `class PortError:`

- **第 174-179 行** docstring：`{code, message, retryable}`；`retryable` 驱动 R3 重试，R1 只透传不解释。

- **第 180 行** `code: str`：稳定错误码（如 `BUDGET_EXCEEDED`）。上层按码决策。

- **第 181 行** `message: str`：人类可读信息。

- **第 182 行** `retryable: bool = False`：默认不可重试。**fail-safe 默认**：没声明就当不可重试，避免误重试副作用。

### 第 185-208 行：Usage DTO + from_mapping

- **第 185 行** `@dataclass(frozen=True)`
  token/成本元数据，不可变。

- **第 186 行** `class Usage:`

- **第 187-192 行** docstring：本地/免费实现填 0 也合法（不强求真实计量）。

- **第 193 行** `prompt_tokens: int = 0`：输入 token。

- **第 194 行** `completion_tokens: int = 0`：输出 token。

- **第 195 行** `cost_usd: float = 0.0`：本步成本（美元）。

- **第 197 行** `@classmethod`

- **第 198 行** `def from_mapping(cls, raw: Mapping[str, DynamicValue]) -> Usage:`
  **从外部形态恢复 Usage 的唯一归一化边界**（规则 6：动态边界收敛到一处）。

- **第 199-203 行** docstring：关键——只挑已知三字段，忽略额外键（如旧快照/外部 port 的 `total_tokens`）。

- **第 204-208 行** 构造：
  - **第 205 行** `prompt_tokens=int(raw.get("prompt_tokens", 0) or 0)`：`or 0` 处理 None/0 两种缺省。
  - **第 206 行** `completion_tokens=int(raw.get("completion_tokens", 0) or 0)`：同上。
  - **第 207 行** `cost_usd=float(raw.get("cost_usd", 0.0) or 0.0)`：同上。
  - **为什么不 `Usage(**raw)`**：raw 可能带未知键（如 `total_tokens`），直接展开会抛 `TypeError`（B5）。显式挑字段才稳健。

### 第 211-243 行：Result DTO（跨模块信封）

- **第 211 行** `@dataclass(frozen=True)`
  Port 调用统一信封，不可变。

- **第 212 行** `class Result:`

- **第 213-220 行** docstring：**最关键的语义**——「业务失败也算 `ok=True`，仅 Port 异常为 `ok=False`」。`ok` 表达的是「Port 是否正常返回」，不是业务成败。

- **第 221 行** `ok: bool`：Port 是否正常返回。

- **第 222 行** `data: DynamicValue = None`：返回数据，形态由 Port 决定。

- **第 223 行** `usage: Usage | None = None`：token/成本元数据。

- **第 224 行** `error: PortError | None = None`：仅 `ok=False` 时存在的结构化错误。

- **第 226 行** `@classmethod`

- **第 227 行** `def success(cls, data: DynamicValue = None, usage: Usage | None = None) -> Result:`
  构造成功信封。

- **第 228-232 行** docstring：参数说明。

- **第 233 行** `return cls(ok=True, data=data, usage=usage)`：固定 ok=True，error 留 None。

- **第 235 行** `@classmethod`

- **第 236 行** `def failure(cls, code: str, message: str, retryable: bool = False) -> Result:`
  构造失败信封，折叠异常为 PortError。

- **第 237-242 行** docstring：参数说明。

- **第 243 行** `return cls(ok=False, error=PortError(code=code, message=message, retryable=retryable))`
  **失败回填信封**：data 留 None，错误装进 `PortError`。Program 收到的是这个 Result 对象本身（见 runner.py 第 170、524-531 行），能读到稳定错误码。

### 第 246-318 行：Budgets DTO（六维硬预算）

- **第 246 行** `@dataclass(frozen=True)`
  多维预算，不可变。每次扣减产生新对象。

- **第 247 行** `class Budgets:`

- **第 248-253 行** docstring：6 维预算，由 R1 强制扣减，**Program 不许自己数数**。每项都有 `max_*` 上限与 `used_*` 已用。

- **第 254 行** `max_turns: int = 30`：最大步数。

- **第 255 行** `max_hops: int = 5`：最大子 Agent 深度。

- **第 256 行** `max_rounds: int = 3`：最大轮次。

- **第 257 行** `max_cost_usd: float = 5.0`：最大成本（美元）。

- **第 258 行** `max_tokens: int = 100_000`：最大 token 数。

- **第 259 行** `max_parallel_children: int = 8`：最大并行子 Agent 数。

- **第 260 行** `used_turns: int = 0`：已用步数。

- **第 261 行** `used_hops: int = 0`：已用子 Agent 深度。

- **第 262 行** `used_rounds: int = 0`：已用轮次。

- **第 263 行** `used_cost_usd: float = 0.0`：已用成本。

- **第 264 行** `used_tokens: int = 0`：已用 token。

- **第 265 行** `used_parallel_children: int = 0`：已用并行子 Agent 数。

- **第 267 行** `def exceeded(self) -> str | None:`
  按固定优先级检查所有硬上限，返回首个超限维度名。

- **第 268-274 行** docstring：说明 checks 元组顺序即优先级，命中即返回，保证多维护约时错误码确定。

- **第 275-282 行** `checks` 元组：六个 `(维度名, 是否超限)` 对，按 turns → hops → rounds → cost_usd → tokens → parallel_children 顺序声明。
  - 用 `>=` 而非 `>`：达到上限即视为超限（硬边界）。

- **第 283 行** `for name, hit in checks:`
  按声明顺序遍历。

- **第 284-285 行** `if hit: return name`：命中首个即返回。不这么写（比如遍历 dict），多维护约时返回哪个维度不确定，错误码漂移。

- **第 286 行** `return None`：全部未超限。

- **第 288 行** `def check_parallel(self, count: int) -> str | None:`
  检查并行数是否超限，**不产生副作用**（区别于 consume）。

- **第 289-295 行** docstring：参数/返回说明。

- **第 296 行** `return "parallel_children" if count > self.max_parallel_children else None`
  这里用 `>`（严格大于）：count 正好等于上限是允许的。与 exceeded 的 `>=` 不同——因为 consumed 后的 parallel_children 是累计量，而 check_parallel 是「本批准备并发数」，语义不同。

- **第 298-301 行** `def consume(self, *, turns=0, hops=0, rounds=0, cost_usd=0.0, tokens=0, parallel_children=0) -> Budgets:`
  按各维度增量生成新 Budgets。全关键字参数（`*` 之后），防位置传参错位。

- **第 302-309 行** docstring：关键——成本 round 到 10 位避免浮点累积误差。

- **第 310-318 行** `return replace(...)`：
  - **第 312-317 行** 各维度 `used_* = used_* + 增量`。
  - **第 315 行** `used_cost_usd=round(self.used_cost_usd + cost_usd, 10)`：**浮点防护**。多次小数累加（如 0.1+0.2）会有 0.30000000000000004 误差，round 到 10 位消除。不这么做，成本软上限的 `>=` 判断会因浮点误差误触发或不触发。

### 第 321-333 行：_freeze 与托管 scratch 键映射

- **第 321 行** `def _freeze(mapping: Scratchpad | None) -> Scratchpad:`
  把映射冻结为只读视图。

- **第 322 行** docstring：已经是 MappingProxyType 时复用，避免重复包装。

- **第 323-324 行** `if isinstance(mapping, MappingProxyType): return mapping`：幂等冻结。重复包装会产生嵌套代理，语义虽不变但多一层开销。

- **第 325 行** `return MappingProxyType(dict(mapping or {}))`
  先 `dict(...)` 复制一份再冻结。不复制的话，外部持有的原 dict 仍可就地改，冻结形同虚设。

- **第 328 行** `_MANAGED_SCRATCH_KEYS: Mapping[str, str] = MappingProxyType({...})`
  旧键 → AgentState 具名字段的映射表，本身也冻结。

- **第 329 行** 注释：用户代码历史上用 scratch 读托管键，这里把旧键映射到具名字段——**向后兼容层**。

- **第 330-332 行** 七个映射项：`output`/`last_skill`/`last_child`/`last_wait_human`/`last_step_kinds`/`last_tokens`/`last_reason`，都映射到同名具名字段。
  - 作用：`AgentState.get("output")` 和 `AgentState.set("output", v)` 能读到/写到真正的字段，而非 scratch 里的同名键。

### 第 336-429 行：AgentState DTO（会话全部进度）

- **第 336 行** `@dataclass(frozen=True)`
  不可变状态。任何修改产生新对象（通过 with_/set）。

- **第 337 行** `class AgentState:`

- **第 338-352 行** docstring：**字段分两层**——框架托管字段（编译期确定类型，由 fold 写入）+ scratch（用户自定义键，动态）。这个分层是理解 get/set/with_ 的关键。

- **第 353 行** `messages: tuple[Message, ...] = ()`：对话历史，只增。用 tuple 不可变。

- **第 354 行** `scratch: Scratchpad = field(default_factory=dict)`：用户自定义只读命名空间。`field(default_factory=dict)` 而不是 `= {}`——避免所有实例共享同一个可变默认值（Python 经典陷阱）。

- **第 355 行** `budgets: Budgets = field(default_factory=Budgets)`：多维预算，默认新实例。

- **第 356 行** `version: str = "0.0.0"`：状态版本。

- **第 357 行** `output: DynamicValue = None`：最终输出（Terminate 写入）。

- **第 358 行** `last_skill: DynamicValue = None`：最近一次 SKILL Effect 的结果。

- **第 359 行** `last_child: DynamicValue = None`：最近一次 Spawn 的结果。

- **第 360 行** `last_wait_human: DynamicValue = None`：最近一次 Suspend 的结果。

- **第 361 行** `last_step_kinds: tuple[str, ...] = ()`：最近一步的 Effect kind 序列。用 tuple 不可变。

- **第 362 行** `last_tokens: int = 0`：最近一步累计 token。

- **第 363 行** `last_reason: str = ""`：最近一次结束/挂起原因。

- **第 365 行** `def __post_init__(self) -> None:`
  构造后钩子：归一三个容器为不可变形态。

- **第 366 行** docstring：frozen 下必须用 `object.__setattr__` 绕过冻结保护。

- **第 367 行** `object.__setattr__(self, "messages", tuple(self.messages))`：messages 归一为 tuple。

- **第 368 行** `object.__setattr__(self, "scratch", _freeze(self.scratch))`：scratch 归一为 MappingProxyType 只读视图。

- **第 369 行** `object.__setattr__(self, "last_step_kinds", tuple(self.last_step_kinds))`：last_step_kinds 归一为 tuple。
  **为什么不直接赋值**：dataclass frozen=True 会禁止 `self.x = ...`，只能走 `object.__setattr__`。这三行让「外部传 list / dict 进来」也能被归一为不可变，杜绝后门。

- **第 371 行** `def get(self, key: str, default: DynamicValue = None) -> DynamicValue:`
  统一读接口。

- **第 372-378 行** docstring：托管键读显式字段，其余读 scratch。

- **第 379 行** `managed = _MANAGED_SCRATCH_KEYS.get(key)`
  查映射表，看 key 是否是旧托管键。

- **第 380-381 行** `if managed is not None: return getattr(self, managed)`
  命中托管键：读具名字段。用 `is not None` 而非真值判断——映射值都是非空字符串，但写法更稳健。

- **第 382 行** `return self.scratch.get(key, default)`：非托管键读 scratch，缺失回退 default。

- **第 384 行** `def set(self, key: str, value: DynamicValue) -> AgentState:`
  写入单键，返回新对象（不可变语义）。

- **第 385-393 行** docstring：托管字段走显式分支；其余键写入 scratch。

- **第 394 行** `if key in _MANAGED_SCRATCH_KEYS:`
  判断是否托管字段。

- **第 395 行** 注释：**编码规则 4**——显式分派到具名字段，不用 `**{key: value}` 动态拼装。

- **第 396-404 行** `return self.with_(...)` 七个字段：
  - **第 397 行** `output=value if key == "output" else self.output`：只更新匹配的字段，其余保持原值。七个三元表达式互斥，恰好一个命中。
  - 为什么不 `with_(**{key: value})`：动态拼装会让类型检查器无法验证字段存在，且可能把旧键名（如 `output`）写进去后不经 remap——这里直接用具名字段名，最稳。

- **第 405 行** `data = dict(self.scratch)`：用户键分支——复制 scratch（不就地改，不可变语义）。

- **第 406 行** `data[key] = value`：写入新 dict。

- **第 407 行** `return self.with_(scratch=data)`：经 with_ 归一为冻结视图后返回新对象。

- **第 409 行** `def with_(self, **changes: DynamicValue) -> AgentState:`
  **唯一状态变更入口**。所有 fold / set 最终都走这里。

- **第 410-418 行** docstring：参数 `**changes` 为要更新的字段；旧托管键名会 remap 到具名字段；scratch/messages/last_step_kinds 在 replace 前先归一。

- **第 419 行** `remapped: dict[str, DynamicValue] = {}`：remap 结果容器。

- **第 420-422 行** 循环：
  - **第 421 行** `managed = _MANAGED_SCRATCH_KEYS.get(key)`：查映射。
  - **第 422 行** `remapped[managed if managed is not None else key] = value`：旧键 remap 成具名键，否则原样。这保证 `with_(output=...)` 和 `with_(last_skill=...)` 即使传旧键名也落到正确字段。

- **第 423-424 行** `if "scratch" in remapped: remapped["scratch"] = _freeze(...)`：scratch 归一为只读。

- **第 425-426 行** `if "messages" in remapped: remapped["messages"] = tuple(...)`：messages 归一为 tuple。

- **第 427-428 行** `if "last_step_kinds" in remapped: remapped["last_step_kinds"] = tuple(...)`：last_step_kinds 归一为 tuple。

- **第 429 行** `return replace(self, **remapped)`：dataclasses.replace 生成新对象——原对象完全不变。

### 第 432-444 行：BudgetDelta TypedDict

- **第 432 行** `class BudgetDelta(TypedDict, total=False):`
  Effect 声明的单步预算消耗。`total=False` 表示所有键可缺省。

- **第 433-438 行** docstring：未提供某维度按 0（turns 默认 1，见 fold.py 第 96 行）。

- **第 439 行** `turns: int`：步数增量。

- **第 440 行** `hops: int`：子 Agent 深度增量。

- **第 441 行** `rounds: int`：轮次增量。

- **第 442 行** `cost_usd: float`：成本增量。

- **第 443 行** `tokens: int`：token 增量。

- **第 444 行** `parallel_children: int`：并行子 Agent 数增量。

### 第 447-479 行：EffectPayload TypedDict（已知键全集）

- **第 447 行** `class EffectPayload(TypedDict, total=False):`
  Effect 参数负载的**已知键全集**。`total=False` 兼容异构构造（不同 Effect kind 只用其中几个键）。

- **第 448-458 行** docstring：按 Effect kind 选用各键。关键点——`x-*` 扩展键不在这里声明，由消费模块在 payload 里动态透传。

- **第 459 行** `messages: list[DynamicValue]`：LLM 请求消息。

- **第 460 行** `tools: list[DynamicValue]`：LLM 工具定义。

- **第 461 行** `model: str`：模型名。

- **第 462 行** `temperature: float`：采样温度。

- **第 463 行** `max_tokens: int`：最大生成 token。

- **第 464 行** `task_hint: str`：LLM 任务类型提示（如 code/chat，供 R4 路由选模型）。

- **第 465 行** `meta: dict[str, DynamicValue]`：扩展元数据透传容器（如 max_cost_per_call；键语义由消费模块声明）——x-* 精神的落点。

- **第 466 行** `call: DynamicValue`：单个工具调用。

- **第 467 行** `calls: list[DynamicValue]`：批量工具调用（解释器展开用，见 interpreter.py 第 81 行）。

- **第 468 行** `call_id: str`：调用关联 id。

- **第 469 行** `budget: BudgetDelta`：本步预算声明。

- **第 470 行** `port: str`：目标 port 名。

- **第 471 行** `output: DynamicValue`：END 的输出（旧式，现由 Terminate.output 替代）。

- **第 472 行** `reason: str`：END 的原因。

- **第 473 行** `name: str`：技能名参数。

- **第 474 行** `part: str`：技能部分参数。

- **第 475 行** `question: str`：HITL 问题参数。

- **第 476 行** `options: list[str]`：HITL 候选项。

- **第 477 行** `program_factory: Callable[[], Program] | None`：子 Agent 的 Program 工厂。

- **第 478 行** `program: Program`：子 Agent 的 Program 本体（旧式，工厂更利于快照）。

- **第 479 行** `state: AgentState`：子 Agent 的初始状态。

### 第 482-484 行：_empty_payload

- **第 482 行** `def _empty_payload() -> EffectPayload:`
  Effect.payload 的 default_factory。

- **第 483 行** docstring：返回空 EffectPayload。

- **第 484 行** `return cast(EffectPayload, {})`
  cast 只是类型提示，运行时就是空 dict。用工厂函数而非 `{}` 默认值，避免多实例共享同一个 dict。

### 第 487-518 行：Invoke DTO（唯一走 Port 的 Effect）

- **第 487 行** `@dataclass(frozen=True)`
  不可变。

- **第 488 行** `class Invoke:`

- **第 489-497 行** docstring：**唯一进入 AOP 增强链与 Port 调用的 Effect**——声明「调用什么能力」。逐字段说明。

- **第 498 行** `port: str`：目标 Port 注册名（∈ PORT_PROTOCOLS，装配期校验；不可运行时新增种类）。

- **第 499 行** `payload: EffectPayload = field(default_factory=_empty_payload)`：参数负载，已知键在 EffectPayload 声明，x-* 扩展键动态保留。

- **第 500 行** `batch: bool = False`：是否批量（payload['calls'] 由解释器展开为多个独立 Invoke）。

- **第 501 行** `trace_id: str | None = None`：链路标识，Runner 按 step/index 注入（见 runner.py 第 483 行）。

- **第 502 行** `idempotency_key: str | None = None`：副作用幂等键，R3 幂等去重与副作用重试红线读此字段。batch 展开时原样透传给每个子 Invoke（interpreter.py 第 93 行）——不这么传，批量副作用的重试保护就失效。

- **第 504 行** `def __post_init__(self) -> None:`
  冻结 payload。

- **第 505 行** docstring：frozen 下用 object.__setattr__。

- **第 506 行** `object.__setattr__(self, "payload", cast(EffectPayload, _freeze(self.payload)))`
  payload 归一为只读视图。防止 Program 或组合层拿到 Invoke 后改 payload。

- **第 508 行** `def with_payload(self, **extra: DynamicValue) -> Invoke:`
  在副本上增量更新 payload，返回新 Invoke（供组合层改写消费）。

- **第 509-515 行** docstring：`**extra` 是要合并的键值对（已知键 + x-* 扩展键）；返回 payload 更新后的新 Invoke。

- **第 516 行** `data: dict[str, DynamicValue] = dict(self.payload)`：复制 payload（MappingProxyType → 可变 dict）。

- **第 517 行** `data.update(extra)`：合并新键。

- **第 518 行** `return replace(self, payload=cast(EffectPayload, data))`：新 Invoke（新 payload 会再经 __post_init__ 冻结）。原对象不变。

### 第 521-531 行：Spawn DTO（递归子 Agent）

- **第 521 行** `@dataclass(frozen=True)`
  不可变。

- **第 522 行** `class Spawn:`

- **第 523-528 行** docstring：**递归创建子 Agent 的 Effect（内核路径，不进 AOP 链）**。关键点——`program_factory` 而非 generator：快照不携带不可序列化的 generator，恢复时重建。

- **第 529 行** `program_factory: Callable[[], Program] | None = None`：重建子 Program 的无参工厂。

- **第 530 行** `state: AgentState | None = None`：子 Agent 初始状态；缺省继承父会话 state（见 runner.py 第 371 行）。

- **第 531 行** `trace_id: str | None = None`：链路标识。

### 第 534-544 行：Suspend DTO（挂起等人工）

- **第 534 行** `@dataclass(frozen=True)`
  不可变。

- **第 535 行** `class Suspend:`

- **第 536-541 行** docstring：**挂起等人工输入的 Effect（内核快路径，不进 AOP 链）**。

- **第 542 行** `question: str = ""`：向人工提出的问题。

- **第 543 行** `options: tuple[str, ...] = ()`：候选答案，tuple 不可变。

- **第 544 行** `trace_id: str | None = None`：链路标识。

### 第 547-557 行：Terminate DTO（结束会话）

- **第 547 行** `@dataclass(frozen=True)`
  不可变。

- **第 548 行** `class Terminate:`

- **第 549-554 行** docstring：**结束会话并产出结果的 Effect（内核快路径，不进 AOP 链）**。

- **第 555 行** `output: DynamicValue = None`：最终输出。

- **第 556 行** `reason: str = ""`：结束原因。

- **第 557 行** `trace_id: str | None = None`：链路标识。

**四个 Effect 成员都带 trace_id**：这是 Runner 能用 `replace(e, trace_id=...)` 统一打标签的前提（runner.py 第 482-485 行）。少一个字段，统一 replace 就得特殊分支。

### 第 560-561 行：Effect tagged union

- **第 560 行** `Effect = Invoke | Spawn | Suspend | Terminate`
  Python 3.10+ 联合类型别名。内核按**具体类型**分派，而不是按字符串字段——这是 tagged union 的本质。

- **第 561 行** docstring：强调只有 Invoke 走 AOP 增强链，其余三个是内核快路径。

### 第 563-568 行：_EFFECT_BY_DISPOSITION 映射

- **第 563 行** `_EFFECT_BY_DISPOSITION: Mapping[str, type[Invoke | Spawn | Suspend | Terminate]] = MappingProxyType({...})`
  disposition 字符串 → Effect 类型的映射，冻结。

- **第 564-567 行** 四项：`"invoke"` → Invoke、`"spawn"` → Spawn、`"suspend"` → Suspend、`"terminate"` → Terminate。
  - 注意：这个表在 types.py 里定义了，但实际 `_effect_from_plain`（第 895 行）用的是 if 链而非这张表——它是给装配层/类型 consumers 用的公共映射。

### 第 571-585 行：disposition_of 函数

- **第 571 行** `def disposition_of(effect: Effect) -> Disposition:`
  按 Effect 具体类型返回判别标签（checkpoint 持久化与分派用）。

- **第 572-578 行** docstring：参数/返回说明。

- **第 579-580 行** `if isinstance(effect, Invoke): return Disposition.INVOKE`：Invoke 判别。

- **第 581-582 行** `if isinstance(effect, Spawn): return Disposition.SPAWN`：Spawn 判别。

- **第 583-584 行** `if isinstance(effect, Suspend): return Disposition.SUSPEND`：Suspend 判别。

- **第 585 行** `return Disposition.TERMINATE`：兜底——不是前三，就是 Terminate。这里用兜底而非第四个 isinstance，因为 union 只有四个成员，逻辑完备；若未来加第五成员，这里会静默错判——但 union 类型检查器会兜住遗漏的分支（配合 match  exhaustiveness）。

### 第 588-600 行：DONE 哨兵与 Program 类型

- **第 588 行** `class _Done:`
  Program 结束哨兵类型（单例 DONE）。

- **第 589 行** docstring：说明是单例。

- **第 590 行** `__slots__ = ()`：禁止实例动态加属性，且省内存。哨兵对象无需任何状态。

- **第 592 行** `def __repr__(self) -> str:  # pragma: no cover`
  返回 `DONE`，便于调试时识别结束哨兵。`pragma: no cover` 标记这行不要求测试覆盖（调试辅助）。

- **第 594 行** `return "DONE"`：repr 返回值。

- **第 597 行** `type DoneSentinel = _Done  # PEP 695（Python ≥3.12）`
  用 PEP 695 的 `type` 语句定义类型别名（延迟求值）。标注 Python ≥3.12 要求。

- **第 598 行** `DONE = _Done()`：全局单例。所有地方用 `is DONE` 比较（单例身份比较，见 interpreter.py 第 139 行、runner.py 第 473 行）。

- **第 599 行** `Program = Generator[Effect, DynamicValue, None]`
  用户 Agent 逻辑类型：yield Effect，send 收 DynamicValue，return None。

- **第 600 行** docstring：读 State → yield Effect → send() 收结果 → ... → return。

### 第 603-626 行：Event DTO（事件溯源最小单元）

- **第 603 行** `@dataclass(frozen=True)`
  事件不可变——append-only 的前提，落盘后绝不改写。

- **第 604 行** `class Event:`

- **第 605-614 行** docstring：定位——append-only，逻辑时钟保证确定性回放。逐字段说明。

- **第 615 行** `seq: int`：事件序号。

- **第 616 行** `session_id: str`：会话标识。

- **第 617 行** `step: int`：逻辑步号。

- **第 618 行** `state_snapshot: AgentState`：该步折叠后的 State 快照。

- **第 619 行** `effects: tuple[Effect, ...]`：该步的 Effect。

- **第 620 行** `results: tuple[Result, ...]`：该步的 Result（与 effects 按位置对应）。

- **第 621 行** `batched: bool = False`：是否批量执行（决定 send 回填 list 还是单值）。

- **第 622 行** `ts: int = 0`：**逻辑时钟（=step）**。不存墙钟时间——保证同样输入产生逐字节相同的事件日志（见 runner.py 第 558 行 `ts=step_no`）。

- **第 623 行** `cost_usd: float = 0.0`：该步用量成本。

- **第 624 行** `tokens: int = 0`：该步用量 token。

- **第 625 行** `status: str = "running"`：running/done/suspended/error（+ cancelled，见 runner.py 第 64 行）。

- **第 626 行** `reason: str = ""`：结束/挂起原因（稳定原因码，见 runner.py 第 95-99 行）。

### 第 629-643 行：CancellationToken DTO（对外不可变）

- **第 629 行** `@dataclass(frozen=True, slots=True)`
  frozen 不可变 + slots 省内存/禁止动态属性。

- **第 630 行** `class CancellationToken:`

- **第 631-636 行** docstring：显式注入，向子 effect/流递归传播，不用全局状态。**关键说明**：`cancel()` 返回新令牌，避免共享状态被就地改写（并发安全）。

- **第 638 行** `cancelled: bool = False`：是否已取消。

- **第 639 行** `reason: str = ""`：取消原因（进事件溯源）。

- **第 641 行** `def cancel(self, reason: str) -> CancellationToken:`
  生成已取消的新令牌（原令牌不变）。

- **第 643 行** `return CancellationToken(cancelled=True, reason=reason)`
  **这里埋着一个坑，正是设计决策 6 的由来**：frozen token 的 `cancel()` 返回新对象，但 Runner 在构造时拿到的是旧 token 引用，运行中调用 `token.cancel()` 产生的新对象 Runner 根本感知不到。所以 Runner 内部另用可变的 `_CancellationHolder` 原地置位（runner.py 第 110-124 行）。这个 token 只作为「构造初值」和「向子 Agent 传播的快照」使用。

### 第 646-662 行：RuntimeCtx DTO（可替换的上下文）

- **第 646 行** `@dataclass` 非 frozen——因为 Runner 的 `_bind` 要原地注入 `invoke` 字段（runner.py 第 291 行）。

- **第 647 行** `class RuntimeCtx:`

- **第 648-655 行** docstring：Runner 默认构造的上下文；上层可换成字段超集（R2 桥接用）。逐字段说明。

- **第 656 行** `effect: Effect`：当前 Effect。

- **第 657 行** `state: AgentState`：当前状态。

- **第 658 行** `session_id: str`：会话标识。

- **第 659 行** `attempt: int = 0`：尝试次数（R3 重试读此字段）。

- **第 660 行** `meta: dict[str, DynamicValue] = field(default_factory=dict)`：临时元数据。

- **第 661 行** `trace: list[str] = field(default_factory=list)`：hook/执行轨迹。

- **第 662 行** `invoke: Callable[[Effect, DynamicValue], Awaitable[Result]] | None = None`：由 Runner 注入的 Port 调用回调（洋葱中心使用）。缺省 None，由 `_bind` 填上。

### 第 665-681 行：Session DTO（聚合根）

- **第 665 行** `@dataclass` 可变——会话的 state/events 会被 Runner 就地推进。

- **第 666 行** `class Session:`

- **第 667-676 行** docstring：**program + state + event_log 聚合根**。关键点——用 `program_factory` 而非 generator：checkpoint 不可序列化，恢复时重建并重放。

- **第 677 行** `session_id: str`：会话标识。

- **第 678 行** `program_factory: Callable[[], Program]`：重建 Program 的无参工厂（必填，恢复的钥匙）。

- **第 679 行** `state: AgentState = field(default_factory=AgentState)`：当前状态。

- **第 680 行** `events: list[Event] = field(default_factory=list)`：事件日志（append-only，用 list 方便 append）。

- **第 681 行** `version: str = "0.0.0"`：会话版本。

### 第 684-701 行：StepReport DTO（单步报告）

- **第 684 行** `@dataclass(frozen=True)`
  不可变报告。

- **第 685 行** `class StepReport:`

- **第 686-693 行** docstring：单步报告；错误进报告不抛穿。

- **第 694 行** `step: int`：步号。

- **第 695 行** `state: AgentState`：折叠后状态。

- **第 696 行** `effects: tuple[Effect, ...] = ()`：本步 Effect。

- **第 697 行** `results: tuple[Result, ...] = ()`：本步 Result。

- **第 698 行** `status: str = "running"`：本步状态。

- **第 699 行** `reason: str = ""`：原因。

- **第 700 行** `error: str | None = None`：错误码（仅 error 态非空）。

- **第 701 行** `events: tuple[Event, ...] = ()`：截至本步的全部事件。

### 第 704-719 行：RunResult DTO（一次 run 终态）

- **第 704 行** `@dataclass(frozen=True)`
  不可变。

- **第 705 行** `class RunResult:`

- **第 706-712 行** docstring：一次 run 的终态。

- **第 713 行** `session_id: str`：会话标识。

- **第 714 行** `state: AgentState`：终态状态。

- **第 715 行** `status: str`：done/suspended/error。

- **第 716 行** `events: tuple[Event, ...]`：全部事件。

- **第 717 行** `output: DynamicValue = None`：最终输出。

- **第 718 行** `reason: str = ""`：结束原因。

- **第 719 行** `error: str | None = None`：错误码。

### 第 722-725 行：DispatchFn / CallFn 回调签名

- **第 722 行** `DispatchFn = Callable[[Effect, DynamicValue], Awaitable[DynamicValue]]`
  链路调用签名（装配层注入）；返回 DynamicValue（装配层可能包了增强链返回任意值）。

- **第 723 行** docstring。

- **第 724 行** `CallFn = Callable[[Effect, DynamicValue], Awaitable[Result]]`
  port 调用函数签名（上层注入）；**返回统一 Result 信封**。Runner 构造必填的 `call` 就是此类型。

- **第 725 行** docstring。

### 第 728-744 行：EventStore Protocol（可插拔快照存储）

- **第 728 行** `class EventStore(Protocol):`
  checkpoint 存储协议（R1 §4）；异步以支持 DB/Redis 持久化后端。

- **第 729-736 行** docstring：
  - 方法体 `...` 是 Protocol 语法（接口定义），不是省略实现。默认实现 `InMemoryEventStore` 在 runner.py。
  - **异步化（v2.1）**：落 Redis/DB 是生产刚需，同步会在 Runner 的 async 路径上阻塞事件循环。签名统一 async，内存实现内部无 IO 直接返回。

- **第 738 行** `async def save(self, session_id: str, snapshot: JsonSnapshot) -> None:`
  持久化会话快照。

- **第 739 行** docstring：参数说明。

- **第 740 行** `...`：Protocol 方法体省略号。

- **第 742 行** `async def load(self, session_id: str) -> JsonSnapshot | None:`
  读取会话快照；不存在返回 None。

- **第 743 行** docstring。

- **第 744 行** `...`：Protocol 方法体省略号。

### 第 747-793 行：_plain（确定性序列化核心）

- **第 747-749 行** 注释分隔符：`# 确定性序列化：事件日志逐字节可比对、checkpoint 可持久化`。
  这是「事件溯源 + 指纹」得以成立的技术底座。

- **第 750 行** `def _plain(obj: DynamicValue) -> DynamicValue:`
  递归规范化任意对象为纯 dict/list/标量。

- **第 751-759 行** docstring：按优先级列出分支——标量 → Enum.value → pydantic.model_dump → SimpleNamespace.vars → dataclass 字段 → 类对象转 str → Mapping → 序列 → to_dict() → 兜底 str(obj)。说明 set/frozenset 顺序不定，需排序。

- **第 761-762 行** `if obj is None or isinstance(obj, (str, int, float, bool)): return obj`
  标量原样返回（递归终止条件）。

- **第 763-764 行** `if isinstance(obj, Enum): return obj.value`
  Enum 取 value（如 Disposition.INVOKE → "invoke"），保证进 JSON 是字符串。

- **第 765-770 行** Effect tagged union 特殊分支：
  - **第 765-766 行** 注释：注入 disposition 判别标签，保证 checkpoint 可无损重建。
  - **第 767 行** `if isinstance(obj, (Invoke, Spawn, Suspend, Terminate)):`
  - **第 768 行** `plain = {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}`：逐字段规范化。
  - **第 769 行** `plain["disposition"] = disposition_of(obj).value`：**关键行**——显式加判别标签。
  - **第 770 行** `return plain`
  - **为什么必须在通用 dataclass 分支（第 775 行）之前**：否则各 Effect 成员只剩字段字典，类型信息丢失，`_effect_from_plain` 无法区分 Invoke/Spawn/Suspend/Terminate。

- **第 771-772 行** `if hasattr(obj, "model_dump"): return _plain(obj.model_dump(mode="python"))`
  pydantic 模型：先转 python 形态 dict 再递归。

- **第 773-774 行** `if isinstance(obj, types.SimpleNamespace): return {k: _plain(v) for k, v in vars(obj).items()}`
  SimpleNamespace：取 `__dict__` 转 dict。

- **第 775-776 行** `if dataclasses.is_dataclass(obj) and not isinstance(obj, type): return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}`
  通用 dataclass：逐字段规范化。`not isinstance(obj, type)` 排除类对象本身。

- **第 777-781 行** 类对象处理：
  - **第 777-779 行** 注释：提前排除 `type` 可消除 pyright 对 `is_dataclass()` TypeGuard 收窄的误报——运行时行为不变，只为类型检查器。
  - **第 780-781 行** `if isinstance(obj, type): return str(obj)`：类对象转字符串。

- **第 782-783 行** `if isinstance(obj, Mapping): return {str(k): _plain(v) for k, v in obj.items()}`
  Mapping（含 MappingProxyType）：键转字符串（JSON 要求键是 str），值递归。

- **第 784-788 行** set/frozenset 处理：
  - **第 785-786 行** 注释（B9）：set 迭代顺序随 `PYTHONHASHSEED` 变化，须规范化后按字符串键排序，保证 canonical/指纹跨进程确定。
  - **第 787 行** `normalized = [_plain(v) for v in obj]`：先规范化元素。
  - **第 788 行** `return sorted(normalized, key=lambda v: str(v))`：按字符串表示排序。元素可能异质（混合类型），不能直接 `sorted(obj)`——用 `str(v)` 作稳定排序键。

- **第 789-790 行** `if isinstance(obj, (list, tuple)): return [_plain(v) for v in obj]`
  序列递归规范化，输出统一为 list（tuple → list，JSON 友好）。

- **第 791-792 行** `if hasattr(obj, "to_dict"): return _plain(getattr(obj, "to_dict")())`
  有 to_dict 的对象（如 Message）：调方法后递归。放在 set/序列之后是为了让更具体的类型优先。

- **第 793 行** `return str(obj)`：兜底——任何未知对象转字符串，保证最终一定能 JSON 序列化。**不这么写**：遇到未知对象 json.dumps 会抛 TypeError，整个 checkpoint 落盘失败。

### 第 796-804 行：canonical（确定性 JSON）

- **第 796 行** `def canonical(obj: DynamicValue) -> str:`
  确定性 JSON：sort_keys + 紧凑分隔符，事件哈希与快照比对的基础。

- **第 797-803 行** docstring：参数/返回说明。返回排序、紧凑、保留中文的 JSON 字符串。

- **第 804 行** `return json.dumps(_plain(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
  - `sort_keys=True`：键排序，同样内容不同插入顺序得到相同字符串。
  - `ensure_ascii=False`：中文不转义成 `\uXXXX`，可读。
  - `separators=(",", ":")`：无空格紧凑输出，减少体积且确定。
  - 三者缺一，哈希就不确定或不可读。

### 第 807-809 行：fingerprint（事件日志指纹）

- **第 807 行** `def fingerprint(events: Sequence[Event]) -> str:`
  事件日志指纹（确定性门禁比对它）。

- **第 808 行** docstring：对规范化列表取 SHA256。

- **第 809 行** `return hashlib.sha256(canonical(list(events)).encode("utf-8")).hexdigest()`
  事件列表 → canonical 字符串 → UTF-8 编码 → SHA256 → 十六进制。任何事件字段被改，指纹就变。

### 第 812-826 行：checkpoint（生成快照）

- **第 812 行** `def checkpoint(session: Session) -> JsonSnapshot:`
  把会话折叠为可 JSON 持久化的快照（**不含 program，因其不可序列化**）。

- **第 813-819 行** docstring：参数/返回说明。

- **第 820-826 行** `return cast(JsonSnapshot, {...})` 五键：
  - **第 821 行** `"session_id": session.session_id`
  - **第 822 行** `"version": session.version`
  - **第 823 行** `"state": _plain(session.state)`：状态规范化。
  - **第 824 行** `"events": [_plain(e) for e in session.events]`：事件逐个规范化。
  - **第 825 行** `"fingerprint": fingerprint(session.events)`：附事件指纹。
  - 这就是 JsonSnapshot（第 71 行）的五个固定键。

### 第 829-837 行：Replayed（重放结果代理）

- **第 829 行** `class Replayed(dict[str, DynamicValue]):`
  重放结果代理：让 program 的 `resp.tool_calls` 等**属性访问**在反序列化后成立。

- **第 830 行** docstring。

- **第 832 行** `def __getattr__(self, key: str) -> DynamicValue:`
  把字典键当属性访问。

- **第 833 行** docstring：缺失键抛 AttributeError（符合属性访问语义，不能抛 KeyError）。

- **第 834-837 行** 实现：
  - **第 835 行** `return self[key]`：键当属性读。
  - **第 836-837 行** `except KeyError as exc: raise AttributeError(key) from exc`：异常类型转换。
  - **为什么需要**：快照里的 Result.data 是纯 dict，但 Program 当初写的是 `resp.tool_calls`。resume 回放时若不还原成可属性访问对象，Program 会 AttributeError（见 revive 第 840 行）。

### 第 840-850 行：revive（把纯 dict/list 还原为可属性访问）

- **第 840 行** `def revive(obj: DynamicValue) -> DynamicValue:`
  把规范化后的纯 dict/list 递归还原为可属性访问的对象。

- **第 841-845 行** docstring：dict → Replayed；list → 逐项 revive；其余标量原样。

- **第 846-847 行** `if isinstance(obj, dict): return Replayed({k: revive(v) for k, v in obj.items()})`
  dict 递归还原并包成 Replayed。

- **第 848-849 行** `if isinstance(obj, list): return [revive(v) for v in obj]`：list 递归。

- **第 850 行** `return obj`：标量原样。

### 第 853-892 行：restore_state（从快照还原 AgentState）

- **第 853 行** `def restore_state(data: StateSnapshot) -> AgentState:`
  从规范化快照还原 AgentState（**兼容旧快照：缺字段回退 scratch**）。

- **第 854-862 行** docstring：说明旧快照把托管字段塞在 scratch；managed() 优先读显式键，缺失才从 scratch 弹出。

- **第 863 行** `scratchpad = dict(data["scratch"])`：复制 scratch（后续要 pop）。

- **第 864 行** `raw: dict[str, DynamicValue] = dict(data)`：TypedDict 转普通 dict，支持变量键读取。

- **第 866 行** `def managed(key: str, default: DynamicValue) -> DynamicValue:`
  内部辅助：读托管字段，优先显式字段，兼容旧快照从 scratch 回退。

- **第 867 行** docstring。

- **第 868 行** `return raw[key] if key in raw else scratchpad.pop(key, default)`
  优先显式键；没有则从 scratch **弹出**（pop 而非 get，避免重复残留）。

- **第 870-876 行** 七个托管字段还原：
  - **第 870 行** `output = managed("output", None)`
  - **第 871 行** `last_skill = managed("last_skill", None)`
  - **第 872 行** `last_child = managed("last_child", None)`
  - **第 873 行** `last_wait_human = managed("last_wait_human", None)`
  - **第 874 行** `last_step_kinds = managed("last_step_kinds", ())`
  - **第 875 行** `last_tokens = managed("last_tokens", 0)`
  - **第 876 行** `last_reason = managed("last_reason", "")`

- **第 877-879 行** 清理旧 scratch：
  - **第 877 行** 注释：旧快照里这些键可能仍留在 scratch，弹出避免重复。
  - **第 878-879 行** `for old_key in ("last_skill", "last_child", "last_wait_human"): scratchpad.pop(old_key, None)`：这三个最容易残留（曾被用户代码写入 scratch）。

- **第 880-892 行** `return AgentState(...)`：
  - **第 881-885 行** messages 还原：`Message(role=m["role"], content=m["content"], name=m.get("name"), tool_calls=tuple(m.get("tool_calls") or ()))`——逐个重建 Message，`or ()` 处理空值。
  - **第 886 行** `scratch=scratchpad`：已清理的 scratch（AgentState.__post_init__ 会再冻结）。
  - **第 887 行** `budgets=Budgets(**data["budgets"])`：12 字段直接展开（budgets 字段稳定，无未知键风险）。
  - **第 888 行** `version=data["version"]`
  - **第 889-891 行** 七个托管字段回填。
  - 不这么写（直接 `AgentState(**data)`）：旧快照键结构不匹配，且 scratch 里的托管旧键不会被归一化。

### 第 895-923 行：_effect_from_plain（从字典重建 Effect）

- **第 895 行** `def _effect_from_plain(raw: Mapping[str, DynamicValue]) -> Effect:`
  从规范化字典重建 Effect tagged union（checkpoint 恢复的**唯一边界**）。

- **第 896-904 行** docstring：参数/返回说明；**disposition 缺失时回退 INVOKE**（兼容无标签的早期事件快照）。

- **第 905 行** `disposition = str(raw.get("disposition", Disposition.INVOKE.value))`
  读判别标签，缺省 "invoke"。

- **第 906-913 行** Invoke 分支：
  - **第 907-913 行** `return Invoke(port=str(raw.get("port", "")), payload=cast(EffectPayload, dict(raw.get("payload") or {})), batch=bool(raw.get("batch", False)), trace_id=raw.get("trace_id"), idempotency_key=raw.get("idempotency_key"))`：五字段逐一回填。

- **第 914-917 行** Spawn 分支：
  - **第 915 行** 注释：`program_factory` 不可序列化，恢复时由调用方经 program_factory 重注入；此处留空。
  - **第 916 行** `raw_state = raw.get("state")`
  - **第 917 行** `return Spawn(state=restore_state(raw_state) if raw_state is not None else None)`：有 state 则递归还原，没有留 None（继承父 state）。

- **第 918-922 行** Suspend 分支：
  - **第 919-922 行** `return Suspend(question=str(raw.get("question", "")), options=tuple(raw.get("options") or ()))`：question/options 回填，options 转 tuple。

- **第 923 行** `return Terminate(output=raw.get("output"), reason=str(raw.get("reason", "")))`
  兜底分支——不是前三，就是 Terminate（与 disposition_of 第 585 行同构）。

### 第 926-955 行：restore_checkpoint（从快照重建 Session）

- **第 926 行** `def restore_checkpoint(snapshot: JsonSnapshot, program_factory: Callable[[], Program]) -> Session:`
  从快照重建 State 与事件日志；Program 纯函数 + 重放定位断点。

- **第 927-934 行** docstring：参数/返回说明。

- **第 935 行** `events: list[Event] = []`：事件容器。

- **第 936-951 行** 逐事件重建循环：
  - **第 936 行** `for e in snapshot["events"]:` 按快照事件顺序重建。
  - **第 938-950 行** `Event(...)` 构造，字段一一回填：
    - **第 939 行** `seq=e["seq"], session_id=e["session_id"], step=e["step"]`
    - **第 940 行** `state_snapshot=restore_state(cast(StateSnapshot, e["state_snapshot"]))`：状态快照递归还原。
    - **第 941 行** `effects=tuple(_effect_from_plain(x) for x in e["effects"])`：每个 Effect 从字典重建。
    - **第 942-947 行** results 还原：
      - **第 943 行** `Result(ok=r["ok"], data=revive(r["data"]), ...)`：data 经 revive 还原为可属性访问对象（resume 回放的关键）。
      - **第 944 行** `usage=Usage.from_mapping(r["usage"]) if r.get("usage") else None`：有 usage 则归一化。
      - **第 945 行** `error=PortError(**r["error"]) if r.get("error") else None`：有 error 则重建 PortError。
    - **第 948-949 行** `batched=e["batched"], ts=e["ts"], cost_usd=e["cost_usd"], tokens=e["tokens"], status=e["status"], reason=e.get("reason", "")`：其余字段回填。
  - **第 951 行** 循环闭合。

- **第 952-955 行** `return Session(...)`:
  - **第 953 行** `session_id=snapshot["session_id"], program_factory=program_factory`：program_factory 由**调用方重新注入**（快照不含 generator）。
  - **第 954 行** `state=restore_state(snapshot["state"]), events=events, version=snapshot["version"]`：终态 State + 重建的事件日志。
  - 至此 checkpoint 闭环：checkpoint（第 812 行）写出 → 存储 → restore_checkpoint 读回 → Runner 用它重建执行态并回放（runner.py 第 250 行 `_replay`）。

---

## 2. fold.py 逐行讲解（223 行）

fold.py 是**模块唯一的状态变更入口**。记住三个词：纯函数、不可变、只增不减。它不读时间、不读随机、不碰 IO，同样的入参必得同样的 State。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R1 fold：模块唯一的状态变更入口（纯函数，不可变回填）。`
  一句话定位：状态只能在 fold 里变，且是纯函数。

- **第 2 行** 空行。

- **第 3-6 行** 三条约定：
  - **第 4 行** `- fold 只依赖入参，不读时间/随机/IO；同样的入参必得同样的 State。`
    确定性纪律——事件回放的基础。
  - **第 5 行** `- 预算由 payload['budget'] 声明、在 fold 统一扣减（Program 不许自己数数）。`
    预算纪律——Program 只声明消耗，由 fold 统一执行扣减。
  - **第 6 行** `- 所有返回都是新对象（frozen dataclass + with_），绝不就地修改入参 State。`
    不可变纪律。

- **第 7 行** `"""` docstring 结束。

### 第 9-12 行：imports

- **第 9 行** `from __future__ import annotations` 延迟注解。

- **第 11 行** `from collections.abc import Iterable, Sequence`
  `Iterable`（replay_events 的事件序列）/ `Sequence`（fold 的 effects/results）。

- **第 12 行** `from typing import cast`
  `_budget_delta` 里把 dict cast 成 BudgetDelta。

### 第 14-29 行：from .types import（14 个符号）

- **第 14-29 行** 导入 14 个符号：
  - DTO：`AgentState`/`BudgetDelta`/`DynamicValue`/`Effect`/`Invoke`/`Message`/`PortError`/`Result`/`Spawn`/`Suspend`/`Terminate`。
  - 工具：`canonical`（tool 消息序列化）/`disposition_of`（kind 序列）/ `PortName`（port 判别）。
  - 依赖方向：fold → types，不反向。types 不知道 fold 的存在。

### 第 31 行：__all__

- **第 31 行** `__all__ = ["field", "fold", "replay_events", "state_append_message", "state_set"]`
  导出五个符号。注意 `field` 也导出——它是给 Program 读 Effect 字段的通用工具（兼容 dict/dataclass/pydantic 三种形态）。

### 第 34-49 行：field（通用字段读取）

- **第 34 行** `def field(obj: DynamicValue, name: str, default: DynamicValue = None) -> DynamicValue:`
  读取对象字段，兼容三种形态：pydantic 模型 / dict / dataclass / Replayed。

- **第 35-44 行** docstring：参数 obj/name/default 与返回值说明。

- **第 45-46 行** `if obj is None: return default`：None 直接回退 default（避免对 None 做后续判断）。

- **第 47-48 行** `if isinstance(obj, dict): return obj.get(name, default)`
  Replayed / 纯 dict：按键读。

- **第 49 行** `return getattr(obj, name, default)`
  pydantic / dataclass：按属性读。`getattr` 带 default，字段不存在也不抛异常。
  - **为什么三种都要兼容**：Effect.payload 的值可能来自 pydantic 模型（装配层）、dict（快照恢复）、dataclass（内核 DTO）。统一用这一个函数读，调用方不必关心形态。

### 第 52-60 行：state_set（Program 用的不可变 scratch 写入）

- **第 52 行** `def state_set(state: AgentState, key: str, value: DynamicValue) -> AgentState:`
  Program 用的不可变 scratch 写入（phase/cursor/steps…），返回新 State。

- **第 53-59 行** docstring：key 写入键；**托管键由 AgentState.set 内部显式分派**。

- **第 60 行** `return state.set(key, value)`
  直接委托 `AgentState.set`（types.py 第 384 行）。fold 不重复实现写入逻辑。

### 第 63-65 行：state_append_message（对话消息追加）

- **第 63 行** `def state_append_message(state: AgentState, message: Message) -> AgentState:`
  向对话历史追加一条消息。

- **第 64 行** docstring：返回新 State（messages 重建为 tuple）。

- **第 65 行** `return state.with_(messages=(*state.messages, message))`
  **只增不减的具体实现**：用解包 `(*state.messages, message)` 构造新 tuple，不修改旧 messages。`with_` 会再归一（types.py 第 425-426 行）。不这么写（比如 `list.append`），就违反了不可变纪律。

### 第 68-82 行：_plain_value（单值规范化）

- **第 68 行** `def _plain_value(obj: DynamicValue) -> DynamicValue:`
  把单个值规范化为可序列化形态（确定性快照的一部分）。

- **第 69-75 行** docstring：三个分支——pydantic 模型走 model_dump；dict/list/tuple 递归；其余标量原样。

- **第 76-77 行** `if hasattr(obj, "model_dump"): return obj.model_dump(mode="python")`
  pydantic 模型先转 dict。

- **第 78-79 行** `if isinstance(obj, dict): return {k: _plain_value(v) for k, v in obj.items()}`
  dict 递归规范化每个值。

- **第 80-81 行** `if isinstance(obj, (list, tuple)): return [_plain_value(v) for v in obj]`
  序列递归，输出统一为 list。

- **第 82 行** `return obj`：标量原样。
  - 与 types.py 的 `_plain`（第 750 行）区别：`_plain_value` 是**轻量版**，只处理快照里会出现的三种形态；`_plain` 是**完整版**，处理所有对象类型。`_plain_value` 用于 `_message_for` 生成消息内容，`_plain` 用于整个 checkpoint。

### 第 85-97 行：_budget_delta（Effect 声明的单步预算）

- **第 85 行** `def _budget_delta(effect: Effect) -> BudgetDelta:`
  Effect 声明的单步预算；未声明时只扣 1 turn。

- **第 86-92 行** docstring：三个分支——非 Invoke 无预算声明只扣默认 1 turn；Invoke 无 budget 时空 dict 起步；turns 缺失补默认 1。

- **第 93-95 行** 声明读取：
  - **第 93-95 行** `declared: dict[str, DynamicValue] = dict(effect.payload.get("budget") or {}) if isinstance(effect, Invoke) else {}`
    - 只有 Invoke 才有 payload.budget；Spawn/Suspend/Terminate 走空 dict。
    - `or {}`：budget 为 None/空时回退空 dict。
    - `dict(...)` 复制一份——后面第 96 行要写入 turns，不改原 payload。

- **第 96 行** `declared["turns"] = declared.get("turns", 1)`
  **每个 Effect 至少消耗一个 turn**。即使 Effect 没声明预算，也扣 1——防止 Program 通过不声明预算来「免费」推进。

- **第 97 行** `return cast(BudgetDelta, declared)`：cast 成 TypedDict 返回。

### 第 100-129 行：_message_for（Effect → 对话消息）

- **第 100 行** `def _message_for(effect: Effect, result: Result) -> Message | None:`
  按 Effect 类型生成对话消息；返回 None 表示该类 Effect 不进 messages。

- **第 101-108 行** docstring：四个分支说明——失败 Result 记 system 消息；Invoke(llm) 记 assistant；Invoke(tool) 记 tool；其余不进 messages。

- **第 109 行** `data = result.data if result.ok else None`：成功取 data，失败置 None。

- **第 110-112 行** 失败分支：
  - **第 110 行** `if not result.ok:`
  - **第 111 行** `err = result.error or PortError("UNKNOWN_PORT_ERROR", "unknown port failure")`：错误信封缺失时兜底构造一个——不这么写，`err.code` 会 AttributeError。
  - **第 112 行** `return Message(role="system", content=f"[{err.code}] {err.message}")`：记 system 消息，把稳定错误码写进对话上下文，让模型/上层感知故障。

- **第 113-114 行** `if not isinstance(effect, Invoke): return None`
  非 Invoke（Spawn/Suspend/Terminate）不生成消息，由 AgentState 的托管字段承载。

- **第 115-118 行** LLM 分支：
  - **第 115 行** `if effect.port == PortName.LLM.value:`
  - **第 116 行** `content = field(data, "content", "") or ""`：取正文，`or ""` 防 None。
  - **第 117 行** `calls = tuple(_plain_value(c) for c in (field(data, "tool_calls", []) or []))`：工具调用列表规范化后转 tuple。
  - **第 118 行** `return Message(role="assistant", content=content, tool_calls=calls)`

- **第 119-128 行** TOOL 分支：
  - **第 119 行** `if effect.port == PortName.TOOL.value:`
  - **第 120 行** `call = effect.payload.get("call") or {}`：取调用对象。
  - **第 121 行** 注释：call_id 优先取调用对象内字段，回退到 payload 顶层声明。
  - **第 122 行** `call_id = field(call, "call_id", effect.payload.get("call_id", ""))`：两层回退——调用对象内的 call_id 优先，payload 顶层次之。
  - **第 123-127 行** 构造 payload 字典：
    - **第 124 行** `"ok": field(data, "ok", True)`：成功标志。
    - **第 125 行** `"value": _plain_value(field(data, "value"))`：结果值规范化。
    - **第 126 行** `"error": field(data, "error")`：错误信息（可能 None）。
  - **第 128 行** `return Message(role="tool", content=canonical(payload), name=str(call_id))`
    **结果信封经 canonical 序列化后放 content**，call_id 放 name。这样工具结果可逐字节比对。

- **第 129 行** `return None`：skill 及其他 port 不进 messages。

### 第 132-205 行：fold（核心状态折叠函数）

- **第 132-137 行** 函数签名：
  - `state: AgentState`：当前状态（不变）。
  - `effects: Sequence[Effect]`：本批已执行的 Effect。
  - `results: Sequence[Result]`：本批 Effect 的执行结果。
  - `reason: str = ""`：本步结束原因（写入 last_reason；空则沿用旧值）。

- **第 138-150 行** docstring：处理顺序——① 生成消息 ② 特殊 kind 托管字段 ③ 累计 usage ④ 统一扣预算。

- **第 151 行** `messages: list[Message] = list(state.messages)`
  消息累积器，**从旧消息副本起步**（不就地改旧 tuple）。

- **第 152 行** `budgets = state.budgets`：预算累积器（不可变，逐 Effect 替换）。

- **第 153 行** 注释：托管字段用显式局部变量（编译期可枚举），不用 `**dict` 拼装（编码规则 4）。

- **第 154-157 行** 五个托管字段局部副本：`output`/`last_skill`/`last_child`/`last_wait_human` 从 state 取初值。

- **第 158 行** `cost = 0.0`：本轮 usage 累计成本，扣完预算后清零，避免跨 Effect 重复计入。

- **第 159 行** `tokens = 0`：本轮 usage 累计 token（prompt + completion）。

- **第 160 行** 注释：effects 与 results 由 Runner 保证等长、同序，zip 逐一配对。

- **第 161 行** `for effect, result in zip(effects, results):`
  逐一配对处理。zip 天然截断到短的一方——Runner 保证等长，若不等长会静默丢数据，这是信任内部保证的取舍。

- **第 162-165 行** ① 生成消息：
  - **第 163 行** `message = _message_for(effect, result)`
  - **第 164-165 行** `if message is not None: messages.append(message)`

- **第 166-174 行** ② 特殊 kind 托管字段（互斥分支）：
  - **第 167-168 行** `if isinstance(effect, Invoke) and effect.port == PortName.SKILL.value: last_skill = _plain_value(result.data)`：SKILL 结果写 last_skill。
  - **第 169-170 行** `elif isinstance(effect, Spawn): last_child = _plain_value(result.data)`：子 Agent 结果单列托管——与 SKILL 来源不同，上层分别取用。
  - **第 171-172 行** `elif isinstance(effect, Suspend): last_wait_human = _plain_value(result.data)`：挂起结果。
  - **第 173-174 行** `elif isinstance(effect, Terminate): output = _plain_value(effect.output)`：**Terminate 直接产出最终 output**（替代旧的 END + payload['output']）。注意取的是 `effect.output`（Effect 自带），而非 `result.data`。
  - **为什么用 if/elif 互斥**：tagged union 四成员彼此排斥，一个 Effect 只会命中一个分支。

- **第 175-180 行** ③ 累计 usage：
  - **第 176 行** `if result.usage is not None:`
  - **第 177 行** `cost += float(field(result.usage, "cost_usd", 0.0))`：成本累加。
  - **第 178-180 行** `tokens += int(field(result.usage, "prompt_tokens", 0)) + int(field(result.usage, "completion_tokens", 0))`：prompt + completion token 累加。
  - 用 `field()` 而非直接属性访问：usage 可能是 pydantic 模型/dict/dataclass。

- **第 181-192 行** ④ 统一扣预算：
  - **第 182 行** `delta = _budget_delta(effect)`：取 Effect 声明的预算。
  - **第 183-191 行** `budgets = budgets.consume(...)`：六维扣减，替换累积器。
    - **第 184 行** `turns=int(delta.get("turns", 0))`
    - **第 185 行** `hops=int(delta.get("hops", 0))`
    - **第 186 行** `rounds=int(delta.get("rounds", 0))`
    - **第 187 行** `cost_usd=float(delta.get("cost_usd", 0.0)) + cost`
    - **第 188 行** 注释（B1）：usage 实际 token 与 cost 同口径扣进预算，否则 6 维预算的 token 维形同虚设。
    - **第 189 行** `tokens=int(delta.get("tokens", 0)) + tokens`
    - **第 190 行** `parallel_children=int(delta.get("parallel_children", 0))`
  - **第 192 行** `cost = 0.0`：已并入预算，清零以防下一个 Effect 重复累加。
    - **为什么不累加所有 Effect 的 usage 再一次总扣**：逐 Effect 扣并清零，使每个 Effect 的 usage 精确归属于它自己，便于事件里逐条归因。

- **第 194-205 行** 一次性构造新 State：
  - **第 194 行** 注释：消息与 kind 序列转 tuple，满足 frozen 不可变约束。
  - **第 195 行** `return state.with_(`
  - **第 196 行** `messages=tuple(messages)`：累积消息转 tuple。
  - **第 197 行** `budgets=budgets`：扣减后的预算。
  - **第 198-201 行** 四个托管字段。
  - **第 202 行** `last_step_kinds=tuple(disposition_of(e).value for e in effects)`：本步 Effect 的 kind 序列（disposition 值）。
  - **第 203 行** `last_tokens=tokens`：本步累计 token。
  - **第 204 行** `last_reason=reason or state.last_reason`：有 reason 用新的，空则沿用旧值（`or` 的语义）。
  - **第 205 行** `)` 闭合。
  - 整个过程零副作用：入参 state/effects/results 都不变，返回全新对象。

### 第 208-223 行：replay_events（事件重放）

- **第 208-210 行** 函数签名：`events: Iterable[DynamicValue]` + `initial_state: AgentState` → `AgentState`。

- **第 211-219 行** docstring：参数/返回说明；事件序列每项需有 effects/results/reason 属性或键。

- **第 220 行** `state = initial_state`：从初始状态起步。

- **第 221 行** `for event in events:` 按事件顺序重放，保证与执行期轨迹一致。

- **第 222 行** `state = fold(state, event.effects, event.results, getattr(event, "reason", ""))`
  逐步 fold。`getattr(event, "reason", "")` 兼容没有 reason 属性的早期事件。用 `getattr` 而非 `event.reason` 是为了兼容 dict/对象两种形态（不过这里 `event.effects` 也假设是属性，事件重建后是 Event dataclass，所以 OK）。

- **第 223 行** `return state`：终态。这就是「事件溯源」——只靠事件日志 + 初始状态就能重建任意时刻的状态，不需要重新执行任何 IO。

---

## 3. interpreter.py 逐行讲解（166 行）

interpreter.py 是 **generator 解释器**。它只做确定性推进，不做任何 IO。职责边界：把 generator 的一次推进（`next` / `send`）翻译成「下一批 Effect 或 DONE」。batch 展开也在这里完成，保证 Runner 拿到的是扁平、可并行的 Effect 批次。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R1 解释器：驱动 Program generator，只做确定性推进，不做任何 IO。`
  一句话定位：确定性推进 + 零 IO。

- **第 2 行** 空行。

- **第 3 行** `职责边界：把 generator 的一次推进（next / send）翻译为"下一批 Effect 或 DONE"。`
  明确输入（推进）与输出（Effect 批或 DONE）。

- **第 4-5 行** batch 展开职责：
  - **第 4 行** `batch 展开（一个 Effect 携带多个 calls → 多个独立 Effect）也在此完成，`
  - **第 5 行** `保证 Runner 拿到的永远是扁平、可并行的 Effect 批次。`
  - 这就是设计决策 4：Interpreter 负责把 batch 摊平，Runner 不必理解 batch 语义。

- **第 6 行** `"""` docstring 结束。

### 第 7-10 行：imports

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `from dataclasses import dataclass, replace`
  `dataclass` 定义 EffectBatch；`replace` 在 `_expand` 里降级 batch / 构造子 Invoke。

- **第 10 行** `from typing import Protocol, cast`
  `Protocol` 定义 Interpreter 接口；`cast` 转 EffectPayload。

### 第 12-24 行：from .errors / .types import

- **第 12 行** `from .errors import ProgramCrashed, ProgramInvalid`
  两类 Program 契约异常。

- **第 13-24 行** 从 types 导入 11 个符号：
  - `DONE`/`DoneSentinel`：结束哨兵。
  - `DynamicValue`/`Effect`/`EffectPayload`/`Program`：核心类型。
  - `Invoke`/`Spawn`/`Suspend`/`Terminate`：Effect 四成员（`_is_effect` 判别用）。

### 第 26 行：__all__

- **第 26 行** `__all__ = ["EffectBatch", "Interpreter", "InterpreterImpl", "SendResult"]`
  导出四个符号。

### 第 29-39 行：EffectBatch DTO

- **第 29 行** `@dataclass(frozen=True)`
  不可变批次。

- **第 30 行** `class EffectBatch:`

- **第 31-37 行** docstring：
  - 一批待执行 Effect；`batched=True` 表示回填 Program 时传 list 而非单值。
  - `effects`：本批 Effect（tuple 保证不可变）。
  - `batched`：Program 当初是「批量 yield」还是单 yield；决定 send 回填的是 list[结果] 还是单个结果。**batch 展开后 >1 个也置 True**。

- **第 38 行** `effects: tuple[Effect, ...]`：Effect 元组。

- **第 39 行** `batched: bool = False`：默认非批量。

### 第 42-43 行：SendResult 类型别名

- **第 42 行** `type SendResult = EffectBatch | DoneSentinel`
  PEP 695 类型别名：解释器推进结果 = 下一批 Effect **或** 结束哨兵。

- **第 43 行** docstring：说明两种可能。

### 第 46-62 行：Interpreter Protocol

- **第 46 行** `class Interpreter(Protocol):`
  解释器协议（可插拔）。

- **第 47-51 行** docstring：`send` 一次推进一步；方法体 `...` 是 Protocol 接口语法，不是省略实现；默认实现是下方的 `InterpreterImpl`。

- **第 53 行** `def send(self, value: DynamicValue) -> SendResult:`
  推进一步。

- **第 54-61 行** docstring：参数 value 是上次 yield Effect 的执行结果（首次忽略，内部走 next）；返回 EffectBatch 或 DONE。

- **第 62 行** `...`：Protocol 方法体省略号。

### 第 65-67 行：_is_effect（Effect 合法性判别）

- **第 65 行** `def _is_effect(value: DynamicValue) -> bool:`
  判断 yield 值是否为合法 Effect。

- **第 66 行** docstring：tagged union 四成员之一。

- **第 67 行** `return isinstance(value, (Invoke, Spawn, Suspend, Terminate))`
  用四成员元组做 isinstance。不这么写（比如检查有没有 `port` 属性），无法精确识别四成员，且会把任意对象误当 Effect。

### 第 70-96 行：_expand（batch 展开）

- **第 70 行** `def _expand(effect: Effect) -> tuple[Effect, ...]:`
  batch=True 的 Invoke 按 `payload['calls']` 展开为多个独立 Invoke。

- **第 71-78 行** docstring：三个分支——非 Invoke 或无 batch 原样返回；batch 但无 calls 退化为单个非 batch Invoke；batch 且有 calls 逐个生成独立 Invoke，trace_id 与幂等键原样透传。

- **第 79-80 行** `if not isinstance(effect, Invoke) or not effect.batch: return (effect,)`
  非 Invoke 或不 batch：原样返回单元素元组。Spawn/Suspend/Terminate 永远不展开。

- **第 81 行** `calls = list(effect.payload.get("calls") or ())`
  取待展开的调用列表，None 回退空元组再转 list。

- **第 82-83 行** `if not calls: return (replace(effect, batch=False),)`
  **批量声明为空：降级为单 Effect**。不这么处理会产出空批次——Runner 拿到空 effects 会导致 step 无意义推进，甚至 fold 后状态不变但仍记事件。

- **第 84 行** 注释（D-2）：展开时移除 calls 聚合键，避免每个子 Effect 又携带整份 calls（port 种类不变）。

- **第 85 行** `payload: dict[str, DynamicValue] = {k: v for k, v in effect.payload.items() if k != "calls"}`
  复制 payload 但剔除 calls 键。不剔除的话，N 个子 Effect 各自带整份 N 个 calls，数据重复且下游可能再次展开。

- **第 86-96 行** 返回 tuple 推导式：
  - **第 87 行** `Invoke(` 构造每个子 Invoke。
  - **第 88 行** `port=effect.port`：port 不变。
  - **第 89 行** `payload=cast(EffectPayload, {**payload, "call": call})`：展开后的 payload = 去 calls 的公共部分 + 单个 `call` 键。
  - **第 90 行** `batch=False`：子 Effect 已展开，不再是 batch（防止二次展开）。
  - **第 91 行** `trace_id=effect.trace_id`：保留链路溯源。
  - **第 92-93 行** 注释 + `idempotency_key=effect.idempotency_key`：**父 Effect 的幂等键传给每个子 Effect，否则批量副作用的重试红线失效**（设计决策 4 的核心）。
  - **第 95 行** `for call in calls`：逐个 call 生成。
  - 注意：父 Invoke 的 idempotency_key 是「整批」的键，展开后每个子 Effect 共享它——R3 幂等去重时按此键 + call 内容区分。

### 第 99-166 行：InterpreterImpl 类

- **第 99 行** `class InterpreterImpl:`

- **第 100 行** docstring：`program.send(上次结果) → 下一批 Effect | DONE`；**预算检查在 Runner 层**（解释器不管预算）。

- **第 102-115 行** `__init__` 构造函数：
  - **第 102 行** `def __init__(self, program: Program):`
  - **第 103-110 行** docstring：用 generator 构造解释器；非 generator 直接 ProgramInvalid。
  - **第 112-113 行** `if not (hasattr(program, "send") and hasattr(program, "__next__")): raise ProgramInvalid(...)`
    **契约校验**：generator 必须同时具备 `send` 与 `__next__`。鸭子类型判断，不要求 `isinstance(Generator)`（generator 类型检查复杂）。不这么判，后续 `next(self._gen)` 会抛 TypeError 而非框架异常。
  - **第 114 行** `self._gen = program`：保存底层 generator。
  - **第 115 行** `self._started = False`：是否已完成首次 next()。首推用 next，其后用 send——因为 generator 未启动时直接 `send(value)` 会对非 None 值抛 TypeError。

- **第 117-120 行** `started` 属性：
  - **第 117 行** `@property`
  - **第 118 行** `def started(self) -> bool:`
  - **第 119 行** docstring：True 表示后续应走 send 而非 next。
  - **第 120 行** `return self._started`

- **第 122-166 行** `send` 方法（核心推进逻辑）：
  - **第 122 行** `def send(self, value: DynamicValue) -> SendResult:`
  - **第 123-134 行** docstring：首次 next() 启动，其后 send(value) 回填上次结果；异常说明。
  - **第 135 行** `try:`
  - **第 136 行** 注释：首次必须 next() 启动 generator。
  - **第 137 行** `raw = next(self._gen) if not self._started else self._gen.send(value)`
    **推进核心**：未启动走 `next()`，已启动走 `send(value)`。
  - **第 138-139 行** `except StopIteration: return DONE`
    generator return ⇒ 程序结束，返回结束哨兵。
  - **第 140-141 行** `except ProgramInvalid: raise`
    契约违规**原样抛出**，交由 Runner 落入报告（不包装，保留原语义）。
  - **第 142-144 行** `except Exception as exc: raise ProgramCrashed(...) from exc`
    **任何未预期异常折叠为 ProgramCrashed**，绝不裸抛穿运行时。`from exc` 保留异常链便于诊断。
  - **第 145 行** `self._started = True`：推进成功标记已启动。
  - **第 147-150 行** 单 Effect 分支：
    - **第 147 行** `if _is_effect(raw):`
    - **第 148 行** `single = _expand(raw)`：单 Effect（Invoke 可能是 batch，需展开）。
    - **第 149 行** `batched = (isinstance(raw, Invoke) and raw.batch) or len(single) > 1`
      batched 标志两个来源：原本就是 batch Invoke，或展开后多于 1 个。
    - **第 150 行** `return EffectBatch(single, batched=batched)`
  - **第 151-162 行** Effect 列表分支：
    - **第 151-152 行** 注释：Program 也允许一次 yield 一个 Effect 列表（并行声明）。
    - **第 153 行** `if isinstance(raw, (list, tuple)) and raw:`
      **仅认「非空」列表**：空列表既非合法 Effect 批、也不能当 DONE，必须落到底部按契约违规报告（第 164 行）。不这么写，空列表会被误当 DONE 提前结束程序。
    - **第 154 行** `items: tuple[DynamicValue, ...] = tuple(raw)`：归一为 tuple。
    - **第 155 行** `if all(_is_effect(e) for e in items):`
      要求列表中**每个**元素都是合法 Effect。混合了非法对象的列表整体拒绝。
    - **第 156 行** 注释：逐项展开后再合并——列表里每个 Effect 自己可能也是 batch。
    - **第 157-161 行** 嵌套推导式：
      - **第 158-161 行** `many: tuple[Effect, ...] = tuple(expanded for item in items for expanded in _expand(cast(Effect, item)))`
        两层循环：外层遍历 items，内层遍历每个 item 展开后的子 Effect，全部摊平到一个扁平批次。
    - **第 162 行** `return EffectBatch(many, batched=True)`：列表 yield 一定是 batched（回填 list）。
  - **第 163-166 行** 非法对象兜底：
    - **第 163 行** 注释：走到这里说明 yield 了非法对象（含空列表）。
    - **第 164-166 行** `raise ProgramInvalid(f"program yielded {type(raw).__name__}; expected Effect | list[Effect]")`
      错误信息带上实际类型名，便于诊断。

**Interpreter 与 Runner 的分工总结**：Interpreter 只推进 generator + 展开 batch，输出扁平的 EffectBatch；预算检查、trace_id 注入、IO 调用、fold、事件落盘全在 Runner。这正是「唯一 IO 点」纪律的体现。

---

## 4. runner.py 逐行讲解（718 行）

runner.py 是**唯一的 async IO 编排点**。整个内核只有这个文件有 `async`/`await`。链路顺序：预算预检 → 驱动 generator → batch 展开 → call(Port) → fold 纯函数回填 → 事件溯源 → checkpoint → 继续/挂起/结束。所有异常在此折叠为 Result，绝不裸抛穿运行时。

### 第 1-11 行：模块 docstring

- **第 1 行** `"""R1 Runner：唯一的 async IO 编排点。`
  一句话定位。

- **第 2 行** 空行。

- **第 3-4 行** 链路顺序：
  - **第 3 行** `链路顺序：预算预检 → 驱动 generator → batch 展开 → call(Port) →`
  - **第 4 行** `fold 纯函数回填 → 事件溯源 → checkpoint → 继续/挂起/结束。`

- **第 5 行** 空行。

- **第 6-7 行** 依赖注入：
  - **第 6 行** `依赖注入：call / store / ctx_factory / notify 均可替换；store 未注入时用内存实现，`
  - **第 7 行** `因此 R1 无需外部服务即可独立闭环。所有异常在此折叠为 Result，绝不裸抛穿运行时。`

- **第 8 行** 空行。

- **第 9-10 行** 内核零依赖扩展体系：
  - **第 9 行** `内核零依赖扩展体系：AOP 增强链由装配层包进 call 槽，Lifecycle 通知由 notify 回调`
  - **第 10 行** `承载（内核只认 Callable，不 import hooks/aop）。依赖方向恒定 装配层 → 内核。`
  - 这是设计决策 1 的落点：内核不认识 hooks/aop，只认回调。

- **第 11 行** `"""` docstring 结束。

### 第 12-18 行：imports

- **第 12 行** `from __future__ import annotations` 延迟注解。

- **第 14 行** `import asyncio`
  `asyncio.gather` 并发执行一批 Effect（第 515 行）。

- **第 15 行** `import inspect`
  `inspect.isawaitable` 判断 call 返回值是否可 await（第 184 行）。

- **第 16 行** `from collections.abc import Callable` 回调类型注解。

- **第 17 行** `from dataclasses import dataclass, replace`
  `replace` 给 Effect 打 trace_id（第 483 行）。

- **第 18 行** `from enum import Enum`
  三个内部枚举（_StepStatus/_ErrCode/_Phase）。

### 第 20-49 行：from .errors / .fold / .interpreter / .types import

- **第 20-22 行** 从 errors 导入五个异常类：`AgentKitError`/`BudgetExceeded`/`PortMissing`/`ProgramCrashed`/`ProgramInvalid`。

- **第 23 行** `from .fold import field, fold, replay_events`
  纯函数状态折叠与重放。

- **第 24 行** `from .interpreter import EffectBatch, InterpreterImpl, SendResult`
  解释器。

- **第 25-49 行** 从 types 导入 24 个符号：DTO（AgentState/Effect/Event/Result/Session/RunResult/StepReport/RuntimeCtx/Usage 等）+ Effect 四成员 + 工具（checkpoint/restore_checkpoint/DONE）。

### 第 51 行：__all__

- **第 51 行** `__all__ = ["InMemoryEventStore", "Runner"]`
  只导出两个符号——Runner 与默认存储。内部枚举/常量不暴露。

### 第 54-55 行：NotifyFn 类型别名

- **第 54 行** `type NotifyFn = Callable[[str, DynamicValue], None]`
  Lifecycle 通知注入回调签名：`(相位名, 只读投影)`。

- **第 55 行** docstring：内核零依赖 hooks，只认 Callable。

### 第 58-64 行：_StepStatus 枚举

- **第 58 行** `class _StepStatus(str, Enum):`
  单步状态枚举，值与 Event/StepReport.status 的既有字符串一致。

- **第 60 行** `RUNNING = "running"`

- **第 61 行** `DONE = "done"`

- **第 62 行** `SUSPENDED = "suspended"`

- **第 63 行** `ERROR = "error"`

- **第 64 行** `CANCELLED = "cancelled"`：取消态。

### 第 67-72 行：_ErrCode 枚举

- **第 67 行** `class _ErrCode(str, Enum):`
  Runner 内部折叠错误码，值与 errors.py 的稳定 code 对齐。

- **第 69 行** `PORT_CRASHED = "PORT_CRASHED"`：Port 调用抛未预期异常。

- **第 70 行** `PROGRAM_CRASHED = "PROGRAM_CRASHED"`：Program 内部异常。

- **第 71 行** `UNKNOWN = "UNKNOWN"`：兜底未知错误。

- **第 72 行** `CHILD_PROGRAM_MISSING = "CHILD_PROGRAM_MISSING"`：Spawn 缺 program_factory。

### 第 75-92 行：_Phase 枚举（13 个通知相位）

- **第 75 行** `class _Phase(str, Enum):`

- **第 76-79 行** docstring：**Runner 发出的 Lifecycle 相位名（13 个）**；取值与 `hooks.LifecyclePhase` 一致。内核零依赖 hooks，故在此具名对齐而非 import；新增相位须同步两处枚举。

- **第 80 行** `BEFORE_TOOL = "before_tool"`：工具调用前。

- **第 81 行** `AFTER_TOOL = "after_tool"`：工具成功返回后。

- **第 82 行** `TOOL_ERROR = "tool_error"`：工具失败时。

- **第 83 行** `CHILD_SPAWN = "child_spawn"`：子 Agent 创建。

- **第 84 行** `CHILD_DONE = "child_done"`：子 Agent 完成。

- **第 85 行** `CANCELLED = "cancelled"`：取消触发。

- **第 86 行** `BUDGET_CHECK = "budget_check"`：预算检查（通过或超限都发）。

- **第 87 行** `WAIT_HUMAN = "wait_human"`：挂起等人工。

- **第 88 行** `STATE_FOLD = "state_fold"`：状态折叠完成。

- **第 89 行** `SESSION_START = "session_start"`：会话开始。

- **第 90 行** `USER_PROMPT = "user_prompt"`：用户提示注入。

- **第 91 行** `RESUMED = "resumed"`：恢复运行。

- **第 92 行** `SESSION_END = "session_end"`：会话结束（终态）。

### 第 95-99 行：稳定原因码

- **第 95 行** 注释：`# 稳定原因码：进 Event.reason / RunResult.reason，值一旦发布不可更改。`
  设计决策 18。

- **第 96 行** `_REASON_PROGRAM_RETURNED = "program_returned"`：Program 正常 return。

- **第 97 行** `_REASON_WAIT_HUMAN = "wait_human"`：挂起等人工。

- **第 98 行** `_REASON_TERMINATED = "terminated"`：Program 主动 Terminate。

- **第 99 行** `_REASON_PARALLEL_CHILDREN = "parallel_children"`：并行子 Agent 超限。

### 第 101-102 行：子会话 id 分隔符

- **第 101 行** 注释：`# 子会话 id 分隔符：<parent>{_CHILD_SESSION_SEPARATOR}<seq>。`

- **第 102 行** `_CHILD_SESSION_SEPARATOR = ":child:"`
  子会话 id 格式（设计决策 14）：`<父会话>:child:<序号>`。用特殊分隔符避免与父 id 混淆，便于溯源层级。

### 第 104-107 行：Usage 动态字段名

- **第 104 行** 注释：`# Usage 动态字段名：读错字段会静默丢成本/token，故具名收敛。`

- **第 105 行** `_USAGE_COST_FIELD = "cost_usd"`

- **第 106 行** `_USAGE_PROMPT_TOKENS_FIELD = "prompt_tokens"`

- **第 107 行** `_USAGE_COMPLETION_TOKENS_FIELD = "completion_tokens"`
  三个常量把动态字符串收敛为具名常量，避免多处硬编码拼错（拼错会静默读不到，丢成本/token）。

### 第 110-124 行：_CancellationHolder（可变取消态）

- **第 110 行** `@dataclass`
  可变（非 frozen）——这是关键。

- **第 111 行** `class _CancellationHolder:`

- **第 112-117 行** docstring：**设计决策 6 的完整说明**——
  `CancellationToken` 对外保持 frozen 语义（`cancel()` 返回新对象），但 Runner 构造后无法感知新对象，导致运行中取消失效。holder 由 Runner 独占持有，通过 `Runner.cancel()` 原地置位；step() 每步读 holder，兑现取消承诺。

- **第 118 行** `cancelled: bool = False`：是否已取消。

- **第 119 行** `reason: str = ""`：取消原因。

- **第 121 行** `def cancel(self, reason: str) -> None:`
  原地置位取消态。

- **第 123-124 行** `self.cancelled = True` / `self.reason = reason`：原地改（与 frozen token 的「返回新对象」相反）。
  - 为什么不直接用 CancellationToken：见第 112-117 行说明——frozen token 的新对象无法传播回已持有旧引用的 Runner。

### 第 127-138 行：_Runtime（单会话执行态）

- **第 127 行** `@dataclass`
  可变——step 会改 step/last_value。

- **第 128 行** `class _Runtime:`

- **第 129-135 行** docstring：单个会话在 Runner 内的可变执行态（与 Session 分离，便于重放重建）。三字段说明。

- **第 136 行** `interpreter: InterpreterImpl`：该会话的 generator 解释器，承载推进游标。

- **第 137 行** `last_value: DynamicValue = None`：上一次 Effect 的结果，下一步 send 回 Program。

- **第 138 行** `step: int = 0`：当前步号（逻辑时钟，Event.ts 与 trace_id 都取自它）。

### 第 141-159 行：InMemoryEventStore（默认快照存储）

- **第 141 行** `class InMemoryEventStore(EventStore):`
  R1 内置默认 checkpoint 存储：进程内存字典。

- **第 142 行** docstring。

- **第 144 行** `def __init__(self) -> None:`

- **第 145 行** docstring：初始化内存快照表（无持久化，仅用于独立闭环测试）。

- **第 146 行** `self.snapshots: dict[str, JsonSnapshot] = {}`：会话 id → 快照。

- **第 148 行** `async def save(self, session_id: str, snapshot: JsonSnapshot) -> None:`

- **第 149-154 行** docstring：参数说明。

- **第 155 行** `self.snapshots[session_id] = snapshot`：内存写入（无 IO，但保持 async 签名与协议一致）。

- **第 157 行** `async def load(self, session_id: str) -> JsonSnapshot | None:`

- **第 158 行** docstring：不存在返回 None。

- **第 159 行** `return self.snapshots.get(session_id)`：内存读取。

### 第 162-173 行：_send_value（Result → Program 回填值）

- **第 162 行** `def _send_value(event: Event) -> DynamicValue:`
  把事件里的 Result 还原成 Program 当初 send 收到的值。**resume 回放的关键**。

- **第 163-169 行** docstring：
  - batched：返回 list（每个 Result 取 data，失败则取 Result 本身）。
  - 单个：取首个值的 data / Result；无结果返回 None。
  - **注意失败时用 Result 对象本身（而非 data=None）回填**，让 Program 能读到错误信封（设计决策 7）。

- **第 170 行** `values = [r.data if r.ok else r for r in event.results]`
  **失败回填 Result 本体的实现**：成功取 data，失败取整个 Result。

- **第 171-172 行** `if event.batched: return values`：批量返回 list。

- **第 173 行** `return values[0] if values else None`：单个取首值，无结果 None。

### 第 176-184 行：_maybe_await（统一等待）

- **第 176 行** `async def _maybe_await(value: DynamicValue) -> DynamicValue:`
  统一等待：call 既可是同步值也可是 awaitable。

- **第 177-183 行** docstring：参数/返回说明。

- **第 184 行** `return await value if inspect.isawaitable(value) else value`
  可 await 则 await，否则原样返回。**让 call 同时支持同步与异步实现**——装配层注入的 Port 适配器不必都写成 async。

### 第 187-233 行：Runner.__init__（依赖注入装配）

- **第 187 行** `class Runner:`

- **第 188 行** docstring：唯一 async IO 编排点；所有可替换依赖走构造注入，R1 可独立闭环。

- **第 190-200 行** 构造签名：
  - **第 192 行** `call: CallFn`：必填位置参数——Port 调用函数。
  - **第 193 行** `*`：之后全是关键字参数。
  - **第 194 行** `store: EventStore | None = None`：快照存储。
  - **第 195 行** `ctx_factory: Callable[[Effect, Session], DynamicValue] | None = None`：上下文工厂。
  - **第 196 行** `checkpoint_every: int = 1`：每 N 步落一次快照。
  - **第 197 行** `max_steps: int = 1000`：单 run 步数硬上限。
  - **第 198 行** `cancellation: CancellationToken | None = None`：取消令牌。
  - **第 199 行** `notify: NotifyFn | None = None`：Lifecycle 通知回调。

- **第 201-211 行** docstring：逐参数说明。
  - **第 204 行** call：AOP 增强链由装配层包进此槽，内核零感知。
  - **第 207 行** checkpoint_every：终态总是落，最小强制为 1。
  - **第 209 行** cancellation：子 Agent 派生时继承同一令牌。

- **第 212 行** `self._call = call`：保存 Port 调用。

- **第 213 行** `self._store = store or InMemoryEventStore()`：缺省内存存储。

- **第 214-218 行** ctx_factory 缺省：
  - **第 214 行** `self._ctx_factory = ctx_factory or (`
  - **第 215-217 行** lambda 构造 `RuntimeCtx(effect=effect, state=session.state, session_id=session.session_id)`。
  - **第 218 行** `)` 闭合。
  - 缺省上下文只有三个字段，上层可传工厂返回字段超集（R2 桥接）。

- **第 219 行** `self._checkpoint_every = max(1, checkpoint_every)`
  **至少每步落盘**。不这么写（允许 0）：`rt.step % 0` 会 ZeroDivisionError。

- **第 220 行** `self._max_steps = max_steps`：步数硬上限。

- **第 221 行** `self._cancellation = cancellation`：原始令牌（**注意是 frozen token，不是 holder**）——用于向子 Agent 传播。

- **第 222 行** `self._notify = notify or (lambda hook, projection: None)`：缺省 no-op 通知——不这么写，每处调用都得判空。

- **第 223-227 行** cancel holder 初始化：
  - **第 223 行** 注释：可变 holder 接收运行期 `Runner.cancel()`；初值来自构造令牌。
  - **第 224-227 行** `self._cancel_holder = _CancellationHolder(cancelled=cancellation.cancelled if cancellation else False, reason=cancellation.reason if cancellation else "")`
    初值从构造令牌取，之后运行期取消走 holder（设计决策 6）。

- **第 228 行** `self._runtimes: dict[str, _Runtime] = {}`：session_id → 执行态。

- **第 229 行** `self._reasons: dict[str, str] = {}`：session_id → 最近一次结束原因。

- **第 230 行** `self._errors: dict[str, str | None] = {}`：session_id → 最近一次错误码。

- **第 231-233 行** 子会话序号：
  - **第 231-232 行** 注释：按父 session 独立递增；同批并发 Spawn 必须拿到唯一 id。**asyncio 单线程中「读 seq → 写 seq+1」之间没有 await，故并发安全**（GIL + 无 await 区间保证原子性）。
  - **第 233 行** `self._child_seq: dict[str, int] = {}`

### 第 235-248 行：_runtime（取或懒建执行态）

- **第 235 行** `def _runtime(self, session: Session) -> _Runtime:`

- **第 236-242 行** docstring：取（或懒建）会话执行态；首次创建时重建 generator 并**回放事件到断点**。

- **第 243 行** `rt = self._runtimes.get(session.session_id)`：查缓存。

- **第 244-248 行** 首次创建：
  - **第 244 行** `if rt is None:`（注释：首次，新建解释器并回放已有事件）
  - **第 245 行** `rt = _Runtime(InterpreterImpl(session.program_factory()))`：用工厂重建 generator。
  - **第 246 行** `self._runtimes[session.session_id] = rt`：缓存。
  - **第 247 行** `self._replay(session, rt)`：回放到断点。
  - **第 248 行** `return rt`

### 第 250-279 行：_replay（从事件日志推进 generator 到断点）

- **第 250-252 行** 函数签名：`_replay(self, session, rt, answer=None)`。

- **第 253-259 行** docstring：从事件日志把新建 generator 推进到断点（**Program 纯函数 ⇒ 轨迹一致**）。answer 仅当末事件为 suspended 时作为最后回填值。

- **第 260 行** `events = session.events`

- **第 261-262 行** `if not events: return`：无事件，generator 停在初始态。

- **第 263-264 行** `if rt.interpreter.send(None) is DONE: return`
  首次 next() 即结束：空 Program。用 `is DONE` 单例比较。

- **第 265 行** 注释：回放除末事件外的全部事件；遇到 done/error 说明已到终态，停止。

- **第 266-270 行** 回放循环：
  - **第 266 行** `for event in events[:-1]:` 除末事件外逐个。
  - **第 267-268 行** `if event.status in (DONE, ERROR): return`：遇终态停止。
  - **第 269-270 行** `if rt.interpreter.send(_send_value(event)) is DONE: return`：用事件结果回填推进；若 DONE 提前停。

- **第 271-279 行** 末事件决定最后回填值：
  - **第 272 行** `last = events[-1]`
  - **第 273-275 行** suspended 分支：
    - **第 273 行** `if last.status == _StepStatus.SUSPENDED.value:`
    - **第 274 行** 注释：挂起恢复——优先用人工 answer，否则沿用事件里的结果。
    - **第 275 行** `rt.last_value = answer if answer is not None else _send_value(last)`
  - **第 276-277 行** running 分支：`rt.last_value = _send_value(last)`。
  - **第 278-279 行** else（done/error）：`rt.last_value = None`，无需再回填。
  - **这就是 resume 的机制**：重建一个全新 generator，用历史事件「重演」到挂起点，再把人工答案作为最后回填值——Program 是纯函数，重演轨迹与首次执行一致。

### 第 281-294 行：_bind（给上下文注入 invoke 回调）

- **第 281 行** `@staticmethod`

- **第 282 行** `def _bind(ctx: DynamicValue, runner: Runner) -> DynamicValue:`

- **第 283-290 行** docstring：给上下文注入 invoke 回调，让增强链中心能调用 Port call。

- **第 291 行** `ctx.invoke = lambda effect, c: runner._call(effect, c)`
  **闭包绑定**：增强链中心（洋葱模型最内层）通过 `ctx.invoke` 调到真正的 Port call。`ctx` 用 DynamicValue 而非 RuntimeCtx——允许上层传字段超集的自定义上下文。

- **第 292 行** 注释：子 Agent 继承父 Runner 的取消令牌（递归传播）。

- **第 293 行** `ctx.cancellation = runner._cancellation`：把 frozen token 注入上下文，**子 Agent 通过 ctx 读到同一令牌**（取消递归传播的另一条路径）。

- **第 294 行** `return ctx`：原地设置后返回同一对象（所以 RuntimeCtx 非 frozen）。

### 第 296-351 行：_execute（执行单个 Effect）

- **第 296-298 行** 函数签名：`async def _execute(self, effect, ctx, session) -> tuple[Result, tuple[Event, ...]]:`。

- **第 299-311 行** docstring：执行单个 Effect，返回 Result 与子会话事件。
  - 分派（tagged union）：Spawn 走递归子 Agent（不进 AOP）；Invoke 走 call 槽（装配层已包入 AOP）；Suspend/Terminate 走内核快路径（由 step 在批次层面处理，不进 _execute）。
  - 返回：非 Spawn 时事件元组为空。

- **第 312-313 行** `if isinstance(effect, Spawn): return await self._child(effect, session)`
  Spawn 走递归分支。

- **第 314-316 行** tool 相位判定：
  - **第 314-315 行** 注释：Invoke(port=tool) 发 tool 相位通知（R5 发 tool 系列，规格 P2）；内核只认 notify 回调与 PortName 枚举，不 import hooks。
  - **第 316 行** `is_tool = isinstance(effect, Invoke) and effect.port == PortName.TOOL.value`

- **第 317-320 行** BEFORE_TOOL 通知：
  - **第 318-320 行** `self._notify(_Phase.BEFORE_TOOL.value, {"session_id": ..., "port": PortName.TOOL.value})`

- **第 321 行** `try:`

- **第 322 行** `result = await _maybe_await(self._call(effect, ctx))`
  **唯一实际调用 Port 的地方**。call 由构造注入，内部可含 AOP 增强链；_maybe_await 兼容同步/异步。

- **第 323-330 行** AgentKitError 分支：
  - **第 323 行** `except AgentKitError as exc:`
  - **第 324 行** 注释：框架异常，用自身 code 折叠为失败信封。
  - **第 325-329 行** 若 is_tool 发 TOOL_ERROR 通知（带 exc.code）。
  - **第 330 行** `return Result.failure(exc.code, str(exc)), ()`
    框架异常的 code 稳定、不可改（errors.py 的 code），直接用作 Result.error.code。

- **第 331-340 行** 未预期异常分支：
  - **第 331 行** `except Exception as exc:  # 异常绝不抛穿运行时  # noqa: BLE001`
  - **第 332 行** 注释：未预期异常统一折叠，保留异常类型名便于诊断。
  - **第 333-337 行** 若 is_tool 发 TOOL_ERROR 通知（错误码用 `PORT_CRASHED`）。
  - **第 338-340 行** `return Result.failure(_ErrCode.PORT_CRASHED.value, f"{type(exc).__name__}: {exc}"), ()`
    保留异常类型名 + 消息。

- **第 341-350 行** 调用成功后的 tool 相位通知：
  - **第 342-345 行** result.ok → AFTER_TOOL。
  - **第 346-350 行** 否则 → TOOL_ERROR（错误码取 `result.error.code`，缺失用 UNKNOWN）。

- **第 351 行** `return result, ()`：返回 Result + 空事件元组。

**_execute 的错误语义总结**：所有异常都折叠成 `Result.failure(code, msg)`，以数据形态在链路流动，不抛穿运行时。这正是 errors.py docstring 说的「让错误以数据形态在链路里流动」。

### 第 353-384 行：_child（递归子 Agent）

- **第 353-355 行** 函数签名：`async def _child(self, effect: Spawn, session: Session) -> tuple[Result, tuple[Event, ...]]:`。

- **第 356-362 行** docstring：Spawn Effect 递归创建子 Session，复用同一解释器与注入函数。返回「以子 run 的 output 为 data 的 Result」+ 子会话全部事件。

- **第 365 行** `self._notify(_Phase.CHILD_SPAWN.value, {"session_id": session.session_id})`
  通知：子 Agent 创建。

- **第 366-370 行** program_factory 校验：
  - **第 366 行** `factory = effect.program_factory`
  - **第 367-370 行** `if factory is None: return Result.failure(_ErrCode.CHILD_PROGRAM_MISSING.value, "spawn needs program_factory"), ()`
    **没有工厂就无法重建子 Program**，直接失败信封（如从旧快照恢复的 Spawn 没带工厂）。

- **第 371 行** `child_state: AgentState = effect.state or session.state`
  子初始状态：Effect 指定优先，否则继承父会话 state。

- **第 372 行** `child_state = child_state.with_(budgets=child_state.budgets.consume(hops=1))`
  **子 Agent 深度 +1**。预算 hops 维度在此硬扣——不这么做，递归 Spawn 无限深也不会触发 hops 上限。

- **第 373-380 行** 子会话 id 生成：
  - **第 373 行** 注释：用单调递增序号作子会话 id 后缀，保证同批并发 Spawn 的 id 唯一。
  - **第 374 行** `seq = self._child_seq.get(session.session_id, 0)`：读当前序号。
  - **第 375 行** `self._child_seq[session.session_id] = seq + 1`：递增（无 await 区间，并发安全）。
  - **第 376-380 行** `child = Session(session_id=f"{session.session_id}{_CHILD_SESSION_SEPARATOR}{seq}", program_factory=factory, state=child_state)`
    id 格式（设计决策 14）：`<父>:child:<序号>`。

- **第 381 行** `res = await self.run(child)`
  **递归跑完整子会话**（含其自身的 Spawn）。复用同一 Runner，所以取消/预算/通知机制一致。

- **第 383 行** `self._notify(_Phase.CHILD_DONE.value, {"session_id": session.session_id, "status": res.status})`
  通知：子 Agent 完成。

- **第 384 行** `return Result.success(res.output, usage=Usage()), tuple(res.events)`
  子 run 的 output 作 Result.data，usage 用空 Usage（子会话用量已在父 fold 逐 Effect 扣过），子事件返回给父 step 挂到父日志。

### 第 386-395 行：cancel（运行期取消）

- **第 386 行** `def cancel(self, reason: str) -> None:`

- **第 387-394 行** docstring：**运行中请求取消：下一 step 检查到后停止执行任何 Effect**。与 frozen CancellationToken 不冲突：token 对外仍不可变，本方法只更新 Runner 内部 holder。子 Agent 共享同一 Runner 树时经 holder 递归可见。

- **第 395 行** `self._cancel_holder.cancel(reason)`
  原地置位 holder（设计决策 6 的兑现）。下一 step（第 406 行）读到即停。

### 第 397-579 行：step（单步执行——内核主流程）

这是整个内核最核心的方法。逐段拆解。

- **第 397 行** `async def step(self, session: Session) -> StepReport:`

- **第 398-404 行** docstring：执行一个 step（供调试 / HITL 单步使用）；session 的 state/events 会被就地推进。

#### 取消检查（第 405-426 行）

- **第 405 行** 注释：取消检查——holder 已置位则本步不执行任何 Effect，写 cancelled 事件并报告。

- **第 406 行** `if self._cancel_holder.cancelled:`
  每步开头读 holder（设计决策 6 的兑现点）。

- **第 407 行** `reason = self._cancel_holder.reason`：取取消原因。

- **第 408-417 行** 追加 cancelled 事件：
  - **第 409 行** `seq=len(session.events) + 1`：序号。
  - **第 411 行** `step=0`：取消事件不占逻辑步号。
  - **第 412 行** `state_snapshot=session.state`：当前状态。
  - **第 413-414 行** effects/results 为空——取消不执行任何 Effect。
  - **第 415 行** `status=_StepStatus.CANCELLED.value`
  - **第 416 行** `reason=reason`
  - **第 417 行** `))` 闭合。

- **第 418 行** `self._notify(_Phase.CANCELLED.value, {"session_id": ..., "reason": reason})`：发取消通知。

- **第 419 行** `self._reasons[session.session_id] = reason`：记录原因。

- **第 420-426 行** `return StepReport(step=0, state=session.state, status=CANCELLED, reason=reason, events=tuple(session.events))`
  立即返回，主流程不执行。

#### 执行态初始化与局部变量（第 428-448 行）

- **第 428 行** `try:`

- **第 429 行** `rt = self._runtime(session)`：取或懒建执行态（首次会回放）。

- **第 430-439 行** 构造期异常兜底：
  - **第 430 行** `except (ProgramInvalid, ProgramCrashed) as exc:`
  - **第 431 行** 注释：Program 构造即失败，直接产出 error 报告。
  - **第 432-439 行** 返回 `StepReport(step=0, state=..., status=ERROR, error=exc.code, reason=str(exc), events=...)`。

- **第 440 行** `step_no = rt.step`：本步逻辑时钟。

- **第 441 行** `rt.step += 1`
  **游标前进（即使本步失败也推进，保证 trace_id 单调）**。不这么做，失败步与下一步 trace_id 会重复，链路追踪错乱。

- **第 442 行** `state = session.state`：当前状态。

- **第 443-448 行** 局部变量初始化：`effects=()`、`results=()`、`batch=None`、`status=RUNNING`、`reason=""`、`error=None`。

#### 0. 预算预检（第 450-460 行）

- **第 450 行** 注释：`# 0. 预算预检：超限强制 Terminate（内核硬强制，Program 无法绕过）`
  设计决策 8。

- **第 451 行** `exceeded = state.budgets.exceeded()`：纯函数调用，返回首个超限维度名或 None。

- **第 452-457 行** 超限分支：
  - **第 453 行** `reason = f"{BudgetExceeded.code}:{exceeded}"`：原因码 = 异常 code + 维度名（如 `BUDGET_EXCEEDED:turns`）。
  - **第 454 行** `effects = (Terminate(reason=reason),)`：**强制合成 Terminate**，Program 没有机会继续 yield。
  - **第 455 行** `results = (Result.success(None),)`
  - **第 456 行** `status = _StepStatus.DONE.value`：直接转终态。
  - **第 457 行** 发 BUDGET_CHECK 通知（带 exceeded 维度名）。
  - **Program 完全旁路与无法绕过**：超限发生在推进解释器之前，Program 根本不会被调用。

- **第 458-460 行** 未超限分支：
  - **第 458 行** `else:`
  - **第 459 行** 注释：预算检查通过（纯函数判定，读 state.budgets）。
  - **第 460 行** `self._notify(_Phase.BUDGET_CHECK.value, {"session_id": ..., "exceeded": ""})`

#### 1. 推进解释器（第 461-471 行）

- **第 461 行** 注释：`# 1. 推进解释器，拿到下一批 Effect 或 DONE。`

- **第 462 行** `try:`

- **第 463 行** `batch = rt.interpreter.send(rt.last_value)`
  解释器推进一步（首次 next，其后 send）。

- **第 464-466 行** 契约异常：
  - **第 464 行** `except (ProgramInvalid, ProgramCrashed) as exc:`
  - **第 465 行** `status, error, reason = ERROR, exc.code, str(exc)`
  - **第 466 行** `batch = EffectBatch(())`：空批次，保证后续字段有值（后面会读 `batch.batched`）。

- **第 467-471 行** 其他异常（pragma no cover）：
  - **第 467 行** `except Exception as exc:  # pragma: no cover  # noqa: BLE001`
  - **第 468-470 行** `status, error, reason = ERROR, PORT... 实为 PROGRAM_CRASHED, str(exc)`
  - **第 471 行** `batch = EffectBatch(())`

#### DONE / 批次分派（第 473-532 行）

- **第 473-477 行** DONE 分支：
  - **第 473 行** `if status == RUNNING and batch is DONE:`
  - **第 474 行** 注释：Program 已 return：合成一个 Terminate 收尾。
  - **第 475 行** `status, reason = DONE, _REASON_PROGRAM_RETURNED`
  - **第 476 行** `effects = (Terminate(reason=reason),)`
  - **第 477 行** `results = (Result.success(None),)`
  - **为什么要合成 Terminate**：让后续 fold / 事件 / checkpoint 路径统一——不需要为「Program 自然结束」单独走一套收尾逻辑。

- **第 478-485 行** 常规推进 + trace_id 注入：
  - **第 478 行** `elif status == RUNNING:`
  - **第 479 行** `assert isinstance(batch, EffectBatch)`：断言类型（前面已保证非 DONE 必是 EffectBatch）。
  - **第 480-481 行** 注释：给每个 Effect 打 trace_id：`<session>:<step>:<index>`；tagged union 各成员均带 trace_id 字段，可统一 replace。
  - **第 482-485 行** `effects = tuple(replace(e, trace_id=f"{session.session_id}:{step_no:04d}:{i:02d}") for i, e in enumerate(batch.effects))`
    - **trace_id 格式（设计决策 10）**：`<会话id>:<4位步号>:<2位序号>`。零填充保证字典序 = 时间序，便于日志排序。
    - `replace` 统一作用于四成员——因为它们都有 trace_id 字段（types.py 第 501/531/544/557 行）。

- **第 486-490 行** 并行度硬检查：
  - **第 486 行** `if state.budgets.check_parallel(len(effects)):`
    并行度是内核硬预算（设计决策 8 的一部分）。
  - **第 487 行** `reason = f"{BudgetExceeded.code}:{_REASON_PARALLEL_CHILDREN}"`
  - **第 488-489 行** 强制 Terminate + success result。
  - **第 490 行** `status = DONE`
  - **不这么检查**：Program 一次 yield 1000 个 Effect 会打爆下游 Port。

- **第 491-499 行** Terminate 整批快路径：
  - **第 491 行** `elif effects and all(isinstance(e, Terminate) for e in effects):`
    **整批都是 Terminate 才走快路径**（设计决策 9）。
  - **第 492 行** 注释：Program 主动 Terminate：整批都是才走快路径，不调用 Port。
  - **第 493 行** `results = tuple(Result.success(None) for _ in effects)`
  - **第 494-495 行** `first_term = effects[0]` + assert：取首个 Terminate 的原因。
  - **第 496-499 行** `status, reason = DONE, str(first_term.reason or _REASON_TERMINATED)`
    原因用 Terminate.reason，空则回退稳定码 `terminated`。
  - **为什么必须「整批都是」**：混合批（含 Invoke + Terminate）若有可执行 Effect，不能被 Terminate 吞掉——否则副作用丢失。

- **第 500-509 行** Suspend 整批快路径：
  - **第 500 行** `elif effects and all(isinstance(e, Suspend) for e in effects):`
    **整批都是 Suspend 才挂起**（设计决策 9）。
  - **第 501 行** 注释：混合批含可执行 Effect 不能被吞。
  - **第 502 行** `results = tuple(Result.success(None) for _ in effects)`
  - **第 503 行** `status, reason = SUSPENDED, _REASON_WAIT_HUMAN`
  - **第 504-505 行** `first_suspend = effects[0]` + assert。
  - **第 506-509 行** 发 WAIT_HUMAN 通知（带 question）。

- **第 510-532 行** 常规批（并发执行 Invoke）：
  - **第 510 行** 注释：`# 常规批：为每个 Invoke 构造上下文并绑定 invoke，并发执行。`
  - **第 511-514 行** 构造上下文：
    - **第 513 行** `self._bind(self._ctx_factory(e, session), self) for e in effects`
    - 每个 Effect 一个上下文，经 `_bind` 注入 invoke 与 cancellation。
  - **第 515-517 行** 并发执行：
    - **第 515-517 行** `executed = await asyncio.gather(*(self._execute(e, c, session) for e, c in zip(effects, ctxs)))`
    - **`asyncio.gather` 是唯一并发点**——一批 Effect 并行执行，各自返回 (Result, 子事件)。
  - **第 518 行** `results = tuple(item[0] for item in executed)`：取出所有 Result。
  - **第 519-522 行** 子会话事件挂父日志：
    - **第 519 行** 注释：子 Agent 的轨迹对父可见。
    - **第 520 行** `child_events = tuple(ev for item in executed for ev in item[1])`：汇总所有子事件。
    - **第 521-522 行** `for ev in child_events: session.events.append(ev)`：挂到父日志（先于本步事件追加）。
  - **第 523-532 行** 回填值计算：
    - **第 523 行** 注释：batch 用 list，单个取 data（失败取 Result 本身）。
    - **第 524-532 行** `rt.last_value = (...)`:
      - **第 525-526 行** `[r.data if r.ok else r for r in results] if batch.batched`：批量回填 list，失败项保留 Result 本体。
      - **第 527-531 行** 单个：`results[0].data if results and results[0].ok else (results[0] if results else None)`——**失败回填 Result 本体**（设计决策 7）。

#### 2. fold 与事件落盘（第 534-579 行）

- **第 534 行** 注释：`# 2. 纯函数折叠：本步的 Effect/Result → 新 State。`

- **第 535 行** `new_state = fold(state, effects, results, reason)`
  唯一状态变更入口。

- **第 536 行** `session.state = new_state`：就地更新会话状态。

- **第 537-538 行** 发 STATE_FOLD 通知（带 step_no）。

- **第 539-549 行** 汇总本步 usage：
  - **第 540-543 行** `cost = sum(float(field(r.usage, _USAGE_COST_FIELD, 0.0) or 0.0) for r in results if r.usage)`
  - **第 544-549 行** `tokens = sum((field(r.usage, PROMPT, 0) or 0) + (field(r.usage, COMPLETION, 0) or 0) for r in results if r.usage)`
  - 用 `field()` 读动态 usage（可能是 dict/dataclass/pydantic），具名常量防拼错。

- **第 550-563 行** 构造 Event：
  - **第 551 行** `seq=len(session.events)`：逻辑序号 = 当前事件数（append-only）。**注意**：前面已追加子会话事件，所以这里的 seq 会跳过子事件占的号——这是刻意的，父子事件同处一个日志流。
  - **第 553 行** `step=step_no`
  - **第 554 行** `state_snapshot=new_state`：折叠后状态。
  - **第 555-556 行** effects/results。
  - **第 557 行** `batched=bool(batch.batched) if isinstance(batch, EffectBatch) else False`：仅 EffectBatch 有 batched 属性；DONE 场景为 False。
  - **第 558 行** `ts=step_no`：**逻辑时钟（无墙钟，保证确定性）**（设计决策 11）。
  - **第 559-562 行** cost/tokens/status/reason。
  - **第 563 行** `)` 闭合。

- **第 564 行** `session.events.append(event)`：追加事件（含前面已追加的子会话事件）。

- **第 565 行** `self._reasons[session.session_id] = reason`：记录原因。

- **第 566 行** `self._errors[session.session_id] = error`：记录错误码。

- **第 567-569 行** checkpoint 落盘：
  - **第 567 行** 注释：终态总落快照；运行中按 checkpoint_every 节流。
  - **第 568 行** `if status != RUNNING or rt.step % self._checkpoint_every == 0:`
    - 终态（done/suspended/error/cancelled）无条件落。
    - 运行中按节流间隔落（checkpoint_every 最小为 1，即每步都落）。
  - **第 569 行** `await self._store.save(session.session_id, checkpoint(session))`
    设计决策 12。

- **第 570-579 行** 返回 StepReport：step/state/effects/results/status/reason/error/events 八字段。

### 第 581-602 行：run（跑到终态）

- **第 581 行** `async def run(self, session: Session) -> RunResult:`

- **第 582-588 行** docstring：跑到 done / suspended / error；**已是终态的会话直接返回，不重复执行**。

- **第 589-594 行** 终态幂等检查（设计决策 13）：
  - **第 589-593 行** `if session.events and session.events[-1].status in (DONE, ERROR, SUSPENDED):`
  - **第 594 行** `return self._result(session)`：直接返回，不重跑。

- **第 595 行** `self._notify(_Phase.SESSION_START.value, {"session_id": ...})`：发会话开始通知。

- **第 596-597 行** user_prompt 通知：
  - **第 596 行** 注释：会话启动即收到用户提示（Program 首轮的输入相位）。
  - **第 597 行** `self._notify(_Phase.USER_PROMPT.value, {"session_id": ...})`

- **第 598-602 行** 主循环与异常兜底：
  - **第 599 行** `return await self._loop(session)`
  - **第 600-602 行** `except (ProgramInvalid, ProgramCrashed) as exc: return self._program_error(session, exc)`
    构造期 Program 违规不抛穿，折叠为 error RunResult（与 step 同口径）。

### 第 604-623 行：_program_error（构造期异常兜底）

- **第 604 行** `def _program_error(self, session: Session, exc: AgentKitError) -> RunResult:`

- **第 605-610 行** docstring：构造期 Program 异常的兜底——写 error 事件 + 错误码，返回 error RunResult。

- **第 611-620 行** 追加 error 事件：
  - **第 612 行** `seq=len(session.events) + 1`
  - **第 614 行** `step=0`
  - **第 618 行** `status=ERROR`
  - **第 619 行** `reason=str(exc)`

- **第 621 行** `self._errors[session.session_id] = exc.code`：记录错误码。

- **第 622 行** `self._reasons[session.session_id] = str(exc)`：记录原因。

- **第 623 行** `return self._result(session)`

### 第 625-645 行：resume（从挂起断点继续）

- **第 625 行** `async def resume(self, session: Session, answer: DynamicValue) -> RunResult:`

- **第 626-633 行** docstring：从挂起断点继续——答案 send 回 Program，**事件日志不动**。

- **第 634-640 行** 重建 + 回放：
  - **第 634 行** 注释：重建解释器并按事件回放，把 answer 作为末事件（suspended）的回填值。
  - **第 636 行** `rt = _Runtime(InterpreterImpl(session.program_factory()))`：全新解释器。
  - **第 637-638 行** 构造异常 → `_program_error`。
  - **第 639 行** `self._runtimes[session.session_id] = rt`：替换执行态。
  - **第 640 行** `self._replay(session, rt, answer=answer)`：回放到断点并注入答案（设计决策 15）。

- **第 641 行** `self._notify(_Phase.RESUMED.value, {"session_id": ..., "answer": answer})`：发恢复通知。

- **第 642-645 行** 续跑：
  - **第 643 行** `return await self._loop(session, initial_value=answer)`
  - **第 644-645 行** 异常兜底。

### 第 647-671 行：_loop（反复 step 直到终态）

- **第 647-649 行** 函数签名：`async def _loop(self, session, initial_value=None) -> RunResult:`。

- **第 650-656 行** docstring：反复 step 直到终态或触碰 max_steps 硬上限。

- **第 658 行** `rt = self._runtime(session)`：取执行态。

- **第 659-660 行** `if initial_value is not None: rt.last_value = initial_value`：resume 场景覆盖首步回填值。

- **第 661 行** `for _ in range(self._max_steps):`
  步数硬上限兜底——防止 Program 死循环（设计决策 8 的兜底）。

- **第 662-664 行** `report = await self.step(session)` + `if report.status != RUNNING: break`
  逐步执行，非 running 即停。

- **第 665 行** `result = self._result(session)`：汇总终态。

- **第 666-670 行** SESSION_END 通知：
  - **第 666 行** 注释：终态才发；挂起不计结束，resume 后会再次 run。
  - **第 667 行** `if result.status in (DONE, ERROR):`
  - **第 668-670 行** 发通知（带 status）。

- **第 671 行** `return result`

### 第 673-691 行：_result（从终态汇总 RunResult）

- **第 673 行** `def _result(self, session: Session) -> RunResult:`

- **第 674-679 行** docstring：状态取自末事件，原因/错误取自 Runner 记录。

- **第 681 行** `last = session.events[-1] if session.events else None`

- **第 682 行** `status = last.status if last else _StepStatus.RUNNING.value`
  无事件视为仍在运行。

- **第 683-691 行** `return RunResult(...)`：
  - **第 688 行** `output=session.state.output`：终态输出（Terminate 写入）。
  - **第 689 行** `reason=self._reasons.get(session.session_id, "")`
  - **第 690 行** `error=self._errors.get(session.session_id)`

### 第 693-702 行：replay（事件溯源重放）

- **第 693 行** `def replay(self, session: Session, initial_state: AgentState) -> AgentState:`

- **第 694-700 行** docstring：从初始 State 逐步 fold 得到终态。

- **第 702 行** `return replay_events(session.events, initial_state)`
  委托 fold 模块的 `replay_events`（第 208 行）——纯函数重放，不执行任何 IO。

### 第 704-718 行：load（从 checkpoint 恢复 Session）

- **第 704 行** `async def load(self, session_id: str, program_factory: Callable[[], Program]) -> Session:`

- **第 705-714 行** docstring：从 checkpoint 恢复 Session；异常 `PortMissing`（该会话无 checkpoint）。

- **第 715 行** `snap = await self._store.load(session_id)`：异步读取快照。

- **第 716-717 行** `if snap is None: raise PortMissing(f"no checkpoint for session {session_id}")`
  无快照抛框架异常（有稳定 code `PORT_MISSING`）。

- **第 718 行** `return restore_checkpoint(snap, program_factory)`
  重建 Session（types.py 第 926 行）。之后 `_runtime` 懒建执行态时会 `_replay` 到断点，完成恢复闭环。

---

## 5. errors.py 逐行讲解（46 行）

errors.py 是**框架级异常分类**。核心纪律：异常只做「分类 + 稳定错误码」，不承载恢复策略；一律进 StepReport，不抛穿运行时。

### 第 1-8 行：模块 docstring

- **第 1 行** `"""R1 框架级异常分类：一律进 StepReport，不抛穿运行时。`
  一句话定位：异常是数据，不是控制流。

- **第 2 行** 空行。

- **第 3-4 行** 异常如何流动：
  - **第 3 行** `每个异常携带稳定 code，Runner 捕获后折叠为 Result.failure(code, ...)，`
  - **第 4 行** `让错误以数据形态在链路里流动，而不是以栈展开打断执行。`
  - 对应 runner.py 第 323-340 行的捕获折叠逻辑。

- **第 5 行** 空行。

- **第 6-7 行** 设计约束（R1 §4）：
  - **第 6 行** `设计约束（R1 §4）：异常只做"分类 + 稳定错误码"，不承载恢复策略；`
  - **第 7 行** `重试/降级由上层 R3 依据 code 决定，内核不因异常中断事件溯源。`
  - 内核只给 code，重试策略是上层的事。这就是「稳定原因码/稳定错误码」纪律。

- **第 8 行** `"""` docstring 结束。

### 第 9 行：imports

- **第 9 行** `from __future__ import annotations` 延迟注解。这个文件其实没有复杂注解，但保持全模块一致。

### 第 12-16 行：AgentKitError（基类）

- **第 12 行** `class AgentKitError(Exception):`
  所有框架异常的基类。Runner 的 `except AgentKitError`（runner.py 第 323 行）就靠它统一捕获。

- **第 13 行** docstring：基类说明——code 是进信封的错误码。

- **第 15 行** 注释：`# code 写入 Result.error.code，是上层重试/降级的判定依据，一旦发布不可更改。`
  **稳定码纪律**：code 是契约，发布后不能改，否则上层决策逻辑失效。

- **第 16 行** `code = "AGENTKIT_ERROR"`：基类默认 code。子类覆盖。

### 第 19-22 行：BudgetExceeded（预算超限）

- **第 19 行** `class BudgetExceeded(AgentKitError):`

- **第 20 行** docstring：预算超限；由 Runner 预算预检触发。

- **第 22 行** `code = "BUDGET_EXCEEDED"  # 预算硬上限触顶，step 强制转 done 而不继续调用 Port`
  注释点明行为：触顶后 step 强制转 done（runner.py 第 452-457 行），不再调用 Port。

### 第 25-28 行：ProgramInvalid（Program 契约违规）

- **第 25 行** `class ProgramInvalid(AgentKitError):`

- **第 26 行** docstring：Program 契约违规——非 generator 或 yield 非 Effect 对象。

- **第 28 行** `code = "PROGRAM_INVALID"  # Program 未遵守 generator / yield Effect 的契约`

### 第 31-34 行：PortMissing（port 未注册）

- **第 31 行** `class PortMissing(AgentKitError):`

- **第 32 行** docstring：请求的 port 未注册；装配期或运行期 resolve 失败。

- **第 34 行** `code = "PORT_MISSING"  # 装配期或运行期找不到对应 port 适配器`
  另在 runner.py 第 717 行复用——`load()` 找不到 checkpoint 也抛 PortMissing。

### 第 37-40 行：ProgramCrashed（Program 内部崩溃）

- **第 37 行** `class ProgramCrashed(AgentKitError):`

- **第 38 行** docstring：Program 内部抛未预期异常；解释器包装原始信息。

- **第 40 行** `code = "PROGRAM_CRASHED"  # 解释器捕获到 Program 内部未预期异常`
  对应 interpreter.py 第 144 行的包装。

### 第 43-46 行：ChainAborted（链路判决中止）

- **第 43 行** `class ChainAborted(AgentKitError):`

- **第 44 行** docstring：链路判决 abort，终止整个 run。

- **第 46 行** `code = "CHAIN_ABORTED"  # hook 返回 abort 判决，终止整个 run`
  由上层装配的 AOP/hook 链使用——内核定义了这个稳定码，但触发点在装配层。

**五个异常码一览**：`AGENTKIT_ERROR`（基类）/ `BUDGET_EXCEEDED` / `PROGRAM_INVALID` / `PORT_MISSING` / `PROGRAM_CRASHED` / `CHAIN_ABORTED`。全部与 runner.py 的 `_ErrCode` 和 fold.py 的失败信封对齐。

---

## 6. __init__.py 逐行讲解（33 行）

__init__.py 是**包级 API 收敛**：外部只需 `from runtime import X`，不必知道 X 在哪个文件。

### 第 1-4 行：包 docstring

- **第 1 行** `"""R1 核心运行时：Effect 解释器 + 唯一执行循环 + 预算 + 事件溯源。`
  一句话概括整个包。

- **第 2 行** 空行。

- **第 3 行** `公共 API 见 docs/02-模块详设/Agent/R1-核心运行时.md §4。`
  指路详设文档。

- **第 4 行** `"""` docstring 结束。

### 第 5 行：imports

- **第 5 行** `from __future__ import annotations` 延迟注解。

### 第 7-10 行：from .errors import（6 个异常）

- **第 7-10 行** 导入 `AgentKitError`、`BudgetExceeded`、`ChainAborted`、`PortMissing`、`ProgramCrashed`、`ProgramInvalid` 六个异常类。

### 第 11 行：from .fold import（4 个函数）

- **第 11 行** `from .fold import fold, replay_events, state_append_message, state_set`
  导入四个纯函数。

### 第 12 行：from .interpreter import（3 个符号）

- **第 12 行** `from .interpreter import EffectBatch, Interpreter, InterpreterImpl`
  导入批次类型与解释器协议/实现。

### 第 13-19 行：from .types import（30+ 符号）

- **第 13-19 行** 从 types 导入全部公共 DTO 与工具函数：
  - **第 14-15 行** `DONE`、`AgentState`、`BudgetDelta`、`Budgets`、`CallFn`、`CancellationToken`、`DispatchFn`、`Disposition`、`DoneSentinel`、`DynamicValue`、`Effect`、`EffectPayload`、`Event`、`EventStore`。
  - **第 16-17 行** `Invoke`、`JsonSnapshot`、`Message`、`Payload`、`PortError`、`PortName`、`Program`、`Result`、`RunResult`、`RuntimeCtx`、`Scratchpad`、`Session`、`Spawn`、`StateSnapshot`、`StepReport`、`Suspend`、`Terminate`、`Usage`。
  - **第 18-19 行** `canonical`、`checkpoint`、`disposition_of`、`fingerprint`、`restore_checkpoint`。

### 第 20 行：from .runner import（2 个符号）

- **第 20 行** `from .runner import InMemoryEventStore, Runner`
  导出 Runner 与默认存储。

### 第 22-33 行：__all__（公共导出面）

- **第 22 行** `__all__ = [` 开始。

- **第 23-32 行** 声明全部公共符号，与上面各 import 一一对应：
  - **第 23-24 行** 核心 DTO：DONE/AgentState/Budgets/Effect/EffectPayload/Event/EventStore/Message/PortError/Program/Result/Usage/RunResult/RuntimeCtx/Session/StepReport/JsonSnapshot/StateSnapshot。
  - **第 26-27 行** 预算/动态/Effect 成员：BudgetDelta/DynamicValue/Payload/Scratchpad/DoneSentinel/Disposition/PortName/Invoke/Spawn/Suspend/Terminate/disposition_of。
  - **第 28 行** 回调/工具：CallFn/CancellationToken/DispatchFn/canonical/checkpoint/fingerprint/restore_checkpoint。
  - **第 29 行** fold 函数：fold/replay_events/state_append_message/state_set。
  - **第 30 行** 解释器与 Runner：Interpreter/InterpreterImpl/EffectBatch/Runner/InMemoryEventStore。
  - **第 31-32 行** 六个异常类。
- **第 33 行** `]` 闭合。
- 作用：装配层/harness 统一从这里 import，包内部文件重组不影响外部调用。

---

## 7. 关键设计决策深挖（19 条）

上面逐行讲完了代码。这里把 0.读前GPS 列出的 19 条决策逐条「反面推演」一遍：每条说清**内核怎么落地**、**为什么必须这样**、**改成另一种写法会怎样**。这是把 2141 行源码读薄之后剩下的骨架。

### 决策 1：内核零领域策略

- **落地**：types.py 第 4 行不 import 任何实现；runner.py 第 204 行把 AOP 增强链塞进 `call` 槽；第 210 行 notify 只认 Callable。
- **为什么**：内核若 import 了 hooks/aop/pydantic/DB，就绑死具体实现，无法独立闭环、无法替换后端，依赖方向会从「装配层→内核」反转成双向。
- **反面**：一旦内核里出现 `from hooks import ...`，harness 改个相位名就要改内核；单元测试必须拉起整套 hook 系统才能跑一个 step。

### 决策 2：Effect tagged union 四成员

- **落地**：`Effect = Invoke | Spawn | Suspend | Terminate`（types.py:560），`disposition_of` 按类型判别（571-585），Runner `_execute` 按成员分派（runner.py:312）。
- **为什么**：四成员语义正交——Invoke 是「调外部能力」，Spawn 是「递归生娃」，Suspend 是「等人」，Terminate 是「收摊」。只有 Invoke 需要 AOP 与 Port，其余三个是内核快路径。
- **反面**：若用一个大类 + `kind` 字符串字段，类型检查器失去保护，漏写一个分支不会报错；序列化时字段与类型信息容易错位。

### 决策 3：唯一 IO 点在 Runner

- **落地**：只有 runner.py 有 `async`/`await`；interpreter.py 与 fold.py 全是同步纯函数。
- **为什么**：IO 集中在一处，才能统一做异常折叠、trace_id、checkpoint、取消、通知。纯函数部分可脱离环境测试。
- **反面**：若 fold 里也读时钟/发请求，事件回放就不可复现（同样事件两次 fold 结果不同），断点恢复彻底失效。

### 决策 4：Interpreter 纯推进 + batch 展开传幂等键

- **落地**：`send()` 只做 `next`/`send` 与 `_expand`（interpreter.py:137、148）；展开时把 `idempotency_key` 与 `trace_id` 透传每个子 Invoke（91-93）。
- **为什么**：Runner 不该理解 batch 语义——它拿到的永远是扁平、可并行的 Effect 批次，直接 gather 即可。
- **反面**：若幂等键不传，展开后的 N 个子 Invoke 各自没有幂等保护，R3 重试时会重复执行副作用（如重复扣款、重复发消息）——这是「副作用重试红线」被突破。

### 决策 5：fold 纯函数只增不减

- **落地**：`messages = list(state.messages)` 起步再 append（fold.py:151、165），最后 `tuple(messages)` 经 `with_` 返回新对象（195-205）。
- **为什么**：消息是审计轨迹，删改等于篡改历史；纯函数让 `replay_events` 能从事件日志无损重建状态。
- **反面**：若用 `state.messages.append(...)` 就地改，frozen dataclass 会挡住 tuple 的 append（属性不可变），但开发者可能改成 list 字段——那旧 State 会被新 State 共享引用污染，回放结果随调用历史漂移。

### 决策 6：取消用可变 holder（frozen token 无法感知）

- **落地**：对外 `CancellationToken` frozen，`cancel()` 返回新对象（types.py:641-643）；Runner 内部另设可变 `_CancellationHolder`（runner.py:110-124），`Runner.cancel()` 原地置位（395），step 每步开头读（406）。
- **为什么**：frozen 保证并发安全，但「返回新对象」意味着已持有旧引用的 Runner 永远感知不到取消。holder 是专为「运行期可变观测」开的窄口。
- **反面**：若直接让 token 可变（去掉 frozen），子 Agent 与父 Runner 共享同一对象时，一处 cancel 会就地改写所有人的状态——看似方便，实则破坏「显式传播」纪律且引入数据竞争。

### 决策 7：失败回填 Result 信封

- **落地**：`_send_value` 失败时取 `r`（Result 本体）而非 `r.data`（runner.py:170）；常规批回填同理（524-531）；fold 的失败消息带 `[code] message`（fold.py:110-112）。
- **为什么**：Program 里写的是 `resp = yield effect`，失败时必须让 `resp` 携带错误码与 retryable，上层才能据此降级或重试。
- **反面**：若失败回填 `data=None`，Program 拿到 None 无法区分「Port 返回空」与「Port 挂了」，错误被静默吞掉。

### 决策 8：预算硬检查无法绕过

- **落地**：`exceeded()` 在 step 第 451 行、推进解释器**之前**调用；超限合成 Terminate 转 done（452-457）；并行数单独 `check_parallel`（486）；`_loop` 的 `max_steps` 兜底（661）。
- **为什么**：预算是内核的硬边界，必须由内核执行，不能信任 Program 自觉。Program 甚至不会被调用——从源头断绝绕过。
- **反面**：若把预算检查交给 Program 自己写「if step > 30: return」，恶意或 bug Program 可轻易忽略，导致无限循环、成本失控。

### 决策 9：Terminate/Suspend 必须整批

- **落地**：`all(isinstance(e, Terminate) for e in effects)`（runner.py:491）与 `all(isinstance(e, Suspend) ...)`（500）都是「整批都是」才走快路径。
- **为什么**：tagged union 允许 Program 一次 yield 混合批次（如 `[Invoke(tool), Terminate]`）。若只要有一个 Terminate 就把整批当结束，那个 Invoke 的副作用就被吞了。
- **反面**：若改成 `any(...)`，混合批里的可执行 Effect 不执行，数据丢失且无法察觉——这是最隐蔽的一类 bug。

### 决策 10：trace_id 格式

- **落地**：`f"{session_id}:{step_no:04d}:{i:02d}"`（runner.py:483），四成员都带 trace_id 字段（types.py:501/531/544/557）。
- **为什么**：格式自带层级（会话/步/序号），零填充让字符串字典序等于时间序，日志排序无需再解析。
- **反面**：若 trace_id 是随机 UUID，无法从 id 看出发生顺序；若用裸整数，跨会话会撞号。

### 决策 11：逻辑时钟 ts=step 无墙钟

- **落地**：`ts=step_no`（runner.py:558），Event 字段 docstring 明说逻辑时钟（types.py:622）。
- **为什么**：确定性回放要求同样输入产生逐字节相同的事件日志。墙钟（datetime.now）每次不同，指纹永远对不上。
- **反面**：若 Event.ts 写墙钟，`fingerprint` 每次运行都变，checkpoint 门禁比对永远失败，事件溯源失去意义。真实时间由外壳在需要时注入，不进内核。

### 决策 12：checkpoint 终态总落

- **落地**：`if status != RUNNING or rt.step % checkpoint_every == 0`（runner.py:568）；终态无条件落，运行中按节流落。
- **为什么**：终态（done/suspended/error）是最需要持久化的点——崩溃恢复必须从终态或最近一步继续。运行中可节流以降 IO 成本。
- **反面**：若只在运行中按间隔落、终态不落，最后一步的 output 可能丢失，恢复后状态不完整。

### 决策 13：终态幂等

- **落地**：run() 开头检查末事件 status ∈ {done, error, suspended}，是则直接返回（runner.py:589-594）。
- **为什么**：防止重复 run 同一 Session 导致 Effect 重复执行（尤其带副作用的 Invoke）。
- **反面**：若无此检查，外部重试/重复调用会重放已完成会话，产生重复副作用。

### 决策 14：子会话 id 格式与序号

- **落地**：`f"{parent}{_CHILD_SESSION_SEPARATOR}{seq}"`（runner.py:377），分隔符 `:child:`（102），序号按父 session 独立递增（374-375）。
- **为什么**：同批并发 Spawn 必须拿到唯一 id；按父 session 独立计数避免全局锁；`asyncio` 单线程中「读 seq→写 seq+1」之间无 await，天然原子。
- **反面**：若用 `len(children)` 当 id，并发时两个 Spawn 读到相同长度，子会话 id 冲突，事件日志串台。

### 决策 15：resume 回放

- **落地**：`resume()` 重建 interpreter → `_replay(session, rt, answer=answer)` → `_loop(initial_value=answer)`（runner.py:634-643）；`_replay` 把 answer 作为末 suspended 事件的回填值（273-275）。
- **为什么**：Program 是纯函数，重演历史事件必然得到同样轨迹；从断点续跑无需重新执行已完成的 IO。
- **反面**：若能直接序列化 generator 并 pickle 恢复，看似省事，但 generator 不可序列化（含运行栈），这条路走不通——这正是 `Session.program_factory` 存在的原因（types.py:669）。

### 决策 16：13 个通知相位

- **落地**：`_Phase` 枚举 13 个相位（runner.py:75-92），docstring 明说「取值与 hooks.LifecyclePhase 一致，新增须同步两处」。
- **为什么**：内核要发生命周期事件给上层 hooks，但零依赖hooks 不能 import。用字符串枚举「具名对齐」是折中——内核认字符串，上层认枚举，值一致即可对接。
- **反面**：若内核直接 import hooks.LifecyclePhase，依赖反转；若两边各自硬编码字符串且不同步，hook 永远收不到通知。

### 决策 17：事件溯源

- **落地**：Event append-only（types.py:605），`replay_events` 逐步 fold（fold.py:208-223），`Runner.replay` 委托它（runner.py:693-702）。
- **为什么**：事件日志是「发生了什么」的唯一真相，State 只是它的折叠结果。有了日志，任意时刻状态都可重建，审计/回放/恢复统一。
- **反面**：若只存最终 State 不存事件，无法回答「为什么变成这样」，也无法做断点恢复。

### 决策 18：稳定原因码

- **落地**：`_REASON_*` 四个常量（runner.py:95-99）写进 Event.reason / RunResult.reason；错误码在 errors.py 中「一旦发布不可更改」（第 15 行）。
- **为什么**：原因是给程序读的契约，不是给人看的文案。上层按码做分支判断，改码等于破坏契约。
- **反面**：若 reason 写自由文本（如 `"程序结束啦"`），上层无法稳定匹配，文案一改逻辑就断。

### 决策 19：x-* 扩展键不进内核结构

- **落地**：`PortName` docstring 明说 x-* 只用于 payload 扩展键（types.py:133）；`EffectPayload.meta` 是透传容器（465）；`Invoke.with_payload` 允许合并 x-* 键（508-518）；`field()` 兼容三种形态读取（fold.py:34-49）。
- **为什么**：扩展性是必要的，但不能让扩展污染内核类型。约定 x-* 前缀作为「扩展命名空间」，内核原样透传、不解释、不提升为枚举字段。
- **反面**：若每来一个新需求就往 Effect 加一个具名字段，内核 DTO 会无限膨胀；若把 x-* 键当 Port 种类，闭集校验失效。

---

## 8. 逐行讲解后的整体心智图

```
                      ┌──────────────────────────────────────┐
                      │        装配层 / harness（上层）        │
                      │  call 槽包 AOP 链 · notify 挂 hooks    │
                      └──────────────┬───────────────────────┘
                                     │ 构造注入（依赖方向恒定向内）
                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│                            Runner（唯一 IO 点）                          │
│                                                                        │
│  run() ──► _loop() ──► step() ×N ──► done/suspended/error               │
│   │           │          │                                              │
│   │           │          ├─0 取消检查（_CancellationHolder 原地置位）      │
│   │           │          ├─1 预算预检 exceeded() → 超限强制 Terminate     │
│   │           │          ├─2 interpreter.send(last_value) → batch/DONE   │
│   │           │          ├─3 trace_id = <session>:<step>:<index>         │
│   │           │          ├─4 并行检查 → Terminate/Suspend 整批快路径      │
│   │           │          │   否则 asyncio.gather(_execute) 并发          │
│   │           │          │     ├─ Spawn → _child() 递归子会话            │
│   │           │          │     └─ Invoke → call() → Result（异常折叠）    │
│   │           │          ├─5 fold(state, effects, results) → 新 State    │
│   │           │          ├─6 Event(ts=step) append + 子事件挂父日志        │
│   │           │          └─7 checkpoint 落盘（终态总落）                  │
│   │           │                                                         │
│   │           └─ 每步：更新 _runtimes / _reasons / _errors / notify       │
│   ├─ resume(): 重建 interpreter → _replay(answer) → _loop                │
│   ├─ replay(): 委托 fold.replay_events（纯函数）                          │
│   └─ load():   store.load → restore_checkpoint                          │
└───────────────┬────────────────────────────────────────────────────────┘
                │ 调用
                ▼
┌───────────────────────────┐   ┌────────────────────────────────────┐
│ InterpreterImpl（零 IO）    │   │ fold（纯函数，只增不减）              │
│  · send: next/send 推进     │   │  · 生成消息(失败/llm/tool)           │
│  · _is_effect 判别四成员    │   │  · 特殊 kind → 托管字段              │
│  · _expand: batch 展开      │   │  · 累计 usage → consume 扣 6 维预算   │
│    (透传 trace_id/幂等键)   │   │  · with_() 产生全新 AgentState        │
│  · 非 Effect → ProgramInvalid│  │  · replay_events 逐步 fold           │
└───────────────┬───────────┘   └────────────────┬───────────────────┘
                │ 产出                              │ 产出
                ▼                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      types.py（唯一类型来源，零依赖）                     │
│  DynamicValue/Payload/Scratchpad（动态边界别名）                         │
│  Effect = Invoke | Spawn | Suspend | Terminate（tagged union 四成员）    │
│  Disposition/PortName（枚举闭集）                                        │
│  Result{ok,data,usage,error} · PortError{code,message,retryable}        │
│  Budgets{6 max + 6 used} · AgentState{托管字段 + scratch}                │
│  Event{seq,step,ts=step,...} · Session{program_factory,state,events}     │
│  _plain → canonical → fingerprint → checkpoint → restore_checkpoint     │
└────────────────────────────────────────────────────────────────────────┘
                ▲
                │ 定义稳定 code
         errors.py（6 个异常，code 稳定不可改）
                ▲
                │ 重导出
         __init__.py（包级 API 收敛）

关键数据流：
  Program yield Effect ─► Interpreter 展开 ─► Runner 打 trace_id + 并发 call
       ─► Result 信封 ─► fold 回填 State ─► Event(ts=step) ─► checkpoint
       ─► send(last_value) 回 Program（失败回填 Result 本体）

恢复闭环：checkpoint ─store─► load ─► restore_checkpoint ─► Session
       ─► _runtime 懒建 ─► _replay 重演到断点 ─► resume(answer)/run 续跑
```

---

## 9. 细节速记卡（逐行精华）

**内核定位**
- 内核零领域策略：types.py 不 import 任何实现；hooks/aop 由装配层包进 call 槽，内核只认 Callable（types.py:4、runner.py:9-10）。
- 唯一 IO 点在 Runner：只有 runner.py 有 async/await；interpreter/fold 纯函数（runner.py:1）。
- 依赖方向恒定：装配层 → 内核，不反向（types.py:5）。

**Effect 与分派**
- Effect tagged union 四成员：Invoke（走 Port/AOP）/ Spawn（递归子 Agent）/ Suspend（挂起）/ Terminate（结束）（types.py:560）。
- 只有 Invoke 走 AOP 链，其余三个是内核快路径（types.py:561）。
- disposition 判别标签在 _plain 里注入，且必须放在通用 dataclass 分支之前，否则类型信息丢失（types.py:765-770）。
- PortName 是闭集（llm/tool/skill/rag/memory），x-* 扩展键只在 payload 透传，不进内核结构（types.py:131-139）。

**状态与折叠**
- fold 纯函数只增不减：messages 从旧副本起步 append，绝不删改（fold.py:151、65）。
- 所有状态变更走 with_() → replace，产生新对象（types.py:409-429）。
- frozen dataclass + MappingProxyType 保证不可变（types.py:6、321-325）。
- 预算在 fold 统一扣减，Program 不许自己数数（fold.py:5、181-192）。
- 每个 Effect 至少扣 1 turn，防「不声明预算免费推进」（fold.py:96）。
- usage 的 token 与 cost 同口径扣进预算，否则 token 维形同虚设（fold.py:188-189）。
- 托管字段显式分派，不用 `**{key:value}` 动态拼装（编码规则 4）（types.py:394-404）。

**解释器**
- 首次 next() 启动，其后 send(value) 回填（interpreter.py:137）——generator 未启动不能 send 非 None。
- batch 展开透传 trace_id 与 idempotency_key，否则批量副作用重试红线失效（interpreter.py:91-93）。
- 空 calls 降级为单个非 batch Invoke，避免空批次（interpreter.py:82-83）。
- 空列表 yield 按契约违规处理，不能当 DONE（interpreter.py:153、164）。
- 未预期异常折叠为 ProgramCrashed，绝不裸抛穿（interpreter.py:142-144）。

**Runner 编排**
- 取消用可变 holder：frozen token 的 cancel() 返回新对象，Runner 感知不到，故 holder 原地置位（runner.py:110-124、386-395）。
- 失败回填 Result 本体而非 data=None，让 Program 读到错误码（runner.py:170、524-531）。
- 预算硬检查无法绕过：exceeded() 在推进解释器之前，超限强制 Terminate（runner.py:450-457）。
- Terminate/Suspend 必须整批才走快路径，混合批含可执行 Effect 不能被吞（runner.py:491、500）。
- trace_id 格式 `<session>:<step:04d>:<index:02d>`，零填充保证字典序=时间序（runner.py:483）。
- 逻辑时钟 ts=step，无墙钟，保证确定性回放（runner.py:558）。
- 并行 check_parallel 是内核硬预算，防一次 yield 打爆下游（runner.py:486）。
- 子会话 id `<parent>:child:<seq>`，seq 按父 session 独立递增，无 await 区间并发安全（runner.py:102、374-377）。
- 子 Agent hops+1 硬扣，防无限递归（runner.py:372）。
- 13 个通知相位具名对齐 hooks.LifecyclePhase，内核不 import hooks（runner.py:75-92）。
- 稳定原因码 program_returned/wait_human/terminated/parallel_children，发布不可改（runner.py:95-99）。
- 终态总落 checkpoint，运行中按 checkpoint_every 节流（最小 1）（runner.py:567-569）。

**事件与恢复**
- Event append-only，seq=len(events)，子事件先挂父日志（runner.py:551、564）。
- canonical：sort_keys + ensure_ascii=False + 紧凑分隔符，哈希确定（types.py:804）。
- fingerprint = SHA256(canonical(events))，防篡改/漂移（types.py:809）。
- checkpoint 五键：session_id/version/state/events/fingerprint，不含 program（types.py:820-826）。
- resume 回放：重建 generator → _replay 重演历史事件 → answer 作末 suspended 回填（runner.py:634-643）。
- Replayed/revive 让 `resp.tool_calls` 属性访问在反序列化后成立（types.py:829-850）。
- restore_state 兼容旧快照：托管字段缺失时从 scratch 回退并弹出（types.py:866-879）。
- 终态幂等：run() 遇 done/error/suspended 直接返回，不重复执行（runner.py:589-594）。

**稳定性与错误**
- 稳定错误码一律发布后不可改，进 Result.error.code 由 R3 决策重试/降级（errors.py:15、runner.py:330）。
- 所有异常折叠为 Result.failure，以数据形态流动，不抛穿运行时（runner.py:323-340）。
- 错误码六类：AGENTKIT_ERROR / BUDGET_EXCEEDED / PROGRAM_INVALID / PORT_MISSING / PROGRAM_CRASHED / CHAIN_ABORTED。

---

*这是 runtime 内核的逐行讲解版。与 skills 样板册同格式：「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」。覆盖 types.py / fold.py / interpreter.py / runner.py / errors.py / __init__.py 全部 6 个文件、2141 行源码。*
