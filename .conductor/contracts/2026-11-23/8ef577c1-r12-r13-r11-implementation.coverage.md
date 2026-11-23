# 需求覆盖矩阵：R11/R12/R13 全量实现（大需求门禁）

> 需求列来自用户原话 "先提交代码，然后按照要求继续做R11 R12 R13三个模块，R12好像已经做完一部分了，你得检测下，R13文档名称说是契约，你得看下是不是需求文档" + 四项拍板（全选推荐）。
> 收口前做一次模式切换红队复审（假设规格错/漏，逐维找洞），记在末尾。

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| 用户：先提交代码 | 采用 commit 86b6257（57 文件，工作树干净；amend 修 shell 引号 @ 前缀） | FR-0 | AC-0 overview 提交留证 |
| 用户：继续做 R11/R12/R13 | 采用 场景4 开发 full，器官链 plan→brainstorm→impact→tdd→git→review→memory | FR-0 | AC-0 route card full |
| 用户：R12 做完一部分要检测 | 采用 只读检测出 60% 完成度报告（57 passed 但窄口径门禁） | FR-1 | AC-2/AC-3 |
| 用户：R13 文档名含契约判断是否需求文档 | 采用 判定为模块详设/横切规格参考（同 R8/R9/R10 模板） | FR-2 | AC-1 |
| 拍板：R13 按规格文档处理 | 采用 只同步文档，不建 plugin/tests/r13/gate r13 | FR-2 | AC-1 |
| 拍板：R12 补齐全范围 | 采用 Agent 装配器+7错误码+4测试文件+门禁口径+85%覆盖 | FR-3 | AC-2/AC-3 |
| 拍板：R11 全量实现 | 采用 四组API+profile+治理+CLI+tests/r11+gate r11 | FR-4 | AC-4/AC-5 |
| 拍板：顺序 R13→R12→R11 | 采用 文档解除歧义→R12 地基→R11 上层 | FR-5 | AC-1→AC-2→AC-4 |
| R12-T01 架构组合 compile | 采用 复用 10 现有架构 + 补语义测试 | FR-3 | AC-2 |
| R12-T02 嵌套 Router→Graph→ReAct | 采用 补三层端到端用例 | FR-3 | AC-2 |
| R12-T03 流式 + 分布式 trace | 采用 R10 Trace 接线 + 用例 | FR-3 | AC-2 |
| R12-T04 插件化注入 | 采用 R13 四要素 + 用例 | FR-3 | AC-2 |
| R12-T05 装配期检查 | 采用 7 错误码 E05/E10-E14/E16 + test_static | FR-3 | AC-2 |
| R12-T06 并发边界 | 采用 race 取消 + map_effect budget + test_edges | FR-3 | AC-2 |
| R12-T07 合规审计 | 采用 R9 AuditLogger 接线 + 用例 | FR-3 | AC-2 |
| R12-T08 算子装配 | 采用 复用现有 18 用例 | FR-3 | AC-2 |
| R12-T09 静态检查 | 采用 新建 test_static.py | FR-3 | AC-2 |
| R12-T10 边界行为 | 采用 新建 test_edges.py（10 条 reason） | FR-3 | AC-2 |
| R12-T11 四坑接线 | 采用 新建 test_wiring.py（含 race/retry/budget） | FR-3 | AC-2 |
| R12-D01 demo | 采用 补 make demo-r12 规则 + 直接命令验收 | FR-3 | AC-3 |
| R12-G1 覆盖 ≥85% | 采用 gate_check --cov 纳入 archs，真绿 | FR-3 | AC-3 |
| R11 A1 集成契约 API | 采用 src/api/ 投影 + tests | FR-4 | AC-4 |
| R11 A2 架构组合 API | 采用 Agent 装配器投影 | FR-4 | AC-4 |
| R11 A3 应用开发 API | 采用 R9 三 Port 默认装配落地（组合层，不改 R5/R9） | FR-4 | AC-4 |
| R11 A4 组件开发 API | 采用 四要素注入投影 | FR-4 | AC-4 |
| R11 profile 预设 | 采用 配置版本+模板 | FR-4 | AC-4 |
| R11 治理 killswitch/限额/灰度/flag | 采用 治理组件 + 用例 | FR-4 | AC-4 |
| R11 CLI 脚手架/评测/回放/门禁 | 采用 CLI 四命令 + 用例 | FR-4 | AC-4 |
| R11 事件流/Projection 外接 | 采用 R10 stream/projection 经契约接入，不 import 内核内部 | FR-4 | AC-4 |
| R11-gate 注册 | 采用 gate_check/ast_scan/Makefile/pyproject 加 r11 | FR-4 | AC-5 |
| 门禁体系可扩展 | 采用 R11/R12 门禁 key 登记 + README 表 | FR-6 | AC-5/AC-9 |
| 全量 pytest/mypy 无回归 | 采用 TDD RED→GREEN + 全量验证 | FR-7 | AC-6 |
| 源码纯净 | 采用 source_purity --strict + HEAD 基线 + 无兼容层 | FR-7 | AC-7 |
| 独立 review | 采用 全新上下文子代理 + review_gate | FR-7 | AC-8 |
| 文档同步 | 采用 Affected 清单逐条勾销 | FR-6 | AC-9 |
| Agent 端到端 | 采用 capability().build() 产 Session + 换实现零改 | FR-3 | AC-10 |
| OTel/OTLP 真导出 | 范围外 本次只做内存/录制实现，属后续独立项 | — | — |
| 真 LLM judge | 否决 用户此前选确定性默认，不调真实 LLM | — | — |
| R13 独立模块 | 否决 用户拍板按规格文档处理 | — | — |
| make 命令本体 | 范围外 本机无 make，用等价直接 Python 命令验收，Makefile 规则仍写（见 contract Open 环境注意） | — | — |

## 模式切换红队复审（假设规格错/漏）
- 维度1 门禁假绿：R12 旧门禁窄口径（doc 指 R2、无 archs symbols、只 cov compose）→ 已列 AC-3 强制扩口径真绿。
- 维度2 R13 体裁误判：若当独立模块建会造重复协议死代码 → 已列"否决"行 + AC-1 禁建。
- 维度3 接线副作用：Agent 装配器触达全模块，R5/R9 源码禁止改（工具层互不直连）→ Invariants 固定，AC-4/AC-6 回归。
- 维度4 未完成模块依赖：R11 依赖 R12 Agent，顺序已拍板 R12→R11 → FR-5 顺序门。
- 维度5 环境造假绿：本机无 make，禁止降断言 → 显式登记"环境缺失"用直接命令，Makefile 仍写。
- 维度6 覆盖率造假：85% 必须含 archs/api，不得只 cov 轻包 → AC-3/AC-5 显式口径。
- 结论：六维均有对应 FR/验收或显式否决/范围外/环境登记，无裸漏项。
