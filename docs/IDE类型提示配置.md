# IDE 类型提示配置：让类型"直接可见"

> 症状：鼠标停在参数/变量上不显示类型与含义；期望函数、变量、属性、方法、类、参数的类型都能直接看到。
> 代码侧类型注解本来就是完整的（mypy/pyright 双 0 error），缺的是**编辑器侧配置**。

---

## 已为本项目配置好的文件

| 文件 | 作用 |
|------|------|
| `.vscode/settings.json` | 解释器指向 `.venv`；开启四类**内联类型提示**（callArgumentNames / functionReturnTypes / variableTypes / pytestParameters）；开启 hover；`extraPaths` 加 `src`；排除缓存目录 |
| `pyrightconfig.json` | 让 CLI 与编辑器共用同一解析规则：src 布局、venv、Python 3.13、`extraPaths=["src"]` |

内联提示 vs hover 的区别：
- **内联提示（inlay hints）**：类型/参数名直接显示在那行代码上，不用悬停——这是"直接看见"的关键。
- **hover**：鼠标停上去显示完整签名 + docstring（含义）。

## 需要你在编辑器里确认的三件事

1. **装 Pylance 扩展**（`ms-python.vscode-pylance`）。它随 Python 扩展安装；如果编辑器用的是 Jedi 或无语言服务器，hover 就是空的。这是最常见原因。
2. **选对解释器**：必须是项目里的 `.venv/Scripts/python.exe`。选错解释器 → pydantic/httpx 的类型全丢 → hover 只显示 Unknown。
3. **重新打开窗口**：改完 `settings.json` 后 Reload Window 让 Pylance 重读配置。

如果你用的不是 VS Code 系编辑器，`.vscode/settings.json` 不生效，但 `pyrightconfig.json` 对任何基于 pyright 的语言服务器都有效——按该编辑器的方式开启 inlay hints 即可。

## 验证是否生效

打开 `src/harness/tools/registry.py`，应该看到：
- `async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:` 的参数名与类型直接可见；
- `self._specs: dict[str, ToolSpec]` 这类变量类型以内联灰字显示；
- 鼠标停在 `ToolCall` 上弹出类定义与 docstring。

命令行复核（应输出 0 error）：
```bash
.venv/Scripts/pyright.exe
.venv/Scripts/mypy.exe --strict src
```

## 顺带修复的问题

- `tests/r5/conftest.py` 的 `FakeTransport.call_tool` 参数名 `tool_name` 与 `MCPTransport` 协议的 `tool` 不一致，
  导致 pyright 报 4 处"参数名不匹配"（也会让 hover 信息变差）。已改为 `tool`。
- `pyrightconfig.json` 的诊断范围只含 `src/`：tests 大量使用 duck fake，历史遗留宽松类型，纳入诊断会产生 176 条噪音红线。
  Pylance 仍会解析 tests 里的符号，hover/go-to-def 不受影响。

---

*配置日期：2026-11-19 · Python ≥3.12 · pyright 1.1.414 + Pylance*
