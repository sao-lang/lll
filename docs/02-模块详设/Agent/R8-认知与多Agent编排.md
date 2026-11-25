# R8：认知与多 Agent 编排（cognition + archs）

> **实现状态**：已实现（src/harness/cognition/ + archs/；tests/r8/ + tests/demos/demo_r8.py；门禁 r8 13/13）。


> **权威来源**：[《AGENT 总设计》](../../01-总览与设计/AGENT-总设计.md) §R8。本文为模块级拆分详设。
> 层：L1/L2 · 依赖：R1 + compose（算子核心）+ R13 契约 · 源码包：`src/harness/cognition/` + `src/archs/` · 门禁 key：r8

---

## 1. 职责与边界

**做**：认知服务（意图/澄清/规划/重规划/执行器/反思/进度追踪）；多 Agent 编排原语（child 派生/并发上限/join/预算分账/通信/黑板/handoff/supervisor 的内核机制）。
**不做**：执行循环（R1）；10 内置架构与 7 组合算子的逐个详设（已拆到 [R12](./R12-内置架构与组合.md)）——本章只给认知服务与架构归约。

## 2. 设计架构

认知模块是可独立调用的服务（内部走 Effect + compose call/notify 通知（AOP 已删除）），内置架构只是装配品。多 Agent 与单 Agent 共用同一解释器：`Spawn(program_factory, state)` → Runner 递归创建子 Session，子 trace 挂父 span，预算从父切分。铁律：**架构即 Program，可任意嵌套**。

## 3. 流程图

```mermaid
flowchart TD
    subgraph 认知
        UI["用户意图"] --> INT["IntentStrategy 识别 label/confidence/gaps"]
        INT -->|默认四级级联 详见 R13| C1["L1 关键词匹配"] --> C2{"达阈值?"}
        C2 -->|否| C3["L2 语义匹配(采样库)"] --> C4{"达阈值?"}
        C4 -->|否| C5["L3 便宜模型"] --> C6{"达阈值?"}
        C6 -->|否| C7["L4 主模型兜底"]
        C2 -->|是| CLR
        C4 -->|是| CLR
        C6 -->|是| CLR
        C7 --> CLR
        CLR{"信息不足?"}
        CLR -->|是| ASK["Suspend(question/options) 澄清<br/>发 wait_human 通知，resumed 后继续"]
        CLR -->|否| PLAN["Planner 产出 steps"] --> EXEC["Executor 逐 step 调度"]
        EXEC --> F{"失败超阈值/环境变化?"}
        F -->|是| RP["RePlanner 重规划"] --> EXEC
        F -->|否| REF["ReflectStrategy 批评"] 
        REF -->|不过关| EXEC
        REF -->|过关| DONE["完成"]
    end
    subgraph 编排
        SUP["Supervisor 拆任务"] --> PAR["par(Spawn(...)) 并发+分账"]
        PAR --> JOIN["JoinStrategy 聚合"]
        PAR --> COM["AgentMailbox 通信+Blackboard"]
    end
    EXEC --> SUP
```

## 4. API 规范（完整接口）

```python
class IntentStrategy(Protocol):                     # 整体意图策略；默认四级级联，注入即整包替换
    name: str
    async def recognize(self, text, ctx) -> IntentResult: ...   # label/confidence/gaps[/trace]
# 默认四级级联 CascadeIntentStrategy 的四个可注入部件（协议+默认+入口+校验 见 R13 §4/§4.1）：
#   KeywordMatcher(L1 关键词) / IntentSampleStore(L2 采样库) / CheapIntentModel(L3) / PrimaryIntentModel(L4)
#   build() -> Program 逐级 yield Effect，命中阈值即停；经 intent_as_program 供 Router 单入口消费
class ReflectStrategy(Protocol):
    name: str
    async def critique(self, draft, goal, ctx) -> Critique: ...  # passed/score/comments
class Planner(Protocol):
    name: str
    async def plan(self, goal, ctx) -> tuple[Step,...]: ...
class RePlanner(Protocol):
    name: str
    async def replan(self, goal, steps, failures, ctx) -> tuple[Step,...]: ...
class Executor(Protocol):
    name: str
    async def execute(self, step, ctx) -> StepResult: ...

class Arch(Protocol):
    name: str
    version: str
    def static_check(self) -> StaticCheckReport: ...
    def compile(self) -> Program: ...

# 10 内置架构（全部返回 Program）
ReAct / ToolUse / PlanExecute / Reflection / Router / RouterSkill /
Supervisor / Handoff / Blackboard / Graph /（+ 组合算子支撑嵌套）
# 组合算子
seq / par / race / map_effect / fallback / retry / loop_until / branch_value（8 个 A 类算子，详见 R12）
```

## 5. 元数据与关键字段（核心字段）

| 字段 | 说明 |
|------|------|
| IntentResult | label/confidence(0~1)/gaps（信息缺口触发澄清）；可选 trace（命中级 + 逐级 label/confidence，观察语义、非内核必填） |
| Critique | passed/score/comments |
| Step | 可执行步骤（明确动作+校验条件） |
| ChildHandle | child_session_id/budget 分账/trace 父 span |
| JoinStrategy | all/any/majority/reduce + timeout |
| StaticCheckReport | errors/arch/version/ports_required/budgets_resolved |

**10 架构归约**：ToolUse=并行 Effect；ReAct=自循环；PlanExecute=状态机游标；Reflection=budget 轮换；Router=branch；RouterSkill=分流+skill 正文(R5)；Supervisor=par(child)+join；Handoff=换 program+hops；Blackboard=共享 scratch；Graph=声明式编译+static_check；+组合算子嵌套。

