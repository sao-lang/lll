# 多 Agent 项目编码指南

> 范围：用现有 R1/R2 代码（`src/runtime` + `src/hooks`）做多 Agent 项目。
> 结论先行：**内核原语已具备（CHILD Effect + 递归子Session + 预算分账），架构糖（Supervisor/Blackboard/Handoff 类）还没实现**。
> 本文给三层写法：① 现在就能跑的手搓方式 ② 推荐的封装方式 ③ 设计目标（等 R12/R8 代码）。

---

## 一、代码现状（先说清楚能用什么）

| 能力 | 代码状态 | 位置 |
|------|---------|------|
| `Effect(CHILD)` | ✅ 已实现 | `runtime/types.py` `EffectKind.CHILD` |
| 递归子Session | ✅ 已实现 | `runner.py` `_child()`：子会话 `parent:child:N`，预算 `hops+1` |
| 并行扇出 | ✅ 已实现 | `runner.py` `step()` 用 `asyncio.gather` 并发执行一批 Effect |
| 子事件挂父 trace | ✅ 已实现 | `step()` 把 child_events 追加到父 session.events |
| 子会话ID唯一 | ✅ 已修复 | `runner._child_seq` 单调递增（并发同批不再冲突） |
| 预算分账/并行上限 | ✅ 已实现 | `Budgets.max_parallel_children` / `check_parallel` |
| `JoinStrategy`（all/any/majority） | ❌ 未实现 | 只在 R8/R12 文档里 |
| Supervisor / Blackboard / Handoff 类 | ❌ 未实现 | 只在 R12 文档里 |
| 黑板共享 scratch | ⚠️ 部分 | 可传 `payload["state"]` 给子Agent，但没有自动合并 |

所以**现在就能写多 Agent**，只是要自己写聚合逻辑（join），没有 `Supervisor(...)` 这种一行装配。

---

## 二、写法 1：现在就能跑（手搓 Supervisor）

```python
import asyncio
from runtime import AgentState, Effect, EffectKind, Result, Runner, Session, Usage

# 1. 子Agent：注意 program_factory 必须是 Callable[[], Program]（无参！）
def make_child(name: str):
    def child():
        resp = yield Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": name}]})
        yield Effect(EffectKind.END, {"output": f"{name}: {resp['content']}"})
    return child

# 2. 父Agent（Supervisor）：拆任务 → 并行 CHILD → 自己写 join
def supervisor(state):
    tasks = ["研究市场", "分析竞品", "写摘要"]
    results = yield [                              # 一批 CHILD = 并行扇出
        Effect(EffectKind.CHILD, {"program_factory": make_child(t)}) for t in tasks
    ]
    # CHILD 回填的是子Agent的 output 本身（字符串），不是 dict
    merged = " | ".join(str(r) for r in results)   # 这就是最简 join=all
    yield Effect(EffectKind.END, {"output": merged})

async def main():
    async def call(effect, ctx):
        if effect.kind is EffectKind.LLM:
            return Result.success({"content": "完成"}, usage=Usage(prompt_tokens=10, cost_usd=0.01))
        return Result.success(None)
    async def dispatch(effect, ctx):
        return None

    runner = Runner(dispatch=dispatch, call=call)
    session = Session(session_id="proj", program_factory=lambda: supervisor(AgentState()))
    result = await runner.run(session)
    print(result.status, result.output)

asyncio.run(main())
```

**三条铁律**（踩过的坑）：
1. `program_factory` **必须无参**：写 `lambda: child(...)`，不要直接传 `child(state)`。
2. CHILD 回填给 program 的是**子Agent的 `output` 值本身**（这里 `resp['content']` 拼出的字符串），不是 `Result` 对象、不是 dict。
3. 子会话 ID 由内核保证唯一（`proj:child:0/1/2`），不要自己在 payload 里塞 session_id。

### 换 join 策略（自己写即可）

