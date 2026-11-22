# 代码精读 · skills 技能模块（逐行讲解版）

> **逐行讲解**：每个代码块都标注行号，逐行解释「这行做什么、为什么这么写、不这么写会怎样」。目标是脱离源码也能逐行读懂。
> 覆盖 `src/harness/skills/provider.py`（197 行）+ `__init__.py`（19 行）全部代码。
> 总入口见 [00](./00-总入口与全局链路.md)。

---

## 0. 读前GPS

这个模块只有两个文件，但它是**横切安全能力**：它定义的 `InjectionDetector` 被 tools 的 webfetch、rag 的 post、memory 的 MemoryStore 三处复用。逐行读懂它，那三处的注入检测逻辑就都懂了。

全模块两个核心类：
- `KeywordInjectionDetector`：关键词黑名单检测器。
- `FileSkillProvider`：磁盘技能包发现 + 正文加载 + 注入检测。

---

## 1. provider.py 逐行讲解

### 第 1-8 行：模块 docstring

- **第 1 行** `"""R5 技能包提供者：从磁盘发现技能清单，并在披露正文前做注入检测。`
  一句话定位：磁盘发现 + 正文安全检测。注意归属 R5（工具·MCP·技能域）。

- **第 2 行** 空行（docstring 内分段）。

- **第 3 行** `职责：实现 R2 ``SkillPort`` 的默认实现 ``FileSkillProvider``（规则 9：协议与默认实现成对），`
  点明这是「协议 + 默认实现成对」纪律的落地。规则 9 指：每个可注入协议必须有开箱默认实现。

- **第 4 行** `并给出可替换的注入检测器协议 ``InjectionDetector`` 与内置默认 ``KeywordInjectionDetector``。`
  第二个「协议 + 默认实现」对：检测器也可插拔。

- **第 5 行** `关键纪律：正文加载是 L3 披露的最后一环，命中注入必须 fail closed（规则 5 用 Enum 表达裁决，`
  强调 fail-closed：命中注入就拒绝，不放行也不净化。裁决用枚举而非 bool。

- **第 6 行** `禁止魔法串）；manifest 的磁盘形态用 TypedDict 显式声明（规则 4），动态 JSON 只在解析一处 cast（规则 6）。`
  三条编码规则：4=字段显式声明、5=有限取值用 Enum、6=动态边界收敛到一处。

- **第 7 行** `公共 API 见 docs/02-模块详设/Agent/R5-工具MCP与技能.md；协议契约见 src/compose/ports.py::SkillPort。`
  指路：详设和协议定义在哪。

- **第 8 行** `"""` docstring 结束。

### 第 9-15 行：imports 与导出

- **第 9 行** `from __future__ import annotations`
  延迟求值注解，允许在类型注解里引用尚未定义的类型，降低 import 顺序耦合。

- **第 11 行** `import json`
  解析 manifest.json。

- **第 12 行** `from enum import Enum`
  定义 `InjectionVerdict` 裁决枚举。

- **第 13 行** `from pathlib import Path`
  磁盘路径操作（iterdir / is_dir / read_text）。

- **第 14 行** `from typing import Protocol, TypedDict, cast, runtime_checkable`
  - `Protocol`：定义检测器可插拔协议。
  - `TypedDict`：声明 manifest.json 的磁盘形态。
  - `cast`：JSON 动态值在解析边界收敛类型。
  - `runtime_checkable`：让 `isinstance(x, InjectionDetector)` 可用于装配期履约校验。

- **第 16 行** `from compose import SkillManifest`
  导入 R2 定义的技能清单 DTO。注意依赖方向：harness 依赖 compose，不反向。

- **第 18-23 行** `__all__ = [...]`
  导出四个符号：裁决枚举、检测器协议、默认检测器、技能提供者。`__init__.py` 据此重导出。

### 第 26-35 行：InjectionVerdict 枚举

- **第 26 行** `class InjectionVerdict(str, Enum):`
  继承 `str, Enum` 双基类：既是枚举（有限取值、可比较），又能当字符串用（进 JSON/日志/审计）。

- **第 27 行** `"""注入检测裁决（规则 5：有限取值集合用 Enum，禁止裸魔法串）。`
  说明为什么用 Enum：避免 `"clean"` / `"injected"` 裸字符串散落各处。

- **第 29-30 行** 成员说明 docstring。

- **第 34 行** `CLEAN = "clean"`
  未命中任何注入特征，可安全披露。

- **第 35 行** `INJECTED = "injected"`
  命中注入特征，必须拒绝披露（fail closed）。

**为什么不用 bool**：bool 有真值歧义——`False` 是「干净」还是「检测器故障」？枚举成员语义明确，且未来可扩展（如 `SUSPICIOUS`）。

