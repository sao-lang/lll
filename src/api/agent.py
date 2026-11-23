# purity-exempt-file: InMemoryEventStream 为 R10 B.1.2 指定默认实现命名（生产级有界队列）；门面可选注入供可观测场景
"""语义门面 Agent —— 用户只碰 say()/run()，零 message/effect/payload/ctx 暴露。

设计（契约③④；Round1 B1-B4 + Round2 Blk-1~4 根因修复）：
- 位于 api 层（不改依赖方向：compose 不反向依赖 harness/api）；
- 按 port 分派 call：llm→gateway、tool→ToolRegistry、其余→注入的 port handler（不绕过审批/沙盒/审计）；
- 履约校验：纯签名匹配（unbound 方法、跳过非 runtime_checkable 协议），不靠 isinstance；
- 注入的 capability/strategy/port handler 装配期真 install 进持久 Registry，运行期可解析；
- say() 在 running loop 中走独立线程；输入经 compose.bind_input（ContextVar）按调用链隔离；
- 单次装配（不重复建 store）；say() 只抽 content，不泄漏内部 dict。

compose.Agent 保留为纯装配器（产 Session），本门面是用户默认入口。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable
from contextvars import ContextVar
from typing import Callable, Protocol, cast

from compose import (
    PortCtx, Provider, Registry, ToolSpec, bind_input,
)
from runtime import AgentState, Budgets, CallFn, DynamicValue, Effect, EventStore, Invoke, Program, Result, RunResult, Session

from .runtime import AgentRuntime
from harness.observability import InMemoryEventStream

from .runtime import assemble_runtime

__all__ = ["Agent"]


class ArchLike(Protocol):
    """架构门面接受的最小协议：能 compile 成 Program。"""
    compile: Callable[[], Program]


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


# 记忆召回上下文（ContextVar 按调用链隔离，并发 say 互不串扰）。
_memory_context: ContextVar[tuple[str, ...]] = ContextVar("agentkit_memory_context", default=())

_ALLOWED_ANSWER_KEYS = frozenset({"content", "text", "answer", "output"})


def _extract_answer(out: DynamicValue) -> str:
    """从内部产出抽用户文本：白名单字段，任何形态都不泄漏 Effect/Error 信封（Blk S1）。"""
    if isinstance(out, dict):
        for key in _ALLOWED_ANSWER_KEYS:
            if key in out and out[key] is not None:
                return str(out[key])
        return ""  # 无白名单字段：不吐内部 dict
    if isinstance(out, str):
        return out
    return ""  # Result/对象等内部信封：不 str() 泄漏


def _signature_conforms(obj: Provider, proto: type) -> bool:
    """结构 + 签名可绑定性履约校验（Round3 Blk-1 根因修复）。

    - 不调 isinstance（非 runtime_checkable 协议会抛 TypeError；合法实现被误拒）。
    - 每个协议方法：实现必须 call able，且用 `Signature.bind` 验证"按协议签名调用能否绑定"
      ——捕获参数名齐但顺序反/多必填/全 keyword-only 的伪实现；空实现（方法体 pass）也拒。
    - 属性型成员（name 等）：只验存在且类型合理（name 为 str）。
    """
    ann = tuple(getattr(proto, "__annotations__", {}).keys())
    methods = tuple(n for n, v in vars(proto).items() if callable(v) and not n.startswith("_"))
    attrs = tuple(dict.fromkeys(ann + methods))
    if not attrs:
        return False
    for name in attrs:
        expected = getattr(proto, name, None)
        actual = getattr(type(obj), name, None)  # 未绑定（含 self）
        if not callable(expected):
            # 属性型：name 须为 str（拒 WrongNameType）
            if not hasattr(obj, name):
                return False
            if name == "name" and not isinstance(getattr(obj, name), str):
                return False
            continue
        if not callable(actual):
            return False
        try:
            sig_e = inspect.signature(expected)
            sig_a = inspect.signature(actual)
        except (TypeError, ValueError):
            return False
        # 签名顺序 + kind 严格匹配（Round4 S1/S2 根因修复）：
        # 协议参数序列（去 self）必须按**同顺序**出现在实现参数序列（去 self）中；
        # 允许实现末尾有额外可选参数 / *args / **kwargs（更宽松），但协议参数的
        # kind 必须兼容（keyword-only 协议参数不能由位置参数兑现，反之亦然）。
        ep = [p for p in sig_e.parameters.values()
              if p.name not in ("self", "cls")
              and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
        ap = [p for p in sig_a.parameters.values()
              if p.name not in ("self", "cls")
              and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
        has_var_pos = any(p.kind is inspect.Parameter.VAR_POSITIONAL
                          for p in sig_a.parameters.values())
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in sig_a.parameters.values())
        # 实现有 *args/**kwargs 时可承接任意参数形态（合法通配实现，Round4 S2）：
        # 跳过参数名/kind 比对，但**不跳过**下方空实现检查（Round5 红队披露的边缘回归）。
        if not (has_var_pos or has_var_kw):
            if len(ep) > len(ap):
                return False
            for idx, p in enumerate(ep):
                if idx >= len(ap):
                    return False
                got = ap[idx]
                if got.name != p.name:
                    return False  # 顺序反（SneakySwap）→ 拒
                if (p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.POSITIONAL_ONLY)
                        and got.kind is inspect.Parameter.KEYWORD_ONLY):
                    return False  # 协议可位置调用，实现却 kw-only（SneakyKW）→ 拒
                if p.kind is inspect.Parameter.KEYWORD_ONLY and got.kind is not inspect.Parameter.KEYWORD_ONLY:
                    return False
                if (p.kind is inspect.Parameter.POSITIONAL_ONLY
                        and got.kind not in (inspect.Parameter.POSITIONAL_ONLY,
                                             inspect.Parameter.POSITIONAL_OR_KEYWORD)):
                    return False
        # 空实现拒：方法体只有 ... / pass（去掉 docstring 后无实质语句）
        try:
            src_fn = inspect.getsourcefile(actual)
            if src_fn and src_fn != "<string>":
                import ast
                src = inspect.getsource(actual)
                tree = ast.parse("".join(line.lstrip() for line in src.splitlines()))
                fn_node = next((n for n in ast.walk(tree)
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
                if fn_node is not None:
                    body = [s for s in fn_node.body
                            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                    and isinstance(s.value.value, str))]
                    if not body or all(
                        (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                         and s.value.value is Ellipsis) or isinstance(s, ast.Pass)
                        for s in body
                    ):
                        return False
        except (OSError, TypeError, SyntaxError):
            pass  # 无法取源码（如 C 扩展/动态类）：跳过空实现检查
    return True


class Agent:
    """语义门面：arch + 可注入 capability/策略/工具/端口，say()/run() 直接出结果。

    用户路径（零胶水）：
        Agent(ReAct(tools)).capability(ModelCapability(llm=...)).say("查天气")
    不暴露 Session/Effect/message/ctx；所有装配在执行内部完成。
    """

    name = "agent"
    version = "12.0.0"

    def __init__(
        self,
        arch: ArchLike,
        *,
        session_id: str = "agent",
        system: str | None = None,
        stream: InMemoryEventStream | None = None,
        store: EventStore | None = None,
        budgets: Budgets | None = None,
    ) -> None:
        """构造语义门面。

        默认零胶水（不传底座）；可选注入 stream/store/budgets 供可观测场景
        （事件流投影 / 审计同源回放 / 自定义预算）——不改变 say() 语义。
        system: 一等系统提示，门面在首轮 LLM 调用前置注入（用户无需包装 LLM）。
        """
        self._arch = arch
        self._system = system
        self._session_id = session_id
        self._stream = stream
        self._store = store
        self._budgets = budgets
        self.last_run: RunResult | None = None  # 最近一次 RunResult（只读诊断）
        self.runtime: AgentRuntime | None = None  # 最近一次装配的运行期（可观测底座读取）
        self._caps: dict[str, Provider] = {}
        self._tools: list[tuple[ToolSpec, Callable[..., DynamicValue]]] = []
        self._port_handlers: dict[str, Callable[[Effect, DynamicValue], Awaitable[Result]]] = {}
        self._installed: dict[str, Provider] = {}  # 幂等按对象身份：同名但换对象要重装（Round4 S3）

    @property
    def stream(self) -> InMemoryEventStream:
        """可观测事件流底座（一等公开属性）：内置自建，注入则复用注入实例。

        同源回退：runtime 已装配（say 之后访问）时返回 rt.stream——装配时
        self._stream 尚为 None 会让 assemble_runtime 自建实例，懒建一个新实例
        会造成两实例不同源、事件永远收不到（死注入变体）。全程单实例同源。
        用户目标：agent.stream 直接可读；想外接自己的 sink 时用 use(自己的 sink 能力)。
        """
        if self._stream is None:
            rt_stream = getattr(self.runtime, "stream", None) if self.runtime else None
            if rt_stream is not None:
                self._stream = rt_stream
            else:
                from harness.observability import InMemoryEventStream as _Stream
                self._stream = _Stream()
        return self._stream

    def capability(self, cap: Provider) -> "Agent":
        """注入能力（按 name 登记）；装配期真 install 进持久 Registry。"""
        name = str(getattr(cap, "name", "unknown"))
        self._caps[name] = cap
        return self

    def use(self, cap: Provider) -> "Agent":
        """能力插拔的一等 API（用户目标写法）：capability() 的链式别名。

        use() 与 capability() 完全等价——都是真注入、装配期真 install，
        不是兼容双轨，只是用户-facing 的命名。链式返回 self 支持连续 use()。
        """
        return self.capability(cap)

    def register_tool(self, name: str, fn: Callable[..., DynamicValue], **kwargs: DynamicValue) -> "Agent":
        """注册工具到门面（tool port 走四级路由）。"""
        from compose import DangerLevel
        danger = kwargs.get("danger") or DangerLevel.SAFE
        self._tools.append((
            ToolSpec(name=name, description=str(kwargs.get("description") or ""),
                     danger=danger),
            fn,
        ))
        return self

    def register_port(self, port: str, handler: Callable[[Effect, DynamicValue], Awaitable[Result]]) -> "Agent":
        """注入自定义 port handler（Blk-2：非 llm/tool port 不再断链）。"""
        self._port_handlers[port] = handler
        return self

    def inject_strategy(self, strategy: Provider) -> "Agent":
        """注入策略：纯签名履约校验 + 真接进决策点（R13 第四要素，Round3 Blk-1）。"""
        from compose.ports import PORT_PROTOCOLS
        matched = [pname for pname, proto in PORT_PROTOCOLS.items()
                   if _signature_conforms(strategy, proto)]
        if not matched:
            raise TypeError(
                f"strategy {type(strategy).__name__} 不满足任何框架协议签名（履约校验失败）")
        # 真接入：策略按其履约的 port 注册为 handler（运行期 call 会消费），不再是死对象。
        port = matched[0]
        self._port_handlers.setdefault(port, self._as_handler(strategy))
        self._caps["strategy"] = strategy
        return self

    @staticmethod
    def _as_handler(provider: Provider) -> Callable[[Effect, DynamicValue], Awaitable[Result]]:
        """把注入的 Provider 适配成 port handler（Round4 S4 根因修复）。

        - 按 provider 方法签名自动取参：payload 里有同名键 → 取之；缺的必填参数
          由 `ctx` 注入（参数名叫 ctx/context）或留空，不再要求 payload 显式 args/ctx。
        - 注册名≠port 名的映射在 _install_caps 完成（provider 注册名 → 推断 port）。
        """
        async def handler(effect: Effect, ctx: DynamicValue) -> Result:
            port = str(getattr(effect, "port", ""))
            payload = dict(getattr(effect, "payload", {}) or {})
            op = str(payload.get("op") or "call")
            fn = getattr(provider, op, None)
            if not callable(fn):
                return Result.failure("PORT_OP_MISSING", f"{port}.{op} provider op absent")
            try:
                sig = inspect.signature(fn)
                kwargs: dict[str, DynamicValue] = {}
                for p in sig.parameters.values():
                    if p.name in ("self", "cls"):
                        continue
                    if p.name in payload:
                        kwargs[p.name] = payload[p.name]
                    elif p.name in ("ctx", "context"):
                        kwargs[p.name] = ctx
                    elif p.default is inspect.Parameter.empty and p.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                        kwargs[p.name] = None  # 缺参以 None 兜底（S4：不再 TypeError）
                out = fn(**kwargs)
                return Result.success(await out if inspect.isawaitable(out) else out)
            except Exception as exc:  # noqa: BLE001
                return Result.failure("PORT_ERROR", str(exc))
        return handler

    @staticmethod
    def _provider_port(reg_name: str, provider: Provider) -> str:
        """从 provider 注册名推断 port 名（S4：注册名≠port名也能消费）。

        优先 provider 自带的 `port`/`name` 声明；否则注册名去掉 _provider/_store 后缀
        作为 port（memory_provider→memory、audit_store→audit）。
        """
        declared = getattr(provider, "port", None)
        if isinstance(declared, str) and declared:
            return declared
        for suffix in ("_provider", "_store", "_port", "_adapter"):
            if reg_name.endswith(suffix) and len(reg_name) > len(suffix):
                return reg_name[:-len(suffix)]
        return reg_name

    def _install_caps(self) -> None:
        """装配期幂等 install 注入的 capability（Round3 幂等 + Round4 S3 同名替换重装）。

        每个 cap 用**独立 Registry** install（同名替换不触发重名异常），provider 统一
        适配进 _port_handlers（注册名→port 推断，消费路径不再死对象）。
        """
        for name, cap in self._caps.items():
            if name == "strategy":
                continue
            if name in self._installed and self._installed[name] is cap:
                continue
            install = getattr(cap, "install", None)
            if not callable(install):
                raise TypeError(f"capability {name} 无 install()，不满足 Capability 协议")
            cap_registry = Registry()  # 每次独立：同名 cap 替换不撞 registry 重名
            install(self, cap_registry)
            self._installed[name] = cap
            for reg_name in cap_registry.names():
                if reg_name in ("llm", "tool"):
                    continue
                try:
                    provider = cap_registry.resolve(reg_name)
                    port = self._provider_port(reg_name, provider)
                    self._port_handlers[port] = self._as_handler(provider)
                except Exception:  # noqa: BLE001
                    pass

    def _derive_call(self, tool_handler: CallFn) -> CallFn:
        """构造统一 call：llm→dispatch，tool→四级路由，其余→注入的 port handler。"""
        cap = self._caps.get("model")
        dispatch = cast(CallFn | None, getattr(cap, "dispatch", lambda: None)()) if cap else None

        async def call(effect: Effect, ctx: DynamicValue) -> Result:
            if not isinstance(effect, Invoke):
                return Result.failure("INVALID_EFFECT", "facade call only handles Invoke")
            port = effect.port
            if port == "tool":
                return await tool_handler(effect, ctx)
            if port == "llm":
                if dispatch is None:
                    return Result.failure("PORT_MISSING", "no model capability injected")
                return await self._call_llm(effect, ctx, dispatch)
            # 其余 port：查注入的 handler（Blk-2：任意 port 可注入，不再只认 llm/tool）
            handler = self._port_handlers.get(port)
            if handler is not None:
                try:
                    result = handler(effect, ctx)
                    resolved = await result if inspect.isawaitable(result) else result
                    return resolved if isinstance(resolved, Result) else Result.success(resolved)
                except Exception as exc:  # noqa: BLE001
                    return Result.failure("PORT_ERROR", str(exc))
            return Result.failure("PORT_MISSING", f"no handler for port: {port}")

        return cast(CallFn, call)

    async def _call_llm(self, effect: Invoke, ctx: DynamicValue, dispatch: CallFn) -> Result:
        """llm port：ctx 转换 + 消息归一 + system 前置 + ToolCall 归一 + usage 搬运（框架内部）。"""
        pctx = PortCtx(
            session_id=str(getattr(ctx, "session_id", "")),
            run_id=str(getattr(ctx, "run_id", "facade-run")),
            meta=dict(getattr(ctx, "meta", {}) or {}),
        )
        raw = list(effect.payload.get("messages") or [])
        if not raw:
            raw = [{"role": "user", "content": ""}]
        messages = _normalize_history(raw)
        # system 一等参数（B9）：门面在首轮前置 system 消息，用户无需包装 LLM。
        # 已含 system（架构自带或历史回注）时不重复注入。
        if self._system and not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": self._system}, *messages]
        # 记忆召回（MemoryCapability 钩子）：把命中记忆拼入 system 段（自动召回，用户零胶水）。
        recalled = _memory_context.get()
        if recalled:
            memory_text = "相关记忆：" + "；".join(recalled)
            sys_idx = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
            if sys_idx is None:
                messages = [{"role": "system", "content": memory_text}, *messages]
            else:
                m = dict(messages[sys_idx])
                m["content"] = f"{m.get('content', '')}\n{memory_text}".strip()
                messages = [*messages[:sys_idx], m, *messages[sys_idx + 1:]]
        clean = Invoke(port="llm", payload={
            "messages": messages,
            "model": effect.payload.get("model") or "",
            "tools": effect.payload.get("tools") or [],
        })
        result = await dispatch(clean, pctx)
        data = result.data
        if isinstance(data, dict):
            data = dict(data)
            raw_calls = data.get("tool_calls") or []
            data["tool_calls"] = [
                {"name": tc.name, "args": tc.args, "call_id": tc.call_id,
                 "danger": getattr(tc, "danger", None), "source": getattr(tc, "source", "local")}
                if hasattr(tc, "name") else tc
                for tc in raw_calls
            ]
            usage = data.pop("usage", None)
            if usage is not None and getattr(result, "usage", None) is None:
                return Result(result.ok, data, usage, result.error)
        return Result(result.ok, data, result.usage, result.error)

    async def _arun(self) -> DynamicValue:
        """内部执行：单次装配 → Runner.run → 取 output（全程语义级）。

        用 assemble_runtime 的 tool_setup/call_factory 钩子，让工具注册与 llm call
        都在**同一次装配**中完成——tools/audit/sandbox/store/bus 全同源（Round3 审计断裂修复）。
        """
        self._install_caps()
        from harness.tools import ToolRegistry, clear_pending, pending_tools
        tools_snapshot = list(self._tools)
        facade = self

        def tool_setup(registry: ToolRegistry) -> None:
            # @tool 一份声明三处消费：pending 的 ToolFn（spec+fn）自动注册进执行链，
            # 用户无需再 register_tool。同名时以显式 register_tool 为准（去重不冲突）。
            pending = pending_tools()
            registered: set[str] = set()
            for tool_fn in pending:
                registry.register(tool_fn.spec, tool_fn)
                registered.add(tool_fn.spec.name)
            if pending:
                clear_pending()  # 已注册即消费，避免跨 say 重复累积
            for spec, fn in tools_snapshot:
                if spec.name in registered:
                    continue
                registry.register(spec, fn)

        def call_factory(tool_handler: CallFn) -> CallFn:
            return facade._derive_call(tool_handler)

        # 注入即生效（B5）：门面注入的 SecurityCapability 必须真进执行链。
        # 不传时 assemble_runtime 默认 new 一个，导致用户注入的 sandbox/human/audit
        # 被静默替换——这是"看起来注入成功、实际没生效"的死注入根因。
        from harness.security import SecurityCapability
        sec_cap = self._caps.get("security")
        security = sec_cap if isinstance(sec_cap, SecurityCapability) else None

        rt = assemble_runtime(None,
                              stream=self._stream,
                              store=self._store,
                              security=security,
                              tool_setup=tool_setup, call_factory=call_factory)
        self.runtime = rt
        session = Session(
            session_id=self._session_id,
            program_factory=self._arch.compile,
            state=AgentState(budgets=self._budgets or Budgets()),
        )
        result = await rt.runner.run(session)
        self.last_run = result
        return result.output

    async def asay(self, text: str) -> str:
        """异步语义入口：输入经 ContextVar 按调用链绑定（并发安全）。

        MemoryCapability 注入后自动 recall（before）与写回（after）——用户在钩子里
        得到记忆能力，无需手写 memory.write/recall/ingest。
        """
        from harness.context import MemoryCapability
        mem_cap = next(
            (c for c in self._caps.values() if isinstance(c, MemoryCapability)), None)
        ctx = PortCtx(session_id=self._session_id, run_id="facade-run")
        with bind_input(text):
            if mem_cap is not None:
                recalled = await mem_cap.recall_before(text, ctx)
                token = _memory_context.set(recalled)
                try:
                    answer = _extract_answer(await self._arun())
                finally:
                    _memory_context.reset(token)
            else:
                answer = _extract_answer(await self._arun())
        if mem_cap is not None:
            mem_cap.write_after(text, answer, ctx)
        return answer

    def say(self, text: str) -> str:
        """同步语义入口：running loop 中走独立线程，只返回白名单文本。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.asay(text))
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self.asay(text))).result()

    def run(self, text: str) -> str:
        """语义入口：say 的同义。"""
        return self.say(text)
