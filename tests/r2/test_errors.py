"""R2-T08：错误信封结构（通用能力，不依赖旧架构总线）。"""
from runtime import Result, PortError


def test_result_error_envelope():
    r = Result.failure("E1", "msg", retryable=True)
    assert r.error == PortError(code="E1", message="msg", retryable=True)
