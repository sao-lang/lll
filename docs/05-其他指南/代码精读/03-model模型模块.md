# 代码精读 03 · model 模型模块

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/model/` 全部 9 个文件。
> 总入口见 [00](./00-总入口与全局链路.md)，可靠性执行器见 [06 resilience](./06-resilience可靠性模块.md)。

---

## 目录

1. 一句话心智模型
2. 模块文件地图
3. 共享类型（types.py）：三态 / 成本 / 缓存键
4. 网关主链路（gateway.py）：complete
5. 降级解析：唯一决策点
6. singleflight：并发缓存击穿防护
7. 成本预留：reserve/commit/cancel
8. 结构化输出与修复循环
9. 三个流式入口
10. 缓存实现（cache.py）
11. 路由（router.py）+ 注册表（registry.py）
12. 流式帮助函数 + Port 扩展
13. 端到端：一次 complete 调用
14. 细节速记卡

---

## 1. 一句话心智模型

model 模块的唯一入口是 `ModelGateway.complete()`。它把一次模型调用编排成一条链：

> **降级决策 → 缓存查询 → 成本预留 → 调后端 → 结构化校验/修复 → 按实结算 → 写缓存**

**换 LLMPort 后端时上层零改动**——这是它的核心可插拔承诺。成本只记录 LLMPort 响应里的 usage，**不自行估算**。

它对外有两个适配入口：`as_call()`（给可靠性执行器用，吃 Invocation）和 `as_dispatch()`（给 compose dispatch 用，吃 (effect, ctx)）。两者共用同一个 `_center` 实现。

---

## 2. 模块文件地图

| 文件 | 角色 |
|---|---|
| `gateway.py` | ModelGateway：主链路编排（574 行，核心） |
| `cache.py` | 三层可插拔缓存（CachePort / CacheStore / CacheEmbedder） |
| `router.py` | ModelRouter 协议 + RuleRouter |
| `registry.py` | 模型名 → 真实后端实例/端点 |
| `structured.py` | 最小 JSON Schema 校验 + 修复提示 |
| `types.py` | 三态 / 成本账本 / CostGuard / 缓存键 / 规格 |
| `stream.py` | 文本流包成事件流的帮助函数 |
| `ports.py` | LLMPortWithUsage 扩展协议 |
| `__init__.py` | 公共 API |

---

## 3. 共享类型（types.py）

### 3.1 降级解析三态

`BackendResolution = LLMPort | RefuseExecution | TargetUnresolved`：

- `LLMPort`：切到这个后端。
- `RefuseExecution(reason)`：拒答，产出 `REFUSED` 失败，**绝不调任何后端**。`to_result()` 折叠为 `Result.failure("REFUSED", reason)`。
- `TargetUnresolved(target)`：非空目标但未注册，显式失败 `TARGET_UNRESOLVED`，**绝不静默回退默认模型**。

稳定错误码常量：`REFUSED_CODE = "REFUSED"`、`TARGET_UNRESOLVED_CODE = "TARGET_UNRESOLVED"`。

### 3.2 StructuredOutputSpec（pydantic 模型）

字段：

- `json_schema`：字段名对外是 `schema`（别名），内部用 `json_schema` 避免遮蔽 `BaseModel.schema()`。`ConfigDict(extra="forbid", populate_by_name=True)`——`schema=` 和 `json_schema=` 都能构造。
- `mode`：`constrained` / `prompt` / `repair` 三选一，默认 `repair`。
- `max_repair`：最大修复次数，默认 2，`ge=0`。
- `strict`：严格模式，默认 True——未过 schema 的输出绝不标记为合法。

### 3.3 缓存键

`cache_key_string(request, prompt_version)`：

```
payload = {messages, model, temperature, max_tokens, prompt_version}
raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
return sha256(raw).hexdigest()[:32]
```

**稳定 JSON（sort_keys）+ sha256 前 32 位**。任何影响输出的参数都进键，prompt 版本变化即失效。

### 3.4 成本账本 CostAttributor

继承 `UsageLedger`。`record(CostRecord)` 时按四个维度同步累计：run_id / agent_id / user_id / model。金额和 token 都累计，`round(..., 10)` 避免浮点误差。提供 `run_cost` / `user_cost` / `model_cost` / `snapshot` 快捷查询。

### 3.5 CostGuard：成本硬闸

为什么需要它：并发下「先调用后扣费」会让硬闸失效（预算 0.5、20 个并发全部通过检查）。

字段：`max_cost_usd`、`spent`（已花）、`_reserved`（未结清预留）、`_active`（字典 id→金额）、`_seq`（递增序号）。

方法：

- `allow(cost)`（旧 API）：`spent + _reserved + estimated <= max`。
- `charge(cost)`（旧 API）：直接加 spent。
- **`reserve(estimated)`**：负数按 0；`allow` 失败返回 None；否则 `_seq += 1`，`_reserved += amount`，`_active[seq] = amount`，返回 `Reservation(id, amount)`。
- **`commit(reservation, actual)`**：凭证不在 `_active`（重复/伪造）→ 忽略；否则删凭证、`_reserved -= 预留金额`、`spent += actual`（差额自动退还/补扣）。
- **`cancel(reservation)`**：删凭证、释放预留，**不记支出**。
- `remaining()` = max - spent - reserved。

---

## 4. 网关主链路（gateway.py）：complete

### 4.1 构造参数

必填：`llm`（实现 LLMPortWithUsage）。

可选：`router`（缺省 RuleRouter）、`cache`（缺省 ExactCache）、`spec`（结构化规格，None 不校验）、`validator`（缺省 MinimalSchemaValidator，仅 spec 非空时创建）、`prompt_version`（默认 `"current"`，进缓存键）、`cost_guard`、`registry`、`cost_estimator`、`bus`（Lifecycle 通知）。

内部还有两个重要字段：`ledger = CostAttributor()`（成本账本）、`_inflight` 字典（singleflight 用）。

### 4.2 complete 的骨架

```
resolution = _resolve_llm(request, ctx, degrade_target)
if 不是 LLMPort: return _resolution_error_response(resolution)   # 拒答/未注册
cached = await cache.get(request, version)
if cached: return cached                                        # 命中即短路
key = cache_key_string(...)
if key not in _inflight:
    fut = 建 Future; _inflight[key] = fut
    response = await _complete_miss(...)                        # owner 独占后端调用
    fut.set_result(response); pop(key); return response
