# Change: 8ef577c1-R12R13R11 / 2026-11-23 / confirmed

## Why
用户要求"继续做 R11/R12/R13 三个模块"，并提示 R12 疑似已做一部分、R13 文档名含"契约"需判定体裁。只读检测发现版图与字面不同：R12 仅约 60% 完成（算子核心落地但用户入口 Agent 装配器缺位、门禁口径错位导致窄口径假绿）；R13 实为与 R8/R9/R10 同模板的模块详设（横切规格文档），其契约大部分已物化，剩余缺口全属 R11/R12 装配层——若照字面新建独立模块会造重复协议的死代码，违反用户源码纯净铁律。用户拍板四项：R13 按规格文档处理 / R12 补齐全范围 / R11 全量实现 / 顺序 R13文档→R12→R11。

## Delta
- 替换 R13 详设定性：设计态快照 → 横切规格参考（已部分物化）；`- 删除 \`R13 详设:290\` 的"设计态（代码待实现）"标记（因大部分契约已落地而消失）`；`- 删除 \`R13 §4\` 中与源码分叉的 \`Capability.build/provides\` 签名（因 \`compose/registry.py\` 的 \`install(container,reg)+requires\` 为代码事实而消失）`。
- 新增 `src/compose/agent.py`（或 `src/api/` 内）：`Agent` 装配器（`arch`/`capability`/`rag`/`memory`/`telemetry`/`event_stream`/`build`），R12 用户唯一入口。
- 新增 7 个 static_check 错误码：E05（cond 目标非法）/E10（高危未配沙盒）/E11（缺 Port）/E12（预算冲突）/E13（join 未注册）/E14（child_spec 非法）/E16（共享键冲突）。
- 新增 `tests/r12/test_archs.py`、`test_static.py`、`test_edges.py`、`test_wiring.py`、`test_agent.py`、`test_archs_deep.py`（6 个测试文件，其中前 4 个为矩阵缺失文件）+ race 取消/budget 注入/三层嵌套边界用例。
- 替换 R12 门禁口径：`gate_check.py::MODULES["r12"]` 的 `doc` 从 R2 文档 → R12 详设；symbols 纳入 archs 符号；`--cov` 纳入 `archs` 并提 `coverage` 60→85；`ast_scan.py::TARGETS["r12"]` 纳入 `src/archs`。
- 新增 `Makefile` 的 `demo-r12:` 规则（当前只有 .PHONY）。
- 新增 `src/api/`（R11）：四组 API 投影（A1 集成契约/A2 架构组合/A3 应用开发/A4 组件开发）+ `profile` 预设 + `static_check` 装配 + 治理（killswitch/成本限额/灰度/feature flag）+ CLI（脚手架/评测/回放/门禁）；R9 三 Port 默认装配在 A3 装配层落地。
- 新增 `tests/r11/` + `tests/demos/demo_r11.py` + `gate_check/ast_scan/Makefile` 注册 `r11`。
- 同步文档：AGENT-总设计 §R11/R12/R13 + 附录 B.1.2/B.1.3、R11/R12/R13 详设状态、README 模块表、门禁体系。
- 删除：`- 删除 \`R12 门禁 doc 指向 R2 文档\` 的窄口径配置（因纳入 archs 全范围口径而消失）`；`- 删除 \`R11 §11 功能接线备注\` 中关于 Agent 装配器的"未实现"标注（因 \`Agent\` 落地而消失，转为真实接线）`。

## Invariants
- R1（src/runtime/）一行不改（D-7）；Program 是唯一运行期契约；预算治理不是算子。
- 依赖方向恒为 工具层 → 组合层 → R1；R13 横切不占独立层；R11 只做投影/装配/治理，不做具体能力实现。
- 端口签名以源码为准（src/compose/ports.py）；Capability 以 `compose/registry.py::Capability`（install+requires+name+version）为准，不改其签名去迁就过时文档。
- 源码纯净铁律：禁止死代码/打补丁/兼容层/测试桩；fake 仅在 tests/；R13 不新建 `src/harness/plugin`/`tests/r13`/`gate r13`（会造重复协议）。
- 失败测试先行（TDD RED→GREEN），证据只认命令+退出码；source_purity --strict + HEAD 基线 rc=0。
- 门禁口径扩大后必须真绿：R12 覆盖率 ≥85%（含 archs）、R11 ≥85%，不得降阈值造假绿。
- R9 三 Port 默认装配在 R11 的 A3 层落地（组合层跨模块装配），R5/R9 源码不改（工具层互不直连）；无注入仍 fail-closed。
- 文档与代码同等交付：改公共契约/接口/架构决策必须同步 Affected 清单文档，否则 Blocker。

