# MyOutBrain V1 实施地图

## Notes

本地图记录 V1 工单完成后形成的实施上下文指针。

## Decisions-so-far

- 候选洞见保存在可回收的原子 catalog 中；相似候选在写入前合并证据与 recurrence，且不会进入永久知识。见 [04 — 从证据生成候选洞见](issues/04-generate-candidate-insights.md)。

## Fog

- 下一前沿是候选审阅与衍生洞见晋升；拒绝动作将负责产生反思流程已支持读取的轻量抑制指纹。
