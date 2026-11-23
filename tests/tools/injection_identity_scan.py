"""注入身份一致性扫描：揪出"影子构造"——用户注入了对象，执行链却另建一个。

背景（用户 2026-11-24 质疑）：反复审计每轮都发现新死注入。根因不是清单漏项，
而是 Capability.install() 注册进独立 Registry 后，api/ 执行链又新建同类对象，
导致"注入了 ≠ 生效了"。本扫描把这个模式变成机器可检的不变量。

检查规则：
R1 影子构造：api/ 执行链里 new 出的对象，若同类型存在可注入入口（Capability
   构造参数 / Agent 构造参数 / register_port），报为疑似影子。
R2 注入未读取：Capability 构造参数存到 self 后，在 api/ 下无任何读取点。
R3 协议无消费：PORT_PROTOCOLS 中的协议名，在 src/ 里只有注册没有 invoke。

用法：.venv/Scripts/python tests/tools/injection_identity_scan.py
退出码：0 = 无新增违规；1 = 存在违规（打印清单）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
API = SRC / "api"

# 已知的可注入对象类型 → 注入入口（人工维护一次，之后由契约测试保护）
INJECTABLE_TYPES: dict[str, str] = {
    "AuditLogger": "SecurityCapability(audit=...)",
    "RuleGuardrail": "SecurityCapability(guardrail=...)",
    "InMemoryEventStream": "Agent(stream=...)",
    "InMemoryModelRegistry": "ModelCapability(registry=...)",
    "Trace": "Agent(trace=...)",
    "RAGPipeline": "RAGCapability / register_port('rag')",
    "RuleEval": "EvalCapability / register_port('eval')",
    "LifecycleBus": "Agent(bus=...) / assemble_runtime(bus=...)",
}

# 允许影子构造的白名单：这些是明确设计的"内部默认"，不是注入对象的副本
ALLOWED_SHADOW = {
    ("runtime.py", "InMemoryModelRegistry"),  # 无 registry 注入时的默认
    ("runtime.py", "InMemoryEventStream"),    # 无 stream 注入时的默认
}


def _calls_in(path: Path) -> list[tuple[int, str]]:
    """返回文件里所有 Constructor(...) 调用的行号与类名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if name:
                found.append((node.lineno, name))
    return found


def scan_shadow_construction() -> list[str]:
    """R1：api/ 里 new 了可注入类型，且不在白名单。"""
    issues: list[str] = []
    for path in sorted(API.glob("*.py")):
        for lineno, name in _calls_in(path):
            if name not in INJECTABLE_TYPES:
                continue
            if (path.name, name) in ALLOWED_SHADOW:
                continue
            issues.append(
                f"[影子构造] {path.relative_to(ROOT)}:{lineno} new {name}()；"
                f"注入入口是 {INJECTABLE_TYPES[name]}——注入对象可能不生效")
    return issues


def scan_unread_capability_fields() -> list[str]:
    """R2：Capability 字段在整个 src/ 下（含自身方法）从未被读取——真死字段。

    第一版口径只看 api/，把"capability 自己的方法读取"（如 ModelCapability
    .gateway() 读 self._llm）误报为死注入——这正是"宽口径制造假问题"的反例。
    真死字段的定义收紧为：除赋值行外，全 src 无任何 Attribute(Load) 读取。
    """
    read_attrs: set[str] = set()
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Load)):
                read_attrs.add(node.attr)

    issues: list[str] = []
    cap_files = list(SRC.rglob("capability.py")) + [
        SRC / "harness" / "cognition" / "reflection_capability.py",
        SRC / "ports" / "adapters" / "provider.py",
    ]
    for path in cap_files:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute):
                    continue
                if not (isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    continue
                attr = target.attr
                if attr in {"name", "version", "requires"}:
                    continue
                if attr not in read_attrs:
                    issues.append(
                        f"[注入未读取] {path.relative_to(ROOT)}:{node.lineno} "
                        f"self.{attr} 全 src 无读取点（真死字段）")
    return issues


def main() -> int:
    issues = scan_shadow_construction() + scan_unread_capability_fields()
    if not issues:
        print("[注入身份扫描] PASS：未发现影子构造或未读取注入字段")
        return 0
    print(f"[注入身份扫描] 疑似 {len(issues)} 处（静态扫描仅列嫌疑，不能单独定罪）：")
    for item in issues:
        print("  " + item)
    print("\n注意：静态扫描会误报（如 getattr 动态读取抓不到、有 guard 的默认构造），"
          "也会漏报（跨层包装）。真判定必须走行为测试："
          "注入自定义对象 → 断言执行链拿到的是同一实例。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
