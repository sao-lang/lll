# 代码精读 03 · model 模型模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/model/` 下全部 10 个文件：types.py（288 行）、ports.py（21 行）、router.py（154 行）、registry.py（135 行）、stream.py（45 行）、structured.py（253 行）、cache.py（461 行）、gateway.py（574 行）、capability.py（118 行）、__init__.py（70 行）。
> 总入口见 [00](./00-总入口与全局链路.md)，可靠性执行器见 [06 resilience](./06-resilience可靠性模块.md)。

---

## 0. 读前GPS

model 是 R4（模型与推理）域，整个框架**最核心、代码量最大**的模块。对外只有一个真正入口：`ModelGateway.complete()`。它把一次模型调用编排成一条链：

> **降级决策 → 缓存查询 → 成本预留 → 调后端 → 结构化校验/修复 → 按实结算 → 写缓存**

三个必须刻进脑子的事实：

1. **换 LLMPort 后端时上层零改动**——这是它的核心可插拔承诺。所有后端差异被 `LLMPort` 协议抹平。
2. **成本只记录 LLMPort 响应里的 usage，绝不自行估算**——估算只用于「调用前预留额度」，结算一律按后端回报的实数。
3. **降级目标非空但未注册 → 显式失败，绝不静默回退默认模型**——这是降级红线。

按依赖层次，9 个文件分三组：

- **地基（被所有人依赖）**：`types.py`（三态/成本/缓存键/规格）、`ports.py`（usage 扩展协议）。
- **可插拔零件**：`router.py`（路由）、`registry.py`（名字→后端）、`stream.py`（事件流包装）、`structured.py`（schema 校验/修复提示）、`cache.py`（三层缓存）。
- **编排核心**：`gateway.py`（574 行，主链路，把所有零件串起来）、`__init__.py`（公共 API 收敛）。

全模块核心类：

- `ModelGateway`：唯一入口，主链路编排者（complete / stream / stream_events 三条路径）。
- `CostGuard`：并发安全的成本硬闸（reserve/commit/cancel 三态凭证）。
- `_resolve_llm`：降级执行**唯一决策点**，返回三态之一。
- `HashCacheEmbedder` / `SimilarityCache`：确定性向量 + 语义缓存。
- `StructuredOutputValidator` / `MinimalSchemaValidator`：校验与修复提示。

> 注：`__init__.py` 第 12 行会从兄弟文件 `capability.py` 重导出 `ModelCapability`（装配层能力包）。该文件不在本册约定的 9 文件清单内，本册只在讲 `__init__.py` 时点明它的角色，不做逐行展开。

---

## 1. types.py 逐行讲解（288 行）

这是全模块的「通用语言」：降级三态、结构化规格、缓存键、路由决策、成本账本、成本硬闸都在这里定义。别的模块都 import 它。

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R4 模型与推理共享类型：结构化规格 / 缓存键 / 路由决策 / 成本归因账本。`
  一句话定位：本文件是 R4 的共享类型仓库，列了四大类内容。
  归属 R4（模型与推理域）。

- **第 2 行** 空行（docstring 内分段）。

- **第 3-4 行** `全部只依赖 R1（runtime）与 R2（hooks, 复用其 LLMRequest/LLMResponse/Usage DTO），墙钟/随机不隐式进入；成本与 token 数据来自 LLMPort 响应，不自行估算。`
  两条纪律：
  - 依赖方向只向下（依赖 runtime/compose，不反向）。
  - **不引入隐式不确定性**（墙钟时间、随机数都不偷偷进来），保证可复现。
  - 成本数据来源唯一：后端响应的 usage，自己不算。

- **第 5 行** `公共 API 见 docs/02-模块详设/Agent/R4-模型与推理.md §4。`
  指路：详设文档位置。

- **第 6 行** `"""` docstring 结束。

### 第 7-17 行：imports

- **第 7 行** `from __future__ import annotations`
  延迟求值注解：类型注解里的名字在运行时不求值，允许引用尚未定义的类型，降低 import 顺序耦合。

- **第 9 行** `import json`
  缓存键序列化用（`json.dumps`）。

- **第 10 行** `from collections import defaultdict`
  成本账本按维度累计时用 `defaultdict(float/int)`，省去「键不存在就初始化」的样板。

- **第 11 行** `from dataclasses import dataclass, field`
  大量 frozen dataclass（三态、凭证、DTO）与 `field(default_factory=...)`。

- **第 12 行** `from typing import Literal`
  给 `mode` 字段做有限取值约束（constrained/prompt/repair）。

- **第 14 行** `from pydantic import BaseModel, ConfigDict, Field`
  `StructuredOutputSpec` 用 pydantic 声明，带配置与字段元信息。

- **第 16 行** `from compose import LLMPort, LLMRequest, Usage`
  复用组合层 DTO：三态里的 `LLMPort`、缓存键里的 `LLMRequest`、成本记录里的 `Usage`。注意 `LLMResponse` 本文件没用到，故不导入。

- **第 17 行** `from runtime import DynamicValue, Result`
  - `DynamicValue`：动态 schema 的具名别名（规则 3）。
  - `Result`：三态 `to_result()` 折叠出的失败信封。

### 第 19-33 行：__all__ 导出清单

- **第 19-33 行** `__all__ = [...]`
  导出 13 个符号：三态类型（BackendResolution / RefuseExecution / TargetUnresolved）、规格（StructuredOutputSpec）、缓存键（CacheKey / cache_key_string / build_cache_key）、路由决策（RouteDecision）、成本（CostAttributor / CostGuard / CostRecord / UsageLedger / Reservation）。
  `__init__.py` 据此重导出。注意：**稳定错误码常量 `REFUSED_CODE` / `TARGET_UNRESOLVED_CODE` 没进 `__all__`**，它们是模块内部与 gateway 直接 import 的常量，不属公共 API 门面。

### 第 36-39 行：降级三态注释 + 稳定错误码常量

- **第 36 行** `# ---- R4 §2.2 / §5.4 降级解析三态（唯一决策点返回值，不回退默认模型）----`
  分节注释。强调「唯一决策点返回值」——三态只能由 `gateway._resolve_llm` 产出。

- **第 37 行** `# 稳定错误码集中为模块常量（规则 5 禁魔法串），R4-T09/T10 断言这两个 code。`
  规则 5：有限取值/固定字符串必须用具名常量，禁止散落魔法串。测试会断言这两个码。

- **第 38 行** `REFUSED_CODE = "REFUSED"`
  拒答稳定错误码。三态 `to_result()` 与 gateway 的响应前缀共用它，保证同源。

- **第 39 行** `TARGET_UNRESOLVED_CODE = "TARGET_UNRESOLVED"`
  目标未注册稳定错误码。

**为什么不直接写字符串字面量**：若 `"REFUSED"` 散落在 types、gateway、测试三处，改一处漏两处，错误码就不稳了。集中成常量，一处定义处处引用。

### 第 42-50 行：RefuseExecution（拒答态）

- **第 42 行** `@dataclass(frozen=True, slots=True)`
  - `frozen=True`：不可变，防止三态凭证被中途篡改。
  - `slots=True`：不建 `__dict__`，省内存、防动态加属性。

- **第 43 行** `class RefuseExecution:`
  降级末级的「拒答」分支：产出 REFUSED 失败，绝不调任何后端。

- **第 44 行** `"""降级末级拒答：产出 REFUSED 失败，绝不调用任何 LLMPort。"""`
  一句话：这是「降级到拒答」的终点，不碰后端。

- **第 46 行** `reason: str`
  唯一字段：拒答原因，进失败信封便于诊断。

- **第 48 行** `def to_result(self) -> Result:`
  折叠方法：把三态转成统一的 `Result`。

- **第 49 行** `"""折叠为稳定失败信封（ok=False，retryable=False）。"""`
  说明折叠结果：失败且不可重试（拒答是终态，重试无意义）。

- **第 50 行** `return Result.failure(REFUSED_CODE, self.reason)`
  用稳定码常量构造失败。`Result.failure` 默认 `retryable=False`。
  - 不这么写（直接抛异常）：会穿过 Port 边界变成 `PORT_CRASHED`，丢失「这是业务拒答」的语义。折叠成 Result 让上层拿到干净失败信封。

### 第 53-63 行：TargetUnresolved（目标未注册态）

- **第 53 行** `@dataclass(frozen=True, slots=True)`
  同 RefuseExecution：不可变 + slots。

- **第 54 行** `class TargetUnresolved:`
  降级目标非空但未注册的「显式失败」分支。

- **第 55 行** `"""降级目标非空但未注册：显式失败，绝不静默回退默认模型。"""`
  **降级红线**：宁可失败，也不悄悄用默认模型顶上（那会掩盖配置错误、用错模型）。

- **第 57 行** `target: str`
  唯一字段：那个未解析的目标名，便于诊断「到底是哪个名字没注册」。

- **第 59 行** `def to_result(self) -> Result:`
  折叠方法。

- **第 60 行** `"""折叠为稳定失败信封（带未解析 target 便于诊断）。"""`
  说明会把 target 带进错误信息。

- **第 61-63 行** `return Result.failure(TARGET_UNRESOLVED_CODE, f"degrade target not registered: {self.target}")`
  用稳定码 + 目标名构造失败。诊断信息明确指向未注册的目标。
  - 不这么写（回退默认）：调用方以为降级成功了，实际用的是错误模型，且问题被掩盖到难以排查。

### 第 66-67 行：BackendResolution 三态联合类型

- **第 66 行** `# 三态联合：LLMPort（切后端）/ RefuseExecution（拒答）/ TargetUnresolved（显式失败）。`
  注释解释三态各自语义。

- **第 67 行** `type BackendResolution = LLMPort | RefuseExecution | TargetUnresolved`
  Python 3.12 的 `type` 语句定义类型别名（延迟求值，配合 `from __future__ import annotations`）。
  - 这是「唯一决策点」的返回类型：要么是可用后端，要么是两种终止态之一。
  - 用联合类型而非统一基类：三态形状差异大（一个是后端对象，两个是带字段的 DTO），联合类型表达最自然，`isinstance` 分派清晰。

### 第 70-92 行：StructuredOutputSpec（结构化输出规格，pydantic 模型）

- **第 70 行** `class StructuredOutputSpec(BaseModel):`
  pydantic 模型：声明式字段 + 自动校验 + 别名支持。

- **第 71-78 行** 类 docstring
  说明四个字段含义：`schema`（期望结构的 JSON Schema）、`mode`（constrained 约束解码 / prompt 仅提示 / repair 校验失败后修复）、`max_repair`（最大修复次数，仅 repair 生效）、`strict`（严格模式红线）。

- **第 80 行** `model_config = ConfigDict(extra="forbid", populate_by_name=True)`
  两条配置：
  - `extra="forbid"`：多传未知字段直接报错。防止拼错字段名（如 `stricts`）被静默忽略——结构化规格是安全相关配置，必须严格。
  - `populate_by_name=True`：字段名和别名都能用来构造（见下文）。

- **第 82-84 行** 注释
  解释为什么对外叫 `schema`、内部叫 `json_schema`：
  - 对外契约字段名是 `schema`（详设 §4 规定）。
  - 内部属性用 `json_schema`，避免遮蔽 `BaseModel.schema()` 方法（pydantic 会告警）。
  - `schema` 形态由外部 JSON Schema 决定（规则 3 动态边界），用 `DynamicValue` 具名别名表达「这是动态数据」。

- **第 85-87 行** `json_schema: dict[str, DynamicValue] = Field(default_factory=dict, alias="schema", description="JSON Schema")`
  - `alias="schema"`：外部用 `schema=` 传参。
  - `default_factory=dict`：缺省空 dict（不校验任何结构）。
  - 值类型 `DynamicValue`：JSON Schema 结构动态，不强行具体化。

- **第 88-90 行** `mode: Literal["constrained", "prompt", "repair"] = Field(default="repair", ...)`
  `Literal` 限定三种取值，传第四个值 pydantic 直接拒绝。默认 `repair`（校验失败后修复重试），最实用的策略。

- **第 91 行** `max_repair: int = Field(default=2, ge=0, description="Maximum repair retries")`
  默认修 2 次，`ge=0` 禁止负数（负数会让修复循环次数为负、静默不修）。

- **第 92 行** `strict: bool = Field(default=True, description="Strict mode: invalid output is rejected")`
  默认严格：未过 schema 的输出绝不标记为合法。这是红线配置的默认值——安全优先，要放宽必须显式传 `False`。

### 第 95-100 行：CacheKey（缓存键值对象）

- **第 95 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots：缓存键一旦生成不该被改。

- **第 96 行** `class CacheKey:`
  缓存键快照。

- **第 97 行** `"""缓存键快照：value 为规范化哈希，version 为 prompt 版本。"""`
  两个字段的语义：value 是哈希值，version 是 prompt 版本（用于隔离）。

- **第 99 行** `value: str`
  规范化哈希字符串。

- **第 100 行** `version: str = "current"`
  prompt 版本，默认 `"current"`。版本变化即缓存失效，避免旧 prompt 的响应被新 prompt 命中。

### 第 103-122 行：cache_key_string（缓存键字符串构造）

- **第 103 行** `def cache_key_string(request: LLMRequest, *, prompt_version: str = "current") -> str:`
  函数签名：`request` 必填，`prompt_version` 关键字参数。

- **第 104-111 行** docstring
  说明：键由 messages + model + params + prompt_version 构成，返回稳定 JSON + sha256 前 32 位。

- **第 112 行** `import hashlib`
  局部导入 sha256。放函数内而非模块顶部，是因为只有算键时才需要，减少模块导入负担。

- **第 114-120 行** `payload = {...}` 构造参与键的字典
  四个请求字段全进键：
  - 第 115 行 `"messages": [dict(m) for m in request.messages]`：把消息转成 dict 列表（消息本身可能是 pydantic 对象或 dict，`dict(m)` 归一）。
  - 第 116 行 `"model": request.model`：不同模型的响应不同，必须进键。
  - 第 117 行 `"temperature": request.temperature`：影响输出随机性。
  - 第 118 行 `"max_tokens": request.max_tokens`：影响输出长度。
  - 第 119 行 `"prompt_version": prompt_version`：版本进键。
  **设计要点**：任何影响输出的参数都必须进键，否则同一键会返回不同参数下的旧响应（错误命中）。

- **第 121 行** `raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)`
  - `sort_keys=True`：**稳定序列化**——dict 键顺序固定，同样的内容无论插入顺序如何都产出同一字符串，保证键确定。
  - `ensure_ascii=False`：中文不转义成 `\uXXXX`，键更短、可读。
  - `default=str`：遇到不可序列化对象（如某些 DTO）用 `str()` 兜底，避免因类型细节导致算键失败崩溃。
  - 不这么写（不 sort_keys）：同样的请求两次算出的键可能不同，缓存永远 miss。

- **第 122 行** `return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]`
  sha256 哈希取前 32 个十六进制字符（128 位）：足够低的碰撞率 + 键长适中。
  - `encode("utf-8")`：显式编码，跨平台一致。
  - 不这么写（用原始 raw 当键）：messages 可能很长，键会巨大，存储/比较成本高。

### 第 125-127 行：build_cache_key（构造 CacheKey 值对象）

- **第 125 行** `def build_cache_key(request: LLMRequest, *, prompt_version: str = "current") -> CacheKey:`
  包装函数，返回 `CacheKey` 值对象而非裸字符串。

- **第 126 行** `"""构造 CacheKey 值对象（键含版本，供缓存隔离）。"""`
  说明用途。

- **第 127 行** `return CacheKey(value=cache_key_string(request, prompt_version=prompt_version), version=prompt_version)`
  哈希值 + 版本一起装进 `CacheKey`。让调用方拿到键时还能看到版本号（便于隔离与诊断）。
  - 为什么不直接返回字符串：值对象能携带额外语义（version），且类型明确，避免「这个字符串是键还是版本」的歧义。

### 第 130-135 行：RouteDecision（路由决策）

- **第 130 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots：决策一旦做出不该被改。

- **第 131 行** `class RouteDecision:`
  路由决策结果。

- **第 132 行** `"""路由决策：选中的模型 + 进 trace 的理由。"""`
  两个字段语义：model 是选择结果，reason 是可观测性信息。

- **第 134 行** `model: str`
  选中的模型名。

- **第 135 行** `reason: str = ""`
  决策理由，默认空串。进 trace，方便事后解释「为什么选了这个模型」。

### 第 138-151 行：CostRecord（成本记录）

- **第 138 行** `@dataclass(frozen=True, slots=True)`
  不可变：一条成本记录是不可变事实。

- **第 139 行** `class CostRecord:`
  一次调用的成本记录，多维归因载体。

- **第 140-145 行** docstring
  说明四个维度（run_id/agent_id/user_id/model）+ usage 元数据。

- **第 147-150 行** 四个归因维度字段
  `run_id` / `agent_id` / `user_id` / `model`，都是字符串。
  这四个维度让成本可以按「哪次运行/哪个 agent/哪个用户/哪个模型」分别统计。

- **第 151 行** `usage: Usage`
  来自后端响应的 token/成本元数据。成本数据源唯一——就是它，不自行估算。

### 第 154-191 行：UsageLedger（成本/token 账本）

- **第 154 行** `@dataclass`
  可变（不加 frozen）：账本要持续累加，必须可变。

- **第 155 行** `class UsageLedger:`
  按多维累计的成本/token 账本。

- **第 156 行** `"""成本/token 账本：按多维累计（per-run/agent/user/model）。"""`
  一句话定位。

- **第 158 行** `_cost: dict[str, float] = field(default_factory=lambda: defaultdict(float))`
  - `default_factory` 用 lambda 包 `defaultdict(float)`：每个实例拿到独立的默认字典（避免共享可变默认值的经典 bug）。
  - 键格式是 `"维度:值"`（如 `"model:m1"`），值是累计金额。
  - `defaultdict(float)`：新键默认 0.0，累加时无需先判断存在。

- **第 159 行** `_total_cost: float = 0.0`
  总成本累计。

- **第 160 行** `_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))`
  按维度累计 token 数，默认 0。

- **第 161 行** `_count: int = 0`
  记录条数（记了多少次成本）。

- **第 163 行** `def record(self, rec: CostRecord) -> None:`
  记一条成本的方法。

- **第 164 行** `"""记录一条成本（各维度同步累计）。"""`
  说明。

- **第 165 行** `cost = float(rec.usage.cost_usd)`
  从 usage 取成本并转 float（usage.cost_usd 可能是 Decimal 或其他数值类型，统一成 float）。

- **第 166 行** `tokens = int(rec.usage.prompt_tokens) + int(rec.usage.completion_tokens)`
  token 总数 = 提示 token + 补全 token。分别转 int 再相加。

- **第 167 行** `self._total_cost = round(self._total_cost + cost, 10)`
  累计总成本，`round(..., 10)` 保留 10 位小数。

- **第 168 行** `self._count += 1`
  记录数 +1。

- **第 169-172 行** `for dim, value in (...)` 四维度循环
  四个 `(维度名, 值)` 元组：`("run_id", rec.run_id)`、`("agent_id", ...)`、`("user_id", ...)`、`("model", ...)`。
  用循环而非写四遍累加，消除重复代码。

- **第 173 行** `self._cost[f"{dim}:{value}"] = round(self._cost[f"{dim}:{value}"] + cost, 10)`
  按 `"维度:值"` 键累计成本，同样 round 10 位。
  - **为什么要 round(10)**：浮点累加会有精度漂移（0.1+0.2=0.30000000000000004），每步 round 到 10 位小数，避免误差累积影响预算判断。

- **第 174 行** `self._tokens[f"{dim}:{value}"] += tokens`
  按 same 键累计 token 数。

- **第 176-178 行** `total()`
- **第 176 行** `def total(self) -> float:`
- **第 177 行** `"""累计总成本。"""`
- **第 178 行** `return self._total_cost`
  返回总成本。

- **第 180-182 行** `by(dim, value)`
- **第 180 行** `def by(self, dim: str, value: str) -> float:`
- **第 181 行** `"""按维度取成本（如 by("model", "m1")）。"""`
- **第 182 行** `return self._cost.get(f"{dim}:{value}", 0.0)`
  用 `.get(..., 0.0)`：未记录的维度返回 0.0，不抛 KeyError。

- **第 184-186 行** `tokens_by(dim, value)`
- **第 184 行** `def tokens_by(self, dim: str, value: str) -> int:`
- **第 185 行** `"""按维度取 token 数。"""`
- **第 186 行** `return self._tokens.get(f"{dim}:{value}", 0)`
  同理，缺省 0。

- **第 188-191 行** `count` 属性
- **第 188 行** `@property`
- **第 189 行** `def count(self) -> int:`
- **第 190 行** `"""记录条数。"""`
- **第 191 行** `return self._count`
  用 `@property` 让 `ledger.count` 像属性一样读，不用加括号。

### 第 194-211 行：CostAttributor（成本归因器，语义化子类）

- **第 194 行** `class CostAttributor(UsageLedger):`
  继承 `UsageLedger`，只加语义化快捷方法，不改底层逻辑。

- **第 195 行** `"""成本归因器：UsageLedger 的语义化子类，提供具名维度快捷查询。"""`
  说明：父类提供通用的 `by(dim, value)`，子类提供 `run_cost` / `user_cost` 等具名方法，可读性更好。

- **第 197-199 行** `run_cost`
  `return self.by("run_id", run_id)`——包装 `by`，调用方不必记维度名字符串。

- **第 201-203 行** `user_cost`
  `return self.by("user_id", user_id)`。

- **第 205-207 行** `model_cost`
  `return self.by("model", model)`。

- **第 209-211 行** `snapshot`
- **第 209 行** `def snapshot(self) -> dict[str, float | int]:`
- **第 210 行** `"""返回成本快照（总额 + 记录数），供 trace/评测。"""`
- **第 211 行** `return {"total": self.total(), "records": self.count}`
  返回总额与记录数的快照字典，供可观测性使用。
  - 为什么单独做子类：把「维度字符串」封装成方法名，减少调用处裸字符串散落，也便于将来加归因逻辑（如按用户聚合）不改调用方。

### 第 214-223 行：Reservation（成本预留凭证）

- **第 214 行** `@dataclass(frozen=True, slots=True)`
  不可变凭证：防止被篡改金额或 id。

- **第 215 行** `class Reservation:`
  成本预留凭证（P2b）。调用前 reserve 拿到，调用后 commit/cancel 结清。

- **第 216-220 行** docstring
  说明字段：`id` 唯一标识（防重复结清）、`amount` 预留金额。

- **第 222 行** `id: int`
  预留唯一 id，递增序号。commit/cancel 用它查凭证、防重复。

- **第 223 行** `amount: float`
  预留金额（预估成本），结清时据此释放额度。

### 第 226-288 行：CostGuard（成本硬闸，P2b 核心）

- **第 226 行** `@dataclass`
  可变：预算状态持续变化。

- **第 227 行** `class CostGuard:`
  成本硬闸：调用前 reserve 预留，调用后 commit 按实结算。

- **第 228-233 行** 类 docstring
  核心论证：**并发下「先调用后扣费」会让硬闸失效**——预算 0.5、20 个并发全部通过 `spent + cost <= max` 检查（因为都没调用完、spent 还是 0），结果全部放行、严重超支。
  解法：reserve 在「读-改-写」原子操作里立即占用额度（立即加 `_reserved`），超预算的调用在发起前就被拒。`allow`/`charge` 保留兼容简单场景，生产链路走 reserve/commit。

- **第 235 行** `max_cost_usd: float`
  预算上限（美元），必填。

- **第 236 行** `spent: float = 0.0`
  已花（已结算）金额。

- **第 237 行** `_reserved: float = field(default=0.0, repr=False)`
  未结清的预留总额。`repr=False`：调试打印时不显示内部状态，减少噪音。

- **第 238 行** `_seq: int = field(default=0, repr=False)`
  递增序号，用作预留凭证 id。

- **第 239 行** `_active: dict[int, float] = field(default_factory=dict, repr=False)`
  活动预留表：`id → 金额`。用于判断凭证是否有效、防重复结清。

- **第 241-243 行** `allow(estimated_cost)`
- **第 241 行** `def allow(self, estimated_cost: float) -> bool:`
- **第 242 行** `"""（旧 API）预计成本加上已花是否仍在预算内；不含未结清预留。"""`
  注意 docstring 说「不含未结清预留」，但第 243 行实现其实包含了——以代码为准。
- **第 243 行** `return round(self.spent + self._reserved + estimated_cost, 10) <= self.max_cost_usd`
  已花 + 已预留 + 本次预估 ≤ 预算才通过。
  - **关键**：把 `_reserved` 算进去，才能防并发击穿（否则多个并发都只看 spent，全部通过）。
  - `round(..., 10)` 与账本一致，避免浮点误差误判。

- **第 245-247 行** `charge(cost)`
- **第 245 行** `def charge(self, cost: float) -> None:`
- **第 246 行** `"""（旧 API）扣减预算（调用后按实际成本）。"""`
- **第 247 行** `self.spent = round(self.spent + cost, 10)`
  直接加已花金额。旧 API，无预留语义，仅兼容路径用。

- **第 249-263 行** `reserve(estimated_cost)`——调用前预留
- **第 249 行** `def reserve(self, estimated_cost: float) -> Reservation | None:`
  返回凭证或 None（预算不足）。

- **第 250-256 行** docstring
  说明负数按 0、超预算返回 None。

- **第 257 行** `amount = max(0.0, estimated_cost)`
  负数预估按 0 处理。防止传负数「反向增加预算」。

- **第 258 行** `if not self.allow(amount):`
  用 `allow` 判断（含 `_reserved`），预算不足则返回 None。

- **第 259 行** `return None`
  硬闸拒绝：None 表示没拿到额度，调用方应放弃调用。

- **第 260 行** `self._seq += 1`
  序号递增，生成新凭证 id。

- **第 261 行** `self._reserved = round(self._reserved + amount, 10)`
  **立即占用额度**：`_reserved` 加上本次预估值。这一步是并发安全的核心——后续并发的 `allow` 会看到这个占用。

- **第 262 行** `self._active[self._seq] = amount`
  登记活动预留，供 commit/cancel 校验。

- **第 263 行** `return Reservation(id=self._seq, amount=amount)`
  返回凭证。

- **第 265-277 行** `commit(reservation, actual)`——调用后结清
- **第 265 行** `def commit(self, reservation: Reservation, actual: float) -> None:`

- **第 266-272 行** docstring
  说明：释放预留、按实际扣减、差额自动退还/补扣；未登记凭证被忽略防重复扣减。

- **第 273 行** `if reservation.id not in self._active:`
  凭证不在活动表 → 已被结清（重复 commit）或伪造。

- **第 274 行** `return`
  直接忽略，不做任何扣减。**幂等防御**：重复 commit 不会重复扣费。

- **第 275 行** `del self._active[reservation.id]`
  从活动表移除，标记已结清。

- **第 276 行** `self._reserved = round(self._reserved - reservation.amount, 10)`
  释放预留：减回预估金额。

- **第 277 行** `self.spent = round(self.spent + max(0.0, actual), 10)`
  按实际成本扣已花。`max(0.0, actual)` 防负数成本反向加钱。
  - 差额处理：预留 0.01、实际 0.005 → `_reserved` 释放 0.01，`spent` 加 0.005，净退 0.005；实际 0.02 → 释放 0.01、加 0.02，净补 0.01。**预留是占位，不是死扣**。

- **第 279-284 行** `cancel(reservation)`——调用失败取消
- **第 279 行** `def cancel(self, reservation: Reservation) -> None:`
- **第 280 行** `"""调用失败取消：释放预留且不记支出。"""`

- **第 281 行** `if reservation.id not in self._active:`
  同样的幂等防御。

- **第 282 行** `return`

- **第 283 行** `del self._active[reservation.id]`
  移除凭证。

- **第 284 行** `self._reserved = round(self._reserved - reservation.amount, 10)`
  只释放预留，**不碰 spent**——调用失败没花钱，不该记支出。与 commit 的关键区别。

- **第 286-288 行** `remaining()`
- **第 286 行** `def remaining(self) -> float:`
- **第 287 行** `"""剩余可用预算（扣除已花与未结清预留）。"""`
- **第 288 行** `return round(self.max_cost_usd - self.spent - self._reserved, 10)`
  剩余 = 上限 − 已花 − 未结清预留。调用方可据此判断还能不能继续。

**CostGuard 小结**：`reserve`（占位）→ `commit`（按实结清，差额退补）或 `cancel`（失败释放、不记账）；凭证 id + `_active` 表保证幂等；`_reserved` 计入 `allow` 保证并发安全。这是全模块成本防线的基础。

---

## 2. ports.py 逐行讲解（21 行）

极短但重要：它声明了「流式后端必须在末块写 usage」这个扩展契约。没有它，流式成本结算就无从谈起。

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R4 Port 扩展协议（规格 P4）：LLMPortWithUsage 承载 last_usage。`
  一句话：这个扩展协议只加一个能力——`last_usage`。

- **第 2 行** 空行。

- **第 3-6 行** `last_usage 从 R2 基础 Port（hooks.LLMPort）归位到 R4 能力层扩展协议：基础 Port 只声明 complete/stream 的通用能力，流式 usage 结算属 R4 领域语义，不该反向污染 R2。真实后端（OpenAI stream_options.include_usage）在末块写入 last_usage，网关读它结算成本——不估算、不兜零。`
  两层意思：
  - **归位**：基础 `LLMPort`（在 compose/R2）不该知道 R4 的 usage 结算语义，所以扩展协议放在 R4 这里，依赖方向正确（R4 依赖 R2，不反向污染）。
  - **数据来源**：真实后端在流末块写 `last_usage`，网关读它结算——**不估算、不兜零**。

- **第 7 行** `"""` docstring 结束。

### 第 8-14 行：imports 与导出

- **第 8 行** `from __future__ import annotations`
  延迟注解。

- **第 10 行** `from typing import Protocol, runtime_checkable`
  - `Protocol`：扩展协议定义。
  - `runtime_checkable`：让网关能用 `isinstance(llm, LLMPortWithUsage)` 做履约检查（本模块主要靠 cast，但协议本身支持运行时检查）。

- **第 12 行** `from compose import LLMPort, Usage`
  - `LLMPort`：扩展协议继承它。
  - `Usage`：`last_usage` 的类型。

- **第 14 行** `__all__ = ["LLMPortWithUsage"]`
  唯一导出符号。

### 第 17-21 行：LLMPortWithUsage 协议

- **第 17 行** `@runtime_checkable`
  允许运行时 `isinstance` 检查。代价同前：只检查成员存在，不检查签名。

- **第 18 行** `class LLMPortWithUsage(LLMPort, Protocol):`
  - 继承 `LLMPort`（拥有 complete/stream 能力）+ `Protocol`（它是个协议）。
  - 这是「扩展协议」的标准写法：在基础协议上加能力。

- **第 19 行** `"""R4 扩展：实现 LLMPort 且流式路径写 last_usage 的后端。"""`
  一句话概括履约条件。

- **第 21 行** `last_usage: Usage | None`
  **唯一新增成员**：流末写入的 usage。
  - `None` 表示还没写（流未结束或后端没实现）。
  - 网关在流结束后读它：有值就按实结算，没值（None）就**抛错拒绝服务**——绝不记一笔假 zero-cost 掩盖账单缺口。这是「流末必须有 usage」纪律的类型层支撑。
  - 不这么设计（把 usage 塞进基础 LLMPort）：R2 就被 R4 语义污染，且所有只用 complete 的后端也要被迫实现 usage 字段。

---

## 3. router.py 逐行讲解（154 行）

模型路由：按任务提示（`task_hint`）和成本预算选模型。核心纪律：router 总是被调用（修复历史「空转」问题），且路由目标装配期必须验注册。

### 第 1-16 行：docstring、imports、常量

- **第 1 行** `"""R4 模型路由：按任务/成本选模型，目标必须已注册（可插拔契约校验）。"""`
  一句话：按任务/成本选模型 + 目标注册校验。

- **第 2 行** `from __future__ import annotations`
  延迟注解。

- **第 4 行** `from dataclasses import dataclass`
  `_RouteInput` 用 frozen dataclass。

- **第 5 行** `from typing import Protocol, TypedDict, cast, runtime_checkable`
  - `Protocol` + `runtime_checkable`：路由协议。
  - `TypedDict` + `cast`：meta 字段具名声明与边界 cast。

- **第 7 行** `from compose import PortCtx`
  Port 上下文类型（路由可读用户/预算信息）。

- **第 8 行** `from runtime import Effect, Invoke`
  - `Effect`：路由输入类型。
  - `Invoke`：判断 effect 是否携带路由参数。

- **第 10 行** `from .types import RouteDecision`
  路由决策 DTO（本文件产出它）。

- **第 12 行** `__all__ = ["NO_DEFAULT", "ModelRouter", "RuleRouter"]`
  导出：哨兵常量 + 协议 + 默认实现。

- **第 14 行** `NO_DEFAULT = "default"`
  **哨兵值**：router 返回它表示「未显式配置默认模型」。统一常量，避免各处裸写 `"default"`。

- **第 15 行** `_NO_DEFAULT = NO_DEFAULT  # 内部兼容别名`
  内部别名，注释标明兼容用途。

