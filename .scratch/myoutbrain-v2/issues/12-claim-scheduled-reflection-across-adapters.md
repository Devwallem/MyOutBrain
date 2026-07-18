# 12 — 跨入口领取计划反思

**What to build:** 普通学习信号可以按每周计划进入待执行反思，计划到点时不调用模型，而由下一位兼容智能体安全领取、完成或归还运行。

**Blocked by:** 08 — 从学习信号生成分组反思批次; 11 — 安装三种入口并协商协议能力.

**Status:** resolved

- [x] 空队列到点不创建无效工作也不唤醒能力引擎。
- [x] 到点只冻结输入闭包并创建 queued 运行，不保存模型密钥或发起网络请求。
- [x] 多入口领取使用租约，只有一个结果可以幂等完成。
- [x] 入口崩溃或租约到期后运行安全回到 queued。
- [x] 明确永久缺失的输入可以被用户放弃，并保留不含正文的原因。
- [x] 计划周期可配置，显式反思不受计划限制。

## Answer

已通过 V2.2 客户端中立领域协议实现计划反思 tracer bullet：SQLite 核心原子维护计划、冻结闭包、queued/claimed 状态、租约、归还、完成与永久缺失放弃；静态调度 CLI 只发送领域请求，不直接访问数据库，也不调用模型或网络。显式反思可以接管完整的计划闭包，因此不受计划限制。

## Comments

- 2026-07-19：实例 schema 升至 10，新增 `reflection.schedule`、`reflection.enqueue`、`reflection.claim`、`reflection.return`、`reflection.complete`、`reflection.abandon`，并同步三种代理入口的能力协商与 reflector skill。
- 2026-07-19：公开 CLI/领域黑盒测试覆盖空队列、冻结闭包、跨入口单租约与单结果、崩溃/过期/主动归还、永久缺失放弃及显式反思接管；最终 `python -m unittest discover -s tests` 为 245 项通过，`python -m mypy src tests --strict` 对 73 个源文件零问题。
- 2026-07-19：Standards/Spec 双轴复审均无剩余问题；复审中补齐静态 tick 核心游标、部分放弃保留有效输入、显式反思接管、过期与主动归还后的旧 claim 脱敏。未预做工单 15。
