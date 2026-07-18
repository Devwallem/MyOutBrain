from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from typing import cast
import unittest

from myoutbrain.memory_gateway import MemoryGateway
from tests.cli_support import run_cli


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected object, got {type(value).__name__}")
    return cast(dict[str, object], value)


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AssertionError("expected an array of objects")
    return cast(list[dict[str, object]], value)


class CounterevidenceReviewTests(unittest.TestCase):
    def _approved_memory(self, root: Path, source: Path) -> dict[str, object]:
        proposed = run_cli(
            "propose-source-memory",
            str(source),
            "--name",
            "Release support window",
            "--body",
            "Nova 4 receives security fixes through 2028.",
            "--scope",
            "Nova 4 release support",
            "--idempotency-key",
            "propose-support-window-v1",
            "--root",
            str(root),
            "--format",
            "json",
        )
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        proposal = _mapping(json.loads(proposed.stdout))
        approved = run_cli(
            "approve-source-memory",
            cast(str, proposal["proposal_id"]),
            "--expected-version",
            "0",
            "--idempotency-key",
            "approve-support-window-v1",
            "--entrance",
            "codex",
            "--root",
            str(root),
            "--format",
            "json",
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        return proposal

    def _recall(self, root: Path, *, key: str = "support-review") -> dict[str, object]:
        recalled = run_cli(
            "recall-memory",
            "How long does Nova 4 receive security fixes?",
            "--task",
            key,
            "--entrance",
            "codex",
            "--answerable",
            "true",
            "--answerability-reason",
            "covered",
            "--root",
            str(root),
            "--format",
            "json",
        )
        self.assertEqual(recalled.returncode, 0, recalled.stderr)
        return _mapping(json.loads(recalled.stdout))

    def _route(
        self,
        root: Path,
        payload_path: Path,
        *,
        recall: dict[str, object],
        memory_id: str,
        source_id: str,
        claim: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        payload_path.write_text(
            json.dumps(
                {
                    "recall_id": recall["recall_id"],
                    "memory_id": memory_id,
                    "expected_version": 1,
                    "proposed_understanding": claim,
                    "applicability_scope": "Nova 4 release support",
                    "source": {
                        "kind": "public",
                        "source_id": source_id,
                        "source_version": 1,
                        "locator": f"https://nova.example/support/{source_id}",
                        "content_hash": hashlib.sha256(claim.encode("utf-8")).hexdigest(),
                        "observed_at": "2026-07-19T09:00:00+00:00",
                        "applicability_scope": "Nova 4 release support",
                    },
                }
            ),
            encoding="utf-8",
        )
        routed = run_cli(
            "route-counterevidence",
            str(payload_path),
            "--idempotency-key",
            idempotency_key,
            "--root",
            str(root),
            "--format",
            "json",
        )
        self.assertEqual(routed.returncode, 0, routed.stderr)
        return _mapping(json.loads(routed.stdout))

    def _decide(
        self,
        root: Path,
        batch_path: Path,
        proposal: dict[str, object],
        *,
        decision: str,
        key: str,
    ) -> dict[str, object]:
        batch_path.write_text(
            json.dumps(
                {
                    "batch_id": f"bat_{key}",
                    "decisions": [
                        {
                            "proposal_id": proposal["proposal_id"],
                            "proposal_version": proposal["proposal_version"],
                            "decision": decision,
                            "edited_content": None,
                            "reason": f"Creator chose to {decision} counterevidence.",
                            "defer_until": None,
                            "confirm_personal_cognition": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        decided = run_cli(
            "review-batch",
            str(batch_path),
            "--idempotency-key",
            key,
            "--entrance",
            "codex",
            "--root",
            str(root),
            "--format",
            "json",
        )
        self.assertEqual(decided.returncode, 0, decided.stderr)
        return _mapping(json.loads(decided.stdout))

    def test_counterevidence_enters_recall_and_creates_review_without_mutating_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source = temporary_root / "Support policy.md"
            payload_path = temporary_root / "counterevidence.json"
            source.write_text("Nova 4 support was originally planned through 2028.\n")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal = self._approved_memory(instance_root, source)
            initial = self._recall(instance_root)
            memory_id = cast(str, proposal["planned_memory_id"])
            public_claim = "Nova 4 security support ended in 2026."
            payload_path.write_text(
                json.dumps(
                    {
                        "recall_id": initial["recall_id"],
                        "memory_id": memory_id,
                        "expected_version": 1,
                        "canonical_name": "Release support window",
                        "proposed_understanding": public_claim,
                        "applicability_scope": "Nova 4 release support",
                        "source": {
                            "kind": "public",
                            "source_id": "web_nova_support_notice",
                            "source_version": 1,
                            "locator": "https://nova.example/support/4",
                            "content_hash": hashlib.sha256(
                                public_claim.encode("utf-8")
                            ).hexdigest(),
                            "observed_at": "2026-07-19T09:00:00+00:00",
                            "applicability_scope": "Nova 4 release support",
                        },
                    }
                ),
                encoding="utf-8",
            )

            routed = run_cli(
                "route-counterevidence",
                str(payload_path),
                "--idempotency-key",
                "route-nova-support-counterevidence",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(routed.returncode, 0, routed.stderr)
            result = _mapping(json.loads(routed.stdout))
            recall = _mapping(result["recall"])
            self.assertEqual(recall["recall_id"], initial["recall_id"])
            self.assertEqual(
                recall["answerability"],
                {
                    "answerable": False,
                    "reason": "unresolved-conflict",
                    "overridden_by_core": True,
                },
            )
            counterevidence = _objects(recall["counterevidence"])[0]
            self.assertEqual(counterevidence["source_id"], "web_nova_support_notice")
            self.assertEqual(counterevidence["relationship"], "contradicts")

            review = _mapping(result["review_proposal"])
            self.assertEqual(review["intent"], "integrate")
            self.assertEqual(review["formation"], "derived")
            self.assertEqual(review["priority"], "blocking")
            self.assertEqual(
                review["target"], {"memory_id": memory_id, "expected_version": 1}
            )
            self.assertEqual(review["applicability_scope"], "Nova 4 release support")
            self.assertEqual(
                _mapping(review["approval_effect"])["type"],
                "revise_canonical_memory",
            )
            self.assertIn("task:support-review", cast(list[object], review["context_coverage"]))
            self.assertIn(
                f"recall:{initial['recall_id']}",
                cast(list[object], review["context_coverage"]),
            )

            pending_recall = self._recall(instance_root)
            pending_memory = _objects(pending_recall["memories"])[0]
            self.assertEqual(pending_memory["version"], 1)
            self.assertEqual(pending_memory["state"], "current")
            self.assertEqual(
                pending_memory["body"],
                "Nova 4 receives security fixes through 2028.",
            )
            self.assertEqual(_mapping(pending_memory["evidence"])["source_count"], 1)
            self.assertEqual(
                pending_recall["answerability"],
                {
                    "answerable": False,
                    "reason": "unresolved-conflict",
                    "overridden_by_core": True,
                },
            )
            unrelated_task = self._recall(instance_root, key="unrelated-support-audit")
            self.assertEqual(
                unrelated_task["answerability"],
                {"answerable": True, "reason": "covered", "overridden_by_core": False},
            )

    def test_approved_counterevidence_uses_the_existing_revision_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source = temporary_root / "Support policy.md"
            payload_path = temporary_root / "counterevidence.json"
            batch_path = temporary_root / "batch.json"
            source.write_text("Original support policy.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            original = self._approved_memory(instance_root, source)
            memory_id = cast(str, original["planned_memory_id"])
            recall = self._recall(instance_root)
            claim = "Nova 4 security support ended in 2026."
            routed = self._route(
                instance_root,
                payload_path,
                recall=recall,
                memory_id=memory_id,
                source_id="web_nova_end_of_support",
                claim=claim,
                idempotency_key="route-approved-counterevidence",
            )
            review = _mapping(routed["review_proposal"])

            decided = self._decide(
                instance_root,
                batch_path,
                review,
                decision="approve",
                key="approve-counterevidence",
            )

            outcome = _objects(decided["outcomes"])[0]
            self.assertEqual(outcome["status"], "applied")
            materialization = _mapping(outcome["materialization"])
            self.assertEqual(materialization["memory_id"], memory_id)
            self.assertEqual(materialization["version"], 2)
            recalled = self._recall(instance_root, key="after-counterevidence-approval")
            memory = _objects(recalled["memories"])[0]
            self.assertEqual(memory["version"], 2)
            self.assertEqual(memory["state"], "current")
            self.assertEqual(memory["body"], claim)
            self.assertEqual(_mapping(memory["evidence"])["source_count"], 1)
            self.assertEqual(
                recalled["answerability"],
                {"answerable": True, "reason": "covered", "overridden_by_core": False},
            )
            evidence = _objects(_mapping(memory["evidence"])["references"])[0]
            expanded = run_cli(
                "expand-recall-evidence",
                cast(str, recalled["recall_id"]),
                memory_id,
                "--evidence-ref",
                cast(str, evidence["reference_id"]),
                "--budget-bytes",
                "1024",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(expanded.returncode, 0, expanded.stderr)
            receipt = _objects(_mapping(json.loads(expanded.stdout))["evidence"])[0]
            self.assertEqual(receipt["source_id"], "web_nova_end_of_support")
            self.assertEqual(receipt["status"], "receipt-only")
            self.assertEqual(
                receipt["locator"],
                "https://nova.example/support/web_nova_end_of_support",
            )

    def test_rejection_retains_decision_and_all_sources_without_majority_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source = temporary_root / "Support policy.md"
            first_payload = temporary_root / "first-counterevidence.json"
            second_payload = temporary_root / "second-counterevidence.json"
            batch_path = temporary_root / "batch.json"
            source.write_text("Original support policy.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            original = self._approved_memory(instance_root, source)
            memory_id = cast(str, original["planned_memory_id"])
            recall = self._recall(instance_root)
            claim = "Nova 4 security support ended in 2026."
            first = self._route(
                instance_root,
                first_payload,
                recall=recall,
                memory_id=memory_id,
                source_id="web_nova_notice_one",
                claim=claim,
                idempotency_key="route-counterevidence-one",
            )
            second = self._route(
                instance_root,
                second_payload,
                recall=recall,
                memory_id=memory_id,
                source_id="web_nova_notice_two",
                claim=claim,
                idempotency_key="route-counterevidence-two",
            )
            self.assertTrue(second["deduplicated"])
            first_review = _mapping(first["review_proposal"])
            second_review = _mapping(second["review_proposal"])
            self.assertEqual(second_review["proposal_id"], first_review["proposal_id"])
            self.assertEqual(len(_objects(second_review["supporting_evidence"])), 2)
            still_pending = self._recall(instance_root)
            self.assertFalse(_mapping(still_pending["answerability"])["answerable"])
            pending_memory = _objects(still_pending["memories"])[0]
            self.assertEqual(pending_memory["version"], 1)

            decided = self._decide(
                instance_root,
                batch_path,
                second_review,
                decision="reject",
                key="reject-counterevidence",
            )

            outcome = _objects(decided["outcomes"])[0]
            self.assertEqual(outcome["status"], "rejected")
            retained = MemoryGateway(instance_root).unified_review_proposal(
                cast(str, second_review["proposal_id"])
            )
            self.assertIsNotNone(retained)
            retained_review = cast(dict[str, object], retained)
            self.assertEqual(retained_review["status"], "rejected")
            retained_sources = _objects(retained_review["supporting_evidence"])
            self.assertEqual(len(retained_sources), 2)
            self.assertTrue(
                all(source["relationship"] == "contradicts" for source in retained_sources)
            )
            after_rejection = self._recall(instance_root, key="after-rejection")
            memory = _objects(after_rejection["memories"])[0]
            self.assertEqual(memory["version"], 1)
            self.assertEqual(memory["state"], "current")
            self.assertEqual(
                memory["body"], "Nova 4 receives security fixes through 2028."
            )
            self.assertTrue(_mapping(after_rejection["answerability"])["answerable"])
            repeated_recall = self._recall(instance_root)
            repeated = self._route(
                instance_root,
                first_payload,
                recall=repeated_recall,
                memory_id=memory_id,
                source_id="web_nova_notice_one",
                claim=claim,
                idempotency_key="repeat-rejected-counterevidence",
            )
            repeated_review = _mapping(repeated["review_proposal"])
            self.assertTrue(repeated["deduplicated"])
            self.assertEqual(repeated_review["status"], "rejected")
            self.assertEqual(len(_objects(repeated_review["supporting_evidence"])), 2)
            self.assertFalse(
                _mapping(_mapping(repeated["recall"])["signals"])[
                    "unresolved_conflict"
                ]
            )


if __name__ == "__main__":
    unittest.main()
