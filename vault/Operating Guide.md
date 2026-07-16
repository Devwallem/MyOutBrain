# Operating Guide

## Daily Loop

1. 在 [[Capture Queue]] 记录待采集 Markdown 的位置。
2. 采集原始材料，并保存返回的稳定来源身份。
3. 针对单一来源查询或反思，检查引用是否能够人工核验。
4. 使用 `review` 审阅候选洞见；接受、拒绝或暂缓。
5. 只有当衍生洞见确实代表本人判断时，才执行 `promote`。
6. 在 [[Creative Works Index]] 组织基于已验证认知的表达。

## Commands

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m myoutbrain capture <markdown-path> --sensitivity local-only --root .
.\.venv\Scripts\python.exe -m myoutbrain ask <source-id> "<question>" --root .
.\.venv\Scripts\python.exe -m myoutbrain reflect <source-id> "<prompt>" --root .
.\.venv\Scripts\python.exe -m myoutbrain review --root .
.\.venv\Scripts\python.exe -m myoutbrain promote <insight-id> --title "<title>" --root .
.\.venv\Scripts\python.exe -m myoutbrain rebuild --root .
.\.venv\Scripts\python.exe -m myoutbrain evaluate-recall evaluation\recall-baseline.json
```

云端调用还需要当次显式加入 `--allow-cloud`；`local-only` 材料即使获得通用授权也不会外发。

## Storage Boundaries

- `vault/`：索引与人可读的永久知识
- `store/`：去重原始对象、轻量记录和知识演变事件
- `runtime/`：可删除并通过 `rebuild` 恢复的投影、缓存和候选工作区

索引笔记不带系统 `id:` 元数据，重建时会被安全忽略。不要手工伪造正式知识笔记的身份或状态；让 MyOutBrain 工作流创建和迁移它们。

## Related

- [[MyOutBrain Home]]
- [[Capture Queue]]
- [[Knowledge Index]]
