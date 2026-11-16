# Agent 框架设计方案

> 核心公式：**Agent 应用 = LLM + Harness**
> 设计主线：**运行时 = Effect 解释器**，一切架构（内置的 10 种或用户自定义）都是解释器上的脚本；
> 一切扩展能力（RAG/记忆/评测/可观测/MCP/沙盒）都是外壳上的 Port 接口 + 默认实现。

---

## 0. 分层总览

```
┌─────────────────────────────────────────────────────────┐
│  L2 抽象层（用户 API）                                    │
│  函数式组合算子 + 10 种内置架构 + @tool 装饰器 + prompt 模板 │
├─────────────────────────────────────────────────────────┤
│  L1 外壳 Harness（协议接入）                              │
│  RAG / 记忆 / 会话 / MCP / 评测 / 可观测 / 沙盒 / 审计      │
│  —— 全部是 Port 接口，用户可替换实现，全部内置默认实现        │
├─────────────────────────────────────────────────────────┤
│  L0 核心运行时（唯一解释循环）                             │
│  AgentState → Program → Effects → 链路 → Port → 回填      │
│  链路 = middleware · hooks · filter · pipes（七挂载点）    │
└─────────────────────────────────────────────────────────┘
```

铁律：**运行时 import 零具体实现，只认 Port；外壳只做协议转换和工程化；用户只在 L2 写代码。**

---

## 1. 核心运行时

### 1.1 三原语

```python
@dataclass(frozen=True)
class AgentState:
    messages: tuple[Message, ...]
    scratchpad: Mapping[str, Any]      # 黑板：phase / steps / cursor / hops / 中间产物
    budgets: Budgets                   # max_turns / max_hops / max_rounds / max_cost
    version: str                       # program+配置版本，进 trace

class Effect:
    kind: Literal["llm", "tool", "child", "skill", "wait_human", "end"]
    payload: dict                      # port 名 + 参数 + 期望 schema
    batch: bool = False                # True = 一批并行

# Program：generator，只 yield Effect 不执行
Program = Callable[[AgentState], Iterator[Effect]]
```

### 1.2 主循环

```python
async def run(session):
    program = session.program
    state = session.state
    while True:
        effects = program.resume(state)           # 纯函数，可重放
        if effects is Done: break
        results = []
        for eff in expand(effects):               # batch 拆并行
            verdict = await chains.dispatch(eff)  # 七挂载点链路
            if verdict.reject: state = apply(verdict)  # filter 可改写/中断
            else: results.append(await ports.call(eff))
        state = fold(state, results)              # 结果回填
        session.log.append(Event(state, effects, results))  # 事件溯源
        if not session.resumable: persist_checkpoint(session)
```

- **中断即一等公民**：`Effect("wait_human")` 不 resume 就持久化，审批后断点续跑。
- **事件溯源**：每次状态转移 append 事件 → checkpoint / 回放 / 时间旅行 / 评测重放，全部免费。
- **Budgets 由运行时强制执行**，程序不自己数数（hops、max_rounds、max_cost 十种架构统一收口）。

---

## 2. 链路机制：一种底座、四个泳道、七个挂载点

middleware / hooks / filter / pipes 是**同一条 Effect 分发管道**上的四种语义，不是四套系统：

| 泳道 | 签名 | 语义 | 典型用途 |
|------|------|------|---------|
| middleware | `async def mw(ctx, next)` | 包裹执行（洋葱模型） | 重试 / 超时 / 熔断 / 鉴权 / 成本计量 |
| hooks | `async def hook(event, ctx)` | 只读观察 | trace / 指标 / 日志 / 审计采集 |
| filter | `def fl(input) -> Verdict` | 判决 allow / redact / reject | 输入输出护栏 / schema 校验 / 注入防御 |
| pipes | `def pipe(data) -> data` | 纯数据变换 | 上下文压缩 / 消息规范化 / PII 清洗 |

**七挂载点**（固定枚举，运行时按点装配）：

```
ON_INPUT → BEFORE_LLM → AFTER_LLM → BEFORE_TOOL → AFTER_TOOL → ON_STATE → ON_OUTPUT
```

### 2.1 与安全体系及其他模块的协同（对应需求 6）

链路是全框架唯一的"必经之路"，所有按协议接入的模块都通过挂载点参与：

- **filter 承担步骤级错误治理**：`Verdict` 不只有 allow/reject，还带错误策略——
  `Verdict(allow=True)` / `Verdict(redact=新数据)` / `Verdict(retry=n, backoff=...)` /
  `Verdict(skip, fallback=默认结果)` / `Verdict(abort, reason=...)` / `Verdict(degrade=降级通道)`。
  每一步执行报错先走 filter 判决，再决定重试、跳过、降级还是终止。
