from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import re
import sqlite3
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_reflect import initialize_cloud_source, reflection_response
from tests.test_cli_promote import create_derived_insight


class MigrateV1PermanentKnowledgeTests(unittest.TestCase):
    def test_approved_insight_becomes_idempotent_explainable_canonical_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, insight_id, _, source_id = create_derived_insight(
                Path(temporary_directory)
            )

            migrated = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
                "--format",
                "json",
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            summary = json.loads(migrated.stdout)
            self.assertEqual(summary["disposition"], "migrated")
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["source_schema_version"], 1)
            self.assertEqual(summary["source_count"], 1)
            self.assertEqual(summary["insight_count"], 1)
            self.assertEqual(summary["cognition_count"], 0)

            recalled = run_cli(
                "recall",
                "reflection reusable guidance",
                "--root",
                str(library_root),
                "--task",
                "reuse prior reflection",
                "--access",
                "local-trusted",
                "--memory-id",
                insight_id,
                "--format",
                "json",
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            recalled_data = json.loads(recalled.stdout)
            recalled_item = next(
                item
                for item in recalled_data["items"]
                if item["memory_id"] == insight_id
            )
            self.assertEqual(recalled_item["memory_state"], "canonical")
            self.assertEqual(recalled_item["source_ids"], [source_id])
            self.assertIn(
                "Reflection turns experience into reusable guidance.",
                recalled_item["content"],
            )

            explained = run_cli(
                "why-memory",
                insight_id,
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            explanation = json.loads(explained.stdout)
            self.assertEqual(explanation["confirmation_status"], "confirmed")
            self.assertEqual(explanation["current_version"], 1)
            self.assertEqual(explanation["current_source_ids"], [source_id])
            self.assertEqual(len(explanation["versions"]), 1)

            repeated = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_summary = json.loads(repeated.stdout)
            self.assertEqual(repeated_summary["disposition"], "already-complete")
            self.assertEqual(
                repeated_summary["source_fingerprint"],
                summary["source_fingerprint"],
            )

    def test_promoted_cognition_survives_without_legacy_markdown_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, insight_id, _, source_id = create_derived_insight(
                Path(temporary_directory)
            )
            promoted = run_cli(
                "promote",
                insight_id,
                "--title",
                "Reflection Is Reusable",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )
            self.assertEqual(promoted.returncode, 0, promoted.stderr)
            cognition_id = promoted.stdout.split("personal cognition ", 1)[1].split()[0]

            migrated = run_cli("migrate-v1", "--root", str(library_root))
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            repeated = run_cli("migrate-v1", "--root", str(library_root))
            self.assertEqual(repeated.returncode, 0, repeated.stderr)

            for note_path in (library_root / "vault").glob("*.md"):
                note_path.unlink()

            recalled = run_cli(
                "recall",
                "reflection reusable guidance",
                "--root",
                str(library_root),
                "--task",
                "reuse prior reflection",
                "--access",
                "local-trusted",
                "--memory-id",
                cognition_id,
                "--memory-id",
                insight_id,
                "--format",
                "json",
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            items = json.loads(recalled.stdout)["items"]
            item_ids = {item["memory_id"] for item in items}
            self.assertIn(cognition_id, item_ids)
            self.assertNotIn(insight_id, item_ids)
            cognition = next(
                item for item in items if item["memory_id"] == cognition_id
            )
            self.assertEqual(cognition["source_ids"], [source_id])

            explained = run_cli(
                "why-memory",
                cognition_id,
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertEqual(
                json.loads(explained.stdout)["current_source_ids"],
                [source_id],
            )
            archived_explanation = run_cli(
                "why-memory",
                insight_id,
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(
                archived_explanation.returncode,
                0,
                archived_explanation.stderr,
            )
            archived_data = json.loads(archived_explanation.stdout)
            self.assertEqual(archived_data["state"], "inactive")
            self.assertEqual(archived_data["current_source_ids"], [source_id])

            status = run_cli(
                "migration-status",
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            status_data = json.loads(status.stdout)
            self.assertEqual(status_data["status"], "complete")
            self.assertEqual(status_data["insight_count"], 1)
            self.assertEqual(status_data["cognition_count"], 1)
            self.assertGreater(status_data["event_count"], 0)

    def test_changed_v1_truth_after_completion_stops_without_reimporting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, insight_id, insight_path, _ = create_derived_insight(
                Path(temporary_directory)
            )
            first = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_fingerprint = json.loads(first.stdout)["source_fingerprint"]
            insight_path.write_text(
                insight_path.read_text(encoding="utf-8").replace(
                    "Reflection turns experience into reusable guidance.",
                    "Changed legacy understanding must not be silently imported.",
                ),
                encoding="utf-8",
            )

            repeated = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
            )

            self.assertEqual(repeated.returncode, 3, repeated.stderr)
            self.assertIn("changed after migration completed", repeated.stderr)
            status = run_cli(
                "migration-status",
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(
                json.loads(status.stdout)["source_fingerprint"],
                first_fingerprint,
            )
            explained = run_cli(
                "why-memory",
                insight_id,
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertIn(
                "Reflection turns experience into reusable guidance.",
                json.loads(explained.stdout)["current_content"],
            )

    def test_unreviewed_candidate_is_not_promoted_by_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflected = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(
                        source_id
                    )
                },
            )
            self.assertEqual(reflected.returncode, 0, reflected.stderr)
            candidate_match = re.search(r"cand_[0-9a-f]{64}", reflected.stdout)
            self.assertIsNotNone(candidate_match)
            candidate_id = candidate_match.group(0) if candidate_match else ""

            migrated = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
                "--format",
                "json",
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            summary = json.loads(migrated.stdout)
            self.assertEqual(summary["insight_count"], 0)
            self.assertEqual(summary["cognition_count"], 0)
            recalled = run_cli(
                "recall",
                "reusable insight",
                "--root",
                str(library_root),
                "--task",
                "candidate isolation",
                "--access",
                "local-trusted",
                "--memory-id",
                candidate_id,
                "--format",
                "json",
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(json.loads(recalled.stdout)["items"], [])

    def test_unknown_source_schema_stops_before_creating_a_new_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "brain"
            library_root.mkdir()
            (library_root / "myoutbrain.toml").write_text(
                "schema_version = 99\n",
                encoding="utf-8",
            )

            migrated = run_cli("migrate-v1", "--root", str(library_root))

            self.assertEqual(migrated.returncode, 3, migrated.stderr)
            self.assertIn("unsupported V1 migration source schema version 99", migrated.stderr)
            self.assertFalse((library_root / "store" / "memory.sqlite3").exists())

    def test_status_upgrades_the_previous_core_schema_before_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, _, _, _ = create_derived_insight(
                Path(temporary_directory)
            )
            database_path = library_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = OFF;
                    DROP TABLE legacy_knowledge_metadata;
                    DROP TABLE legacy_source_metadata;
                    DROP TABLE legacy_audit_events;
                    DROP TABLE legacy_migration_runs;
                    PRAGMA user_version = 4;
                    """
                )

            status = run_cli(
                "migration-status",
                "--root",
                str(library_root),
                "--format",
                "json",
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["status"], "not-started")

    def test_existing_identical_core_source_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root, _, _, source_id = create_derived_insight(temporary_root)
            digest = source_id.removeprefix("src_")
            database_path = library_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO source_objects
                        (source_id, content_hash, object_reference, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        f"sha256:{digest}",
                        f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
                        "2026-07-17T12:00:00+08:00",
                    ),
                )
                connection.commit()

            migrated = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
                "--format",
                "json",
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(json.loads(migrated.stdout)["source_count"], 1)

    def test_interrupted_migration_preserves_v1_truth_and_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, _, _, _ = create_derived_insight(
                Path(temporary_directory)
            )
            permanent_roots = (
                library_root / "vault",
                library_root / "store" / "records",
                library_root / "store" / "objects",
                library_root / "store" / "journal",
            )
            before = {
                path.relative_to(library_root).as_posix(): path.read_bytes()
                for root in permanent_roots
                for path in root.rglob("*")
                if path.is_file()
            }

            interrupted = run_cli(
                "migrate-v1",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "legacy-migration-after-database"
                },
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)

            status = run_cli(
                "migration-status",
                "--root",
                str(library_root),
                "--format",
                "json",
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["status"], "not-started")
            after = {
                path.relative_to(library_root).as_posix(): path.read_bytes()
                for root in permanent_roots
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
