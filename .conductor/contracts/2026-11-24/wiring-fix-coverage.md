# 需求覆盖矩阵 · 接线问题全量修复（2026-11-24）

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R0 先立好门禁，然后再修 | 门禁先红后绿，tests/wiring 为唯一验收 | FR0 | G-W1~W7 + R1 全绿；已先红 2 条再修 |
| R1 应该还是注入能力包 | 能力包一等装配单位；架构/门面收能力包，桥梁层解包策略 | FR1 | G-W4 Router(classifier=cap)、G-W5 Reflection(reflector=cap)、G-W7 自定义包 |
| R2 策略和工具注入能力包 | 能力包=组合根，持策略+工具；MCP transport/cheap模型/telemetry 等注入包内 | FR2 | A3/G-W1b、B1/B3 的 spy 注入断言 |
| R3 用户自己封装能力包 | provider 声明 op；install 注册→执行链 handler | FR3 | G-W7 先红后绿（已过） |
| R4 MCP tools 进内置 registry | 修 A3：transport→MCPManager(wired_reg)→connect()→tools 进执行链 | FR4 | G-W1b 门面级行为 |
| R5 三级渐进式披露 | 修 A4：before_model 相位触发 disclose()，不新增门面入口 | FR5 | G-W2 行为 |
| R6 记忆来源对话注入上下文 | 本来正确，只加保护门禁（G-W3），不改链 | FR6 | G-W3 绿 |
| R7 四级级联可替换 | 保持两级替换；能力包纯策略容器、桥梁解包（C1 已修） | FR7 | G-W4/R1 绿 + r8 部件替换测试 |
| R8 修所有问题 | A1/A2/A3/A4/B1/B2/B3 逐项修；A5 诚实边界登记 | FR8 | 台账每项 RED→GREEN |
| R9 按 conductor 执行 | plan→debug→brainstorm→tdd→git→review→memory；full 档覆盖矩阵 | FR9 | coverage_check rc=0 / commitment_ledger rc=0 / review_gate Blocker=0 |

FR 数 = 10（≥8 触发 full，已按 full 执行）。
