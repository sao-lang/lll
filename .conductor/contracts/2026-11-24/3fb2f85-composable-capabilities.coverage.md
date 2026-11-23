# 需求覆盖矩阵 · 四能力包可组装化（2026-11-24）

| 需求（用户原话） | 决策 | AC | 状态 |
|---|---|---|---|
| "意图识别能力…可以随意组装" | 采用 CognitionCapability + 组合式桥 | AC-1/2/3 | 待实现 |
| "反思能力…可以随意组装" | 采用 ReflectionCapability + 已有 Reflection 架构 | AC-4 | 待实现 |
| "无论是内置架构，还是用组合式 API 写" | 采用双路径消费同一策略对象 | AC-1/4 | 待实现 |
| "封装一个 llm 的通用能力包，兼容不同厂商模型" | 采用 LLMProviderCapability + OpenAI-compatible 工厂（协议可注入自定义） | AC-6 | 待实现 |
| "llm provider 是最基础的组件，需要做特别处理" | 采用独立 Provider 包与 ModelCapability 分层装配 | AC-6 | 待实现 |
| "需要提示词模板，不期望直接注入 prompt" | 采用 PromptTemplate + PromptCapability（system/user 双段渲染） | AC-5 | 待实现 |
| "无论系统提示词还是用户提示词" | 采用双段模板 + role 绑定 | AC-5 | 待实现 |
| 非 OpenAI 原生协议（Anthropic/Gemini） | 范围外（诚实边界） | — | 协议可注入自实现 |
| 密钥落文件/模板 | 否决（安全红线） | — | — |
