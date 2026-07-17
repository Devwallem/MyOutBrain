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


if __name__ == "__main__":
    unittest.main()
