# 代码精读 · compose 装配层（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/compose/` 全部 12 个文件、1928 行源码：ports.py（337）+ dto.py（277）+ program.py（485）+ registry.py（138）+ notify.py（212）+ call.py（118）+ guards.py（89）+ dispatch.py（45）+ context.py（73）+ tool_layer.py（48）+ failure.py（23）+ __init__.py（83）。
> 总入口见 [00](./00-总入口与全局链路.md)。

---

## 0. 读前GPS

### 模块定位

compose 是**框架真正核心**，一句话：**R1 内核一行不改，compose 负责把领域能力"装配"成 R1 认识的形式**。

依赖方向恒定单向：

```
工具/能力层 R3~R9  →  compose 装配层（本包）  →  runtime 内核 R1
```

- compose **只 import runtime**（`from runtime import ...`），绝不 import harness/model 等上层模块。
- 反向依赖一旦出现，R1 就会被领域概念污染，"内核领域无知"的设计直接崩塌。

### 核心类与概念

| 文件 | 核心产物 | 角色 |
| --- | --- | --- |
| program.py | 10 个 A 类组合算子 | Program → Program 的纯变换 |
| call.py | 4 个 call 装饰器 + build_call | CallFn → CallFn 的纯变换 |
| ports.py | 16 个 Protocol + PORT_PROTOCOLS | 外部能力的接口契约目录 |
| dto.py | ToolSpec/LLMRequest/Usage 等 | 跨模块数据契约 |
| registry.py | Registry | name → Provider 注册表 |
| notify.py | LifecycleBus + 16 相位 | "发生了什么"的通知总线 |
| guards.py / context.py | FaultRecord/RoutingTarget/CallState | 具名协作状态与 typed 目标 |
| dispatch.py | make_dispatch | 按 effect.port 分派到 Port |
| tool_layer.py | layer_program | 给能力层的 Program 外壳 |
| failure.py | FailureClass | 失败分类 DTO |

### 七种承载形态（全模块骨架）

整个 compose 把"框架能力"拆成七种互不混淆的承载形态，读每个文件时先问自己"它属于哪一种"：

1. **算子**（program.py）：装配期 Program → Program，运行期只发 Effect。
2. **守卫**（guards.py）：只承载 typed 数据结构与读写，不含拦截逻辑。
3. **call 装饰器**（call.py）：调用级 CallFn → CallFn，只覆盖 Invoke。
4. **notify**（notify.py）：16 相位只读通知，返回值不被消费、异常隔离。
5. **Port**（ports.py）：外部能力的协议契约，可插拔、可替换。
6. **组件**（registry.py、tool_layer.py）：注册表与 Program 外壳，提供装配基础设施。
7. **内核机制**（dispatch.py、context.py）：分派与调用上下文，直接对齐 R1 接口。

### 关键设计决策（读完全篇后回来看）

- **A 类算子装配期纯变换，不改原 Program**：每次 `program()` 产出新 generator，compile 幂等。
- **x-compose.* scratch 键传进度**：运行期状态放 scratch 的 `x-` 键，算子实例不持有进度；resume 靠 `program_factory` 重建（D-8）。
- **gate 是唯一能 yield Suspend 的 A 类算子**（D-6），其余算子只发 Invoke/Terminate/Effect。
- **par 一次 yield Effect 列表**走 R1 的列表并行批（interpreter + runner 的 gather），不是自己起线程。
- **majority 平票取首现**：`max` 的 key 里用 `-keys.index(k)` 做确定性 tie-break。
- **call 装饰器只覆盖 Invoke**（D-7）：非 Invoke 走 R1 快路径，不进装饰器链。
- **build_call 先注册在外层**：`base → decorators[0] → ...` 循环包裹，后装饰的在外层先执行。
- **layer_program 给 R1 领域无知 Program**：能力层只产 `Invoke + Terminate`，R1 零领域感知。
- **DangerLevel 权威定义在 compose**（dto.py）：跨层 DTO 的物理约束——两端都 import 同一个 Enum，避免字符串漂移。
- **16 相位 notify 只读投影 + 异常隔离**：Notice 用 MappingProxyType 冻结，订阅者异常绝不抛穿主流程。
- **dispatch 不构造 Invocation**：R1 明确不构造，dispatch 只做 `(effect, ctx) -> Result`。
- **PORT_PROTOCOLS 是协议目录**：装配期用它做 `isinstance` 履约校验，缺一方法即装配失败。
- **依赖方向恒定**：compose 只 import runtime，上层领域模块反向依赖 compose。

---

## 1. ports.py 逐行讲解（337 行）

这是 compose 最大的"目录型"文件：不写任何实现，只声明**外部能力的接口契约**。运行时只认 Protocol 不认实现，替换实现 = `registry.register(name, adapter)`。

### 第 1-34 行：模块 docstring

- **第 1 行** `"""Port 协议族：全部外部能力的接口契约（原 R2 hooks/protocols.py 归位组合层，规格 P2）。`
  一句话定位 + 沿革：原本在 R2 的 hooks/protocols.py，规格 P2 把它归位到组合层。归属 R2 契约。

- **第 3 行** `运行时只认 Protocol 不认实现；替换实现 = registry.register(name, adapter)。`
  点明"可插拔"机制：运行时面向协议编程，换实现不动内核代码，只在注册表里换一个 adapter。

- **第 4 行** `所有适配器公共契约：无会话状态（ctx 传递）、异常折叠为 Result、带 name。`
  三条铁律：① 适配器不持有会话状态（状态靠 ctx/Effect 传递，才能 resume）；② 异常折叠成 Result 不抛穿；③ 具名（可校验、可归因）。

- **第 6-10 行** 协议清单（13 个 Port）：LLM/Tool/Skill/RAG/Memory/Store/Sandbox/Audit/Eval/Telemetry/Human/MCP/EventStream。
  注意 docstring 说的是 **13 个主 Port**，但下文实际定义了 **16 个 Protocol**（多的 3 个是 AuditStore、DisclosureIndex、TelemetryExporter 这类"后端/扩展点"协议）。不是错误，是主协议与辅助协议分层罗列。

- **第 12-13 行** `约定：每个 Protocol 方法签名即契约（参数 + 返回值全部标注）；方法体统一 \`...\` 占位——这是 Python Protocol 接口语法（不是实现被省略）。`
  这是全文件最重要的阅读约定：看到 `...` 不要以为是"代码省略"，它就是接口声明。

- **第 14-30 行** 默认实现类归属表（Port → 默认类 → 归属模块）。
  逐行点出关键项：
  - 第 17 行 `LLMPort → OpenAICompatLLM → R4 src/ports/adapters/llm.py ✅已实现`。
  - 第 18-21 行 ToolPort/MCPPort/SkillPort/DisclosureIndex 都在 R5，✅已实现。
  - 第 22-30 行 RAG/Memory/Store/Sandbox/Audit/Human/Eval/Telemetry/EventStream 标注（待实现），由装配层在用户未注入时注册默认实例。
  - 这张表的价值：**协议与默认实现成对**（规则 9），每个可注入协议都有开箱默认，装配期不会"缺 provider"。

- **第 32 行** `"待实现"指对应模块尚未开发；装配层（R12/R13）在用户未注入时注册这些默认实例。`
  解释"待实现"的含义与兜底策略：默认实现由装配层补位，用户不感知。

- **第 33 行** `公共 API 见 docs/02-模块详设/Agent/R13-可插拔与协议契约.md。`
  指路到详设文档。

- **第 34 行** `"""` 模块 docstring 结束。

### 第 35-46 行：imports

- **第 35 行** `from __future__ import annotations`
  延迟求值注解，允许注解里引用尚未定义的类型（后面 DTO 类在下方 import，注解先写不报错）。

- **第 37 行** `from collections.abc import AsyncIterator, Callable`
  - `AsyncIterator`：`stream` / `subscribe` 的返回类型。
  - `Callable`：`register(fn)` 这类接受函数的签名。

- **第 38 行** `from contextlib import AbstractContextManager`
  `TelemetryPort.span` 返回上下文管理器的类型标注。

- **第 39 行** `from typing import Protocol, runtime_checkable`
  - `Protocol`：定义全部接口。
  - `runtime_checkable`：让 `isinstance(adapter, LLMPort)` 可用于装配期履约校验。

- **第 41 行** `from runtime import DynamicValue`
  **依赖方向的关键证据**：compose 只 import runtime。动态形态统一用 `DynamicValue` 具名别名，不散落裸 `Any`（规则 3）。

- **第 43-46 行** `from .dto import (DisclosurePacket, LLMRequest, LLMResponse, PortCtx, SkillManifest, ToolCall, ToolResult, ToolSpec)`
  从同包 dto.py 导入 8 个 DTO 作为方法签名类型。协议与 DTO 同属 R2 契约层，同包内互相引用无循环。

### 第 48-66 行：__all__ 导出清单

- **第 48-66 行** `__all__ = ["PORT_PROTOCOLS", "AuditPort", "AuditStore", "DisclosureIndex", ...]`
  导出 17 个符号：16 个协议类 + 1 个协议目录 `PORT_PROTOCOLS`。`__init__.py` 据此重导出。
  注意导出面比 docstring 的"13 个 Port"更全——辅助协议（AuditStore/DisclosureIndex/TelemetryExporter）也在其中。

### 第 69-89 行：LLMPort（模型补全协议）

- **第 69 行** `@runtime_checkable`
  装饰器：允许运行时 `isinstance()` 检查。代价：只能检查成员存在，不检查方法签名。

- **第 70 行** `class LLMPort(Protocol):`
  LLM 能力协议。基础 Port，**领域字段不进**（`last_usage` 归位到 R4 扩展协议）。

- **第 71-75 行** docstring：
  - 第 73-74 行点明 `last_usage`（流式 usage 结算）已归位到 `src/harness/model/ports.py` 的 `LLMPortWithUsage`（规格 P4）。
  - 这是**依赖方向的 discipline**：基础 Port 不反向依赖模型领域，领域扩展自己继承扩展。

- **第 76 行** `name: str`
  具名标识：装配期按 name 校验履约、进 trace 归因。

- **第 78 行** `async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:`
  一次性补全签名：入参 `LLMRequest` + `PortCtx`，返回结构化 `LLMResponse`（含 tool_calls 与 usage）。

- **第 79 行** docstring：`一次性补全：返回结构化响应（含 tool_calls 与 usage）；同时写 last_usage。`

- **第 80 行** `...` 协议方法占位。

- **第 82 行** `def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:`
  流式补全签名。**注意不是 `async def`**。

- **第 83-88 行** docstring 里的关键说明（第 85-87 行）：
  - 若写成 `async def -> AsyncIterator[str]` 会得到 "coroutine of iterator"，调用方要先 `await` 才能迭代，违反 `async for` 惯例。
  - PEP 525 标准签名就是 `def -> AsyncIterator`。
  - 取消由调用方 `aclose()` 传播。

- **第 89 行** `...` 协议方法占位。

### 第 93-108 行：ToolPort（工具协议）

- **第 93 行** `@runtime_checkable` 履约校验装饰器。

- **第 94 行** `class ToolPort(Protocol):` 工具能力：注册、目录查询、调用三件套。

- **第 95 行** docstring 一句话定位。

- **第 96 行** `name: str` 具名标识。

- **第 98 行** `def register(self, spec: ToolSpec, fn: Callable[..., DynamicValue]) -> None:`
  注册工具：spec 定义 schema，fn 是实现。同步方法（注册是内存操作，无 IO）。

- **第 99 行** docstring + **第 100 行** `...`。

- **第 102 行** `def catalog(self, level: int = 1) -> list[ToolSpec]:`
  目录查询。**渐进披露**的关键：level=1 只 name+描述，2/3 含完整 schema。

- **第 103 行** docstring 说明 level 语义 + **第 104 行** `...`。

- **第 106 行** `async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:`
  调用工具：异步（工具可能 IO）；异常折叠为 ToolResult，不抛穿。

- **第 107 行** docstring + **第 108 行** `...`。

### 第 111-126 行：SkillPort（技能协议）

- **第 111 行** `@runtime_checkable`。

- **第 112 行** `class SkillPort(Protocol):` 技能能力：渐进式披露正文。

- **第 113 行** docstring 定位。

- **第 114 行** `name: str`。

- **第 116 行** `def discover(self) -> list[SkillManifest]:`
  发现技能清单。第 117 行 docstring 强调"具名视图，非 DynamicValue"——技能是框架可理解的结构，不是动态 blob。

- **第 118 行** `...`。

- **第 120 行** `def catalog(self) -> list[SkillManifest]:`
  技能目录（name/version/触发词，常驻 L1）。

- **第 121 行** docstring + **第 122 行** `...`。

- **第 124 行** `def body(self, name: str) -> str:`
  加载正文（L3 按需），**正文须过注入检测**（fail closed）。

- **第 125 行** docstring + **第 126 行** `...`。

### 第 129-140 行：RAGPort（检索增强协议）

- **第 129 行** `@runtime_checkable`。

- **第 130 行** `class RAGPort(Protocol):` 检索增强：入库与检索。

- **第 131 行** docstring。

- **第 132 行** `name: str`。

- **第 134 行** `async def ingest(self, documents: list[DynamicValue], ctx: PortCtx) -> int:`
  入库文档，返回成功入库的块数（int 而非 bool——可观测"入了几块"）。

- **第 135 行** docstring + **第 136 行** `...`。

- **第 138 行** `async def retrieve(self, query: str, ctx: PortCtx) -> list[DynamicValue]:`
  检索命中列表，每项带引用溯源信息（RAG 的 citation 要求）。

- **第 139 行** docstring + **第 140 行** `...`。

### 第 143-158 行：DisclosureIndex（三级披露协议）

- **第 143 行** `@runtime_checkable`。

- **第 144 行** `class DisclosureIndex(Protocol):`
  三级披露能力：把统一合集投影成 L1 目录 / L2 schema / L3 正文。

- **第 145-149 行** docstring：
  - 第 147 行点明"为何是独立 Port"：披露策略可替换（默认 ToolIndex），token 预算是硬约束。
  - 第 148 行点明装配期经 `PORT_PROTOCOLS["disclosure"]` 做 isinstance 履约校验——这解释了为什么 `PORT_PROTOCOLS` 里有 disclosure 这个看似"辅助"的键。

- **第 150 行** `name: str`。

- **第 152-156 行** `disclose(self, names, skills) -> DisclosurePacket:` 多行签名。
  入参：命中意图名 + 技能清单；返回披露包。

- **第 157 行** docstring：`超预算由实现显式拒绝`——token 硬约束由实现保证，协议只约定语义。

- **第 158 行** `...`。

### 第 161-180 行：MemoryPort（记忆协议）

- **第 161 行** `@runtime_checkable`。

- **第 162 行** `class MemoryPort(Protocol):`
  记忆能力：按命名空间读写。**基础 Port，领域字段不进**。

- **第 163-167 行** docstring：
  - 第 165-166 行点明 `recall`（语义召回）已归位到 R7 扩展协议 `MemoryPortWithRecall`（`src/harness/context/ports.py`，规格 P8）。
  - 同样的"基础不依赖领域"纪律，与 LLMPort 的 last_usage 归位同构。

- **第 168 行** `name: str`。

- **第 170 行** `async def read(self, namespace: str, key: str, ctx: PortCtx) -> DynamicValue:`
  按命名空间+键读取；不存在返回 None（第 171 行 docstring）。

- **第 172 行** `...`。

- **第 174 行** `async def write(self, namespace: str, key: str, value: DynamicValue, ctx: PortCtx) -> None:`
  写入；内容须过注入检测且不含 secret 明文（第 175 行 docstring）——安全约束写进契约。

- **第 176 行** `...`。

- **第 178 行** `async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:`
  遗忘指定记忆（TTL 或显式删除，第 179 行 docstring）。

- **第 180 行** `...`。

### 第 183-198 行：StorePort（持久化协议）

- **第 183 行** `@runtime_checkable`。

- **第 184 行** `class StorePort(Protocol):` 持久化：会话快照存取。

- **第 185-189 行** docstring 关键说明（第 187-188 行）：
  - `save/load` 涉及外部 IO（DB/Redis/文件），同步会阻塞 Runner 事件循环→**必须 async**。
  - 默认实现 `InMemoryStorePort`（R7）内部无 IO，但签名统一 async 以支持远程后端。

- **第 190 行** `name: str`。

- **第 192 行** `async def save(self, session_id: str, snapshot: dict[str, DynamicValue]) -> None:`
  保存快照；结构须可确定性 JSON 序列化（第 193 行 docstring）——确定性是 resume 的前提。

- **第 194 行** `...`。

- **第 196 行** `async def load(self, session_id: str) -> dict[str, DynamicValue] | None:`
  读取快照；不存在返回 None（第 197 行 docstring）。

- **第 198 行** `...`。

### 第 201-210 行：SandboxPort（沙盒协议）

- **第 201 行** `@runtime_checkable`。

- **第 202 行** `class SandboxPort(Protocol):` 在隔离环境执行函数。

- **第 203 行** docstring。

- **第 204 行** `name: str`。

- **第 206-208 行** `async def run(self, fn, args, ctx) -> DynamicValue:` 多行签名。
  入参：函数 + 参数 + ctx。

- **第 209 行** docstring：`资源限额与网络白名单由实现保证`——安全责任在实现侧，协议声明边界。

- **第 210 行** `...`。

### 第 213-224 行：AuditStore（审计存储后端协议）

