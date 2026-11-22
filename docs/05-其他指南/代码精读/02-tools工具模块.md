# 代码精读 02 · tools 工具模块

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/tools/` 全部 8 个文件。
> 总入口见 [00](./00-总入口与全局链路.md)，注入检测的细节在 [05 skills](./05-skills技能模块.md)。

---

## 目录

1. 一句话心智模型
2. 模块文件地图
3. 基础枚举（types.py）
4. 装饰器 @tool（decorator.py）
5. 注册表 ToolRegistry（registry.py）：四级 danger 路由
6. 执行类工具（execution.py）
7. 三级披露 ToolIndex（index.py）
8. MCP 合并（mcp.py）
9. WebFetch 工具（webfetch.py）
10. 能力装配 ToolsCapability（capability.py）
11. 端到端：从 @tool 到一次调用
12. 细节速记卡

---

## 1. 一句话心智模型

tools 模块把「一个 Python 函数」变成「一个可被模型调用、带安全路由的工具」。它管四件事：

- **注册**：`@tool` 装饰器给函数贴上 `ToolSpec`（名字、描述、schema、danger 等级），注册表统一收管。
- **披露**：按 L1 目录 / L2 schema / L3 正文三级，渐进式地把工具信息给模型，省 token。
- **调用**：按 danger 四级路由——SAFE 直执行、LOW 审计后执行、HIGH 沙盒、CRITICAL 审批+沙盒。
- **接入**：MCP 外部工具、WebFetch、执行类工具都汇入同一个 Registry。

**安全红线**：danger 以**注册时的 spec 为准**，忽略调用方传来的 `call.danger`（防伪造降级）；高危无沙盒直接拒绝，审批不通过直接拒绝。

---

## 2. 模块文件地图

| 文件 | 角色 |
|---|---|
| `types.py` | ToolSource / SandboxPolicy / DangerLevel 枚举 |
| `decorator.py` | `@tool` 装饰器 + 待注册容器 |
| `registry.py` | ToolRegistry：注册/目录/schema/静态检查/调用 |
| `execution.py` | 代码/Shell/文件三种执行类工具 |
| `index.py` | ToolIndex 三级披露 |
| `mcp.py` | MCPTransport 协议 + MCPManager |
| `webfetch.py` | WebFetchTool + WebFetcher 协议 |
| `capability.py` | ToolsCapability 能力装配 |

注意 `DangerLevel` 的权威定义在 `compose`（底层 DTO 物理约束），本模块从 compose 重导出作为 R5 统一消费入口。

---

## 3. 基础枚举（types.py）

- `ToolSource`：`LOCAL`（本地 @tool）/ `MCP`（外部 MCP server）/ `SKILL`（技能暴露的工具）。
- `SandboxPolicy`：`AUTO`（由 danger 决定）/ `ALWAYS` / `NEVER`。**仅作声明**，运行期路由以 danger 为准。
- `DangerLevel`：来自 compose，四级 `SAFE` / `LOW` / `HIGH` / `CRITICAL`，有 `coerce` 做字符串归一。

所有枚举都是 `str, Enum`，与 JSON Schema 字符串互通，避免裸字符串散落。

---

## 4. 装饰器 @tool（decorator.py）

### 4.1 用法

```
@tool(danger=DangerLevel.SAFE, name="calc")
def add(a: int, b: int) -> int: ...
```

`danger` 是**必填关键字参数**（没有默认值），强制调用方声明风险。

其他参数：`name`（缺省取函数名）、`sandbox`（默认 AUTO）、`timeout`（默认 30.0）、`retries`（默认 2）。**后三个当前只接收不生效**——代码里用 `_ = (sandbox, timeout, retries)` 明确丢弃，因为 R9 执行外壳尚未定型，不往 ToolSpec 塞未定语义的字段。

### 4.2 它做了什么

`tool(...)` 是装饰器工厂，返回一个 `decorate(original)`：

1. 取函数签名和类型注解 `get_type_hints`。
2. 看**第一个参数**的类型注解：若是 `pydantic.BaseModel` 子类 → 记下 `model_cls`。
3. 分两条路生成执行体和 schema：
   - 有 model：`schema = model_cls.model_json_schema()`；执行体包装成 `original(model_cls(**kwargs))`——**pydantic 以 extra=forbid 构造，脏参数在实现执行前就被挡下**。
   - 无 model：`schema = {"type":"object","properties":{}}`；执行体 `original(**kwargs)` 透传，参数校验交给 registry 层。
4. 用 docstring 首行作描述（多行正文不进 L1 目录，避免披露膨胀；无 docstring 返回空串）。
5. 构造 `ToolSpec(name, description, parameters_schema, danger, source=LOCAL)`，包成 `ToolFn(spec, fn)`。
6. **入列**：`_PENDING.append(tool_fn)`。

### 4.3 待注册容器：为什么是模块级

`_PENDING` 是模块级全局列表。装饰即入列，装配期由 `registry.register_pending()` 批量消费。

- 用模块级是为了**零参数兼容**现有全部 `@tool` 调用点。
- 代价是全局状态，所以提供 `pending_tools()`（返回不可变快照）和 `clear_pending()`（清空），测试用 autouse fixture 保证隔离。

`ToolFn` 是 `frozen=True, slots=True` 的包装器，`__call__` 以关键字参数调执行体。

---

## 5. 注册表 ToolRegistry（registry.py）

### 5.1 构造与注册

构造收三个可选依赖：`sandbox`、`human`、`audit` 三个 Port，以及 `name`（默认 `"tools"`）。缺席时按 fail-closed 处理。

**`register(spec, fn)`**：

- registry 冻结（`_frozen=True`）后拒绝注册，防运行期工具集漂移。
- 重名拒绝（防调用路由二义性）。
- **不拒绝 `danger=None`**——先让装配期 `static_check` 统一报告问题。

**`register_pending()`**：批量注册装饰器的待注册项。关键点是**先全量预检再批量注册**：

1. 取 `_PENDING` 快照，空则返回。
2. frozen 检查（先于重名检查）。
3. 全量预检：用 `seen` 集合（含已有工具名）检查所有待注册项有无重名——发现任何一个就整批拒绝。
4. 预检通过后逐个 `register`，不会再抛，所以**无需回滚**。
5. 全部成功后 `clear_pending()`；失败时容器保留，修正后可重试。

**`bind(name, fn)`**：延迟替换已注册工具的**执行体**（不是 spec）。只允许替换已知工具，未注册抛 KeyError——防止绕过 register 偷偷注入实现。冻结后 bind 仍可用（支持热更执行体）。

### 5.2 目录与 schema（渐进披露的关键）

- `catalog(level=1)`：返回目录，**每项清空 `parameters_schema`**（用 `model_copy(update=...)` 浅拷贝，不改原 spec）——L1 红线：绝不披露完整 schema。
- `catalog(level>=2)`：返回完整 spec 列表（注册顺序）。
- `schema_of(name)`：按名返回完整 schema 的**副本**（防外部改写内部状态），未注册抛 KeyError。

### 5.3 静态检查 `static_check()`

装配期逐工具检查，收集**全部**问题而非遇到第一个就停：

- `danger is None` → `"<name>: danger not declared"`，continue。
- danger 是 HIGH 或 CRITICAL 且无 sandbox → 报缺沙盒。
- danger 是 CRITICAL 且无 human → 报缺人工审核（沙盒在场也不能免）。

`freeze()` 冻结目录，返回 self（可链式）。

### 5.4 核心：`call(call, ctx) -> ToolResult`

这是四级 danger 路由的落点。完整流程：

**准备**：

- `started = time.perf_counter()` 记开始时间。
- `initial = DangerLevel.coerce(call.danger)` 只为 meta 记录用。
- 构造 `meta` 字典：`call_id` / `danger` / `source` / `sandbox=False` / `approved=False` / `retryable=True`。

**`finish()` 内闭包**：所有出口统一补 `duration_ms`（毫秒，保留 3 位），把 `retryable` 置为 `ok`（成功可重试、失败不盲目重试），构造 `ToolResult` 时**复制 meta**（防调用方改动注册表内部状态）。

**路由**：

1. 未注册 → `finish(ok=False, error="NOT_FOUND")`。
2. **以注册 spec 为准重算 danger**：`level = DangerLevel.coerce(spec.danger)`，覆盖 `call.danger`（防伪造降级）。meta 里的 danger/source 也刷新为 spec 的值。
3. **参数校验**：仅当 `spec.parameters_schema` 含 `"properties"` 键时，做 `extra=forbid`——遍历 `call.args`，第一个不在 allowed 集合里的键就返回 `INVALID_ARGS`。无 properties 键（如 MCP 的 `{"type":"object"}`）视为不约束，避免误伤外部工具。
4. 按 level 分派：

| 等级 | 行为 |
|---|---|
| SAFE | 直接 `await _maybe_await(fn(**args))`。 |
| LOW | 有 audit 先 `record()`，无 audit 则 meta 标 `audit_missing=True` 并降级执行（不静默放行），然后执行。 |
| HIGH | 无 sandbox → `NO_SANDBOX` 拒绝；有则 `await sandbox.run(fn, args, ctx)`，meta 标 `sandbox=True`。**绝不降级直执行**。 |
| CRITICAL | 无 human → `NO_HUMAN` 拒绝；`await human.ask(question, ["approve","deny"], ctx)`，答案不是 approve（大小写不敏感、去空白）→ `APPROVAL_DENIED`；有 approve 后再检查 sandbox，无则 `NO_SANDBOX`；有则沙盒执行，meta 标 approved + sandbox。 |

5. 任何异常（工具/沙盒/审计/审批）→ 折叠为 `TOOL_ERROR: <异常>` 失败信封，**不抛穿**。

`_maybe_await` 检查 `inspect.isawaitable`，是 awaitable 才 await，统一同步/异步执行体。

---

## 6. 执行类工具（execution.py）

三种：代码执行（D10）、Shell（D11）、文件（D12）。共同基类 `_ExecTool`：

- `danger = DangerLevel.HIGH`——执行类一律 HIGH，**强制走沙盒路由**。
- 构造收 sandbox（可缺席）+ name/description/properties/required，生成 ToolSpec。
- `_run(fn, args, ctx)`：sandbox 缺席 → `_ExecOutcome(ok=False, error="NO_SANDBOX")`；有则 `await sandbox.run`，异常折叠为 `EXEC_ERROR`。

三个子类：

- `CodeExecTool`：`execute(code, ctx)`，参数 `code: string`。
- `ShellExecTool`：构造收 `allowlist`（允许的命令首词元组，空=拒绝一切）。`execute(command)` 先取命令首词（`strip().split(" ", 1)[0]`），不在白名单直接返回 False，**连沙盒都不惊动**（最小攻击面）。
- `FileExecTool`：构造收 `root`（虚拟根，空串=不开放任何路径）。`read(path)` 先校验 `os.path.abspath(join(root, path))` 是否等于 root 或以 `root + os.sep` 开头——阻断 `../` 和绝对路径逃逸，越界直接 False；通过才交 sandbox。

职责切分：R5 管校验和白名单，R9 的 SandboxPort 管隔离。

---

## 7. 三级披露 ToolIndex（index.py）

`ToolIndex` 实现 `DisclosureIndex`，`disclose(names, skills) -> DisclosurePacket`。

### 7.1 两个常量

- `_l1_token_budget = 2048`（构造默认）。
- `_force_threshold = 20`（构造默认）：工具总数超过 20 且无命中意图时强制 L1-only。

### 7.2 流程

1. **L1**：`registry.catalog(level=1)` 全量目录（无完整 schema）。
2. **L2**：`names` 非空时，从 `catalog(level=2)` 里过滤出命中名的完整 spec（保持注册顺序）；无命中则空。
3. **L3**：注入了 SkillPort 才加载——遍历技能清单，按 name 调 `skills.body(name)`；若抛 `ValueError`（正文命中注入检测）→ 留空串而非抛穿，避免不可信正文进披露包。
4. **算成本**：L1 算 `name + description` 长度，L2 算 `str(parameters_schema)` 长度，L3 算正文长度，三者求和。
5. **强制 L1-only**：总数 > 阈值且 `names` 为空 → L2 清空（不报错、不全量注入）。
6. **预算检查**：`token_cost > l1_token_budget` → 抛 `ValueError` 显式拒绝（不静默塞超量上下文给模型）。
7. 返回 `DisclosurePacket(l1, l2, l3, token_cost)`。

---

## 8. MCP 合并（mcp.py）

### 8.1 MCPTransport 协议：框架的唯 一 MCP 扩展点

用户实现三个 async 方法 + `name` 属性即可接自己的 MCP 客户端：

- `list_tools(server)`：返回工具清单。**工具名用原始名，不要加 `mcp__` 前缀**（前缀由 Manager 统一加，重复会双前缀）；ToolSpec 的 danger 必填。
- `call_tool(server, tool, args)`：tool 用原始名，异常不要吞（上层折叠）。
- `ping(server)`：可达性探测，返回 bool，不要抛异常。

与 `MCPPort` 的分工：MCPPort（compose 层，带 ctx）是框架消费的门面，由 MCPManager 实现；MCPTransport（本模块，不带 ctx）是传输层，由用户/内置适配器实现。

用 `isinstance(客户端, MCPTransport)` 验证合规（runtime_checkable）。

### 8.2 MCPManager

- `_servers` 列表记已连接 server（供 health 用，重复 connect 不重复记账）。
- `_invoker(server, tool_name)` 用闭包绑定参数（避免循环晚绑定），返回 `async invoke(**args)` 委托 `transport.call_tool`。
- `connect(server, ctx)`：

  1. 记录 server。
  2. `transport.list_tools(server)` 拿原始 spec。
  3. 逐条改全名为 `mcp__<server>__<原始名>`、改 source 为 MCP，注册进同一个 ToolRegistry（**与本地工具无差别**）。
  4. 返回注入后的 spec 列表。

- `list_tools` / `call_tool` / `ping`：带 ctx 的门面，直接委托 transport。
- `health(ctx)`：对所有已连接 server 逐一 ping，单点异常记 False 而非中断整体健康检查。

---

## 9. WebFetch 工具（webfetch.py）

`WebFetchTool` 抓网页正文，一律视为不可信输入：

- 注入 `WebFetcher`（协议：`async fetch(url, ctx) -> str`）——R5 不内置 httpx/requests，网络 IO 由外部承担。
- 注入 `InjectionDetector`（缺省 `KeywordInjectionDetector`）。
- 可选 `AuditPort`。

`spec.danger = LOW`（外部内容风险走审计路径；检测在工具内强制）。

`fetch(url, ctx)` 流程：

1. `raw = await fetcher.fetch(url, ctx)`。
2. `verdict = detector.detect(raw)`。
3. 命中 `INJECTED` → 有 audit 先 record，然后**抛 ValueError 拒载**（不净化，避免带攻击载荷的文本进上下文）。
4. 干净正文也 audit 留痕（满足「网页视为不可信」的可审计要求），返回 raw。

---

## 10. 能力装配 ToolsCapability（capability.py）

`name="tools"`，`version="5.0.0"`，`requires=()`。

构造：

- registry 缺省 → 用传入的 sandbox/human/audit 造一个默认 ToolRegistry；传了自定义 registry 则忽略外层三 Port（避免语义歧义）。
- disclosure 缺省 → 用该 registry + skills 造 ToolIndex（透传 skills 以启用 L3 正文加载）。

四个 Port 名常量：`tool` / `disclosure` / `mcp` / `skill`。

**`install(reg)`**：

1. **先** `registry.register_pending()`（flush 可能抛重名/freeze，须在 Port 注册前暴露，避免半装配）。
2. 再注册四个 port：tool、disclosure 必注册；mcp、skill 为 None 时跳过（缺席即降级）。

`build()`：`layer_program("tool", payload={"layer":"tools"})()`。

`disclose(names)`：从 SkillPort.catalog() 取技能清单（缺席传空清单仍按工具维度降级工作），委托 disclosure.disclose。

其余Getter：`registry()` / `index()` / `manager()` / `skills()`。

---

## 11. 端到端：从 @tool 到一次调用

**装配期**：

1. 模块导入时，`@tool` 装饰把 ToolFn 塞进 `_PENDING`。
2. `ToolsCapability(sandbox=..., human=..., audit=...)` 造默认 registry + disclosure。
3. `install(reg)`：`register_pending()` 全量预检后批量注册 → 注册四个 port。
4. `static_check()` 报告 danger 缺失/高危无沙盒/CRITICAL 无人工。

**运行期一次 HIGH 工具调用**：

1. Program yield `Invoke(port="tool", payload={calls:[{name:"shell_exec", args:{command:"ls"}}]})`。
2. Interpreter 展开为独立 tool Effect，Runner 调 dispatch → registry.call。
3. 未注册 → NOT_FOUND；danger 以 spec 为准（HIGH），覆盖 call 里伪造的 SAFE。
4. 参数校验：properties 含 `command`，检查 args 有没有多余键。
5. level=HIGH → 检查 sandbox：无 → NO_SANDBOX；有 → `sandbox.run(fn, args, ctx)`。
6. finish 补 duration_ms、retryable，构造 ToolResult 返回。
7. Runner 折叠成 Result 回填 Program。

**模型看工具列表时**：`disclose(names)` → L1 全目录（无完整 schema）+ L2 命中项的完整 schema + L3 技能正文（若注入且未命中注入检测）→ 算 token cost → 超预算抛错或强制 L1-only。

---

## 12. 细节速记卡

- **danger 以注册 spec 为准**：忽略调用方传的 `call.danger`，防伪造降级。
- **四级路由**：SAFE 直执行 / LOW 审计后执行 / HIGH 沙盒 / CRITICAL 审批+沙盒。
- **HIGH 无沙盒、CRITICAL 无人工直接拒绝**，绝不降级直执行。
- **L1 目录清空 schema**：`catalog(1)` 浅拷贝后把 `parameters_schema` 置空。
- **先全量预检再批量注册**：register_pending 避免「注册一半抛错」的半状态。
- **bind 只换执行体不换 spec**：且冻结后仍可 bind（热更）。
- **参数校验只在 schema 有 properties 键时做**：无该键视为不约束（兼容 MCP）。
- **SandboxPolicy 只是声明**：运行期路由看 danger，不看 policy。
- **@tool 的 timeout/retries 当前不生效**：只接收不写入 ToolSpec。
- **docstring 只取首行**进 L1 目录。
- **MCP 工具全名是 `mcp__<server>__<tool>`**：transport 返回原始名，Manager 统一加前缀。
- **MCP 工具注入同一 Registry**：catalog/call 与本地工具无差别。
- **health 单点异常记 False**，不中断整体。
- **WebFetch 命中注入抛 ValueError 拒载**，不净化；干净正文也留审计。
- **披露超预算抛 ValueError**，不静默塞超量上下文。
- **工具总数 > 20 且无命中意图强制 L1-only**。
- **install 先 flush 待注册项再注册 port**：避免半装配。

---

*下一册：[03 model 模型模块](./03-model模型模块.md)。*
