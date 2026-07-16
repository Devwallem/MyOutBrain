# MyOutBrain V1 实施地图

## Notes

本地图记录 V1 工单完成后形成的实施上下文指针。

## Decisions-so-far

- 候选洞见保存在可回收的原子 catalog 中；相似候选在写入前合并证据与 recurrence，且不会进入永久知识。见 [04 — 从证据生成候选洞见](issues/04-generate-candidate-insights.md)。
- 证据召回以版本化离线评测集和共享检索契约度量，生成文风不能掩盖召回错误；当前明确采用无嵌入词法基线，只有持续的代表性失败才触发更复杂 RAG。见 [08 — 建立证据召回评测](issues/08-evaluate-evidence-recall.md)。

## Fog

- V1 实施工单已全部解决；下一阶段应以真实个人材料扩充召回评测集，再根据重复失败决定是否引入嵌入、重排或其他检索能力。
