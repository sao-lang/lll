#!/usr/bin/env python3
"""规则验证：按语言执行 coding-rules-<lang>.md 中的验证命令，汇总退出码。

用途：conductor 的 S4 自省阶段。把"AI 声称遵守规则"变成"工具退出码为证据"。
输出可直接贴进 overview 的"验证结果"一节。

退出码：0 = 全部命令通过；1 = 有失败或命令缺失。
"""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REFS = Path(__file__).resolve().parents[1] / "references"
LANGS = ("python", "typescript", "react", "vue", "go", "rust", "dart")


@dataclass(frozen=True)
class Result:
    cmd: str
    code: int
    ok: bool
    note: str = ""


def extract_commands(md: Path) -> list[str]:
    """从规则文件的 ```bash 代码块提取命令（忽略注释与空行）。"""
    if not md.is_file():
        return []
    text = md.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\s*\n(.*?)```", text, re.S)
    cmds: list[str] = []
    for block in blocks:
        for line in block.splitlines():
            line = line.split("#", 1)[0].strip()  # 去行尾注释
            if line:
                cmds.append(line)
    return cmds


def run_one(cmd: str, cwd: Path, timeout: int) -> Result:
    first = shlex.split(cmd)[0] if cmd else ""
    if not shutil.which(first):
        return Result(cmd, -1, False, "命令不存在（工具未安装）")
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        note = ""
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            note = tail[-1][:120] if tail else ""
        return Result(cmd, proc.returncode, proc.returncode == 0, note)
    except subprocess.TimeoutExpired:
        return Result(cmd, -2, False, f"超时（>{timeout}s）")


def main() -> int:
    ap = argparse.ArgumentParser(description="conductor 规则验证汇总")
    ap.add_argument("--lang", required=True, choices=LANGS, help="要验证的语言")
    ap.add_argument("--cwd", default=".", help="命令执行目录（项目根）")
    ap.add_argument("--timeout", type=int, default=300, help="单命令超时秒数")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    args = ap.parse_args()

    md = REFS / f"coding-rules-{args.lang}.md"
    cmds = extract_commands(md)
    if not cmds:
        print(f"[verify] 未从 {md.name} 提取到验证命令。", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n".join(cmds))
        return 0

    cwd = Path(args.cwd).resolve()
    print(f"# 验证结果 · {args.lang}（{cwd}）\n")
    results = [run_one(c, cwd, args.timeout) for c in cmds]
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"- [{mark}] exit={r.code} `{r.cmd}`" + (f" — {r.note}" if r.note else ""))
    passed = sum(1 for r in results if r.ok)
    print(f"\n小结：{passed}/{len(results)} 通过。")
    print("> 退出码为客观证据；非 0 项禁止进 review、禁止标完成。")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