- **第 213 行** `@runtime_checkable`。

- **第 214 行** `class AuditStore(Protocol):`
  审计存储后端（外部工具可注入）：**append-only 追加，不可篡改**。

- **第 215-219 行** docstring：
  - 第 217 行：默认实现（R9）内存/文件，用户可注入 DB/远程审计系统。
  - 第 218 行：异步——审计落盘/远程是 IO，不阻塞事件循环。

- **第 220 行** `name: str`。

- **第 222 行** `async def append(self, event: dict[str, DynamicValue]) -> None:`
  追加审计事件，写入后不可改（第 223 行 docstring）。

- **第 224 行** `...`。

### 第 227-233 行：AuditPort（审计能力协议）

- **第 227 行** `class AuditPort(Protocol):`
  **注意：这里没有 @runtime_checkable 装饰器**，与前后多数协议不同（TelemetryPort 同理，见第 261 行）。但二者仍在 `PORT_PROTOCOLS` 中（第 334 行）——装配期若对它们做 isinstance 校验，需要先补装饰器。这是一处值得留意的细节不对称。

- **第 228 行** docstring：审计能力：记录结构化事件（异步；存储后端经 AuditStore 注入）。
  体现 AuditPort（能力门面）与 AuditStore（可替换后端）的职责分离。

- **第 229 行** `name: str`。

- **第 231 行** `async def record(self, event: dict[str, DynamicValue], ctx: PortCtx) -> None:`
  追加审计事件（append-only + 哈希链，不可旁路）；内部委托 AuditStore（第 232 行 docstring）。

- **第 233 行** `...`。

### 第 236-243 行：EvalPort（评测协议）

- **第 236 行** `@runtime_checkable`。

- **第 237 行** `class EvalPort(Protocol):` 对输出打分。

- **第 238 行** docstring。

- **第 239 行** `name: str`。

- **第 241 行** `async def score(self, case_id: str, output: DynamicValue, ctx: PortCtx) -> float:`
  单个用例打分，归一化到 0~1（第 242 行 docstring）。

- **第 243 行** `...`。

### 第 246-258 行：TelemetryExporter（遥测导出后端协议）

- **第 246 行** `@runtime_checkable`。

- **第 247 行** `class TelemetryExporter(Protocol):`
  遥测导出后端（外部工具可注入）：把 span/metric/log 导出到外部系统。

- **第 248-253 行** docstring 关键说明：
  - 第 250 行：默认实现（R10）in-process（仅内存收集，供测试）；用户可注入 OTLP/Jaeger/Langfuse。
  - 第 251-252 行：**OTel 标准做法**——采集 API 同步（关键路径不 await），导出异步（不阻塞）。这是"同步采集 + 异步导出"分离的依据。

- **第 254 行** `name: str`。

- **第 256 行** `async def export(self, records: tuple[DynamicValue, ...]) -> None:`
  批量导出；失败内部消化，不抛穿业务路径（第 257 行 docstring）——遥测绝不能拖垮主流程。

- **第 258 行** `...`。

### 第 261-280 行：TelemetryPort（可观测协议）

- **第 261 行** `class TelemetryPort(Protocol):`
  同样**没有 @runtime_checkable**（与 AuditPort 对称）。

- **第 262-267 行** docstring 关键说明：
  - 第 264-266 行：同步 API 是有意为之（OTel 惯例）——span 是上下文管理器必须同步；metric/log 同步入队，远程导出由注入的 TelemetryExporter 异步批量完成；关键业务路径不因遥测 IO 阻塞。
  - 第 266 行点明外部系统接入点 = TelemetryExporter。

- **第 268 行** `name: str`。

- **第 270 行** `def span(self, name: str, ctx: PortCtx) -> AbstractContextManager[DynamicValue]:`
  开启链路 span，返回上下文管理器（第 271 行 docstring）。

- **第 272 行** `...`。

- **第 274 行** `def metric(self, name: str, value: float, tags: dict[str, DynamicValue] | None = None) -> None:`
  记录指标，tags 可选维度标签（第 275 行 docstring）。

- **第 276 行** `...`。

- **第 278 行** `def log(self, event: dict[str, DynamicValue]) -> None:`
  记录结构化日志事件（第 279 行 docstring）。

- **第 280 行** `...`。

### 第 283-290 行：HumanPort（人工在环协议）

- **第 283 行** `@runtime_checkable`。

- **第 284 行** `class HumanPort(Protocol):` 向人提问并取回答。

- **第 285 行** docstring。

- **第 286 行** `name: str`。

- **第 288 行** `async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:`
  提问并返回回答；**挂起态须能跨周期恢复**（第 289 行 docstring）——与 gate 算子的 Suspend/resume 语义对齐。

- **第 290 行** `...`。

### 第 293-310 行：MCPPort（MCP 工具协议）

- **第 293 行** `@runtime_checkable`。

- **第 294 行** `class MCPPort(Protocol):`
  MCP 能力：发现与调用外部工具。R5 统一以本协议的**带 ctx 签名**为准。

- **第 295 行** docstring。

- **第 296 行** `name: str`。

- **第 298 行** `async def list_tools(self, server: str, ctx: PortCtx) -> list[ToolSpec]:`
  列出 MCP server 的工具；L1 披露只进目录不进 schema（第 299 行 docstring）——与 ToolPort.catalog 的渐进披露一致。

- **第 300 行** `...`。

- **第 302-304 行** `async def call_tool(self, server, tool, args, ctx) -> DynamicValue:` 多行签名。
  调用 MCP 工具，返回形态由工具决定（第 305 行 docstring）。

- **第 306 行** `...`。

- **第 308 行** `async def ping(self, server: str, ctx: PortCtx) -> bool:`
  健康检查：探测 server 可达。第 309 行 docstring 强调 **R5/R13 契约，纳入履约校验**——所以 ping 不是可有可无的辅助方法。

- **第 310 行** `...`。

### 第 313-324 行：EventStreamPort（事件流协议）

- **第 313 行** `@runtime_checkable`。

- **第 314 行** `class EventStreamPort(Protocol):`
  事件流能力（L3 接入契约）：**只读订阅**标准事件流。

- **第 315 行** docstring。

- **第 316 行** `name: str`。

- **第 318 行** `def subscribe(self, topic: str, since: int | None = None) -> AsyncIterator[DynamicValue]:`
  订阅事件流，从 since 序号续读；**只读不侵入内核**（第 319 行 docstring）——L3 接入的边界纪律。

- **第 320 行** `...`。

- **第 322 行** `def topics(self) -> list[str]:`
  返回可订阅的 topic 列表（第 323 行 docstring）。

- **第 324 行** `...`。

### 第 327-337 行：PORT_PROTOCOLS 协议目录

- **第 327-330 行** 四行注释：
  - 第 327 行：`# port 名 → Protocol 类型；供装配期 isinstance 校验适配器是否履约。`
  - 第 328 行：键是装配约定的 port 名，值是对应的 R2 Protocol 类。
  - 第 329-330 行：用法示例——`registry.register("llm", adapter)` 后，用 `isinstance(adapter, PORT_PROTOCOLS["llm"])` 判断是否实现了全部方法（**缺一即装配期失败**）。

- **第 331 行** `PORT_PROTOCOLS: dict[str, type] = {`
  协议目录定义。类型注解 `dict[str, type]`（值是类对象）。

- **第 332 行** `"llm": LLMPort, "tool": ToolPort, "skill": SkillPort, "rag": RAGPort,`
  前四个主能力。

- **第 333 行** `"memory": MemoryPort, "store": StorePort, "sandbox": SandboxPort,`
  记忆、持久化、沙盒。

- **第 334 行** `"audit": AuditPort, "eval": EvalPort, "telemetry": TelemetryPort,`
  审计、评测、可观测。（注意 audit/telemetry 对应的协议没有 runtime_checkable，见第 227/261 行的注意点。）

- **第 335 行** `"human": HumanPort, "mcp": MCPPort, "event_stream": EventStreamPort,`
  人工在环、MCP、事件流。

- **第 336 行** `"disclosure": DisclosureIndex,`
  第 14 个键：披露协议。比 docstring 的 13 个主 Port 多一个，因为披露是可替换策略且受 token 硬约束。

- **第 337 行** `}` 目录结束。共 **14 个键**。

---

## 2. dto.py 逐行讲解（277 行）

