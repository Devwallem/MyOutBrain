---
name: myoutbrain-reflector
description: Turn explicit or leased scheduled MyOutBrain learning signals into traceable, grouped unified-review proposals. Use when the creator explicitly asks to reflect now, a compatible entrance can claim scheduled reflection, pending reflection inputs need inspection, proposals need grouping, or permanently missing inputs must be explicitly abandoned.
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

## Claim scheduled reflection

Use the negotiated MCP `myoutbrain_gateway` tool when available, with the same JSON request accepted by `python -m myoutbrain gateway <request-json> --root <instance>`. Declare protocol 2.2 and only capabilities the entrance understands.

1. Call `reflection.claim` with `reflection_claim.v1`, a stable idempotency key, `expected_version: 0`, the current offset-aware time and a bounded lease of 30–3600 seconds. `claimed: false` means there is no work; do not wake or invoke a capability engine.
2. When claimed, use only `run.inputs`. They are the due-time frozen closure; do not add later queue entries or read SQLite, Vault, the object store, or invisible history.
3. Form candidates using the same rules as **Reflect now**, then call `reflection.complete` before `lease_expires_at`. Declare `reflection_complete.v1` and `review_payload.v1`, use the returned run `version` as `expected_version`, and include the lease token and exact frozen input IDs.
4. If execution cannot finish, call `reflection.return` with the same lease token and version so another compatible entrance can retry. A process crash needs no separate recovery action: the core returns an expired lease to queued on the next claim.
5. Report only the completed proposal identities and blind spots. Never approve the proposals.

The scheduler only freezes and queues inputs. It never supplies model credentials or invokes a model/network provider.
An OS scheduler can invoke `python -m myoutbrain enqueue-scheduled-reflection --root <instance> --format json`; the command generates a per-tick idempotency key and dispatches `reflection.enqueue` through the same negotiated protocol. It does not need to know the mutable schedule version.

## Abandon selected inputs

For an explicit immediate reflection, only when the creator explicitly abandons it, submit selected input IDs and a non-sensitive reason:

```text
python -m myoutbrain abandon-reflection <payload-json> --root <instance> --idempotency-key <stable-key> --format json
```

Confirm the returned IDs were cleaned. Do not use abandonment as routine expiry, scheduled processing, or a substitute for rejecting a proposal.

For a scheduled run whose frozen input is permanently unavailable, use negotiated `reflection.abandon` with `reflection_abandon.v1`, the observed run version, the affected input IDs, `confirm_permanent_missing: true`, and a short reason that contains no input body. Never abandon a scheduled run merely because a lease expired or an entrance is temporarily unavailable.
