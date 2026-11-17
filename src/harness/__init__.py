"""AgentKit Harness：L1 能力插件层（R3~R9）。

全部基于 R1（runtime）+ R2（hooks）构建，内核零策略；能力以插件/Hook 形式挂载。
当前已实现：R3 可靠性（resilience）。子包按需引入，避免顶层 import 全部依赖。
"""
from __future__ import annotations