else:
    return await asyncio.shield(inflight)                       # 等待者共享结果
```

**关键顺序**：拒答优先于缓存——先过唯一决策点，非 LLMPort 的三态直接折叠，**不读缓存、不调后端**。

---

## 5. 降级解析：唯一决策点

`_resolve_llm(request, ctx, degrade_target=None)` 返回三态之一。这是**降级执行的唯一决策点**，任何分支都不回退默认模型（除非压根没指定目标）。

流程：

1. 构造一个临时 `Invoke(port="llm", payload={messages, model, task_hint})` 给 router 用。
2. `decision = router.route(effect, ctx)`——**router 总是被调用**（修复历史「router 空转」问题）。
3. `target = degrade_target or (decision.model if decision.model != NO_DEFAULT else None)`——传入的降级目标优先于路由结果。
4. 路由理由写进 `ctx.meta["model_route"]` 列表，供 trace。
5. 若 `target == "refuse"` → 返回 `RefuseExecution`。**REFUSE 优先于一切**：不解析、不缓存、不调后端。
6. 若 `target is None` → 返回默认后端 `self._llm`（设计允许的唯一下沉情形）。
7. 否则 `registry.resolve_llm(target)`；未注册 → `TargetUnresolved`。**绝不静默回退**。

`NO_DEFAULT = "default"` 是哨兵值：router 返回它表示「未显式配置默认模型」。

### 5.1 两个终止型的折叠

- `_resolution_error_response(resolution)`：把 RefuseExecution/TargetUnresolved 折成带稳定错误码前缀的 `LLMResponse`（`error="REFUSED: ..."` 或 `"TARGET_UNRESOLVED: ..."`）。
- `_raise_resolution_error(resolution)`：流式路径用，抛带稳定码的 ValueError（经 Runner 折叠为失败信封）。

### 5.2 降级目标从哪来：`_extract_target`

- 有 Invocation（可靠性执行器路径）→ 读 `inv.routing_target()`。
- 无 Invocation（dispatch 路径）→ 从 `ctx.meta` 读 `RoutingTarget`（经 `CallState`）。
- 取到 target；reason 只写进 `ctx.meta["degrade_reason"]` 作 trace，**不作 target**。

---

## 6. singleflight：并发缓存击穿防护

场景：40 个并发请求同时 miss 同一 key，如果不防护会真调 40 次后端、记 40 笔成本。

机制：

- 第一个 miss 成为 owner，建 `asyncio.Future` 放进 `_inflight[key]`。
- owner 独占调用 `_complete_miss`，成功后 `set_result`，异常/取消 `set_exception` 广播给所有等待者（避免它们各自重试再次击穿）。
- `finally` 里 `pop(key)`。

细节：

- 给 Future 加 `add_done_callback(lambda f: f.exception() if not f.cancelled() else None)`——消费 owner 路径的异常，避免「Future exception was never retrieved」警告；有等待者时 `exception()` 只读取不清除，不影响它们 await。
- `set_exception` 前检查 `if not inflight.done()`，防止已置终态时重复 set 抛 InvalidStateError 淹没原异常。
- 等待者用 `asyncio.shield(inflight)`，这样等待者被取消不会取消 owner 的调用。

---

## 7. 成本预留：_complete_miss

一次未命中请求的完整链路：

1. **预留**：有 cost_guard 才 reserve，用 `cost_estimator(request)` 估。缺省估算法 `_default_cost_estimate`：有 max_tokens 取 `max(0.01, max_tokens × 1e-5)`，否则 0.01 USD（保守，宁可少放不超支）。预留失败（None）→ 调用前就返回 `COST_LIMIT` 错误，绝不发后端请求。
2. **调后端**：`response = await llm.complete(request, ctx)`。
3. **错误响应**：若有 error → cancel 预留、照实记账（usage 通常为 0）、直接返回（不校验不缓存）。
4. **结构化校验**：有 validator 时——repair 模式走修复循环；constrained/prompt 模式且 strict 时 `check()`，有错则 cancel 预留、返回 `SCHEMA_INVALID` 错误响应（**N3：不抛 ValueError 穿透成 PORT_CRASHED**）。
5. **结算**：`_record_cost(response, ctx, reservation=reservation)`——有预留走 `commit` 按实结清；无预留走旧 allow/charge 兼容路径。然后 `reservation = None` 防 finally 重复 cancel。
6. **写缓存**：仅合法响应（无 error）才 put。**非法输出绝不写缓存**，否则污染后续请求。
7. **异常兜底**：任何异常/取消 → cancel 预留，不记支出，重新抛出。

### 7.1 _record_cost

- 从 `response.usage.cost_usd` 取成本（不估算）。
- 有 reservation → `cost_guard.commit`；无 → `allow` + `charge`（失败抛 ValueError）。
- `coerce_port_ctx(ctx)` 取 agent_id / user_id（唯一的 PortCtx 动态边界）。
- `ledger.record(CostRecord(...))`。

---

## 8. 结构化输出与修复循环

### 8.1 StructuredOutputValidator

- 构造时校验 validator 满足协议且 `name` 非空（可插拔四要素④）。
- `should_repair`：仅 repair 模式为 True。
- `check(text)`：用当前校验器校验（网关修复循环与校验共用同一口径）。
- `build_repair_prompt(bad, errors)`：生成提示——「你上次输出没过校验，原文是…，错误是…，请返回修正后的 JSON」。

### 8.2 最小 JSON Schema 校验（不引外部依赖）

`validate_json_schema(value, schema)`：值是 str 先试 `json.loads`（模型常返回 JSON 文本），解析失败返回错误。然后递归 `_check`。

`_check` 支持的关键字：

- `type`：`object`/`array`/`string`/`number`/`integer`/`boolean`/`null`，用查表分派。**schema 声明了 type 但值非法（如 `"str"`）→ 报错，不静默跳过**（N4：静默跳过会让坏 schema 下的非法输出被当合法）。
- `enum`：值不在列表 → 报错（仅 enum 是合法列表时约束，坏 schema 不误伤）。
- `required`：逐字段查存在性，**一次报全缺失项**便于修复。
- `properties`：只对数据中实际存在的键递归下钻（缺席交给 required）。
- `minimum`：仅数值类型才比。

类型错即 return，不继续深挖（避免噪音）。bool 是 int 子类，`number`/`integer` 检查都排除 bool。

### 8.3 修复循环 `_complete_with_repair`

```
for _ in range(validator.max_repair + 1):   # +1 是首次校验
    errors = validator.check(current.content)
    if not errors: return current            # 合法立即返回，不多调一次
    repair_prompt = validator.build_repair_prompt(...)
    patched = LLMRequest(messages=[*原 messages, system(repair_prompt)])
    current = await llm.complete(patched, ctx)
    record_cost(current)                     # 每次修复也记账
