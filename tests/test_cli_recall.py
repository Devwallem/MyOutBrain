from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tests.cli_support import run_cli


class RecallMemoryFromCliTests(unittest.TestCase):
    def test_creator_can_request_a_minimal_task_scoped_evidence_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text(
                "The launch codename was selected during the current task.",
                encoding="utf-8",
            )
            capture = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T14:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "launch-plan",
                "--digest",
                "The launch codename is Cobalt Finch.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "launch planning task",
                "--context-gap",
                "earlier naming discussions unavailable",
                "--format",
                "json",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            receipt = json.loads(capture.stdout)

            result = run_cli(
                "recall",
                "Cobalt Finch",
                "--root",
                str(instance_root),
                "--task",
                "launch-plan",
                "--access",
                "task-scoped",
                "--purpose",
                "substantive",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            package = json.loads(result.stdout)
            self.assertTrue(package["retrieval_performed"])
            self.assertTrue(package["common_knowledge_queried"])
            self.assertEqual(package["answerability"], "insufficient")
            self.assertEqual(len(package["items"]), 1)
            item = package["items"][0]
            self.assertEqual(item["memory_id"], receipt["digest_id"])
            self.assertEqual(item["memory_state"], "buffered")
            self.assertEqual(item["match"], "full-text")
            self.assertEqual(item["source_ids"], [receipt["source_id"]])
            serialized = json.dumps(package)
            self.assertNotIn("memory.sqlite3", serialized)
            self.assertNotIn("store/objects", serialized)


if __name__ == "__main__":
    unittest.main()
