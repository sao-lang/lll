# LLM 日志封装指南：想打印 LLM 请求/响应，接哪里？

> 一句话结论：**项目不缺扩展设计，缺的是"选哪个口"的对照**。框架有 7 种承载形态（见《AGENT-总设计》§1.2）；LLM 日志要看**原文**，正解是 **③call 装饰器**或 **⑤Port 包装**，不是 ④notify 通知。
> 本指南自包含：所有签名以 `src/` 当前源码为准（2026-11-23 复核），三种接法均在 `tests/r11/test_llm_log_extension.py` 用真实链路（Runner → ReliabilityExecutor → ModelGateway → fake LLM）验证通过。

---

## 一、先分清三件事

| 你想知道什么 | 用哪个口 | 能看到什么 |
|---|---|---|
| 这次模型调用**发了什么 prompt、回了什么 token**、usage 多少 | ③call 装饰器（推荐）或 ⑤Port 包装 | 完整 `messages` / `content` / `usage` / `error` |
| 只想知道**发生了 after_model 这件事**、耗时/相位/错误码 | ④notify（`LifecyclePhase`） | **只有 `model` 和 `error`**，没有正文 |
| 想把日志**导出到外部平台**（OTel/JSONL） | ⑤`TelemetryPort.log` / `EventStreamPort` | 结构化事件，由 exporter 异步批量导出 |

**最常见的误区**：订阅 `AFTER_MODEL` 之后发现打印不出 prompt——这是设计如此，不是 bug。`Notice.payload` 只有 `{"model": ..., "error": ...}`（`gateway.py:128-134`），正文从不进通知，避免把敏感内容广播给所有订阅者。

---

## 二、接法一：call 装饰器（最常用，推荐）

### 它能看到什么

装饰器工作在 `Invocation` 层：`inv.effect` 是 `Invoke`（`payload` 就是原始请求），`result` 是 `Result`（`data` 是网关返回值，`error` 是错误）。

### 可照抄模板

```python
from compose import Invocation
from runtime import Invoke, Result

def logger(nxt):
    """③call 装饰器：Invocation → Result 的包裹器。"""
    async def wrapped(inv: Invocation) -> Result:
        payload = inv.effect.payload if isinstance(inv.effect, Invoke) else {}
        result = await nxt(inv)          # 先跑真实调用，绝不吞异常
        data = result.data if isinstance(result.data, dict) else {}
        print({                          # 换成你的日志 SDK
            "messages": payload.get("messages", []),
            "model": payload.get("model", ""),
            "content": data.get("content", ""),
            "usage": data.get("usage"),
            "error": result.error.message if result.error else "",
        })
        return result                    # 原样返回，不改写
    return wrapped
```

### 装到生产链路上

```python
from harness.model import ModelCapability
from harness.resilience import ResilienceCapability
from runtime import Runner

cap = ModelCapability(llm=my_llm)                       # my_llm 实现 LLMPort
executor = ResilienceCapability(extra_plugins=(logger,)).install(cap.gateway().as_call())
runner = Runner(call=executor.as_call())                # R1 一行不改
```

`extra_plugins` 只接受 `(CallFn) -> CallFn` 的函数式变换器（`ResilienceCapability` 构造期校验 callable，非 callable 抛 `ValueError`）。它在 Invocation 层最外层包裹，重试/超时/熔断都在它内层——所以**每条真实调用打一条日志**，重试重试几次就打几条，这是可观测的正确粒度。

> 另一种装饰器入口：`compose.build_call(base, decorators=(...))`，工作在更底层 `CallFn = (effect, ctx) -> Result`，签名是 `async def deco(base: CallFn) -> CallFn`。两者二选一，别叠。

---

## 三、接法二：LLMPort 包装（要拿原始 DTO 时用）

### 它能看到什么

`LLMRequest` / `LLMResponse` 强类型 DTO：请求有 `messages/tools_catalog/model/temperature/max_tokens`，响应有 `content/tool_calls/usage/model/error`。比 call 装饰器更"贴后端"，还能顺手记 `stream()` 的流式输出。

### 可照抄模板

```python
from collections.abc import AsyncIterator
from compose import LLMRequest, LLMResponse, PortCtx, Usage

class LoggingLLM:
    """⑤Port 替换：包住任意 LLMPort，代理 complete/stream。"""
    name = "logging-llm"

    def __init__(self, inner):             # inner 是 OpenAICompatLLM 等真实后端
        self.inner = inner

    @property
    def last_usage(self) -> Usage | None: # LLMPortWithUsage 要求此属性
        return self.inner.last_usage

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        resp = await self.inner.complete(req, ctx)
        print({"messages": req.messages, "content": resp.content, "usage": resp.usage})
        return resp

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        async for chunk in self.inner.stream(req, ctx):
            print({"chunk": chunk})
            yield chunk
```

