# Podman 环境探测留痕（2026-11-24，方案阶段）
命令在项目根用 .venv/Scripts/python 执行；原始 stdout 摘录，未加工。

## 1) 命名管道 /_ping
$ .venv/Scripts/python -c "...open(r'\\.\\pipe\\podman-machine-default','r+b')... GET /_ping ..."
HTTP/1.1 200 OK | Api-Version: 1.44 | Libpod-Api-Version: 6.2.0 | Docker-Experimental: true | BODY=OK

## 2) 本地镜像（probe_podman.py images）
COUNT 5: postgres:16-alpine, redis:7-alpine, node:22-alpine, widgetpilot/node-base:dev, wp-sandbox:dev （均 daocloud/localhost；无 jaeger）

## 3) 镜像拉取（probe_podman.py pull，X-Registry-Auth=e30=）
$ ... pull docker.m.daocloud.io/jaegertracing/all-in-one 1.62  -> HTTP/1.1 403 Forbidden
$ ... pull docker.io/jaegertracing/all-in-one 1.62           -> HTTP/1.1 500 Internal Server Error
$ ... pull quay.io/jaegertracing/all-in-one 1.62             -> HTTP/1.1 404 Not Found

## 4) 代理与 Podman 配置
socket probe: 127.0.0.1:61303 CLOSED(timeout); 127.0.0.1:54802 OPEN
shell env: HTTP_PROXY=http://127.0.0.1:54802; HKCU\Environment DEEPSEEK_API_KEY present, len=35（不记录前缀/明文）
GET /info: HTTPProxy=None, HTTPSProxy=None, RegistryConfig.Mirrors=[]
