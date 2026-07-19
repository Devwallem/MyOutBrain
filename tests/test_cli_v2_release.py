from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
from typing import cast
import unittest

from tests.cli_support import cli_invocation, PROJECT_ROOT, run_cli


CLIENTS = ("codex", "opencode", "claude-code")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _cli_gateway(
    instance_root: Path,
    request_path: Path,
    *,
    client: str,
    operation: str,
    parameters: dict[str, object],
    capabilities: tuple[str, ...] = (),
    write: dict[str, object] | None = None,
    maximum_minor: int = 3,
    expected_returncode: int = 0,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    request = _domain_request(
        client=client,
        operation=operation,
        parameters=parameters,
        capabilities=capabilities,
        write=write,
        maximum_minor=maximum_minor,
    )
    _write_json(request_path, request)
    result = run_cli(
        "gateway",
        str(request_path),
        "--root",
        str(instance_root),
        environment=environment,
    )
    if result.returncode != expected_returncode:
        raise AssertionError(result.stdout or result.stderr)
    response = json.loads(result.stdout)
    if not isinstance(response, dict):
        raise AssertionError(result.stdout)
    return cast(dict[str, object], response)


def _domain_request(
    *,
    client: str,
    operation: str,
    parameters: dict[str, object],
    capabilities: tuple[str, ...] = (),
    write: dict[str, object] | None = None,
    maximum_minor: int = 3,
) -> dict[str, object]:
    request: dict[str, object] = {
        "protocol": {
            "minimum": {"major": 2, "minor": 0},
            "maximum": {"major": 2, "minor": maximum_minor},
        },
        "client": {
            "name": client,
            "capabilities": list(capabilities),
        },
        "operation": operation,
        "parameters": parameters,
    }
    if write is not None:
        request["write"] = write
    return request


def _mcp_gateway(
    instance_root: Path,
    *,
    client: str,
    operation: str,
    parameters: dict[str, object],
    capabilities: tuple[str, ...] = (),
    write: dict[str, object] | None = None,
) -> dict[str, object]:
    request = _domain_request(
        client=client,
        operation=operation,
        parameters=parameters,
        capabilities=capabilities,
        write=write,
    )
    command, environment = cli_invocation(
        ("mcp", "--root", str(instance_root)),
        None,
    )
    messages = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": client, "version": "2.3"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "myoutbrain_gateway",
                "arguments": {"request": request},
            },
        },
    )
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    tool_result = cast(dict[str, object], responses[-1]["result"])
    if tool_result["isError"]:
        raise AssertionError(tool_result)
    return cast(dict[str, object], tool_result["structuredContent"])