装配：`cap = ModelCapability(llm=LoggingLLM(real_llm))`，后面照旧。

**适合**：要记流式分片、要按原始 vendor 字段记日志、要在后端调用前后加自己的埋点。
**不适合**：只想记"业务层一次 LLM 调用"——那用 call 装饰器，因为缓存命中时 Port 根本不会被调，而你通常还是想知道"这次业务问了什么"。

---

## 四、接法三：Lifecycle 通知（只记相位，不记正文）

### 它能看到什么

`Notice` 是 frozen 只读投影，字段：`phase / session_id / port / payload / meta / seq`。`BEFORE_MODEL` / `AFTER_MODEL` 的 `payload` 只有 `model` + `error`（`AFTER_MODEL` 在 `finally` 里所有出口恰好发一次，异常路径 `error` 非空）。

### 可照抄模板

```python
from compose import LifecycleBus, LifecyclePhase

bus = LifecycleBus()
bus.on(LifecyclePhase.BEFORE_MODEL, lambda n: print("start", n.session_id, n.payload))
bus.on(LifecyclePhase.AFTER_MODEL,  lambda n: print("done", n.payload, n.meta))
```

装配必须把**同一个 bus** 交给网关（不能只交给 Runner）：

```python
cap = ModelCapability(llm=my_llm, bus=bus)
# 或：runtime = assemble_runtime(bus=bus); gw = runtime.model_gateway(my_llm)
```

**红线**：
- 通知返回值框架不消费，不能拦截/改写；
- 订阅者抛异常被 `emit` 就地隔离，不会打断主流程；
- 订阅者是**同步**的，别在里面 `await` 或做阻塞 IO；重活交给 Port/exporter。
- 直接调 `gw.complete()` / `gw.stream()` **不发** model 相位；走 `as_call()` / `as_dispatch()` 才发。

---

## 五、推荐组合（抄这个就够）

```
③call 装饰器：记 prompt/response/usage/error  ← 业务审计主力
④notify：记 before/after 相位 + session_id     ← 串 trace、算耗时
⑤TelemetryPort.log / EventStreamPort：异步导出  ← 落到外部平台
```

三者互不冲突，同一次调用可以同时触发（测试 `test_port_wrapper_and_notice_coexist_on_one_call` 已锁死）。

---

## 六、六条铁律（别踩）

1. **不改 R1**：内核只认 Program 和 `call`，不认识"日志"。
2. **不拿通知当拦截**：要改请求走 ①A 类 `map_effect` 算子或 R7 投影点；要拒绝走 R3 领域策略。
3. **脱敏**：prompt 里可能有 secret/PII，落盘前过 `api.redact_output`（返回 `SafetyVerdict`）；密钥只从环境变量读，绝不写进日志/代码。
4. **保持返回值原样**：装饰器 `return result`，Port 包装 `return resp`；吞异常或改 data 会破坏回放与确定性。
5. **Port 种类不可新增**：包装现有 `llm` 协议即可；自定义名字只能用 `x-*` payload 扩展键，不能新注册 Port 种类。
6. **异常要抛出去**：可靠性策略靠异常/失败 Result 做熔断和降级；logger 只观察，不 `except` 吞掉真实错误。

---

## 七、文档口径提醒

- 现行权威是《AGENT-总设计》§1.2 / §5 / §6：7 承载形态，`notify` 是 R1 回调语义；
- 《插件封装指南》标注为**历史文档**，其中 `src/compose/lifecycle.py` 这个路径已不存在——总线实现现位于 `src/compose/notify.py`（`LifecycleBus` / `LifecyclePhase` / `Notice` / `runner_notifier`），从 `compose` 直接导出；
- 《增强器封装指南》的 `extra_plugins` 变换器写法与本指南一致；AOP 五角色/`AOPContainer` 已彻底删除，别按旧文档找入口。

---

## 八、细节速记卡

| 问题 | 答案 |
|---|---|
| 打印完整 LLM 日志接哪？ | `ResilienceCapability(extra_plugins=(logger,))` 的 `(Invocation)->Result` 变换器 |
| 要原始 `LLMRequest`/`LLMResponse`？ | 包一层 `LoggingLLM` 传给 `ModelCapability(llm=...)` |
| `after_model` 能拿到 prompt 吗？ | **不能**，payload 只有 `model` / `error` |
| bus 只给 Runner 够吗？ | 不够；model 相位由网关发，必须 `ModelCapability(bus=...)` 或 `assemble_runtime(bus=...)` |
| 订阅者能 `await` 吗？ | 不能，同步、异常隔离 |
| 直接 `complete()` 有通知吗？ | 没有，走 `as_call()` / `as_dispatch()` |
| 测试在哪？ | `tests/r11/test_llm_log_extension.py`（4 条，全绿） |
| 红线？ | 不改 R1、不拦截、不吞异常、先脱敏、Port 种类不新增 |
