# 编码规则 · Go

> 语言特定规则，与 `coding-rules-common.md` 一起遵守。目标版本：Go 1.22+。

## 注释方案（Go Doc Comment，中文）

- 所有导出标识符（函数 / 类型 / 方法 / 常量 / 变量 / 包）必须有 godoc 注释，**以标识符名开头**：
  `// Reserve 调用前原子预留预算额度。...`
- 包注释在 `package` 上方写整体职责。
- 复杂 `switch` / `for` / `select` 按 common §4 逐分支 / 逐循环注释，尤其 `select` 的每个 case 含义。

```
// Reserve 调用前原子预留预算额度。
// 负数按 0 处理；预算不足返回 nil 而非错误（正常路径）。
func (g *CostGuard) Reserve(estimatedCost float64) *Reservation { ... }
```

## 类型

- 签名与结构体字段全部显式类型；禁裸 `any` 作公开签名（仅 JSON 边界 `json.RawMessage` 或边界 type）。
- 用判别式结构体 + 方法表达状态，避免 `map[string]any` 传递业务数据。
- 错误用 `error` + 自定义类型，禁字符串错误做控制流判断（用 `errors.Is` / `errors.As`）。

## 禁魔法字符串

- typed const 定义枚举：`type Status string; const ( StatusActive Status = "active" )`。
- iota 用于数值枚举；配置键 / 事件名 / 前缀一律 `const`。
- 比较用 typed const，不裸写 `"active"`；错误码用 `var ErrX = errors.New("...")` 具名。

## 格式与长度

- gofmt 自动格式（无行宽硬限，惯例 ≤ 120）；单函数 ≤ 50 行（Go 惯例短小），单文件 ≤ 400 行。
- 工具：gofmt（格式）、go vet（静态）、staticcheck（lint + 复杂度）、golangci-lint（集成）。

## 推荐语法

range over int / func（1.22）、`min`/`max`/`clear` 内置、泛型、`errors.Join`、
`for range` 循环变量新语义、`slices`/`maps` 标准库。

## 验证命令（S4 必跑，退出码为证据）

```bash
gofmt -l .                         # 格式（有输出即失败）
go vet ./...                       # 静态检查
staticcheck ./...                  # lint
go test ./... -cover               # 测试 + 覆盖率
```