全框架的**跨模块数据契约**。两种载体并存：`@dataclass(frozen=True, slots=True)` 的不可变值对象 + pydantic `_Strict` 严格模型。读文件时先看清每个 DTO 属于哪一种。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""共享 DTO：跨模块数据契约（pydantic），全框架统一（原 R2 hooks/dto.py 归位组合层）。`
  一句话定位 + 沿革（原 R2 hooks/dto.py，规格 P2 归位）。

- **第 3 行** `字段只增不改语义；废弃字段先标记 deprecated 至少一个 minor 版本。`
  **DTO 演进纪律**：跨模块共享，不能随便改语义；废弃要过渡（先 deprecated 一个 minor 版本）。

- **第 4-5 行** `所有形态由外部 port 决定的字段统一用 DynamicValue（动态边界具名别名），禁止散落裸 Any（见 docs/编码规则.md 规则 3）。`
  规则 3 落地：动态形态用 `DynamicValue` 具名，而不是到处 `Any`。

- **第 6 行** `每个字段的 Field(description) 说明语义；复杂类在类 docstring 补字段约束。`
  解释为什么下文 pydantic 字段都带 `description`，以及为什么复杂类有大段 docstring。

- **第 7 行** `"""` docstring 结束。

### 第 9-16 行：imports

- **第 9 行** `from __future__ import annotations` 延迟注解。

- **第 11 行** `from dataclasses import dataclass`
  不可变值对象用（StreamEvent/SkillManifest/DisclosurePacket）。

- **第 12 行** `from enum import Enum`
  有限取值用 Enum（规则 5）。

- **第 14 行** `from pydantic import BaseModel, ConfigDict, Field`
  - `BaseModel`：严格模型基类。
  - `ConfigDict`：配置 `extra="forbid"`。
  - `Field`：字段带 description 与默认值。

- **第 16 行** `from runtime import DynamicValue`
  依赖方向恒定：只 import runtime。

### 第 18-32 行：__all__ 导出清单

- **第 18-32 行** `__all__ = ["DangerLevel", "DisclosurePacket", "SkillManifest", "LLMRequest", "LLMResponse", "PortCtx", "StreamEvent", "StreamEventKind", "ToolCall", "ToolResult", "ToolSpec", "Usage", "make_call"]`
  导出 13 个符号（`__init__.py` 据此重导出）。注意 `Invocation` **不在**这里——它在 context.py（CallContext 的别名）。

### 第 35-52 行：StreamEventKind 枚举

- **第 35 行** `class StreamEventKind(str, Enum):`
  继承 `str, Enum` 双基类：既是枚举（有限取值），又能当字符串用（进 JSON/日志/流式协议）。

- **第 36-44 行** docstring 成员说明：
  - 第 39 行 `TEXT_DELTA`：文本增量，data 为 str。
  - 第 40 行 `TOOL_CALL_START / DELTA / END`：工具调用生命周期事件。
  - 第 41 行 `HANDOFF`：角色/程序切换，data 为 `{"to": ...}`。
  - 第 42 行 `USAGE`：末块结算，data 为 Usage。
  - 第 43 行 `DONE`：流结束。

- **第 46-52 行** 七个成员值：
  - **第 46 行** `TEXT_DELTA = "text_delta"`
  - **第 47 行** `TOOL_CALL_START = "tool_call_start"`
  - **第 48 行** `TOOL_CALL_DELTA = "tool_call_delta"`
  - **第 49 行** `TOOL_CALL_END = "tool_call_end"`
  - **第 50 行** `HANDOFF = "handoff"`
  - **第 51 行** `USAGE = "usage"`
  - **第 52 行** `DONE = "done"`

  为什么用 Enum 不用裸字符串：禁止 `"text_delta"` 这种魔法串散落，拼写错误在类型层暴露。

### 第 55-81 行：DangerLevel 枚举（权威定义）

- **第 55 行** `class DangerLevel(str, Enum):`
  **权威定义就在 compose**——这是跨层 DTO 的物理约束：R9 安全层、R5 工具层、网关都从 compose import 同一个 Enum，避免各自维护字符串导致漂移。

- **第 56-60 行** docstring：
  - 第 56 行：规则 5（有限取值用 Enum）。
  - 第 58-59 行：四级语义对齐 R9 §5（权威）：`SAFE`=直接执行 / `LOW`=审计后直执行 / `HIGH`=沙盒执行 / `CRITICAL`=审批后沙盒执行。

- **第 62-65 行** 四个成员：
  - **第 62 行** `SAFE = "SAFE"`
  - **第 63 行** `LOW = "LOW"`
  - **第 64 行** `HIGH = "HIGH"`
  - **第 65 行** `CRITICAL = "CRITICAL"`

- **第 67-68 行** `@classmethod def coerce(cls, value: str | DangerLevel | None) -> DangerLevel:`
  归一外部字符串/枚举为成员。**fail closed**：未知或 None 回退到最严格的 CRITICAL。

- **第 69-73 行** docstring 关键论证（第 71-72 行）：
  - 为什么不回退 SAFE：danger 决定沙盒与审批路由，未知值按最低风险处理会**放行高危工具**，属安全漏洞。
  - 按 CRITICAL 处理只会多走一次审批/沙盒，代价可接受。这是典型的"安全侧不对称代价"决策。

- **第 74-75 行** `if isinstance(value, cls): return value`
  已是成员，原样返回（幂等）。

- **第 76-80 行** `if isinstance(value, str): try: return cls(value) except ValueError: return cls.CRITICAL`
  字符串尝试构造成员；无法识别（ValueError）→ CRITICAL。

- **第 81 行** `return cls.CRITICAL`
  None 或其它类型 → CRITICAL。兜底分支保证任何输入都返回合法成员。

### 第 84-93 行：StreamEvent 值对象

- **第 84 行** `@dataclass(frozen=True, slots=True)`
  - `frozen=True`：不可变，防止事件被订阅者改写。
  - `slots=True`：无 `__dict__`，省内存、防动态加属性。

- **第 85 行** `class StreamEvent:`
  结构化流式事件（跨 Port/网关统一载体，规则 3 动态边界具名 kind）。

- **第 86-90 行** docstring：第 89 行说明 kind 决定 data 形态（文本 str / 工具 dict / USAGE 为 Usage）。

- **第 92 行** `kind: StreamEventKind` 事件类型。

- **第 93 行** `data: DynamicValue = None` 载荷，形态由 kind 决定。

### 第 96-99 行：_Strict 严格模型基类

- **第 96 行** `class _Strict(BaseModel):`
  严格模型基类：拒绝未声明字段（extra=forbid），避免脏数据静默混入。

- **第 97 行** docstring 同上。

- **第 99 行** `model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)`
  - `extra="forbid"`：多传字段直接报错，而不是静默丢弃——边界脏数据无处藏身。
  - `arbitrary_types_allowed=True`：允许 `DynamicValue` 这类运行时类型作为字段类型。

### 第 102-114 行：SkillManifest 值对象

- **第 102 行** `@dataclass(frozen=True, slots=True)` 不可值对象。

- **第 103 行** `class SkillManifest:`
  技能清单条目（SkillPort.discover/catalog 返回，L1 常驻披露）。

- **第 104-109 行** docstring 字段说明：
  - 第 107 行：name 唯一名、version 语义版本、description 一句话、triggers 触发词元组（命中后加载 L3 正文）。

- **第 111-114 行** 四个字段：
  - **第 111 行** `name: str`
  - **第 112 行** `version: str`
  - **第 113 行** `description: str`
  - **第 114 行** `triggers: tuple[str, ...] = ()`
    用 tuple 而非 list：不可变，契合 frozen 语义。

### 第 117-131 行：DisclosurePacket 值对象

- **第 117 行** `@dataclass(frozen=True, slots=True)`。

- **第 118 行** `class DisclosurePacket:`
  三级披露投影结果（DisclosureIndex.disclose 返回）。

- **第 119-126 行** docstring 字段说明：
  - 第 122 行 `l1`：常驻目录（name+一句话，**绝不含 parameters_schema 完整 schema**）。
  - 第 123 行 `l2`：命中意图后披露的完整 ToolSpec（含 schema）。
  - 第 124 行 `l3`：技能正文（name→正文映射）。
  - 第 125 行 `token_cost`：估算 token 成本（进 trace）。

- **第 128-131 行** 四个字段：
  - **第 128 行** `l1: tuple[ToolSpec, ...]`
  - **第 129 行** `l2: tuple[ToolSpec, ...]`
  - **第 130 行** `l3: dict[str, str]`
  - **第 131 行** `token_cost: int`

### 第 134-144 行：Usage 模型

- **第 134 行** `class Usage(_Strict):` token 与成本元数据。

- **第 135-140 行** docstring：prompt_tokens 输入、completion_tokens 输出、cost_usd 本步成本（美元），本地/免费实现可全填 0。

- **第 142 行** `prompt_tokens: int = Field(default=0, description="Input token count")`
- **第 143 行** `completion_tokens: int = Field(default=0, description="Output token count")`
- **第 144 行** `cost_usd: float = Field(default=0.0, description="Cost of this step (USD)")`

  全部带默认值：适配器不填也能构造成功（向后兼容）。

### 第 147-166 行：PortCtx 模型

- **第 147 行** `class PortCtx(_Strict):`
  Port 调用上下文：适配器拿会话信息的**唯一途径**，不持有会话状态。

- **第 148-155 行** docstring 字段说明：
  - 第 153 行关键：`budget_left` 是 R1 Budgets 的**只读投影**，目的是"避免 DTO 反向依赖内核"——DTO 不 import runtime 的 Budgets 类型，只拿一个动态值。

- **第 157-166 行** 五个字段：
  - **第 157 行** `session_id: str = Field(default="", ...)`
  - **第 158 行** `run_id: str = Field(default="", ...)`
  - **第 159 行** `trace_span: str | None = Field(default=None, ...)` 可选 span 标识。
  - **第 160-163 行** `budget_left: DynamicValue = Field(default=None, description="Read-only projection of remaining budget (avoids DTO depending on core)")`
  - **第 164-166 行** `meta: dict[str, DynamicValue] = Field(default_factory=dict, ...)`
    临时元数据（动态键）。`default_factory` 而非 `{}`：避免可变默认值共享。

### 第 169-183 行：ToolCall 模型

- **第 169 行** `class ToolCall(_Strict):` 工具调用。

- **第 170-177 行** docstring 字段说明：
  - 第 173 行：call_id 关联 id（回填结果时据此匹配）。
  - 第 175 行：danger 风险级别（R9 高危路由据此决定是否走沙盒+审批；缺省 SAFE 兼容旧构造）。
  - 第 176 行：source 工具来源（local/mcp/skill，审计用）。

- **第 179-183 行** 五个字段：
  - **第 179 行** `call_id: str = Field(description="Correlation id")` 必填。
  - **第 180 行** `name: str = Field(description="Tool name")` 必填。
  - **第 181 行** `args: dict[str, DynamicValue] = Field(default_factory=dict, ...)` 键由工具 schema 决定，故动态。
  - **第 182 行** `danger: DangerLevel = Field(default=DangerLevel.SAFE, description="Risk level (Enum; unknown values normalized by coerce)")`
    缺省 SAFE 兼容旧构造；未知值由 coerce 归一（注意字段注解是 DangerLevel 而非 `str | DangerLevel`，归一化责任在构造方）。
  - **第 183 行** `source: str = Field(default="local", ...)`

### 第 186-202 行：ToolSpec 模型

- **第 186 行** `class ToolSpec(_Strict):`
  工具目录条目（L1 披露载体：常驻只有 name+description）。

- **第 187-194 行** docstring 字段说明：
  - 第 191 行：`parameters_schema` 是 JSON Schema，**L2/L3 才披露**。
  - 第 192 行：danger 风险级别（SAFE/LOW/HIGH/CRITICAL）。

- **第 196-202 行** 五个字段：
  - **第 196 行** `name: str = Field(description="Globally unique tool name")` 全局唯一。
  - **第 197 行** `description: str = Field(default="", ...)` 一句话描述。
  - **第 198-200 行** `parameters_schema: dict[str, DynamicValue] = Field(default_factory=dict, description="JSON Schema")`
  - **第 201 行** `danger: DangerLevel | None = Field(default=None, description="Risk level; required at assembly time")`
    与 ToolCall 不同：这里允许 None，因为在**装配期**必须补齐（description 明说 required at assembly time）。
  - **第 202 行** `source: str = Field(default="local", ...)`

### 第 205-220 行：ToolResult 模型

- **第 205 行** `class ToolResult(_Strict):` 工具调用结果信封。

- **第 206-212 行** docstring 字段说明：
  - 第 209 行：call_id 关联 id、ok 是否成功。
  - 第 211 行：meta 含 danger/source/sandbox 等元数据（动态键）。

- **第 214-220 行** 五个字段：
  - **第 214 行** `call_id: str = Field(description="Correlation id")` 必填。
  - **第 215 行** `ok: bool = Field(default=True, ...)` 缺省成功。
  - **第 216 行** `value: DynamicValue = Field(default=None, ...)` 返回值形态由工具决定。
  - **第 217 行** `error: str | None = Field(default=None, ...)` 错误信息。
  - **第 218-220 行** `meta: dict[str, DynamicValue] = Field(default_factory=dict, description="Metadata such as danger/source/sandbox")`

### 第 223-242 行：LLMRequest 模型

- **第 223 行** `class LLMRequest(_Strict):` LLM 补全请求。

- **第 224-231 行** docstring 字段说明：
  - 第 227 行：messages 每项为 dict，形态兼容各家 vendor。
  - 第 228 行：tools_catalog（L1 披露；None 表示不披露）。

- **第 233-242 行** 六个字段：
  - **第 233-235 行** `messages: list[dict[str, DynamicValue]] = Field(default_factory=list, ...)`
  - **第 236-238 行** `tools_catalog: list[ToolSpec] | None = Field(default=None, ...)`
  - **第 239 行** `model: str | None = Field(default=None, ...)` 模型名覆盖。
  - **第 240 行** `temperature: float | None = Field(default=None, ...)` 采样温度。
  - **第 241 行** `max_tokens: int | None = Field(default=None, ...)` 最大输出。
  - **第 242 行** `task_hint: str | None = Field(default=None, description="Task type hint (e.g. code/chat) for R4 model routing")`
    任务类型提示，供 R4 模型路由使用（领域字段通过 hint 传入，而非污染基础 Port）。

### 第 245-257 行：LLMResponse 模型

- **第 245 行** `class LLMResponse(_Strict):` LLM 补全响应。

- **第 246-251 行** docstring 字段说明：content 正文、tool_calls 列表、usage、model 实际使用模型。

- **第 253-257 行** 五个字段：
  - **第 253 行** `content: str | None = Field(default=None, ...)`
  - **第 254 行** `tool_calls: list[ToolCall] = Field(default_factory=list, ...)`
  - **第 255 行** `usage: Usage = Field(default_factory=Usage, ...)` 默认新建空 Usage。
  - **第 256 行** `model: str = Field(default="", ...)` 实际使用的模型。
  - **第 257 行** `error: str | None = Field(default=None, description="Backend error code/message; non-None means the call failed (R3 retries/breaks on this)")`
    **error 非 None 即调用失败**，R3 据此重试/熔断——这是"异常折叠为 Result/DTO"纪律的落地（不抛异常，塞进字段）。

### 第 260-277 行：make_call 工厂函数

- **第 260-267 行** 函数签名（多行）：
  - **第 261 行** `name: str` 工具名。
  - **第 262 行** `args: dict[str, DynamicValue]` 参数字典。
  - **第 263 行** `call_id: str = ""` 关联 id，留空兜底。
  - **第 265 行** `danger: DangerLevel = DangerLevel.SAFE` 风险级别。
  - **第 266 行** `source: str = "local"` 工具来源。
  - **第 267 行** `) -> ToolCall:` 返回 ToolCall。

- **第 268-276 行** docstring：第 272 行强调 `call_id` 留空时用 name 兜底（保证 id 非空）。

- **第 277 行** `return ToolCall(call_id=call_id or name, name=name, args=args, danger=danger, source=source)`
  **关键兜底行**：`call_id or name`——空字符串是假值，自动用 name 顶上。
  - 不这么写：call_id 为空，ToolResult 回填时无法匹配调用，关联丢失。
  - 用 `or` 而非显式 `if`：一行完成，语义清晰（空→name）。

---

## 3. program.py 逐行讲解（485 行）

**全模块最核心的文件**：10 个 A 类组合算子，装配期 `Program → Program` 纯变换。读之前先把 docstring 的 5 条设计约束刻进脑子，后面每个算子都是这 5 条的具体落地。

### 第 1-14 行：模块 docstring（5 条设计约束）

- **第 1 行** `"""A 类 10 组合算子（规格 §2.3 / D-4）：装配期 Program → Program 纯变换。`
  一句话定位：装配期变换，运行期不改原 Program。

- **第 3-9 行** 5 条设计约束（R1 不改前提）：
  - **第 4 行** `装配期纯变换，不改原 Program（compile 幂等）；`——同一 Program 编译两次结果一致。
  - **第 5-6 行** `必须是纯函数：运行期状态进 State.scratch 的 x-* 键，禁止算子实例持有流程进度（resume 靠 program_factory 重建，D-8）；`——**最关键的一条**：进度在 scratch 不在实例，才能靠工厂重建 resume。
  - **第 7 行** `轮次/跳数交 Budgets，架构 generator 内禁自数；`——循环上限由 R1 Budgets 强制，算子内只是防御性护栏。
  - **第 8 行** `State 只读，改状态靠 state.set 产生新对象（局部持有，不写 R1 fold）；`——不可变状态，新对象替换。
  - **第 9 行** `gate 是唯一可 yield Suspend 的 A 类算子（D-6）。`——挂起权限唯一。

- **第 11-13 行** 算子间数据传递机制：
  - 第 11-12 行：`_run_child` 拦截子 Program 的 Terminate 提取 output，写入局部 State 的 `x-compose.*` scratch 键。
  - 第 12-13 行：下游算子启动前把 `x-*` 键合并进回注 State（R1 fold 不感知 x-*，故由组合层自身承担 carry，纯函数、resume 可重建）。
  - 这段解释了 `_carry_x` 为什么存在：R1 的 fold 不认识 x-*，组合层得自己搬运进度。

- **第 14 行** `"""` docstring 结束。

### 第 15-44 行：imports 与 __all__

- **第 15 行** `from __future__ import annotations` 延迟注解。

- **第 17 行** `from collections.abc import Callable, Generator, Sequence`
  - `Callable`：算子/HOF 签名。
  - `Generator`：内部 program 的生成器类型。
  - `Sequence`：par/race/fallback 的序列入参。

- **第 18 行** `from enum import Enum` JoinMode 枚举。

- **第 19 行** `from typing import Final` _Keys 常量标注。

- **第 21-29 行** `from runtime import (AgentState, DynamicValue, Effect, Invoke, Program, Suspend, Terminate)`
  **依赖方向恒定**：只 import runtime。Element 释义：
  - `AgentState`：不可变状态，算子靠 `set/with_` 产生新对象。
  - `Effect`：yield 给 R1 执行的效果。
  - `Invoke`：能力调用 Effect。
  - `Suspend`：挂起 Effect（只有 gate 用）。
  - `Terminate`：终止 Effect（携带 output）。

- **第 31-44 行** `__all__`：导出 JoinMode、ProgramFn、10 个算子（seq/par/branch/race/loop/map_state/map_effect/fallback/gate/retry）。

### 第 46-48 行：ProgramFn 类型别名

- **第 46-47 行** 注释：`Program 是 generator 对象；实际 program 由工厂函数（每次调用产出新 generator）提供，resume 靠 program_factory 重建（D-8）。算子在该函数层面做装配期纯变换。`
  **贯穿全文件的核心**：Program 不是 generator 实例，而是"产出 generator 的工厂"——每次调用 `program()` 得到全新 generator，这正是 resume 可重建的基础。

- **第 48 行** `type ProgramFn = Callable[[], Program]`
  Python 3.12 的 `type` 语句（PEP 695）类型别名。用 `type` 而非 `ProgramFn = ...` 赋值：真正的类型别名，工具链识别更好、惰性求值。

### 第 51-58 行：_Keys 私有 scratch 键

- **第 51 行** `class _Keys:`
  算子私有 scratch 键集中定义，**禁用魔法字符串**。

- **第 52 行** docstring：`算子私有 scratch 键（x-* 前缀，D-2 扩展键约定），集中定义禁用魔法字符串。`

- **第 53 行** `PREFIX: Final[str] = "x-compose."`
  前缀统一。为什么加 `x-`：R1 fold 忽略 x-* 键，组合层专用命名空间，不会和核心状态冲突。

- **第 54 行** `SEQ_OUTPUT: Final[str] = PREFIX + "seq.output"` seq 的输出键。
- **第 55 行** `ATTEMPT: Final[str] = PREFIX + "attempt"` fallback 的尝试序号键。
- **第 56 行** `FAIL_FLAG: Final[str] = PREFIX + "fail"` 失败标记键（fallback/retry 共用）。
- **第 57 行** `LOOP_ROUND: Final[str] = PREFIX + "loop.round"` loop 的轮次键。
- **第 58 行** `RETRY_COUNT: Final[str] = PREFIX + "retry.count"` retry 的计数键。

  全部 `Final[str]` + 拼接前缀：改前缀只改一处。

### 第 61-65 行：JoinMode 枚举

- **第 61 行** `class JoinMode(str, Enum):` par 并行结果的聚合策略。
- **第 62 行** docstring：`有限取值集合，禁用裸字符串比较`。
- **第 63 行** `ALL = "all"` 全部按槽位顺序。
- **第 64 行** `ANY = "any"` 首个非 None。
- **第 65 行** `MAJORITY = "majority"` 多数票。

### 第 68-86 行：_as_state 辅助函数

- **第 68 行** `def _as_state(value: DynamicValue) -> AgentState:`
  把回注值归一为可读 State 视图。R1 回注的是 `Result.data`（DynamicValue），可能是 dict/str/None，不能直接当 State 用。

- **第 69-81 行** docstring 三分支说明：
  - 第 72 行：本身就是 AgentState → 原样返回。
  - 第 73 行：dict → 按其键构造 State（测试/上层自定义回注）。
  - 第 74 行：其它（str/None）→ 返回空 State（组合层局部进度另由 `_carry_x` 维护）。

- **第 82-83 行** `if isinstance(value, AgentState): return value`
  已是 State 直接返回，零开销。

- **第 84-85 行** `if isinstance(value, dict): return AgentState().set("scratch", value) if value else AgentState()`
  dict → 塞进 scratch 构造 State。空 dict 走空 State（`if value` 避免设空 scratch）。

- **第 86 行** `return AgentState()`
  其余形态兜底为空 State——**容错**：不因回注值形态异常让整个算子崩掉。

### 第 89-103 行：_carry_x 辅助函数

- **第 89 行** `def _carry_x(into: AgentState, source: AgentState) -> AgentState:`
  把 source 的 x-* scratch 键合并进 into（source 覆盖）。

- **第 90 行** docstring 关键点：`组合层自持的进度不被 fold 回注冲掉`——这是函数存在的核心理由。

- **第 92-98 行** Args/Returns 说明：into 保留托管字段/messages/budget；返回合并后的新 State（不可变）。

- **第 99 行** `merged = dict(into.scratch)`
  先复制 into 的 scratch（快照，不改原对象）。

- **第 100-102 行** `for key, value in source.scratch.items(): if str(key).startswith(_Keys.PREFIX): merged[key] = value`
  只挑 `x-compose.` 前缀的键覆盖进来。
  - 为什么不整体覆盖：回注 State 里可能有 R1 托管的 messages/budget，整体覆盖会丢失；只搬 x-* 键精准。
  - `str(key)` 兜底：key 可能非字符串（动态 scratch）。

- **第 103 行** `return into.with_(scratch=merged)`
  用 `with_` 产生新 State（不可变风格），不改 into。

### 第 106-133 行：_run_child（子 Program 驱动引擎）

这是**所有算子复用的底层引擎**：驱动子 Program，双向转发 Effect，拦截 Terminate。

- **第 106-109 行** 函数签名与返回类型 `Generator[Effect, AgentState, tuple[DynamicValue, AgentState]]`：
  产 Effect、收 AgentState、最终返回 `(output, state)` 二元组。

- **第 110-115 行** docstring 三分支：
  - 第 113 行：子 yield Terminate → **拦截**（不转发给 R1，避免提前结束整个 run），返回 output 与最新 State。
  - 第 114 行：子 yield 其它 Effect → 转发外层执行，回注 State 经 x-* 合并后 send 回子 Program。
  - 第 115 行：子 StopIteration（无 Terminate 直接 return）→ output 取 `State.output`。

- **第 117 行** `gen = child()`
  调用工厂产出**新 generator**（D-8 resume 重建的落地点）。

- **第 118-119 行** `try: effect = next(gen)`
  启动 generator，拿第一个 Effect。`try` 包裹是为了捕获 StopIteration。

- **第 120 行** `while True:` 驱动循环。

- **第 121-122 行** `if isinstance(effect, Terminate): return effect.output, state`
  **拦截 Terminate**：直接返回 output，不 yield 给 R1。这样子进程结束不会终结整个 run。

- **第 123-124 行** 注释：真实 R1 回注的是 `Result.data`（DynamicValue），原样送回子 generator；组合层进度由局部 state 自持，不依赖回注值形态（D-8）。

- **第 125 行** `sent: DynamicValue = yield effect`
  **双向转发核心**：把 effect  yielded 给外层 R1 执行，暂停等回注，回注值存进 `sent`。

- **第 126-130 行** `if isinstance(effect, Invoke): carried = {k: v for k, v in effect.payload.items() if str(k).startswith("x-")}; if carried: state = state.with_(scratch={**dict(state.scratch), **carried})`
  - 若当前 Effect 是 Invoke，从 payload 里挑 `x-` 开头的键（第 127-128 行）。
  - 非空则合并进局部 state 的 scratch（第 130 行）。
  - 作用：下游算子通过 Invoke payload 传递的进度，被局部 state 捕获，不靠回注值。
  - 注意这里前缀是 `"x-"`（比 `_Keys.PREFIX` 的 `"x-compose."` 更宽），能兼容更多 x-* 键。

- **第 131 行** `effect = gen.send(sent)`
  把回注值 send 回子 generator，拿下一个 Effect。

- **第 132-133 行** `except StopIteration: return state.output, state`
  generator 自然结束（没 yield Terminate）→ 用 state.output 兜底。

### 第 136-142 行：_as_program 辅助函数

- **第 136 行** `def _as_program(child: ProgramFn | Effect) -> ProgramFn:`
  把 Effect 常量包装为 Program（便于声明式 arm）。

- **第 137 行** docstring 同上。

- **第 138-141 行** `if isinstance(child, Effect): def constant() -> ...: yield child; return constant`
  是 Effect → 包成"只 yield 它一次"的常量 Program 工厂。

- **第 142 行** `return child`
  已是 ProgramFn 直接返回（幂等）。

  （注：此函数在 branch/fallback 的声明式 arm 场景使用，让"一个固定 Effect"和"一个 Program"能用同一接口表达。）

### 第 145-160 行：seq（顺序组合）

- **第 145 行** `def seq(a: ProgramFn, b: ProgramFn) -> ProgramFn:`
  先跑 a 到 Terminate，output 写入 x-* 后跑 b，b 的 output 为整体产出。

- **第 146-154 行** docstring + Args/Returns：`b 从 scratch 读取 a 的产出`；`返回新 Program（不改 a/b）`。

- **第 155 行** `def program() -> Generator[Effect, AgentState, None]:`
  内部 Program 生成器。

- **第 156 行** `out_a, state = yield from _run_child(a, AgentState())`
  用 `_run_child` 跑 a，从空 State 起步，拿 a 的 output 与 state。`yield from` 把子生成器委托出去。

- **第 157 行** `state = state.set(_Keys.SEQ_OUTPUT, out_a)`
  **关键传递行**：把 a 的 output 写进 `x-compose.seq.output` scratch 键，b 才能读到。

- **第 158 行** `out_b, state = yield from _run_child(b, state)`
  带着含 a output 的 state 跑 b。

- **第 159 行** `yield Terminate(output=out_b)`
  整体产出 = b 的 output。

- **第 160 行** `return program` 返回新 Program 工厂（不改 a/b）。

### 第 163-172 行：_hashable 辅助函数

- **第 163 行** `def _hashable(value: DynamicValue) -> DynamicValue:`
  把不可哈希的回注值归一为可哈希键（多数票统计用）。

- **第 164-171 行** docstring：返回本身（标量）或其字符串形式（容器）。

- **第 172 行** `return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)`
  标量（str/int/float/bool/None）原样；容器（dict/list）转 str。
  - 为什么：dict/list 不可哈希，不能直接当 dict 的 key 计数；转 str 后能计数。
  - `type(None)` 而非 `None`：isinstance 需要类型对象。

### 第 175-198 行：_join_outputs（聚合策略）

- **第 175 行** `def _join_outputs(join: str, outputs: list[DynamicValue]) -> DynamicValue:`
  按 join 策略聚合并行子 Program 的 output（纯函数，**墙钟不介入**）。

- **第 176-188 行** docstring 三分支：
  - 第 179 行：any → 首个非 None；majority → 多数票（**平票取首现，确定性**）；all 及其它 → 按槽位顺序返回完整 list。

- **第 189-190 行** `if join == JoinMode.ANY.value: return next((value for value in outputs if value is not None), None)`
  any：生成器表达式找首个非 None，`next` 带缺省 None。短路，找到即停。

- **第 191-197 行** majority 分支：
  - **第 191 行** `if join == JoinMode.MAJORITY.value and outputs:` 仅 majority 且非空才统计。
  - **第 192 行** `keys = [_hashable(value) for value in outputs]` 归一为可哈希键。
  - **第 193-195 行** `counts: dict[DynamicValue, int] = {}` + 循环 `counts[key] = counts.get(key, 0) + 1` 计数。
  - **第 196 行** `winner = max(counts, key=lambda k: (counts[k], -keys.index(k)))`
    **核心确定性行**：主键是票数 `counts[k]`；次键是 `-keys.index(k)`——首次出现的位置取负，票数相同时**位置越靠前（index 越小）→ 负得越少 → 值越大 → max 越倾向它**，即平票取首现。
    - 不这么写：`max(counts, key=counts.get)` 在平票时返回 dict 插入顺序的第一个，虽在 CPython 也确定但语义不显式；显式 `-keys.index(k)` 把"首现优先"写进契约。
  - **第 197 行** `return outputs[keys.index(winner)]`
    用键在 keys 里的位置取回**原始 output**（而非归一后的 key）。

- **第 198 行** `return list(outputs)`
  all 及未知策略：完整列表，按槽位顺序。

### 第 201-253 行：par（扇出并行）

- **第 201 行** `def par(items: Sequence[ProgramFn], *, join: str = "all") -> ProgramFn:`
  扇出并行：每轮把各子 Program 的 Effect 汇成一列表**一次 yield**（走 R1 列表并行），按 join 聚合。

- **第 202-206 行** docstring 关键说明：
  - 第 204-205 行：R1 interpreter 认 `yield list[Effect]` 为并行批（interpreter.py:153-162），runner gather 并发执行并按序回注 list（runner.py:515-526）。
  - 第 206 行：所有子 Program 同步推进（lockstep），Terminate 的子取其 output，全部完成后聚合。

- **第 215 行** `def program() -> Generator[Effect, DynamicValue, None]:` 注意 send 类型是 DynamicValue（回注 list）。

- **第 216 行** `gens = [child() for child in items]`
  为每个子 Program 产出新 generator（并行起步）。

- **第 217 行** `outputs: dict[int, DynamicValue] = {}` 槽位 → 最终 output。

- **第 218-219 行** 注释 + `pending: dict[int, Effect] = {}`
  `pending[i]` 是槽位 i 下一次待并行下发的 Effect（Terminate 已拦截记账）。

- **第 220-229 行** 初始化每个子 generator：
  - **第 221-222 行** `try: effect = next(gen)` 启动。
  - **第 223-225 行** `except StopIteration: outputs[i] = None; continue` 空子 Program 记 None。
  - **第 226-227 行** `if isinstance(effect, Terminate): outputs[i] = effect.output` 立即 Terminate 的子记账。
  - **第 228-229 行** `else: pending[i] = effect` 否则进待下发表。

- **第 230 行** `while pending:` 只要还有待下发 Effect 就继续（lockstep 轮次）。

- **第 231 行** `slots = list(pending.keys())` 本轮参与并行的槽位。

- **第 232 行** `batch = [pending[slot] for slot in slots]` 按槽位顺序组装 Effect 批。

- **第 233 行** 注释：`# 一次 yield 整个 Effect 列表 → R1 并行执行 → 按序回注 list。`

- **第 234 行** `injected_raw: DynamicValue = yield batch  # type: ignore[misc]`
  **全文件最关键的一行**：一次 yield 一个列表，R1 把它当并行批并发执行，回注一个按序排列的结果列表。`type: ignore[misc]` 是因为 send 类型是 DynamicValue 而非批类型，类型检查器会抱怨。

- **第 235-239 行** 回注值归一：
  - **第 236-237 行** `list(injected_raw) if isinstance(injected_raw, (list, tuple))` 是序列就转 list。
  - **第 238 行** `else [injected_raw] * len(slots)` 单值广播到所有槽位（容错：某些后端只回一个值）。

- **第 240-250 行** 逐槽位推进：
  - **第 241 行** `pending.pop(slot)` 先移除旧 Effect。
  - **第 242-246 行** `try: effect = gens[slot].send(value); except StopIteration: outputs[slot] = None; continue` send 回注，自然结束记 None。
  - **第 247-248 行** `if isinstance(effect, Terminate): outputs[slot] = effect.output` 该子完成。
  - **第 249-250 行** `else: pending[slot] = effect` 否则下一轮继续。

- **第 251 行** `ordered = [outputs.get(i) for i in range(len(gens))]`
  按原始槽位顺序取 output（缺失为 None）——**确定性**保证。

- **第 252 行** `yield Terminate(output=_join_outputs(join, ordered))` 聚合后终止。

- **第 253 行** `return program`。

### 第 256-282 行：branch（条件分流）

- **第 256-260 行** 函数签名：`fn(state) -> str` 选 `arms[key]`，缺失走 `default`。

- **第 261-269 行** docstring + Args/Returns。

- **第 271 行** `def program() -> ...:`。

- **第 272-273 行** 注释 + `state = yield Invoke(port="state").with_payload(op="read")`
  **首次推进拿初始 state**：yield 一个空操作 Invoke（port="state", op="read"）触发回注，拿到 R1 的初始 State。
  - 为什么不直接 `AgentState()`：要读 R1 的真实初始状态（messages/budget 等），必须走一次回注。

- **第 274 行** `state = _as_state(state)` 归一为 State 视图。

- **第 275 行** `key = fn(state)` 纯函数决策。

- **第 276 行** `target = arms.get(key) or arms.get(default or "")`
  先取命中 arm，缺失取 default。`or` 链：`arms.get(key)` 为 None 才找 default。

- **第 277-279 行** `if target is None: yield Terminate(output=None, reason="no_route"); return`
  **无路由兜底**：既不命中也无 default → Terminate 带 `reason="no_route"`，不静默失败。

- **第 280 行** `out, _ = yield from _run_child(target, state)` 跑选中的 arm。

- **第 281 行** `yield Terminate(output=out)` 输出 arm 的产出。

- **第 282 行** `return program`。

### 第 285-334 行：race（竞速）

- **第 285 行** `def race(items: Sequence[ProgramFn]) -> ProgramFn:`
  首个完成胜出，取消其余（取消走 R1 cancel 设施，**算子只编排**）。

- **第 286-293 行** docstring + Args/Returns。

- **第 294 行** `def program() -> ...:`。

- **第 295 行** `gens = [child() for child in items]` 起全部子 generator。

- **第 296 行** `pending: dict[int, Effect] = {}` 待下发 Effect。

- **第 297 行** `first_output: DynamicValue = None` 首个胜出者的 output。

- **第 298-307 行** 初始化（与 par 同构）：
  - **第 299-302 行** `try: effect = next(gen); except StopIteration: continue` 空子跳过。
  - **第 303-305 行** `if isinstance(effect, Terminate): if first_output is None: first_output = effect.output` 立即完成者抢占首位。
  - **第 306-307 行** `else: pending[i] = effect` 其余进待下发表。

- **第 308 行** `while pending:` 驱动循环。

- **第 309-311 行** `slots`/`batch`/`yield batch` 同 par——**一次 yield 列表走 R1 并行批**。

- **第 312-316 行** 回注值归一（与 par 完全同构）。

- **第 317-330 行** 逐槽位推进：
  - **第 318 行** `pending.pop(slot)` 移除旧 Effect。
  - **第 319-321 行** 注释 + `if first_output is not None: continue`
    **已有胜出者：不再推进其余**（取消语义交 R1，算子只做编排）。
  - **第 322-325 行** `try: effect = gens[slot].send(value); except StopIteration: continue`。
  - **第 326-328 行** `if isinstance(effect, Terminate): if first_output is None: first_output = effect.output` 抢占首位（只在还没有胜者时）。
  - **第 329-330 行** `else: pending[slot] = effect`。

- **第 331-332 行** `if first_output is not None: pending.clear()`
  一旦有胜者，清空 pending 结束循环——其余 generator 被丢弃（GC），由 R1 负责取消残留 Effect。

- **第 333 行** `yield Terminate(output=first_output)` 输出胜者产出。

- **第 334 行** `return program`。

### 第 337-362 行：loop（显式循环）

- **第 337-342 行** 函数签名：`body` 循环体、`cond(state) -> bool` 继续条件、`max_rounds=3` 防御性上限。

- **第 343 行** docstring：`cond 为真时重复 body，轮次上限交 Budgets（此处仅防御性护栏）。`
  **关键定位**：真实轮次约束由 R1 Budgets 强制，这里的 max_rounds 只是防止架构 generator 自数跑飞。

- **第 345-351 行** Args/Returns。

- **第 353 行** `def program() -> ...:`。

- **第 354 行** `state = yield Invoke(port="state").with_payload(op="read")`
  同 branch：yield 空操作 Invoke 拿初始 State。

- **第 355 行** `state = _as_state(state)` 归一。

- **第 356 行** `state = state.set(_Keys.LOOP_ROUND, 0)`
  轮次计数写进 `x-compose.loop.round`（scratch，不在实例）。

- **第 357 行** `while cond(state) and int(state.get(_Keys.LOOP_ROUND, 0)) < max_rounds:`
  双条件：业务 cond 为真 **且** 轮次未超上限。`int(...)` 兜底转换（回注值可能是 str）。

- **第 358 行** `out, state = yield from _run_child(body, state)` 跑一轮循环体。

- **第 359 行** `state = state.set(_Keys.LOOP_ROUND, int(state.get(_Keys.LOOP_ROUND, 0)) + 1)`
  轮次 +1，写回 scratch（**不可变更新**：set 返回新 State）。

- **第 360 行** `state = state.set(_Keys.SEQ_OUTPUT, out)`
  保存本轮 output 到 `x-compose.seq.output`，循环结束后作为整体产出。

- **第 361 行** `yield Terminate(output=state.get(_Keys.SEQ_OUTPUT))`
  输出**最后一轮**的 output（每轮覆盖）。

- **第 362 行** `return program`。

### 第 365-382 行：map_state（State 视图变换 HOF）

- **第 365 行** `def map_state(lens: Callable[[AgentState], AgentState]) -> Callable[[ProgramFn], ProgramFn]:`
  State → State 的纯变换，返回可作用于一个 Program 的高阶函数。

- **第 366-373 行** docstring + Args/Returns：`接收 Program、返回新 Program 的高阶函数`。

- **第 374 行** `def apply(child: ProgramFn) -> ProgramFn:` 外层 HOF。

- **第 375 行** `def program() -> ...:` 内层 Program。

- **第 376 行** `state = yield Invoke(port="state").with_payload(op="read")` 拿初始 State。

- **第 377 行** `state = _as_state(state)` 归一。

- **第 378 行** `state = lens(state)`
  **核心变换行**：用 lens 改写 State 视图（如注入上下文、脱敏），再传给子 Program。

- **第 379 行** `out, _ = yield from _run_child(child, state)` 用变换后的 State 跑子 Program。

- **第 380 行** `yield Terminate(output=out)` 原样输出子的产出。

- **第 381-382 行** `return program` / `return apply` 两层返回。

### 第 385-408 行：map_effect（Effect payload 变换 HOF）

- **第 385 行** `def map_effect(fn: Callable[[Effect], Effect]) -> Callable[[ProgramFn], ProgramFn]:`
  Effect → Effect 的纯变换：脱敏 / 参数规范化 / 注入标记 / 软预算注入。

- **第 386-393 行** docstring + Args/Returns：
  - 第 389 行关键：`fn: Effect → Effect 的纯函数（Terminate 原样透传，避免破坏终态）`。

- **第 394 行** `def apply(child: ProgramFn) -> ProgramFn:`。

- **第 395 行** `def program() -> ...:`。

- **第 396 行** `gen = child()` 起子 generator。

- **第 397 行** `try:`。

- **第 398 行** `effect = next(gen)` 启动。

- **第 399 行** `while True:` 驱动循环。

- **第 400-402 行** `if isinstance(effect, Terminate): yield effect; return`
  **终态原样透传**：不经过 fn 变换，直接 yield 并结束——防止变换破坏 Terminate 的 output/reason。

- **第 403 行** `sent: AgentState = yield fn(effect)`
  **核心变换行**：先 `fn(effect)` 变换 Effect 再 yield 给 R1；收回的回注值标为 AgentState。

- **第 404 行** `effect = gen.send(sent)` 回注给子 generator。

- **第 405-406 行** `except StopIteration: return` 自然结束。

- **第 407-408 行** `return program` / `return apply`。

### 第 411-438 行：fallback（降级选级骨架）

- **第 411-416 行** 签名：`p` 首选、`ladders` 降级阶梯序列、`refuse` 兜底拒答。

- **第 417 行** docstring：`按序尝试 p → ladders，全失败走 refuse。`

- **第 419-425 行** Args/Returns。

- **第 427 行** `arms = [p, *ladders, refuse]`
  把首选 + 阶梯 + 拒答拼成一个有序列表（拒答也是一"臂"，保证必有结果）。

- **第 429 行** `def program() -> ...:`。

- **第 430 行** `state = AgentState().set(_Keys.ATTEMPT, 0)`
  尝试序号写 scratch（不从回注读——降级是本地流程，从 0 起算）。

- **第 431 行** `for arm in arms:` 逐臂尝试。

- **第 432 行** `out, state = yield from _run_child(arm, state)` 跑当前臂。

- **第 433-435 行** `if not state.get(_Keys.FAIL_FLAG, False): yield Terminate(output=out); return`
  **成功判定**：没有 `x-compose.fail` 标记就视为成功，立即终止。

- **第 436 行** `state = state.set(_Keys.ATTEMPT, int(state.get(_Keys.ATTEMPT, 0)) + 1)`
  失败则尝试序号 +1，继续下一臂。

- **第 437 行** `yield Terminate(output=None, reason="all_failed")`
  所有臂都失败（含 refuse 也标记失败）→ `reason="all_failed"`。

- **第 438 行** `return program`。

### 第 441-459 行：gate（审批/澄清门 · 唯一能 Suspend 的算子）

- **第 441 行** `def gate(p: ProgramFn, policy: Callable[[AgentState], object]) -> ProgramFn:`
  审批/澄清门：可 yield Suspend 挂起，resume 后续跑 p。

- **第 442 行** docstring：`（D-6 唯一能挂起的 A 类算子）`——**全模块唯一权限**，其他算子不得 yield Suspend。

- **第 444-449 行** Args/Returns：
  - 第 446 行：`policy: 状态 → 审批策略；返回 None 表示无需审批`。

- **第 451 行** `def program() -> ...:`。

- **第 452 行** `state = yield Invoke(port="state").with_payload(op="read")` 拿初始 State。

- **第 453 行** `state = _as_state(state)` 归一。

- **第 454 行** `answer = state.last_wait_human`
  **resume 检测**：读上次人工等待的答案。挂起后 R1 会把人工回答回注到 State，下一轮从这里拿到。

- **第 455-456 行** `if policy(state) is not None and answer is None: state = yield Suspend(question="approval", options=("yes", "no"))`
  **唯一 Suspend 点**：策略要求审批（policy 非 None）且还没答案（answer None）→ yield Suspend 挂起。
  - 第二次进来时 answer 已填 → 跳过挂起，继续往下——这就是 resume。
  - `options=("yes", "no")` 用 tuple（不可变）。

- **第 457 行** `out, _ = yield from _run_child(p, state)` 审批通过后跑被包裹的 p。

- **第 458 行** `yield Terminate(output=out)` 输出 p 的产出。

- **第 459 行** `return program`。

### 第 462-485 行：retry（流程级重放）

- **第 462 行** `def retry(p: ProgramFn, *, attempts: int = 3, backoff: float = 0.0) -> ProgramFn:`
  流程级重放：失败按 program_factory 重建重跑（纯函数、状态进 x-*）。

- **第 463 行** docstring：`D-5/D-8`——与 call.py 的调用级 retry_call 区分：这里是**整个流程重跑**，那里是单次调用重试。

- **第 465-471 行** Args/Returns：
  - 第 466 行：`p: 被包裹的 Program（每次重跑重新调用，等价重建）`。
  - 第 468 行：`backoff: 退避秒数（写入 scratch 交 R3 装饰器，不在算子内 sleep）`——**算子内禁 sleep**，退避交给 R3/call 装饰器。

- **第 473 行** `def program() -> ...:`。

- **第 474 行** `state = AgentState().set(_Keys.RETRY_COUNT, 0)` 重试计数写 scratch。

- **第 475 行** `out: DynamicValue = None` 输出预置。

- **第 476 行** `for _ in range(max(1, attempts)):`
  至少 1 次（`max(1, ...)` 兜底，防止 attempts=0 一次都不跑）。

- **第 477 行** `out, state = yield from _run_child(p, state)`
  每次迭代都重新调用 p（工厂重建）——D-8 落地。

- **第 478-479 行** `if not state.get(_Keys.FAIL_FLAG, False): break`
  无失败标记则成功退出。

- **第 480 行** `count = int(state.get(_Keys.RETRY_COUNT, 0)) + 1` 计数 +1。

- **第 481-483 行** `state = state.set(_Keys.RETRY_COUNT, count).set(_Keys.PREFIX + "backoff", backoff)`
  链式 set：更新计数 + 写退避值到 `x-compose.backoff`（交 R3 装饰器读取）。
  - 注意这里 backoff 键是直接拼字符串 `PREFIX + "backoff"` 而非 _Keys 常量——小瑕疵（其他键都集中定义了）。

- **第 484 行** `yield Terminate(output=out)` 输出最后一次的产出（可能失败）。

- **第 485 行** `return program` 文件最后一行。

---

## 4. registry.py 逐行讲解（138 行）

共用注册表与可插拔基契约。三体系正交后，本模块只留"跟扩展实现无关的数据契约"——**一份注册表被可靠性执行器 / LifecycleBus / 装配层共用**（P6）。

### 第 1-9 行：模块 docstring

- **第 1 行** `"""共用注册表与可插拔协议基契约（原 R2 hooks/types.py 归位组合层，规格 P2）。`
  定位 + 沿革。

- **第 3-6 行** 三体系正交后的保留项：
  - 第 4 行 `Registry：Provider 注册表（可靠性执行器 / LifecycleBus / 装配层共用同一实例，P6）。`——**共用一份实例**是关键设计。
  - 第 5 行 Provider/Capability/ProviderRegistry：可插拔基契约（R13 术语基线）。
  - 第 6 行 HookBusError：装配期错误（重名、缺 provider、冻结后写入）。

- **第 8 行** `组合层统一装配（原 how/when 维度已归位到领域执行器与 notify.py）。`
  点明 how 维度归领域执行器、when 维度归 notify.py——解释本模块为何只剩数据契约。

- **第 9 行** `"""` 结束。

### 第 10-21 行：imports 与 __all__

- **第 10 行** `from __future__ import annotations` 延迟注解。

- **第 12 行** `from collections.abc import Iterable` names() 返回类型。

- **第 13 行** `from typing import Protocol, runtime_checkable` 三个基契约协议。

- **第 15-21 行** `__all__`：Capability / HookBusError / Provider / ProviderRegistry / Registry 五个符号。

### 第 24-35 行：ProviderRegistry 协议

- **第 24 行** `@runtime_checkable` 履约校验。

- **第 25 行** `class ProviderRegistry(Protocol):` 最小统一注册表视图（供各装配组件持有）。

- **第 26-30 行** docstring 关键（第 28-29 行）：
  - R4 §6 要求网关与扩展容器共用一份注册表。
  - 此协议只约束 `register/has`，**避免 hooks 反向依赖 harness.model**（分层：hooks 不 import harness）。

- **第 31 行** `name: str` 具名。

- **第 33 行** `def register(self, name: str, provider: Provider) -> object: ...`
  注册方法，返回 object（只约束存在，不约束具体返回类型）。

- **第 35 行** `def has(self, name: str) -> bool: ...` 查询方法。

### 第 38-41 行：Provider 协议

- **第 38 行** `@runtime_checkable`。

- **第 39 行** `class Provider(Protocol):` 所有可替换策略的基契约。

- **第 40 行** docstring：`带 name、可被替换`。

- **第 41 行** `name: str` 唯一要求——结构化类型，任何带 name 的对象都算 Provider。

### 第 44-62 行：Capability 协议

- **第 44 行** `@runtime_checkable`。

- **第 45 行** `class Capability(Protocol):`
  能力级开关：**装了通过 install 生效，不装缺席（缺席即降级）**。

- **第 46-50 行** docstring 属性：name 能力名、version 版本、requires 依赖的其他能力名。

- **第 51-53 行** 三个属性：
  - **第 51 行** `name: str`
  - **第 52 行** `version: str`
  - **第 53 行** `requires: tuple[str, ...]` 依赖列表用 tuple（不可变）。

- **第 55 行** `def install(self, container: object, reg: Registry) -> None:`
  把本能力的增强器装到容器、provider 装到注册表。

- **第 56-61 行** docstring 参数：container（含 registry 属性的装配对象）、reg（Provider 注册表）。"两表统一目标"——增强器表 + provider 表。

- **第 62 行** `...` 协议占位。

### 第 65-70 行：HookBusError 异常

- **第 65 行** `class HookBusError(Exception):`
  装配/执行错误（重名、缺 port、冻结后写入等）。

- **第 66-68 行** docstring：第 68 行关键点——`保留类名以稳定错误码兼容（code="HOOKBUS_ERROR"），语义扩展为共用容器装配错误`。
  为什么保留旧名：错误码是对外契约，改名会破坏上游 catch。

- **第 70 行** `code = "HOOKBUS_ERROR"` 稳定错误码。

### 第 73-138 行：Registry 类（核心实现）

- **第 73 行** `class Registry:` 注册表：name → Provider，支持注册/解析/替换；装配期可 freeze 拒绝后续写入。

- **第 74 行** docstring 同上。

#### 第 76-79 行：__init__

- **第 76 行** `def __init__(self) -> None:`。
- **第 77 行** docstring：`初始化空注册表，装配期 freeze 后拒绝任何写入。`
- **第 78 行** `self._providers: dict[str, Provider] = {}` 存储字典。
- **第 79 行** `self._frozen = False` 冻结标志。

#### 第 81-96 行：register

- **第 81 行** `def register(self, name: str, provider: Provider) -> Registry:`
  注册 provider；重名报错（除非用 replace）；返回 self 支持链式。

- **第 82-90 行** docstring 参数/返回/异常（第 89 行：HookBusError — 已冻结或重名）。

- **第 91-92 行** `if self._frozen: raise HookBusError("registry is frozen")`
  **冻结检查在前**：装配完成后不允许再注册，防止运行期动态改写装配。

- **第 93-94 行** `if name in self._providers: raise HookBusError(f"duplicate provider name: {name}")`
  **重名检查**：防静默覆盖（覆盖要用显式 replace）。

- **第 95 行** `self._providers[name] = provider` 写入。

- **第 96 行** `return self` 支持链式 `reg.register(...).register(...)`。

#### 第 98-110 行：resolve

- **第 98 行** `def resolve(self, name: str) -> Provider:` 解析 provider；缺失报错。
- **第 99-107 行** docstring（第 106 行异常：HookBusError — 未注册）。
- **第 108-109 行** `if name not in self._providers: raise HookBusError(f"provider not registered: {name}")`
  **显式报错**：缺 provider 时在装配期暴露，而不是运行期 None 崩溃。
- **第 110 行** `return self._providers[name]` 返回。

#### 第 112-125 行：replace

- **第 112 行** `def replace(self, name: str, provider: Provider) -> Registry:`
  替换已注册 provider；返回 self。

- **第 113-121 行** docstring（第 120 行异常：仅已冻结）。

- **第 122-123 行** `if self._frozen: raise HookBusError("registry is frozen")`
  冻结后连替换也拒绝——**冻结是彻底只读**。

- **第 124 行** `self._providers[name] = provider`
  直接覆盖（不检查存在性：替换不存在的名字等于注册，宽容）。

- **第 125 行** `return self` 链式。

#### 第 127-133 行：has / names

- **第 127 行** `def has(self, name: str) -> bool:` 是否已注册。

- **第 128 行** docstring。

- **第 129 行** `return name in self._providers` 成员判断。

- **第 131 行** `def names(self) -> Iterable[str]:` 已注册名称列表（快照）。

- **第 132 行** docstring："快照"——返回新 list，外部修改不影响内部。

- **第 133 行** `return list(self._providers)` 构造快照（dict 迭代顺序 = 插入顺序，确定）。

#### 第 135-138 行：freeze

- **第 135 行** `def freeze(self) -> Registry:` 冻结注册表。

- **第 136 行** docstring：`装配期完成后），此后 register/replace 均拒绝`。

- **第 137 行** `self._frozen = True` 置标志。

- **第 138 行** `return self` 链式（`reg.freeze()` 后仍可 resolve/has/names）。

---

## 5. notify.py 逐行讲解（212 行）

"三体系正交"里的 **when 维度**：回答"发生了什么"。核心纪律——订阅者收到 Notice 只做事，**返回值框架不消费**（不能拦截、不能改写），异常就地隔离。与 AOP Enhancer（how，返回值被消费）严格分开。

### 第 1-9 行：模块 docstring

- **第 1 行** `"""通知体系：16 个相位 + 通知总线（原 R2 hooks/lifecycle.py 归位组合层，规格 P2）。`
  定位 + 沿革 + 相位数量（16）。

- **第 3-5 行** when 维度说明：
  - 第 3 行：`三体系正交中的 "when" 维度：回答"发生了什么"`。
  - 第 4-5 行：订阅者收到 Notice 只做事，返回值框架不消费——不能拦截、不能改写、异常就地隔离。与 AOP Enhancer（how，返回值被消费）严格分开。

- **第 7-8 行** 依赖方向：`订阅插件 → LifecycleBus → 内核通知；本模块不 import runtime/aop 实现，Notice 的调用上下文以只读投影字段承载（结构化，避免循环依赖）。`
  **关键**：notify 不 import runtime，靠只读投影字段承载上下文，避免循环依赖。

- **第 9 行** `"""` 结束。

### 第 10-28 行：imports、TYPE_CHECKING 与 __all__

- **第 10 行** `from __future__ import annotations` 延迟注解（配合 TYPE_CHECKING 引用 Invocation）。

- **第 12 行** `from collections.abc import Callable, Mapping` 类型标注。

- **第 13 行** `from dataclasses import dataclass, field` Notice 用 frozen dataclass + field(default_factory)。

- **第 14 行** `from enum import StrEnum` 16 相位枚举（StrEnum 而非 Enum：可直接当字符串）。

- **第 15 行** `from types import MappingProxyType` 只读字典视图。

- **第 16 行** `from typing import TYPE_CHECKING, Any` 类型检查期引用 + 动态载荷类型。

- **第 18-19 行** `if TYPE_CHECKING: from compose import Invocation`
  **仅类型检查期引用**，运行期不 import——避免与 compose/__init__.py 循环依赖。Invocation 只用于注解，延迟求值下无需运行期存在。

- **第 21-28 行** `__all__`：PHASE_COUNT / ErrorHook / LifecycleBus / LifecyclePhase / Notice / runner_notifier。

### 第 31-56 行：LifecyclePhase 枚举（16 相位）

- **第 31 行** `class LifecyclePhase(StrEnum):`
  16 个通知相位（**最终口径，不得增删**）。StrEnum 让成员本身就是字符串，便于与 Runner 的相位名字符串对接。

- **第 32-40 行** docstring 分组：
  - 第 35 行 会话/轮次：session_start / user_prompt / session_end / state_fold / budget_check
  - 第 36 行 能力调用：before_model / after_model / before_tool / after_tool / tool_error
  - 第 37 行 子 Agent：child_spawn / child_done
  - 第 38 行 控制流：cancelled / wait_human / resumed
  - 第 39 行 AOP 拒绝：invoke_rejected

- **第 41-56 行** 16 个成员逐行：
  - **第 41 行** `SESSION_START = "session_start"`
  - **第 42 行** `USER_PROMPT = "user_prompt"`
  - **第 43 行** `SESSION_END = "session_end"`
  - **第 44 行** `STATE_FOLD = "state_fold"`
  - **第 45 行** `BUDGET_CHECK = "budget_check"`
  - **第 46 行** `BEFORE_MODEL = "before_model"`
  - **第 47 行** `AFTER_MODEL = "after_model"`
  - **第 48 行** `BEFORE_TOOL = "before_tool"`
  - **第 49 行** `AFTER_TOOL = "after_tool"`
  - **第 50 行** `TOOL_ERROR = "tool_error"`
  - **第 51 行** `CHILD_SPAWN = "child_spawn"`
  - **第 52 行** `CHILD_DONE = "child_done"`
  - **第 53 行** `CANCELLED = "cancelled"`
  - **第 54 行** `WAIT_HUMAN = "wait_human"`
  - **第 55 行** `RESUMED = "resumed"`
  - **第 56 行** `INVOKE_REJECTED = "invoke_rejected"`

  为什么"不得增删"：相位是跨层契约，Runner 发、总线收，任一侧增删都会错位。

### 第 59-60 行：PHASE_COUNT

- **第 59 行** `PHASE_COUNT = len(LifecyclePhase)`
  相位数量 = 枚举长度。**不写死 16**：口径自证，增删成员时自动同步。

- **第 60 行** docstring：`通知相位数量（16，口径自证的单一来源）。`

### 第 63-108 行：Notice 值对象

- **第 63 行** `@dataclass(frozen=True)` 不可变（注意这里没有 slots，因为有 __post_init__ 用 object.__setattr__）。

- **第 64 行** `class Notice:` 通知载荷：Invocation 的**只读投影** + 相位名 + 专属 payload。

- **第 65-73 行** docstring 字段说明：
  - 第 69 行：session_id 从 Invocation.session/projection 派生。
  - 第 70 行：port 能力端口名（Invoke 场景；非能力相位为空串）。
  - 第 72 行：meta 是 Invocation.meta 的只读快照（**单一上下文，不另建状态**）。

- **第 74-78 行** 五个字段：
  - **第 74 行** `phase: LifecyclePhase`
  - **第 75 行** `session_id: str = ""`
  - **第 76 行** `port: str = ""`
  - **第 77 行** `payload: Mapping[str, Any] = field(default_factory=dict)`
  - **第 78 行** `meta: Mapping[str, Any] = field(default_factory=dict)`

#### 第 80-83 行：__post_init__（冻结视图）

- **第 80 行** `def __post_init__(self) -> None:`
  构造后钩子：冻结 payload/meta 为只读视图。

- **第 81 行** docstring：`frozen 下用 object.__setattr__`。

- **第 82 行** `object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))`
  绕过 frozen 限制，把 payload 包成 `MappingProxyType` 只读视图 + 复制一份 dict。
  - 双重保险：① 复制（外部改原 dict 不影响）；② 只读代理（订阅者不能改）。

- **第 83 行** `object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))`
  meta 同理。

#### 第 85-108 行：from_invocation 类方法

- **第 85-92 行** 签名：`cls, phase, inv: Invocation, *, payload=None` → Notice。

- **第 93-101 行** docstring：`由 Invocation 投影出 Notice（上下文单一：Notice 只读，不复制可变状态）`。

- **第 102-108 行** 构造：
  - **第 103 行** `phase=phase`
  - **第 104 行** `session_id=str(getattr(inv.session, "session_id", "") or "")`
    **防御式取值**：getattr 带缺省 `or ""`——session 缺 session_id 属性或为 None 都不崩。
  - **第 105 行** `port=str(getattr(inv.effect, "port", "") or "")` 同理取 port。
  - **第 106 行** `payload=payload or {}` 缺省空。
  - **第 107 行** `meta=inv.meta` 直接引用 Invocation 的 meta（Notice.__post_init__ 会复制+冻结）。

### 第 111-115 行：类型别名

- **第 111 行** `type NoticeFn = Callable[[Notice], None]`
  订阅者签名：收 Notice、无返回值（**返回值框架不消费**）。

- **第 112 行** docstring 同上。

- **第 114 行** `type ErrorHook = Callable[[str, LifecyclePhase, Exception], None]`
  订阅者异常钩子：(订阅者名, 相位, 异常)。

- **第 115 行** docstring：`由装配层决定是否接 R10 trace`——钩子可选，默认静默。

### 第 118-185 行：LifecycleBus 类

- **第 118 行** `class LifecycleBus:`
  通知总线：按相位注册订阅者，emit 时异常就地隔离、不阻断其余订阅与主流程。

- **第 119-123 行** docstring：
  - 第 121 行：同步语义——emit 不返回 awaitable；订阅者是同步副作用（记录/观测/审计）。
  - 第 122 行：异常默认静默隔离；可选 on_error 钩子让装配层接可观测（R10）。

- **第 124 行** `__slots__ = ("_on_error", "_subscribers")`
  限制属性，省内存、防误加属性。

#### 第 126-133 行：__init__

- **第 126 行** `def __init__(self, on_error: ErrorHook | None = None) -> None:`
- **第 127-131 行** docstring（第 130 行：缺省 None 保持静默隔离，向后兼容）。
- **第 132 行** `self._subscribers: dict[LifecyclePhase, list[tuple[str, NoticeFn]]] = {}`
  相位 → [(订阅者名, 回调)] 列表。
- **第 133 行** `self._on_error = on_error` 保存错误钩子。

#### 第 135-147 行：on（订阅）

- **第 135 行** `def on(self, phase, fn, *, name="") -> LifecycleBus:` 订阅一个相位。

- **第 136-144 行** docstring（第 140 行：fn 名字 + register(bus) 的插件持有自身状态；第 141 行：name 缺省用函数名）。

- **第 145 行** `key = name or getattr(fn, "__name__", "anonymous")`
  **具名兜底链**：显式 name → 函数名 → "anonymous"。三级兜底保证总有名字进 trace。

- **第 146 行** `self._subscribers.setdefault(phase, []).append((key, fn))`
  setdefault 创建新列表（首次订阅该相位时），追加。

- **第 147 行** `return self` 链式订阅。

#### 第 149-168 行：emit（广播 · 异常隔离核心）

- **第 149 行** `def emit(self, notice: Notice) -> None:` 广播通知。

- **第 150 行** docstring：`异常就地隔离，保证一个坏订阅者不影响其余订阅与主流程。`

- **第 155 行** `for name, fn in self._subscribers.get(notice.phase, ()):`
  只遍历该相位的订阅者，缺省空 tuple（不报错）。

- **第 156-157 行** `try: fn(notice)` 调用订阅者。

- **第 158 行** `except Exception as exc:  # 通知型 hook：异常就地隔离，绝不抛穿  # noqa: BLE001`
  **捕获一切异常**：通知型 hook 绝不能把异常抛给主流程。

- **第 159-162 行** 注释关键（第 161-162 行）：
  - 钩子自身的异常同样就地隔离——except 块内新异常不会被本 try 捕获，若不二次包裹会从 emit 抛穿，破坏"绝不抛穿"铁律并覆盖调用方结果。

- **第 163-167 行** 错误钩子调用（二次保护）：
  - **第 163 行** `if self._on_error is not None:` 有钩子才调。
  - **第 164-165 行** `try: self._on_error(name, notice.phase, exc)` 把异常交给装配层观测。
  - **第 166-167 行** `except Exception: pass  # 错误钩子自身异常同样隔离`
    **二次隔离**：连错误钩子自己报错也吞掉（否则会从 emit 抛穿）。

- **第 168 行** `continue` 继续下一个订阅者。

#### 第 170-185 行：subscribe（插件注册）

- **第 170 行** `def subscribe(self, plugin: object) -> LifecycleBus:`
  注册 Lifecycle 插件（契约：name + register(bus)）。

- **第 171-179 行** docstring（第 178 行异常：ValueError — 插件缺 name 或 register）。

- **第 180-181 行** `pname = getattr(plugin, "name", None)` / `register = getattr(plugin, "register", None)`
  防御式取属性。

- **第 182-183 行** `if not pname or not callable(register): raise ValueError("lifecycle plugin must have name and register(bus)")`
  **装配期校验**：缺契约立即报错，不带病运行。

- **第 184 行** `register(self)` 把总线交给插件，插件自行订阅若干相位。

- **第 185 行** `return self` 链式。

### 第 188-212 行：runner_notifier（装配接线入口）

- **第 188 行** `def runner_notifier(bus: LifecycleBus) -> Callable[[str, Mapping[str, Any]], None]:`
  把 Runner 的 notify 回调桥接到 LifecycleBus。

- **第 189-199 行** docstring 关键：
  - 第 191-192 行：Runner 内核零依赖 hooks，只发 `(相位名字符串, 只读投影)`。
  - 第 192-193 行：本函数把相位名归一为 LifecyclePhase、投影包成 Notice 发到总线——补上"9 个 Runner 相位发到 no-op 无人接"的接线断点（规格 P2）。
  - 第 198 行：未知相位名安全忽略（不抛穿内核）。

- **第 200 行** `def notify(phase_name: str, projection: Mapping[str, Any]) -> None:` 内部回调。

- **第 201-202 行** `try: phase = LifecyclePhase(phase_name)`
  字符串构造成枚举成员（StrEnum 直接支持）。

- **第 203-204 行** `except ValueError: return  # 未知相位：安全忽略，不让通知打断主流程`
  **未知相位容错**：Runner 可能发枚举外的相位名，忽略而不报错。

- **第 205-210 行** `bus.emit(Notice(...))`：
  - **第 206 行** `phase=phase`
  - **第 207 行** `session_id=str(projection.get("session_id", "") or "")`
  - **第 208 行** `port=str(projection.get("port", "") or "")`
  - **第 209 行** `payload=projection` 整个投影作为 payload。

- **第 212 行** `return notify` 返回可直接传给 `Runner(notify=...)` 的回调。

---

## 6. call.py 逐行讲解（118 行）

③ call 装饰器：`CallFn → CallFn` 纯函数组合，`build_call` 构造注入 R1 的 call。四件套 retry/timeout/danger_route/injection_guard **只覆盖 Invoke**（D-7）。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""③ call 装饰器（规格 §2.4）：CallFn → CallFn 纯函数组合 + build_call。`
  定位：call 级纯变换。

- **第 3-4 行** `retry（调用级）/timeout/danger_route/injection_guard 均为函数组合，无固定角色链；只覆盖 Invoke（D-7）。R3 可靠性由 harness.resilience.ReliabilityExecutor 显式编排。`
  两句关键：① 装饰器无固定角色链（不是 AOP 那种固定顺序）；② **只覆盖 Invoke**，非 Invoke 走 R1 快路径；③ R3 可靠性另有显式执行器，别和这里的 retry 混淆。

- **第 5 行** `"""` 结束。

### 第 6-17 行：imports 与 __all__

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `import asyncio` retry_call 的 sleep、timeout 的 wait_for 用。

- **第 9 行** `from collections.abc import Awaitable, Callable, Sequence`
  Awaitable 未直接用（历史遗留 import）；Callable/Sequence 用于签名。

- **第 10 行** `from typing import Final` _Keys 常量。

- **第 12 行** `from runtime import CallFn, DynamicValue, Effect, Invoke, Result`
  依赖方向恒定：只 import runtime。

- **第 14-17 行** `__all__`：`CallFn, build_call, retry_call, timeout, danger_route, injection_guard`。

### 第 20-23 行：_Keys

- **第 20 行** `class _Keys:` call 装饰器私有 scratch 键（x-* 约定），集中定义禁用魔法字符串。
- **第 22 行** `PREFIX: Final[str] = "x-compose."` 统一前缀（与 program.py 同前缀）。
- **第 23 行** `ROUTE: Final[str] = PREFIX + "danger_route"` 路由记录键。

### 第 26-29 行：_ctx_meta 辅助函数

- **第 26 行** `def _ctx_meta(ctx: object) -> dict[str, DynamicValue]:`
  从 R1 RuntimeCtx 取 meta 字典（缺省空），供装饰器记录具名协作数据。

- **第 28 行** `meta = getattr(ctx, "meta", None)` 防御式取属性。

- **第 29 行** `return meta if isinstance(meta, dict) else {}`
  类型不符返回**新空字典**——注意：这个空字典不会被挂回 ctx（与 guards.py 的 CallState._meta 不同，那里会 setattr 挂回）。这里只是给 danger_route 写个路由标记，写进临时字典影响有限。

### 第 32-62 行：retry_call（调用级重试）

- **第 32-38 行** 签名：`base: CallFn`、`attempts=3`、`backoff=0.0`、`retry_on: Callable[[Result], bool] | None = None`。

- **第 39 行** docstring：调用级 retry（与 A 类流程级 retry 区分，D-5）。

- **第 41-48 行** Args/Returns：`retry_on` 缺省按 `Result.error.retryable` 判定。

- **第 50 行** `should_retry = retry_on or (lambda r: bool(r.error and r.error.retryable))`
  **缺省策略**：自定义 retry_on 优先；否则 `result.error` 存在且 `retryable` 为真才重试。
  - `bool(...)` 包裹：确保返回 bool（error 可能为 None）。

- **第 52 行** `async def call(effect: Effect, ctx: object) -> Result:` 内部 CallFn。

- **第 53 行** `result: Result | None = None` 结果预置。

- **第 54 行** `for _ in range(max(1, attempts)):` 至少 1 次。

- **第 55 行** `result = await base(effect, ctx)` 调用内层（可能是 base dispatch 或其它装饰器）。

- **第 56-57 行** `if result.ok or not should_retry(result): return result`
  **成功或不该重试 → 立即返回**（短路）。

- **第 58-59 行** `if backoff > 0: await asyncio.sleep(backoff)` 退避（异步 sleep 不阻塞事件循环）。

- **第 60 行** `return result or Result.failure("RETRY_EXHAUSTED", "no attempt")`
  兜底：循环结束（理论上不会，因为至少 1 次）返回耗尽错误。

- **第 62 行** `return call` 返回新 CallFn。

### 第 65-72 行：timeout（调用级超时）

- **第 65 行** `def timeout(base: CallFn, *, seconds: float) -> CallFn:`
  wait_for 包 base（R1 无 wait_for 限制见 D-5）。

- **第 66 行** docstring 同上。

- **第 67 行** `async def call(effect: Effect, ctx: object) -> Result:`。

- **第 68-69 行** `try: return await asyncio.wait_for(base(effect, ctx), timeout=seconds)`
  超时控制交给 asyncio.wait_for。

- **第 70-71 行** `except TimeoutError: return Result.failure("TIMEOUT", f"call exceeded {seconds}s")`
  **异常折叠为 Result**：超时不抛穿，返回失败 Result（规则：异常折叠为 Result）。

- **第 72 行** `return call`。

### 第 75-82 行：danger_route（危险路由）

- **第 75 行** `def danger_route(base: CallFn, registry: object) -> CallFn:`
  按 payload.danger 经 registry 路由到沙箱 Port（沙箱是⑤Port）。

- **第 76 行** docstring 同上。

- **第 77 行** `async def call(effect: Effect, ctx: object) -> Result:`。

- **第 78 行** `resolve = getattr(registry, "resolve", None)` 防御式取 resolve 方法。

- **第 79-80 行** `if callable(resolve) and isinstance(effect, Invoke): _ctx_meta(ctx)[_Keys.ROUTE] = effect.payload.get("danger", "")`
  **只覆盖 Invoke**（D-7）：非 Invoke 不处理；有 resolve 能力才记录路由标记到 ctx.meta 的 `x-compose.danger_route` 键。
  - 注意这里只是**记录**，真正的路由由后续 ReliabilityExecutor/网关消费——装饰器只做标记，不改 Effect。

- **第 81 行** `return await base(effect, ctx)` 透传给内层。

- **第 82 行** `return call`。

### 第 85-95 行：injection_guard（注入检测）

- **第 85 行** `def injection_guard(base: CallFn, detector: object) -> CallFn:`
  不可信输入过 detector，命中则拦截（fail-closed）。

- **第 86 行** docstring 同上。

- **第 87 行** `async def call(effect: Effect, ctx: object) -> Result:`。

- **第 88 行** `detect = getattr(detector, "detect", None)` 防御式取 detect 方法。

- **第 89-90 行** `if callable(detect) and isinstance(effect, Invoke): verdict = detect(effect.payload, ctx)`
  **只覆盖 Invoke**：非 Invoke 不检测。调 detector 得到裁决。

- **第 91 行** `allowed = getattr(verdict, "allowed", None)` 防御式取 allowed 属性（裁决是鸭子类型，不假设具体类）。

- **第 92-93 行** `if allowed is False: return Result.failure("INJECTION_DETECTED", "untrusted input rejected")`
  **fail-closed**：只有明确 `allowed is False` 才拦截（用 `is False` 而非 `not allowed`——None 未知时不拦，避免误杀）。

- **第 94 行** `return await base(effect, ctx)` 未命中透传。

- **第 95 行** `return call`。

### 第 98-118 行：build_call（构造注入 R1 的 call）

- **第 98-102 行** 签名：`base: CallFn`、`decorators: Sequence[Callable[[CallFn], CallFn]] = ()`。

- **第 103 行** docstring：`构造注入 R1 的 call：③装饰器链（内层先注册）。`

- **第 105-106 行** 组合顺序：`base → decorators[0] → ... → decorators[n]`；`Runner(call=build_call(...)) —— R1 一行不改`。
  **关键理解**：装饰器相对 base 都在"外层"依次包裹——列表靠前的先被包进去（离 base 最近、运行时最内），列表靠后的后包（最外层、运行时**先执行**）。注册顺序与执行顺序相反，这是函数组合的固有性质。

- **第 108-113 行** Args/Returns：base 是中心 dispatch（make_dispatch 产出）；decorators 如 retry_call/timeout。

- **第 115 行** `call = base` 从 base 起。

- **第 116-117 行** `for deco in decorators: call = deco(call)`
  **循环包裹**：每次把当前 call 交给下一个装饰器包一层。

- **第 118 行** `return call` 返回符合 R1 CallFn 签名的最终 call。

---

## 7. guards.py 逐行讲解（89 行）

具名故障 / typed 目标 DTO 与 call 层协作状态。四坑语义（B1/B2/R3-6/R3-7）的载体：从 `Invocation.meta` 改为 `CallState(ctx.meta)`。**本模块只有数据结构与读写，不含任何 check/observe 拦截逻辑**。

### 第 1-11 行：docstring、imports 与 __all__

- **第 1 行** `"""具名故障 / typed 目标 DTO 与 call 层协作状态（领域数据，非拦截协议）。`
  定位：领域数据，不是拦截协议。

- **第 3-4 行** `四坑语义（B1/B2/R3-6/R3-7）的载体：从 Invocation.meta 改为 CallState(ctx.meta)。本模块只有数据结构与读写，不含任何 check/observe 拦截逻辑。`
  点明职责边界：只管数据读写，拦截逻辑在别处。

- **第 5 行** `"""` 结束。

- **第 6 行** `from __future__ import annotations`。

- **第 8 行** `from dataclasses import dataclass` 两个 frozen DTO。

- **第 9 行** `from typing import Final` 协作键常量。

- **第 11 行** `__all__ = ["FaultRecord", "RoutingTarget", "CallState"]` 三符号。

### 第 14-17 行：_Keys 协作键

- **第 14 行** `class _Keys:` ctx.meta 具名协作键（**替代旧 Invocation.meta 裸键**），集中定义禁用魔法字符串。
- **第 16 行** `FAULT: Final[str] = "fault"` 故障键。
- **第 17 行** `ROUTING_TARGET: Final[str] = "routing_target"` 路由目标键。

  注意这里**没有** `x-compose.` 前缀——这是 ctx.meta 的协作键（跨策略共享），与 scratch 的 x-* 键（算子私有进度）用途不同。

### 第 20-29 行：FaultRecord DTO

- **第 20 行** `@dataclass(frozen=True, slots=True)` 不可变值对象。
- **第 21 行** `class FaultRecord:` 具名故障记录（四坑语义载体）。
- **第 22-27 行** docstring Attributes：
  - 第 25 行：`reason` 故障原因（rate_limited/retry_exhausted/timeout/breaker_open...），**只进 trace**。
  - 第 26 行：`failed_target` 降级阶梯后端注册名（B2；R3-7 retry/timeout 必带）。
- **第 28 行** `reason: str` 必填。
- **第 29 行** `failed_target: str = ""` 缺省空串（兼容不需要目标场景）。

### 第 32-41 行：RoutingTarget DTO

- **第 32 行** `@dataclass(frozen=True, slots=True)`。
- **第 33 行** `class RoutingTarget:` typed 降级目标（②守卫裁决 → 网关消费，与 Registry 同源）。
- **第 34-39 行** docstring Attributes：
  - 第 37 行：`target` 后端注册名或 `"refuse"`（拒答也是一个目标）。
  - 第 38 行：`reason` 降级原因（**只进 trace，不参与选后端**）——reason 不进决策，纯观测。
- **第 40 行** `target: str` 必填。
- **第 41 行** `reason: str = ""` 缺省空串。

### 第 44-89 行：CallState 类

- **第 44 行** `class CallState:`
  call 层具名协作状态：从 ctx.meta 读写 fault / routing_target（替代 Invocation.meta）。

- **第 45-49 行** docstring 关键（第 47-48 行）：
  - 状态存在调用上下文（R1 RuntimeCtx.meta）；可靠性执行器与各领域策略**共享同一份 meta**（B3 状态统一）。
  - 单次流程进度**不进策略实例**（D-8）。

- **第 50 行** `def __init__(self, ctx: object) -> None:` 绑定调用上下文。

- **第 51-55 行** docstring：ctx 是 R1 RuntimeCtx（含 meta 字典）或测试最小 ctx。

- **第 56 行** `self._ctx = ctx` 保存。

#### 第 58-67 行：_meta（读写同源）

- **第 58 行** `def _meta(self) -> dict[str, object]:`
  取 ctx.meta 字典（**缺省构造空字典挂回 ctx，保证读写同源**）。

- **第 59 行** docstring 同上。

- **第 60 行** `meta = getattr(self._ctx, "meta", None)` 防御式取值。

- **第 61-66 行** `if not isinstance(meta, dict): meta = {}; try: setattr(self._ctx, "meta", meta); except (AttributeError, TypeError): pass`
  - 非 dict → 新建空字典（第 62 行）。
  - **挂回 ctx**（第 64 行）：保证后续读写都看到同一份。
  - **异常吞掉**（第 65-66 行）：ctx 不可写（如 frozen）时仅本地读写，不崩。

- **第 67 行** `return meta` 返回（一定可用）。

#### 第 69-89 行：读写方法

- **第 69 行** `def write_fault(self, fault: FaultRecord) -> None:` 写入具名故障记录（领域策略报告故障的唯一入口）。
- **第 71 行** `self._meta()[_Keys.FAULT] = fault` 写 `fault` 键。

- **第 73 行** `def fault(self) -> FaultRecord | None:` 读取当前故障记录。
- **第 75-76 行** `value = self._meta().get(_Keys.FAULT); return value if isinstance(value, FaultRecord) else None`
  **类型校验**：类型不符返回 None（防脏数据混入）。

- **第 78 行** `def write_routing_target(self, target: RoutingTarget) -> None:` 写入 typed 降级目标。
- **第 80 行** `self._meta()[_Keys.ROUTING_TARGET] = target` 写 `routing_target` 键。

- **第 82 行** `def routing_target(self) -> RoutingTarget | None:` 读取 typed 降级目标。
- **第 84-85 行** `value = self._meta().get(_Keys.ROUTING_TARGET); return value if isinstance(value, RoutingTarget) else None` 类型校验同 fault。

- **第 87 行** `def session_id(self) -> str:` 从 ctx 派生会话标识（**B1 选级隔离维度的单一入口**）。
- **第 89 行** `return str(getattr(self._ctx, "session_id", "") or "")`
  三级兜底：getattr 缺省 ""、`or ""` 处理 None、str() 归一化。

---

## 8. dispatch.py 逐行讲解（45 行）

按 `effect.port` 分派到 Port，构造 Result（含 Usage）。**最小职责**：`async (effect, ctx) -> Result`；**不构造 Invocation**（R1 明确不构造）。

### 第 1-12 行：docstring、imports 与 __all__

- **第 1 行** `"""dispatch（规格 §2.5）：按 effect.port 分派到 Port，构造 Result（含 Usage）。`
  定位。

- **第 3-4 行** `最小职责：async (effect, ctx) -> Result；不构造 Invocation（R1 明确不构造）。typed target 消费仅降级场景需要，由②降级守卫 + ③装饰器协作完成，dispatch 不感知。`
  两句关键：① **不构造 Invocation**；② dispatch 不感知降级 target（职责单一）。

- **第 5 行** `"""`。

- **第 6 行** `from __future__ import annotations`。

- **第 8 行** `from collections.abc import Callable, Mapping` PortMap 类型。

- **第 10 行** `from runtime import CallFn, DynamicValue, Effect, Invoke, PortError, Result`
  依赖方向：只 import runtime。`PortError` 导入但未直接使用（历史遗留）。

- **第 12 行** `__all__ = ["dispatch", "make_dispatch"]`。

### 第 15-16 行：PortMap 类型别名

- **第 15 行** 注释：`# Port 分派器签名：(effect, ctx) -> Result；由各能力模块（R4/R5/R6/R7）提供。`
- **第 16 行** `PortMap = Mapping[str, Callable[[Invoke, object], Result]]`
  port 名 → 处理器（同步返回 Result；异步由 CallFn 层 await）。

