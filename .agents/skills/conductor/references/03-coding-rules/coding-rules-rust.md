# 编码规则 · Rust

> 语言特定规则，与 `references/03-coding-rules/coding-rules-common.md` 一起遵守。Edition 2021/2024。

## 注释方案（rustdoc，中文）

- 公开项用 `///` 文档注释；首行一句摘要，空行后 `# Examples` / `# Panics` / `# Errors` / `# Safety`。
- crate 级用 `//!` 写整体职责。
- 复杂 `match` / `if let` / 循环按 common §4 逐分支 / 逐循环注释；`unsafe` 块必须写 `# Safety` 说明不变量。

```
/// 调用前原子预留预算额度。
///
/// # Errors
/// 预算不足返回 Err(BudgetExceeded)，不发起后端请求。
pub fn reserve(&mut self, estimated_cost: f64) -> Result<Reservation, BudgetError> { ... }
```

## 类型

- 签名与字段全部显式类型；禁裸 `Any`（Rust 本身强类型，仅需避免 `Box<dyn Any>` 滥用）。
- JSON 边界用 `serde_json::Value` 并尽快反序列化为具体结构体。
- 用 `Result<T, E>` 表达可失败，禁 `unwrap`/`expect` 于生产路径（测试可用）。

## 禁魔法字符串

- 用 `enum` + `strum` 或手写 `as_str()`；常量 `const PREFIX: &str = "cache:"`。
- 状态 / 事件名 / 错误码一律枚举或 const；比较用枚举变体，不裸写字符串。
- 数值阈值 `const DEFAULT_TIMEOUT_S: f64 = 30.0`。

## 格式与长度

- rustfmt 默认 `max_width = 100`；单函数 ≤ 50 行，单文件 ≤ 400 行，圈复杂度 ≤ 10。
- 工具：rustfmt（格式）、clippy（lint）、cargo check（类型）、cargo test（测试）。

## 推荐语法

`let-else`、`if let` chains、`impl Trait`、async fn in trait、`#[derive]`、`Iterator` 组合子、
`match` 穷尽性、`ThisError`/`anyhow` 分层错误。

## 验证命令（S4 必跑，退出码为证据）

```bash
cargo fmt --check                  # 格式
cargo clippy -- -D warnings        # lint（警告即失败）
cargo check                        # 类型
cargo test --all-features          # 测试
```
