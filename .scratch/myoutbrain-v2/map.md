# MyOutBrain V2 Map

## Decisions-so-far

- 工单 06：规范记忆生命周期只经显式迁移改变；修订与取代保留历史，可恢复停用退出普通召回，永久删除先展示完整影响闭包并以最小墓碑阻止静默恢复。详见 [工单 06](issues/06-revise-historicize-deactivate-and-erase-memory.md)。
- 工单 09：本地或公开反证以规范来源凭证进入任务内召回并强制不可回答，同时生成 blocking 统一整合提案；待审不改规范知识，批准沿既有 revision 路径物化，拒绝保留来源与决定且不做多数裁决。详见 [工单 09](issues/09-route-counterevidence-through-review.md)。
- 工单 10：胶囊重组采用写时复制、语义指纹、结构版本 CAS、原子指针切换与旧胶囊重定向；切换前故障清理 staged 副本，语义漂移转为统一 research 审阅提案，固定召回门禁保护身份、正文、关系与召回等价。详见 [工单 10](issues/10-reorganize-capsules-without-changing-recall.md)。
- 工单 13：手动增量迁移以经审计的传递知识闭包和逻辑 ZIP 为边界；导入先 dry-run，按包与检查点幂等，目标分歧经统一审阅批准后重映射版本并完成关系导入。详见 [工单 13](issues/13-transfer-an-audited-knowledge-closure.md)。
- 工单 11：三种可替换代理入口通过统一主实例注册表与 V2.1 MCP/CLI 领域协议接入；协议按版本区间和能力协商，未理解的审阅效果禁止批准，语义写入使用幂等键与期望版本。详见 [工单 11](issues/11-install-adapters-and-negotiate-capabilities.md)。
- 工单 12：计划反思通过 V2.2 客户端中立协议在 SQLite 核心中冻结输入闭包并提供跨入口租约；空队列不唤醒，完成/归还/过期/永久缺失均幂等收敛，显式反思可接管计划闭包。详见 [工单 12](issues/12-claim-scheduled-reflection-across-adapters.md)。
- 工单 14：实例维护通过 V2.3 统一领域协议提供带维护锁的冷全量 ZIP、恢复到新目录后的只读 Doctor 门禁、只重建投影的显式 repair，以及保护所有历史引用并需精确确认的孤立对象 GC；规范损坏进入受限只读且最小删除标记阻止静默恢复。详见 [工单 14](issues/14-back-up-diagnose-and-garbage-collect-the-instance.md)。

## Notes

- 工单实施细节、验收结果与基线失败记录保存在各工单的 Answer 和 Comments 中。

## Fog

- 工单 15 尚未实施，不属于已完成工单 10–14 的范围。