### 第 19-40 行：make_dispatch

- **第 19 行** `def make_dispatch(ports: PortMap) -> CallFn:`
  构造 dispatch：按 effect.port 查 Port 处理器并调用，异常折叠为 Result。

- **第 20-27 行** docstring + Args/Returns：ports 是 port 名 → 处理器。

- **第 28 行** `async def call(effect: Effect, ctx: object) -> Result:` 内部 CallFn。

- **第 29 行** 注释：`# 分支：非 Invoke 不进 dispatch（Spawn/Suspend/Terminate 走 R1 快路径，D-7）。`

- **第 30-31 行** `if not isinstance(effect, Invoke): return Result.failure("INVALID_EFFECT", "dispatch only handles Invoke")`
  **只处理 Invoke**（D-7）：非 Invoke 返回失败 Result。
  - 为什么失败而非透传：dispatch 是 Invoke 的专属分派器，其它 Effect 由 R1 快路径处理，不该到这里。

- **第 32 行** `handler = ports.get(effect.port)` 按 port 名查处理器。

- **第 33-34 行** `if handler is None: return Result.failure("PORT_MISSING", f"port not registered: {effect.port}")`
  **缺 port 显式报错**（折叠为 Result）：装配期漏注册在这里暴露。

- **第 35-36 行** `try: return handler(effect, ctx)` 调用处理器。