- **pipes 承担流程流转**：数据在挂载点之间的形态转换（如 AFTER_LLM 的 JSON 修复、ON_STATE 的压缩裁剪）。
- **用户自定义**：三个泳道全部暴露接口，`use()` / `filter()` / `pipe()` 注册即可，洋葱序自动装配。
- **可插拔模块通过链路接入**：护栏模块 = ON_INPUT/ON_OUTPUT filter；审计模块 = 全挂载点 hook；
  成本模块 = 全挂载点 middleware；上下文压缩 = ON_STATE pipe。

---

## 3. 工具系统（对应需求 1、2）

### 3.1 @tool 装饰器（内置 pydantic）

```python
@tool(
    name="transfer",                          # 缺省取函数名
    danger=DangerLevel.HIGH,                  # 分级：SAFE / LOW / HIGH / CRITICAL
    sandbox=SandboxPolicy.AUTO,               # HIGH 及以上自动进沙盒执行
    timeout=30, retries=2,
)
class TransferArgs(BaseModel):                # pydantic 即 schema
    from_account: str = Field(..., description="转出账户")
    amount: Decimal = Field(..., gt=0)

    model_config = ConfigDict(extra="forbid") # 默认拒绝未声明字段

def transfer(args: TransferArgs, ctx: ToolContext) -> ToolResult:
    """转账给指定账户。"""                       # docstring 即 description
    ...
```

设计要点：

- pydantic model → JSON Schema 自动生成，注册时**装配期校验**（重复名、schema 非法立即报错）。
- 同步/异步均支持；工具抛异常**不穿透运行时**，统一折叠为 `ToolResult(error=...)` 交给 filter 判决。
- `danger` 分级驱动沙盒路由（见 §7.3）与审批路由（CRITICAL → `Effect("wait_human")`）。

### 3.2 MCP 接入模块（对应需求 2）

```python
class MCPPort(Protocol):
    async def list_servers(self) -> list[ServerInfo]: ...
    async def list_tools(self, server: str) -> list[ToolSchema]: ...
    async def call(self, server: str, tool: str, args: dict) -> ToolResult: ...
    async def health(self) -> ServerHealth: ...          # 掉线/重连状态
```

- **渐进式披露**：MCP 工具**全量注册、按需加载**——运行时 tools 合集只保留
  `name + one-line description`（轻量目录），完整参数 schema 和工具正文在模型
  决定调用时才拉取注入（`Effect("tool", load_schema=True)`）。
- 命名空间隔离：`mcp__<server>__<tool>`，避免与本地工具冲突。
- MCP 工具与本地 @tool 工具在运行时**无差别**：同一 ToolRegistry、同一链路、同一沙盒策略。
- 掉线熔断：health 检查 + 链路 middleware 提供自动重连/降级。

### 3.3 渐进式披露模块（对应需求 5）

统一的 **ToolIndex** 机制，同时服务本地工具、MCP 工具与 Skill：

```
第一级（常驻上下文）：name + 一句话描述          ← 全量，占 token 极少
第二级（按需加载）：  完整参数 schema + few-shot  ← 命中意图才注入
第三级（正文加载）：  Skill 正文 / 工具长文档     ← Effect("skill", part="body")
```

RouterSkill 架构直接构建在该模块之上。

---

## 4. RAG 子系统（对应需求 3）

流水线五段式，**每段都是 Protocol + 内置默认实现，任意可替换**：

```python
class Splitter(Protocol):          # 切分器
    def split(self, doc: Document) -> list[Chunk]: ...

class Embedder(Protocol):          # 向量化
    async def embed(self, texts: list[str]) -> list[Vector]: ...

class VectorStore(Protocol):       # 存储（db / vector db 均实现此接口接入）
    async def upsert(self, chunks: list[Chunk]): ...
    async def search(self, vec: Vector, top_k: int) -> list[Hit]: ...

class Retriever(Protocol):         # 检索策略（可多路）
    async def retrieve(self, query: Query) -> list[Hit]: ...

class PostProcessor(Protocol):     # 结果筛选与拼接
    def rerank(self, hits: list[Hit], query: Query) -> list[Hit]: ...
    def filter(self, hits: list[Hit]) -> list[Hit]: ...       # 分类 / 过滤
    def assemble(self, hits: list[Hit], budget: TokenBudget) -> str: ...
```

**内置优化策略**（默认开启、可关闭）：

- 查询改写（多查询扩展 / HyDE）
- 混合检索（向量 + BM25 / 关键词，RRF 融合）
- 重排（默认 cross-encoder 模型可换）
- 上下文预算感知拼接：按 token 预算贪心装入，附引用溯源
- 检索缓存 + 空结果降级（提示模型"未检索到"而非编造）

---

## 5. 认知模块集（对应需求 5）

