# 11 — 安装三种入口并协商协议能力

**What to build:** 创作者可以为 Codex、OpenCode 和 Claude Code 安装可替换入口，三者通过同一 MCP/CLI 领域契约使用同一个主私人实例，并在写入前协商版本与能力。

**Blocked by:** 03 — 召回知识并查看召回日志; 07 — 完成统一审阅提案与批次决定.

**Status:** resolved

- [x] MCP 与 CLI 对相同请求产生同构领域响应和稳定错误类别。
- [x] 三种入口可以幂等安装、重装、检查和卸载。
- [x] 入口目录不保存规范数据，卸载不修改私人实例。
- [x] 主版本不兼容时拒绝语义写入，旧次版本可以兼容读取。
- [x] 入口无法理解提案效果时不能批准，并且不得旁路访问 SQLite。
- [x] 写操作使用幂等键和 `expected_version` 进行并发保护。

## Answer

工单 11 tracer bullet 已完成。MCP stdio 与 CLI gateway 共用版本化领域 request/response schema、协商逻辑和稳定错误信封；协议按客户端范围与服务端支持区间的交集协商，审阅批准要求入口声明精确的效果能力，所有语义写入必须携带幂等键与 `expected_version`，并只经 MemoryGateway/本地核心访问规范状态。

Codex、OpenCode 与 Claude Code 入口通过本地统一注册表发现同一个主私人实例，支持幂等安装、重装、实际协商检查和安全卸载。配置与 Skill 带受管所有权标识，安装器拒绝覆盖或删除未受管入口；入口目录只保存可替换配置和无状态 Skill，不保存 SQLite、对象、Vault 或生成视图。

## Comments

- 2026-07-18：新增传输中立的 V2.1 领域协议、可打包 JSON Schema、`gateway` CLI 与 MCP stdio `myoutbrain_gateway` 工具；相同请求在两种传输上返回同构成功或稳定错误响应，V2.0 客户端仍可读取。
- 2026-07-18：`review.decide` 在本地核心边界验证完整提案效果、精确 `review_effect.<type>.v1` 能力、提案版本、幂等键和 `expected_version`；入口及 MCP 层均不查询 SQLite。
- 2026-07-18：新增三种客户端的受管配置与通用无状态 Skill。本地实例注册表固定一个主实例；安装、重装、检查和卸载保持用户原配置，拒绝覆盖/删除未受管同名入口，卸载前后规范实例快照不变。
- 2026-07-18：按公开 CLI/领域接缝 TDD；44 项相关及依赖回归通过，严格 `mypy` 覆盖 66 个源/测试文件通过。最终完整套件 210 项中 205 项通过；5 项失败均为固定起点已存在并由工单 01/02/03/07/08 记录的 `AnswerWithPublicResearchFallbackTests` 公共检索时效 fixture。
- 2026-07-18：按 `code-review` 完成 Standards/Spec 双轴审查并修复全部实质发现：主实例发现与并发 compare-and-set、真实协议/能力协商检查、版本范围交集、Schema/解析器一致性、受管入口所有权、客户端 profile 和稳定错误信封去重；未实施工单 12/13/15 或计划调度。
