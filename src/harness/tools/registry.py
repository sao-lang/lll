"""M4 完整工具注册表：实现 M3 的 ToolPort，本地 @tool 与 MCP 工具在运行时无差别。

三条链路：注册期 static_check / 披露期 L1-L2-L3 / 调用期 danger 四级路由。
工具异常一律折叠为 ToolResult(ok=False)，交 M2 回程 filter，绝不抛穿运行时。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from ports import DangerLevel, PortCtx, ToolCall, ToolResult, ToolSpec

from .decorator import SandboxPolicy, ToolDefinition, ToolDefinitionError

__all__ = ["ToolRegistry", "ToolEntry"]


@dataclass(frozen=True)
class ToolEntry:
    spec: ToolSpec
    fn: Callable[..., Any]
    definition: ToolDefinition | None = None


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ToolRegistry:
    """ToolPort 实现；可整体替换 M3 的 InMemoryToolRegistry。"""

    name = "tool-registry"

    def __init__(
        self,
        *,
        sandbox: Any | None = None,
        approver: Any | None = None,
        index_enabled: bool = True,
        clock: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._sandbox = sandbox            # SandboxPort（M8 实现）
        self._approver = approver          # HumanPort / ApprovalPort（M8 实现）
        self._index_enabled = index_enabled
        self._clock = clock                # 默认逻辑时钟 0.0，保证事件日志确定性

    # ------------------------------------------------------------------ #
    # ① 注册期
    # ------------------------------------------------------------------ #
    def register(
        self, spec_or_definition: ToolSpec | ToolDefinition,
        fn: Callable[..., Any] | None = None, *, bind: bool = False,
    ) -> "ToolRegistry":
        if isinstance(spec_or_definition, ToolDefinition):
            definition = spec_or_definition
            spec, impl = definition.spec, definition.fn or fn
        else:
            definition, spec, impl = None, spec_or_definition, fn
        if spec.name in self._tools and not bind:
            raise ToolDefinitionError(f"duplicate tool name: {spec.name}")
        if spec.danger is None:
            raise ToolDefinitionError(f"tool {spec.name!r} must declare danger (M4 红线)")
        if impl is None:
            raise ToolDefinitionError(f"tool {spec.name!r} has no implementation; use bind()")
        if spec.danger in (DangerLevel.HIGH, DangerLevel.CRITICAL) and self._sandbox is None:
            raise ToolDefinitionError(f"tool {spec.name!r} is {spec.danger.value} but no SandboxPort")
        if spec.danger is DangerLevel.CRITICAL and self._approver is None:
            raise ToolDefinitionError(f"tool {spec.name!r} is CRITICAL but no approval port")
        self._tools[spec.name] = ToolEntry(spec=spec, fn=impl, definition=definition)
        return self

    def bind(self, name: str, fn: Callable[..., Any]) -> "ToolRegistry":
        """给「@tool 装饰 pydantic 模型」得到的定义绑定实现。"""
        definition = getattr(fn, "__agentkit_tool__", None)
        if definition is not None and definition.name != name:
            raise ToolDefinitionError(f"bound name {name!r} != definition {definition.name!r}")
        entry = self._tools.get(name)
        if entry is None:
            raise ToolDefinitionError(f"unknown tool {name!r}; register its definition first")
        self._tools[name] = ToolEntry(entry.spec, fn, entry.definition)
        return self

    def add(self, definition: ToolDefinition) -> "ToolRegistry":
        if definition.fn is None:
            raise ToolDefinitionError(f"definition {definition.name!r} needs bind(name, fn)")
        return self.register(definition)

    def catalog(self, level: int = 1) -> list[ToolSpec]:
        """L1 默认只给 name+description（省 token）；level=2 或 index_enabled=False 给完整 schema。"""
        out = []
        for entry in self._tools.values():
            spec = entry.spec
            if level == 1 and self._index_enabled:
                spec = spec.model_copy(update={"parameters_schema": {}})
            out.append(spec)
        return out

    def schema_of(self, name: str) -> dict[str, Any]:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolDefinitionError(f"unknown tool {name!r}")
        return dict(entry.spec.parameters_schema)

    def static_check(self) -> list[str]:
        """装配期错误清单；freeze() 非空即拒绝（M4 红线全部在这里可查）。"""
        errors: list[str] = []
        for name, entry in self._tools.items():
            spec = entry.spec
            if spec.danger is None:
                errors.append(f"{name}: danger not declared")
            schema = spec.parameters_schema
            if not isinstance(schema, dict) or "properties" not in schema:
                errors.append(f"{name}: invalid parameters_schema")
            if spec.danger in (DangerLevel.HIGH, DangerLevel.CRITICAL) and self._sandbox is None:
                errors.append(f"{name}: {spec.danger.value} tool without SandboxPort")
            if spec.danger is DangerLevel.CRITICAL and self._approver is None:
                errors.append(f"{name}: CRITICAL tool without approval port")
        if len(self._tools) > 20 and not self._index_enabled:
            errors.append("more than 20 tools but progressive disclosure disabled")
        if any(e.spec.source == "mcp" for e in self._tools.values()) and any(
            spec.parameters_schema for spec in self.catalog(level=1)
        ):
            errors.append("MCP tools fully injected into L1 context (M4 红线)")
        return errors

    def freeze(self) -> "ToolRegistry":
        errors = self.static_check()
        if errors:
            raise ToolDefinitionError("static_check failed: " + "; ".join(errors))
        return self

    # ------------------------------------------------------------------ #
    # ③ 调用期：danger 四级路由
    # ------------------------------------------------------------------ #
    async def _approve(self, spec: ToolSpec, ctx: PortCtx) -> bool:
        approver = self._approver
        if approver is None:
            return False
        level = spec.danger.value if spec.danger else "unknown"
        question = f"批准执行高危工具 {spec.name}（{level}）？"
        if hasattr(approver, "approve"):
            return bool(await _maybe(approver.approve(question, ctx)))
        if hasattr(approver, "ask"):
            answer = await _maybe(approver.ask(question, ["approve", "deny"], ctx))
            return str(answer).lower() in ("approve", "yes", "y", "true")
        return False

    async def _invoke(self, fn: Callable[..., Any], args: Any, entry: ToolEntry, ctx: PortCtx) -> Any:
        return await _maybe(fn(args, ctx))

    async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:
        entry = self._tools.get(call.name)
        if entry is None:
            return ToolResult(call_id=call.call_id, ok=False, error=f"unknown tool: {call.name}")
        spec, fn, definition = entry.spec, entry.fn, entry.definition
        meta: dict[str, Any] = {
            "danger": spec.danger.value if spec.danger else "unknown",
            "source": spec.source,
            "sandbox": False,
            "duration_ms": self._clock(),
        }
        try:
            model = definition.args_model if definition else None
            if model is not None:
                allowed = set(model.model_fields)
                extra = set(call.args) - allowed
                if extra:
                    return ToolResult(
                        call_id=call.call_id, ok=False,
                        error=f"unexpected arguments (extra='forbid'): {sorted(extra)}", meta=meta,
                    )
                parsed: Any = model.model_validate(call.args)
            else:
                parsed = dict(call.args)

            sandbox_needed = (
                definition is not None and definition.sandbox is SandboxPolicy.ALWAYS
            ) or (spec.danger is not None and spec.danger is not DangerLevel.SAFE)
            if spec.danger is DangerLevel.CRITICAL:
                meta["approved"] = await self._approve(spec, ctx)
                if not meta["approved"]:
                    return ToolResult(
                        call_id=call.call_id, ok=False, error="approval denied", meta=meta
                    )
            if sandbox_needed and self._sandbox is not None:
                meta["sandbox"] = True
                value = await _maybe(
                    self._sandbox.run(lambda: fn(parsed, ctx), parsed, ctx)
                )
            else:
                value = await self._invoke(fn, parsed, entry, ctx)
            return ToolResult(
                call_id=call.call_id, ok=True, value=await _maybe(value), meta=meta
            )
        except ValidationError as exc:
            return ToolResult(call_id=call.call_id, ok=False, error=f"invalid args: {exc}", meta=meta)
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id, ok=False,
                error=f"{type(exc).__name__}: {exc}", meta=meta,
            )
