# WidgetPilot · AI 手机控件生成 Agent（产品需求与技术设计方案）

> **文档类型**：PRD + 技术方案（用于设计评审、开发交付与工程复盘）  
> |  |  |  
> | --- | --- |  
> **版本**：v1.1（技术栈修订）　|　**最后更新**：2026-11-12  
> **产品定义**：用户用自然语言描述想要的手机桌面控件并选择规格与附件，系统生成符合规格、可隔离渲染、可管理的 TSX + React 控件代码，产出带完整证据链的控件包。  
> **适用范围**：产品需求、系统架构、前端交互设计、后端工程化与 harness 控制链路、接口契约、评测与运营。  
> **业务背景**：复现作者曾参与过的「描述生成手机控件」业务（Lynx + React 控件打包嵌入手机系统）。因环境与设备限制，本项目不生成 Lynx 代码、不打包、不装机；产物为可在浏览器隔离渲染的 TSX + React 页面代码，但严格遵循真实控件规格。  
> **排版与功能链条参考**：同目录 `PatchPilot-生产级方案.md`（§1–§9 骨架、F\*/W\*/X* 编号体系、§8 总表、§9 灰度与对抗清单）；本文档的域按本项目重命名，编号不复用。  
> **v1.1 变更**：Agent 开发不再使用 LangChain / LangGraph / Temporal / AI SDK / LLM 观测框架等任何 Agent 框架，harness 全链路改为原始手写实现；新增 §11「手写 Harness 内核规格」，并同步替换 §3.3 / §5 / §6.8 / §7 / §8 的技术栈表述。

---

## 0. 定稿决策记录

本轮四项待定项已按工程最优解定稿，后续设计均以此为准。

| #   | 决策         | 定稿内容                                                                                                                                                                                                                             |
| --- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D-1 | 跨用户语料入库与撤回 | 「沙箱测试通过 + 自动评分通过」且用户未关闭共享才入库；**附件原文、账号信息、PII 永不入库**；用户删除控件 → 公共语料立即失效；入库前脱敏、去重、投毒检测                                                                                                                                              |
| D-2 | 沙箱测试失败上限   | 自纠正回边 **≤2 次**，之后输出失败原因码 + 保留部分产物 + 转人工，不无限重试                                                                                                                                                                                    |
| D-3 | 账号系统范围     | JWT + 邮箱密码；两个角色：普通用户 / 语料审核员；不接 OAuth                                                                                                                                                                                            |
| D-4 | 模型分档       | 做。简单控件用便宜模型，带附件或复杂交互用强模型；阈值走配置中心版本化，影子验证后再灰度                                                                                                                                                                                     |
| D-5 | Agent 开发栈  | **不使用 LangChain / LangGraph / Temporal / AI SDK / CopilotKit / LangFuse 等任何 Agent 框架**；agent loop、结构化输出、工具注册、checkpoint 与恢复、双向护栏、检索融合、事件流、评测全部手写（§11）。通用基础设施（Postgres / Redis / FastAPI / Pydantic / React / OTel / esbuild 等）保留 |

---

## 1. 产品概述

### 1.1 背景与问题定义

手机桌面控件（日历、时钟、闹钟、待办等）的开发原本需要人工写 Lynx + React 代码、打包、发布、装机。描述生成控件的链路把「描述 → 代码 → 打包 → 发布 → 安装 → 桌面可用」自动化，但真正决定它能不能成为产品的不是生成质量，而是三件事：

1. **生成产物不可信**：模型吐出的代码可能越权调用能力、越出规格尺寸、渲染报错，必须在渲染前被确定性拦住。
2. **长任务可恢复**：生成、编译、测试是长链路，用户会打断、会跨天回来继续，状态不能存在前端内存里。
3. **交互要让人看懂 Agent 在做什么**：流式输出若只是 token 刷屏，用户既无法纠偏也无法建立信任。

### 1.2 目标与非目标

**目标**

1. 端到端跑通「描述 + 附件 → 规格判定 → TSX 代码 → 编译 + 沙箱测试 → 控件包 → 按规格卡片预览与管理」。
2. 建立一条**完整、可断言、可回放**的 harness 控制链路，覆盖输入校验、双向 LLM 护栏、上下文与检索、生成回路、契约校验、沙箱测试、运行时隔离、恢复、观测、评测、成本、审计。
3. 前端做到「流式可纠偏、过程可回放、产物可按真实规格预览」。
4. 支持账号体系、跨用户语料共享与可撤回、跨天/月断点恢复。
5. 质量、安全、成本、体验均有可量化指标，支持影子模式与回归门禁。

**非目标**

不生成 Lynx 代码、不编译移动端 bundle、不打包上传服务器、不安装到手机桌面、不接手机系统 API；不做通用聊天助手（只处理控件生成）；不做设备环境模拟；不做多 Agent 角色编排；不提供在线关闭门禁的开关。

### 1.3 用户与角色

| 角色    | 关心什么                              | 主要界面      |
| ----- | --------------------------------- | --------- |
| 普通用户  | 描述能不能一次生成想要的控件、过程能不能打断/继续、卡片能不能管理 | 生成会话页、控件墙 |
| 语料审核员 | 哪些产物进入公共语料、是否有投毒或越权代码             | 语料审核页     |
| 平台管理员 | 成本、配额、护栏命中率、回归门禁                  | 运行监控、评测页  |

权限裁剪在服务端完成；前端隐藏按钮只作为体验优化。

### 1.4 核心流程

**触发**：用户在会话中输入描述、选择规格、附加文件，提交生成任务。

**用户旅程**：`选择规格 + 输入描述/附件 → 输入校验与进 LLM 护栏 → 意图与附件 role 判定 → 技能预取与检索 → 生成回路 → 出 LLM 护栏 → 契约校验 → 沙箱编译/渲染/断言 → 产物入库与卡片预览 → 安装/删除管理 → 评测合格回流公共语料`。

**会话状态机**（前后端共用同一枚举，定义见 §4.2）：

```
DRAFT → RUNNING ⇄ PAUSED（用户停止 / 断点）
      → AWAITING_INPUT（描述不足 / 附件 role 歧义 / 规格冲突）
      → VALIDATING（出护栏 + 契约校验 + 沙箱测试）
      → COMPLETED（产物就绪，进入控件墙）
旁路：FAILED（带原因码 + 证据引用） / CANCELLED
```

**产物状态机**：`GENERATED → INSTALLED → DELETED`。安装与删除是 App 内的状态操作，不触发真实装机。

任何旁路状态必须带**原因码 + 证据引用**，前端不得只显示「失败」。

### 1.5 控件规格

六种规格是一等输入，由用户显式选择，系统不推断：

| 规格    | 网格      | 典型可用信息密度     | 约束示例              |
| ----- | ------- | ------------ | ----------------- |
| `2x1` | 2 列 1 行 | 单行文本 / 数字    | 放不下月视图日历，触发规格冲突提示 |
| `1x2` | 1 列 2 行 | 竖排短信息        | 不适合宽表格            |
| `2x2` | 2×2     | 日期 + 简短事项    | 中等                |
| `2x4` | 2 列 4 行 | 列表型（待办 / 日程） | 可滚动区域需声明          |
| `4x2` | 4 列 2 行 | 横向多指标        | 横向布局优先            |
| `4x4` | 4×4     | 日历月视图 / 复杂面板 | 信息密度上限最高          |

规格同时约束生成协议里的 `layout_slots`、静态校验里的尺寸断言、前端卡片渲染的真实比例。

### 1.6 成功指标

| 层  | 指标                                                   | 定义                        |
| -- | ---------------------------------------------------- | ------------------------- |
| 质量 | **一次生成通过率**、回边次数 P50/P95、规格合规率、沙箱测试通过率               | 一次通过率 = 无自纠正直接通过全部校验的任务占比 |
| 安全 | 双向护栏命中率 / 误拦截率、越权能力拦截率（目标 100%）、PII 泄漏数（恒 0）、投毒语料拦截率 | 与成功率独立统计                  |
| 成本 | 单会话总成本 P50/P95（模型 token + 沙箱机时 + 编译）、按模型档位分账         | 回边与重复检索计入本轮               |
| 体验 | TTFT、SSE 首事件延迟、跨天恢复成功率、平均交互轮次、控件安装率                  | 安装率作为「用户认可产物」的代理指标        |
| 语料 | 公共语料采纳率、检索引用率、撤回生效时延、负例回流占比                          | 衡量跨用户共享是否真的提升一次通过率        |

**指标口径**：离线结果报分布与置信区间，不报单点估计；一次生成通过率上升但沙箱通过率或安装率下降，判为无效优化。

### 1.7 设计原则

1. **确定性门禁高于模型自律**：护栏、能力白名单、金额/次数上限写在代码层，不写在 prompt 里。
2. **生成协议与渲染协议共用同一份契约**：`manifest` 声明的能力，静态校验比对它、运行时按它放行、评测按它出判据。
3. **护栏是两次 LLM 边界 + 一次执行验证**：进 LLM 前、出 LLM 后、渲染前，三者职责不重叠。
4. **私有数据不共享**：只有脱敏后的 spec / 代码 / 过程摘要可进公共语料；附件原文、账号信息、PII 永不入库。
5. **状态持久化与无状态计算分离**：会话与任务在 Postgres，恢复调度由手写 checkpoint + 租约实现（§11.4），worker 可随时被杀；挂起不占计算资源。
6. **生成代码不在编排进程里执行**：编译与渲染测试在一次性沙箱，线上渲染在受限 iframe。
7. **证据先于结论**：产物只有通过契约校验与沙箱测试才允许进入控件墙。
8. **门禁没有「关掉」这个开关**：所有对照实验在影子模式或离线回放完成。

---

## 2. 需求规格

### 2.1 功能需求总览

| 编号    | 需求                                                    | 优先级 | 所属端 |
| ----- | ----------------------------------------------------- | --- | --- |
| FR-01 | 账号注册/登录、JWT、用户级数据隔离、两角色                               | P0  | 前后端 |
| FR-02 | 会话列表与会话状态机（含跨天/月断点恢复）                                 | P0  | 前后端 |
| FR-03 | 任务打断 / 继续 / 取消，手写 checkpoint + 租约续跑（不用 workflow 框架）   | P0  | 前后端 |
| FR-04 | 六种规格选择与规格可行性校验                                        | P0  | 前后端 |
| FR-05 | 附件上传（图片/视频/文本）与传输层校验                                  | P0  | 前后端 |
| FR-06 | 附件 role 判定（视觉参考 / 内容提取 / 数据源 / 风格参考）与多模态处理            | P0  | 后端  |
| FR-07 | 输入校验（传输层 + 语义层）与进 LLM 护栏                              | P0  | 后端  |
| FR-08 | 意图白名单路由（只放行控件生成）与技能渐进式披露                              | P0  | 后端  |
| FR-09 | 生成回路：TSX + React 结构化输出、模型分档、自纠正回边 ≤2                  | P0  | 后端  |
| FR-10 | 出 LLM 护栏与 `WidgetSpec`/`manifest` 契约校验                | P0  | 后端  |
| FR-11 | 沙箱内 TSX 编译 + 渲染 + 规格/能力/交互行为断言                        | P0  | 后端  |
| FR-12 | `WidgetBundle` 产物、hash、版本与按用户隔离存储                     | P0  | 后端  |
| FR-13 | 渲染协议：iframe sandbox + CSP + postMessage bridge + 能力放行 | P0  | 前后端 |
| FR-14 | 控件墙：按真实规格比例渲染卡片、安装/删除/版本/回滚                           | P0  | 前端  |
| FR-15 | 分通道类型化 SSE 事件流、partial JSON 渐进渲染、增量代码 patch           | P0  | 前后端 |
| FR-16 | event-sourced reducer、seq 回放、时间线 scrubber 与「从这里重生成」   | P1  | 前端  |
| FR-17 | 跨用户公共语料：入库门禁、脱敏、投毒防护、撤回、引用可溯源                         | P1  | 后端  |
| FR-18 | RAG 优化：硬过滤、混合检索 + rerank、版本快照、缓存、负例回流                 | P1  | 后端  |
| FR-19 | 全链路 trace、只追加审计、成本分账、配额与熔断                            | P1  | 前后端 |
| FR-20 | 离线回放评测、失败模式 rubric、变更触发回归门禁                           | P1  | 前后端 |
| FR-21 | 重试 / 降级 / 熔断、粒度化 kill switch                          | P1  | 后端  |
| FR-22 | 前后端契约单一事实来源（OpenAPI + 事件 schema + 枚举）                 | P0  | 前后端 |
| FR-23 | 记忆管理（用户偏好：配色、常用规格、常用控件类型）                             | P2  | 后端  |

