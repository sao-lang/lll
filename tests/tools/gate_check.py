"""门禁执行器：python tests/tools/gate_check.py --module r1|r2

按《门禁体系》G0~G4 逐条检查，全部为合入前置条件：
  G0 契约冻结 / G1 质量(mypy strict + 覆盖率 + docstring + 红线扫描)
  G2 行为断言 / G3 集成与 demo 冒烟 / G4 文档与版本一致
任一不绿 → 退出码 1（拒绝合入，无例外通道）。

G5 设计稳定性（契约锁）已于 2026-11-18 按用户要求删除——它锁死公共 API 与文档锚点，
导致多轮迭代只敢改文档表述、不敢动代码（薄实现长期得不到纠正）。
质量门禁（测试/覆盖率/类型/红线）保留不变。
G0 增加规则 11 检查：模块承诺的默认实现类必须在生产源码真实存在（fake/门面不算），
防止 R5 那种"协议+MCPManager 门面+测试 fake 三件套冒充 stdio/http 默认实现"的假完成。

R1=核心运行时(src/runtime)，R2=插件总线与协议(src/hooks)。
旧 m1~m4 体系已重写为 R1/R2，见 docs/02-模块详设/Agent/。
"""
from __future__ import annotations

import argparse
import ast
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
PY = sys.executable

# 规则 11（设计承诺反向闭包核对）机器化：协议声明的默认实现必须真实存在。
# key=PORT_PROTOCOLS 的 port 名；value=(默认实现类名, 期望源码路径片段)。
# 来源：docs/01-总览与设计/AGENT-总设计.md 附录 B.1.1/B.1.2 + 各模块回头清单。
DEFAULT_IMPL_CONTRACTS: dict[str, tuple[str, str]] = {
    "llm": ("OpenAICompatLLM", "ports/adapters/llm.py"),
    "tool": ("ToolRegistry", "harness/tools/registry.py"),
    "skill": ("FileSkillProvider", "harness/skills/provider.py"),
    "disclosure": ("ToolIndex", "harness/tools/index.py"),
    "mcp": ("HttpMCPTransport", "ports/adapters/mcp.py"),
    "webfetch": ("HttpFetcher", "ports/adapters/webfetch.py"),
    "rag": ("RAGPipeline", "harness/rag/pipeline.py"),
    "memory": ("MemoryStore", "harness/memory/memory.py"),
    "store": ("InMemoryStorePort", "harness/memory/store.py"),
    # R9/R10 回头清单（B.1.2 → B.1.1）：六个默认实现必须真实存在。
    "sandbox": ("LocalSandbox", "harness/security/ports.py"),
    "audit": ("AuditLogger", "harness/security/ports.py"),
    "human": ("ConsoleHumanPort", "harness/security/ports.py"),
    "eval": ("RuleEval", "harness/eval/rules.py"),
    "telemetry": ("NoopTelemetry", "harness/observability/telemetry.py"),
    "event_stream": ("InMemoryEventStream", "harness/observability/stream.py"),
}