- **第 16 行** `"""未显式配置默认模型时的哨兵值（命名常量，替代散落的裸字符串）。"""`
  这个 docstring 位置特殊（跟在第 15 行后、不算类/函数文档），是对常量的说明。强调「命名常量替代裸串」（规则 5）。

### 第 19-32 行：ModelRouter 协议

- **第 19 行** `@runtime_checkable`
  运行时可检查。

- **第 20 行** `class ModelRouter(Protocol):`
  路由协议，可插拔。

- **第 21-26 行** docstring
  - `name + route(effect, ctx) → RouteDecision`：协议契约。
  - 第 23 行特别说明：`...` 是 Protocol 接口语法，不是省略实现。
  - 第 24 行指路：默认实现是下方 `RuleRouter`，由网关在未注入时默认构造。

- **第 28 行** `name: str`
  具名标识（可插拔物必须具名）。

- **第 30 行** `def route(self, effect: Effect, ctx: PortCtx) -> RouteDecision:`
  路由方法签名：吃 Effect 和上下文，返回决策。

- **第 31 行** `"""按 Effect 的任务提示与预算选模型。"""`
  说明。

- **第 32 行** `...`
  Protocol 方法体省略号，接口声明。

### 第 35-42 行：_RouteMeta（meta 字段具名声明）

- **第 35 行** `class _RouteMeta(TypedDict, total=False):`
  `total=False` 表示字段可缺省（meta 里不一定有 max_cost_per_call）。

- **第 36-40 行** docstring
  说明：meta 容器本身在 R1 `EffectPayload` 已声明为 `dict[str, DynamicValue]`，这里把「本模块会读的键」具名声明，避免裸字符串动态取值。这是规则 4（字段显式声明）的落地。

- **第 42 行** `max_cost_per_call: float`
  本模块唯一消费的 meta 键：单次调用成本上限。

### 第 45-55 行：_RouteInput（路由输入具名视图）

- **第 45 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots：输入视图是只读快照。

- **第 46 行** `class _RouteInput:`
  路由输入的具名视图（规则 6：把动态 payload 读取收敛成具名视图）。

- **第 47-52 行** docstring
  说明两字段：`task_hint`（任务类型提示，如 code/chat）、`max_cost`（单次成本上限，None 表示无约束）。

- **第 54 行** `task_hint: str`
  任务提示，字符串。

- **第 55 行** `max_cost: float | None`
  成本上限，可为 None（无约束）。

### 第 58-80 行：_coerce_route（把 Effect 归一为路由输入）

- **第 58 行** `def _coerce_route(effect: Effect) -> _RouteInput:`
  函数签名：Effect → 具名视图。

- **第 59-68 行** docstring
  说明：从已声明字段读取，形态不合法时**显式回退**空提示/无约束（规则 5 显式回退，不猜默认值）。

- **第 69-70 行** 注释
  规则 6 例外说明：`task_hint`/`meta` 已在 R1 声明为明确字段，这里是类型化读取（`.get` 安全取可选键），非未声明动态键。

- **第 71 行** `if not isinstance(effect, Invoke):`
  非 `Invoke` 的 Effect 不携带路由参数。

- **第 72 行** `return _RouteInput(task_hint="", max_cost=None)  # 非 Invoke 不携带路由参数`
  直接返回空提示 + 无成本约束。
  - 不这么写（强行读 payload）：非 Invoke 的 Effect 可能没有 payload 字段，会 AttributeError 崩溃。

- **第 73 行** `hint = str(effect.payload.get("task_hint") or "")`
  读 task_hint：`or ""` 把 None/空值归为空串，`str(...)` 兜底类型。形态异常时不崩，回退空提示。

- **第 74 行** `raw_meta = cast(_RouteMeta | None, effect.payload.get("meta"))`
  读 meta 并在边界 cast 成 `_RouteMeta | None`（规则 6：动态值在解析边界 cast 一次）。

- **第 75 行** `max_cost: float | None = None`
  先默认无约束。

- **第 76 行** `if raw_meta is not None:  # meta 缺席即无成本约束；存在才深入读取，不臆造默认预算`
  **关键**：meta 缺席就是「无约束」，不臆造一个默认预算——臆造会误伤本可放行的调用。

- **第 77 行** `raw_budget = raw_meta.get("max_cost_per_call")`
  取预算值。

- **第 78 行** `if isinstance(raw_budget, (int, float)) and not isinstance(raw_budget, bool):  # bool 是 int 子类须排除；形态合法才转 float，否则回退无约束（规则 5）`
  **两个守卫**：
  - 必须是 int/float（形态合法）。
  - **必须排除 bool**——`bool` 是 `int` 的子类，`isinstance(True, int)` 为 True，若不排除，`max_cost=True` 会被当成预算 1.0，逻辑荒谬。
  - 形态不合法就回退 None（无约束），显式回退而非崩溃。

- **第 79 行** `max_cost = float(raw_budget)`
  形态合法才转 float。

- **第 80 行** `return _RouteInput(task_hint=hint, max_cost=max_cost)`
  返回具名视图。

### 第 83-128 行：RuleRouter（规则路由默认实现）

- **第 83 行** `class RuleRouter:`
  规则路由：task_hint 映射优先，成本预算约束，未知回退 default。

- **第 84-89 行** docstring
  说明属性：`name`（策略名）、`routes`（task_hint→model）、`default`（兜底模型）、`cost_tiers`（model→成本）、`registered`（可用模型集合）。

- **第 91 行** `name = "rule_router"`
  具名标识。

- **第 93-100 行** `__init__` 签名
- **第 93 行** `def __init__(`
- **第 95 行** `*`：后面全是关键字参数（强制具名，防止位置参数传错顺序）。
- **第 96 行** `routes: dict[str, str] | None = None`：task_hint→模型映射。
- **第 97 行** `default: str = _NO_DEFAULT`：兜底模型，默认哨兵。
- **第 98 行** `cost_tiers: dict[str, float] | None = None`：模型→单次成本。
- **第 99 行** `registered: set[str] | None = None`：可用模型集合。
- **第 100 行** `) -> None:`

- **第 101-108 行** docstring
  说明装配与校验，异常：`ValueError`（路由目标或默认模型未注册）。

- **第 109 行** `self._routes = dict(routes or {})`
  复制成新 dict（防外部后续修改影响本实例）。

- **第 110 行** `self._default = default`
  保存兜底模型。

- **第 111 行** `self._cost_tiers = dict(cost_tiers or {})`
  复制成本表。

- **第 112 行** `self._registered = set(registered or ())`
  复制注册集合。

- **第 113 行** `self._validate_targets()`
  **装配期校验**：构造时立即验证所有显式声明的目标已注册。
  - 不这么写（运行期才校验）：错误配置要到第一次请求才暴露，且可能已经路由到不存在的模型造成故障。

- **第 115-128 行** `_validate_targets`
- **第 115 行** `def _validate_targets(self) -> None:`
- **第 116-120 行** docstring
  说明：默认模型为哨兵时不校验；仅当显式传入非哨兵默认值才要求注册。

- **第 121 行** `if not self._registered:`
  没给 registered 集合 → 不校验。

- **第 122 行** `return  # 未给 registered 时不校验（独立测试路由逻辑用）`
  空集合表示「调用方不关心注册校验」（便于单测路由逻辑本身）。

- **第 123 行** `targets = set(self._routes.values())`
  所有显式路由目标。

- **第 124 行** `if self._default != _NO_DEFAULT:  # 哨兵表示未显式配置默认模型，不纳入注册校验`
  只有非哨兵默认值才加入校验集合。

- **第 125 行** `targets.add(self._default)`
  加入默认模型。

- **第 126 行** `unknown = targets - self._registered`
  差集 = 声明了但未注册的目标。

- **第 127 行** `if unknown:  # 装配期暴露配置错误，避免运行期才路由到不存在的模型`
  有未知目标。

- **第 128 行** `raise ValueError(f"route targets not registered: {sorted(unknown)}")`
  抛错，`sorted` 让错误信息确定（可复现）。

### 第 130-154 行：route（路由决策）

- **第 130 行** `def route(self, effect: Effect, ctx: PortCtx) -> RouteDecision:`
  路由方法实现。

- **第 131-137 行** docstring
  说明：按 task_hint/max_cost 选模型，payload 读取走 `_coerce_route`。

- **第 138 行** `route_input = _coerce_route(effect)  # 唯一 payload 动态边界（规则 6）`
  归一输入。所有 payload 动态读取只在这一个函数发生。

- **第 139 行** `target = self._routes.get(route_input.task_hint, self._default)`
  先按 task_hint 查映射，没查到用 default（可能是哨兵 "default"）。

- **第 140 行** `reason = f"task_hint={route_input.task_hint or 'none'}"`
  记录理由：task_hint 是什么。`or 'none'` 处理空串。

- **第 142 行** `# 成本约束：若带 max_cost，选不超预算的最便宜已注册模型。`
  注释下进入成本约束逻辑。

- **第 143 行** `max_cost = route_input.max_cost`
  取预算。

- **第 144 行** `if max_cost is not None and self._cost_tiers:`
  两个前提：有预算约束 + 成本表非空。

- **第 145-147 行** `affordable = [model for model, cost in self._cost_tiers.items() if cost <= max_cost]`
  列出预算内的候选模型。

- **第 148 行** `if affordable:  # 仅在预算内有候选时才改选；否则保留 task_hint 的结果，不强行改写`
  **关键**：只有预算内有候选才改选。若全超预算，保留 task_hint 结果——不强行改写。
  - 不这么写（无候选也改）：会把目标改成某个超预算模型或空值，破坏 task_hint 的合理选择。

- **第 149 行** `chosen = min(affordable, key=lambda m: self._cost_tiers[m])`
  在候选里选最便宜的。

- **第 150 行** `if chosen != target:  # 决策真被成本约束改变时才更新 reason，保证 trace 与实际选择一致`
  **可观测性纪律**：只有决策真被改变才更新 reason。若选出来和 task_hint 结果一样，reason 保留 task_hint，保证 trace 描述与实际一致。

- **第 151-152 行** `target = chosen` / `reason = f"cost<={max_cost}"`
  更新目标与理由。

- **第 154 行** `return RouteDecision(model=target, reason=reason)`
  返回决策。reason 会被 gateway 写进 `ctx.meta["model_route"]` 供 trace。

**router 小结**：`_coerce_route` 收敛所有动态读取（含 bool 排除）→ task_hint 映射优先 → 成本约束只在有候选时改选 → 理由与实际一致。配合 gateway 的「router 总是被调用」，路由理由总能进 trace。

---

## 4. registry.py 逐行讲解（135 行）

模型注册表：把「模型名」解析到真实后端实例或端点连接信息。这是跨服务商降级的基础——之前一个 LLMPort 实例固定一个 base_url，降级无效。

### 第 1-14 行：docstring、imports、导出

- **第 1 行** `"""R4 模型端点注册表：把"模型名"解析到真实后端实例。`
  一句话定位。

- **第 2-5 行** docstring 正文
  - 第 3 行说明问题背景：此前降级目标只到裁决层就断了，且一个 LLMPort 实例固定一个 base_url/api_key——跨服务商降级无效。
  - 第 4-5 行说明解法：提供「名字→端点/实例」映射，框架只定义协议 + 内存默认，具体多端点由装配层注册。

