#!/usr/bin/env python3
"""把项目内的 conductor skill 同步到 WorkBuddy 全局 skills 目录。

背景：Windows 无符号链接权限，全局安装采用复制，两份独立。
本项目 .agents/skills/conductor 是唯一真相源；改完跑本脚本同步到 ~/.workbuddy/skills/。

用法：
    python scripts/sync-global.py              # 同步（覆盖全局副本）
    python scripts/sync-global.py --dry-run    # 只看要同步什么，不实际复制
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]          # .agents/skills/conductor
GLOBAL_ROOT = Path.home() / ".workbuddy" / "skills" / "conductor"


def rel_files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 conductor 到全局 skills 目录")
    ap.add_argument("--dry-run", action="store_true", help="只列出差异，不复制")
    args = ap.parse_args()

    if not SKILL_ROOT.is_dir():
        print(f"[sync] 源目录不存在：{SKILL_ROOT}", file=sys.stderr)
        return 1

    src_files = rel_files(SKILL_ROOT)
    to_copy: list[Path] = []
    for rel in src_files:
        src = SKILL_ROOT / rel
        dst = GLOBAL_ROOT / rel
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            to_copy.append(rel)

    # 全局副本里源已删除的文件（提示，不自动删，避免误删用户改动）
    stale: list[Path] = []
    if GLOBAL_ROOT.is_dir():
        for rel in rel_files(GLOBAL_ROOT):
            if not (SKILL_ROOT / rel).exists():
                stale.append(rel)

    print(f"[sync] 源：{SKILL_ROOT}")
    print(f"[sync] 目标：{GLOBAL_ROOT}")
    if not to_copy and not stale:
        print("[sync] 已是最新，无需同步。")
        return 0

    for rel in to_copy:
        print(f"  同步：{rel}")
    for rel in stale:
        print(f"  全局多余（源已删，需手动确认删除）：{rel}")

    if args.dry_run:
        print("[sync] dry-run，未实际复制。")
        return 0

    for rel in to_copy:
        src, dst = SKILL_ROOT / rel, GLOBAL_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f"[sync] 完成，同步 {len(to_copy)} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
