# 领域文档

本文件规定工程技能探索代码库时，如何读取和使用领域文档。

## 开始探索前

- 读取根目录的 `GLOSSARY.md`；如果存在 `GLOSSARY-MAP.md`，则按其中索引读取与当前主题相关的各份 `GLOSSARY.md`。
- 读取 `docs/adr/` 中与当前工作相关的架构决策记录（ADR）。多上下文仓库还需检查 `src/<context>/docs/adr/` 中对应上下文的决策。

如果这些文件不存在，静默继续即可，不要把缺失作为问题提出，也不要建议预先创建。只有在 `/domain-modeling`（可通过 `/grill-with-docs` 和 `/improve-codebase-architecture` 使用）实际解决术语或决策时，才按需创建它们。

## 文件布局

单上下文仓库（适用于大多数仓库）：

```text
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多上下文仓库（根目录存在 `GLOSSARY-MAP.md`）：

```text
/
├── GLOSSARY-MAP.md
├── docs/adr/                         # 系统级决策
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                 # 上下文级决策
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## 使用 glossary 中的术语

在 issue 标题、重构建议、假设或测试名称中提到领域概念时，使用 `GLOSSARY.md` 定义的术语。不要改用 glossary 明确标为不推荐的近义词。

如果需要的概念尚未收录，先确认是否引入了项目并不使用的说法；若确实存在术语缺口，记录下来交由 `/domain-modeling` 处理。

## 标明 ADR 冲突

如果输出与已有 ADR 冲突，应明确指出，而不是悄悄覆盖。例如：

> _与 ADR-0007（事件溯源订单）冲突；但由于……，建议重新讨论该决策。_
