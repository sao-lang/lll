"""红线静态扫描（AST），按模块：python tests/tools/ast_scan.py --module r1|r2|r3|r4

R1：Program 内不得有时间/随机/网络调用（破坏确定性）；纯运行时不 import random/datetime。
R2：hook/plugin 强制留名；对外只有 host.define()/plugin() 扩展点，无 middleware/filter/pipe 独立 API。
R3：降级阶梯装配期强校验（无环/末级拒答）+ 副作用重试幂等键检查；能力插件不 import IO SDK。
R4：网关必须做结构化校验（未过 schema 不进业务）+ 成本闸（路由/重试不得绕过 Budgets）。

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
    "r3": [SRC / "harness" / "resilience", ROOT / "tests" / "demos" / "demo_r3.py", ROOT / "tests" / "r3"],
    "r4": [SRC / "harness" / "model", ROOT / "tests" / "demos" / "demo_r4.py", ROOT / "tests" / "r4"],
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
    """R2 红线：hook/plugin 留名 + 唯一扩展点（host.define/plugin，无 middleware/filter/pipe 注册 API）。"""
    violations: list[str] = []
    # 检查 v5.py 强制 plugin 留名（Host.plugin 校验 plugin.name）
    v5 = SRC / "hooks" / "v5.py"
    if v5.exists():
        source = v5.read_text(encoding="utf-8")
        if "plugin must have a name" not in source:
            violations.append("Host.plugin() 未强制 plugin 留名")
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


IO_SDK_PREFIXES = (
    "httpx", "requests", "aiohttp", "boto3", "sqlalchemy", "urllib", "socket",
)

# 规则 6 允许的"动态读取边界"模块：context 动态字段只读在这里发生。
DYNAMIC_BOUNDARY_FILES = {"_ctxview.py"}


def _scan_coding_rules(files: list[Path], pkg: Path) -> list[str]:
    """编码规则 5/6 红线（防回归）：禁魔法串比较 + 动态读取须收敛到边界。

    规则 5：有限取值集合用 Enum，禁止 `x == "literal"` 这类裸字符串比较
            （trace 标签/错误码作为参数传给 Decision 不算比较，不拦）。
    规则 6：ctx.meta / effect.payload 的动态读取只允许在 _ctxview.py 边界模块；
            插件内出现 .meta.get/.payload.get/getattr 即违规（除非上一行有
            "规则 6 例外" 注释，用于鸭子插件装配期容错）。
    v5.1 补充：RequestScope 已无 meta 容器（请求期参数经回调直送 effect、装配期配置走
            构造注入），插件内 .meta.get 一律视为旧裸键残留；effect 具名字段
            （如 idempotency_key）不在此列。判定逻辑不变，仍按上述 AST 特征扫描。
    """
    violations: list[str] = []
    for path in files:
        if not path.is_relative_to(pkg) or path.name in DYNAMIC_BOUNDARY_FILES:
            continue
        tree = parse(path)
        lines = path.read_text(encoding="utf-8").splitlines()

        # 规则 5：字符串字面量比较
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} 规则5 魔法串比较 "
                                f"{ast.unparse(node.left)} vs {comp.value!r}（应改用 Enum）"
                            )

        # 规则 6：动态读取须在边界模块
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_dynamic = False
            # .meta.get(...) / .payload.get(...)
            if isinstance(func, ast.Attribute) and func.attr == "get":
                base = ast.unparse(func.value)
                if base.endswith(".meta") or base.endswith(".payload"):
                    is_dynamic = True
            # getattr(...)
            if isinstance(func, ast.Name) and func.id == "getattr":
                is_dynamic = True
            if not is_dynamic:
                continue
            # 例外：紧邻上方 3 行内含"规则 6 例外"注释（装配期鸭子插件容错）
            comment_ok = any(
                "规则 6 例外" in lines[i]
                for i in range(max(0, node.lineno - 4), min(len(lines), node.lineno - 1))
            )
            if not comment_ok:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} 规则6 动态读取未收敛到 "
                    f"_ctxview.py 边界（或缺少'规则 6 例外'注释）"
                )
    return violations


def scan_r3(files: list[Path]) -> list[str]:
    """R3 红线：降级装配期校验 + 幂等键检查 + hook 留名 + 不 import IO SDK。"""
    violations: list[str] = []
    pkg = SRC / "harness" / "resilience"

    # 降级阶梯必须调用 validate_ladder（装配期强校验：无环/末级拒答/quality 递减）。
    degrade = pkg / "defenses.py"
    if degrade.exists() and "validate_ladder(ladder)" not in degrade.read_text(encoding="utf-8"):
        violations.append("Degradation 未调用 validate_ladder（降级成环/无拒答兜底未拦截）")

    # 重试插件必须检查幂等键（无幂等键的副作用不得重试）。
    # 幂等键由 _ctxview 边界读取后以具名字段提供，故检查 idempotency_key 引用。
    retry = pkg / "retry.py"
    if retry.exists() and "idempotency_key" not in retry.read_text(encoding="utf-8"):
        violations.append("RetryPolicy 未检查 idempotency_key（无幂等键副作用可能重复执行）")

    # 能力插件不得直接 import IO SDK（分层纪律：只依赖 R1+R2）。
    for path in files:
        if not path.is_relative_to(pkg):
            continue
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in IO_SDK_PREFIXES):
                        violations.append(f"{path.relative_to(ROOT)} import IO SDK {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(p) for p in IO_SDK_PREFIXES):
                    violations.append(f"{path.relative_to(ROOT)} import IO SDK {node.module}")

    # hook 类必须留名（复用 R2 判定）。
    for path in files:
        if not (path.is_relative_to(ROOT / "tests")):
            continue
        tree = parse(path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Hook"):
                if not _declares_name(node):
                    violations.append(f"{path.relative_to(ROOT)} hook 类 {node.name} 无 name 属性")

    # 编码规则 5/6 红线（防回归）
    violations.extend(_scan_coding_rules(files, pkg))
    return violations


def scan_r4(files: list[Path]) -> list[str]:
    """R4 红线：结构化校验 + 成本闸 + 缓存键版本 + 不 import IO SDK。"""
    violations: list[str] = []
    pkg = SRC / "harness" / "model"

    # 网关必须做结构化校验（strict：未过 schema 的输出不进业务）。
    gateway = pkg / "gateway.py"
    if gateway.exists():
        text = gateway.read_text(encoding="utf-8")
        # v1.6：校验入口可为模块级 validate_json_schema（旧）或注入校验器的 .check()
        # （SchemaValidator 可插拔）；两者任一 + 超次拒答即满足红线。
        has_validation = "validate_json_schema" in text or "_validator.check(" in text
        if not has_validation or "repair exhausted" not in text:
            violations.append("ModelGateway 未做结构化校验/超次拒答（非法输出可能进业务）")
        if "_record_cost" not in text or "CostGuard" not in text:
            violations.append("ModelGateway 未做成本归因/成本闸（可能绕过 Budgets）")

    # 缓存键必须含版本。
    types = pkg / "types.py"
    if types.exists() and "prompt_version" not in types.read_text(encoding="utf-8"):
        violations.append("缓存键未含 prompt_version（版本变化无法失效）")

    # 能力插件不得直接 import IO SDK。
    for path in files:
        if not path.is_relative_to(pkg):
            continue
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in IO_SDK_PREFIXES):
                        violations.append(f"{path.relative_to(ROOT)} import IO SDK {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(p) for p in IO_SDK_PREFIXES):
                    violations.append(f"{path.relative_to(ROOT)} import IO SDK {node.module}")

    # 编码规则 5/6 红线（防回归）
    violations.extend(_scan_coding_rules(files, pkg))
    return violations


SCANNERS = {"r1": scan_r1, "r2": scan_r2, "r3": scan_r3, "r4": scan_r4}


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