这些模块是**可独立调用的服务**（内部走 Effect/链路），内置架构只是它们的组装品：

| 模块 | 职责 | 输出 |
|------|------|------|
| **意图识别** | 分类 + 澄清判断（信息不足时产生 `Effect("wait_human")` 提问而非瞎猜） | 意图标签 + 置信度 + 缺口清单 |
| **规划** | 任务分解 | `steps: list[Step]`（写入 scratchpad，供 PlanExecute 游标消费） |
| **重规划** | 由执行器失败事件触发（步骤失败超阈值 / 环境变化），重新产出 steps | 修订后的 steps + 变更说明 |
| **执行器** | 逐 step 调度工具/子 agent，失败事件上报重规划 | step 结果 + 错误事件 |
| **反思** | 对草稿/结果批评打分 | pass / revise + 批评意见（Reflection 架构的内核） |
| **子 agent 管理** | child 的生命周期：派发（`Effect("child")`）、并发上限、结果 join、超时回收、预算分账 | ChildHandle / join 结果 |

---

## 6. 上下文、记忆与会话（对应需求 5）

### 6.1 上下文管理（内置优化策略）

- 组装：system + 工具目录（一级披露）+ 检索注入 + 记忆 + 历史 + 当前输入
- 优化策略默认内置：历史摘要压缩（超阈值触发 compaction）、重要度保留（最近 N 轮 + 高价值消息钉住）、
  工具结果裁剪（大结果存 StorePort、上下文只留句柄）、token 预算分配器。

### 6.2 记忆管理（长短期）

```python
class MemoryPort(Protocol):
    # 短期：会话内 scratchpad 持久化
    # 长期：跨会话，三类记忆
    async def write(self, kind: Literal["episodic", "semantic", "procedural"], item: MemoryItem): ...
    async def recall(self, query: str, kind: ..., top_k: int) -> list[MemoryItem]: ...
    async def forget(self, item_id: str): ...          # 遗忘与治理
```

- 短期记忆 = 会话 scratchpad（随 checkpoint 持久化）；
- 长期记忆默认实现基于向量库（走 RAG 的 Embedder/VectorStore，不重复造轮子）；
- 写入策略 hook 化：会话结束时由 hook 提炼"值得长期保存"的事实。

### 6.3 会话管理（跨周期断点恢复）

- 会话 = session_id + 事件日志 + checkpoint。**事件溯源天然支持跨周期恢复**：
  进程重启 / 换机器 / 隔天继续，从最近 checkpoint + 事件重放恢复到一致状态。
- `wait_human`、`wait_event` 等挂起态跨周期有效，恢复后从断点继续。

### 6.4 Prompt 模板接口（对应需求 5）

```python
class PromptTemplate(Protocol):
    def render(self, ctx: PromptContext) -> str: ...   # 变量、条件段、few-shot 插槽
    version: str                                        # 模板版本进 trace
```

内置变量注册表（{tools_catalog}、{memory}、{retrieved}、{budgets}...）+ 文件加载器 + 热更新；
模板缺失变量在装配期报错。

---

## 7. 安全体系（对应需求 5）

### 7.1 进出两级护栏

- **输入护栏**（ON_INPUT filter）：prompt 注入检测、敏感内容过滤、权限预检、输入净化。
- **输出护栏**（ON_OUTPUT filter）：内容安全、结构化 schema 校验、敏感信息脱敏、
  高危动作复核（拦截后转 `wait_human`）。
- 两级护栏均为 filter 接口的实现，策略链可插拔。

### 7.2 安全审计模块

```python
class AuditPort(Protocol):
    async def log(self, event: AuditEvent): ...        # 谁在何时让 agent 做了什么/花了多少/动了哪些数据
class PromptSafetyStrategy(Protocol):                  # 审计策略接口（可插拔）
    def inspect(self, text: str, ctx: SecurityContext) -> SafetyReport: ...
```

- 内置默认策略：规则库（注入模式/越狱模式）+ 模型判别 + 启发式（工具结果含指令样本检测）。
- 审计事件走 hook 采集，**写进同一事件日志**（回放时安全上下文可复现）。

### 7.3 沙盒机制（危险操作自动执行）

- 路由规则：`@tool(danger=...)` 注册时声明 → 运行时**自动**将 LOW 以上路由到 SandboxPort，
  CRITICAL 附加审批。用户无须手写任何沙盒代码。
- 内置沙盒实现：文件系统隔离（虚拟根/白名单路径）、网络隔离（出站白名单）、
  资源限额（CPU/内存/时长）、进程级隔离（容器/微 VM 可选）。
- 沙盒内执行结果同样过链路（审计、脱敏不豁免）。

---

## 8. 可观测与评测（对应需求 4）

