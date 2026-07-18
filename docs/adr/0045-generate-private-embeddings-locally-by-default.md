---
status: superseded by ADR-0087
---

# 私人记忆默认使用本地 Embedding

MyOutBrain 私人实例默认使用本地多语言 Embedding 模型构建语义召回索引，模型不可用时退化为来源关系和 SQLite 全文检索，不自动上传私人文本。云端 Embedding 仅在创作者明确启用并确认提供方、模型、发送范围和成本后用于敏感度允许的内容，`local_only` 内容永不外发；不同模型和敏感度使用独立可重建索引，更换提供方只重建运行数据而不修改规范记忆。
