# 编码规则 · Dart

> 语言特定规则，与 `references/03-coding-rules/coding-rules-common.md` 一起遵守。Dart 3.x / Flutter。

## 注释方案（dartdoc，中文）

- 公开 API（库 / 类 / 构造函数 / 方法 / 函数 / 类型 / 字段）写 `///` 文档注释；首行一句摘要，空行后展开参数 / 返回 / 异常。
- 库级用 `///` 在 `@dart` 指令后写整体职责。
- 复杂 `switch` / `if-else` / 循环按 common §4 逐分支 / 逐循环注释。

```
/// 调用前原子预留预算额度。
///
/// 返回 [Reservation]；预算不足返回 null（正常路径，非异常）。
Reservation? reserve(double estimatedCost) { ... }
```

## 类型

- 签名与字段全部显式类型；禁裸 `dynamic`（公开签名），仅 JSON 边界用 `Object?` + 类型守卫收窄。
- 用 sealed class + 模式匹配表达代数数据类型；判别联合建模状态。
- 空安全：不用 `!` 强解引用（除非已证明并用注释说明），优先 `?.` / `??` / 类型提升。

## 禁魔法字符串

- 用 `enum Status { active }` 或 `static const String kPrefix = 'cache:'`。
- 状态 / 路由名 / 事件名 / 共享偏好 key 一律枚举或 const；比较用枚举，不裸写字符串。
- 数值阈值 `static const double kDefaultTimeout = 30.0`。

## 格式与长度

- dart format 默认行宽 80；单函数 ≤ 50 行，单文件 ≤ 300 行，圈复杂度 ≤ 10。
- 工具：dart format（格式）、dart analyze（静态 + lint）、very_good_analysis 或 flutter_lints。

## 推荐语法

records、patterns / destructuring、sealed class、`extension` type（3.3）、`switch` expressions、
`async` 生成器、`late` 慎用并注释、`unawaited` 显式标记。

## 验证命令（S4 必跑，退出码为证据）

```bash
dart format --set-exit-if-changed .  # 格式（有变更即失败）
dart analyze                         # 静态 + lint
dart test                            # 测试
# Flutter 项目追加：flutter test --coverage
```
