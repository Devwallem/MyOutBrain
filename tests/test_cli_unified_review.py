from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import cast
import unittest

from tests.cli_support import run_cli


def proposal_payload(
    *,
    intent: str,
    formation: str,
    priority: str,
    title: str,
    content: str,
    effect_type: str,
    personal_cognition: bool = False,
    dependencies: list[str] | None = None,
) -> dict[str, object]:
    return {
        "title": title,
        "content": content,
        "intent": intent,
        "formation": formation,
        "priority": priority,
        "applicability_scope": "unified review acceptance",
        "approval_effect": {
            "type": effect_type,
            "canonical_name": title,
            "personal_cognition": personal_cognition,
        },
        "target": {"memory_id": None, "expected_version": 0},
        "supporting_evidence": [
            {"kind": "task", "reference": f"ticket-07:{title}"}
        ],
        "opposing_evidence": [],
        "dependencies": dependencies or [],
        "context_coverage": ["ticket 07 acceptance"],
        "blind_spots": [],
        "near_proposal_ids": [],
        "conflict_proposal_ids": [],
        "sensitivity": "local-only",
        "evidence_retention": "receipt",
        "migration_restrictions": [],
    }


def submit_proposal(
    instance_root: Path,
    payload_path: Path,
    payload: dict[str, object],
    idempotency_key: str,
) -> dict[str, object]:
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_cli(
        "review-propose",
        str(payload_path),
        "--idempotency-key",
        idempotency_key,
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    submission = json.loads(result.stdout)
    proposal = submission["proposal"]
    if not isinstance(proposal, dict):
        raise AssertionError(submission)
    return proposal


class UnifiedReviewTests(unittest.TestCase):
    def test_complete_versioned_proposal_payload_is_listed_in_the_unified_queue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            payload = {
                "title": "Repeatable review lessons",
                "content": "Keep each review decision independently retryable.",
                "intent": "derive",
                "formation": "derived",
                "priority": "routine",
                "applicability_scope": "cross-project engineering work",
                "approval_effect": {
                    "type": "create_derived_memory",
                    "canonical_name": "Retryable review decisions",
                    "personal_cognition": False,
                },
                "target": {"memory_id": None, "expected_version": 0},
                "supporting_evidence": [
                    {
                        "kind": "source",
                        "source_id": "src_task_07",
                        "source_version": 1,
                        "locator": "task:07",
                    }
                ],
                "opposing_evidence": [],
                "dependencies": [],
                "context_coverage": ["ticket 07 acceptance"],
                "blind_spots": ["no cross-client validation"],
                "near_proposal_ids": [],
                "conflict_proposal_ids": [],
                "sensitivity": "local-only",
                "evidence_retention": "excerpt",
                "migration_restrictions": ["private-instance-only"],
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            initialized = run_cli("init", "--root", str(instance_root))
            proposed = run_cli(
                "review-propose",
                str(payload_path),
                "--idempotency-key",
                "derive-retryable-review-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            submission = json.loads(proposed.stdout)
            proposal = submission["proposal"]
            self.assertFalse(submission["deduplicated"])
            self.assertRegex(proposal["proposal_id"], r"^prp_[0-9a-f]{32}$")
            self.assertEqual(proposal["schema_version"], 1)
            self.assertEqual(proposal["proposal_version"], 1)
            self.assertEqual(proposal["status"], "pending")
            self.assertEqual(proposal["group_id"], None)
            self.assertEqual(proposal["available_decisions"], [
                "approve",
                "approve-edited",
                "reject",
                "defer",
            ])
            for field, value in payload.items():
                self.assertEqual(proposal[field], value)
            self.assertEqual(
                json.loads(listed.stdout),
                {"schema_version": 1, "groups": [], "proposals": [proposal]},
            )

    def test_source_backed_integration_proposal_uses_the_same_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Working Agreement.md"
            source_path.write_text(
                "Review related changes together and preserve independent failures.\n",
                encoding="utf-8",
            )
            run_cli("init", "--root", str(instance_root))

            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Review batches",
                "--body",
                "Apply dependent review decisions atomically.",
                "--scope",
                "review workflow",
                "--idempotency-key",
                "source-review-batches-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            source_proposal = json.loads(proposed.stdout)
            proposals = json.loads(listed.stdout)["proposals"]
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            self.assertEqual(proposal["proposal_id"], source_proposal["proposal_id"])
            self.assertEqual(proposal["intent"], "integrate")
            self.assertEqual(proposal["formation"], "explicit")
            self.assertEqual(proposal["priority"], "routine")
            self.assertEqual(proposal["content"], source_proposal["proposed_memory"]["body"])
            self.assertEqual(
                proposal["approval_effect"],
                {
                    "canonical_name": "Review batches",
                    "personal_cognition": False,
                    "type": "create_source_backed_canonical_memory",
                },
            )
            self.assertEqual(
                proposal["target"],
                {
                    "expected_version": 0,
                    "memory_id": source_proposal["planned_memory_id"],
                },
            )
            self.assertEqual(
                proposal["supporting_evidence"],
                [{"kind": "source", **source_proposal["source"]}],
            )

    def test_exact_duplicates_merge_evidence_while_near_and_conflicting_items_only_group(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            run_cli("init", "--root", str(instance_root))

            base_payload = {
                "title": "Batch review rule",
                "content": "Independent review items may succeed separately.",
                "intent": "derive",
                "formation": "derived",
                "priority": "routine",
                "applicability_scope": "review batches",
                "approval_effect": {
                    "type": "create_derived_memory",
                    "canonical_name": "Independent review items",
                    "personal_cognition": False,
                },
                "target": {"memory_id": None, "expected_version": 0},
                "supporting_evidence": [{"kind": "source", "source_id": "src_a"}],
                "opposing_evidence": [],
                "dependencies": [],
                "context_coverage": ["batch behavior"],
                "blind_spots": [],
                "near_proposal_ids": [],
                "conflict_proposal_ids": [],
                "sensitivity": "local-only",
                "evidence_retention": "receipt",
                "migration_restrictions": [],
            }
            payload_path.write_text(json.dumps(base_payload), encoding="utf-8")
            original = json.loads(
                run_cli(
                    "review-propose",
                    str(payload_path),
                    "--idempotency-key",
                    "batch-rule-a",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposal"]

            duplicate_payload = {
                **base_payload,
                "supporting_evidence": [{"kind": "source", "source_id": "src_b"}],
            }
            payload_path.write_text(json.dumps(duplicate_payload), encoding="utf-8")
            duplicate = json.loads(
                run_cli(
                    "review-propose",
                    str(payload_path),
                    "--idempotency-key",
                    "batch-rule-b",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )

            near_payload = {
                **base_payload,
                "content": "Independent review items can complete separately.",
                "near_proposal_ids": [original["proposal_id"]],
            }
            payload_path.write_text(json.dumps(near_payload), encoding="utf-8")
            near = json.loads(
                run_cli(
                    "review-propose",
                    str(payload_path),
                    "--idempotency-key",
                    "batch-rule-near",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposal"]

            conflict_payload = {
                **base_payload,
                "content": "Every review item must fail when any independent item fails.",
                "conflict_proposal_ids": [original["proposal_id"]],
            }
            payload_path.write_text(json.dumps(conflict_payload), encoding="utf-8")
            conflict = json.loads(
                run_cli(
                    "review-propose",
                    str(payload_path),
                    "--idempotency-key",
                    "batch-rule-conflict",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposal"]
            queue = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )

            self.assertTrue(duplicate["deduplicated"])
            self.assertEqual(
                duplicate["proposal"]["proposal_id"], original["proposal_id"]
            )
            self.assertEqual(
                duplicate["proposal"]["supporting_evidence"],
                [
                    {"kind": "source", "source_id": "src_a"},
                    {"kind": "source", "source_id": "src_b"},
                ],
            )
            self.assertEqual(len(queue["proposals"]), 3)
            self.assertNotEqual(near["proposal_id"], original["proposal_id"])
            self.assertNotEqual(conflict["proposal_id"], original["proposal_id"])
            self.assertEqual(len(queue["groups"]), 1)
            group = queue["groups"][0]
            self.assertEqual(group["kind"], "mixed")
            self.assertEqual(
                set(group["proposal_ids"]),
                {original["proposal_id"], near["proposal_id"], conflict["proposal_id"]},
            )
            self.assertEqual(
                {
                    (relation["type"], frozenset(relation["proposal_ids"]))
                    for relation in group["relations"]
                },
                {
                    ("near", frozenset({original["proposal_id"], near["proposal_id"]})),
                    (
                        "conflict",
                        frozenset({original["proposal_id"], conflict["proposal_id"]}),
                    ),
                },
            )

    def test_modified_batch_approval_materializes_all_four_intents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            proposals = [
                submit_proposal(
                    instance_root,
                    payload_path,
                    proposal_payload(
                        intent="derive",
                        formation="derived",
                        priority="routine",
                        title="Derived review lesson",
                        content="Keep review failures retryable.",
                        effect_type="create_derived_memory",
                    ),
                    "four-intents-derive",
                ),
                submit_proposal(
                    instance_root,
                    payload_path,
                    proposal_payload(
                        intent="integrate",
                        formation="explicit",
                        priority="priority",
                        title="Integrated review rule",
                        content="Apply dependent decisions atomically.",
                        effect_type="create_canonical_memory",
                    ),
                    "four-intents-integrate",
                ),
                submit_proposal(
                    instance_root,
                    payload_path,
                    proposal_payload(
                        intent="archive",
                        formation="explicit",
                        priority="routine",
                        title="Review session archive",
                        content="Ticket 07 decisions and their outcomes.",
                        effect_type="create_human_archive",
                    ),
                    "four-intents-archive",
                ),
                submit_proposal(
                    instance_root,
                    payload_path,
                    proposal_payload(
                        intent="research",
                        formation="hypothesis",
                        priority="blocking",
                        title="Review grouping research",
                        content="Can deterministic grouping remain compact at scale?",
                        effect_type="create_research_thread",
                    ),
                    "four-intents-research",
                ),
            ]
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_four_intents",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": (
                                    "Keep every review failure safely retryable."
                                    if proposal["intent"] == "derive"
                                    else None
                                ),
                                "reason": "Accepted in the ticket 07 tracer bullet.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                            for proposal in proposals
                        ],
                    }
                ),
                encoding="utf-8",
            )

            decided = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "four-intents-batch-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "four-intents-batch-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(decided.returncode, 0, decided.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            result = json.loads(decided.stdout)
            self.assertEqual(json.loads(repeated.stdout), result)
            self.assertEqual(result["batch_id"], "bat_four_intents")
            self.assertEqual(result["status"], "complete")
            self.assertFalse(result["partial_success"])
            self.assertEqual(
                [outcome["status"] for outcome in result["outcomes"]],
                ["applied", "applied", "applied", "applied"],
            )
            self.assertEqual(result["outcomes"][0]["decision"], "edited-approved")
            self.assertEqual(
                result["outcomes"][0]["final_content"],
                "Keep every review failure safely retryable.",
            )
            self.assertEqual(
                [outcome["materialization"]["kind"] for outcome in result["outcomes"]],
                ["canonical-memory", "canonical-memory", "human-archive", "research-thread"],
            )
            self.assertEqual(
                result["outcomes"][0]["materialization"]["authorship"],
                "system-derived",
            )
            listed = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            self.assertEqual(listed["proposals"], [])

    def test_personal_cognition_requires_item_confirmation_and_failed_item_can_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            personal = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="integrate",
                    formation="explicit",
                    priority="priority",
                    title="My review preference",
                    content="I prefer reviewing dependent changes together.",
                    effect_type="create_canonical_memory",
                    personal_cognition=True,
                ),
                "personal-review-preference",
            )
            rejected = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="archive",
                    formation="derived",
                    priority="routine",
                    title="Discarded archive",
                    content="This archive should not be retained.",
                    effect_type="create_human_archive",
                ),
                "reject-archive",
            )
            deferred = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="research",
                    formation="hypothesis",
                    priority="routine",
                    title="Deferred research",
                    content="Investigate this after the current milestone.",
                    effect_type="create_research_thread",
                ),
                "defer-research",
            )
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_personal_bulk_attempt",
                        "confirm_personal_cognition": True,
                        "decisions": [
                            {
                                "proposal_id": personal["proposal_id"],
                                "proposal_version": personal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Bulk approval must not establish identity.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            },
                            {
                                "proposal_id": rejected["proposal_id"],
                                "proposal_version": rejected["proposal_version"],
                                "decision": "reject",
                                "edited_content": None,
                                "reason": "Not useful.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            },
                            {
                                "proposal_id": deferred["proposal_id"],
                                "proposal_version": deferred["proposal_version"],
                                "decision": "defer",
                                "edited_content": None,
                                "reason": "Review after the milestone.",
                                "defer_until": "2026-08-01T00:00:00+00:00",
                                "confirm_personal_cognition": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            first = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "personal-bulk-attempt",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            first_result = json.loads(first.stdout)
            self.assertTrue(first_result["partial_success"])
            self.assertEqual(
                [outcome["status"] for outcome in first_result["outcomes"]],
                ["failed", "rejected", "deferred"],
            )
            self.assertEqual(
                first_result["outcomes"][0]["error"],
                "personal_cognition_requires_item_confirmation",
            )
            queued = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"]
            pending_personal = next(
                item for item in queued if item["proposal_id"] == personal["proposal_id"]
            )
            self.assertEqual(pending_personal["retry_count"], 1)
            self.assertEqual(
                pending_personal["last_error"],
                "personal_cognition_requires_item_confirmation",
            )
            self.assertEqual(
                next(
                    item for item in queued if item["proposal_id"] == deferred["proposal_id"]
                )["status"],
                "deferred",
            )
            self.assertNotIn(rejected["proposal_id"], {item["proposal_id"] for item in queued})

            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_personal_item_confirmation",
                        "decisions": [
                            {
                                "proposal_id": personal["proposal_id"],
                                "proposal_version": personal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "I explicitly confirm this represents me.",
                                "defer_until": None,
                                "confirm_personal_cognition": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            retried = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "personal-item-confirmation",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(retried.returncode, 0, retried.stderr)
            outcome = json.loads(retried.stdout)["outcomes"][0]
            self.assertEqual(outcome["status"], "applied")
            self.assertTrue(outcome["materialization"]["personal_cognition"])
            self.assertEqual(
                outcome["materialization"]["authorship"],
                "creator-personal-cognition",
            )

    def test_independent_item_succeeds_while_dependency_group_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            independent = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="archive",
                    formation="explicit",
                    priority="routine",
                    title="Independent archive",
                    content="This decision does not depend on the others.",
                    effect_type="create_human_archive",
                ),
                "independent-archive",
            )
            prerequisite = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="derive",
                    formation="derived",
                    priority="priority",
                    title="Atomic prerequisite",
                    content="Establish the prerequisite understanding.",
                    effect_type="create_derived_memory",
                ),
                "atomic-prerequisite",
            )
            dependent = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="research",
                    formation="hypothesis",
                    priority="priority",
                    title="Atomic dependent",
                    content="Research the consequence of the prerequisite.",
                    effect_type="create_research_thread",
                    dependencies=[str(prerequisite["proposal_id"])],
                ),
                "atomic-dependent",
            )
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_partial_atomic_failure",
                        "decisions": [
                            {
                                "proposal_id": independent["proposal_id"],
                                "proposal_version": independent["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": None,
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            },
                            {
                                "proposal_id": prerequisite["proposal_id"],
                                "proposal_version": prerequisite["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": None,
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            },
                            {
                                "proposal_id": dependent["proposal_id"],
                                "proposal_version": 999,
                                "decision": "approve",
                                "edited_content": None,
                                "reason": None,
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            decided = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "partial-atomic-failure",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(decided.returncode, 0, decided.stderr)
            result = json.loads(decided.stdout)
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["partial_success"])
            self.assertEqual(
                [outcome["status"] for outcome in result["outcomes"]],
                ["applied", "failed", "failed"],
            )
            self.assertEqual(
                result["outcomes"][1]["error"],
                "dependency_group_failed:proposal_version_conflict",
            )
            self.assertEqual(
                result["outcomes"][2]["error"],
                "dependency_group_failed:proposal_version_conflict",
            )
            queued = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"]
            self.assertEqual(
                {item["proposal_id"] for item in queued},
                {prerequisite["proposal_id"], dependent["proposal_id"]},
            )
            self.assertEqual({item["retry_count"] for item in queued}, {1})

    def test_materialization_failure_rolls_back_and_a_new_batch_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            proposal = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="archive",
                    formation="explicit",
                    priority="routine",
                    title="Retryable archive",
                    content="Materialize this exactly once after a retry.",
                    effect_type="create_human_archive",
                ),
                "retryable-archive",
            )

            def write_batch(batch_id: str) -> None:
                batch_path.write_text(
                    json.dumps(
                        {
                            "batch_id": batch_id,
                            "decisions": [
                                {
                                    "proposal_id": proposal["proposal_id"],
                                    "proposal_version": proposal["proposal_version"],
                                    "decision": "approve",
                                    "edited_content": None,
                                    "reason": None,
                                    "defer_until": None,
                                    "confirm_personal_cognition": False,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            write_batch("bat_injected_failure")
            failed = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "injected-failure",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAIL_REVIEW_PROPOSAL": str(proposal["proposal_id"])
                },
            )

            self.assertEqual(failed.returncode, 0, failed.stderr)
            failed_outcome = json.loads(failed.stdout)["outcomes"][0]
            self.assertEqual(failed_outcome["status"], "failed")
            self.assertEqual(
                failed_outcome["error"],
                "application_failed:injected_review_failure",
            )
            queued = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"]
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["status"], "pending")
            self.assertEqual(queued[0]["retry_count"], 1)

            write_batch("bat_retry_after_failure")
            retried = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "retry-after-failure",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(retried.returncode, 0, retried.stderr)
            outcome = json.loads(retried.stdout)["outcomes"][0]
            self.assertEqual(outcome["status"], "applied")
            self.assertEqual(outcome["materialization"]["kind"], "human-archive")

    def test_only_undeferred_routine_proposals_expire_to_compact_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            proposals: dict[str, dict[str, object]] = {}
            for label, priority in (
                ("routine", "routine"),
                ("priority", "priority"),
                ("blocking", "blocking"),
                ("deferred", "routine"),
            ):
                proposals[label] = submit_proposal(
                    instance_root,
                    payload_path,
                    proposal_payload(
                        intent="research",
                        formation="hypothesis",
                        priority=priority,
                        title=f"{label.title()} proposal",
                        content=f"Generated body for the {label} proposal.",
                        effect_type="create_research_thread",
                    ),
                    f"expiry-{label}",
                )
            deferred = proposals["deferred"]
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_defer_expiry_candidate",
                        "decisions": [
                            {
                                "proposal_id": deferred["proposal_id"],
                                "proposal_version": deferred["proposal_version"],
                                "decision": "defer",
                                "edited_content": None,
                                "reason": "Review next year.",
                                "defer_until": "2027-01-01T00:00:00+00:00",
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            deferred_result = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "defer-expiry-candidate",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(deferred_result.returncode, 0, deferred_result.stderr)

            expired = run_cli(
                "review-expire",
                "--as-of",
                "2026-10-18T00:00:00+00:00",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(expired.returncode, 0, expired.stderr)
            result = json.loads(expired.stdout)
            self.assertEqual(result["retention_days"], 90)
            self.assertEqual(len(result["expired"]), 1)
            compact = result["expired"][0]
            self.assertEqual(compact["proposal_id"], proposals["routine"]["proposal_id"])
            self.assertEqual(compact["status"], "expired")
            self.assertEqual(compact["formation"], "hypothesis")
            self.assertEqual(compact["title"], "Routine proposal")
            self.assertFalse(compact["content_retained"])
            self.assertRegex(compact["exact_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                compact["supporting_evidence"],
                proposals["routine"]["supporting_evidence"],
            )
            queued_ids = {
                proposal["proposal_id"]
                for proposal in json.loads(
                    run_cli(
                        "review-list",
                        "--root",
                        str(instance_root),
                        "--format",
                        "json",
                    ).stdout
                )["proposals"]
            }
            self.assertEqual(
                queued_ids,
                {
                    proposals["priority"]["proposal_id"],
                    proposals["blocking"]["proposal_id"],
                    proposals["deferred"]["proposal_id"],
                },
            )

    def test_source_memory_can_be_approved_through_the_unified_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Batch Source.md"
            batch_path = temporary_root / "batch.json"
            source_path.write_text(
                "Dependent review changes form one atomic group.\n",
                encoding="utf-8",
            )
            run_cli("init", "--root", str(instance_root))
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Atomic review group",
                "--body",
                "Apply dependent review changes atomically.",
                "--scope",
                "unified review",
                "--idempotency-key",
                "unified-source-proposal",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            proposal = json.loads(proposed.stdout)
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_source_memory",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Approve through the unified queue.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            approved = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "unified-source-batch",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            outcome = json.loads(approved.stdout)["outcomes"][0]
            self.assertEqual(outcome["status"], "applied")
            self.assertEqual(
                outcome["materialization"]["memory_id"],
                proposal["planned_memory_id"],
            )
            explained = run_cli(
                "why-memory",
                proposal["planned_memory_id"],
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertEqual(
                json.loads(explained.stdout)["current_content"],
                "Apply dependent review changes atomically.",
            )
            self.assertEqual(
                json.loads(
                    run_cli(
                        "review-list",
                        "--root",
                        str(instance_root),
                        "--format",
                        "json",
                    ).stdout
                )["proposals"],
                [],
            )

    def test_legacy_source_approval_updates_the_unified_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Source.md"
            source_path.write_text("One source-backed rule.\n", encoding="utf-8")
            run_cli("init", "--root", str(instance_root))
            proposal = json.loads(
                run_cli(
                    "propose-source-memory",
                    str(source_path),
                    "--name",
                    "Source-backed rule",
                    "--body",
                    "Approve this source-backed rule explicitly.",
                    "--scope",
                    "review compatibility",
                    "--idempotency-key",
                    "legacy-source-proposal",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )

            approved = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "legacy-source-approval",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)["proposals"], [])

    def test_payload_cannot_grant_an_intent_effect_or_identity_it_does_not_have(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            payload_path = temporary_root / "proposal.json"
            run_cli("init", "--root", str(instance_root))
            invalid = proposal_payload(
                intent="integrate",
                formation="hypothesis",
                priority="routine",
                title="Unverified identity claim",
                content="This unverified claim represents the creator.",
                effect_type="create_derived_memory",
                personal_cognition=True,
            )
            payload_path.write_text(json.dumps(invalid), encoding="utf-8")

            proposed = run_cli(
                "review-propose",
                str(payload_path),
                "--idempotency-key",
                "invalid-authority",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(proposed.returncode, 2)
            self.assertIn("approval effect", proposed.stderr)
            self.assertEqual(
                json.loads(
                    run_cli(
                        "review-list",
                        "--root",
                        str(instance_root),
                        "--format",
                        "json",
                    ).stdout
                )["proposals"],
                [],
            )

    def test_integration_checks_target_version_before_preserving_a_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Revision Source.md"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            source_path.write_text("The first review rule.\n", encoding="utf-8")
            run_cli("init", "--root", str(instance_root))
            source_proposal = json.loads(
                run_cli(
                    "propose-source-memory",
                    str(source_path),
                    "--name",
                    "Review revision rule",
                    "--body",
                    "Review the first version.",
                    "--scope",
                    "review revisions",
                    "--idempotency-key",
                    "revision-source",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            approved = run_cli(
                "approve-source-memory",
                source_proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "revision-source-approval",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            memory_id = source_proposal["planned_memory_id"]

            revision_payload = proposal_payload(
                intent="integrate",
                formation="explicit",
                priority="priority",
                title="Review revision rule",
                content="Review the current version and preserve the prior version.",
                effect_type="revise_canonical_memory",
            )
            revision_payload["target"] = {
                "memory_id": memory_id,
                "expected_version": 99,
            }
            wrong_version = submit_proposal(
                instance_root,
                payload_path,
                revision_payload,
                "revision-wrong-target",
            )

            def decide(proposal: dict[str, object], batch_id: str, key: str) -> dict[str, object]:
                batch_path.write_text(
                    json.dumps(
                        {
                            "batch_id": batch_id,
                            "decisions": [
                                {
                                    "proposal_id": proposal["proposal_id"],
                                    "proposal_version": proposal["proposal_version"],
                                    "decision": "approve",
                                    "edited_content": None,
                                    "reason": "Approve the reviewed revision.",
                                    "defer_until": None,
                                    "confirm_personal_cognition": False,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                result = run_cli(
                    "review-batch",
                    str(batch_path),
                    "--idempotency-key",
                    key,
                    "--entrance",
                    "codex",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                )
                if result.returncode != 0:
                    raise AssertionError(result.stderr)
                decoded = cast(dict[str, object], json.loads(result.stdout))
                outcomes = cast(list[dict[str, object]], decoded["outcomes"])
                return outcomes[0]

            conflict = decide(
                wrong_version,
                "bat_wrong_target_version",
                "wrong-target-version",
            )
            self.assertEqual(conflict["status"], "failed")
            self.assertEqual(conflict["error"], "target_version_conflict")

            revision_payload["target"] = {
                "memory_id": memory_id,
                "expected_version": 1,
            }
            correct_version = submit_proposal(
                instance_root,
                payload_path,
                revision_payload,
                "revision-correct-target",
            )
            revised = decide(
                correct_version,
                "bat_correct_target_version",
                "correct-target-version",
            )
            self.assertEqual(revised["status"], "applied")
            materialization = cast(dict[str, object], revised["materialization"])
            self.assertEqual(materialization["memory_id"], memory_id)
            self.assertEqual(materialization["version"], 2)
            explanation = json.loads(
                run_cli(
                    "why-memory",
                    memory_id,
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            self.assertEqual(explanation["current_version"], 2)
            self.assertEqual(
                [version["content"] for version in explanation["versions"]],
                [
                    "Review the first version.",
                    "Review the current version and preserve the prior version.",
                ],
            )


if __name__ == "__main__":
    unittest.main()
