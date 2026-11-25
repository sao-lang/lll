"""真实 e2e 套件公共 fixture。

纪律：
- 每个用例独立实例、独立事件循环（Windows proactor 下 asyncio.run 换 loop 会持有失效资源）；
- DeepSeek key 只从进程环境/注册表 User 区读到内存，绝不落文件；
- 真容器/真网失败如实登记，不改断言造绿。
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_deepseek_key() -> str:
    """从进程环境读；缺失则从注册表 User 区读到内存（禁止落文件）。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
                if isinstance(value, str) and value:
                    os.environ["DEEPSEEK_API_KEY"] = value  # 仅进程内存
                    return value
        except OSError:
            pass
    return ""


DEEPSEEK_KEY = _load_deepseek_key()
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# 环境兼容：某些 shell 只注入大写 NO_PROXY，httpx 对本地回环的绕过依赖小写 no_proxy。
# 真实 e2e 大量使用 127.0.0.1 本地 server（MCP/WebFetch/provider/OTLP/Jaeger），
# 若误走 HTTP_PROXY 会拿到 502。统一把大写 NO_PROXY 同步到小写（只影响本地回环，
# 不影响 api.deepseek.com 真网）。
if "NO_PROXY" in os.environ:
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


@pytest.fixture
def deepseek_key() -> str:
    if not DEEPSEEK_KEY:
        pytest.skip("DEEPSEEK_API_KEY 不在进程环境也不在注册表 User 区（环境阻断，证据见 podman-probe-evidence.md）")
    return DEEPSEEK_KEY


@pytest.fixture(scope="function")
def loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """每用例独立 loop，用例结束即关闭（禁跨用例共享，避免 proactor 失效资源）。"""
    new_loop = asyncio.new_event_loop()
    try:
        yield new_loop
    finally:
        new_loop.close()


def run(coro: Any, loop: asyncio.AbstractEventLoop) -> Any:
    """在给定独立 loop 上跑协程，跑完清理 pending task。"""
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def assert_no_secret(text: str) -> None:
    """精确断言：文本不含任何 sk- 凭据片段。"""
    assert "sk-" not in text, "响应/日志/报告泄漏了 sk- 凭据"
