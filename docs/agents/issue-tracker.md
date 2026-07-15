# 事项跟踪器：本地 Markdown

本仓库的事项与规格说明（spec，也可理解为 PRD）以 Markdown 文件形式存放在 `.scratch/` 中。

## 约定

- 每个功能使用一个目录：`.scratch/<feature-slug>/`
- 规格说明文件为 `.scratch/<feature-slug>/spec.md`
- 每张实施工单使用单独文件：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号；不得把所有工单合并到一个文件中
- 分流状态写在事项文件顶部附近的 `Status:` 行中（角色字符串见 `triage-labels.md`）
- 评论与讨论历史追加到文件底部的 `## Comments` 标题下

## 当技能要求“发布到事项跟踪器”时

在 `.scratch/<feature-slug>/` 下创建新文件；目录不存在时一并创建。

## 当技能要求“获取相关工单”时

读取所引用路径中的文件。用户通常会直接提供文件路径或事项编号。

## 寻路操作

供 `/wayfinder` 使用。**地图（map）**文件对应每张工单的一个**子工单（child）**文件。

- **地图**：`.scratch/<effort>/map.md`——记录 Notes、Decisions-so-far 与 Fog 正文
- **子工单**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 开始编号，正文中写明问题；`Type:` 行记录工单类型（`research`/`prototype`/`grilling`/`task`），`Status:` 行记录 `claimed`/`resolved`
- **阻塞关系**：在文件顶部附近写 `Blocked by: NN, NN`；列出的所有工单均为 `resolved` 后，该工单才解除阻塞
- **前沿（Frontier）**：扫描 `.scratch/<effort>/issues/`，寻找处于开放、未阻塞且未认领状态的文件；编号最小者优先
- **认领**：开始工作前，将 `Status:` 更新为 `claimed` 并保存
- **解决**：在 `## Answer` 标题下追加答案，将 `Status:` 更新为 `resolved`，然后在 `map.md` 的 Decisions-so-far 中追加上下文指针（摘要与链接）
