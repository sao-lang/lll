"""G5 设计稳定性门禁：锁定已实现模块的公共契约，防止设计漂移导致返工。

两种模式：
  check（默认，CI）：从当前代码提取 R1/R2 公共契约 → 与 contract_lock.json 比对；
                    任何增删改（含字段/方法/枚举值）即失败 → 必须走契约变更评审。
  update（人工，评审后）：重新生成基线并写入 contract_lock.json。

同时检查设计文档"契约锚点"：文档里对已实现模块的契约描述不得回退到旧值
（如 scratchpad 旧字段名、tests/m1 旧路径），防止改设计不改代码造成漂移。

用法：
  python tests/tools/contract_lock.py check        # CI 门禁
  python tests/tools/contract_lock.py update       # 评审后更新基线（需人工确认）
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
LOCK = ROOT / "tests" / "tools" / "contract_lock.json"

# 已实现模块 → 源码包。只有这里的公共契约被锁定。
# R3 已实现 → 纳入锁定（实现后必须 update 基线，之后改动走契约变更评审）。
LOCKED_PACKAGES = ("runtime", "hooks", "harness/resilience")

# 设计文档契约锚点：(文件, 必须出现, 禁止出现)。防止文档漂移回旧设计。
DOC_ANCHORS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("docs/02-模块详设/Agent/R1-核心运行时.md",
     ("scratch:", "tests/r1/", "EffectKind", "WAIT_HUMAN"),
     ("scratchpad:", "tests/m1/", "make demo-m1")),
    ("docs/02-模块详设/Agent/R2-插件总线与协议.md",
     ("HookMode", "DecisionAction", "OBSERVE", "ABORT", "tests/r2/"),
     ("tests/m2/", "tests/m3/", "matcher: Matcher")),
    ("docs/02-模块详设/Agent/R3-可靠性.md",
     ("FailureClass", "RetryPolicy", "CircuitBreaker", "DegradeLadder", "tests/r3/"),
     ("tests/m3/", "代码待实现")),
    ("docs/01-总览与设计/AGENT-总设计.md",
     ("10 内置架构", "R1~R13", "scratch"),
     ("11 内置架构", "R1~R11", "scratchpad:")),
)


def extract_contract() -> dict[str, dict[str, list[str]]]:
    """从 locked 包的源码 AST 提取公共契约：类 → 字段/方法/枚举值。"""
    contract: dict[str, dict[str, list[str]]] = {}
    for pkg in LOCKED_PACKAGES:
        for path in sorted((ROOT / "src" / pkg).glob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                    continue
                fields = [
                    n.target.id for n in node.body
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                ]
                methods = [
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not n.name.startswith("_")
                ]
                enum_values = [
                    n.targets[0].id for n in node.body
                    if isinstance(n, ast.Assign)
                    and isinstance(n.targets[0], ast.Name)
                    and not n.targets[0].id.startswith("_")
                ]
                key = f"{pkg}.{path.stem}.{node.name}"
                contract[key] = {
                    "fields": sorted(fields),
                    "methods": sorted(methods),
                    "enum_values": sorted(enum_values),
                }
    return contract


def digest(contract: dict) -> str:
    """契约快照的确定性哈希（sort_keys 保证稳定）。"""
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def check_doc_anchors() -> list[str]:
    """检查设计文档锚点，返回违规列表。"""
    problems: list[str] = []
    for rel, required, forbidden in DOC_ANCHORS:
        path = ROOT / rel
        if not path.exists():
            problems.append(f"文档缺失: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        for token in required:
            if token not in text:
                problems.append(f"{rel} 缺少契约锚点 {token!r}（设计漂移？）")
        for token in forbidden:
            if token in text:
                problems.append(f"{rel} 出现已废弃契约 {token!r}（设计回退？）")
    return problems


def cmd_check() -> int:
    """CI 门禁：比对代码契约与基线 + 文档锚点。"""
    if not LOCK.exists():
        print("[G5] FAIL 契约锁文件不存在，请先运行 update 生成基线")
        return 1

    current = extract_contract()
    baseline = json.loads(LOCK.read_text(encoding="utf-8"))
    locked = baseline["contract"]
    locked_hash = baseline["hash"]

    problems: list[str] = []
    # 1) 哈希比对（快速失败信号）
    if digest(current) != locked_hash:
        # 2) 逐项 diff，给出精确变更清单
        for key in sorted(set(locked) | set(current)):
            old = locked.get(key)
            new = current.get(key)
            if old is None:
                problems.append(f"新增公共契约 {key}（需评审是否纳入锁定）")
            elif new is None:
                problems.append(f"删除公共契约 {key}（破坏性变更！）")
            elif old != new:
                for kind in ("fields", "methods", "enum_values"):
                    if old[kind] != new[kind]:
                        problems.append(
                            f"{key}.{kind}: {old[kind]} → {new[kind]}"
                        )
    # 3) 文档锚点
    problems.extend(check_doc_anchors())

    if problems:
        print("[G5] FAIL 设计稳定性门禁未通过：")
        for p in problems:
            print(f"  - {p}")
        print("\n  公共契约变更必须走《设计稳定性门禁》评审流程：")
        print("  确认是兼容扩展 → 评审后 python tests/tools/contract_lock.py update")
        print("  确认是破坏性变更 → 走架构评审，禁止直接 update 绕过")
        return 1
    print(f"[G5] PASS 公共契约未变（{len(current)} 项符号），文档锚点一致，哈希 {locked_hash[:12]}…")
    return 0


def cmd_update() -> int:
    """评审后重新生成基线（人工操作，CI 不调用）。"""
    contract = extract_contract()
    payload = {
        "version": "1.0",
        "description": "R1/R2 已实现模块公共契约基线；变更须走设计稳定性门禁评审",
        "locked_packages": list(LOCKED_PACKAGES),
        "hash": digest(contract),
        "contract": contract,
    }
    LOCK.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[G5] 基线已更新：{len(contract)} 项公共契约，哈希 {payload['hash'][:12]}…")
    print("[G5] 提醒：请确保本次变更已通过架构评审，并在 commit message 标注 BREAKING/CONTRACT。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="G5 设计稳定性门禁（契约锁）")
    parser.add_argument("mode", nargs="?", default="check", choices=("check", "update"))
    args = parser.parse_args()
    return cmd_check() if args.mode == "check" else cmd_update()


if __name__ == "__main__":
    sys.exit(main())
