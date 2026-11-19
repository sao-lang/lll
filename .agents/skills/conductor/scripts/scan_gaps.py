#!/usr/bin/env python3
"""缺口扫描：把 R5-GAP-LIST 那套人工审计半自动化，生成缺口台账草稿。

扫描目标（启发式，输出需人工逐条核实，不替代判断）：
  1. 未完成标记：TODO / FIXME / XXX / HACK / pass / NotImplementedError
  2. 测试替身出现在生产代码：Fake* / Stub* / Mock* 类（tests/ 下跳过）
  3. 协议/门面无默认实现：Protocol 类无同名 *Impl/默认实现（提示，弱信号）
  4. install() 有定义但无调用者：装配层断点（提示，弱信号）

退出码：恒为 0（草稿供核对，不直接判失败）；用 --fail-on-hits 可改为有命中即 1。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MARKERS = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b|NotImplementedError|^\s*pass\s*$", re.M)
FAKE_CLASS = re.compile(r"^class\s+(Fake|Stub|Mock)\w*", re.M)
PROTOCOL = re.compile(r"^class\s+(\w+)\s*\([^)]*\bProtocol\b[^)]*\)", re.M)
INSTALL_DEF = re.compile(r"def\s+install\s*\(")

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "tests", "tmp", ".agents", "overviews"}


@dataclass(frozen=True)
class Hit:
    category: str
    file: str
    line: int
    text: str


def iter_source(root: Path, exts: set[str]) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in exts:
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in p.parts):
            continue
        files.append(p)
    return files


def scan(root: Path, exts: set[str]) -> list[Hit]:
    hits: list[Hit] = []
    all_texts: dict[Path, str] = {}
    for f in iter_source(root, exts):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        all_texts[f] = text
        rel = str(f.relative_to(root))
        for m in MARKERS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            snippet = text[m.start():].splitlines()[0].strip()[:80]
            hits.append(Hit("未完成标记", rel, line, snippet))
        for m in FAKE_CLASS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(Hit("生产代码含测试替身", rel, line, m.group(0)))

    # Protocol 无同名实现（弱信号）
    protocols: set[str] = set()
    for f, text in all_texts.items():
        for m in PROTOCOL.finditer(text):
            protocols.add(m.group(1))
    all_text = "\n".join(all_texts.values())
    for name in sorted(protocols):
        if not re.search(rf"class\s+{re.escape(name)}(Impl|Default|Manager|Registry|Provider)\b", all_text):
            hits.append(Hit("协议无默认实现（需核实）", "(全仓)", 0, f"{name}"))

    # install() 定义但无外部调用（弱信号）
    install_defs = sum(len(INSTALL_DEF.findall(t)) for t in all_texts.values())
    install_calls = len(re.findall(r"\.install\s*\(", all_text))
    if install_defs > 0 and install_calls == 0:
        hits.append(Hit("install 无调用者（装配断点）", "(全仓)", 0, f"{install_defs} 个定义，0 个调用"))

    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="dev-workflow 缺口扫描（草稿）")
    ap.add_argument("--path", default=".", help="源码根路径")
    ap.add_argument("--exts", default=".py,.ts,.tsx,.vue,.go,.rs,.dart",
                    help="扫描扩展名，逗号分隔")
    ap.add_argument("--fail-on-hits", action="store_true", help="有命中即返回 1")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    exts = {e.strip() for e in args.exts.split(",") if e.strip()}
    hits = scan(root, exts)

    print(f"# 缺口台账草稿（{root}）\n")
    if not hits:
        print("无启发式命中。仍需人工核对设计承诺与跨模块接线。")
        return 0
    print(f"共 {len(hits)} 条命中，按文件:行号核对：\n")
    by_cat: dict[str, list[Hit]] = {}
    for h in hits:
        by_cat.setdefault(h.category, []).append(h)
    for cat, items in by_cat.items():
        print(f"## {cat}（{len(items)}）")
        for h in items:
            loc = f"{h.file}:{h.line}" if h.line else h.file
            print(f"- `{loc}` — {h.text}")
        print()
    print("> 草稿仅供核对：fake 不算实现、门面不算实现、拿不出行号证据不得标完成。")
    return 1 if (args.fail_on_hits and hits) else 0


if __name__ == "__main__":
    sys.exit(main())
