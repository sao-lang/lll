"""红线静态扫描（AST），按模块：python tests/tools/ast_scan.py --module r1|r2

R1：Program 内不得有时间/随机/网络调用（破坏确定性）；纯运行时不 import random/datetime。
R2：hook 强制留名；对外只有 bus.on() 一个注册入口，无 middleware/filter/pipe 独立 API。

扫描真实源码，不做运行时推断；任一红线命中退出码 1。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

TARGETS = {
    "r1": [SRC / "runtime", ROOT / "tests" / "demos" / "demo_r1.py", ROOT / "tests" / "r1"],
    "r2": [SRC / "hooks", ROOT / "tests" / "demos" / "demo_r2.py", ROOT / "tests" / "r2"],
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


def scan_r1(files: list[Path]) -> list[str]:
    """R1 红线：Program 纯函数 + 纯运行时不 import 时间/随机。"""
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
                if any(name.startswith(bad) for bad in FORBIDDEN_IN_PROGRAM):
                    violations.append(f"{path.relative_to(ROOT)}:{call.lineno} Program 内调用 {name}()")
        # 仅扫描 src/runtime 的 import（测试/demo 里 import time 做计时是允许的）
        if path.is_relative_to(SRC / "runtime"):
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = ast.unparse(node)
                    if "random" in module or "datetime" in module:
                        violations.append(f"{path.relative_to(ROOT)} 纯运行时 import {module}")
    print(f"   扫描 {len(files)} 个文件 / {generators} 个 generator 函数")
    return violations


def _declares_name(cls_node: ast.ClassDef) -> bool:
    """类属性 name = ... 或 self.name = ... 均算留名。"""
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


def scan_r2(files: list[Path]) -> list[str]:
    """R2 红线：hook 留名 + 唯一扩展点（无 middleware/filter/pipe 注册 API）。"""
    violations: list[str] = []
    # 检查 bus.py 强制留名
    bus = SRC / "hooks" / "bus.py"
    if bus.exists():
        source = bus.read_text(encoding="utf-8")
        if "hook must have a non-empty name" not in source:
            violations.append("bus.on() 未强制 hook 留名")
    # 检查对外 API 里没有 middleware/filter/pipe 的注册方法
    init = SRC / "hooks" / "__init__.py"
    if init.exists():
        text = init.read_text(encoding="utf-8").lower()
        for banned in ("middleware", "def filter(", "def pipe("):
            if banned in text:
                violations.append(f"hooks/__init__.py 出现被禁的独立 API: {banned}")
    # 检查测试里的 hook 类都声明了 name（留名纪律）
    for path in files:
        if not (path.is_relative_to(ROOT / "tests")):
            continue
        tree = parse(path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Hook"):
                if not _declares_name(node):
                    violations.append(f"{path.relative_to(ROOT)} hook 类 {node.name} 无 name 属性")
    return violations


SCANNERS = {"r1": scan_r1, "r2": scan_r2}


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentKit 红线静态扫描（R1/R2）")
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
