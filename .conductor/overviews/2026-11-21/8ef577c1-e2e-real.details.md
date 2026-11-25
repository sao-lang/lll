# 真实 e2e 测试方案 v2 · 逐用例效果与测试有效性（替代 v1 的"按文件罗列"）

> v1 缺陷：只列了测试文件与覆盖主题，没列每个用例**要看到什么**、**凭什么说测对了**。v2 补齐两件事：①每个用例写满五栏；②用反证变异证明用例不是假绿。

## 1. 用例书写契约（每个用例必须填满五栏）

| 栏 | 要求 |
|---|---|
| 前置/输入 | 具体值，不写"构造一些数据" |
| 真实入口 | 用户可见 API（如 `agent.asay` / `store.write` / `OpenAICompatLLM.complete`），禁止直调私有方法或改私有字段造场景 |
| 预期效果 | 可观察断言：返回值精确值、计数、HTTP 接收端记录、LLM 实际收到的 messages、span 内容；禁止 `assert obj is not None` 这类无鉴别力断言 |
| 失败判据 | 出现什么就判用例无效（如"断言只查注册表名字""异常被 try 吞掉"） |
| 反证变异 | 临时改坏哪一行 → 本用例必须变红；不红则用例判无效重写 |

## 2. "怎么保证测的是对的"——四道机制

1. **反证变异（核心）**：每个用例落地时执行一次变异，保存"改造前红 / 改回后绿"的命令与输出。变异只针对被测行为的那一行（如删掉 `MemoryStore.write` 里的 `self.ingest(...)`），不碰环境。不红 = 假绿 = 重写用例。
2. **真实入口门禁**：用例只能通过用户能调用的 API 触发行为；禁止手搓 DTO 直接塞内部状态、禁止绕过 `Agent.asay` 直接调 hook、禁止 mock 掉被测对象本身（可 mock 外部依赖，如 vendor HTTP）。
3. **双实现差分**：内置实现与用户自定义实现跑同一输入，预期效果必须一致；自定义实现的副作用计数必须 >0 且内置计数保持 0，证明"用户注入的真的在用"。
4. **负向/隔离断言**：tenant 串味、secret 递归绕过、注入串、断流、错 key 都必须走 fail-closed；用例含负向断言，防止"只测 happy path"的假绿。

## 3. 样板：记忆管理模块（R7）逐用例效果

源码事实：`src/harness/memory/memory.py`（`write` L147、`recall` L166、`refine_kind` L287、`promote/demote` L391/399、`forget_expired` L403）、`src/harness/context/capability.py`（`recall_before` L36 / `write_after` L42）、`src/api/agent.py`（`asay` L716，recall 注入 system L517-520，写回 L756-757）。

统一 ctx：`PortCtx(session_id="e2e-mem", meta={"scope": {"tenant": "t1"}})`；自定义 LLM 用 `tests/e2e/impls/recording_llm.py`（履约 LLMPort，记录每次 `complete(req, ctx)` 的 `req.messages` 原文，返回固定短答）。

