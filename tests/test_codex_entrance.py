from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from myoutbrain.codex_entrance import (
    CodexEntrance,
    CodexTaskRequest,
    CodexVisibleExperience,
)
from myoutbrain.memory_gateway import (
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)
from tests.cli_support import run_cli


class CodexEntranceTests(unittest.TestCase):
    def test_before_task_uses_task_scope_and_skips_casual_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            entrance = CodexEntrance(instance_root)
            entrance.after_task(
                CodexVisibleExperience(
                    visible_text="Project Aster uses signed release manifests.",
                    occurred_at="2026-07-18T10:00:00+08:00",
                    task_pointer="aster-release",
                    digest="Aster releases require signed manifests.",
                    sensitivity="local-only",
                    visible_context="current Aster release task",
                    context_gaps=("earlier Aster discussions are unavailable",),
                )
            )
            entrance.after_task(
                CodexVisibleExperience(
                    visible_text="Project Birch uses a separate review flow.",
                    occurred_at="2026-07-18T10:01:00+08:00",
                    task_pointer="birch-release",
                    digest="Birch uses a separate review flow.",
                    sensitivity="local-only",
                    visible_context="current Birch release task",
                    context_gaps=("other project history is unavailable",),
                )
            )

            substantive = entrance.before_task(
                CodexTaskRequest(
                    question="What does the Aster release require?",
                    task_pointer="aster-release",
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )
            casual = entrance.before_task(
                CodexTaskRequest(
                    question="hello",
                    task_pointer="aster-release",
                    purpose=QueryPurpose.CASUAL,
                )
            )

            self.assertEqual(substantive.task_pointer, "aster-release")
            self.assertEqual(len(substantive.evidence_package.items), 1)
            self.assertIn(
                "signed manifests",
                substantive.evidence_package.items[0].content,
            )
            self.assertFalse(casual.evidence_package.retrieval_performed)
            self.assertEqual(casual.evidence_package.items, ())

    def test_after_task_submits_visible_text_and_explicit_blind_spots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )

            receipt = CodexEntrance(instance_root).after_task(
                CodexVisibleExperience(
                    visible_text="Only the current task confirmed the rollback owner.",
                    occurred_at="2026-07-18T10:30:00+08:00",
                    task_pointer="rollback-plan",
                    digest="The rollback plan has an explicitly confirmed owner.",
                    sensitivity="local-only",
                    visible_context="current rollback task messages",
                    context_gaps=("messages before this task are unavailable",),
                )
            )
            recalled = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="rollback owner",
                    task="rollback-plan",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(recalled.items[0].memory_id, receipt.digest_id)
            self.assertEqual(recalled.items[0].entrance, "codex")
            self.assertEqual(recalled.items[0].task, "rollback-plan")


if __name__ == "__main__":
    unittest.main()
