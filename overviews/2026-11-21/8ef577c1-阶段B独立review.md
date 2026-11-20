# 阶段 B 独立 review（算子核心架构重构·改代码）

> 审查性质：全新上下文独立审查子代理；只读审查 + 本报告为唯一写动作。
> 项目根：`E:/vsc-workspace/lania-zip/lll`
> 依据：`docs/01-总览与设计/组合式工具层重构规格-20261121.md`（§2 / §3 P1~P9 / §6 / §8 D-1~D-10）、`overviews/2026-11-21/8ef577c1-源码证据台账.md`
> 实测命令：`git status --short src/runtime/`、`pytest tests/ -q`、`mypy --strict src`、`gate_check.py --module r1|r2|r3|r12`

## 结论

**Blocker=3 / Should=4 / Nit=2；不可交付（Blocker≠0）。**

形式门禁全绿（pytest 全绿、mypy strict 零错、四模块各 13/13），但架构核心目标未落地：②守卫 + ③装饰器 + `build_call`/`dispatch` 的生产接线为零，生产仍走改名的 AOP 五角色链；新增 r12 四坑门禁存在自证式弱断言且缺 R3-7。属于典型的"测试与门禁形式全绿掩盖语义断裂"。

## 逐维度判定

| # | 维度 | 判定 | 关键证据 |
|---|---|---|---|
| 1 | R1 零改动 | **PASS** | `git status --short src/runtime/` 为空 |
| 2 | 10 算子齐全且纯函数 | **PASS** | `compose/program.py:127,145,162,191,208,236,256,282,312,333` 十 def 齐全；`gate` 在 `:327 yield Suspend`（D-6）；算子无实例进度字段（D-8）。但 par/race 空壳问题见 S4 |
| 3 | AOP 彻底删除（字面） | **PASS** | `src/aop/` 不存在；`grep -rn aop src --include=*.py` 命中均为 docstring/注释（`compose/context.py:3`、`guards.py:4`、`hooks/*.py`、`runtime/runner.py:10`）。语义残留见 S3 |
| 4 | BudgetGuard 删除 | **PASS** | 源码零定义；`tests/r3/test_capability.py:62` 仅为"已删除"注释。tmp 备份残留见 N1 |
| 5 | 四坑语义保真 | **FAIL** | 见 B2：B2 断言自证化、R3-7 缺失 |
| 6 | 行为等价（CompositeCall ↔ AOPContainer） | **PASS** | `call_chain.py:192-250` 与 `git show HEAD:src/aop/container.py` 的 `_build_chain/_call` 五角色顺序、`_MAX_REROUTE=8`、`_REFUSE_TARGET`、`include_wardens=False` 重跑、`_emit_rejected` 去重逐条等价；全量 pytest 绿 |
| 7 | 接线四问 | **FAIL** | 见 B1/B3：build_call/dispatch 未注入 Runner；网关 typed target 消费**未断**（`gateway.py:210` 读 `inv.routing_target()`）；`notify(invoke_rejected)` **可达**（`call_chain.py:142`，r2/r3 测试覆盖） |
| 8 | DTO 归位无双源 | **PASS** | `Verdict` 唯一定义在 `call.py:24`；`FaultRecord` 唯一在 `guards.py:29`、`RoutingTarget` 唯一在 `guards.py:41`，`context.py:15-16` 仅 re-export；`Invocation = CallContext`（`context.py:72`）别名统一。无 aop/compose 双份类 |
| 9 | gate_check r12 | **PASS** | `gate_check.py:162-168` 恰好 26 符号；r12 13/13（覆盖率 61%≥60%、demo 冒烟、integration `test_r6_pilot.py` 登记）；r1/r2/r3 仍 13/13。但 scan_r12 纯度检查虚报见 S2 |
| 10 | 全量测试与类型 | **PASS** | `pytest tests/ -q` 约 600 用例全绿；`mypy --strict src` → `Success: no issues found in 75 source files` |

## Blocker（必须修复，否则不可交付）

### B1 · ②守卫 + ③装饰器 + build_call/dispatch 零生产接线，重构核心目标未落地