if spec.strict: return SCHEMA_INVALID 错误响应
return current                               # 非 strict 返回最后一次（可能仍非法）
```

**最多 max_repair + 1 次校验、max_repair 次修复调用**，防止无限重试烧成本。

---

## 9. 三个流式入口

### 9.1 `stream(request, ctx)` → AsyncIterator[str]

- 拒答优先（终止三态抛稳定码错误）。
- 缓存命中 → 逐字符吐缓存文本（`for ch in cached.content`），跳过预留/后端/结算。
- 未命中 → 调用前 reserve；`async for chunk in llm.stream(...)` 逐块 yield 并累积。
- 流末读 `llm.last_usage`——**缺失即抛错拒绝服务，绝不记假 zero-cost 掩盖账单缺口**。
- 正常流末：按 usage 结算 + 写缓存；异常/断流：cancel 预留，不结算不写缓存。

### 9.2 `stream_events(request, ctx)` → AsyncIterator[StreamEvent]

- 缓存命中 → 吐 TEXT_DELTA + USAGE + DONE 三个事件。
- 后端有 `stream_events` 用原生的，否则用 `wrap_text_stream` 把文本流包成事件流（能力探测，不污染 LLMPort 协议）。
- 透传所有事件，旁路累积文本和 USAGE 事件。
- 流末结算依据：`settled`（USAGE 事件）优先，其次 `last_usage`；两者都无则抛错（不记假成本）。
- **流式路径禁用结构化校验**（无法校验半截流），不经过 R1 Effect 循环。

### 9.3 三条路径的统一纪律

`complete` / `stream` / `stream_events` 都：调用前 reserve、正常结束 commit、异常断流 cancel，三路径一致。

---

## 10. 缓存实现（cache.py）

三层可插拔，每层都有协议 + 默认实现：

### 层 1：CachePort（策略）

异步 `get(request, version)` / `put(request, resp, version)` / `invalidate(request, version)`。异步理由：gateway 是 async，Redis(asyncio)/DB(asyncpg) 客户端也是 async；同步协议会阻塞事件循环。

### 层 2：CacheStore（存储工具）

存 JSON 字符串（外部存储只认字节/字符串，序列化在缓存层完成）：`get(key)` / `put(key, value, ttl)` / `delete(key)` / `scan(prefix)`。

`InMemoryCacheStore`：dict + `_expiry` 字典（TTL 用 `time.time()`）；get 时检查过期则删除返 None；scan 跳过过期项。

### 层 3：CacheEmbedder（向量模型）

`HashCacheEmbedder` 是默认实现，设计很讲究：

- 字符 bigram 哈希到定长向量，不依赖分词库（中文无空格也能成 bigram）、不调模型/网络/文件。
- 用确定性多项式字符哈希（31 进制），**不用进程随机化的内置 `hash()`**，保证跨进程同文本同向量。
- 抗收敛三件套：512 维（`DEFAULT_CACHE_EMBED_DIM=512`）、桶二值化（出现置 1.0 不累加频次）、512 字符头尾窗口（`MAX_EMBED_CHARS=512`）。
- 为什么二值化：若按频次累加，长文本各桶计数趋同为 n/64，向量收敛为常量，不相关文本余弦趋近 1.0（误命中）。二值化+窗口实测误命中归零。
- 空文本显式返回零向量（`_cosine` 判 0），避免两个空 query 余弦=1 误命中。
- `_bigrams` 用 `^` / `$` 边界哨兵，让首尾字符也成组。

### 两个策略实现

**ExactCache**：sha256 精确匹配（`build_cache_key`）。get 命中 hits+1，未命中 misses+1；put 写 `resp.model_dump_json()`；invalidate 删精确键。store 可注入（默认内存），ttl 可配。

**SimilarityCache**：

- 构造：`threshold` 默认 0.85（必须在 (0,1]）、store、embedder、ttl。
- embedder 用哨兵 `_UNSET` 区分「未传」（用 HashCacheEmbedder）和「显式 None」（降级 difflib 文本相似度）。
- `get`：取查询文本（最后一条 user 消息，无则拼全部）→ 算向量 → scan 同前缀全部候选，逐个算相似度（双方有向量走余弦，否则 SequenceMatcher），**遍历完整后取全局最优再按 threshold 裁决**（先到先得会误判）。
- `put`：存条目 `{text, vec, resp}` 的 JSON，键含原文 hash 保证同 query 覆盖。
- `invalidate`：语义失效可能命中多条，**逐个删除**所有相似度 ≥ threshold 的条目。

`_query_text` 取最后一条 user 消息并做小写+空白归一。

---

## 11. 路由（router.py）+ 注册表（registry.py）

### 11.1 RuleRouter

- `NO_DEFAULT = "default"` 哨兵：未显式配置默认模型。
- 构造：`routes`（task_hint→model）、`default`、`cost_tiers`（model→成本）、`registered`（可用模型集合）。
- `_validate_targets()`：装配期校验显式声明的路由目标必须已注册（哨兵默认值不校验）；未给 registered 时不校验。

`route(effect, ctx)`：

1. `_coerce_route(effect)` 读输入：`task_hint`（payload 里）+ `max_cost`（payload.meta.max_cost_per_call，排除 bool 因为它是 int 子类）。
2. `target = routes.get(task_hint, default)`，reason 记 task_hint。
3. 成本约束：有 max_cost 且 cost_tiers 非空 → 在预算内选最便宜的已注册模型；**仅当预算内有候选时才改选**，否则保留 task_hint 的结果；只有决策真被改变才更新 reason（保证 trace 与实际一致）。

### 11.2 InMemoryModelRegistry

三张表：`_endpoints`（名字→端点连接信息）、`_llms`（名字→后端实例）、`_providers`（名字→provider）。

- `register(name, provider=None, base_url=..., api_key=..., model=...)`：传 provider 走 provider 表，传连接信息走端点表，二者互斥；缺参数抛 HookBusError。
- `register_llm` / `register_provider`：**同名同实例幂等，同名异实例拒绝**（防装配期重复注册到不同实例）。
- `resolve(name)`：provider 优先，其次端点。
- `resolve_llm(name)`：只查 `_llms`。
- `has(name)`：三张表任一命中。

`ModelEndpoint` 是 frozen DTO：name / base_url / api_key / model（密钥不进 trace/日志）。

---

## 12. 流式帮助函数 + Port 扩展

`stream.py`：

- `has_stream_events(llm)`：`callable(getattr(llm, "stream_events", None))` 能力探测。
- `wrap_text_stream(llm, req, ctx)`：把只有 `stream()->AsyncIterator[str]` 的旧后端包成事件流——逐块 yield `TEXT_DELTA`，流末若有 `last_usage` 发 `USAGE`，最后发 `DONE`。不缓存整段，保持低延迟。

`ports.py`：

- `LLMPortWithUsage(LLMPort, Protocol)` 加一个 `last_usage: Usage | None`。真实后端（OpenAI 的 `stream_options.include_usage`）在末块写入，网关读它结算——**不估算、不兜零**。

---

## 13. 端到端：一次 complete 调用

假设请求 messages=[user:"你好"]、model=None、max_tokens=100，装配了 cost_guard 和 spec：

1. `_resolve_llm`：router.route 读 task_hint="" → 返回 default（NO_DEFAULT）→ target=None → 默认后端。
2. 查缓存：ExactCache 算 sha256 → miss。
3. singleflight：`_inflight` 无此 key → 建 Future，成为 owner。
4. `_complete_miss`：
   - reserve：估算 `max(0.01, 100×1e-5)=0.01` USD → 通过，得 Reservation(id=1, amount=0.01)。
   - `llm.complete(request, ctx)` → LLMResponse(content='{"name":"张三"}', usage=cost_usd=0.005)。
   - validator.check：spec.mode=repair → 进修复循环；第一次 check 通过 → 直接返回。
   - `_record_cost`：commit(reservation, 0.005) → `_reserved` 释放 0.01，spent += 0.005（差额 0.005 退还）；ledger.record。
   - cache.put 写响应。
5. Future.set_result(response)，pop key，返回。

若第 3 步另有 39 个并发请求同时来：它们看到 key 已在 `_inflight`，直接 `await shield(fut)`，共享同一次后端调用和同一笔成本。

---

## 14. 细节速记卡

- **拒答优先于缓存**：REFUSE 三态不读缓存、不调后端。
- **router 总是被调用**：修复历史空转问题，路由理由进 trace。
- **未注册目标显式失败**：`TargetUnresolved`，绝不静默回退默认模型。
- **唯一下沉情形**：target 为 None（router 未指定）才走默认后端。
- **singleflight 用 shield**：等待者取消不影响 owner；异常广播防重复击穿。
- **非法输出不写缓存**：防污染后续请求。
- **strict 校验失败折叠 error 响应**：不抛穿成 PORT_CRASHED。
- **成本预留三路径一致**：complete/stream/stream_events 都 reserve/commit/cancel。
- **流末必须有 usage**：缺失即拒绝服务，不记假零成本。
- **流式禁结构化校验**：无法校验半截流。
- **embedder 不用内置 hash()**：进程随机化会破坏确定性，用 31 进制多项式哈希。
- **向量二值化 + 512 维 + 头尾窗口**：抗长文本收敛误命中。
- **语义缓存阈值在遍历后判**：命中必须是全局最优，不是先到先得。
- **语义失效删所有达阈值条目**：不只删第一个。
- **embedder 哨兵区分未传/显式 None**：未传用哈希向量，显式 None 降级 difflib。
- **CostGuard 同名异实例拒绝**：防装配期重复注册。
- **路由成本约束只在有候选时改选**：否则保留 task_hint 结果。
- **bool 不是 number/integer**：类型检查显式排除。
- **修复循环最多 max_repair 次修复**：防止无限烧成本。
- **cache_key 含 prompt_version**：版本变化即失效。

---

*下一册：[04 rag 检索增强模块](./04-rag检索增强模块.md)。可靠性执行器如何调用本模块的网关见 [06](./06-resilience可靠性模块.md)。*