def _approved_memory(
    instance_root: Path,
    temporary_root: Path,
    *,
    stem: str,
    name: str,
    body: str,
    scope: str,
) -> str:
    source = temporary_root / f"{stem}.md"
    source.write_text(body + "\n", encoding="utf-8")
    proposed = run_cli(
        "propose-source-memory",
        str(source),
        "--name",
        name,
        "--body",
        body,
        "--scope",
        scope,
        "--idempotency-key",
        f"{stem}-proposal-v1",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if proposed.returncode != 0:
        raise AssertionError(proposed.stderr)
    proposal = json.loads(proposed.stdout)
    approved = run_cli(
        "approve-source-memory",
        proposal["proposal_id"],
        "--expected-version",
        "0",
        "--idempotency-key",
        f"{stem}-approval-v1",
        "--entrance",
        "codex",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if approved.returncode != 0:
        raise AssertionError(approved.stderr)
    memory_id = proposal["planned_memory_id"]
    if not isinstance(memory_id, str):
        raise AssertionError(proposal)
    return memory_id


def _submit_pending_review(instance_root: Path, temporary_root: Path) -> str:
    payload_path = temporary_root / "shared-review.json"
    _write_json(
        payload_path,
        {
            "title": "Shared release review",
            "content": "Every compatible entrance sees the same review state.",
            "intent": "derive",
            "formation": "derived",
            "priority": "routine",
            "applicability_scope": "V2 release",
            "approval_effect": {
                "type": "create_derived_memory",
                "canonical_name": "Shared release review",
                "personal_cognition": False,
            },
            "target": {"memory_id": None, "expected_version": 0},
            "supporting_evidence": [
                {"kind": "task", "reference": "ticket-15:shared-review"}
            ],
            "opposing_evidence": [],
            "dependencies": [],
            "context_coverage": ["ticket 15 release gate"],
            "blind_spots": [],
            "near_proposal_ids": [],
            "conflict_proposal_ids": [],
            "sensitivity": "local-only",
            "evidence_retention": "receipt",
            "migration_restrictions": [],
        },
    )
    submitted = run_cli(
        "review-propose",
        str(payload_path),
        "--idempotency-key",
        "ticket-15-shared-review-v1",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    proposal_id = json.loads(submitted.stdout)["proposal"]["proposal_id"]
    if not isinstance(proposal_id, str):
        raise AssertionError(submitted.stdout)
    return proposal_id


class V2ReleaseBlackBoxTests(unittest.TestCase):
    def test_release_01_empty_instance_installs_three_healthy_entrances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            registry = temporary_root / "instances.json"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            client_files = {
                "codex": ("config.toml", 'model = "test"\n'),
                "opencode": ("opencode.json", '{"theme":"test"}\n'),
                "claude-code": (".mcp.json", '{"permissions":{"allow":[]}}\n'),
            }
            health: list[dict[str, object]] = []
            for client, (filename, existing) in client_files.items():
                client_root = temporary_root / client
                client_root.mkdir()
                config = client_root / filename
                config.write_text(existing, encoding="utf-8")
                arguments = (
                    client,
                    "--root",
                    str(instance_root),
                    "--config",
                    str(config),
                    "--skills-dir",
                    str(client_root / "skills"),
                    "--registry",
                    str(registry),
                )
                installed = run_cli("adapter", "install", *arguments)
                reinstalled = run_cli("adapter", "reinstall", *arguments)
                checked = run_cli("adapter", "check", *arguments)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                check = json.loads(checked.stdout)
                self.assertEqual(check["status"], "installed")
                self.assertEqual(check["instance"], str(instance_root.resolve()))
                health.append(
                    {
                        "protocol": check["protocol"],
                        "common_capabilities": check["capabilities"]["common"],
                    }
                )

            self.assertEqual(health[0], health[1])
            self.assertEqual(health[1], health[2])
            self.assertEqual(
                health[0]["protocol"],
                {
                    "client": {
                        "minimum": {"major": 2, "minor": 0},
                        "maximum": {"major": 2, "minor": 3},
                    },
                    "compatible": True,
                    "negotiated": {"major": 2, "minor": 3},
                    "server": {"major": 2, "minor": 3},
                },
            )
            common = cast(list[str], health[0]["common_capabilities"])
            self.assertIn("memory_recall.v1", common)
            self.assertIn("review_list.v1", common)
            self.assertIn("backup_restore.v1", common)

    def test_release_02_learning_signal_is_reflected_edited_and_recalled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            source = temporary_root / "learning-signal.md"
            source.write_text(
                "Release checks should exercise portable behavior.\n",
                encoding="utf-8",
            )
            signal_path = temporary_root / "signal.json"
            _write_json(
                signal_path,
                {
                    "signal_kind": "confirmed-decision",
                    "entrance": "codex",
                    "task_pointer": "ticket-15-release",
                    "occurred_at": "2026-07-19T10:00:00+08:00",
                    "excerpt": "Release checks exercise portable behavior.",
                    "source_reference": {
                        "source_id": "ticket-15-release",
                        "version": "worktree:1",
                        "locator": str(source),
                    },
                    "source_fingerprint": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "applicability_scope": "V2 release",
                    "context_coverage": ["release acceptance decision"],
                    "blind_spots": ["No earlier release candidate was in scope."],
                    "sensitivity": "local-only",
                },
            )
            captured_response = _mcp_gateway(
                instance_root,
                client="codex",
                operation="experience.submit_signal",
                capabilities=("learning_signal.v1",),
                parameters=json.loads(signal_path.read_text(encoding="utf-8")),
                write={
                    "idempotency_key": "ticket-15-signal-v1",
                    "expected_version": 0,
                },
            )
            captured = cast(dict[str, object], captured_response["result"])
            input_id = cast(dict[str, object], captured["input"])["input_id"]
            reflection_path = temporary_root / "reflection.json"
            _write_json(
                reflection_path,
                {
                    "input_ids": [input_id],
                    "proposals": [
                        {
                            "candidate_id": "portable-release-rule",
                            "input_ids": [input_id],
                            "near_candidate_ids": [],
                            "conflict_candidate_ids": [],
                            "proposal": {
                                "title": "Portable release rule",
                                "content": "Release checks exercise portable behavior.",
                                "intent": "integrate",
                                "formation": "explicit",
                                "priority": "routine",
                                "applicability_scope": "V2 release",
                                "approval_effect": {
                                    "type": "create_canonical_memory",
                                    "canonical_name": "Portable release rule",
                                    "personal_cognition": False,
                                },
                                "target": {"memory_id": None, "expected_version": 0},
                                "supporting_evidence": [
                                    {"kind": "reflection-input"}
                                ],
                                "opposing_evidence": [],
                                "dependencies": [],
                                "context_coverage": ["ticket 15 release gate"],
                                "blind_spots": [],
                                "near_proposal_ids": [],
                                "conflict_proposal_ids": [],
                                "sensitivity": "local-only",
                                "evidence_retention": "excerpt",
                                "migration_restrictions": [],
                            },
                        }
                    ],
                },
            )
            reflected = run_cli(
                "reflect-now",
                str(reflection_path),
                "--idempotency-key",
                "ticket-15-reflection-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(reflected.returncode, 0, reflected.stderr)
            reflected_data = json.loads(reflected.stdout)
            proposal_id = reflected_data["candidate_proposal_ids"][
                "portable-release-rule"
            ]
            queue = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            proposal = next(
                item for item in queue["proposals"] if item["proposal_id"] == proposal_id
            )
            batch_path = temporary_root / "batch.json"
            edited_body = "Release checks must exercise portable user behavior."
            _write_json(
                batch_path,
                {
                    "batch_id": "bat_ticket_15_release",
                    "decisions": [
                        {
                            "proposal_id": proposal_id,
                            "proposal_version": proposal["proposal_version"],
                            "decision": "approve-edited",
                            "edited_content": edited_body,
                            "reason": "Clarified during V2 release review.",
                            "defer_until": None,
                            "confirm_personal_cognition": False,
                        }
                    ],
                },
            )
            decided = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "ticket-15-review-v1",
                "--entrance",
                "opencode",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(decided.returncode, 0, decided.stderr)
            outcome = json.loads(decided.stdout)["outcomes"][0]
            memory_id = outcome["materialization"]["memory_id"]
            recalled = run_cli(
                "recall-memory",
                "Portable release rule",
                "--task",
                "ticket-15-approved-learning",
                "--entrance",
                "claude-code",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            memory = json.loads(recalled.stdout)["memories"][0]
            self.assertEqual((memory["memory_id"], memory["version"]), (memory_id, 1))
            self.assertEqual(memory["body"], edited_body)

    def test_release_03_three_entrances_share_recall_logs_and_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            memory_id = _approved_memory(
                instance_root,
                temporary_root,
                stem="portable-recall",
                name="Portable recall rule",
                body="Compatible entrances recall one canonical memory version.",
                scope="V2 release",
            )
            proposal_id = _submit_pending_review(instance_root, temporary_root)

            packages: list[dict[str, object]] = []
            queues: list[dict[str, object]] = []
            for client in CLIENTS:
                recalled = _mcp_gateway(
                    instance_root,
                    client=client,
                    operation="memory.recall",
                    capabilities=("memory_recall.v1",),
                    parameters={
                        "question": "Portable recall rule",
                        "task": "ticket-15-cross-client-recall",
                        "budget_bytes": 16 * 1024,
                        "answerability": {
                            "answerable": True,
                            "reason": "covered",
                        },
                    },
                )
                packages.append(cast(dict[str, object], recalled["result"]))
                listed = _mcp_gateway(
                    instance_root,
                    client=client,
                    operation="review.list",
                    capabilities=("review_list.v1",),
                    parameters={},
                )
                queues.append(cast(dict[str, object], listed["result"]))

            selected = [
                cast(list[dict[str, object]], package["memories"])[0]
                for package in packages
            ]
            self.assertEqual(
                [(item["memory_id"], item["version"]) for item in selected],
                [(memory_id, 1), (memory_id, 1), (memory_id, 1)],
            )
            self.assertEqual(
                [
                    cast(dict[str, object], package["source_declaration"])["kind"]
                    for package in packages
                ],
                ["myoutbrain", "myoutbrain", "myoutbrain"],
            )
            self.assertEqual(queues[0], queues[1])
            self.assertEqual(queues[1], queues[2])
            self.assertEqual(
                cast(list[dict[str, object]], queues[0]["proposals"])[0]["proposal_id"],
                proposal_id,
            )
            cli_queue = _cli_gateway(
                instance_root,
                request_path,
                client="codex",
                operation="review.list",
                capabilities=("review_list.v1",),
                parameters={},
            )
            self.assertEqual(cli_queue["result"], queues[0])

            activity_response = _mcp_gateway(
                instance_root,
                client="codex",
                operation="activity.recall_log",
                capabilities=("recall_activity.v1",),
                parameters={},
            )
            activity = cast(dict[str, object], activity_response["result"])
            events = cast(list[dict[str, object]], activity["events"])
            self.assertEqual({event["entrance"] for event in events}, set(CLIENTS))
            normalized_events = []
            for event in events:
                normalized = dict(event)
                normalized.pop("recall_id")
                normalized.pop("occurred_at")
                normalized.pop("entrance")
                normalized_events.append(normalized)
            self.assertEqual(normalized_events[0], normalized_events[1])
            self.assertEqual(normalized_events[1], normalized_events[2])
            serialized = json.dumps(activity, ensure_ascii=False)
            self.assertNotIn("Compatible entrances recall", serialized)
            self.assertNotIn("Portable recall rule", serialized)

    def test_release_04_counterevidence_blocks_answer_without_mutating_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            memory_id = _approved_memory(
                instance_root,
                temporary_root,
                stem="support-window",
                name="Support window",
                body="Nova 4 receives security fixes through 2028.",
                scope="Nova 4 support",
            )
            recall_arguments = (
                "recall-memory",
                "Support window",
                "--task",
                "ticket-15-support-review",
                "--entrance",
                "codex",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            initial_process = run_cli(*recall_arguments)
            self.assertEqual(initial_process.returncode, 0, initial_process.stderr)
            initial = json.loads(initial_process.stdout)
            counterevidence_body = "Nova 4 security support ended in 2026."
            counterevidence_path = temporary_root / "counterevidence.json"
            _write_json(
                counterevidence_path,
                {
                    "recall_id": initial["recall_id"],
                    "memory_id": memory_id,
                    "expected_version": 1,
                    "proposed_understanding": counterevidence_body,
                    "applicability_scope": "Nova 4 support",
                    "source": {
                        "kind": "public",
                        "source_id": "web_nova_4_support",
                        "source_version": 1,
                        "locator": "https://nova.example/support/4",
                        "content_hash": hashlib.sha256(
                            counterevidence_body.encode("utf-8")
                        ).hexdigest(),
                        "observed_at": "2026-07-19T02:00:00+00:00",
                        "applicability_scope": "Nova 4 support",
                    },
                },
            )
            routed = _mcp_gateway(
                instance_root,
                client="opencode",
                operation="memory.route_counterevidence",
                capabilities=("counterevidence_review.v1",),
                parameters=json.loads(counterevidence_path.read_text(encoding="utf-8")),
                write={
                    "idempotency_key": "ticket-15-counterevidence-v1",
                    "expected_version": 1,
                },
            )
            pending_process = run_cli(*recall_arguments)

            self.assertEqual(pending_process.returncode, 0, pending_process.stderr)
            routed_data = cast(dict[str, object], routed["result"])
            routed_recall = cast(dict[str, object], routed_data["recall"])
            routed_answerability = cast(
                dict[str, object], routed_recall["answerability"]
            )
            routed_proposal = cast(
                dict[str, object], routed_data["review_proposal"]
            )
            self.assertEqual(
                routed_answerability["reason"],
                "unresolved-conflict",
            )
            self.assertEqual(routed_proposal["priority"], "blocking")
            pending = json.loads(pending_process.stdout)
            self.assertEqual(
                pending["answerability"],
                {
                    "answerable": False,
                    "reason": "unresolved-conflict",
                    "overridden_by_core": True,
                },
            )
            before_memory = initial["memories"][0]
            after_memory = pending["memories"][0]
            self.assertEqual(
                (after_memory["memory_id"], after_memory["version"], after_memory["state"]),
                (memory_id, 1, "current"),
            )
            self.assertEqual(after_memory["body"], before_memory["body"])
            self.assertEqual(
                after_memory["evidence"]["source_count"],
                before_memory["evidence"]["source_count"],
            )

    def test_release_05_capsule_split_fault_recovery_and_merge_preserve_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first_id = _approved_memory(
                instance_root,
                temporary_root,
                stem="capsule-first",
                name="Snapshot recovery",
                body="Restore snapshots into a new verified directory.",
                scope="release maintenance",
            )
            second_id = _approved_memory(
                instance_root,
                temporary_root,
                stem="capsule-second",
                name="Backup verification",
                body="Verify backup hashes before switching instances.",
                scope="release maintenance",
            )

            def recall_signature(name: str) -> dict[str, object]:
                recalled = run_cli(
                    "recall-memory",
                    name,
                    "--task",
                    "ticket-15-capsule-regression",
                    "--entrance",
                    "codex",
                    "--answerable",
                    "true",
                    "--answerability-reason",
                    "covered",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                )
                self.assertEqual(recalled.returncode, 0, recalled.stderr)
                package = json.loads(recalled.stdout)
                memory = package["memories"][0]
                return {
                    "memory_id": memory["memory_id"],
                    "version": memory["version"],
                    "state": memory["state"],
                    "body": memory["body"],
                    "scope": memory["scope"],
                    "evidence": memory["evidence"],
                    "answerability": package["answerability"],
                }

            before = {
                "Snapshot recovery": recall_signature("Snapshot recovery"),
                "Backup verification": recall_signature("Backup verification"),
            }
            inspected = _cli_gateway(
                instance_root,
                request_path,
                client="codex",
                operation="maintenance.inspect",
                capabilities=("capsule_maintenance.v1",),
                parameters={},
            )
            capsules = cast(
                list[dict[str, object]],
                cast(dict[str, object], inspected["result"])["capsules"],
            )
            source_capsule_ids = [
                cast(str, capsule["capsule_id"])
                for capsule in capsules
                if set(cast(list[str], capsule["primary_memory_ids"]))
                & {first_id, second_id}
            ]
            self.assertEqual(len(source_capsule_ids), 2)
            merge_write = {
                "idempotency_key": "ticket-15-fault-recovery-v1",
                "expected_version": 1,
            }
            failed = _cli_gateway(
                instance_root,
                request_path,
                client="codex",
                operation="maintenance.reorganize",
                capabilities=("capsule_maintenance.v1",),
                parameters={
                    "action": "merge",
                    "source_capsule_ids": source_capsule_ids,
                },
                write=merge_write,
                expected_returncode=7,
                environment={"MYOUTBRAIN_CAPSULE_FAULT_STAGE": "switched"},
            )
            self.assertEqual(
                cast(dict[str, object], failed["error"])["category"],
                "integrity_failure",
            )
            recovered = _cli_gateway(
                instance_root,
                request_path,
                client="opencode",
                operation="maintenance.reorganize",
                capabilities=("capsule_maintenance.v1",),
                parameters={
                    "action": "merge",
                    "source_capsule_ids": source_capsule_ids,
                },
                write=merge_write,
            )
            recovered_result = cast(dict[str, object], recovered["result"])
            merged_capsule_id = cast(list[str], recovered_result["target_capsule_ids"])[0]
            self.assertTrue(
                cast(dict[str, object], recovered_result["recall_regression"])[
                    "equivalent"
                ]
            )

            configuration_path = instance_root / "myoutbrain.toml"
            configuration = configuration_path.read_text(encoding="utf-8")
            constrained = configuration.replace(
                "capsule_target_bytes = 65536",
                "capsule_target_bytes = 60",
            ).replace(
                "capsule_hard_limit_bytes = 131072",
                "capsule_hard_limit_bytes = 80",
            )
            configuration_path.write_text(constrained, encoding="utf-8")
            split = _cli_gateway(
                instance_root,
                request_path,
                client="claude-code",
                operation="maintenance.reorganize",
                capabilities=("capsule_maintenance.v1",),
                parameters={
                    "action": "split",
                    "source_capsule_ids": [merged_capsule_id],
                },
                write={
                    "idempotency_key": "ticket-15-split-v1",
                    "expected_version": 2,
                },
            )
            split_result = cast(dict[str, object], split["result"])
            split_capsule_ids = cast(list[str], split_result["target_capsule_ids"])
            self.assertEqual(len(split_capsule_ids), 2)
            self.assertTrue(
                cast(dict[str, object], split_result["recall_regression"])["equivalent"]
            )

            configuration_path.write_text(configuration, encoding="utf-8")
            merged_again = _cli_gateway(
                instance_root,
                request_path,
                client="codex",
                operation="maintenance.reorganize",
                capabilities=("capsule_maintenance.v1",),
                parameters={
                    "action": "merge",
                    "source_capsule_ids": split_capsule_ids,
                },
                write={
                    "idempotency_key": "ticket-15-merge-v1",
                    "expected_version": 3,
                },
            )
            self.assertTrue(
                cast(
                    dict[str, object],
                    cast(dict[str, object], merged_again["result"])[
                        "recall_regression"
                    ],
                )["equivalent"]
            )
            after = {
                "Snapshot recovery": recall_signature("Snapshot recovery"),
                "Backup verification": recall_signature("Backup verification"),
            }
            self.assertEqual(after, before)

    def test_release_06_audited_migration_is_previewed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            package_path = temporary_root / "portable.zip"
            self.assertEqual(run_cli("init", "--root", str(source_root)).returncode, 0)
            self.assertEqual(run_cli("init", "--root", str(target_root)).returncode, 0)
            memory_id = _approved_memory(
                source_root,
                temporary_root,
                stem="portable-migration",
                name="Portable migration rule",
                body="Audit the complete knowledge closure before migration.",
                scope="V2 migration",
            )
            planned = run_cli(
                "migration-plan",
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--root",
                str(source_root),
                "--format",
                "json",
            )
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "0",
                "--idempotency-key",
                "ticket-15-export-v1",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
                "--format",
                "json",
            )
            previewed = run_cli(
                "migration-import-dry-run",
                str(package_path),
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            imported = run_cli(
                "migration-import",
                str(package_path),
                "--expected-version",
                "0",
                "--idempotency-key",
                "ticket-15-import-v1",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "migration-import",
                str(package_path),
                "--expected-version",
                "0",
                "--idempotency-key",
                "ticket-15-import-v1",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "Portable migration rule",
                "--task",
                "ticket-15-import-verification",
                "--entrance",
                "claude-code",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(target_root),
                "--format",
                "json",
            )

            for process in (planned, exported, previewed, imported, repeated, recalled):
                self.assertEqual(process.returncode, 0, process.stderr)
            plan = json.loads(planned.stdout)
            self.assertTrue(plan["allowed"])
            self.assertEqual(plan["blockers"], [])
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["status"], "ready")
            self.assertEqual(
                set(preview["checks"].values()),
                {"passed"},
            )
            self.assertEqual(json.loads(imported.stdout)["disposition"], "imported")
            self.assertEqual(
                json.loads(repeated.stdout)["disposition"],
                "already-imported",
            )
            imported_memory = json.loads(recalled.stdout)["memories"][0]
            self.assertEqual(
                (imported_memory["memory_id"], imported_memory["version"]),
                (memory_id, 1),
            )

    def test_release_07_cold_snapshot_restores_to_a_doctor_verified_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            active_root = temporary_root / "active-instance"
            restored_root = temporary_root / "restored-instance"
            snapshot = temporary_root / "cold-snapshot.zip"
            self.assertEqual(run_cli("init", "--root", str(active_root)).returncode, 0)
            memory_id = _approved_memory(
                active_root,
                temporary_root,
                stem="recovery-rule",
                name="Recovery switch rule",
                body="Switch only after the restored instance passes Doctor.",
                scope="V2 recovery",
            )
            created = run_cli(
                "backup-create",
                str(snapshot),
                "--expected-version",
                "0",
                "--idempotency-key",
                "ticket-15-backup-v1",
                "--entrance",
                "codex",
                "--root",
                str(active_root),
                "--format",
                "json",
            )
            verified = run_cli("backup-verify", str(snapshot), "--format", "json")
            restored = run_cli(
                "backup-restore",
                str(snapshot),
                str(restored_root),
                "--expected-version",
                "0",
                "--idempotency-key",
                "ticket-15-restore-v1",
                "--format",
                "json",
            )
            doctor = run_cli(
                "doctor",
                "--root",
                str(restored_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "Recovery switch rule",
                "--task",
                "ticket-15-restored-recall",
                "--entrance",
                "claude-code",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(restored_root),
                "--format",
                "json",
            )

            for process in (created, verified, restored, doctor, recalled):
                self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(json.loads(verified.stdout)["valid"])
            restore_result = json.loads(restored.stdout)
            self.assertTrue(restore_result["switch_allowed"])
            self.assertEqual(restore_result["doctor_mode"], "read-only")
            self.assertEqual(json.loads(doctor.stdout)["overall"], "ok")
            restored_memory = json.loads(recalled.stdout)["memories"][0]
            self.assertEqual(
                (restored_memory["memory_id"], restored_memory["version"]),
                (memory_id, 1),
            )

    def test_release_08_old_minor_reads_but_cannot_apply_unknown_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal_id = _submit_pending_review(instance_root, temporary_root)
            listed = _cli_gateway(
                instance_root,
                request_path,
                client="legacy-adapter",
                operation="review.list",
                capabilities=("review_list.v1",),
                parameters={},
                maximum_minor=0,
            )
            proposal = next(
                item
                for item in cast(
                    list[dict[str, object]],
                    cast(dict[str, object], listed["result"])["proposals"],
                )
                if item["proposal_id"] == proposal_id
            )
            rejected = _cli_gateway(
                instance_root,
                request_path,
                client="legacy-adapter",
                operation="review.decide",
                capabilities=("review_payload.v1", "review_decision.v1"),
                parameters={
                    "proposal_id": proposal_id,
                    "decision": "approve",
                    "edited_content": None,
                    "reason": "Legacy adapter attempted approval.",
                    "defer_until": None,
                    "confirm_personal_cognition": False,
                },
                write={
                    "idempotency_key": "ticket-15-legacy-approval-v1",
                    "expected_version": proposal["proposal_version"],
                },
                maximum_minor=0,
                expected_returncode=2,
            )

            self.assertEqual(listed["protocol_version"], {"major": 2, "minor": 0})
            error = cast(dict[str, object], rejected["error"])
            self.assertEqual(error["category"], "capability_required")
            self.assertEqual(
                cast(dict[str, object], error["details"])["missing"],
                ["review_effect.create_derived_memory.v1"],
            )
            still_pending = _cli_gateway(
                instance_root,
                request_path,
                client="codex",
                operation="review.list",
                capabilities=("review_list.v1",),
                parameters={},
            )
            pending_ids = {
                item["proposal_id"]
                for item in cast(
                    list[dict[str, object]],
                    cast(dict[str, object], still_pending["result"])["proposals"],
                )
            }
            self.assertIn(proposal_id, pending_ids)

    def test_release_09_basic_loop_needs_no_embedding_network_or_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            blocked_imports = temporary_root / "blocked-imports"
            blocked_imports.mkdir()
            (blocked_imports / "sentence_transformers.py").write_text(
                "raise RuntimeError('Embedding implementation must stay unused')\n",
                encoding="utf-8",
            )
            offline_environment = {
                "PYTHONPATH": os.pathsep.join(
                    (str(blocked_imports), str(PROJECT_ROOT / "src"))
                ),
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "",
            }
            initialized = run_cli(
                "init",
                "--root",
                str(instance_root),
                environment=offline_environment,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            source = temporary_root / "offline.md"
            source.write_text("The basic memory loop remains local.\n", encoding="utf-8")
            proposed = run_cli(
                "propose-source-memory",
                str(source),
                "--name",
                "Offline memory loop",
                "--body",
                "The basic memory loop remains local.",
                "--scope",
                "V2 release",
                "--idempotency-key",
                "offline-loop-proposal-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment=offline_environment,
            )
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            proposal = json.loads(proposed.stdout)
            approved = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "offline-loop-approval-v1",
                "--entrance",
                "opencode",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment=offline_environment,
            )
            recalled = run_cli(
                "recall-memory",
                "Offline memory loop",
                "--task",
                "ticket-15-offline-loop",
                "--entrance",
                "claude-code",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment=offline_environment,
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            package = json.loads(recalled.stdout)
            self.assertEqual(package["memories"][0]["version"], 1)
            project = tomllib.loads(
                (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            self.assertNotIn("sentence-transformers>=3", project["dependencies"])
            self.assertEqual(
                project["optional-dependencies"]["embeddings"],
                ["sentence-transformers>=3"],
            )


if __name__ == "__main__":
    unittest.main()
