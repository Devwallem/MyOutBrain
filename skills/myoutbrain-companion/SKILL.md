---
name: myoutbrain-companion
description: Use a MyOutBrain private instance as Codex's shared long-term memory before and after tasks. Use when Codex should recall task-relevant evidence, answer through the companion loop, submit only currently visible task context with explicit blind spots, or continue work without creating a model-specific user-memory silo.
---

# MyOutBrain Companion

Treat Codex as a replaceable intelligent entrance. Keep identity, provenance, approved knowledge, and long-term memory in the MyOutBrain private instance.

## Before a task

1. Identify the private-instance root and one stable task pointer. Do not invent a pointer from unavailable history.
2. Classify the request as `substantive`, `casual`, or `operation`.
3. For a substantive request, run:

   ```text
   python -m myoutbrain codex-context "<question>" --root <instance> --task-pointer <task> --purpose substantive --access task-scoped --format json
   ```

4. For casual chat or a simple operation, skip memory use. If an auditable decision is useful, call `codex-context` with the matching purpose and honor its `retrieval_performed: false` result.
5. Use only the returned evidence package. Do not open the private database, Vault, object store, indexes, or unrelated task memory directly.
6. Keep `task-scoped` access by default. Use `local-trusted` only when the user explicitly requests broader local recall. Use `public-external` only for an external-safe query; it excludes `local-only` memory.

## Answer and research

- Treat Codex as the current replaceable capability engine. Form the answer from the current visible task plus the returned task evidence when those inputs completely support the conclusion; name the relevant memory IDs or current-task evidence when traceability matters.
- Do not call the CLI `answer` command merely to restate a fact explicitly supplied in the current visible request. Finish the response first, then submit the visible experience through `codex-submit`.
- Query the common-knowledge baseline before public research.
- When a complete answer is unsupported and standardized public supplementation is needed, use the existing `answer` command with an explicitly sanitized `--public-query` and a configured capability provider. Keep the result unknown if research remains insufficient.
- Never change `local-only` to `cloud-allowed`, add `--allow-cloud`, or reconfigure a provider merely to make an answer command succeed. If no authorized provider can process the required evidence, answer only from fully sufficient visible/local evidence or remain unknown.
- Distinguish common knowledge, public evidence, and capability-engine inference. Never treat an unapproved integration proposal as canonical knowledge.
- Use `answer --force-consolidation` only when the task is urgent or consequential and the latest task buffer must be proposed for review before answering.

## After a substantive task

1. Check only for an explicit learning signal: a user correction, confirmed decision, reusable step, repeated failure plus resolution, or research question worth tracking. Message count, token count, task duration, ordinary completion and silence are not signals.
2. If no learning signal exists, do not create a file, submit an input, or start reflection.
3. If a signal exists, create a temporary UTF-8 JSON payload containing only the necessary excerpt, stable source identity/version/locator, SHA-256 source fingerprint, applicability scope, visible coverage and explicit blind spots. Do not copy the full conversation, codebase or tool output.
4. Submit through the shared gateway:

   ```text
   python -m myoutbrain submit-learning-signal <payload-json> --root <instance> --idempotency-key <stable-task-signal-key> --format json
   ```

5. Delete the temporary file after a successful or failed submission. Do not maintain a separate Codex memory file or directly invoke proposal writes.
6. Use `cloud-allowed` only when the excerpt and reference are explicitly safe for external processing. The default is `local-only`.

## Review and continuity

- Use `consolidate --task <task>` to prepare proposals and `review-memory` for natural approval, correction, rejection, or conflict preservation. Never approve on the user's behalf.
- Use `$myoutbrain-reflector` only for explicit immediate reflection. Companion and Reflector coordinate solely through reflection input and proposal identities returned by the gateway.
- Use `pending-consolidation-reviews` when returning to offline work.
- Expect memory IDs and accepted knowledge to survive capability-engine replacement and deletion/rebuild of indexes or Obsidian views. If they do not, stop and report an integrity failure.
