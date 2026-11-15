"""红线静态扫描（AST），按模块执行：python tools/ast_scan.py --module m1

M1 纯函数 / M2 组件留名与短路 / M3 适配器无状态与错误信封 / M4 MCP 全量注入。
扫描对象是真实源码，不做运行时推断；任一红线命中即退出码 1。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "agentkit"

TARGETS = {
    "m1": [SRC / "runtime", ROOT / "demos" / "demo_m1.py", ROOT / "tests" / "m1"],
    "m2": [SRC / "chains", ROOT / "demos" / "demo_m2.py", ROOT / "tests" / "m2"],
    "m3": [SRC / "ports", ROOT / "demos" / "demo_m3.py", ROOT / "tests" / "m3"],
    "m4": [SRC / "harness" / "tools", ROOT / "demos" / "demo_m4.py", ROOT / "tests" / "m4"],
}

FORBIDDEN_IN_PROGRAM = (
    "datetime.now", "time.time", "time.monotonic", "random.", "requests.",
    "httpx.", "urllib.", "socket.", "aiohttp.", "open", "os.system",
    "subprocess.", "boto3.", "sqlalchemy.",
)


def files_of(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
        elif path.exists():
            out.append(path)
    return out


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def scan_m1(files: list[Path]) -> list[str]:
    violations: list[str] = []
    generators = 0
    for path in files:
        tree = parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
                continue
            generators += 1
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                name = dotted(call.func)
                if any(name.startswith(bad) or name == bad for bad in FORBIDDEN_IN_PROGRAM):
                    violations.append(f"{path.relative_to(ROOT)}:{call.lineno} Program 内调用 {name}()")
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ast.unparse(node)
                if "random" in module or "datetime" in module:
                    violations.append(f"{path.relative_to(ROOT)} 纯运行时 import {module}")
    print(f"   扫描 {len(files)} 个文件 / {generators} 个 generator 函数")
    return violations


def _declares_name(cls_node: ast.ClassDef) -> bool:
    """类属性 name = ... 或 self.name = ...（含元组赋值 self.name, x = ...）均算留名。"""
    for stmt in ast.walk(cls_node):
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == "name":
                return True
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for part in ast.walk(target):
                    if isinstance(part, ast.Name) and part.id == "name":
                        return True
                    if isinstance(part, ast.Attribute) and part.attr == "name":
                        return True
    return False


def scan_m2(files: list[Path]) -> list[str]:
    violations: list[str] = []
    builtins = SRC / "chains" / "builtins.py"
    tree = parse(builtins)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not _declares_name(node):
            violations.append(f"chains/builtins.py 组件 {node.name} 无 name 属性")
    source = (SRC / "chains" / "chains.py").read_text(encoding="utf-8")
    if "require_name(" not in source:
        violations.append("chains.py 未强制组件留名")
    if "allow_short_circuit" not in source:
        violations.append("chains.py 未处理未声明的 middleware 短路")
    return violations


def scan_m3(files: list[Path]) -> list[str]:
    violations: list[str] = []
    adapters = SRC / "ports" / "adapters"
    for path in sorted(adapters.glob("*.py")):
        if path.name == "fakes.py":  # fake 允许脚本状态
            continue
        tree = parse(path)
        for node in tree.body:  # 模块级全局可变状态（__all__ 等元数据除外）
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if targets and all(t.startswith("__") for t in targets):
                    continue
                violations.append(f"{path.name}:{node.lineno} 模块级可变全局状态")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                        if target.value.id == "self" and target.attr in (
                            "session_id", "session", "_session", "state", "_state"
                        ):
                            violations.append(f"{path.name}:{node.lineno} 适配器持有会话状态 self.{target.attr}")
    call_source = (SRC / "ports" / "call.py").read_text(encoding="utf-8")
    if "retryable" not in call_source:
        violations.append("call.py 的错误信封缺少 retryable")
    return violations


def scan_m4(files: list[Path]) -> list[str]:
    violations: list[str] = []
    registry = (SRC / "harness" / "tools" / "registry.py").read_text(encoding="utf-8")
    mcp = (SRC / "harness" / "tools" / "mcp.py").read_text(encoding="utf-8")
    decorator = (SRC / "harness" / "tools" / "decorator.py").read_text(encoding="utf-8")
    if "MCP tools fully injected" not in registry:
        violations.append("registry.static_check 未拦截 MCP 全量注入")
    if 'parameters_schema": {}' not in registry:
        violations.append("catalog(level=1) 未剥离完整 schema")
    if "messages" in mcp or "context.append" in mcp:
        violations.append("mcp.py 疑似把工具 schema 注入上下文")
    if "additionalProperties" not in decorator:
        violations.append("@tool 未落实 extra=forbid 语义")
    if "danger" not in decorator:
        violations.append("@tool 未强制 danger 声明")
    return violations


SCANNERS = {"m1": scan_m1, "m2": scan_m2, "m3": scan_m3, "m4": scan_m4}


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentKit 红线静态扫描")
    parser.add_argument("--module", required=True, choices=sorted(SCANNERS))
    args = parser.parse_args()
    files = files_of(TARGETS[args.module])
    print(f"[红线扫描 {args.module.upper()}]")
    violations = SCANNERS[args.module](files)
    if violations:
        for item in violations:
            print(f"  FAIL  {item}")
        print(f"[红线扫描 {args.module.upper()}] 不通过：{len(violations)} 处违规")
        return 1
    print(f"[红线扫描 {args.module.upper()}] PASS：无红线违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
