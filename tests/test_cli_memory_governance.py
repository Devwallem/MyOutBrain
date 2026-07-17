from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_memory_evolution import accept_new, propose, remember_evidence


class MemoryGovernanceTests(unittest.TestCase):
    def test_forget_defaults_to_reversible_deactivation_with_an_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            evidence = remember_evidence(
                temporary_root,
                instance_root,
                name="reversible-memory",
                digest="Project Cedar review cadence is weekly.",
                task="reversible-memory",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "reversible-memory")["proposal_id"],
            )

            forgotten = run_cli(
                "forget-memory",
                memory_id,
                "forget this",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            hidden = run_cli(
                "recall",
                "Project Cedar review cadence",
                "--root",
                str(instance_root),
                "--task",
                "cedar-audit",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--format",
                "json",
            )
            inactive_audit = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(forgotten.returncode, 0, forgotten.stderr)
            forgotten_result = json.loads(forgotten.stdout)
            self.assertEqual(forgotten_result["action"], "deactivated")
            self.assertEqual(forgotten_result["memory_id"], memory_id)
            self.assertEqual(hidden.returncode, 0, hidden.stderr)
            self.assertEqual(json.loads(hidden.stdout)["items"], [])
            self.assertEqual(inactive_audit.returncode, 0, inactive_audit.stderr)
            audit = json.loads(inactive_audit.stdout)
            self.assertEqual(audit["state"], "inactive")
            self.assertEqual(audit["current_source_ids"], [evidence["source_id"]])
            self.assertEqual(audit["current_version"], 1)
            self.assertEqual(audit["lifecycle_events"][-1]["action"], "deactivated")

            restored = run_cli(
                "forget-memory",
                memory_id,
                "restore this",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            visible = run_cli(
                "recall",
                "Project Cedar review cadence",
                "--root",
                str(instance_root),
                "--task",
                "cedar-audit",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--format",
                "json",
            )

            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(json.loads(restored.stdout)["action"], "reactivated")
            visible_items = json.loads(visible.stdout)["items"]
            self.assertEqual([item["memory_id"] for item in visible_items], [memory_id])

    def test_permanent_deletion_requires_an_exact_bounded_impact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            evidence = remember_evidence(
                temporary_root,
                instance_root,
                name="deletion-preview",
                digest="Project Birch review cadence is weekly.",
                task="deletion-preview",
            )
            proposal = propose(instance_root, "deletion-preview")
            memory_id = accept_new(instance_root, proposal["proposal_id"])

            previewed = run_cli(
                "delete-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["disposition"], "preview")
            self.assertEqual(preview["memory_id"], memory_id)
            self.assertEqual(preview["source_ids"], [evidence["source_id"]])
            self.assertEqual(preview["canonical_memory_count"], 1)
            self.assertEqual(preview["scope"], "one-canonical-memory")
            self.assertEqual(
                preview["proposal_ids_to_delete"],
                [proposal["proposal_id"]],
            )
            self.assertEqual(len(preview["review_ids_to_delete"]), 1)
            self.assertRegex(
                preview["confirmation_token"],
                r"^delete_[0-9a-f]{64}$",
            )

            rejected = run_cli(
                "delete-memory",
                memory_id,
                "--confirm",
                "delete_wrong",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            still_visible = run_cli(
                "recall",
                "Project Birch review cadence",
                "--root",
                str(instance_root),
                "--task",
                "deletion-check",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--format",
                "json",
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertIn("confirmation", rejected.stderr.casefold())
            self.assertEqual(still_visible.returncode, 0, still_visible.stderr)
            self.assertEqual(
                [item["memory_id"] for item in json.loads(still_visible.stdout)["items"]],
                [memory_id],
            )

    def test_confirmed_permanent_deletion_cascades_and_cannot_be_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            conversation = temporary_root / "delete-me.txt"
            secret_text = "Project Elm private launch phrase is silver lantern."
            conversation.write_text(
                f"Private planning record. {secret_text} End of record.",
                encoding="utf-8",
            )
            remembered = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T12:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "delete-elm",
                "--digest",
                secret_text,
                "--sensitivity",
                "local-only",
                "--visible-context",
                "permanent deletion acceptance",
                "--context-gap",
                "earlier history unavailable",
                "--format",
                "json",
            )
            self.assertEqual(remembered.returncode, 0, remembered.stderr)
            receipt = json.loads(remembered.stdout)
            proposal = propose(instance_root, "delete-elm")
            memory_id = accept_new(instance_root, proposal["proposal_id"])
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            old_view = instance_root / json.loads(built.stdout)["view_paths"][0]
            previewed = run_cli(
                "delete-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            token = json.loads(previewed.stdout)["confirmation_token"]

            deleted = run_cli(
                "delete-memory",
                memory_id,
                "--confirm",
                token,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall",
                "silver lantern",
                "--root",
                str(instance_root),
                "--task",
                "post-delete",
                "--access",
                "local-trusted",
                "--format",
                "json",
            )
            rebuilt = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            reimported = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-18T12:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "reimport-elm",
                "--digest",
                secret_text,
                "--sensitivity",
                "local-only",
                "--visible-context",
                "attempted reimport",
                "--context-gap",
                "earlier history unavailable",
                "--format",
                "json",
            )

            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            deletion = json.loads(deleted.stdout)
            self.assertEqual(deletion["disposition"], "deleted")
            self.assertEqual(deletion["memory_id"], memory_id)
            self.assertEqual(deletion["removed_source_ids"], [receipt["source_id"]])
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(json.loads(recalled.stdout)["items"], [])
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            self.assertEqual(json.loads(rebuilt.stdout)["view_count"], 0)
            self.assertFalse(old_view.exists())
            self.assertNotEqual(reimported.returncode, 0)
            self.assertIn("permanently deleted", reimported.stderr)
            object_files = [
                path
                for path in (instance_root / "store" / "objects").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(object_files, [])
            journal = (
                instance_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            for removed_identity in (
                memory_id,
                receipt["source_id"],
                receipt["experience_id"],
                receipt["digest_id"],
                proposal["proposal_id"],
            ):
                self.assertNotIn(removed_identity, journal)
            self.assertNotIn(secret_text, journal)
            self.assertIn("memory.permanently-deleted", journal)
            self.assertIn(
                "external-backups-must-be-rotated-or-deleted-by-owner",
                deletion["existing_backup_clearance"],
            )

    def test_permanent_deletion_retains_a_source_shared_by_another_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            first = remember_evidence(
                temporary_root,
                instance_root,
                name="shared-first",
                digest="Project Fir uses a weekly review.",
                task="shared-first",
            )
            first_memory_id = accept_new(
                instance_root,
                propose(instance_root, "shared-first")["proposal_id"],
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="shared-second",
                digest="The sourdough starter is fed at dawn.",
                task="shared-second",
            )
            second_proposal = propose(instance_root, "shared-second")
            second_memory_id = accept_new(
                instance_root,
                second_proposal["proposal_id"],
            )
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO canonical_memory_sources (memory_id, source_id)
                    VALUES (?, ?)
                    """,
                    (second_memory_id, first["source_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_memory_version_sources
                        (memory_id, version, source_id)
                    VALUES (?, 1, ?)
                    """,
                    (second_memory_id, first["source_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO integration_proposal_related
                        (proposal_id, memory_id)
                    VALUES (?, ?)
                    """,
                    (second_proposal["proposal_id"], first_memory_id),
                )
                object_reference = connection.execute(
                    "SELECT object_reference FROM source_objects WHERE source_id = ?",
                    (first["source_id"],),
                ).fetchone()[0]
                connection.commit()
            shared_object = instance_root / "store" / "objects" / object_reference

            previewed = run_cli(
                "delete-memory",
                first_memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            preview = json.loads(previewed.stdout)
            deleted = run_cli(
                "delete-memory",
                first_memory_id,
                "--confirm",
                preview["confirmation_token"],
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall",
                "sourdough starter dawn",
                "--root",
                str(instance_root),
                "--task",
                "shared-retention",
                "--access",
                "local-trusted",
                "--memory-id",
                second_memory_id,
                "--format",
                "json",
            )
            history = run_cli(
                "review-memory",
                "--history",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            result = json.loads(deleted.stdout)
            self.assertEqual(result["removed_source_ids"], [])
            self.assertEqual(
                result["retained_shared_source_ids"],
                [first["source_id"]],
            )
            self.assertTrue(shared_object.is_file())
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(
                [item["memory_id"] for item in json.loads(recalled.stdout)["items"]],
                [second_memory_id],
            )
            self.assertEqual(history.returncode, 0, history.stderr)
            self.assertIn(
                second_memory_id,
                {
                    review["canonical_memory_id"]
                    for review in json.loads(history.stdout)["reviews"]
                },
            )

    def test_interrupted_permanent_deletion_resumes_file_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            receipt = remember_evidence(
                temporary_root,
                instance_root,
                name="resumable-delete",
                digest="Resumable deletion phrase is amber compass.",
                task="resumable-delete",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "resumable-delete")["proposal_id"],
            )
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            view_path = instance_root / json.loads(built.stdout)["view_paths"][0]
            index_path = (
                instance_root
                / "runtime"
                / "indexes"
                / "fulltext"
                / "stale.json"
            )
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text("stale", encoding="utf-8")
            object_path = next(
                path
                for path in (instance_root / "store" / "objects").rglob("*")
                if path.is_file()
            )
            previewed = run_cli(
                "delete-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            token = json.loads(previewed.stdout)["confirmation_token"]

            interrupted = run_cli(
                "delete-memory",
                memory_id,
                "--confirm",
                token,
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": (
                        "permanent-deletion-before-cleanup"
                    )
                },
            )

            cleanup_manifest = (
                instance_root / "store" / "permanent-deletion-cleanup.json"
            )
            self.assertEqual(interrupted.returncode, 86)
            self.assertTrue(cleanup_manifest.is_file())
            self.assertTrue(object_path.is_file())
            self.assertTrue(view_path.is_file())
            self.assertTrue(index_path.is_file())

            recovered = run_cli(
                "recall",
                "amber compass",
                "--root",
                str(instance_root),
                "--task",
                "recover-delete",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--source-id",
                str(receipt["source_id"]),
                "--format",
                "json",
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(recovered.stdout)["items"], [])
            self.assertFalse(cleanup_manifest.exists())
            self.assertFalse(object_path.exists())
            self.assertFalse(view_path.exists())
            self.assertFalse(index_path.exists())

    def test_storage_report_separates_durable_and_rebuildable_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            buffered = remember_evidence(
                temporary_root,
                instance_root,
                name="storage-buffered",
                digest="Unreviewed storage note remains buffered.",
                task="storage-buffered",
            )
            canonical = remember_evidence(
                temporary_root,
                instance_root,
                name="storage-canonical",
                digest="Approved storage note becomes canonical.",
                task="storage-canonical",
            )
            accept_new(
                instance_root,
                propose(instance_root, "storage-canonical")["proposal_id"],
            )
            rebuildable_file = (
                instance_root
                / "runtime"
                / "indexes"
                / "fulltext"
                / "acceptance-projection.json"
            )
            rebuildable_file.parent.mkdir(parents=True, exist_ok=True)
            rebuildable_file.write_text('{"rebuildable": true}\n', encoding="utf-8")
            object_paths_before = sorted(
                path
                for path in (instance_root / "store" / "objects").rglob("*")
                if path.is_file()
            )

            reported = run_cli(
                "storage-report",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(reported.returncode, 0, reported.stderr)
            report = json.loads(reported.stdout)
            self.assertEqual(report["evidence"]["count"], 2)
            self.assertGreater(report["evidence"]["bytes"], 0)
            self.assertEqual(report["canonical"]["count"], 1)
            self.assertEqual(report["canonical"]["version_count"], 1)
            self.assertGreater(report["canonical"]["bytes"], 0)
            self.assertEqual(report["buffer"]["count"], 1)
            self.assertGreater(report["buffer"]["bytes"], 0)
            self.assertEqual(report["rebuildable_indexes"]["count"], 1)
            self.assertEqual(
                report["rebuildable_indexes"]["bytes"],
                rebuildable_file.stat().st_size,
            )
            self.assertEqual(
                report["destructive_maintenance"],
                "requires-explicit-approval",
            )
            self.assertEqual(
                sorted(
                    path
                    for path in (instance_root / "store" / "objects").rglob("*")
                    if path.is_file()
                ),
                object_paths_before,
            )
            self.assertIn(buffered["source_id"], report["evidence"]["source_ids"])
            self.assertIn(canonical["source_id"], report["evidence"]["source_ids"])


if __name__ == "__main__":
    unittest.main()
