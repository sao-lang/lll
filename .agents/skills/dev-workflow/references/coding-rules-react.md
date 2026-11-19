# 编码规则 · React

> 框架特定规则，与 `coding-rules-common.md`、`coding-rules-typescript.md` 一起遵守。React 18/19。

## 注释方案（TSDoc + 组件说明，中文）

- 每个组件顶部写 TSDoc：职责、Props 语义、副作用、数据流方向。
- Props / 状态字段逐项中文注释；复杂 hook 依赖、useEffect 触发条件必须注释"为何依赖这些"。
- 复杂条件渲染 / 列表渲染按 common §4 逐分支 / 逐循环注释。

```
/** 工具调用卡片：渲染单次工具调用的状态与结果。
 *
 * @remarks danger 决定徽标颜色；result 为 null 时显示加载态。
 * 不入 messages，独立托管渲染。
 */
export function ToolCallCard({ call, result }: ToolCallCardProps) { ... }
```

## 类型

- Props / State / Ref / 事件处理器全部显式类型；禁 `any`，children 用 `ReactNode`。
- 判别联合建模状态（`{status:"loading"} | {status:"success"; data:T} | {status:"error"; err:E}`）。

## 禁魔法字符串

- 组件名、事件名、data-testid、localStorage key、CSS class 语义名一律具名常量。
- 状态字符串用联合类型/枚举，不用裸 `'loading'` / `'done'` 散落。

## 格式与长度

- 行宽 100；组件文件 ≤ 150 行，单函数 ≤ 50 行；超出拆子组件 / 自定义 hook。
- 自定义 hook 抽离跨组件状态逻辑；useEffect 依赖数组完整且解释清楚。

## 推荐语法与模式

函数组件 + Hooks；`use()`（19）、Server Components 语义、`useMemo`/`useCallback` 仅在实测必要时用、
受控组件、错误边界、lazy + Suspense 做代码分割。

## 验证命令（S4 必跑，退出码为证据）

```bash
prettier --check .                 # 格式
eslint . --max-warnings=0          # lint（含 react-hooks 规则）
tsc --noEmit                       # 类型
vitest run --coverage              # 测试 + 覆盖率
```
