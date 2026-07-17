from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myoutbrain.consolidation import ConsolidationScheduler
from myoutbrain.core_types import WriterLocked
from myoutbrain.generation import (
    Citation,
    GeneratedCandidate,
    GeneratedReflection,
    GenerationRequest,
    ProviderUsage,
)
from myoutbrain.local_core import LocalMemoryCore
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
                "usage": {"input_tokens": 120, "output_tokens": 40},
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
            state = json.loads(
                (instance_root / "store" / "scheduled-consolidation.json").read_text(
                    encoding="utf-8"
                )
            )
            external_call = next(iter(state["external_calls"].values()))
            self.assertEqual(external_call["purpose"], "scheduled-consolidation")
            self.assertEqual(
                external_call["evidence_memory_ids"], [shareable["digest_id"]]
            )
            self.assertEqual(external_call["input_tokens"], 120)
            self.assertEqual(external_call["output_tokens"], 40)
            self.assertEqual(external_call["actual_cost_usd"], 0.0002)
            self.assertEqual(external_call["result"], "proposed")

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

            rejected_response = {
                "usage": {"input_tokens": 110, "output_tokens": 35},
                "candidates": [
                    {
                        "text": "This response cites outside the authorized batch.",
                        "supporting_evidence": [
                            {
                                "source_id": "mem_outside_batch",
                                "locator": "memory-buffer",
                            }
                        ],
                        "contrary_evidence": [],
                        "derivation": "Invalid evidence provenance.",
                    }
                ],
                "insufficient_evidence": False,
            }
            rejected = run_cli(
                *run_arguments,
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": json.dumps(
                        rejected_response
                    )
                },
            )
            self.assertEqual(rejected.returncode, 6)
            self.assertIn("outside its batch", rejected.stderr)

            candidate_text = "Retry succeeded without committing canonical memory."
            response = {
                "usage": {"input_tokens": 100, "output_tokens": 30},
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
            self.assertEqual(completed["attempt_count"], 4)
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
            state = json.loads(
                (instance_root / "store" / "scheduled-consolidation.json").read_text(
                    encoding="utf-8"
                )
            )
            failed_calls = [
                call
                for call in state["external_calls"].values()
                if call["result"] == "failed"
            ]
            self.assertEqual(len(failed_calls), 2)
            timeout_call = next(
                call for call in failed_calls if "timeout" in call["error"]
            )
            self.assertIsNone(timeout_call["actual_cost_usd"])
            rejected_call = next(
                call for call in failed_calls if call["input_tokens"] == 110
            )
            self.assertEqual(rejected_call["output_tokens"], 35)
            self.assertEqual(rejected_call["actual_cost_usd"], 0.00018)

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
            self.assertEqual(
                queue[0]["notification_id"], notification["notification_id"]
            )

    def test_forced_offline_review_uses_durable_retryable_notification_outbox(
        self,
    ) -> None:
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
                name="forced-offline",
                digest="An important offline answer needs this latest correction.",
                task="important-answer",
            )
            forced = run_cli(
                "consolidate",
                "--force",
                "--task",
                "important-answer",
                "--conversation-state",
                "inactive",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={"MYOUTBRAIN_NOTIFICATION_ADAPTER": "unavailable"},
            )
            self.assertEqual(forced.returncode, 0, forced.stderr)
            forced_result = json.loads(forced.stdout)
            self.assertEqual(forced_result["notification_status"], "failed")
            self.assertRegex(forced_result["run_id"], r"^forced_[0-9a-f]{64}$")

            notification_path = temporary_root / "retried-notification.json"
            retried = run_cli(
                "retry-consolidation-notifications",
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

            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(
                json.loads(retried.stdout)["notifications"][0][
                    "notification_status"
                ],
                "delivered",
            )
            queue = json.loads(pending.stdout)["pending_reviews"]
            self.assertEqual(queue[0]["trigger"], "forced")
            self.assertEqual(queue[0]["notification_status"], "delivered")
            notification = json.loads(notification_path.read_text(encoding="utf-8"))
            self.assertEqual(
                notification["notification_id"], queue[0]["notification_id"]
            )

    def test_crashed_running_attempt_is_resumed_under_the_process_lock(self) -> None:
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
                name="crash-recovery",
                digest="A crashed scheduled attempt must remain safely retryable.",
                task="crash-recovery",
            )
            due_at = "2026-07-20T06:00:00+08:00"
            self.assertEqual(
                run_cli(
                    "schedule-consolidation",
                    "crash-recovery",
                    "--task",
                    "crash-recovery",
                    "--run-at",
                    due_at,
                    "--every-hours",
                    "24",
                    "--mode",
                    "local",
                    "--root",
                    str(instance_root),
                ).returncode,
                0,
            )
            state_path = instance_root / "store" / "scheduled-consolidation.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = "run_" + hashlib.sha256(
                f"crash-recovery:1:{due_at}".encode("utf-8")
            ).hexdigest()
            state["runs"][run_id] = {
                "run_id": run_id,
                "schedule_id": "crash-recovery",
                "due_at": due_at,
                "mode": "local",
                "status": "running",
                "attempt_count": 1,
                "started_at": "2026-07-20T05:59:00+08:00",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")

            recovered = run_cli(
                "run-scheduled-consolidation",
                "crash-recovery",
                "--now",
                due_at,
                "--conversation-state",
                "active",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(recovered.stdout)["attempt_count"], 2)

    def test_crash_after_cloud_proposal_persistence_recovers_delivery(self) -> None:
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
                name="cloud-checkpoint",
                digest="A persisted proposal must survive scheduler process loss.",
                task="cloud-checkpoint",
                sensitivity="cloud-allowed",
            )
            scheduler = ConsolidationScheduler(instance_root)
            scheduler.authorize_cloud(
                provider="openai",
                model="gpt-5-mini",
                allowed_sensitivity="cloud-allowed",
                batch_size=1,
                token_limit=3000,
                cost_limit_usd=0.01,
                input_cost_per_million_usd=1.0,
                output_cost_per_million_usd=2.0,
            )
            due_at = "2026-07-20T06:30:00+08:00"
            scheduler.schedule(
                "cloud-checkpoint",
                task="cloud-checkpoint",
                run_at=due_at,
                every_hours=24,
                mode="cloud",
            )
            persisted = LocalMemoryCore(instance_root).propose_manual_consolidation(
                "cloud-checkpoint",
                digest_ids=(str(receipt["digest_id"]),),
                proposed_understanding="The cloud response had already become a proposal.",
            )
            run_id = "run_" + hashlib.sha256(
                f"cloud-checkpoint:1:{due_at}".encode("utf-8")
            ).hexdigest()
            state_path = instance_root / "store" / "scheduled-consolidation.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["runs"][run_id] = {
                "run_id": run_id,
                "schedule_id": "cloud-checkpoint",
                "due_at": due_at,
                "mode": "cloud",
                "status": "running",
                "attempt_count": 1,
                "started_at": "2026-07-20T06:29:00+08:00",
            }
            call_id = f"{run_id}:attempt-1"
            state["external_calls"][call_id] = {
                "call_id": call_id,
                "status": "dispatched",
                "purpose": "scheduled-consolidation",
                "provider": "openai",
                "model": "gpt-5-mini",
                "authorization_generation": 1,
                "evidence_memory_ids": [receipt["digest_id"]],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            scheduler.revoke_cloud()

            recovered = scheduler.run_due(
                "cloud-checkpoint",
                now=due_at,
                conversation_state="active",
            )

            self.assertEqual(recovered.attempt_count, 2)
            self.assertEqual(
                [proposal.proposal_id for proposal in recovered.proposals],
                [persisted[0].proposal_id],
            )
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                final_state["external_calls"][call_id]["result"],
                "interrupted-unknown",
            )

    def test_inflight_schedule_edit_and_revocation_do_not_get_overwritten(self) -> None:
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
                name="race-safe",
                digest="An in-flight cloud run must preserve newer user configuration.",
                task="race-safe",
                sensitivity="cloud-allowed",
            )
            scheduler = ConsolidationScheduler(instance_root)
            scheduler.authorize_cloud(
                provider="race-provider",
                model="race-model",
                allowed_sensitivity="cloud-allowed",
                batch_size=1,
                token_limit=3000,
                cost_limit_usd=0.01,
                input_cost_per_million_usd=1.0,
                output_cost_per_million_usd=2.0,
            )
            scheduler.schedule(
                "race-safe",
                task="race-safe",
                run_at="2026-07-20T07:00:00+08:00",
                every_hours=24,
                mode="cloud",
            )
            digest_id = str(receipt["digest_id"])

            class ReconfiguringProvider:
                name = "race-provider"
                model = "race-model"

                def generate(self, request: GenerationRequest) -> object:
                    raise AssertionError("not used")

                def reflection_input_token_upper_bound(
                    self, request: GenerationRequest
                ) -> int:
                    return len(json.dumps(request.to_data()).encode("utf-8"))

                def reflect(self, request: GenerationRequest) -> GeneratedReflection:
                    scheduler.schedule(
                        "race-safe",
                        task="newer-task",
                        run_at="2026-07-20T07:00:00+08:00",
                        every_hours=12,
                        mode="cloud",
                    )
                    scheduler.revoke_cloud()
                    LocalMemoryCore(instance_root).propose_manual_consolidation(
                        "race-safe"
                    )
                    return GeneratedReflection(
                        candidates=(
                            GeneratedCandidate(
                                text="A bounded proposal from the dispatched batch.",
                                supporting_evidence=(
                                    Citation(
                                        source_id=digest_id,
                                        locator="memory-buffer",
                                    ),
                                ),
                                contrary_evidence=(),
                                derivation="The supplied digest supports the proposal.",
                            ),
                        ),
                        insufficient_evidence=False,
                        usage=ProviderUsage(input_tokens=120, output_tokens=40),
                    )

            with mock.patch(
                "myoutbrain.library.configured_generation_provider",
                return_value=ReconfiguringProvider(),
            ):
                completed = scheduler.run_due(
                    "race-safe",
                    now="2026-07-20T07:00:00+08:00",
                    conversation_state="active",
                )

            state = json.loads(
                (instance_root / "store" / "scheduled-consolidation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(completed.next_run_at, "2026-07-20T07:00:00+08:00")
            self.assertEqual(state["schedules"]["race-safe"]["task"], "newer-task")
            self.assertEqual(state["schedules"]["race-safe"]["every_hours"], 12)
            self.assertEqual(state["authorization"]["status"], "revoked")
            replacement_run_id = "run_" + hashlib.sha256(
                "race-safe:2:2026-07-20T07:00:00+08:00".encode("utf-8")
            ).hexdigest()
            self.assertNotEqual(completed.run_id, replacement_run_id)
            call = next(iter(state["external_calls"].values()))
            self.assertLess(
                call["authorization_generation"],
                state["authorization"]["generation"],
            )
            self.assertEqual(call["result"], "proposed")

    def test_post_response_writer_contention_keeps_usage_and_is_retryable(self) -> None:
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
                name="post-response-lock",
                digest="Usage survives local contention after a paid response.",
                task="post-response-lock",
                sensitivity="cloud-allowed",
            )
            digest_id = str(receipt["digest_id"])
            scheduler = ConsolidationScheduler(instance_root)
            scheduler.authorize_cloud(
                provider="usage-provider",
                model="usage-model",
                allowed_sensitivity="cloud-allowed",
                batch_size=1,
                token_limit=3000,
                cost_limit_usd=0.01,
                input_cost_per_million_usd=1.0,
                output_cost_per_million_usd=2.0,
            )
            scheduler.schedule(
                "post-response-lock",
                task="post-response-lock",
                run_at="2026-07-20T08:00:00+08:00",
                every_hours=24,
                mode="cloud",
            )

            class UsageProvider:
                name = "usage-provider"
                model = "usage-model"

                def generate(self, request: GenerationRequest) -> object:
                    raise AssertionError("not used")

                def reflection_input_token_upper_bound(
                    self, request: GenerationRequest
                ) -> int:
                    return len(json.dumps(request.to_data()).encode("utf-8"))

                def reflect(self, request: GenerationRequest) -> GeneratedReflection:
                    return GeneratedReflection(
                        candidates=(
                            GeneratedCandidate(
                                text="A paid response awaiting local proposal persistence.",
                                supporting_evidence=(
                                    Citation(digest_id, "memory-buffer"),
                                ),
                                contrary_evidence=(),
                                derivation="The bounded evidence supports this proposal.",
                            ),
                        ),
                        insufficient_evidence=False,
                        usage=ProviderUsage(input_tokens=110, output_tokens=35),
                    )

            with (
                mock.patch(
                    "myoutbrain.library.configured_generation_provider",
                    return_value=UsageProvider(),
                ),
                mock.patch.object(
                    LocalMemoryCore,
                    "propose_manual_consolidation",
                    side_effect=WriterLocked,
                ),
            ):
                with self.assertRaises(WriterLocked):
                    scheduler.run_due(
                        "post-response-lock",
                        now="2026-07-20T08:00:00+08:00",
                        conversation_state="active",
                    )

            state = json.loads(
                (instance_root / "store" / "scheduled-consolidation.json").read_text(
                    encoding="utf-8"
                )
            )
            call = next(iter(state["external_calls"].values()))
            self.assertEqual(call["result"], "failed")
            self.assertEqual(call["input_tokens"], 110)
            self.assertEqual(call["output_tokens"], 35)
            self.assertEqual(call["actual_cost_usd"], 0.00018)
            run = next(iter(state["runs"].values()))
            self.assertEqual(run["status"], "retryable")


if __name__ == "__main__":
    unittest.main()