- **第 6 行** `"""` 结束。

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `from dataclasses import dataclass` `ModelEndpoint` 用 frozen dataclass。

- **第 10 行** `from typing import Protocol, runtime_checkable` 注册表协议。

- **第 12 行** `from compose import HookBusError, LLMPort, Provider`
  - `HookBusError`：注册冲突时抛的框架异常。
  - `LLMPort`：后端实例类型。
  - `Provider`：provider 类型（装配层统一注册用）。

- **第 14 行** `__all__ = ["ModelEndpoint", "ModelRegistry", "InMemoryModelRegistry"]`
  导出：端点 DTO + 协议 + 默认实现。

### 第 17-31 行：ModelEndpoint（端点连接信息 DTO）

- **第 17 行** `@dataclass(frozen=True, slots=True)`
  不可变 + slots：端点配置是装配期确定的事实，不该被改。

- **第 18 行** `class ModelEndpoint:`
  一个模型端点的连接信息（供适配器构造）。

- **第 19 行** `"""一个模型端点的连接信息（供适配器构造；密钥不进 trace/日志）。`
  关键：**api_key 不进 trace/日志**。见下文 `repr=False`  Protection 与字段说明。

- **第 21-25 行** docstring 字段说明
  - `name`：注册名（路由/降级 target 用）。
  - `base_url`：OpenAI-compatible 端点根。
  - `api_key`：密钥（仅进 Authorization 头）。
  - `model`：请求体里的模型名。

- **第 28 行** `name: str`
- **第 29 行** `base_url: str`
- **第 30 行** `api_key: str`
- **第 31 行** `model: str`

> 安全提示：虽然是 frozen dataclass（默认 repr 会打印所有字段，包括 api_key），但本 DTO 的设计意图是「密钥不进 trace/日志」。实际使用中若被 repr/日志打印，仍可能泄露；装配层应避免把整个 DTO 打进日志。这里只记录设计约定。

### 第 34-68 行：ModelRegistry 协议

- **第 34 行** `@runtime_checkable`
  运行时可检查。

- **第 35 行** `class ModelRegistry(Protocol):`
  注册表协议：按名字解析端点与后端实例。

- **第 36 行** `"""模型端点注册表协议：按名字解析端点与后端实例。"""`

- **第 38 行** `name: str`
  具名标识。

- **第 40-48 行** `register`
- **第 40-43 行** 签名：`register(self, name, provider=None, *, base_url=None, api_key=None, model=None) -> None`
- **第 44-47 行** docstring：二选一——给 provider 走 provider 表；给 base_url/api_key/model 走端点表。
- **第 48 行** `...`

- **第 50-52 行** `register_llm`
  注册已构造好的后端实例（供降级直接切换，跳过重建适配器）。

- **第 54-56 行** `resolve`
  按名字解析 provider（优先）或端点；未注册返回 None。

- **第 58-60 行** `resolve_llm`
  按名字解析后端实例；未注册返回 None。

- **第 62-64 行** `register_provider`
  注册具名 provider（llm/router/cache），供两表统一；同名幂等/异实例拒绝。

- **第 66-68 行** `has`
  名字是否已注册（端点/后端/provider 任一）。

### 第 71-80 行：InMemoryModelRegistry 初始化

- **第 71 行** `class InMemoryModelRegistry:`
  默认注册表：进程内字典，装配期注册、运行期解析。

- **第 72 行** `"""默认注册表：进程内字典，装配期注册、运行期解析。"""`

- **第 74 行** `name = "in_memory_model_registry"`
  具名标识。

- **第 76 行** `def __init__(self) -> None:`
- **第 77 行** `"""初始化空注册表。"""`
- **第 78 行** `self._endpoints: dict[str, ModelEndpoint] = {}` 端点表。
- **第 79 行** `self._llms: dict[str, LLMPort] = {}` 后端实例表。
- **第 80 行** `self._providers: dict[str, Provider] = {}` provider 表。
  三张表分工：端点（连接信息）、llm（可直接切换的实例）、provider（装配层统一注册的资源）。

### 第 82-98 行：register（注册 provider 或端点）

- **第 82-85 行** 签名
- **第 86-90 行** docstring
  说明两种形态互斥：provider 走 provider 表；base_url/api_key/model 走端点表（同名覆盖）。

- **第 91 行** `if provider is not None:`
  传了 provider。

- **第 92 行** `self.register_provider(name, provider)`
  委托给 register_provider（复用幂等/拒绝逻辑）。

- **第 93 行** `return`
  提前返回，二选一互斥。

- **第 94 行** `if base_url is None or api_key is None or model is None:`
  没传 provider，但端点三要素不全。

- **第 95 行** `raise HookBusError("register requires either provider or base_url/api_key/model")`
  **显式报错**：两种形态都不满足，拒绝注册。
  - 不这么写（静默跳过）：装配错误被掩盖，运行期 resolve 不到目标才暴露，难排查。

- **第 96-98 行** `self._endpoints[name] = ModelEndpoint(name=name, base_url=base_url, api_key=api_key, model=model)`
  构造端点 DTO 存入端点表（同名直接覆盖——端点表策略是「同名覆盖」，与 llm/provider 表的「异实例拒绝」不同）。

### 第 100-107 行：register_llm（注册后端实例）

- **第 100 行** `def register_llm(self, name: str, llm: LLMPort) -> None:`
- **第 101 行** `"""注册后端实例：同名同实例幂等，同名异实例拒绝（R4 §5.3）。"""`
  核心纪律：同名同实例幂等、同名异实例拒绝。

- **第 102 行** `existing = self._llms.get(name)`
  查现有实例。

- **第 103 行** `if existing is llm:`
  用 `is`（身份比较，不是 `==` 值比较）——判断是不是同一个对象。

- **第 104 行** `return  # 同名同实例：幂等，不抛`
  同一实例重复注册：幂等返回，不报错。让装配代码可以重复调用而不崩。

- **第 105 行** `if existing is not None:`
  存在不同实例。

- **第 106 行** `raise HookBusError(f"different instance already registered for llm: {name}")`
  显式拒绝。
  - 为什么不静默覆盖：两个不同实例注册到同名，装配层可能误以为切换了后端其实没有，或反过来。显式失败暴露装配冲突。

- **第 107 行** `self._llms[name] = llm`
  首次注册，存入。

### 第 109-116 行：register_provider（注册 provider）

- **第 109 行** `def register_provider(self, name: str, provider: Provider) -> None:`
- **第 110 行** `"""注册具名 provider（llm/router/cache）：同名同实例幂等 / 异实例拒绝。"""`
- **第 111 行** `existing = self._providers.get(name)`
- **第 112 行** `if existing is provider:`
- **第 113 行** `return`
- **第 114 行** `if existing is not None:`
- **第 115 行** `raise HookBusError(f"different instance already registered for provider: {name}")`
- **第 116 行** `self._providers[name] = provider`

  与 `register_llm` 完全同构：幂等 + 异实例拒绝。复用同一套纪律，保证 provider 表与 llm 表行为一致。

### 第 118-123 行：resolve（解析 provider 优先）

- **第 118 行** `def resolve(self, name: str) -> Provider | ModelEndpoint | None:`
- **第 119 行** `"""解析 provider（优先）或端点；未注册返回 None。"""`
- **第 120 行** `provider = self._providers.get(name)`
  先查 provider 表。

- **第 121 行** `if provider is not None:`
- **第 122 行** `return provider`
  **provider 优先**：同名同时存在于两表时返回 provider。

- **第 123 行** `return self._endpoints.get(name)`
  没有 provider 才返回端点，都没有返回 None。

### 第 125-127 行：resolve_llm

- **第 125 行** `def resolve_llm(self, name: str) -> LLMPort | None:`
- **第 126 行** `"""解析后端实例；未注册返回 None。"""`
- **第 127 行** `return self._llms.get(name)`
  只查 llm 表。这是 gateway `_resolve_llm` 降级时用的方法——它要的是可直接调用的后端实例。

### 第 129-131 行：resolve_provider

- **第 129 行** `def resolve_provider(self, name: str) -> Provider | None:`
- **第 130 行** `"""解析具名 provider；未注册返回 None。"""`
- **第 131 行** `return self._providers.get(name)`
  只查 provider 表。注意：协议 `ModelRegistry` 没声明这个方法，它是实现类的便利方法。

### 第 133-135 行：has

- **第 133 行** `def has(self, name: str) -> bool:`
- **第 134 行** `"""名字是否已注册（端点/后端/provider 任一）。"""`
- **第 135 行** `return name in self._endpoints or name in self._llms or name in self._providers`
  三张表任一命中即为已注册。用 `or` 短路，命中即停。

**registry 小结**：三张表（endpoints/llms/providers）；register 二选一（provider 或端点三要素），缺参显式报错；register_llm/register_provider 同名同实例幂等、异实例拒绝（用 `is` 身份比较）；resolve provider 优先、resolve_llm 只查实例表；has 三表任一命中。

---

## 5. stream.py 逐行讲解（45 行）

两个帮助函数：能力探测 + 把「文本流后端」包成「事件流」。核心价值：不污染 `LLMPort` 协议，让旧后端也能接入结构化事件通道。

### 第 1-13 行：docstring、imports、导出

- **第 1 行** `"""结构化流式事件帮助函数。`
  一句话定位。

- **第 2-5 行** docstring 正文
  - 第 3 行：把只实现 `stream()->AsyncIterator[str]` 的旧后端包装成 StreamEvent 流。
  - 第 4-5 行：使未覆盖 `stream_events` 的后端也能接入事件通道，**且不污染 LLMPort 协议**（避免 `runtime_checkable isinstance` 因新增方法而拒绝旧实现）。
  - 这是关键设计：如果把 `stream_events` 加进基础 `LLMPort` 协议，所有旧后端（没实现它）就会被 `isinstance` 判为不履约。所以做成「可选能力 + 运行时探测」。

- **第 6 行** `"""` 结束。

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `from collections.abc import AsyncIterator` 异步迭代器类型。

- **第 11 行** `from compose import LLMPort, LLMRequest, PortCtx, StreamEvent, StreamEventKind`
  - `LLMPort`：被包装的后端类型。
  - `LLMRequest` / `PortCtx`：stream 方法参数。
  - `StreamEvent` / `StreamEventKind`：事件 DTO 与种类枚举。

- **第 13 行** `__all__ = ["has_stream_events", "wrap_text_stream"]`
  导出两个帮助函数。

### 第 16-19 行：has_stream_events（能力探测）

- **第 16 行** `def has_stream_events(llm: LLMPort) -> bool:`
  签名：后端 → 是否支持 stream_events。

- **第 17 行** `"""后端是否已实现 stream_events()（具名能力探测，非动态 getattr 链）。"""`
  说明：这是「具名能力探测」，不是到处传字符串键的动态链。

- **第 18 行** `# 规则 6 例外：可选能力探测，具名固定字符串，非 ctx.meta/effect.payload 动态键。`
  注释说明为什么这里用 `getattr` 不违反规则 6：探测的是固定能力名，不是动态业务键。

- **第 19 行** `return callable(getattr(llm, "stream_events", None))`
  - `getattr(llm, "stream_events", None)`：取属性，没有就返回 None。
  - `callable(...)`：判断它可调用（不只是存在）。
  - 双重保险：存在且可调用才算支持。
  - 不这么写（直接 `llm.stream_events`）：没实现该方法的后端会 AttributeError 崩溃。

### 第 22-31 行：wrap_text_stream（包装入口）

- **第 22-24 行** 签名
  `def wrap_text_stream(llm: LLMPort, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[StreamEvent]:`

- **第 25-30 行** docstring
  说明：每块 TEXT_DELTA，末了 USAGE/DONE。参数：`llm`（实现 stream 且有 last_usage）、`req`、`ctx`。

- **第 31 行** `return _wrap(llm, req, ctx)`
  委托给 `_wrap`。
  - 为什么要分两层：异步生成器函数（`async def` + `yield`）不能直接用普通函数签名返回生成器对象的方式被调用——`wrap_text_stream` 是普通函数返回异步迭代器，`_wrap` 才是真正的 `async def` 生成器。这样调用方 `wrap_text_stream(...)` 立即拿到迭代器，而 `async for` 时才执行 `_wrap`。

### 第 34-45 行：_wrap（包装实现）

- **第 34-36 行** 签名 `async def _wrap(...) -> AsyncIterator[StreamEvent]:`
- **第 37 行** `"""包装实现：逐块转发 TEXT_DELTA，流末按 last_usage 补 USAGE/DONE。"""`
- **第 38 行** `# 规则 6 例外：包装具名固定能力的旧后端（stream/last_usage），非动态键读取。`

- **第 39 行** `stream = getattr(llm, "stream")`
  取后端的 `stream` 方法（这是 LLMPort 必有的能力，直接取，无缺省）。

- **第 40 行** `last_usage = getattr(llm, "last_usage", None)`
  取 `last_usage`（可选，没有就 None）。**注意**：这里在迭代前就取了一次引用。
  - 潜在细节：若后端在流过程中才写入 `last_usage`，此时取到的是旧值（可能为 None）。但按契约后端应在流末写入——更稳妥的写法应在流结束后再取。这里记录代码实际行为：提前取引用。

- **第 41 行** `async for chunk in stream(req, ctx):  # 逐块转发不缓存整段，保持流式低延迟`
  逐块迭代后端文本流。**不缓存整段**——保持低延迟，块来了立刻转发出去。

- **第 42 行** `yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk)`
  每块包成 TEXT_DELTA 事件吐出。

- **第 43 行** `if last_usage is not None:  # 仅有 usage 才发 USAGE；缺失也不伪造，结算责任在网关`
  有 usage 才发 USAGE 事件。**缺失不伪造**——不编一个零成本 usage。

- **第 44 行** `yield StreamEvent(kind=StreamEventKind.USAGE, data=last_usage)`
  吐 USAGE 事件。

- **第 45 行** `yield StreamEvent(kind=StreamEventKind.DONE)`
  最后吐 DONE 事件，标记流结束。

**stream 小结**：`has_stream_events` 用 `callable(getattr(...))` 安全探测；`wrap_text_stream` 是普通函数返回 `_wrap` 异步生成器；`_wrap` 逐块转发 TEXT_DELTA、流末补 USAGE（有才发、不伪造）和 DONE。整个设计不动 `LLMPort` 协议。

---

## 6. structured.py 逐行讲解（253 行）

结构化输出：最小 JSON Schema 校验（零外部依赖）+ 修复重试提示。红线：strict 模式下未过 schema 的输出绝不标记为合法。

### 第 1-23 行：docstring、imports、导出

- **第 1 行** `"""R4 结构化输出：最小 JSON Schema 校验 + 修复重试提示。`
  一句话定位。

- **第 2-5 行** docstring 正文
  - 第 3 行：不引入 jsonschema 外部依赖，支持常见关键字（type/required/properties/enum/minimum）。
  - 第 4 行：复杂 schema 可注入自定义 validator（R13 可插拔）。
  - 第 5 行：**红线**——strict 模式下未过 schema 的输出绝不标记为合法（不进工具调用/业务逻辑）。

- **第 6 行** `"""` 结束。

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `import json` 解析模型返回的 JSON 文本。

- **第 10 行** `from collections.abc import Callable` `_type_ok` 里 checkers 表的值类型。

- **第 11 行** `from enum import Enum` 两个有限取值枚举。

- **第 12 行** `from typing import Protocol, runtime_checkable` 校验器协议。

- **第 14 行** `from runtime import DynamicValue` 动态 schema/值的具名别名。

- **第 16 行** `from .types import StructuredOutputSpec` 规格 DTO。

- **第 18-23 行** `__all__`
  导出四个：`MinimalSchemaValidator`、`SchemaValidator`、`StructuredOutputValidator`、`validate_json_schema`。

### 第 26-48 行：_JsonType（JSON Schema type 枚举）

- **第 26 行** `class _JsonType(str, Enum):`
  双基类：有限取值 + 可当字符串用。私有枚举，不进公共契约。

- **第 27-30 行** docstring
  说明：成员对应 JSON Schema 标准类型；`_type_ok` 用查表分派，未知值显式回退「不校验」。

- **第 32-38 行** 七个成员
  `OBJECT="object"` / `ARRAY="array"` / `STRING="string"` / `NUMBER="number"` / `INTEGER="integer"` / `BOOLEAN="boolean"` / `NULL="null"`。
  对应 JSON Schema 的七种标准类型。

- **第 40-48 行** `coerce` 类方法
- **第 40 行** `@classmethod`
- **第 41 行** `def coerce(cls, raw: object) -> _JsonType | None:`
- **第 42 行** `"""字符串归一为成员；未知/缺省值回退 None（不校验，规则 5 显式回退）。"""`
- **第 43 行** `if not isinstance(raw, str):`
  非字符串（含 None）直接回退。
- **第 44 行** `return None`
- **第 45 行** `try:`
- **第 46 行** `return cls(raw)`
  字符串尝试转枚举成员。
- **第 47 行** `except ValueError:`
  非法取值（如 `"str"` 而非 `"string"`）。
- **第 48 行** `return None`
  回退 None。配合 `_check` 第 133 行：schema 声明了 type 但 coerce 返回 None → 报错（N4）。

### 第 51-67 行：_OutputMode（输出模式枚举）

- **第 51 行** `class _OutputMode(str, Enum):`
  与 pydantic `Literal` 契约对齐的内部枚举。

- **第 52-55 行** docstring
  说明三成员：CONSTRAINED 约束解码 / PROMPT 仅提示 / REPAIR 修复重试。

- **第 57-59 行** 三成员
  `CONSTRAINED="constrained"` / `PROMPT="prompt"` / `REPAIR="repair"`。

- **第 61-67 行** `coerce`
- **第 61 行** `@classmethod`
- **第 62 行** `def coerce(cls, raw: str) -> _OutputMode:`
- **第 63 行** `"""字符串归一为成员；未知值显式回退 REPAIR（保守校验，规则 5）。"""`
- **第 64 行** `try:`
- **第 65 行** `return cls(raw)`
- **第 66 行** `except ValueError:`
- **第 67 行** `return cls.REPAIR`
  **未知值回退 REPAIR**（最保守：会做校验和修复）。
  - 与 `_JsonType.coerce` 回退 None 不同：这里回退到具体成员，因为模式必须有个可执行的值。选 REPAIR 是因为它最安全（其他模式如 prompt 只提示不校验）。

### 第 70-85 行：SchemaValidator 协议

- **第 70 行** `@runtime_checkable`
  运行时可检查（装配期履约校验用）。

- **第 71 行** `class SchemaValidator(Protocol):`
  校验器协议，可插拔（四要素①）。

- **第 72-79 行** docstring
  说明成员：`name`（具名标识，装配期校验非空、进 trace）、`validate`（收原始值与 schema，返回错误列表）。
  第 78 行：复杂 schema（anyOf/pattern/items）由注入方基于 jsonschema 实现，框架不内置大依赖。

- **第 81 行** `name: str`
  具名标识。

- **第 83 行** `def validate(self, value: object, schema: dict[str, DynamicValue]) -> list[str]:`
  方法签名：值 + schema → 错误列表。

- **第 84 行** `"""返回错误描述列表；空列表表示合法。"""`
- **第 85 行** `...` 协议方法体。

### 第 88-99 行：MinimalSchemaValidator（默认校验器）

- **第 88 行** `class MinimalSchemaValidator:`
  默认校验器（四要素②）：委托模块级 `validate_json_schema`。

- **第 89-93 行** docstring
  说明：零外部依赖，覆盖最小场景；需要更强 schema 时注入自定义实现替换。

- **第 95 行** `name: str = "minimal-json-schema"`
  具名标识。

- **第 97 行** `def validate(self, value: object, schema: dict[str, DynamicValue]) -> list[str]:`
- **第 98 行** `"""调用最小 JSON Schema 校验，返回错误列表。"""`
- **第 99 行** `return validate_json_schema(value, schema)`
  薄委托：不做额外逻辑，方便替换。
  - 为什么做成类而非直接调函数：可插拔协议需要一个带 `name` 的对象，类满足协议，函数不行。

### 第 102-120 行：validate_json_schema（模块级校验入口）

- **第 102 行** `def validate_json_schema(value: object, schema: dict[str, DynamicValue]) -> list[str]:`
- **第 103-111 行** docstring
  说明：`value` 若是 str 先试 `json.loads`（模型常返回 JSON 文本）；schema 支持 type/required/properties/enum/minimum；返回错误列表，空表示通过。

- **第 112 行** `errors: list[str] = []`
  错误累计列表。

- **第 113 行** `data = value`
  待校验数据先等于原值。

- **第 114 行** `if isinstance(value, str):`
  模型输出常是 JSON 文本字符串。

- **第 115 行** `try:`
- **第 116 行** `data = json.loads(value)`
  尝试解析成 Python 对象。

- **第 117 行** `except (ValueError, TypeError):`
  解析失败（非法 JSON）。`JSONDecodeError` 是 ValueError 子类，被覆盖。

- **第 118 行** `return [f"invalid JSON text: {value[:80]!r}"]`
  立即返回一条错误。`value[:80]` 截取前 80 字符，避免错误信息过长；`!r` 带引号显示便于诊断。
  - 不这么写（让异常抛出）：模型输出非法 JSON 是常见情况，应作为「校验不通过」返回错误列表，而不是崩溃。

- **第 119 行** `_check(data, schema, "$", errors)`
  递归校验入口，根路径用 `"$"`（JSONPath 风格）。

- **第 120 行** `return errors`
  返回全部错误。

### 第 123-159 行：_check（递归校验核心）

- **第 123 行** `def _check(data: DynamicValue, schema: dict[str, DynamicValue], path: str, errors: list[str]) -> None:`
  递归函数：往 `errors` 原地追加错误，无返回值。

- **第 124-130 行** docstring
  说明参数：data（待校验节点）、schema（节点对应 schema 片段）、path（错误路径如 `$.name`）、errors（累计列表）。

- **第 131 行** `raw_type = schema.get("type")`
  取 type 声明（可能是字符串或非法值）。

- **第 132 行** `expected = _JsonType.coerce(raw_type)`
  归一成枚举或 None。

