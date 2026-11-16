"""门禁执行器：python tests/tools/gate_check.py --module m1|m2|m3|m4

按《门禁体系》G0~G4 逐条检查，全部为合入前置条件：
  G0 契约冻结 / G1 质量(mypy strict + 覆盖率 + docstring + 红线扫描)
  G2 行为断言 / G3 集成与 demo 冒烟 / G4 文档与版本一致
任一不绿 → 退出码 1（拒绝合入，无例外通道）。
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

MODULES = {
    "m1": {
        "title": "M1 核心运行时", "pkg": "runtime", "path": "runtime",
        "doc": "docs/02-模块详设/M1-核心运行时.md", "demo": "tests/demos/demo_m1.py", "coverage": 90,
        "symbols": ["Effect", "EffectKind", "AgentState", "Budgets", "Result", "Runner",
                    "Session", "Event", "fold", "fingerprint", "checkpoint"],
        "behavior": ["test_determinism.py", "test_replay.py", "test_budgets.py", "test_batch.py"],
        "integration": ["test_runner.py", "test_suspend.py", "test_errors.py", "test_types.py"],
    },
    "m2": {
        "title": "M2 链路系统", "pkg": "chains", "path": "chains",
        "doc": "docs/02-模块详设/M2-链路系统.md", "demo": "tests/demos/demo_m2.py", "coverage": 90,
        "symbols": ["Chains", "MountPoint", "Verdict", "ChainCtx", "ChainEvent", "DispatchResult"],
        "behavior": ["test_verdict.py", "test_trace.py", "test_hooks.py"],
        "integration": ["test_chains.py", "test_freeze.py"],
    },
    "m3": {
        "title": "M3 端口协议层", "pkg": "ports", "path": "ports",
        "doc": "docs/02-模块详设/M3-端口协议层.md", "demo": "tests/demos/demo_m3.py", "coverage": 85,
        "symbols": ["PortRegistry", "PortCtx", "LLMRequest", "LLMResponse", "ToolCall",
                    "ToolResult", "ToolSpec", "make_call", "PORT_PROTOCOLS"],
        "behavior": ["test_errors.py", "test_registry.py", "test_ports.py"],
        "integration": ["test_adapters.py", "test_child.py"],
    },
    "m4": {
        "title": "M4 工具系统", "pkg": "harness.tools", "path": "harness/tools",
        "doc": "docs/02-模块详设/M4-工具系统.md", "demo": "tests/demos/demo_m4.py", "coverage": 85,
        "symbols": ["tool", "ToolRegistry", "ToolIndex", "MCPManager", "DangerLevel",
                    "SandboxPolicy", "ToolDefinitionError"],
        "behavior": ["test_decorator.py", "test_index.py", "test_routing.py"],
        "integration": ["test_mcp.py", "test_errors.py"],
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


# --------------------------------------------------------------------------- #
def g0(cfg: dict, module: str) -> None:
    try:
        mod = importlib.import_module(cfg["pkg"])
        missing = [name for name in cfg["symbols"] if not hasattr(mod, name)]
        record("G0 契约符号", not missing, f"{len(cfg['symbols'])} 个公共 API 齐全" if not missing else f"缺失 {missing}")
    except Exception as exc:
        record("G0 契约符号", False, f"import 失败: {exc}")

    doc = ROOT / cfg["doc"]
    ok = doc.exists() and all(k in doc.read_text(encoding="utf-8")
                              for k in ("门禁清单", "功能验证矩阵"))
    record("G0 文档契约（门禁清单 + 验证矩阵）", ok, str(doc.relative_to(ROOT)))

    proc = command(["tests/tools/ast_scan.py", "--module", module])
    record("G0 装配期静态检查 / 红线扫描", proc.returncode == 0,
           proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr.strip())


def g1(cfg: dict, module: str) -> None:
    proc = command(["-m", "pytest", f"tests/{module}", "-q", "-c", "pyproject.toml",
                    f"--cov={cfg['pkg']}", "--cov-report=term",
                    f"--cov-fail-under={cfg['coverage']}"])
    cover_line = next((line for line in (proc.stdout or "").splitlines()
                       if line.startswith("TOTAL")), "")
    detail = f"覆盖率 {cover_line.split()[-1]}" if cover_line else "pytest rc=0"
    record(f"G1 单测 + 覆盖率 ≥ {cfg['coverage']}%", proc.returncode == 0,
           detail if proc.returncode == 0 else tail(proc))

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
    parser = argparse.ArgumentParser(description="AgentKit 模块门禁")
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
