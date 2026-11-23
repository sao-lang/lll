# wiring-contract · 按原设计的接线边界（2026-11-24，用户纠正后）

> 用户原话：「你在做接线的时候要考虑原来的设计，以及正常的接线规则，要合理，不是把所有模块一股脑全部接线上去。这是在做一个agent框架，你要有框架开发者的思维。」
> 这份契约取代之前"逐模块全部接上"的错误方法。先立门禁（行为不变量），再修。

## §1 四条原设计事实（源码证据，已核实）

### C1 Tool / MCP：MCP client 把远端 tools 注册进**同一个内置 ToolRegistry**
- `harness/tools/mcp.py:83-103` `MCPManager.connect()`：`_transport.list_tools(server)` → 加 `mcp__` 前缀 → `self._registry.register(spec, invoker)`。**唯一注册点就是 connect()**。
- `ports/adapters/mcp.py:247/307` Stdio/HttpMCPTransport 履约 `MCPTransport`。
- `api/runtime.py:169-199`：`wired_tools = tools or ToolRegistry(...)` → `tool_setup(wired_tools)` → `_tool_call_handler(wired_tools)`。
- **正确接线**：transport → MCPManager(wired_tools, transport) → `await connect()` → tools 进 `wired_tools` → 执行链可见。**不是**把 MCP 当另一个 port 注入 `_port_handlers`。
- 当前缺口：生产链无 `connect()` 调用；`ToolsCapability._registry`、`MCPManager.registry`、`wired_tools` 是三个不同对象。

### C2 三级渐进式披露：L1 name+描述常驻 / L2 命中后完整 schema / L3 技能正文
- `compose/dto.py:117-131` `DisclosurePacket(l1, l2, l3, token_cost)`；`compose/ports.py:144-158` `DisclosureIndex.disclose(names, skills)`。
- `harness/tools/index.py:39-81` `ToolIndex.disclose`：按 names 取 level=2、`SkillPort.body()` 取 L3。
- 设计文档 R5:295：**组合层在 before_model 相位调用 disclose()**。
- **正确接线**：披露是 tools/skills 模块内部能力，由组合层在 before_model 调用；门面不该单独暴露"disclosure 能力包"。工具目录要能进入 model 请求的 tools_catalog（当前 arch 已有 `payload["tools"]` 链路）。

### C3 记忆 → 上下文：记忆来源对话，注入上下文（system 段）；manager 是组合根，内部 6 部件不进门面
- `harness/context/manager.py:33-45`：`MemoryStore` 是 manager 的 7 个自有依赖之一；`commit_and_compact` 先 `write_cold(transcript)` 再 compact。
- `harness/context/assembler.py` / `reinjector.py:26-28` / `token_counter.py:13-15` / `offloader.py:1-4`：明文"内部协议，不进 PORT_PROTOCOLS"。
- `api/agent.py:668-681`：`recall_before(text)` → `_memory_context` ContextVar → `_call_llm:508-517` 拼入 system；`write_after(text, answer)` → `ingest("用户问：…；答：…")`。
- **正确接线**：门面只暴露 `MemoryCapability` 一个钩子（recall_before/write_after）；换记忆后端走 `MemoryCapability(manager=ContextMemoryManager(memory=MyStore()))`。**不要把 assembler/compaction/blob/reinjector/token_counter 一股脑提到门面**。
- 当前状态：这条链**本来就是对的**——无需改，门禁要保护它不被未来改动破坏。

### C4 意图四级级联 + 可替换：替换粒度两级，桥梁归架构
- `cascade.py:51-54` 阈值 L1=0.7/L2=0.6/L3=0.6/L4=0.5；`L1 关键词→L2 采样→L3 便宜模型→L4 主模型`，达阈值短路。
- 文档 R13:126-129：**整包替换**（传任意 `IntentStrategy`，框架不包装级联）与**部件级替换**（默认级联内换 keyword/samples/cheap/primary/thresholds）两级并存。
- `intent_as_program`（cascade.py:280）是 async recognize → sync Program 的唯一桥梁；`Router`/`RouterSkill` 必须用 Program 路径（Effect 才进预算/路由）。
- **正确边界**：能力包 = strategy 持有者 + `install` + `recognize/critique` 转调；**不持有 `intent_as_program`、不暴露 `build()`**。架构构造器 `Router(classifier=...)` 收**策略**（`cap.strategy`），不收能力包。循环在 `archs.Reflection`（loop_until），不在能力包。
- 当前违反：`CognitionCapability` import `intent_as_program` + 暴露 `build()`；测试用 `Router(classifier=cap)` / `Reflection(reflector=cap)` 把能力包塞进架构。

## §2 接线分层原则（框架开发者思维）

| 层 | 职责 | 可注入什么 | 不可注入什么 |
|---|---|---|---|
| 门面 api/ | 语义入口 say/resume/evaluate/recognize_intent；装配唯一真相源 | 能力包 use()、stream/store（有真实消费） | 模块内部算法部件 |
| 组合层 compose/ | 算子、Port 协议、before_model 等相位 | 算子/协议实现 | 具体后端 |
| 架构层 archs/ | 编排策略（Router/Reflection/ReAct） | **能力包**（内部由桥梁解包策略） | 直接塞裸策略绕开能力包 |
| 能力包层 harness/*/capability | 持有策略/组合根、install 注册、门面钩子 | 策略、组合根、后端实例 | 架构桥梁(Program) |
| 模块内部 | 算法部件（assembler/compaction/blob/token_counter/index） | 模块构造参数/with_ | 门面入口 |

## §3 门禁（先立、先红，再修）

每条必须是**行为级**：注入自定义对象 → 走真实装配 → 断言执行链拿到同一实例/同一行为。静态扫描只列嫌疑，不单独定罪。

- G-W1 MCP 同源：`ToolsCapability(registry=reg)` + `MCPManager(reg, transport)` + `Agent` 执行链 `wired_tools` 同一实例；`connect()` 后 `mcp__*` tools 对 LLM 可见（tool_calls 真执行）。
- G-W2 披露：before_model 相位触发 `disclose()`，L1/L2/L3 结构正确；门面不新增独立 disclosure 入口。
- G-W3 记忆链：`MemoryCapability(manager=with MyStore)` → say → 自定义 store 收到 ingest（对话原文）且 recall 注入 system；内部 6 部件不得出现在 Agent 构造参数。
- G-W4 意图边界：`CognitionCapability` 无 `build()`、不 import `intent_as_program`；`Router(classifier=cap)` 可跑（桥梁解包）；整包替换与部件替换两级都可测。
- G-W5 反思边界：`Reflection(reflector=cap)` 可跑；能力包无循环逻辑。
- G-W6 影子构造：注入 audit/telemetry/guardrail 后，执行链不再 new 同类对象（行为断言，不是 grep）。
- G-W7 自定义能力包：用户自封装 capability + provider（声明 `op`）→ 注册 provider 真进执行链 handler 并可调用。
- G-R1 静态红线：能力包禁止 import `intent_as_program`/`archs`；门面构造参数禁止出现内部算法部件名（AST 可查，低误报）。

## §4 修复顺序（门禁红之后）
1. 先修结构耦合（C4 能力包桥梁归桥接层、C1 三 registry 同源）——它们决定后续修复是否返工。
2. 再修死注入（audit / telemetry / disclosure 触发点）——每条以行为门禁为验收。
3. 不改本来正确的链（C3 记忆链），只加保护测试。