| 用例 | 前置/输入 | 真实入口 | 预期效果（断言） | 失败判据 | 反证变异（必须变红） |
|---|---|---|---|---|---|
| MEM-01 write 与 recall 同源 | 空 store；`"用户偏好中文回答"` | `await store.write("prefs","lang",值,ctx)` → `await store.recall(MemoryQuery(text="用户偏好中文回答"),ctx)` | `await store.read("prefs","lang") == 值`；recall 结果 ≥1 且存在 item.content 含该值；`store.tier_count() >= 1` | 只断言 `read` 成功（B7 旧缺陷正是 read 有值但 recall 空） | 删 `memory.py:160` 的 `self.ingest(...)` → recall 必须返回 `[]` |
| MEM-02 KV 读写与幂等遗忘 | 同 MEM-01 已写 | `await store.forget("prefs","lang",ctx)` 连续两次 | 第 1 次后 `read(...) is None`；第 2 次不抛异常；`tier_count()` 不因第二次变化 | 用 `assert True` 或只查异常类型 | 把 `pop(...,None)` 改成 `pop(...)`（无默认）→ 第二次必须抛 KeyError |
| MEM-03 secret 递归拒绝 | `key="api_key"`；嵌套值 `{"p":[{"password":"x"}]}` | `await store.write(...)` ×2 | 两次都 `pytest.raises(ValueError)`；第一次 match `secret key rejected`；第二次 match `secret/injection rejected`；`read` 为 None、`tier_count()==0` | 只断言抛异常但没断言未落库 | 删 `memory.py:151` 的 `is_secret_key(key)` → 第一次不抛，红 |
| MEM-04 注入串 fail-closed | 值 `"IGNORE PREVIOUS INSTRUCTIONS"`（R5 检测器真实命中串） | `await store.write("note","n",值,ctx)` | 抛 ValueError；KV 未写；recall 为空 | 用自造"检测器"替身 | 把 `_scan_text` 改为恒 `False` → 不抛，红 |
| MEM-05 kind 优先级 | 参数化 8 组：`"不要英文"`→PREFERENCE；`"我是工程师"`(conf .9)→PROFILE；`"项目 Alpha 更新"`→ENTITY；`"先打开工具然后运行"`→ENTITY（entity 先于 procedural）；`"决定了方案"`→JOURNAL；`"昨天参加了会议"`→EPISODIC；`"xyz"`→SEMANTIC；`"xyz"`+`preference_hits=3`→PREFERENCE | `MemoryStore.refine_kind(text, conf, preference_hits=n)` | 8 组返回值精确等于预期 MemoryKind | 只测单一路径（如只测 preference） | 把 ENTITY 判定挪到 PROCEDURAL 之后 → 第 4 组红 |
| MEM-06 温度层方向 | `write_cold("长对话",ctx)` 得到 COLD 项 | `store.promote(item)` ×2 → `store.demote(item)` | `tier_of(item)` 依次 COLD→WARM→HOT→WARM（promote=回温，非沉降） | 只断言 `tier_of(item) is not None` | `_shift` 里 `delta` 取反 → 第一次 promote 得 COLD，红 |
| MEM-07 过期遗忘 | `store.ingest("旧事",ctx,now=1_000)` | `store.forget_expired(ttl=1, now=1_002)` | 返回含该 item；`tier_count()==0`；再调用返回 `[]` | 不动 now 靠真实时间 | 把 `moment - ts > effective` 改为 `<` → 返回空，红 |
| MEM-08 tenant 隔离与 query 越权 | t1、t2 各写一条差异记忆 | `await store.recall(MemoryQuery(text=...),ctx_t1)`；再带 `scope=MemoryScope(tenant="t2")` 用 t1 ctx 调一次 | 第 1 次只含 t1 内容；第 2 次 `pytest.raises(ValueError, match="tenant mismatch")` | 只在同 tenant 自证 | 删 `memory.py:173` 的 tenant 过滤 → 第 1 次串味，红 |
| MEM-09 预算贪心裁剪 | 两条记忆：长 30 字符、短 5 字符；`TokenBudget(limit=20)` | `await store.recall(MemoryQuery(text=..., budget=...),ctx)` | 只返回短项（长项被跳过而非整批丢弃）；`len(out)==1` | 只断言 `len(out)<=top_k` | 把 `used + cost > budget_limit` 改成 `>=`（边界错）或删掉 → 红 |
| MEM-10 门面自动召回进 LLM | 先 `await store.write("prefs","lang","用户偏好中文回答",ctx)` | `await agent.asay("我的偏好是什么")`（agent 注入 `MemoryCapability(manager=ContextMemoryManager(memory=store))` + 注入 RecordingLLM） | RecordingLLM 收到的 `messages` 中存在 system 消息含 `"相关记忆：用户偏好中文回答"`；且不含其他 tenant 内容 | 直接调 `mem_cap.recall_before` 自证（绕过门面） | 删 `api/agent.py:520` 的 memory_text 拼接 → system 无该文本，红 |
| MEM-11 门面自动写回并跨轮生效 | 全新 agent/store，无预置记忆 | 第 1 轮 `await agent.asay("请记住我喜欢蓝色")`；第 2 轮 `await agent.asay("我喜欢什么颜色")` | 第 1 轮后 `store.tier_count()>0` 且 recall 命中含"喜欢蓝色"；第 2 轮 RecordingLLM 的 system 含"喜欢蓝色" | 只验 `agent.history`（短期历史，不代表长期记忆） | 删 `api/agent.py:757` 的 `write_after` → 第 2 轮红 |
| MEM-12 用户自定义记忆后端注入生效 | 用户侧 `UserMemory`（tests/e2e/impls）完整履约 `read/write/forget/recall/ingest`，真词面索引 + 调用计数 | `MemoryCapability(manager=ContextMemoryManager(memory=UserMemory()))` → `agent.asay` 两轮 | 自定义端 `recall_calls>0`、`ingest_calls>0`；第 2 轮 LLM system 含自定义端返回的文本；内置 `MemoryStore.tier_count()==0` | 只做 `isinstance(custom, MemoryPort)` | 把 `MemoryCapability(manager=...)` 换成默认 manager → 自定义计数保持 0，红 |
| MEM-13 只注入记忆能力 | 同上，但 agent 不注入其他 Capability | `agent.asay("你好")` | 返回非空、trace 有 before_model/after_model、无异常；证明"只注入所需，框架其余走默认" | 注入全部能力后测（失去隔离意义） | `assemble_runtime` 默认新建替换注入的 memory → MEM-12 红 |

## 4. 其余模块用例效果（同样五栏，落地时逐条展开）

### LLM 真网（`test_real_llm_api.py`）

