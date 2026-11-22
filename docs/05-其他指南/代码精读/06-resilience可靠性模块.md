# 代码精读 06 · resilience 可靠性模块

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/resilience/` 全部 7 个文件（约 1400 行）。
> 这是六个模块里最绕的一个——但核心只有一个执行器 + 九种防线，逐个讲清就不难。
> 总入口见 [00](./00-总入口与全局链路.md)，它包裹的网关见 [03 model](./03-model模型模块.md)。

---

## 目录

1. 一句话心智模型
2. 模块文件地图
3. 执行器的编排骨架（executor.py）
4. 前置放行：四种终止拒绝 + 两种降级
5. 预算内调用：超时/重试/舱壁的包裹顺序
6. 失败降级与重路由
7. 共享类型（types.py）：失败分类 / 退避 / 降级阶梯 / 熔断状态
8. 熔断器三态机（breaker.py）
9. 重试策略（retry.py）
10. 超时（timeout.py）
11. 其余六道防线（defenses.py）
12. 能力装配（capability.py）
13. 四坑修复：为什么这么设计
14. 端到端：一次失败调用走降级
15. 细节速记卡

---

## 1. 一句话心智模型

这个模块**没有** AOP 角色、没有统一拦截协议、没有槽位列表。它只有一句话：

> **ReliabilityExecutor 用显式代码，按可靠性工程语义串联九种领域策略，每种策略只暴露自己的领域方法。**

编排顺序：

```
前置放行（killswitch/watchdog/idempotency/ratelimit/breaker）
  → 预算内中心调用（timeout 最内 / retry 居中 / bulkhead 最外）
  → 失败记数（breaker.observe_outcome）
  → 失败降级选级（degradation.select）+ 重路由