### 第 38-53 行：InjectionDetector 协议

- **第 38 行** `@runtime_checkable`
  装饰器：允许运行时 `isinstance()` 检查。代价是不能检查方法签名，只检查成员存在。

- **第 39 行** `class InjectionDetector(Protocol):`
  可插拔协议定义。框架内所有注入检测点（webfetch/rag/memory）都认这个协议。

- **第 40-43 行** docstring + `name: str` 属性。
  `name` 是「可插拔物必须具名」纪律的落地：装配期按 name 校验履约、进 trace 归因。

- **第 45 行** `def detect(self, text: str) -> InjectionVerdict:`
  协议方法签名：输入文本，输出裁决枚举。

- **第 46-51 行** 方法 docstring：参数、返回值说明。

- **第 53 行** `...`
  Protocol 方法体用省略号，表示接口声明而非实现。这不是「代码省略」，是 Python 协议语法。

### 第 56-66 行：默认注入关键词表

- **第 56 行** `# 默认注入特征串：覆盖常见的"忽略上文/越权披露"提示注入写法。`
  注释说明威胁模型：prompt injection 的两种典型——让模型忽略系统指令、套取系统提示词。

- **第 57-66 行** `_DEFAULT_KEYWORDS: tuple[str, ...] = (...)`
  8 个关键词：
  - `"ignore previous instructions"` / `"ignore all previous instructions"` / `"disregard previous instructions"`：忽略上文。
  - `"forget everything above"`：忘记上文。
  - `"reveal your system prompt"` / `"show me the system prompt"`：套取系统提示。
  - `"you are now"`：角色劫持。
  - `"jailbreak"`：越狱。

  用 tuple 而非 list：不可变，防止运行期被意外修改。

### 第 69-101 行：KeywordInjectionDetector 类

- **第 69 行** `class KeywordInjectionDetector:`
  默认检测器实现。注意它**没有显式继承** `InjectionDetector`——Protocol 是结构化类型（鸭子类型），有 `name` 和 `detect` 就算履约，不需要继承。

- **第 70 行** `"""默认注入检测器：任意关键词大小写不敏感命中即判 INJECTED。"""`
  一句话概括算法。

- **第 72 行** `name: str = "keyword_injection"`
  具名标识。

- **第 74 行** `_keywords: tuple[str, ...]`
  类级类型注解（声明实例属性类型）。

- **第 76-84 行** 构造函数 docstring。
  第 82-83 行特别重要：说明虽然声明类型是 tuple，但鸭子调用方可能传单个字符串，所以运行时要归一。

- **第 86 行** `raw: tuple[str, ...] | str = keywords`
  用联合类型接收，承认运行时可能是 str。

- **第 87 行** `self._keywords = (raw,) if isinstance(raw, str) else tuple(raw)`
  **关键防御行**：如果传的是字符串，包成单元素元组 `(raw,)`；否则转 tuple。
  - 若不这么处理：传 `"jailbreak"` 会被 `for keyword in self._keywords` 按字符迭代成 `"j"`、`"a"`...，导致几乎任何文本都命中（误判灾难）。
  - 用 `tuple(raw)` 而非直接赋值：防外部后续修改原 list 影响本实例。

- **第 89-101 行** `detect` 方法。

  - **第 89 行** `def detect(self, text: str) -> InjectionVerdict:`
  - **第 90-96 行** docstring。
  - **第 97 行** `folded = text.casefold()`
    **大小写归一**：用 `casefold()` 而非 `lower()`。`casefold()` 是更彻底的大小写折叠（Unicode 全折叠，如德语 ß→ss），防大小写变换绕过黑名单。
  - **第 98 行** 注释：`# 先折叠大小写再比对，防止通过大小写变换绕过关键词黑名单。`
  - **第 99 行** `if any(keyword.casefold() in folded for keyword in self._keywords):`
    生成器表达式 + `any`：任一关键词（也 casefold）是文本子串即命中。`any` 短路，命中即停，不必检查全部关键词。
    - 注意关键词也 casefold：因为注入的自定义关键词表可能含大写。
  - **第 100 行** `return InjectionVerdict.INJECTED`
    命中返回裁决枚举成员。
  - **第 101 行** `return InjectionVerdict.CLEAN`
    未命中返回干净裁决。

### 第 104-110 行：_ManifestFile TypedDict

- **第 104 行** `class _ManifestFile(TypedDict, total=False):`
  声明 manifest.json 的磁盘形态。`total=False` 表示所有字段都可缺省（技能包可以不写 version/description/triggers）。

- **第 105 行** docstring。