## Affected
- 源码新增：`src/api/`（R11）、`src/compose/agent.py`（Agent 装配器，或归入 src/api 由实现时按依赖方向裁决）。
- 源码改动：`src/archs/recipes.py`（7 错误码）、`tests/tools/gate_check.py`、`tests/tools/ast_scan.py`、`Makefile`、`pyproject.toml`（coverage source 加 api）。
- 测试新增：`tests/r11/`、`tests/r12/{test_archs,test_static,test_edges,test_wiring}.py`、`tests/demos/demo_r11.py`。
- 设计文档：`docs/02-模块详设/Agent/R11-集成接入与治理.md`、`R12-内置架构与组合.md`（或 R2-算子与装配.md）、`R13-可插拔与协议契约.md`、`docs/01-总览与设计/AGENT-总设计.md`（§R11/R12/R13 + 附录 B.1.2/B.1.3）、`docs/03-流程与规范/门禁体系.md`、`docs/README.md`。
- 需 conductor 编排影响面核实：Agent 装配器接线触达全部 R1-R10 能力；gate 口径扩大会暴露存量覆盖不足；R11 治理可能影响全局配置语义。

## Acceptance
- AC-1 R13 详设定性修正（横切规格参考）：无"设计态（代码待实现）"残留；Capability 签名与 `compose/registry.py` 一致；剩余缺口指向 R12/R11；不新建 tests/r13/gate r13。
- AC-2 `pytest tests/r12 -q` 全绿且新增 4 矩阵文件；7 个错误码 E05/E10-E14/E16 各有断言。
- AC-3 `gate_check.py --module r12` 的 doc 指向 R12 详设、symbols 含 archs、`--cov` 含 archs 且覆盖率 ≥85%；`ast_scan.py --module r12` TARGETS 含 src/archs；`make demo-r12`（或等价直接命令）PASS。
- AC-4 `pytest tests/r11 -q` 全绿：四组 API 投影 + profile + static_check 装配 + 治理四件 + CLI 四命令 + R9 三 Port 默认装配，各有端到端用例。
- AC-5 `gate_check.py --module r11` / `ast_scan.py --module r11` 各 rc=0 且覆盖率 ≥85%（真绿）。
- AC-6 `pytest tests/ -q` 全量退出 0（无回归）；`mypy --strict src` rc=0。
- AC-7 source_purity check --strict + HEAD 基线 rc=0（新增污染 0）；R1 零改动（`git status src/runtime/` 空）。
- AC-8 独立 review Blocker=0；review_gate rc=0；commitment_ledger --strict rc=0（如有清单）。
- AC-9 文档同步核对：总设 §R11/R12/R13 + B.1.2 + R11/R12/R13 详设 + README + 门禁体系，0 死链、0 过时"待实现"标注。
- AC-10 Agent 装配器端到端：`Agent(arch).capability(...).build()` 产可跑 Session；换实现上层零改；缺 Port 装配期报错。

## Open
- 无（四项方向用户已全选推荐：R13 规格文档处理 / R12 全范围 / R11 全量 / 顺序 R13→R12→R11）。
- 被否决方案（防复活）：R13 建独立模块 tests/r13+gate r13（造重复协议死代码）；R12 只补关键路径（留 TODO 非全范围）；R11 先核心后治理（用户要全量）；先写代码后改文档（R13 歧义会带进规格）。
- 环境注意：本机 `make` 未安装，demonstrate/gate 验收用等价直接 Python 命令（Makefile 规则仍写入作为可移植入口），不降断言造假绿。
