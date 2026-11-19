# 编码规则 · TypeScript

> 语言特定规则，与 `references/03-coding-rules/coding-rules-common.md` 一起遵守。目标版本：TypeScript 5.x。

## 注释方案（TSDoc，中文）

- 导出 API / 函数 / 类 / 接口 / 类型 / 字段写 TSDoc `/** ... */`；首行一句摘要，空行后 `@param` / `@returns` / `@throws` / `@remarks`。
- 复杂控制流按 common §4 模板逐分支 / 逐循环注释。

```
/** 调用前原子预留预算额度。
 *
 * @param estimatedCost 预估成本，负数按 0 处理。
 * @returns 预留凭证；预算不足返回 null。
 * @remarks 并发下在读改写原子操作里占用额度。
 */
reserve(estimatedCost: number): Reservation | null { ... }
```

## 类型

- 严格模式：`strict: true`、`noImplicitAny`、`exactOptionalPropertyTypes`。
- 禁 `any` / `unknown` 滥用（公开签名禁 `any`）；JSON 边界用 `type JsonValue = ...` 具名别名。
- 外部不可信数据用 zod / io-ts 等运行时校验收敛为静态类型，不用 `as` 强转。

## 禁魔法字符串

- 用 `const X = {...} as const` + `type X = ValueOf<typeof X>`，或原生 `enum`。
- 配置键、事件名、路由 key、action 名一律具名；比较用枚举成员，不裸写字符串。
- 字符串 key 构造改为函数，如 `cacheKey(kind, version, identity)`。

## 格式与长度

- 行宽 100（Prettier `printWidth`）；单函数 ≤ 50 行，单文件 ≤ 300 行，圈复杂度 ≤ 10。
- 工具：Prettier（格式）、ESLint（lint + 复杂度 + no-magic-numbers）、tsc（类型）。

## 推荐语法

`satisfies`、`const` 泛型参数、模板字面量类型、 discriminatory union、`using`/`Symbol.dispose`、
`import type`、可选链与 nullish 合并。

## 验证命令（S4 必跑，退出码为证据）

```bash
prettier --check .                 # 格式
eslint . --max-warnings=0          # lint + 复杂度 + 魔法值规则
tsc --noEmit                       # 类型
vitest run --coverage              # 测试 + 覆盖率
```