- **第 107-110 行** 四个字段：`name: str` / `version: str` / `description: str` / `triggers: list[str]`。
  为什么要 TypedDict：规则 4「字段显式声明」——让下游读 `data.get("name")` 时有类型提示，而不是全靠 `Any`。

### 第 113-197 行：FileSkillProvider 类

- **第 113 行** `class FileSkillProvider:`
  R2 `SkillPort` 的默认实现。

- **第 114 行** `"""SkillPort 默认实现：扫描 root 下含 manifest.json 的子目录作为技能包。"""`
  一句话定位：目录结构约定——root 下每个含 manifest.json 的子目录 = 一个技能包。

- **第 116 行** `name: str = "file_skill"`
  具名标识。

- **第 118-119 行** 实例属性类型注解：`_root: Path`、`_detector: InjectionDetector`。

- **第 121-131 行** 构造函数。

  - **第 121 行** `def __init__(self, root: Path, *, detector: InjectionDetector | None = None) -> None:`
    `root` 必填位置参数；`detector` 关键字参数（`*` 之后），可缺省。
  - **第 122-128 行** docstring。第 126-127 行强调：detector 缺省/显式 None 都归一为默认检测器，**不存在「缺席放行」分支**。
  - **第 129 行** `self._root = root`
    保存根目录。
  - **第 130 行** 注释：`# 缺省即默认检测器：正文披露是 L3 最后一环，绝不允许因未注入而绕过检测。`
  - **第 131 行** `self._detector = detector if detector is not None else KeywordInjectionDetector()`
    **fail-closed 落地行**：任何情况下检测器都存在。传 None 不会导致「不检测直接放行」。

- **第 133-150 行** `discover()` 方法：扫描磁盘发现技能。

  - **第 133 行** `def discover(self) -> list[SkillManifest]:`
  - **第 134 行** docstring 首行。
  - **第 137 行** returns 说明：排序后遍历，保证顺序确定。
  - **第 139 行** `manifests: list[SkillManifest] = []`
    结果容器。
  - **第 140 行** 注释：`# 排序遍历：目录枚举顺序不稳定，排序后 catalog/披露才有确定顺序。`
    **确定性纪律**：不排序的话，`Path.iterdir()` 顺序依赖文件系统，同一批技能在不同机器披露顺序不同，影响可复现性。
  - **第 141 行** `for child in sorted(self._root.iterdir()):`
    排序遍历每个直接子项。
  - **第 142 行** 注释：`# 只认子目录，普通文件不是技能包。`
  - **第 143-144 行** `if not child.is_dir(): continue`
    跳过普通文件（如 README）。
  - **第 145 行** `manifest_path = child / "manifest.json"`
    拼出清单路径。
  - **第 146 行** 注释：`# 没有 manifest.json 的目录不是合法技能包，跳过而非报错。`
  - **第 147-148 行** `if not manifest_path.is_file(): continue`
    跳过无清单的目录。注意是「跳过不报错」——容忍根目录下有其他无关子目录。
  - **第 149 行** `manifests.append(self._read_manifest(manifest_path))`
    读取清单并加入结果。
  - **第 150 行** `return manifests`
    返回全部技能清单。

- **第 152-158 行** `catalog()` 方法。

  - **第 152 行** `def catalog(self) -> list[SkillManifest]:`
  - **第 153 行** docstring：`L1 常驻披露，与 discover 同形`。
  - **第 156 行** `重新扫描得到的 SkillManifest 列表（不缓存，避免磁盘变动后目录陈旧）。`
    **关键设计**：不缓存。技能包可以热插拔——往目录里放一个新技能，下一次 catalog 立即可见。
  - **第 158 行** `return self.discover()`
    直接委托 discover，二者同形。

- **第 160-179 行** `body(name)` 方法：加载技能正文（L3）。

  - **第 160 行** `def body(self, name: str) -> str:`
  - **第 161 行** docstring：`按需加载技能正文（L3），命中注入则拒绝披露。`
  - **第 162-169 行** 参数/返回/异常说明：`KeyError`（正文不存在）、`ValueError`（命中注入）。
  - **第 171 行** `target = self._root / name / "SKILL.md"`
    正文路径约定：`<root>/<技能名>/SKILL.md`。
  - **第 172 行** 注释：`# 正文不存在时显式报错，不静默返回空串（避免把"无内容"误当"已加载"）。`
  - **第 173-174 行** `if not target.is_file(): raise KeyError(name)`
    **显式报错**：文件不存在不能返回空串让上层以为加载成功了。
  - **第 175 行** `text = target.read_text(encoding="utf-8")`
    读正文。显式指定 utf-8，避免依赖系统默认编码（Windows 可能是 GBK）。
  - **第 176 行** 注释：`# 正文必过注入检测：命中即 fail closed，绝不把不可信正文交给模型。`
  - **第 177-178 行** `if self._detector.detect(text) is InjectionVerdict.INJECTED: raise ValueError(...)`
    **核心安全行**：用 `is` 比较枚举成员（枚举单例），命中即抛 ValueError。
    - 这个 ValueError 会被上层 `ToolIndex.disclose` 捕获 → 该技能 l3 正文留空串（见 tools 模块）。
  - **第 179 行** `return text`
    干净正文才返回。

