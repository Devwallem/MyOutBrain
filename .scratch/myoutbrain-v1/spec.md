# MyOutBrain V1：单篇 Markdown 的认知闭环

Status: ready-for-agent

## Problem Statement

作为单一创作者，用户希望建立一个能够持续积累、快速调用、不断反思并逐步贴合本人知识、判断和表达方式的个人认知库，最终让系统从协作搭档演进为必须经本人确认发布的影子作者。

当前项目只有领域语言和架构决定，尚无一条真正可运行的知识闭环。普通文件仓库无法区分原始材料、个人认知、AI 产生的候选洞见和创作成果；简单地把全部内容交给模型又会带来隐私泄露、来源丢失、重复存储和 AI 自我强化等风险。用户需要先验证一个足够小但端到端完整的工作流，证明知识能够安全采集、带来源查询、产生候选洞见、经人工晋升并在删除运行数据后恢复，而不是一开始建设复杂的 RAG 平台。

## Solution

第一版提供一个本地 Python 命令行工具，以单篇 Markdown 为首个输入，打通初始化、采集、查询、反思、审阅和重建六个动作。

Obsidian Vault 保存人可读的永久知识；永久对象存储保存按内容去重的原始材料和轻量记录；运行存储保存可重建的目录、全文索引、候选工作区、缓存和日志。每份来源和知识笔记都有不依赖文件名的稳定身份，知识笔记使用 YAML 属性表达类型、状态、作者属性、敏感级别、来源和演变关系。

查询直接以单篇 Markdown 形成最小证据包，不引入嵌入、向量数据库或复杂 RAG。OpenAI Responses API 是首个生成适配器，但模型能力通过提供方中立接口访问。任何云端调用都必须遵守当次外发授权和内容敏感级别，并留下审计记录。

反思只能产生临时候选洞见。候选洞见经用户确认后才能成为永久衍生洞见，衍生洞见再次经用户明确认可后才能成为个人认知。系统不得自行完成任何晋升。

## User Stories