### 2.2 非功能需求

| 类别  | 要求                                                                  |
| --- | ------------------------------------------------------------------- |
| 安全  | 生成代码绝不在编排进程执行；沙箱与 iframe 双重隔离；能力白名单最小必要；跨用户语料不含私有数据；审计只追加且可完整重放一次会话 |
| 可靠性 | 会话与任务持久化；worker 可随时被杀并接管；跨天/月恢复不丢状态、不重复副作用；`interrupt` 幂等           |
| 性能  | TTFT P95 < 1s；SSE 首事件 P95 < 1.5s；流式渲染不卡顿（rAF 批处理）；控件卡片并发渲染不阻塞主线程    |
| 可用性 | 断线重连按 `runId + seq` 续传；失败有原因码与重试入口；空态/加载/错误/无权限各有明确设计               |
| 合规  | 附件与会话按用户隔离；脱敏后进入公共语料；用户可关闭共享并可撤回；留存期可配置                             |
| 可观测 | 任一会话可重建「输入 → 护栏 → 检索 → 生成 → 校验 → 沙箱 → 交付 → 成本」完整路径                  |
| 成本  | 单会话预算可设、可预测、超限确定性中断并提示，不是提示模型省着点                                    |

### 2.3 威胁模型

| 资产                     | 不可信输入                         | 攻击路径                                                                                                                                                         | 控制点                                                                                    |
| ---------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| 用户账号、会话、附件原文、生成产物、公共语料 | 用户描述、图片/视频/文本附件、模型输出、公共语料检索结果 | ① 描述夹带注入指令（忽略规则 / 输出系统提示 / 生成外联代码）；② 附件伪装类型或夹带恶意内容；③ 模型生成调用 `eval`/`fetch`/系统 API 的代码；④ 恶意产物进入公共语料造成跨用户投毒传播；⑤ 越权访问他人会话/包；⑥ iframe 逃逸或访问父页面；⑦ 超大附件 / 循环生成耗尽预算 | 输入校验 + 双向护栏 + 能力白名单 + 契约校验 + 沙箱测试 + iframe sandbox/CSP + 语料入库门禁 + 租户过滤 + 只追加审计 + 配额与熔断 |

注入防护不能只依赖 prompt 中「描述是数据」的声明：内容型过滤只能覆盖单一藏匿点，**能力边界与运行时隔离才是最终控制面**。公共语料同理——检索到的历史代码也是不可信数据，不得直接执行、不得携带原用户信息。

### 2.4 架构约束

1. 护栏是确定性代码：进 LLM 护栏与出 LLM 护栏各自有独立判据、命中日志与误拦截率指标。
2. 生成代码只使用 TSX + React，import 白名单只有 `react` 与 `@widget/bridge`，默认导出 `Widget` 组件。
3. 编译与测试在一次性沙箱完成，**不模拟真实设备环境**；不做装机、不接系统 API。
4. 运行时 iframe 使用 `sandbox="allow-scripts"` + 严格 CSP，宿主与控件只通过窄 `postMessage` 协议通信。
5. 能力通过 bridge 按 `manifest` 声明放行：时间、定时器、受限本地存储、静态数据、主题、语言；禁止任意网络请求与系统能力。
6. 会话与任务 = 持久化状态 + 无状态 worker；checkpoint 只存结论与引用，大对象（代码、附件、日志、快照）放对象存储。
7. 公共语料只收脱敏后的 spec / 代码 / 过程摘要，且必须可撤回；私有数据与公共语料物理或逻辑隔离。
8. 门禁不可在线关闭；所有 A/B 对照走影子模式或离线回放。
9. 工具数量是每轮常驻税：技能通过结构化输出按需披露，不新增常驻工具。
10. 编排保持简单：单一生成 loop + 规程按需加载，不堆多 Agent 角色。

### 2.5 假设与依赖

- 生成产物只需在浏览器 webview 等价环境（DOM + JS）中可渲染，不要求移动端原生能力。
- 模型能力由可替换的 model gateway 提供，支持商业 API 与自托管推理。
- 沙箱测试使用 headless 浏览器 / jsdom 等价环境，不依赖真实设备。
- 评测集来自脱敏历史会话与人工构造的对抗用例，不公开、不进入模型训练语料。

### 2.6 横切能力体系（X1–X12）

> 本节不改变 §3 分层、不新增服务与 agent 角色。编号 X1–X12 与 §8 总表、FR 表对应。

| #   | 能力           | 机制要点                                                                              | 落在           |
| --- | ------------ | --------------------------------------------------------------------------------- | ------------ |
| X1  | 会话状态与断点恢复    | 会话/任务持久实体 + **手写** checkpoint / 租约 / 幂等副作用；跨天/月按 `runId + seq` 恢复；挂起不占计算资源（§11.4） | D1 / W2      |
| X2  | 上下文管理        | state 只存结论与引用；tool/日志窗口化；超阈值压缩保留任务契约与已排除方案；固定槽位保持 prompt caching                  | D4 / X4      |
| X3  | 记忆管理         | 用户偏好（配色、常用规格、常用控件类型）结构化存储、版本化、可清除；不把历史代码当经验回喂                                     | D4 / D7      |
| X4  | 成本管理         | 单会话预算（token / 轮次 / 工具调用 / 沙箱分钟）四维硬上限；模型分档；缓存复用；按档位分账                              | D4 / D8      |
| X5  | 重试 / 降级 / 熔断 | 瞬时故障指数退避有上限；业务失败是**自纠正回边**不是 retry；沙箱不可用降级为静态校验 + 只读诊断；粒度化 kill switch            | D1 / D5 / D8 |
| X6  | 意图路由与技能渐进式披露 | 只放行控件生成；按 `widget_type + grid` 确定性预取规程；模型可显式请求技能但需白名单与次数校验                        | D3           |
| X7  | 输入校验与双向护栏    | 传输层 → 语义层 → 进 LLM 护栏；出 LLM 护栏；渲染前沙箱测试作为执行验证（三者不重叠）                                | D2 / D3 / D5 |
| X8  | RAG 与语料治理    | 四层语料、硬过滤优先、混合检索 + rerank、版本快照、缓存、负例回流；跨用户语料入库门禁与撤回                                | D7           |
| X9  | 沙箱测试与运行时隔离   | TSX 编译、渲染、尺寸/能力/交互断言在一次性沙箱；线上 iframe sandbox + CSP + bridge                       | D5 / D6 / W4 |
| X10 | 评测与回归门禁      | 离线回放 + 失败模式 rubric；规程/prompt/模型/检索变更触发回归；报告含分布与置信区间                               | D7 / D8      |
| X11 | 可观测与审计       | 全链路 trace、只追加审计、每步证据引用、成本账、护栏命中日志                                                 | D8           |
| X12 | 流式事件协议与前端回放  | 分通道类型化 SSE、partial JSON、增量 patch、event-sourced reducer、seq 回放                     | W3 / W5      |

明确不做：通用多 Agent 编排、通用工具集（shell / 网络 / 任意文件写）、把模型自报置信度当路由判据、把 X1–X12 做成看板打勾。

---

## 3. 总体架构

### 3.1 分层

```mermaid
flowchart TB
  subgraph Client["前端 App（React SPA）"]
    WEB["会话列表 / 生成会话页 / 控件墙 / 时间线回放 / 账号"]
  end
  subgraph Edge["接入层"]
    GW["API Gateway / BFF · JWT · 限流 · SSE 网关"]
  end
  subgraph Control["控制面（无状态）"]
    API["Control Plane API · 会话/附件/产物/评测"]
    ORCH["Orchestrator · 手写 agent loop + checkpoint/租约"]
    GUARD["Guardrails · 进/出 LLM 双向护栏"]
    RAG["Retrieval · 四层语料 + 入库门禁"]
    EVAL["Eval Runner · 离线回放 + 回归门禁"]
  end
  subgraph Exec["验证执行面（每任务一次性）"]
    SBX["Sandbox · TSX 编译 / 渲染 / 行为断言"]
    OBJ[("对象存储 · 源码/编译产物/日志/快照")]
  end
  subgraph Runtime["用户侧渲染"]
    CARD["规格卡片 · iframe sandbox + CSP + bridge"]
  end
  subgraph Data["数据面"]
    PG[("PostgreSQL · 账号/会话/checkpoint/产物索引/审计")]
    RDS[("Redis · 队列/幂等/SSE 扇出/限流")]
    VEC[("向量库 · 公共语料 + 规程索引")]
  end
  WEB --> GW --> API
  API --> PG
  ORCH --> PG
  ORCH --> RDS
  ORCH --> GUARD
  ORCH --> RAG --> VEC
  ORCH --> SBX --> OBJ
  API -->|SSE 事件流| WEB
  ORCH --> EVAL
  EVAL -->|合格产物| RAG
  OBJ --> CARD
```

**三条硬边界**：① 控制面与验证执行面不共享进程 / 文件系统 / 凭证；② 生成代码绝不在编排进程里执行；③ 用户侧渲染在受限 iframe，控件无法访问宿主页面与用户数据。

### 3.2 一次任务的完整时序

1. 用户登录，App 拉取会话列表；新建会话并选择 `grid`。
2. 输入描述 + 上传附件 → 传输层校验（类型/大小/数量/内容嗅探）→ 对象存储，返回 `attachment_id`。
3. 语义层校验：描述充分性、附件 role 是否明确、`grid` 枚举合法性；不足则 `AWAITING_INPUT` 并给出具体补充建议。
4. **进 LLM 护栏**：注入模式、PII、意图白名单、预算前置。命中 → 拒绝或脱敏，不进模型，写命中日志。
5. 意图与规格确定 → 按 `widget_type + grid` 确定性预取生成规程（技能渐进式披露）。
6. 上下文装配：跨用户公共语料 + 规程库 + 用户记忆 + 本次 `AttachmentInsight`，按 token 预算与固定槽位装配；硬过滤优先。
7. 生成回路：结构化输出 `WidgetSpec` → 生成 TSX + React 代码；模型按复杂度分档；每轮记 token / 成本。
8. **出 LLM 护栏**：schema、能力白名单、禁止内容、PII、描述符合度、规格一致性。失败 → 自纠正回边（≤2 次）。
9. 契约校验：`spec ↔ manifest ↔ 代码能力声明` 比对 + hash；不通过不进沙箱。
10. **沙箱测试**（不模拟设备）：TSX 编译 → 一次性沙箱挂载渲染 → 规格尺寸 / 无运行时错误 / 能力调用全走 bridge / 交互行为断言 → 日志与快照进证据包。失败 → 回边或按 D-2 转人工。
11. 生成 `WidgetBundle`（源码 + 编译产物 + manifest + 静态校验 + 沙箱测试结果 + hash），状态 `COMPLETED`。
12. 前端 `artifact` 事件到达 → 控件墙按 `grid` 真实比例渲染受限 iframe 卡片；用户可安装 / 删除 / 查看 manifest 与 trace。
13. 评测合格后，脱敏产物经**入库门禁**进入公共语料，供其他用户检索（可撤回）；失败案例回流为负例。
14. 全流程事件按 `seq` 写入 SSE 流与只追加审计；成本按档位分账。


