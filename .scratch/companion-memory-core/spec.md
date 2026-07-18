# MyOutBrain 第一阶段：同伴记忆核心

Status: ready-for-agent

## Problem Statement

作为单一创作者，用户希望拥有一个长期连续的同伴：可以自然接收其新知识、经历、吐槽和修正，优先调用双方已经接受的知识体系，知识不足时补充公开信息，形成可靠回答，再把本轮产生的新体会用于后续交流。模型、Codex 或其他智能体只是可替换的能力与入口，更换它们不能让同伴失忆或产生相互割裂的用户理解。

现有 V1 已验证原始材料采集、来源引用、候选洞见审阅、个人认知晋升和运行层重建，但永久知识仍以人类维护的 Obsidian Markdown 为中心，对话学习也只存在于一次性内存原型。它尚未提供统一的对话接收、缓冲记忆、语义召回、知识充分性判断、公开检索补充、规范化长期记忆、计划整合与自然审阅闭环。

用户不希望管理大量分类、文件夹、标签或审批表格。第一阶段需要先让整体文本功能真正运行，再考虑人格、情绪识别、多媒体和自主学习。

## Solution

建立一个独立本地核心，作为单一创作者长期知识、经历和认知演变的唯一事实来源。完整文本证据按内容只保存一次；Agent 日常调用紧凑、结构化、带来源和版本的规范记忆。Obsidian 降为可重建的人类审计与修正视图，不再承担唯一永久知识事实。

Codex Skill 成为第一个智能体入口，CLI 继续作为主要验收与运维入口。所有入口通过统一记忆网关查询当前任务所需的最小证据包，并把新经历和体会提交到记忆缓冲区。缓冲内容可以立即参与召回，但不会直接覆盖规范记忆。

召回同时利用稳定关系、全文检索和本地 Embedding 语义候选。能力引擎只在内部证据足以支持完整结论时回答；否则先进行脱敏公开检索并重新判断，仍不足则保持未知并说明缺口。Embedding 只找候选，不决定知识正确性、语义融合或最终答案。

手动、定时或紧急强制整合会召回相似新旧内容，并生成按主题组织的自然语言整合提案。确定性维护可以自动提交；任何改变长期语义的补充、修订、冲突处理、停用或删除必须经用户自然审阅。定时任务可以在范围、敏感度和预算受限的持续授权下使用云端能力，但只能提出语义变化，不能自动写入规范记忆。

## User Stories