- **第 37-38 行** `except Exception as exc: return Result.failure("PORT_CRASHED", f"{type(exc).__name__}: {exc}")`
  **异常折叠为 Result，不抛穿 R1**（`# noqa: BLE001` 抑制过宽异常警告）：带异常类型名 + 消息进 Result。

- **第 40 行** `return call` 返回符合 R1 CallFn 签名的 dispatch。

### 第 43-45 行：dispatch 便捷函数

- **第 43 行** `async def dispatch(ports: PortMap, effect: Effect, ctx: object) -> Result:`
  便捷 dispatch：直接按 ports 分派单个 Effect（make_dispatch 的无封装形态）。

- **第 44 行** docstring 同上。

- **第 45 行** `return await make_dispatch(ports)(effect, ctx)`
  委托：现造一个 dispatch 再调用。每次新建有微小开销，但便捷（无需先 make）。

---

## 9. context.py 逐行讲解（73 行）

调用协作 DTO：CallContext/Invocation/FaultRecord/RoutingTarget/Next。这些是领域调用上下文与故障数据，**不进 R1**（R1 明确不构造 Invocation）。

### 第 1-19 行：docstring、imports 与 __all__

- **第 1 行** `"""调用协作 DTO：CallContext/Invocation/FaultRecord/RoutingTarget/Next。`
  定位。