### 3.3 关键技术决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 产物形态 | 受限 TSX + React 代码 + 声明式 `manifest`，而非纯 JSON 蓝图 | 用户需要「渲染后台生成的代码」与动态交互；用声明式契约把开放代码关进可校验的笼子 |
| 生成协议 | 先 `WidgetSpec` 再产码，schema 校验 + validator 自纠正回边 | 复杂嵌套 structured output 在规模下易碎，采用 prompt 生成 → 校验 → 自纠正 |
| 渲染协议 | iframe `sandbox="allow-scripts"` + CSP + 窄 postMessage bridge | 与宿主隔离，能力按 manifest 声明放行，未声明调用拒绝 |
| 沙箱测试 | 一次性沙箱内编译 + 渲染 + 行为断言，**不模拟设备** | 环境限制下仍能对产物做确定性验收，且避免把不可信代码放进编排进程 |
| 语料共享 | 私有数据隔离 + 脱敏产物经入库门禁进公共语料 | 跨用户参考的同时堵住 PII 泄露与投毒传播 |
| 编排 | 单一生成 loop + 手写 checkpoint / 租约 / 幂等（§11） | 产物封闭，不需要多 Agent 角色；挂起与跨天恢复靠持久化 |
| Agent 开发栈 | **零 Agent 框架**：模型直连 HTTP、工具自研注册、结构化输出自研校验、检索融合自研、SSE 自研 | 无黑盒、依赖少、每一步可断言，且能完整讲清 harness 的实现细节 |
| 实时通道 | SSE（`seq` + `Last-Event-ID` + `runId` 重连） | 服务端单向、可回放、代理与鉴权简单；中断走独立 POST signal |
| 流式渲染 | 分通道类型化事件 + partial JSON + 增量 patch | 比 token 刷屏更可纠偏、可回放，也更省渲染开销 |
| 护栏 | 进 LLM / 出 LLM 各一次 + 渲染前沙箱验证 | 三道职责不重叠，判据均为确定性代码 |
| 状态管理 | 前端 event-sourced reducer | 纯函数、可回放、可单测，支撑时间线 scrubber |

---

## 4. 数据模型与接口契约

### 4.1 核心实体

| 实体 | 关键字段 | 说明 |
|---|---|---|
| `User` | id, email, password_hash, role(`user`/`curator`), share_enabled, quota, created_at | 账号与配额归属 |
| `Session` | id, user_id, title, state, grid, created_at, updated_at, last_seq | 会话主记录，`state` 见 §4.2 |
| `Run` | id, session_id, run_id, attempt_no, checkpoint_rev, model_tier, started_at, ended_at | 一次执行尝试，挂起/恢复/重跑都开新记录 |
| `Checkpoint` | session_id, run_id, rev, node, state_blob_ref, created_at | 只存结论与引用；跨天恢复的唯一依据 |
| `Attachment` | id, user_id, session_id, kind, mime, size, object_key, role, insight_ref, hash | 附件与 role 判定结果 |
| `WidgetBundle` | id, user_id, session_id, spec_ref, source_ref, compiled_ref, manifest, static_check, sandbox_test, state, hash, versions[] | 产物索引，对象在对象存储 |
| `CorpusItem` | id, bundle_id, owner_user_id(脱敏), grid, widget_type, spec_version, recipe_version, quality_score, toxicity_check, revoked_at | 公共语料条目，可撤回 |
| `AuditEvent` | seq, user_id, session_id, actor, action, decision, payload, trace_id, prev_hash | 只追加，hash 链防篡改 |
| `EvalCase` | id, source(`history`/`adversarial`/`expert`), input_ref, expected, rubric, result | 评测集 |

**字段归属原则**：逐轮过程数据（spec 增量、代码 patch、护栏命中、回边、成本）放事件流与 `Run`，不塞进 `Session` 主记录；大对象一律存对象存储引用。

### 4.2 状态与枚举

```ts
type GridSpec = "2x2" | "2x4" | "4x2" | "4x4" | "2x1" | "1x2";
type WidgetType = "calendar" | "clock" | "alarm" | "task" | "custom";
type AttachmentRole = "visual_reference" | "content_extraction" | "data_source" | "style_reference";
type Capability = "date" | "timer" | "local_storage" | "static_data" | "theme" | "locale";
type ModelTier = "cheap" | "standard" | "frontier";

type SessionState =
  | "DRAFT" | "RUNNING" | "PAUSED" | "AWAITING_INPUT" | "VALIDATING"
  | "COMPLETED" | "FAILED" | "CANCELLED";

type BundleState = "GENERATED" | "INSTALLED" | "DELETED";

type ReasonCode =
  | "INPUT_INVALID" | "INTENT_OUT_OF_SCOPE" | "GUARD_INPUT_HIT" | "GUARD_OUTPUT_HIT"
  | "SPEC_CONFLICT" | "CAPABILITY_DENIED" | "CONTRACT_MISMATCH" | "COMPILE_FAILED"
  | "SANDBOX_TEST_FAILED" | "RETRY_EXHAUSTED" | "BUDGET_EXHAUSTED"
  | "SANDBOX_UNAVAILABLE" | "HUMAN_REQUIRED" | "MODEL_ERROR";
```

### 4.3 三份协议（核心契约）

#### 4.3.1 生成协议 `WidgetSpec`

```ts
type WidgetSpec = {
  spec_version: string;              // 版本化，索引与 manifest 均绑定
  widget_id: string;
  grid: GridSpec;                    // 用户显式选择，不推断
  widget_type: WidgetType;
  intent: string;                    // 归一化后的描述
  attachments: {
    attachment_id: string;
    role: AttachmentRole;            // 由描述决定，可在前端纠偏
    insight_ref: string;             // AttachmentInsight 对象引用
  }[];
  layout_slots: { slot: string; content: string }[];   // 受 grid 信息密度约束
  capabilities: Capability[];        // 声明式能力，渲染侧按此放行
  data_contract: {
    sources: ("time" | "local_storage" | "static")[];
    storage_namespace: string;       // 按 user + widget 隔离
  };
  style: { palette?: string[]; radius?: number; font_scale?: number };
  constraints: { forbidden: string[]; max_elements: number };
  language: "tsx";
  react_version: string;
  import_allowlist: ["react", "@widget/bridge"];
  export_contract: { default: "Widget" };
  props_contract: ["grid", "theme", "locale", "tick", "data"];
};
```

生成侧先产出可校验的 `WidgetSpec`，再据此生成代码；规格冲突（如 `2x1` + 月视图）在 spec 层即被拒绝并提示用户改选。

#### 4.3.2 产物协议 `WidgetBundle`

```ts
type WidgetBundle = {
  bundle_id: string;
  user_id: string;
  session_id: string;
  spec: WidgetSpec;
  source: { path: "widget.tsx"; content_ref: string; language: "tsx" };
  compiled: { content_ref: string; hash: string };
  manifest: {
    spec_version: string;
    spec_hash: string;
    grid: GridSpec;
    widget_type: WidgetType;
    capabilities: Capability[];      // 与 spec 一致，静态校验比对
    import_allowlist: string[];
    exports: { default: "Widget" };
    props: string[];
    recipe_version: string;          // 所用规程版本
    retrieval_snapshot: string;      // 所用检索快照与引用
    model: string; prompt_version: string; model_tier: ModelTier;
  };
  static_check: { passed: boolean; violations: string[] };
  sandbox_test: {
    passed: boolean;
    cases: { name: string; passed: boolean; detail_ref: string }[];
    log_ref: string; snapshot_ref?: string;
  };
  state: BundleState;
  hash: string;                      // 整包摘要，防篡改
  created_at: string;
};
```

**同一份 manifest 贯穿四处**：生成侧按 spec 产码 → 静态校验比对能力声明 → 沙箱测试按声明放行 → 运行时 bridge 按声明放行 → 评测按声明出判据。

#### 4.3.3 渲染协议 `WidgetRuntime`

```ts
type WidgetRuntime = {
  card: { grid: GridSpec; px: { w: number; h: number } };  // 真实比例
  iframe: {
    sandbox: ["allow-scripts"];       // 不含 allow-same-origin / top-navigation / forms / popups
    csp: string;                      // 禁外联脚本、禁 eval、禁网络
    srcdoc_ref: string;               // 指向 compiled 产物
  };
  bridge_messages: [
    "widget:ready", "widget:resize", "widget:error",
    "host:theme", "host:locale", "host:tick", "host:data"
  ];                                  // 窄协议，非任意 JSON-RPC
  capability_bridge: Capability[];    // = manifest.capabilities ∩ 白名单；未声明调用拒绝并上报
  limits: { render_timeout_ms: number; max_message_rate: number };
};
```

宿主 ↔ 控件只走上述消息；时间统一由 `host:tick` 提供，本地存储按 `storage_namespace` 隔离；控件不得 `fetch`、不得访问父页面 DOM、不得调用系统 API。

### 4.4 REST API 概要

| 方法 | 路径 | 用途 | 权限 |
|---|---|---|---|
| `POST` | `/api/auth/register` / `/login` | 注册 / 登录，签发 JWT | 公开 |
| `GET` / `POST` | `/api/sessions` | 会话列表 / 新建（含 `grid`） | 本人 |
| `GET` | `/api/sessions/{id}` | 会话快照 + 产物引用 | 本人 |
| `POST` | `/api/sessions/{id}/messages` | 提交描述 + `attachment_ids` + `grid` | 本人 |
| `POST` | `/api/sessions/{id}/interrupt\|resume\|cancel` | 打断 / 继续 / 取消（独立 POST signal） | 本人 |
| `POST` | `/api/attachments` | 附件上传，返回 `attachment_id` | 本人 |
| `PATCH` | `/api/sessions/{id}/attachment-roles` | 纠偏附件 role（前端规格卡交互） | 本人 |
| `GET` | `/api/sessions/{id}/events` | SSE 事件流（`Last-Event-ID` + `runId`） | 本人 + 短期 ticket |
| `GET` | `/api/bundles` | 当前用户的控件包列表 | 本人 |
| `POST` | `/api/bundles/{id}/install\|delete\|rollback` | 包状态管理 | 本人 |
| `GET` | `/api/bundles/{id}/preview` | 返回受限 iframe 渲染所需的编译产物引用 | 本人 |
| `GET` | `/api/bundles/{id}/evidence` | manifest / 静态校验 / 沙箱测试 / trace | 本人 |
| `GET` | `/api/corpus` / `POST /api/corpus/{id}/revoke` | 公共语料检索（内部）/ 撤回 | 审核员 |
| `GET` | `/api/evals` / `/api/metrics` / `/api/audit` | 评测、指标、审计 | 管理员 |

统一响应 `{ code, message, traceId, retryable, evidenceRef? }`；`code` 与 `ReasonCode` 对齐。

### 4.5 实时事件协议

```ts
type AgentEventKind =
  | "run" | "plan" | "spec" | "attachment" | "skill" | "routing"
  | "code" | "guard" | "validation" | "sandbox" | "artifact"
  | "cost" | "checkpoint" | "interrupted" | "notice" | "error" | "done";

type AgentEvent = {
  seq: number;                 // 每会话单调递增
  sessionId: string;
  runId: string;               // 重连与跨天恢复的锚点
  ts: string;
  kind: AgentEventKind;
  phase: SessionState;
  payload: unknown;            // 按 kind 用 discriminated union 收窄
  traceId: string;
};
```

- **分通道**：`plan`（结论式计划）、`spec`（partial JSON 渐进）、`attachment`（role 判定结果）、`code`（增量 patch）、`validation`（护栏/契约/沙箱结果）、`artifact`（产物就绪）、`notice`（成本、降级、提示）。每类独立渲染组件。
- **可靠性**：`Last-Event-ID` 续传 + 服务端保留最近 N 条，超窗先拉快照；心跳保活；显式 `done` / `error`；事件按 `seq` 在前端 reducer 去重与乱序丢弃。
- **中断语义**：`interrupt` 不是关闭连接——服务端写 checkpoint，流上推 `interrupted`，UI 切为「已暂停 · 从 XX 继续」；重连按 `runId + seq` 恢复。
- **向前兼容**：前端 reducer 对未知 `kind` 必须忽略而非报错。

---

## 5. 后端详细设计

> 每个域给出目标、功能、技术栈、关键要点、验收。编号 D1–D8 与 §8 总表对应。

### D1 · 账号、会话与编排域

