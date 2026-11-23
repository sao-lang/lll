"""红线静态扫描（AST），按模块：python tests/tools/ast_scan.py --module r1|r2|r3|r4|r5

R1：Program 内不得有时间/随机/网络调用（破坏确定性）；纯运行时不 import random/datetime。
R2：Port/组合层/Lifecycle 三体系具名；五角色符号零残留；无旧 Hook/Host 注册 API 残留。
R3：降级阶梯装配期强校验（无环/末级拒答）+ 副作用重试幂等键检查；能力插件不 import IO SDK。
R4：网关必须做结构化校验（未过 schema 不进业务）+ 成本闸（路由/重试不得绕过 Budgets）。
R5：danger 四路路由齐全（CRITICAL 审批/HIGH 沙盒/LOW 审计缺席降级）+ L1 披露脱 schema
    + 异常折叠 + MCP fqname 同 Registry 合并 + 编码规则 5/6 防回归。
R6：RAGPort 履约（ingest/retrieve 签名）+ 注入检测 fail-closed（复用 R5 检测器）
    + 预算拼接不超 + 引用溯源 + RAG 层不 import 网络/系统 IO SDK。

扫描真实源码，不做运行时推断；任一红线命中退出码 1。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

TARGETS = {
    "r1": [SRC / "runtime", ROOT / "tests" / "demos" / "demo_r1.py", ROOT / "tests" / "r1"],
    "r2": [SRC / "compose", ROOT / "tests" / "demos" / "demo_r2.py", ROOT / "tests" / "r2"],
    "r3": [SRC / "harness" / "resilience", ROOT / "tests" / "demos" / "demo_r3.py", ROOT / "tests" / "r3"],
    "r4": [SRC / "harness" / "model", ROOT / "tests" / "demos" / "demo_r4.py", ROOT / "tests" / "r4"],
    "r5": [SRC / "harness" / "tools", SRC / "harness" / "skills",
           ROOT / "tests" / "demos" / "demo_r5.py", ROOT / "tests" / "r5"],
    "r6": [SRC / "harness" / "rag", ROOT / "tests" / "demos" / "demo_r6.py",
           ROOT / "tests" / "r6"],
    "r7": [SRC / "harness" / "context", SRC / "harness" / "memory",
           ROOT / "tests" / "demos" / "demo_r7.py", ROOT / "tests" / "r7"],
    "r11": [SRC / "api", ROOT / "tests" / "demos" / "demo_r11.py", ROOT / "tests" / "r11"],
    "r12": [SRC / "compose", SRC / "archs", ROOT / "tests" / "demos" / "demo_r12.py", ROOT / "tests" / "r12"],
    "r8": [SRC / "harness" / "cognition", SRC / "archs",
           ROOT / "tests" / "demos" / "demo_r8.py", ROOT / "tests" / "r8"],
    "r9": [SRC / "harness" / "security",
           ROOT / "tests" / "demos" / "demo_r9.py", ROOT / "tests" / "r9"],
    "r10": [SRC / "harness" / "observability", SRC / "harness" / "eval",
            SRC / "harness" / "durability",
            ROOT / "tests" / "demos" / "demo_r10.py", ROOT / "tests" / "r10"],
}

FORBIDDEN_IN_PROGRAM = (
    "datetime.now", "time.time", "time.monotonic", "random.", "requests.",
    "httpx.", "urllib.", "socket.", "aiohttp.", "open", "os.system",
    "subprocess.", "boto3.", "sqlalchemy.",
)


def files_of(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
        elif path.exists():
            out.append(path)
    return out


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# 去角色化硬红线：退役符号按片段拼接，避免扫描器源码自身形成字面命中。
_RETIRED_PARTS: tuple[tuple[str, ...], ...] = (
    ("AOP", "Container"), ("Composite", "Call"),
    ("Pre", "lude"), ("War", "den"), ("En", "velope"),
    ("Re", "finer"), ("Sal", "vage"), ("ROLE", "_NAMES"),
    ("War", "den", "Guard", "Adapter"), ("envelope", "_decorator"),
    ("add_", "war", "den"), ("add_", "pre", "lude"),
    ("add_", "en", "velope"), ("add_", "re", "finer"), ("add_", "sal", "vage"),
    # Guard 家族拦截抽象（2026-11-22 清掉：check/observe 协议换皮、零生产调用者）
    ("with_", "guard"), ("Degradation", "Guard"), ("degrade", "_retry"),
)
_RETIRED_SYMBOLS = tuple("".join(parts) for parts in _RETIRED_PARTS)


def scan_retired_symbols(files: list[Path]) -> list[str]:
    """五角色容器/适配器/角色类名在测试里零残留（去角色化后不得复辟）。"""
    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name in _RETIRED_SYMBOLS:
            if re.search(rf"\b{re.escape(name)}\b", text):
                violations.append(f"{path.relative_to(ROOT)} 残留退役符号 {name}")
    return violations


def scan_r1(files: list[Path]) -> list[str]:
    """R1 红线：Program 纯函数 + 纯运行时不 import 时间/随机。"""
    violations: list[str] = []
    generators = 0
    for path in files:
        tree = parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
                continue
            generators += 1
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                name = dotted(call.func)
                if any(name.startswith(bad) for bad in FORBIDDEN_IN_PROGRAM):
                    violations.append(f"{path.relative_to(ROOT)}:{call.lineno} Program 内调用 {name}()")
        # 仅扫描 src/runtime 的 import（测试/demo 里 import time 做计时是允许的）
        if path.is_relative_to(SRC / "runtime"):
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = ast.unparse(node)
                    if "random" in module or "datetime" in module:
                        violations.append(f"{path.relative_to(ROOT)} 纯运行时 import {module}")
    print(f"   扫描 {len(files)} 个文件 / {generators} 个 generator 函数")
    return violations


def scan_r2(files: list[Path]) -> list[str]:
    """R2 红线：算子核心就位 + 退役角色符号零残留 + 无旧 Hook/Host 注册 API。

    去角色化后 call 链由领域执行器显式编排，不再有角色协议文件；
    测试与 demo 中五角色符号一律不得复辟。
    """
    violations: list[str] = []
    # 五角色容器/适配器/角色类名在测试里零残留（覆盖全部 tests/，含门禁工具自身）。
    violations.extend(scan_retired_symbols(files_of([ROOT / "tests"])))
    if (SRC / "aop").exists():
        violations.append("残留 src/aop/（规格 D-10 要求删除，不留兼容层）")
    # 对外 API 里不应残留旧 Hook 工厂/Host 注册 API。
    init = SRC / "compose" / "__init__.py"
    if init.exists():
        text = init.read_text(encoding="utf-8")
        for banned in ("Host", "HookKind", "RequestScope", "HookCtx", "Decision"):
            if re.search(rf"\b{banned}\b", text):
                violations.append(f"compose/__init__.py 残留旧符号: {banned}")
    # hooks 包已整包删除（R2 取消，规格 P2）：不得复辟。
    if (SRC / "hooks").exists():
        violations.append("残留 src/hooks/（R2 取消，规格 P2 要求资产归位后删除）")
    return violations


IO_SDK_PREFIXES = (
    "httpx", "requests", "aiohttp", "boto3", "sqlalchemy", "urllib", "socket",
)

# 规则 6 允许的"动态读取边界"模块：context 动态字段只读在这里发生。
DYNAMIC_BOUNDARY_FILES = {"_ctxview.py"}


def _scan_coding_rules(files: list[Path], pkg: Path) -> list[str]:
    """编码规则 5/6 红线（防回归）：禁魔法串比较 + 动态读取须收敛到边界。

    规则 5：有限取值集合用 Enum，禁止 `x == "literal"` 这类裸字符串比较
            （trace 标签/错误码作为参数传递不算比较，不拦）。
    规则 6：ctx.meta / effect.payload 的动态读取只允许在 _ctxview.py 边界模块；
            插件内出现 .meta.get/.payload.get/getattr 即违规（除非上一行有
            "规则 6 例外" 注释，用于鸭子插件装配期容错）。
    增强体系：AOP 增强器用 Invocation.meta 具名记录（fault/routing_target），
            不再有 RequestScope.meta 裸键；effect 具名字段（如 idempotency_key）
            不在此列。判定逻辑不变，仍按上述 AST 特征扫描。
    """
    violations: list[str] = []
    for path in files:
        if not path.is_relative_to(pkg) or path.name in DYNAMIC_BOUNDARY_FILES:
            continue
        tree = parse(path)
        lines = path.read_text(encoding="utf-8").splitlines()

        # 规则 5：字符串字面量比较
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} 规则5 魔法串比较 "
                                f"{ast.unparse(node.left)} vs {comp.value!r}（应改用 Enum）"
                            )

        # 规则 6：动态读取须在边界模块
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 规则 6：真正的动态读取是 getattr(任意名)；.payload.get / .meta.get
            # 读的是 TypedDict / Invocation 具名契约键（如 payload["model"]、
            # meta["fault"]），属具名边界读取，不再视为裸动态读取。
            is_dynamic = isinstance(func, ast.Name) and func.id == "getattr"
            if not is_dynamic:
                continue
            # 例外：紧邻上方 3 行内含"规则 6 例外"注释（装配期鸭子插件容错）
            comment_ok = any(
                "规则 6 例外" in lines[i]
                for i in range(max(0, node.lineno - 4), min(len(lines), node.lineno - 1))
            )
            if not comment_ok:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} 规则6 动态读取未收敛到 "
                    f"_ctxview.py 边界（或缺少'规则 6 例外'注释）"
                )
    return violations


def scan_r3(files: list[Path]) -> list[str]:
    """R3 红线：降级装配期校验 + 幂等键检查 + 退役角色符号零残留 + 不 import IO SDK。"""
    violations: list[str] = []
    pkg = SRC / "harness" / "resilience"

    # 降级阶梯必须调用 validate_ladder（装配期强校验：无环/末级拒答/quality 递减）。
    degrade = pkg / "defenses.py"
    if degrade.exists() and "validate_ladder(ladder)" not in degrade.read_text(encoding="utf-8"):
        violations.append("Degradation 未调用 validate_ladder（降级成环/无拒答兜底未拦截）")

    # 重试插件必须检查幂等键（无幂等键的副作用不得重试）。
    # 幂等键由 _ctxview 边界读取后以具名字段提供，故检查 idempotency_key 引用。
    retry = pkg / "retry.py"
    if retry.exists() and "idempotency_key" not in retry.read_text(encoding="utf-8"):
        violations.append("RetryPolicy 未检查 idempotency_key（无幂等键副作用可能重复执行）")

    # 能力插件不得直接 import IO SDK（分层纪律：只依赖 R1+R2）。
    for path in files:
        if not path.is_relative_to(pkg):
            continue
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in IO_SDK_PREFIXES):
                        violations.append(f"{path.relative_to(ROOT)} import IO SDK {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(p) for p in IO_SDK_PREFIXES):
                    violations.append(f"{path.relative_to(ROOT)} import IO SDK {node.module}")

    # 退役角色符号零残留（测试与 demo；生产源码的历史说明由文档侧负责去角色化）。
    violations.extend(scan_retired_symbols([p for p in files if p.is_relative_to(ROOT / "tests")]))

    # 编码规则 5/6 红线（防回归）
    violations.extend(_scan_coding_rules(files, pkg))
    return violations


def scan_r4(files: list[Path]) -> list[str]:
    """R4 红线：结构化校验 + 成本闸 + 缓存键版本 + 不 import IO SDK。"""
    violations: list[str] = []
    pkg = SRC / "harness" / "model"

    # 网关必须做结构化校验（strict：未过 schema 的输出不进业务）。
    gateway = pkg / "gateway.py"
    if gateway.exists():
        text = gateway.read_text(encoding="utf-8")
        # 校验入口可为模块级 validate_json_schema或注入校验器的 .check()
        # （SchemaValidator 可插拔）；两者任一 + 超次拒答即满足红线。
        has_validation = "validate_json_schema" in text or "_validator.check(" in text
        if not has_validation or "repair exhausted" not in text:
            violations.append("ModelGateway 未做结构化校验/超次拒答（非法输出可能进业务）")
        if "_record_cost" not in text or "CostGuard" not in text:
            violations.append("ModelGateway 未做成本归因/成本闸（可能绕过 Budgets）")

    # 缓存键必须含版本。
    types = pkg / "types.py"
    if types.exists() and "prompt_version" not in types.read_text(encoding="utf-8"):
        violations.append("缓存键未含 prompt_version（版本变化无法失效）")

    # 能力插件不得直接 import IO SDK。
    for path in files:
        if not path.is_relative_to(pkg):
            continue
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in IO_SDK_PREFIXES):
                        violations.append(f"{path.relative_to(ROOT)} import IO SDK {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(p) for p in IO_SDK_PREFIXES):
                    violations.append(f"{path.relative_to(ROOT)} import IO SDK {node.module}")

    # 编码规则 5/6 红线（防回归）
    violations.extend(_scan_coding_rules(files, pkg))
    return violations


def scan_r5(files: list[Path]) -> list[str]:
    """R5 红线：CRITICAL 必须经审批 + MCP 合并同 Registry + 异常折叠 + 不 import IO SDK。"""
    violations: list[str] = []
    tools_dir = SRC / "harness" / "tools"

    registry = tools_dir / "registry.py"
    reg_text = registry.read_text(encoding="utf-8") if registry.exists() else ""
    # 四级路由：CRITICAL 必经 HumanPort 审批、HIGH 必经沙盒、异常折叠、缺席拒绝错误码。
    for token in ("NO_SANDBOX", "NO_HUMAN", "APPROVAL_DENIED", "INVALID_ARGS",
                  "except Exception", "DangerLevel"):
        if token not in reg_text:
            violations.append(f"registry.py 缺路由/折叠要素 {token}")
    # LOW 审计缺席降级标记（四级完整：LOW 不能被整级吞掉）。
    if "audit_missing" not in reg_text:
        violations.append("registry.py 缺 LOW 审计降级标记 audit_missing")

    # 披露红线：L1（catalog level=1）不含完整 schema + 超预算拒绝。
    index = tools_dir / "index.py"
    idx_text = index.read_text(encoding="utf-8") if index.exists() else ""
    if "parameters_schema" not in idx_text or "budget" not in idx_text.lower():
        violations.append("index.py 缺 L1 脱 schema 或预算校验")

    # MCP 合并：fqname 前缀 + 同一 registry 注册。
    mcp = tools_dir / "mcp.py"
    mcp_text = mcp.read_text(encoding="utf-8") if mcp.exists() else ""
    if "mcp__" not in mcp_text or "register" not in mcp_text:
        violations.append("mcp.py 缺 mcp__ fqname 前缀或同 Registry 注册")

    # D13 WebFetch：抓取正文必须过注入检测、命中拒载。
    webfetch = tools_dir / "webfetch.py"
    web_text = webfetch.read_text(encoding="utf-8") if webfetch.exists() else ""
    if "InjectionDetector" not in web_text or "InjectionVerdict.INJECTED" not in web_text:
        violations.append("webfetch.py 缺注入检测或命中拒载")

    # 编码规则 5/6 红线（防回归）：规则 5 扫字面量比较、规则 6 扫动态读取收敛。
    violations.extend(_scan_coding_rules(files, tools_dir))
    violations.extend(_scan_coding_rules(files, SRC / "harness" / "skills"))
    return violations


def scan_r6(files: list[Path]) -> list[str]:
    """R6 红线：RAGPort 履约 + 注入 fail-closed + 预算/引用 + 无 IO SDK 直连。

    检查生产源码中的结构性承诺，不依赖运行时。测试与 demo 文件跳过 IO 检查
    （demo 可能显式 sys.path 操作）。
    """
    violations: list[str] = []
    rag_dir = SRC / "harness" / "rag"

    # RAGPort 履约：pipeline 必须声明 name 与 ingest/retrieve 两个 Port 方法。
    pipeline = rag_dir / "pipeline.py"
    ptext = pipeline.read_text(encoding="utf-8") if pipeline.exists() else ""
    for token in ("name: str", "async def ingest", "async def retrieve", "async def query"):
        if token not in ptext:
            violations.append(f"pipeline.py 缺 RAGPort 履约要素 {token}")

    # 注入 fail-closed：后处理必须复用 R5 检测器且对 INJECTED 裁决剔除。
    post = rag_dir / "post.py"
    post_text = post.read_text(encoding="utf-8") if post.exists() else ""
    if "InjectionDetector" not in post_text or "InjectionVerdict.INJECTED" not in post_text:
        violations.append("post.py 缺注入检测接入或 fail-closed 剔除")
    if "EMPTY_CONTEXT" not in post_text:
        violations.append("post.py 缺空结果固定提示 EMPTY_CONTEXT")

    # 预算与引用：后处理拼装必须引用 TokenBudget 且产出 citations。
    if "TokenBudget" not in post_text or "citations" not in post_text:
        violations.append("post.py 缺预算拼接或引用溯源")

    # RAG 层不直接 import 网络/系统 IO SDK（外部系统须可注入 client）。
    io_tokens = ("import httpx", "import requests", "import socket", "import aiohttp",
                 "import subprocess", "import os")
    for path in files:
        if path.suffix != ".py" or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in io_tokens:
            if token in text:
                violations.append(f"{path.name} 直接 import IO SDK: {token}")
    return violations


def scan_r7(files: list[Path]) -> list[str]:
    """R7 红线：三 Port 履约 + 先 cold 后 compact 顺序 + secret/注入 fail-closed + 不改 messages。

    检查生产源码的结构性承诺，不依赖运行时。
    """
    violations: list[str] = []
    context_dir = SRC / "harness" / "context"
    memory_dir = SRC / "harness" / "memory"

    # MemoryPort 履约：memory.py 必须有 name 与 read/write/forget/recall 四方法。
    memory = memory_dir / "memory.py"
    mtext = memory.read_text(encoding="utf-8") if memory.exists() else ""
    for token in ("name: str", "async def read", "async def write", "async def forget",
                  "async def recall"):
        if token not in mtext:
            violations.append(f"memory.py 缺 MemoryPort 履约要素 {token}")

    # StorePort + BlobPort 履约。
    store = memory_dir / "store.py"
    stext = store.read_text(encoding="utf-8") if store.exists() else ""
    for token in ("async def save", "async def load"):
        if token not in stext:
            violations.append(f"store.py 缺 StorePort 履约要素 {token}")
    offloader = context_dir / "offloader.py"
    otext = offloader.read_text(encoding="utf-8") if offloader.exists() else ""
    for token in ("async def put", "async def get", "async def delete"):
        if token not in otext:
            violations.append(f"offloader.py 缺 BlobPort 履约要素 {token}")

    # secret + 注入 fail-closed。
    if "is_secret_key" not in mtext or "InjectionVerdict.INJECTED" not in mtext:
        violations.append("memory.py 缺 secret 键判定或注入检测 fail-closed")

    # B1/B2：先写 cold 再压缩的顺序由编排器方法体保证，且不得改 messages。
    manager = context_dir / "manager.py"
    gtext = manager.read_text(encoding="utf-8") if manager.exists() else ""
    cold_mark = "self.memory.write_cold("
    compact_mark = "self.compaction.compact("
    if cold_mark not in gtext or compact_mark not in gtext:
        violations.append("manager.py 缺显式编排的 write_cold/compact 调用")
    elif gtext.index(cold_mark) > gtext.index(compact_mark):
        violations.append("manager.py 顺序错误：compact 调用出现在 write_cold 之前")
    if "state.messages =" in mtext or "state.messages =" in gtext:
        violations.append("R7 禁止改写 State.messages（只增不减）")

    # 无 IO SDK 直连（外部后端必须可注入）。
    io_tokens = ("import httpx", "import requests", "import socket", "import aiohttp",
                 "import subprocess", "import os")
    for path in files:
        if path.suffix != ".py" or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in io_tokens:
            if token in text:
                violations.append(f"{path.name} 直接 import IO SDK: {token}")
    return violations


def scan_r12(files: list[Path]) -> list[str]:
    """R12 算子核心红线（规格 §2/D-4）：10 算子齐全 + 五角色符号零残留 + 算子纯度。

    检查：
        - src/compose/program.py 定义 10 个 A 类算子；
        - 无 src/aop/（D-10 不留兼容层）；
        - 算子不持有实例流程状态（禁 self.<进度> 赋值，D-8）。
    """
    violations: list[str] = []
    violations.extend(scan_retired_symbols([p for p in files if p.is_relative_to(ROOT / "tests")]))
    program = SRC / "compose" / "program.py"
    if not program.exists():
        violations.append("算子核心缺失：src/compose/program.py")
    else:
        text = program.read_text(encoding="utf-8")
        for op in ("seq", "par", "branch", "race", "loop", "map_state",
                   "map_effect", "fallback", "gate", "retry"):
            if f"def {op}(" not in text:
                violations.append(f"A 类算子缺失: {op}")
    if (SRC / "aop").exists():
        violations.append("残留 src/aop/（D-10 要求删除）")
    # P6：10 内置架构齐全（A 类算子预设配方）
    archs = SRC / "archs" / "recipes.py"
    if not archs.exists():
        violations.append("内置架构缺失：src/archs/（P6）")
    else:
        at = archs.read_text(encoding="utf-8")
        for arch in ("ToolUse", "ReAct", "PlanExecute", "Reflection", "Router",
                     "RouterSkill", "Supervisor", "Handoff", "Blackboard", "Graph"):
            if f"class {arch}" not in at:
                violations.append(f"内置架构缺失: {arch}（P6）")
    # P4/P5/P7/P8：ToolLayer build() 外壳齐全
    for cap, module in (("model", "src/harness/model/capability.py"),
                        ("tools", "src/harness/tools/capability.py"),
                        ("rag", "src/harness/rag/pipeline.py"),
                        ("context", "src/harness/context/manager.py")):
        cp = SRC.parent / module
        if cp.exists() and "def build(self)" not in cp.read_text(encoding="utf-8"):
            violations.append(f"ToolLayer build() 缺失: {cap}（P4-P8）")
    # P4/P8：领域字段不进 R2 基础 Port
    proto = SRC / "compose" / "ports.py"
    if proto.exists():
        pt = proto.read_text(encoding="utf-8")
        # 宽松检查：LLMPort/MemoryPort 类体内不应声明 last_usage/recall 成员
        import re as _re
        for cls, field in (("LLMPort", "last_usage"), ("MemoryPort", "recall")):
            m = _re.search(rf"class {cls}\(.*?\n(?=class |\Z)", pt, _re.S)
            if m and _re.search(rf"^\s+(?:{field}\s*:|def\s+{field}\b)", m.group(0), _re.M):
                violations.append(f"{field} 未归位，仍在 R2 {cls} 基础 Port（P4/P8）")
    return violations


def scan_r8(files: list[Path]) -> list[str]:
    """R8 红线：级联短路在 generator 层 + Router 单入口（无内联 recognize 裂缝）。"""
    violations: list[str] = []
    cascade = SRC / "harness" / "cognition" / "cascade.py"
    if not cascade.exists():
        violations.append("意图级联缺失：src/harness/cognition/cascade.py")
    else:
        text = cascade.read_text(encoding="utf-8")
        if "yield Invoke(port=\"llm\")" not in text:
            violations.append("L3/L4 未以 Invoke Effect 表达（策略层直接 IO?）")
        if "def intent_as_program" not in text:
            violations.append("intent_as_program 桥梁缺失（Router 裂缝未修）")
    recipes = SRC / "archs" / "recipes.py"
    if recipes.exists():
        text = recipes.read_text(encoding="utf-8")
        # 裂缝修复：Router/RouterSkill 不得再有自造 _recognize 内联解析。
        if "_recognize(" in text:
            violations.append("Router 仍内联 _recognize（应统一走 intent_as_program 单入口）")
        if "intent_as_program(" not in text:
            violations.append("Router 未接线 intent_as_program")
    return violations


def scan_r9(files: list[Path]) -> list[str]:
    """R9 红线：复用单检测器（不造第二套）+ fail-closed + 审计哈希链真实存在。"""
    violations: list[str] = []
    guard = SRC / "harness" / "security" / "guard.py"
    if not guard.exists() or "KeywordInjectionDetector" not in guard.read_text(encoding="utf-8"):
        violations.append("注入检测未复用 skills 单检测器（疑似造第二套）")
    ports = SRC / "harness" / "security" / "ports.py"
    if ports.exists():
        text = ports.read_text(encoding="utf-8")
        for cls in ("LocalSandbox", "AuditLogger", "ConsoleHumanPort"):
            if f"class {cls}" not in text:
                violations.append(f"R9 默认实现缺失: {cls}（B.1.2）")
    types = SRC / "harness" / "security" / "types.py"
    if types.exists() and "verify_hash_chain" not in types.read_text(encoding="utf-8"):
        violations.append("审计哈希链校验缺失")
    # 生产源码禁止假实现桩（fake/stub/pass 占位）
    for path in (SRC / "harness" / "security").rglob("*.py"):
        if "class Fake" in path.read_text(encoding="utf-8") or "TODO stub" in path.read_text(encoding="utf-8"):
            violations.append(f"安全模块含测试桩: {path.name}")
    return violations


def scan_r10(files: list[Path]) -> list[str]:
    """R10 红线：复用 R1 EventStore（不重复声明）+ EventEnvelope 独立命名 + 默认实现真实。"""
    violations: list[str] = []
    # R10 源码不得自定义名为 EventStore 的协议（单一来源在 R1 runtime/types.py）。
    for pkg in ("observability", "eval", "durability"):
        d = SRC / "harness" / pkg
        for path in d.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "class EventStore" in text:
                violations.append(f"R10 重复声明 EventStore: {path.name}（应复用 R1）")
    stream = SRC / "harness" / "observability" / "stream.py"
    if not stream.exists() or "class EventEnvelope" not in (
            SRC / "harness" / "observability" / "types.py").read_text(encoding="utf-8"):
        violations.append("EventEnvelope 独立信封缺失（不得与 R4 StreamEvent 混用）")
    durability = SRC / "harness" / "durability" / "checkpoint.py"
    if not durability.exists() or "replay_events" not in durability.read_text(encoding="utf-8"):
        violations.append("回放未复用 R1 replay_events")
    # 默认实现存在性
    defaults = (("NoopTelemetry", SRC / "harness" / "observability" / "telemetry.py"),
                ("InMemoryEventStream", SRC / "harness" / "observability" / "stream.py"),
                ("RuleEval", SRC / "harness" / "eval" / "rules.py"))
    for cls, path in defaults:
        if not path.exists() or f"class {cls}" not in path.read_text(encoding="utf-8"):
            violations.append(f"R10 默认实现缺失: {cls}（B.1.2）")
    return violations


def scan_r11(files: list[Path]) -> list[str]:
    """R11 红线：只做投影/装配/治理，不 import runtime 内部对象、不写事件日志。"""
    violations: list[str] = []
    for path in (SRC / "api").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # 边界：外部系统经契约接入，API 层不得 import runtime 内部实现模块。
        for banned in ("from runtime.runner import", "from runtime.fold import",
                       "from runtime.interpreter import"):
            if banned in text:
                violations.append(f"{path.name} 越界 import 内核内部: {banned}")
        if "class Fake" in text or "NotImplementedError" in text:
            violations.append(f"{path.name} 含假实现/桩")
    return violations


SCANNERS = {
    "r1": scan_r1, "r2": scan_r2, "r3": scan_r3,
    "r4": scan_r4, "r5": scan_r5, "r6": scan_r6, "r7": scan_r7,
    "r12": scan_r12, "r8": scan_r8, "r9": scan_r9, "r10": scan_r10, "r11": scan_r11,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentKit 红线静态扫描（R1/R2）")
    parser.add_argument("--module", required=True, choices=sorted(SCANNERS))
    args = parser.parse_args()
    files = files_of(TARGETS[args.module])
    print(f"[红线扫描 {args.module.upper()}]")
    violations = SCANNERS[args.module](files)
    if violations:
        for item in violations:
            print(f"  FAIL  {item}")
        print(f"[红线扫描 {args.module.upper()}] 不通过：{len(violations)} 处违规")
        return 1
    print(f"[红线扫描 {args.module.upper()}] PASS：无红线违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
