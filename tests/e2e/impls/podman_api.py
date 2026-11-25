"""用户侧 Podman Docker-兼容 API 客户端（e2e 基础设施，src 零改）。

Podman Desktop 的命名管道 ``\\\\.\\pipe\\podman-machine-default`` 暴露 Docker
兼容 HTTP API，但未安装 docker SDK / requests。本模块直接用内建 ``open`` 以
``r+b`` + ``buffering=0`` 读写 HTTP/1.1 原始报文（含 chunked 解码），提供起停
真 Jaeger 容器所需的最小动词。

参考 tmp/probe_podman.py 与 tmp/probe_jaeger_container.py 的实测实现。
"""
from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass

PIPE = r"\\.\pipe\podman-machine-default"
IMAGE_JAEGER = "quay.io/jaegertracing/all-in-one:1.62.0"


def http(
    method: str,
    path: str,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    pipe: str = PIPE,
) -> tuple[str, bytes, dict[str, str]]:
    """向命名管道发一个 HTTP/1.1 请求，返回 (状态行, 响应体, 响应头)。

    ``Connection: close`` 保证服务端关管后 ``f.read`` 自然返回 b"" 结束读取；
    chunked 编码手工解包。无超时参数（命名管道 open 不支持），靠服务端关管。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    headers = {
        "Host": "docker",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    head = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
    req = f"{method} {path} HTTP/1.1\r\n{head}\r\n\r\n".encode() + body
    with open(pipe, "r+b", buffering=0) as f:
        f.write(req)
        raw = b""
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            raw += chunk
    header_blob, _, payload = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("utf-8", "replace").split("\r\n")
    status = lines[0]
    parsed: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            parsed[key.strip().lower()] = value.strip()
    if "chunked" in parsed.get("transfer-encoding", ""):
        out = b""
        rest = payload
        while True:
            size_line = re.match(rb"^([0-9a-fA-F]+)\r\n", rest)
            if not size_line:
                break
            size = int(size_line.group(1), 16)
            rest = rest[size_line.end():]
            if size == 0:
                break
            out += rest[:size]
            rest = rest[size + 2:]
        payload = out
    return status, payload, parsed


def status_code(status_line: str) -> int:
    """从 'HTTP/1.1 201 Created' 取数字状态码；非标准返回 0。"""
    parts = status_line.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


def free_port() -> int:
    """向 OS 申请一个空闲高位端口（bind :0 后立即释放）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def free_ports(count: int) -> list[int]:
    """申请 count 个互不相同的空闲端口。"""
    ports: list[int] = []
    while len(ports) < count:
        port = free_port()
        if port not in ports:
            ports.append(port)
    return ports


@dataclass(frozen=True, slots=True)
class JaegerContainer:
    """一次真容器实例的连接信息与 ID（teardown 用）。"""

    container_id: str
    name: str
    api_port: int
    otlp_port: int

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def otlp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.otlp_port}"


class PodmanAPI:
    """最小 Docker 兼容动词集：create / start / stop / remove。"""

    def __init__(self, pipe: str = PIPE) -> None:
        self.pipe = pipe

    def request(self, method: str, path: str, body: bytes = b"",
                extra_headers: dict[str, str] | None = None
                ) -> tuple[str, bytes, dict[str, str]]:
        """发请求；命名管道路径为实例属性，便于未来替换/诊断。"""
        return http(method, path, body, extra_headers, pipe=self.pipe)

    def create_jaeger(self, name: str, api_port: int, otlp_port: int,
                      image: str = IMAGE_JAEGER) -> str:
        """创建 all-in-one 容器，返回容器 ID；非 201 原样抛带响应体的错误。"""
        payload = {
            "Image": image,
            "HostConfig": {
                "PortBindings": {
                    "16686/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(api_port)}],
                    "4318/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(otlp_port)}],
                }
            },
            "ExposedPorts": {"16686/tcp": {}, "4318/tcp": {}},
        }
        status, body, _ = self.request(
            "POST", f"/containers/create?name={name}",
            json.dumps(payload).encode("utf-8"))
        data = self._json_body(body)
        if status_code(status) != 201 or "Id" not in data:
            raise RuntimeError(
                f"create 容器失败: status={status!r} body={body.decode('utf-8', 'replace')[:500]}")
        return str(data["Id"])

    def start(self, container_id: str) -> None:
        """启动容器；204 为成功。"""
        status, body, _ = self.request("POST", f"/containers/{container_id}/start")
        if status_code(status) != 204:
            raise RuntimeError(
                f"start 容器失败: status={status!r} body={body.decode('utf-8', 'replace')[:500]}")

    def stop(self, container_id: str) -> None:
        """停止容器（已停/不存在容错：teardown 不因 304/404 中断）。"""
        self.request("POST", f"/containers/{container_id}/stop")

    def remove(self, container_id: str) -> None:
        """强制删除容器（teardown 同样容错）。"""
        self.request("DELETE", f"/containers/{container_id}?force=1")

    def cleanup(self, container_id: str) -> None:
        """停止 + 删除；任何异常内部消化，只保证尽最大努力清理。"""
        for fn in (self.stop, self.remove):
            try:
                fn(container_id)
            except Exception:  # noqa: BLE001 - teardown 不得掩盖用例真实失败
                pass

    @staticmethod
    def _json_body(body: bytes) -> dict[str, object]:
        """把响应体按 JSON 对象解析；非对象/空体返回空 dict。"""
        if not body:
            return {}
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