- **第 133 行** `if "type" in schema and expected is None:`
  **N4 关键点**：schema 里**声明了** type 键，但 coerce 失败（非法值如 `"str"`）。
  - 注意条件不能只写 `expected is None`：因为 type 缺席时 expected 也是 None，那是合法的「不校验」。必须用 `"type" in schema` 区分「声明了但非法」和「没声明」。

- **第 134-135 行** 注释
  `# N4：schema 声明了 type 但值非法（如 "str"）→ 报校验错误，不静默跳过。静默跳过会让坏 schema 下的非法输出被当成合法，与 strict 红线冲突。`
  这正是要防的：坏 schema + 静默跳过 = 非法输出被当合法。

- **第 136 行** `errors.append(f"{path}: invalid schema type {raw_type!r}")`
  报「schema 类型非法」错误。

- **第 137 行** `return`
  直接返回，不再校验该节点其余约束。

- **第 138 行** `if expected is not None and not _type_ok(data, expected):`
  type 合法且数据不符合该类型。

- **第 139 行** `errors.append(f"{path}: expected {expected.value}, got {type(data).__name__}")`
  报类型不匹配，带期望与实际类型名。

- **第 140 行** `return  # 类型错则不继续深挖，避免噪音`
  类型错就停，不再检查 enum/required 等——类型都不对，后续检查没意义且会产生一堆噪音错误。

- **第 142 行** `raw_enum = schema.get("enum")`
  取 enum 声明。

- **第 143 行** `if isinstance(raw_enum, list) and data not in raw_enum:  # 仅 enum 为合法列表时约束取值；坏 schema 形态不误伤合法值`
  两个守卫：enum 必须是 list（合法形态）；数据不在列表里才报错。
  - 形态不合法（如 enum 是字符串）就跳过——坏 schema 不该误伤本可合法的数据。

- **第 144 行** `errors.append(f"{path}: value {data!r} not in enum {raw_enum}")`
  报 enum 不匹配。

- **第 146 行** `if expected is _JsonType.OBJECT and isinstance(data, dict):  # 仅对象类型且数据确为 dict 才深挖字段，避免对标量误报`
  双重守卫：schema type 是 object **且** 数据确实是 dict，才检查 required/properties。
  - 若数据不是 dict（类型检查第 138 行应该已经 return 了，这里是防御），跳过避免误报。

- **第 147 行** `for req in schema.get("required", []):  # 逐个必需字段查存在性；不提前 break，一次报全缺失项便于修复`
  遍历 required 列表，**一次报全所有缺失字段**（不 break）——修复提示能一次列出所有问题，减少往返。
  - `schema.get("required", [])`：缺 required 键当空列表。

- **第 148 行** `if req not in data:  # 缺字段即记错；required 列表形态由 schema 保证，异常形态当作空约束`
  字段不存在。

- **第 149 行** `errors.append(f"{path}.{req}: required field missing")`
  报缺失，路径带字段名。

- **第 150 行** `raw_props = schema.get("properties", {})`
  取 properties。

- **第 151 行** `if isinstance(raw_props, dict):  # properties 形态合法才递归，防止把非 dict 值当成子 schema`
  形态守卫。

- **第 152 行** `for prop, sub_schema in raw_props.items():  # 逐字段下钻；只校验数据中实际存在的键，缺席交由 required 负责`
  遍历属性。只校验数据里存在的键——缺席的键由 required 报错负责，不重复。

- **第 153 行** `if prop in data and isinstance(sub_schema, dict):  # 双重守卫：字段缺席或子 schema 畸形都跳过，兼容宽松 schema`
  双重守卫：字段存在 + 子 schema 是 dict。

- **第 154 行** `_check(data[prop], sub_schema, f"{path}.{prop}", errors)`
  递归下钻，路径追加 `.字段名`。

- **第 156 行** `if expected in (_JsonType.NUMBER, _JsonType.INTEGER):  # 仅数值类型才谈最小值，字符串比大小无意义`
  只有 number/integer 类型才检查 minimum。

- **第 157 行** `minimum = schema.get("minimum")`
  取最小值。

- **第 158 行** `if isinstance(minimum, (int, float)) and isinstance(data, (int, float)) and data < minimum:  # 边界/数据都须是数值；形态不合法视为无约束（显式回退）`
  三重守卫：minimum 是数值、data 是数值、data 小于 minimum。
  - 形态不合法就当无约束，显式回退。
  - 注意没排除 bool（`isinstance(True, int)` 为 True），但此处是数值比较场景，影响较小。

- **第 159 行** `errors.append(f"{path}: {data} < minimum {minimum}")`
  报小于最小值。

### 第 162-173 行：_type_ok（类型判定查表）

- **第 162 行** `def _type_ok(data: DynamicValue, expected: _JsonType) -> bool:`
- **第 163 行** `"""判断数据是否符合 JSON Schema 的 type 声明（枚举分派，规则 5）。"""`
- **第 164 行** `checkers: dict[_JsonType, Callable[[DynamicValue], bool]] = {`
  查表：枚举 → 判定函数。比一长串 `if/elif` 清晰，且易扩展。

- **第 165 行** `_JsonType.OBJECT: lambda v: isinstance(v, dict),`
- **第 166 行** `_JsonType.ARRAY: lambda v: isinstance(v, list),`
- **第 167 行** `_JsonType.STRING: lambda v: isinstance(v, str),`
- **第 168 行** `_JsonType.NUMBER: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),`
  **排除 bool**：bool 是 int 子类，`number` 不该接受 true/false。
- **第 169 行** `_JsonType.INTEGER: lambda v: isinstance(v, int) and not isinstance(v, bool),`
  同样排除 bool。
- **第 170 行** `_JsonType.BOOLEAN: lambda v: isinstance(v, bool),`
- **第 171 行** `_JsonType.NULL: lambda v: v is None,`
- **第 172 行** `}`
- **第 173 行** `return checkers[expected](data)`
  查表调用。`expected` 已由 coerce 保证是合法枚举成员，直接索引不会 KeyError。

### 第 176-253 行：StructuredOutputValidator（校验器 + 修复提示）

- **第 176 行** `class StructuredOutputValidator:`
  结构化输出校验器：validate() 判定合法性，build_repair_prompt() 生成修复提示。

- **第 177-181 行** docstring
  说明属性 `should_repair`（repair 模式为真）。

- **第 183-185 行** `__init__` 签名
- **第 183 行** `def __init__(`
- **第 184 行** `self, spec: StructuredOutputSpec, *, validator: SchemaValidator | None = None`
- **第 185 行** `) -> None:`

- **第 186-194 行** docstring
  说明装配规格 + 校验器（四要素③替换入口），异常：TypeError（不满足协议）、ValueError（name 为空）。

- **第 195 行** `self._spec = spec`
  保存规格。

- **第 196 行** `self._mode = _OutputMode.coerce(spec.mode)`
  模式归一成枚举（未知值回退 REPAIR）。

- **第 197 行** `chosen = validator if validator is not None else MinimalSchemaValidator()`
  注入校验器，没注入用默认（四要素②）。

- **第 198 行** `# 履约校验（四要素④）：协议成员齐备 + 具名非空，不满足显式拒绝。`
  注释点明可插拔四要素之四：履约校验。

- **第 199 行** `if not isinstance(chosen, SchemaValidator):`
  用 `runtime_checkable` 协议做 `isinstance` 检查（检查成员齐备，不检查签名）。

- **第 200 行** `raise TypeError("validator must implement SchemaValidator protocol")`
  不满足协议 → 显式拒绝。

- **第 201 行** `if not chosen.name:`
  名为空。

- **第 202 行** `raise ValueError("validator.name must be non-empty")`
  可插拔物必须具名，空名拒绝。

- **第 203 行** `self._validator = chosen`
  保存校验器。

- **第 205-208 行** `schema_validator` 属性
- **第 205 行** `@property`
- **第 206 行** `def schema_validator(self) -> SchemaValidator:`
- **第 207 行** `"""当前生效的校验器（默认或注入），供外部读取。"""`
- **第 208 行** `return self._validator`

- **第 210-213 行** `should_repair` 属性
- **第 210 行** `@property`
- **第 211 行** `def should_repair(self) -> bool:`
- **第 212 行** `"""是否允许自然语言修复重试（枚举比较，非裸字符串）。"""`
- **第 213 行** `return self._mode is _OutputMode.REPAIR`
  用 `is` 比较枚举单例（不是 `==` 字符串）。

- **第 215-218 行** `max_repair` 属性
- **第 215 行** `@property`
- **第 216 行** `def max_repair(self) -> int:`
- **第 217 行** `"""最大修复次数。"""`
- **第 218 行** `return self._spec.max_repair`

- **第 220-238 行** `validate(text)`
- **第 220 行** `def validate(self, text: str) -> tuple[dict[str, DynamicValue] | list[DynamicValue] | str | int | None, bool]:`
  返回 `(解析值, 是否合法)` 元组。解析值形态由 JSON 决定，用 DynamicValue 联合类型。
- **第 221-228 行** docstring
  说明参数 text、返回元组；strict 模式下非法输出 ok 必为 False。
- **第 229 行** `# 走注入校验器（默认 MinimalSchemaValidator），判定口径可替换。`
- **第 230 行** `errors = self._validator.validate(text, self._spec.json_schema)`
  调校验器（默认或注入），保证判定口径可替换。
- **第 231 行** `if errors:`
- **第 232 行** `return None, False`
  有错误 → 返回 (None, False)。
- **第 233 行** `try:`
- **第 234 行** `parsed = json.loads(text)`
  无错误再解析 JSON。
- **第 235 行** `except (ValueError, TypeError):`
  解析失败。
- **第 236 行** 注释 `# schema 无 type 约束且文本非 JSON：按纯文本成功（合法但无法结构化解析）。`
- **第 237 行** `return text, True`
  按纯文本成功返回。
  - 为什么算合法：schema 没约束 type，纯文本也满足；只是无法结构化解析而已。
- **第 238 行** `return parsed, True`
  解析成功返回对象。

- **第 240-242 行** `check(text)`
- **第 240 行** `def check(self, text: str) -> list[str]:`
- **第 241 行** `"""用当前校验器校验输出文本，返回错误列表（供网关修复循环共用同一口径）。"""`
- **第 242 行** `return self._validator.validate(text, self._spec.json_schema)`
  与 `validate` 用同一个校验器——保证「校验」和「修复循环判定」口径一致。

- **第 244-253 行** `build_repair_prompt(bad_output, errors)`
- **第 244 行** `def build_repair_prompt(self, bad_output: str, errors: list[str]) -> str:`
- **第 245 行** `"""生成修复提示：附上原输出与校验错误，让模型纠偏。"""`
- **第 246-252 行** `lines = [...]`
  构造提示行列表：
  - 第 247 行 `"Your previous output failed schema validation."`：说明上次输出没过校验。
  - 第 248 行 `f"Original output: {bad_output}"`：附上原输出。
  - 第 249 行 `"Validation errors:"`：错误标题。
  - 第 250 行 `*[f"  - {e}" for e in errors]`：逐条列出错误（用 `*` 展开列表进外层列表）。
  - 第 251 行 `"Return a corrected JSON object that satisfies the schema."`：要求返回修正后的 JSON。
- **第 253 行** `return "\n".join(lines)`
  用换行连接成完整提示。

**structured 小结**：`_JsonType`/`_OutputMode` 两枚举归一有限取值；`SchemaValidator` 协议 + `MinimalSchemaValidator` 默认（可插拔四要素）；`validate_json_schema`/`_check`/`_type_ok` 做零依赖最小校验（N4：坏 schema 的非法 type 必须报错；bool 排除）；`StructuredOutputValidator` 统一校验与修复提示口径，履约校验保证可插拔物具名且齐备。

---

## 7. cache.py 逐行讲解（461 行）

三层可插拔缓存，每层「协议 + 默认实现」。这是全模块设计最讲究的文件之一——尤其是 `HashCacheEmbedder` 的抗收敛设计和 `SimilarityCache` 的「遍历后判阈值」。

### 第 1-44 行：docstring、imports、常量、导出

- **第 1 行** `"""R4 缓存：异步协议 + 可插拔存储工具 + 可插拔 embedding。`
  一句话定位。

- **第 3-13 行** docstring 的三层说明
  - 层 1 `CachePort`：缓存策略协议（异步 get/put/invalidate，收 LLMRequest 原文）。默认实现 ExactCache（精确）/ SimilarityCache（语义）。
  - 层 2 `CacheStore`：存储工具协议（get/put/delete/scan，存 JSON 字符串）。默认 InMemoryCacheStore；用户可实现 Redis/DB。
  - 层 3 `CacheEmbedder`：embedding 模型协议。默认 HashCacheEmbedder（bigram 哈希向量）；显式 None 降级 difflib；可注入 OpenAI/本地 embedding。

- **第 15 行** `"""` 结束。

- **第 16 行** `from __future__ import annotations` 延迟注解。

- **第 18 行** `import json` 条目序列化/反序列化。

- **第 19 行** `import math` sqrt 归一化、余弦。

- **第 20 行** `from difflib import SequenceMatcher` 文本相似度降级路径（零依赖退路）。

- **第 21 行** `from typing import Protocol, TypedDict, cast, runtime_checkable`
  三个协议 + 条目 TypedDict + 边界 cast。

- **第 23 行** `from compose import LLMRequest, LLMResponse`
  缓存进出的是请求与响应 DTO。

- **第 25 行** `from .types import build_cache_key`
  精确键构造复用 types 的实现。

- **第 27-37 行** `__all__`
  导出：三个协议（CacheEmbedder/CachePort/CacheStore）、两个常量（DEFAULT_CACHE_EMBED_DIM/MAX_EMBED_CHARS）、三个实现（ExactCache/HashCacheEmbedder/InMemoryCacheStore/SimilarityCache）。

- **第 39-41 行** 注释（维度选择论证）
  - 第 39 行：定长向量才能被 `_cosine` 跨条目比较。
  - 第 40-41 行：**512 维而非 64 维的原因**——64 桶在长文本上哈希碰撞导致向量收敛为常量、不相关文本误命中（独立 review Blocker-1 实测）；512 维 + 二值化 + 窗口截断后误命中归零。

- **第 42 行** `DEFAULT_CACHE_EMBED_DIM: int = 512`
  默认维度常量（具名常量，禁魔法值）。

- **第 43-44 行** 注释 + `MAX_EMBED_CHARS: int = 512`
  输入窗口上限：超长文本取头尾各一半，从源头限制 bigram 数，防相似度随长度收敛。

### 第 47-71 行：层 1 CachePort（策略协议）

- **第 47-49 行** 分节注释 `# 层 1：缓存策略协议`

- **第 50 行** `@runtime_checkable`
  运行时可检查。

- **第 51 行** `class CachePort(Protocol):`
  缓存策略协议（异步）。

- **第 52-57 行** docstring
  - **异步理由**：gateway 的 complete/stream 均 async，Redis(asyncio)/DB(asyncpg) 客户端也是 async；同步协议会逼远程存储实现者阻塞事件循环。
  - key 策略归实现者：ExactCache 算精确哈希，SimilarityCache 用相似度，用户可实现任意策略。

- **第 59 行** `name: str`
  具名标识。

- **第 61-63 行** `get`
  - **第 61 行** `async def get(self, request: LLMRequest, *, version: str = "current") -> LLMResponse | None:`
  - **第 62 行** `"""命中返回响应，未命中 None。"""`
  - **第 63 行** `...`

- **第 65-67 行** `put`
  - **第 65 行** `async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "current") -> None:`
  - **第 66 行** `"""写入响应。"""`
  - **第 67 行** `...`

- **第 69-71 行** `invalidate`
  - **第 69 行** `async def invalidate(self, request: LLMRequest, *, version: str = "current") -> None:`
  - **第 70 行** `"""失效该请求对应的缓存条目。"""`
  - **第 71 行** `...`

### 第 75-102 行：层 2 CacheStore（存储工具协议）

- **第 75-77 行** 分节注释 `# 层 2：存储工具协议（用户可换 Redis/DB）`

- **第 78 行** `@runtime_checkable`
- **第 79 行** `class CacheStore(Protocol):`
- **第 80-84 行** docstring
  - **为何存 str 而非 LLMResponse**：外部存储（Redis/DB）只认字节/字符串；JSON 序列化在缓存层完成，存储工具保持简单通用。
- **第 86 行** `name: str`
- **第 88-90 行** `get(key) -> str | None`
- **第 92-94 行** `put(key, value, *, ttl=None)`
- **第 96-98 行** `delete(key)`
- **第 100-102 行** `scan(prefix) -> list[tuple[str, str]]`
  按前缀遍历返回 (key, value) 列表，供语义缓存匹配候选集。

### 第 105-147 行：InMemoryCacheStore（默认存储工具）

- **第 105 行** `class InMemoryCacheStore:`
  默认存储工具：进程内字典 + 可选 TTL。

- **第 106 行** `"""默认存储工具：进程内字典 + 可选 TTL（墙钟由 time.time 提供，仅测试可注入 clock）。"""`
  说明墙钟用 time.time。

- **第 108 行** `name = "in_memory_cache_store"`
  具名标识。

- **第 110 行** `def __init__(self) -> None:`
- **第 111 行** `"""初始化空存储。"""`
- **第 112 行** `self._data: dict[str, str] = {}` 数据字典。
- **第 113 行** `self._expiry: dict[str, float] = {}` 过期时刻字典（key → Unix 时间戳）。

- **第 115-123 行** `get`
- **第 115 行** `async def get(self, key: str) -> str | None:`
- **第 116 行** `"""读 key；TTL 过期则删除并返回 None。"""`
- **第 117 行** `import time`
  局部导入。
- **第 118 行** `exp = self._expiry.get(key)`
  取过期时刻。
- **第 119 行** `if exp is not None and time.time() >= exp:`
  有过期时刻且已到/超过。
- **第 120 行** `self._data.pop(key, None)`
  删数据（懒删除：读时才清理，不做后台定时器）。
- **第 121 行** `self._expiry.pop(key, None)`
  删过期记录。
- **第 122 行** `return None`
  当未命中返回。
- **第 123 行** `return self._data.get(key)`
  未过期返回值，不存在返回 None。

- **第 125-132 行** `put`
- **第 125 行** `async def put(self, key: str, value: str, *, ttl: int | None = None) -> None:`
- **第 126 行** `"""写 key；ttl 给定时设置过期时刻。"""`
- **第 127 行** `import time`
- **第 128 行** `self._data[key] = value`
  写入数据。
- **第 129 行** `if ttl is None:`
  无 TTL。
- **第 130 行** `self._expiry.pop(key, None)`
  清除该 key 的过期记录（防止之前设过 TTL 残留）。
- **第 131 行** `else:`
- **第 132 行** `self._expiry[key] = time.time() + ttl`
  设置过期时刻 = 现在 + ttl 秒。

- **第 134-137 行** `delete`
- **第 134 行** `async def delete(self, key: str) -> None:`
- **第 135 行** `"""删除 key 及其 TTL。"""`
- **第 136 行** `self._data.pop(key, None)`
- **第 137 行** `self._expiry.pop(key, None)`
  数据与过期记录一起删，防残留。

- **第 139-147 行** `scan`
- **第 139 行** `async def scan(self, prefix: str) -> list[tuple[str, str]]:`
- **第 140 行** `"""返回所有 prefix 开头的 (key, value)（跳过已过期项）。"""`
- **第 141 行** `import time`
- **第 142 行** `now = time.time()`
  固定一个「现在」，避免循环中多次取时间不一致。
- **第 143-147 行** 列表推导
  - 第 144 行 `(k, v) for k, v in self._data.items()`
  - 第 145 行 `if k.startswith(prefix)`
    只要指定前缀的键。
  - 第 146 行 `and not (k in self._expiry and now >= self._expiry[k])`
    跳过已过期项（注意这里只跳过、不删除，删除留给 get 的懒清理）。

### 第 150-161 行：层 3 CacheEmbedder（向量模型协议）

- **第 150-152 行** 分节注释 `# 层 3：embedding 协议（用户可换向量模型）`
- **第 153 行** `@runtime_checkable`
- **第 154 行** `class CacheEmbedder(Protocol):`
- **第 155 行** `"""Embedding 模型协议：把文本映射为向量（供语义缓存算余弦相似度）。"""`
- **第 157 行** `name: str`
- **第 159-161 行** `embed`
  - **第 159 行** `async def embed(self, text: str) -> tuple[float, ...]:`
  - **第 160 行** `"""文本 → 向量（维度由实现决定，同一实现内须一致）。"""`
    关键约束：同一实现内维度必须一致，否则向量间无法比较。
  - **第 161 行** `...`

### 第 164-166 行：bigram 边界哨兵

- **第 164 行** `# bigram 边界哨兵：让首/尾字符也参与成组，保留位置信息（具名常量）。`
- **第 165 行** `_BIGRAM_START = "^"`
  文本开头插入的哨兵。
- **第 166 行** `_BIGRAM_END = "$"`
  文本结尾插入的哨兵。
  有了哨兵，单字符文本也能成 bigram（`^x`、`x$`），首尾字符都参与，保留位置信息。

### 第 169-239 行：HashCacheEmbedder（默认 embedding，设计最讲究）

- **第 169 行** `class HashCacheEmbedder:`
  默认 embedding：字符 bigram 哈希到定长向量，零依赖、确定性、中文友好。

- **第 170-179 行** 类 docstring（设计论证，重点）
  - 第 172-174 行：不依赖分词库（中文无空格也能成 bigram）、不调模型/网络/文件；用**确定性多项式字符哈希（不用进程随机化的内置 `hash()`）**，保证跨进程同文本同向量；L2 归一化后交给 `_cosine`。
  - 第 176-178 行：**抗收敛设计（Blocker-1）**——桶二值化（出现置 1.0 不累加频次）+ 512 维 + 512 字符头尾窗口。若用计数累加，长文本各桶计数趋同为 n/64，向量收敛为常量导致不相关文本余弦趋近 1.0；二值化+窗口实测长文本误命中归零。

- **第 181 行** `name: str = "hash_cache_embedder"`
  具名标识。

- **第 183-194 行** `__init__`
- **第 183 行** `def __init__(self, dim: int = DEFAULT_CACHE_EMBED_DIM) -> None:`
- **第 184-191 行** docstring（Args: dim 必须正整数；Raises: ValueError）。
- **第 192 行** `if dim <= 0:`
  维度必须正。