- **第 181-197 行** `_read_manifest(path)` 私有方法。

  - **第 181 行** `def _read_manifest(self, path: Path) -> SkillManifest:`
  - **第 182-188 行** docstring。
  - **第 189 行** 注释：`# json.loads 返回动态值，仅在解析边界 cast 一次（规则 6），下游只读具名字段。`
    **动态边界收敛**：规则 6 要求动态数据只在入口 cast 一次，不要到处 `data["xxx"]` 散落。
  - **第 190 行** `data = cast(_ManifestFile, json.loads(path.read_text(encoding="utf-8")))`
    读文件 → json 解析 → cast 成 TypedDict。`cast` 只是类型提示，运行时不做校验——若 JSON 是数组而非对象，后续 `.get` 会报错（此处假设磁盘文件可信）。
  - **第 191 行** `triggers_raw = data.get("triggers", [])`
    取触发词，缺省空列表。
  - **第 192-197 行** 构造 `SkillManifest`：
    - `name=str(data.get("name", ""))`：缺省空串。
    - `version=str(data.get("version", ""))`：缺省空串。
    - `description=str(data.get("description", ""))`：缺省空串。
    - `triggers=tuple(triggers_raw)`：list 转 tuple，DTO 不可变语义。
    - 每个字段都用 `str(...)` 包裹：防 JSON 里写了数字等异常类型导致类型不符。

---

## 2. __init__.py 逐行讲解（19 行）

- **第 1-3 行** docstring：`R5 技能包：磁盘技能发现 + 注入检测（SkillPort 默认实现）。统一导出...`
  包级说明。

- **第 5 行** `from __future__ import annotations` 延迟注解。

- **第 7-12 行** `from .provider import (...)`
  从 provider 导入四个符号：`FileSkillProvider`、`InjectionDetector`、`InjectionVerdict`、`KeywordInjectionDetector`。
  这是**包级 API 收敛**：外部只需 `from harness.skills import InjectionDetector`，不必知道内部文件路径。

- **第 14-19 行** `__all__ = [...]`
  声明公共导出面。tools/webfetch、rag/post、memory/memory 都是 `from harness.skills import ...` 从这里拿检测器。

---

## 3. 逐行讲解后的整体心智图

```
磁盘技能包 <root>/<name>/{manifest.json, SKILL.md}
        │
        ├─ discover()  排序遍历 → 找 manifest.json → _read_manifest（cast 一次）→ SkillManifest
        ├─ catalog()   = discover()，不缓存（热插拔）
        └─ body(name)  读 SKILL.md → detector.detect
                            ├─ INJECTED → raise ValueError（上层留空串）
                            └─ CLEAN    → 返回正文

InjectionDetector（协议，可注入）
  └─ KeywordInjectionDetector（默认）：casefold + any(关键词 in 文本)
```

---

## 4. 细节速记卡（逐行精华）

- **casefold 不是 lower**：Unicode 全折叠，防大小写绕过。
- **单字符串入参包成元组**：否则按字符迭代导致全量误判（第 87 行）。
- **detector 永远存在**：传 None 也用默认值，无「缺席放行」分支（第 131 行）。
- **目录排序遍历**：确定性，可复现（第 141 行）。
- **无 manifest 跳过不报错**：容忍无关子目录（第 147 行）。
- **正文不存在抛 KeyError**：不静默返回空串（第 174 行）。
- **命中注入抛 ValueError**：由上层决定降级方式（第 178 行）。
- **catalog 不缓存**：技能热插拔（第 156 行）。
- **JSON 只在解析边界 cast 一次**：动态边界收敛（第 190 行）。
- **triggers 转 tuple**：DTO 不可变（第 197 行）。
- **encode 显式 utf-8**：不依赖系统默认编码（第 175、190 行）。
- **用 `is` 比枚举**：枚举成员是单例（第 177 行）。
- **Protocol 不需继承**：结构化类型，有成员即履约。
- **横切复用**：本模块的 detector 被 tools/rag/memory 三处 import（见 `__init__.py` 第 14-19 行）。

---

*这是逐行讲解版的样板册。其余模块按同一格式：「行号 + 代码 + 做什么 + 为什么 + 不这么写会怎样」。*