- **功能**：注册登录、JWT、用户级隔离、会话状态机、任务打断/继续/取消、**手写** checkpoint / 租约续跑 / 幂等副作用。
- **技术栈**：FastAPI、Postgres（版本化 checkpoint 表 + `side_effect` 幂等表 + 租约列）、Redis Streams、Pydantic v2、JWT（短时 + refresh）。**不使用 Temporal 或任何 workflow 框架**。
- **要点**：会话与 `Run` 是持久实体；`checkpoint` 只存结论与引用，大对象放对象存储；worker 无状态，任一实例凭 `runId` 接管；恢复调度由「扫描过期租约 → 原子抢租约 → 从该 rev 恢复 → 心跳续租」手写实现（§11.4）；`interrupt()` 恢复时节点从顶部重执行，前置副作用幂等键 = `session_id + step + payload_hash`；业务性失败（生成不合规）是**自纠正回边**不是基础设施重试；跨天/月恢复靠 checkpoint + `runId + seq`，挂起期间不占计算资源；两角色（`user` / `curator`）权限裁剪在服务端。
- **验收**：kill worker 后另一实例续跑且不重复产物；暂停 7 天后恢复状态完整；取消立即释放沙箱；跨用户读取他人会话/包一律 403。

### D2 · 附件处理域

- **功能**：上传、传输层校验、role 判定、多模态处理、产出 `AttachmentInsight`。
- **技术栈**：对象存储、文件类型嗅探（magic number）、图片/关键帧处理、OCR/内容提取、结构化文本解析。
- **要点**：**role 由用户描述决定**——「把这张图当背景」→ `visual_reference`，「提取图片右上角的日期」→ `content_extraction`，视频同理（取关键帧/元信息），文本可作 `data_source`；同一附件不同描述走不同处理器；role 不明确时 `AWAITING_INPUT` 请求澄清，不猜测；输出统一为带引用的 `AttachmentInsight{ source_ref, extracted, confidence }`；附件原文永不进入公共语料；大小/数量/类型/内容嗅探校验在上传时完成。
- **验收**：同一图片两种描述走出两条处理路径；伪造扩展名被拒；超大附件被拒且给出原因码；role 纠偏后可从该步重跑。

### D3 · 输入校验、护栏与路由域

- **功能**：语义层输入校验、**进 LLM 护栏**、意图白名单路由、规格校验、技能渐进式披露。
- **技术栈**：**纯代码规则判定**（自研 pattern / rule registry）、Pydantic v2、正则/模式库、技能 manifest + JSON Schema；护栏不依赖 prompt 或策略框架。
- **要点**：三层分离——传输层（D2）→ 语义层（描述充分性、role 明确性、`grid` 枚举）→ 进 LLM 护栏（注入模式、PII、意图范围、预算前置）；护栏命中**不进模型**，写命中类型与原文引用；只放行「生成控件」意图，其他一律 `INTENT_OUT_OF_SCOPE`；规格冲突（如 `2x1` + 月视图）在 spec 前提示改选；技能按 `widget_type + grid` 确定性预取，模型可显式请求但需白名单、深度 = 1、单轮次数与 token 上限；不新增常驻工具。
- **验收**：注入描述被拦且未产生模型调用；非控件请求被拒；误拦截率可测；技能漏加载可归因；规程变更触发回归门禁。

### D4 · 生成编排与成本域

- **功能**：上下文装配、生成回路、模型分档、结构化输出、自纠正回边、成本管理、记忆管理。
- **技术栈**：**自研 model gateway**（直连 provider HTTP / OpenAI-compatible 协议，不引入 SDK 框架）、**手写 agent loop**、自研结构化输出校验与 partial-JSON 解析、自研工具注册表、Pydantic v2、原生 usage 字段计量、tiktoken（纯计数工具库）。全部实现见 §11。
- **要点**：上下文按固定槽位装配（系统契约 → 规程 → 检索片段 → 附件洞察 → 用户记忆 → 会话结论），骨架与正文去重以保住 prompt caching；state 只存结论与引用；生成先出 `WidgetSpec` 再产 TSX 代码；模型分档按「是否带附件 / widget_type 复杂度 / 是否触发回边」的**确定性 rubric**，阈值走配置中心版本化、变更触发回归；自纠正回边 ≤2 次（D-2），attempt 边界重置档位；成本四维硬上限（token / 轮次 / 工具调用 / 沙箱分钟），触顶确定性中断而非提示模型省着点；按档位分账，回边与重复检索计入本轮；用户偏好结构化存储、可清除，**不把历史代码整段回喂**。
- **验收**：单会话不超预算；换模型只改配置；同题同版本两次跑结果一致（复现指纹含 prompt/规程/检索快照/模型版本）；回边次数与成本可测。


### D5 · 出护栏、契约校验与沙箱测试域

- **功能**：**出 LLM 护栏**、`spec ↔ manifest ↔ 代码` 契约校验、TSX 编译、沙箱内渲染与行为断言。
- **技术栈**：规则引擎、esbuild / tsc、AST 静态分析、headless 浏览器或 jsdom 等价环境、一次性沙箱（gVisor/Kata 或容器 + seccomp/cgroups）、对象存储（日志/快照）。
- **要点**：出 LLM 护栏判据 = 结构化 schema、能力白名单、禁止内容（eval / 外联 / 未知 import）、PII、描述符合度、规格一致性；**不把模型文本直接当代码**；契约校验比对 manifest 声明与代码实际 import/能力调用，不一致不进沙箱；沙箱内依次执行：TSX 编译 → 挂载渲染 → 规格尺寸断言 → 无运行时错误 → 能力调用全走 bridge → 交互行为断言（日历翻月、时钟 tick、闹钟开关、任务勾选）；**不模拟真实设备环境**、不接系统 API；生成代码不在编排进程里跑；测试日志与快照进证据包；失败按 D-2 回边或转人工；沙箱不可用时降级为静态校验 + 只读诊断，绝不跳过校验直接交付。
- **验收**：注入「生成 fetch 外联代码」被静态扫描拦下；`2x1` 尺寸越界被断言拦下；未声明能力调用被 bridge 拒绝并上报；沙箱超时强制销毁；同一产物重复测试结果一致。

### D6 · 产物与包管理域

- **功能**：`WidgetBundle` 生成与版本、hash、按用户隔离存储、渲染协议下发、安装/删除/回滚状态机。
- **技术栈**：对象存储、Postgres 索引、`git`-式版本链、CSP 生成、`postMessage` bridge。
- **要点**：源码与编译产物双存，hash 绑定 manifest；`GENERATED → INSTALLED → DELETED` 状态操作带审计与二次确认（删除不可逆，不做乐观更新）；渲染协议按 §4.3.3 下发，`capability_bridge = manifest.capabilities ∩ 全局白名单`；iframe 不含 `allow-same-origin` / `top-navigation` / `forms` / `popups`；时间统一由 `host:tick` 提供，存储按 namespace 隔离；控件运行时错误上报并写 `widget:error` 事件；版本可回滚（沿用上一版编译产物）。
- **验收**：任一包可定位到 spec / manifest / 校验 / 沙箱结果 / trace；安装与删除不产生重复副作用；iframe 内无法访问父页面与用户数据；CSP 拦截外联脚本。

### D7 · 知识、检索与评测域

- **功能**：四层语料、跨用户公共语料入库门禁、RAG 优化、离线回放评测、失败模式 rubric、回归门禁。
- **技术栈**：向量库 + BM25 + RRF + rerank、版本化索引快照、自建 Eval Runner、脱敏管道、规则 + 轨迹断言 + 人工抽检。
- **要点**：
  - **四层语料**：规范层（6 种规格的尺寸/信息密度/可用能力）、规程与组件层（日历/时钟/闹钟/任务各自生成规程 + 可复用片段）、样例层（历史成功 `spec → bundle` 对）、附件层（本次 `AttachmentInsight`）。
  - **跨用户共享（D-1）**：所有用户生成的描述、过程、代码、任务在**脱敏 + 沙箱测试通过 + 自动评分通过 + 用户未关闭共享**后进入公共语料；附件原文/账号/PII 永不入库；入库前做投毒检测（恶意描述、越权代码、重复灌水）；绑定 `grid / widget_type / spec_version / recipe_version` 元数据；用户删除控件 → 语料立即失效；生成时可解释「参考了哪个控件」。
  - **检索优化**：硬过滤优先（`grid + widget_type + capability`）→ 混合检索（向量 + 关键词，规程名/API 名靠关键词）→ rerank；索引绑定版本快照，manifest 记录所用快照 → 可复现；固定槽位 + 去重以保住 prompt caching；只注入 top-k 相关片段，**不整段回喂历史代码**；缓存键 `(spec_hash, recipe_version, retrieval_snapshot)` + 高频描述语义缓存；失败案例回流为负例。
  - **优化目标**：降低回边次数、提高一次生成通过率，而非只看 top-k 命中率。
  - **评测**：离线回放 + 失败模式 rubric（正确生成 / 安全转人工 / 可恢复错误 / 越权代码 / 规格不符 / 死循环 / 放弃），按严重度加权；数值用确定性断言，不用裁判模型；规程 / prompt / 模型 / 检索 / 阈值变更触发回归门禁；评测跑多次报分布与置信区间。
- **验收**：检索无结果时不编造、转人工；公共语料撤回后立即不再被检索到；投毒样本 100% 被入库门禁拦下；任一任务可在离线回放中复现。

### D8 · 治理、观测与成本域

- **功能**：全链路 trace、只追加审计、成本分账、配额与熔断、粒度化 kill switch、指标与告警。
- **技术栈**：OpenTelemetry SDK（通用可观测标准）+ **自建 `agent_event` / `trace_span` 表** + Prometheus/Grafana + Loki、WORM/hash-chain 审计、纯代码策略判定、Redis 限流；**不使用 LLM 专用观测平台 SDK**。
- **要点**：任一会话可重建「输入 → 护栏 → 检索 → 生成 → 校验 → 沙箱 → 交付 → 成本」完整路径；护栏每次命中留命中类型、判据与原文引用；成本归因到用户 / 会话 / 轮次 / 模型档位；瞬时故障（429/5xx、沙箱启动失败）指数退避有上限；业务失败是回边不是 retry；降级阶梯（沙箱不可用 → 静态校验 + 只读诊断；模型不可用 → 便宜模型 → 规则兜底；预算触顶 → 确定性中断转人工）**降级不降低安全级别**；粒度化 kill switch（只暂停生成、不影响已有卡片预览）；unsafe-action 超阈值自动隔离并告警；门禁不可在线关闭，所有对照在影子 / 离线完成。
- **验收**：审计可完整重放一次会话；成本 P50/P95 可出账；故障注入后保状态且优雅降级；kill switch 按粒度生效。

---

## 6. 前端详细设计 ★

> 定位：**前端是这个产品最有讲头的部分**。Agent 产品的交互不是「聊天气泡 + 打字机」，而是让用户看得见意图被识别、规格被采纳、代码在长出来、校验在发生，并且能在中途纠偏、在任意一步回放。

### 6.1 信息架构

```
WidgetPilot App
 ├─ 登录 / 注册
 ├─ 会话中心（全部会话 · 状态 · 最后活动 · 新建时选规格）
 │   └─ 生成会话页 ★（描述 + 附件 · 分通道流式 · 代码/预览双轨 · 时间线回放）
 ├─ 控件墙 ★（按真实规格比例的卡片网格 · 安装/删除/版本/回滚）
 ├─ 语料审核页（审核员：入库队列 / 投毒命中 / 撤回）
 ├─ 评测与回归（一次通过率趋势 / 护栏命中 / 沙箱结论 / 门禁结果）
 ├─ 运行监控（成本 / 配额 / 熔断 / 告警）
 └─ 设置（共享开关 / 记忆偏好清除 / 主题 / 语言）
```

角色视图裁剪：普通用户默认进会话中心，审核员默认进语料审核页，管理员看评测与运行监控。

### 6.2 页面清单与职责

