# 第一阶段同伴闭环

MyOutBrain 的私人实例是长期记忆、来源关系和已批准知识的唯一事实来源。Codex 是第一个智能体入口；CLI 是验收与运维入口；生成模型、Embedding 模型、公开检索工具和 Obsidian 都是可替换能力或可重建投影。

## Codex Skill

仓库内的 `skills/myoutbrain-companion` 可以复制或链接到 Codex 的 Skills 目录。它要求 Codex 在实质任务前通过统一记忆网关取回任务相关的最小证据包，并在任务后只提交当前实际可见的经历、稳定任务指针和明确盲区。

Skill 不读取 SQLite、对象存储、索引或整个 Vault，也不维护 Codex 专属的用户模型。

## 完整文本闭环

以下命令展示公共入口。所有命令都显式指定同一个私人实例：

1. 任务前获取上下文：

   ```powershell
   python -m myoutbrain codex-context "当前任务需要知道什么？" `
     --root <private-instance> `
     --task-pointer <stable-task-pointer> `
     --purpose substantive `
     --access task-scoped `
     --format json
   ```

2. 完成任务后提交当前可见经历：

   ```powershell
   python -m myoutbrain codex-submit <visible-task-file> `
     --root <private-instance> `
     --occurred-at <ISO-8601-with-offset> `
     --task-pointer <stable-task-pointer> `
     --digest "<compact-memory-digest>" `
     --sensitivity local-only `
     --visible-context "<what-was-visible>" `
     --context-gap "<what-was-unavailable>" `
     --format json
   ```

3. Codex 本身是当前可替换能力引擎；当当前可见任务与任务证据包已完整支持结论时，可直接形成带来源回答。只有需要标准化公开补充时，才用 `answer` 提供不含私人上下文的 `--public-query` 和已配置能力提供方。不得为了让命令成功而把 `local-only` 放宽为 `cloud-allowed`；检索后仍不足则返回 `unknown`，不会生成长期结论。
4. `answer` 的可追溯结果先写入记忆缓冲区。运行 `consolidate --task <task>` 只生成整合提案；创作者通过 `review-memory <proposal-id> "accept"` 或自然纠正、拒绝、保留冲突后，规范记忆才改变。
5. 后续 `codex-context` 或 `recall` 会返回稳定的规范记忆身份和来源关系。未批准提案不会进入共同知识基线。

## 连续性与重建

更换生成或 Embedding 能力引擎不得改变记忆身份、来源关系或已批准知识。`runtime/indexes` 与 `vault/Knowledge Views` 可以删除：语义召回会重建兼容索引，`build-views` 会从规范记忆重新生成知识视图。重建失败应作为完整性故障处理，不能回退到模型自有记忆。

## 第一阶段边界

第一阶段只验证可靠文本闭环。人格与情绪系统、声音或作者模仿、音视频及其他多媒体入口、生产级多渠道适配器，以及无边界自主网络学习仍属后续范围。能力引擎可以协助研究和表达，但不拥有同伴身份、长期记忆或未经审阅的规范语义写权限。