- **第 193 行** `raise ValueError("dim must be a positive integer")`
  非正显式拒绝（0 维向量无法比较）。
- **第 194 行** `self.dim = dim`

- **第 196-203 行** `_bigrams`（切 bigram）
- **第 196 行** `@staticmethod`
- **第 197 行** `def _bigrams(text: str) -> list[str]:`
- **第 198-201 行** docstring
  说明：带边界的字符 bigram；空文本回退为单个哨兵组（`^$`）；首字符 `^x`、尾字符 `x$` 各成一组。
- **第 202 行** `padded = _BIGRAM_START + text.lower() + _BIGRAM_END`
  前后加哨兵，`lower()` 归一大小写（大小写不敏感匹配）。
- **第 203 行** `return [padded[i : i + 2] for i in range(len(padded) - 1)]`
  滑动窗口取所有相邻两字符组。长度 n+2 的文本产出 n+1 个 bigram。

- **第 205-210 行** `_hash_bucket`（确定性哈希）
- **第 205 行** `def _hash_bucket(self, gram: str) -> int:`
- **第 206 行** `"""确定性字符哈希 → [0, dim) 桶号（多项式 31 进制，非进程随机 hash）。"""`
- **第 207 行** `code = 0`
  初始哈希值。
- **第 208 行** `for char in gram:`
  遍历 bigram 的两个字符。
- **第 209 行** `code = (code * 31 + ord(char)) % self.dim`
  **多项式 31 进制哈希 + 取模**：
  - `code * 31 + ord(char)`：经典多项式滚动哈希，31 是常用质数基数。
  - `% self.dim`：限制在 [0, dim) 桶范围。
  - **为什么不用内置 `hash()`**：Python 进程的 `hash()` 对字符串做了随机化（PYTHONHASHSEED），同一文本在不同进程哈希值不同 → 缓存条目跨进程写入后读不到，或误命中。自己实现保证确定性。
  - 不这么写（用随机 hash）：缓存无法跨进程/重启共享，语义缓存形同虚设。

- **第 212-239 行** `embed`（生成向量）
- **第 212 行** `async def embed(self, text: str) -> tuple[float, ...]:`
- **第 213-220 行** docstring
  说明：L2 归一化定长向量（二值化 + 头尾窗口）；空文本返回零向量；返回长度 dim、分量为 0 或 1/sqrt(命中桶数)。
- **第 221-222 行** 注释
  `# 空文本显式返回零向量：_bigrams("") 会产出 "^$" 单位向量，两个空 query 余弦=1.0 会误命中，故在源头短路（review Should-2）。`
- **第 223 行** `if not text:`
  空文本守卫。
- **第 224 行** `return (0.0,) * self.dim`
  返回全零向量。`_cosine` 对零向量返回 0.0（保守不误命中）。
  - 不这么写（返回 `^$` 的单位向量）：两个空 query 余弦=1.0，会错误命中彼此的缓存。
- **第 225-226 行** 注释
  `# 超长取头尾各半：保留首尾位置信息，同时把 bigram 数限制在窗口内，防计数收敛导致不相关长文本高相似度（Blocker-1）。`
- **第 227 行** `source = text`
- **第 228 行** `if len(source) > MAX_EMBED_CHARS:`
  超过 512 字符。
- **第 229 行** `half = MAX_EMBED_CHARS // 2`
  一半 = 256。
- **第 230 行** `source = source[:half] + source[-half:]`
  取头 256 + 尾 256，拼成 512 字符窗口。中间丢弃。
- **第 231 行** `buckets: list[float] = [0.0] * self.dim`
  初始化全零桶数组。
- **第 232 行** `for gram in self._bigrams(source):`
  遍历 bigram。
- **第 233 行** 注释 `# 二值化：桶出现过置 1.0，不按频次累加——避免长文本各桶计数趋同。`
- **第 234 行** `buckets[self._hash_bucket(gram)] = 1.0`
  **二值化**：置 1.0 而非 `+= 1`。
  - 不这么写（累加频次）：长文本 bigram 多，各桶计数趋同为 n/dim，向量归一化后各分量趋同 → 任意两段长文本余弦趋近 1.0 → 大量误命中。
- **第 235 行** `norm = math.sqrt(sum(v * v for v in buckets))`
  计算 L2 范数。
- **第 236 行** 注释 `# 理论上非空文本至少有 1 个 bigram，norm>0；保留零向量防御避免除零。`
- **第 237 行** `if norm == 0.0:`
  防御性检查。
- **第 238 行** `return tuple(buckets)`
  零范数直接返回（避免第 239 行除零）。
- **第 239 行** `return tuple(v / norm for v in buckets)`
  L2 归一化，各分量变成 0 或 `1/sqrt(k)`（k 是命中桶数）。归一化后可直接点积算余弦。

### 第 242-250 行：_Unset 哨兵

- **第 242 行** `class _Unset:`
  哨兵类型：区分「embedder 未传」（用默认）与「显式 None」（降级 difflib）。

- **第 243 行** `"""哨兵类型：区分"embedder 未传"（用默认）与"显式 None"（降级 difflib）。"""`
  **为什么需要**：`embedder` 参数默认若是 `None`，就无法区分「调用方没传该参数（想要默认 embedder）」和「调用方明确传 None（想要 difflib 降级）」。用一个唯一哨兵对象解决。

- **第 245 行** `def __repr__(self) -> str:`
- **第 246 行** `"""返回稳定标识，便于诊断。"""`
- **第 247 行** `return "<UNSET>"`
  repr 稳定，便于调试。

- **第 250 行** `_UNSET = _Unset()`
  唯一哨兵实例。所有「未传」都指向它，`isinstance(x, _Unset)` 或 `is x` 判断。

### 第 253-260 行：_cosine（余弦相似度）

- **第 253 行** `def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:`
- **第 254 行** `"""余弦相似度；维度不符或零向量返回 0.0（保守，不误命中）。"""`
- **第 255 行** `if len(a) != len(b) or not a:`
  两个守卫：维度不一致、或空向量。
- **第 256 行** `return 0.0`
  保守返回 0（不匹配）。
  - 维度不符返回 0 而非抛错：不同 embedder 的向量混进同一存储时，不崩溃、不误命中。
- **第 257 行** `dot = sum(x * y for x, y in zip(a, b))`
  点积。
- **第 258 行** `na = math.sqrt(sum(x * x for x in a))`
  向量 a 的范数。
- **第 259 行** `nb = math.sqrt(sum(y * y for y in b))`
  向量 b 的范数。
- **第 260 行** `return dot / (na * nb) if na and nb else 0.0`
  余弦 = 点积/(范数积)；任一范数为 0（零向量）返回 0。

### 第 263-266 行：共享助手常量

- **第 263-265 行** 分节注释 `# 共享助手`
- **第 266 行** `_ROLE_KEY, _CONTENT_KEY, _USER_ROLE = "role", "content", "user"`
  三个具名常量，替代散落的裸字符串键（`role`/`content`/`user`）。规则 5。

### 第 269-278 行：_SemanticCacheEntry（语义缓存条目 TypedDict）

- **第 269 行** `class _SemanticCacheEntry(TypedDict):`
  条目持久化结构（规则 4：字段显式声明）。注意没有 `total=False`，三个字段都必须有。

- **第 270-274 行** docstring
  说明：`text`（归一化查询原文）、`vec`（可选向量，无 embedder 时 None）、`resp`（LLMResponse 的 JSON 字符串）。

- **第 276 行** `text: str`
- **第 277 行** `vec: list[float] | None`
- **第 278 行** `resp: str`

### 第 281-286 行：_parse_entry（解析条目）

- **第 281 行** `def _parse_entry(raw: str) -> _SemanticCacheEntry | None:`
- **第 282 行** `"""把存储工具的 JSON 字符串解析为条目视图；非法 JSON 返回 None。"""`
- **第 283 行** `try:`
- **第 284 行** `return cast(_SemanticCacheEntry, json.loads(raw))`
  解析并在边界 cast 成 TypedDict（规则 6）。
- **第 285 行** `except json.JSONDecodeError:`
  非法 JSON。
- **第 286 行** `return None`
  返回 None。
  - 不这么写（抛异常）：一条坏数据会中断整个 scan 循环，影响其他正常候选项。返回 None 让调用方跳过坏条目。

### 第 289-301 行：_query_text（提取查询文本）

- **第 289 行** `def _query_text(request: LLMRequest) -> str:`
- **第 290 行** `"""提取用于相似度匹配的文本（最后一条 user 消息，无则拼全部 content）。"""`
- **第 291-295 行** 列 user 消息
  - 第 291-295 行：`user_texts = [str(m.get(_CONTENT_KEY, "")) for m in request.messages if isinstance(m, dict) and m.get(_ROLE_KEY) == _USER_ROLE]`
  - 只取 role=="user" 的消息 content，归一成 str。
- **第 296 行** `if user_texts:`
  有 user 消息。
- **第 297 行** `return " ".join(user_texts[-1].lower().split())`
  取**最后一条** user 消息（最近的意图最相关），小写 + 按空白切分再 join（归一空白）。
- **第 298-300 行** 无 user 消息时
  - `all_texts = [str(m.get(_CONTENT_KEY, "")) for m in request.messages if isinstance(m, dict)]`
  - 取所有消息的 content。
- **第 301 行** `return " ".join(" ".join(all_texts).lower().split())`
  全部拼接 + 小写 + 空白归一。
  - 为什么要归一（小写 + 空白）：让语义匹配忽略大小写和多余空白，减少无意义差异。

### 第 304-306 行：_store_key（存储键构造）

- **第 304 行** `def _store_key(kind: str, version: str, identity: str) -> str:`
- **第 305 行** `"""存储键：前缀隔离实现类型 + 版本 + 身份（供 scan 按其前缀遍历）。"""`
- **第 306 行** `return f"cache:{kind}:{version}:{identity}"`
  格式 `cache:实现类型:版本:身份`。
  - `kind`（exact/semantic）隔离两种缓存，避免互相污染。
  - `version` 隔离 prompt 版本。
  - `identity` 是具体键。
  - scan 用 `cache:semantic:{version}:` 前缀取出该版本全部语义条目。

### 第 309-351 行：ExactCache（精确缓存）

- **第 312 行** `class ExactCache:`
  精确缓存：sha256 精确匹配；存储工具可注入（缺省内存）。

- **第 313 行** `"""精确缓存：sha256 精确匹配；存储工具可注入（缺省 InMemoryCacheStore）。"""`
- **第 315 行** `name = "exact_cache"`

- **第 317-327 行** `__init__`
- **第 317 行** `def __init__(self, *, store: CacheStore | None = None, ttl: int | None = None) -> None:`
- **第 318-323 行** docstring：store（缺省内存）、ttl（None 永久）。
- **第 324 行** `self._store = store if store is not None else InMemoryCacheStore()`
  注入存储工具，缺省内存。
- **第 325 行** `self._ttl = ttl`
- **第 326 行** `self.hits = 0`
  命中计数。
- **第 327 行** `self.misses = 0`
  未命中计数。

- **第 329-331 行** `_identity`
- **第 329 行** `def _identity(self, request: LLMRequest, version: str) -> str:`
- **第 330 行** `"""精确身份 = request 的 sha256（含版本）。"""`
- **第 331 行** `return build_cache_key(request, prompt_version=version).value`
  复用 types 的 sha256 键。

- **第 333-341 行** `get`
- **第 333 行** `async def get(self, request: LLMRequest, *, version: str = "current") -> LLMResponse | None:`
- **第 334 行** `"""命中 +1 返回响应；未命中 +1 返回 None。"""`
- **第 335 行** `key = _store_key("exact", version, self._identity(request, version))`
  构造存储键。
- **第 336 行** `raw = await self._store.get(key)`
  读存储。
- **第 337 行** `if raw is None:`
- **第 338 行** `self.misses += 1`
- **第 339 行** `return None`
- **第 340 行** `self.hits += 1`
- **第 341 行** `return LLMResponse.model_validate_json(raw)`
  命中：把 JSON 字符串反序列化成 LLMResponse。

- **第 343-346 行** `put`
- **第 343 行** `async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "current") -> None:`
- **第 344 行** `"""写入精确键 → 响应 JSON。"""`
- **第 345 行** `key = _store_key("exact", version, self._identity(request, version))`
- **第 346 行** `await self._store.put(key, resp.model_dump_json(), ttl=self._ttl)`
  `model_dump_json()` 序列化响应，ttl 透传。

- **第 348-351 行** `invalidate`
- **第 348 行** `async def invalidate(self, request: LLMRequest, *, version: str = "current") -> None:`
- **第 349 行** `"""删除精确键。"""`
- **第 350 行** `key = _store_key("exact", version, self._identity(request, version))`
- **第 351 行** `await self._store.delete(key)`
  只删精确键（精确失效只影响一个条目）。

### 第 354-461 行：SimilarityCache（语义相似度缓存）

- **第 357 行** `class SimilarityCache:`
  语义相似度缓存：可注入 embedder（向量余弦）与 store。

- **第 358-363 行** docstring
  - 缺省用 HashCacheEmbedder 走向量余弦；显式传 `embedder=None` 降级 difflib 文本相似度；两种模式都把候选存进 CacheStore（可 Redis/DB），跨实例共享。

- **第 365 行** `name = "similarity_cache"`

- **第 367-395 行** `__init__`
- **第 367-374 行** 签名
  - 第 370 行 `threshold: float = 0.85`：命中最低相似度。
  - 第 371 行 `store: CacheStore | None = None`
  - 第 372 行 `embedder: CacheEmbedder | None | _Unset = _UNSET`：**三态类型**——_Unset（未传）、None（显式降级）、CacheEmbedder（注入实现）。
  - 第 373 行 `ttl: int | None = None`
- **第 375-383 行** docstring：threshold 必须在 (0,1]，越高越保守。
- **第 384 行** `if not 0.0 < threshold <= 1.0:`
  阈值范围校验：大于 0 且 ≤ 1。
- **第 385 行** `raise ValueError("threshold must be in (0, 1]")`
  非法阈值拒绝。
- **第 386 行** `self._threshold = threshold`
- **第 387 行** `self._store = store if store is not None else InMemoryCacheStore()`
- **第 388 行** 注释 `# 哨兵区分"未传"与"显式 None"：未传用默认哈希 embedder，显式 None 保留 difflib 降级。`
- **第 389 行** `if isinstance(embedder, _Unset):`
  未传（是哨兵）。
- **第 390 行** `self._embedder: CacheEmbedder | None = HashCacheEmbedder()`
  用默认哈希 embedder。
- **第 391 行** `else:`
- **第 392 行** `self._embedder = embedder`
  显式值（可能是 None 或注入实现），直接采用。
- **第 393 行** `self._ttl = ttl`
- **第 394 行** `self.hits = 0`
- **第 395 行** `self.misses = 0`

- **第 397-399 行** `_identity`
- **第 397 行** `def _identity(self, text: str) -> str:`
- **第 398 行** `"""身份 = 原文（有 embedder 时用向量串，供确定性键）。"""`
- **第 399 行** `return text`
  直接返回原文（注释说有 embedder 时可用向量串，但代码实际返回原文）。

- **第 401-403 行** `_vector_of`
- **第 401 行** `async def _vector_of(self, text: str) -> tuple[float, ...] | None:`
- **第 402 行** `"""有 embedder 才算向量；否则 None（走文本路径）。"""`
- **第 403 行** `return await self._embedder.embed(text) if self._embedder is not None else None`
  有 embedder 就算向量，没有返回 None（降级文本相似度）。

- **第 405-428 行** `get`（语义查询，核心）
- **第 405 行** `async def get(self, request: LLMRequest, *, version: str = "current") -> LLMResponse | None:`
- **第 406 行** `"""按相似度找最匹配条目；≥ threshold 才命中。"""`
- **第 407 行** `query = _query_text(request)`
  提取查询文本。
- **第 408 行** `q_vec = await self._vector_of(query)`
  算查询向量（可能 None）。
- **第 409 行** `# 条目结构见 _SemanticCacheEntry（text/vec/resp 明确字段）。`
- **第 410 行** `prefix = _store_key("semantic", version, "")`
  语义条目的 scan 前缀。
- **第 411 行** `best: LLMResponse | None = None`
  最优响应。
- **第 412 行** `best_score = 0.0`
  最优分数。
- **第 413 行** `for _key, raw in await self._store.scan(prefix):  # 遍历同前缀全部候选取最相似者；阈值裁决延到循环后，避免先到先得`
  **遍历全部候选**。
  - **为什么不能先到先得**：候选项的存储顺序不确定，第一个相似度达阈值的未必是最相似的。必须遍历完取全局最优再判阈值，否则会命中一个「够用但不是最好」的旧响应。
- **第 414 行** `entry = _parse_entry(raw)`
  解析条目。
- **第 415 行** `if entry is None:  # 解析失败的条目跳过，不让坏数据参与匹配或污染缓存`
- **第 416 行** `continue`
  跳过坏条目。
- **第 417 行** `if q_vec is not None and entry["vec"] is not None:  # 双方都有向量才走余弦；否则降级文本相似度（零依赖开箱即用）`
- **第 418 行** `score = _cosine(q_vec, tuple(entry["vec"]))`
  双方有向量 → 余弦。
- **第 419 行** `else:`
- **第 420 行** `score = SequenceMatcher(None, query, entry["text"]).ratio()`
  否则 difflib 文本相似度。
- **第 421 行** `if score > best_score:  # 只保留当前最优；遍历完整后再按 threshold 一锤定音`
- **第 422 行** `best_score = score`
- **第 423 行** `best = LLMResponse.model_validate_json(entry["resp"])`
  记录最优响应。
- **第 424 行** `if best is not None and best_score >= self._threshold:  # 阈值在循环后统一判定：命中必须是全局最优候选`
  **阈值在循环后判**：命中必须是最优候选且分数 ≥ 阈值。
- **第 425 行** `self.hits += 1`
- **第 426 行** `return best`
- **第 427 行** `self.misses += 1`
- **第 428 行** `return None`

- **第 430-445 行** `put`（写条目）
- **第 430 行** `async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "current") -> None:`
- **第 431 行** `"""写入条目（原文 + 可选向量 + 响应 JSON）到存储工具。"""`
- **第 432 行** `query = _query_text(request)`
- **第 433 行** `vec = await self._vector_of(query)`
- **第 434-438 行** `entry = {...}` 构造条目
  - 第 435 行 `"text": query`
  - 第 436 行 `"vec": list(vec) if vec is not None else None`（tuple 转 list 便于 JSON 序列化）
  - 第 437 行 `"resp": resp.model_dump_json()`
- **第 439 行** 注释 `# 键含原文 hash，保证同 query 覆盖；scan 按前缀取回全部候选`
- **第 440-443 行** 用原文 hash 做 identity
  - `build_cache_key(LLMRequest(messages=[{"role": "user", "content": query}]), prompt_version="sem").value`
  - 构造一个只含 query 的临时请求算哈希，作为 identity。
- **第 444 行** `key = _store_key("semantic", version, identity)`
- **第 445 行** `await self._store.put(key, json.dumps(entry, ensure_ascii=False), ttl=self._ttl)`
  序列化条目写入。

- **第 447-461 行** `invalidate`（语义失效）
- **第 447 行** `async def invalidate(self, request: LLMRequest, *, version: str = "current") -> None:`
- **第 448 行** `"""删除相似度 ≥ threshold 的条目（语义失效）。"""`
- **第 449 行** `query = _query_text(request)`
- **第 450 行** `q_vec = await self._vector_of(query)`
- **第 451 行** `prefix = _store_key("semantic", version, "")`
- **第 452 行** `for key, raw in await self._store.scan(prefix):  # 语义失效可能命中多条，须逐个删除，不能只删第一个`
  **逐个删除所有达阈值条目**——语义失效可能命中多条（都是相似意图的旧响应），只删第一个会留下其余旧响应继续被误命中。
- **第 453 行** `entry = _parse_entry(raw)`
- **第 454 行** `if entry is None:  # 坏条目无法算分，跳过以免影响其它候选的失效判断`
- **第 455 行** `continue`
- **第 456 行** `if q_vec is not None and entry["vec"] is not None:  # 与 get 同一套相似度口径，保证写入/失效判定一致`
- **第 457 行** `score = _cosine(q_vec, tuple(entry["vec"]))`
- **第 458 行** `else:`
- **第 459 行** `score = SequenceMatcher(None, query, entry["text"]).ratio()`
- **第 460 行** `if score >= self._threshold:  # 达阈值即失效：语义相同的历史条目都清掉，避免旧响应被再次命中`
- **第 461 行** `await self._store.delete(key)`
  与 get 用同一套相似度口径，保证一致性。

**cache 小结**：三层可插拔（CachePort 策略 / CacheStore 存储 / CacheEmbedder 向量），每层协议+默认。HashCacheEmbedder 用 31 进制确定性哈希（不用进程随机 `hash()`）、二值化、512 维、512 字符头尾窗口抗收敛；空文本零向量防误命中。SimilarityCache 遍历全部候选后判阈值（全局最优）、失效删所有达阈值条目、哨兵区分未传/显式 None。InMemoryCacheStore 用懒删除 TTL。

---

## 8. gateway.py 逐行讲解（574 行）

核心中的核心。`ModelGateway` 是所有零件的编排者，三条路径（complete / stream / stream_events）共用一套纪律。按函数分节逐行讲。

### 第 1-57 行：docstring、imports、常量

- **第 1 行** `"""R4 模型网关：编排 缓存→路由→调用→结构化校验/修复→成本归因。`
  一句话点明主链路顺序。

- **第 2-4 行** docstring 正文
  - 第 3 行：上层唯一入口 `ModelGateway.complete()`；换 LLMPort 实现时上层零改动（R4 §8 验收 3）。
  - 第 4 行：成本归因只记录 LLMPort 响应里的 usage，不自行估算；CostGuard 保证不绕过预算。

- **第 5 行** `"""` 结束。

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `import asyncio` future / shield / get_running_loop（singleflight 用）。

- **第 9 行** `from collections.abc import AsyncIterator, Awaitable, Callable`
  - `AsyncIterator`：流式返回类型。
  - `Awaitable` / `Callable`：适配入口返回类型。

- **第 10 行** `from typing import NoReturn, cast`
  - `NoReturn`：`_raise_resolution_error` 永远抛异常。
  - `cast`：类型断言（LLMPortWithUsage、effect→Invoke）。

