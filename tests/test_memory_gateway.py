from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from myoutbrain.memory_gateway import (
    Answerability,
    ExperienceSubmission,
    MemoryAccess,
    MemoryGateway,
    MemoryState,
    QueryPurpose,
    RecallMatch,
    RecallRequest,
)
from tests.cli_support import run_cli


class MemoryGatewayTests(unittest.TestCase):
    def test_entrance_submits_only_visible_experience_through_shared_gateway(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            visible_experience = temporary_root / "visible-task.txt"
            visible_experience.write_text(
                "The visible task confirms that release notes require source links.",
                encoding="utf-8",
            )

            receipt = MemoryGateway(instance_root).submit(
                ExperienceSubmission(
                    experience_path=visible_experience,
                    occurred_at="2026-07-18T09:00:00+08:00",
                    entrance="codex",
                    task_pointer="release-notes",
                    digest="Release notes must retain source links.",
                    sensitivity="local-only",
                    visible_context="current release-notes task only",
                    context_gaps=("earlier task messages are unavailable",),
                )
            )
            recalled = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="What must release notes retain?",
                    task="release-notes",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(recalled.items[0].memory_id, receipt.digest_id)
            self.assertEqual(recalled.items[0].entrance, "codex")
            self.assertEqual(recalled.items[0].task, "release-notes")

    def test_gateway_coordinates_proposal_and_canonical_review_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            visible_experience = temporary_root / "visible-task.txt"
            visible_experience.write_text(
                "Project Rowan requires signed provenance records.",
                encoding="utf-8",
            )
            gateway = MemoryGateway(instance_root)
            receipt = gateway.submit(
                ExperienceSubmission(
                    experience_path=visible_experience,
                    occurred_at="2026-07-18T09:30:00+08:00",
                    entrance="codex",
                    task_pointer="rowan-release",
                    digest="Rowan releases require signed provenance records.",
                    sensitivity="local-only",
                    visible_context="current Rowan release task",
                    context_gaps=("earlier Rowan history is unavailable",),
                )
            )

            proposals = gateway.propose_consolidation("rowan-release")
            proposal = next(
                item
                for item in proposals
                if receipt.digest_id in item.evidence_memory_ids
            )
            review = gateway.review_proposal(proposal.proposal_id, "accept")

            self.assertEqual(review.decision, "accepted")
            self.assertIsNotNone(review.canonical_memory_id)

    def test_new_buffered_memory_is_immediately_recalled_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text(
                "We agreed to record every unavailable part of the task history.",
                encoding="utf-8",
            )
            capture = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T12:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "implement-02",
                "--digest",
                "Explicit context gaps prevent false claims of remembered history.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current implementation task",
                "--context-gap",
                "messages before this task are unavailable",
                "--format",
                "json",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            receipt = json.loads(capture.stdout)

            package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="How should explicit context gaps be handled?",
                    task="implement-02",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertTrue(package.retrieval_performed)
            self.assertTrue(package.common_knowledge_queried)
            self.assertEqual(package.answerability, Answerability.INSUFFICIENT)
            self.assertEqual(len(package.items), 1)
            item = package.items[0]
            self.assertEqual(item.memory_id, receipt["digest_id"])
            self.assertIn("Explicit context gaps", item.content)
            self.assertEqual(item.memory_state, MemoryState.BUFFERED)
            self.assertFalse(item.confirmed)
            self.assertEqual(item.source_ids, (receipt["source_id"],))
            self.assertEqual(item.occurred_at, "2026-07-17T12:00:00+08:00")
            self.assertEqual(item.sensitivity, "local-only")
            self.assertEqual(item.entrance, "codex")
            self.assertEqual(item.task, "implement-02")
            self.assertEqual(item.match, RecallMatch.FULL_TEXT)

    def test_explicit_source_relation_bypasses_lexical_and_task_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text(
                "A stable source relation should not depend on similar wording.",
                encoding="utf-8",
            )
            capture = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T12:30:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "original-task",
                "--digest",
                "Stable provenance links provide deterministic recall.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "original task",
                "--context-gap",
                "other tasks unavailable",
                "--format",
                "json",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            receipt = json.loads(capture.stdout)

            package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="completely unrelated vocabulary",
                    task="different-task",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    source_ids=(receipt["source_id"],),
                )
            )
            identity_package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="deterministic recall",
                    task="different-task",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    memory_ids=(receipt["digest_id"],),
                    source_ids=(receipt["source_id"],),
                )
            )

            self.assertEqual(len(package.items), 1)
            self.assertEqual(package.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(package.items[0].match, RecallMatch.SOURCE_RELATION)
            self.assertEqual(len(identity_package.items), 1)
            self.assertEqual(
                identity_package.items[0].match,
                RecallMatch.STABLE_IDENTITY,
            )

    def test_three_access_levels_bound_task_and_sensitivity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            captures: dict[str, dict[str, object]] = {}
            scenarios = (
                ("local-alpha", "task-alpha", "local-only", "private Aurora constraint"),
                ("cloud-alpha", "task-alpha", "cloud-allowed", "public Aurora fact"),
                ("cloud-beta", "task-beta", "cloud-allowed", "unrelated Aurora task"),
            )
            for index, (name, task, sensitivity, detail) in enumerate(scenarios):
                conversation = temporary_root / f"{name}.txt"
                conversation.write_text(f"Conversation for {detail}.", encoding="utf-8")
                capture = run_cli(
                    "remember",
                    str(conversation),
                    "--root",
                    str(instance_root),
                    "--occurred-at",
                    f"2026-07-17T13:0{index}:00+08:00",
                    "--entrance",
                    "codex",
                    "--task",
                    task,
                    "--digest",
                    f"Project Aurora records a {detail}.",
                    "--sensitivity",
                    sensitivity,
                    "--visible-context",
                    task,
                    "--context-gap",
                    "other tasks unavailable",
                    "--format",
                    "json",
                )
                self.assertEqual(capture.returncode, 0, capture.stderr)
                captures[name] = json.loads(capture.stdout)

            gateway = MemoryGateway(instance_root)
            task_scoped = gateway.recall(
                RecallRequest(
                    query="Project Aurora",
                    task="task-alpha",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )
            public_external = gateway.recall(
                RecallRequest(
                    query="Project Aurora",
                    task="task-alpha",
                    access=MemoryAccess.PUBLIC_EXTERNAL,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )
            local_trusted = gateway.recall(
                RecallRequest(
                    query="Project Aurora",
                    task="task-alpha",
                    access=MemoryAccess.LOCAL_TRUSTED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(
                {item.memory_id for item in task_scoped.items},
                {
                    captures["local-alpha"]["digest_id"],
                    captures["cloud-alpha"]["digest_id"],
                },
            )
            self.assertEqual(
                [item.memory_id for item in public_external.items],
                [captures["cloud-alpha"]["digest_id"]],
            )
            self.assertEqual(
                {item.memory_id for item in local_trusted.items},
                {
                    captures["local-alpha"]["digest_id"],
                    captures["cloud-alpha"]["digest_id"],
                    captures["cloud-beta"]["digest_id"],
                },
            )

    def test_public_access_uses_the_strongest_sensitivity_for_shared_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "shared.txt"
            conversation.write_text(
                "One body can be referenced by entrances with different sensitivity.",
                encoding="utf-8",
            )
            common = (
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--entrance",
                "codex",
                "--visible-context",
                "current task",
                "--context-gap",
                "other tasks unavailable",
                "--format",
                "json",
            )
            cloud_capture = run_cli(
                *common,
                "--occurred-at",
                "2026-07-17T13:30:00+08:00",
                "--task",
                "shared-task",
                "--digest",
                "Project Juniper has a shared source.",
                "--sensitivity",
                "cloud-allowed",
            )
            private_capture = run_cli(
                *common,
                "--occurred-at",
                "2026-07-17T13:31:00+08:00",
                "--task",
                "private-review",
                "--digest",
                "Project Juniper source requires local handling.",
                "--sensitivity",
                "local-only",
            )
            self.assertEqual(cloud_capture.returncode, 0, cloud_capture.stderr)
            self.assertEqual(private_capture.returncode, 0, private_capture.stderr)

            package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="Project Juniper",
                    task="shared-task",
                    access=MemoryAccess.PUBLIC_EXTERNAL,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(package.items, ())
            self.assertEqual(package.answerability, Answerability.INSUFFICIENT)

    def test_casual_queries_skip_retrieval_and_unknown_substantive_queries_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            gateway = MemoryGateway(instance_root)

            casual = gateway.recall(
                RecallRequest(
                    query="hello",
                    task="small-talk",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.CASUAL,
                )
            )
            unknown = gateway.recall(
                RecallRequest(
                    query="What is the unrecorded launch date?",
                    task="launch-plan",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertFalse(casual.retrieval_performed)
            self.assertFalse(casual.common_knowledge_queried)
            self.assertEqual(casual.answerability, Answerability.NOT_REQUIRED)
            self.assertEqual(casual.items, ())
            self.assertTrue(unknown.retrieval_performed)
            self.assertTrue(unknown.common_knowledge_queried)
            self.assertEqual(unknown.answerability, Answerability.INSUFFICIENT)
            self.assertEqual(unknown.items, ())

    def test_full_text_recall_matches_an_exact_chinese_phrase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text("我们确认了本轮发布使用的内部代号。", encoding="utf-8")
            capture = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T15:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "发布计划",
                "--digest",
                "本轮发布代号是苍蓝雀。",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "当前发布任务",
                "--context-gap",
                "早期命名讨论不可见",
                "--format",
                "json",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            receipt = json.loads(capture.stdout)

            package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="苍蓝雀",
                    task="发布计划",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(len(package.items), 1)
            self.assertEqual(package.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(package.items[0].match, RecallMatch.FULL_TEXT)

    def test_full_text_recall_excludes_items_sharing_only_a_generic_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            receipts: dict[str, dict[str, object]] = {}
            scenarios = (
                (
                    "target",
                    "Project Orion deployment requires signed manifests.",
                    "Orion deployment evidence.",
                ),
                (
                    "distractor",
                    "Project Atlas meetings use a generic project checklist.",
                    "Atlas meeting evidence.",
                ),
            )
            for index, (name, digest, body) in enumerate(scenarios):
                conversation = temporary_root / f"{name}.txt"
                conversation.write_text(body, encoding="utf-8")
                capture = run_cli(
                    "remember",
                    str(conversation),
                    "--root",
                    str(instance_root),
                    "--occurred-at",
                    f"2026-07-17T15:1{index}:00+08:00",
                    "--entrance",
                    "codex",
                    "--task",
                    "deployment-review",
                    "--digest",
                    digest,
                    "--sensitivity",
                    "local-only",
                    "--visible-context",
                    "deployment task",
                    "--context-gap",
                    "other projects unavailable",
                    "--format",
                    "json",
                )
                self.assertEqual(capture.returncode, 0, capture.stderr)
                receipts[name] = json.loads(capture.stdout)

            package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="How should Project Orion deployment be handled?",
                    task="deployment-review",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(
                [item.memory_id for item in package.items],
                [receipts["target"]["digest_id"]],
            )


if __name__ == "__main__":
    unittest.main()
