# 需求覆盖矩阵 · 架构层契约兑现（用户原话 → 决策 → FR → 验收）

> 需求列逐条抄用户 14:36~15:20 的原话；四层契约 = 可插拔 / 组合 / 门面 / 端到端。

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| "message等数据不暴露出来" | 采用：语义门面 say()/run(text)，messages/effect/payload/ctx 全部内部化 | FR-1 | AC-1 门面公开方法签名零 message/effect/payload/ctx 类型；say() 可跑通 |
| "ReAct由组合式API封装成" | 采用：ReAct.compile 用 compose.program 算子组合，不再手搓 yield | FR-2 | AC-2 static_check + 源码断言 ReAct 复用 seq/loop/map_effect 等算子 |
| "组合式API可以让用户封装内置没有的架构，要FP" | 采用：算子是 ProgramFn→ProgramFn 纯变换，公开给用户封装自定义 Arch | FR-3 | AC-3 用户自定义架构用公开算子组合，跑通且零框架改动 |
| "框架的核心原则之一是可注入，记忆管理等功能应该是用户自己实现的，框架只不过内置了一个默认实现" | 采用：三层可插拔，默认实现可替换，不强制用户用 | FR-4 | AC-4 注入自定义 MemoryPort 后框架消费它；不注入用默认 |
| "框架内部就是要做编排" | 采用：语义门面内部完成 ctx 转换/消息归一/usage 搬运，用户零胶水 | FR-5 | AC-5 用户只写 say() 即可，三处胶水不出现在用户代码 |
| "利用框架的组合式API快速搭agent" | 采用：Agent(ReAct(...)).capability(...).build() 一条链跑出结果 | FR-6 | AC-6 零胶水端到端实测：命令+退出码+真实输出 |
| "内置架构就应该由这些组合式API封装起来" | 采用：10 个内置架构全部改为算子组合（非手搓 Effect） | FR-7 | AC-7 10/10 架构 compile 复用算子的源码断言 + 行为等价测试 |
| "有一个组合层，可以让用户注入根据框架接口封装的能力" | 采用：Capability 注入口公开且履约校验 | FR-8 | AC-8 自定义 Capability 注入后其 Port 进 Registry 且被运行期消费 |
| "能力中可以注入根据接口封装的策略和工具" | 采用：策略级 Provider + 工具级 ToolPort 均可注入 | FR-9 | AC-9 注入自定义策略/工具各一条实测 |
| "这些接口都有默认实现" | 采用：协议全集逐个补默认实现（含 CheapIntentModel/PrimaryIntentModel 缺口） | FR-10 | AC-10 gate 规则11 扩展核对默认实现覆盖率，缺口清零或有 Open 说明 |
| "就是设计文档中的Registry Capability那些东西" | 采用：R13 四要素（协议+默认+入口+校验）逐点兑现，缺一即假可插拔 | FR-11 | AC-11 所有注入点履约校验 isinstance 生效；非法实现装配期拒绝 |
| "保证质量" / "不要停" | 采用：TDD RED→GREEN、全量门禁、独立 review Blocker=0 | FR-12 | AC-12 pytest/mypy/12门禁/ast/purity 全绿 + review Clean |
| "做全代码检测" / "之前全代码检测为什么没测出来" | 采用：检测源换成对外宣称（__all__/PROFILES/10架构），按用户路径验，不按内部 AC | FR-13 | AC-13 四层用户路径实测清单全部贴真实输出 |
| 红队补漏 B1：零胶水链的终点 API 未定义（build() 只产 Session） | 采用：语义门面 run/say 直接出结果，链终点是门面方法不是 build() | FR-14 | AC-14 一条链门口拿到结果（非 Session），贴真实输出 |
| 红队补漏 B2：文本如何进入 Program 未定义 | 采用：门面内部把输入文本注入架构首轮 messages，不靠用户在 call 里补 | FR-15 | AC-15 say(文本) 后架构首轮 LLM 请求含该文本 |
| 红队补漏 B3：契约②算子公开 vs 契约③不暴露 effect 冲突 | 采用（用户15:39确认方案A）：用户直接用 FP 算子碰 Effect 封装架构；仅 message/payload 数据由门面内部注入不暴露 | FR-16 | AC-16 用户用 compose FP 算子封装自定义架构并跑通，与内置同权 |
| 红队补漏 B4：门面在依赖方向下无法闭环 | 采用（用户跳过，取推荐）：语义门面放 api 层，用户入口=api.Agent；compose.Agent 保留纯装配器；依赖方向不变 | FR-17 | AC-17 门面在 api 层实现，compose 不 import harness/api，依赖扫描通过 |
| 红队补漏 B5：state 算子在真实 Runner 下拿不到 state | 采用：架构算子化只用不读 state port 的算子；读 state 的通过 Program 闭包内 state 传递或显式转换 | FR-18 | AC-18 10 架构算子化后真实 Runner（非 fake state port）跑通 |
| 红队补漏 B6：删除推导零验收，新旧双轨 | 采用：待删项（手搓 generator/必填 call/暴露 DTO）登记符号名，refactor 节点 removal_check 零残留 | FR-19 | AC-19 removal_check 对旧路径符号零残留 |
| 红队补漏 S3：四要素的"协议"要素无独立验收 | 采用：协议全集约30个逐条核对存在+runtime_checkable | FR-20 | AC-20 协议清单核对脚本 PASS |
| 红队补漏 S4：FP 纯变换的纯性无验收 | 采用：compile 幂等/不改原对象/算子实例不持进度 | FR-21 | AC-21 纯性断言测试 PASS |