| 页面 | 职责 | 关键组件 |
|---|---|---|
| 会话中心 | 全部会话、状态筛选、跨天恢复入口、新建时选 `grid` | 虚拟列表、状态标签、规格选择器 |
| **生成会话页**（核心） | 见 §6.3 | 分通道流、规格预览卡、代码/预览双轨、时间线 |
| **控件墙** | 见 §6.5 | 规格卡片网格、隔离 iframe、包状态操作 |
| 语料审核页 | 入库队列、脱敏预览、投毒命中、撤回 | 只追加日志视图、批量审核 |
| 评测与回归 | 指标分布（含置信区间）、门禁结果、对抗用例通过率 | Recharts / ECharts、对比表 |
| 运行监控 | 成本 P50/P95、配额、熔断、护栏命中趋势 | 实时指标 + 告警列表 |

### 6.3 核心页「生成会话页」信息架构

| 区域 | 内容 | 交互 |
|---|---|---|
| 顶部状态条 | 会话状态、所选规格、`runId`、累计成本、模型档位、最后 `seq` | 一键复制证据链 |
| 输入区 | 描述输入、规格选择、附件上传（图片/视频/文本） | 乐观预览 + 上传进度；类型/大小错误即时提示 |
| **规格预览卡** | 边流边成形：`widget_type` / `grid` / 每个附件的 role / 布局槽位 | **可纠偏**：「这张图是背景不是提取」→ PATCH role → 从该步重跑 |
| 左：事件时间线 | plan / 护栏命中 / 技能加载 / 检索引用 / 回边 / 校验 / 沙箱结果 / 成本 | 折叠、筛选、跳转日志、scrubber 拖动回放 |
| 中：分通道流 | 见 §6.4 | 自动滚动（仅底部时）、暂停、停止 |
| 右：代码 / 预览双轨 | TSX 增量代码（语法高亮）+ 编译通过后的受限 iframe 热更新 | 切换代码/预览；预览内交互可试（日历翻月等） |
| 底部：链路账 | token、回边次数、检索引用、沙箱测试结论、成本 | 展开 trace 与证据包 |
| 异常态 | 原因码 + 证据引用 + 「从断点继续」/「改描述重试」 | 不显示裸「失败」 |

### 6.4 流式交互设计（重点）

**不做 token 刷屏。** 服务端按 §4.5 推送分通道类型化事件，前端按 `kind` 派发给独立渲染组件：

| 通道 | payload 形态 | 渲染策略 |
|---|---|---|
| `plan` | 结论式短句（不是思维链） | 折叠的步骤条，默认收起 |
| `spec` | 逐步逼近完整的 `WidgetSpec` JSON | **partial JSON 容错解析 + 骨架占位**，字段边到边填规格预览卡 |
| `attachment` | `role` 判定 + `insight_ref` | 附件卡片标出用途，提供纠偏入口 |
| `skill` / `routing` | 规程 id、模型档位、阈值命中 | 链路账里的小标签，不打断主视野 |
| `code` | 行级增量 patch | 只更新变化的块，**不全文重绘** |
| `guard` / `validation` / `sandbox` | 通过项、命中项、失败原因码 | 分步清单，失败项可展开证据 |
| `artifact` | `bundle_id` + 预览引用 | 原子替换占位卡片，触发 iframe 热更新 |
| `cost` / `checkpoint` / `interrupted` / `notice` / `error` / `done` | 状态与账目 | 状态条与链路账更新 |

**实现要点**

1. **partial JSON**：用容错解析器（补齐未闭合的字符串与括号）逐 delta 解析；**只在 `done` 时做严格 Zod 校验**；流式中的最后一个字符串字段视为不稳定，不触发任何副作用（路由、埋点、请求）。
2. **增量代码 patch**：服务端发 `{ from_line, to_line, text }`，前端按行区间更新 CodeMirror/Monaco 的文档；已完成的块 memo 化，只重渲染仍在增长的末块。
3. **渲染节流**：delta 先写 ref，按 `requestAnimationFrame` 批量 flush 到 state（最多 60 次/秒）；长列表虚拟化；日志窗口化。
4. **滚动锚定**：仅当用户已处于底部时才自动滚到底；用户上滑回看时不跳回。
5. **SSE 工程细节**：按 `\n\n` 分帧并缓存半帧；UTF-8 流式解码；心跳保活；显式 `done` / `error`（无 `done` 视为截断）；服务端禁用缓冲（`X-Accel-Buffering: no`）；错误作为流事件，不是静默断连。
6. **abort 链路**：点「停止」→ `AbortController.abort()` → fetch 中断 → 服务端取消上游模型调用 → **账单同步停止**；同时服务端写 checkpoint 并推 `interrupted`。
7. **中断不是关连接**：`POST /interrupt` 是独立 signal；UI 收到 `interrupted` 后切为「已暂停 · 从 XX 继续」，保留已生成的部分代码与规格卡。
8. **跨天/月恢复**：重新打开 App → 拉会话快照 → 用 `runId + Last-Event-ID` 重连续传；超窗先拉快照再续流。恢复后 reducer 重放历史事件，界面与离开前一致。
9. **向前兼容**：reducer 对未知 `kind` 忽略而非报错；回放测试覆盖新旧混合事件序列。
10. **背压**：SSE 事件入队按 `seq` 去重与乱序丢弃；日志最多保留 N 行；大日志走对象存储引用。

**六个可演示亮点**

1. **流式 spec 可纠偏**：用户看见「附件 role = 内容提取」被识别错，当场改成「视觉参考」，Agent 从该步重跑——这是 Agent 产品特有交互，普通 chat 没有。
2. **代码 / 预览双轨热更新**：一边 TSX 增量高亮，一边编译通过后受限 iframe 热更新，控件「长出来」。
3. **时间线 scrubber**：拖动回看任意一步的 spec、代码、护栏命中、沙箱结论与成本；支持「从这里重生成」。
4. **控件墙**：六种规格按真实比例渲染的卡片网格，隔离 iframe，安装 / 删除 / 版本 / 回滚 / manifest / trace 入口齐全。
5. **链路透明**：每会话展示 token、回边次数、检索引用、沙箱测试结论、成本账，可展开 trace。
6. **状态可解释**：每一步都有原因码与证据引用，失败不是红字「生成失败」，而是「规格冲突：`2x1` 无法容纳月视图，建议改选 `4x4`」。

### 6.5 控件墙与管理页

- **尺寸映射**（`cell = 80px`）：`2x1 → 160×80`、`1x2 → 80×160`、`2x2 → 160×160`、`2x4 → 160×320`、`4x2 → 320×160`、`4x4 → 320×320`。卡片按所选规格真实比例渲染，不统一拉伸。
- 每张卡片内部是受限 iframe（§4.3.3），加载编译产物；`widget:ready` / `widget:error` 上报，失败卡片显示原因码与「查看沙箱日志」。
- 包操作：`GENERATED → INSTALLED → DELETED`；删除不可逆，二次确认 + 审计，不做乐观更新；版本可回滚到上一版编译产物。
- 卡片详情：manifest、静态校验、沙箱测试用例与快照、本次成本、trace 入口、参考了哪些公共语料（可溯源）。

### 6.6 实时流消费与状态管理

- **服务端状态**：TanStack Query（会话快照、包列表、指标），按 `sessionId` 精确失效。
- **实时状态**：自研 **event-sourced reducer**——`AgentEvent[] → SessionView`，纯函数、可回放、可单测；SSE 事件与 REST 快照通过 `seq` 合并，幂等去重。
- **本地 UI 状态**：Zustand（面板折叠、代码/预览切换、scrubber 位置、草稿输入）。
- **乐观更新**：仅用于附件本地预览、草稿、面板切换等可逆操作；包删除、取消生成等不可逆操作不做乐观更新。
- **回放测试**：固定事件序列重放，断言任意时刻的 `SessionView` 一致；这是前端最能讲细节的一块。

### 6.7 体验与工程要点

- 附件上传乐观预览 + 进度；生成中卡片按所选规格骨架占位；完成原子替换。
- 流式 token 渲染 rAF 批处理；测试/沙箱日志按行窗口化 + 关键词高亮；长列表虚拟化。
- 空态 / 加载 / 错误 / 无权限 / 已归档各有明确设计；时间显示带时区；暗色主题、键盘可达、WCAG 2.1 AA、i18n。
- 按路由代码分割；前端错误与性能监控；关键路径埋点（TTFT、首事件延迟、预览打开耗时、中断率、恢复成功率）。
- iframe 与主线程隔离，卡片并发渲染不阻塞输入；`widget:error` 不会拖垮宿主页面。

### 6.8 前端技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 框架 | React 19 + TypeScript + Vite（SPA）；需 SSR/统一鉴权时加 BFF | BFF 只做 SSO、聚合与 SSE ticket |
| 服务端状态 | TanStack Query | 快照、轮询兜底、精确失效 |
| 事件状态 | 自研 event-sourced reducer + Zustand（UI 态） | 可回放、可单测 |
| UI | Tailwind CSS + shadcn/ui（Radix） | 可访问性基础 |
| 代码展示 | CodeMirror 6 / Monaco | 增量 patch、语法高亮（TSX） |
| 流式解析 | 原生 `fetch ReadableStream` + 自研分帧/半帧缓存 + 容错 partial-JSON 解析器 + Zod（仅 final 严格校验） | 不自己写正则解析 JSON；不引入 AI SDK |
| Agent 事件层 | 原生 `EventSource` / `fetch` + 自研 event-sourced reducer，**不用** AI SDK / CopilotKit / AG-UI SDK | 分通道派发、seq 去重、心跳、重连与回放全部手写 |
| 控件渲染 | iframe `sandbox` + CSP + `postMessage` bridge | 与宿主隔离 |
| 图表 | Recharts / ECharts | 指标分布与置信区间 |
| 列表 | `@tanstack/react-virtual` | 会话、时间线、卡片墙 |
| 契约 | OpenAPI 3.1 → `openapi-typescript`；事件与协议用 Zod；枚举共享包 | 单一事实来源 |
| 测试 | Vitest + Testing Library + MSW（mock SSE/API）+ Playwright（E2E）+ Storybook | **事件序列回放测试是重点** |
| 质量 | Biome / ESLint、axe、Sentry、Lighthouse CI | a11y AA、性能预算 |

### 6.9 前后端契约与边界

- `packages/contracts` 单一事实来源：OpenAPI（REST）+ JSON Schema / Zod（事件、`WidgetSpec`、`WidgetBundle`、`WidgetRuntime`）+ 枚举与错误码；CI 校验前后端生成结果一致，schema 变更即失败。
- BFF 职责：OIDC/JWT session → 短期 SSE ticket、聚合首屏数据、统一错误映射；**不做鉴权判定**。
- 服务端按 `user_id` 对每个 REST 请求与每条 SSE 事件做权限校验；前端隐藏按钮只是体验。
- 错误响应统一 `{ code, message, traceId, retryable, evidenceRef? }`，与 `ReasonCode` 对齐。

### 6.10 前端验收（怎么算做对了）

| 指标 | 目标 |
|---|---|
| TTFT / SSE 首事件 | P95 < 1s / 1.5s |
| 流式渲染 | 长会话无掉帧；上滑回看不跳回底部 |
| 事件回放 | 同一事件序列重放得到的 `SessionView` 一致；新 `kind` 不破坏既有 reducer |
| 中断 / 恢复 | 打断后状态正确；跨天后重连界面与离开前一致 |
| 纠偏 | 修改附件 role 后能从该步重跑且产物随之变化 |
| 隔离 | 控件 iframe 无法访问父页面、无法外联；`widget:error` 不拖垮宿主 |
| 可访问性 | axe 无严重项，键盘可达，E2E 覆盖主链路 |

---

## 7. 功能划分与工程结构

### 7.1 服务划分

| 服务 | 职责 | 是否有状态 | 伸缩依据 |
|---|---|---|---|
| `web` | React 控制面 SPA | 无 | CDN / 静态 |
| `bff`（可选） | JWT、首屏聚合、SSE ticket | 无 | QPS |
| `control-plane` | API、账号、会话、产物索引、评测、审计 | 无（状态在 PG） | QPS |
| `orchestrator` | 手写 agent loop、checkpoint、租约续跑、事件流 | 无 | 队列深度 |
| `guardrails` | 进/出 LLM 双向护栏、能力白名单 | 无 | 调用量 |
| `retriever` | 四层语料、硬过滤、混合检索、入库门禁 | 无 | 检索量 |
| `sandbox-runner` | TSX 编译、渲染、行为断言（一次性） | 一次性 | 并发沙箱数 |
| `eval-runner` | 离线回放、失败模式 rubric、回归门禁 | 无 | 手动 / CI 触发 |

