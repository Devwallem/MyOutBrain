from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_memory_evolution import accept_new, propose, remember_evidence
from tests.test_cli_promote import create_derived_insight


class ObsidianKnowledgeViewTests(unittest.TestCase):
    def test_manifest_path_cannot_escape_the_disposable_view_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            remember_evidence(
                temporary_root,
                instance_root,
                name="safe-view",
                digest="Canonical memory must survive a tampered view manifest.",
                task="safe-view",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "safe-view")["proposal_id"],
            )
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            manifest_path = (
                instance_root / "runtime" / "knowledge-views" / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["views"][0]["path"] = (
                "vault/Knowledge Views/../../store/memory.sqlite3"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            rejected = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            audit = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("escapes", rejected.stderr)
            self.assertEqual(audit.returncode, 0, audit.stderr)
            self.assertEqual(json.loads(audit.stdout)["memory_id"], memory_id)

    def test_migrated_canonical_identity_accepts_controlled_view_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root, insight_id, _, _ = create_derived_insight(
                Path(temporary_directory)
            )
            migrated = run_cli(
                "migrate-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            result = json.loads(built.stdout)
            view_path = instance_root / result["view_paths"][0]
            generated = view_path.read_text(encoding="utf-8")
            view_path.write_text(
                generated.replace(
                    "## Current understanding\n\n",
                    "## Current understanding\n\nHuman clarification: ",
                    1,
                ),
                encoding="utf-8",
            )

            synced = run_cli(
                "sync-view-edits",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(synced.returncode, 0, synced.stderr)
            sync_result = json.loads(synced.stdout)
            self.assertEqual(sync_result["edit_count"], 1)
            self.assertEqual(sync_result["edits"][0]["memory_id"], insight_id)

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

    def test_human_view_edit_returns_as_buffered_evidence_and_a_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            remember_evidence(
                temporary_root,
                instance_root,
                name="editable-cadence",
                digest="Project Atlas review cadence is weekly.",
                task="editable-cadence",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "editable-cadence")["proposal_id"],
            )
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            view_path = instance_root / json.loads(built.stdout)["view_paths"][0]
            generated = view_path.read_text(encoding="utf-8")
            view_path.write_text(
                generated.replace(
                    (
                        "## Current understanding\n\n"
                        "Project Atlas review cadence is weekly."
                    ),
                    (
                        "## Current understanding\n\n"
                        "Project Atlas review cadence is monthly."
                    ),
                ),
                encoding="utf-8",
            )

            blocked_rebuild = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(blocked_rebuild.returncode, 2)
            self.assertIn("sync-view-edits", blocked_rebuild.stderr)
            self.assertIn(
                "Project Atlas review cadence is monthly.",
                view_path.read_text(encoding="utf-8"),
            )

            synced = run_cli(
                "sync-view-edits",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            canonical = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            pending = run_cli(
                "review-memory",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(synced.returncode, 0, synced.stderr)
            result = json.loads(synced.stdout)
            self.assertEqual(result["edit_count"], 1)
            self.assertEqual(result["edits"][0]["memory_id"], memory_id)
            self.assertRegex(result["edits"][0]["digest_id"], r"^mem_[0-9a-f]{64}$")
            self.assertEqual(canonical.returncode, 0, canonical.stderr)
            audit = json.loads(canonical.stdout)
            self.assertEqual(
                audit["current_content"],
                "Project Atlas review cadence is weekly.",
            )
            self.assertEqual(audit["current_version"], 1)
            self.assertEqual(pending.returncode, 0, pending.stderr)
            proposals = json.loads(pending.stdout)["proposals"]
            self.assertEqual(len(proposals), 1)
            self.assertEqual(
                proposals[0]["proposed_understanding"],
                "Project Atlas review cadence is monthly.",
            )
            self.assertIn(memory_id, proposals[0]["related_canonical_memory_ids"])

            repeated = run_cli(
                "sync-view-edits",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["edit_count"], 0)

    def test_natural_audit_query_explains_sources_versions_and_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            weekly = remember_evidence(
                temporary_root,
                instance_root,
                name="weekly-audit",
                digest="Project Atlas review cadence is weekly.",
                task="weekly-audit",
            )
            weekly_id = accept_new(
                instance_root,
                propose(instance_root, "weekly-audit")["proposal_id"],
            )
            daily = remember_evidence(
                temporary_root,
                instance_root,
                name="daily-audit",
                digest="Project Atlas review cadence is daily.",
                task="daily-audit",
            )
            conflict = propose(instance_root, "daily-audit")
            preserved = run_cli(
                "review-memory",
                str(conflict["proposal_id"]),
                (
                    f"preserve conflict with {weekly_id} because: "
                    "the observations disagree"
                ),
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(preserved.returncode, 0, preserved.stderr)
            daily_id = json.loads(preserved.stdout)["canonical_memory_id"]

            audited = run_cli(
                "audit-memory",
                "How does Project Atlas review cadence work?",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(audited.returncode, 0, audited.stderr)
            result = json.loads(audited.stdout)
            self.assertEqual(result["query"], "How does Project Atlas review cadence work?")
            audits = {audit["memory_id"]: audit for audit in result["audits"]}
            self.assertEqual(set(audits), {weekly_id, daily_id})
            weekly_audit = audits[weekly_id]
            self.assertEqual(weekly_audit["confirmation_status"], "conflicted")
            self.assertEqual(weekly_audit["current_source_ids"], [weekly["source_id"]])
            self.assertEqual(weekly_audit["versions"][0]["status"], "current")
            self.assertEqual(
                weekly_audit["unresolved_conflicts"][0]["memory_id"],
                daily_id,
            )
            self.assertEqual(
                weekly_audit["unresolved_conflicts"][0]["source_ids"],
                [daily["source_id"]],
            )
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            view_paths = [
                instance_root / relative_path
                for relative_path in json.loads(built.stdout)["view_paths"]
            ]
            view_by_memory = {
                next(
                    line.removeprefix("memory_id: ")
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("memory_id: ")
                ): path
                for path in view_paths
            }
            weekly_view = view_by_memory[weekly_id].read_text(encoding="utf-8")
            self.assertIn("## Unresolved conflicts", weekly_view)
            self.assertIn("the observations disagree", weekly_view)
            self.assertIn(f"[[{view_by_memory[daily_id].stem}]]", weekly_view)


if __name__ == "__main__":
    unittest.main()
