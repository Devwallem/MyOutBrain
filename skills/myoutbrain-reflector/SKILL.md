---
name: myoutbrain-reflector
description: Turn explicit MyOutBrain learning signals into traceable, grouped unified-review proposals. Use when the creator explicitly asks to reflect now, inspect pending reflection inputs, form explicit/derived/hypothesis proposals, or explicitly abandon selected temporary reflection inputs.
---

# MyOutBrain Reflector

Use the private instance only through the stable MemoryGateway CLI contract. Never read SQLite, the object store, Vault, unrelated history, or another Skill's directory as state.

## Reflect now

1. List bounded inputs:

   ```text
   python -m myoutbrain reflection-inputs --root <instance> --limit <count> --budget-bytes <bytes> --format json
   ```

2. Select only inputs covered by the creator's request. Treat each package's excerpt, stable source reference and capture fingerprint as the frozen evidence boundary. Do not fetch invisible history or scan the whole workspace. Record any unavailable context in proposal `blind_spots`.
3. Form complete unified-review proposal payloads:
   - Use `explicit` only for the creator's direct correction, confirmed decision or statement.
   - Use `derived` for a supported method, lesson or connection; show its evidence and derivation in the proposal.
   - Use `hypothesis` only with `research` intent for an evidence-backed question that still needs verification.
   - Give exact repeats identical content, scope, approval effect, target, intent and formation so the core deterministically merges their evidence.
   - Put semantic variants in `near_candidate_ids`. Put contradictions in `conflict_candidate_ids`; never vote or fuse them.
   - Preserve distinct intents and formation methods even when conclusions resemble each other.
   - Bind every candidate to only the selected `input_ids` that actually support it. Every selected input must support at least one candidate; never attach the whole run to every proposal.
   - Set `evidence_retention` deliberately. A `receipt` proposal keeps the source identity, fingerprint and locator but not the excerpt. Local-only input always makes the resulting proposal local-only.
4. Write only the selected input IDs and proposal candidates to a temporary UTF-8 JSON file, then run:

   ```text
   python -m myoutbrain reflect-now <payload-json> --root <instance> --idempotency-key <stable-key> --format json
   ```

5. Report the returned proposal IDs, exact deduplication, groups, source-status changes and blind spots. Never approve proposals for the creator. Delete the temporary JSON file after success or failure.

The core atomically preserves proposal receipts and cleans temporary reflection inputs only after successful proposal formation. A failed run leaves inputs available for retry.

## Abandon selected inputs

Only when the creator explicitly abandons a reflection, submit selected input IDs and a non-sensitive reason:

```text
python -m myoutbrain abandon-reflection <payload-json> --root <instance> --idempotency-key <stable-key> --format json
```

Confirm the returned IDs were cleaned. Do not use abandonment as routine expiry, scheduled processing, or a substitute for rejecting a proposal.