## 6. 可插拔契约

| 注入点 | 协议 | 默认 | 校验 |
|--------|------|------|------|
| 意图 | `IntentStrategy`（整体替换）/ 四部件 `KeywordMatcher`·`IntentSampleStore`·`CheapIntentModel`·`PrimaryIntentModel`（仅默认级联内可注入） | 默认四级级联 `CascadeIntentStrategy`（关键词→语义→便宜模型→主模型，R13 §4.1）；其余 LLM 分类/critic/分解 | 返回结构完整；缺 name 拒绝 |
| 反思/规划 | ReflectStrategy/Planner 等 | LLM critic/分解 | 返回结构完整；缺 name 拒绝 |
| 子 Agent | ChildSpec | ReAct 子 | 并发上限/预算分账 |
| 架构 | Arch | 10 内置（详见 R12） | static_check 查悬空边/环/缺 port |

## 7. 模块内闭环核对（CL-1~CL-5）

| 条件 | 实现 | 证据 |
|------|------|------|
| CL-1 契约自足 | 依赖 R1 types + compose 算子/call + notify + 认知协议 | import 扫描 |
| CL-2 自带 fake | FakeLLMPort + fake classifier | fake 用例 |
| CL-3 自带测试 | tests/r8（已落地 22 用例）+ tests/r12 | 覆盖率报告 |
| CL-4 独立 demo | `make demo-arch` | demo 冒烟 |
| CL-5 文档自足 | 本文 + AGENT-总设计对应章节 | 文档评审 |

## 8. 验收标准

1. 认知服务 + 10 架构各有端到端用例；
2. 装配期检查：悬空边/环/不可达/缺 port/预算冲突；
3. 同 Arch 多次 compile 行为一致，算子不改原对象；
4. 嵌套组合 Router→Graph→ReAct 跑通，换架构零改动。

## 9. 功能验证矩阵

| 功能点 | 用例 ID | 验证方式 | 通过标准 |
|--------|---------|---------|---------|
| 意图识别 | R8-T01 | `pytest tests/r8/test_intent.py -q` | 分类+置信度+gaps |
| 意图四级级联 | R8-T08 | `pytest tests/r8/test_intent.py -q` | 默认四级逐级短路/兜底/四部件注入/strategy 整体替换（详见 R13-T06/T07） |
| 规划执行 | R8-T02 | `pytest tests/r8/test_plan.py -q` | steps 游标正确 |
| 反思轮换 | R8-T03 | `pytest tests/r8/test_reflect.py -q` | max_rounds 处结束 |
| 10 架构 | R8-T04 | `pytest tests/r12/test_archs.py -q` | 各端到端通过 |
| 组合算子 | R8-T05 | `pytest tests/r12/test_compose.py -q` | seq/par/branch/budget 生效 |
| 嵌套组合 | R8-T06 | 同上 | 三层嵌套跑通 |
| 装配检查 | R8-T07 | `pytest tests/r12/test_static.py -q` | 悬空边/环装配期报错 |
| 独立闭环 | R8-D01 | `make demo-arch` | demo 冒烟 |

> **实现注**：R8-T01 / T02 / T03 / T08 已在 `tests/r8/` 落地（test_intent.py / test_plan.py / test_reflect.py / test_cascade_service.py）；T04~T07 属 R12 组合算子与架构，见 `tests/r12/`。

## 10. 门禁清单

| 门禁 | 条目 | 达标标准 | 级别 |
|------|------|---------|------|
| R8-G1 | 覆盖 | 认知+10 架构端到端 ≥85% | G1 |
| R8-G2 | 装配检查/幂等 | 悬空边/环/缺 port 装配期报错；compile 幂等 | G2 |
| R8-G3 | 嵌套 | 三层嵌套；换架构零改动 | G3 |
| R8-红线 | 纪律 | 架构内直接 IO/自数轮次→拒绝 | 红线 |

---

## 11. 实现落点（2026-11-23）

**源码**：
- `src/harness/cognition/contracts.py`：IntentResult / IntentStrategy / Planner / Executor / RePlanner / ReflectStrategy / IntentSample / Critique / Step / StepResult（契约单一来源，由 `src/archs/base.py` re-export）。
- `src/harness/cognition/cascade.py`：CascadeIntentStrategy（L1 `RegexKeywordMatcher` → L2 `InMemoryIntentSampleStore` → L3 cheap / L4 primary，经 `yield Invoke(port="llm")`；阈值、trace、generator 内短路）。
- `src/harness/cognition/services.py`：`intent_as_program` 桥接 + 确定性服务 RulePlanner / RuleExecutor / RuleRePlanner / ThresholdReflector。
- `src/archs/recipes.py`：Router / RouterSkill 统一经 `intent_as_program` 单入口消费（原 `_recognize` 内联 `asyncio.run` 已移除）。
- 测试与 demo：`tests/r8/`（22 用例）、`tests/demos/demo_r8.py`；门禁 r8 13/13。

**四项既定决策（实现已遵循）**：
1. **确定性默认**：认知服务默认走规则/阈值实现，不依赖真实 LLM；可整体注入 strategy 或逐部件注入。
2. **单检测器复用**：注入检测复用 `harness.skills.KeywordInjectionDetector`，认知侧不另立检测器（与 R9 共享同一实现）。
3. **R1 EventStore 复用**：进度/快照类持久化复用 R1 EventStore，R8 不重复声明，也不自建事件日志。
4. **全量落地**：四级级联、四部件、桥接与确定性服务一次性交付，不留设计态占位。

---
*R8 模块详设 v2.1 · 已实现（src/harness/cognition/，11-23）*