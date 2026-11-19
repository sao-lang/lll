# 编码规则 · Python

> 语言特定规则，与 `references/03-coding-rules/coding-rules-common.md` 一起遵守。目标版本：Python 3.12+。

## 注释方案（Google Style docstring，中文）

- 模块 / 类 / 函数 / 方法全部写 docstring；首行一句摘要，空行后 `Args:` / `Returns:` / `Raises:` / `Yields:` / `Note:`。
- 公开 API 必须有 docstring；私有函数至少首行摘要。
- 复杂控制流按 common §4 模板逐分支 / 逐循环注释。

```
def reserve(self, estimated_cost: float) -> Reservation | None:
    """调用前原子预留预算额度。

    Args:
        estimated_cost: 预估成本，负数按 0 处理。

    Returns:
        预留凭证；预算不足返回 None。

    Raises:
        无——预算不足是正常路径，折叠为 None。
    """
```

## 类型

- 全部签名标注类型；用 `X | Y` 联合、`list[T]`、`dict[K, V]`，`from __future__ import annotations` 顶置。
- 禁裸 `Any`；JSON 边界用 `type JsonValue = ...` 具名别名（见 common §1）。
- 动态形态用 pydantic `BaseModel` 建模，`model_config = ConfigDict(extra="forbid")`。

## 禁魔法字符串

- 用 `class Status(StrEnum)`（3.11+ `enum.StrEnum`）定义状态 / 类型 / 事件名 / 错误码。
- 常量用模块级 `UPPER_SNAKE`；配置键集中在 `class Keys:` 或 `Final` 常量。
- 比较用 `is`（枚举成员），不用 `==` 裸字符串。

## 格式与长度

- 行宽 88（Black 默认）；docstring 72。
- 单函数 ≤ 50 行，单文件 ≤ 500 行，圈复杂度 ≤ 10。
- 工具：Black（格式）、Ruff（lint + 复杂度）、mypy --strict（类型）、pydocstyle（docstring）。

## 推荐语法

`match/case`、`type` 语句、`dataclass(slots=True, frozen=True)`、`StrEnum`、f-string 嵌套、
`typing.override`、结构模式匹配、异常组（ExceptionGroup）。

## 验证命令（S4 必跑，退出码为证据）

```bash
ruff check .                       # lint + 复杂度 + 魔法值线索
ruff format --check .              # 格式（或 black --check .）
mypy --strict src                  # 类型
pydocstyle src                     # docstring 完整性
pytest -q --cov=src --cov-fail-under=80   # 测试 + 覆盖率
```