1. As a single creator, I want the public program to initialize a private instance, so that application code and personal data can evolve independently.
2. As a single creator, I want initialization to preserve existing private content, so that setup and upgrades cannot overwrite my knowledge.
3. As a single creator, I want Git version control to remain optional, so that creating a private instance never silently creates or publishes repository history.
4. As a single creator, I want the private instance to record its schema version, so that program upgrades can migrate data safely.
5. As a single creator, I want complete text conversations stored once, so that future reinterpretation remains possible without duplicate growth.
6. As a single creator, I want identical content from multiple tasks or entrances to share one source object, so that provenance grows without copying bodies.
7. As a single creator, I want every captured experience to state what context was visible and what history was unavailable, so that the companion never pretends to remember unseen conversations.
8. As a single creator, I want each experience to preserve source, time, entrance, task and sensitivity, so that later use remains traceable and private.
9. As a single creator, I want substantive conversations to produce compact memory digests, so that useful context can accumulate without storing many verbose derivatives.
10. As a single creator, I want one generic memory-digest shape, so that knowledge, experience, method and preference do not require rigid classification.
11. As a single creator, I want buffered memory to be recallable immediately, so that the companion remembers what I said before scheduled consolidation runs.
12. As a single creator, I want buffered memory kept separate from canonical memory, so that recent statements do not silently overwrite established understanding.
13. As a single creator, I want the companion to query our common knowledge baseline before substantive answers, so that it builds on what we already accept.
14. As a single creator, I want casual chat and simple operations to avoid unnecessary retrieval, so that the system remains responsive and economical.
15. As a single creator, I want exact identities and source relations used during recall, so that known relationships do not depend on probabilistic similarity.
16. As a single creator, I want full-text search over canonical and buffered memory, so that names and precise phrases can be found locally.
17. As a single creator, I want semantically equivalent wording to be recalled, so that different expressions of the same understanding are not missed.
18. As a single creator, I want private Embeddings generated locally by default, so that semantic indexing does not require sending my memory to a cloud provider.
19. As a single creator, I want Embedding failure to degrade to local relationship and full-text recall, so that the knowledge system remains usable.
20. As a single creator, I want vector indexes to be rebuildable and provider-versioned, so that changing an Embedding model cannot alter canonical knowledge.
21. As a single creator, I want similarity to produce candidates rather than conclusions, so that nearby vectors cannot automatically merge or validate knowledge.
22. As a single creator, I want the system to decide whether evidence is sufficient for a complete answer, so that fluent partial answers do not hide missing knowledge.
23. As a single creator, I want answerability to be a binary gate, so that incomplete, stale or conflicted evidence is treated as insufficient.
24. As a single creator, I want time-sensitive and high-risk facts rechecked, so that old internal knowledge is not trusted merely because it exists.
25. As a single creator, I want sanitized public search when internal knowledge is insufficient, so that the companion can supplement missing or outdated facts without exposing private context.
26. As a single creator, I want private context sent to external services only within explicit authorization, so that public search does not become knowledge-base exfiltration.
27. As a single creator, I want answers to distinguish common knowledge, public evidence and companion inference, so that important claims remain traceable.
28. As a single creator, I want the system to remain unknown after unsuccessful research, so that it does not manufacture a conclusion from gaps.
29. As a single creator, I want verified facts, unresolved gaps and next validation steps shown when no conclusion is possible, so that research can continue productively.
30. As a single creator, I want each substantive answer to produce at most a compact memory update, so that repeated explanation does not multiply storage.
31. As a single creator, I want new memory linked to original evidence rather than copying conversation text, so that storage remains approximately linear in unique sources.
32. As a single creator, I want manual consolidation, so that I can update knowledge immediately when I choose.
33. As a single creator, I want scheduled consolidation, so that the system can prepare memory integration without waiting for an active conversation.
34. As a single creator, I want forced consolidation for urgent tasks, so that relevant recent memory can be integrated before a consequential answer.
35. As a single creator, I want scheduled cloud analysis constrained by provider, model, sensitivity, batch and budget, so that unattended work cannot silently expand authority or cost.
36. As a single creator, I want local-only content excluded from scheduled cloud work, so that the strongest privacy designation always wins.
37. As a single creator, I want deterministic deduplication and index maintenance applied automatically, so that machine housekeeping does not require approval.
38. As a single creator, I want every semantic change expressed as an integration proposal, so that canonical understanding changes only through my decision.
39. As a single creator, I want similar buffered items grouped into a small number of topic proposals, so that review does not become repetitive administration.
40. As a single creator, I want to accept, edit, reject or preserve conflict through natural conversation, so that review feels like working with a companion rather than operating a database.
41. As a single creator, I want integration proposals delivered immediately after completion, so that finished work is not hidden until an unrelated future conversation.
42. As a single creator, I want a local notification when scheduled integration finishes offline, so that I can open the pending natural review directly.
43. As a single creator, I want approved knowledge revised under a stable identity, so that current understanding can change without multiplying near-duplicate records.
44. As a single creator, I want old versions and reasons for change preserved, so that I can audit how understanding evolved.
45. As a single creator, I want unresolved conflicts kept side by side, so that the system does not invent certainty by silently merging disagreement.
46. As a single creator, I want original evidence left unchanged by integration, so that later models can reinterpret it independently.
47. As a single creator, I want Obsidian views generated from canonical memory, so that I can inspect important knowledge without making Markdown the machine truth.
48. As a single creator, I want deleted Obsidian views rebuilt, so that a human projection is never irreplaceable storage.
49. As a single creator, I want edits made through the human view submitted as new evidence, so that manual correction follows the same review and provenance rules.
50. As a single creator, I want to ask why the companion believes something, so that it can expose sources, confirmation state and evolution through natural conversation.
51. As a single creator, I want “forget” to deactivate memory without destroying provenance by default, so that reversible control remains easy.
52. As a single creator, I want explicit permanent erasure to remove source, derivatives and future backup scope, so that sensitive memory can truly be deleted.
53. As a single creator, I want storage usage separated into evidence, canonical memory, buffer and rebuildable indexes, so that growth remains understandable.
54. As a single creator, I want large original evidence protected from automatic deletion, so that capacity optimization cannot destroy proof without approval.
55. As a single creator, I want models and tools treated as replaceable capability engines, so that changing them does not interrupt memory continuity.
56. As a single creator, I want every agent entrance to share the same private memory core, so that no model develops an isolated version of me.
57. As a single creator, I want entrances to receive only task-relevant evidence, so that shared memory does not mean universal disclosure.
58. As a single creator, I want all agent updates submitted through one memory gateway, so that long-term writes remain coordinated and auditable.
59. As a Codex user, I want the current visible task context submitted with explicit blind spots, so that MyOutBrain can learn from our work without claiming access to unavailable task history.
60. As a single creator, I want the complete receive-recall-research-answer-write-review-recall loop demonstrated end to end, so that later personality and autonomous-learning work builds on a functioning core.