1. As a single creator, I want to initialize MyOutBrain in my private project, so that the knowledge workflow has a known and valid starting state.
2. As a single creator, I want initialization to preserve existing files, so that starting MyOutBrain cannot overwrite my work.
3. As a single creator, I want initialization to report missing Obsidian CLI setup clearly, so that I know how to complete the local integration.
4. As a single creator, I want the system to distinguish permanent data from rebuildable runtime data, so that I know what must be backed up.
5. As a single creator, I want raw private objects and runtime data excluded from normal Git tracking, so that large or sensitive machine data does not enter repository history accidentally.
6. As a single creator, I want to capture one Markdown document from the command line, so that I can begin building my personal cognitive library with the smallest useful workflow.
7. As a single creator, I want captured content to retain its original bytes and provenance, so that I can always return to the source material.
8. As a single creator, I want every captured source to receive a stable identity, so that renaming a file does not break references.
9. As a single creator, I want identical content captured twice to reuse the existing object, so that storage does not grow through duplication.
10. As a single creator, I want duplicate capture to report that the source already exists, so that idempotent behavior is visible rather than confusing.
11. As a single creator, I want the same content under a different filename to be recognized as identical, so that filenames do not defeat deduplication.
12. As a single creator, I want empty or unreadable Markdown rejected without partial writes, so that invalid sources cannot corrupt the library.
13. As a single creator, I want source records to include creation time, content hash and origin, so that provenance can be inspected later.
14. As a single creator, I want to mark whether knowledge is local-only or eligible for cloud use, so that sensitive material cannot leave my computer accidentally.
15. As a single creator, I want missing sensitivity metadata to be handled explicitly, so that privacy behavior is never inferred silently.
16. As a single creator, I want to ask a natural-language question about the captured Markdown, so that I can quickly call on the knowledge I stored.
17. As a single creator, I want each answer to be grounded in a minimal evidence package, so that the model sees only what the request requires.
18. As a single creator, I want every material claim in an answer linked to its source identity and locator, so that I can verify it.
19. As a single creator, I want the system to say when the evidence is insufficient, so that fluent model output is not mistaken for stored knowledge.
20. As a single creator, I want local-only content excluded from cloud requests even when I accidentally authorize cloud use generally, so that the strongest privacy rule wins.
21. As a single creator, I want cloud transmission to require explicit authorization for the current request, so that an earlier approval does not become permanent consent.
22. As a single creator, I want each external model call audited by time, provider, model, purpose and source identities, so that I can review data egress.
23. As a single creator, I want API credentials read from the environment, so that secrets are never written to the repository, Vault or logs.
24. As a single creator, I want model failure to leave permanent knowledge unchanged, so that network or provider errors cannot create half-finished records.
25. As a single creator, I want to request reflection on the captured material, so that the system can propose new connections, questions and hypotheses.
26. As a single creator, I want every candidate insight to show supporting evidence, opposing evidence when present and its derivation, so that I can judge it critically.
27. As a single creator, I want reflection output to remain temporary by default, so that AI speculation cannot pollute permanent knowledge.
28. As a single creator, I want materially similar candidate insights merged, so that repeated reflection does not create a growing pile of duplicates.
29. As a single creator, I want repeated appearances of a candidate recorded, so that recurrence can inform review without duplicating content.
30. As a single creator, I want candidate insights to have a configurable expiry policy, so that abandoned AI output can be reclaimed.
31. As a single creator, I want to review candidates from the command line, so that the complete workflow works before a custom graphical interface exists.
32. As a single creator, I want to accept a candidate as a derived insight, so that worthwhile ideas can become permanent without being treated as my beliefs.
33. As a single creator, I want to edit a candidate before accepting it, so that permanent wording reflects my judgment.
34. As a single creator, I want to defer a candidate, so that uncertainty does not force acceptance or rejection.
35. As a single creator, I want to reject a candidate, so that poor ideas are removed from active review.
36. As a single creator, I want rejected candidates to retain only a lightweight fingerprint, so that the same rejected idea is not proposed repeatedly without retaining unnecessary text.
37. As a single creator, I want to promote a derived insight to personal cognition through a separate explicit action, so that only statements I endorse represent me.
38. As a single creator, I want AI-authored content prevented from becoming personal cognition automatically, so that personal fitting data remains trustworthy.
39. As a single creator, I want accepted knowledge written as readable Obsidian Markdown, so that it remains useful without MyOutBrain.
40. As a single creator, I want knowledge-note filenames to remain human-readable while identity stays stable, so that I can rename and organize notes safely.
41. As a single creator, I want accepted notes opened in Obsidian when the CLI is available, so that I can inspect and continue editing immediately.
42. As a single creator, I want successful permanent writes to use atomic replacement, so that interruption cannot leave truncated knowledge notes.
43. As a single creator, I want concurrent writers rejected with a clear lock message, so that the single-writer invariant is protected.
44. As a single creator, I want updates to supersede old cognition rather than erase it, so that the evolution of my thinking remains traceable.
45. As a single creator, I want every capture, review, promotion and supersession recorded as an event, so that knowledge evolution can be audited.
46. As a single creator, I want to delete all runtime data and rebuild it from permanent knowledge, so that indexes and catalogs never become irreplaceable truth.
47. As a single creator, I want rebuild to produce the same observable library state, so that recovery is deterministic.
48. As a single creator, I want rebuild failures to identify the invalid permanent record without damaging valid records, so that recovery problems are diagnosable.
49. As a single creator, I want command output to distinguish success, user error, configuration error and provider failure, so that automation and humans can respond correctly.
50. As a single creator, I want a small recall evaluation set with expected sources and unanswerable questions, so that future retrieval complexity is driven by evidence.
51. As a single creator, I want retrieval evaluation to score evidence selection separately from answer fluency, so that a model cannot hide poor recall by guessing correctly.
52. As a single creator, I want the first workflow to remain usable without embeddings, so that vector infrastructure is not required before it proves necessary.
53. As a single creator, I want future model and embedding providers replaceable, so that the personal cognitive library is not locked to one API.
54. As a single creator, I want permanent knowledge to be included in encrypted backup scope while runtime data is excluded, so that disaster recovery remains compact and understandable.
55. As a single creator, I want the first workflow to establish a trustworthy foundation for a future collaborative partner, so that later creative capabilities build on verified personal cognition rather than accumulated AI guesses.

## Implementation Decisions

