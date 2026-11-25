"""公开 API 面真实 e2e：introspection 清单登记 + CLI/governance/stream/static 逐条真调用。

覆盖 api 模块 28 个 __all__ 导出与公开方法；新增符号需在映射表登记（API-02 校验逐条映射）。
禁止只测符号存在/isinstance；全部走真实入口断言精确值。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compose import PortCtx

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / ".conductor" / "analysis" / "2026-11-24" / "api-inventory.json"


def test_api01_inventory_complete() -> None:
    """全量 API 清单必须穷举且计数精确（122 模块 / 514 方法 / 74 函数 / 0 导入失败）。"""
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert len(data) == 122, f"模块数应 122，实际 {len(data)}"
    methods = sum(len(c["methods"]) for e in data.values() for c in e["classes"])
    funcs = sum(len(e["functions"]) for e in data.values())
    assert methods == 514, f"公开方法数应 514，实际 {methods}"
    assert funcs == 74, f"模块函数数应 74，实际 {funcs}"
    assert all(not e.get("import_error") for e in data.values()), "不得有导入失败模块"


def test_api02_mapping_complete() -> None:
    """全量 588 符号逐条映射：用户可见族必须有真用例，EXEMPT 理由须符合准入白名单。"""
    from api_surface_map import build_map, load_symbols, COVERED_FAMILIES, EXEMPT_REASONS
    mapping = build_map()
    symbols = load_symbols()
    # ① 每条符号恰好一条归属（逐条，不是数行数）
    by_id = {s["id"]: s for s in mapping["symbols"]}
    assert len(by_id) == len(symbols) == 588, "映射必须逐条覆盖全部 588 符号"
    assert {s["id"] for s in symbols} == set(by_id), "映射 id 必须与 introspection 符号集合完全一致"
    # ② 用户可见族零 EXEMPT（不许把公开 API 塞进豁免蒙混）
    for prefix in COVERED_FAMILIES:
        family = [s for s in mapping["symbols"] if s["id"].startswith(prefix)]
        assert family, f"映射表缺用户可见族 {prefix}"
        assert all(s["status"] == "covered_by" for s in family), \
            f"用户可见族 {prefix} 有 EXEMPT 符号（登记即覆盖，禁止）"
    # ③ covered_by 目标必须精确到真存在的 ::node id（不许文件级登记即覆盖）
    for s in mapping["symbols"]:
        if s["status"] == "covered_by":
            assert "::" in s["target"], f"{s['id']} covered_by 非 node id：{s['target']}"
            target_file_str, node = s["target"].split("::", 1)
            target_file = ROOT / target_file_str
            assert target_file.is_file(), f"{s['id']} 的用例文件不存在：{target_file}"
            assert f"def {node}(" in target_file.read_text(encoding="utf-8"), \
                f"{s['id']} 的用例节点不存在：{s['target']}"
    # ④ exempt 理由非空且落在准入白名单
    for s in mapping["symbols"]:
        if s["status"] == "exempt":
            assert s["reason"].strip(), f"{s['id']} EXEMPT 无理由"
            assert any(s["reason"].startswith(r) for r in EXEMPT_REASONS), \
                f"{s['id']} EXEMPT 理由不符准入：{s['reason']}"
    # ⑤ api.__all__ 交叉校验：真被 e2e 调用的用户可见 API 不得 EXEMPT
    import api
    for name in api.__all__:
        family_symbols = [s for s in mapping["symbols"]
                          if s["id"] == f"api.{name}" or s["id"].startswith(f"api.{name}.")]
        if family_symbols and any(s["status"] == "covered_by" for s in family_symbols):
            assert all(s["status"] == "covered_by" for s in family_symbols), \
                f"api.__all__ 的 {name} 部分符号被 EXEMPT（定义即豁免，禁止）"


def test_api02_all_protos_count() -> None:
    """PORT_PROTOCOLS 必须是 14 键的权威集合，不多不少。"""
    from compose.ports import PORT_PROTOCOLS
    expected = {"llm", "tool", "skill", "rag", "memory", "store", "sandbox",
                "audit", "eval", "telemetry", "human", "mcp", "event_stream", "disclosure"}
    assert set(PORT_PROTOCOLS) == expected
    assert len(PORT_PROTOCOLS) == 14


def test_api03_capabilities_count() -> None:
    """能力包源码真相：11 个 harness Capability + LLMProviderCapability，逐个可 import。"""
    mods = [
        ("harness.cognition.capability", "CognitionCapability"),
        ("harness.cognition.reflection_capability", "ReflectionCapability"),
        ("harness.context.capability", "MemoryCapability"),
        ("harness.eval.capability", "EvalCapability"),
        ("harness.model.capability", "ModelCapability"),
        ("harness.observability.capability", "ObservabilityCapability"),
        ("harness.prompt", "PromptCapability"),
        ("harness.rag.capability", "RAGCapability"),
        ("harness.resilience.capability", "ResilienceCapability"),
        ("harness.security.capability", "SecurityCapability"),
        ("harness.tools.capability", "ToolsCapability"),
    ]
    import importlib
    for mod_name, cls_name in mods:
        cls = getattr(importlib.import_module(mod_name), cls_name)
        install = getattr(cls, "install", None)
        assert install is not None, f"{cls_name} 缺 install"
        # 反证：真调用 install 签名可绑定（不是只看符号存在）
        import inspect
        sig = inspect.signature(install)
        assert "container" in sig.parameters or "reg" in sig.parameters, f"{cls_name}.install 签名不符 Capability 协议"
    from ports.adapters.provider import LLMProviderCapability
    import inspect
    assert "container" in inspect.signature(LLMProviderCapability.install).parameters


def test_api04_cli_real_commands() -> None:
    """CLI 真执行：init 成功、未知命令失败，commands() 精确含四命令。"""
    from api import CLI
    cli = CLI()
    cmds = cli.commands()
    assert set(cmds) == {"init", "eval", "replay", "gate"}
    ok = cli.run("init", name="e2e-proj")
    assert ok.ok is True, f"init 应成功，实际 {ok}"
    bad = cli.run("no-such-command")
    assert bad.ok is False
    assert "unknown" in str(bad.data).lower() or "unknown" in str(getattr(bad, "error", "")).lower()


def test_api05_governance_real_decisions() -> None:
    """治理真判定：熔断/成本/灰度/开关的精确返回值。"""
    from api.governance import KillSwitch, CostLimiter, Rollout, FeatureFlags, Governance
    ks = KillSwitch()
    assert ks.allow("any") is True
    ks.enable("maintenance")
    assert ks.enabled is True
    assert ks.allow("any") is False
    ks.disable()
    assert ks.allow("any") is True

    limiter = CostLimiter(cap=5.0)
    assert limiter.charge(3.0) is True
    assert limiter.remaining() == 2.0
    assert limiter.charge(4.0) is False
    assert limiter.remaining() == 2.0

    rollout = Rollout(percent=100)
    assert rollout.enabled("k1") is True, "100% 放量下任意 key 必命中"
    zero = Rollout(percent=0)
    assert zero.enabled("k1") is False, "0% 放量下任意 key 必不命中"

    flags = FeatureFlags()
    assert flags.is_on("f") is False
    flags.set("f", True)
    assert flags.is_on("f") is True

    gov = Governance(cost_cap=1.0)
    assert gov.allow("cheap", cost=0.5) is True
    assert gov.allow("expensive", cost=2.0) is False


def test_api06_stream_gateway_projection(loop: "object") -> None:
    """StreamGateway 真发布+投影：EventEnvelope 进 stream 后 Timeline 可读。"""
    from api import StreamGateway
    from harness.observability import EventEnvelope
    gw = StreamGateway()
    env = EventEnvelope(seq=1, topic="trace.span", ts=1.0, type="span", data={"name": "llm"})
    gw.publish(env)
    # publish 后订阅能读到该事件（精确断言事件内容），用统一 loop fixture
    from conftest import run
    async def check() -> None:
        got = []
        async for e in gw.stream().subscribe("trace.span"):
            got.append(e)
            if len(got) >= 1:
                break
        assert len(got) >= 1
        assert got[0].data.get("name") == "llm"
    run(check(), loop)  # type: ignore[arg-type]


def test_api07_static_check_api() -> None:
    """static_check_api 真返回 StaticCheckReport，errors 为空、arch/version 精确。"""
    from api import static_check_api
    from api.dsl import custom_arch
    from runtime import Invoke, Terminate

    def factory():
        out = yield Invoke(port="llm", payload={})
        yield Terminate(output=out)
        return out
    arch = custom_arch(factory, name="api07-arch", version="2.3.4")
    report = static_check_api(arch)
    assert report.arch == "api07-arch"
    assert report.version == "2.3.4"
    assert tuple(report.errors) == ()
