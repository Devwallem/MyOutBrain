# 01 — 验证对话沉淀与经验召回闭环

**Type:** prototype

**Status:** ready-for-human

**Prototype branch:** `codex/dialogue-learning-prototype`

**Question:** 对话只保存一次、提炼产物仅引用 turn ID 时，人工审阅、指纹去重和回答前召回的状态模型是否符合真实使用直觉？

## Run

```powershell
.\.venv\Scripts\myoutbrain-dialogue-prototype.exe
```

## Observe

- 未接受的候选不得参与召回
- 重复对话不得生成第二份相同教训
- 拒绝后只保留轻量指纹
- 无价值闲聊不得生成可复用产物
- 新问题只能召回已经人工接受的经验

## Answer

等待用户亲手运行原型并确认或指出不符合直觉的状态转换。确定性演示已经证明当前模型能够完成一次“捕获 → 提炼 → 接受 → 去重 → 回答前召回”闭环，原始对话只计入一次存储，产物不复制全文。