- **第 12 行** `from compose.context import Invocation, RoutingTarget`
  可靠性执行器路径的 Invocation 与 typed 降级目标。

- **第 13 行** `from compose.guards import CallState`
  dispatch 路径读 RoutingTarget 的状态守卫。

- **第 14 行** `from harness._ctxview import as_port_ctx, coerce_port_ctx`
  - `as_port_ctx`：`_center` 把 ctx 归一成 PortCtx。
  - `coerce_port_ctx`：取 agent_id/user_id 的具名视图（规则 6 唯一动态边界）。

- **第 15 行** `from harness.model.ports import LLMPortWithUsage`
  扩展协议（流式 usage）。

- **第 16-27 行** `from compose import (...)` 批量导入
  LLMPort、LLMRequest、LLMResponse、LifecycleBus、LifecyclePhase、Notice、PortCtx、StreamEvent、StreamEventKind、Usage。涵盖后端协议、DTO、生命周期通知、事件。

- **第 28 行** `from runtime import Effect, Invoke, Message, PortName, Result`
  - `Effect` / `Invoke`：effect 形态与判断。
  - `Message`：修复循环构造 system 消息。
  - `PortName`：端口名常量。
  - `Result`：adapt 入口的返回信封。

- **第 30 行** `from .cache import CachePort, ExactCache`
- **第 31 行** `from .registry import ModelRegistry`
- **第 32 行** `from .router import NO_DEFAULT, ModelRouter, RuleRouter`
- **第 33 行** `from .stream import has_stream_events, wrap_text_stream`
- **第 34 行** `from .structured import SchemaValidator, StructuredOutputValidator`
- **第 35-47 行** `from .types import (...)`
  导入全部类型：REFUSED_CODE、TARGET_UNRESOLVED_CODE、BackendResolution、CostAttributor、CostGuard、CostRecord、RefuseExecution、Reservation、StructuredOutputSpec、TargetUnresolved、cache_key_string。

- **第 49 行** `__all__ = ["ModelGateway"]`
  唯一公共导出。

- **第 51-52 行** 注释
  `# R4 本地拒绝目标常量（规则 5）：值与 R3 DegradeMode.REFUSE 对齐，但网关不依赖 R3，故在本层用具名常量替代魔法串，比较时右侧是常量名而非字符串字面量。`
  解释为什么不 import R3 的常量（避免依赖），而用本地具名常量。

- **第 53 行** `REFUSE_TARGET = "refuse"`
  本地拒答目标常量。

- **第 55-56 行** 注释
  `# 无定价信息时的保守单次预估（美元）。用户可通过 cost_estimator 注入精确单价（如接 router.cost_tiers / 模型定价表）；此值偏保守，宁可少放也不超支。`

- **第 57 行** `_DEFAULT_ESTIMATED_COST_USD = 0.01`
  默认预估 0.01 美元。保守策略：宁可少放不超支。

### 第 60-64 行：_default_cost_estimate（默认成本预估）

- **第 60 行** `def _default_cost_estimate(request: LLMRequest) -> float:`
- **第 61 行** `"""默认成本预估：有 max_tokens 时按 1e-5 USD/token 估，否则用保守常量。"""`
- **第 62 行** `if request.max_tokens is not None and request.max_tokens > 0:`
  有正的 max_tokens。
- **第 63 行** `return max(_DEFAULT_ESTIMATED_COST_USD, request.max_tokens * 1e-5)`
  两者取大：按 token 估（1e-5/token）和保守常量 0.01 取较大者。
  - `max` 保证即使 token 很少也至少预留 0.01（保守）。
- **第 64 行** `return _DEFAULT_ESTIMATED_COST_USD`
  无 max_tokens 用常量 0.01。

### 第 67-116 行：ModelGateway 类与 __init__

- **第 67 行** `class ModelGateway:`
  模型网关：可插拔组合 LLMPort + 路由 + 缓存 + 结构化校验 + 成本账本。

- **第 68-72 行** docstring
  说明属性 ledger（成本账本，外部可查）、cache（缓存实例）。

- **第 74-87 行** `__init__` 签名（全部关键字参数）
  - 第 77 行 `llm: LLMPortWithUsage`：唯一必填。
  - 第 78 行 `router: ModelRouter | None = None`
  - 第 79 行 `cache: CachePort | None = None`
  - 第 80 行 `spec: StructuredOutputSpec | None = None`
  - 第 81 行 `validator: SchemaValidator | None = None`
  - 第 82 行 `prompt_version: str = "current"`
  - 第 83 行 `cost_guard: CostGuard | None = None`
  - 第 84 行 `registry: ModelRegistry | None = None`
  - 第 85 行 `cost_estimator: Callable[[LLMRequest], float] | None = None`
  - 第 86 行 `bus: LifecycleBus | None = None`
  - 第 87 行 `) -> None:`

- **第 88-101 行** docstring
  说明各参数。重点：
  - 第 92 行：router「complete 时真调用，不再空转」（修复历史问题）。
  - 第 95-96 行：validator 仅在 spec 非空时生效，替换后校验与修复循环用同一实现。
  - 第 98 行：registry 缺省 None = 仅默认后端（兼容旧行为）。
  - 第 100 行：cost_estimator 供 reserve 预留，可注入定价表。

- **第 102 行** `self._llm = llm`
  默认后端。

- **第 103 行** `self._router = router or RuleRouter()`
  缺省规则路由。
  - 用 `or`（真值判断）：若调用方传了一个 falsy 的 router 对象会被替换，但正常 router 实例都是 truthy，可接受。

- **第 104 行** `self._cache = cache if cache is not None else ExactCache()`
  缺省精确缓存。**注意这里用 `is not None` 而非 `or`**——允许调用方传一个自定义但 falsy 的缓存对象。

- **第 105 行** `self._registry = registry`
- **第 106 行** `self._spec = spec`
- **第 107 行** `self._validator = StructuredOutputValidator(spec, validator=validator) if spec else None`
  **仅 spec 非空时创建校验器**。没有 spec 就没有校验成本。
- **第 108 行** `self._prompt_version = prompt_version`
- **第 109 行** `self._cost_guard = cost_guard`
- **第 110 行** `self._cost_estimator = cost_estimator or _default_cost_estimate`
  缺省预估函数。
- **第 111 行** `self._bus = bus  # Lifecycle 通知（before_model/after_model；缺省不发）`
- **第 112 行** `self.ledger = CostAttributor()`
  成本账本（公共属性）。
- **第 113 行** `self.cache = self._cache`
  缓存公共别名。
- **第 114-115 行** 注释
  `# P2a singleflight：同一缓存 key 的并发 miss 共享一次后端调用，避免击穿（40 并发 → 真调 40 次）、重复记账。`
- **第 116 行** `self._inflight: dict[str, asyncio.Future[LLMResponse]] = {}`
  **singleflight 核心结构**：key → Future。在途请求表。

### 第 118-121 行：registry 属性

- **第 118 行** `@property`
- **第 119 行** `def registry(self) -> ModelRegistry | None:`
- **第 120 行** `"""装配核对用：网关解析降级目标所用的注册表（与共用容器同源）。"""`
- **第 121 行** `return self._registry`
  暴露注册表供装配核对（验证容器/网关/能力共用同一份注册表）。

### 第 123-134 行：_notify_model（生命周期通知）

- **第 123-124 行** 签名
  `def _notify_model(self, phase: LifecyclePhase, request: LLMRequest, ctx: PortCtx, *, extra: dict[str, object] | None = None) -> None:`
- **第 125 行** `"""发 model 相位通知（纯通知、异常隔离由总线保证，不拦截调用）。"""`
  说明：纯通知，异常隔离由总线负责，通知失败不影响主调用。
- **第 126 行** `if self._bus is None:`
  没装配总线。
- **第 127 行** `return`
  直接返回（通知是可选的）。
- **第 128-134 行** `self._bus.emit(Notice(...))` 构造通知
  - 第 129 行 `phase=phase`
  - 第 130 行 `session_id=ctx.session_id`
  - 第 131 行 `port=PortName.LLM.value`
  - 第 132 行 `payload={"model": request.model or "", **(extra or {})}`：模型名 + 额外信息（如 error）。
  - 第 133 行 `meta=dict(ctx.meta)`：meta 复制一份（快照）。
  - 第 134 行 `))`

### 第 136-175 行：_resolve_llm（降级唯一决策点，核心）

- **第 136-138 行** 签名
  `def _resolve_llm(self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None) -> BackendResolution:`

- **第 139-151 行** docstring（设计要点全在这里）
  - 第 139 行：降级执行唯一决策点（R4 §2.2/§5.5）：返回三态，任何分支不回退默认模型。
  - 第 143 行：degrade_target 来自 inv.meta 的 typed routing_target，优先于 router 结果。
  - 第 147 行：**router 总是被调用**（修复 `_router` 空转），路由理由进 ctx.meta 供 trace。
  - 第 148 行：**REFUSE 优先于一切**：不解析、不缓存、不调后端。
  - 第 149 行：非空未注册 target → TargetUnresolved（不再 raise ValueError 折叠成 PORT_CRASHED）。
  - 第 150 行：registry 缺省时：无 target 走默认后端（兼容）；有 target 则 TargetUnresolved（不回退）。

- **第 152 行** `# 构造 router 需要的 Invoke：task_hint 来自请求。`
- **第 153-160 行** 构造临时 Invoke
  - 第 153 行 `effect = Invoke(`
  - 第 154 行 `port=PortName.LLM.value,`
  - 第 155-159 行 `payload={"messages": [...], "model": ..., "task_hint": ...}`：把请求转成 router 能消费的 Effect（messages 转 dict、model/task_hint 缺省空串）。
  - 第 160 行 `)`
- **第 161 行** `decision = self._router.route(effect, ctx)`
  **router 总是被调用**——无论后续如何，路由先执行，理由进 trace。这是修复历史「router 定义了却从不被调用」问题的关键行。

- **第 162 行** `target = degrade_target or (decision.model if decision.model and decision.model != NO_DEFAULT else None)`
  **目标优先级**：
  - 传入的 `degrade_target`（typed 降级目标）优先。
  - 否则用 router 决策的 model，但要排除哨兵 `NO_DEFAULT`（"default"）和空值——这两种情况视为「未指定目标」→ None。
  - `or` 短路：degrade_target 非空直接用。

- **第 163-165 行** `ctx.meta.setdefault("model_route", []).append(f"{decision.reason}->{target or 'default'}")`
  把路由理由写进 `ctx.meta["model_route"]` 列表（用 `setdefault` 保证列表存在）。
  - 格式 `理由->目标`，目标空时显示 `default`。供 trace 解释「路由做了什么」。

- **第 166 行** `if target == REFUSE_TARGET:`
  目标是拒答。

- **第 167 行** `return RefuseExecution(reason="degraded to refusal; no backend invoked")`
  返回拒答态。**REFUSE 优先于一切**：在这里就返回，后面不解析、不缓存、不调后端。

- **第 168 行** `if target is None:`
  无目标。

- **第 169-170 行** `# 无目标：router 未显式指定 → 默认后端（设计允许的唯一下沉情形）。` / `return self._llm`
  **唯一下沉情形**：只有 router 完全没指定目标时才走默认后端。这是设计允许的唯一回退，其他非空目标都必须解析。

- **第 171 行** `resolved = self._registry.resolve_llm(target) if self._registry is not None else None`
  有注册表就解析目标，没有就 None。

- **第 172 行** `if resolved is None:`
  解析失败（目标未注册或没注册表）。

- **第 173-174 行** `# 非空未注册目标：显式失败，绝不静默回退（降级红线）。` / `return TargetUnresolved(target=target)`
  **降级红线**：非空目标解析不到就显式失败，绝不静默回退默认。
  - 不这么写（回退 self._llm）：降级目标配错也不会被发现，且用错模型。

- **第 175 行** `return resolved`
  解析成功，返回后端实例。

### 第 177-187 行：_resolution_error_response（终止态折叠为响应）

- **第 177 行** `@staticmethod`
- **第 178 行** `def _resolution_error_response(resolution: RefuseExecution | TargetUnresolved) -> LLMResponse:`
- **第 179-182 行** docstring
  说明：把终止型三态折叠为带稳定错误码前缀的 LLMResponse；error 前缀即稳定码，与三态 to_result() 同源常量。

- **第 183 行** `code = REFUSED_CODE if isinstance(resolution, RefuseExecution) else TARGET_UNRESOLVED_CODE`
  按三态类型选稳定码。

- **第 184-187 行** 构造响应
  - 第 185 行 `content="", tool_calls=[], usage=Usage(),`：空内容、空工具、空 usage。
  - 第 186 行 `error=f"{code}: {resolution.reason if isinstance(resolution, RefuseExecution) else resolution.target}"`：错误 = 稳定码 + 详情（拒答用 reason，未注册用 target）。
  - 第 184 行 `return LLMResponse(`

### 第 189-194 行：_raise_resolution_error（终止态抛异常，流路径用）

- **第 189 行** `@staticmethod`
- **第 190 行** `def _raise_resolution_error(resolution: RefuseExecution | TargetUnresolved) -> NoReturn:`
- **第 191 行** `"""流路径的终止型三态：抛带稳定码的 ValueError（经 runner 折叠为失败信封）。"""`
- **第 192 行** `code = REFUSED_CODE if isinstance(resolution, RefuseExecution) else TARGET_UNRESOLVED_CODE`
- **第 193 行** `detail = resolution.reason if isinstance(resolution, RefuseExecution) else resolution.target`
- **第 194 行** `raise ValueError(f"{code}: {detail}")`
  抛异常（不是返回响应）——因为流是异步生成器，不能像 complete 那样返回响应，只能抛错让 Runner 折叠成失败信封。
  - 与 `_resolution_error_response` 同源稳定码：`complete` 用前者返回响应，`stream`/`stream_events` 用后者抛异常。

### 第 196-210 行：_extract_target（从 typed 目标读 target）

- **第 196 行** `def _extract_target(self, ctx: object, inv: Invocation | None = None) -> str | None:`
- **第 197-202 行** docstring
  - 唯一来源；reason 只进 trace 不作 target。
  - P4 双入口兼容：inv 给定读 `inv.routing_target()`（可靠性执行器路径）；inv 缺省从 `ctx.meta` 读 RoutingTarget（dispatch 路径，经 CallState）。
- **第 203 行** `routing = inv.routing_target() if inv is not None else CallState(ctx).routing_target()`
  按路径读 typed RoutingTarget。
- **第 204 行** `target = routing.target if routing is not None else None`
  取 target。
- **第 205 行** `if routing is not None:`
  有路由目标信息。
- **第 206 行** `# 规则 6 例外：meta 是 R1 RuntimeCtx 具名固定字段`
- **第 207 行** `ctx_meta = getattr(ctx, "meta", None)`
- **第 208 行** `if isinstance(ctx_meta, dict):`
- **第 209 行** `ctx_meta.setdefault("degrade_reason", []).append(routing.reason)`
  **reason 只写进 `ctx.meta["degrade_reason"]` 作 trace，不作 target**——避免把降级理由误当成目标名。
- **第 210 行** `return target`
  返回 target（不含 reason）。

### 第 212-248 行：_center（adapt 入口共用实现）

- **第 212 行** `async def _center(self, effect: Effect, ctx: object, target: str | None) -> Result:`
- **第 213 行** `"""模型中心调用（as_call / as_dispatch 共用的唯一实现）。"""`
- **第 214 行** `if not isinstance(effect, Invoke):`
  非 Invoke 的 effect。
- **第 215 行** `return Result.failure("INVALID_EFFECT", "model port needs Invoke effect")`
  显式失败（模型 port 只接受 Invoke）。

- **第 217 行** `port_ctx = as_port_ctx(ctx)`
  把 ctx 归一成 PortCtx。

- **第 218 行** `payload = effect.payload`
  取 payload。

- **第 219-223 行** 构造请求
  - `messages=list(payload.get("messages", []))`
  - `model=str(payload.get("model") or "") or None`
  - `task_hint=str(payload.get("task_hint") or "") or None`
  - 缺省归 None/空，形态健壮。

- **第 224-227 行** BEFORE_MODEL 通知
  构造一个不含 task_hint 的简化请求发通知。

- **第 229 行** `error_text = ""`
  错误文本，供 finally 通知用。

- **第 230 行** `try:`

- **第 231 行** `if target == REFUSE_TARGET:`
  目标是拒答。

- **第 232 行** `error_text = REFUSED_CODE`
- **第 233 行** `return RefuseExecution("degraded to refusal; no backend invoked").to_result()`
  直接折叠成 Result 失败（不调 complete）。**REFUSE 在最前面短路**。

- **第 234 行** `response = await self.complete(request, port_ctx, degrade_target=target)`
  正常路径调 complete，带 degrade_target。

- **第 235 行** `error_text = response.error or ""`
  记录响应错误。

- **第 236 行** `if response.error:`
  响应有错。

- **第 237 行** `return Result.failure(response.error.split(":")[0], response.error)`
  用错误前缀（`:` 前部分）作为稳定码折叠失败。`SCHEMA_INVALID: ...` → 稳定码 `SCHEMA_INVALID`。

- **第 238-242 行** 成功路径
  - `return Result.success({"content": ..., "tool_calls": ..., "usage": ...})`
  - 把响应的三个关键字段装进 Result。

- **第 243 行** `except BaseException as exc:`
  捕获所有异常（含取消）。

- **第 244 行** `error_text = error_text or f"{type(exc).__name__}: {exc}"`
  异常时补错误文本（若还没设）。

- **第 245 行** `raise`
  原样重抛（不吞异常）。

- **第 246-248 行** `finally:` 发 AFTER_MODEL 通知
  - 第 247 行 `self._notify_model(LifecyclePhase.AFTER_MODEL, request, port_ctx, extra={"error": error_text})`
  - 无论成功/失败/异常都发通知，带 error 信息。

### 第 250-258 行：as_call（可靠性执行器适配入口）

- **第 250 行** `def as_call(self) -> Callable[[Invocation], Awaitable[Result]]:`
- **第 251-253 行** docstring
  兼容入口（可靠性执行器 core）：Invocation → _center。降级目标从 `inv.routing_target()` 读（R3 typed，与 Registry 同源）。
- **第 255 行** `async def adapter(inv: Invocation) -> Result:`
  内部适配器。
- **第 256 行** `target = self._extract_target(inv.ctx, inv)`
  从 Invocation 读 target（传 inv）。
- **第 257 行** `return await self._center(inv.effect, inv.ctx, target)`
  调共用实现。
- **第 258 行** `return adapter`
  返回适配器函数。

### 第 260-270 行：as_dispatch（compose dispatch 适配入口）

- **第 260 行** `def as_dispatch(self) -> Callable[[Effect, object], Awaitable[Result]]:`
- **第 261-265 行** docstring
  §2.5 dispatch 形态（规格 P4）：直接吃 (effect, ctx)，不构造 Invocation。before/after_model 通知保留；降级目标从 ctx.meta 的 typed RoutingTarget 读（经 CallState 写入）；REFUSE 真拒答、未注册目标 TargetUnresolved 折叠失败，绝不回退默认。
- **第 267 行** `async def dispatch(effect: Effect, ctx: object) -> Result:`
- **第 268 行** `target = self._extract_target(ctx)`
  从 ctx 读 target（不传 inv）。
- **第 269 行** `return await self._center(effect, ctx, target)`
- **第 270 行** `return dispatch`

**两个入口的关系**：`as_call` 吃 Invocation（可靠性执行器路径），`as_dispatch` 吃 (effect, ctx)（compose 直接分派路径），两者都归一到同一个 `_center` 实现，区别只在「从哪里读降级目标」。

### 第 272-321 行：complete（主链路，核心）

- **第 272-278 行** 签名
  `async def complete(self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None) -> LLMResponse:`

- **第 279-286 行** docstring
  - 一次完整模型调用：缓存命中直接返回，否则路由→调用→校验→记账。
  - 返回：缓存命中/正常/修复后响应；strict 失败/修复超次返回 `error="SCHEMA_INVALID: ..."` 响应（N3：不抛穿成 PORT_CRASHED；由 as_call 折叠为 Result.failure("SCHEMA_INVALID")）。

- **第 288 行** `# 拒答优先于缓存：先过唯一决策点，非 LLMPort 三态直接折叠，绝不读缓存或调后端。`
  **关键顺序**：拒答决策在缓存之前。

- **第 289 行** `resolution = self._resolve_llm(request, ctx, degrade_target=degrade_target)`
  先过唯一决策点。

- **第 290 行** `if not isinstance(resolution, LLMPort):`
  不是后端（是终止态）。

- **第 291 行** `return self._resolution_error_response(resolution)`
  直接折叠成错误响应。**不读缓存、不调后端**。
  - 不这么写（先查缓存）：拒答请求可能命中旧缓存返回内容，违背「拒答优先」。

- **第 293 行** `cached = await self._cache.get(request, version=self._prompt_version)`
  查缓存。

- **第 294 行** `if cached is not None:  # 命中即短路：不再调后端、不再记成本（usage 已随条目存好）`
- **第 295 行** `return cached`
  命中直接返回。

- **第 297-298 行** 注释 singleflight 说明
  `# P2a singleflight：相同 key 的并发 miss 共享一次完整调用（含记账/校验/缓存），后续等待者直接拿同一 Future 的结果，不再各调一次后端、各记一笔成本。`

- **第 299 行** `key = cache_key_string(request, prompt_version=self._prompt_version)`
  算 singleflight key（复用缓存键）。

- **第 300 行** `inflight = self._inflight.get(key)`
  查在途表。

- **第 301 行** `if inflight is None:  # 首个 miss 成为 owner 建 Future；并发后来者走 shield 等同一结果，不重复击穿`
  没有在途请求 → 成为 owner。

- **第 302 行** `loop = asyncio.get_running_loop()`
  取事件循环。

- **第 303 行** `inflight = loop.create_future()`
  建 Future。