MODULES = {
    "r1": {
        "title": "R1 核心运行时", "pkg": "runtime", "path": "runtime",
        "doc": "docs/02-模块详设/Agent/R1-核心运行时.md", "demo": "tests/demos/demo_r1.py", "coverage": 90,
        "symbols": ["Effect", "Invoke", "Spawn", "Suspend", "Terminate", "Disposition",
                    "AgentState", "Budgets", "Result", "Runner",
                    "Session", "Event", "fold", "fingerprint", "checkpoint"],
        "behavior": ["test_determinism.py", "test_replay.py", "test_budgets.py", "test_batch.py"],
        "integration": ["test_runner.py", "test_suspend.py", "test_errors.py", "test_types.py",
                        "test_snapshot_migration.py"],
    },
    "r2": {
        "title": "R2 算子与装配（Port + 组合层 call + notify）", "pkg": "compose", "path": "compose",
        "doc": "docs/02-模块详设/Agent/R2-算子与装配.md", "demo": "tests/demos/demo_r2.py", "coverage": 60,
        # 旧符号 HookKind/Host/RequestScope/Signal/HookCtx/Decision/DecisionAction 已删除（P13）。
        # 算子核心（10 算子 + call 装饰器）在 compose 包，由 r12 门禁 + ast_scan 核对。
        "symbols": ["LifecycleBus", "LifecyclePhase", "Notice", "PHASE_COUNT",
                    "Registry", "Capability", "PORT_PROTOCOLS",
                    "LLMRequest", "ToolSpec", "make_call"],
        "behavior": ["test_lifecycle.py"],
        "integration": ["test_ports.py", "test_registry.py", "test_errors.py",
                        "test_lifecycle_wiring.py"],
    },
    "r3": {
        "title": "R3 可靠性", "pkg": "harness.resilience", "path": "harness/resilience",
        "doc": "docs/02-模块详设/Agent/R3-可靠性.md", "demo": "tests/demos/demo_r3.py", "coverage": 90,
        "symbols": ["FailureClass", "FailureClassifier", "DimensionKey", "DEFAULT_FAILURE_TABLE",
                    "backoff_delay", "classify_failure", "normalize_code", "validate_ladder",
                    "RetryPolicy", "CircuitBreaker", "Degradation", "RateLimit", "RatePermit",
                    "Bulkhead", "Idempotency", "Watchdog", "KillSwitch",
                    "ResilienceCapability", "ReliabilityExecutor", "Timeout",
                    "DegradeLadder", "DegradeStep", "DegradeMode",
                    "BreakerStatus", "BreakerState", "BreakerStore", "InMemoryBreakerStore"],
        "behavior": ["test_types.py", "test_breaker.py", "test_retry.py",
                     "test_classify_signature.py", "test_breaker_store.py"],
        "integration": ["test_degrade.py", "test_degrade_pipeline.py", "test_capability.py",
                        "test_e2e_degrade.py", "test_timeout.py"],
    },
    "r4": {
        "title": "R4 模型与推理", "pkg": "harness.model", "path": "harness/model",
        "doc": "docs/02-模块详设/Agent/R4-模型与推理.md", "demo": "tests/demos/demo_r4.py", "coverage": 85,
        "symbols": ["StructuredOutputSpec", "CacheKey", "RouteDecision", "CostRecord",
                    "UsageLedger", "CostAttributor", "CostGuard", "build_cache_key",
                    "ModelRouter", "RuleRouter", "CachePort", "ExactCache",
                    "validate_json_schema", "StructuredOutputValidator",
                    "SchemaValidator", "MinimalSchemaValidator",
                    "ModelGateway", "ModelCapability", "HashCacheEmbedder"],
        "behavior": ["test_types.py", "test_router.py", "test_structured.py", "test_cache.py",
                     "test_cache_embedder_default.py"],
        "integration": ["test_adapter.py", "test_cost.py", "test_gateway.py",
                        "test_e2e_wiring.py", "test_model_registry.py", "test_refuse.py"],
    },
    "r5": {
        "title": "R5 工具·MCP·技能", "pkg": "harness.tools", "path": "harness",
        "doc": "docs/02-模块详设/Agent/R5-工具MCP与技能.md", "demo": "tests/demos/demo_r5.py", "coverage": 95,
        "cov_tests": ["tests/r5", "tests/wiring/test_mcp_facade_wiring.py",
                      "tests/wiring/test_disclosure_before_model.py",
                      "tests/wiring/test_disclosure_sources_gate.py",
                      "tests/wiring/test_guardrail_output_injection.py"],
        "cov_tests": ["tests/r5", "tests/wiring/test_mcp_facade_wiring.py",
                      "tests/wiring/test_disclosure_before_model.py",
                      "tests/wiring/test_disclosure_sources_gate.py",
                      "tests/wiring/test_guardrail_output_injection.py"],
        "symbols": ["ToolSource", "SandboxPolicy", "ToolFn", "tool", "ToolRegistry",
                    "ToolIndex", "MCPTransport", "MCPManager", "ToolsCapability",
                    "WebFetcher", "WebFetchTool", "pending_tools", "clear_pending"],
        "behavior": ["test_decorator.py", "test_routing.py", "test_index.py", "test_errors.py"],
        "integration": ["test_mcp.py", "test_skills.py", "test_capability.py", "test_webfetch.py",
                        "test_custom_mcp_client.py"],
        # R5 跨 tools/skills 两包：额外强制 skills 包覆盖率（主 pkg=tools 已由 coverage 字段约束）。
        "extra_cov": ["harness.skills"],
        # 规则 11：本模块承诺的默认实现必须真实存在（fake/门面不算）。
        "required_defaults": {
            "tool": DEFAULT_IMPL_CONTRACTS["tool"],
            "skill": DEFAULT_IMPL_CONTRACTS["skill"],
            "disclosure": DEFAULT_IMPL_CONTRACTS["disclosure"],
            "mcp": DEFAULT_IMPL_CONTRACTS["mcp"],
            "webfetch": DEFAULT_IMPL_CONTRACTS["webfetch"],
        },
    },
    "r6": {
        "title": "R6 RAG 检索增强", "pkg": "harness.rag", "path": "harness/rag",
        "doc": "docs/02-模块详设/Agent/R6-RAG检索增强.md", "demo": "tests/demos/demo_r6.py", "coverage": 85,
        "symbols": ["RAGPipeline", "Document", "Chunk", "Hit", "Query", "RAGResult",
                    "TokenBudget", "Vector", "Splitter", "RecursiveSplitter",
                    "Embedder", "HashEmbedder", "VectorStore", "InMemoryVectorStore",
                    "Retriever", "VectorRetriever", "HybridRetriever",
                    "PostProcessor", "DefaultPostProcessor", "QueryRewriter",
                    "Reranker", "LexicalReranker"],
        "behavior": ["test_pipeline.py", "test_budget.py", "test_hybrid.py"],
        "integration": ["test_fallback.py", "test_injection.py", "test_cache.py"],
        "required_defaults": {
            "rag": DEFAULT_IMPL_CONTRACTS["rag"],
        },
    },
    "r7": {
        "title": "R7 上下文与记忆", "pkg": "harness.context", "path": "harness/context",
        "doc": "docs/02-模块详设/Agent/R7-上下文与记忆.md", "demo": "tests/demos/demo_r7.py", "coverage": 85,
        "symbols": [
            "ContextSection", "TrimPolicy", "TRIGGER_RATIO", "ContextAssembler",
            "DefaultContextAssembler", "SYSTEM_SECTION_NAME", "CompactionPolicy",
            "DefaultCompactionPolicy", "count_tokens", "BlobPort", "Offloader",
            "InMemoryBlobStore", "DefaultOffloader", "BLOB_HANDLE_SCHEME",
            "make_handle", "parse_handle", "ContextMemoryManager",
            "Reinjector", "DefaultReinjector", "TokenCounter", "DefaultTokenCounter",
            "estimate_tokens", "MemoryScope", "ScopeResolver", "make_item_id",
            # D4/D5 新增方案（落盘策略 + KV cache 一等可观测指标）
            "CacheTelemetry",
        ],
        "behavior": [
            "test_types.py", "test_assembly.py", "test_compaction.py",
            "test_optimization_chains.py", "test_context_policies.py", "test_trap_fixes.py",
        ],
        "integration": [
            "test_offload.py", "test_memory.py", "test_wiring.py",
        ],
        "extra_cov": ["harness.memory"],
        "required_defaults": {
            "memory": DEFAULT_IMPL_CONTRACTS["memory"],
            "store": DEFAULT_IMPL_CONTRACTS["store"],
        },
    },
    "r11": {
        "title": "R11 集成接入与治理（api：四组 API + 装配 + 治理 + CLI）",
        "pkg": "api", "path": "api",
        "doc": "docs/02-模块详设/Agent/R11-集成接入与治理.md",
        "demo": "tests/demos/demo_r11.py", "coverage": 85,
        "symbols": [
            "PROFILES", "profile_names", "PortRegistry", "PROTOCOL_NAMES",
            "StreamGateway", "static_check_api",
            "KillSwitch", "CostLimiter", "Rollout", "FeatureFlags", "Governance",
            "AgentBuilder", "CLI", "CLIResult",
        ],
        "behavior": ["test_builder.py", "test_compose.py", "test_ports.py",
                     "test_stream.py", "test_static.py", "test_governance.py", "test_cli.py"],
        "integration": [],
    },
    "r12": {
        "title": "R12 算子核心（compose 10 算子 + archs 10 架构 + Agent 装配器）",
        "pkg": "compose", "path": "compose",
        "doc": "docs/02-模块详设/Agent/R12-内置架构与组合.md",
        "demo": "tests/demos/demo_r12.py", "coverage": 85,
        # 规格 P6/P9：10 算子 + archs 10 架构 + Agent 装配器 + call 装饰器 + DTO。
        "symbols": [
            "seq", "par", "branch", "race", "loop", "map_state", "map_effect",
            "fallback", "gate", "retry", "Agent",
            "build_call", "dispatch", "make_dispatch",
            "FaultRecord", "RoutingTarget", "CallState", "CallContext",
            "retry_call", "timeout", "danger_route", "injection_guard",
            "layer_program",
        ],
        "behavior": ["test_compose.py", "test_call.py", "test_r3_four_pits.py",
                     "test_archs.py", "test_static.py", "test_edges.py", "test_agent.py"],
        "integration": ["test_r6_pilot.py", "test_archs_semantics.py", "test_tool_layers.py",
                        "test_wiring.py"],
        "extra_cov": ["archs"],
        # compose 的生命周期/Registry/DTO 契约由 tests/r2 覆盖，与 r12 算子测试合并计量。
        "cov_tests": ["tests/r2", "tests/r12"],
    },
    "r8": {
        "title": "R8 认知与多 Agent 编排",
        "pkg": "harness.cognition", "path": "harness/cognition",
        "doc": "docs/02-模块详设/Agent/R8-认知与多Agent编排.md",
        "demo": "tests/demos/demo_r8.py", "coverage": 85,
        # 认知能力包（cognition/capability.py + reflection_capability.py）的测试
        # 写在 r11 门面集成测试里——同包覆盖率须纳入，否则新代码拉低 r8 覆盖率。
        "cov_tests": ["tests/r8", "tests/r11/test_cognition_capability_red.py",
                      "tests/r11/test_reflection_capability_red.py"],
        "symbols": [
            "IntentResult", "IntentSample", "Critique", "Step", "StepResult",
            "IntentStrategy", "KeywordMatcher", "IntentSampleStore",
            "CheapIntentModel", "PrimaryIntentModel",
            "CascadeIntentStrategy", "InMemoryIntentSampleStore", "RegexKeywordMatcher",
            "intent_as_program",
            "RulePlanner", "RuleExecutor", "RuleRePlanner", "ThresholdReflector",
        ],
        "behavior": ["test_intent.py", "test_plan.py", "test_reflect.py",
                     "test_cascade_service.py"],
        "integration": [],
    },
    "r9": {
        "title": "R9 安全与 HITL",
        "pkg": "harness.security", "path": "harness/security",
        "doc": "docs/02-模块详设/Agent/R9-安全与HITL.md",
        "demo": "tests/demos/demo_r9.py", "coverage": 85,
        "symbols": [
            "SecurityCtx", "SafetyVerdict", "SafetyReport", "AuditEvent", "verify_hash_chain",
            "Guardrail", "RuleGuardrail", "inspect_untrusted", "redact_secret",
            "LocalSandbox", "AuditLogger", "ConsoleHumanPort",
            "ApprovalFlow", "SecurityCapability",
        ],
        "behavior": ["test_guardrail.py", "test_audit.py", "test_sandbox.py", "test_approval.py"],
        "integration": ["test_wiring.py"],
        "required_defaults": {
            "sandbox": DEFAULT_IMPL_CONTRACTS["sandbox"],
            "audit": DEFAULT_IMPL_CONTRACTS["audit"],
            "human": DEFAULT_IMPL_CONTRACTS["human"],
        },
    },
    "r10": {
        "title": "R10 可观测·评测·持久化",
        "pkg": "harness.observability", "path": "harness/observability",
        "doc": "docs/02-模块详设/Agent/R10-可观测评测与持久化.md",
        "demo": "tests/demos/demo_r10.py", "coverage": 85,
        "cov_tests": ["tests/r10", "tests/wiring/test_observability_capability_gate.py",
                      "tests/wiring/test_eval_capability_gate.py",
                      "tests/wiring/test_audit_verification_gate.py"],
        "cov_tests": ["tests/r10", "tests/wiring/test_observability_capability_gate.py",
                      "tests/wiring/test_eval_capability_gate.py",
                      "tests/wiring/test_audit_verification_gate.py"],
        "symbols": [
            "TraceSpan", "EventEnvelope", "ReadModel", "NoopTelemetry", "Trace",
            "InMemoryEventStream", "Projection", "TimelineProjection",
        ],
        "behavior": ["test_trace.py", "test_stream.py"],
        "integration": ["test_replay.py", "test_eval.py", "test_eval_suite.py"],
        "extra_cov": ["harness.eval", "harness.durability"],
        "required_defaults": {
            "eval": DEFAULT_IMPL_CONTRACTS["eval"],
            "telemetry": DEFAULT_IMPL_CONTRACTS["telemetry"],
            "event_stream": DEFAULT_IMPL_CONTRACTS["event_stream"],
        },
    },

}

