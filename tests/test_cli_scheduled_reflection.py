from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.cli_support import run_cli


class ScheduledReflectionCliTests(unittest.TestCase):
    def test_due_empty_schedule_does_not_queue_or_wake_a_capability_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            capability_request = temporary_root / "capability-request.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            configured = _gateway(
                instance_root,
                request_path,
                operation="reflection.schedule",
                parameters={
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 168,
                },
                idempotency_key="routine-reflection:schedule:v1",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )

            due = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="routine-reflection:due:2026-07-20",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
                environment={
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(capability_request),
                },
            )

            self.assertEqual(configured.returncode, 0, configured.stdout)
            schedule = json.loads(configured.stdout)["result"]["schedule"]
            self.assertEqual(schedule["every_hours"], 168)
            self.assertEqual(schedule["version"], 1)
            self.assertEqual(due.returncode, 0, due.stdout)
            result = json.loads(due.stdout)["result"]
            self.assertEqual(
                result,
                {
                    "queued": False,
                    "reason": "empty",
                    "run": None,
                    "schedule": {
                        "enabled": True,
                        "every_hours": 168,
                        "next_due_at": "2026-07-27T03:00:00+08:00",
                        "version": 2,
                    },
                    "wake_capability_engine": False,
                },
            )
            self.assertFalse(capability_request.exists())
            later_input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="next-static-tick",
                excerpt="A later static scheduler tick can queue new work.",
            )
            next_tick = run_cli(
                "enqueue-scheduled-reflection",
                "--now",
                "2026-07-27T03:00:00+08:00",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(next_tick.returncode, 0, next_tick.stdout)
            next_result = json.loads(next_tick.stdout)
            self.assertTrue(next_result["queued"])
            self.assertEqual(next_result["run"]["input_ids"], [later_input_id])

    def test_due_schedule_freezes_inputs_in_a_queued_run_without_model_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            capability_request = temporary_root / "capability-request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="first",
                excerpt="A confirmed decision belongs in the scheduled reflection closure.",
            )
            self.assertEqual(
                _gateway(
                    instance_root,
                    request_path,
                    operation="reflection.schedule",
                    parameters={
                        "enabled": True,
                        "first_due_at": "2026-07-20T03:00:00+08:00",
                        "every_hours": 168,
                    },
                    idempotency_key="scheduled-closure:configure",
                    expected_version=0,
                    capabilities=["reflection_schedule.v1"],
                ).returncode,
                0,
            )

            enqueued = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="scheduled-closure:due",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
                environment={"MYOUTBRAIN_FAKE_REQUEST_FILE": str(capability_request)},
            )

            self.assertEqual(enqueued.returncode, 0, enqueued.stdout)
            result = json.loads(enqueued.stdout)["result"]
            self.assertTrue(result["queued"])
            self.assertEqual(result["reason"], "due")
            self.assertFalse(result["wake_capability_engine"])
            self.assertEqual(
                result["run"],
                {
                    "frozen_input_count": 1,
                    "input_ids": [input_id],
                    "run_id": result["run"]["run_id"],
                    "scheduled_for": "2026-07-20T03:00:00+08:00",
                    "status": "queued",
                    "trigger": "scheduled",
                    "version": 0,
                },
            )
            self.assertRegex(result["run"]["run_id"], r"^rfr_[0-9a-f]{64}$")
            self.assertFalse(capability_request.exists())

    def test_only_one_adapter_claims_the_frozen_run_under_an_idempotent_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first_input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="frozen-first",
                excerpt="Only the due-time closure belongs to this run.",
            )
            _gateway(
                instance_root,
                request_path,
                operation="reflection.schedule",
                parameters={
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 168,
                },
                idempotency_key="claim:configure",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            queued = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="claim:enqueue",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            run_id = json.loads(queued.stdout)["result"]["run"]["run_id"]
            second_input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="after-freeze",
                excerpt="This later signal must wait for a future run.",
            )
            explicit_inputs = run_cli(
                "reflection-inputs",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            codex_claim = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="claim:codex:first",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )
            codex_retry = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="claim:codex:first",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )
            opencode_claim = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:01+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="claim:opencode:first",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="opencode",
            )

            self.assertEqual(codex_claim.returncode, 0, codex_claim.stdout)
            first = json.loads(codex_claim.stdout)["result"]
            self.assertTrue(first["claimed"])
            self.assertEqual(first["run"]["run_id"], run_id)
            self.assertEqual(first["run"]["status"], "claimed")
            self.assertEqual(first["run"]["version"], 1)
            self.assertEqual(first["run"]["claimed_by"], "codex")
            self.assertEqual(
                first["run"]["lease_expires_at"],
                "2026-07-20T03:06:00+08:00",
            )
            self.assertEqual(
                [item["input_id"] for item in first["run"]["inputs"]],
                [first_input_id],
            )
            self.assertNotIn(second_input_id, json.dumps(first))
            self.assertEqual(
                [
                    item["input_id"]
                    for item in json.loads(explicit_inputs.stdout)["inputs"]
                ],
                [first_input_id, second_input_id],
            )
            self.assertEqual(json.loads(codex_retry.stdout)["result"], first)
            self.assertEqual(opencode_claim.returncode, 0, opencode_claim.stdout)
            self.assertEqual(
                json.loads(opencode_claim.stdout)["result"],
                {"claimed": False, "reason": "no-work", "run": None},
            )

    def test_expired_or_returned_lease_safely_requeues_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            _capture_signal(
                temporary_root,
                instance_root,
                name="lease-recovery",
                excerpt="A crashed adapter must not strand scheduled reflection.",
            )
            _gateway(
                instance_root,
                request_path,
                operation="reflection.schedule",
                parameters={
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 168,
                },
                idempotency_key="lease-recovery:configure",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            queued = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="lease-recovery:enqueue",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            run_id = json.loads(queued.stdout)["result"]["run"]["run_id"]
            first_claim = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 60,
                },
                idempotency_key="lease-recovery:codex",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )
            first_run = json.loads(first_claim.stdout)["result"]["run"]

            reclaimed = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:02:00+08:00",
                    "lease_seconds": 120,
                },
                idempotency_key="lease-recovery:opencode",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="opencode",
            )
            reclaimed_run = json.loads(reclaimed.stdout)["result"]["run"]
            expired_retry = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 60,
                },
                idempotency_key="lease-recovery:codex",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )

            returned = _gateway(
                instance_root,
                request_path,
                operation="reflection.return",
                parameters={
                    "run_id": run_id,
                    "lease_token": reclaimed_run["lease_token"],
                    "now": "2026-07-20T03:02:30+08:00",
                    "reason": "adapter-shutdown",
                },
                idempotency_key="lease-recovery:opencode:return",
                expected_version=3,
                capabilities=["reflection_claim.v1"],
                client_name="opencode",
            )
            third_claim = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:02:31+08:00",
                    "lease_seconds": 120,
                },
                idempotency_key="lease-recovery:claude",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="claude-code",
            )
            returned_claim_retry = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:02:00+08:00",
                    "lease_seconds": 120,
                },
                idempotency_key="lease-recovery:opencode",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="opencode",
            )

            self.assertEqual(first_claim.returncode, 0, first_claim.stdout)
            self.assertEqual(reclaimed.returncode, 0, reclaimed.stdout)
            self.assertEqual(reclaimed_run["run_id"], run_id)
            self.assertEqual(reclaimed_run["version"], 3)
            self.assertEqual(reclaimed_run["claimed_by"], "opencode")
            self.assertNotEqual(reclaimed_run["lease_token"], first_run["lease_token"])
            expired_result = json.loads(expired_retry.stdout)["result"]
            self.assertFalse(expired_result["claimed"])
            self.assertEqual(expired_result["reason"], "lease-expired")
            self.assertNotIn("lease_token", json.dumps(expired_result))
            self.assertNotIn("A crashed adapter", json.dumps(expired_result))
            self.assertEqual(returned.returncode, 0, returned.stdout)
            self.assertEqual(
                json.loads(returned.stdout)["result"],
                {
                    "reason": "adapter-shutdown",
                    "returned": True,
                    "run": {"run_id": run_id, "status": "queued", "version": 4},
                },
            )
            final_run = json.loads(third_claim.stdout)["result"]["run"]
            self.assertEqual(final_run["run_id"], run_id)
            self.assertEqual(final_run["version"], 5)
            self.assertEqual(final_run["claimed_by"], "claude-code")
            returned_retry_result = json.loads(returned_claim_retry.stdout)["result"]
            self.assertFalse(returned_retry_result["claimed"])
            self.assertEqual(returned_retry_result["reason"], "lease-returned")
            self.assertNotIn("lease_token", json.dumps(returned_retry_result))
            self.assertNotIn("A crashed adapter", json.dumps(returned_retry_result))

    def test_claimed_run_has_one_idempotent_completion_across_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="single-result",
                excerpt="Scheduled reflection completion submits one review proposal.",
            )
            _gateway(
                instance_root,
                request_path,
                operation="reflection.schedule",
                parameters={
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 168,
                },
                idempotency_key="single-result:configure",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            queued = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="single-result:enqueue",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            run_id = json.loads(queued.stdout)["result"]["run"]["run_id"]
            claimed = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="single-result:claim",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )
            claimed_run = json.loads(claimed.stdout)["result"]["run"]
            parameters = {
                "run_id": run_id,
                "lease_token": claimed_run["lease_token"],
                "completed_at": "2026-07-20T03:02:00+08:00",
                "reflection": {
                    "input_ids": [input_id],
                    "proposals": [
                        {
                            "candidate_id": "scheduled-decision",
                            "input_ids": [input_id],
                            "near_candidate_ids": [],
                            "conflict_candidate_ids": [],
                            "proposal": _proposal_payload(
                                "Scheduled reflection completion",
                                "Scheduled reflection submits one unified review proposal.",
                            ),
                        }
                    ],
                },
            }

            completed = _gateway(
                instance_root,
                request_path,
                operation="reflection.complete",
                parameters=parameters,
                idempotency_key="single-result:complete",
                expected_version=1,
                capabilities=["reflection_complete.v1", "review_payload.v1"],
                client_name="codex",
            )
            retried = _gateway(
                instance_root,
                request_path,
                operation="reflection.complete",
                parameters=parameters,
                idempotency_key="single-result:complete",
                expected_version=1,
                capabilities=["reflection_complete.v1", "review_payload.v1"],
                client_name="codex",
            )
            competing = _gateway(
                instance_root,
                request_path,
                operation="reflection.complete",
                parameters=parameters,
                idempotency_key="single-result:opencode-complete",
                expected_version=1,
                capabilities=["reflection_complete.v1", "review_payload.v1"],
                client_name="opencode",
            )
            queue = run_cli("review-list", "--root", str(instance_root), "--format", "json")
            pending = run_cli(
                "reflection-inputs", "--root", str(instance_root), "--format", "json"
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            result = json.loads(completed.stdout)["result"]
            self.assertEqual(result["run_id"], run_id)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["cleaned_input_ids"], [input_id])
            self.assertEqual(set(result["candidate_proposal_ids"]), {"scheduled-decision"})
            self.assertEqual(json.loads(retried.stdout)["result"], result)
            self.assertNotEqual(competing.returncode, 0)
            self.assertEqual(len(json.loads(queue.stdout)["proposals"]), 1)
            self.assertEqual(json.loads(pending.stdout)["inputs"], [])

    def test_creator_can_abandon_permanently_missing_input_without_retaining_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            excerpt = "This transient source body must not survive abandonment."
            input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="permanently-missing",
                excerpt=excerpt,
            )
            retained_input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="still-available",
                excerpt="This valid input must remain available after partial abandonment.",
            )
            _gateway(
                instance_root,
                request_path,
                operation="reflection.schedule",
                parameters={
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 168,
                },
                idempotency_key="missing:configure",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            queued = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="missing:enqueue",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            run_id = json.loads(queued.stdout)["result"]["run"]["run_id"]
            claimed = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="missing:claim",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )
            (temporary_root / "permanently-missing.md").unlink()
            parameters = {
                "run_id": run_id,
                "abandoned_at": "2026-07-20T03:02:00+08:00",
                "reason": "source-permanently-unavailable",
                "permanently_missing_input_ids": [input_id],
                "confirm_permanent_missing": True,
            }

            unsafe = _gateway(
                instance_root,
                request_path,
                operation="reflection.abandon",
                parameters={**parameters, "reason": excerpt},
                idempotency_key="missing:unsafe-abandon",
                expected_version=1,
                capabilities=["reflection_abandon.v1"],
                client_name="codex",
            )

            abandoned = _gateway(
                instance_root,
                request_path,
                operation="reflection.abandon",
                parameters=parameters,
                idempotency_key="missing:abandon",
                expected_version=1,
                capabilities=["reflection_abandon.v1"],
                client_name="codex",
            )
            retried = _gateway(
                instance_root,
                request_path,
                operation="reflection.abandon",
                parameters=parameters,
                idempotency_key="missing:abandon",
                expected_version=1,
                capabilities=["reflection_abandon.v1"],
                client_name="codex",
            )
            claim_retry = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="missing:claim",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )
            pending = run_cli(
                "reflection-inputs", "--root", str(instance_root), "--format", "json"
            )

            self.assertEqual(abandoned.returncode, 0, abandoned.stdout)
            self.assertEqual(unsafe.returncode, 2, unsafe.stdout)
            result = json.loads(abandoned.stdout)["result"]
            self.assertEqual(
                result,
                {
                    "cleaned_input_ids": [input_id],
                    "reason": "source-permanently-unavailable",
                    "run_id": run_id,
                    "status": "abandoned",
                },
            )
            self.assertNotIn(excerpt, abandoned.stdout)
            self.assertEqual(json.loads(retried.stdout)["result"], result)
            self.assertNotIn(excerpt, claim_retry.stdout)
            self.assertEqual(
                [item["input_id"] for item in json.loads(pending.stdout)["inputs"]],
                [retained_input_id],
            )

    def test_explicit_reflection_is_not_limited_by_the_configured_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            input_id = _capture_signal(
                temporary_root,
                instance_root,
                name="explicit-bypass",
                excerpt="Explicit reflection remains available while scheduling is disabled.",
            )
            configured = _gateway(
                instance_root,
                request_path,
                operation="reflection.schedule",
                parameters={
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 72,
                },
                idempotency_key="explicit-bypass:configure",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            queued = _gateway(
                instance_root,
                request_path,
                operation="reflection.enqueue",
                parameters={"now": "2026-07-20T03:00:00+08:00"},
                idempotency_key="explicit-bypass:enqueue",
                expected_version=0,
                capabilities=["reflection_schedule.v1"],
            )
            reflection_path = temporary_root / "explicit-reflection.json"
            reflection_path.write_text(
                json.dumps(
                    {
                        "input_ids": [input_id],
                        "proposals": [
                            {
                                "candidate_id": "explicit-while-disabled",
                                "input_ids": [input_id],
                                "near_candidate_ids": [],
                                "conflict_candidate_ids": [],
                                "proposal": _proposal_payload(
                                    "Explicit reflection",
                                    "Explicit reflection is independent of schedule cadence.",
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            reflected = run_cli(
                "reflect-now",
                str(reflection_path),
                "--root",
                str(instance_root),
                "--idempotency-key",
                "explicit-bypass:reflect-now",
                "--format",
                "json",
            )
            later_claim = _gateway(
                instance_root,
                request_path,
                operation="reflection.claim",
                parameters={
                    "now": "2026-07-20T03:01:00+08:00",
                    "lease_seconds": 300,
                },
                idempotency_key="explicit-bypass:claim",
                expected_version=0,
                capabilities=["reflection_claim.v1"],
                client_name="codex",
            )

            self.assertEqual(configured.returncode, 0, configured.stdout)
            self.assertEqual(
                json.loads(configured.stdout)["result"]["schedule"]["every_hours"],
                72,
            )
            self.assertEqual(reflected.returncode, 0, reflected.stderr)
            self.assertEqual(json.loads(reflected.stdout)["status"], "completed")
            self.assertTrue(json.loads(queued.stdout)["result"]["queued"])
            self.assertEqual(
                json.loads(later_claim.stdout)["result"],
                {"claimed": False, "reason": "no-work", "run": None},
            )


def _gateway(
    instance_root: Path,
    request_path: Path,
    *,
    operation: str,
    parameters: dict[str, object],
    idempotency_key: str,
    expected_version: int,
    capabilities: list[str],
    environment: dict[str, str] | None = None,
    client_name: str = "scheduler",
) -> subprocess.CompletedProcess[str]:
    request_path.write_text(
        json.dumps(
            {
                "protocol": {
                    "minimum": {"major": 2, "minor": 2},
                    "maximum": {"major": 2, "minor": 2},
                },
                "client": {
                    "name": client_name,
                    "capabilities": capabilities,
                },
                "operation": operation,
                "parameters": parameters,
                "write": {
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            }
        ),
        encoding="utf-8",
    )
    return run_cli(
        "gateway",
        str(request_path),
        "--root",
        str(instance_root),
        environment=environment,
    )


def _capture_signal(
    temporary_root: Path,
    instance_root: Path,
    *,
    name: str,
    excerpt: str,
) -> str:
    source = temporary_root / f"{name}.md"
    source.write_text(excerpt + "\n", encoding="utf-8")
    payload = temporary_root / f"{name}-signal.json"
    payload.write_text(
        json.dumps(
            {
                "signal_kind": "confirmed-decision",
                "entrance": "codex",
                "task_pointer": f"scheduled-{name}",
                "occurred_at": "2026-07-19T18:00:00+08:00",
                "excerpt": excerpt,
                "source_reference": {
                    "source_id": f"task-scheduled-{name}",
                    "version": "run:1",
                    "locator": str(source),
                },
                "source_fingerprint": hashlib.sha256(source.read_bytes()).hexdigest(),
                "applicability_scope": "scheduled reflection",
                "context_coverage": ["confirmed decision"],
                "blind_spots": ["earlier context unavailable"],
                "sensitivity": "local-only",
            }
        ),
        encoding="utf-8",
    )
    captured = run_cli(
        "submit-learning-signal",
        str(payload),
        "--root",
        str(instance_root),
        "--idempotency-key",
        f"scheduled-{name}:signal",
        "--format",
        "json",
    )
    if captured.returncode != 0:
        raise AssertionError(captured.stderr)
    return str(json.loads(captured.stdout)["input"]["input_id"])


def _proposal_payload(title: str, content: str) -> dict[str, object]:
    return {
        "title": title,
        "content": content,
        "intent": "integrate",
        "formation": "explicit",
        "priority": "routine",
        "applicability_scope": "scheduled reflection",
        "approval_effect": {
            "type": "create_canonical_memory",
            "canonical_name": title,
            "personal_cognition": False,
        },
        "target": {"memory_id": None, "expected_version": 0},
        "supporting_evidence": [{"kind": "reflection-input"}],
        "opposing_evidence": [],
        "dependencies": [],
        "context_coverage": ["frozen scheduled input"],
        "blind_spots": ["unavailable history"],
        "near_proposal_ids": [],
        "conflict_proposal_ids": [],
        "sensitivity": "local-only",
        "evidence_retention": "excerpt",
        "migration_restrictions": [],
    }


if __name__ == "__main__":
    unittest.main()