- **第 3 行** `这些是领域调用上下文与故障数据，不进 R1（R1 明确不构造 Invocation）。`
  **关键边界**：R1 不知道 Invocation 存在，这些 DTO 只活在 compose 层。

- **第 4 行** `"""`。

- **第 5 行** `from __future__ import annotations`。

- **第 7 行** `from collections.abc import Awaitable, Callable, Mapping` Next/meta_view 类型。

- **第 8 行** `from dataclasses import dataclass, field` CallContext 用 @dataclass + field。

- **第 9 行** `from types import MappingProxyType` meta_view 只读快照。

- **第 10 行** `from typing import Any, Final` 动态值类型 + 常量。

- **第 12 行** `from runtime import Effect, Result` 依赖方向：只 import runtime。

- **第 14 行** `from .guards import FaultRecord, RoutingTarget`
  从同包 guards.py 复用两个 typed DTO——**两个 DTO 只有一份定义**，避免重复。

- **第 16-19 行** `__all__`：`CallContext, Invocation, Next, FaultRecord, RoutingTarget, FAULT_KEY, ROUTING_TARGET_KEY`。

### 第 22-23 行：协作键常量

- **第 22 行** `FAULT_KEY: Final[str] = "fault"` 故障键（模块级，与 guards._Keys.FAULT 同值）。
- **第 23 行** `ROUTING_TARGET_KEY: Final[str] = "routing_target"` 路由目标键。

  注意这里有**两套同值键**：CallContext 用模块级常量，CallState 用 `_Keys` 类常量。值一致所以互操作没问题，但定义分散是小瑕疵。