## Implementation Decisions

- Build an independent local-core module that owns private-instance initialization, canonical memory, source objects, locking, migrations, retrieval orchestration, consolidation, review, audit and storage reporting behind one small interface.
- Evolve the existing deep knowledge-workflow module rather than adding a parallel pass-through layer. Preserve reusable content-addressing, writer locking, provider authorization, event auditing and reconstruction behavior.
- Use SQLite as the canonical-memory store for stable identities, normalized memory, buffered digests, source relations, versions, conflicts, integration proposals and review decisions.
- Keep complete source bodies in content-addressed object storage. Canonical memory stores source pointers and compact normalized content rather than duplicate bodies.
- Keep retrieval indexes, model caches and generated Obsidian views rebuildable. Neither an Embedding index nor a human projection is a source of truth.
- Represent buffered learning as one generic digest with compact content, source scope, time, entrance, task, sensitivity, state and fingerprint. Classification may be inferred dynamically but is not permanent identity.
- Allow buffered digests to participate in recall before integration while preventing them from directly replacing canonical memory.
- Add one memory-gateway interface as the reusable seam for Codex and future entrances. It supports task-scoped context retrieval and source-linked experience submission; callers do not know storage or index details.
- Use three fixed memory-gateway access levels in the first stage: local trusted, task scoped and public external. Do not build custom per-note ACLs.
- Build recall as a deep module that combines stable relationships, SQLite full-text search and Embedding candidates behind one interface.
- Provide a local Embedding adapter and a deterministic test adapter at a real seam. Cloud Embedding remains optional and requires explicit instance configuration; local-only data is never eligible.
- Store Embedding provider, model, dimensions and normalized-representation version with every rebuildable index generation. Never mix vector spaces.
- Treat candidate similarity as retrieval evidence only. A capability engine considers source, time, version and conflict before proposing semantic relationships.
- Make answerability binary. Relevant but incomplete evidence is insufficient and cannot produce a synthesized conclusion.
- Search the public web with sanitized queries after internal insufficiency. Supplying private evidence to an external model follows existing explicit authorization and sensitivity rules.
- Keep answers natural while retaining internal provenance. Surface key sources automatically for changing, disputed, high-risk or web-derived claims.
- Write post-answer learning to the buffer first. No model response can directly become canonical knowledge or personal cognition.
- Support manual, scheduled and urgent forced consolidation. Do not infer idle state or account quota.
- Allow scheduled cloud analysis only under a revocable standing authorization that fixes provider, model, allowed sensitivity, batch size, token ceiling and cost ceiling.
- Permit deterministic maintenance to commit automatically. Any change to long-term semantics becomes an integration proposal and requires natural review.
- Apply accepted changes transactionally under stable knowledge identities. Preserve source links, previous versions, supersession reasons and unresolved conflicts.
- Generate Obsidian knowledge views from canonical memory and route human edits back through evidence capture and review.
- Deliver completed proposals immediately. Use the active conversation when available and a local notification plus pending-review queue otherwise. External notification channels require separate authorization.
- Keep the companion identity, memory and relationship history in the private instance. Models, tools and Codex are replaceable capability engines or entrances.
- Use Codex Skill as the first intelligent entrance and CLI as the primary acceptance and operational entrance. The Skill records only actually visible context and stable task pointers.
- Defer personality presets, emotion inference, voice imitation, multimedia ingestion, production multi-entrance adapters and autonomous network learning.

