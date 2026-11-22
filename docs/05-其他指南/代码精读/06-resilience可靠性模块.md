# 代码精读 06 · resilience 可靠性模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/resilience/` 全部 8 个文件：types.py（445 行）+ defenses.py（354 行）+ breaker.py（217 行）+ retry.py（96 行）+ timeout.py（49 行）+ executor.py（290 行）+ capability.py（111 行）+ `__init__.py`（49 行）。
> 总入口见 [00](./00-总入口与全局链路.md)，它包裹的网关见 [03 model](./03-model模型模块.md)。

---

## 0. 读前GPS

这个模块是六个模块里**最绕的一个**，但拆开后其实只有一个核心执行器 + 九种领域策略。先记住一句话定位：

> **ReliabilityExecutor 用显式代码，按可靠性工程语义串联九种领域策略，每种策略只暴露自己的领域方法。**

**它没有 AOP 角色**：没有角色协议、没有角色枚举、没有槽位列表、没有统一方法名契约。`KillSwitch.is_tripped`、`RateLimit.acquire`、`CircuitBreaker.permit`、`Degradation.select` 各叫各的名字，执行器用显式代码把它们按语义串起来。这是理解全模块的钥匙——别去找「拦截器链」，那里什么都没有。

全模块核心类（按阅读顺序）：

- `types.py`：`FailureClass`/`Jitter`/`backoff_delay`/`DegradeLadder`/`BreakerState` —— 失败分类、退避、降级阶梯、熔断状态的**共享数据类型**。
- `defenses.py`：`Degradation`/`RateLimit`/`Bulkhead`/`Idempotency`/`Watchdog`/`KillSwitch` —— 六道防线（降级选级 + 五种前置/隔离策略）。
- `breaker.py`：`CircuitBreaker` —— 三态熔断器。
- `retry.py`：`RetryPolicy` —— 重试策略。
- `timeout.py`：`Timeout` —— 超时策略。
- `executor.py`：`ReliabilityExecutor` —— **核心编排器**，把上面所有策略串成一次可靠调用。
- `capability.py`：`ResilienceCapability` —— 能力装配，opt-in 构造注入。
- `__init__.py`：包级统一导出。

贯穿全模块的**五条纪律**（读每行时都回想它们）：

1. **全 opt-in**：九种策略全部构造注入，传实例才参与，`None` 即缺席——不写空实现占位。
2. **四坑**（历史 bug 修复）：B1 session 隔离、B2 failed_target 是注册名、R3-6 永久剔除、R3-7 fault 必带 failed_target。
3. **B3 meta 同源**：前置策略写的 fault/target 与网关读的是同一份 meta，重路由才不失效。
4. **失败分类共用**：一次分类同时给 retryable 与 count_toward_breaker，避免重试插件与熔断插件语义漂移；429 不熔断。
5. **时钟/随机显式注入**：墙钟、随机源不隐式进入，保证可测试、可回放。

---

## 1. types.py 逐行讲解（445 行）

### 第 1-11 行：模块 docstring

- **第 1 行** `"""R3 可靠性共享类型：失败分类表 / 维度键 / 退避算法 / 降级阶梯 / 熔断状态。`
  一句话定位：这个文件是**纯共享类型层**，不干任何 IO，只定义数据和纯函数。归属 R3（可靠性域）。

- **第 3 行** `职责（R3 §4/§5.5）：`
  下面列三条职责。

- **第 4-5 行** `失败分类是重试与熔断的共享设施：一次分类同时给出 retryable 与 count_toward_breaker，避免重试插件与熔断插件各自解释错误码导致语义漂移。`
  **设计要点「失败分类共用」**：这是全文件最重要的存在理由。如果重试和熔断各自写一套 `if code == "429"`，迟早一边改了另一边没改，语义就漂移了。共用一次分类结果，两边永远一致。

- **第 6 行** `退避算法为纯函数（随机源显式注入），墙钟/随机不隐式进入，保证可测试、可回放。`
  强调退避是纯函数：随机源 `rng` 由调用方传入，不在函数内部 `random.random()`。这样测试可以传固定种子的 `Random`，结果可复现。

- **第 7 行** `降级阶梯装配期强校验（无环、末级必须拒答、quality 严格递减），防止编造与死循环。`
  提前预告三条强校验，后面 `validate_ladder` 会逐行讲。

- **第 9 行** `分层纪律：只依赖 R1（runtime）与 R2（hooks），不 import 任何 IO SDK（编码规则 8）。`
  依赖方向纪律：harness 只依赖内层 runtime/compose，不碰任何具体 IO SDK（如 redis、requests）。

- **第 10 行** `公共 API 见 docs/02-模块详设/Agent/R3-可靠性.md §4。`
  指路：详设文档位置。

- **第 11 行** `"""` docstring 结束。

### 第 12-22 行：imports 与导出

- **第 12 行** `from __future__ import annotations`
  延迟求值注解，允许在类型注解里引用尚未定义的类型（如 `Jitter | str` 里提前引用），降低 import 顺序耦合。

- **第 14 行** `import re`
  为 `_HTTP_5XX` 正则匹配服务。

- **第 15 行** `from collections.abc import Mapping`
  类型注解用：声明分类表是只读映射。

- **第 16 行** `from collections.abc import Set as AbstractSet`
  引入抽象集合类型，让 `DegradeLadder.pick` 的参数能接受 frozenset/set 等任意集合，而非绑死具体 set。

- **第 17 行** `from dataclasses import dataclass, field`
  `dataclass` 定义不可变数据类；`field` 给 `BreakerState.window` 提供默认工厂。

- **第 18 行** `from enum import Enum`
  定义 `Jitter`/`DegradeMode`/`BreakerStatus` 三个枚举。

- **第 19 行** `from random import Random`
  退避函数的随机源类型（显式注入，不隐式 new）。

- **第 20 行** `from typing import Protocol, runtime_checkable`
  定义可插拔分类器协议 `FailureClassifier`。

- **第 22 行** `from runtime import Result`
  导入 R1 的结果类型。注意依赖方向：harness 依赖 runtime，不反向。

- **第 24-41 行** `__all__ = [...]`
  导出 17 个符号：默认失败表、熔断状态/状态枚举、降级阶梯三件套、维度键、失败分类、分类器协议、抖动枚举、重试参数、退避/分类/归一化/校验函数。下面是各符号的详细定义。`__init__.py` 据此重导出。

### 第 44-60 行：RetrySpec 重试参数数据类

- **第 44 行** `@dataclass(frozen=True, slots=True)`
  两个装饰器：`frozen=True` 让实例**不可变**（创建后不能改字段），`slots=True` 去掉 `__dict__` 省内存、防误加新属性。领域参数对象应当是值语义、不可变。

- **第 45 行** `class RetrySpec:`
  重试参数（R3 自身持有的领域参数，不依赖 hooks）。注意它和 `RetryPolicy` 的区别：`RetrySpec` 是纯参数数据，`RetryPolicy` 是带行为、带注入依赖的策略对象。

- **第 46-53 行** docstring：列出七个字段的含义。

- **第 54 行** `max_attempts: int = 3`
  最大尝试次数，默认 3（首次 + 2 次重试）。

- **第 55 行** `base_delay: float = 0.5`
  初始退避 0.5 秒。

- **第 56 行** `multiplier: float = 2.0`
  退避倍数 2（指数退避：0.5, 1.0, 2.0...）。

- **第 57 行** `max_delay: float = 30.0`
  退避上限 30 秒，防止无限增大。

- **第 58 行** `jitter: str = "full"`
  抖动策略默认 `"full"`（全抖动最常用，防重试风暴）。注意这里类型是 `str` 而非 `Jitter`——跨层传参时是字符串，用 `Jitter.coerce` 归一。

- **第 59 行** `respect_retry_after: bool = True`
  默认遵守服务端 Retry-After 头。

- **第 60 行** `idempotency_required: bool = True`
  默认要求幂等键（副作用重试红线）。

### 第 63-92 行：Jitter 抖动枚举

- **第 63 行** `class Jitter(str, Enum):`
  继承 `str, Enum` 双基类：既是枚举（有限取值，规则 5），又能当字符串用。

- **第 64-71 行** docstring：列四个成员与算法。

- **第 73 行** `NONE = "none"`
  无抖动：固定退避。`base_delay=0` 时装配期禁用（防风暴）。

- **第 74 行** `FULL = "full"`
  全抖动：`uniform(0, raw)`，最常用。

- **第 75 行** `EQUAL = "equal"`
  等抖动：`raw/2 + uniform(0, raw/2)`。

- **第 76 行** `DECORRELATED = "decorrelated"`
  去相关抖动：`min(max, prev × uniform(1,3))`，AWS 推荐。

- **第 78-79 行** `@classmethod def coerce(cls, raw: Jitter | str) -> Jitter:`
  类方法：把字符串或枚举成员统一归一为枚举成员。为什么需要：RetrySpec.jitter 是 str，backoff_delay 里要用枚举比较，必须归一。

- **第 80-86 行** docstring：参数/返回说明。

- **第 87-88 行** `if isinstance(raw, cls): return raw`
  已是枚举成员则原样返回。

- **第 89-90 行** `try: return cls(raw)`
  尝试用字符串构造枚举（如 `"full"` → `Jitter.FULL`）。

- **第 91-92 行** `except ValueError: return cls.FULL`
  **关键防御**：未知字符串（拼错、乱配）不抛异常，而是**显式回退 FULL**（安全防风暴）。为什么不回退 NONE：NONE 无抖动会形成重试风暴，FULL 是最安全默认。这是「规则 5：未知值不静默改写语义，但要安全兜底」的落地。

### 第 95-97 行：失败分类小节注释

- **第 95-97 行** 分隔注释 `# --- 失败分类：共享设施，重试与熔断共用一次分类结果 ---`
  标记这一块是共享设施。

### 第 98-112 行：FailureClass 失败分类结论

- **第 98 行** `@dataclass(frozen=True, slots=True)`
  同样不可变 + 省内存。分类结论是值对象，一旦算出不该被改。

- **第 99 行** `class FailureClass:`
  一次失败的分类结论。

- **第 100-107 行** docstring：四字段说明。

- **第 109 行** `retryable: bool`
  是否可重试（瞬态错误为真）。给 retry 用。

- **第 110 行** `count_toward_breaker: bool`
  是否计入熔断滑窗。**429 等非宕机错误为 False**——这是「429 不熔断」的数据层落点。

- **第 111 行** `suggested_delay: float | None`
  服务端建议延迟（Retry-After 秒），None 表示无建议。

- **第 112 行** `code: str`
  归一化后的失败码（如 `"SERVER_ERROR"`）。

### 第 115-116 行：_Triple 类型别名

- **第 115 行** `type _Triple = tuple[bool, bool, str]`
  Python 3.12 `type` 语句定义类型别名：失败规则三元组。

- **第 116 行** docstring：`(retryable, count_toward_breaker, 归一化码)`。
  为什么用三元组：默认规则表里每条规则只需这三个值，紧凑表达。

### 第 118-133 行：_DEFAULT_RULES 默认分类表

- **第 119 行** `_DEFAULT_RULES: Mapping[str, _Triple] = {`
  归一化族 → 语义三元组的核心规则表。这是「失败分类共用」的数据源。

- **第 121 行** `"RATE_LIMITED": (True, False, "RATE_LIMITED"),`
  **429 限流：重试但不算熔断**。注释解释：是对方正常保护，不是对方宕机。这个 `False` 就是「429 不熔断」红线。

- **第 123 行** `"SERVER_ERROR": (True, True, "SERVER_ERROR"),`
  服务端错误：重试且计入熔断。

- **第 124 行** `"TIMEOUT": (True, True, "TIMEOUT"),`
  超时：重试且计入熔断。

- **第 125 行** `"CONN_RESET": (True, True, "CONN_RESET"),`
  连接重置：重试且计入熔断。

- **第 127-130 行** `BAD_REQUEST/UNAUTHORIZED/FORBIDDEN/PORT_MISSING`
  四个确定性错误：都不重试（`False`）、都不计熔断（`False`）。重试也不会成功，白费一次调用。

- **第 132 行** `"SCHEMA_INVALID": (True, False, "SCHEMA_INVALID"),`
  schema 非法：**可重试但不计熔断**。理由：可能是模型偶发生成非法格式，重试有救；但不该把它算作对端宕机。

- **第 133 行** `}` 表结束。

### 第 135-157 行：_CODE_ALIASES 错误码别名表

- **第 136 行** `_CODE_ALIASES: Mapping[str, str] = {`
  HTTP 码 / 符号码 → 归一化族。字符串前缀匹配，覆盖 5xx 段。

- **第 137-139 行** `"429" / "RATE_LIMITED" / "TOO_MANY_REQUESTS" → "RATE_LIMITED"`
  三种写法都归到限流族。

- **第 140-145 行** `500/502/503/SERVER_ERROR/SERVICE_UNAVAILABLE/INTERNAL_ERROR → "SERVER_ERROR"`
  六种写法都归到服务端错误族。

- **第 146-147 行** `TIMEOUT / DEADLINE_EXCEEDED → "TIMEOUT"`

- **第 148-150 行** `CONN_RESET / CONNECTION_RESET / NETWORK_ERROR → "CONN_RESET"`

- **第 151-153 行** `400 → BAD_REQUEST / 401 → UNAUTHORIZED / 403 → FORBIDDEN`

- **第 154-156 行** `PORT_MISSING / SCHEMA_INVALID / SCHEMA_VALIDATION → 对应族`

- **第 157 行** `}` 表结束。

  为什么要有别名表：不同 Port（OpenAI/本地模型/工具）报同一类错误用的码不一样，别名表把它们统一到语义族，下游只需认 9 个族名。

### 第 159-161 行：成功/未知默认分类

- **第 160 行** `_SUCCESS_CLASS = FailureClass(False, False, None, "OK")`
  成功是终态：不重试、不计熔断、无建议延迟、码为 OK。

- **第 161 行** `_UNKNOWN_CLASS = FailureClass(False, False, None, "UNKNOWN")`
  未知兜底：同样保守（不重试、不计熔断）。但注意未知码在 `_DefaultClassifier` 里会走另一条分支尊重 retryable 标记，这个常量只是防御性兜底。

### 第 163-172 行：DEFAULT_FAILURE_TABLE 导出表

- **第 164 行** `DEFAULT_FAILURE_TABLE: Mapping[str, FailureClass] = {`
  把 `_DEFAULT_RULES` 的三元组展开成完整的 `FailureClass`，导出供校验与注入方查询覆盖完整性。

- **第 165-170 行** 字典推导式：
  - `retryable=triple[0]`：取三元组第 0 位。
  - `count_toward_breaker=triple[1]`：第 1 位。
  - `suggested_delay=None`：默认表不预设服务端延迟。
  - `code=triple[2]`：第 2 位即归一化码。
  - `for family, triple in _DEFAULT_RULES.items()`：遍历规则表。

- **第 172 行** `}` 结束。

### 第 174 行：_HTTP_5XX 正则

- **第 174 行** `_HTTP_5XX = re.compile(r"^5\d\d$")`
  匹配三位 5xx 码（500-599）。为什么需要：别名表只列了 500/502/503，其它 5xx（如 504、507）靠这个正则兜底，避免漏判成未知码。

### 第 177-190 行：normalize_code 归一化函数

- **第 177 行** `def normalize_code(raw: str) -> str:`
  把错误码归一到语义族（HTTP 码与符号码等价）。

- **第 178-184 行** docstring：参数/返回说明，强调未知码原样返回（规则 5：不静默改写语义）。

- **第 185 行** `upper = raw.strip().upper()`
  先去首尾空白、再转大写。为什么：错误码可能带空格或大小写混杂（`" service_unavailable "`），归一后才能查表。