### 第 26-66 行：CallContext DTO

- **第 26 行** `@dataclass` 可变（注意这里**不是 frozen**——CallContext 是可变协作容器，与 guards 的 frozen DTO 不同）。

- **第 27 行** `class CallContext:`
  组合层调用上下文（**原 Invocation 归位**）：effect + ctx + session + 具名 meta。

- **第 28-35 行** docstring 字段说明：
  - 第 33 行：session 含 session_id（B1 隔离维度来源）。
  - 第 34 行：meta 具名协作数据（fault/routing_target），守卫与装饰器共享同一份。

- **第 36-39 行** 四个字段：
  - **第 36 行** `effect: Effect` 当前 Effect（Invoke 类）。
  - **第 37 行** `ctx: object` R1 RuntimeCtx 或测试最小 ctx。
  - **第 38 行** `session: object` 会话视图。
  - **第 39 行** `meta: dict[str, Any] = field(default_factory=dict)` 具名协作数据。

- **第 41-44 行** session_id 属性：
  - **第 41 行** `@property`。
  - **第 42 行** `def session_id(self) -> str:` 从 session 派生会话标识（B1 隔离维度单一入口）。
  - **第 43 行** docstring。
  - **第 44 行** `return str(getattr(self.session, "session_id", "") or "")` 三级兜底。

- **第 46-48 行** write_fault / 实现：
  - **第 46 行** `def write_fault(self, fault: FaultRecord) -> None:`。
  - **第 48 行** `self.meta[FAULT_KEY] = fault` 写 `fault` 键。

- **第 50-53 行** fault 读取：
  - **第 50 行** `def fault(self) -> FaultRecord | None:`。
  - **第 52-53 行** `value = self.meta.get(FAULT_KEY); return value if isinstance(value, FaultRecord) else None` 类型校验。

- **第 55-57 行** write_routing_target：
  - **第 55 行** `def write_routing_target(self, target: RoutingTarget) -> None:`。
  - **第 57 行** `self.meta[ROUTING_TARGET_KEY] = target`。

- **第 59-62 行** routing_target 读取：
  - **第 59 行** `def routing_target(self) -> RoutingTarget | None:`。
  - **第 61-62 行** `value = self.meta.get(ROUTING_TARGET_KEY); return value if isinstance(value, RoutingTarget) else None`。

- **第 64-66 行** meta_view：
  - **第 64 行** `def meta_view(self) -> Mapping[str, object]:` 返回 meta 只读快照（供 notify 投影）。
  - **第 65 行** docstring：`不暴露可变字典`。
  - **第 66 行** `return MappingProxyType(dict(self.meta))` 复制 + 只读代理，双保险。

### 第 69-73 行：Invocation 别名与 Next

- **第 69 行** 注释：`# Invocation 是 CallContext 的归位别名（源码/测试迁移期统一命名；非兼容层，是同一 DTO）`
  **关键澄清**：Invocation 不是新的兼容类，就是 CallContext 的另一个名字。

- **第 70 行** `Invocation = CallContext`
  赋值式别名（不是继承、不是包装）——`Invocation is CallContext` 为真。

- **第 72 行** `type Next = Callable[[CallContext], Awaitable[Result]]`
  领域调用下一跳签名（装饰/包裹中心调用时传递）。Python 3.12 `type` 语句。

- **第 73 行** docstring 同上。

---

## 10. tool_layer.py 逐行讲解（48 行）

ToolLayer 统一外壳：每个能力层 `build() -> Program`。**Program 是 R1 唯一认识的领域无知载体**；本模块把"使用该能力"表达为 `Invoke + Terminate`。

### 第 1-17 行：docstring、imports 与 __all__

- **第 1 行** `"""ToolLayer 统一外壳（规格 §4 / P4-P8）：每个能力层 build() -> Program。`
  定位。

- **第 3 行** `Program 是 R1 唯一认识的领域无知载体（Generator[Effect, DynamicValue, None]）。`
  **领域无知的物理边界**：R1 只认 Program，不认领域类。

- **第 4-6 行** `各 harness/*/capability.py 只持业务实现，外壳把"使用该能力"表达为可组合的 Program：一个 port 对应的 Invoke + Terminate。A 类算子（seq/branch/...）在装配期组合这些 Program，R1 只认 Program、零领域感知（D-7）。`
  解释完整链路：能力层 → 外壳 → A 类组合 → R1。

- **第 8-9 行** `内部业务逻辑（gateway.complete / RAGPipeline.query / ContextMemoryManager.assemble 等）保持不变，由组合层 dispatch 按 port 分派到对应 Port 时消费 payload。`
  **外壳不改业务实现**：业务逻辑在 Port 里，外壳只发 Invoke。

- **第 10 行** `"""`。

- **第 11 行** `from __future__ import annotations`。

- **第 13 行** `from collections.abc import Callable, Mapping`。

- **第 15 行** `from runtime import DynamicValue, Effect, Invoke, Program, Terminate` 依赖方向：只 import runtime。

- **第 17 行** `__all__ = ["layer_program"]` 单符号导出。

### 第 20-48 行：layer_program 函数

- **第 20-25 行** 签名：`port: str`、`payload: Mapping[str, DynamicValue] | None = None`、`output: DynamicValue = None`、`-> Callable[[], Program]`。

- **第 26-40 行** docstring：
  - 第 28-31 行 Args：port（dispatch 按此分派）、payload（随 Invoke 携带的只读输入）、output（Terminate 承载的输出，组合算子按此串联）。
  - 第 34 行：Returns `Program 工厂（每次调用产出新 generator，resume 可重建，D-8）`。
  - 第 36-39 行 用法示例：`class MyCapability: def build(self) -> Program: return layer_program("rag", payload={"layer": "rag"})()`。

- **第 41 行** `data: dict[str, DynamicValue] = dict(payload or {})`
  **复制 payload 为 dict**：外部传 Mapping（可能只读/可变），复制成内部 dict 后 `**data` 展开安全；同时快照隔离外部后续修改。

- **第 43 行** `def program() -> Program:` 内部 Program 生成器工厂。

- **第 44 行** `yield Invoke(port=port).with_payload(**data)`
  第一步：发 Invoke，携带 port 与 payload。dispatch 收到后按 port 分派到对应 Port 执行业务。

- **第 45 行** `yield Terminate(output=output)`
  第二步：发 Terminate，携带预设 output。**固定的两步结构**：Invoke（做事）→ Terminate（收尾）。

- **第 46 行** `return None` 生成器显式返回（产出 Terminate 后结束）。

- **第 48 行** `return program` 返回工厂（不是 generator 实例）——D-8 resume 可重建。

---

## 11. failure.py 逐行讲解（23 行）

最短的文件：失败分类 DTO（领域概念，非编排角色）。R3 重试/熔断判定结果，独立于任何执行器或角色协议。

### 第 1-9 行：docstring、imports 与 __all__

- **第 1 行** `"""失败分类 DTO（领域概念，非编排角色）。`
  定位：领域概念，不是编排角色——强调它不参与 Program/CallFn 组合。

- **第 3 行** `R3 重试/熔断判定结果，独立于任何执行器或角色协议。`
  独立于执行器：任何执行器都能消费这个判定结果。

- **第 4 行** `"""`。

- **第 5 行** `from __future__ import annotations`。

- **第 7 行** `from dataclasses import dataclass`。

- **第 9 行** `__all__ = ["FailureClass"]` 单符号导出。

### 第 12-23 行：FailureClass

- **第 12 行** `@dataclass(frozen=True)` 不可变（无 slots，保持简单）。
- **第 13 行** `class FailureClass:` 失败分类（R3 重试/熔断判定结果）。
- **第 14-20 行** docstring 字段说明：
  - 第 17 行：`code` 失败类别码（如 retryable/terminal）。
  - 第 18 行：`retryable` 是否可重试。
  - 第 19 行：`suggested_delay` 建议退避秒数（服务端 Retry-After）。
- **第 21 行** `code: str` 必填。
- **第 22 行** `retryable: bool = False` 缺省不可重试（保守）。
- **第 23 行** `suggested_delay: float | None = None` 缺省无建议。

---

## 12. __init__.py 逐行讲解（83 行）

包级 API 收敛：外部只需 `from compose import ...`，不必知道内部文件路径。按"依赖方向"分组导入再统一导出。

### 第 1-8 行：模块 docstring 与 future import

- **第 1 行** `"""组合层 = 算子核心：A 类算子装配 Program + B/C 类构造注入 R1 的 call。`
  一句话定位全包。

- **第 3 行** `框架真正核心。依赖方向：工具层(R3~R9) → 组合层(本包) → 内核 R1。`
  **依赖方向恒定**的权威声明。

