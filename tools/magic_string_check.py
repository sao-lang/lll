#!/usr/bin/env python3
"""魔法字符串检查器（零依赖 / 可移植）：禁止用裸字符串表达有限取值集合。

用途：写完或修改 Python 代码后必跑，输出为空才算完成：
    python magic_string_check.py path/to/file.py [more.py ...]
    python magic_string_check.py src/            # 扫整个目录
    python magic_string_check.py --selftest      # 验证检查器本身有效

退出码：0=干净；1=发现魔法串；2=用法/语法错误。

检测三类（这是 AI 最常写出魔法串的地方）：
  E1 比较中的字符串字面量：if status == "active" / x in ("a", "b") / case "pending"
  E2 状态类变量被赋字符串字面量：status = "active"（变量名含 status/state/mode/kind/
     action/level/phase/type/result/verdict/direction/role...）
  E3 函数直接返回字符串字面量作为状态：return "ok"（同上，返回的是有限取值的语义）

默认跳过：测试文件（test_*.py / *_test.py / conftest.py / tests/ 目录）——断言里的
期望字符串是测试的本职，不是状态机魔法串。用 --check-tests 可强制检查测试。

合法场景用行内注释豁免（写在同行或上一行）：# magic-ok: 外部协议要求原样字符串
典型合法：HTTP 方法名/外部 API 的枚举值/日志消息/dict 的键名/命令行参数。

为何需要机器检查而不是只靠规则文字：AI 读完"用 Enum"的规定后仍会在写代码时
顺手用裸串；把检查变成交付前的强制命令，才把软约束变成硬门禁。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import TypeGuard

__all__ = ["scan_source", "scan_path", "Violation"]

# 豁免标记：同行或上一行注释含任一标记即跳过该处。
_EXEMPT_MARKERS = ("magic-ok", "magic_ok", "noqa: magic", "noqa:magic")

# 状态类变量名片段（E2/E3 用）：命中即认为该变量承载"有限取值语义"。
_STATE_HINTS = (
    "status", "state", "mode", "kind", "action", "level", "phase", "type",
    "result", "verdict", "direction", "role", "stage", "step", "policy",
    "category", "outcome", "decision", "tone", "severity", "priority",
)


class Violation:
    """一条违规记录：文件 / 行号 / 类别 / 说明。"""

    def __init__(self, path: str, line: int, code: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.code}] {self.detail}"

    def __repr__(self) -> str:
        return f"Violation({self!s})"


def _is_str(node: ast.AST | None) -> TypeGuard[ast.Constant]:
    """节点是否为字符串字面量（f-string 的 JoinedStr 不算——它是消息文本）。

    用 TypeGuard 而非 bool：让类型检查器在 if 分支内把 node 收窄为 Constant，
    后续访问 .value 才有类型（否则 hover 显示 Attribute value is unknown）。
    """
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _str_literals_in(node: ast.AST) -> list[str]:
    """取容器（tuple/list/set）里的字符串字面量值；非纯字符串容器返回空。"""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return []
    values: list[str] = []
    for item in node.elts:
        if not _is_str(item) or not isinstance(item.value, str):
            return []
        values.append(item.value)
    return values


def _looks_like_state(name: str) -> bool:
    """变量名是否像"有限取值"（包含状态类片段，或全大写常量名）。"""
    lowered = name.lower()
    if any(hint in lowered for hint in _STATE_HINTS):
        return True
    return name.isupper() and len(name) > 1


def _exempt(lines: list[str], lineno: int) -> bool:
    """该行或上一行是否带豁免注释（大小写不敏感）。"""
    for idx in (lineno - 1, lineno - 2):
        if 0 <= idx < len(lines):
            if any(marker in lines[idx].lower() for marker in _EXEMPT_MARKERS):
                return True
    return False


def scan_source(source: str, filename: str = "<string>") -> list[Violation]:
    """扫描一段 Python 源码，返回违规列表（不抛语法错误之外的异常）。"""
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    found: list[Violation] = []

    # Enum 类体里的 `NAME = "value"` 是正确模式（规则 5 的正解），不能当 E2 报。
    # 先收集所有 Enum 类的源码行区间，扫描时跳过。
    enum_lines: set[int] = set()
    for top in ast.walk(tree):
        if isinstance(top, ast.ClassDef):
            is_enum = any(
                (isinstance(base, ast.Name) and base.id == "Enum")
                or (isinstance(base, ast.Attribute) and base.attr == "Enum")
                for base in top.bases
            )
            if is_enum:
                enum_lines.update(range(top.lineno, getattr(top, "end_lineno", top.lineno) + 1))

    for node in ast.walk(tree):
        if _exempt(lines, getattr(node, "lineno", 0)):
            continue

        # E1a：比较运算中的字符串字面量（== != in not in）。
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                    continue
                if _is_str(right):
                    found.append(Violation(
                        filename, node.lineno, "E1",
                        f"比较中的魔法串 {right.value!r}：改用 Enum 成员（{ast.unparse(node.left)} vs ...）",
                    ))
                elif isinstance(op, (ast.In, ast.NotIn)) and _str_literals_in(right):
                    found.append(Violation(
                        filename, node.lineno, "E1",
                        f"in 集合魔法串 {ast.unparse(right)}：改用 Enum 元组",
                    ))
                elif _is_str(node.left) and isinstance(op, (ast.Eq, ast.NotEq)):
                    found.append(Violation(
                        filename, node.lineno, "E1",
                        f"左侧魔法串 {node.left.value!r}：改用 Enum 成员",
                    ))

        # E1b：match/case 的字符串字面量模式。
        if isinstance(node, ast.MatchValue) and _is_str(node.value):
            found.append(Violation(
                filename, node.lineno, "E1",
                f"case 魔法串 {node.value.value!r}：改用 Enum 成员做匹配",
            ))

        # E2：状态类变量被赋字符串字面量（Enum 类体内的声明已排除）。
        if getattr(node, "lineno", 0) in enum_lines:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None or not _is_str(value):
                continue
            # 模块级常量（`_ERR_X = "X"` / `REFUSE_TARGET = "refuse"`）本身就是
            # "具名常量替代魔法串"的正解，不报；只报函数体内的局部状态赋值。
            if getattr(node, "col_offset", 0) == 0:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and _looks_like_state(target.id):
                    found.append(Violation(
                        filename, node.lineno, "E2",
                        f"状态变量 {target.id} 被赋魔法串 {value.value!r}：改用 Enum",
                    ))
                elif isinstance(target, ast.Attribute) and _looks_like_state(target.attr):
                    found.append(Violation(
                        filename, node.lineno, "E2",
                        f"状态字段 {target.attr} 被赋魔法串 {value.value!r}：改用 Enum",
                    ))

        # E3：函数返回字符串字面量（有限取值语义）；空串是缺省值不算魔法串。
        if isinstance(node, ast.Return) and _is_str(node.value) and node.value.value:
            found.append(Violation(
                filename, node.lineno, "E3",
                f"返回魔法串 {node.value.value!r}：改用 Enum 成员",
            ))

    return found


def _is_test_file(path: Path) -> bool:
    """是否测试文件：断言中的期望字符串是测试本职，默认不当魔法串。"""
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" \
        or "tests" in path.parts


def scan_path(target: Path, *, check_tests: bool = False) -> list[Violation]:
    """扫描文件或目录（目录递归 .py；自动跳过隐藏目录/__pycache__/venv）。"""
    if target.is_file():
        files = [target] if target.suffix == ".py" else []
    else:
        files = [
            p for p in target.rglob("*.py")
            if not any(part.startswith(".") or part in ("__pycache__", "venv", ".venv")
                       for part in p.parts)
        ]
    if not check_tests:
        # 测试里的 `assert x == "expected"` 是断言，不是状态机魔法串；默认跳过。
        files = [p for p in files if not _is_test_file(p)]
    found: list[Violation] = []
    for path in sorted(files):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            found.append(Violation(str(path), 0, "E0", f"读取失败：{exc}"))
            continue
        try:
            found.extend(scan_source(source, str(path)))
        except SyntaxError as exc:
            found.append(Violation(str(path), exc.lineno or 0, "E0", f"语法错误：{exc.msg}"))
    return found


def _selftest() -> int:
    """自测：内置 5 个反例必须被抓、5 个正例必须放行，验证检查器本身没坏。"""
    bad = '''\
def f(status):
    if status == "active":
        return "ok"
    if status in ("new", "old"):
        pass
    mode = "fast"
    match status:
        case "pending":
            pass
'''
    good = '''\
from enum import Enum
class Status(str, Enum):
    ACTIVE = "active"
    PENDING = "pending"
def f(status: Status) -> Status:
    if status is Status.ACTIVE:
        return Status.ACTIVE
    if status == "active":  # magic-ok: 外部 API 返回原样字符串
        pass
    return Status.PENDING
'''
    bad_found = scan_source(bad, "bad.py")
    good_found = scan_source(good, "good.py")
    ok = len(bad_found) >= 4 and not good_found
    print(f"selftest: bad={len(bad_found)} (需>=4), good={len(good_found)} (需=0) -> "
          f"{'PASS' if ok else 'FAIL'}")
    for item in bad_found:
        print("  " + str(item))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="魔法串检查器：写完代码必跑，输出为空才算完成")
    parser.add_argument("paths", nargs="*", type=Path, help="要检查的 .py 文件或目录")
    parser.add_argument("--selftest", action="store_true", help="运行内置自测")
    parser.add_argument("--check-tests", action="store_true", help="连测试文件一起检查（默认跳过）")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.paths:
        parser.print_help()
        return 2

    found: list[Violation] = []
    for path in args.paths:
        if not path.exists():
            print(f"路径不存在：{path}", file=sys.stderr)
            return 2
        found.extend(scan_path(path, check_tests=args.check_tests))

    for item in found:
        print(item)
    if found:
        print(f"\n发现 {len(found)} 处魔法串 → 改为 Enum 或加 # magic-ok 豁免注释", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
