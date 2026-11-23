"""语义门面 Agent —— 用户只碰 say()/run()，零 message/effect/payload/ctx 暴露。

设计（契约③④；红队 B1/B2/B4）：
- 位于 api 层（不改依赖方向：compose 不反向依赖 harness/api）；
- 内部完成三处胶水：① RuntimeCtx→PortCtx ② ReAct 消息格式归一 ③ usage 搬进 Result.usage；
- 从注入的 ModelCapability 自动推导 llm dispatch（runtime(call) 不再是用户必填）；
- 用户输入文本注入架构首轮 messages（不靠用户在 call 里补）。

compose.Agent 保留为纯装配器（产 Session），本门面是用户默认入口。
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Protocol, cast

from compose import PortCtx, Provider
from runtime import AgentState, Budgets, CallFn, DynamicValue, Effect, Invoke, Program, Result, Session


class ArchLike(Protocol):
    """架构门面接受的最小协议：能 compile 成 Program。"""
    compile: Callable[[], Program]  # 协议属性声明，无占位实现

from .runtime import assemble_runtime

__all__ = ["Agent"]


def _normalize_history(msgs: list[dict[str, DynamicValue]]) -> list[dict[str, DynamicValue]]:
    """ReAct 内部消息 → OpenAI-compatible（框架内部，用户不碰）。"""
    normalized: list[dict[str, DynamicValue]] = []
    id_by_name: dict[str, str] = {}
    for m in msgs:
        role = str(m.get("role") or "user")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("call_id") or c.get("id") or "call")
                name = str(c.get("name") or "")
                id_by_name[name] = cid
                calls.append({"id": cid, "type": "function", "function": {
                    "name": name,
                    "arguments": str(c.get("arguments") or json.dumps(c.get("args") or {})),
                }})
            normalized.append({"role": "assistant", "content": str(m.get("content") or ""),
                               "tool_calls": calls})
        elif role == "tool":
            name = str(m.get("name") or "")
            normalized.append({"role": "tool",
                               "tool_call_id": str(m.get("call_id") or id_by_name.get(name) or name),
                               "content": json.dumps(m.get("content"), ensure_ascii=False)})
        else:
            normalized.append({"role": role, "content": str(m.get("content") or "")})
    return normalized


class Agent:
    """语义门面：arch + 可注入 capability/策略，say()/run() 直接出结果。

    用户路径（零胶水）：
        Agent(ReAct(tools)).capability(ModelCapability(llm=...)).say("查天气")
    不暴露 Session/Effect/message/ctx；所有装配在执行内部完成。
    """

    name = "agent"
    version = "12.0.0"

    def __init__(self, arch: ArchLike, *, session_id: str = "agent") -> None:
        self._arch = arch
        self._session_id = session_id
        self._caps: dict[str, Provider] = {}

    def capability(self, cap: Provider) -> "Agent":
        """注入能力（按 name 登记）；履约校验在 say() 装配期统一做。"""
        name = str(getattr(cap, "name", "unknown"))
        self._caps[name] = cap
        return self

    def inject_strategy(self, strategy: Provider) -> "Agent":
        """注入策略：装配期做履约校验（R13 第四要素），非法实现拒绝。"""
        from compose.ports import PORT_PROTOCOLS
        # 语义级校验：策略必须实现至少一个框架协议（runtime_checkable Protocol）。
        matched = [pname for pname, proto in PORT_PROTOCOLS.items()
                   if self._conforms(strategy, proto)]
        if not matched:
            raise TypeError(
                f"strategy {type(strategy).__name__} 不满足任何框架协议（履约校验失败）")
        self._caps["strategy"] = strategy
        return self

    @staticmethod
    def _conforms(obj: Provider, proto: type) -> bool:
        """isinstance 履约校验（proto 为 runtime_checkable Protocol）。"""
        try:
            return isinstance(obj, proto)
        except TypeError:
            return False

    def _validate_caps(self) -> None:
        """装配期履约校验：capability 必须有 name/install（Capability 协议最小面）。"""
        for name, cap in self._caps.items():
            if name == "strategy":
                continue
            if not callable(getattr(cap, "install", None)):
                raise TypeError(f"capability {name} 无 install()，不满足 Capability 协议")

    def _derive_llm_call(self) -> CallFn | None:
        """从注入的 ModelCapability 推导 llm port 的 CallFn（框架内部，用户不写）。"""
        cap = self._caps.get("model")
        if cap is None or not hasattr(cap, "dispatch"):
            return None  # 无模型能力：走 assemble_runtime 默认（tool only），llm 显式失败
        dispatch = cast(CallFn, getattr(cap, "dispatch")())  # CallFn

        async def llm_call(effect: Effect, ctx: DynamicValue) -> Result:
            if isinstance(effect, Invoke) and effect.port == "llm":
                pctx = PortCtx(
                    session_id=str(getattr(ctx, "session_id", self._session_id)),
                    run_id=f"{self._session_id}-run",
                    meta=dict(getattr(ctx, "meta", {}) or {}),
                )
                # 框架内部归一化消息 + 注入工具 schema（用户不碰）。
                raw = list(effect.payload.get("messages") or [])
                if not raw:
                    raw = [{"role": "user", "content": ""}]
                clean_effect = Invoke(
                    port="llm",
                    payload={
                        "messages": _normalize_history(raw),
                        "model": effect.payload.get("model") or "",
                        "tools": effect.payload.get("tools") or [],
                    },
                )
                result = await dispatch(clean_effect, pctx)
                # usage 搬进 Result.usage（框架内部搬运，用户不做）：dispatch 的 data 带 usage
                data = result.data
                usage = data.get("usage") if isinstance(data, dict) else None
                if usage is not None and getattr(result, "usage", None) is None:
                    if result.ok:
                        return Result.success(data, usage)
                    return Result(False, data, usage, result.error)
                return result
            return Result.failure("PORT_MISSING", "no handler for llm port")

        return cast(CallFn, llm_call)

    def _inject_input(self, arch: ArchLike, text: str) -> None:
        """把用户输入文本注入架构首轮（框架内部，红队 B2）。

        ReAct 的 messages 是 compile 闭包局部；通过 arch 的 input_text 属性在 compile 前设置，
        ReAct.compile 读取它作为首轮 user 消息。
        """
        setattr(arch, "input_text", text)

    def _run(self, text: str) -> DynamicValue:
        """内部执行：装配 → Runner.run → 取 output（全程语义级）。"""
        self._validate_caps()
        call = self._derive_llm_call()
        rt = assemble_runtime(call, stream=None) if call is not None else assemble_runtime()

        # 把用户输入注入架构（在 compile 前设置，compile 读取）
        self._inject_input(self._arch, text)
        session = Session(
            session_id=self._session_id,
            program_factory=self._arch.compile,
            state=AgentState(budgets=Budgets()),
        )
        result = asyncio.run(rt.runner.run(session))
        return result.output

    def say(self, text: str) -> str:
        """语义入口：说一句话，拿回答复文本（不暴露内部数据）。"""
        out = self._run(text)
        return str(out) if out is not None else ""

    def run(self, text: str) -> str:
        """语义入口：say 的同义（红队 S1：say/run 两个语义方法都要有）。"""
        return self.say(text)