```

**opt-in 纪律**：九种策略全部构造注入，传实例才参与，None 即缺席——不写空实现占位。

---

## 2. 模块文件地图

| 文件 | 角色 |
|---|---|
| `executor.py` | ReliabilityExecutor：编排骨架（289 行） |
| `types.py` | 失败分类表 / 退避公式 / 降级阶梯 / 熔断状态（444 行） |
| `defenses.py` | 降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch（353 行） |
| `breaker.py` | 熔断器三态机（216 行） |
| `retry.py` | 重试策略 |
| `timeout.py` | 超时策略 |
| `capability.py` | ResilienceCapability 装配 |

---

## 3. 执行器的编排骨架（executor.py）

### 3.1 两个辅助函数

`_await_result(outcome)`：core 可同步可异步返回 Result，统一 await（`inspect.isawaitable` 判断）。

`_as_invocation(effect, ctx)`：把 `(effect, ctx)` 包成 `CallContext`（Invocation 的别名）。若 ctx 本来就是 Invocation 则复用（**共享 meta**）。关键：meta 与 R1 RuntimeCtx.meta 同源——前置策略写的 fault / routing_target 与网关从 ctx.meta 读的是**同一份**，这是重路由不失效的关键（B3 状态统一）。

### 3.2 构造

`core` 必填（中心 Port 调用，如 `gateway.as_call()`）。其余全是 opt-in：

`registry` / `bus` / `notify` / `kill_switch` / `watchdog` / `idempotency` / `rate_limit` / `bulkhead` / `breaker` / `retry` / `timeout` / `degradation` / `transformers`。

### 3.3 对外两个入口

- `as_call() -> CallFn`：适配 R1，内部 `_invoke(_as_invocation(effect, ctx))`。
- `run_invocation(inv)`：直接对给定 Invocation 跑完整链（测试入口）。

---

## 4. 前置放行：_preflight

返回非 None 即**终止类拒绝**短路；降级类则写 fault 后继续放行。按顺序：

1. **KillSwitch.is_tripped(session_id)** 为真 → 拒绝 `"killswitch"`。
2. **Watchdog.detect(inv)** 命中循环 → 拒绝 `"watchdog:loop"`。
3. **Idempotency.check_and_mark(key)** 为 True（窗口内重复）→ 拒绝 `"duplicate_side_effect"`。无幂等键则跳过。
4. **RateLimit.acquire()**：
   - `REJECT` → 拒绝 `"rate_limited"`。
   - `DEGRADE` → 写 `FaultRecord(reason="rate_limited", failed_target=...)`，**放行**。
5. **Breaker.permit(inv)**：返回非 None（降级原因）→ 写 fault，**放行**。

终止类拒绝走 `_reject`：发 `invoke_rejected` 通知 + 返回 `Result.failure("INVOKE_REJECTED", reason)`。

`invoke_rejected` 去重：用 `inv.meta["_rejected_emitted"]` 标记，每次调用只发一次。

---

## 5. 预算内调用：_budgeted_call

用闭包逐层包裹 core，顺序是**超时最内 → 重试居中 → 舱壁最外**：

```
call = base(core)
if timeout:  call = lambda i: timeout.run_with_deadline(i, call)
if retry:    call = lambda i: retry.run_with_retries(i, call)
if bulkhead: call = lambda i: bulkhead.run_in_slot(i, call)
for transform in transformers: call = transform(call)   # 最外层
```

注意 Python 闭包的晚绑定陷阱：代码用默认参数 `_p=inner`、`_t=timeout` 等**显式绑定当前值**，避免循环/条件里 late binding 全部指向最后一个值。

**熔断记数**：若有 breaker——

- 调用抛异常 → `observe_outcome(inv, Result.failure("INVOKE_EXCEPTION", ...))` 后**原样重抛**（不吞异常，交降级链处理）。
- 正常返回 → `observe_outcome(inv, result)`。

无 breaker 时直接 `await call(inv)`。

---

## 6. 失败降级与重路由：_invoke

一次可靠调用的主流程：

1. `_preflight` → 有拒绝直接返回。
2. **前置降级类 fault**（限流/熔断）：若已有 fault 且无 routing_target → 先 `_apply_recovery` 选级写 typed target。若目标是 refuse → 返回 `REFUSED`；否则从 meta 移除 fault（已消费，避免重路由重复裁决）。
3. 执行 `_budgeted_call`：
   - 配了 degradation 且中心抛异常 → 折叠为失败 Result + 写 fault（reason="invoke_exception"），交降级链。
   - 无 degradation → 异常保持原样抛。
4. **重路由循环**（最多 `_MAX_REROUTE = 8` 次，防阶梯成环）：
   - result.ok → break。
   - 若上一轮已有降级目标且仍失败 → 把该目标记为**新的失败目标**写 fault。
   - 清掉旧 routing_target → `_apply_recovery` 重新选级 → 若 refuse 则 `REFUSED` 并 break → 否则重新 `_budgeted_call`。
5. 最终仍失败 → 发 `invoke_rejected`，返回 result。

`_apply_recovery` 逻辑：读 fault（无则造一个 `invoke_failed`）→ `degradation.select(fault, session_id)` → `write_routing_target(RoutingTarget(target, reason))` → 发 rejected 通知 → 返回 target。

**网关如何消费**：`ModelGateway._extract_target` 从 `inv.routing_target()` 读出 target，透传给 `_resolve_llm`——这就是「降级切后端」的完整闭环。

---

## 7. 共享类型（types.py）

### 7.1 失败分类：重试与熔断共用一次分类

`FailureClass` 四字段：`retryable`（是否重试）、`count_toward_breaker`（是否计入熔断）、`suggested_delay`（Retry-After）、`code`（归一码）。

默认规则表（背下来）：

| 归一族 | retryable | 计入熔断 | 典型 |
|---|---|---|---|
| RATE_LIMITED | 是 | **否** | 429（对方正常保护，不是宕机） |
| SERVER_ERROR | 是 | 是 | 500/502/503 |
| TIMEOUT | 是 | 是 | 超时 |
| CONN_RESET | 是 | 是 | 连接重置/网络错误 |
| BAD_REQUEST | 否 | 否 | 400 |
| UNAUTHORIZED | 否 | 否 | 401 |
| FORBIDDEN | 否 | 否 | 403 |
| PORT_MISSING | 否 | 否 | 端口未注册 |
| SCHEMA_INVALID | 是 | **否** | schema 偶发非法 |

`normalize_code(raw)`：先大写去空白 → 查别名表（HTTP 码和符号码等价，如 "503" 和 "SERVICE_UNAVAILABLE" 都归 SERVER_ERROR）→ 未列的 5xx 兜底 SERVER_ERROR → 否则原样返回（**未知码不静默改写语义**）。

`_DefaultClassifier.classify`：成功 → 终态不参与；失败查表；未知码**尊重 PortError.retryable 显式标记，但保守地不计熔断**。

`classify_failure(result, ctx, classifier=None)`：统一入口，可注入自定义分类器替换。

### 7.2 退避公式 backoff_delay

```
raw = min(max_delay, base_delay × multiplier^(attempt-1))
none         → raw
full         → uniform(0, raw)                    # 最常用，防重试风暴
equal        → raw/2 + uniform(0, raw/2)
decorrelated → min(max_delay, prev × uniform(1,3))  # prev=0 时回退 base_delay
有 Retry-After → max(结果, retry_after)           # 尊重对方背压
```

`Jitter` 枚举：`NONE` / `FULL` / `EQUAL` / `DECORRELATED`，未知值**显式回退 FULL**（安全防风暴）。

`validate_retry_spec` 装配期校验：`max_attempts < 1` / `max_delay < 0` / `multiplier < 1` 拒绝；`base_delay=0 且 jitter=none` 拒绝（重试风暴）。

### 7.3 降级阶梯 DegradeLadder

`DegradeStep`：`target` / `mode` / `quality`（1.0 最优 → 0.0 拒答）/ `max_latency_ms` / `condition`。

`DegradeMode`：`REROUTE`（换备用模型）/ `CACHE`（用缓存答案）/ `FALLBACK_VALUE`（静态兜底）/ `REFUSE`（明确拒答，禁止编造）。

`DegradeLadder.pick(available)`：从可用目标里选 quality 最高的；REFUSE 级总可用；无候选返回临时 REFUSE 级（不编造）。

`validate_ladder` 装配期**强校验**（红线）：

1. steps 非空。
2. **末级必须 REFUSE**（否则目标剔除干净后可能编造答案）。
3. 非拒答级 target 不重复（重复即成环，会来回震荡）。
4. quality **严格递减**（否则无法单调收敛）。

### 7.4 熔断状态 BreakerState

不可变快照：`status`（字符串值）/ `failures` / `window`（tuple[bool]，True=失败）/ `half_open_successes` / `opened_at`。

`failure_rate()` = 窗口中 True 的比例，空窗 0。

---

## 8. 熔断器三态机（breaker.py）

### 8.1 构造参数（默认值背下来）

`failure_rate=0.5`、`minimum_samples=5`、`window_size=20`、`open_cooldown_s=30`、`half_open_max_probes=3`、`half_open_success_to_close=2`、`per_dimension=True`。

还可注入：`clock`（缺省返回 0.0）、`store`（缺省内存）、`dimension_of`（算维度的纯函数）、`failed_target_of`。

### 8.2 维度隔离

`Dimension = tuple[str, str]`（如 `("provider","openai")`）。`per_dimension=False` 时所有调用归并到空维度 `_EMPTY=("","")`。

每个维度一把 `asyncio.Lock`（懒建），load→决策→save 全程在锁内，与 record 互斥。

### 8.3 三态转移

**check(dimension)**（放行判定）：

- CLOSED → None（放行）。
- OPEN：冷却期内（`clock - opened_at < cooldown`）→ 返回 `"breaker_open"`；冷却期满 → 转 HALF_OPEN（清窗口、清成功数、清 opened_at）→ reload。
- HALF_OPEN：窗口长度 ≥ max_probes（3）→ 返回 `"breaker_half_open"`；否则放行探测。

**record(result, dimension)**（记数）：

- CLOSED：窗口追加本次结果（保留最近 20），failures 累加；样本数 ≥ 5 且失败率 ≥ 0.5 → 转 OPEN，记 opened_at。
- HALF_OPEN：成功 → 连续成功数 +1，达 2 → **回到 CLOSED**（重置为初始状态）；失败 → **回 OPEN**，记 opened_at。
- OPEN：不处理（等冷却）。

### 8.4 两个领域入口

`permit(inv)` → `check(_dimension(inv))`，返回降级原因或 None。
`observe_outcome(inv, result)` → `record(result, _dimension(inv))`。

这两个就是执行器调用的方法——执行器不关心三态机内部。

### 8.5 BreakerStore

协议 `load(dim)` / `save(dim, state)`，异步可落 Redis/DB。`InMemoryBreakerStore` 进程内 dict。

`_normalize_key(key)`：只认长度为 2 的 tuple，否则回退空维度。

`_failed_target_of(inv)`（breaker 内部版）：Invoke.payload.model 优先，缺省 `"primary"`。

---

## 9. 重试策略（retry.py）

`RetryPolicy` 构造：`max_attempts=3` / `base_delay=0.5` / `multiplier=2.0` / `max_delay=30.0` / `jitter=FULL` / `respect_retry_after=True` / `idempotency_required=True`，可注入 `rng` / `sleeper` / `target_of`。

`run_with_retries(inv, next)`：

1. 循环 attempt 1..max_attempts：`result = await next(inv)`。
2. `fc = classify_failure(result, inv)`；不可重试或成功 → 直接返回。
3. 记录 `inv.meta["retry_classified"] = fc.code`。
4. 已到最大次数 → 写 fault（`retry_exhausted` + failed_target）→ 返回。
5. **幂等红线**：`_is_retryable_side_effect(inv)` 为真（port=tool 且无 idempotency_key 且 idempotency_required）→ 返回 `IDEMPOTENCY_REQUIRED` 失败，**禁止重试**。
6. 算 delay（带 prev_delay 和 fc.suggested_delay）→ `await sleeper(delay)` → 下一轮。

耗尽时写 fault 是关键：交给降级链选级（`target_of` 派生失败目标注册名，R3-7）。

---

## 10. 超时（timeout.py）

`Timeout(timeout=30.0, target_of=...)`，构造校验 timeout > 0。

`run_with_deadline(inv, next)`：

```
try: return await asyncio.wait_for(next(inv), timeout)
except (TimeoutError/asyncio.TimeoutError):
    写 fault(reason="timeout", failed_target=...)
    return Result.failure("TIMEOUT", "invoke exceeded Xs")