- The only primary external interface is the `myoutbrain` CLI. It exposes initialization, capture, ask, reflect, review and rebuild operations.
- The CLI delegates to one deep knowledge-workflow module. Storage layout, hashing, atomic writes, event recording, model calls and Obsidian control remain hidden behind this interface.
- The first supported input is one local Markdown document. PDF, Word, URL and AI-conversation adapters are deferred even though they are planned future inputs.
- Raw source bytes are immutable and content-addressed with SHA-256. A repeated hash reuses the existing object rather than writing another copy.
- Permanent data consists of the Obsidian knowledge layer, immutable source objects, lightweight records and the knowledge-evolution journal. Runtime catalogs, indexes, extracted representations, candidates, caches and operational logs are rebuildable or reclaimable according to policy.
- The SQLite catalog is a query projection rather than a source of truth. Rebuild derives it from permanent records and Vault metadata.
- Each source, knowledge note, candidate and event has a stable opaque identity independent of filename. Identities use type-specific prefixes and remain unchanged across rename or movement.
- Permanent knowledge-note metadata includes identity, kind, state, authorship, sensitivity, creation time, update time, source identities and supersession relationships.
- Supported permanent knowledge kinds are source reference, personal cognition, derived insight and creative work. Retrieval fragments are never permanent knowledge notes.
- Supported states include active, superseded and archived. Superseding knowledge preserves the previous item and records the relationship.
- Supported authorship values distinguish user, system and mixed authorship. Only an explicit user action may establish personal cognition.
- Supported sensitivity values distinguish local-only content from content eligible for an explicitly authorized cloud request. Missing or invalid sensitivity cannot silently become cloud-eligible.
- The first ask workflow creates an evidence package directly from the captured Markdown. It does not use embeddings, a vector database, reranking, a knowledge graph or model-hosted file search.
- Evidence-package entries carry stable source identity and a human-verifiable locator. Generated answers must associate material claims with those entries and must admit when the evidence cannot answer the question.
- Generation uses a provider-neutral interface with an OpenAI Responses API adapter first. Provider name and model are configuration, not domain state.
- Embedding, reranking, transcription and other model abilities have separate provider interfaces. The first workflow does not implement an embedding adapter or vector index.
- API secrets are obtained from process environment or an operating-system credential mechanism. They are never persisted in permanent records, Vault metadata, audit payloads or normal logs.
- A cloud request requires both per-request authorization and cloud-eligible source sensitivity. The model adapter receives only the evidence package, never unrestricted Vault access.
- External-call audit records contain time, provider, model, purpose, referenced source identities and a non-secret request fingerprint. Audit must not create another full copy of source content.
- Reflection returns candidates without writing to permanent knowledge. Each candidate contains supporting evidence, contrary evidence when found, a derivation summary, recurrence count, state and expiry metadata.
- Candidate similarity handling prevents repeated storage of materially equivalent proposals. Rejection preserves a compact fingerprint sufficient to suppress immediate repetition while allowing candidate text to expire.
- Candidate review supports accept as derived insight, edit then accept, defer and reject. Promotion from derived insight to personal cognition is a separate explicit operation.
- Neither a generation provider nor reflection logic can call the permanent promotion operation without a user decision supplied through the CLI.
- Accepted knowledge is written as ordinary Obsidian-compatible Markdown and opened through an Obsidian CLI adapter when available. Failure to open the UI does not roll back an otherwise valid permanent write.
- Obsidian CLI is an external adapter. The knowledge workflow does not depend on Obsidian internals and remains able to create valid Markdown when Obsidian is unavailable.
- The system assumes one user, one Windows primary device and one writer. Mutations acquire a project lock and use write-to-temporary plus atomic replacement semantics.
- Command failures use stable categories and nonzero exit statuses for user input, configuration, lock, provider, integrity and unexpected failures. Partial permanent mutations are prevented or recoverable from the event journal.
- Git history remains private. Code, configuration, domain documentation, Vault Markdown and lightweight permanent records are eligible for version control; raw objects and runtime data are excluded.
- Backup automation is not part of the first workflow, but the storage classification must make the permanent backup set unambiguous and exclude rebuildable runtime data.
- Retrieval complexity is progressive. A recall evaluation set records expected evidence, missing evidence, incorrect evidence and explicitly unanswerable questions. Embeddings are added only after observed recall failures justify them.
- The design must keep storage growth proportional to unique source content plus a bounded amount of metadata and runtime projection. It must not materialize all pairwise knowledge relationships or retain every generated query result.

## Testing Decisions