- **第 186-187 行** `if upper in _CODE_ALIASES: return _CODE_ALIASES[upper]`
  先查别名表，命中直接返回族名。

- **第 188-189 行** `if _HTTP_5XX.match(upper): return "SERVER_ERROR"`
  未列别名但形如 5xx → 兜底服务端错误。

- **第 190 行** `return raw`
  **未知码原样返回**（注意返回的是原始 `raw` 而非 `upper`）。为什么：不静默改写语义，让上层分类兜底；保留原始值便于日志排查。

### 第 193-211 行：FailureClassifier 可插拔协议

- **第 193 行** `@runtime_checkable`
  允许运行时 `isinstance(x, FailureClassifier)` 检查。代价：只检查成员存在，不检查签名。

- **第 194 行** `class FailureClassifier(Protocol):`
  可注入的失败分类策略协议。默认表 / 自定义实现都认这个契约。

- **第 195-201 行** docstring：说明 `name` 属性（可插拔物强制留名）、方法体 `...` 是协议语法不是省略实现、默认实现是 `_DefaultClassifier`。

- **第 203 行** `name: str`
  策略名，装配期按名校验、进 trace 归因。

- **第 205 行** `def classify(self, result: Result, ctx: object) -> FailureClass:`
  协议方法签名：输入结果和可选上下文，输出分类结论。

- **第 206-210 行** 方法 docstring：说明异常由调用方折叠不抛穿，ctx 可鸭子读取但默认表只依赖 `result.error`。

- **第 211 行** `...`
  Protocol 方法体省略号，接口声明而非实现。

### 第 214-231 行：_DefaultClassifier 默认分类器

- **第 214 行** `class _DefaultClassifier:`
  内置默认分类器，基于静态规则表，无状态。注意它**没有显式继承** `FailureClassifier`——Protocol 是结构化类型，有 `name` 和 `classify` 就算履约。

- **第 215 行** docstring：无状态静态规则表。

- **第 217 行** `name = "default_failure"`
  具名标识。

- **第 219 行** `def classify(self, result: Result, ctx: object) -> FailureClass:`
  分类方法。

- **第 220 行** docstring：`成功→终态；失败→查表；未知码按确定性错误`。

- **第 221-222 行** `if result.ok: return _SUCCESS_CLASS`
  成功直接返回终态分类，不参与任何重试/熔断判定。

- **第 223 行** `error = result.error`
  取错误对象。

- **第 224-225 行** `if error is None: return _UNKNOWN_CLASS`
  `# pragma: no cover` 表示这行防御性兜底在正常测试覆盖不到——`Result.failure` 必带 error，但保险起见防 None。

- **第 226 行** `family = normalize_code(error.code)`
  归一化错误码到族名。

- **第 227 行** `triple = _DEFAULT_RULES.get(family)`
  查默认规则表。

- **第 228-230 行** 未知码分支：
  - `if triple is None:` 规则表没这族。
  - 注释：**尊重 PortError.retryable 显式标记，但保守地不计熔断**（未知码不误伤熔断统计）。
  - `return FailureClass(error.retryable, False, None, family or "UNKNOWN")`：retryable 用对端显式标记，count_toward_breaker 硬编码 False（保守），code 用族名或 UNKNOWN。

- **第 231 行** `return FailureClass(triple[0], triple[1], None, family)`
  命中规则表：直接展开三元组的 retryable / count_toward_breaker，code 用族名。

### 第 234-246 行：classify_failure 统一入口

- **第 234-236 行** `def classify_failure(result, ctx, *, classifier: FailureClassifier | None = None) -> FailureClass:`
  统一分类入口。`classifier` 是仅关键字参数（在 `*` 后），可注入自定义分类器替换默认表。

- **第 237-244 行** docstring：参数/返回说明。

- **第 245 行** `active = classifier or _DefaultClassifier()`
  **可插拔落点**：传了自定义分类器就用它，否则 new 一个默认分类器。`or` 利用了对象真值（实例总为真）。

- **第 246 行** `return active.classify(result, ctx)`
  委托给实际分类器的 classify。所有调用方（retry 等）都走这个统一入口，保证分类语义处处一致。

### 第 249-251 行：维度键小节注释

- **第 249-251 行** 分隔注释 `# --- 维度键：按 provider/model/tool/tenant 隔离熔断/限流计数 ---`
  预告维度隔离设计。

### 第 252-266 行：DimensionKey 维度键

- **第 252 行** `@dataclass(frozen=True, slots=True)` 不可变数据类。

- **第 253 行** `class DimensionKey:`
  隔离计数维度（如 `("provider", "openai")`）。

- **第 254-259 行** docstring：两字段说明。

- **第 261 行** `dim_type: str`
  维度类型（provider/model/tool/tenant）。

- **第 262 行** `name: str`
  目标名。

- **第 264 行** `def as_tuple(self) -> tuple[str, str]:`
  转为二元组表示。

- **第 265-266 行** docstring + `return (self.dim_type, self.name)`
  为什么需要：熔断 store 的 key 是二元组 `(dim_type, name)`，`as_tuple` 把对象转成可哈希的 key。

### 第 269-271 行：退避算法小节注释

- **第 269-271 行** 分隔注释 `# --- 退避算法：纯函数，随机源显式注入（墙钟/随机不隐式进入） ---`
  强调纯函数纪律。

### 第 272-286 行：validate_retry_spec 装配期校验

- **第 272 行** `def validate_retry_spec(spec: RetrySpec) -> None:`
  装配期校验重试参数完整性。为什么在装配期而非运行期：配置错误应该尽早暴露，别等线上重试时才炸。

- **第 273-278 行** docstring：列四条校验。

- **第 279-280 行** `if spec.max_attempts < 1: raise ValueError(...)`
  max_attempts 必须 ≥ 1，否则一次都不尝试。

- **第 281-282 行** `if spec.max_delay < 0: raise ValueError(...)`
  退避上限不能为负。

- **第 283-284 行** `if spec.multiplier < 1: raise ValueError(...)`
  倍数不能小于 1（否则退避越算越小，失去意义）。

- **第 285-286 行** `if spec.base_delay == 0 and Jitter.coerce(spec.jitter) is Jitter.NONE: raise ValueError(...)`
  **防重试风暴关键校验**：base_delay=0 且无抖动 → 所有客户端同一时刻以零延迟疯狂重试，形成风暴。装配期直接拒绝。注意用 `Jitter.coerce` 归一后再比 `is Jitter.NONE`（枚举单例用 is 比较）。

### 第 289-327 行：backoff_delay 退避计算（核心纯函数）

- **第 289-296 行** 函数签名：
  - `attempt`：当前重试序号（≥1）。
  - `spec`：重试参数。
  - `rng`：注入的随机源。
  - `prev_delay=0.0`：上次退避值（decorrelated 用）。
  - `retry_after=None`：服务端建议延迟。

- **第 297-312 行** docstring：完整算法公式（四种 jitter + Retry-After + 参数说明）。这是理解退避的权威注释。

- **第 313 行** `raw = min(spec.max_delay, spec.base_delay * (spec.multiplier ** max(0, attempt - 1)))`
  先算指数退避基线：`base_delay × multiplier^(attempt-1)`，再用 `min(max_delay, ...)` 封顶。`max(0, attempt-1)` 防止 attempt=0 时指数为负。attempt=1 → ×2^0=1 → 0.5；attempt=2 → ×2^1=2 → 1.0；依此类推。

- **第 314 行** `jitter = Jitter.coerce(spec.jitter)  # 未知值显式回退 FULL`
  归一抖动策略，未知值回退 FULL。

- **第 315-316 行** `if jitter is Jitter.NONE: delay = raw`
  无抖动：固定退避。注释说明装配期已保证 base_delay>0，所以不会风暴。

- **第 317-318 行** `elif jitter is Jitter.FULL: delay = rng.uniform(0.0, raw)`
  全抖动：在 `[0, raw)` 均匀采样。**最常用**，把同步重试的客户打散到整个区间，防重试风暴。

- **第 319-321 行** `elif jitter is Jitter.EQUAL: half = raw / 2; delay = half + rng.uniform(0.0, half)`
  等抖动：保留一半基线（保证最小退避强度），另一半抖动（打散）。结果落在 `[raw/2, raw)`。

- **第 322-324 行** `else: # DECORRELATED`
  - `base = prev_delay if prev_delay > 0 else spec.base_delay`：首次没有上次值，回退到 base_delay 作起点。
  - `delay = min(spec.max_delay, base * rng.uniform(1.0, 3.0))`：按上次退避值放大 1-3 倍，同样封顶。去相关设计让每次退避与上次的随机性独立，避免周期性同步。

- **第 325-326 行** `if spec.respect_retry_after and retry_after is not None: delay = max(delay, retry_after)`
  **尊重对方背压**：给了 Retry-After 就取较大值。为什么用 max：既要打散（jitter 结果），又不能比服务端要求短——两个目标取交集即 max。

- **第 327 行** `return max(0.0, delay)`
  最终下限保护：退避秒数永不为负。

### 第 330-332 行：降级阶梯小节注释

- **第 330-332 行** 分隔注释 `# --- 降级阶梯：无环 + 末级拒答 + quality 递减 ---`
  预告三条红线。

### 第 333-339 行：DegradeMode 降级方式枚举

- **第 333 行** `class DegradeMode(str, Enum):`
  降级方式枚举（有限取值，规则 5）。

- **第 335 行** docstring：有限取值集合。

- **第 336 行** `REROUTE = "reroute"` 换备用模型/port。

- **第 337 行** `CACHE = "cache"` 用语义缓存答案。

- **第 338 行** `FALLBACK_VALUE = "fallback_value"` 用静态兜底值。

- **第 339 行** `REFUSE = "refuse"` **明确拒答（禁止编造）**。这是红线级成员——阶梯末级必须是它。

### 第 342-357 行：DegradeStep 阶梯的一级

- **第 342 行** `@dataclass(frozen=True, slots=True)` 不可变。

- **第 343 行** `class DegradeStep:`
  降级阶梯的一级。

- **第 344-351 行** docstring：五字段说明。

- **第 353 行** `target: str`
  目标名（模型/port/缓存键；拒答级为空串）。

- **第 354 行** `mode: DegradeMode`
  降级方式。

- **第 355 行** `quality: float`
  质量分（1.0 最优 → 0.0 拒答），进 trace 供评测。

- **第 356 行** `max_latency_ms: int | None = None`
  可选延迟预算。

- **第 357 行** `condition: str | None = None`
  可选触发条件表达式。

### 第 360-386 行：DegradeLadder 阶梯与 pick

- **第 360 行** `@dataclass(frozen=True, slots=True)` 不可变。

- **第 361 行** `class DegradeLadder:`
  降级阶梯（主→备→便宜→缓存→拒答）。

- **第 362-366 行** docstring：`steps` 有序、quality 严格递减、末级必须 REFUSE。

- **第 368 行** `steps: tuple[DegradeStep, ...]`
  有序步骤元组。用 tuple 保证不可变、顺序确定。

- **第 370 行** `def pick(self, available: AbstractSet[str]) -> DegradeStep:`
  从可用目标集合里选 quality 最高的一级。用抽象集合类型，接受 set/frozenset。

- **第 371-377 行** docstring：参数/返回说明。

- **第 378-382 行** 列表推导式筛候选：
  - `for step in self.steps`：遍历所有级。
  - `if step.mode is DegradeMode.REFUSE`：拒答级**总可用**（无条件入选）。
  - `or (step.target and step.target in available)`：非拒答级要求 target 非空且在 available 集合里。

- **第 383-385 行** 无候选兜底：
  - `if not candidates:` 没有任何候选（理论上合法阶梯必有拒答级，不该发生）。
  - 注释：若被绕过校验，返回一个临时拒答级（不编造）。
  - `return DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0)`：**fail-closed**——宁可拒答也不编造答案。

- **第 386 行** `return max(candidates, key=lambda step: step.quality)`
  从候选里选 quality 最高的一级。为什么用 max：`quality` 已装配期校验严格递减，最高 quality 就是当前可用的最优选择。

### 第 389-408 行：validate_ladder 阶梯强校验（红线）

- **第 389 行** `def validate_ladder(ladder: DegradeLadder) -> None:`
  装配期强校验降级阶梯（R3 红线）。

- **第 390-394 行** docstring：四条校验。

- **第 395 行** `steps = ladder.steps` 取步骤元组。

- **第 396-397 行** `if not steps: raise ValueError("degrade ladder must not be empty")`
  校验 1：空阶梯无任何候选，直接拒绝。

- **第 398-399 行** `if steps[-1].mode is not DegradeMode.REFUSE: raise ValueError(...)`
  校验 2（**阶梯末级必须 REFUSE**）：末级不是拒答 → 目标被剔除干净后 `pick` 无候选，可能编造答案。这条红线直接防「编造」。

- **第 400 行** `targets = [step.target for step in steps if step.mode is not DegradeMode.REFUSE]`
  收集所有非拒答级的目标名（拒答级 target 为空，不参与查重）。

- **第 401-402 行** `if len(set(targets)) != len(targets): raise ValueError("...cycle...")`
  校验 3：目标重复即成环。为什么危险：降级会在两个相同 target 的级之间来回震荡，永远选不出确定结果，浪费调用。

- **第 403-404 行** `for prev, nxt in zip(steps, steps[1:]): if nxt.quality >= prev.quality:`
  校验 4：相邻两级配对（`zip(steps, steps[1:])` 是经典的相邻配对技巧），下一级 quality 必须严格小于上一级。否则无法单调收敛。

- **第 405-408 行** `raise ValueError(f"degrade ladder quality must strictly decrease: {prev.quality} -> {nxt.quality}")`
  报错信息带出具体是哪两级，方便装配期定位配置错误。

### 第 411-412 行：熔断状态小节注释

- **第 411-412 行** 分隔注释 `# --- 熔断状态：三态机的不可变快照 ---`
  预告熔断状态。

### 第 414-419 行：BreakerStatus 三态枚举

- **第 414 行** `class BreakerStatus(str, Enum):`
  熔断器三态枚举（有限取值，规则 5）。

- **第 415 行** docstring：三态。

- **第 417 行** `CLOSED = "closed"`
  关闭（健康，正常放行）。

- **第 418 行** `OPEN = "open"`
  打开（熔断，拒绝放行）。

- **第 419 行** `HALF_OPEN = "half_open"`
  半开（冷却后有限探测）。

### 第 422-445 行：BreakerState 熔断状态快照

- **第 422 行** `@dataclass(frozen=True, slots=True)` 不可变——状态快照是值对象，每次迁移都 `replace` 出新对象，不就地改。

- **第 423 行** `class BreakerState:`
  熔断器在某维度上的不可变状态快照。

- **第 424-432 行** docstring：五字段说明。

- **第 434 行** `status: str = BreakerStatus.CLOSED.value`
  当前三态之一（字符串值 `"closed"`，与枚举对齐）。用字符串而非枚举类型是为了跨 store 序列化方便（落 Redis/DB 时是纯字符串）。

- **第 435 行** `failures: int = 0`
  累计失败数（用于样本量判定）。

- **第 436 行** `window: tuple[bool, ...] = field(default_factory=tuple)`
  滑窗（`True`=失败，`False`=成功），计算失败率。用 `field(default_factory=tuple)` 而非 `= ()`：可变默认值要用工厂函数——这里 tuple 虽不可变，但用工厂是 dataclass 的标准安全写法，避免未来改成 list 时踩坑。

- **第 437 行** `half_open_successes: int = 0`
  半开态连续成功次数。

- **第 438 行** `opened_at: float | None = None`
  进入 open 的时钟值（注入 clock 的返回类型；秒或步号），None 表示非 open。

- **第 440 行** `def failure_rate(self) -> float:`
  滑窗失败率；空窗返回 0。

