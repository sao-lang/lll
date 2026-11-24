"""相位 → OTLP 桥接（examples 层，src 零改）。

把框架 16 相位 lifecycle 通知转成 OTLP span 上报 Jaeger——补齐 Jaeger 上
"只有 3 个自动埋点 span、80% 交互流程不可见"的缺口。

设计（针对独立 review 的问题，先想清楚再写）：
- hook 同步、在 Agent loop 线程执行 → 只入队（O(1)），**不阻塞主流程**；
- 后台 daemon 线程 drain 队列 → export_span（慢 IO 不在主路径）；
- 相位 span 与 llm/tool span **共用 agent.trace.trace_id**（Jaeger 串成一条链）；
- Notice.payload（phase/port/exceeded/step/status/error）作为 OTLP attributes；
- 失败消费在 telemetry 内部（不抛穿）；close() 优雅停线程。
"""
from __future__ import annotations

import queue
import threading
from typing import Any

from compose import LifecyclePhase, Notice
from harness.observability import TraceSpan


class PhaseOTLPBridge:
    """16 相位 Notice → 后台线程 → OTLP span（完整交互流程可见）。"""

    def __init__(self, agent: Any | None,
                 telemetry: Any | None = None) -> None:  # purity-exempt: telemetry 鸭子 OTLPTelemetry
        self.agent = agent
        self._telemetry = telemetry
        self._q: queue.Queue[tuple[Notice, int]] = queue.Queue()  # (Notice, 到达时刻ns)
        self._stop = threading.Event()
        self._attached = False
        self._seq = 0
        self._root_span_id = ""  # session 根 span：所有相位挂其下形成链路树
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="phase-otlp-export")
        self._worker.start()

    def attach(self) -> None:
        """注册全部 16 相位 hook（跨 say 持久，框架 _replay_hooks 重放）。"""
        if self.agent is None or self._attached:
            return
        for phase in LifecyclePhase:
            self.agent.on(phase, self._on_notice, name="trace.otlp.phase")
        self._attached = True

    def _on_notice(self, notice: Notice) -> None:
        """同步 hook：入队时打**真实到达时刻**（不是导出时刻），不 IO、不抛穿。"""
        import time
        self._q.put((notice, time.time_ns()))

    def _run(self) -> None:
        """后台线程：drain → export（慢 HTTP 在这里，不在主路径）。"""
        while not self._stop.is_set():
            try:
                notice, arrived_ns = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._export(notice, arrived_ns)
            except Exception:  # noqa: BLE001 - 遥测绝不抛穿（通知语义）
                pass
            finally:
                self._q.task_done()

    def _telemetry_of(self) -> Any | None:
        """优先注入的 telemetry；否则从 agent 的能力包取（OTLPTelemetry）。"""
        if self._telemetry is not None:
            return self._telemetry
        if self.agent is None:
            return None
        try:
            from harness.observability.capability import ObservabilityCapability
            cap = next((c for c in self.agent._caps.values()
                        if isinstance(c, ObservabilityCapability)), None)
            return cap.telemetry if cap is not None else None
        except (AttributeError, StopIteration):
            return None

    def _export(self, notice: Notice, arrived_ns: int) -> None:
        """把 Notice 转成相位 span 上报（同 trace_id + 真实时刻 + 链路树）。

        session_start 建根 span，其余相位 parent=根 span → Jaeger 呈现一颗
        交互链路树（而非一堆平级散点）；start/end 用 hook 到达时刻（非导出时刻）。
        """
        telemetry = self._telemetry_of()
        if telemetry is None or not hasattr(telemetry, "export_span"):
            return  # NoopTelemetry 无 export_span → 不上报（离线零 IO，诚实）
        trace_id = ""
        if self.agent is not None:
            try:
                trace_id = str(self.agent.trace.trace_id)  # 与自动埋点同链
            except Exception:  # noqa: BLE001
                trace_id = ""
        if not trace_id:
            trace_id = notice.session_id or "phase"

        self._seq += 1
        phase = notice.phase.value if hasattr(notice.phase, "value") else str(notice.phase)
        span_id = f"phase-{self._seq}-{phase}"
        # session_start 为根；新会话（seq 重置或不同 session）也重建根
        is_root = phase == "session_start" or not self._root_span_id
        if is_root:
            self._root_span_id = span_id
        payload = dict(notice.payload)
        span = TraceSpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_id="" if is_root else self._root_span_id,
            name=f"phase:{phase}",
            component="lifecycle",
            duration_ms=0.0,  # 相位是瞬时点；start/end 显式用真实到达时刻
        )
        extra: dict[str, Any] = {"phase": phase, "port": notice.port,
                                 "r1_seq": notice.seq}
        extra.update({str(k): v for k, v in payload.items()})
        telemetry.export_span(span, extra_attrs=extra,
                              start_ns=arrived_ns, end_ns=arrived_ns + 1_000)

    def close(self) -> None:
        """优雅停后台线程：drain 完队列剩余事件再退出（避免丢尾部相位）。"""
        self._q.join()  # 等队列里所有已入队事件被处理（task_done 配对）
        self._stop.set()
        self._worker.join(timeout=2.0)
