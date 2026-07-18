from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from typing import cast
import unittest

from tests.cli_support import (
    cli_invocation,
    PROJECT_ROOT,
    run_cli,
    start_cli,
    wait_until_lock_is_held,
)


def write_request(path: Path, request: dict[str, object]) -> None:
    path.write_text(json.dumps(request), encoding="utf-8")


def submit_derived_proposal(instance_root: Path, path: Path) -> dict[str, object]:
    path.write_text(
        json.dumps(
            {
                "title": "Portable approval semantics",
                "content": "An adapter approves only effects it understands.",
                "intent": "derive",
                "formation": "derived",
                "priority": "routine",
                "applicability_scope": "agent adapter protocol",
                "approval_effect": {
                    "type": "create_derived_memory",
                    "canonical_name": "Portable approval semantics",
                    "personal_cognition": False,
                },
                "target": {"memory_id": None, "expected_version": 0},
                "supporting_evidence": [
                    {"kind": "task", "reference": "ticket-11"}
                ],
                "opposing_evidence": [],
                "dependencies": [],
                "context_coverage": ["ticket 11 acceptance"],
                "blind_spots": [],
                "near_proposal_ids": [],
                "conflict_proposal_ids": [],
                "sensitivity": "local-only",
                "evidence_retention": "receipt",
                "migration_restrictions": [],
            }
        ),
        encoding="utf-8",
    )
    submitted = run_cli(
        "review-propose",
        str(path),
        "--idempotency-key",
        "portable-approval-proposal-v1",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    proposal = json.loads(submitted.stdout)["proposal"]
    if not isinstance(proposal, dict):
        raise AssertionError(submitted.stdout)
    return proposal


def run_mcp(
    instance_root: Path,
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    command, environment = cli_invocation(
        ("mcp", "--root", str(instance_root)),
        None,
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
    responses: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        response = json.loads(line)
        if not isinstance(response, dict):
            raise AssertionError(line)
        responses.append(cast(dict[str, object], response))
    return responses


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def canonical_snapshot(root: Path) -> dict[str, bytes]:
    return {
        relative_path: content
        for relative_path, content in tree_snapshot(root).items()
        if relative_path == "myoutbrain.toml"
        or relative_path.startswith(("store/", "vault/"))
    }


class AgentAdapterProtocolTests(unittest.TestCase):
    def test_protocol_contract_is_discoverable_and_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "protocol.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            write_request(
                request_path,
                {
                    "protocol": {
                        "minimum": {"major": 2, "minor": 0},
                        "maximum": {"major": 2, "minor": 1},
                    },
                    "client": {"name": "diagnostic", "capabilities": []},
                    "operation": "protocol.describe",
                    "parameters": {},
                },
            )

            described = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )

            self.assertEqual(described.returncode, 0, described.stderr)
            response = json.loads(described.stdout)
            self.assertEqual(response["result"]["current"], {"major": 2, "minor": 2})
            self.assertEqual(
                response["result"]["operations"],
                {
                    "reads": [
                        "instance.status",
                        "maintenance.inspect",
                        "maintenance.plan",
                        "migration.import_dry_run",
                        "migration.plan",
                        "protocol.describe",
                        "review.list",
                    ],
                    "writes": [
                        "maintenance.configure_partition",
                        "maintenance.reorganize",
                        "migration.export",
                        "migration.import",
                        "reflection.abandon",
                        "reflection.claim",
                        "reflection.complete",
                        "reflection.enqueue",
                        "reflection.return",
                        "reflection.schedule",
                        "review.decide",
                    ],
                },
            )
            self.assertIn(
                "review_effect.create_derived_memory.v1",
                response["server_capabilities"],
            )
            schema_dir = PROJECT_ROOT / "src" / "myoutbrain" / "schemas"
            request_schema = json.loads(
                (schema_dir / "domain-request-v2.json").read_text(encoding="utf-8")
            )
            response_schema = json.loads(
                (schema_dir / "domain-response-v2.json").read_text(encoding="utf-8")
            )
            compatibility = json.loads(
                (schema_dir / "compatibility-v2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request_schema["$id"], "myoutbrain://schema/domain-request-v2")
            self.assertEqual(response_schema["$id"], "myoutbrain://schema/domain-response-v2")
            self.assertEqual(compatibility["current"], {"major": 2, "minor": 2})

    def test_old_minor_adapter_can_read_current_instance_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            write_request(
                request_path,
                {
                    "protocol": {
                        "minimum": {"major": 2, "minor": 0},
                        "maximum": {"major": 2, "minor": 0},
                    },
                    "client": {"name": "codex", "capabilities": []},
                    "operation": "instance.status",
                    "parameters": {},
                },
            )

            result = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertTrue(response["ok"])
            self.assertEqual(response["operation"], "instance.status")
            self.assertEqual(response["protocol_version"], {"major": 2, "minor": 0})
            self.assertEqual(
                response["server_protocol_version"],
                {"major": 2, "minor": 2},
            )
            self.assertEqual(response["result"]["canonical_schema_version"], 10)
            self.assertEqual(response["result"]["integrity"]["overall"], "ok")

    def test_protocol_range_with_a_supported_intersection_is_negotiated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            for minimum, maximum, expected in (
                ((1, 0), (2, 1), (2, 1)),
                ((2, 0), (3, 0), (2, 2)),
            ):
                with self.subTest(minimum=minimum, maximum=maximum):
                    write_request(
                        request_path,
                        {
                            "protocol": {
                                "minimum": {"major": minimum[0], "minor": minimum[1]},
                                "maximum": {"major": maximum[0], "minor": maximum[1]},
                            },
                            "client": {"name": "range-client", "capabilities": []},
                            "operation": "instance.status",
                            "parameters": {},
                        },
                    )

                    result = run_cli(
                        "gateway",
                        str(request_path),
                        "--root",
                        str(instance_root),
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        json.loads(result.stdout)["protocol_version"],
                        {"major": expected[0], "minor": expected[1]},
                    )

    def test_request_parser_enforces_the_published_schema_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "request.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            request: dict[str, object] = {
                "protocol": {
                    "minimum": {"major": 2, "minor": 0},
                    "maximum": {"major": 2, "minor": 1},
                },
                "client": {
                    "name": "schema-client",
                    "capabilities": ["instance_status.v1", "instance_status.v1"],
                },
                "operation": "instance.status",
                "parameters": {},
                "unexpected": True,
            }
            write_request(request_path, request)

            unknown = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )
            self.assertEqual(unknown.returncode, 2, unknown.stderr)
            self.assertEqual(
                json.loads(unknown.stdout)["error"],
                {
                    "category": "invalid_request",
                    "details": {},
                    "message": "gateway request contains unknown fields: unexpected",
                },
            )

            request.pop("unexpected")
            write_request(request_path, request)
            duplicate = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )
            self.assertEqual(duplicate.returncode, 2, duplicate.stderr)
            self.assertEqual(
                json.loads(duplicate.stdout)["error"]["message"],
                "client.capabilities must not contain duplicates",
            )

    def test_incompatible_major_rejects_semantic_write_with_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "write.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            write_request(
                request_path,
                {
                    "protocol": {
                        "minimum": {"major": 1, "minor": 0},
                        "maximum": {"major": 1, "minor": 9},
                    },
                    "client": {
                        "name": "old-client",
                        "capabilities": [
                            "review_payload.v1",
                            "review_decision.v1",
                        ],
                    },
                    "operation": "review.decide",
                    "parameters": {
                        "proposal_id": "prp_does_not_matter",
                        "decision": "approve",
                    },
                    "write": {
                        "idempotency_key": "old-client-write-v1",
                        "expected_version": 1,
                    },
                },
            )

            result = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            response = json.loads(result.stdout)
            self.assertFalse(response["ok"])
            self.assertEqual(response["operation"], "review.decide")
            self.assertEqual(
                response["error"],
                {
                    "category": "protocol_incompatible",
                    "message": "client protocol range is incompatible",
                    "details": {
                        "client_maximum": {"major": 1, "minor": 9},
                        "client_minimum": {"major": 1, "minor": 0},
                        "server": {"major": 2, "minor": 2},
                    },
                },
            )
            queue = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(json.loads(queue.stdout)["proposals"], [])

    def test_adapter_cannot_approve_an_effect_it_does_not_understand(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "write.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            proposal = submit_derived_proposal(
                instance_root,
                temporary_root / "proposal.json",
            )
            write_request(
                request_path,
                {
                    "protocol": {
                        "minimum": {"major": 2, "minor": 0},
                        "maximum": {"major": 2, "minor": 1},
                    },
                    "client": {
                        "name": "opencode",
                        "capabilities": [
                            "review_payload.v1",
                            "review_decision.v1",
                        ],
                    },
                    "operation": "review.decide",
                    "parameters": {
                        "proposal_id": proposal["proposal_id"],
                        "decision": "approve",
                    },
                    "write": {
                        "idempotency_key": "unknown-effect-write-v1",
                        "expected_version": proposal["proposal_version"],
                    },
                },
            )

            result = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["error"]["category"], "capability_required")
            self.assertEqual(
                response["error"]["details"],
                {"missing": ["review_effect.create_derived_memory.v1"]},
            )
            queue = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            self.assertEqual(queue["proposals"][0]["status"], "pending")

    def test_understood_write_is_idempotent_and_version_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "write.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            proposal = submit_derived_proposal(
                instance_root,
                temporary_root / "proposal.json",
            )
            write_request(
                request_path,
                {
                    "protocol": {
                        "minimum": {"major": 2, "minor": 0},
                        "maximum": {"major": 2, "minor": 1},
                    },
                    "client": {
                        "name": "claude-code",
                        "capabilities": [
                            "review_payload.v1",
                            "review_decision.v1",
                            "review_effect.create_derived_memory.v1",
                        ],
                    },
                    "operation": "review.decide",
                    "parameters": {
                        "proposal_id": proposal["proposal_id"],
                        "decision": "approve",
                    },
                    "write": {
                        "idempotency_key": "portable-approved-write-v1",
                        "expected_version": proposal["proposal_version"],
                    },
                },
            )

            stale_request = json.loads(request_path.read_text(encoding="utf-8"))
            stale_request["write"]["expected_version"] = 0
            write_request(request_path, stale_request)
            stale = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )
            self.assertEqual(stale.returncode, 2, stale.stderr)
            self.assertEqual(
                json.loads(stale.stdout)["error"]["category"],
                "version_conflict",
            )
            stale_request["write"]["expected_version"] = proposal["proposal_version"]
            write_request(request_path, stale_request)

            first = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )
            replay = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual(json.loads(first.stdout), json.loads(replay.stdout))
            response = json.loads(first.stdout)
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["status"], "complete")
            self.assertEqual(response["result"]["outcomes"][0]["status"], "applied")
            conflicting_request = json.loads(
                request_path.read_text(encoding="utf-8")
            )
            conflicting_request["parameters"]["reason"] = "changed retry"
            write_request(request_path, conflicting_request)
            conflicting = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(instance_root),
            )
            self.assertEqual(conflicting.returncode, 2, conflicting.stderr)
            self.assertEqual(
                json.loads(conflicting.stdout)["error"]["category"],
                "idempotency_conflict",
            )
            queue = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            self.assertEqual(queue["proposals"], [])

    def test_mcp_and_cli_return_the_same_domain_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "status.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            request = {
                "protocol": {
                    "minimum": {"major": 2, "minor": 0},
                    "maximum": {"major": 2, "minor": 1},
                },
                "client": {"name": "codex", "capabilities": []},
                "operation": "instance.status",
                "parameters": {},
            }
            write_request(request_path, request)
            cli_response = json.loads(
                run_cli(
                    "gateway",
                    str(request_path),
                    "--root",
                    str(instance_root),
                ).stdout
            )

            responses = run_mcp(
                instance_root,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "acceptance", "version": "1"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "myoutbrain_gateway",
                            "arguments": {"request": request},
                        },
                    },
                ],
            )

            initialize_result = cast(dict[str, object], responses[0]["result"])
            self.assertEqual(initialize_result["protocolVersion"], "2025-11-25")
            listed = cast(dict[str, object], responses[1]["result"])
            tools = cast(list[dict[str, object]], listed["tools"])
            input_schema = cast(dict[str, object], tools[0]["inputSchema"])
            properties = cast(dict[str, object], input_schema["properties"])
            request_schema = cast(dict[str, object], properties["request"])
            self.assertEqual(
                request_schema["$id"],
                "myoutbrain://schema/domain-request-v2",
            )
            tool_result = cast(dict[str, object], responses[2]["result"])
            self.assertFalse(tool_result["isError"])
            self.assertEqual(tool_result["structuredContent"], cli_response)
            content = cast(list[dict[str, object]], tool_result["content"])
            self.assertEqual(
                json.loads(cast(str, content[0]["text"])),
                cli_response,
            )

    def test_mcp_and_cli_return_the_same_scheduled_reflection_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            request_path = temporary_root / "reflection-schedule.json"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            request = {
                "protocol": {
                    "minimum": {"major": 2, "minor": 2},
                    "maximum": {"major": 2, "minor": 2},
                },
                "client": {
                    "name": "codex",
                    "capabilities": ["reflection_schedule.v1"],
                },
                "operation": "reflection.schedule",
                "parameters": {
                    "enabled": True,
                    "first_due_at": "2026-07-20T03:00:00+08:00",
                    "every_hours": 168,
                },
                "write": {
                    "idempotency_key": "transport-neutral-reflection-schedule",
                    "expected_version": 0,
                },
            }
            write_request(request_path, request)
            cli_response = json.loads(
                run_cli(
                    "gateway",
                    str(request_path),
                    "--root",
                    str(instance_root),
                ).stdout
            )

            responses = run_mcp(
                instance_root,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "acceptance", "version": "1"},
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
                ],
            )

            tool_result = cast(dict[str, object], responses[1]["result"])
            self.assertFalse(tool_result["isError"])
            self.assertEqual(tool_result["structuredContent"], cli_response)

    def test_mcp_and_cli_return_the_same_stable_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            missing_instance = temporary_root / "missing"
            request_path = temporary_root / "status.json"
            request = {
                "protocol": {
                    "minimum": {"major": 2, "minor": 0},
                    "maximum": {"major": 2, "minor": 1},
                },
                "client": {"name": "codex", "capabilities": []},
                "operation": "instance.status",
                "parameters": {},
            }
            write_request(request_path, request)

            cli_result = run_cli(
                "gateway",
                str(request_path),
                "--root",
                str(missing_instance),
            )
            responses = run_mcp(
                missing_instance,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "acceptance", "version": "1"},
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
                ],
            )

            self.assertEqual(cli_result.returncode, 3, cli_result.stderr)
            cli_response = json.loads(cli_result.stdout)
            self.assertEqual(
                cli_response["error"]["category"],
                "configuration_conflict",
            )
            tool_result = cast(dict[str, object], responses[1]["result"])
            self.assertTrue(tool_result["isError"])
            self.assertEqual(tool_result["structuredContent"], cli_response)


