---
name: grill-design
display_name: 需求拷问
display_name_en: Design Grilling
description: 「需求拷问」——把一份方案 / 设计 / 想法，用「设计树 + 前沿」一轮轮拷问，直到每个分支都定了、没有静默假设。触发词：拷问我、帮我把这个方案打穿、这个想法有哪些坑、压力测试一下我的设计、挑战一下我的计划、这个方案靠不靠谱。
description_zh: 用「设计树 + 前沿」一轮轮拷问一份方案或想法，直到每个分支都有结论、没有静默假设；事实由 agent 自己查、决策交用户，收尾给结构化结论。
description_en: Grill a plan or design round by round with a design tree and a frontier of answerable questions, until every branch is decided and no silent assumption remains. The agent looks facts up; the user keeps the decisions.
category: productivity
version: 1.0.0
author: Dolikas
---

# 需求拷问（把方案打穿）

把用户的一个方案 / 设计 / 想法，当成一棵「设计树」来拷问，直到达成共识。

## 核心机制

- **设计树**：每个决策会分叉出挂在它下面的决策。
- **前沿（frontier）**：所有「前提已经定了、现在就能问」的问题集合。
- **一轮问完整个前沿**：每个问题编号 + 给出推荐答案，等用户回答后再算下一轮。
- **事实归 agent，决策归用户**：能自己查的事实（跑脚本 / 读文档 / 查配置），绝不去问用户；
  派子代理去查，**不阻塞** —— 查的过程中先问前沿里其余的问题。
- **收尾**：前沿清空 = 达成一致；在用户确认前**不要行动**。

## 一轮的格式

```
❓ **Q1** - **<问题标题>**：<问题正文，可含多个选项>
➡️ <推荐答案>

---

❓ **Q2** - **<问题标题>**：<问题正文，可含多个选项>
➡️ <推荐答案>
```

## 怎么问（本平台惯用）

一次抛 2~4 个前沿问题，用 `AskUserQuestion`（每个问题给 2~4 个选项，推荐项放第一个并标「（推荐）」）。
本轮回答会重塑树：已定的决策把前沿往外推、解锁原本卡住的问题 —— 重新算前沿，问下一轮。

## 两个校准

1. **「先实测再讨论」优先**：碰到「事实型」问题（这个脚本到底行不行 / 这个值是不是这样），
   先跑一下 / 读一下再问，别把能查的推给用户。
2. **收尾给结构化结论**：达成一致后，用「分节 + 表格」把定下来的决策列一遍，标出**仍未决的**，
   别只甩一句「好的」。

---

## 移植说明

**来源**：灵感取自 <https://github.com/mattpocock/skills> → `skills/productivity/grilling`（MIT License, © Matt Pocock）。
**并非照搬** —— 这是**蒸馏**后的平台原生技能：只继承了「设计树 + 前沿」这一核心机制，
其余（触发词、提问方式、收尾习惯）按本平台习惯重写。

> **License（MIT）**：原作品以 **MIT License** 发布，© Matt Pocock。按其要求保留版权与许可声明：
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction,
> including without limitation the rights to use, copy, modify, merge, publish, distribute,
> sublicense, and/or sell copies of the Software, subject to the inclusion of the above copyright
> notice and this permission notice in all copies or substantial portions of the Software.
原版 `grill-me` 是个空壳指针（只调 `grilling`），本技能直接合并为单体。