- **第 442-443 行** `if not self.window: return 0.0`
  空窗除零保护，返回 0。

- **第 444 行** `return sum(1 for ok_flag in self.window if ok_flag) / len(self.window)`
  统计窗口中 `True`（失败）的比例。注意变量名叫 `ok_flag` 但语义是「失败标志」——命名略有误导，逻辑是 `if ok_flag` 即该次为失败则计数。

---

## 2. defenses.py 逐行讲解（354 行）

### 第 1-8 行：模块 docstring

- **第 1 行** `"""R3 其余防线：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。`
  一句话定位：六道防线（外加降级选级）。

- **第 3-5 行** `每个插件只暴露**自身的领域语义方法**（限流许可 acquire、舱位占放 acquire/release、幂等认领 claim、循环探测 inspect、开关查询 is_tripped、降级选级 select），不再有统一拦截协议。编排由 ReliabilityExecutor 显式调用这些方法。`
  **设计要点「无 AOP 角色」**：每个策略方法名都不同（`acquire`/`release`/`check_and_mark`/`inspect`/`is_tripped`/`select`），没有统一接口。执行器必须显式知道每个策略的方法名去调用——这是与「统一拦截器链」架构的本质区别。

- **第 7 行** `时钟/存储显式注入，墙钟/随机不隐式进入，保证可测试、可回放。内核零策略。`
  纪律复述：时钟、存储都是构造注入，测试可传假时钟/假存储。

- **第 8 行** `"""` docstring 结束。

### 第 9-29 行：imports 与导出

- **第 9 行** `from __future__ import annotations` 延迟注解。

- **第 11 行** `import time`
  为 `Idempotency` 提供缺省时钟 `time.time`。

- **第 12 行** `from collections.abc import Awaitable, Callable, Collection, MutableMapping`
  类型注解用：`NextFn` 签名、时钟回调、白名单集合、存储映射。

- **第 13 行** `from enum import Enum`
  定义 `RatePermit`/`_ExceedPolicy`/`_KillScope` 三个枚举。

- **第 15 行** `from compose.context import FaultRecord, Invocation`
  `FaultRecord` 是写 fault 的证据载体（含 failed_target，R3-7）；`Invocation` 是调用上下文。

- **第 16 行** `from runtime import Invoke, Result`
  `Invoke` 用于识别副作用/模型调用；`Result` 返回结果。

- **第 18 行** `from .types import DegradeLadder, DegradeMode, DegradeStep, validate_ladder`
  复用 types.py 的阶梯类型和强校验。

- **第 20-29 行** `__all__ = [...]`
  导出 8 个符号：六道防线 + `RatePermit` 枚举 + `failed_target_of` 工具函数。

### 第 32-43 行：failed_target_of 工具函数（B2 核心）

- **第 32 行** `def failed_target_of(inv: Invocation) -> str:`
  从 Invocation 派生**失败目标注册名**。这是「B2：failed_target 是注册名」的核心落点。

- **第 33 行** docstring 首行：`（B2：阶梯后端注册名，非维度二元组）`——强调它返回的是阶梯里 target 那种名字（如 `"backup"`），不是熔断的 `("provider","openai")` 二元组。

- **第 40 行** `effect = inv.effect`
  取调用的 effect。

- **第 41-42 行** `if isinstance(effect, Invoke): return str(effect.payload.get("model") or "primary")`
  如果是 Invoke，从 `payload["model"]` 取模型名；取不到（None/空串）则 `or "primary"` 回退到主目标。`str(...)` 保证返回字符串。

- **第 43 行** `return "primary"`
  非 Invoke（不是模型调用）一律返回 `"primary"`。

  为什么这个函数重要：R3-7 要求 fault 必带 failed_target，而 failed_target 必须是阶梯能识别的注册名——否则 `Degradation.select` 剔除时剔不掉，降级失效。这个函数统一派生逻辑，各处复用。

### 第 46-47 行：NextFn 类型别名

- **第 46 行** `type NextFn = Callable[[Invocation], Awaitable[Result]]`
  领域动作续延：舱壁 `run_in_slot` 包裹的「下一跳」签名。

- **第 47 行** docstring：说明用途。

### 第 50-60 行：RatePermit 限流裁决枚举

- **第 50 行** `class RatePermit(str, Enum):` 限流许可裁决。

- **第 52-57 行** docstring：三成员说明。

- **第 58 行** `ADMIT = "admit"` 取到令牌，放行。

- **第 59 行** `REJECT = "reject"` 拒绝（fail-fast，终止类）。

- **第 60 行** `DEGRADE = "degrade"` 超限降级，交由执行器写 fault 并选级（降级放行类）。

  这个枚举体现「终止拒绝 vs 降级放行」的两分：REJECT 是终止，DEGRADE 是写 fault 后放行。

### 第 63-83 行：_ExceedPolicy 超限策略枚举

- **第 63 行** `class _ExceedPolicy(str, Enum):`
  超限策略的有限取值（内部用）。

- **第 65-70 行** docstring：三成员说明。

- **第 71 行** `WAIT = "wait"` 降级（写 fault 交 Degradation 选级）。

- **第 72 行** `REJECT = "reject"` 直接拒绝（fail-fast）。

- **第 73 行** `DEGRADE = "degrade"` 降级（同 WAIT）。

  注意 WAIT 和 DEGRADE 语义相同——都是写 fault 交降级，名字不同只是历史遗留的两个别名。

- **第 75-76 行** `@classmethod def coerce(cls, raw: _ExceedPolicy | str) -> _ExceedPolicy:`
  字符串归一为枚举成员。

- **第 77 行** docstring：未知值显式回退 DEGRADE（fail-closed）。

- **第 78-79 行** `if isinstance(raw, cls): return raw` 已是成员原样返回。

- **第 80-81 行** `try: return cls(raw)` 尝试构造。

- **第 82-83 行** `except ValueError: return cls.DEGRADE` 未知值回退 DEGRADE（保守降级，不 fail-open 放行）。

### 第 86-99 行：_KillScope killswitch 作用域枚举

- **第 86 行** `class _KillScope(str, Enum):`
  killswitch 作用域的有限取值。

- **第 88 行** `SESSION = "session"` 会话级。

- **第 89 行** `GLOBAL = "global"` 全局级。

- **第 91-92 行** `@classmethod def coerce(cls, raw: _KillScope | str) -> _KillScope:`
  字符串归一。

- **第 93 行** docstring：未知值回退 SESSION（最小权限）。

- **第 94-95 行** `if isinstance(raw, cls): return raw`

- **第 96-97 行** `try: return cls(raw)`

- **第 98-99 行** `except ValueError: return cls.SESSION`
  未知值回退 SESSION 而非 GLOBAL——**最小权限原则**：宁可作用域小（只影响本会话），也不要误伤全局。

### 第 102-164 行：Degradation 降级策略（核心，四坑落点）

- **第 102 行** `class Degradation:`
  降级策略：唯一选级裁决者，持有 ladder + available + 按会话隔离状态。

- **第 103-110 行** docstring：明确列「修复历史四坑」——
  - B1：目标状态按 session_id 隔离，禁容器级单值。
  - B2：failed_target 必须是阶梯注册名（上游 `failed_target_of` 派生）。
  - R3-6：故障目标永久从实例 available 剔除并写回（非局部副本）。
  - R3-7：上游故障记录必须携带 failed_target。
  这段代码就是为这四坑服务的，下面逐行看落点。

- **第 111 行** `name = "r3_degrade"` 具名标识。

- **第 113-119 行** 构造函数签名：
  - `ladder`：降级阶梯（必填位置参数）。
  - `available=None`：初始可用注册名集合（各会话的初始模板），关键字参数。
  - `session_state=None`：按 session_id 隔离的状态表（B1）。

- **第 120-126 行** docstring：装配说明，强调 available 缺省空（走 REFUSE）。

- **第 127 行** `validate_ladder(ladder)`
  **装配期强校验**：调用 types.py 的 `validate_ladder`，末级非 REFUSE / 有环 / quality 不递减都会在这里抛异常，error 配置进不了系统。

- **第 128 行** `self._ladder = ladder` 保存阶梯。

- **第 129 行** `self._initial_available = set(available) if available is not None else set()`
  保存初始可用集副本。`set(available)` 复制一份，防外部后续修改原集合影响本实例；None 视为空集（此时所有非拒答目标不可用，直接 REFUSE）。

- **第 130 行** `self._session_state = session_state if session_state is not None else {}`
  保存会话状态表（B1 的容器）。外部可传入共享的 dict（实现跨实例状态共享），缺省新建。

- **第 132-142 行** `_session(session_id)` 取会话状态切片（**B1 落点**）：

  - **第 132 行** `def _session(self, session_id: str) -> dict[str, object]:`
  - **第 133-137 行** docstring：每会话独立 available 副本，一个会话剔除 primary 不影响另一会话，消除「全局单槽串味」。
  - **第 138 行** `state = self._session_state.get(session_id)` 查本会话是否已有状态。
  - **第 139-140 行** `if state is None: state = {"available": set(self._initial_available)}`
    缺失则从初始模板**新建一个独立副本**。`set(...)` 复制是关键——每个会话拿到自己的集合，改一个不影响别人。
  - **第 141 行** `self._session_state[session_id] = state` 注册到状态表。
  - **第 142 行** `return state` 返回本会话切片。

- **第 144-146 行** `pick(available)` 便捷选级：

  - **第 144 行** `def pick(self, available: set[str] | None = None) -> DegradeStep:`
  - **第 145 行** docstring：从给定（或初始）可用集选 quality 最高级。
  - **第 146 行** `return self._ladder.pick(available if available is not None else set(self._initial_available))`
    委托阶梯的 `pick`，缺省时用初始集副本。

- **第 148-164 行** `select(fault, session_id)` 唯一选级裁决（**四坑总落点**）：

  - **第 148 行** `def select(self, fault: FaultRecord, *, session_id: str) -> str:`
    `session_id` 是仅关键字参数。
  - **第 149-156 行** docstring：分派说明，逐条对应 B1/R3-6/B2。
  - **第 157 行** `state = self._session(session_id)  # B1：本会话独立切片`
  - **第 158 行** `available: set[str] = state["available"]  # type: ignore[assignment]`
    取出本会话可用集。`type: ignore` 是因为 `dict[str, object]` 取值静态类型是 object，运行时确定是 set，抑制类型检查器告警。
  - **第 159-162 行** R3-6 永久剔除：
    - **第 159 行** `if fault.failed_target:` 有失败目标才剔。
    - **第 161 行** `available.discard(fault.failed_target)`
      **从本会话 available 里永久剔除**。`discard` 对不存在的元素不报错（幂等）。
    - **第 162 行** `state["available"] = available`
      **写回状态表**（R3-6 关键）。为什么不写回就是 bug：如果只用局部副本 discard 而不写回，下次调用状态表还是旧的，失败目标又被选进来——这就是「永久剔除」与「临时剔除」的区别。
  - **第 163 行** `choice = self.pick(available)` 从剔除后的可用集选级。
  - **第 164 行** `return choice.target or DegradeMode.REFUSE.value`
    返回选定 target；拒答级 target 为空串，用 `or` 回退到 `"refuse"` 字符串。B2 体现：这里返回的是注册名，执行器拿去写 routing_target。

### 第 166-206 行：RateLimit 限流（令牌桶）

- **第 166 行** `class RateLimit:` 限流策略：令牌桶许可，时钟显式注入。

- **第 167 行** docstring + **第 168 行** `name = "r3_ratelimit"`。

- **第 170-177 行** 构造函数签名：
  - `rate=60.0`：每秒补充 60 个令牌。
  - `burst=10`：桶容量 10。
  - `on_exceed=_ExceedPolicy.WAIT`：超限策略默认 WAIT（即降级）。
  - `clock=None`：注入时钟。

- **第 178 行** docstring：装配说明。

- **第 179 行** `self._rate = rate` 保存速率。

- **第 180 行** `self._burst = burst` 保存容量。

- **第 181 行** `self._on_exceed = _ExceedPolicy.coerce(on_exceed)` 归一超限策略。

- **第 182 行** `self._clock = clock or (lambda: 0.0)`
  注入时钟，缺省返回常量 0.0。为什么缺省是常量：单测里不推进时间也能稳定工作；生产环境应传 `time.time`。

- **第 183 行** `self._tokens = float(burst)` 初始令牌满桶。

- **第 184 行** `self._last = self._clock()` 记录上次补充时刻。

- **第 186-191 行** `_refill()` 补充令牌：

  - **第 186 行** `def _refill(self) -> None:`
  - **第 187 行** docstring：按经过时间补充令牌（上限 burst）。
  - **第 188 行** `now = self._clock()` 当前时刻。
  - **第 189 行** `elapsed = max(0.0, now - self._last)`
    经过时间，`max(0.0, ...)` 防时钟回拨导致负补充。
  - **第 190 行** `self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)`
    补充 `elapsed × rate` 个令牌，`min(burst, ...)` 封顶不溢出。
  - **第 191 行** `self._last = now` 更新时刻。

- **第 193-206 行** `acquire()` 取令牌：

  - **第 193 行** `def acquire(self) -> RatePermit:`
  - **第 194-199 行** docstring：三种返回说明。
  - **第 200 行** `self._refill()` 先补充。
  - **第 201-203 行** `if self._tokens >= 1.0: self._tokens -= 1.0; return RatePermit.ADMIT`
    有令牌就扣 1 放行。用 `>= 1.0` 而非 `> 0` 保证扣完不为负。
  - **第 204-205 行** `if self._on_exceed is _ExceedPolicy.REJECT: return RatePermit.REJECT`
    超限且策略是 REJECT → 终止拒绝。
  - **第 206 行** `return RatePermit.DEGRADE`
    其余情况（WAIT/DEGRADE）→ 降级放行，由执行器写 fault 交降级选级。

### 第 209-248 行：Bulkhead 舱壁

- **第 209 行** `class Bulkhead:` 舱壁策略：按池隔离并发，池满拒绝；用 `try/finally` 保证释放。

- **第 210 行** docstring + **第 211 行** `name = "r3_bulkhead"`。

- **第 213-219 行** 构造函数签名：
  - `pools=None`：池名→容量映射，缺省 `{"default": 10}`。
  - `on_full=_ExceedPolicy.REJECT`：满时策略默认拒绝。
  - `pool="default"`：本插件使用的池名。

- **第 220 行** docstring：装配说明。

- **第 221 行** `self._capacity = dict(pools or {"default": 10})`
  容量表副本。`pools or {...}`：传 None 或空 dict 都用默认单池。

- **第 222 行** `self._used: dict[str, int] = {name: 0 for name in self._capacity}`
  每个池的已用槽数初始化为 0。

- **第 223 行** `self._on_full = _ExceedPolicy.coerce(on_full)` 归一满时策略。

- **第 224 行** `self._pool = pool` 保存本插件池名。

- **第 226-233 行** `acquire(pool)` 占槽：

  - **第 227 行** docstring：成功 True，池满 False。
  - **第 228 行** `cap = self._capacity.get(pool, self._capacity.get("default", 0))`
    容量取值：优先该池自己的容量，没有则回退 default 池容量，再没有则 0（不可占）。
  - **第 229 行** `used = self._used.get(pool, 0)` 已用槽数。
  - **第 230-231 行** `if used >= cap: return False` 满则返回 False。
  - **第 232-233 行** `self._used[pool] = used + 1; return True` 占一个槽。

- **第 235-237 行** `release(pool)` 释放槽：

  - **第 236 行** docstring：下限 0，防重复释放。
  - **第 237 行** `self._used[pool] = max(0, self._used.get(pool, 0) - 1)`
    `max(0, ...)` 是关键防御：即使被错误地重复 release，计数也不会变负（否则计数错乱会让舱壁永远拒绝）。