```python
# any：首个完成就继续（简单版——取第一个非空结果）
merged = next((r for r in results if r), "无结果")

# majority：按多数投票
from collections import Counter
merged = Counter(str(r) for r in results).most_common(1)[0][0]

# reduce：自定义聚合（如打分取最高）
merged = max(results, key=lambda r: score(r))
```

---

## 三、写法 2：推荐的封装（自己写个薄 arch 层）

把"拆任务 + join"抽成可复用的架构函数，上层一行装配——这就是 R12 里 `Supervisor` 的雏形：

```python
from collections.abc import Callable
from runtime import AgentState, Effect, EffectKind, Program

def supervisor_arch(
    planner: Callable[[AgentState], list[str]],
    child_factory: Callable[[str], Callable[[], Program]],
    join: Callable[[list], str] = lambda rs: "|".join(map(str, rs)),
):
    """返回一个 Program：规划拆任务 → 并行子Agent → join。"""
    def program(state):
        tasks = planner(state)
        results = yield [
            Effect(EffectKind.CHILD, {"program_factory": child_factory(t)}) for t in tasks
        ]
        yield Effect(EffectKind.END, {"output": join(results)})
    return program

# 用法：换 planner / 换 join / 换子Agent，上层零改动
arch = supervisor_arch(
    planner=lambda s: ["研究", "分析", "总结"],
    child_factory=make_child,
    join=lambda rs: "\n".join(str(r) for r in rs),
)
session = Session(session_id="p", program_factory=lambda: arch(AgentState()))
```

这正是框架的核心思想：**架构 = 产出 Program 的函数，可任意嵌套**。你写的 `supervisor_arch` 将来 R12 的 `Supervisor` 类就是它的标准化版本。

---

## 四、写法 3：设计目标（等 R12/R8 代码实现后）

文档里的目标装配形态（`docs/02-模块详设/Agent/R12-内置架构与组合.md`）：

```python
# 目标 API（当前代码还不支持，需要实现 archs/ 包）
agent = (Agent(arch=Supervisor(
    planner=MyPlanner(),          # 自定义拆任务策略（Protocol 可替换）
    child_spec=ChildSpec(arch=ReAct(tools=tools), max_children=8),
    join=MajorityVote(),
))
    .capability(RAG(pipeline=...))   # 子Agent可挂自己的能力
    .build())

# 其他多Agent架构（R12 已设计）
Handoff(roles={"客服": arch1, "技术": arch2}, max_hops=5)   # 角色接力
Blackboard(agents=[a1, a2, a3], rounds=3)                  # 共享黑板
Router(classifier=..., arms={"研究": supervisor, "闲聊": react})  # 分流到多Agent
```

**要让这些可用，需要实现**（就是任务清单里待办的 #29）：
- `src/archs/`：10 个架构类 + `Agent` 装配器 + `static_check()`
- `src/cognition/`：Planner/JoinStrategy 等策略协议实现
- R13 的 `Capability` 装配糖

---

## 五、多Agent的全局纪律（沿用编码规则）

1. **预算交内核**：子Agent的 hops/turns/cost 由 R1 `Budgets` 强制，架构里不许自己数数。
2. **副作用带幂等键**：并行子Agent重试时不能重复扣款/发消息。
3. **外部输入不可信**：子Agent返回的文本进父上下文前要过注入检测（R9 guard hook）。
4. **异常不抛穿**：子Agent失败以 `Result.failure()` 回流，父Agent决定降级还是重试。
5. **子会话隔离**：每个子Agent有独立 state/events，共享数据要显式传 `payload["state"]` 或走 MemoryPort。
6. **trace 可对齐**：子事件已自动挂父 trace，日志里用 `session_id` 区分 `proj:child:0/1/2`。

---

## 六、验证

现有代码跑多Agent的最小验证（已纳入测试）：

```bash
.venv/Scripts/python.exe -m pytest tests/r1/test_multiagent.py -q -c pyproject.toml
```

覆盖：并行扇出结果聚合、子会话ID唯一、预算分账。

---

*多Agent编码指南 v1.0 · 2026-11-17 · 对应已实现的 R1/R2 内核原语 + R12 设计目标*
