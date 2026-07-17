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
                "--input-cost-per-million-usd",
                "1.0",
                "--output-cost-per-million-usd",
                "2.0",
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
            self.assertEqual(authorization["input_cost_per_million_usd"], 1.0)
            self.assertEqual(authorization["output_cost_per_million_usd"], 2.0)

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
                "--input-cost-per-million-usd",
                "1.0",
                "--output-cost-per-million-usd",
                "2.0",
                "--root",
                str(instance_root),
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("local-only", invalid.stderr)

    def test_scheduled_cloud_run_excludes_local_only_and_obeys_batch_and_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            configuration_path = instance_root / "myoutbrain.toml"
            configuration = configuration_path.read_text(encoding="utf-8")
            configuration = configuration.replace(
                'provider = "openai"\nmodel = "gpt-5-mini"',
                'provider = "fake"\nmodel = "analysis-v1"',
                1,
            )
            configuration_path.write_text(configuration, encoding="utf-8")
            private = remember_digest(
                temporary_root,
                instance_root,
                name="scheduled-private",
                digest="Private salary correction must remain local.",
                task="cloud-nightly",
                sensitivity="local-only",
            )
            shareable = remember_digest(
                temporary_root,
                instance_root,
                name="scheduled-shareable",
                digest="Shareable launch correction may be analyzed remotely.",
                task="cloud-nightly",
                sensitivity="cloud-allowed",
            )
            deferred = remember_digest(
                temporary_root,
                instance_root,
                name="scheduled-deferred",
                digest="Second shareable item waits for the next bounded batch.",
                task="cloud-nightly",
                sensitivity="cloud-allowed",
            )
            authorized = run_cli(
                "authorize-scheduled-consolidation",
                "--provider",
                "fake",
                "--model",
                "analysis-v1",
                "--allowed-sensitivity",
                "cloud-allowed",
                "--batch-size",
                "1",
                "--token-limit",
                "2000",
                "--cost-limit-usd",
                "0.01",
                "--input-cost-per-million-usd",
                "1.0",
                "--output-cost-per-million-usd",
                "2.0",
                "--root",
                str(instance_root),
            )
            self.assertEqual(authorized.returncode, 0, authorized.stderr)
            scheduled = run_cli(
                "schedule-consolidation",
                "cloud-nightly",
                "--task",
                "cloud-nightly",
                "--run-at",
                "2026-07-20T03:00:00+08:00",
                "--every-hours",
                "24",
                "--mode",
                "cloud",
                "--root",
                str(instance_root),
            )
            self.assertEqual(scheduled.returncode, 0, scheduled.stderr)
            request_path = temporary_root / "scheduled-request.json"
            candidate_text = (
                "Remote analysis proposes the shareable launch correction."
            )
            response = {
                "candidates": [
                    {
                        "text": candidate_text,
                        "supporting_evidence": [
                            {
                                "source_id": shareable["digest_id"],
                                "locator": "memory-buffer",
                            }
                        ],
                        "contrary_evidence": [],
                        "derivation": "Bounded comparison of the supplied digest.",
                    }
                ],
                "insufficient_evidence": False,
            }

            run_result = run_cli(
                "run-scheduled-consolidation",
                "cloud-nightly",
                "--now",
                "2026-07-20T03:00:00+08:00",
                "--conversation-state",
                "active",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_path),
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": json.dumps(response),
                },
            )

            self.assertEqual(run_result.returncode, 0, run_result.stderr)
            run = json.loads(run_result.stdout)
            self.assertEqual(run["mode"], "cloud")
            self.assertEqual(run["canonical_changes"], 0)
            self.assertFalse(run["deterministic_maintenance"]["semantic_change"])
            self.assertIn(
                run["deterministic_maintenance"]["index_status"],
                ("rebuilt", "deferred", "current-empty"),
            )
            self.assertEqual(len(run["proposals"]), 1)
            self.assertEqual(
                run["proposals"][0]["proposed_understanding"],
                candidate_text,
            )
            recorded = json.loads(request_path.read_text(encoding="utf-8"))
            serialized_request = json.dumps(recorded, ensure_ascii=False)
            self.assertIn(str(shareable["digest_id"]), serialized_request)
            self.assertNotIn(str(private["digest_id"]), serialized_request)
            self.assertNotIn(str(deferred["digest_id"]), serialized_request)
            self.assertEqual(recorded["authorization"], {"allow_cloud": True})
            self.assertEqual(recorded["purpose"], "scheduled-consolidation")
            self.assertLessEqual(recorded["max_output_tokens"], 2000)
            self.assertEqual(recorded["max_cost_usd"], 0.01)

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
            journal = (
                instance_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn("consolidation.deterministic-maintenance", journal)
            self.assertIn("consolidation.schedule-completed", journal)

    def test_scheduled_cloud_failure_is_audited_and_retryable_without_advancing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            configuration_path = instance_root / "myoutbrain.toml"
            configuration_path.write_text(
                configuration_path.read_text(encoding="utf-8").replace(
                    'provider = "openai"\nmodel = "gpt-5-mini"',
                    'provider = "fake"\nmodel = "analysis-v1"',
                    1,
                ),
                encoding="utf-8",
            )
            receipt = remember_digest(
                temporary_root,
                instance_root,
                name="retry-cloud",
                digest="Retryable cloud analysis retains this buffered evidence.",
                task="retry-cloud",
                sensitivity="cloud-allowed",
            )

            def authorize(token_limit: int, cost_limit: float) -> None:
                result = run_cli(
                    "authorize-scheduled-consolidation",
                    "--provider",
                    "fake",
                    "--model",
                    "analysis-v1",
                    "--allowed-sensitivity",
                    "cloud-allowed",
                    "--batch-size",
                    "1",
                    "--token-limit",
                    str(token_limit),
                    "--cost-limit-usd",
                    str(cost_limit),
                    "--input-cost-per-million-usd",
                    "1.0",
                    "--output-cost-per-million-usd",
                    "2.0",
                    "--root",
                    str(instance_root),
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            authorize(100, 0.01)
            self.assertEqual(
                run_cli(
                    "schedule-consolidation",
                    "retry-cloud",
                    "--task",
                    "retry-cloud",
                    "--run-at",
                    "2026-07-20T04:00:00+08:00",
                    "--every-hours",
                    "24",
                    "--mode",
                    "cloud",
                    "--root",
                    str(instance_root),
                ).returncode,
                0,
            )
            request_path = temporary_root / "retry-request.json"
            run_arguments = (
                "run-scheduled-consolidation",
                "retry-cloud",
                "--now",
                "2026-07-20T04:00:00+08:00",
                "--conversation-state",
                "active",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            over_budget = run_cli(
                *run_arguments,
                environment={"MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_path)},
            )
            self.assertEqual(over_budget.returncode, 2)
            self.assertIn("token limit", over_budget.stderr)
            self.assertFalse(request_path.exists())

            authorize(2000, 0.01)
            provider_failure = run_cli(
                *run_arguments,
                environment={
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_path),
                    "MYOUTBRAIN_FAKE_ERROR": "timeout",
                },
            )
            self.assertEqual(provider_failure.returncode, 6)
            self.assertTrue(request_path.is_file())
            request_path.unlink()

            candidate_text = "Retry succeeded without committing canonical memory."
            response = {
                "candidates": [
                    {
                        "text": candidate_text,
                        "supporting_evidence": [
                            {
                                "source_id": receipt["digest_id"],
                                "locator": "memory-buffer",
                            }
                        ],
                        "contrary_evidence": [],
                        "derivation": "Retried the same bounded evidence batch.",
                    }
                ],
                "insufficient_evidence": False,
            }
            retried = run_cli(
                *run_arguments,
                environment={
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_path),
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": json.dumps(response),
                },
            )

            self.assertEqual(retried.returncode, 0, retried.stderr)
            completed = json.loads(retried.stdout)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["attempt_count"], 3)
            self.assertEqual(completed["next_run_at"], "2026-07-21T04:00:00+08:00")
            self.assertEqual(completed["canonical_changes"], 0)
            self.assertEqual(
                completed["proposals"][0]["proposed_understanding"],
                candidate_text,
            )
            journal = (
                instance_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            self.assertGreaterEqual(
                journal.count("consolidation.schedule-retryable"),
                2,
            )

    def test_offline_completion_queues_review_and_sends_local_notification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            remember_digest(
                temporary_root,
                instance_root,
                name="offline-review",
                digest="Offline scheduled work should notify the creator locally.",
                task="offline-review",
            )
            self.assertEqual(
                run_cli(
                    "schedule-consolidation",
                    "offline-review",
                    "--task",
                    "offline-review",
                    "--run-at",
                    "2026-07-20T05:00:00+08:00",
                    "--every-hours",
                    "24",
                    "--mode",
                    "local",
                    "--root",
                    str(instance_root),
                ).returncode,
                0,
            )
            notification_path = temporary_root / "notification.json"

            completed = run_cli(
                "run-scheduled-consolidation",
                "offline-review",
                "--now",
                "2026-07-20T05:00:00+08:00",
                "--conversation-state",
                "inactive",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_NOTIFICATION_ADAPTER": "recording",
                    "MYOUTBRAIN_NOTIFICATION_FILE": str(notification_path),
                },
            )
            pending = run_cli(
                "pending-consolidation-reviews",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            run = json.loads(completed.stdout)
            self.assertEqual(run["delivery"], "pending-review-queue")
            self.assertEqual(run["notification_status"], "delivered")
            self.assertTrue(notification_path.is_file())
            notification = json.loads(
                notification_path.read_text(encoding="utf-8")
            )
            self.assertEqual(notification["title"], "Memory review is ready")
            self.assertIn(run["proposals"][0]["proposal_id"], notification["body"])
            self.assertEqual(
                notification["action"],
                f"myoutbrain://pending-review/{run['run_id']}",
            )
            self.assertEqual(pending.returncode, 0, pending.stderr)
            queue = json.loads(pending.stdout)["pending_reviews"]
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["run_id"], run["run_id"])
            self.assertEqual(queue[0]["notification_status"], "delivered")


if __name__ == "__main__":
    unittest.main()