- **第 239-248 行** `run_in_slot(inv, next)` 舱位隔离执行：

  - **第 240 行** docstring：占槽 → 调下一跳 → finally 释放。
  - **第 241 行** `if not self.acquire(self._pool):` 占槽失败（池满）。
  - **第 242-243 行** `if self._on_full is _ExceedPolicy.REJECT: return Result.failure("BULKHEAD_FULL", "bulkhead_full")`
    满时策略是 REJECT → 直接返回失败（终止类）。
  - **第 244 行** `inv.write_fault(FaultRecord(reason="bulkhead_full", failed_target=failed_target_of(inv)))`
    否则（降级策略）→ **写 fault（R3-7，带 failed_target）仍放行**，交降级选级。
  - **第 245-246 行** `try: return await next(inv)` 调下一跳。
  - **第 247-248 行** `finally: self.release(self._pool)`
    **finally 保证释放**：即使 next 抛异常，槽也一定归还，防止泄漏导致舱壁逐渐耗尽。

### 第 251-277 行：Idempotency 幂等

- **第 251 行** `class Idempotency:` 幂等策略：按幂等键去重认领，TTL 到期自动淘汰。

- **第 252 行** docstring + **第 253 行** `name = "r3_idempotency"`。

- **第 255-261 行** 构造函数签名：
  - `store=None`：键→登记时间戳的映射。
  - `ttl=86400`：24 小时（秒）。
  - `clock=None`：注入时钟。

- **第 262 行** docstring：真淘汰说明。

- **第 263 行** `self._store = store if store is not None else {}` 存储，可外部注入共享。

- **第 264 行** `self._ttl = ttl` 保存 TTL。

- **第 265 行** `self._clock = clock or time.time`
  缺省用真实墙钟 `time.time`（注意与 RateLimit 不同——幂等需要真实时间推进才有效）。

- **第 267-277 行** `check_and_mark(key)` 认领：

  - **第 268 行** docstring：True=窗口内已见过（重复），False=首次并登记。
  - **第 269 行** `now = self._clock()` 当前时刻。
  - **第 270 行** `marked_at = self._store.get(key)` 查登记时间戳。
  - **第 271-273 行** 过期真淘汰：
    - `if marked_at is not None and now - marked_at >= self._ttl:` 命中且已过期。
    - `self._store.pop(key, None)` 真删除（不是逻辑标记，防存储无限膨胀）。
    - `marked_at = None` 局部变量也置空。
  - **第 274-275 行** `if marked_at is not None: return True`
    未过期的命中 → 重复副作用 → 返回 True（执行器据此终止拒绝）。
  - **第 276-277 行** `self._store[key] = now; return False`
    未命中 → 登记当前时刻，返回 False（首次，放行）。

### 第 279-333 行：Watchdog 循环看门狗

- **第 279 行** `class Watchdog:` 看门狗：检测重复 / A-B 交替循环，命中则拒绝。

- **第 280 行** docstring + **第 281 行** `name = "r3_watchdog"`。

- **第 283-290 行** 构造函数签名：
  - `window=10`：观察窗大小。
  - `repeat_k=3`：重复阈值。
  - `detect_abab=True`：是否检测 A-B 交替。
  - `kinds=None`：关注的 port 白名单。

- **第 291 行** docstring：装配说明。

- **第 292 行** `self._window = window` 保存窗口大小。

- **第 293 行** `self._repeat_k = repeat_k` 保存重复阈值。

- **第 294 行** `self._detect_abab = detect_abab` 保存交替检测开关。

- **第 295 行** `self._kinds = frozenset(kinds) if kinds else None`
  白名单转 frozenset（不可变、查找快）；None/空表示不限制（关注所有）。

- **第 296 行** `self._history: list[str] = []` 观测历史。

- **第 298-306 行** `_kind_of(inv)` 派生观测标识：

  - **第 299 行** docstring：由 Invoke.port 派生，不在白名单跳过。
  - **第 300 行** `effect = inv.effect`
  - **第 301-302 行** `if not isinstance(effect, Invoke): return None` 非 Invoke 不关注。
  - **第 303 行** `kind = effect.port` 取 port 作为标识。
  - **第 304-305 行** `if self._kinds is not None and kind not in self._kinds: return None` 不在白名单跳过。
  - **第 306 行** `return kind` 返回标识。

- **第 308-323 行** `inspect(kind)` 记录并检测循环：

  - **第 309-312 行** docstring：True=命中循环，False=正常。
  - **第 313 行** `self._history.append(kind)` 记录观测点。
  - **第 314 行** `self._history = self._history[-self._window:]`
    只保留最近 window 个。为什么切片赋新列表：限制内存 + 让检测只看近期模式。
  - **第 315-318 行** 重复检测：
    - `if self._repeat_k > 0 and len(self._history) >= self._repeat_k:` 阈值有效且历史够长。
    - `tail = self._history[-self._repeat_k:]` 取末尾 k 个。
    - `if len(set(tail)) == 1: return True`
      **set 去重后长度为 1 = 末尾 k 个全相同** → 陷入重复循环。这是「重复检测」的核心技巧。
  - **第 319-322 行** A-B 交替检测：
    - `if self._detect_abab and len(self._history) >= 4:` 开关开且历史至少 4 个。
    - `tail = self._history[-4:]` 取末尾 4 个 `t0,t1,t2,t3`。
    - `if tail[0] == tail[2] and tail[1] == tail[3] and tail[0] != tail[1]: return True`
      **t0==t2 且 t1==t3 且两者不同 = A-B-A-B 交替模式** → 也是循环（两种调用来回切，永不推进）。这是「AB 交替检测」红线。
  - **第 323 行** `return False` 未命中任何循环模式。

- **第 325-333 行** `detect(inv)` 执行器入口：

  - **第 326-329 行** docstring：非关注类型返回 False。
  - **第 330 行** `kind = self._kind_of(inv)` 派生标识。
  - **第 331-332 行** `if kind is None: return False` 不关注则放行。
  - **第 333 行** `return self.inspect(kind)` 委托 inspect。

### 第 336-353 行：KillSwitch 紧急开关

- **第 336 行** `class KillSwitch:` 紧急开关：标志置位时拒绝（人工/运维一键熔断）。

- **第 337 行** docstring + **第 338 行** `name = "r3_killswitch"`。

- **第 340-345 行** 构造函数签名：
  - `flag_store=None`：标志存储 dict。
  - `scope=_KillScope.SESSION`：作用域，默认会话级。

- **第 346 行** docstring：未知值回退 SESSION。

- **第 347 行** `self.flag_store = flag_store if flag_store is not None else {}` 存储，可外部注入（运维写入口）。

- **第 348 行** `self._scope = _KillScope.coerce(scope)` 归一作用域。

- **第 350-353 行** `is_tripped(session_id)` 是否置位（**双键检测**）：

  - **第 351 行** docstring：全局标志或本作用域标志为真即置位。
  - **第 352 行** `keys = ("kill:global", f"kill:{self._scope.value}")`
    **双键**：一个固定的全局键 `kill:global`，一个按作用域拼的键（如 `kill:session` 或 `kill:global`——注意 scope=GLOBAL 时两键相同，去重也无害）。
  - **第 353 行** `return any(self.flag_store.get(k, False) for k in keys)`
    **任一为真即触发**。`any` 短路。这就是「killswitch 双键」红线：全局标志和用户作用域标志都能触发紧急熔断，任一置位即拒绝。

---

