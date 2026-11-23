# ReAct 聊天助手 · 语义门面最小 Demo

用 **真实 DeepSeek API**（或离线假 LLM）跑一次 ReAct 聊天助手，展示注入接线重构后的
**目标 API 形态**：初始代码不需要多少，能力插拔即生效，编排全自动。

## 运行

```bash
# 密钥只走环境变量，绝不写进代码/文件
export DEEPSEEK_API_KEY=sk-xxx        # Windows PowerShell: $env:DEEPSEEK_API_KEY="sk-xxx"
.venv/Scripts/python examples/react-chat/main.py
```

可选：`CHAT_PROMPT="深圳天气怎么样？"` 指定问题；`CHAT_OFFLINE=1` 用本地假 LLM 跑（不联网、不花钱，用于验证接线）。

## 核心路径（就这几行）

```python
@tool(description="查询指定城市的当前天气（温度、天气状况）", danger=DangerLevel.LOW)
def get_weather(city: str) -> dict[str, object]:
    return {"city": city, "temperature": 28, "condition": "晴"}

agent = Agent(ReAct([get_weather], llm="deepseek-chat", max_turns=2),
              system="你是简洁的中文助手。")
agent.use(ModelCapability(llm=llm, cost_guard=CostGuard(max_cost_usd=0.05)))
agent.use(SecurityCapability())   # 沙盒/审计/审批默认实现
agent.use(MemoryCapability())     # say 自动召回（前）+ 自动写回（后）
agent.use(ResilienceCapability(timeout=Timeout(timeout=30.0)))  # 九策略真包装 llm 链
agent.on(LifecyclePhase.AFTER_TOOL, lambda n: print(n.phase, n.port))  # lifecycle hook
answer = agent.say("用 get_weather 查一下深圳的天气")
# agent.stream — 内置事件流；agent.trace — 自动 llm/tool span
# HITL：gate 算子挂起后 agent.resume("yes") 续跑
```

## 自动编排了什么（旧 demo 里的胶水，现在全是框架内部）

| 旧 demo 手写 | 现在 | 框架内部位置 |
|---|---|---|
| 手写 `ToolSpec` + `parameters_schema` + `register_tool` | `@tool` 装饰即声明：schema 从类型注解逐参数推导，装配期自动注册 | `harness.tools.decorator` + 门面 `tool_setup` |
| `_SystemPromptLLM` 包装器补 system | `Agent(system=…)` 一等参数，首轮 LLM 调用前置注入 | `api.agent._call_llm` |
| 手动 `MemoryStore.write/recall/ingest` + `ContextMemoryManager.assemble` | `use(MemoryCapability())` 后 say 自动 recall（拼进 system 段）+ 自动写回 | `harness.context.capability` + 门面钩子 |
| 手动 `StreamGateway` + 相位接线 | `agent.stream` 内置事件流（懒建与 runtime 同源），AFTER_TOOL/SESSION_END 自动发布 | `api.runtime.bind_stream_projection` |
| usage 手动搬进 `Result.usage` | 门面 `_call_llm` 自动搬运，R1 Budgets 自动扣费 | `api.agent._call_llm` |
| RuntimeCtx→PortCtx / ReAct 消息归一化 | 全部框架内部完成 | `api.agent._normalize_history` 等 |
| 手动拿 `runtime.bus` 订阅（错过本次运行） | `agent.on(phase, fn)` say 前注册，装配期真订阅本次运行 bus，跨 say 持久 | `api.agent.on` + `_arun` |

## 报告里的"注入生效证据"

每个 `use()` 的能力都给出行为级证据（不是登记名字）：

- **schema 推导**：打印 `city:str → {"type":"string"}, required:["city"]`，直通 LLM tools_catalog
- **system 前置**：离线假 LLM 捕获首轮消息 `roles=['system','user']`
- **记忆自动召回**：预置记忆后，首轮 system 消息含"相关记忆：…"
- **记忆自动写回**：say 前 N 条 → say 后 N+1 条（问答对自动入库）
- **事件流同源**：`agent.stream` 收到真实 seq 事件（非 synthetic）

## 诚实边界（demo 会如实打印，不美化）

- say() 只返回白名单文本；`last_run`/`runtime` 是只读诊断，不泄漏内部消息
- 记忆/事件流/审计默认**进程内内存实现**，无跨进程持久化与外部导出器
- 事件流投影自动发布（本链可见 after_tool/session_end）；after_model 事件仅在网关走
  bus 分发路径时发布（已知边界）；trace span 仍需显式埋点
- `LocalSandbox` 线程隔离非 OS 强沙箱；demo 用 LOW 工具（直执行但必写审计）不触发审批
- 默认记忆召回用 `HashEmbedder`（跨进程确定性哈希）；生产应注入真实 embedder
- 真实 LLM 调用需 `DEEPSEEK_API_KEY`，避免误扣费；`CostGuard` 每次调用前硬闸