class AgentAdapterInstallationTests(unittest.TestCase):
    def test_concurrent_first_installs_cannot_claim_different_primary_instances(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            first_root = temporary_root / "First"
            second_root = temporary_root / "Second"
            registry_path = temporary_root / "instances.json"
            ready_path = temporary_root / "registry-lock-ready"
            for instance_root in (first_root, second_root):
                self.assertEqual(
                    run_cli("init", "--root", str(instance_root)).returncode,
                    0,
                )

            first = start_cli(
                "adapter",
                "install",
                "codex",
                "--root",
                str(first_root),
                "--config",
                str(temporary_root / "codex.toml"),
                "--skills-dir",
                str(temporary_root / "codex-skills"),
                "--registry",
                str(registry_path),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "hold-writer-lock",
                    "MYOUTBRAIN_HOLD_SECONDS": "1",
                    "MYOUTBRAIN_LOCK_READY_FILE": str(ready_path),
                },
            )
            wait_until_lock_is_held(ready_path, first)

            second = run_cli(
                "adapter",
                "install",
                "opencode",
                "--root",
                str(second_root),
                "--config",
                str(temporary_root / "opencode.json"),
                "--skills-dir",
                str(temporary_root / "opencode-skills"),
                "--registry",
                str(registry_path),
            )
            first_stdout, first_stderr = first.communicate(timeout=5)

            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertTrue(first_stdout)
            self.assertEqual(second.returncode, 4, second.stderr)
            self.assertFalse((temporary_root / "opencode.json").exists())
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(
                registry["primary_instance"],
                str(first_root.resolve()),
            )

    def test_three_clients_have_idempotent_replaceable_state_free_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            original_instance = canonical_snapshot(instance_root)
            registry_path = temporary_root / "instances.json"
            clients = {
                "codex": ("config.toml", 'model = "test"\n'),
                "opencode": ("opencode.json", '{"theme":"test"}\n'),
                "claude-code": (".mcp.json", '{"permissions":{"allow":[]}}\n'),
            }
            for client, (config_name, existing_config) in clients.items():
                with self.subTest(client=client):
                    client_root = temporary_root / client
                    config_path = client_root / config_name
                    skills_dir = client_root / "skills"
                    client_root.mkdir()
                    config_path.write_text(existing_config, encoding="utf-8")
                    common = (
                        client,
                        "--root",
                        str(instance_root),
                        "--config",
                        str(config_path),
                        "--skills-dir",
                        str(skills_dir),
                        "--registry",
                        str(registry_path),
                    )

                    installed = run_cli("adapter", "install", *common)
                    installed_again = run_cli("adapter", "install", *common)
                    reinstalled = run_cli("adapter", "reinstall", *common)
                    checked = run_cli("adapter", "check", *common)

                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    self.assertEqual(installed_again.returncode, 0, installed_again.stderr)
                    self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    self.assertEqual(installed.stdout, installed_again.stdout)
                    check = json.loads(checked.stdout)
                    self.assertEqual(check["client"], client)
                    self.assertEqual(check["status"], "installed")
                    self.assertEqual(
                        check["protocol"],
                        {
                            "client": {
                                "maximum": {"major": 2, "minor": 2},
                                "minimum": {"major": 2, "minor": 0},
                            },
                            "compatible": True,
                            "negotiated": {"major": 2, "minor": 2},
                            "server": {"major": 2, "minor": 2},
                        },
                    )
                    self.assertTrue(check["config_matches"])
                    self.assertTrue(check["skill_matches"])
                    self.assertTrue(
                        (skills_dir / "myoutbrain" / "SKILL.md").is_file()
                    )
                    self.assertFalse(any(client_root.rglob("*.sqlite3")))
                    self.assertFalse((client_root / "store").exists())

                    discovered_check = run_cli(
                        "adapter",
                        "check",
                        client,
                        "--config",
                        str(config_path),
                        "--skills-dir",
                        str(skills_dir),
                        "--registry",
                        str(registry_path),
                    )
                    self.assertEqual(
                        discovered_check.returncode, 0, discovered_check.stderr
                    )
                    self.assertEqual(
                        json.loads(discovered_check.stdout)["instance"],
                        str(instance_root.resolve()),
                    )

                    before_uninstall = canonical_snapshot(instance_root)
                    removed = run_cli("adapter", "uninstall", *common)
                    self.assertEqual(removed.returncode, 0, removed.stderr)
                    self.assertFalse(
                        (skills_dir / "myoutbrain" / "SKILL.md").exists()
                    )
                    remaining = config_path.read_text(encoding="utf-8")
                    self.assertIn(
                        "model" if client == "codex" else (
                            "theme" if client == "opencode" else "permissions"
                        ),
                        remaining,
                    )
                    self.assertNotIn("myoutbrain", remaining.lower())
                    self.assertEqual(canonical_snapshot(instance_root), before_uninstall)

            self.assertEqual(canonical_snapshot(instance_root), original_instance)

            discovered = run_cli(
                "adapter",
                "check",
                "codex",
                "--config",
                str(temporary_root / "codex" / "config.toml"),
                "--skills-dir",
                str(temporary_root / "codex" / "skills"),
                "--registry",
                str(registry_path),
            )
            self.assertEqual(discovered.returncode, 3, discovered.stderr)
            self.assertEqual(
                json.loads(discovered.stdout)["instance"],
                str(instance_root.resolve()),
            )

    def test_installer_preserves_unmanaged_config_and_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            config_path = temporary_root / "opencode.json"
            skill_path = temporary_root / "skills" / "myoutbrain" / "SKILL.md"
            registry_path = temporary_root / "instances.json"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            unmanaged_config = {
                "mcp": {"myoutbrain": {"type": "remote", "url": "https://example.test"}}
            }
            config_path.write_text(json.dumps(unmanaged_config), encoding="utf-8")
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("user-owned skill\n", encoding="utf-8")

            result = run_cli(
                "adapter",
                "install",
                "opencode",
                "--root",
                str(instance_root),
                "--config",
                str(config_path),
                "--skills-dir",
                str(temporary_root / "skills"),
                "--registry",
                str(registry_path),
            )

            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(json.loads(config_path.read_text()), unmanaged_config)
            self.assertEqual(skill_path.read_text(encoding="utf-8"), "user-owned skill\n")

            config_path.write_text("{}\n", encoding="utf-8")
            skill_path.unlink()
            installed = run_cli(
                "adapter",
                "install",
                "opencode",
                "--root",
                str(instance_root),
                "--config",
                str(config_path),
                "--skills-dir",
                str(temporary_root / "skills"),
                "--registry",
                str(registry_path),
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            config_path.write_text(json.dumps(unmanaged_config), encoding="utf-8")

            removed = run_cli(
                "adapter",
                "uninstall",
                "opencode",
                "--root",
                str(instance_root),
                "--config",
                str(config_path),
                "--skills-dir",
                str(temporary_root / "skills"),
                "--registry",
                str(registry_path),
            )
            self.assertEqual(removed.returncode, 3, removed.stderr)
            self.assertEqual(json.loads(config_path.read_text()), unmanaged_config)
            self.assertTrue(skill_path.is_file())


if __name__ == "__main__":
    unittest.main()
