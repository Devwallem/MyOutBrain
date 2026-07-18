from __future__ import annotations

from pathlib import Path
import json
import tempfile
from typing import cast
import unittest

from tests.cli_support import run_cli


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected object, received {value!r}")
    return cast(dict[str, object], value)


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AssertionError(f"expected object list, received {value!r}")
    return cast(list[dict[str, object]], value)


class CapsuleReorganizationTests(unittest.TestCase):
    def test_fixed_recall_gate_exercises_alias_fts_history_counterevidence_and_dependency(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            historical = self._approve_memory(
                temporary_root,
                instance_root,
                key="regression-historical",
                name="Snapshot restore location",
                body=(
                    "Backups must always be restored into a new directory before "
                    "verification."
                ),
                scope="storage recovery",
            )
            replacement = self._approve_memory(
                temporary_root,
                instance_root,
                key="regression-replacement",
                name="Current recovery policy",
                body="Current recovery requires an isolated verified directory.",
                scope="storage recovery",
            )
            merge_peer = self._approve_memory(
                temporary_root,
                instance_root,
                key="regression-peer",
                name="Backup verification",
                body="Backup hashes are verified before any instance switch.",
                scope="storage recovery",
            )
            dependency_source = self._approve_memory(
                temporary_root,
                instance_root,
                key="regression-dependency-source",
                name="Former recovery policy",
                body="Former recovery allowed switching before hash verification.",
                scope="storage recovery",
            )
            collision = self._approve_memory(
                temporary_root,
                instance_root,
                key="regression-collision",
                name="Snapshot policy",
                body="Photography snapshots require an explicit publication review.",
                scope="photography",
            )
            del collision
            historical_id = cast(str, _object(historical["memory"])["memory_id"])
            replacement_id = cast(str, _object(replacement["memory"])["memory_id"])
            dependency_source_id = cast(
                str,
                _object(dependency_source["memory"])["memory_id"],
            )
            renamed = run_cli(
                "rename-memory",
                historical_id,
                "--name",
                "Snapshot policy",
                "--expected-version",
                "1",
                "--idempotency-key",
                "regression-alias-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(renamed.returncode, 0, renamed.stderr)
            historicized = run_cli(
                "historicize-memory",
                historical_id,
                "--reason",
                "The rule is retained for historical recovery decisions.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "regression-historical-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(historicized.returncode, 0, historicized.stderr)
            superseded = run_cli(
                "supersede-memory",
                dependency_source_id,
                "--replacement-memory-id",
                replacement_id,
                "--replacement-version",
                "1",
                "--reason",
                "The current verified recovery policy replaces the former rule.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "regression-dependency-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            conflict_source = temporary_root / "regression-counterevidence.md"
            conflict_source.write_text("Conflicting restore policy.\n", encoding="utf-8")
            conflict = run_cli(
                "propose-source-memory",
                str(conflict_source),
                "--name",
                "Snapshot policy",
                "--body",
                (
                    "Backups must never be restored into a new directory before "
                    "verification."
                ),
                "--scope",
                "storage recovery",
                "--idempotency-key",
                "regression-counterevidence-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(conflict.returncode, 0, conflict.stderr)
            self.assertEqual(_object(json.loads(conflict.stdout))["suggested_action"], "conflict")

            source_ids = [
                cast(str, _object(historical["primary_capsule"])["capsule_id"]),
                cast(str, _object(merge_peer["primary_capsule"])["capsule_id"]),
            ]
            reorganized = self._gateway(
                temporary_root,
                instance_root,
                client="claude-code",
                operation="maintenance.reorganize",
                parameters={"action": "merge", "source_capsule_ids": source_ids},
                write={"idempotency_key": "six-category-gate-v1", "expected_version": 1},
            )
            categories = cast(
                dict[str, object],
                _object(_object(reorganized["result"])["recall_regression"])["categories"],
            )
            for category in (
                "name-collision",
                "old-alias",
                "cross-partition-fts",
                "historical-trusted",
                "counterevidence",
                "dependency",
            ):
                self.assertGreater(len(cast(list[object], categories[category])), 0, category)
            cross_partition_cases = _objects(categories["cross-partition-fts"])
            self.assertTrue(
                any(
                    _object(case["signature"])["cross_partition_hit"]
                    for case in cross_partition_cases
                ),
                "the fixed gate must exercise a real routed-capsule miss",
            )

    def test_capacity_and_sparse_topic_conditions_produce_executable_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            configuration_path = instance_root / "myoutbrain.toml"
            configuration = configuration_path.read_text(encoding="utf-8")
            configuration_path.write_text(
                configuration.replace(
                    "capsule_target_bytes = 65536",
                    "capsule_target_bytes = 1000",
                ).replace(
                    "capsule_hard_limit_bytes = 131072",
                    "capsule_hard_limit_bytes = 2000",
                ),
                encoding="utf-8",
            )
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="capacity-first",
                name="Snapshot restore",
                body="Restore every cold snapshot into a verified new directory.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="capacity-second",
                name="Backup verification",
                body="Verify every backup before replacing an active private instance.",
                scope="recovery",
            )
            source_ids = [
                cast(str, _object(first["primary_capsule"])["capsule_id"]),
                cast(str, _object(second["primary_capsule"])["capsule_id"]),
            ]
            sparse = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.plan",
                parameters={},
            )
            sparse_plans = _objects(_object(sparse["result"])["plans"])
            merge_plan = next(plan for plan in sparse_plans if plan["action"] == "merge")
            self.assertEqual(merge_plan["reason"], "sparse-same-topic")
            self.assertEqual(set(cast(list[object], merge_plan["source_capsule_ids"])), set(source_ids))

            merged = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={"action": "merge", "source_capsule_ids": source_ids},
                write={"idempotency_key": "capacity-merge-v1", "expected_version": 1},
            )
            merged_capsule_id = cast(
                str,
                cast(list[object], _object(merged["result"])["target_capsule_ids"])[0],
            )
            merged_structure = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.inspect",
                parameters={},
            )
            merged_capsule = next(
                capsule
                for capsule in _objects(_object(merged_structure["result"])["capsules"])
                if capsule["capsule_id"] == merged_capsule_id
            )
            merged_partition_id = cast(
                str,
                _object(merged_capsule["partition"])["partition_id"],
            )
            self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.configure_partition",
                parameters={
                    "partition_id": merged_partition_id,
                    "merge_forbidden": True,
                },
                write={
                    "idempotency_key": "capacity-no-merge-v1",
                    "expected_version": 0,
                },
            )
            merged_configuration = configuration_path.read_text(encoding="utf-8")
            configuration_path.write_text(
                merged_configuration.replace(
                    "capsule_target_bytes = 1000",
                    "capsule_target_bytes = 60",
                ).replace(
                    "capsule_hard_limit_bytes = 2000",
                    "capsule_hard_limit_bytes = 80",
                ),
                encoding="utf-8",
            )
            capacity = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.plan",
                parameters={},
            )
            capacity_plans = _objects(_object(capacity["result"])["plans"])
            split_plan = next(plan for plan in capacity_plans if plan["action"] == "split")
            self.assertEqual(split_plan["reason"], "capacity-hard-limit")
            self.assertEqual(split_plan["source_capsule_ids"], [merged_capsule_id])

            split = self._gateway(
                temporary_root,
                instance_root,
                client="claude-code",
                operation="maintenance.reorganize",
                parameters={
                    "action": "split",
                    "source_capsule_ids": [merged_capsule_id],
                },
                write={"idempotency_key": "capacity-split-v1", "expected_version": 2},
            )
            split_result = _object(split["result"])
            self.assertEqual(split_result["status"], "retired")
            self.assertEqual(len(cast(list[object], split_result["target_capsule_ids"])), 2)
            self.assertTrue(_object(split_result["recall_regression"])["equivalent"])
            split_structure = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.inspect",
                parameters={},
            )
            target_ids = set(cast(list[str], split_result["target_capsule_ids"]))
            self.assertTrue(
                all(
                    _object(capsule["partition"])["merge_forbidden"]
                    for capsule in _objects(_object(split_structure["result"])["capsules"])
                    if capsule["capsule_id"] in target_ids
                )
            )

    def test_topic_split_rejects_a_target_over_the_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            configuration_path = instance_root / "myoutbrain.toml"
            configuration = configuration_path.read_text(encoding="utf-8")
            configuration_path.write_text(
                configuration.replace(
                    "capsule_target_bytes = 65536",
                    "capsule_target_bytes = 40",
                ).replace(
                    "capsule_hard_limit_bytes = 131072",
                    "capsule_hard_limit_bytes = 80",
                ),
                encoding="utf-8",
            )
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="hard-first",
                name="First hard-limit record",
                body="A" * 50,
                scope="hard limit",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="hard-second",
                name="Second hard-limit record",
                body="B" * 50,
                scope="hard limit",
            )
            third = self._approve_memory(
                temporary_root,
                instance_root,
                key="hard-third",
                name="Third hard-limit record",
                body="C" * 10,
                scope="hard limit",
            )
            source_ids = [
                cast(str, _object(first["primary_capsule"])["capsule_id"]),
                cast(str, _object(second["primary_capsule"])["capsule_id"]),
                cast(str, _object(third["primary_capsule"])["capsule_id"]),
            ]
            configuration_path.write_text(
                configuration.replace(
                    "capsule_target_bytes = 65536",
                    "capsule_target_bytes = 200",
                ).replace(
                    "capsule_hard_limit_bytes = 131072",
                    "capsule_hard_limit_bytes = 200",
                ),
                encoding="utf-8",
            )
            merged = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={"action": "merge", "source_capsule_ids": source_ids},
                write={"idempotency_key": "hard-merge-v1", "expected_version": 1},
            )
            merged_id = cast(
                str,
                cast(list[object], _object(merged["result"])["target_capsule_ids"])[0],
            )
            configuration_path.write_text(
                configuration.replace(
                    "capsule_target_bytes = 65536",
                    "capsule_target_bytes = 40",
                ).replace(
                    "capsule_hard_limit_bytes = 131072",
                    "capsule_hard_limit_bytes = 80",
                ),
                encoding="utf-8",
            )
            blocked = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={
                    "action": "split",
                    "source_capsule_ids": [merged_id],
                    "topic_groups": [
                        {
                            "topic": "too large",
                            "memory_ids": [
                                cast(str, _object(first["memory"])["memory_id"]),
                                cast(str, _object(second["memory"])["memory_id"]),
                            ],
                        },
                        {
                            "topic": "small remainder",
                            "memory_ids": [
                                cast(str, _object(third["memory"])["memory_id"])
                            ],
                        },
                    ],
                },
                write={"idempotency_key": "hard-split-v1", "expected_version": 2},
                expected_returncode=2,
            )
            self.assertEqual(_object(blocked["error"])["category"], "invalid_request")

    def test_recall_regression_failure_rolls_back_the_pointer_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="gate-first",
                name="Snapshot recovery",
                body="Restore snapshots into a new directory.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="gate-second",
                name="Backup verification",
                body="Verify backups before switching instances.",
                scope="recovery",
            )
            source_ids = [
                cast(str, _object(first["primary_capsule"])["capsule_id"]),
                cast(str, _object(second["primary_capsule"])["capsule_id"]),
            ]
            failed = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={"action": "merge", "source_capsule_ids": source_ids},
                write={"idempotency_key": "regression-gate-v1", "expected_version": 1},
                expected_returncode=2,
                environment={"MYOUTBRAIN_CAPSULE_REGRESSION_FAIL": "1"},
            )
            self.assertEqual(
                _object(failed["error"])["category"],
                "recall_regression_failed",
            )
            inspected = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.inspect",
                parameters={},
            )
            structure = _object(inspected["result"])
            self.assertEqual(structure["structural_version"], 1)
            capsules = _objects(structure["capsules"])
            self.assertEqual(
                {
                    capsule["status"]
                    for capsule in capsules
                    if capsule["capsule_id"] in source_ids
                },
                {"active"},
            )
            self.assertFalse(
                any(capsule["status"] in ("redirecting", "retired") for capsule in capsules)
            )
            self.assertFalse(
                any(capsule["status"] == "staged" for capsule in capsules)
            )

    def test_every_reorganization_stage_recovers_by_retrying_the_same_write(self) -> None:
        for stage in ("planned", "staged", "validated", "switched", "retired"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                instance_root = temporary_root / "MyOutBrain"
                self.assertEqual(
                    run_cli("init", "--root", str(instance_root)).returncode,
                    0,
                )
                first = self._approve_memory(
                    temporary_root,
                    instance_root,
                    key=f"fault-{stage}-first",
                    name="Snapshot recovery",
                    body="Restore snapshots into a new directory.",
                    scope="recovery",
                )
                second = self._approve_memory(
                    temporary_root,
                    instance_root,
                    key=f"fault-{stage}-second",
                    name="Backup verification",
                    body="Verify backups before switching instances.",
                    scope="recovery",
                )
                source_ids = [
                    cast(str, _object(first["primary_capsule"])["capsule_id"]),
                    cast(str, _object(second["primary_capsule"])["capsule_id"]),
                ]
                write = {
                    "idempotency_key": f"recover-{stage}-v1",
                    "expected_version": 1,
                }
                failed = self._gateway(
                    temporary_root,
                    instance_root,
                    client="codex",
                    operation="maintenance.reorganize",
                    parameters={
                        "action": "merge",
                        "source_capsule_ids": source_ids,
                    },
                    write=write,
                    expected_returncode=7,
                    environment={"MYOUTBRAIN_CAPSULE_FAULT_STAGE": stage},
                )
                self.assertEqual(_object(failed["error"])["category"], "integrity_failure")
                after_failure = self._gateway(
                    temporary_root,
                    instance_root,
                    client="claude-code",
                    operation="maintenance.inspect",
                    parameters={},
                )
                if stage in ("planned", "staged", "validated"):
                    self.assertFalse(
                        any(
                            capsule["status"] == "staged"
                            for capsule in _objects(
                                _object(after_failure["result"])["capsules"]
                            )
                        ),
                        f"pre-switch failure at {stage} must remove staged copies",
                    )

                recovered = self._gateway(
                    temporary_root,
                    instance_root,
                    client="opencode",
                    operation="maintenance.reorganize",
                    parameters={
                        "action": "merge",
                        "source_capsule_ids": source_ids,
                    },
                    write=write,
                )
                result = _object(recovered["result"])
                self.assertEqual(result["status"], "retired")
                self.assertEqual(result["structural_version"], 2)
                self.assertTrue(_object(result["recall_regression"])["equivalent"])

    def test_validated_retry_aborts_and_proposes_review_after_semantic_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="drift-first",
                name="Snapshot recovery",
                body="Restore snapshots into a new directory.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="drift-second",
                name="Backup verification",
                body="Verify backups before switching instances.",
                scope="recovery",
            )
            first_memory_id = cast(str, _object(first["memory"])["memory_id"])
            source_ids = [
                cast(str, _object(first["primary_capsule"])["capsule_id"]),
                cast(str, _object(second["primary_capsule"])["capsule_id"]),
            ]
            parameters: dict[str, object] = {
                "action": "merge",
                "source_capsule_ids": source_ids,
            }
            write: dict[str, object] = {
                "idempotency_key": "drift-merge-v1",
                "expected_version": 1,
            }
            failed = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters=parameters,
                write=write,
                expected_returncode=7,
                environment={"MYOUTBRAIN_CAPSULE_FAULT_STAGE": "validated"},
            )
            self.assertEqual(_object(failed["error"])["category"], "integrity_failure")
            revised = run_cli(
                "revise-memory",
                first_memory_id,
                "--body",
                "Restore snapshots into an isolated verified directory.",
                "--reason",
                "The approved recovery procedure changed.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "drift-revision-v2",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(revised.returncode, 0, revised.stderr)
            aborted = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.reorganize",
                parameters=parameters,
                write=write,
            )
            result = _object(aborted["result"])
            self.assertEqual(result["status"], "aborted")
            self.assertEqual(result["reason"], "semantic-change-requires-review")
            self.assertEqual(result["structural_version"], 1)
            proposal = _object(result["proposal"])
            self.assertEqual(proposal["intent"], "research")
            self.assertEqual(
                _object(proposal["approval_effect"])["type"],
                "create_research_thread",
            )
            self.assertIn("Before integrity hash:", cast(str, proposal["content"]))
            self.assertIn("Current integrity hash:", cast(str, proposal["content"]))

    def test_semantic_difference_aborts_maintenance_and_creates_review_proposal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="semantic-first",
                name="Snapshot restore",
                body="Restore snapshots only into a new directory.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="semantic-second",
                name="Backup verification",
                body="Verify backups before switching instances.",
                scope="recovery",
            )
            first_memory_id = cast(str, _object(first["memory"])["memory_id"])
            first_capsule_id = cast(str, _object(first["primary_capsule"])["capsule_id"])
            second_capsule_id = cast(str, _object(second["primary_capsule"])["capsule_id"])
            before = self._recall(instance_root, "Snapshot restore")

            aborted = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={
                    "action": "merge",
                    "source_capsule_ids": [first_capsule_id, second_capsule_id],
                    "proposed_bodies": {
                        first_memory_id: "Snapshots may overwrite the active directory.",
                    },
                },
                write={
                    "idempotency_key": "semantic-change-abort-v1",
                    "expected_version": 1,
                },
            )

            result = _object(aborted["result"])
            self.assertEqual(result["status"], "aborted")
            self.assertEqual(result["reason"], "semantic-change-requires-review")
            self.assertEqual(result["structural_version"], 1)
            proposal = _object(result["proposal"])
            self.assertEqual(proposal["intent"], "integrate")
            self.assertEqual(_object(proposal["target"])["memory_id"], first_memory_id)
            self.assertEqual(
                _object(proposal["approval_effect"])["type"],
                "revise_canonical_memory",
            )
            queue = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="review.list",
                parameters={},
            )
            self.assertIn(
                proposal["proposal_id"],
                [item["proposal_id"] for item in _objects(_object(queue["result"])["proposals"])],
            )
            after = self._recall(instance_root, "Snapshot restore")
            self.assertEqual(
                [
                    (memory["memory_id"], memory["version"], memory["body"])
                    for memory in _objects(after["memories"])
                ],
                [
                    (memory["memory_id"], memory["version"], memory["body"])
                    for memory in _objects(before["memories"])
                ],
            )

    def test_topic_plan_splits_a_merged_capsule_without_changing_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="split-first",
                name="Snapshot restore",
                body="Restore snapshots only into a new directory.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="split-second",
                name="Integrity verification",
                body="Verify integrity before changing the active instance.",
                scope="recovery",
            )
            first_memory_id = cast(str, _object(first["memory"])["memory_id"])
            second_memory_id = cast(str, _object(second["memory"])["memory_id"])
            first_capsule_id = cast(str, _object(first["primary_capsule"])["capsule_id"])
            second_capsule_id = cast(str, _object(second["primary_capsule"])["capsule_id"])
            merged = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={
                    "action": "merge",
                    "source_capsule_ids": [first_capsule_id, second_capsule_id],
                },
                write={"idempotency_key": "merge-before-split", "expected_version": 1},
            )
            merged_capsule_id = cast(
                str,
                cast(list[object], _object(merged["result"])["target_capsule_ids"])[0],
            )
            before = {
                first_memory_id: self._recall(instance_root, "Snapshot restore"),
                second_memory_id: self._recall(instance_root, "Integrity verification"),
            }
            topic_groups = [
                {
                    "topic": "snapshot recovery",
                    "memory_ids": [first_memory_id],
                },
                {
                    "topic": "integrity verification",
                    "memory_ids": [second_memory_id],
                },
            ]
            planned = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.plan",
                parameters={
                    "source_capsule_id": merged_capsule_id,
                    "topic_groups": topic_groups,
                },
            )
            topic_plan = next(
                plan
                for plan in _objects(_object(planned["result"])["plans"])
                if plan["reason"] == "topic-divergence"
            )
            self.assertEqual(topic_plan["source_capsule_ids"], [merged_capsule_id])

            split = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.reorganize",
                parameters={
                    "action": "split",
                    "source_capsule_ids": [merged_capsule_id],
                    "topic_groups": topic_groups,
                },
                write={"idempotency_key": "topic-split-v1", "expected_version": 2},
            )

            result = _object(split["result"])
            self.assertEqual(result["action"], "split")
            self.assertEqual(result["structural_version"], 3)
            self.assertEqual(len(cast(list[object], result["target_capsule_ids"])), 2)
            redirects = _objects(result["redirects"])
            self.assertEqual(
                {redirect["source_capsule_id"] for redirect in redirects},
                {merged_capsule_id},
            )
            self.assertEqual(len(redirects), 2)
            inspected = self._gateway(
                temporary_root,
                instance_root,
                client="claude-code",
                operation="maintenance.inspect",
                parameters={},
            )
            active = [
                capsule
                for capsule in _objects(_object(inspected["result"])["capsules"])
                if capsule["status"] == "active"
            ]
            self.assertEqual(
                {capsule["topic"] for capsule in active},
                {"snapshot recovery", "integrity verification"},
            )
            self.assertEqual(
                sorted(
                    cast(str, memory_id)
                    for capsule in active
                    for memory_id in cast(
                        list[object], capsule["primary_memory_ids"]
                    )
                ),
                sorted((first_memory_id, second_memory_id)),
            )
            for memory_id, prior_package in before.items():
                query = (
                    "Snapshot restore"
                    if memory_id == first_memory_id
                    else "Integrity verification"
                )
                current_package = self._recall(instance_root, query)
                prior = next(
                    memory
                    for memory in _objects(prior_package["memories"])
                    if memory["memory_id"] == memory_id
                )
                current = next(
                    memory
                    for memory in _objects(current_package["memories"])
                    if memory["memory_id"] == memory_id
                )
                self.assertEqual(
                    (current["memory_id"], current["version"], current["body"]),
                    (prior["memory_id"], prior["version"], prior["body"]),
                )

    def test_sparse_capsules_merge_by_copy_switch_and_redirect_without_recall_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="merge-first",
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before switching.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="merge-second",
                name="Backup verification",
                body="Verify a backup before it can replace the active instance.",
                scope="recovery",
            )
            before = {
                "first": self._recall(instance_root, "Cold snapshot recovery"),
                "second": self._recall(instance_root, "Backup verification"),
            }
            first_capsule_id = cast(str, _object(first["primary_capsule"])["capsule_id"])
            second_capsule_id = cast(str, _object(second["primary_capsule"])["capsule_id"])

            merged = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={
                    "action": "merge",
                    "source_capsule_ids": [first_capsule_id, second_capsule_id],
                },
                write={"idempotency_key": "merge-recovery-v1", "expected_version": 1},
            )

            result = _object(merged["result"])
            self.assertEqual(result["action"], "merge")
            self.assertEqual(result["status"], "retired")
            self.assertEqual(result["structural_version"], 2)
            self.assertEqual(
                result["completed_stages"],
                ["planned", "staged", "validated", "switched", "retired"],
            )
            target_ids = cast(list[object], result["target_capsule_ids"])
            self.assertEqual(len(target_ids), 1)
            redirects = _objects(result["redirects"])
            self.assertEqual(
                {redirect["source_capsule_id"] for redirect in redirects},
                {first_capsule_id, second_capsule_id},
            )
            self.assertEqual(
                {redirect["target_capsule_id"] for redirect in redirects},
                set(target_ids),
            )
            regression = _object(result["recall_regression"])
            self.assertTrue(regression["equivalent"])
            self.assertEqual(
                _object(result["integrity"]),
                {
                    "records_complete": True,
                    "unique_primary_copy": True,
                    "dictionary_pointer_closure": True,
                    "semantic_hashes_unchanged": True,
                    "memory_record_count": 2,
                },
            )
            self.assertEqual(
                set(cast(dict[str, object], regression["categories"])),
                {
                    "name-collision",
                    "old-alias",
                    "cross-partition-fts",
                    "historical-trusted",
                    "counterevidence",
                    "dependency",
                },
            )
            inspected = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.inspect",
                parameters={},
            )
            retired_sources = [
                capsule
                for capsule in _objects(_object(inspected["result"])["capsules"])
                if capsule["capsule_id"] in (first_capsule_id, second_capsule_id)
            ]
            self.assertEqual(
                [
                    (
                        capsule["status"],
                        capsule["body_bytes"],
                        capsule["memory_record_count"],
                        capsule["primary_memory_ids"],
                    )
                    for capsule in retired_sources
                ],
                [("retired", 0, 0, []), ("retired", 0, 0, [])],
            )

            after = {
                "first": self._recall(instance_root, "Cold snapshot recovery"),
                "second": self._recall(instance_root, "Backup verification"),
            }
            for key in before:
                before_memories = _objects(before[key]["memories"])
                after_memories = _objects(after[key]["memories"])
                self.assertEqual(
                    [
                        (memory["memory_id"], memory["version"], memory["body"])
                        for memory in after_memories
                    ],
                    [
                        (memory["memory_id"], memory["version"], memory["body"])
                        for memory in before_memories
                    ],
                )

    def test_partition_constraints_persist_across_clients_and_block_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = self._approve_memory(
                temporary_root,
                instance_root,
                key="first",
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before switching.",
                scope="recovery",
            )
            second = self._approve_memory(
                temporary_root,
                instance_root,
                key="second",
                name="Backup verification",
                body="Verify a backup before it can replace the active instance.",
                scope="recovery",
            )
            inspected = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.inspect",
                parameters={},
            )
            structure = _object(inspected["result"])
            capsules = _objects(structure["capsules"])
            first_capsule_id = cast(str, _object(first["primary_capsule"])["capsule_id"])
            second_capsule_id = cast(str, _object(second["primary_capsule"])["capsule_id"])
            first_capsule = next(
                capsule for capsule in capsules if capsule["capsule_id"] == first_capsule_id
            )
            partition_id = cast(str, _object(first_capsule["partition"])["partition_id"])

            configured = self._gateway(
                temporary_root,
                instance_root,
                client="opencode",
                operation="maintenance.configure_partition",
                parameters={
                    "partition_id": partition_id,
                    "name": "Recovery rules",
                    "pinned": True,
                    "merge_forbidden": True,
                },
                write={"idempotency_key": "pin-recovery-v1", "expected_version": 0},
            )
            constraint = _object(_object(configured["result"])["constraint"])
            self.assertEqual(constraint["name"], "Recovery rules")
            self.assertTrue(constraint["pinned"])
            self.assertTrue(constraint["merge_forbidden"])

            cross_client = self._gateway(
                temporary_root,
                instance_root,
                client="claude-code",
                operation="maintenance.inspect",
                parameters={},
            )
            cross_capsules = _objects(_object(cross_client["result"])["capsules"])
            persisted = next(
                capsule
                for capsule in cross_capsules
                if _object(capsule["partition"])["partition_id"] == partition_id
            )
            self.assertEqual(_object(persisted["partition"])["name"], "Recovery rules")
            self.assertTrue(_object(persisted["partition"])["pinned"])

            blocked = self._gateway(
                temporary_root,
                instance_root,
                client="codex",
                operation="maintenance.reorganize",
                parameters={
                    "action": "merge",
                    "source_capsule_ids": [first_capsule_id, second_capsule_id],
                },
                write={"idempotency_key": "blocked-merge-v1", "expected_version": 1},
                expected_returncode=2,
            )
            error = _object(blocked["error"])
            self.assertEqual(error["category"], "constraint_conflict")

    def _approve_memory(
        self,
        temporary_root: Path,
        instance_root: Path,
        *,
        key: str,
        name: str,
        body: str,
        scope: str,
    ) -> dict[str, object]:
        source = temporary_root / f"{key}.md"
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
            f"propose-{key}",
            "--root",
            str(instance_root),
            "--format",
            "json",
        )
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        proposal = _object(json.loads(proposed.stdout))
        approved = run_cli(
            "approve-source-memory",
            cast(str, proposal["proposal_id"]),
            "--expected-version",
            "0",
            "--idempotency-key",
            f"approve-{key}",
            "--entrance",
            "codex",
            "--root",
            str(instance_root),
            "--format",
            "json",
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        return _object(json.loads(approved.stdout))

    def _gateway(
        self,
        temporary_root: Path,
        instance_root: Path,
        *,
        client: str,
        operation: str,
        parameters: dict[str, object],
        write: dict[str, object] | None = None,
        expected_returncode: int = 0,
        environment: dict[str, str] | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "protocol": {
                "minimum": {"major": 2, "minor": 0},
                "maximum": {"major": 2, "minor": 2},
            },
            "client": {
                "name": client,
                "capabilities": ["capsule_maintenance.v1"],
            },
            "operation": operation,
            "parameters": parameters,
        }
        if write is not None:
            request["write"] = write
        request_path = temporary_root / f"{operation.replace('.', '-')}-{client}.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False),
            encoding="utf-8",
        )
        result = run_cli(
            "gateway",
            str(request_path),
            "--root",
            str(instance_root),
            environment=environment,
        )
        self.assertEqual(
            result.returncode,
            expected_returncode,
            result.stderr or result.stdout,
        )
        return _object(json.loads(result.stdout))

    def _recall(self, instance_root: Path, question: str) -> dict[str, object]:
        result = run_cli(
            "recall-memory",
            question,
            "--task",
            "capsule-maintenance",
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
        self.assertEqual(result.returncode, 0, result.stderr)
        return _object(json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
