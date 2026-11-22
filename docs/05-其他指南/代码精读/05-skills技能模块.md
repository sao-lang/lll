# 代码精读 05 · skills 技能模块

> 面向手机阅读，脱离源码可读。本文拆 `src/harness/skills/` 全部 2 个文件（provider.py 197 行 + __init__.py）。
> 别看模块小，它的注入检测器被 tools/webfetch、rag/post、MemoryStore 三处复用，是横切安全能力。
> 总入口见 [00](./00-总入口与全局链路.md)。

---

## 目录

1. 一句话心智模型
2. 注入裁决与协议
3. 默认检测器 KeywordInjectionDetector
4. 技能发现 FileSkillProvider
5. 技能正文加载与 fail-closed
6. manifest 解析
7. 这个模块被谁用
8. 端到端：一次技能披露
9. 细节速记卡

---

## 1. 一句话心智模型

skills 模块干两件事：

- **发现技能**：扫磁盘目录，每个含 `manifest.json` 的子目录是一个技能包，实现 R2 的 `SkillPort`。
- **注入检测**：提供 `InjectionDetector` 协议和默认关键词检测器，任何「不可信正文」进上下文前都要过它，命中就拒绝（fail-closed）。

技能加载是渐进披露的**最后一环（L3 正文）**。核心纪律：**正文必过注入检测，不存在「检测器缺席放行」的分支**。

---

## 2. 注入裁决与协议

### InjectionVerdict（枚举）

- `CLEAN = "clean"`：未命中注入特征，可安全披露。
- `INJECTED = "injected"`：命中，必须拒绝披露。

**为什么用枚举不用 bool**：避免真值歧义（枚举成员语义明确）。

### InjectionDetector（协议）

- `name` 属性：检测器唯一名，装配期按 name 校验。
- `detect(text) -> InjectionVerdict`：检测文本。

可注入替换——生产可接更狠的检测器（如基于模型的注入分类）。

---

## 3. 默认检测器 KeywordInjectionDetector

`_DEFAULT_KEYWORDS` 内置 8 个常见注入串：

```
"ignore previous instructions"
"ignore all previous instructions"
"disregard previous instructions"
"forget everything above"
"reveal your system prompt"
"show me the system prompt"
"you are now"
"jailbreak"
```

**构造的鸭子兼容**：声明类型是 `tuple[str, ...]`，但测试/调用方可能只传单个字符串。代码显式归一：

```
raw = keywords
self._keywords = (raw,) if isinstance(raw, str) else tuple(raw)
```

为什么重要：如果不管，传字符串会被按字符迭代，每个字符都成关键词，导致任何文本都命中（误判）。

`detect(text)`：

1. `folded = text.casefold()`——先折叠大小写再比对，防通过大小写变换绕过黑名单。
2. `any(keyword.casefold() in folded for keyword in self._keywords)`——任一命中即 INJECTED，否则 CLEAN。

---

## 4. 技能发现 FileSkillProvider

实现 `SkillPort`，`name = "file_skill"`。

构造收 `root: Path`（技能包根目录，每个**直接子目录**是一个技能包）和可选 `detector`。

**detector 缺省/显式 None 都归一为 KeywordInjectionDetector**——代码注释明确写「fail closed：正文必过注入检测，不存在缺席放行分支」。

### discover()

扫描所有可用技能：

1. `sorted(self._root.iterdir())`——**排序遍历**，因为目录枚举顺序不稳定，排序后 catalog/披露才有确定顺序。
2. 只认子目录（普通文件跳过）。
3. 必须有 `manifest.json`（没有的目录不是合法技能包，跳过而非报错）。
4. 每个合法目录 `_read_manifest()` 得 SkillManifest。

### catalog()

返回技能目录（L1 常驻披露），与 discover 同形。**不缓存**——每次重新扫描，避免磁盘变动后目录陈旧。

---

## 5. 技能正文加载与 fail-closed

