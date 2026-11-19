# 编码规则 · 通用铁律

> 跨语言通用约束，由 `conductor` 在 S3（实现阶段）按需加载。
> 与对应语言的 `coding-rules-<lang>.md` 一起遵守；两者冲突时以语言文件为准。
> 本文件是**门禁**，不是建议——违反后果见 §6 强制机制。

## 0. 术语与注释方案约定

- **魔法值**：未命名的字面量，直接出现在逻辑中且含义不可自解释。
- **魔法字符串**：未定义的字符串字面量，用于状态、类型、配置键、错误码、路由 key、事件名等语义场景。
- **具名常量 / 枚举**：以命名符号承载语义，逻辑中只引用符号、不重复字面量。

注释统一"来源居中"方案：中文描述意图与契约，专有名词保留英文。docstring / doc comment 首行一句摘要（供 IDE hover），空行后展开细节。

---

## 1. 类型原则（禁动态类型）

1. 参数、返回、字段、变量一律标注明确类型。
2. 禁止 `Any` / `dynamic` / `interface{}` / `Object` / `DynamicValue` 等动态类型作为**函数签名与公开字段**类型。
3. 仅在数据结构确实无法静态描述时（任意 JSON、外部 vendor payload、反射边界），用**具名别名**集中定义一处：
   - TS: `type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue }`
   - Python: `type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]`
   - Go: 用 `json.RawMessage` 或边界处 `type Payload map[string]any`
   - Rust: `serde_json::Value`（仅反序列化边界）
   - Dart: `Object?` 仅在 JSON 边界，配合类型守卫收窄
4. 禁止裸 `Any` 散落；每次使用需在注释写明"为何无法静态描述"。

---

## 2. 禁魔法字符串 / 魔法值（硬规则）

**问题**：字符串字面量散落在 `if status == "active"`、`if kind == "user"`、`cache.get("prefix_"+...)`、
`raise ValueError("...")` 等处——改名要全局搜索、拼错不报错、IDE 无法跳转、语义无处可查。

**强制要求**：

1. **语义字符串必须具名**：状态、类型、枚举值、配置键、路由 key、事件名、错误码、HTTP header、permission、feature flag 等一切有业务含义的字符串，一律用枚举或具名常量定义，逻辑中只引用符号。
2. **按语言选择载体**：
   - Python: `class Status(StrEnum): ACTIVE = "active"`（`enum.StrEnum`，3.11+）
   - TS: `const Status = { Active: "active" } as const; type Status = ValueOf<typeof Status>`，或原生 `enum`
   - Go: `type Status string; const ( StatusActive Status = "active" )`（typed const）
   - Rust: `enum Status { Active }` 或 `const PREFIX: &str = "cache:"`
   - Dart: `enum Status { active }` 或 `static const String kPrefix = 'cache:'`
3. **数值魔法值同理**：超时、阈值、重试次数等用具名常量（`const DEFAULT_TIMEOUT_S = 30.0`），禁止裸 `30.0` 散落。
4. **允许的例外**（需在常量处注释说明，不得裸写）：
   - 结构性字面量：JSON 键名在 schema 定义处、正则模式、格式串、SQL 片段——集中于一个定义点。
   - 一次性展示文本：UI 固定文案、日志模板（若复用则仍应具名）。
   - 测试断言中的期望字面量（可读性优先，允许直接写）。
5. **字符串拼接构造 key/前缀禁止**：改为常量 + 格式化函数，如 `cache_key(kind, version, identity)`。

**反例 → 正例**：
```
# 反例
if user.status == "active" and role == "admin":
    audit.record("user_action", ...)

# 正例
class UserStatus(StrEnum): ACTIVE = "active"
class Role(StrEnum): ADMIN = "admin"
class AuditEvent(StrEnum): USER_ACTION = "user_action"
if user.status is UserStatus.ACTIVE and role is Role.ADMIN:
    audit.record(AuditEvent.USER_ACTION, ...)
```

---

## 3. 注释完整性

1. 每个函数 / 方法 / 类 / 结构体 / 类型 / 字段 / 枚举 / 关键变量写完整中文注释。
2. docstring 首行一句摘要（用于 L1 目录 / IDE hover），空行后展开：参数、返回、异常、副作用、不变量。
3. **复杂控制流必须逐分支 / 逐循环说明意图**（模板见 §4）。
4. 注释解释"为什么"与"契约"，不翻译代码；禁止无信息量的 `* @return the result`。

---

## 4. 复杂控制流注释模板

**多分支（if / else if / else、switch / match）**
```
说明判定依据：比较什么、优先级顺序、每个分支代表的业务状态
- 分支 A：触发条件 → 为何走这里 → 结果含义
- 分支 B：...
- 兜底 else/default：为何这样兜底（fail-closed 还是 fail-open）
```

**多 if 顺序执行（非互斥）**
```
说明：这些 if 是独立检查还是累积状态？先后顺序是否影响结果？
```

**循环（for / while / 列表推导式 / 嵌套）**
```
说明：迭代对象、循环不变量、终止条件、每轮动作、为何用推导式/为何展开写
嵌套循环：逐层说明外层与内层职责
```

**正则 / 状态机 / 递归**
```
说明：匹配目标、状态迁移图、递归基与收敛条件
```

---

## 5. 长度与复杂度基线

> 说明：跨语言没有统一的"官方行数上限"。以下是各语言格式化工具默认值 + 业界通行基线，
> 作为可配置约束；项目已有更严规则时以项目为准。

| 项 | 基线 | 来源性质 |
|---|---|---|
| 单行字符数 | 见各语言文件（80/100/120） | 格式化工具默认值 |
| 单函数行数 | ≤ 50 行 | 业界通行（含 docstring） |
| 单文件行数 | ≤ 300–500 行 | 业界通行 |
| 圈复杂度 | ≤ 10 | linter 默认值 |
| 嵌套深度 | ≤ 3 层 | 通行 |
| 参数个数 | ≤ 5（超出改参数对象） | 通行 |

超限处理：先拆函数/模块，再考虑例外；例外需在 overview 显式登记理由。

---

## 6. 强制机制（三道锁）

规则从"建议"变"门禁"，靠三道锁，缺一不可：

- **锁 1 · 写码前（流程约束）**：本会话首次写码前必须先读 `references/03-coding-rules/coding-rules-common.md` + 当前语言文件；未读禁止产生代码。**违反后果**：该步无效、回退重做。
- **锁 2 · 写码中（生成约束）**：每写一个函数/类/循环，边写边对照注释模板、类型原则、禁魔法字符串、长度基线，不攒到最后改。**违反后果**：S4 必被 linter 拦下，返工成本更高。
- **锁 3 · 写码后（机器门禁，真强制）**：跑 formatter --check + linter + type checker + 注释检查，全绿才算达标。**违反后果**：退出码非 0 → 禁止进 review、禁止标完成。

**诚实边界**：锁 1/2 靠 prompt 自律（可能打折）；锁 3 靠工具退出码（客观强制）。三锁叠加才可靠。
**例外**：必须显式声明并写入 overview（生成代码 / 第三方文件 / vendor 边界），禁止以"技术债"名义静默放行。

各语言的验证命令清单见对应 `coding-rules-<lang>.md` 的"验证命令"一节。

---

## 7. 语法现代化

1. 使用当前运行时稳定版的最新语法，但以可读性为准，不为炫技牺牲兼容性。
2. 优先用语言内置能力替代第三方库（如 `dataclass`、`satisfies`、range-over-func、records）。
3. 弃用写法不引入新代码；遇到旧写法在改动范围内顺带更新（重构场景除外，避免扩大 diff）。
