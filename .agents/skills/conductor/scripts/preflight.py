#!/usr/bin/env python3
"""会话预检：检查外部 skill 与验证工具是否就位，缺失即给安装命令。

用途：conductor 的 S0 阶段运行。skill-matrix.md 声明的依赖若未安装，
流程会形同虚设——本脚本把"依赖是否真的可用"变成可执行检查。

退出码：0 = 全部就位；1 = 有缺失（缺失项已在输出中列出并附安装方式）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# 外部 skill：名称 → 仓库/安装提示（与 references/skill-matrix.md 对齐）
EXTERNAL_SKILLS: dict[str, str] = {
    "ponytail": "npx skills add DietrichGebert/ponytail --skill ponytail",
    "caveman": "npx skills add DietrichGebert/ponytail --skill caveman",
    "handoff": "npx skills add mattpocock/skills --skill handoff",
    "grill-design": "WorkBuddy 市场：grill-design（需求拷问）",
    "grill-me": "WorkBuddy 市场：grill-me（深度追问）",
    "code-quality-review": "WorkBuddy 市场：code-quality-review（七维审查）",
    "metago-code-review-deep": "WorkBuddy 市场：metago-code-review-deep（四级报告）",
}

# 语言 → 验证工具命令首词（与各 coding-rules-<lang>.md 的验证命令对齐）
LANG_TOOLS: dict[str, list[str]] = {
    "python": ["ruff", "mypy", "pydocstyle", "pytest"],
    "typescript": ["prettier", "eslint", "tsc", "vitest"],
    "react": ["prettier", "eslint", "tsc", "vitest"],
    "vue": ["prettier", "eslint", "vue-tsc", "vitest"],
    "go": ["gofmt", "go", "staticcheck"],
    "rust": ["cargo", "rustfmt"],
    "dart": ["dart", "flutter"],
}

SKILL_SEARCH_DIRS = (
    ".agents/skills", ".claude/skills", ".codex/skills", ".opencode/skill",
)


@dataclass(frozen=True)
class Missing:
    kind: str
    name: str
    hint: str


def skill_present(name: str, project_root: Path) -> bool:
    candidates = [project_root / d / name for d in SKILL_SEARCH_DIRS]
    home = Path.home()
    candidates += [home / ".workbuddy" / "skills" / name, home / ".claude" / "skills" / name]
    return any(c.is_dir() for c in candidates)


def tool_present(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def main() -> int:
    ap = argparse.ArgumentParser(description="conductor 会话预检")
    ap.add_argument("--project-root", default=".", help="项目根路径，默认当前目录")
    ap.add_argument("--langs", nargs="*", default=None,
                    help="只检查这些语言的工具；默认检查全部语言工具（仅报告，不强制全装）")
    ap.add_argument("--skip-skills", action="store_true", help="跳过外部 skill 检查")
    args = ap.parse_args()

    root = Path(args.project_root).resolve()
    missing: list[Missing] = []

    if not args.skip_skills:
        for name, hint in EXTERNAL_SKILLS.items():
            if not skill_present(name, root):
                missing.append(Missing("skill", name, hint))

    langs = args.langs if args.langs is not None else list(LANG_TOOLS)
    for lang in langs:
        for cmd in LANG_TOOLS.get(lang, []):
            if not tool_present(cmd):
                missing.append(Missing(f"tool:{lang}", cmd, f"安装 {cmd}（{lang} 验证所需）"))

    if not missing:
        print("[preflight] 全部就位。")
        return 0

    print(f"[preflight] 发现 {len(missing)} 项缺失：\n")
    for item in missing:
        print(f"  - [{item.kind}] {item.name}")
        print(f"      {item.hint}")
    print("\n提示：缺失项在 S4 自省时会导致验证命令无法执行（退出码非 0 即门禁不通过）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
