"""R2-T04/T06（真实覆盖，替代幻影 tests/r2/test_trace.py）：
hook 故障隔离且记 trace + trace 含全部 plugin name。

设计出处：R2-插件总线与协议.md §9 验证矩阵 R2-T04/T06、§5 观察扩展点。
"""
from __future__ import annotations

import asyncio

from hooks import BailHook, ParallelHook, RequestScope


class TestPluginNameTrace:
    def test_trace_contains_all_tapped_plugin_names(self) -> None:
        """每次扩展点调用把已 tap 的插件名记入 trace（顺序即 tap 序）。"""
        hook = BailHook("before_invoke")
        hook.tap("guard_a", lambda scope, effect: None)
        hook.tap("guard_b", lambda scope, effect: None)
        scope = RequestScope()
        asyncio.run(hook.call(scope))
        assert any("guard_a" in t for t in scope.trace)
        assert any("guard_b" in t for t in scope.trace)


class TestParallelFailureTrace:
    def test_observer_exception_isolated_and_traced(self) -> None:
        """PARALLEL 观察 tap 抛异常：不影响主流程、不抛穿，但记 trace 供诊断。"""
        scope = RequestScope()
        hook = ParallelHook("after_invoke")

        def bad(scope: RequestScope, effect: object) -> None:
            raise RuntimeError("observer blew up")

        def good(scope: RequestScope, effect: object) -> None:
            scope.trace.append("good-observed")

        hook.tap("bad_observer", bad).tap("good_observer", good)
        # 不抛异常即"隔离成功"
        asyncio.run(hook.call(scope))
        assert "good-observed" in scope.trace
        assert any("bad_observer" in t and "RuntimeError" in t for t in scope.trace), \
            f"异常观察插件未留痕：{scope.trace}"
