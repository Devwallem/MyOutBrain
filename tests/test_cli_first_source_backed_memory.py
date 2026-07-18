from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.cli_support import run_cli


class FirstSourceBackedMemoryTests(unittest.TestCase):
    def test_local_source_submission_creates_only_a_pending_integration_proposal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Working Agreement.md"
            source_bytes = (
                "The team records a decision only after the owner explicitly approves it.\n"
                + ("Supporting detail stays in the local source.\n" * 300)
            ).encode("utf-8")
            source_path.write_bytes(source_bytes)
            initialized = run_cli("init", "--root", str(instance_root))

            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Decision recording rule",
                "--body",
                "Record a decision only after its owner explicitly approves it.",
                "--scope",
                "team working agreements",
                "--idempotency-key",
                "proposal-working-agreement-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated_proposal = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Decision recording rule",
                "--body",
                "Record a decision only after its owner explicitly approves it.",
                "--scope",
                "team working agreements",
                "--idempotency-key",
                "proposal-working-agreement-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            proposal = json.loads(proposed.stdout)
            self.assertEqual(repeated_proposal.returncode, 0, repeated_proposal.stderr)
            self.assertEqual(json.loads(repeated_proposal.stdout), proposal)
            self.assertEqual(proposal["status"], "pending")
            self.assertEqual(proposal["intent"], "integrate")
            self.assertEqual(proposal["formation"], "explicit")
            self.assertEqual(proposal["proposal_version"], 1)
            self.assertEqual(
                proposal["approval_effect"],
                "create_source_backed_canonical_memory",
            )
            self.assertEqual(
                proposal["proposed_memory"],
                {
                    "body": "Record a decision only after its owner explicitly approves it.",
                    "body_bytes": 62,
                    "name": "Decision recording rule",
                    "scope": "team working agreements",
                },
            )
            self.assertEqual(
                proposal["body_budget"],
                {
                    "hard_limit_bytes": 8192,
                    "target_bytes": 4096,
                    "within_target": True,
                },
            )
            self.assertRegex(proposal["proposal_id"], r"^prp_[0-9a-f]{32}$")
            self.assertRegex(proposal["planned_memory_id"], r"^mem_[0-9a-f]{32}$")
            self.assertRegex(proposal["source"]["source_id"], r"^src_[0-9a-f]{32}$")
            self.assertEqual(proposal["source"]["version"], 1)
            self.assertEqual(
                proposal["source"]["content_hash"],
                f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
            )
            self.assertEqual(proposal["source"]["locator"], str(source_path.resolve()))
            self.assertEqual(
                proposal["source"]["applicability_scope"],
                "team working agreements",
            )
            self.assertIn("+00:00", proposal["source"]["observed_at"])
            self.assertEqual(proposal["source"]["retention"], "receipt")

            before_approval = run_cli(
                "why-memory",
                proposal["planned_memory_id"],
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            legacy_approval = run_cli(
                "review-memory",
                proposal["proposal_id"],
                "I accept this proposal.",
                "--root",
                str(instance_root),
            )
            self.assertEqual(before_approval.returncode, 2)
            self.assertIn("canonical memory does not exist", before_approval.stderr)
            self.assertEqual(legacy_approval.returncode, 2)
            self.assertIn("pending integration proposal does not exist", legacy_approval.stderr)

    def test_explicit_approval_atomically_materializes_the_first_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Decision.md"
            source_path.write_text(
                "A decision enters shared practice after its owner approves it.\n",
                encoding="utf-8",
            )
            initialized = run_cli("init", "--root", str(instance_root))
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Decision recording rule",
                "--body",
                "Record a decision only after its owner explicitly approves it.",
                "--scope",
                "team working agreements",
                "--idempotency-key",
                "proposal-decision-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            proposal = json.loads(proposed.stdout)

            approved = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-decision-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            retried = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-decision-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            materialized = json.loads(approved.stdout)
            self.assertEqual(json.loads(retried.stdout), materialized)
            self.assertEqual(materialized["proposal_id"], proposal["proposal_id"])
            self.assertEqual(materialized["status"], "applied")
            self.assertEqual(materialized["decision"], "approved")
            self.assertEqual(
                materialized["memory"],
                {
                    "body": "Record a decision only after its owner explicitly approves it.",
                    "body_bytes": 62,
                    "current_version": 1,
                    "memory_id": proposal["planned_memory_id"],
                    "name": "Decision recording rule",
                    "scope": "team working agreements",
                    "state": "current",
                },
            )
            self.assertEqual(
                materialized["dictionary"],
                {
                    "canonical_name": "Decision recording rule",
                    "current_version": 1,
                    "memory_id": proposal["planned_memory_id"],
                    "primary_capsule_id": materialized["primary_capsule"]["capsule_id"],
                },
            )
            self.assertRegex(
                materialized["primary_capsule"]["capsule_id"],
                r"^cap_[0-9a-f]{32}$",
            )
            self.assertEqual(materialized["primary_capsule"]["body_bytes"], 62)
            self.assertEqual(
                materialized["primary_capsule"]["memory_record_count"], 1
            )
            self.assertEqual(materialized["source"], proposal["source"])
            self.assertEqual(materialized["audit_event"]["event_type"], "review.applied")
            self.assertEqual(materialized["audit_event"]["before_version"], None)
            self.assertEqual(materialized["audit_event"]["after_version"], 1)
            self.assertNotIn(
                materialized["memory"]["body"],
                json.dumps(materialized["audit_event"], ensure_ascii=False),
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
            explanation = json.loads(explained.stdout)
            self.assertEqual(explanation["current_version"], 1)
            self.assertEqual(explanation["current_content"], materialized["memory"]["body"])
            self.assertEqual(
                explanation["current_source_ids"],
                [proposal["source"]["source_id"]],
            )

    def test_approval_recovers_without_partial_materialization_after_interruption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Atomic.md"
            source_path.write_text("Only approved knowledge becomes canonical.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Approval boundary",
                "--body",
                "Only explicitly approved knowledge becomes canonical.",
                "--scope",
                "knowledge governance",
                "--idempotency-key",
                "proposal-atomic-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            proposal = json.loads(proposed.stdout)

            interrupted = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-atomic-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "source-memory-approval-after-database"
                },
            )
            after_interruption = run_cli(
                "why-memory",
                proposal["planned_memory_id"],
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recovered = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-atomic-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            self.assertEqual(interrupted.returncode, 86)
            self.assertEqual(after_interruption.returncode, 2)
            self.assertIn("canonical memory does not exist", after_interruption.stderr)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(recovered.stdout)["status"], "applied")

    def test_memory_body_hard_budget_and_idempotency_key_conflicts_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Budget.md"
            source_path.write_text("Evidence remains available locally.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            accepted = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Evidence budget",
                "--body",
                "Keep canonical memory compact.",
                "--scope",
                "storage",
                "--idempotency-key",
                "proposal-budget-v1",
                "--root",
                str(instance_root),
            )
            reused_key = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Evidence budget",
                "--body",
                "Different semantic content.",
                "--scope",
                "storage",
                "--idempotency-key",
                "proposal-budget-v1",
                "--root",
                str(instance_root),
            )
            oversized = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Oversized memory",
                "--body",
                "x" * 8193,
                "--scope",
                "storage",
                "--idempotency-key",
                "proposal-budget-oversized",
                "--root",
                str(instance_root),
            )

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(reused_key.returncode, 2)
            self.assertIn("different request", reused_key.stderr)
            self.assertEqual(oversized.returncode, 2)
            self.assertIn("8192-byte hard limit", oversized.stderr)


if __name__ == "__main__":
    unittest.main()
