from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from tests.cli_support import run_cli


class LearningReflectionCliTests(unittest.TestCase):
    def test_task_without_a_learning_signal_creates_no_reflection_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "Private Companion"
            initialization = run_cli("init", "--root", str(root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            payload = Path(temporary_directory) / "no-signal.json"
            payload.write_text(
                json.dumps(
                    {
                        "signal_kind": None,
                        "entrance": "codex",
                        "task_pointer": "routine-formatting-task",
                    }
                ),
                encoding="utf-8",
            )

            submission = run_cli(
                "submit-learning-signal",
                str(payload),
                "--root",
                str(root),
                "--idempotency-key",
                "routine-formatting-task:boundary",
                "--format",
                "json",
            )
            pending = run_cli(
                "reflection-inputs",
                "--root",
                str(root),
                "--format",
                "json",
            )

            self.assertEqual(submission.returncode, 0, submission.stderr)
            self.assertEqual(
                json.loads(submission.stdout),
                {"captured": False, "input": None},
            )
            self.assertEqual(pending.returncode, 0, pending.stderr)
            self.assertEqual(json.loads(pending.stdout)["inputs"], [])

    def test_explicit_signal_captures_only_a_bounded_minimal_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "Private Companion"
            self.assertEqual(run_cli("init", "--root", str(root)).returncode, 0)
            source = temporary_root / "decision.md"
            source_body = (
                "We decided that release evidence must use stable source identities.\n"
                "Unrelated transcript detail that must not be copied into the input.\n"
            )
            source.write_text(source_body, encoding="utf-8")
            fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
            payload = temporary_root / "signal.json"
            payload.write_text(
                json.dumps(
                    {
                        "signal_kind": "confirmed-decision",
                        "entrance": "codex",
                        "task_pointer": "release-evidence",
                        "occurred_at": "2026-07-18T18:00:00+08:00",
                        "excerpt": (
                            "Release evidence must use stable source identities."
                        ),
                        "source_reference": {
                            "source_id": "task-release-evidence",
                            "version": "git:a1e6f7c",
                            "locator": str(source),
                        },
                        "source_fingerprint": fingerprint,
                        "applicability_scope": "release evidence",
                        "context_coverage": ["current release decision"],
                        "blind_spots": ["earlier alternatives were not visible"],
                        "sensitivity": "local-only",
                    }
                ),
                encoding="utf-8",
            )

            submission = run_cli(
                "submit-learning-signal",
                str(payload),
                "--root",
                str(root),
                "--idempotency-key",
                "release-evidence:decision:1",
                "--format",
                "json",
            )
            pending = run_cli(
                "reflection-inputs",
                "--root",
                str(root),
                "--format",
                "json",
            )

            self.assertEqual(submission.returncode, 0, submission.stderr)
            captured = json.loads(submission.stdout)
            self.assertTrue(captured["captured"])
            self.assertEqual(pending.returncode, 0, pending.stderr)
            inputs = json.loads(pending.stdout)["inputs"]
            self.assertEqual(inputs, [captured["input"]])
            reflection_input = inputs[0]
            self.assertEqual(reflection_input["signal_kind"], "confirmed-decision")
            self.assertEqual(
                reflection_input["source_reference"]["source_id"],
                "task-release-evidence",
            )
            self.assertEqual(reflection_input["source_fingerprint"], fingerprint)
            self.assertEqual(
                reflection_input["blind_spots"],
                ["earlier alternatives were not visible"],
            )
            self.assertNotIn("Unrelated transcript detail", json.dumps(inputs))
            self.assertLessEqual(len(json.dumps(inputs).encode("utf-8")), 8192)

    def test_explicit_reflection_deduplicates_groups_and_cleans_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "Private Companion"
            self.assertEqual(run_cli("init", "--root", str(root)).returncode, 0)
            source = temporary_root / "learning.md"
            source_body = "We decided that release evidence uses stable identities.\n"
            source.write_text(source_body, encoding="utf-8")
            captured_source_fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
            signal_path = temporary_root / "signal.json"
            signal_path.write_text(
                json.dumps(
                    {
                        "signal_kind": "confirmed-decision",
                        "entrance": "codex",
                        "task_pointer": "release-evidence",
                        "occurred_at": "2026-07-18T18:10:00+08:00",
                        "excerpt": "Release evidence uses stable source identities.",
                        "source_reference": {
                            "source_id": "task-release-evidence",
                            "version": "git:a1e6f7c",
                            "locator": str(source),
                        },
                        "source_fingerprint": captured_source_fingerprint,
                        "applicability_scope": "release evidence",
                        "context_coverage": ["confirmed decision and observed result"],
                        "blind_spots": ["earlier alternatives were unavailable"],
                        "sensitivity": "local-only",
                    }
                ),
                encoding="utf-8",
            )
            captured_process = run_cli(
                "submit-learning-signal",
                str(signal_path),
                "--root",
                str(root),
                "--idempotency-key",
                "release-evidence:signal",
                "--format",
                "json",
            )
            self.assertEqual(captured_process.returncode, 0, captured_process.stderr)
            input_id = json.loads(captured_process.stdout)["input"]["input_id"]
            second_source = temporary_root / "correction.md"
            second_body = "The creator repeated the stable identity decision.\n"
            second_source.write_text(second_body, encoding="utf-8")
            second_fingerprint = hashlib.sha256(second_source.read_bytes()).hexdigest()
            second_signal = temporary_root / "second-signal.json"
            second_signal.write_text(
                json.dumps(
                    {
                        "signal_kind": "user-correction",
                        "entrance": "codex",
                        "task_pointer": "release-evidence",
                        "occurred_at": "2026-07-18T18:11:00+08:00",
                        "excerpt": "Stable identities, not mutable paths.",
                        "source_reference": {
                            "source_id": "task-release-correction",
                            "version": "run:2",
                            "locator": str(second_source),
                        },
                        "source_fingerprint": second_fingerprint,
                        "applicability_scope": "release evidence",
                        "context_coverage": ["creator correction"],
                        "blind_spots": ["reason for repetition was unavailable"],
                        "sensitivity": "local-only",
                    }
                ),
                encoding="utf-8",
            )
            second_capture = run_cli(
                "submit-learning-signal",
                str(second_signal),
                "--root",
                str(root),
                "--idempotency-key",
                "release-evidence:correction",
                "--format",
                "json",
            )
            self.assertEqual(second_capture.returncode, 0, second_capture.stderr)
            second_input_id = json.loads(second_capture.stdout)["input"]["input_id"]
            bounded = run_cli(
                "reflection-inputs",
                "--root",
                str(root),
                "--limit",
                "1",
                "--budget-bytes",
                "8192",
                "--format",
                "json",
            )
            self.assertEqual(bounded.returncode, 0, bounded.stderr)
            bounded_result = json.loads(bounded.stdout)
            self.assertEqual(len(bounded_result["inputs"]), 1)
            self.assertTrue(bounded_result["truncated"])
            self.assertLessEqual(
                bounded_result["used_bytes"], bounded_result["budget_bytes"]
            )
            source.write_text(
                "The source changed after the signal was captured.\n",
                encoding="utf-8",
            )
            completion_path = temporary_root / "reflection.json"
            completion_path.write_text(
                json.dumps(
                    {
                        "input_ids": [input_id, second_input_id],
                        "proposals": [
                            {
                                "candidate_id": "explicit-decision",
                                "input_ids": [second_input_id],
                                "near_candidate_ids": [],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    title="Stable release evidence",
                                    content=(
                                        "Release evidence must use stable source identities."
                                    ),
                                    intent="integrate",
                                    formation="explicit",
                                    canonical_name="Stable release evidence",
                                ),
                            },
                            {
                                "candidate_id": "exact-repeat",
                                "input_ids": [input_id],
                                "near_candidate_ids": [],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    title="Same decision from another extraction",
                                    content=(
                                        "Release evidence must use stable source identities."
                                    ),
                                    intent="integrate",
                                    formation="explicit",
                                    canonical_name="Stable release evidence",
                                ),
                            },
                            {
                                "candidate_id": "derived-method",
                                "input_ids": [input_id],
                                "derivation": (
                                    "The confirmed identity rule prevents provenance "
                                    "links from depending on mutable locations."
                                ),
                                "near_candidate_ids": ["explicit-decision"],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    title="Stable identities preserve provenance",
                                    content=(
                                        "Stable source identities reduce broken provenance links."
                                    ),
                                    intent="derive",
                                    formation="derived",
                                    canonical_name="Stable provenance links",
                                ),
                            },
                            {
                                "candidate_id": "research-hypothesis",
                                "input_ids": [input_id],
                                "near_candidate_ids": ["derived-method"],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    title="Content addressed locator research",
                                    content=(
                                        "Test whether content-addressed locators improve moves."
                                    ),
                                    intent="research",
                                    formation="hypothesis",
                                    canonical_name=None,
                                    evidence_retention="receipt",
                                    sensitivity="cloud-allowed",
                                ),
                            },
                            {
                                "candidate_id": "conflicting-statement",
                                "input_ids": [input_id],
                                "near_candidate_ids": [],
                                "conflict_candidate_ids": ["explicit-decision"],
                                "proposal": _proposal_payload(
                                    title="Mutable paths are sufficient",
                                    content=(
                                        "Release evidence may rely on mutable file paths."
                                    ),
                                    intent="integrate",
                                    formation="explicit",
                                    canonical_name="Mutable release evidence",
                                ),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            reflection = run_cli(
                "reflect-now",
                str(completion_path),
                "--root",
                str(root),
                "--idempotency-key",
                "release-evidence:reflection:1",
                "--format",
                "json",
            )
            queue = run_cli(
                "review-list", "--root", str(root), "--format", "json"
            )
            pending = run_cli(
                "reflection-inputs", "--root", str(root), "--format", "json"
            )

            self.assertEqual(reflection.returncode, 0, reflection.stderr)
            result = json.loads(reflection.stdout)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["cleaned_input_ids"], [input_id, second_input_id]
            )
            self.assertEqual(result["source_status"][0]["status"], "changed")
            candidate_map = result["candidate_proposal_ids"]
            self.assertEqual(
                candidate_map["explicit-decision"], candidate_map["exact-repeat"]
            )
            self.assertEqual(queue.returncode, 0, queue.stderr)
            review = json.loads(queue.stdout)
            self.assertEqual(len(review["proposals"]), 4)
            self.assertEqual(
                {proposal["formation"] for proposal in review["proposals"]},
                {"explicit", "derived", "hypothesis"},
            )
            self.assertEqual(len(review["groups"]), 1)
            self.assertEqual(review["groups"][0]["kind"], "mixed")
            relation_types = {
                relation["type"] for relation in review["groups"][0]["relations"]
            }
            self.assertEqual(relation_types, {"near", "conflict"})
            explicit = next(
                proposal
                for proposal in review["proposals"]
                if proposal["content"]
                == "Release evidence must use stable source identities."
            )
            self.assertIn(
                "source task-release-evidence is changed since signal capture",
                explicit["blind_spots"],
            )
            self.assertTrue(
                any(
                    evidence.get("kind") == "reflection-input-receipt"
                    and evidence.get("source_fingerprint")
                    == captured_source_fingerprint
                    for evidence in explicit["supporting_evidence"]
                )
            )
            hypothesis = next(
                proposal
                for proposal in review["proposals"]
                if proposal["formation"] == "hypothesis"
            )
            hypothesis_receipt = next(
                evidence
                for evidence in hypothesis["supporting_evidence"]
                if evidence.get("kind") == "reflection-input-receipt"
            )
            self.assertEqual(hypothesis_receipt["retention"], "receipt")
            self.assertNotIn("excerpt", hypothesis_receipt)
            self.assertEqual(hypothesis["sensitivity"], "local-only")
            self.assertIn(
                "contains-local-only-reflection-input",
                hypothesis["migration_restrictions"],
            )
            receipt_source_ids = {
                evidence["source_reference"]["source_id"]
                for evidence in explicit["supporting_evidence"]
                if evidence.get("kind") == "reflection-input-receipt"
            }
            self.assertEqual(
                receipt_source_ids,
                {"task-release-evidence", "task-release-correction"},
            )
            derived = next(
                proposal
                for proposal in review["proposals"]
                if proposal["formation"] == "derived"
            )
            self.assertEqual(
                {
                    evidence["source_reference"]["source_id"]
                    for evidence in derived["supporting_evidence"]
                    if evidence.get("kind") == "reflection-input-receipt"
                },
                {"task-release-evidence"},
            )
            self.assertTrue(
                any(
                    evidence.get("kind") == "reflection-derivation"
                    for evidence in derived["supporting_evidence"]
                )
            )
            self.assertEqual(pending.returncode, 0, pending.stderr)
            self.assertEqual(json.loads(pending.stdout)["inputs"], [])

    def test_failed_reflection_keeps_input_until_explicit_abandonment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "Private Companion"
            self.assertEqual(run_cli("init", "--root", str(root)).returncode, 0)
            source = temporary_root / "failure.md"
            source_body = "A repeated failure was fixed by validating the boundary.\n"
            source.write_text(source_body, encoding="utf-8")
            source_fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
            signal_path = temporary_root / "signal.json"
            signal_path.write_text(
                json.dumps(
                    {
                        "signal_kind": "failure-and-resolution",
                        "entrance": "codex",
                        "task_pointer": "boundary-fix",
                        "occurred_at": "2026-07-18T18:20:00+08:00",
                        "excerpt": "Validate the boundary after the repeated failure.",
                        "source_reference": {
                            "source_id": "task-boundary-fix",
                            "version": "run:2",
                            "locator": str(source),
                        },
                        "source_fingerprint": source_fingerprint,
                        "applicability_scope": "boundary validation",
                        "context_coverage": ["failure and successful resolution"],
                        "blind_spots": ["the first failed run was unavailable"],
                        "sensitivity": "local-only",
                    }
                ),
                encoding="utf-8",
            )
            capture = run_cli(
                "submit-learning-signal",
                str(signal_path),
                "--root",
                str(root),
                "--idempotency-key",
                "boundary-fix:signal",
                "--format",
                "json",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            input_id = json.loads(capture.stdout)["input"]["input_id"]
            invalid = temporary_root / "invalid-reflection.json"
            invalid.write_text(
                json.dumps({"input_ids": [input_id], "proposals": []}),
                encoding="utf-8",
            )

            failed = run_cli(
                "reflect-now",
                str(invalid),
                "--root",
                str(root),
                "--idempotency-key",
                "boundary-fix:invalid",
                "--format",
                "json",
            )
            still_pending = run_cli(
                "reflection-inputs", "--root", str(root), "--format", "json"
            )
            abandonment_path = temporary_root / "abandon.json"
            abandonment_path.write_text(
                json.dumps(
                    {
                        "input_ids": [input_id],
                        "reason": "creator explicitly abandoned this reflection",
                    }
                ),
                encoding="utf-8",
            )
            abandoned = run_cli(
                "abandon-reflection",
                str(abandonment_path),
                "--root",
                str(root),
                "--idempotency-key",
                "boundary-fix:abandon",
                "--format",
                "json",
            )
            cleaned = run_cli(
                "reflection-inputs", "--root", str(root), "--format", "json"
            )

            self.assertEqual(failed.returncode, 2)
            self.assertEqual(
                [item["input_id"] for item in json.loads(still_pending.stdout)["inputs"]],
                [input_id],
            )
            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            result = json.loads(abandoned.stdout)
            self.assertEqual(result["status"], "abandoned")
            self.assertEqual(result["cleaned_input_ids"], [input_id])
            self.assertNotIn("Validate the boundary", json.dumps(result))
            self.assertEqual(json.loads(cleaned.stdout)["inputs"], [])

    def test_immediate_reflection_rejects_unbounded_and_ambiguous_requests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            oversized_path = temporary_root / "oversized.json"
            oversized_ids = [f"rfi_{index}" for index in range(21)]
            oversized_path.write_text(
                json.dumps(
                    {
                        "input_ids": oversized_ids,
                        "proposals": [
                            {
                                "candidate_id": "bounded-candidate",
                                "input_ids": oversized_ids,
                                "near_candidate_ids": [],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    title="Bounded candidate",
                                    content="Reflection runs stay bounded.",
                                    intent="integrate",
                                    formation="explicit",
                                    canonical_name="Bounded reflection",
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            ambiguous_path = temporary_root / "ambiguous.json"
            ambiguous_path.write_text(
                json.dumps(
                    {
                        "input_ids": ["rfi_one"],
                        "proposals": [
                            {
                                "candidate_id": "first",
                                "input_ids": ["rfi_one"],
                                "near_candidate_ids": ["second"],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    title="First",
                                    content="First candidate.",
                                    intent="integrate",
                                    formation="explicit",
                                    canonical_name="First candidate",
                                ),
                            },
                            {
                                "candidate_id": "second",
                                "input_ids": ["rfi_one"],
                                "near_candidate_ids": [],
                                "conflict_candidate_ids": ["first"],
                                "proposal": _proposal_payload(
                                    title="Second",
                                    content="Second candidate.",
                                    intent="integrate",
                                    formation="explicit",
                                    canonical_name="Second candidate",
                                ),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            oversized = run_cli(
                "reflect-now",
                str(oversized_path),
                "--root",
                str(temporary_root / "instance"),
                "--idempotency-key",
                "oversized",
            )
            ambiguous = run_cli(
                "reflect-now",
                str(ambiguous_path),
                "--root",
                str(temporary_root / "instance"),
                "--idempotency-key",
                "ambiguous",
            )

            self.assertEqual(oversized.returncode, 2)
            self.assertIn("exceeds 20 selected inputs", oversized.stderr)
            self.assertEqual(ambiguous.returncode, 2)
            self.assertIn("cannot be both near and conflict", ambiguous.stderr)


def _proposal_payload(
    *,
    title: str,
    content: str,
    intent: str,
    formation: str,
    canonical_name: str | None,
    evidence_retention: str = "excerpt",
    sensitivity: str = "local-only",
) -> dict[str, object]:
    effect_by_intent = {
        "integrate": "create_canonical_memory",
        "derive": "create_derived_memory",
        "research": "create_research_thread",
    }
    return {
        "title": title,
        "content": content,
        "intent": intent,
        "formation": formation,
        "priority": "routine",
        "applicability_scope": "release evidence",
        "approval_effect": {
            "type": effect_by_intent[intent],
            "canonical_name": canonical_name,
            "personal_cognition": False,
        },
        "target": {"memory_id": None, "expected_version": 0},
        "supporting_evidence": [{"kind": "reflection-input"}],
        "opposing_evidence": [],
        "dependencies": [],
        "context_coverage": ["current task"],
        "blind_spots": ["unavailable history"],
        "near_proposal_ids": [],
        "conflict_proposal_ids": [],
        "sensitivity": sensitivity,
        "evidence_retention": evidence_retention,
        "migration_restrictions": [],
    }


if __name__ == "__main__":
    unittest.main()
