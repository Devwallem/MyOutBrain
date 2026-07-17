from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_consolidate import remember_digest


class ScheduledConsolidationTests(unittest.TestCase):
    def test_forced_consolidation_is_task_scoped_and_proposal_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            urgent = remember_digest(
                temporary_root,
                instance_root,
                name="urgent-buffer",
                digest="Urgent launch review requires the latest correction.",
                task="urgent-answer",
            )
            unrelated = remember_digest(
                temporary_root,
                instance_root,
                name="unrelated-buffer",
                digest="Garden planning remains unrelated to the launch.",
                task="garden-plan",
            )

            forced = run_cli(
                "consolidate",
                "--force",
                "--task",
                "urgent-answer",
                "--conversation-state",
                "active",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            reviews = run_cli(
                "review-memory",
                "--history",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(forced.returncode, 0, forced.stderr)
            result = json.loads(forced.stdout)
            self.assertEqual(result["trigger"], "forced")
            self.assertEqual(result["scope"], "task-related")
            self.assertEqual(result["delivery"], "active-conversation")
            self.assertEqual(result["canonical_changes"], 0)
            self.assertEqual(len(result["proposals"]), 1)
            self.assertEqual(
                result["proposals"][0]["evidence_memory_ids"],
                [urgent["digest_id"]],
            )
            self.assertNotIn(
                unrelated["digest_id"],
                json.dumps(result, ensure_ascii=False),
            )
            self.assertEqual(reviews.returncode, 0, reviews.stderr)
            self.assertEqual(json.loads(reviews.stdout)["reviews"], [])

            ambiguous = run_cli(
                "consolidate",
                "--force",
                "--task",
                "garden-plan",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(ambiguous.returncode, 2)
            self.assertIn("conversation-state", ambiguous.stderr)

    def test_scheduled_cloud_authorization_is_bounded_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )

            authorized = run_cli(
                "authorize-scheduled-consolidation",
                "--provider",
                "fake-cloud",
                "--model",
                "analysis-v1",
                "--allowed-sensitivity",
                "cloud-allowed",
                "--batch-size",
                "3",
                "--token-limit",
                "900",
                "--cost-limit-usd",
                "0.25",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(authorized.returncode, 0, authorized.stderr)
            authorization = json.loads(authorized.stdout)
            self.assertEqual(authorization["status"], "active")
            self.assertEqual(authorization["provider"], "fake-cloud")
            self.assertEqual(authorization["model"], "analysis-v1")
            self.assertEqual(
                authorization["allowed_sensitivity"], "cloud-allowed"
            )
            self.assertEqual(authorization["batch_size"], 3)
            self.assertEqual(authorization["token_limit"], 900)
            self.assertEqual(authorization["cost_limit_usd"], 0.25)

            revoked = run_cli(
                "revoke-scheduled-consolidation",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            status = run_cli(
                "scheduled-consolidation-authorization",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertEqual(json.loads(revoked.stdout)["status"], "revoked")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["status"], "revoked")

            invalid = run_cli(
                "authorize-scheduled-consolidation",
                "--provider",
                "fake-cloud",
                "--model",
                "analysis-v1",
                "--allowed-sensitivity",
                "local-only",
                "--batch-size",
                "3",
                "--token-limit",
                "900",
                "--cost-limit-usd",
                "0.25",
                "--root",
                str(instance_root),
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("local-only", invalid.stderr)

    def test_explicit_local_schedule_runs_when_due_and_only_creates_proposals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            receipt = remember_digest(
                temporary_root,
                instance_root,
                name="scheduled-local",
                digest="Scheduled local review prepares a bounded proposal.",
                task="nightly-review",
            )
            configured = run_cli(
                "schedule-consolidation",
                "nightly",
                "--task",
                "nightly-review",
                "--run-at",
                "2026-07-20T02:00:00+08:00",
                "--every-hours",
                "24",
                "--mode",
                "local",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            early = run_cli(
                "run-scheduled-consolidation",
                "nightly",
                "--now",
                "2026-07-20T01:59:59+08:00",
                "--conversation-state",
                "active",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            due = run_cli(
                "run-scheduled-consolidation",
                "nightly",
                "--now",
                "2026-07-20T02:00:00+08:00",
                "--conversation-state",
                "active",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            reviews = run_cli(
                "review-memory",
                "--history",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(configured.returncode, 0, configured.stderr)
            self.assertEqual(json.loads(configured.stdout)["schedule_id"], "nightly")
            self.assertEqual(early.returncode, 2)
            self.assertIn("not due", early.stderr)
            self.assertEqual(due.returncode, 0, due.stderr)
            run = json.loads(due.stdout)
            self.assertEqual(run["trigger"], "scheduled")
            self.assertEqual(run["mode"], "local")
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["delivery"], "active-conversation")
            self.assertEqual(run["canonical_changes"], 0)
            self.assertEqual(run["next_run_at"], "2026-07-21T02:00:00+08:00")
            self.assertEqual(len(run["proposals"]), 1)
            self.assertEqual(
                run["proposals"][0]["evidence_memory_ids"],
                [receipt["digest_id"]],
            )
            self.assertEqual(reviews.returncode, 0, reviews.stderr)
            self.assertEqual(json.loads(reviews.stdout)["reviews"], [])


if __name__ == "__main__":
    unittest.main()