- **证据**
  - `src/harness/model/capability.py:96-102`：生产装配入口仍为 `def container(self) -> AOPContainer: return AOPContainer(self.gateway().as_call(), registry=..., bus=...)`。
  - `src/harness/resilience/capability.py:8,51`：`from compose import AOPContainer, Envelope, Prelude, Refiner, Salvage, Warden`；`def install(self, container: AOPContainer, ...)` 按五角色注册。
  - `src/compose/__init__.py:19`：`AOPContainer = CompositeCall` 迁移别名。
  - 全 src 检索：`build_call` / `make_dispatch` / `degrade_retry` / `with_guard` / `DegradationGuard` 在 `src/compose/` 之外**零引用**；`Runner(call=...)` 仅出现在 docstring（`call.py:159`、`__init__.py:4`），无生产调用点。
- **后果**：规格 §2.4/§2.5、P4、P6、D-3 要求的"③call 装饰器链 + ②有状态守卫取代 AOP 五角色固定链"未实现。新建的 `compose/call.py`、`guards.py`、`dispatch.py` 沦为仅供 r12 单测的死代码；生产实际运行的是改了包名/类名的旧 AOPContainer。测试全绿只证明旧链未回归，不证明新架构成立。
- **改法**：将 `ModelCapability.container()` 与 `ResilienceCapability.install()` 改为 `build_call(make_dispatch(ports), decorators=[retry_call, timeout, ...], guards=[...])`，由 `DegradationGuard`/`degrade_retry` 承接降级重路由；移除 `AOPContainer` 别名与 `chain_roles.py` 五角色协议，或让 `CompositeCall` 内部改为驱动 `Guard.check/observe` 而非五角色槽位。补一条"生产装配使用 build_call"的接线断言门禁。

### B2 · r12 四坑门禁断言被弱化：B2 自证化 + R3-7 完全缺失

- **证据**
  - `tests/r12/test_r3_four_pits.py:77-83`：`test_b2_failed_target_is_registration_name` 用 `_fault("rate_limited", "primary")` **手工构造** `FaultRecord` 写入 `CallState`，再断言读出 `failed_target == "primary"`——写入即读出，恒真。未复现 `tests/r3/test_degrade.py:117-126` 中由 `RateLimit.guard()` 真实产出 `fault.failed_target=="primary"` 的路径。
  - 该文件全篇无 `retry_exhausted` / `timeout` / R3-7 用例；`grep failed_target tests/r12/` 仅命中 `:83` 一处。文件 docstring `:3-6` 却宣称"四坑…断言不放水…逐字相同"。
  - 对照：`tests/r3/test_e2e_degrade.py:133-138` 的 R3-7 真实断言（`primary.calls==2`、`backup.calls==1`）在 r12 无任何对应。
  - 另：B1/R3-6 三条断言确与 `test_degrade.py:55-81` 逐字等价（`:58-61/:67-68/:73-75`），此部分合格。
- **后果**：门禁虚报四坑覆盖；B2 恒真断言无法捕获 failed_target 生成逻辑回归；R3-7 在新增门禁零覆盖。任务验收明确"断言被弱化必须标 Blocker"。
- **改法**：B2 改为调用真实 `RateLimit.guard()` / `Degradation.select()` 产出 fault 后断言；补 R3-7 用例，逐字复制 `tests/r3/test_e2e_degrade.py:133-138` 的调用次数断言与 `fault.failed_target=="primary"`。

### B3 · 双轨状态容器不一致：DegradationGuard 与生产链不共享 typed target 存储

- **证据**
  - `compose/guards.py:66-93`：`CallState` 通过 `getattr(ctx, "meta")` 读写 fault/routing_target。
  - `compose/call_chain.py:256-266`：`as_call()` 构造 `CallContext(..., meta={})`，是**独立于 ctx.meta 的新字典**。
  - `harness/model/gateway.py:210`：网关读 `inv.routing_target()`，即 `CallContext.meta`。
  - `compose/chain_roles.py:86`：`Salvage.recover(exc, inv)` 是注册契约；`DegradationGuard` 只有 `check/observe`（`guards.py:116,120`），无 `recover`，无法作为 salvage 注入 CompositeCall。
- **后果**：即使把 DegradationGuard 接进生产链，它写入 `ctx.meta` 的 typed target 也无法被从 `inv.meta` 读取的网关消费，降级重路由静默失效——正是"迁移只换 import 但语义断裂"的形态。
- **改法**：统一为单一状态容器——`DegradationGuard` 改操作 `CallContext.write_fault/write_routing_target`，或令 `CompositeCall.as_call()` 的 `CallContext.meta` 直接绑定 `ctx.meta`（同一 dict 引用）。补一条"守卫写入后网关读到同一对象"的集成断言。

