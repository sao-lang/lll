#!/usr/bin/env python3
"""方案阶段 introspection：穷举 src/ 的公开导出符号与方法级 API 清单。

用 AST + importlib 双源交叉：AST 保证不漏文件，importlib 保证拿到真实签名。
输出 JSON 与 Markdown，供 e2e 覆盖矩阵引用。本脚本不是测试用例。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

OUT_DIR = ROOT / ".conductor" / "analysis" / "2026-11-24"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def source_of(obj: Any) -> str | None:
    try:
        return inspect.getsourcefile(obj)
    except TypeError:
        return None


def rel(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def sig(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "(<no-signature>)"


def first_line(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    return doc.splitlines()[0].strip() if doc else ""


def ast_exports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            if value is not None:
                try:
                    return [str(x) for x in ast.literal_eval(value)]
                except (ValueError, TypeError):
                    return []
    return []


def modules() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for path in sorted(SRC.rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        if parts[-1] == "__init__":
            name = ".".join(parts[:-1])
        else:
            name = ".".join(parts)
        out.append((name, path))
    return out


def main() -> int:
    inventory: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    all_modules = modules()

    for name, path in all_modules:
        entry: dict[str, Any] = {
            "file": rel(str(path)),
            "ast_all": ast_exports(path),
            "runtime_all": [],
            "functions": [],
            "classes": [],
            "import_error": "",
        }
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # 环境/依赖缺失必须显式登记，不静默跳过
            entry["import_error"] = f"{type(exc).__name__}: {exc}"
            inventory[name] = entry
            failures.append({"module": name, "error": entry["import_error"]})
            continue

        entry["runtime_all"] = [str(x) for x in getattr(mod, "__all__", [])]
        source_file = rel(str(path))
        for attr in sorted(vars(mod)):
            if attr.startswith("_"):
                continue
            obj = getattr(mod, attr)
            origin = rel(source_of(obj) if inspect.isfunction(obj) or inspect.isclass(obj) else None)
            if inspect.isclass(obj) and (origin == source_file or not origin):
                methods: list[dict[str, str]] = []
                for mname, member in sorted(vars(obj).items()):
                    if mname.startswith("_") and mname not in ("__call__", "__aiter__", "__enter__", "__exit__"):
                        continue
                    if inspect.isfunction(member) or inspect.ismethod(member):
                        kind = "method"
                    elif isinstance(member, (property, classmethod, staticmethod)):
                        kind = type(member).__name__
                    else:
                        continue
                    target = member.fget if isinstance(member, property) else member
                    methods.append({
                        "name": mname,
                        "kind": kind,
                        "signature": sig(target),
                        "doc": first_line(target),
                    })
                entry["classes"].append({
                    "name": attr,
                    "defined_in": origin,
                    "bases": [b.__name__ for b in getattr(obj, "__bases__", ())],
                    "methods": methods,
                })
            elif inspect.isfunction(obj) and (origin == source_file or not origin):
                entry["functions"].append({
                    "name": attr,
                    "signature": sig(obj),
                    "doc": first_line(obj),
                })
        inventory[name] = entry

    (OUT_DIR / "api-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# src/ 全量公开 API 清单（introspection 穷举）",
        "",
        f"模块数={len(inventory)}，导入失败={len(failures)}。",
        "来源：`api_inventory.py`（AST + importlib，按定义模块去重，公开方法级）。",
        "",
    ]
    if failures:
        lines += ["## 导入失败（显式登记，不静默跳过）", ""]
        lines += [f"- `{f['module']}`: {f['error']}" for f in failures]
        lines += [""]
    for name, entry in inventory.items():
        if not entry["classes"] and not entry["functions"] and not entry["ast_all"]:
            continue
        lines += [f"## `{name}` — {entry['file']}", ""]
        if entry["import_error"]:
            lines += [f"- 导入失败：{entry['import_error']}", ""]
            continue
        if entry["ast_all"]:
            lines += [f"- `__all__` ({len(entry['ast_all'])}): " + ", ".join(f"`{x}`" for x in entry["ast_all"]), ""]
        for f in entry["functions"]:
            lines += [f"- 函数 `{f['name']}{f['signature']}` — {f['doc']}"]
        for c in entry["classes"]:
            lines += [f"- 类 `{c['name']}` (bases: {', '.join(c['bases']) or 'object'}) — {len(c['methods'])} 个公开方法"]
            for m in c["methods"]:
                lines += [f"  - `{m['name']}{m['signature']}` — {m['doc']}"]
        lines += [""]
    (OUT_DIR / "api-inventory.md").write_text("\n".join(lines), encoding="utf-8")

    classes = sum(len(e["classes"]) for e in inventory.values())
    methods = sum(len(c["methods"]) for e in inventory.values() for c in e["classes"])
    funcs = sum(len(e["functions"]) for e in inventory.values())
    print(json.dumps({
        "modules": len(inventory),
        "classes": classes,
        "methods": methods,
        "module_functions": funcs,
        "import_failures": failures,
        "json": rel(str(OUT_DIR / "api-inventory.json")),
        "markdown": rel(str(OUT_DIR / "api-inventory.md")),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