| 用例 | 真实入口 | 预期效果 | 反证变异 |
|---|---|---|---|
| LLM-01 complete 真网 | `OpenAICompatLLM(...DeepSeek...).complete(req,ctx)` | `error is None`、`content` 非空、`usage.prompt_tokens>0`、`last_usage is usage`；序列化结果无 `sk-` | 把 base_url 指向不可达地址 → 必须红（error 非 None） |
| LLM-02 stream 真网 | `.stream(req,ctx)` 消费完 | ≥1 chunk、拼接非空、`last_usage.prompt_tokens>0` | 把 SSE `data:` 过滤改成恒跳过 → chunks=0，红 |
| LLM-03 stream 取消 | 消费 1 chunk 后 `aclose()` | 不抛穿，底层响应关闭 | 删 `finally` 关闭 → 资源告警/挂起，红 |
| LLM-04 gateway 编排 | `ModelGateway.complete/stream/stream_events` | before_model/after_model 各真发一次；usage 进 R1 预算 | 去掉 `_notify_model` → 相位计数 0，红 |
| LLM-05 自定义 LLMPort 调注入 provider | 用户侧 `ProviderBackedLLM` 真 POST 到本地 provider（httpx） | 断言 provider 端收到请求体、agent 回答来自 provider；内置适配器调用计数 0 | 把自定义实现改成 `return LLMResponse(content="OK")` → provider 端计数 0，红 |

### 双实现矩阵（port/capability/tool/strategy）

| 用例组 | 预期效果 | 反证变异 |
|---|---|---|
| PORT-xx（`PORT_PROTOCOLS` 每键 × 内置/自定义） | 自定义实现被框架经 `assemble_runtime` 真调用，副作用计数 >0；返回值与内置实现同语义；`PORT_PROTOCOLS` 键集合本身不因测试扩大 | 把 `reg.register` 换成 `reg.has` 假注册 → 计数 0，红 |
| CAP-xx（11 Capability） | `install` 后 `reg.resolve(name)` 返回注入实例（identity）；用户不注入时返回默认实例；注入实例被真消费 | 恢复"默认新建替换注入" → identity 断言红 |
| TOOL-xx | `@tool` 生成的 JSON Schema 真出现在 LLM `tools_catalog`；ReAct 真调工具且审计有 tool span；自定义 ToolPort 替换后工具结果来自自定义端 | 删 schema 注入 → LLM 端 tools 为空，红 |
| STRAT-xx（R8 NamedStrategy + Arch/Join） | 自定义策略的 `plan/exec/reflect` 方法被真调用（计数>0），执行结果反映自定义输出；不是只取 prompt | 让架构只取 prompt → 计数 0，红 |

### 编排、可观测、降级

| 用例 | 预期效果 | 反证变异 |
|---|---|---|
| ORCH-01 一轮 `asay` 全链 | 16 相位事件齐全、审计验链通过、checkpoint save→restore 后状态一致、预算扣费=真 usage | 删任一相位通知 → 相位断言红 |
| TRACE-01 自定义 SDK 装填 | 自定义 SDK 收到 llm/tool/phase span，trace_id 一致、duration>0；OTLP 接收端记录到 POST body 含 span 名 | 让 SDK 只记不收（空实现）→ 接收端记录 0，红 |
| TRACE-02 容器（预检通过时） | Jaeger API `/api/traces?service=...` 返回同 trace_id 的 span；预检失败则显式 skip 带原因，不伪造通过 | 上报改成 no-op → API 查无 trace，红 |
| DEG-01 错 key / 断网 | `complete.error` 非 None 且不含密钥；R3 重试/熔断计数变化；主流程不崩 | 把错误折叠改成抛裸异常 → 红 |
| DEG-02 无 key / 无容器 | pytest skip 且 reason 明确；已注册的断言一条不少（检查 skip 标记不覆盖整个文件） | 用 `pytest.skip` 掩盖真实失败 → 覆盖率检查红 |

## 5. 落地顺序与证据

1. 先写 `tests/e2e/impls/` 的用户侧自定义实现（真履约，非桩）+ `tests/e2e/conftest.py` 的真入口 fixture。
2. 按 MEM-01~MEM-13 → LLM-01~05 → PORT/CAP/TOOL/STRAT → ORCH/TRACE/DEG 顺序 TDD：先 red（失败原因=目标行为缺失），再 green。
3. 每完成一组，按"反证变异"列改坏对应一行、跑该用例确认红、恢复确认绿，把命令+退出码记入 overview。
4. 最终证据：`.venv/Scripts/python -m pytest tests/e2e -q`、全量 pytest、mypy strict、`source_purity --strict`、全模块 gate、`git diff src/runtime/` 为空。

## 6. 验收口径（怎么算方案合格）

- 每个用例五栏填满，没有"验证正常工作"这类空话；
- 每个用例至少一个反证变异在落地时实测变红，没有变异就不算完成；
- MEM-01/MEM-10/MEM-11/MEM-12 四个关键用例必须走 `agent.asay` 或 `store.write/recall` 真入口，任何直调内部方法的写法判为无效；
- 双实现差分的"自定义计数>0 且内置计数=0"是硬断言，不能用 `isinstance` 替代。
