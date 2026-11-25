# purity-exempt-file: __exit__ 的 exc_type/exc/tb 是上下文管理器协议固定签名
"""R10 遥测：NoopTelemetry（B.1.2 默认实现）+ Trace（trace_id 串联）。

履约 compose.ports.TelemetryPort 同步签名（span/metric/log，OTel 惯例）：
采集同步入队、关键路径不阻塞；NoopTelemetry 内存收集（故障不影响主流程）。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from itertools import count
from types import TracebackType
from typing import TYPE_CHECKING, Literal

from compose import PortCtx, TelemetryPort
from runtime import DynamicValue, Usage as RuntimeUsage
if TYPE_CHECKING:
    from compose import LifecycleBus, Notice, Usage
    UsageLike = Usage | RuntimeUsage

from .types import TraceSpan

__all__ = ["NoopTelemetry", "Trace"]


class _SpanHandle(AbstractContextManager[TraceSpan]):
    """span 上下文句柄：进入记开始、退出记结束（本地时钟注入，内核不隐式取）。"""
    def __init__(self, span: TraceSpan, telemetry: NoopTelemetry) -> None:
        """保存 span 与归属遥测。"""
        self._span = span
        self._telemetry = telemetry
        self._started = 0.0

    def __enter__(self) -> TraceSpan:
        """记录开始并注册 span。"""
        import time

        self._started = time.perf_counter()
        self._telemetry._register(self._span)
        return self._span

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> Literal[False]:
        """记录耗时（不吞异常）。"""
        import time

        from dataclasses import replace
        latest = next((s for s in self._telemetry.spans()
                       if s.span_id == self._span.span_id), self._span)
        ended = replace(latest, duration_ms=round(
            (time.perf_counter() - self._started) * 1000.0, 3))
        self._telemetry._replace(ended)
        return False


class NoopTelemetry(TelemetryPort):
    """TelemetryPort 默认实现：in-process 内存收集（无外部导出故障面）。

    名字沿用 B.1.2 的 NoopTelemetry；真实语义=不导出到外部系统、但完整内存留痕，
    供 trace/metric/log 断言与回放（不是桩：三方法均有真实可观测产出）。
    """
    name = "noop_telemetry"

    def __init__(self) -> None:
        """初始化空收集器。"""
        self._spans: list[TraceSpan] = []
        self._metrics: list[tuple[str, float, dict[str, DynamicValue] | None]] = []
        self._logs: list[dict[str, DynamicValue]] = []

    def span(self, name: str, ctx: PortCtx) -> AbstractContextManager[DynamicValue]:
        """开启 span（返回上下文管理器，供 with 使用）。"""
        span = TraceSpan(trace_id=str(ctx.trace_span or "root"), span_id=name,
                         name=name, component="")
        return _SpanHandle(span, self)

    def metric(self, name: str, value: float,
               tags: dict[str, DynamicValue] | None = None) -> None:
        """记录指标（同步入队）。"""
        self._metrics.append((name, value, dict(tags) if tags else None))

    def log(self, event: dict[str, DynamicValue]) -> None:
        """记录结构化日志事件。"""
        self._logs.append(dict(event))

    # 内部注册（供 _SpanHandle 与 Trace 使用）
    def _register(self, span: TraceSpan) -> None:
        """注册 span 到收集器。"""
        self._spans.append(span)

    def _replace(self, ended: TraceSpan) -> None:
        """用结束态替换同 span_id 的记录（保留插入序）。"""
        self._spans = [ended if s.span_id == ended.span_id else s for s in self._spans]

    def spans(self) -> tuple[TraceSpan, ...]:
        """返回 span 快照。"""
        return tuple(self._spans)

    def metrics(self) -> tuple[tuple[str, float, dict[str, DynamicValue] | None], ...]:
        """返回指标快照。"""
        return tuple(self._metrics)

    def logs(self) -> tuple[dict[str, DynamicValue], ...]:
        """返回日志快照。"""
        return tuple(self._logs)


class Trace:
    """全链路 trace：一个 trace_id 串 lifecycle/LLM/tool，组件 name 留痕。

    实现 LifecycleBus 插件协议（name + register(bus)）：装配期 `bus.subscribe(trace)`
    后，session root（agent 级单例）下每轮 say 开一个 `turn:N`，16 相位 + llm/tool
    挂当前 turn 下。OTLP 导出后 Jaeger 呈现一棵"会话→轮次→相位→LLM→工具→输出"
    的完整链路树，多轮对话同树不同分支，无需外部桥接胶水层。
    R1 零改动：相位来源仍是框架既有 LifecycleBus emit。
    """
    name = "trace.lifecycle"
    _ids = count(1)

    def __init__(self, trace_id: str, telemetry: NoopTelemetry | None = None) -> None:
        """构造 trace。

        Args:
            trace_id: 链路标识。
            telemetry: 遥测后端；None 用 NoopTelemetry。
        """
        self.trace_id = trace_id
        self.telemetry = telemetry or NoopTelemetry()
        self._parent_var: ContextVar[str] = ContextVar("trace_parent", default="")
        self._root_id = ""       # session root（agent 级单例，跨轮保持）
        self._root_start: float = 0.0
        # turn 的 id/开始时刻必须随执行上下文隔离：并发 say 共享实例属性时，
        # close 会按最后一个 turn 的 id 回写，其余 turn 的 duration 全为 0。
        self._turn_id_var: ContextVar[str] = ContextVar("trace_turn_id", default="")
        self._turn_start_var: ContextVar[float] = ContextVar(
            "trace_turn_start", default=0.0)
        self._turn_seq = count(1)  # 轮次序号（名字唯一）：count.__next__ 在 GIL 下原子
        self._registered_buses: set[int] = set()  # 已见 bus id（判重辅助，见 register）

    @property
    def _parent(self) -> str:
        return self._parent_var.get()

    @_parent.setter
    def _parent(self, value: str) -> None:
        self._parent_var.set(value)

    def register(self, bus: "LifecycleBus") -> None:
        """LifecycleBus 插件协议：订阅全相位，把生命周期转成同一棵 span 树。

        session_start 开 root（parent 空）；其余相位在 root 下开瞬时子 span；
        session_end 收 root（记真实 duration）。bus 每次 say 全新：按总线上真实
        订阅痕迹判重，不靠 id——bus GC 后 id 会被新 bus 复用而静默漏订阅。
        """
        bus_id = id(bus)
        if bus_id in self._registered_buses:
            # id 命中不代表同一 bus（id 会被 GC 后的新 bus 复用）：以总线上
            # 是否真有本 trace 的 hook 为准，避免新 bus 被跳过。
            if any(fn == self._on_phase
                   for subs in bus._subscribers.values() for _, fn in subs):
                return
        self._registered_buses.add(bus_id)
        from compose import LifecyclePhase
        for phase in LifecyclePhase:
            bus.on(phase, self._on_phase, name="trace.lifecycle")

    def _on_phase(self, notice: "Notice") -> None:
        """同步相位 hook：开/收 turn 或登记相位子 span。

        session root 是 agent 级单例（首次开、每轮终态记真实 duration）；每轮
        say 的 session_start→session_end 之间开一个 turn:N 容器 span，相位与
        llm/tool 都挂当前 turn 下——多轮对话在 Jaeger 上是一棵树的多个分支。
        """
        raw = getattr(notice, "phase", "")
        phase = str(getattr(raw, "value", raw))  # LifecyclePhase(StrEnum) → 值
        if phase == "session_start":
            if not self._root_id:  # session root 单例：仅首次开
                self._open_root()
            self._open_turn()  # 本轮容器 span（parent=root）
            self._phase_span(phase)
            return
        if not self._turn_id_var.get():
            return  # 无 turn（相位早于 session_start）：不造孤立 span
        if phase == "session_end":
            self._phase_span(phase)  # turn 的最后一个子节点
            self._close_turn()
            self._close_root()  # 会话终态收 root：记真实 duration
            return
        self._phase_span(phase)  # 其余相位：当前 turn 下的瞬时子 span

    def _phase_span(self, phase: str) -> None:
        """登记并收口一个 turn 下的瞬时相位子 span。

        _register + _replace 一步完成，使 OTLP 后端（_replace 触发导出）上报该
        span——只 register 的相位会存而不上报，Jaeger 上看不到走向。
        """
        span_id = f"{self.trace_id}-phase-{next(self._ids)}"
        span = TraceSpan(trace_id=self.trace_id, span_id=span_id,
                         parent_id=self._turn_id_var.get(), name=f"phase:{phase}",
                         component="lifecycle", duration_ms=0.001)
        self.telemetry._register(span)
        self.telemetry._replace(span)

    def _open_root(self) -> None:
        """开 session root span（agent 级单例，parent 空，跨轮保持）。"""
        import time
        self._root_id = f"{self.trace_id}-root-{next(self._ids)}"
        self._root_start = time.perf_counter()
        root = TraceSpan(trace_id=self.trace_id, span_id=self._root_id,
                         parent_id="", name="session", component="lifecycle")
        # 只注册不导出：OTLP 后端 _replace 会触发导出，若此处再 replace 一次，
        # root 会在开/关时被导出两次 → Jaeger 出现两个同名 root（同 spanId）。
        # root 在 session_end 由 _close_root 导出一次，且带真实 duration。
        self.telemetry._register(root)

    def _close_root(self) -> None:
        """会话终态收 root：记真实 duration（root 单例跨轮保持，不清除 id）。"""
        import time
        from dataclasses import replace
        root = next((s for s in self.telemetry.spans()
                     if s.span_id == self._root_id), None)
        if root is not None:
            ended = replace(root, duration_ms=round(
                (time.perf_counter() - self._root_start) * 1000.0, 3))
            self.telemetry._replace(ended)

    def _open_turn(self) -> None:
        """开一个 turn:N 容器 span（parent=root），后续相位/llm/tool 挂其下。"""
        import time
        turn_no = next(self._turn_seq)
        turn_id = f"{self.trace_id}-turn-{turn_no}-{next(self._ids)}"
        self._turn_id_var.set(turn_id)  # 每轮独立持有，互不覆盖
        self._turn_start_var.set(time.perf_counter())
        turn = TraceSpan(trace_id=self.trace_id, span_id=turn_id,
                         parent_id=self._root_id, name=f"turn:{turn_no}",
                         component="lifecycle")
        self.telemetry._register(turn)
        self._parent = turn_id  # llm/tool 继承当前 turn

    def _close_turn(self) -> None:
        """收 turn span：记本轮真实 duration 并导出，parent 回到 root。"""
        import time
        from dataclasses import replace
        turn_id = self._turn_id_var.get()
        start = self._turn_start_var.get()
        turn = next((s for s in self.telemetry.spans()
                     if s.span_id == turn_id), None)
        if turn is not None:
            ended = replace(turn, duration_ms=round(
                (time.perf_counter() - start) * 1000.0, 3))
            self.telemetry._replace(ended)
        self._turn_id_var.set("")
        self._parent = self._root_id

    @contextmanager
    def span(self, name: str, *, component: str = "") -> Iterator[TraceSpan]:
        """开启嵌套 span：自动接 parent_id，退出后恢复父级并记录真实耗时。"""
        import time
        from dataclasses import replace
        span_id = f"{self.trace_id}-{next(self._ids)}"
        span = TraceSpan(trace_id=self.trace_id, span_id=span_id,
                         name=name, parent_id=self._parent, component=component)
        self.telemetry._register(span)
        previous = self._parent
        self._parent = span_id
        start = time.perf_counter()
        try:
            yield span
        finally:
            # 读回块内可能已写入的 usage（record_usage），否则 duration 写入会用
            # 进入时的快照（tokens=0）覆盖真实账单——页面/Jaeger 全显示 0。
            latest = next((s for s in self.telemetry.spans()
                           if s.span_id == span_id), span)
            ended = replace(latest, duration_ms=(time.perf_counter() - start) * 1000.0)
            self.telemetry._replace(ended)
            self._parent = previous

    def spans(self) -> tuple[TraceSpan, ...]:
        """返回本 trace 的全部 span。"""
        return tuple(s for s in self.telemetry.spans() if s.trace_id == self.trace_id)

    def record_usage(self, usage: "UsageLike") -> None:
        """把本次 LLM usage 记到当前 span（tokens = prompt+completion, cost）。

        门面在 llm span 的 `with` 块内拿到 Result.usage 后调用；span 是 frozen
        DTO，用 _replace 按 span_id 原位替换（与退出时的 duration 记录同机制）。
        无当前 span（用户在 span 外手调）时静默不记，不造孤立数据。
        """
        span_id = self._parent
        if not span_id:
            return
        current = next((s for s in self.telemetry.spans()
                        if s.span_id == span_id), None)
        if current is None:
            return
        from dataclasses import replace
        tokens = int(getattr(usage, "prompt_tokens", 0) or 0) + int(
            getattr(usage, "completion_tokens", 0) or 0)
        cost = float(getattr(usage, "cost_usd", 0.0) or 0.0)
        self.telemetry._replace(replace(current, tokens=tokens, cost=cost))

    def record_metric(self, name: str, value: float,
                      tags: dict[str, DynamicValue] | None = None) -> None:
        """记录指标到遥测后端。"""
        self.telemetry.metric(name, value, tags)

    def record_log(self, event: dict[str, DynamicValue]) -> None:
        """记录日志到遥测后端。"""
        self.telemetry.log(event)
