"""RED：HashEmbedder 跨进程确定性（体检 r11 门禁偶发失败根因）。

_HashClient 文档宣称"确定性向量"，但用 Python 内置 hash()——受 PYTHONHASHSEED
每进程随机化影响：同一文本在不同进程嵌入出不同向量。后果：
1) 跨进程向量缓存/去重全不稳定；
2) dim=16 下两个不同中文句子（无空格整句单 token）有 1/16 概率碰撞到同一桶，
   向量完全相同 → 点积 1.0 ≥ 0.92 近重复阈值 → 写入被误合并（r11
   test_memory_capability_hook_red偶发失败实证）。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

EMBED_SNIPPET = (
    "import sys; sys.path.insert(0, r'{src}'); "
    "from harness.rag.embedder import HashEmbedder; "
    "import asyncio; "
    "v = asyncio.run(HashEmbedder().embed(['用户偏好：喜欢喝咖啡'])); "
    "print(repr(v[0]))"
)


def _embed_under_seed(seed: str) -> tuple[float, ...]:
    """在指定 PYTHONHASHSEED 的子进程里嵌入同一文本，返回向量。"""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    proc = subprocess.run(
        [sys.executable, "-c", EMBED_SNIPPET.format(src=str(SRC))],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return ast.literal_eval(proc.stdout.strip())


def test_red_hash_embedder_is_cross_process_deterministic() -> None:
    """同一文本在不同 PYTHONHASHSEED 进程中嵌入向量必须逐位相同（当前 FAIL）。"""
    v0 = _embed_under_seed("0")
    v1 = _embed_under_seed("1")
    v2 = _embed_under_seed("2")
    assert v0 == v1 == v2, (
        "HashEmbedder 跨进程不确定：同文本在不同 PYTHONHASHSEED 下向量不同"
        f"（seed0={v0[:4]}... seed1={v1[:4]}... seed2={v2[:4]}...）"
    )