## Should（应修复）

### S1 · P4 未落地：网关 `as_call` 仍是 Invocation 形态而非 §2.5 dispatch 形态
- **证据**：`src/harness/model/gateway.py:194` `def as_call(self) -> Callable[[Invocation], Awaitable[Result]]`；`:12 from compose.context import Invocation`；`:202-204 adapter(inv: Invocation)`。规格 §2.5/P4 要求改为直接吃 `(effect, ctx)`、不构造 Invocation。
- **后果**：`compose/dispatch.py` 全模块悬空，typed target 消费仍绑在 Invocation 抽象上，§2.5 的"按 port 分派 + 构造 Result"职责无实现者。
- **改法**：`as_call()` 改为 `async (effect, ctx)`，内部构造 `CallContext`；分派统一经 `make_dispatch`。

### S2 · `scan_r12` docstring 承诺的 D-8 纯度检查未实现
- **证据**：`tests/tools/ast_scan.py:440-464` 的 `scan_r12` 仅检查 10 个 `def <op>(` 与 `src/aop` 不存在；docstring `:446` 声称"算子不持有实例流程状态（禁 self.<进度> 赋值，D-8）"，代码无该 AST 扫描。
- **后果**：D-8 算子纯度红线无机器化门禁，未来算子持有流程进度导致 resume 丢失不会被拦截。
- **改法**：遍历 `program.py` AST，检测算子函数体内 `self.<attr> = ...` 赋值即报违规。

### S3 · AOP 概念在符号与门禁口径上残留（D-3 未完全兑现）
- **证据**：`compose/__init__.py:19,62` 导出 `AOPContainer`；`harness/model/capability.py:8,96-102`、`harness/resilience/capability.py:1,3,8,51` 仍以 AOPContainer / AOP 角色作为装配入口；`gate_check.py:58` r2 标题仍为"Port + AOP + Lifecycle"、`:62` 注释仍称"AOP 五角色与 AOPContainer 在 aop 包"（该包已删）；`tests/demos/demo_r2.py:4` 冒烟文案"AOP 五角色顺序"。
- **后果**：D-3「AOP 概念取消」仅在目录层面兑现，框架概念与对外装配 API 仍是 AOP 口径，规格 §1.1"对外 API 不变"与"取消 AOP"之间的边界不清。
- **改法**：以 B1 的接线改造为前提移除 AOPContainer 别名与 `container()` 入口；同步改写 r2 门禁标题/注释、r3 demo 文案为 7 承载形态口径。

### S4 · `par` / `race` 为空壳算子，未实现规格语义
- **证据**：`compose/program.py:145-159` 的 `par` 仅 `yield Invoke(port="tool", batch=True).with_payload(calls=...)` 后 `Terminate(output=None)`，`join` 参数只透传无聚合；`:191-205` 的 `race` 同样只发 batch Invoke，`output` 取 `state.get(SEQ_OUTPUT)`，无"首个完成胜出/取消其余"逻辑。规格 §2.3 明确二者语义为"扇出并行+join 聚合"与"竞速取消"。
- **后果**：与规格 §1.2"不保留空壳算子"的根因相悖；依赖 par/race 的上层拿不到聚合结果或取消保证。
- **改法**：按 R1 batch/cancel 设施实现真实 join 聚合与首个完成取消；若一期仅支持骨架，应在 docstring 与门禁中标明"未实现"而非计入 10 算子齐全。

## Nit

### N1 · BudgetGuard 仍存于 tmp 备份
- `tmp/tests_backup/r3/test_budgetguard.py`、`tmp/tests_backup/r3/test_capability.py:16,72,109-110`、`tmp/tests_backup/r4/test_e2e_wiring.py:15,140-156`、`tmp/tests_backup/tools/gate_check.py:71` 仍含符号。若"全仓库无 BudgetGuard"按字面验收则不满足；建议清理备份目录或明确其不在交付范围。

### N2 · `__all__` 重复导出
- `src/compose/__init__.py` 中 `FaultRecord`/`RoutingTarget`（`:64` 与 `:68`）、`Verdict`（`:57` 与 `:64`）被重复列入 `__all__`；无功能影响，建议去重。

## 附：实测门禁输出摘要

