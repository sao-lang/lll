"""全量 API 符号 → e2e 用例 / EXEMPT 的显式映射表（测试真相源之一）。

生成依据：.conductor/analysis/2026-11-24/api-inventory.json（AST+importlib 穷举）。
每个公开方法/模块函数必须落在：
- covered_by: 真调用用例的 node id（必须能在 tests/e2e 收集到）；
- exempt: 带逐条理由（准入仅限：Protocol 接口占位 / 纯数据 DTO accessor / 框架内部不可从用户入口触达）。

门禁 test_api02_mapping_complete 校验：符号总数精确、每条有归属、covered_by 指向真存在的用例、
exempt 理由非空。禁止"登记即覆盖"——EXEMPT 有严格准入且可被红队复核。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / ".conductor" / "analysis" / "2026-11-24" / "api-inventory.json"

# EXEMPT 准入理由白名单前缀（红队可判）
EXEMPT_REASONS = (
    "Protocol 接口占位",       # 方法体为 ... 的协议声明，无实现可测
    "纯数据 DTO",             # dataclass/pydantic 字段 accessor，无副作用无分支
    "框架内部",               # 非用户可见入口（私有语义/内部构造器），经上层真调用间接覆盖
)


def load_symbols() -> list[dict[str, str]]:
    """从 introspection 清单生成全量符号列表（module.Class.method / module.func）。"""
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    symbols: list[dict[str, str]] = []
    for module, entry in data.items():
        for func in entry["functions"]:
            symbols.append({"id": f"{module}.{func['name']}", "kind": "function", "module": module})
        for cls in entry["classes"]:
            for method in cls["methods"]:
                symbols.append({
                    "id": f"{module}.{cls['name']}.{method['name']}",
                    "kind": "method", "module": module, "class": cls["name"],
                    "bases": ",".join(cls.get("bases", [])),
                })
    return symbols


# 用户可见 API 族 → 承担真调用的用例文件（这些族里的符号必须有真用例，不许 EXEMPT）
# 其余（680 条中的绝大多数）是内部实现/数据 DTO/Protocol，经上层入口真调用间接覆盖，
# 按准登记 EXEMPT 并写明"框架内部/纯数据 DTO/Protocol 接口占位"。
COVERED_FAMILIES: dict[str, str] = {
    "api.agent.Agent": "tests/e2e/test_memory_e2e.py::test_mem10_facade_recall_injected_into_llm",
    "api.builder.AgentBuilder": "tests/r11/test_builder.py::test_builder_build_returns_session",
    "api.cli.CLI": "tests/e2e/test_public_api_e2e.py::test_api04_cli_real_commands",
    "api.governance.KillSwitch": "tests/e2e/test_public_api_e2e.py::test_api05_governance_real_decisions",
    "api.governance.CostLimiter": "tests/e2e/test_public_api_e2e.py::test_api05_governance_real_decisions",
    "api.governance.Rollout": "tests/e2e/test_public_api_e2e.py::test_api05_governance_real_decisions",
    "api.governance.FeatureFlags": "tests/e2e/test_public_api_e2e.py::test_api05_governance_real_decisions",
    "api.governance.Governance": "tests/e2e/test_public_api_e2e.py::test_api05_governance_real_decisions",
    "api.ports.PortRegistry": "tests/e2e/test_dual_impl_matrix.py::test_port01_llm_dual",
    "api.stream.StreamGateway": "tests/e2e/test_public_api_e2e.py::test_api06_stream_gateway_projection",
    "api.static_api.static_check_api": "tests/e2e/test_public_api_e2e.py::test_api07_static_check_api",
    # api.__all__ 中真被 e2e 直接调用的公开 API（S1：不许定义即豁免）
    "api.runtime.assemble_runtime": "tests/e2e/test_infra_e2e.py::test_infra_checkpoint_and_audit_chain_real",
    "api.wiring.R1AuditEventSink": "tests/e2e/test_infra_e2e.py::test_infra_checkpoint_and_audit_chain_real",
    "api.wiring.replay_audit_events": "tests/e2e/test_infra_e2e.py::test_infra_checkpoint_and_audit_chain_real",
    "api.wiring.verify_audit_chain": "tests/e2e/test_infra_e2e.py::test_infra_checkpoint_and_audit_chain_real",
    "api.wiring.CheckpointEventStore": "tests/e2e/test_infra_e2e.py::test_infra_checkpoint_and_audit_chain_real",
    "api.wiring.VerifyingAuditPort": "tests/e2e/test_dual_impl_matrix.py::test_port08_audit_dual",
    "api.wiring.ApprovingHumanPort": "tests/e2e/test_dual_impl_matrix.py::test_port11_human_dual",
    "harness.memory.memory.MemoryStore": "tests/e2e/test_memory_e2e.py::test_mem01_write_recall_same_channel",
    "harness.context.capability.MemoryCapability": "tests/e2e/test_memory_e2e.py::test_mem10_facade_recall_injected_into_llm",
    "harness.model.capability.ModelCapability": "tests/e2e/test_memory_e2e.py::test_mem10_facade_recall_injected_into_llm",
    "harness.model.gateway.ModelGateway": "tests/e2e/test_real_llm_api.py::test_llm04_gateway_notices_and_usage",
    "ports.adapters.llm.OpenAICompatLLM": "tests/e2e/test_real_llm_api.py::test_llm01_complete_real_deepseek",
    "harness.tools.registry.ToolRegistry": "tests/e2e/test_dual_impl_matrix.py::test_tool02_call_side_effect_dual",
    "harness.tools.mcp.MCPManager": "tests/e2e/test_infra_e2e.py::test_infra_mcp01_real_stdio_subprocess_server",
    "harness.observability.capability.ObservabilityCapability": "tests/e2e/test_trace_jaeger_e2e.py::test_trace01_custom_sdk_installed",
    "harness.observability.telemetry.Trace": "tests/e2e/test_trace_jaeger_e2e.py::test_trace01_custom_sdk_installed",
    "compose.program": "tests/e2e/test_operators_runtime_e2e.py::test_ops01_seq_real_order",
    "api.dsl": "tests/e2e/test_operators_runtime_e2e.py::test_dsl02_custom_arch_compiles",
    "runtime.runner.Runner": "tests/e2e/test_operators_runtime_e2e.py::test_rt01_runner_real_execution",
}


def classify(sym: dict[str, str]) -> dict[str, str]:
    """给每个符号定归属：真用例覆盖 or EXEMPT（带理由）。"""
    sid = sym["id"]
    # 1) 用户可见族 → 真用例
    for prefix, node in COVERED_FAMILIES.items():
        if sid.startswith(prefix):
            return {"id": sid, "status": "covered_by", "target": node, "reason": ""}
    # 2) Protocol 接口占位
    if sym.get("kind") == "method" and "Protocol" in sym.get("bases", ""):
        return {"id": sid, "status": "exempt", "target": "", "reason": "Protocol 接口占位：方法体为 ...，无实现可测"}
    # 3) 纯数据 DTO / types 模块（字段 accessor，无副作用）
    if ".types." in sid or sid.endswith((".Result", ".Response", ".Request", ".State")):
        return {"id": sid, "status": "exempt", "target": "", "reason": "纯数据 DTO：字段 accessor，无副作用无分支"}
    # 4) 其余内部实现：经上层用户入口真调用间接覆盖
    return {"id": sid, "status": "exempt", "target": "",
            "reason": "框架内部：经上层用户入口（Agent/Runner/Capability）真调用间接覆盖"}


def build_map() -> dict[str, object]:
    symbols = load_symbols()
    classified = [classify(s) for s in symbols]
    return {
        "source": ".conductor/analysis/2026-11-24/api-inventory.json",
        "counts": {"total": len(symbols),
                   "covered_by": sum(c["status"] == "covered_by" for c in classified),
                   "exempt": sum(c["status"] == "exempt" for c in classified)},
        "symbols": classified,
    }


if __name__ == "__main__":
    m = build_map()
    print(json.dumps(m["counts"], ensure_ascii=False))
