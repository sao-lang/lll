# 编码规则 · Vue

> 框架特定规则，与 `references/03-coding-rules/coding-rules-common.md`、`references/03-coding-rules/coding-rules-typescript.md` 一起遵守。Vue 3。

## 注释方案（TSDoc + SFC 说明，中文）

- SFC 顶部写组件职责注释；`<script setup>` 中 props / emits / 关键 ref / computed 逐项中文注释。
- defineProps / defineEmits 的类型与默认值写清语义；复杂 watch / computed 依赖注释"为何依赖这些"。
- 模板中复杂 `v-if / v-else-if / v-else`、`v-for` 按 common §4 用注释说明分支意图与迭代语义。

```
/** 披露层级切换器：按命中意图在 L1 目录 / L2 schema / L3 正文间切换。
 * @remarks level 由父组件控制，本组件仅派发事件不改状态。
 */
const props = defineProps<{ level: DisclosureLevel; names: string[] }>()
/** 命中工具名变化时通知父组件加载 L2 */
const emit = defineEmits<{ (e: 'hit', names: string[]): void }>()
```

## 类型

- defineProps / defineEmits 用泛型类型声明，禁 `any`；ref/computed 显式标注。
- 状态用判别联合；跨组件数据用 provide/inject 时提供 InjectionKey 类型。

## 禁魔法字符串

- 事件名、props 名、provide key、localStorage key、组件 name 一律具名常量或类型联合。
- 状态字符串用枚举/联合类型，不裸写。

## 格式与长度

- 行宽 100；单 SFC ≤ 200 行，单函数 ≤ 50 行；超出拆子组件 / composable。
- 逻辑抽到 `composables/useXxx.ts`，模板只做渲染。

## 推荐语法与模式

Composition API + `<script setup>`；`defineModel`、`toValue`、`watchEffect` 慎用并注释；
composable 复用跨组件逻辑；`v-memo` 仅在实测必要时用。

## 验证命令（S4 必跑，退出码为证据）

```bash
prettier --check .                 # 格式
eslint . --max-warnings=0          # lint（vue 插件规则）
vue-tsc --noEmit                   # 类型
vitest run --coverage              # 测试 + 覆盖率
```