- **第 304-308 行** 加 done callback 消费异常
  - 第 304-305 行注释：`# 消费 owner 路径 fut 的异常，避免无等待者时 "Future exception was never retrieved"；有等待者时不影响它们 await（exception() 只读取、不清除）。`
  - 第 306-308 行：
    ```
    inflight.add_done_callback(
        lambda f: f.exception() if not f.cancelled() else None
    )
    ```
  - **为什么需要**：owner 路径如果 Future 被 set_exception 但没人 await（无等待者），Python 会打 "Future exception was never retrieved" 警告。回调里读一次 `f.exception()` 消费掉。有等待者时，`exception()` 只是读取不清除，不影响它们 await 拿到异常。

- **第 309 行** `self._inflight[key] = inflight`
  登记在途表。

- **第 310 行** `try:  # owner 独占后端调用；异常/取消必须广播给等待者，避免它们各自重试再次击穿`
- **第 311 行** `response = await self._complete_miss(request, ctx, cast(LLMPortWithUsage, resolution))`
  owner 独占调用完整链路。**cast**：resolution 已是 LLMPort，这里断言它是 LLMPortWithUsage（因为有 usage 结算路径）。

- **第 312 行** `inflight.set_result(response)`
  成功：Future 置结果，所有等待者拿到同一响应。

- **第 313 行** `except BaseException as exc:`
  异常/取消。

- **第 314 行** `# 异常/取消传播给所有等待者（避免它们各自重试再次击穿）。`
- **第 315 行** `if not inflight.done():  # 幂等防御：已置终态时再 set 会抛 InvalidStateError 淹没原异常`
  **幂等防御**：只有 Future 未置终态才 set_exception。
  - 不这么写（直接 set）：若已经 set_result 又走到 except（极端情况），set_exception 会抛 InvalidStateError，淹没原始异常。

- **第 316 行** `inflight.set_exception(exc)`
  异常广播给所有等待者——它们不会各自重试（避免重复击穿）。

- **第 317 行** `raise`
  owner 自己重抛。

- **第 318 行** `finally:`
- **第 319 行** `self._inflight.pop(key, None)`
  无论成功失败都从在途表移除，让后续请求可重新发起。

- **第 320 行** `return response`
  owner 返回结果。

- **第 321 行** `return await asyncio.shield(inflight)`
  等待者路径：**用 shield 保护 Future**——等待者被取消时不会取消 owner 的调用（owner 继续完成，其他等待者仍受益）。
  - 不用 shield 的后果：一个等待者被取消 → Future 被取消 → owner 的调用被连带取消 → 所有等待者失败。

**singleflight 小结**：首个 miss 成为 owner 建 Future 独占调用；成功 set_result、异常 set_exception 广播；等待者 `await shield(inflight)` 共享结果且取消不影响 owner；done callback 消费异常警告；finally pop 清理。四重防御（shield / 异常广播 / 幂等防御 / 异常消费）确保并发安全。

### 第 323-380 行：_complete_miss（未命中完整链路）

- **第 323-325 行** 签名 `async def _complete_miss(self, request, ctx, llm: LLMPortWithUsage) -> LLMResponse:`
- **第 326-330 行** docstring
  - 一次未命中请求的完整链路：预留→调用→记账→校验→缓存（singleflight 内执行一次）。
  - llm 由 complete 经唯一决策点解析后传入（避免重复路由）。
  - P2b：调用前 reserve，成功后 commit 按实结算，失败/异常 cancel 释放预留——彻底消除「先调用后扣费」的并发击穿。

- **第 332 行** `reservation = None`
  预留凭证初始 None。

- **第 333 行** `if self._cost_guard is not None:  # 有硬闸才预留；reserve 用预估兜底，失败即视为预算耗尽`
- **第 334 行** `reservation = self._cost_guard.reserve(self._cost_estimator(request))`
  按预估值预留。

- **第 335 行** `if reservation is None:`
  预留失败（预算不足）。

- **第 336-340 行** 调用前拒绝
  - 第 336 行注释：`# 预算不足：调用前就拒，绝不发起后端请求。`
  - 第 337-340 行：返回 `error="COST_LIMIT: budget exhausted before call"` 响应。
  - **关键**：在发后端请求之前就拒，避免产生实际费用。

- **第 342 行** `try:  # 包住整条调用链：任何异常/取消都必须走到下面释放预留，不留悬空额度`
- **第 343 行** `response = await llm.complete(request, ctx)`
  调后端。

- **第 344 行** `if response.error:  # 错误响应提前返回：不校验、不缓存；预留必须在此释放而非拖到结算`
- **第 345 行** `# 后端失败：释放预留、照实记账（usage 为零由响应自带）。`
- **第 346-348 行** 取消预留
  - `if reservation is not None and self._cost_guard is not None:`
  - `self._cost_guard.cancel(reservation)`
  - `reservation = None`
- **第 349 行** `self._record_cost(response, ctx)`
  照实记账（usage 通常为 0，后端响应自带）。
- **第 350 行** `return response`
  错误响应直接返回，不校验不缓存。

- **第 352 行** `if self._validator is not None:  # 仅装配 spec 时才有校验器；非结构化场景跳过校验直接结算`
- **第 353 行** `# E6：按 spec.mode 决定是否修复；constrained/prompt 模式只校验不修复。`
- **第 354 行** `if self._validator.should_repair:`
  repair 模式。

- **第 355 行** `response = await self._complete_with_repair(response, request, ctx, llm=llm)`
  进修复循环。

- **第 356 行** `elif self._spec is not None and self._spec.strict:  # 非修复模式靠 strict 兜底拒绝非法输出`
  constrained/prompt 模式 + strict。

- **第 357 行** `errors = self._validator.check(response.content or "")`
  校验输出。

- **第 358 行** `if errors:  # 有错即判非法：折叠成 error 响应，绝不流入缓存或工具调用`
- **第 359 行** `# N3：strict 失败折叠 error（不抛穿）；预留也要释放。`
- **第 360-362 行** 取消预留。
- **第 363-366 行** 返回 SCHEMA_INVALID 响应
  - `content="", tool_calls=[], usage=response.usage, model=response.model, error=f"SCHEMA_INVALID: {errors}"`
  - **N3 关键**：折叠成 error 响应，不抛 ValueError 穿透成 PORT_CRASHED。由 as_call 折叠为 `Result.failure("SCHEMA_INVALID")`。

- **第 368 行** `# 成功后按实际成本结算预留并记账（reservation 交给 _record_cost commit）。`
- **第 369 行** `self._record_cost(response, ctx, reservation=reservation)`
  记账 + commit 结清预留。

- **第 370 行** `reservation = None  # 已结清，避免 finally 重复 cancel`
  置 None 防 finally 重复释放。

- **第 372 行** `# 非法输出绝不写缓存（否则污染后续请求）；仅合法响应入缓存。`
- **第 373 行** `if not response.error:`
  只有无错响应写缓存。
  - **非法输出不写缓存**：strict 折叠后的响应有 error，不写；修复后合法的写；错误的不写。防止脏数据污染后续请求。

- **第 374 行** `await self._cache.put(request, response, version=self._prompt_version)`
- **第 375 行** `return response`

- **第 376 行** `except BaseException:`
  异常/取消。
- **第 377 行** `# 异常/取消：释放预留，不记支出。`
- **第 378-379 行** 取消预留。
- **第 380 行** `raise`
  重抛。

**_complete_miss 小结**：reserve（预算不足调用前就拒）→ 调后端 → 错误响应提前返回（cancel + 记账，不校验不缓存）→ 校验（repair 循环 / strict 折叠 SCHEMA_INVALID）→ 记账 commit → 仅合法响应写缓存 → 异常 cancel 不记账。预留凭证在所有提前返回路径和异常路径都正确释放。

### 第 382-436 行：stream（文本流）

- **第 382-384 行** 签名 `async def stream(self, request, ctx, *, degrade_target=None) -> AsyncIterator[str]:`
- **第 385-393 行** docstring
  - 流式补全：缓存命中吐缓存，否则逐块转发并在结束时结算。
  - 产出：文本块（逐块）；取消由消费方 aclose() 传播。
  - 说明：**流式路径禁用结构化校验**（无法校验半截流），不经过 R1 Effect 循环。

- **第 394 行** `# 拒答优先于缓存：流路径先过唯一决策点；终止型三态抛稳定码错误，不吐缓存。`
- **第 395 行** `resolution = self._resolve_llm(request, ctx, degrade_target=degrade_target)`
- **第 396 行** `if not isinstance(resolution, LLMPort):`
- **第 397 行** `self._raise_resolution_error(resolution)`
  终止态抛稳定码错误（流路径不能返回响应，只能抛）。

- **第 399 行** `cached = await self._cache.get(request, version=self._prompt_version)`
- **第 400 行** `if cached is not None:  # 命中直接吐缓存文本，跳过预留/后端/结算（usage 已随条目存好）`
- **第 401 行** `# 流式是 Port 层，ctx 是 PortCtx（无 hook trace）；缓存命中信息随响应走。`
- **第 402-403 行** `for ch in cached.content or "": yield ch`
  逐字符吐缓存文本。
- **第 404 行** `return`
  命中后直接结束。

- **第 406 行** `# P2b：流式也在调用前预留（硬闸一致）；流末按实际结算，断流/取消释放。`
- **第 407 行** `reservation = None`
- **第 408 行** `if self._cost_guard is not None:`
- **第 409 行** `reservation = self._cost_guard.reserve(self._cost_estimator(request))`
- **第 410 行** `if reservation is None:`
- **第 411 行** `raise ValueError("cost budget exceeded (CostGuard) before stream")`
  预留失败抛错，不建立流。

- **第 413 行** `chunks: list[str] = []`
  累积文本块（用于流末写缓存）。

- **第 414 行** `llm = resolution`
- **第 415 行** `try:  # 断流/取消也从这里传播；只有正常流末才结算，异常一律释放预留`
- **第 416 行** `# LLMPort.stream 返回 AsyncIterator（PEP 525 签名），直接 async for 迭代。`
- **第 417 行** `async for chunk in llm.stream(request, ctx):`
  逐块迭代后端流。
- **第 418 行** `chunks.append(chunk)`
  累积。
- **第 419 行** `yield chunk`
  实时吐出（低延迟）。

- **第 421 行** `# 流结束：后端必须已把 usage 写入协议成员 last_usage（LLMPort 契约）。`
- **第 422 行** `# 缺失即拒绝服务并显式报错——绝不记一笔假 zero-cost 掩盖账单缺口。`
- **第 423 行** `usage = cast(LLMPortWithUsage, llm).last_usage`
  读后端末块写入的 usage。

- **第 424 行** `if usage is None:`
  **usage 缺失**。

- **第 425-428 行** 抛错
  - `raise ValueError("LLMPort.stream() finished without setting last_usage; cost cannot be settled (fake/zero-cost accounting forbidden)")`
  - **「流末必须有 usage」红线**：缺失就拒绝服务，不记假零成本。

- **第 429 行** `full_response = LLMResponse(content="".join(chunks), usage=usage)`
  拼接完整响应。

- **第 430 行** `self._record_cost(full_response, ctx, reservation=reservation)`
  按实结算 + 记账。

- **第 431 行** `reservation = None`
  防重复 cancel。

- **第 432 行** `await self._cache.put(request, full_response, version=self._prompt_version)`
  写缓存。

- **第 433 行** `except BaseException:  # 断流/取消：不结算不写缓存，取消预留以免额度悬空`
- **第 434-435 行** 取消预留。
- **第 436 行** `raise`
  重抛。

### 第 438-450 行：stream_events（结构化事件入口）

- **第 438-440 行** 签名 `def stream_events(self, request, ctx, *, degrade_target=None) -> AsyncIterator[StreamEvent]:`
  注意是普通 `def`（不是 async def）——它返回 `_stream_events_impl(...)` 返回的异步迭代器。

- **第 441-449 行** docstring
  - 结构化流式事件：透传后端事件，遇 USAGE 结算成本。
  - 产出：StreamEvent 序列（文本/工具/handoff/usage/done）；取消由 aclose() 传播。
  - 说明：若后端未发 USAGE 事件但 last_usage 已写（旧后端默认包装），在流末补结算。

- **第 450 行** `return self._stream_events_impl(request, ctx, degrade_target=degrade_target)`
  委托实现体。

### 第 452-507 行：_stream_events_impl（事件流实现）

- **第 452-454 行** 签名 `async def _stream_events_impl(...) -> AsyncIterator[StreamEvent]:`
- **第 455 行** `"""实现体：透传/包装后端事件，遇 USAGE 或流末 last_usage 结算，断流释放预留。"""`
- **第 456 行** `# 拒答优先于缓存：事件流先过唯一决策点；终止型三态抛稳定码错误。`
- **第 457 行** `resolution = self._resolve_llm(...)`
- **第 458 行** `if not isinstance(resolution, LLMPort):`
- **第 459 行** `self._raise_resolution_error(resolution)`

- **第 461 行** `cached = await self._cache.get(request, version=self._prompt_version)`
- **第 462 行** `if cached is not None:`
- **第 463 行** `yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=cached.content or "")`
  命中吐 TEXT_DELTA。
- **第 464 行** `yield StreamEvent(kind=StreamEventKind.USAGE, data=cached.usage)`
  吐 USAGE。
- **第 465 行** `yield StreamEvent(kind=StreamEventKind.DONE)`
  吐 DONE。
- **第 466 行** `return`

- **第 468 行** `# P2b：事件流同样调用前预留、流末结算（硬闸三路径一致）。`
- **第 469 行** `reservation = None`
- **第 470 行** `if self._cost_guard is not None:  # 与 complete/stream 同闸：预留失败即拒流，不建立事件源`
- **第 471 行** `reservation = self._cost_guard.reserve(self._cost_estimator(request))`
- **第 472 行** `if reservation is None:`
- **第 473 行** `raise ValueError("cost budget exceeded (CostGuard) before stream_events")`

- **第 475 行** `chunks: list[str] = []`
  累积文本。
- **第 476 行** `settled: Usage | None = None`
  USAGE 事件里结算依据。
- **第 477 行** `llm = resolution`
- **第 478 行** `try:  # 同 stream：仅正常流末结算；异常/取消取消预留，绝不留半途账单`
- **第 479-480 行** 注释
  `# 后端有 stream_events 直接用；旧后端用帮助函数把文本流包成事件流。` / `# 规则 6 例外：stream_events 是可选能力探测（具名固定字符串），不在 LLMPort 强制协议内。`
- **第 481 行** `if has_stream_events(llm):  # 能力探测决定事件源：有则用原生事件，旧后端走文本包装兜底`
- **第 482 行** `stream_events_fn = getattr(llm, "stream_events")  # noqa: B009`
  取原生事件方法（`# noqa: B009` 抑制 linter 对 getattr 固定名的告警）。
- **第 483 行** `source = stream_events_fn(request, ctx)`
- **第 484 行** `else:`
- **第 485 行** `source = wrap_text_stream(llm, request, ctx)`
  旧后端走包装。
- **第 486 行** `async for event in source:  # 透传同时旁路累积文本与 usage，供流末兜底结算/写缓存`
- **第 487 行** `if event.kind is StreamEventKind.TEXT_DELTA and isinstance(event.data, str):  # 只收文本块拼完整响应；守卫挡住畸形 data`
  - 用 `is` 比较枚举 + isinstance 守卫 data 类型。
- **第 488 行** `chunks.append(event.data)`
- **第 489 行** `elif event.kind is StreamEventKind.USAGE and isinstance(event.data, Usage):  # USAGE 优先作结算依据；守卫过滤同 kind 的坏负载`
- **第 490 行** `settled = event.data`
- **第 491 行** `yield event  # 原样透传，包含工具/handoff/done`
  所有事件原样透传（不只文本）。

- **第 493 行** `# 结算：优先事件里的 USAGE，其次后端 last_usage；两者都无则拒绝（不记假成本）。`
- **第 494 行** `usage = settled or cast(LLMPortWithUsage, llm).last_usage  # 断流可能收不到 USAGE 事件，靠 last_usage 兜底，绝不记假零成本`
  **结算依据优先级**：USAGE 事件优先 → last_usage 兜底。
- **第 495 行** `if usage is None:`
- **第 496-499 行** 两者都无抛错
  - `raise ValueError("LLMPort.stream_events() finished without USAGE or last_usage; cost cannot be settled (fake/zero-cost accounting forbidden)")`
- **第 500 行** `full = LLMResponse(content="".join(chunks), usage=usage)`
- **第 501 行** `self._record_cost(full, ctx, reservation=reservation)`
- **第 502 行** `reservation = None`
- **第 503 行** `await self._cache.put(request, full, version=self._prompt_version)`
- **第 504 行** `except BaseException:  # 断流/取消：释放预留；不结算不写缓存，避免半途账单与污染条目`
- **第 505-506 行** 取消预留。
- **第 507 行** `raise`

### 第 509-536 行：_record_cost（成本记录 + 结清）

- **第 509-510 行** 签名
  `def _record_cost(self, response: LLMResponse, ctx: PortCtx, *, reservation: Reservation | None = None) -> None:`
- **第 511 行** `"""从响应的 usage 记录成本（不自行估算），结清成本硬闸。"""`
- **第 512-518 行** docstring
  说明：cost 来自 response.usage；agent_id/user_id 由 coerce_port_ctx 具名视图提供（规则 6）；有 reservation 走 commit，无则走旧 allow/charge 兼容路径。

- **第 520 行** `cost = float(response.usage.cost_usd)`
  成本取自响应 usage（不估算）。

- **第 521 行** `if reservation is not None and self._cost_guard is not None:  # 有预留走 commit 按实结清；差额退还/补扣，不按预估死扣`
- **第 522 行** `# P2b：按实际成本结清预留（差额退还/补扣）。`
- **第 523 行** `self._cost_guard.commit(reservation, cost)`
  commit 按实结清。

- **第 524 行** `elif self._cost_guard is not None:`
  无预留但有硬闸（兼容路径）。

- **第 525 行** `# 兼容路径（无预留）：旧 allow/charge 事后语义。`
- **第 526 行** `if not self._cost_guard.allow(cost):`
- **第 527 行** `raise ValueError("cost budget exceeded (CostGuard)")`
  超预算抛错（旧路径的事后检查）。
- **第 528 行** `self._cost_guard.charge(cost)`
  事后扣减。

- **第 529 行** `view = coerce_port_ctx(ctx)  # 唯一 PortCtx 动态边界（规则 6）`
  取 agent_id/user_id 的具名视图。

- **第 530-536 行** `self.ledger.record(CostRecord(...))`
  - 第 531 行 `run_id=ctx.run_id`
  - 第 532 行 `agent_id=view.agent_id`
  - 第 533 行 `user_id=view.user_id`
  - 第 534 行 `model=response.model`
  - 第 535 行 `usage=response.usage`
  - 记入成本账本，完成多维归因。

### 第 538-574 行：_complete_with_repair（修复循环）

- **第 538-545 行** 签名
  `async def _complete_with_repair(self, response, request, ctx, *, llm: LLMPortWithUsage | None = None) -> LLMResponse:`

- **第 546 行** `"""异步修复循环：最多 max_repair 次，每次把校验错误附给模型。"""`
- **第 548 行** `llm: 本次调用已解析的后端（缺省用默认，兼容直接调用）。`

- **第 550 行** `assert self._validator is not None`
  断言校验器存在（构造时 spec 非空才创建，这里必然非空）。

- **第 551 行** `assert self._spec is not None  # validator 存在时 spec 必存在（构造时同源创建）`
  断言 spec 存在，注释解释原因。

- **第 552 行** `spec = self._spec`
- **第 553 行** `current = response`
  当前响应（初始是后端首次响应）。

- **第 554 行** `for _ in range(self._validator.max_repair + 1):  # +1 是首次校验，其后最多 max_repair 次修复，防止无限重试烧成本`
  **循环次数**：`max_repair + 1` 次校验 = 1 次首次校验 + 最多 max_repair 次修复。
  - 例：max_repair=2 → 循环 3 次：首次校验、修复1、修复2。
  - **防止无限重试烧成本**：修不好就停。

- **第 555 行** `# 走 validator 持有的校验器（默认或注入），保证"校验"与"修复"同一口径。`
- **第 556 行** `errors = self._validator.check(current.content or "")`
  校验当前输出。

- **第 557 行** `if not errors:  # 一旦合法立即返回，避免对已合规输出再发起一次无谓修复调用`
- **第 558 行** `return current`
  合法立即返回，不多调一次后端。

- **第 559 行** `repair_prompt = self._validator.build_repair_prompt(current.content or "", errors)`
  生成修复提示（含原输出 + 错误）。

- **第 560 行** `# 统一复用 R1 Message 四字段具名结构（规则 4）：消除仅两字段的重复 ChatMessage。`
- **第 561 行** `repair_message = Message(role="system", content=repair_prompt)`
  用 R1 Message 构造 system 消息（不另建简化 ChatMessage DTO）。

- **第 562-565 行** 构造修复请求
  - 第 563 行 `messages=[*request.messages, repair_message.to_dict()],`：原 messages + 修复提示（解包原列表 + 追加）。
  - 第 564 行 `model=request.model,`
  - 第 562 行 `patched = LLMRequest(`

- **第 566 行** `current = await (llm or self._llm).complete(patched, ctx)`
  用本次解析的后端（或默认）调修复请求。

- **第 567 行** `self._record_cost(current, ctx)`
  **每次修复也记账**（无 reservation，走兼容路径）。

- **第 568 行** `# 超次：strict 模式拒答（N3：折叠为 SCHEMA_INVALID error 响应，不抛穿成 PORT_CRASHED）。`
- **第 569 行** `if spec.strict:`
- **第 570-573 行** 超次返回错误响应
  - `content="", tool_calls=[], usage=current.usage, model=current.model, error="SCHEMA_INVALID: repair exhausted"`
  - strict 模式修不好就折叠成 SCHEMA_INVALID 错误响应（N3：不抛穿）。

- **第 574 行** `return current`
  非 strict 返回最后一次响应（可能仍非法）。
  - **strict vs 非 strict**：strict 严守红线（非法绝不标合法）；非 strict 放行最后一次（宽松场景，调用方自行判断）。

**gateway 小结**：ModelGateway 用 `_resolve_llm` 单点做降级决策（拒答优先、router 总调用、未注册显式失败）；`complete` 用 singleflight 防击穿、reserve/commit/cancel 管成本、非法输出不缓存、strict 折叠 error；`stream`/`stream_events` 流末必须有 usage 才结算（不记假零成本）、三路径成本纪律一致、能力探测兼容旧后端；`as_call`/`as_dispatch` 双入口归一到 `_center`。