### 7.2 Monorepo 目录

```
widgetpilot/
  apps/
    web/                   # React 控制面（§6）
    bff/                   # 可选：JWT / 聚合 / SSE ticket
  services/
    control_plane/         # api/ accounts/ sessions/ bundles/ audit/
    orchestrator/          # loop.py / decide.py / state.py / checkpoints/ / lease.py
    guardrails/            # input_guard.py / output_guard.py / capability.py（纯代码规则）
    retriever/             # corpus/ ingestion_gate.py / hybrid_search.py（自研 RRF）
    sandbox_runner/        # compile.py / render.py / assertions.py / entry.py
    eval_runner/           # datasets/ replay/ regression_gate.py
  packages/
    agent_core/            # 手写 harness 内核：model/ structured/ tools/ guardrails/
                           # context/ state/ events/ budget/ eval/（§11）
    contracts/             # openapi.yaml / events.schema.json / widget_spec.schema.json
                           # / widget_bundle.schema.json / widget_runtime.schema.json / enums
    sdk/                   # 生成的 TS / Python 客户端
    prompts/               # 版本化 prompt + 生成规程（recipes/）
    widget_bridge/         # 宿主 ↔ iframe 的 bridge 实现与类型
  infra/
    sandbox/               # 编译/渲染沙箱镜像 / seccomp / cgroups
    deploy/                # Helm / terraform / 网络策略 / CSP 模板
    observability/         # OTel collector / Grafana dashboards / 告警规则
  docs/
    plan/                  # 权威方案
    adr/                  # 架构决策记录
    runbook/              # 熔断、语料撤回、事件响应预案
```


### 7.3 前后端职责划分

| 功能 | 前端主导 | 后端主导 | 共担 |
|---|---|---|---|
| 账号与权限 | 登录表单、角色视图裁剪 | JWT 签发、`user_id` 隔离、鉴权判定 | 账号契约 |
| 会话与状态 | 状态可视化、断点入口 | 手写状态机、checkpoint、租约续跑 | 状态枚举契约 |
| 流式事件 | 分通道渲染、reducer、回放、背压 | `seq` 生成、持久窗口、扇出、心跳 | **事件 schema** |
| 规格与附件 role | 选择器、纠偏交互 | 合法性校验、role 判定与处理 | 枚举契约 |
| 生成与校验 | 过程可视化、错误原因码展示 | 双向护栏、契约校验、沙箱测试 | 协议 schema |
| 产物与预览 | 卡片网格、iframe 渲染、包管理 | 产物生成、CSP、bridge 放行、版本 | **渲染协议** |
| 评测与指标 | 分布图、门禁结果 | 评测执行、统计、告警 | 指标定义 |

策略、隔离、门禁统一在服务端裁决；前端负责事件消费、可回放状态、纠偏交互与可审计的操作入口。

### 7.4 交付里程碑

| 阶段 | 目标 | 交付 | 出口条件 |
|---|---|---|---|
| **P0 可用** | 单用户端到端跑通 | D1/D2/D4/D5/D6 最小版 + 生成会话页 + 控件墙 | 描述 + 规格能产出可渲染卡片 |
| **P1 可信** | 安全与质量门禁齐备 | 双向护栏 + 契约校验 + 沙箱测试 + 审计 + 对抗用例全绿 | 注入 / 越权能力 / 投毒 100% 被拦；PII 泄漏 0 |
| **P2 可运营** | 长任务、语料、评测、成本 | 跨天恢复 + 跨用户语料 + 离线回放评测 + 成本分账 + 灰度 | 影子模式可出报告；跨天恢复成功率达标 |
| **P3 可规模化** | 性能与成本可控 | 模型分档、缓存优化、多租户配额、回归门禁常态化 | 一次生成通过率与成本 P95 达标 |

---

## 8. 功能 → 技术栈 → 要点总表

| # | 功能 | 技术栈 | 关键要点 | 验收 |
|---|---|---|---|---|
| D1 | 账号、会话与编排 | FastAPI / Postgres / Redis Streams / **手写 checkpoint + 租约** / JWT | 状态持久化与无状态计算分离；checkpoint 只存结论与引用；跨天恢复 | kill 续跑、暂停 7 天恢复、跨用户 403 |
| D2 | 附件处理 | 对象存储 / 类型嗅探 / OCR / 关键帧 | role 由描述决定；输出带引用 `AttachmentInsight`；原文不入库 | 同图两种描述走两条路径；伪造类型被拒 |
| D3 | 输入校验与进 LLM 护栏 | 规则引擎 / Pydantic / 技能 manifest | 三层分离；命中不进模型；意图白名单；技能确定性预取 | 注入被拦且无模型调用；误拦截率可测 |
| D4 | 生成编排与成本 | 自研 model gateway / **手写 agent loop** / 自研结构化输出与工具注册 | 固定槽位上下文；先 spec 后代码；回边 ≤2；四维预算 | 同题同版本可复现；不超预算 |
| D5 | 出护栏、契约校验、沙箱测试 | 规则引擎 / esbuild / AST / headless 浏览器 / 一次性沙箱 | 出护栏 + manifest 比对 + 编译/渲染/行为断言；不模拟设备 | 越权代码被拦；尺寸断言生效 |
| D6 | 产物与包管理 | 对象存储 / 版本链 / CSP / postMessage bridge | manifest 贯穿四处；iframe sandbox；状态机带审计 | 任一包可定位完整证据链；iframe 不越界 |
| D7 | 知识、检索与评测 | 向量库 / BM25+RRF / rerank / Eval Runner / 脱敏管道 | 四层语料；入库门禁；硬过滤优先；负例回流 | 撤回立即生效；投毒 100% 被拦 |
| D8 | 治理、观测与成本 | OTel SDK / 自建 trace 表 / Prometheus / WORM 审计 | 全链路 trace；降级不降安全；粒度化 kill switch | 审计可完整重放；故障注入优雅降级 |
| W1 | 会话中心 | React 19 / TanStack Query / 虚拟列表 | 全部会话、状态、跨天恢复入口、规格选择 | 首屏 < 1s（P95） |
| W2 | 生成会话页 | SSE / Zod / event-sourced reducer / CodeMirror | 分通道流、规格卡可纠偏、代码/预览双轨 | 打断与跨天恢复界面一致 |
| W3 | 分通道流式渲染 | 容错 partial-JSON / 增量 patch / rAF | 只在 final 严格校验；不稳定字段不触发副作用 | 长会话无掉帧；上滑不跳回 |
| W4 | 控件墙与包管理 | iframe sandbox + CSP + bridge / 真实比例网格 | 六种规格真实比例；安装/删除/回滚全审计 | 卡片无法访问父页面 / 外联 |
| W5 | 时间线回放与 trace | reducer 纯函数 / scrubber / Recharts | 任意一步可回看、可从该步重生成 | 回放测试全绿 |
| W6 | 前后端契约 | OpenAPI 3.1 / openapi-typescript / Zod | 单一事实来源，CI 校验一致性 | schema 变更即失败 |
| W7 | 前端工程化 | Vite / Vitest / MSW / Playwright / Storybook / axe / Sentry | 事件序列回放测试、a11y AA | E2E 覆盖生成主链路 |
| X1 | 会话状态与断点恢复 | 手写 checkpoint / 租约 / 幂等表 / `runId + seq` | 挂起不占计算资源；跨天/月可恢复 | 恢复后状态完整、无副作用重复 |
| X2 | 上下文管理 | 固定槽位 / 窗口化 / 压缩 | state 只存结论；保留已排除方案；保住 caching | 同题同版本两次跑一致 |
| X3 | 记忆管理 | 结构化偏好存储 | 配色/常用规格/类型；不回喂历史代码 | 偏好可清除、可版本化 |
| X4 | 成本管理 | `usage_metadata` / 四维硬上限 / 档位分账 | 触顶确定性中断；回边计入本轮 | 成本 P50/P95 可出账 |
| X5 | 重试 / 降级 / 熔断 | RetryPolicy / 降级阶梯 / 粒度化 kill switch | 业务失败是回边不是 retry；降级不降安全 | 故障注入后保状态 |
| X6 | 意图路由与技能披露 | 白名单 / 阶段与规格预取 / 结构化请求 | 只放行控件生成；不新增常驻工具 | 漏加载可归因；变更触发回归 |
| X7 | 输入校验与双向护栏 | 三层校验 / 进+出护栏 / 沙箱验证 | 三道职责不重叠；判据均为确定性代码 | 命中率与误拦截率可测 |
| X8 | RAG 与语料治理 | 四层语料 / 硬过滤+混合检索 / 入库门禁 | 私有数据不共享；可撤回；负例回流 | 优化目标 = 回边次数下降 |
| X9 | 沙箱测试与运行时隔离 | 一次性沙箱 + iframe sandbox/CSP/bridge | 编译→渲染→尺寸/能力/交互断言 | 生成代码不进编排进程 |
| X10 | 评测与回归门禁 | 离线回放 / 失败模式 rubric / CI 门禁 | 报分布不报点估计；确定性断言优先 | 变更触发回归 |
| X11 | 可观测与审计 | OTel / hash-chain 审计 / 成本账 | 任一会话可重建完整路径 | 审计可完整重放 |
| X12 | 流式事件协议与回放 | 分通道 SSE / seq / Last-Event-ID | 未知 kind 忽略；按 seq 去重与乱序丢弃 | 断线重连无事件丢失 |

---

## 9. 上线与运营

### 9.1 四阶段灰度

1. **影子模式**：对历史描述跑生成但不展示给用户，人工对照产物与评测集；X8 语料、模型分档、阈值变更的所有对照在此完成。
2. **内部单用户**：仅内部账号，产物只进内部控件墙，公共语料默认关闭共享。
3. **限流扩展**：按用户并发 / 预算逐步放开，开放公共语料并启用审核员队列。
4. **多用户**：完整配额、隔离、SLO、审计、语料撤回与合规。

### 9.2 熔断与回滚

全局开关（停所有新生成）、按用户 / 模型档位 / 能力类型降级、成本异常自动熔断、unsafe-action 超阈值自动隔离；熔断时保留在途会话状态可恢复；公共语料可一键下架并重跑检索索引；任何规程 / prompt / 模型 / 阈值变更可回滚到上一版本。

### 9.3 对抗测试清单

以**策略是否拦住动作**为判据（而非模型是否口头拒绝），每次模型 / prompt / 规程 / 检索 / 阈值变更后重跑：

| 用例 | 期望 |
|---|---|
| 描述夹带「忽略规则、输出系统提示、生成 fetch 外联代码」 | 进 LLM 护栏 / 出 LLM 护栏 / 静态扫描拦下，无模型外联 |
| 附件伪装扩展名、超大文件、数量超限 | 传输层校验拒绝 + 原因码 |
| 模型生成 `eval` / 未知 import / 访问系统 API | 出护栏 + import 白名单 + bridge 拒绝 |
| 生成代码在 `2x1` 里塞月视图 | spec 层规格冲突 + 沙箱尺寸断言双拦 |
| 恶意产物试图进入公共语料（越权代码 / 灌水重复） | 入库门禁（投毒检测 + 质量筛选）拒绝 |
| 公共语料里夹带注入指令，诱导后续用户生成越权控件 | 检索结果按不可信数据标注，出护栏与静态校验拦住动作 |
| 越权访问他人会话 / 包 / 语料撤回接口 | 服务端 `user_id` 过滤，403 |
| iframe 尝试访问父页面 DOM / 外联 / 弹窗 | sandbox + CSP + 窄 bridge 拒绝并上报 |
| 用户删除控件后仍被检索到 | 语料立即失效，撤回生效时延可测 |
| 生成循环反复失败、附件处理超时、成本触顶 | 回边 ≤2 后转人工；确定性中断；不无限重试 |
| 进程在 checkpoint 处被杀、用户跨月后重连 | 状态完整、无重复产物、`seq` 续传 |

