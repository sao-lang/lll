"""ports.call：kind → port 名 → DTO 校验 → 适配器 → 统一 Result 信封。

异常一律折叠为 Result{ok:False, error:{code,message,retryable}}，绝不抛穿运行时；
处置（retry / skip / degrade / abort）交给 M2 回程 filter。
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from pydantic import ValidationError

from runtime import Effect, EffectKind, PortError, PortMissing, Result
from runtime.types import CallFn

from .dto import LLMRequest, ToolCall
from .registry import PortCtx, PortRegistry, port_ctx_from

__all__ = ["KIND_TO_PORT", "classify_exception", "call", "make_call"]

KIND_TO_PORT: dict[EffectKind, str] = {
    EffectKind.LLM: "llm",
    EffectKind.TOOL: "tool",
    EffectKind.SKILL: "skill",
    EffectKind.WAIT_HUMAN: "human",
}


def classify_exception(exc: BaseException) -> PortError:
    """错误信封分类：retryable=True 是 M2 retry 判决的依据。"""
    code = type(exc).__name__
    if isinstance(exc, PortMissing):
        return PortError("PORT_MISSING", str(exc), False)
    if isinstance(exc, ValidationError):
        return PortError("PORT_INVALID", str(exc), False)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    retryable = (
        isinstance(exc, (TimeoutError, ConnectionError, OSError))
        or any(k in code for k in ("Timeout", "Network", "Connect", "Temporary"))
        or (isinstance(status, int) and status >= 500)
    )
    return PortError(code, str(exc), retryable)


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return dict(obj.model_dump(mode="python"))
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    return {}


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _log(registry: PortRegistry, effect: Effect, ctx: Any, result: Result) -> None:
    tele = registry.try_resolve("telemetry")
    if tele is None or not hasattr(tele, "log"):
        return
    try:
        tele.log({
            "event": "port_call",
            "kind": effect.kind.value,
            "ok": result.ok,
            "error": None if result.ok else (result.error.code if result.error else None),
            "session_id": getattr(ctx, "session_id", ""),
            "components": list(getattr(ctx, "trace", []) or []),
        })
    except Exception:  # 可观测故障不得影响主流程
        pass


async def call(effect: Effect, ctx: Any, registry: PortRegistry | None = None) -> Result:
    """M3 的 CallFn 本体；可由 make_call(registry) 绑定后注入 Runner。"""
    if registry is None:
        registry = getattr(ctx, "registry", None)
    if registry is None:
        return Result(ok=False, error=PortError("PORT_REGISTRY_MISSING", "no registry bound", False))
    pctx: PortCtx = port_ctx_from(ctx)
    payload = dict(effect.payload)

    try:
        if effect.kind is EffectKind.END:
            return Result.success(None)
        if effect.kind is EffectKind.CHILD:
            return Result.failure("CHILD_HANDLED_BY_RUNNER", "child is executed by Runner")

        degrade = (getattr(ctx, "meta", {}) or {}).get("degrade_port")
        port_name = payload.get("port") or degrade or KIND_TO_PORT.get(effect.kind)
        if not port_name:
            return Result.failure("PORT_UNMAPPED", f"no port for {effect.kind.value}")
        adapter = registry.resolve(port_name)

        if payload.get("port") is not None:
            # 显式 x-<name> port：用户按接口接入 db / vector db / 评测平台等自定义能力
            custom_payload = {k: v for k, v in payload.items() if k != "port"}
            if hasattr(adapter, "call"):
                result = Result.success(await _maybe(adapter.call(custom_payload, pctx)))
            elif callable(adapter):
                result = Result.success(await _maybe(adapter(effect, ctx)))
            else:
                result = Result.failure("PORT_NOT_CALLABLE", f"{port_name} has no call()")
        elif effect.kind is EffectKind.LLM:
            req = LLMRequest.model_validate({k: v for k, v in payload.items() if k != "port"})
            response = await _maybe(adapter.complete(req, pctx))
            result = Result.success(response, usage=getattr(response, "usage", None))
        elif effect.kind is EffectKind.TOOL:
            tc = ToolCall.model_validate(_as_dict(payload.get("call") or {}))
            tool_result = await _maybe(adapter.call(tc, pctx))
            result = Result.success(tool_result)
        elif effect.kind is EffectKind.SKILL:
            body = await _maybe(adapter.body(payload.get("name", ""), payload.get("part", "body"), pctx))
            result = Result.success(body)
        elif effect.kind is EffectKind.WAIT_HUMAN:
            answer = await _maybe(
                adapter.ask(payload.get("question", ""), payload.get("options"), pctx)
            )
            result = Result.success(answer)
        else:  # pragma: no cover - 枚举固定，防御未知 kind
            result = Result.failure("PORT_UNSUPPORTED", f"{effect.kind.value} has no adapter shape")
    except Exception as exc:
        result = Result(ok=False, error=classify_exception(exc))

    _log(registry, effect, ctx, result)
    return result


def make_call(registry: PortRegistry) -> CallFn:
    """绑定注册表，产出 Runner 需要的 CallFn（签名 (effect, ctx) -> Awaitable[Result]）。"""
    async def _call(effect: Effect, ctx: Any) -> Result:
        return await call(effect, ctx, registry)
    return _call
