"""start.py · react-chat 一键启动器（密钥不落盘，只读环境变量）。

作用：解决 Windows 上「setx 写入 User 级后，已开终端的新进程仍读不到」的问题——
启动时若当前进程无 DEEPSEEK_API_KEY，则从 Windows 注册表 User 区读取并注入
本进程环境（仅内存，不写任何文件），再启动 Web 服务。

用法：
    .venv/Scripts/python examples/react-chat/start.py

密钥来源优先级（全程不落项目文件）：
    1. 当前进程环境变量 DEEPSEEK_API_KEY（最高）
    2. Windows 注册表 HKCU\\Environment（兜底，读 setx 写入的值）
    3. 都没有 → 以离线模式启动（不报错、不伪造数据）
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "examples" / "trace-viewer" / "server.py"


def _read_user_env(name: str) -> str | None:
    """Windows：从注册表 User 区读取环境变量（只读，不写文件）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-not-found]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
    except (OSError, ImportError, FileNotFoundError):
        return None


def main() -> None:
    key = os.getenv("DEEPSEEK_API_KEY") or _read_user_env("DEEPSEEK_API_KEY")
    if key:
        os.environ["DEEPSEEK_API_KEY"] = key  # 仅注入本进程及子进程，不落盘
        print(f"[start] 已加载 DEEPSEEK_API_KEY（长度 {len(key)}，不写入任何文件）")
    else:
        os.environ["CHAT_OFFLINE"] = "1"
        print("[start] 未找到密钥 → 离线模式（工具返回降级标记，不伪造数据）")

    # 链路追踪后端：同样从注册表兜底读 setx 写入的 endpoint（B1 修复）——
    # 否则旧终端 setx 后子进程读不到，静默退化 NoopTelemetry（零上报）。
    otlp = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or _read_user_env(
        "OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp:
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = otlp
        print(f"[start] 已加载 OTEL_EXPORTER_OTLP_ENDPOINT={otlp}（链路将上报 Jaeger）")
    else:
        print("[start] 未设 OTLP endpoint → 链路用 NoopTelemetry（不上报平台）")

    print(f"[start] 启动 Web 服务：{SERVER}")
    raise SystemExit(subprocess.call([sys.executable, str(SERVER)]))


if __name__ == "__main__":
    main()