---

## 9. capability.py 逐行讲解（118 行）

模型能力包 `ModelCapability`：把 llm/router/cache 注册进共用 Provider 注册表，并持有同一份 `ModelRegistry` 供降级目标切换。这是 model 模块的**装配入口**——前面 8 个文件讲「能力怎么实现」，本章讲「能力怎么被装进框架」。

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R4 能力装配：ModelCapability 构造网关并持有共用 ModelRegistry。`
  一句话定位：能力装配 + 共用注册表持有者。

- **第 3 行** `降级目标后端通过 add_backend 注册到同一 ModelRegistry，供网关按 typed 目标切换；`
  点明 `add_backend` 的用途：注册备用后端，网关按 typed 目标（来自可靠性执行器的 routing_target）切换。

- **第 4 行** `容器/网关/能力共用这份注册表（P6 两表统一，载体换为共用 Registry）。`
  「两表统一」：provider 表与端点表收敛到一份共用 Registry，避免多处状态不一致。

- **第 5 行** `"""` docstring 结束。

### 第 6-21 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `from compose.tool_layer import layer_program`
  引入外壳 Program 工厂（build 方法用）。

- **第 9 行** `from compose import LifecycleBus, Registry`
  - `LifecycleBus`：通知总线，透传给网关发 before_model/after_model。
  - `Registry`：共用 Provider 注册表。

- **第 10 行** `from runtime import CallFn, Program`
  `CallFn` 用于 dispatch 返回类型，`Program` 用于 build 返回类型。

- **第 12 行** `from .ports import LLMPortWithUsage`
  导入带 usage 的 LLM 后端协议（必填依赖类型）。

- **第 14-19 行** 从包内各模块导入默认实现：`ExactCache`/`CachePort`、`ModelGateway`、`InMemoryModelRegistry`/`ModelRegistry`、`RuleRouter`、`SchemaValidator`、`CostGuard`/`StructuredOutputSpec`。
  这些是装配时「缺省可替换」策略的默认选项。

- **第 21 行** `__all__ = ["ModelCapability"]`
  包级只导出能力包一个类。

### 第 24-31 行：类定义与元数据

- **第 24 行** `class ModelCapability:`
  能力包类。

- **第 25-29 行** docstring：`模型能力包：把 llm/router/cache 注册进共用 Provider 注册表。属性 name/version/requires: 能力级元数据（R13 Capability 契约）。`

- **第 30 行** `name = "model"`
  能力名（R13 Capability 三要素之一）。

- **第 31 行** `requires: tuple[str, ...] = ()`
  依赖的其他能力列表，空表示无前置依赖。

### 第 33-59 行：构造函数

- **第 33-44 行** 签名：除 `llm` 必填外，全是可选关键字参数——`router`、`cache`、`spec`、`validator`、`registry`、`bus`、`cost_guard`。
  这是「协议可注入 + 开箱默认」纪律的体现：只强制真正必需的后端，其余策略都可替换。

- **第 45-49 行** docstring。第 48 行特别说明：`cost_guard: 成本硬闸；透传给 gateway 使生产链路可启用（默认 None 不限制）。`

- **第 50-56 行** 逐个保存字段：`_llm`、`_router`、`_cache`、`_spec`、`_validator`、`_bus`、`_cost_guard`。
  注意这里**只保存不构造网关**——网关延迟到 `gateway()` 方法调用时才建，避免装配期就固化依赖。

- **第 57 行** 注释：`# 默认建 ModelRegistry 并注册 "primary"，让降级目标可解析。`

- **第 58 行** `self._model_registry = registry or InMemoryModelRegistry()`
  传了注册表用传的，否则建内存默认。用 `or` 而非 `if not None`：空注册表对象为真值，不会被误替换。

- **第 59 行** `self._model_registry.register_llm("primary", llm)`
  **关键行**：把默认后端注册为 `"primary"`。这样当降级执行器写入 `routing_target="primary"` 时，网关能解析到真实后端——否则降级目标无法落地（对应网关 `_resolve_llm` 的 `resolve_llm` 调用）。

### 第 61-67 行：build()

- **第 61 行** `def build(self) -> Program:`
- **第 62-66 行** docstring：`ToolLayer 外壳（规格 P4）：产出"用模型"的 Program 工厂。内部路由/缓存/成本防护不变（由 gateway 消费），这里只给 R1 一个领域无知的 Program（yield Invoke(port="llm") + Terminate）。`
- **第 67 行** `return layer_program("llm", payload={"layer": "model"})()`
  port 名是 `"llm"`，payload 带 layer 标记。这条外壳 Program 与 tools 的 `"tool"`、rag 的 `"rag"` 同构——R1 只认 Program，零领域感知。

### 第 69-86 行：install()

- **第 69 行** `def install(self, container: object | None = None, reg: Registry | None = None) -> Registry:`
  统一装配入口，返回共用注册表（可链式）。

- **第 70-77 行** docstring。第 73 行说明 `container` 的用途：若给定且有 registry 属性，指向同一份注册表（两表统一）。

- **第 78-79 行** `if reg is not None: self._model_registry = reg`
  传了外部注册表就替换本能力持有的实例。`# type: ignore[assignment]` 是因为 `Registry` 与 `ModelRegistry` 类型不完全兼容，运行时是同一份对象。

- **第 80 行** `registry = self._model_registry`
  局部变量指向最终注册表。

- **第 81-83 行** 注册三个 Provider：
  - `registry.register("llm", self._llm)`：默认后端。
  - `registry.register("model_router", self._router or RuleRouter())`：路由策略，缺省规则路由。
  - `registry.register("model_cache", self._cache or ExactCache())`：缓存，缺省精确缓存。
  - 注意用 `or` 提供默认：传 None 或假值都用默认实现。

- **第 84-85 行** `if container is not None and hasattr(container, "registry"): setattr(container, "registry", registry)`
  两表统一的落地：把容器的 registry 属性指向同一份实例，确保容器/网关/能力共享状态。`hasattr` 守卫避免给任意对象乱设属性。

- **第 86 行** `return registry`
  返回共用注册表，支持链式装配。

### 第 88-96 行：两个属性

- **第 88-91 行** `llm` 属性：返回默认后端实例（装配核对用）。

- **第 93-96 行** `registry` 属性：返回本能力持有的注册表（装配核对用）。

### 第 98-105 行：gateway()

- **第 98 行** `def gateway(self) -> ModelGateway:`
- **第 99 行** docstring：`构造模型网关（传入共用 registry 与通知总线）。`
- **第 100-105 行** 构造 `ModelGateway`，把前面保存的所有字段透传：`llm`、`router`、`cache`、`spec`、`validator`、`registry`、`bus`、`cost_guard`。
  这是前面 8 个文件的「总装」——gateway 的内部逻辑（降级决策、缓存、成本、校验）在这里被接上真实后端与策略。

### 第 107-114 行：dispatch()

- **第 107 行** `def dispatch(self) -> CallFn:`
- **第 108-113 行** docstring：`P4 dispatch 入口（§2.5 形态）：直接吃 (effect, ctx)，不构造 Invocation。模型能力只有 llm 一个 port，故 gateway.as_dispatch() 本身即为该 port 的 dispatch 实现；组合层可直接注入 R1 Runner（不经可靠性执行器），验证"call 按 port 分派 + typed target 从 ctx.meta 读"主线。`
- **第 114 行** `return self.gateway().as_dispatch()`
  直接返回网关的 dispatch 形态。注意每次调用都新建一个 gateway——dispatch 是装配期动作，只调一次，不会在运行期反复构造。

### 第 116-118 行：add_backend()

- **第 116 行** `def add_backend(self, name: str, llm: LLMPortWithUsage) -> None:`
- **第 117 行** docstring：`注册降级目标后端（如 "backup"），供网关按 typed 目标切换。`
- **第 118 行** `self._model_registry.register_llm(name, llm)`
  把备用后端注册进同一份注册表。配合构造函数里注册的 `"primary"`，形成完整的降级目标集合：primary → backup → ...。网关的 `_resolve_llm` 会按 typed 目标名 `resolve_llm`，解析不到就返回 `TargetUnresolved`（绝不静默回退）。

### capability.py 小结

`ModelCapability` 是 model 模块的**装配门面**：
- `build()` 给 R1 一个外壳 Program。
- `install()` 把 llm/router/cache 注册进共用 Registry，并让容器/网关/能力共享同一份。
- `gateway()` 总装出真正的 ModelGateway。
- `dispatch()` 给出符合 R1 CallFn 签名的调用入口。
- `add_backend()` 注册降级目标，补齐全链路。

---

## 10. __init__.py 逐行讲解（70 行）

包级 API 收敛：外部只需 `from harness.model import XXX`，不必知道内部文件路径。

### 第 1-6 行：docstring 与 future import

- **第 1 行** `"""R4 模型与推理能力：多 Provider / 模型路由 / 缓存 / 结构化输出 / 成本归因。`
  一句话列出包能力。

- **第 2-4 行** docstring 正文
  - 第 3 行：全部基于 R1（runtime）+ 组合层 compose（复用其 DTO）构建。
  - 第 4 行：公共 API 见详设文档 §4。

- **第 5 行** `"""` 结束。

- **第 6 行** `from __future__ import annotations` 延迟注解。

### 第 8-11 行：从 cache 导入

- **第 8 行** `from .cache import (`
- **第 9 行** `CacheEmbedder, CachePort, CacheStore, DEFAULT_CACHE_EMBED_DIM,`
- **第 10 行** `ExactCache, HashCacheEmbedder, InMemoryCacheStore, SimilarityCache,`
- **第 11 行** `)`
  从 cache 重导出 8 个符号：三协议、两常量、四实现（注意 MAX_EMBED_CHARS 没在这里导出——它是内部窗口常量）。

### 第 12 行：从 capability 导入

- **第 12 行** `from .capability import ModelCapability`
  从兄弟文件 `capability.py`（不在本册 9 文件清单内）重导出 `ModelCapability`——装配层能力包，负责构造网关、持有共用 ModelRegistry、注册 primary 后端、产出 dispatch。它的存在让「装配」与「主链路」解耦。

### 第 13 行：从 gateway 导入

- **第 13 行** `from .gateway import ModelGateway`
  唯一主链路入口。

### 第 14 行：从 registry 导入

- **第 14 行** `from .registry import InMemoryModelRegistry, ModelEndpoint, ModelRegistry`
  注册表三件套（DTO + 协议 + 默认实现）。

### 第 15 行：从 router 导入

- **第 15 行** `from .router import ModelRouter, RuleRouter`
  路由协议 + 默认实现。注意 `NO_DEFAULT` 没在这里导出（内部哨兵）。

### 第 16-19 行：从 structured 导入

- **第 16 行** `from .structured import (`
- **第 17 行** `MinimalSchemaValidator, SchemaValidator,`
- **第 18 行** `StructuredOutputValidator, validate_json_schema,`
- **第 19 行** `)`
  校验器协议 + 默认实现 + 门面 + 模块级校验函数。

### 第 20-34 行：从 types 导入

- **第 20 行** `from .types import (`
- **第 21-33 行** 14 个符号：BackendResolution、CacheKey、CostAttributor、CostGuard、CostRecord、RefuseExecution、Reservation、RouteDecision、StructuredOutputSpec、TargetUnresolved、UsageLedger、build_cache_key、cache_key_string。
- **第 34 行** `)`
  把地基类型全部重导出。

> 注意：`ports.py` 的 `LLMPortWithUsage` **没有**从这里导出——它是内部扩展协议，装配层直接 `from harness.model.ports import LLMPortWithUsage`（见 capability.py 第 12 行）。

### 第 36-70 行：__all__（公共导出面）

- **第 36 行** `__all__ = [`
- **第 37-69 行** 公共符号清单，分组：
  - 地基类型（BackendResolution/CacheKey/CostAttributor/CostGuard/CostRecord/RefuseExecution/TargetUnresolved/Reservation/RouteDecision/UsageLedger/StructuredOutputSpec）。
  - 缓存（CachePort/CacheStore/InMemoryCacheStore/CacheEmbedder/HashCacheEmbedder/DEFAULT_CACHE_EMBED_DIM/ExactCache/SimilarityCache）。
  - 端点注册表（ModelEndpoint/ModelRegistry/InMemoryModelRegistry）。
  - 装配（ModelCapability）。
  - 主链路（ModelGateway）。
  - 路由（ModelRouter/RuleRouter）。
  - 结构化（SchemaValidator/MinimalSchemaValidator/StructuredOutputValidator/validate_json_schema）。
  - 工具函数（build_cache_key/cache_key_string）。
- **第 70 行** `]`

> **重复项提示**：第 54 行和第 60 行都有 `"RouteDecision"`（`__all__` 里重复列了两次）。Python 对 `__all__` 重复项不报错（`from ... import *` 只会导入一次），不影响功能，属于清单整理的小瑕疵。这里如实记录源码现状。

---

## 11. 逐行讲解后的整体心智图

```
                        ┌─────────────────────────────────────┐
                        │  as_call()      as_dispatch()        │  ← 两个适配入口
                        │  (Invocation)   (effect, ctx)        │
                        └──────────┬──────────────┬────────────┘
                                   │              │
                              _extract_target (typed RoutingTarget)
                                   └──────┬───────┘
                                          ▼
                                      _center  ── BEFORE/AFTER_MODEL 通知
                                          │
                                          ▼
                                    complete(request, ctx)
                                          │
        ┌─────────────────────────────────┼──────────────────────────────────┐
        ▼                                 ▼                                  ▼
  _resolve_llm                      cache.get                        singleflight
  (唯一决策点)                      命中即短路                        (key→Future)
        │                                 │                                  │
   ┌────┼────┐                             │                         owner: _complete_miss
   ▼    ▼    ▼                             │                         等待者: shield(fut)
LLMPort Refuse Unresolved                  │                                  │
   │    (拒答) (未注册)                     │                                  ▼
   │     └─────┴──→ 折叠 error 响应         │                        reserve → llm.complete
   │                                        │                          → 校验/修复(repair)
   └────────────────────────────────────────┘                          → commit → 写缓存
                                                                              │
                                                                    异常/取消 → cancel

  三条路径统一纪律：调用前 reserve，正常 commit，异常 cancel，流末必须有 usage（不记假零成本）

  stream / stream_events：
    - 拒答优先（抛稳定码错误）
    - 缓存命中吐缓存 + USAGE + DONE
    - 能力探测 has_stream_events → 原生事件 or wrap_text_stream 包装
    - 结算依据：settled(USAGE) > last_usage，都无则抛错

  三层缓存：CachePort(策略) → CacheStore(存储) → CacheEmbedder(向量)
    - ExactCache：sha256 精确匹配
    - SimilarityCache：遍历全部候选 → 全局最优 → 遍历后判阈值；失效删所有达阈值条目
    - HashCacheEmbedder：31 进制确定性哈希(不用内置 hash()) + 二值化 + 512 维 + 头尾窗口

  成本：CostGuard reserve/commit/cancel（凭证幂等 + _reserved 计入 allow 防并发击穿）
        UsageLedger/CostAttributor 按 run/agent/user/model 四维归因
```

---

## 12. 细节速记卡（逐行精华）

**降级决策**
- **拒答优先于缓存**：REFUSE 三态不读缓存、不调后端（gateway 第 288-291 行）。
- **router 总是被调用**：修复历史空转，路由理由进 `ctx.meta["model_route"]`（第 161-165 行）。
- **未注册目标显式失败**：`TargetUnresolved`，绝不静默回退默认（第 171-174 行）。
- **唯一下沉情形**：target 为 None（router 未指定）才走默认后端（第 168-170 行）。
- **稳定错误码集中常量**：REFUSED / TARGET_UNRESOLVED，三态折叠与响应前缀同源（types 第 38-39 行）。
- **终止态两折叠**：complete 用 `_resolution_error_response` 返回响应；流路径用 `_raise_resolution_error` 抛异常（第 177-194 行）。

**singleflight（防击穿）**
- **owner 独占调用**：首个 miss 建 Future，set_result/set_exception（第 301-319 行）。
- **等待者用 shield**：被取消不影响 owner（第 321 行）。
- **异常广播**：set_exception 给所有等待者，防各自重试再次击穿（第 313-317 行）。
- **幂等防御**：`if not inflight.done()` 再 set_exception，防 InvalidStateError 淹没原异常（第 315 行）。
- **异常消费**：done_callback 读 `f.exception()`，防 "never retrieved" 警告（第 306-308 行）。
- **finally pop**：无论成败都清理在途表（第 318-319 行）。

**成本**
- **reserve/commit/cancel 三路径一致**：complete/stream/stream_events 都调用前预留、正常结清、异常释放。
- **预算不足调用前就拒**：reserve 返回 None → 直接 COST_LIMIT 错误，绝不发后端请求（第 335-340 行）。
- **commit 差额退补**：预留是占位不是死扣，按实际成本结清（types 第 265-277 行）。
- **cancel 不记账**：调用失败释放预留，不碰 spent（types 第 279-284 行）。
- **凭证幂等**：id + `_active` 表防重复结清（types 第 273-274、281-282 行）。
- **`_reserved` 计入 allow**：并发硬闸核心——先调用后扣费会让 20 并发全通过（types 第 243 行）。
- **成本不估算**：只记 `response.usage.cost_usd`；估算仅用于预留（第 520 行）。
- **round(10)**：浮点累加每步截断，避免误差影响预算判断（types 第 167、173 行）。
- **每次修复也记账**：修复循环里 `_record_cost`（第 567 行）。

**缓存**
- **非法输出不写缓存**：有 error 的响应不 put，防污染后续请求（第 372-374 行）。
- **命中即短路**：缓存命中不再调后端、不再记账（第 293-295 行）。
- **cache_key 含 prompt_version + 稳定 JSON**：`sort_keys=True` 保证确定性，sha256 前 32 位（types 第 114-122 行）。
- **任何影响输出的参数都进键**：messages/model/temperature/max_tokens/version（types 第 114-120 行）。
- **embedder 不用内置 `hash()`**：进程随机化破坏跨进程确定性，用 31 进制多项式哈希（cache 第 205-210 行）。
- **向量二值化**：桶出现置 1.0 不累加频次，防长文本各桶计数趋同、向量收敛（cache 第 233-234 行）。
- **512 维 + 512 字符头尾窗口**：抗收敛三件套（cache 第 42-44、227-230 行）。
- **空文本零向量**：防两个空 query 余弦=1 误命中（cache 第 223-224 行）。
- **语义缓存阈值遍历后判**：命中必须是全局最优，不是先到先得（cache 第 413、424 行）。
- **语义失效删所有达阈值条目**：不只删第一个（cache 第 452-461 行）。
- **embedder 哨兵区分未传/显式 None**：未传用 HashCacheEmbedder，显式 None 降级 difflib（cache 第 242-250、389-392 行）。
- **双方有向量才走余弦**：否则降级 SequenceMatcher（cache 第 417-420 行）。
- **TTL 懒删除**：get/scan 时检查过期，不做后台定时器（cache 第 115-123、139-147 行）。

**结构化输出**
- **strict 校验失败折叠 error 不抛穿**：SCHEMA_INVALID 响应，不变成 PORT_CRASHED（N3，第 358-366 行）。
- **修复循环最多 max_repair 次修复**：`range(max_repair + 1)` 含首次校验，防无限烧成本（第 554 行）。
- **合法立即返回**：不多调一次无谓修复（第 557-558 行）。
- **校验与修复同口径**：`check` 与 `build_repair_prompt` 用同一校验器（structured 第 240-253 行）。
- **N4 坏 schema 不静默跳过**：schema 声明 type 但值非法必须报错，否则非法输出被当合法（structured 第 133-137 行）。
- **bool 不是 number/integer**：类型检查显式排除 bool（`isinstance(v, bool)`）（structured 第 168-169 行）。
- **required 一次报全缺失项**：不 break，便于修复提示一次列全（structured 第 147-149 行）。
- **履约校验四要素**：协议成员齐备 + 具名非空，不满足显式拒绝（structured 第 198-202 行）。

**流式**
- **流末必须有 usage 不记假零成本**：缺 usage 抛错拒绝服务（第 423-428、494-499 行）。
- **USAGE 事件优先于 last_usage**：两者都无才抛错（第 494 行）。
- **流式禁结构化校验**：无法校验半截流（第 392 行）。
- **能力探测兼容旧后端**：`has_stream_events` 用 `callable(getattr(...))`，不污染 LLMPort 协议（stream 第 16-19 行）。
- **包装不缓存整段**：逐块转发保低延迟（stream 第 41-42 行）。
- **缓存命中吐 USAGE/DONE 事件**：事件流命中吐三件套（第 462-466 行）。

**路由与注册表**
- **降级目标装配期验注册**：RuleRouter `_validate_targets`，未注册显式报错（router 第 115-128 行）。
- **成本约束只在有候选时改选**：预算内无候选保留 task_hint 结果（router 第 148 行）。
- **决策真改变才更新 reason**：保证 trace 与实际一致（router 第 150 行）。
- **bool 排除**：max_cost 读 meta 时排除 bool（bool 是 int 子类）（router 第 78 行）。
- **同名同实例幂等 / 异实例拒绝**：registry 用 `is` 身份比较（registry 第 100-116 行）。
- **降级目标未注册显式失败**：resolve_llm 返回 None → TargetUnresolved（registry 第 125-127 行 + gateway 第 171-174 行）。
- **三张表**：endpoints（连接信息）/ llms（实例）/ providers（装配资源），resolve provider 优先（registry 第 118-123 行）。

**易错小细节**
- **__all__ 里 RouteDecision 重复两次**：`__init__.py` 第 54、60 行，不影响功能但属清单瑕疵。
- **LLMPortWithUsage 未从包级导出**：需从 `harness.model.ports` 直接导入。
- **cache_key_string 局部 import hashlib**：只在算键时需要（types 第 112 行）。
- **wrap_text_stream 分两层**：普通函数返回 `_wrap` 异步生成器（stream 第 31、34 行）。

---

*下一册：[04 rag 检索增强模块](./04-rag检索增强模块.md)。可靠性执行器如何调用本模块的网关见 [06](./06-resilience可靠性模块.md)。装配门面 `ModelCapability` 已在本册第 9 章逐行讲清。*