results: list[tuple[str, bool, str]] = []


def command(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def record(gate: str, ok: bool, detail: str = "") -> None:
    results.append((gate, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {gate}" + (f" — {detail}" if detail else ""))
    if not ok and detail and len(detail) > 300:
        print(f"         {detail[-300:]}")


def tail(proc: subprocess.CompletedProcess[str], lines: int = 12) -> str:
    return "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-lines:])


def check_default_impls(module: str) -> None:
    """规则 11 G0：本模块声明的默认实现类必须在生产源码中存在（fake 不算）。

    只检查 cfg 声明的 required_defaults，避免跨模块误报（如 R5 不检查 R9 的 LocalSandbox）。
    """
    cfg = MODULES[module]
    required = cfg.get("required_defaults", {})
    if not required:
        return
    missing: list[str] = []
    for port_name, (class_name, path_hint) in required.items():
        target = SRC / path_hint
        if not target.exists() or f"class {class_name}" not in target.read_text(encoding="utf-8"):
            missing.append(f"{port_name}→{class_name}({path_hint})")
    record("G0 规则11 默认实现存在性", not missing,
           f"{len(required)} 个默认实现类真实存在" if not missing else f"缺失 {missing}")


def g0(cfg: dict, module: str) -> None:
    try:
        mod = importlib.import_module(cfg["pkg"])
        missing = [name for name in cfg["symbols"] if not hasattr(mod, name)]
        record("G0 契约符号", not missing,
               f"{len(cfg['symbols'])} 个公共 API 齐全" if not missing else f"缺失 {missing}")
    except Exception as exc:
        record("G0 契约符号", False, f"import 失败: {exc}")

    doc = ROOT / cfg["doc"]
    ok = doc.exists() and all(k in doc.read_text(encoding="utf-8")
                              for k in ("门禁清单", "功能验证矩阵"))
    record("G0 文档契约（门禁清单 + 验证矩阵）", ok, str(doc.relative_to(ROOT)))

    proc = command(["tests/tools/ast_scan.py", "--module", module])
    record("G0 装配期静态检查 / 红线扫描", proc.returncode == 0,
           proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr.strip())

    # 规则 11：设计承诺的默认实现必须真实存在（防"协议+门面+fake 冒充完成"）。
    check_default_impls(module)


def g1(cfg: dict, module: str) -> None:
    cov_paths = cfg.get("cov_tests", [f"tests/{module}"])
    proc = command(["-m", "pytest", *cov_paths, "-q", "-c", "pyproject.toml",
                    f"--cov={cfg['pkg']}", "--cov-report=term",
                    f"--cov-fail-under={cfg['coverage']}"])
    cover_line = next((line for line in (proc.stdout or "").splitlines()
                       if line.startswith("TOTAL")), "")
    detail = f"覆盖率 {cover_line.split()[-1]}" if cover_line else "pytest rc=0"
    record(f"G1 单测 + 覆盖率 ≥ {cfg['coverage']}%", proc.returncode == 0,
           detail if proc.returncode == 0 else tail(proc))

    # R5 跨包：对额外声明的包逐一强制同一覆盖率阈值（如 harness.skills）。
    for extra in cfg.get("extra_cov", []):
        extra_proc = command(["-m", "pytest", f"tests/{module}", "-q", "-c", "pyproject.toml",
                              f"--cov={extra}", "--cov-report=term",
                              f"--cov-fail-under={cfg['coverage']}"])
        extra_line = next((line for line in (extra_proc.stdout or "").splitlines()
                           if line.startswith("TOTAL")), "")
        record(f"G1 附加包 {extra} 覆盖率 ≥ {cfg['coverage']}%", extra_proc.returncode == 0,
               extra_line.split()[-1] if extra_line else "pytest rc=0")

    mypy = command(["-m", "mypy", "--strict", "src"])
    record("G1 mypy strict", mypy.returncode == 0,
           "no issues found" if mypy.returncode == 0 else tail(mypy))

    compileall = command(["-m", "compileall", "-q", "src"])
    record("G1 语法编译", compileall.returncode == 0)

    pkg_dir = SRC / cfg["path"]
    no_doc = []
    for path in pkg_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if ast.get_docstring(tree) is None:
            no_doc.append(path.name)
    record("G1 模块 docstring", not no_doc, "齐全" if not no_doc else f"缺失 {no_doc}")


def g2(cfg: dict, module: str) -> None:
    paths = [f"tests/{module}/{name}" for name in cfg["behavior"]]
    proc = command(["-m", "pytest", *paths, "-q", "-c", "pyproject.toml"])
    record("G2 行为断言（确定性 / 判决语义 / 错误处置 / 预算 / 路由）",
           proc.returncode == 0, tail(proc, 3))


def g3(cfg: dict, module: str) -> None:
    paths = [f"tests/{module}/{name}" for name in cfg["integration"]]
    proc = command(["-m", "pytest", *paths, "-q", "-c", "pyproject.toml"])
    record("G3 上下游契约测试", proc.returncode == 0, tail(proc, 3))

    demo = command([cfg["demo"]])
    ok = demo.returncode == 0 and "PASS" in demo.stdout
    record("G3 独立 demo 冒烟", ok,
           demo.stdout.strip().splitlines()[-1] if demo.stdout.strip() else tail(demo, 5))


def g4(cfg: dict, module: str) -> None:
    doc = ROOT / cfg["doc"]
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    ok = all(k in text for k in ("闭环", "验收标准", "关键字段", "API 规范"))
    record("G4 文档八要素一致", ok, cfg["doc"])
    version_ok = "version =" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    record("G4 版本号已登记", version_ok, "pyproject.toml")
    previous_green = all(ok for _, ok, _ in results)
    record("G4 前置门禁全绿", previous_green, "G0~G3 全部 PASS" if previous_green else "存在未绿项")
    print("  人工检查清单：验收用例绿 ✓ / 拆分文档与实现一致 ✓ / 版本号更新 ✓（由评审人确认）")


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentKit 模块门禁（R1/R2）")
    parser.add_argument("--module", required=True, choices=sorted(MODULES))
    args = parser.parse_args()
    cfg = MODULES[args.module]
    print(f"=== 门禁执行：{cfg['title']}（{args.module.upper()}）===")
    for label, fn in (("G0", g0), ("G1", g1), ("G2", g2), ("G3", g3), ("G4", g4)):
        print(f"--- {label} ---")
        fn(cfg, args.module)
    failed = [name for name, ok, _ in results if not ok]
    print(f"=== 结果：{len(results) - len(failed)}/{len(results)} 项通过 ===")
    if failed:
        print("门禁不绿 → 拒绝合入；违反红线 → 冻结该模块迭代更新。")
        print("未通过项：" + ", ".join(failed))
        return 1
    print(f"{args.module.upper()} 门禁全绿：允许合入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
