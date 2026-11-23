# change-contract · 认知/反思/模板/Provider 四能力包可组装化（2026-11-24）

> 用户诉求（原话）：「使用意图识别能力、反思能力，现在可以组装吗？本意里这些能力都是可以随意组装的，无论是内置架构，还是用组合式 API 写。期望封装一个 llm 的通用能力包，可以兼容不同厂商的模型，因为 llm provider 是最基础的组件，需要做特别处理。同时需要提示词模板，不期望直接注入 prompt，无论系统还是用户提示词。」

## §1 现状（审计结论，不美化）
- 意图：`CascadeIntentStrategy` 四级真实现 + `Router/RouterSkill` 架构消费；但不可 `use()`，`cheap/primary` 对 `build()` 路径无效，`task/tier` 透传断在 gateway。
- 反思：`Reflection` 架构是真 `loop_until`；`ThresholdReflector` 真策略；但只能作架构构造参数，无能力包。
- 模板：**零实现**（无模板类/插值/依赖）；`system=` 静态字符串；archs 硬编码 prompt。
- Provider：`OpenAICompatLLM` 一个适配器 + `ModelRegistry/ModelRouter` 多后端降级真能跑；但端点表无消费者、无通用 provider 工厂、非 OpenAI 协议缺席。

## §2 Decision（本轮四件事）
- **D1 认知能力包** `CognitionCapability`：包住 `IntentStrategy`（默认 `CascadeIntentStrategy`），履约 Capability 协议（name/version/requires/install）；`Agent.use()` 后真接门面与组合式 API 两条路。修 `build()` 使注入的 `cheap/primary` 在 L3/L4 真被调用；`task/tier/text` 经 `LLMRequest.task_hint/meta` 透传到 `RuleRouter`（U-1 关闭）。
- **D2 反思能力包** `ReflectionCapability`：包住 `ReflectStrategy`（默认 `ThresholdReflector`），`use()` 后注入；`Reflection(reflector=...)` 构造注入保留（非双轨——同一策略对象两条消费路径）；`critique(draft, goal, ctx)` 第三参真传。
- **D3 提示词模板** `PromptTemplate`（system/user 双段 + `string.Template` 变量插值，零新依赖）+ `PromptCapability`（可注入模板注册表）；`Agent(system=)` 接受模板对象；archs 硬编码 prompt 改为经模板能力取，缺省模板与现状等价（无兼容层——同一模块定义单一默认模板集）。用户零直接 prompt 注入：门面/架构都从模板渲染。
- **D4 Provider 能力包** `LLMProviderCapability`：ProviderFactory 协议 + OpenAI-compatible 通用工厂（`register(name, base_url, api_key, model)` → 构造 `OpenAICompatLLM` 实例 → 登记进 `ModelRegistry`）；`ModelCapability(llm=...)` 与 Provider 包组装成"配置→provider→模型能力"一条链。非 OpenAI 原生协议（Anthropic Messages 等）本轮范围外，诚实披露；适配器协议 `LLMPort` 可注入用户自实现。
- **D5 组合式 API 路径**：四能力都能用 `compose` 算子/协议直接在 Program 层组装（如 `branch_value(intent_as_program(...))`、`loop_until(critique→revise)`、`PromptTemplate.render()` 作为 program payload）；不新增第五种"能力"概念。

## §3 Delta（改动面）
- 新：`src/harness/cognition/capability.py`（CognitionCapability）、`src/harness/cognition/reflection_capability.py`（ReflectionCapability）、`src/harness/prompt/`（PromptTemplate/PromptCapability/默认模板集）、`src/ports/adapters/provider.py`（LLMProviderCapability + OpenAI-compatible 工厂）。
- 改：`cascade.py`（build 消费 cheap/primary）、`gateway.py`（task_hint/meta 透传）、`router.py`（tier/task 路由）、`recipes.py`（Reflection ctx + 模板取法）、`api/agent.py`（system 模板化 + 能力配送）。
- 删：无（本轮不删既有 API）。
- R1（src/runtime/）零改动（D-7 硬约束）。

## §4 Acceptance（每条必须有失败测试先行证据）
- AC-1 `use(CognitionCapability(strategy=...))` 后门面/组合式 API 真调注入的意图策略，输出改变。
- AC-2 注入 `cheap/primary` 后 Router 的 L3/L4 真调用注入部件（计数断言）。
- AC-3 `task="intent", tier=cheap/primary` 经 gateway 到 router 决策（低成本模型被选中）。
- AC-4 `use(ReflectionCapability(reflector=...))` 后 Reflection 架构真调注入 reflector（含 ctx 第三参）。
- AC-5 PromptTemplate 变量渲染 system/user 双段，中文变量不丢；门面/架构不再接收裸 prompt 字符串作为主要入口。
- AC-6 LLMProviderCapability 两个不同 base_url 配置构造两个可解析 provider，ModelCapability 按 registry 切换真跑（离线 MockTransport）。
- AC-7 全量 pytest/mypy strict/purity strict/模块 gate 绿；demo 展示四包组装。

## §5 Open（诚实边界）
- 非 OpenAI-compatible 原生协议（Anthropic/Gemini 原生）本轮不实现；用户可实现 LLMPort 注入。
- LLM 密钥只走构造参数/环境变量，不落文件、不进模板。
