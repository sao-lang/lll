# 自定义 MCP 客户端：接口与实现模板

> 适用场景：你有一个框架未内置的 MCP 传输方式（自定义 RPC、企业内部协议、WebSocket 等），
> 想用自己的客户端替换默认的 stdio/http。只需实现一个协议 + 一行注入。

---

## 1. 接口在哪、要实现什么

**唯一接口**：`harness.tools.MCPTransport`（`src/harness/tools/mcp.py`）。
**注入入口**：`MCPManager(registry, 你的客户端)`。

框架已内置两个实现供参考：`StdioMCPTransport`、`HttpMCPTransport`（`src/ports/adapters/mcp.py`）。
你自己的客户端**不需要继承任何类**，实现下面三个方法 + 一个 `name` 属性即可（结构化子类型）。

```python
from harness.tools import MCPTransport        # 接口
from hooks import DangerLevel, PortCtx, ToolSpec
from runtime import DynamicValue

class MyMCPClient:
    """你自己的 MCP 客户端：实现 MCPTransport 三方法即可。"""

    name = "my_mcp"                             # 必填：可插拔物留名（规则 8）

    async def list_tools(self, server: str) -> list[ToolSpec]:
        """列出该 server 的工具。"""
        # 1) 向你的 server 发 tools/list（或等价请求），拿到原始工具定义；
        # 2) 映射成 ToolSpec，注意：
        #    - name 用**原始名**，不要加 mcp__ 前缀（框架统一加）；
        #    - danger 必填：SAFE/LOW/HIGH/CRITICAL，决定沙盒/审批路由；
        #    - parameters_schema 是 JSON Schema（dict）。
        return [
            ToolSpec(
                name="search",                  # 原始名；框架会变成 mcp__<server>__search
                description="搜索",
                parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
                danger=DangerLevel.SAFE,
                source="mcp",
            )
        ]

    async def call_tool(
        self, server: str, tool: str, args: dict[str, DynamicValue],
    ) -> DynamicValue:
        """调用工具：tool 是原始名，args 是参数字典；返回工具结果。"""
        # 发你的调用请求。返回值形态由工具决定；异常不要吞——
        # 上层 ToolRegistry.call 会折叠为失败 ToolResult(ok=False)。
        return {"text": "结果"}

    async def ping(self, server: str) -> bool:
        """可达性探测：返回 True/False，不要抛异常。"""
        return True
```

## 2. 接进框架（一行注入）

```python
from harness.tools import MCPManager, ToolRegistry, ToolsCapability
from hooks import PortCtx

registry = ToolRegistry()
client = MyMCPClient()
mcp = MCPManager(registry, client)              # 注入点：构造参数

# 或走能力装配（推荐）：
cap = ToolsCapability(mcp=MCPManager(registry, client))

# 发现并注入工具（框架自动加 mcp__<server>__ 前缀、注册进同一 Registry）
specs = await mcp.connect("my-server", PortCtx())
# specs[0].name == "mcp__my-server__search"

# 之后与普通工具无差别：
result = await registry.call(make_call("mcp__my-server__search", {"q": "x"}), PortCtx())
```

## 3. 契约要点（踩坑清单）

| 事项 | 要求 | 违反后果 |
|------|------|----------|
| 工具名 | 用**原始名**，不加 `mcp__` 前缀 | 框架再加一次 → 双前缀 `mcp__srv__mcp__x` |
| `danger` | 必填，四级之一 | 装配期 `static_check()` 报问题；未知值 fail-closed 到 CRITICAL |
| `parameters_schema` | 合法 JSON Schema dict | 参数校验 extra=forbid 可能误伤 |
| `call_tool` 异常 | 抛出，别自己吞 | 自己吞会丢失失败信号，上层无法折叠为失败信封 |
| `ping` 异常 | 返回 False，别抛 | health 虽会折叠，但返回 False 更直接 |
| `name` 属性 | 必须有 | 履约校验与审计留名需要 |

## 4. 履约自检（写完必做）

```python
from harness.tools import MCPTransport
assert isinstance(MyMCPClient(), MCPTransport)   # True 才算合规
```

把这个断言写成测试。框架自带的参考测试：`tests/ports/test_mcp_adapter.py::test_transport_protocol_satisfied`
展示了 stdio/http 两个真实现如何通过同一校验。

## 5. 参考实现

- `src/ports/adapters/mcp.py` — `StdioMCPTransport`（真子进程）+ `HttpMCPTransport`（真 HTTP/SSE），
  含 `initialize` 握手、`tools/list`、`tools/call`、`ping`、重连，是完整可抄的范本。
- `tests/r5/conftest.py::FakeTransport` — 最小测试替身（仅用于测试，不是默认实现）。

## 6. 两个接口别混

- `MCPTransport`（本文，无 ctx）— **你实现**，传输层。
- `MCPPort`（`src/hooks/protocols.py`，带 ctx）— **框架实现**（MCPManager），能力门面。