## 3. breaker.py 逐行讲解（217 行）

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R3 熔断器：三态机（closed/open/half_open）+ 滑窗失败率 + 维度隔离。`
  一句话定位：三态机 + 滑窗失败率 + 维度隔离。

- **第 3-5 行** `领域语义：permit(inv) 判定当前维度是否允许放行（OPEN/半开超额返回降级原因）；record(result, dim) 推进三态机。执行器在调用前后显式调用它们，无角色协议。状态由插件实例 + 注入 store 持有。`
  **无 AOP 角色**的具体落地：只暴露 `permit`/`record` 两个领域方法，执行器显式调用。状态在实例 + store 里，跨调用存活。

- **第 6 行** `"""` docstring 结束。

### 第 7-19 行：imports 与导出

- **第 7 行** `from __future__ import annotations` 延迟注解。

- **第 9 行** `import asyncio`
  用 `asyncio.Lock` 做维度级互斥锁。

- **第 10 行** `from collections.abc import Awaitable, Callable`
  类型注解用。

- **第 11 行** `from dataclasses import replace`
  **不可变状态迁移的核心工具**：`replace(state, ...)` 返回改了部分字段的新状态对象，而不是就地改。

- **第 12 行** `from typing import Protocol, runtime_checkable`
  定义 `BreakerStore` 协议。

- **第 14 行** `from compose.context import Invocation` 调用上下文。

- **第 15 行** `from runtime import Invoke, Result` 结果类型 + 副作用识别。

- **第 17 行** `from .types import BreakerState, BreakerStatus`
  复用 types.py 的不可变状态与三态枚举。

- **第 19 行** `__all__ = ["BreakerStore", "CircuitBreaker", "InMemoryBreakerStore"]`
  导出存储协议、熔断器、默认内存存储。

### 第 21-22 行：维度类型别名

- **第 21 行** `_EMPTY: tuple[str, str] = ("", "")`
  空维度。`per_dimension=False` 时所有调用归并到这里。

- **第 22 行** `Dimension = tuple[str, str]`
  维度类型别名，二元组形如 `("provider", "openai")`。

### 第 25-29 行：BreakerStore 协议

- **第 25 行** `@runtime_checkable` 允许运行时 isinstance 检查。

- **第 26 行** `class BreakerStore(Protocol):`
  熔断共享态存储协议（异步，可落 Redis/DB）。

- **第 27 行** docstring：异步存储，可落远端。

- **第 28 行** `async def load(self, dim: Dimension) -> BreakerState | None: ...`
  读维度状态，无记录返回 None。

- **第 29 行** `async def save(self, dim: Dimension, state: BreakerState) -> None: ...`
  写维度状态。

  为什么协议是异步的：生产环境 store 可能是 Redis/DB，读写要 await；内存实现只是立即返回，签名统一。

### 第 32-45 行：InMemoryBreakerStore 默认存储

- **第 32 行** `class InMemoryBreakerStore:` 默认熔断器存储：进程内字典，单实例闭环用。

- **第 33 行** docstring + **第 35-36 行** `__init__` + docstring：空内存态。

- **第 37 行** `self._data: dict[Dimension, BreakerState] = {}`
  维度→状态快照的字典。

- **第 39-41 行** `async def load(self, dim) -> BreakerState | None: return self._data.get(dim)`
  读取，无记录返回 None。

- **第 43-45 行** `async def save(self, dim, state) -> None: self._data[dim] = state`
  写入（覆盖）。

### 第 48-52 行：_normalize_key 维度归一

- **第 48 行** `def _normalize_key(key: object) -> Dimension:`
  把外部传入的任意 key 收敛成二元组维度。

- **第 49 行** docstring：形态不符回退空维度。

- **第 50-51 行** `if isinstance(key, tuple) and len(key) == 2: return (str(key[0]), str(key[1]))`
  只认长度为 2 的 tuple，元素转字符串。

- **第 52 行** `return _EMPTY`
  其它一律回退空维度。为什么防御：外部（如 `state(key)` 公开 API）可能传任意对象，归一后保证 store key 形态一致。

### 第 55-60 行：_failed_target_of 失败目标派生（breaker 内部版）

- **第 55 行** `def _failed_target_of(inv: Invocation) -> str:`
  失败目标注册名（B2）。注意这是 breaker 内部版，逻辑与 defenses.py 的同名函数一致——两处各有一份是为避免循环 import。

- **第 56 行** docstring：Invoke.payload.model 优先，缺省 `"primary"`。

- **第 57-58 行** `effect = inv.effect; if isinstance(effect, Invoke):`

- **第 59 行** `return str(effect.payload.get("model") or "primary")`
  Invoke 取 model，缺省 primary。

- **第 60 行** `return "primary"` 非 Invoke 回退 primary。

  说明：breaker 的 `_failed_target_of` 实际只在构造时作为默认 `failed_target_of` 传入（`self._failed_target_of`），但 breaker 领域入口 `permit`/`observe_outcome` 并不写 fault（写 fault 是执行器职责），所以这个函数在 breaker 里主要是兼容性存在。真正的 B2 落点在 defenses/executor。

### 第 63-105 行：CircuitBreaker 构造

- **第 63 行** `class CircuitBreaker:`
  熔断器：自身持有三态机状态，permit 判放行、record 推进状态。

- **第 64-69 行** docstring：三态机转移规则——
  - CLOSED：样本足够 + 失败率≥阈值 → OPEN。
  - OPEN：冷却期届满 → HALF_OPEN；否则写 fault 重路由。
  - HALF_OPEN：放行有限探测；连续 N 次成功 → CLOSED，任一失败 → OPEN。

- **第 70 行** `name = "r3_breaker"` 具名标识。

- **第 73-87 行** 构造函数签名（默认值要背下来）：
  - `failure_rate=0.5`：失败率阈值 50%。
  - `minimum_samples=5`：最少 5 个样本才开始判定。
  - `window_size=20`：滑窗大小 20。
  - `open_cooldown_s=30.0`：OPEN 冷却 30 秒。
  - `half_open_max_probes=3`：半开最多 3 次探测。
  - `half_open_success_to_close=2`：半开连续 2 次成功回 CLOSED。
  - `per_dimension=True`：默认按维度隔离。
  - `clock=None`：注入时钟。
  - `store=None`：注入存储。
  - `dimension_of=None`：算维度的纯函数。
  - `failed_target_of=None`：算失败目标注册名（B2）。

- **第 88-93 行** docstring：强调状态在本实例跨调用存活，说明两个注入函数用途。

- **第 94 行** `self._threshold = failure_rate` 保存阈值。

- **第 95 行** `self._min_samples = minimum_samples` 保存最小样本数。

- **第 96 行** `self._window_size = window_size` 保存窗口大小。

- **第 97 行** `self._cooldown = open_cooldown_s` 保存冷却时间。

- **第 98 行** `self._max_probes = half_open_max_probes` 保存最大探测数。

- **第 99 行** `self._success_to_close = half_open_success_to_close` 保存关闭所需成功数。

- **第 100 行** `self._per_dimension = per_dimension` 保存维度隔离开关。

- **第 101 行** `self._clock = clock or (lambda: 0.0)` 注入时钟，缺省常量 0.0（测试友好）。

- **第 102 行** `self._store: BreakerStore = store if store is not None else InMemoryBreakerStore()`
  存储缺省用内存实现。

- **第 103 行** `self._dimension_of = dimension_of` 保存维度计算函数。

- **第 104 行** `self._failed_target_of = failed_target_of or _failed_target_of` 保存失败目标函数，缺省内部版。

- **第 105 行** `self._locks: dict[Dimension, asyncio.Lock] = {}`
  **每维度一把锁**的容器（懒建）。为什么按维度：不同 provider 的熔断判定互不阻塞，提高并发；同维度串行，保证 load→决策→save 原子。

### 第 107-113 行：_lock_for 锁懒建

- **第 107 行** `def _lock_for(self, dim: Dimension) -> asyncio.Lock:`
  取维度的锁（首次懒建）。

- **第 109 行** `lock = self._locks.get(dim)` 查现有锁。

- **第 110-112 行** `if lock is None: lock = asyncio.Lock(); self._locks[dim] = lock`
  没有则新建并存入。**懒建**避免构造时给所有可能维度建锁（维度是运行时才知道的）。

- **第 113 行** `return lock` 返回。

### 第 115-119 行：_dim 维度选择

- **第 115 行** `def _dim(self, dimension: Dimension | None) -> Dimension:`
  按 per_dimension 决定用调用方维度还是归并空维度。

- **第 117-118 行** `if not self._per_dimension: return _EMPTY`
  关闭维度隔离 → 所有调用归并到空维度（全局一把锁、一个状态）。

- **第 119 行** `return dimension if dimension is not None else _EMPTY`
  开启时：有维度用维度，没维度回退空维度。

### 第 121-123 行：state 公开查询

- **第 121 行** `async def state(self, key: object) -> BreakerState:`
  读取维度当前状态（无记录视为 CLOSED 默认态）。

- **第 122 行** docstring。

- **第 123 行** `return await self._store.load(_normalize_key(key)) or BreakerState()`
  先归一 key，读 store，无记录返回全新 CLOSED 状态。注意这里没加锁（纯读，且绕过维度转换直接用归一 key）。

### 第 125-141 行：check 放行判定（三态机核心之一）

- **第 125 行** `async def check(self, dimension: Dimension | None) -> str | None:`
  返回降级原因（OPEN 冷却内 / 半开超额），健康返回 None。

- **第 126-129 行** docstring：强调整个 load→冷却迁移→reload→判定在**维度锁内**完成，与 record 互斥。

- **第 130 行** `dim = self._dim(dimension)` 决定维度。

- **第 131 行** `async with self._lock_for(dim):`
  持有该维度锁，保证判定过程不被 record 并发打断。

- **第 132 行** `st = await self._store.load(dim) or BreakerState()` 载入状态。

- **第 133-137 行** OPEN 分支：
  - **第 133 行** `if st.status == BreakerStatus.OPEN.value:` 当前是 OPEN。
  - **第 134-135 行** `if self._clock() - (st.opened_at or 0) < self._cooldown: return "breaker_open"`
    **冷却期内**（当前时间 - 打开时间 < 冷却时长）→ 仍拒绝，返回原因 `"breaker_open"`（执行器据此写 fault 降级）。`opened_at or 0` 防 None。
  - **第 136 行** `await self._maybe_half_open_unlocked(dim)`
    冷却期满 → 迁移到 HALF_OPEN（无锁版，因已持锁）。
  - **第 137 行** `st = await self._store.load(dim) or BreakerState()` reload 迁移后的状态。

- **第 138-140 行** HALF_OPEN 分支：
  - `if st.status == BreakerStatus.HALF_OPEN.value:` 半开态。
  - `if len(st.window) >= self._max_probes: return "breaker_half_open"`
    **半开探测已满**（窗口长度 ≥ 3）→ 返回 `"breaker_half_open"`，不再放行更多探测。
  - 否则（探测名额未满）→ 落到最后的 `return None` 放行探测。

- **第 141 行** `return None`
  CLOSED 健康 / 半开有名额 → 放行。

### 第 143-147 行：maybe_half_open 公开 API

- **第 143 行** `async def maybe_half_open(self, key: object) -> None:`
  公开 API：冷却期满 OPEN→HALF_OPEN（带锁）。

- **第 144 行** docstring。

- **第 145 行** `dim = _normalize_key(key)` 归一 key。

- **第 146-147 行** `async with self._lock_for(dim): await self._maybe_half_open_unlocked(dim)`
  加锁后调无锁版。为什么要有这个公开 API：给运维/测试一个显式触发半开迁移的入口，不必等一次真实调用。

### 第 149-158 行：_maybe_half_open_unlocked 无锁迁移

- **第 149 行** `async def _maybe_half_open_unlocked(self, dim: Dimension) -> None:`
  无锁版本：调用方须已持有该维度锁。

- **第 150 行** docstring：调用方须持锁。

- **第 151 行** `st = await self._store.load(dim)` 读状态。

- **第 152-153 行** `if st is None or st.status != BreakerStatus.OPEN.value: return`
  无记录或非 OPEN → 什么都不做。

- **第 154-155 行** `if self._clock() - (st.opened_at or 0) >= self._cooldown:`
  冷却期满才迁移。

- **第 155-158 行** `await self._store.save(dim, replace(st, status=BreakerStatus.HALF_OPEN.value, window=(), half_open_successes=0, opened_at=None))`
  **OPEN→HALF_OPEN 三态转移**：用 `replace` 从旧状态派生新状态（不可变迁移）——
  - `status` 改为 `"half_open"`。
  - `window=()` **清空滑窗**（旧失败记录不再影响半开判定）。
  - `half_open_successes=0` 清零连续成功数。
  - `opened_at=None` 清除打开时刻。

### 第 160-168 行：record 记数入口

- **第 160-162 行** `async def record(self, result: Result, dimension: Dimension | None = None, *, count: bool = True) -> None:`
  记录一次调用结果并推进三态机。`count=False` 可跳过记数（预留）。

- **第 163 行** docstring：锁内 load→decide→save。

- **第 164-165 行** `if not count: return` 不记数则直接返回。

- **第 166 行** `dim = self._dim(dimension)` 决定维度。

- **第 167-168 行** `async with self._lock_for(dim): await self._record_unlocked(dim, result.ok)`
  持锁调无锁版，传 `result.ok`（成功/失败布尔）。

### 第 170-197 行：_record_unlocked 三态推进（核心之二）

- **第 170 行** `async def _record_unlocked(self, dim: Dimension, success: bool) -> None:`
  无锁记录：调用方须已持锁。

- **第 171 行** docstring。

- **第 172 行** `st = await self._store.load(dim) or BreakerState()` 载入状态。

- **第 173 行** `window = (st.window + (not success,))[-self._window_size:]`
  **滑窗更新**：追加本次结果（`not success`：失败为 True），用 `[-window_size:]` 截尾保留最近 N 个。tuple 拼接生成新元组，符合不可变语义。

- **第 174 行** `failures = st.failures + (0 if success else 1)`
  累计失败数 +1（失败时）。

- **第 175-176 行** `if st.status == BreakerStatus.OPEN.value: return`
  **OPEN 态不处理**：已熔断，等冷却迁移，不记数。

- **第 177-189 行** HALF_OPEN 分支：
  - **第 177 行** `if st.status == BreakerStatus.HALF_OPEN.value:`
  - **第 178-183 行** 成功路径：
    - `if success:`
    - `successes = st.half_open_successes + 1` 连续成功数 +1。
    - **第 180-181 行** `if successes >= self._success_to_close: await self._store.save(dim, BreakerState())`
      **达到 2 次 → 回到 CLOSED**：直接写入一个全新初始状态（窗口空、失败 0、半开成功 0）。这是「半开连续成功 2 次回 CLOSED」红线。
    - **第 182-183 行** `else: await self._store.save(dim, replace(st, half_open_successes=successes))`
      未达标 → 只更新连续成功数，保持 HALF_OPEN 等下一次探测。
  - **第 184-188 行** 失败路径：
    - `else:`（半开态收到失败）
    - `await self._store.save(dim, replace(st, status=BreakerStatus.OPEN.value, opened_at=self._clock(), half_open_successes=0))`
      **任一失败 → 回 OPEN**：改状态为 OPEN、记录新的 opened_at 重新开始冷却、清零连续成功数。这是「半开任一失败回 OPEN」红线。
  - **第 189 行** `return` 半开分支处理完返回。

- **第 190 行** `new_state = replace(st, window=window, failures=failures)`
  CLOSED 分支：基于新窗口和失败数派生新状态。

- **第 191-196 行** CLOSED→OPEN 判定：
  - **第 191 行** `if len(window) >= self._min_samples:` 样本足够（≥5）才判定，防小样本误熔断。
  - **第 192 行** `rate = sum(1 for bad in window if bad) / len(window)` 算滑窗失败率。
  - **第 193-196 行** `if rate >= self._threshold: new_state = replace(new_state, status=BreakerStatus.OPEN.value, opened_at=self._clock())`
    失败率≥50% → 改状态 OPEN、记录打开时刻。**这是 CLOSED→OPEN 三态转移**。

- **第 197 行** `await self._store.save(dim, new_state)` 保存最终状态。

### 第 199-216 行：执行器领域入口

- **第 199 行** `# ---- 执行器调用的领域入口 ----` 分隔注释。

- **第 200-205 行** `_dimension(inv)` 从 Invocation 派生维度：
  - **第 201 行** docstring：自定义函数非二元组时回退空维度。
  - **第 202-203 行** `if self._dimension_of is None: return None` 没配维度函数 → None（最终 `_dim` 会转空维度）。
  - **第 204 行** `dim = self._dimension_of(inv)` 调注入函数。
  - **第 205 行** `return dim if isinstance(dim, tuple) else None`
    **防御**：函数返回值必须是 tuple 二元组，否则视为无维度。防止乱配的维度函数污染 store key。

- **第 207-212 行** `permit(inv)` 放行判定入口：
  - **第 208-210 行** docstring：返回降级原因表示不放行；**写 fault 是执行器职责，本插件只判状态**（再次体现职责分离）。
  - **第 212 行** `return await self.check(self._dimension(inv))` 调 check。

- **第 214-216 行** `observe_outcome(inv, result)` 记数入口：
  - **第 215 行** docstring：把结果记入三态机。
  - **第 216 行** `await self.record(result, self._dimension(inv))` 调 record。

---

## 4. retry.py 逐行讲解（96 行）

### 第 1-5 行：模块 docstring

- **第 1 行** `"""R3 重试策略：按退避算法重放下一跳（领域语义）。`
  一句话定位：按退避重放。

- **第 3 行** `耗尽时把具名故障记录写入 inv.meta（含 failed_target，R3-7），交降级选级。`
  强调耗尽的出口：写 fault（带 failed_target，R3-7），交降级链。

- **第 4 行** `幂等红线：无幂等键的 TOOL 副作用禁止重试。`
  预告幂等红线（第二个安全红线）。

- **第 5 行** `"""` docstring 结束。

### 第 6-18 行：imports 与导出

- **第 6 行** `from __future__ import annotations` 延迟注解。

- **第 8 行** `import asyncio` 缺省 sleeper 用 `asyncio.sleep`。

- **第 9 行** `from collections.abc import Awaitable, Callable` 类型注解。

- **第 10 行** `from random import Random` 随机源类型（显式注入）。

- **第 12 行** `from compose.context import FaultRecord, Invocation` fault 载体 + 上下文。

- **第 13 行** `from runtime import Invoke, PortName, Result`
  `PortName.TOOL` 用于幂等红线判定。

- **第 15 行** `from .defenses import failed_target_of`
  **复用** defenses.py 的失败目标派生函数（B2），避免重复实现。

- **第 16 行** `from .types import Jitter, RetrySpec, backoff_delay, classify_failure, validate_retry_spec`
  复用共享类型与 pure 函数——**失败分类共用**的落点：`classify_failure` 来自 types.py。

- **第 18 行** `__all__ = ["RetryPolicy"]` 导出重试策略。

### 第 20 行：NextFn 类型别名

- **第 20 行** `type NextFn = Callable[[Invocation], Awaitable[Result]]`
  「下一跳」签名：retry 包裹的被重试对象。

### 第 23-56 行：RetryPolicy 构造

- **第 23 行** `class RetryPolicy:` 重试策略：按退避预算重放下一跳，幂等红线保护。

- **第 24 行** docstring + **第 25 行** `name = "r3_retry"`。

- **第 27-40 行** 构造签名：
  - `max_attempts=3` / `base_delay=0.5` / `multiplier=2.0` / `max_delay=30.0` / `jitter=Jitter.FULL` / `respect_retry_after=True` / `idempotency_required=True`。
  - `rng=None`：注入随机源。
  - `sleeper=None`：注入睡眠函数。
  - `target_of=None`：注入失败目标派生函数（R3-7）。

- **第 41-46 行** docstring：强调 `target_of` 用途。

- **第 47 行** `self._target_of = target_of or failed_target_of`
  失败目标派生函数，缺省用 defenses 的默认实现。

- **第 48-53 行** `self._spec = RetrySpec(...)`
  把构造参数打包成不可变 `RetrySpec`：
  - 第 50 行 `jitter=Jitter.coerce(jitter).value`：把 Jitter 枚举/字符串归一后取 `.value` 字符串（RetrySpec.jitter 字段是 str）。

- **第 54 行** `validate_retry_spec(self._spec)`
  **装配期校验**：base_delay=0 + jitter=none 等非法配置在这里被拒。

- **第 55 行** `self._rng = rng if rng is not None else Random()`
  注入随机源，缺省 new 一个。测试可传固定种子 Random。

- **第 56 行** `self._sleep = sleeper`
  保存注入的睡眠函数（None 表示运行时用 asyncio.sleep）。

### 第 58-86 行：run_with_retries 重试执行（核心）

- **第 58-59 行** `async def run_with_retries(self, inv: Invocation, next: NextFn) -> Result:`
  重试执行：失败退避重放；耗尽写 fault。

- **第 60 行** `sleeper = self._sleep or asyncio.sleep`
  注入睡眠函数，缺省 asyncio.sleep。

- **第 61 行** `prev_delay = 0.0`
  上次退避值（decorrelated jitter 用）。

- **第 62 行** `last = Result.failure("RETRY_EXHAUSTED", "no attempts")`
  `last` 兜底初值：万一轮子没进（max_attempts=0 被校验挡住，正常不会），也返回一个失败而不是 NameError。

- **第 63 行** `for attempt in range(1, self._spec.max_attempts + 1):`
  从 1 遍历到 max_attempts。attempt 从 1 起，与 backoff_delay 约定一致。

- **第 64 行** `result = await next(inv)` 调用下一跳（被重试的中心调用）。

- **第 65 行** `last = result` 记录最近一次结果。

- **第 66 行** `fc = classify_failure(result, inv)`
  **失败分类共用**：用共享的 `classify_failure` 分类这一步结果。不自己解释错误码。

- **第 67-68 行** `if not fc.retryable or result.ok: return result`
  不可重试 或 成功 → 直接返回，不再重试。注意 `result.ok` 判断放这里，成功也直接返回。

- **第 69 行** `inv.meta["retry_classified"] = fc.code`
  把分类码写进 meta（可观测性：trace 里能看到这次为什么重试/不重试）。

- **第 70-75 行** 耗尽分支：
  - **第 70 行** `if attempt >= self._spec.max_attempts:` 已是最后一次尝试。
  - **第 71 行** 注释：耗尽写具名故障记录（含 failed_target，R3-7）。
  - **第 72-74 行** `inv.write_fault(FaultRecord(reason="retry_exhausted", failed_target=self._target_of(inv)))`
    **关键**：重试耗尽写 fault，reason=`retry_exhausted`，failed_target 由 `target_of` 派生（注册名，B2）。这是 R3-7 落点——降级链据此剔除失败目标。
  - **第 75 行** `return result` 返回最后一次失败结果。

- **第 76-79 行** 幂等红线：
  - **第 76 行** `if self._is_retryable_side_effect(inv):`
  - **第 77-79 行** `return Result.failure("IDEMPOTENCY_REQUIRED", "idempotency_required for side-effect retry")`
    **幂等红线**：若判定是「无幂等键的 TOOL 副作用」，**禁止重试**，直接返回 `IDEMPOTENCY_REQUIRED` 失败。为什么：重试一个有副作用的工具调用（如发消息、下单）可能造成重复执行，后果严重。
  - 注意顺序：这个检查在「耗尽判断之后、退避之前」，即只在「还要重试」时才检查。

- **第 80-83 行** 计算退避：
  - `delay = backoff_delay(attempt, self._spec, self._rng, prev_delay=prev_delay, retry_after=fc.suggested_delay)`
    用共享退避函数，传入上次退避值和分类里带的服务端建议延迟（尊重 Retry-After）。

- **第 84 行** `prev_delay = delay` 记录本次退避，供下次 decorrelated 用。

- **第 85 行** `await sleeper(delay)` 睡够退避时间。

- **第 86 行** `return last` 循环正常结束（理论上被耗尽分支提前 return），兜底返回。

### 第 88-95 行：_is_retryable_side_effect 幂等判定

- **第 88-89 行** `def _is_retryable_side_effect(self, inv: Invocation) -> bool:`
  幂等红线：TOOL 副作用无幂等键时禁止重试。（方法名略绕：返回 True 表示「是危险的无幂等键副作用」，应禁止重试。）

- **第 90-91 行** `if not self._spec.idempotency_required: return False`
  若配置不要求幂等键（显式 opt-out）→ 不判定为危险，可重试。

- **第 92 行** `effect = inv.effect` 取 effect。

- **第 93-94 行** `if not isinstance(effect, Invoke) or effect.port != PortName.TOOL.value: return False`
  非 Invoke 或 不是 TOOL 端口 → 不是工具副作用，可重试。只有 TOOL 才需幂等保护。

- **第 95 行** `return not bool(effect.idempotency_key)`
  **核心**：没有幂等键（空/None）→ 返回 True（危险，禁止重试）。有幂等键 → False（安全，可重试）。

---

## 5. timeout.py 逐行讲解（49 行）

### 第 1 行：模块 docstring

- **第 1 行** `"""R3 超时插件：时限执行（领域语义），超时写故障记录（含 failed_target，R3-7）。"""`
  一句话定位：时限执行 + 超时写 fault（R3-7）。最简短的插件。

### 第 2-12 行：imports 与导出

- **第 2 行** `from __future__ import annotations` 延迟注解。

- **第 4 行** `import asyncio` 用 `asyncio.wait_for` 做超时。

- **第 5 行** `from collections.abc import Awaitable, Callable` 类型注解。

- **第 7 行** `from compose.context import FaultRecord, Invocation` fault + 上下文。

- **第 9 行** `from .defenses import failed_target_of` 复用失败目标派生函数（B2）。

- **第 10 行** `from runtime import Invoke, Result`
  注意 `Invoke` 导入实际在文件里未被直接使用（failed_target_of 已封装），属无害冗余。

- **第 12 行** `__all__ = ["Timeout"]` 导出超时策略。

### 第 14 行：NextFn 类型别名

- **第 14 行** `type NextFn = Callable[[Invocation], Awaitable[Result]]`
  被时限包裹的「下一跳」签名。

### 第 17-38 行：Timeout 构造

- **第 17 行** `class Timeout:` 时限策略：在给定时限内执行下一跳，超时写故障记录 + 超时失败 Result。

- **第 18 行** docstring + **第 19 行** `name = "r3_timeout"`。

- **第 21-26 行** 构造签名：
  - `timeout=30.0`：超时秒数。
  - `target_of=None`：失败目标派生函数（R3-7）。

- **第 27-34 行** docstring：参数说明 + 异常（timeout ≤ 0）。

- **第 35-36 行** `if timeout <= 0: raise ValueError("timeout must be positive")`
  **装配期校验**：超时必须为正，否则立即拒绝配置。

- **第 37 行** `self._timeout = timeout` 保存时限。

- **第 38 行** `self._target_of = target_of or failed_target_of` 失败目标函数，缺省用默认实现。

### 第 40-48 行：run_with_deadline 时限执行（核心）

- **第 40-41 行** `async def run_with_deadline(self, inv: Invocation, next: NextFn) -> Result:`
  时限执行：超时写 fault + TIMEOUT 失败，否则透传。

- **第 42-43 行** `try: return await asyncio.wait_for(next(inv), timeout=self._timeout)`
  用 `asyncio.wait_for` 包裹下一跳：在时限内完成就返回结果；超时抛 `TimeoutError`。

- **第 44 行** `except (asyncio.TimeoutError, TimeoutError):`
  捕获两种超时异常（Python 3.11+ 里 `asyncio.TimeoutError` 是 `TimeoutError` 的别名，两个都写兼容不同版本）。

- **第 45-47 行** `inv.write_fault(FaultRecord(reason="timeout", failed_target=self._target_of(inv)))`
  **超时写 fault（R3-7，带 failed_target）**：让降级链知道该切哪个后端。

- **第 48 行** `return Result.failure("TIMEOUT", f"invoke exceeded {self._timeout}s")`
  返回超时失败 Result，错误信息带具体秒数，便于排查。

  为什么超时要写 fault：不写的话降级链不知道「哪个目标超时了」，就无法从 available 里剔除它，可能又选到同一个慢后端——这正是 R3-7 的意义。

---

## 6. executor.py 逐行讲解（290 行）

这是全模块的核心。前面的策略都是「零件」，这里把它们装成一台机器。读的时候反复回想五条纪律，尤其是「无 AOP 角色」「全 opt-in」「闭包晚绑定」「包裹顺序」。

### 第 1-17 行：模块 docstring

- **第 1 行** `"""R3 可靠性执行器（去角色化）：按可靠性工程语义显式编排领域策略。`
  一句话定位：**去角色化**——这是整个模块的设计宣言。

- **第 3-7 行** `本模块**没有**任何横切角色概念——没有角色协议、没有角色枚举、没有槽位列表，也没有统一的方法名契约。每个领域策略以**自身的领域方法**参与（KillSwitch.is_tripped / RateLimit.acquire / CircuitBreaker.permit / Degradation.select ...），执行器用显式代码按可靠性工程语义串联`
  **「无 AOP 角色」的完整声明**：连举四个不同名的方法，证明没有统一契约。执行器必须写显式代码 `self._kill_switch.is_tripped(...)`、`self._rate_limit.acquire()` 分别调用——不是遍历一个「拦截器列表」。

- **第 9 行** `前置放行 → 预算内中心调用（超时/重试/舱位）→ 熔断记数 → 失败降级选级 + 重路由`
  四段式编排顺序，后面 `_invoke` 就是这个流程的代码化。

- **第 11-13 行** `这些是可靠性领域固有语义，不是 AOP 的五层横切角色。装配由 ResilienceCapability.install() 用 opt-in 构造注入完成，产出可信注入 R1 的 CallFn；缺席的策略即跳过，无需占位实现。`
  强调两点：语义是可靠性领域固有的，不是硬套 AOP；**全 opt-in**——缺席跳过，不写空实现。

- **第 15-16 行** `四坑（B1 session 隔离 / B2 failed_target 注册名 / R3-6 永久剔除 / R3-7 fault 带 failed_target）由 Degradation.select + FaultRecord 保住，断言不放水。`
  四坑在此声明，由 `Degradation.select`（B1/B2/R3-6）和 `FaultRecord`（R3-7）兜住。

- **第 17 行** `"""` docstring 结束。

### 第 18-40 行：imports 与导出

- **第 18 行** `from __future__ import annotations` 延迟注解。

- **第 20 行** `import inspect`
  为 `_await_result` 判断 `inspect.isawaitable`。

- **第 21 行** `from collections.abc import Awaitable, Callable, Mapping` 类型注解。

- **第 23 行** `from compose.context import CallContext, FaultRecord, Invocation, RoutingTarget`
  - `CallContext`：构造 Invocation。
  - `FaultRecord`：写 fault。
  - `RoutingTarget`：写降级目标（typed target）。

- **第 24 行** `from runtime import CallFn, Effect, Result` R1 的类型。

- **第 26 行** `from .breaker import CircuitBreaker` 熔断策略。

- **第 27-36 行** `from .defenses import (...)`
  导入六道防线 + `failed_target_of`：Bulkhead / Degradation / Idempotency / KillSwitch / RateLimit / RatePermit / Watchdog / failed_target_of。

- **第 37 行** `from .retry import RetryPolicy` 重试策略。

- **第 38 行** `from .timeout import Timeout` 超时策略。

- **第 40 行** `__all__ = ["ReliabilityExecutor"]` 只导出执行器一个符号。

### 第 42-50 行：常量与类型别名

- **第 42-43 行** `_MAX_REROUTE = 8` + docstring
  **降级重路由硬上限 = 8**。为什么需要：降级阶梯万一成环或配置异常，重路由循环会无限打转；8 次上限强制止损。这是防死循环的红线。

- **第 44-45 行** `_REFUSE_TARGET = "refuse"` + docstring
  末级拒答目标名，与 `DegradeMode.REFUSE` 对齐。代码里到处用字符串 `"refuse"` 判断是否已到拒答级。

- **第 47 行** `type _InvCall = Callable[[Invocation], Awaitable[Result]]`
  领域调用签名。

- **第 49-50 行** `type _Transformer = Callable[[_InvCall], _InvCall]`
  函数式调用变换器（extra_plugins 的形态，**非角色插件**）。

### 第 53-55 行：_await_result 同步/异步归一

- **第 53 行** `async def _await_result(outcome: Result | Awaitable[Result]) -> Result:`
  统一等待 core 的同步/异步返回。

- **第 54 行** docstring：具名边界，避免 Any 泄漏。

- **第 55 行** `return await outcome if inspect.isawaitable(outcome) else outcome`
  **关键兼容行**：core 可以返回同步 `Result` 或异步 `Awaitable[Result]`。`inspect.isawaitable` 判断——可等待就 await，否则原样返回。为什么：R1 的 Port 调用有的同步有的异步，统一在此归一，下游只认 Result。

### 第 58-74 行：_as_invocation 构造 Invocation（B3 核心）

- **第 58-59 行** `def _as_invocation(effect: Effect, ctx: object) -> Invocation:`
  由 `(effect, ctx)` 构造 Invocation；ctx 已是 Invocation 则复用。

- **第 60-62 行** docstring：**B3 meta 同源**——`meta` 与 R1 `RuntimeCtx.meta` 同源：前置策略写出的 fault / typed target 与网关从 ctx.meta 读的是同一份（重路由不会静默失效）。这是全模块最隐蔽也最关键的坑之一。

- **第 64-65 行** `if isinstance(ctx, Invocation): return ctx`
  **复用**：ctx 本来就是 Invocation 直接返回。这样它的 meta 就是同一个对象，前面写的 fault 后面能读到。

- **第 66 行** 注释：规则 6 例外——meta/session/session_id 都是 R1 RuntimeCtx 具名固定字段，非动态键（允许直接点名访问）。

- **第 67 行** `ctx_meta = getattr(ctx, "meta", None)` 取 ctx 的 meta。

- **第 68 行** `shared: dict[str, object] = ctx_meta if isinstance(ctx_meta, dict) else {}`
  **同源关键**：若 ctx.meta 是 dict 就**直接复用这个字典对象**（不复制！），否则新建空 dict。复制就断了同源——前置写的 fault 网关读不到，重路由失效。

- **第 69 行** `session = getattr(ctx, "session", None)` 取 session。

- **第 70-73 行** session 缺省构造：
  - `if session is None:` 没有 session。
  - `from types import SimpleNamespace` 局部导入轻量对象。
  - 第 72 行注释：session_id 是 R1 具名固定字段。
  - `session = SimpleNamespace(session_id=str(getattr(ctx, "session_id", "") or ""))` 造一个只有 session_id 的简版 session。

- **第 74 行** `return CallContext(effect=effect, ctx=ctx, session=session, meta=shared)`
  构造 Invocation（CallContext 是 Invocation 的实现），meta 用上面的 `shared`（同源）。

### 第 77-81 行：_idem_key 幂等键派生

- **第 77 行** `def _idem_key(inv: Invocation) -> str | None:`
  从调用派生幂等键（Invoke.idempotency_key），非 Invoke 返回 None。

- **第 78 行** docstring。

- **第 79 行** `effect = inv.effect`

- **第 80 行** 注释：规则 6 例外，idempotency_key 是具名固定字段。

- **第 81 行** `return str(getattr(effect, "idempotency_key", "") or "") or None`
  有键转字符串返回，无键（空串）返回 None。用 `or None` 把空串归一成 None——前置逻辑用 `key is not None` 判断是否要去重。

### 第 84-122 行：ReliabilityExecutor 构造

- **第 84-88 行** 类 docstring：`构造注入全部 opt-in——传实例才参与，None 即该领域能力缺席（不写空实现）`。

- **第 90-107 行** 构造函数签名：
  - **第 92 行** `core`：必填位置参数，中心 Port 调用（如 `gateway.as_call()`）。
  - **第 94-105 行** 全部是关键字参数（在 `*` 后）且都 `| None = None`：registry / bus / notify / kill_switch / watchdog / idempotency / rate_limit / bulkhead / breaker / retry / timeout / degradation。
  - **第 106 行** `transformers: tuple[_Transformer, ...] = ()`：函数式变换器，缺省空元组。
  - 这些 `None` 默认值就是**全 opt-in** 的代码体现：不传就是没有该能力。

- **第 108 行** docstring：core 是中心 Port 调用。

- **第 109 行** `self._core = core` 保存核心调用。

- **第 110 行** `self.registry = registry` 注册表（公开属性）。

- **第 111 行** `self._bus = bus` 事件总线。

- **第 112 行** `self._notify = notify or (lambda _phase, _payload: None)`
  通知回调缺省为**空函数**（no-op）。这里是唯一用空实现的地方——因为它只是观察回调，不影响控制流；策略本身绝不写空实现。

- **第 113-121 行** 逐个保存策略：`self._kill_switch` / `_watchdog` / `_idempotency` / `_rate_limit` / `_bulkhead` / `_breaker` / `_retry` / `_timeout` / `_degradation`。原样保存，None 就是 None。

- **第 122 行** `self._transformers = tuple(transformers)` 变换器转 tuple 固定。

### 第 124-143 行：通知与终止拒绝

- **第 124 行** `# ---- 通知（16 相位之一：invoke_rejected）----` 分隔注释。

- **第 125-126 行** `def _emit_rejected(self, inv: Invocation, reason: str) -> None:`
  发 invoke_rejected（每次调用只发一次，meta 标记去重）。

- **第 127-129 行** 去重：
  - `if inv.meta.get("_rejected_emitted"): return`
  - `inv.meta["_rejected_emitted"] = True`
  **去重机制**：用 meta 里的 `_rejected_emitted` 标记。为什么需要：一次调用可能在多处触发拒绝（前置 + 重路由循环末尾），去重保证通知只发一次，不刷屏。

- **第 130-137 行** 发总线通知：
  - `if self._bus is not None:` 配了总线才发。
  - **第 131 行** `from compose import LifecyclePhase, Notice  # 延迟导入避免循环` 局部导入防循环依赖。
  - **第 132 行** 注释：规则 6 例外，emit 是具名能力探测。
  - **第 133-134 行** `emit = getattr(self._bus, "emit", None); if callable(emit):` 鸭子探测 emit 能力。
  - **第 135-137 行** `emit(Notice.from_invocation(LifecyclePhase.INVOKE_REJECTED, inv, payload={"reason": reason}))` 发 16 相位之一的 `invoke_rejected` 通知。

- **第 138 行** `self._notify("invoke_rejected", {"reason": reason, "session_id": inv.session_id})`
  也调观察回调（如果有）。

- **第 140-143 行** `_reject` 终止拒绝：

  - **第 140 行** `def _reject(self, inv: Invocation, reason: str) -> Result:`
  - **第 141 行** docstring：通知 + 失败 Result（**不调 core**）。
  - **第 142 行** `self._emit_rejected(inv, reason)` 发通知。
  - **第 143 行** `return Result.failure("INVOKE_REJECTED", reason)`
    返回终止拒绝 Result。调用方 `_preflight` 拿到非 None 就直接短路返回，core 根本不会被调用。

### 第 145-173 行：_preflight 前置放行（终止 vs 降级）

- **第 145 行** `# ---- 前置放行（各领域策略以各自领域方法判定，无统一协议）----`
  注意注释直接点题「无统一协议」。

- **第 146-147 行** `async def _preflight(self, inv: Invocation) -> Result | None:`
  返回非 None 即**终止类拒绝**短路；降级类写 fault 后放行（返回 None）。

- **第 148-150 行** KillSwitch：
  - 注释：置位即终止拒绝。
  - `if self._kill_switch is not None and self._kill_switch.is_tripped(inv.session_id): return self._reject(inv, "killswitch")`
  - 全 opt-in：None 跳过；置位 → 终止。

- **第 151-153 行** Watchdog：
  - `if self._watchdog is not None and self._watchdog.detect(inv): return self._reject(inv, "watchdog:loop")`
  - 命中循环 → 终止拒绝。

- **第 154-158 行** Idempotency：
  - **第 155 行** `if self._idempotency is not None:`
  - **第 156 行** `key = _idem_key(inv)` 派生幂等键。
  - **第 157-158 行** `if key is not None and self._idempotency.check_and_mark(key): return self._reject(inv, "duplicate_side_effect")`
    有键且窗口内重复 → 终止拒绝。无键跳过（不拦截）。

- **第 159-167 行** RateLimit（**两分处理**）：
  - **第 160 行** `if self._rate_limit is not None:`
  - **第 161 行** `permit = self._rate_limit.acquire()` 取令牌。
  - **第 162-163 行** `if permit is RatePermit.REJECT: return self._reject(inv, "rate_limited")`
    REJECT → **终止拒绝**。
  - **第 164-167 行** `if permit is RatePermit.DEGRADE: inv.write_fault(FaultRecord(reason="rate_limited", failed_target=failed_target_of(inv)))`
    DEGRADE → **写 fault（带 failed_target，R3-7）但不返回**，继续放行到降级选级。这就是「终止拒绝 vs 降级放行」的分水岭。

- **第 168-172 行** Breaker：
  - `if self._breaker is not None:`
  - `reason = await self._breaker.permit(inv)` 熔断判定。
  - `if reason is not None: inv.write_fault(FaultRecord(reason=reason, failed_target=failed_target_of(inv)))`
    有降级原因（OPEN/半开超额）→ 写 fault 放行，交降级选级。

- **第 173 行** `return None`
  全程无终止拒绝 → 放行，进入预算内调用。

### 第 175-212 行：_budgeted_call 预算内调用（包裹顺序核心）

- **第 175 行** `# ---- 预算内中心调用（超时/重试/舱位，各有领域方法）----`

- **第 176-177 行** `async def _budgeted_call(self, inv: Invocation) -> Result:`
  按超时→重试→舱位顺序包裹 core；熔断结果记数（含异常）。

- **第 178-179 行** `async def base(i: Invocation) -> Result: return await _await_result(self._core(i))`
  最内层 `base` 就是核心调用，统一 await 同步/异步返回。

- **第 181 行** `call: Callable[[Invocation], Awaitable[Result]] = base`
  `call` 初始指向 base，后续逐层重新绑定。

- **第 182 行** 注释：**超时最内层（紧贴 core），重试居中，舱位最外层**——与原装配顺序等价。这是「包裹顺序」红线的声明。

- **第 183-187 行** 超时层（第一个包裹）：
  - **第 183 行** `if self._timeout is not None:` opt-in。
  - **第 184 行** `timeout = self._timeout` 局部变量固定当前值。
  - **第 185 行** `inner = call` 保存当前 call（base）。
  - **第 186-187 行**
    ```
    async def call(i: Invocation, _p: _InvCall = inner, _t: Timeout = timeout) -> Result:
        return await _t.run_with_deadline(i, _p)
    ```
    **闭包晚绑定的解决**：用默认参数 `_p=inner`、`_t=timeout` **显式绑定当前值**。为什么必须这样：Python 闭包在运行时才查找自由变量，如果直接引用 `inner`/`timeout`，等真正调用时它们可能已被后续层重新赋值，导致所有层都指向最后一层——这是经典 late binding 陷阱。默认参数在定义时求值，把当前值「快照」下来。

- **第 188-192 行** 重试层（第二个包裹）：
  - `if self._retry is not None:` → `retry = self._retry` → `inner = call`（此时 call 是超时层）。
  - `async def call(i, _p: _InvCall = inner, _r: RetryPolicy = retry): return await _r.run_with_retries(i, _p)`
  - 同样用默认参数绑定。此时 `_p` 绑定的是超时层 → 重试包在超时**外面**。

- **第 193-197 行** 舱壁层（第三个包裹）：
  - `if self._bulkhead is not None:` → `bulkhead = self._bulkhead` → `inner = call`（此时 call 是重试层）。
  - `async def call(i, _p: _InvCall = inner, _b: Bulkhead = bulkhead): return await _b.run_in_slot(i, _p)`
  - `_p` 绑定重试层 → 舱壁包在重试**外面**。最终顺序：**timeout 最内 → retry 居中 → bulkhead 最外**。

- **第 198-200 行** 变换器层（最外）：
  - 注释：函数式变换器（extra_plugins）：Invocation 层最外层包裹。
  - `for transform in self._transformers: call = transform(call)`
  - 每个变换器把当前 call 包一层，位于舱壁之外。注意这里**没有晚绑定问题**——`transform(call)` 立即求值，call 是当前值。

- **第 202-211 行** 熔断记数（含异常）：
  - **第 202 行** `if self._breaker is not None:` 配了熔断器才记数。
  - **第 203-204 行** `try: result = await call(inv)` 执行整个包裹链。
  - **第 205-209 行** 异常分支：
    - `except Exception:` 捕获调用异常。
    - `await self._breaker.observe_outcome(inv, Result.failure("INVOKE_EXCEPTION", "invoke raised"))`
      **异常也记一次熔断失败**（折叠成失败 Result 记数）。
    - `raise  # 异常原样抛回，交降级链处理（不吞异常）`
      **关键**：记数后**原样重抛**，不吞异常。为什么：让上层 `_invoke` 的降级逻辑接手（配了 degradation 会折叠成失败 Result 走降级；没配则继续抛）。
  - **第 210 行** `await self._breaker.observe_outcome(inv, result)` 正常返回也记数。
  - **第 211 行** `return result` 返回结果。

- **第 212 行** `return await call(inv)`
  无熔断器时直接执行包裹链返回（不记数）。

### 第 214-225 行：_apply_recovery 降级选级

- **第 214 行** `# ---- 失败降级选级（领域决策，保四坑）----`

- **第 215-216 行** `def _apply_recovery(self, inv: Invocation) -> str:`
  降级选级：读 fault → Degradation.select（四坑）→ 写 typed target。

- **第 217-218 行** `if self._degradation is None: return ""`
  没配降级策略 → 返回空串（表示无法降级）。

- **第 219 行** `fault = inv.fault()` 读当前 fault（从同源 meta 读，B3）。

- **第 220-221 行** `if fault is None: fault = FaultRecord(reason="invoke_failed", failed_target=failed_target_of(inv))`
  没有 fault 则造一个（reason=`invoke_failed`，带 failed_target R3-7）。保证 select 总有东西可用。

- **第 222 行** `target = self._degradation.select(fault, session_id=inv.session_id)`
  **四坑落点调用**：把 fault 和 session_id 传给降级策略选级（内部做 B1/B2/R3-6）。

- **第 223 行** `inv.write_routing_target(RoutingTarget(target=target, reason=fault.reason))`
  写 typed target（路由目标）。网关从 `inv.routing_target()` 读出它切后端——这是「降级切后端」闭环的关键一步。

- **第 224 行** `self._emit_rejected(inv, fault.reason)` 发拒绝通知（去重过的）。

- **第 225 行** `return target` 返回目标名（可能是 `"refuse"`）。

### 第 227-279 行：_invoke 主流程（一次可靠调用）

- **第 227-228 行** `async def _invoke(self, inv: Invocation) -> Result:`
  一次可靠调用：前置 → 预算内执行 → 失败降级 + 重路由。

- **第 229-231 行** 前置：
  - `refusal = await self._preflight(inv)` 前置判定。
  - `if refusal is not None: return refusal` 有终止拒绝直接返回。

- **第 233-240 行** 前置降级类 fault 处理（限流 DEGRADE / 熔断）：
  - 注释：调 core 前先选级写 typed target，让中心调用直接用降级目标（保 B1/B2）。
  - **第 235 行** `if inv.fault() is not None and inv.routing_target() is None:`
    有 fault（前置写的降级 fault）且还没定目标。
  - **第 236 行** `target = self._apply_recovery(inv)` 先选级。
  - **第 237-238 行** `if target and target != _REFUSE_TARGET: inv.meta.pop("fault", None)  # 已消费，避免重路由重复裁决`
    目标有效（非 refuse）→ **消费掉 fault**（从 meta 移除），防止后面重路由循环重复裁决同一个 fault。
  - **第 239-240 行** `elif target == _REFUSE_TARGET: return Result.failure("REFUSED", "degrade ladder exhausted; refusal")`
    目标是 refuse → 直接返回 `REFUSED`，不调 core（阶梯已耗尽，不编造）。

- **第 242-253 行** 预算内调用（异常折叠判断）：
  - **第 242 行** `if self._degradation is not None:` 配了降级策略。
  - **第 243-244 行** `try: result = await self._budgeted_call(inv)` 执行调用链。
  - **第 245-251 行** 异常分支：
    - `except Exception as exc:`
    - 注释：配了降级策略 → 中心异常折叠为失败 Result + 具名 fault，交降级链切 backup。
    - `inv.write_fault(FaultRecord(reason="invoke_exception", failed_target=failed_target_of(inv)))`
      写 fault（带 failed_target，R3-7）。
    - `result = Result.failure("INVOKE_EXCEPTION", str(exc))` 异常折叠成失败 Result。
  - **第 252-253 行** `else: result = await self._budgeted_call(inv)`
    没配降级策略 → 不捕获，异常保持原样抛出（让调用方决定）。

- **第 254-275 行** 重路由循环（**_MAX_REROUTE=8**）：
  - **第 254 行** `for _ in range(_MAX_REROUTE):` 最多 8 轮，防阶梯成环死循环。
  - **第 255-256 行** `if result.ok: break` 成功就退出循环。
  - **第 257-258 行** `current = inv.routing_target(); existing_fault = inv.fault()` 取当前目标与 fault。
  - **第 259-264 行** 上一轮降级目标仍失败的处理：
    - `if current is not None and current.target != _REFUSE_TARGET:`
      当前有降级目标且不是 refuse。
    - 注释：把它记为新的失败目标继续选级。
    - `inv.write_fault(FaultRecord(reason=(existing_fault.reason if existing_fault else "reroute_failed"), failed_target=current.target))`
      用当前目标作新的 failed_target 写 fault（R3-7）——这就是「上一轮选的 backup 也挂了，把它也剔除」的机制。reason 沿用旧 fault 或 `reroute_failed`。
  - **第 265-266 行** `elif existing_fault is None: break`
    没目标也没 fault → 无从降级，退出。
  - **第 267 行** `inv.meta.pop("routing_target", None)` 清掉旧目标，准备重新选。
  - **第 268 行** `target = self._apply_recovery(inv)` 重新选级。
  - **第 269-270 行** `if not target: break` 无目标（没配降级）→ 退出。
  - **第 271 行** `inv.meta.pop("fault", None)` 消费 fault。
  - **第 272-274 行** 拒答判定：
    - `if target == _REFUSE_TARGET:`
    - `result = Result.failure("REFUSED", "degrade ladder exhausted; refusal")` 返回 REFUSED。
    - `break` 退出循环（**阶梯末级必须 REFUSE 的意义在此**：所有目标剔除干净后落到这里，明确拒答不编造）。
  - **第 275 行** `result = await self._budgeted_call(inv)`
    拿到新目标后重新执行调用链（网关会读 routing_target 切后端）。

- **第 276-279 行** 循环后处理：
  - **第 276 行** `if not result.ok:` 最终仍失败。
  - **第 277 行** `reason = result.error.message if result.error else "invoke failed"` 取失败原因。
  - **第 278 行** `self._emit_rejected(inv, reason)` 发拒绝通知（去重过，只发一次）。
  - **第 279 行** `return result` 返回结果。

### 第 281-289 行：对外两个入口

- **第 281-285 行** `as_call()` 适配 R1：

  - **第 281-282 行** `def as_call(self) -> CallFn:` docstring：产出符合 R1 CallFn 签名的 call（R1 零感知）。
  - **第 283-284 行**
    ```
    async def adapter(effect: Effect, ctx: object) -> Result:
        return await self._invoke(_as_invocation(effect, ctx))
    ```
    R1 传来 `(effect, ctx)`，先 `_as_invocation` 包装（B3 同源），再跑 `_invoke`。R1 完全不知道 reliability 的存在——零感知。
  - **第 285 行** `return adapter` 返回适配器。

- **第 287-289 行** `run_invocation(inv)` 测试入口：
  - **第 287 行** `async def run_invocation(self, inv: Invocation) -> Result:`
  - **第 288 行** docstring：直接对给定 Invocation 跑完整链（测试入口）。
  - **第 289 行** `return await self._invoke(inv)` 直接调主流程，无需适配。

---

## 7. capability.py 逐行讲解（111 行）

### 第 1-7 行：模块 docstring

- **第 1 行** `"""R3 能力装配：ResilienceCapability 用 opt-in 构造注入领域策略，产出可靠性执行器。`
  一句话定位：把显式选的策略装进执行器。

- **第 3-4 行** `插件保留一等身份（name + 实例状态 + 构造注入）；install 按领域把各策略注入 ReliabilityExecutor 的具名字段——没有角色分派、没有槽位列表、没有 Role 枚举。`
  再次强调「无 AOP 角色」：按**具名字段**注入（`kill_switch=`、`retry=`...），不是角色分派、不是槽位列表。

- **第 5 行** `缺席的策略传 None，执行器跳过该领域步骤（无占位实现）。`
  全 opt-in 复述。

- **第 6 行** `扩展点 extra_plugins 只接受函数式 call 变换器（callable），不是角色插件。`
  扩展点形态说明：是函数，不是角色插件。

- **第 7 行** `"""` docstring 结束。

### 第 8-18 行：imports 与类型别名

- **第 8 行** `from __future__ import annotations` 延迟注解。

- **第 10 行** `from collections.abc import Awaitable, Callable, Mapping` 类型注解。

- **第 11 行** `from typing import cast` 为 install 里的 core 类型转换。

- **第 13 行** `from compose.context import Invocation` 类型注解用。

- **第 14 行** `from runtime import Result` 类型注解用。

- **第 16-18 行** `type _InvCall` / `type _Transformer` + docstring
  与 executor.py 同形的类型别名：函数式调用变换器（扩展点形态，非角色插件）。

### 第 20-33 行：策略导入与导出

- **第 20 行** `from .breaker import CircuitBreaker` 熔断器。

- **第 21-28 行** `from .defenses import (...)` 六道防线：Bulkhead / Degradation / Idempotency / KillSwitch / RateLimit / Watchdog。

- **第 29 行** `from .executor import ReliabilityExecutor` 执行器。

- **第 30 行** `from .retry import RetryPolicy` 重试。

- **第 31 行** `from .timeout import Timeout` 超时。

- **第 33 行** `__all__ = ["ResilienceCapability"]` 只导出能力包。

### 第 36-40 行：ResilienceCapability 类元信息

- **第 36 行** `class ResilienceCapability:` 可靠性能力包：把显式选择的领域策略装进可靠性执行器。

- **第 37 行** docstring。

- **第 38 行** `name = "resilience"` 能力名。

- **第 39 行** `version = "6.0.0"` 版本（注意是 6.0.0，与去角色化重构对应）。

- **第 40 行** `requires: tuple[str, ...] = ()` 无前置依赖能力。

### 第 42-69 行：构造（opt-in 装配）

- **第 42-55 行** 构造签名（全部关键字参数 + `| None = None`）：
  - `breaker` / `retry` / `rate_limit` / `bulkhead` / `watchdog` / `killswitch` / `idempotency` / `degradation` / `timeout`：九种策略，全 opt-in。
  - **第 54 行** `extra_plugins: tuple[_Transformer, ...] = ()`：函数式变换器。

- **第 56 行** docstring：传实例才装，不传即缺席。

- **第 57-65 行** 逐个保存：`self._kill_switch` / `_watchdog` / `_idempotency` / `_rate_limit` / `_bulkhead` / `_breaker` / `_retry` / `_timeout` / `_degradation`。注意参数名 `killswitch` 保存为 `_kill_switch`（下划线位置不同）。

- **第 66 行** `self._extra = tuple(extra_plugins)` 变换器转 tuple 固定。

- **第 67-69 行** extra 校验：
  - `for p in self._extra:` 遍历每个扩展。
  - `if not callable(p): raise ValueError(f"extra plugin must be a call transformer: {p!r}")`
    **构造期校验**：extra_plugins 必须是 callable（函数式变换器）。为什么：错误配置尽早暴露，别等运行时调用才炸。报错带 `!r` 显示具体是哪个对象。

### 第 71-102 行：install 装配执行器

- **第 71-78 行** 签名：
  - `core`：中心 Port 调用（`Callable | object`，因为可能传一个 callable 对象）。
  - `registry=None` / `bus=None` / `notify=None`：基础设施注入。
  - 返回 `ReliabilityExecutor`。

- **第 79-86 行** docstring：参数说明。

- **第 87-102 行** `return ReliabilityExecutor(...)` 构造执行器，逐个具名字段注入：
  - **第 88 行** `cast(Callable[[Invocation], Result | Awaitable[Result]], core)`
    用 `cast` 把 core 的类型收窄——`cast` 只是类型提示，运行时不改值。因为 core 签名允许 `object`，这里告诉类型检查器它实际是可调用的。
  - **第 89-91 行** `registry=registry` / `bus=bus` / `notify=notify` 基础设施。
  - **第 92-101 行** 九种策略按具名字段注入：`kill_switch=self._kill_switch` / `watchdog=` / `idempotency=` / `rate_limit=` / `bulkhead=` / `breaker=` / `retry=` / `timeout=` / `degradation=`。**这就是「按具名字段注入，无角色分派」的代码现场**。
  - **第 102 行** `transformers=self._extra` 变换器注入。

### 第 104-110 行：plugins 装配核验

- **第 104 行** `def plugins(self) -> tuple[object, ...]:`
  已选领域策略（内置 + extra），顺序确定（供装配核验）。

- **第 105 行** docstring。

- **第 106-109 行** `builtin = (...)`
  按固定顺序列出九个内置策略：`_kill_switch` / `_watchdog` / `_idempotency` / `_rate_limit` / `_bulkhead` / `_breaker` / `_retry` / `_timeout` / `_degradation`。

- **第 110 行** `return tuple(p for p in builtin if p is not None) + self._extra`
  过滤掉 None（未装配的），拼上 extra。**顺序确定**：killswitch → watchdog → idempotency → rate_limit → bulkhead → breaker → retry → timeout → degradation → extra。为什么顺序确定：装配核验、日志、测试断言都依赖稳定顺序，不能靠 dict/集合的随机顺序。

---

## 8. __init__.py 逐行讲解（49 行）

### 第 1-6 行：模块 docstring

- **第 1 行** `"""R3 可靠性能力：失败分类 / 重试 / 熔断 / 降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。`
  一句话列出九种能力。

- **第 3-4 行** `每个策略以自身领域方法参与，由 ReliabilityExecutor 显式编排（无编排角色、无角色协议）；全部基于 R1（runtime）构造注入，内核零策略（R3 §1）。`
  包级设计宣言复述：无角色、显式编排、构造注入、内核零策略。

- **第 5 行** `公共 API 见 docs/02-模块详设/Agent/R3-可靠性.md §4。` 指路。

- **第 6 行** `"""` docstring 结束。

### 第 7 行：延迟注解

- **第 7 行** `from __future__ import annotations` 延迟注解。

### 第 9-35 行：包级重导出

- **第 9 行** `from .breaker import BreakerStore, CircuitBreaker, InMemoryBreakerStore`
  熔断三件套：协议 + 实现 + 内存 store。

- **第 10 行** `from .capability import ResilienceCapability` 能力包。

- **第 11-14 行** `from .defenses import (...)`
  六道防线 + RatePermit：Bulkhead / Degradation / Idempotency / KillSwitch / RatePermit / RateLimit / Watchdog。

- **第 15 行** `from .executor import ReliabilityExecutor` 执行器。

- **第 16 行** `from .retry import RetryPolicy` 重试。

- **第 17 行** `from .timeout import Timeout` 超时。

- **第 18-35 行** `from .types import (...)`
  共享类型：DEFAULT_FAILURE_TABLE / BreakerState / BreakerStatus / DegradeLadder / DegradeMode / DegradeStep / DimensionKey / FailureClass / FailureClassifier / Jitter / RetrySpec / backoff_delay / classify_failure / normalize_code / validate_ladder / validate_retry_spec。

  **包级 API 收敛**：外部只需 `from harness.resilience import CircuitBreaker`，不必知道内部文件路径（`breaker.py`）。改内部文件结构时，只要这里同步，外部调用方不受影响。

### 第 37-48 行：__all__ 公共导出面

- **第 37 行** `__all__ = [` 开始。

- **第 38 行** 注释：`# 共享类型`。

- **第 39-41 行** 共享类型一族：FailureClass / FailureClassifier / DimensionKey / DEFAULT_FAILURE_TABLE / backoff_delay / classify_failure / normalize_code / validate_retry_spec / validate_ladder。

- **第 42 行** DegradeMode / DegradeStep / DegradeLadder / Jitter / RetrySpec。

- **第 43 行** BreakerStatus / BreakerState / BreakerStore / InMemoryBreakerStore。

- **第 44 行** 注释：`# 插件（RetryPolicy 是重试策略配置，非状态 Policy）`——特别点明 RetryPolicy 是配置而非带状态的 Policy，避免误解。

- **第 45-47 行** 插件一族：RetryPolicy / CircuitBreaker / Degradation / RateLimit / Bulkhead / Idempotency / Watchdog / KillSwitch / ResilienceCapability / Timeout / RatePermit / ReliabilityExecutor。

- **第 48 行** `]` 结束。`__all__` 定义了 `from harness.resilience import *` 的行为，也是文档化公共 API 的清单。

---

## 9. 逐行讲解后的整体心智图

```
一次可靠调用 = ReliabilityExecutor._invoke(inv)

  ┌─ _preflight 前置放行（各策略各自领域方法，无统一协议）──────────┐
  │  KillSwitch.is_tripped  → 置位           → 终止拒绝              │
  │  Watchdog.detect        → 重复/AB交替    → 终止拒绝              │
  │  Idempotency.check_mark → 窗口内重复     → 终止拒绝（有键才查）  │
  │  RateLimit.acquire      → REJECT         → 终止拒绝              │
  │                         → DEGRADE        → 写 fault（带 failed_target）放行 │
  │  Breaker.permit         → OPEN/半开超额  → 写 fault 放行         │
  └──────────────────────────────────────────────────────────────────┘
                              │ 无终止拒绝
                              ▼
  ┌─ _budgeted_call 预算内调用（闭包逐层包裹，默认参数绑定避免晚绑定）┐
  │  transformer(extra)  最外   ← transform(call)                     │
  │    bulkhead          ← run_in_slot（占槽/finally 释放）          │
  │      retry           ← run_with_retries（退避/幂等红线/耗尽写fault）│
  │        timeout       ← wait_for（超时写 fault）                   │
  │          core = _await_result(self._core)  最内                   │
  │  熔断记数：observe_outcome(成功 or 异常折叠)，异常原样重抛        │
  └──────────────────────────────────────────────────────────────────┘
                              │ 成功→break；失败→重路由
                              ▼
  ┌─ 失败降级 + 重路由（最多 _MAX_REROUTE=8）────────────────────────┐
  │  异常折叠写 fault（invoke_exception）                            │
  │  上一轮目标也失败 → 记为新 failed_target 写 fault（R3-7）        │
  │  _apply_recovery:                                                │
  │    fault → Degradation.select(session_id)                        │
  │      ├─ B1：本会话独立 available 副本                            │
  │      ├─ R3-6：failed_target 永久剔除并写回                       │
  │      └─ pick quality 最高 → target（B2 注册名）                  │
  │    write_routing_target(RoutingTarget(target, reason))           │
  │  目标 == "refuse" → REFUSED（末级拒答，不编造）                  │
  │  否则清旧 target/fault → 重新 _budgeted_call（网关读 target 切后端）│
  └──────────────────────────────────────────────────────────────────┘

关键数据流（B3 meta 同源）：
  前置/插件写 fault、typed target ──同一份 meta──▶ 网关 _extract_target 读出
  _as_invocation 复用 ctx（meta 不复制）保证这条链不断。
```

---

## 10. 细节速记卡（逐行精华）

**架构纪律**

- **无 AOP 角色**：没有角色协议/枚举/槽位列表/统一方法名；每个策略只暴露自己的领域方法，执行器显式编排（executor.py 第 3-7 行）。
- **全 opt-in**：九种策略全构造注入，传实例才参与，None 即缺席——不写空实现占位（executor.py 第 90-107 行；capability.py 第 42-55 行）。唯一空实现是 notify 观察回调（executor.py 第 112 行），因它不影响控制流。
- **按具名字段注入**：install 逐个 `kill_switch=`/`retry=`/... 传值，无角色分派（capability.py 第 92-101 行）。
- **内核零策略 + 时钟/随机显式注入**：可测试、可回放（defenses.py 第 7 行；types.py 第 6 行）。

**包裹与闭包**

- **包裹顺序**：timeout 最内 → retry 居中 → bulkhead 最外 → transformer 更外（executor.py 第 182-200 行）。
- **闭包晚绑定用默认参数解决**：`def call(i, _p=inner, _t=timeout)` 在定义时快照当前值，否则所有层指向最后一层（executor.py 第 186/191/196 行）。
- **熔断异常原样重抛**：记数后 `raise`，不吞，交降级链；同时 observe_outcome 记一次失败（executor.py 第 205-209 行）。

**终止 vs 放行**

- **终止拒绝**：killswitch / watchdog / idempotency / rate REJECT → `_reject` 返回 INVOKE_REJECTED，不调 core（executor.py 第 148-163 行）。
- **降级放行**：rate DEGRADE / breaker OPEN → 写 fault（带 failed_target）后继续（executor.py 第 164-172 行）。
- **invoke_rejected 每次调用只发一次**：meta `_rejected_emitted` 标记去重（executor.py 第 127-129 行）。
- **_MAX_REROUTE=8**：防阶梯成环死循环（executor.py 第 42 行）。

**四坑 + B3**

- **B1 session 隔离**：每个会话独立 available 副本，缺则按初始集新建；一个会话剔除不影响另一个（defenses.py 第 132-142 行）。
- **B2 failed_target 是注册名**：由 `failed_target_of` 派生（payload.model 或 "primary"），非熔断维度二元组（defenses.py 第 32-43 行）。
- **R3-6 永久剔除**：`available.discard(failed_target)` 后**写回**会话状态表，非局部副本（defenses.py 第 159-162 行）。
- **R3-7 fault 必带 failed_target**：timeout / retry / bulkhead / breaker放行 / 异常折叠各处写 FaultRecord 都带（timeout.py 第 45-47 行；retry.py 第 72-74 行；defenses.py 第 244 行；executor.py 第 165/172/248/261 行）。
- **B3 meta 同源**：`_as_invocation` 复用 ctx 的 meta 对象（不复制），保证前置写的 fault 网关能读到（executor.py 第 64-74 行）。

**失败分类（types.py）**

- **一次分类共用**：`classify_failure` 同时给 retryable 与 count_toward_breaker，避免两插件语义漂移（types.py 第 4-5 行；第 234-246 行）。
- **429 不熔断**：RATE_LIMITED = (retryable=True, count_toward_breaker=False)（types.py 第 121 行）。
- **schema 非法可重试但不熔断**：SCHEMA_INVALID = (True, False)（types.py 第 132 行）。
- **未知码尊重 retryable 但不计熔断**：保守，不误伤熔断统计（types.py 第 228-230 行）。
- **归一化**：先 strip+upper → 查别名表 → 5xx 正则兜底 → 未知原样返回（types.py 第 185-190 行）。

**退避（types.py）**

- **四种 jitter**：NONE=raw / FULL=uniform(0,raw) 最常用 / EQUAL=raw/2+uniform(0,raw/2) / DECORRELATED=min(max, prev×uniform(1,3))（types.py 第 315-324 行）。
- **未知 jitter 回退 FULL**：安全防风暴（types.py 第 91-92 行；第 314 行）。
- **base_delay=0 + jitter=none 装配期拒绝**：同样防风暴（types.py 第 285-286 行）。
- **Retry-After 取 max**：尊重对方背压又不失打散（types.py 第 325-326 行）。

**降级阶梯（types.py）**

- **DegradeMode 四值**：REROUTE / CACHE / FALLBACK_VALUE / REFUSE（禁止编造）（types.py 第 336-339 行）。
- **阶梯末级必须 REFUSE**：装配期强校验，防目标剔除干净后编造（types.py 第 398-399 行）。
- **无环 + quality 严格递减**：target 重复成震荡，quality 不递减无法收敛（types.py 第 400-408 行）。
- **pick 选 quality 最高，无候选返回临时 REFUSE**：fail-closed 不编造（types.py 第 378-386 行）。

**熔断三态（breaker.py）**

- **CLOSED→OPEN**：样本≥5 且失败率≥0.5，记 opened_at（breaker.py 第 191-196 行）。
- **OPEN→HALF_OPEN**：冷却期满（now-opened_at≥cooldown），清窗口/成功数/opened_at（breaker.py 第 154-158 行）。冷却期内返回 "breaker_open"（第 134-135 行）。
- **HALF_OPEN→CLOSED**：连续成功 2 次，重置为初始状态（breaker.py 第 180-181 行）。
- **HALF_OPEN→OPEN**：任一失败，记新 opened_at 重新冷却（breaker.py 第 184-188 行）。
- **半开探测上限 3**：窗口≥3 返回 "breaker_half_open"（breaker.py 第 138-140 行）。
- **每维度一把 asyncio.Lock**：load→决策→save 全程持锁，与 record 互斥（breaker.py 第 105-113 行；第 131 行）。
- **per_dimension=False 归并空维度**：所有调用共享一个状态（breaker.py 第 117-119 行）。

**其它防线**

- **幂等红线**：TOOL 副作用无 idempotency_key 禁止重试，返回 IDEMPOTENCY_REQUIRED（retry.py 第 76-79 行；第 88-95 行）。idempotency_required=False 可显式 opt-out。
- **watchdog 重复 + AB 交替检测**：末尾 repeat_k 个全同（set 长度=1）/ 末尾 4 个 t0==t2 && t1==t3 && t0!=t1（defenses.py 第 315-322 行）。
- **killswitch 双键**：`kill:global` 与 `kill:{scope}` 任一置位即触发（defenses.py 第 352-353 行）。
- **令牌桶**：`min(burst, tokens + elapsed×rate)`，elapsed 用 max(0,...) 防时钟回拨（defenses.py 第 189-190 行）。
- **舱壁 try/finally 保证释放**：release 用 max(0,...) 防重复释放变负（defenses.py 第 237 行；第 245-248 行）。
- **_ExceedPolicy/coerce 未知回退 DEGRADE**：fail-closed（defenses.py 第 82-83 行）；**_KillScope 未知回退 SESSION**：最小权限（第 98-99 行）。

---

*本册是 resilience 模块的逐行讲解版。其余模块按同一格式：「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」。下一册：[07 memory 模块精读](./07-memory模块精读.md)。*