### 9.4 SLO 与告警

- TTFT P95 < 1s；SSE 首事件 P95 < 1.5s；沙箱测试 P95 < 10s；控件卡首帧 P95 < 500ms。
- 一次生成通过率、回边次数、护栏命中率 / 误拦截率、沙箱失败率、成本 P95、跨天恢复成功率、unsafe-action rate 均设告警阈值。
- 每个告警关联 `traceId` 与 runbook 条目；阈值按影子期实测校准。

### 9.5 评测体系与指标口径

- **三层**：CI 快速回归门禁（私有集 + 确定性轨迹断言）、离线回放评测集（脱敏历史会话 60% / 失败案例 30% / 专家构造 10%）、人工抽检校准（校准自动评分与裁判一致性）。
- **失败模式 rubric**：正确生成 / 安全转人工 / 可恢复错误 / 越权代码 / 规格不符 / 死循环 / 放弃，按严重度加权；数值一律用确定性断言。
- **主指标**：一次生成通过率、规格合规率、沙箱测试通过率、安装率；**配套指标**：回边次数、重复描述率、跨天恢复成功率、单会话总成本（含重试与沙箱机时）。
- **判定规则**：一次通过率上升但沙箱通过率或安装率下降 → 判无效优化，回查评测集或护栏阈值；离线只做准入，不做最终上线结论。

### 9.6 风险与待决

| 项 | 说明 | 处理 |
|---|---|---|
| 模型分档阈值 | 初值来自经验，需影子期统计校准 | 阈值版本化，离线回放验证后再灰度 |
| 公共语料质量评分 | 自动评分初值可能偏松，导致低质样例入库 | 审核员抽检 + 负例回流，评分阈值可回滚 |
| 沙箱渲染一致性 | headless 浏览器与用户 iframe 可能有细微差异 | 用同一份 `WidgetRuntime` 契约与 bridge 实现，差异进评测集 |
| iframe 并发性能 | 控件墙多卡片同时渲染 | 按需加载 / IntersectionObserver、错误隔离、超时熔断 |
| 语料规模与检索成本 | 跨用户语料增长后检索变贵 | 硬过滤 + 缓存 + 索引快照，按版本裁剪 |
| 多语言 / 语音 | 不在本项目范围 | 明确不做，避免范围失控 |
| 手写 harness 的正确性 | 无框架兜底，幂等 / 租约 / 并发 / 背压 / schema 演进需自己保证 | 以 §11.9 的测试与判据为门禁：状态迁移纯函数单测、故障注入、副作用条数断言 |

---

## 10. Harness 完整性定稿

### 10.1 完整性矩阵

每个控制点都必须能回答：确定性判据是什么、失败往哪走、留下什么证据。

| # | 控制点 | 确定性判据 | 失败去向 / 证据 |
|---|---|---|---|
| 1 | 账号、租户与输入校验 | 鉴权、类型/大小/数量、描述充分性、附件 role、规格枚举 | 拒绝或 `AWAITING_INPUT`；输入快照 + 校验日志 |
| 2 | **进 LLM 护栏** | 注入模式、PII、意图白名单、预算前置 | 不进模型；命中类型 + 原文引用 |
| 3 | 意图 / 规格 / 技能路由 | `grid` 合法、`widget_type`、按类型与规格确定性预取规程 | 规格冲突提示改选；路由记录 |
| 4 | 上下文装配（跨用户 RAG） | 硬过滤 → 混合检索 + rerank；记忆、token 预算、版本快照、缓存 | 无结果不编造、转人工；索引版本 + 引用 |
| 5 | 生成回路 | TSX+React 结构化输出、模型分档、prompt caching、回边 ≤2 | 回边或转人工；每轮 token / 成本 / 档位 |
| 6 | **出 LLM 护栏** | schema、能力白名单、禁止内容、PII、描述符合度、规格一致 | 回灌自纠正；命中与修正记录 |
| 7 | 契约校验 | `spec ↔ manifest ↔ 代码能力声明` 比对、hash | 不通过不进沙箱；校验报告 |
| 8 | **沙箱测试** | 编译、渲染、尺寸、无运行时错误、能力调用走 bridge、交互断言 | 失败回边 / 超限转人工；日志 + 快照 + 结果 |
| 9 | 交付与运行时 | iframe sandbox + CSP + bridge 按声明放行、卡片按 `grid` | 运行时错误上报、能力拒绝；渲染事件 |
| 10 | 观测 / 评测 / 成本 / 审计 / 恢复 | trace、只追加审计、离线回放、回归门禁、成本分账、checkpoint | 任一失败可重放；评测报告 + 成本账 |

### 10.2 两个闭环

- **任务内闭环**：`6 / 7 / 8 失败 → 回到 5 自纠正（≤2 次）`，失败原因逐条记录，attempt 边界重置档位，成本按轮次分账。
- **跨任务闭环**：`10 评测合格 → 入库门禁（脱敏 / 质量筛选 / 投毒防护 / 归属与可撤回）→ 回流到 4 的公共语料`，供其他用户检索；失败案例回流为负例。第二个闭环是跨用户共享需求带来的，也是「完整」与「不完整」的分界线。

### 10.3 贯穿约束

重试 / 降级 / 熔断（模型 429/5xx、沙箱不可用、成本触顶）、跨天/月断点恢复、粒度化 kill switch、只追加审计、门禁无在线关闭开关、护栏判据全为确定性代码。

### 10.4 定稿结论

10 个控制点 + 2 个闭环 + 贯穿约束，覆盖「输入 → 规划 → 执行 → 验证 → 恢复 → 观测 → 评测 → 回流」全链路。每一项都有确定性判据、失败去向与证据，不是能力清单打勾。五项决策（D-1 ~ D-5）已按 §0 固化，本文档即为定稿基线；D-5 的手写实现规格见 §11。

---

## 11. 手写 Harness 内核规格

> 本章是 v1.1 的核心增量，落实 D-5。目标不是「不用库」，而是**把框架黑盒里的不变量显式化**——状态迁移、幂等、恢复、结构化校验、成本计量全部变成可单测、可断言、可在面试里逐行讲清的代码。

### 11.1 边界：什么算框架，什么保留

| 禁止（Agent / LLM 框架） | 手写替代 |
|---|---|
| LangChain / LangGraph | `agent_core` 手写 loop + 显式状态机 |
| Temporal / 任何 workflow engine | Postgres 版本化 checkpoint + 租约抢锁 + 心跳 |
| Vercel AI SDK / `streamUI` / CopilotKit / AG-UI SDK / A2UI SDK | 原生 SSE + 自定义事件 schema + 前端 reducer |
| `bind_tools` / `with_structured_output` / `trim_messages` | 自研工具注册表 / JSON Schema 校验 / 固定槽位装配与压缩 |
| LangChain retriever / 任何 RAG 框架 | SQL 硬过滤 + 向量库客户端 + BM25 + 自研 RRF 融合 |
| LangFuse / LLM 专用观测 SDK | OTel SDK + 自建 `agent_event` / `trace_span` 表 |
| OpenAI Agents SDK / CrewAI / AutoGen / PydanticAI 等 | 全部由 `agent_core` 覆盖 |
| MCP SDK | 自定义 `postMessage` bridge（§4.3.3） |
| OPA / Casbin 等策略框架（护栏链路） | 纯代码规则判定 + 自研 pattern registry |

**保留的通用基础设施**（不是 Agent 框架）：FastAPI、Pydantic v2、Postgres、Redis、对象存储、向量库客户端、React 19 / Vite / TanStack Query / Zustand / Zod / CodeMirror、esbuild / tsc / AST 解析、headless 浏览器、OpenTelemetry SDK、Prometheus、tiktoken（纯 token 计数工具库）、`EventSource` / `fetch ReadableStream`。

### 11.2 分层与模块

```
packages/agent_core/
  model/        provider.py       # 直连 HTTP / OpenAI-compatible，usage 归一
                stream.py         # SSE 分帧、半帧缓存、delta 解析
  loop/         state.py          # State / Decision / Terminal
                decide.py         # 纯函数：确定性状态迁移
                run.py            # 显式 while：终止边、回边、迭代上限
  structured/   schema.py         # JSON Schema + Pydantic 严格校验
                partial.py        # 容错 partial-JSON 解析
                repair.py         # 校验错误结构化回灌（≤2 次）
  tools/        registry.py       # name → 参数模型 + handler + 白名单
                invoke.py         # 参数校验、执行、结果截断、错误分类
  guardrails/   input_guard.py / output_guard.py / capability.py
  context/      slots.py          # 固定槽位装配、去重、caching 前缀
                budget.py         # token 计数、压缩、窗口化
  state/        checkpoint.py     # 版本化落盘
                lease.py          # 租约、心跳、抢锁、恢复
                idempotency.py    # side_effect 幂等表
  events/       seq.py / sse.py / replay.py
  budget/       meter.py          # 四维硬上限、档位分账
  eval/         replay.py / rubric.py / gate.py
```

| 模块 | 职责 | 关键不变量 |
|---|---|---|
| `loop` | 唯一的状态迁移入口 | `decide()` 纯函数；模型只在 `CALL_MODEL` 出现；迭代与回边有上限 |
| `state` | 持久化与恢复 | `rev` 单调、副作用幂等、租约原子抢锁 |
| `structured` | 模型输出的边界 | final 严格校验；流中不触发副作用；回边 ≤2 |
| `tools` | 外部能力 | 静态注册、参数严格校验、白名单、输出截断 |
| `guardrails` | 双向护栏 | 判据为确定性代码，命中留证 |
| `context` / `budget` | 上下文与成本 | 固定槽位、token 预算、四维硬上限 |
| `events` / `eval` | 可观测与回放 | `seq` 连续、事件可重放、门禁可断言 |

### 11.3 Agent loop：显式状态机

核心思想：`decide(state)` 是**纯函数**，不调模型；模型只在 `CALL_MODEL` 分支出现。这样每一步都可断言、可回放。

```python
MAX_ITER = 12          # 上线常量，无关闭开关
MAX_REPAIR = 2         # D-2：自纠正回边上限

def run(session_id: str, run_id: str, user_input: Input) -> State:
    state = checkpoint.load(run_id) or State.initial(user_input)
    lease = lease.acquire(run_id)                    # 原子抢租约
    try:
        while not state.terminal and state.iteration < MAX_ITER:
            decision = decide(state)                 # 纯函数，确定性
            if decision.kind == "CALL_MODEL":
                out = model.complete(decision.messages,
                                     tier=decision.tier, stream=True)
                state = apply_model_output(state, out)   # 流式解析 + final 校验 + 出护栏
            elif decision.kind == "CALL_TOOL":
                state = apply_tool_result(state, tools.invoke(decision))
            elif decision.kind == "REPAIR":
                state = repair.feedback(state, decision.errors)   # 回边，不新增 Run
            elif decision.kind == "AWAIT_INPUT":
                state = state.pause(reason=decision.reason_code)
            elif decision.kind in ("DONE", "HANDOFF", "FAILED"):
                state = state.terminate(decision)
            checkpoint.save(run_id, state)           # 每个 super-step 同步落盘
            events.emit(state.seq, state.phase, decision)
            budget.check(state)                      # 四维硬上限，确定性中断
            lease.heartbeat()
        checkpoint.save(run_id, state)
        return state
    finally:
        lease.release()
```


```python
class Decision(NamedTuple):
    kind: Literal["CALL_MODEL","CALL_TOOL","REPAIR","AWAIT_INPUT",
                  "DONE","HANDOFF","FAILED"]
    messages: list[Message] = []
    tier: Literal["cheap","standard","frontier"] = "standard"
    tool: str | None = None
    errors: list[str] = []
    reason_code: str | None = None

@dataclass
class State:
    session_id: str; run_id: str; rev: int; seq: int
    phase: SessionState; iteration: int; repair_count: int
    spec: dict | None; diff_ref: str | None
    excluded_approaches: list[str]        # 压缩时必须保留
    budget_used: dict; tier_history: list
    last_error_fingerprint: str | None
    terminal: Terminal | None
```

