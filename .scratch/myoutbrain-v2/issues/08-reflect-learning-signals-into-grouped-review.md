# 08 — 从学习信号生成分组反思批次

**What to build:** Companion 在任务末只捕获明确学习信号，Reflector 读取有界最小输入并生成去重、分组且可直接审阅的提案批次。

**Blocked by:** 03 — 召回知识并查看召回日志; 07 — 完成统一审阅提案与批次决定.

**Status:** resolved

- [x] 无学习信号的任务不保存输入也不创建空运行。
- [x] 输入包只保存必要摘录、稳定引用、来源指纹、覆盖范围和盲区。
- [x] Companion 与 Reflector 只通过稳定领域契约访问本地核心。
- [x] 显式反思可以在当前入口立即执行。
- [x] Reflector 区分明确陈述、系统推导和研究假设，并完成精确去重、近似分组和冲突分组。
- [x] 提案形成或运行放弃后安全清理临时输入，不破坏长期凭证。

## Answer

工单 08 tracer bullet 已完成。Companion 只在五类明确学习信号出现时通过 MemoryGateway/CLI 提交有界最小反思输入；Reflector 可以在当前入口读取有数量与字节预算的输入、核对来源指纹，并立即生成统一审阅提案。每个候选精确绑定实际支持它的输入，明确陈述、带推导过程的系统推导与研究假设保持不同形成方式；完全重复提案归并来源，近似与冲突关系只分组且不能对同一候选对同时成立。成功形成提案或创作者明确放弃后，临时输入原子清理；失败时输入保留，长期提案按 receipt/excerpt 策略保留最小凭证且不会放宽 local-only 边界。

## Comments

- 2026-07-18：新增 `submit-learning-signal`、`reflection-inputs`、`reflect-now` 与 `abandon-reflection` CLI，并全部经 `MemoryGateway` 进入本地核心；V2 Schema 升至 9，输入与即时运行持久状态支持幂等写入和安全恢复。
- 2026-07-18：输入摘录上限 2 KiB、完整输入上限 8 KiB；读取和即时运行另有数量/字节边界。无信号纯 no-op，不创建输入或空运行；来源变化/缺失会进入提案盲区而不冒充捕获时内容。
- 2026-07-18：Reflector 候选逐项绑定支持输入；`explicit`、`derived`、`hypothesis` 权限保持分离，derived 必须展示推导。精确重复合并证据、覆盖和盲区，near/conflict 使用客户端中立分组且禁止同一无序候选对的双重关系。
- 2026-07-18：更新 Companion Skill 并新增 Reflector Skill；两者不持有状态、不读取 SQLite，也未实施工单 09/11/12/15 的反证路由、适配器安装、计划领取或发布门禁。
- 2026-07-18：5 项工单 08 黑盒测试、41 项相关/兼容回归与严格 mypy（61 个源/测试文件）通过。最终完整套件 198 项中 193 项通过；5 项失败均为固定基线已记录的 `AnswerWithPublicResearchFallbackTests` 公共检索时效 fixture。
- 2026-07-18：按 `code-review` 完成 Standards/Spec 双轴审查，修复输入与提案来源错配、敏感度放宽、receipt 摘录残留、派生过程缺失、无界读取/运行、精确去重关系丢失及关系类型歧义；最终两轴复核均无剩余发现。
