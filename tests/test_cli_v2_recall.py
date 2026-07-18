from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import cast
import unittest

from myoutbrain.memory_gateway import MemoryGateway
from myoutbrain.v2_recall import (
    CapabilityAnswerability,
    RecallMaterial,
    V2RecallRequest,
)
from tests.cli_support import run_cli


class V2RecallTests(unittest.TestCase):
    def test_gateway_calls_the_capability_engine_after_building_recall_material(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Gateway.md"
            source_path.write_text("Gateway recall remains local.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Gateway recall rule",
                "--body",
                "Gateway recall remains local.",
                "--scope",
                "gateway verification",
                "--idempotency-key",
                "proposal-gateway-recall-v1",
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
                "approve-gateway-recall-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

            class InspectingEngine:
                def __init__(self) -> None:
                    self.assert_question = ""
                    self.memories: tuple[RecallMaterial, ...] = ()

                def assess(
                    self,
                    question: str,
                    memories: tuple[RecallMaterial, ...],
                ) -> CapabilityAnswerability:
                    self.assert_question = question
                    self.memories = memories
                    return CapabilityAnswerability(answerable=True, reason="covered")

            engine = InspectingEngine()
            package = MemoryGateway(instance_root).recall_v2(
                V2RecallRequest(
                    question="Gateway recall rule",
                    task="gateway-contract",
                    entrance="codex",
                ),
                engine,
            )

            self.assertEqual(engine.assert_question, "Gateway recall rule")
            self.assertEqual(len(engine.memories), 1)
            self.assertEqual(engine.memories[0].memory_id, proposal["planned_memory_id"])
            self.assertEqual(engine.memories[0].body, "Gateway recall remains local.")
            answerability = cast(dict[str, object], package["answerability"])
            self.assertTrue(answerability["answerable"])

    def test_approved_memory_is_recalled_with_a_compact_logged_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Working Agreement.md"
            source_path.write_text(
                "A decision enters shared practice only after its owner approves it.\n",
                encoding="utf-8",
            )
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal_result = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Decision recording rule",
                "--body",
                "Record a decision only after its owner explicitly approves it.",
                "--scope",
                "team working agreements",
                "--idempotency-key",
                "proposal-recall-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            proposal = json.loads(proposal_result.stdout)
            approval_result = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-recall-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approval_result.returncode, 0, approval_result.stderr)

            recalled = run_cli(
                "recall-memory",
                "Decision recording rule: when should a decision be recorded?",
                "--task",
                "prepare-team-guidance",
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
            self.assertEqual(package["protocol_version"], {"major": 2, "minor": 1})
            self.assertRegex(package["recall_id"], r"^rec_[0-9a-f]{32}$")
            self.assertEqual(package["budget"]["limit_bytes"], 16384)
            self.assertGreater(package["budget"]["used_bytes"], 62)
            self.assertLessEqual(package["budget"]["used_bytes"], 16384)
            self.assertEqual(
                package["budget"]["used_bytes"],
                len(
                    json.dumps(
                        package,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
            )
            self.assertFalse(package["budget"]["truncated"])
            self.assertEqual(
                package["answerability"],
                {"answerable": True, "reason": "covered", "overridden_by_core": False},
            )
            self.assertEqual(
                package["source_declaration"],
                {
                    "kind": "myoutbrain",
                    "label": "根据你的 MyOutBrain 知识库",
                    "evidence_disclosure": "on-request",
                },
            )
            self.assertEqual(len(package["memories"]), 1)
            memory = package["memories"][0]
            self.assertEqual(memory["memory_id"], proposal["planned_memory_id"])
            self.assertEqual(memory["version"], 1)
            self.assertEqual(memory["state"], "current")
            self.assertEqual(
                memory["body"],
                "Record a decision only after its owner explicitly approves it.",
            )
            self.assertEqual(memory["scope"], "team working agreements")
            self.assertIn("dictionary", memory["candidate_paths"])
            self.assertIn("partition-tree", package["paths_attempted"])
            self.assertIn("local-fts", package["paths_attempted"])
            self.assertIn("global-fts", package["paths_attempted"])
            self.assertEqual(memory["evidence"]["status"], "available")
            self.assertEqual(memory["evidence"]["source_count"], 1)
            self.assertEqual(memory["evidence"]["references"][0]["retention"], "receipt")
            self.assertNotIn("locator", json.dumps(package))
            self.assertNotIn(source_path.read_text(encoding="utf-8"), json.dumps(package))

            activity_result = run_cli(
                "recall-activity",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(activity_result.returncode, 0, activity_result.stderr)
            activity = json.loads(activity_result.stdout)
            self.assertEqual(len(activity["events"]), 1)
            event = activity["events"][0]
            self.assertEqual(event["recall_id"], package["recall_id"])
            self.assertEqual(event["entrance"], "codex")
            self.assertEqual(event["task"], "prepare-team-guidance")
            self.assertEqual(event["selected_memories"], [
                {
                    "candidate_paths": memory["candidate_paths"],
                    "memory_id": proposal["planned_memory_id"],
                    "state": "current",
                    "version": 1,
                }
            ])
            self.assertEqual(event["budget"], package["budget"])
            self.assertEqual(event["answerability"], package["answerability"])
            serialized_activity = json.dumps(activity, ensure_ascii=False)
            self.assertNotIn(
                "Decision recording rule: when should a decision be recorded?",
                serialized_activity,
            )
            self.assertNotIn(memory["body"], serialized_activity)
            self.assertNotIn('"answer":', serialized_activity.casefold())

    def test_same_recall_id_expands_evidence_idempotently_with_its_own_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Evidence.md"
            source_body = (
                "Approved decisions become shared practice. "
                "The source keeps supporting detail outside canonical memory.\n"
            )
            source_path.write_text(source_body, encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Approved decision practice",
                "--body",
                "Approved decisions become shared practice.",
                "--scope",
                "team practice",
                "--idempotency-key",
                "proposal-expand-v1",
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
                "approve-expand-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            recalled = run_cli(
                "recall-memory",
                "Approved decision practice",
                "--task",
                "verify-decision-rule",
                "--entrance",
                "codex",
                "--answerable",
                "false",
                "--answerability-reason",
                "coverage-insufficient",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            package = json.loads(recalled.stdout)
            evidence_reference = package["memories"][0]["evidence"]["references"][0][
                "reference_id"
            ]

            first = run_cli(
                "expand-recall-evidence",
                package["recall_id"],
                proposal["planned_memory_id"],
                "--evidence-ref",
                evidence_reference,
                "--budget-bytes",
                "48",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "expand-recall-evidence",
                package["recall_id"],
                proposal["planned_memory_id"],
                "--evidence-ref",
                evidence_reference,
                "--budget-bytes",
                "48",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            expansion = json.loads(first.stdout)
            self.assertEqual(json.loads(repeated.stdout), expansion)
            self.assertEqual(expansion["protocol_version"], {"major": 2, "minor": 1})
            self.assertEqual(expansion["recall_id"], package["recall_id"])
            self.assertEqual(
                expansion["budget"],
                {"limit_bytes": 48, "used_bytes": 48, "truncated": True},
            )
            self.assertEqual(len(expansion["evidence"]), 1)
            evidence = expansion["evidence"][0]
            self.assertEqual(evidence["memory_id"], proposal["planned_memory_id"])
            self.assertEqual(evidence["source_id"], proposal["source"]["source_id"])
            self.assertEqual(evidence["source_version"], 1)
            self.assertEqual(evidence["retention"], "receipt")
            self.assertEqual(evidence["locator"], str(source_path.resolve()))
            self.assertEqual(evidence["excerpt"].encode("utf-8"), source_body.encode("utf-8")[:48])

            reassessed = run_cli(
                "assess-recall",
                package["recall_id"],
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(reassessed.returncode, 0, reassessed.stderr)
            self.assertEqual(
                json.loads(reassessed.stdout)["answerability"],
                {
                    "answerable": True,
                    "reason": "covered",
                    "overridden_by_core": False,
                },
            )

            activity = json.loads(
                run_cli(
                    "recall-activity",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            self.assertTrue(activity["events"][0]["evidence_expanded"])
            self.assertEqual(activity["events"][0]["budget"], package["budget"])
            self.assertTrue(activity["events"][0]["answerability"]["answerable"])

    def test_bounded_global_fts_recovers_a_memory_outside_the_routed_partition(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)

            def approve(
                *, source_name: str, name: str, body: str, scope: str, key: str
            ) -> dict[str, object]:
                source_path = temporary_root / source_name
                source_path.write_text(body + "\n", encoding="utf-8")
                proposed = run_cli(
                    "propose-source-memory",
                    str(source_path),
                    "--name",
                    name,
                    "--body",
                    body,
                    "--scope",
                    scope,
                    "--idempotency-key",
                    f"proposal-{key}",
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
                    f"approve-{key}",
                    "--entrance",
                    "codex",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                )
                self.assertEqual(approved.returncode, 0, approved.stderr)
                return cast(dict[str, object], proposal)

            approve(
                source_name="Planning.md",
                name="Weekly planning ritual",
                body="Team planning starts with the current weekly priorities.",
                scope="team planning",
                key="planning-v1",
            )
            recovery = approve(
                source_name="Recovery.md",
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="recovery-v1",
            )

            recalled = run_cli(
                "recall-memory",
                "For team planning, where should a cold snapshot be restored before verification?",
                "--task",
                "prepare-recovery-checklist",
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
            recovered = next(
                memory
                for memory in package["memories"]
                if memory["memory_id"] == recovery["planned_memory_id"]
            )
            self.assertIn("global-fts", recovered["candidate_paths"])
            self.assertNotIn("local-fts", recovered["candidate_paths"])
            self.assertTrue(package["signals"]["cross_partition_hit"])

            refused = run_cli(
                "recall-memory",
                "zyxwv unrelated unknown",
                "--task",
                "answerability-gate",
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
            self.assertEqual(refused.returncode, 0, refused.stderr)
            refused_package = json.loads(refused.stdout)
            self.assertEqual(refused_package["memories"], [])
            self.assertEqual(
                refused_package["answerability"],
                {
                    "answerable": False,
                    "reason": "coverage-insufficient",
                    "overridden_by_core": True,
                },
            )

    def test_recall_budget_truncates_whole_memories_and_answerability_is_binary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Large.md"
            body = "bounded-memory-" * 20
            source_path.write_text(body + "\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Bounded recall memory",
                "--body",
                body,
                "--scope",
                "recall budgeting",
                "--idempotency-key",
                "proposal-budgeted-recall-v1",
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
                "approve-budgeted-recall-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

            recalled = run_cli(
                "recall-memory",
                "Bounded recall memory",
                "--task",
                "small-recall-budget",
                "--entrance",
                "codex",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--budget-bytes",
                "1024",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            package = json.loads(recalled.stdout)
            self.assertEqual(package["memories"], [])
            self.assertEqual(package["budget"]["limit_bytes"], 1024)
            self.assertLessEqual(package["budget"]["used_bytes"], 1024)
            self.assertTrue(package["budget"]["truncated"])
            self.assertEqual(
                package["answerability"],
                {
                    "answerable": False,
                    "reason": "coverage-insufficient",
                    "overridden_by_core": True,
                },
            )

            invalid_contract = run_cli(
                "recall-memory",
                "Bounded recall memory",
                "--task",
                "reject-nonbinary-contract",
                "--entrance",
                "codex",
                "--answerable",
                "true",
                "--answerability-reason",
                "coverage-insufficient",
                "--root",
                str(instance_root),
            )
            self.assertEqual(invalid_contract.returncode, 2)
            self.assertIn("answerable=true requires reason covered", invalid_contract.stderr)

            free_text_task = run_cli(
                "recall-memory",
                "Bounded recall memory",
                "--task",
                "What should the answer say?",
                "--entrance",
                "codex",
                "--answerable",
                "false",
                "--answerability-reason",
                "coverage-insufficient",
                "--root",
                str(instance_root),
            )
            self.assertEqual(free_text_task.returncode, 2)
            self.assertIn("recall task must be a stable identifier", free_text_task.stderr)


if __name__ == "__main__":
    unittest.main()