```

超时写 fault 是为了让降级链知道该切哪个后端（R3-7）。

---

## 11. 其余六道防线（defenses.py）

### 11.1 Degradation（降级选级，核心）

构造：`ladder`（强校验）+ `available`（初始可用集）+ `session_state`（按 session_id 隔离的状态表）。

- `_session(session_id)`：**B1**——每个会话独立 available 副本，缺失则按初始集建。一个会话剔除 primary 不影响另一会话。
- `select(fault, session_id)`：取本会话状态 → **R3-6** 把 `fault.failed_target` 从本会话 available 里**永久剔除并写回**（不是局部 discard）→ `pick(available)` 选 quality 最高 → 全剔除则返回 REFUSE。
- B2：failed_target 由上游 `failed_target_of` 派生，是注册名（非维度二元组），此处只做幂等剔除。

### 11.2 RateLimit（限流）

令牌桶：`rate=60`/秒、`burst=10`、`on_exceed`（WAIT/REJECT/DEGRADE，未知回退 DEGRADE）、clock 注入。

`_refill`：`tokens = min(burst, tokens + elapsed × rate)`，`elapsed = max(0, now - last)`。

`acquire`：tokens ≥ 1 → 扣 1 返 ADMIT；否则 on_exceed=REJECT 返 REJECT，其余返 DEGRADE。

### 11.3 Bulkhead（舱壁）

`pools`（池名→容量，默认 `{"default": 10}`）、`on_full`（默认 REJECT）、`pool`（本插件池名）。

`acquire(pool)`：容量取该池或 default；used ≥ cap 返回 False，否则 used+1。
`release(pool)`：`max(0, used-1)`，防重复释放。

`run_in_slot(inv, next)`：占槽失败 → REJECT 返 `BULKHEAD_FULL`，否则写 fault 仍放行 → `try/finally` 保证释放。

### 11.4 Idempotency（幂等）

`store`（键→登记时间戳）、`ttl=86400`（24h）、clock 注入。

`check_and_mark(key)`：命中且已过期（`now - marked_at >= ttl`）→ 真淘汰；命中未过期 → True（重复）；未命中 → 登记并返回 False。

### 11.5 Watchdog（循环看门狗）

`window=10`、`repeat_k=3`、`detect_abab=True`、`kinds`（关注白名单）。

`_kind_of(inv)`：从 Invoke.port 取标识，不在白名单/非 Invoke 返回 None。

`inspect(kind)`：记录到历史（保留最近 window）→ 检测两种模式：

- 重复：末尾 repeat_k 个全相同 → True。
- A-B 交替：末尾 4 个满足 `t0==t2 and t1==t3 and t0!=t1` → True。

### 11.6 KillSwitch（紧急开关）

`flag_store`（dict）、`scope`（SESSION/GLOBAL，未知回退 SESSION 最小权限）。

`is_tripped(session_id)`：检查 `("kill:global", f"kill:{scope}")` 两个键，任一为真即置位。**全局标志和用户作用域标志都能触发**。

### 11.7 RatePermit 与 _ExceedPolicy

- `RatePermit`：`ADMIT` / `REJECT` / `DEGRADE`。
- `_ExceedPolicy`：`WAIT` / `REJECT` / `DEGRADE`（WAIT 和 DEGRADE 语义相同——都写 fault 交降级）。

---

## 12. 能力装配（capability.py）

`ResilienceCapability`：`name="resilience"`，`version="6.0.0"`。

构造收九种策略（全 opt-in）+ `extra_plugins`（函数式 call 变换器，非角色插件；构造时校验 callable）。

`install(core, registry, bus, notify)`：把各策略按具名字段注入 `ReliabilityExecutor`，返回执行器。

`plugins()`：返回已选的内置策略（顺序确定：killswitch / watchdog / idempotency / rate_limit / bulkhead / breaker / retry / timeout / degradation）+ extra，供装配核验。

---

## 13. 四坑修复：为什么这么设计

模块开头注释明确列了四个历史坑，代码处处为它们服务：

- **B1 · session 隔离**：降级目标状态按 session_id 隔离，禁容器级单值。落点：`Degradation._session`。
- **B2 · failed_target 是注册名**：必须是阶梯里的 target 名（如 "backup"），不是熔断维度二元组。落点：`failed_target_of`（读 payload.model 或 "primary"）。
- **R3-6 · 永久剔除**：故障目标从实例 available 里永久删除并写回，不是局部副本 discard。落点：`Degradation.select`。
- **R3-7 · fault 带 failed_target**：上游的 fault 记录（timeout/retry/breaker/bulkhead 写的）都必须携带 failed_target，选级据此剔除。落点：各处 `FaultRecord(...)` 构造。

还有 B3 状态统一：`_as_invocation` 复用同一 meta，保证前置写的 fault 网关能读到。

---

## 14. 端到端：一次失败调用走降级

假设 core 是 `gateway.as_call()`，装配了 breaker + retry + degradation（阶梯：primary → backup → cache → refuse，available={primary, backup}）：

1. `_preflight`：killswitch/watchdog/idempotency 未装配跳过；breaker.permit 返回 None（CLOSED）→ 放行。
2. `_budgeted_call`：包裹 core → retry 包外层 → 无 timeout/bulkhead。
3. 第一次 core 调用：LLM 后端返回 TIMEOUT → retry 分类（retryable=True, count_breaker=True）→ 睡一个 backoff → 重试。
4. 重试仍 TIMEOUT → 达 max_attempts → retry 写 fault（`reason="timeout"` 若 timeout 策略写；这里是 retry_exhausted）+ failed_target="primary"。
5. breaker.observe_outcome 记一次失败（窗口 [F]，样本不足 5 不熔断）。
6. result 仍失败 → 进重路由循环：`_apply_recovery` → `degradation.select(fault, session_id)` → 从本会话 available 剔除 "primary"（剩 {backup}）→ pick 选 "backup"（quality 最高）→ 写 routing_target(target="backup")。
7. 清 fault → 第二次 `_budgeted_call`（core 拿到 inv，gateway._extract_target 读 routing.target="backup" → 切到备用后端）。
8. 备用后端成功 → result.ok → break → 返回成功。

若 backup 也失败：再次写 fault（failed_target="backup"）→ available 空 → pick 返回 REFUSE → 返回 `Result.failure("REFUSED")`，**不编造答案**。

---

## 15. 细节速记卡

- **无 AOP 角色**：每个策略只暴露自己的领域方法，执行器显式编排。
- **全部 opt-in**：None 即缺席，不写空实现。
- **包裹顺序**：timeout 最内 → retry 居中 → bulkhead 最外 → transformer 更外。闭包用默认参数绑定避免晚绑定。
- **熔断异常原样重抛**：不吞，交降级链处理；但同时记数。
- **_MAX_REROUTE=8**：防阶梯成环死循环。
- **终止拒绝 vs 降级放行**：killswitch/watchdog/idempotency/rate REJECT 是终止；rate DEGRADE/breaker 是写 fault 放行。
- **invoke_rejected 每次调用只发一次**：meta 标记去重。
- **B1 session 隔离**：每个会话独立 available 副本。
- **B2 failed_target 是注册名**：payload.model 或 "primary"。
- **R3-6 永久剔除**：写回本会话集合，非局部副本。
- **R3-7 fault 必带 failed_target**：timeout/retry/breaker/bulkhead 各处都写。
- **B3 meta 同源**：_as_invocation 复用同一 meta，网关能读到 fault。
- **失败分类共用**：一次分类同时给 retryable 和 count_toward_breaker，避免两插件语义漂移。
- **429 不熔断**：对方正常保护不算宕机。
- **schema 非法可重试但不熔断**。
- **阶梯末级必须 REFUSE**：禁止编造。
- **quality 严格递减**：装配期强校验。
- **熔断冷却期 OPEN→HALF_OPEN**：半开连续成功 2 次回 CLOSED，任一失败回 OPEN。
- **退避未知 jitter 回退 FULL**：防重试风暴。
- **base_delay=0 + jitter=none 装配期拒绝**：同样是防风暴。
- **幂等红线**：TOOL 副作用无幂等键禁止重试。
- **watchdog 检测重复 + A-B 交替**：末尾 repeat_k 全同 / t0==t2 && t1==t3。
- **killswitch 全局 + 作用域双键**：任一置位即触发。

---

*下一册：[07 MemoryStore 精读](./07-MemoryStore精读.md)——用户最想搞懂的 603 行，逐方法拆解。*