`body(name) -> str`：按需加载技能正文（L3）。

1. 目标路径：`root / name / "SKILL.md"`。
2. 文件不存在 → 抛 `KeyError(name)`。**不静默返回空串**（避免把「无内容」误当「已加载」）。
3. `text = target.read_text(encoding="utf-8")`。
4. `detector.detect(text) is INJECTED` → 抛 `ValueError("Injection detected in skill ...; disclosure refused")`。
5. 否则返回正文。

关键：命中注入是 `ValueError`，由上层（ToolIndex.disclose）捕获后留空串，而不是把带攻击载荷的正文交给模型。

---

## 6. manifest 解析

`_ManifestFile(TypedDict, total=False)`：`name` / `version` / `description` / `triggers: list[str]`（字段可缺省）。

`_read_manifest(path)`：

1. `data = cast(_ManifestFile, json.loads(...))`——动态 JSON **只在解析边界 cast 一次**，下游只读具名字段（规则 6 的动态边界收敛）。
2. 构造 `SkillManifest(name, version, description, triggers=tuple(triggers_raw))`——list 转 tuple。

---

## 7. 这个模块被谁用

注入检测是横切能力，三处复用同一个协议：

| 使用方 | 场景 | 命中处理 |
|---|---|---|
| `FileSkillProvider.body()` | 技能 L3 正文加载 | 抛 ValueError，ToolIndex 留空串 |
| `WebFetchTool.fetch()`（tools 模块） | 网页抓取正文 | 抛 ValueError 拒载 + 记审计 |
| `MemoryStore._scan_text()`（memory 模块） | 记忆写入/召回的正文复检 | 写入拒绝 / 召回跳过 |
| `DefaultPostProcessor.filter()`（rag 模块） | 检索命中过滤 | 剔除该 hit |

四处**都是 fail-closed**，语义一致。

---

## 8. 端到端：一次技能披露

假设模型需要某个技能的详细说明：

1. 装配期：`ToolsCapability(skills=FileSkillProvider(root=Path("skills")))`，skills 被 `install()` 注册为 `skill` port。
2. 披露时：`ToolsCapability.disclose(names)` 先调 `skills.catalog()` 拿技能清单（扫磁盘，未缓存）。
3. `ToolIndex.disclose(names, manifests)` 加载 L3：逐个 `skills.body(manifest.name)`。
4. 若某技能 SKILL.md 含「ignore previous instructions」→ detector 判 INJECTED → body 抛 ValueError → ToolIndex 捕获，该技能 l3 留空串。
5. 其他干净技能正文正常进 DisclosurePacket.l3。
6. 模型拿到目录 + 命中工具 schema + 干净技能正文，不含攻击载荷。

---

## 9. 细节速记卡

- **注入裁决用枚举不用 bool**：CLEAN / INJECTED 语义明确。
- **检测器缺省也有默认实现**：正文披露不存在「缺席放行」分支。
- **单字符串入参归一为单元素元组**：否则按字符迭代会全量误判。
- **大小写不敏感匹配**：先 casefold，防大小写绕过。
- **目录排序遍历**：保证 catalog 顺序确定。
- **manifest 缺失跳过不报错**：不是合法技能包。
- **正文不存在抛 KeyError**：不静默返回空串。
- **命中注入抛 ValueError**：由上层决定如何降级（留空/拒载/剔除）。
- **catalog 不缓存**：磁盘变动后立即可见。
- **动态 JSON 只在解析边界 cast 一次**：下游只读具名字段。
- **triggers 从 list 转 tuple**：DTO 不可变语义。
- **注入检测是横切能力**：skills / tools/webfetch / rag/post / MemoryStore 四处复用，全部 fail-closed。

---

*下一册：[06 resilience 可靠性模块](./06-resilience可靠性模块.md)。最后读 [07 MemoryStore](./07-MemoryStore精读.md)——它也用了本册的注入检测器。*