- **第 4 行** `R1 一行不改，只解释 Program；本包构造 call 注入 Runner(call=...)。`
  R1 零改动的承诺 + 本包的注入方式。

- **第 6 行** `公共 API 见 docs/02-模块详设/Agent/R2-算子与装配.md 与 R12-内置架构与组合.md。`
  指路详设。

- **第 7 行** `"""`。

- **第 8 行** `from __future__ import annotations`。

### 第 10-57 行：分组导入（按依赖层次）

- **第 10-13 行** `from .context import (CallContext, FaultRecord, RoutingTarget, Invocation, Next, FAULT_KEY, ROUTING_TARGET_KEY)`
  调用协作 DTO。注意这里导入的 `FaultRecord/RoutingTarget` 来自 context.py（context.py 第 14 行又从 guards.py 导入），所以是同一对象。

- **第 14 行** `from .failure import FailureClass` 失败分类。

- **第 15-22 行** `from .call import (CallFn, build_call, danger_route, injection_guard, retry_call, timeout)`
  call 装饰器 + build_call。`CallFn` 是 call.py 从 runtime 导入后在此重导出。

- **第 23 行** `from .dispatch import dispatch, make_dispatch` 分派。

- **第 24 行** `from .tool_layer import layer_program` Program 外壳。

- **第 25-29 行** `from .guards import (CallState, FaultRecord, RoutingTarget)`
  guards 的协作状态。`FaultRecord/RoutingTarget` **再次导入**（与第 11 行重复）——因为 context.py 和 guards.py 都导出它们，这里是兼容两种 import 路径；由于指向同一对象，重复无害。

- **第 30-41 行** `from .program import (branch, fallback, gate, loop, map_effect, map_state, par, race, retry, seq)`
  10 个 A 类算子，全部重导出。

- **第 42 行** `# 原 R2 hooks 资产归位（规格 P2）：DTO / Port 协议 / Registry / notify 相位。`
  分组注释：从第 43 行起是 P2 归位的 hooks 资产。

- **第 43-46 行** `from .dto import (DangerLevel, DisclosurePacket, LLMRequest, LLMResponse, PortCtx, SkillManifest, StreamEvent, StreamEventKind, ToolCall, ToolResult, ToolSpec, Usage, make_call)`
  13 个 DTO。

- **第 47-49 行** `from .registry import (Capability, HookBusError, Provider, ProviderRegistry, Registry)`
  注册表与可插拔基契约。

- **第 50-54 行** `from .ports import (PORT_PROTOCOLS, AuditPort, AuditStore, DisclosureIndex, EvalPort, EventStreamPort, HumanPort, LLMPort, MCPPort, MemoryPort, RAGPort, SandboxPort, SkillPort, StorePort, TelemetryExporter, TelemetryPort, ToolPort)`
  协议目录 + 16 个协议。

- **第 55-57 行** `from .notify import (PHASE_COUNT, ErrorHook, LifecycleBus, LifecyclePhase, Notice, runner_notifier)`
  通知体系。

### 第 59-83 行：__all__ 公共导出面

- **第 59 行** `__all__ = [` 开始。

- **第 60-62 行** ①A 类算子（Program → Program）：seq/par/branch/race/loop/map_state/map_effect/fallback/gate/retry 十个。

- **第 63-65 行** call 装饰器 / 装配：CallFn/build_call + retry_call/timeout/danger_route/injection_guard。

- **第 66-67 行** dispatch：dispatch/make_dispatch。

- **第 68-69 行** ToolLayer 外壳（P4-P8）：layer_program。

- **第 70-73 行** 协作 DTO（领域语义，无编排角色）：CallContext/FaultRecord/RoutingTarget/Invocation/Next、FAULT_KEY/ROUTING_TARGET_KEY、FailureClass/CallState。

- **第 74-77 行** 原 R2 hooks 资产归位（规格 P2）：DangerLevel/DisclosurePacket/LLMRequest/LLMResponse/PortCtx/SkillManifest/StreamEvent/StreamEventKind/ToolCall/ToolResult/ToolSpec/Usage/make_call。

- **第 78 行** Capability/HookBusError/Provider/ProviderRegistry/Registry。

- **第 79-81 行** PORT_PROTOCOLS + 16 个协议名。

- **第 82 行** PHASE_COUNT/ErrorHook/LifecycleBus/LifecyclePhase/Notice/runner_notifier。

- **第 83 行** `]` 结束。全包对外导出约 60 个符号——**API 收敛**的单一出口，上层模块从这里一次性拿齐所有能力。

---

## 13. 逐行讲解后的整体心智图

### 13.1 依赖方向（恒定单向）

```
工具/能力层 R3~R9（harness/*）
        │  import compose（拿 Port/算子/DTO/外壳）
        ▼
compose 装配层（本包，12 文件）
        │  import runtime（仅此一个下游依赖）
        ▼
runtime 内核 R1（一行不改，只解释 Program / 执行 Effect / fold State）
```

- 全包所有 `from runtime import ...`：program.py、call.py、dispatch.py、context.py、tool_layer.py、ports.py、dto.py。
- **没有任何** `from harness...` 或 `from model...`——依赖方向是一条不可逆的单向边。

### 13.2 七种承载形态一览

```
① 算子        program.py   10 个 A 类：seq/par/branch/race/loop/map_state/map_effect/fallback/gate/retry
                           装配期 Program → Program，运行期只发 Effect
② 守卫        guards.py    FaultRecord/RoutingTarget/CallState（只数据，无拦截）
③ call 装饰器 call.py      retry_call/timeout/danger_route/injection_guard + build_call
                           只覆盖 Invoke，CallFn → CallFn
④ notify      notify.py    16 相位 LifecyclePhase + Notice(只读) + LifecycleBus(异常隔离)
⑤ Port        ports.py     16 个 Protocol + PORT_PROTOCOLS(14 键) 履约校验目录
⑥ 组件        registry.py  Registry(注册/替换/freeze)  +  tool_layer.py  layer_program(Program 外壳)
⑦ 内核机制    dispatch.py  按 port 分派，不构造 Invocation
              context.py   CallContext/Invocation 别名/Next
              failure.py   FailureClass 失败分类
              dto.py       跨模块 DTO 契约（DangerLevel 权威定义在此）
```

### 13.3 A 类算子：装配期纯变换 + 运行期 carry

```
装配期：compile = seq(a, b)  ─►  返回 program 工厂（不改 a/b，幂等）
                                   │
运行期：每次 program()  ─►  新 generator（D-8 resume 可重建）
                                   │
        _run_child(子 Program, State)
          ├─ next(gen) 拿首个 Effect
          ├─ yield effect 给 R1 执行 → 收回 Result.data
          ├─ Invoke 时挑 payload 的 x-* 键合并进局部 state
          ├─ gen.send(sent) 推进
          ├─ 遇 Terminate → 拦截，return (output, state)  ← 不终结整个 run
          └─ StopIteration → return (state.output, state)
                                   │
        算子间传值：state.set("x-compose.*", output) → 下游从 scratch 读
        （R1 fold 不感知 x-*，组合层自持 carry）
```

### 13.4 par 的 lockstep 并行批

```
gens = [child() for each item]
   │  逐个 next(gen)：Terminate→记账 output，其余→pending[i]
   ▼
while pending:
   batch = [pending[slot] for slot in slots]
   injected = yield batch      ← 一次 yield 列表！R1 interpreter 认并行批
                                 runner gather 并发执行 → 按序回注入
   for slot, value in zip(slots, values):
       gen.send(value) → 记账或再进 pending
   ▼
ordered = [outputs[i] for i in range(n)]  ← 按槽位顺序，确定性
Terminate(output=_join_outputs(join, ordered))
   any      → 首个非 None
   majority → 计数，max((票数, -首现index))，平票取首现
   all      → 完整 list
```

### 13.5 gate：唯一能 Suspend 的算子

```
program()
   │  yield Invoke(state, op=read) → 拿 State
   │  answer = state.last_wait_human
   ├─ policy(state) 非 None 且 answer 为 None
   │      → yield Suspend(question="approval", options=("yes","no"))  ← 挂起
   │      → 人工回答回注到 State.last_wait_human
   ├─ resume：再次进入，answer 已填 → 跳过挂起
   └─ 跑 p → Terminate(output)
```

### 13.6 call 装饰器链与 R1 接线

```
Runner(call=build_call(base=make_dispatch(ports),
                       decorators=[retry_call, timeout, danger_route, ...]))
   │
   │  build_call: call = base; for d in decorators: call = d(call)
   │  列表靠前 → 离 base 近（内层，后执行）；靠后 → 外层，先执行
   ▼
调用链（由外到内）：danger_route/injection_guard(只标记 Invoke) → timeout → retry_call → dispatch
   │  dispatch: 非 Invoke → INVALID_EFFECT；缺 port → PORT_MISSING；异常 → PORT_CRASHED
   ▼
Result（异常全部折叠为 Result，不抛穿 R1）
```

### 13.7 notify 接线（Runner → LifecycleBus）

```
Runner 内核（零 hooks 依赖）只发 (相位名字符串, 只读投影)
        │  Runner(notify=runner_notifier(bus))
        ▼
runner_notifier: 字符串 → LifecyclePhase（未知相位安全忽略）
        │  bus.emit(Notice(只读投影 + payload))
        ▼
LifecycleBus.emit: 逐订阅者调用，异常就地隔离（含 on_error 钩子二次隔离）
        订阅者返回值不消费 — 不能拦截、不能改写
```

### 13.8 Port 可插拔装配

```
PORT_PROTOCOLS: {"llm": LLMPort, "tool": ToolPort, ..., "disclosure": DisclosureIndex}（14 键）
        │  装配期：registry.register(name, adapter)
        │           isinstance(adapter, PORT_PROTOCOLS[name]) 履约校验（缺一方法即失败）
        ▼
默认实现表（docstring 第 16-30 行）：已实现 4 项，待实现 10 项由装配层补位
        │  layer_program(port) 产出 Invoke+Terminate 外壳
        ▼
dispatch 按 effect.port 查处理器 → Port 实现消费 payload
```

---

## 14. 细节速记卡（逐行精华）

### 依赖与边界

- **依赖方向恒定**：compose 只 import runtime，绝不反向 import 上层（__init__.py 第 3 行）。
- **R1 一行不改**：只解释 Program、执行 Effect；compose 构造 call 注入 `Runner(call=...)`。
- **R1 不构造 Invocation**（dispatch.py 第 3 行、context.py 第 3 行）——Invocation 是 CallContext 的别名（context.py 第 70 行）。

### A 类算子（program.py）

- **ProgramFn 是工厂** `Callable[[], Program]`（第 48 行）：每次调用产出新 generator，resume 靠重建（D-8）。
- **装配期纯变换不改原 Program**（第 4 行）：compile 幂等。
- **状态进 `x-compose.*` scratch**（第 53 行）：算子实例不持有进度；resume 靠 factory 重建。
- **`_run_child` 拦截 Terminate**（第 121-122 行）：子进程结束不终结整个 run。
- **回注值形态容错**：`_as_state` 三分支（第 82-86 行），`_carry_x` 只搬 x-* 键（第 100-103 行）。
- **gate 是唯一能 yield Suspend 的算子**（第 442、456 行，D-6）；resume 靠 `state.last_wait_human`（第 454 行）。
- **par 一次 yield Effect 列表**（第 234 行）走 R1 列表并行批；lockstep 推进；`ordered` 按槽位取值（第 251 行）。
- **majority 平票取首现**：`max(counts, key=lambda k: (counts[k], -keys.index(k)))`（第 196 行）——负的 index 让首现者在平票时胜出，确定性。
- **_hashable 归一不可哈希值**（第 172 行）：容器转 str 才能计数。
- **branch/race/loop/map_state 都用 `Invoke(port="state", op="read")` 拿初始 State**（第 273、354、376 行等）。
- **no_route / all_failed 都带 reason**（第 278、437 行）：不静默失败。
- **fallback 的 arms 含 refuse**（第 427 行）：拒答也是一臂，保证必有结果。
- **retry 的 backoff 写 scratch 交 R3**（第 481-483 行），算子内禁 sleep；`max(1, attempts)` 至少跑一次（第 476 行）。
- **map_effect 对 Terminate 原样透传**（第 400-402 行）：不破坏终态。

### call 装饰器（call.py）

- **只覆盖 Invoke**（D-7）：danger_route/injection_guard 都检查 `isinstance(effect, Invoke)`（第 79、89 行）；dispatch 拒绝非 Invoke（dispatch.py 第 30 行）。
- **build_call 先注册在外层（装配期）**：`call = base; for d in decorators: call = d(call)`（第 115-118 行）——装饰器相对 base 在外层依次包裹；列表靠前的先被包进去（离 base 近、内层、后执行），靠后的后包（最外层、运行时先执行）。注册顺序与执行顺序相反，这是函数组合的固有性质。
- **retry_call 缺省按 `Result.error.retryable` 判定**（第 50 行）；成功即返回（第 56-57 行）；至少 1 次（第 54 行）。
- **timeout 用 `asyncio.wait_for`**，超时折叠为 `Result.failure("TIMEOUT", ...)`（第 69-71 行）。
- **injection_guard fail-closed**：只有 `allowed is False` 才拦（第 92 行），用 `is False` 避免 None 误杀。
- **异常一律折叠为 Result，不抛穿 R1**（dispatch.py 第 37-38 行、timeout 第 70-71 行）。

### DTO 与协议

- **DangerLevel 权威定义在 compose**（dto.py 第 55 行）：跨层 DTO 物理约束，避免字符串漂移。`coerce` 未知/None → CRITICAL（第 81 行），**fail closed 不放行高危**。
- **两种 DTO 载体**：frozen dataclass（值对象）+ pydantic `_Strict`（`extra="forbid"` 拒绝脏数据静默混入，第 99 行）。
- **动态形态统一 DynamicValue**（规则 3）：禁止散落裸 Any。
- **budget_left 是 R1 Budgets 的只读投影**（dto.py 第 153 行）：DTO 不反向依赖内核。
- **ToolSpec.danger 装配期必填**（dto.py 第 201 行 description）；ToolCall.danger 缺省 SAFE 兼容旧构造（第 182 行）。
- **make_call 用 `call_id or name` 兜底**（dto.py 第 277 行）：保证关联 id 非空。
- **LLMResponse.error 非 None 即失败**（dto.py 第 257 行）：R3 据此重试/熔断。
- **PORT_PROTOCOLS 是履约校验目录**（ports.py 第 331-337 行，14 键）：`isinstance(adapter, PORT_PROTOCOLS[name])`，缺一方法即装配失败。
- **基础 Port 领域字段不进**：`last_usage`/`recall` 归位到 R4/R7 扩展协议（ports.py 第 73、165 行）。
- **stream 签名是 `def -> AsyncIterator` 而非 `async def`**（ports.py 第 82、85-87 行）：PEP 525 标准，避免 "coroutine of iterator"。
- **AuditPort/TelemetryPort 没有 @runtime_checkable**（ports.py 第 227、261 行）却在 PORT_PROTOCOLS 中——装配期对它们做 isinstance 校验前需注意这处不对称。
- **OTel 惯例：同步采集 + 异步导出**（ports.py 第 251-252、264-266 行）：关键业务路径不因遥测 IO 阻塞。

### 组件与通知

- **Registry 注册/解析/替换/freeze**（registry.py 第 81-138 行）：重名报错、缺 provider 报错、冻结后彻底只读。返回 self 支持链式。
- **一份 Registry 共用**：可靠性执行器 / LifecycleBus / 装配层（registry.py 第 4 行，P6）。
- **HookBusError 保留旧类名稳定错误码**（registry.py 第 68、70 行）：`code="HOOKBUS_ERROR"`。
- **layer_program 给 R1 领域无知 Program**（tool_layer.py 第 3、44-45 行）：固定两步 `Invoke → Terminate`，返回工厂（D-8）。
- **16 相位 notify**（notify.py 第 31-56 行）：`PHASE_COUNT = len(LifecyclePhase)`（第 59 行）口径自证。
- **Notice 只读投影**：`MappingProxyType(dict(...))` 双保险（第 82-83 行）；由 Invocation 投影，不另建状态（第 93 行）。
- **通知异常隔离三层**：订阅者异常就地隔离（第 158 行）→ on_error 钩子可选接 R10（第 163-165 行）→ 钩子自身异常也隔离（第 166-167 行）；**绝不抛穿**。
- **订阅者返回值不消费**（第 111 行 NoticeFn）：when 维度不能拦截/改写，与 AOP how 维度严格分开。
- **runner_notifier 桥接 Runner**（第 188-212 行）：未知相位安全忽略（第 203-204 行），补上"9 个 Runner 相位无人接"的断点。
- **TYPE_CHECKING 引用 Invocation**（notify.py 第 18-19 行）：运行期不 import，避免循环依赖。
- **CallState 从 ctx.meta 读写 fault/routing_target**（guards.py 第 69-85 行）：`_meta()` 缺省构造字典并挂回 ctx，保证读写同源（第 61-66 行）；frozen ctx 时仅本地读写。
- **meta 键有类型校验**：fault()/routing_target() 读出的值类型不符返回 None（guards.py 第 76、85 行；context.py 第 53、62 行）。

---

*这是内核层三册中的 compose 装配层（R2）。逐行覆盖 12 个文件、1928 行源码。*