## Testing Decisions

- Use the existing CLI as the highest primary acceptance seam. Tests execute full user workflows and assert exit status, output, durable behavior and rebuildability rather than internal table shapes.
- Add only one new reusable programmatic seam: the memory-gateway interface consumed by the Codex adapter and future entrances.
- Test the local-core and memory-gateway modules through their interfaces. Do not expose SQLite repositories, FTS queries, vector stores or projection writers merely for testing.
- Use a real temporary SQLite database in tests because it is a local-substitutable dependency and transaction behavior is part of the module implementation.
- Use deterministic adapters for Embedding, generation, public search, Obsidian and notifications. Default tests do not access real networks, providers or desktop applications.
- Keep a small number of contract tests for real adapters, following the existing OpenAI and Obsidian adapter-test prior art.
- Preserve existing CLI acceptance coverage for initialization, content-addressed capture, privacy, locking, audit, provider failure and runtime reconstruction while the new core replaces Vault-as-truth behavior.
- Add acceptance scenarios for immediate buffered recall, semantic synonym recall, binary answerability, sanitized public-search fallback, unknown-after-search, proposal-only scheduled integration, natural review, stable revisions, conflict preservation, deactivation, permanent erasure, view rebuild and storage reporting.
- Extend the existing versioned recall evaluation with synonyms, unintegrated buffer content, stale knowledge, unresolved conflicts and superseded versions. Keep the lexical retriever as a comparison baseline.
- Verify that deleting all rebuildable indexes and Obsidian views does not change observable canonical-memory behavior after reconstruction.
- Verify that an interrupted or failed consolidation leaves either the prior complete state or the approved new complete state, never a partially applied semantic change.
- Verify that scheduled cloud requests exclude local-only evidence, obey batch and budget limits, create audits and cannot mutate canonical memory without review.
- Verify that switching deterministic capability-engine adapters preserves memory identity, source relationships and accepted knowledge.
- Prefer replace-don't-layer tests: once behavior is covered through the new deep interface, remove obsolete tests that assert the former Vault-canonical implementation.

## Out of Scope

- Personality systems, installable personas, OpenClaw SOUL compatibility, emotion inference and user-voice simulation.
- Audio, video, image, OCR, PDF and other large-object ingestion workflows beyond preserving existing source-object behavior.
- Production web, mobile, chat-platform and multi-device adapters.
- Unbounded autonomous web learning or automatically chosen background work based on perceived idle time or model quota.
- Automatic semantic merging, automatic personal-cognition promotion or model-owned long-term memory.
- A full RAG platform, vector database as truth, graph-wide pairwise materialization or similarity-based answer generation.
- Multi-user access, shared knowledge ownership and collaborative conflict resolution between people.
- External messaging, publishing, purchasing or other consequential actions.
- Complete public-release repository extraction from the current permanently private Git history.
- Full backup-provider automation and key-management user interfaces, although permanent and rebuildable scopes must remain explicit.

## Further Notes

- This specification supersedes the old product assumption that Obsidian Markdown is the permanent knowledge fact source; ADR-0042 records normalized canonical memory and rebuildable human views.
- The existing dialogue-learning prototype remains useful as evidence for capture, buffer, review and recall state transitions, but its rigid artifact types and in-memory-only implementation are not the target design.
- The existing lexical recall dataset remains a baseline, not the final retriever. Embedding is introduced only as a rebuildable semantic-candidate source.
- The public distribution will eventually contain the reusable program, empty structures, synthetic examples and generic configuration. A private instance contains all real conversations, canonical memory, fitted preferences and history.
- First-stage success means a complete text loop works reliably before adding personality or attempting to make the companion feel more human.