```python
class TelemetryPort(Protocol):
    def span(self, name: str, ctx: SpanContext) -> Span: ...    # 全链路 trace
    def metric(self, name: str, value: float, tags: ...): ...
    def log(self, event: LogEvent): ...

class EvalPort(Protocol):
    async def run_case(self, case: EvalCase) -> EvalResult: ... # 单 case 评测
    async def regression(self, suite: str) -> SuiteReport: ...  # 回归套件
class Judge(Protocol):
    async def score(self, case: EvalCase, output: Trace) -> Score: ...  # LLM-as-judge 可换
```

- 内置默认实现：本地 JSONL trace 存储 + 基础指标（token/延迟/成本/成功率）+ 规则评分。
- 外部平台接入（Langfuse / LangSmith / 自建）= 实现 TelemetryPort / EvalPort 注册。
- **评测底座 = 事件溯源重放**：把 LLMPort 换成录制实现重放历史，即可回归。

---

## 9. 内置十架构 → 原语归约表

| # | 架构 | 归约结果 |
|---|------|---------|
| ① | ToolUse | 一批 `Effect("tool", batch=True)` 并行收结果 |
| ② | ReAct | program 自循环，模型无 tool_calls 时 Done |
| ③ | PlanExecute | 规划模块产 steps → state.phase 状态机 + 游标 |
| ④ | Reflection | 起草 → 反思模块批评 → `budget(max_rounds)` 控制轮换 |
| ⑤ | Router | `branch(意图识别, arms)` 选 arm |
| ⑥ | RouterSkill | 分流 + `Effect("skill", part="body")` 渐进式加载正文 |
| ⑦ | Supervisor | 规划拆任务 → `par([Effect("child")...])` → join |
| ⑧ | Handoff | 换 program 指针 + `budget(max_hops)` |
| ⑨ | Blackboard | par(children) × 多轮共享 state.scratchpad |
| ⑩ | Graph/DAG | 声明式 → 编译成 program，**装配期 static_check 查悬空边/环** |

---

## 10. 函数式组合 API（L2 用户界面）

```python
# Program 组合
seq(a, b) ; par([...]) ; branch(fn, arms) ; race([...])
retry(n, backoff) ; map_state(lens) ; budget(**limits)

# 链路注册
agent = (
    Agent(program=ReAct(tools=toolkit))
    .use(RetryMiddleware(3), CostMeter(), Timeout(30))       # middleware
    .hook(TracingHook(), AuditHook())                        # hooks
    .filter(InputGuardrail(injection_strategy), OutputSchemaFilter())  # filters
    .pipe(ContextCompaction(), PiiStrip())                   # pipes
    .rag(RAGPipeline(splitter=..., store=PostgresStore(...)))  # 替换任意组件
    .memory(SqliteMemory())                                   # 换实现
    .telemetry(LangfusePort())                                # 换平台
)
```

装配期统一做 static_check：工具 schema、悬空边/环、预算冲突、模板变量缺失、Port 缺实现。

---

## 11. 建议目录结构

```
agentkit/
├── runtime/          # L0：state / effect / program / interpreter / budgets
├── chains/           # middleware hooks filter pipes + 七挂载点装配
├── ports/            # 全部 Protocol 定义（llm tool rag memory store eval telemetry sandbox audit mcp human）
├── harness/          # L1 默认实现：各 Port 的 builtin adapters
│   ├── tools/        # @tool 装饰器 / registry / 渐进式披露 ToolIndex / MCP manager
│   ├── rag/          # 五段流水线 + 内置优化策略
│   ├── cognition/    # 意图识别 / 规划 / 重规划 / 执行器 / 反思 / 子agent管理
│   ├── context/      # 上下文组装与压缩策略
│   ├── memory/       # 长短期记忆 / 会话 checkpoint
│   ├── security/     # 两级护栏 / 沙盒策略 / 审计 / prompt 安全策略
│   └── prompts/      # 模板接口与内置模板
├── archs/            # L2 内置十架构（program + 装配器 + static_check）
├── compose/          # 函数式算子
└── cli/              # 脚手架 / eval runner / replay
```

## 12. 落地路线

1. **P0**：effect loop + LLMPort/ToolPort/TelemetryPort + @tool 装饰器 + ReAct/ToolUse/Router（验证 70% 设计）
2. **P1**：事件溯源持久化、会话断点恢复、链路四泳道完备、两级护栏 + 沙盒路由
3. **P2**：MCP 接入与渐进式披露、RAG 五段流水线、记忆/意图/规划/反思/重规划模块
4. **P3**：多 Agent（Supervisor/Handoff/Blackboard）、Graph/DAG + static_check、组合算子全家桶、Eval 回归

风险提示：链路每个 middleware 必须在 trace 里留名（拒绝黑盒魔法）；Effect 粒度别拆太细（否则回放成本爆炸）。

---

*整理日期：2026-11-13*
