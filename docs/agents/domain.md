# 领域文档

本文说明工程技能在探索代码库时应如何使用本仓库的领域文档。

## 探索前应读取

- 仓库根目录下的 **`CONTEXT.md`**；或者
- 若根目录存在 **`CONTEXT-MAP.md`**，则由它指向每个上下文的 `CONTEXT.md`，并读取与当前主题相关的文件
- **`docs/adr/`** 中与即将处理区域相关的 ADR；在多上下文仓库中，还应检查 `src/<context>/docs/adr/` 下的上下文级决策

若上述文件不存在，**静默继续**。不要报告缺失，也不要预先建议创建。`/domain-modeling` 技能（可由 `/grill-with-docs` 和 `/improve-codebase-architecture` 触发）会在术语或决策真正明确时按需创建这些文件。

## 文件结构

单上下文仓库（适用于大多数仓库）：

```text
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多上下文仓库（根目录存在 `CONTEXT-MAP.md`）：

```text
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← 上下文级决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 使用词汇表中的术语

当输出内容需要命名领域概念时（例如事项标题、重构提案、假设或测试名称），应使用 `CONTEXT.md` 中定义的术语。不要改用词汇表明确排除的同义词。

如果需要的概念尚未出现在词汇表中，这是一项信号：要么正在创造项目并未使用的语言（应重新考虑），要么确实存在领域词汇缺口（应记录并交由 `/domain-modeling` 处理）。

## 标明与 ADR 的冲突

如果输出内容与现有 ADR 冲突，应明确指出，而不是静默覆盖：

> _与 ADR-0007（事件溯源订单）冲突——但由于……值得重新讨论。_