- The `myoutbrain` CLI is the single primary acceptance seam. Tests execute commands as a user would and assert only exit status, standard output, standard error and durable observable results.
- Acceptance scenarios run in an isolated temporary project root. They do not inspect private module state or assert a particular internal class structure.
- OpenAI and Obsidian CLI use deterministic fake adapters in acceptance tests. The fake generation adapter records the exact evidence package and authorization it receives, enabling privacy assertions without network calls.
- A small number of adapter contract tests verify request and response translation for the real OpenAI adapter and command translation for the real Obsidian adapter. Network-dependent tests are explicitly opt-in and are not required for the default suite.
- Capture tests cover first import, exact duplicate import, same content under another filename, changed content, missing file, empty content, unreadable content and interruption before commit.
- Identity tests verify that stable identities survive human-readable filename changes and that all cross-record references resolve after rebuild.
- Privacy tests verify that local-only content never reaches the model adapter, cloud-eligible content still requires current authorization, secrets never appear in output or logs, and audits do not duplicate source bodies.
- Ask tests verify grounded answers, claim-to-evidence association, insufficient-evidence behavior, provider failure and absence of permanent mutations on failure.
- Reflection tests verify support and opposition evidence, candidate-only persistence, similarity merging, recurrence counting, expiry metadata and suppression of recently rejected duplicates.
- Review tests verify every allowed decision and every forbidden state transition, especially that system-authored content cannot bypass explicit user promotion to personal cognition.
- Atomicity tests simulate interruption at mutation boundaries and verify that observers see either the old complete state or the new complete state, never a truncated intermediate state.
- Locking tests start competing mutations and verify that exactly one writer proceeds while the other receives the documented lock failure.
- Rebuild tests delete all runtime projections, rebuild them from permanent data and compare behavior through the CLI before and after rebuild.
- Integrity tests introduce one malformed permanent record and verify that rebuild identifies it, preserves valid data and exits with the integrity failure category.
- Storage tests verify that repeated capture and repeated reflection do not create duplicate source bodies or unbounded candidate copies.
- Recall evaluation tests score evidence selection independently from generated prose and include answerable, unanswerable, conflicting and superseded-knowledge cases.
- Tests are written against observable behavior and invariants, not implementation details. A refactor that preserves the CLI interface and durable outcomes must not require rewriting the acceptance suite.
- There is no existing application-test prior art in the repository. The first acceptance harness establishes the convention for later input, embedding and interface adapters.

## Out of Scope

- PDF, Word, webpage, AI-conversation, email, chat, image, OCR, audio or video ingestion.
- Embeddings, vector databases, hybrid search, reranking, graph retrieval, agentic retrieval or any other full RAG infrastructure.
- Fine-tuning, preference optimization or training a personalized model.
- Producing publishable creative work or operating as an影子作者.
- Obsidian custom plugins, sidebars, cards or other rich embedded interfaces.
- Multi-user access, mobile editing, multi-device writing, synchronization and conflict resolution.
- Automated background folder watching or scheduled ingestion.
- Automatic promotion of any AI-produced content into permanent knowledge.
- Automated encrypted backup execution, remote storage-provider integration and key-management tooling.
- Local model execution, provider routing, fallback chains, quotas or cost optimization beyond keeping the provider interface replaceable.
- Multiple Markdown sources and corpus-wide retrieval; the first workflow validates one captured Markdown end to end.
- Public repository support or migration of private Git history into a public repository.
- Production deployment, remote servers, browser interfaces or desktop packaging.

## Further Notes

- The product north star remains a协作搭档 that may later evolve into an影子作者, but this specification intentionally establishes only the trustworthy knowledge foundation.
- Obsidian is the human knowledge layer, not the storage or retrieval engine. Permanent Markdown must remain useful when MyOutBrain is not running.
- Fine-tuning and retrieval solve different future problems: fitting may learn style and judgment patterns, while evidence selection supplies changing knowledge and precise provenance.
- The repository currently has no application implementation, so all modules and tests described here are new.
- The Obsidian CLI was not available on PATH during discovery. Implementation must provide a clear diagnostic for installing a current Obsidian desktop version, enabling its command-line interface and registering it on Windows.
- The OpenAI Developer Docs MCP was registered during design work and will become available to new Codex tasks after restart. The existing global Codex configuration still contains a `service_tier` value that the current CLI rejects; this is unrelated to the product specification and was not modified.