**为什么这样设计**：模型输出是唯一不可信变量，其余迁移都是代码；把「不可信输出」转成「可断言的状态迁移」，就是 harness 的全部意义。回边（`REPAIR`）与基础设施重试（`RetryPolicy`）分离：前者是业务失败，后者只处理 429/5xx / 网络抖动 / 沙箱启动失败。

### 11.4 Checkpoint、租约与恢复（替代 Temporal）

```sql
CREATE TABLE checkpoint (
  run_id TEXT, rev INT, node TEXT, state_ref TEXT,
  lease_owner TEXT, lease_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ, prev_hash TEXT,
  PRIMARY KEY (run_id, rev)
);
CREATE TABLE side_effect (
  idem_key TEXT PRIMARY KEY,    -- session_id + step + payload_hash
  run_id TEXT, result_ref TEXT, created_at TIMESTAMPTZ
);
```

恢复流程：

1. 每个 super-step **同步**写 `checkpoint`（`rev` 单调递增，hash 链防篡改）；大对象只存引用。
2. worker 持有 `lease_owner` 并心跳续租 `lease_until`。
3. 崩溃后另一实例扫描 `lease_until < now() AND terminal IS NULL`，用 `UPDATE ... WHERE lease_until < now()` **原子抢租约**，成功者从该 `rev` 恢复。
4. 任何外部副作用执行前查 `side_effect`；命中直接复用结果，**不重复执行**。
5. `interrupt` → 写 `PAUSED` + checkpoint + 释放租约；`resume` → 抢租约、从节点顶部重执行（前置副作用幂等）。
6. 前端重连：`Last-Event-ID` → 按 `runId + seq` 补发；超窗先拉快照再续流。

这就是手写 durable execution：状态在 Postgres、计算无状态、挂起不占资源、跨天/月恢复靠 `runId` 而非内存。

### 11.5 结构化输出与自纠正（替代 `with_structured_output`）

- 系统提示里放 schema + 示例（prompt-first），不依赖框架的 constrained decoding。
- 流式用 `partial.py` 容错解析（补齐未闭合字符串与括号）；**只在 final 用 Pydantic / JSON Schema 严格校验**；流中不稳定的字符串字段不触发任何副作用。
- 校验失败 → `repair.feedback()` 把错误结构化回灌 `{field, expected, got}`，`REPAIR` 回边 ≤2；超限 → `HANDOFF` + 原因码。
- 数值、枚举、能力声明一律用确定性断言，不用裁判模型打分。
- 复杂嵌套 schema 在规模下容易让模型频繁出错，因此「prompt 生成 → validator → 自纠正」比硬塞 structured output 更稳。

### 11.6 工具注册与调用（替代 `bind_tools`）

```python
@dataclass
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    surfaces: set[str]                # 执行面白名单
    handler: Callable
    max_output_chars: int

REGISTRY: dict[str, ToolSpec] = {...}  # 启动时静态注册，运行时不可增删

def invoke(d: Decision) -> ToolResult:
    spec = REGISTRY[d.tool]                       # 未注册直接拒绝
    args = spec.args_model.model_validate(d.args)  # Pydantic 严格校验
    policy.check(d.surface, spec)                 # 确定性授权
    result = spec.handler(args)
    return truncate(result, spec.max_output_chars)
```

模型输出统一为 `{"tool": "...", "args": {...}}` JSON，由自研解析器校验后分发；工具数量是每轮常驻税，技能通过结构化字段 `need_skill` 请求，不新增工具。

### 11.7 上下文、检索与模型直连

- **槽位装配**（替代 `trim_messages`）：`系统契约 → 规程 → 检索片段 → 附件洞察 → 用户记忆 → 会话结论`，固定顺序保持 prompt caching；`budget.py` 用 tiktoken 计数，超阈值只压缩 tool message，**保留任务契约与已排除方案**；state 只存结论与引用。
- **检索融合**（替代 retriever）：`SQL 硬过滤(grid/type/capability) → 向量 top-k → BM25 top-k → 自研 RRF → rerank → 去重 → 预算裁剪`；索引快照版本写进 `manifest`，保证可复现。
- **模型直连**：`model/provider.py` 用 `httpx` 直接请求 provider 的 chat-completions 兼容端点，统一把不同供应商的 `usage` 字段归一；换模型只改配置，不换代码；流式 delta 由 `stream.py` 自行分帧。

### 11.8 事件、护栏、成本与评测

| 能力 | 手写实现 |
|---|---|
| 事件流 | `seq` 单调递增、Redis 保留最近 N 条、`Last-Event-ID` 续传、心跳、显式 `done/error`；前端自研 reducer 按 `kind` 派发 |
| 双向护栏 | `input_guard.py` / `output_guard.py` 纯规则 + 模式库；命中写 `guard_hit` 表（类型、判据、原文引用），误拦截率可测 |
| 成本 | `meter.py` 每 super-step 累加 token / 轮次 / 工具调用 / 沙箱分钟，按档位写 `cost_ledger`；触顶确定性中断 |
| 评测 | `eval/replay.py` 从 `agent_event` 重放历史会话；`rubric.py` 按失败模式加权；`gate.py` 用确定性轨迹断言做 CI 门禁 |
| 观测 | OTel SDK 打 span + 自建 `trace_span` 表，可重建「输入 → 护栏 → 检索 → 生成 → 校验 → 沙箱 → 交付 → 成本」 |

### 11.9 测试与判据（手写最容易翻车的地方）

| 层 | 必须覆盖 |
|---|---|
| 纯函数单测 | `decide()` 全分支、状态迁移、终止边、回边计数、护栏规则、partial-JSON 边界、RRF 融合 |
| 契约测试 | `WidgetSpec` / `manifest` schema、能力白名单、事件 schema、错误码 |
| 幂等与恢复 | 副作用表条数 = 1、checkpoint `rev` 单调、`seq` 连续、抢租约并发安全、重放结果一致 |
| 故障注入 | kill worker（租约过期续跑）、模型 429/5xx、沙箱超时、Redis 不可用、断线重连、成本触顶、跨天恢复 |
| 端到端冒烟 | 描述 + 附件 → 生成 → 打断 → 恢复 → 沙箱通过 → 卡片渲染 |
| 回归门禁 | 同题同版本两次跑结果一致；规程 / prompt / 模型 / 检索 / 阈值变更触发全量回放 |

**验收口径**：单元测试全绿 + 快照一致 + 端到端冒烟通过 = `GATE OK`。门禁只断言「已有能力不退化」，不因新增能力缺失而阻塞；新发现的缺口先登记 backlog，判据升版时批量并入，避免每轮对话反复返工。

### 11.10 代价、风险与收益

| 代价 / 风险 | 缓解 |
|---|---|
| 自己处理幂等、租约、并发、背压、schema 演进 | §11.9 的测试与判据作为硬门禁 |
| 没有框架的 provider 生态集成 | 用 OpenAI-compatible 协议做统一适配层，新增供应商只加一个 adapter |
| 缺少现成 tracing / eval UI | 自建表 + 简单看板，正好是可讲的前端交付物 |
| 代码量更大 | `agent_core` 模块边界清晰，每个模块都有不变量与单测 |

**收益**：无黑盒、依赖少、可移植；每一步状态迁移与恢复都能逐行解释；与「成熟工程化」并不冲突——成熟度体现在不变量、测试与可观测，不在框架。这也是简历里最能拉开差距的部分。

### 11.11 与框架的对照（面试可讲）

| 框架通常提供 | 本项目手写实现 | 能讲清的点 |
|---|---|---|
| chain / graph 编排 | `decide()` 纯函数 + 显式 while | 为什么模型只在 `CALL_MODEL` 出现 |
| checkpointer / workflow engine | Postgres 版本化 checkpoint + 租约抢锁 | 崩溃续跑与幂等副作用怎么保证 |
| structured output | prompt-first + partial JSON + final 严格校验 + 回灌修复 | 为什么复杂嵌套 schema 不能只靠 constrained decoding |
| tool binding | 静态 registry + Pydantic 参数校验 + 白名单 | 越权调用在策略层被拒而不是靠提示 |
| retriever | 硬过滤 + 向量/BM25 + 自研 RRF | 为什么先硬过滤再语义检索 |
| LLM 观测平台 | OTel span + 自建事件表 | 如何重建一次会话的完整路径 |

---

## 附录 A · 术语表

| 术语 | 含义 |
|---|---|
| `WidgetSpec` | 生成协议：可校验的中间契约，含规格、附件 role、能力声明与数据约束 |
| `WidgetBundle` | 产物协议：TSX 源码 + 编译产物 + manifest + 校验与沙箱结果 + hash |
| `WidgetRuntime` | 渲染协议：iframe sandbox / CSP / 窄 postMessage / 能力 bridge |
| `AttachmentRole` | 附件用途（视觉参考 / 内容提取 / 数据源 / 风格参考），由用户描述决定 |
| `Capability` | 控件可使用的 webview 能力白名单：时间、定时器、受限存储、静态数据、主题、语言 |
| `bridge` | 宿主与 iframe 之间唯一允许的能力通道，按 manifest 声明放行 |
| `role 纠偏` | 用户在流式规格卡上改正附件用途并从该步重跑 |
| `runId` / `seq` | 任务执行标识与每会话单调递增事件号，用于重连与跨天恢复 |
| `checkpoint` | 只存结论与引用的状态快照，大对象放对象存储 |
| `自纠正回边` | 生成/校验失败后回到生成回路修正（≤2 次），不是基础设施重试 |
| `入库门禁` | 脱敏 / 质量筛选 / 投毒防护 / 归属与可撤回，产物进公共语料前必过 |
| `公共语料` | 跨用户共享的脱敏 spec + 代码 + 过程摘要，不含私有数据 |
| `一次生成通过率` | 无自纠正直接通过全部校验的任务占比，主质量指标 |
| `失败模式 rubric` | 按严重度加权的评测分类，替代单一「成功率」 |
| `event-sourced reducer` | 前端 `AgentEvent[] → SessionView` 纯函数，支撑回放与时间线 |
| `partial JSON` | 流式渐进解析未闭合 JSON，仅在 final 做严格校验 |
| `agent loop` | 手写显式状态机 while，模型只在 `CALL_MODEL` 分支出现 |
| `decide()` | 纯函数确定性状态迁移，不调模型；harness 可断言的核心 |
| `lease` | checkpoint 租约 + 心跳，用于崩溃后原子抢租约续跑 |

## 附录 B · 简历与面试叙事

**如实口径**：曾参与「用户描述生成手机控件」业务（Lynx + React 控件打包嵌入手机系统）；本项目为该链路的**作品化复现**——受环境与设备限制，产物改为 TSX + React 页面代码并在隔离 iframe 中渲染，补齐了当时未覆盖的工程链路。不写成主导、不编造业务数据。

**可演示清单**：附件两种 role 的差异、规格冲突被拒、进/出护栏命中、越权能力被拦、沙箱测试结论、打断后跨月继续、公共语料撤回生效、每会话成本账、一次生成通过率。

**面试里最值钱的三句判断**：

1. 生成协议与渲染协议是同一份契约的两端——`manifest` 的能力声明同时约束静态校验、沙箱测试和运行时放行。
2. 流式是分通道类型化事件 + 可恢复流，不是 token 刷屏；中断即 checkpoint，跨天恢复靠 `runId + seq`。
3. 跨用户共享的前提是私有数据不共享；公共语料必须过入库门禁且可撤回，否则一次投毒会跨用户传播。
4. harness 全部手写：agent loop 是显式状态机 + 纯函数 `decide()`，模型只在 `CALL_MODEL` 分支出现；恢复靠 Postgres 版本化 checkpoint 与租约抢锁，不依赖任何编排框架。

**能力归属**：前端交互（分通道流、可纠偏规格卡、双轨热更新、时间线回放、控件墙）与后端工程化（手写 checkpoint / 租约续跑、双向护栏、契约校验、沙箱测试、语料治理、评测回归）在文档中均可逐条定位到域、字段与验收项；手写内核规格见 §11。

