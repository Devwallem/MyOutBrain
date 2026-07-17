from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_memory_evolution import accept_new, propose, remember_evidence


class ObsidianKnowledgeViewTests(unittest.TestCase):
    def test_canonical_memory_generates_a_traceable_linked_obsidian_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            original = remember_evidence(
                temporary_root,
                instance_root,
                name="weekly-cadence",
                digest="Project Atlas review cadence is weekly.",
                task="initial-cadence",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "initial-cadence")["proposal_id"],
            )
            correction = remember_evidence(
                temporary_root,
                instance_root,
                name="monthly-cadence",
                digest="Project Atlas review cadence is monthly.",
                task="correct-cadence",
            )
            revision = propose(instance_root, "correct-cadence")
            reviewed = run_cli(
                "review-memory",
                str(revision["proposal_id"]),
                f"revise {memory_id} because: the newer evidence corrected it",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            obsidian_request = temporary_root / "obsidian-request.json"

            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--open",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_OBSIDIAN_REQUEST": str(obsidian_request)
                },
            )

            self.assertEqual(built.returncode, 0, built.stderr)
            result = json.loads(built.stdout)
            self.assertEqual(result["view_count"], 1)
            self.assertIsNone(result["obsidian_warning"])
            view_path = instance_root / result["view_paths"][0]
            index_path = instance_root / result["index_path"]
            view = view_path.read_text(encoding="utf-8")
            index = index_path.read_text(encoding="utf-8")
            self.assertIn(f"memory_id: {memory_id}", view)
            self.assertIn("confirmation: confirmed", view)
            self.assertIn("current_version: 2", view)
            self.assertIn("Project Atlas review cadence is monthly.", view)
            self.assertIn(original["source_id"], view)
            self.assertIn(correction["source_id"], view)
            self.assertIn("the newer evidence corrected it", view)
            self.assertNotIn("Evidence captured for weekly-cadence", view)
            self.assertIn(f"[[{view_path.stem}]]", index)
            request = json.loads(obsidian_request.read_text(encoding="utf-8"))
            self.assertEqual(
                request["command"][-1],
                "path=Knowledge Views/Index.md",
            )
            before_rebuild = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(before_rebuild.returncode, 0, before_rebuild.stderr)
            shutil.rmtree(view_path.parent)

            rebuilt = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            after_rebuild = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            self.assertTrue(view_path.is_file())
            self.assertEqual(after_rebuild.returncode, 0, after_rebuild.stderr)
            self.assertEqual(after_rebuild.stdout, before_rebuild.stdout)

    def test_missing_obsidian_is_isolated_from_view_and_canonical_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            remember_evidence(
                temporary_root,
                instance_root,
                name="offline-view",
                digest="Offline knowledge views remain rebuildable.",
                task="offline-view",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "offline-view")["proposal_id"],
            )

            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--open",
                "--format",
                "json",
                environment={"PATH": ""},
            )
            audit = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(built.returncode, 0, built.stderr)
            result = json.loads(built.stdout)
            self.assertIn("Obsidian CLI not found", result["obsidian_warning"])
            self.assertTrue((instance_root / result["index_path"]).is_file())
            self.assertEqual(audit.returncode, 0, audit.stderr)
            self.assertEqual(json.loads(audit.stdout)["memory_id"], memory_id)


if __name__ == "__main__":
    unittest.main()