- `pytest tests/ -q`：约 600 用例，`[100%]`，全绿。
- `mypy --strict src`：`Success: no issues found in 75 source files`。
- `gate_check --module r1/r2/r3/r12`：各 `13/13 项通过`；r12 26 公共 API 齐全、覆盖率 61%、demo 冒烟通过、integration `test_r6_pilot.py` 已登记；R6 试点真 Runner 端到端见 `tests/r12/test_r6_pilot.py:50-70`，gate resume 见 `tests/r12/test_compose.py:131-144`。

---

### Round 2（独立复审：Round 1 三个 Blocker 修复验证）

> 复核性质：全新上下文独立子代理；只读审查 + 本报告为唯一写动作。
> 实测命令：`.venv/Scripts/python -m pytest tests/ -q`、`mypy --strict src`、`gate_check --module r1|r2|r3|12`、`git status --short src/runtime/`，另用 `PYTHONPATH=src` 跑定点探针。

#### 结论

**Blocker=0 / Should=2 / Nit=2；可交付（建议交付前修复 S1/S2 两条行为回归）。**

Round 1 的三个 Blocker 均已真实消除：生产链确实经过 `build_call`/`with_guard`/`Guard` 组合层原语，四坑断言已改为真实故障源并补齐 R3-7，状态容器统一为同一 dict 引用。形式回归全绿（pytest 全量通过、mypy strict 零错、四门禁各 13/13、`src/runtime/` 零改动），且未发现为过门禁而降低断言/阈值/改期望的放水迹象。但修复引入两条**新的行为等价回归**（CircuitBreaker 双记数、拒绝码变更），均无测试覆盖，列为 Should。

#### 逐条 Blocker 判定

| Blocker | 判定 | 关键证据 |
|---|---|---|
| B1 生产零接线 | **已消除** | `src/compose/call_chain.py:204-234`：生产装配入口 `_build_chain` 真实调用 `build_call(base, decorators, guards)`（`:230`）；guards 由 `_warden_phase_guard()`（`:236-264`，实现 `Guard.check/observe`）经 `with_guard` 驱动；decorators 由 `envelope_decorator` 适配（`:224`）。`build_call`/`with_guard` 非空实现（`src/compose/call.py:64-81,150-174`）。生产容器 `src/harness/model/capability.py:96-102` 返回的 `AOPContainer` 即 `CompositeCall`（`src/compose/__init__.py:19`），其 `run_invocation`/`as_call` 均经 `_call → _build_chain`。新增 `tests/r12/test_wiring.py:89-99` 验证守卫经 Guard 协议短路。非"no-op build_call"，也非"适配器存在但生产不调用"。 |
| B2 四坑断言弱化 | **已消除** | `tests/r12/test_r3_four_pits.py:80-95`：B2 改为真实 `RateLimit(rate=60,burst=1,on_exceed="degrade")` 连调两次 `rl.guard()` 耗尽令牌产出 fault，断言 `fault.reason=="rate_limited"`、`fault.failed_target=="primary"`，**不再**手工 `write_fault` 自证（对照 Round 1 `:77-83`）。R3-7 已补齐于 `:126-177`：真实 `RetryPolicy(max_attempts=2)` + 恒失败 LLM 走耗尽路径，`primary.calls==2`/`backup.calls==1`（`:176-177`）与基线 `tests/r3/test_e2e_degrade.py:139-140` 逐字一致。`git diff tests/r3/test_e2e_degrade.py` 仅改 import 路径，断言零改动，无放水。 |
| B3 状态容器双轨 | **已消除** | `src/compose/call_chain.py:54-62`（`_as_ctx`）与 `:320-328`（`as_call`）均以 `shared = ctx.meta if isinstance(ctx.meta, dict) else {}` 直接复用 R1 `ctx.meta` 同一 dict；`CallContext.write_fault/write_routing_target` 写 `self.meta`（`src/compose/context.py:48-64`）；网关从 `inv.routing_target()` 读同一 meta（`src/harness/model/gateway.py:210`）；`src/compose/adapters.py:63-66` `inner` 传 `Invocation` 本身（`base(i.effect, i)`）而非 `i.ctx`。指定用例 `test_degrade_with_stateful_warden_not_self_blocked` 通过。 |

#### 回归门禁

- `git status --short src/runtime/`：空（R1 零改动 PASS）。
- `pytest tests/ -q`：全量 `[100%]` 通过；B2/B3 定向用例 `14 passed`。
- `mypy --strict src`：`Success: no issues found in 76 source files`（新增 `adapters.py`，由 75→76）。
- `gate_check --module r1/r2/r3/r12`：各 `13/13 项通过`。
- 放水检查：`gate_check.py` diff 仅新增 r12 条目（阈值 `coverage:60` 未降）、r2 r3 标题/符号文案更新与删除已废弃的 `BudgetGuard` 符号；无断言强度/覆盖率阈值下调或期望改写。

#### 新增 Should（修复引入的行为回归，非放水）

##### S1 · CircuitBreaker 双角色被重复记数，破坏行为等价
- **证据**：`ResilienceCapability.install` 将 CircuitBreaker 同时注册为 warden 与 envelope（`src/harness/resilience/capability.py:63-65`）。新链中 `envelope_decorator` 调 `CircuitBreaker.wrap`（`src/harness/resilience/breaker.py:220-229`）已 `record` 一次；而 `_WardenPhaseGuard.observe` 又遍历 `_wardens` 调 `w.observe`（`src/compose/call_chain.py:256-262`）→ `CircuitBreaker.observe` → `record`（`breaker.py:216-218`）第二次。原始容器 warden 阶段**不调** warden 的 observe（`git show HEAD:src/aop/container.py` 的 `_guard_phase` 仅 `_run_wardens` 的 `guard`），故原为单次。
- **实测**：探针单容器调用一次后 `cb.state().failures == 2, window_len == 2`（应为 1）。后果：熔断在 `minimum_samples/2` 次真实失败即 OPEN、HALF_OPEN 探测 1 次成功即 CLOSED（原需 `half_open_success_to_close` 次），属可见语义回归；全量测试无用例覆盖（绿不代表无回归）。
- **改法**：`_WardenPhaseGuard.observe` 不应重复驱动已由 envelope 承载的 observe；仅对"仅 warden 且非 envelope"的插件转发，或恢复原始"warden 阶段无 observe"语义。

##### S2 · 终止类拒绝 Result 错误码由 `INVOKE_REJECTED` 变为 `GUARD_REJECTED`
- **证据**：原始 `_guard_phase` 返回 `Result.failure("INVOKE_REJECTED", ...)`（`git show HEAD:src/aop/container.py:215`）。新链由 `_WardenPhaseGuard.check` 返回 `Verdict(allowed=False)`（`src/compose/call_chain.py:249`），再由通用 `with_guard` 统一产出 `Result.failure("GUARD_REJECTED", ...)`（`src/compose/call.py:77`）。探针实测容器拒绝码为 `GUARD_REJECTED`。
- **后果**：按 `error.code == "INVOKE_REJECTED"` 判定终止类拒绝的调用方行为改变；无测试覆盖该 code（r2/r3 仅断言 `LifecyclePhase.INVOKE_REJECTED` 通知，仍可达）。
- **改法**：`with_guard` 允许 guard 自定义拒绝码，或 `_WardenPhaseGuard.check` 短路路径保留 `INVOKE_REJECTED` 对外码。

#### 新增 Nit

- **N1**：`WardenGuardAdapter`（`src/compose/adapters.py:22-51`）被导出（`src/compose/__init__.py:11,55`）且仅在 `tests/r12/test_wiring.py:57-73` 测试，生产 `_build_chain` 实际用的是内联 `_WardenPhaseGuard`（`call_chain.py:236-264`），该适配器不在生产路径上。B1 核心目标（生产经 build_call/with_guard/Guard）已达成，此为 API 表面与生产实现不一致。
- **N2**：`tests/r12/test_r3_four_pits.py:129,174` docstring 中"逐字复用 `test_e2e_degrade.py:133-138`/`:136-138`"的行号因基线文件新增 2 行 import 已偏移（现为 `138-140`）；断言内容确为逐字复用，仅行号陈旧。

#### 复审范围外遗留（沿用 Round 1，非新增 Blocker）

- Round 1 的 S1（`dispatch.py`/`make_dispatch` 生产未接线）、S2（`scan_r12` 未实现 D-8 纯度扫描）、S3（AOP 概念符号残留）、S4（`par`/`race` 空壳）本轮未见修复动作；其中 S1 与 B1 改法相关但不属于 B1"零接线"核心条件（原为 Should），S2~S4 仍按 Should 遗留。
