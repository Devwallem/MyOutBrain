from __future__ import annotations

from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import unittest
from zipfile import ZipFile

from tests.cli_support import run_cli, start_cli, wait_until_lock_is_held
from tests.test_cli_v2_memory_lifecycle import create_source_backed_memory


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected object, received {value!r}")
    return cast(dict[str, object], value)


def _inject_source_object(
    instance_root: Path,
    body: bytes,
    *,
    memory_id: str | None = None,
) -> tuple[str, Path]:
    digest = hashlib.sha256(body).hexdigest()
    source_id = f"src_{digest}"
    object_reference = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    object_path = instance_root / "store" / "objects" / object_reference
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(body)
    database_path = instance_root / "store" / "memory.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO source_objects
                (source_id, content_hash, object_reference, created_at)
            VALUES (?, ?, ?, '2026-07-19T00:00:00+00:00')
            """,
            (source_id, f"sha256:{digest}", object_reference),
        )
        if memory_id is not None:
            version = connection.execute(
                "SELECT current_version FROM canonical_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if version is None:
                raise AssertionError(f"memory disappeared: {memory_id}")
            connection.execute(
                "INSERT INTO canonical_memory_sources (memory_id, source_id) VALUES (?, ?)",
                (memory_id, source_id),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_version_sources
                    (memory_id, version, source_id) VALUES (?, ?, ?)
                """,
                (memory_id, version[0], source_id),
            )
        connection.commit()
    return source_id, object_path


class V2BackupDoctorGarbageCollectionTests(unittest.TestCase):
    def test_gateway_exposes_backup_doctor_restore_and_gc_through_one_domain_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "gateway-instance"
            restored_root = temporary_root / "gateway-restored"
            snapshot = temporary_root / "gateway-backup.zip"
            initialized = run_cli("init", "--root", str(instance_root))

            doctor = self._gateway(
                temporary_root,
                instance_root,
                operation="instance.doctor",
                parameters={"repair": False},
            )
            created = self._gateway(
                temporary_root,
                instance_root,
                operation="backup.create",
                parameters={"output_path": str(snapshot)},
                write={"expected_version": 0, "idempotency_key": "gateway-backup-v1"},
            )
            verified = self._gateway(
                temporary_root,
                instance_root,
                operation="backup.verify",
                parameters={"archive_path": str(snapshot)},
            )
            restored = self._gateway(
                temporary_root,
                instance_root,
                operation="backup.restore",
                parameters={
                    "archive_path": str(snapshot),
                    "destination_path": str(restored_root),
                },
                write={"expected_version": 0, "idempotency_key": "gateway-restore-v1"},
            )
            gc_plan = self._gateway(
                temporary_root,
                instance_root,
                operation="maintenance.gc_plan",
                parameters={},
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(_object(doctor["result"])["mode"], "read-only")
            self.assertEqual(_object(created["result"])["kind"], "cold-full-instance-zip")
            self.assertTrue(_object(verified["result"])["valid"])
            self.assertTrue(_object(restored["result"])["switch_allowed"])
            self.assertEqual(_object(gc_plan["result"])["maintenance_version"], 1)
            self.assertEqual(created["protocol_version"], {"major": 2, "minor": 3})

    def test_cold_backup_restores_the_whole_instance_to_a_verified_new_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "active-instance"
            restored_root = temporary_root / "restored-instance"
            snapshot = temporary_root / "whole-instance.zip"
            initialized = run_cli("init", "--root", str(instance_root))
            approved = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="backup-rule",
                name="Backup switch rule",
                body="Switch only after the restored instance passes Doctor.",
                scope="private instance recovery",
            )
            memory_id = cast(str, _object(approved["memory"])["memory_id"])
            preserved_file = instance_root / "creator-owned.txt"
            preserved_file.write_text("preserve the complete directory\n", encoding="utf-8")

            created = run_cli(
                "backup-create",
                str(snapshot),
                "--expected-version",
                "0",
                "--idempotency-key",
                "cold-backup-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            verified = run_cli(
                "backup-verify",
                str(snapshot),
                "--format",
                "json",
            )
            restored = run_cli(
                "backup-restore",
                str(snapshot),
                str(restored_root),
                "--expected-version",
                "0",
                "--idempotency-key",
                "restore-cold-backup-v1",
                "--format",
                "json",
            )
            doctor = run_cli(
                "doctor",
                "--root",
                str(restored_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "When may the restored instance replace the active one?",
                "--task",
                "backup-recovery",
                "--entrance",
                "codex",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(restored_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            created_result = _object(json.loads(created.stdout))
            self.assertEqual(created_result["kind"], "cold-full-instance-zip")
            self.assertFalse(created_result["incremental"])
            self.assertFalse(created_result["encrypted"])
            with ZipFile(snapshot) as archive:
                self.assertIn("creator-owned.txt", archive.namelist())
                self.assertIn("store/memory.sqlite3", archive.namelist())
            self.assertEqual(
                restored_root.joinpath("creator-owned.txt").read_text(encoding="utf-8"),
                "preserve the complete directory\n",
            )
            restored_result = _object(json.loads(restored.stdout))
            self.assertTrue(restored_result["switch_allowed"])
            self.assertEqual(restored_result["doctor_mode"], "read-only")
            self.assertEqual(_object(json.loads(doctor.stdout))["overall"], "ok")
            recalled_memories = cast(list[dict[str, object]], json.loads(recalled.stdout)["memories"])
            self.assertIn(memory_id, {item["memory_id"] for item in recalled_memories})

    def test_failed_cold_backup_removes_the_partial_archive_and_reopens_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "active-instance"
            snapshot = temporary_root / "failed.zip"
            source = temporary_root / "after-failure.md"
            source.write_text("Writes resume after failed compression.\n", encoding="utf-8")
            initialized = run_cli("init", "--root", str(instance_root))

            failed = run_cli(
                "backup-create",
                str(snapshot),
                "--expected-version",
                "0",
                "--idempotency-key",
                "failed-cold-backup",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={"MYOUTBRAIN_FAULT_INJECTION": "backup-during-compression"},
            )
            captured = run_cli(
                "capture",
                str(source),
                "--sensitivity",
                "local-only",
                "--root",
                str(instance_root),
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(failed.returncode, 7, failed.stderr)
            self.assertFalse(snapshot.exists())
            self.assertEqual(captured.returncode, 0, captured.stderr)

    def test_cold_backup_maintenance_lock_rejects_a_competing_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "active-instance"
            snapshot = temporary_root / "locked.zip"
            ready_file = temporary_root / "maintenance-ready"
            source = temporary_root / "competing.md"
            source.write_text("Competing write.\n", encoding="utf-8")
            initialized = run_cli("init", "--root", str(instance_root))
            backup = start_cli(
                "backup-create",
                str(snapshot),
                "--expected-version",
                "0",
                "--idempotency-key",
                "locked-cold-backup",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "hold-writer-lock",
                    "MYOUTBRAIN_HOLD_SECONDS": "1",
                    "MYOUTBRAIN_LOCK_READY_FILE": str(ready_file),
                },
            )
            wait_until_lock_is_held(ready_file, backup)

            competing = run_cli(
                "capture",
                str(source),
                "--sensitivity",
                "local-only",
                "--root",
                str(instance_root),
            )
            _, backup_stderr = backup.communicate(timeout=5)

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(competing.returncode, 4, competing.stderr)
            self.assertEqual(backup.returncode, 0, backup_stderr)

    def test_explicit_doctor_repair_rebuilds_only_rebuildable_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "instance"
            initialized = run_cli("init", "--root", str(instance_root))
            approved = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="repairable",
                name="Projection repair rule",
                body="Doctor rebuilds projections from canonical records.",
                scope="instance diagnosis",
            )
            memory_id = cast(str, _object(approved["memory"])["memory_id"])
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("DELETE FROM canonical_memory_fts")
                connection.commit()

            diagnosed = run_cli(
                "doctor", "--root", str(instance_root), "--format", "json"
            )
            repaired = run_cli(
                "doctor",
                "--repair",
                "--expected-version",
                "0",
                "--idempotency-key",
                "repair-projections-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            after = run_cli(
                "doctor", "--root", str(instance_root), "--format", "json"
            )
            recalled = run_cli(
                "recall-memory",
                "What does Doctor rebuild from canonical records?",
                "--task",
                "doctor-repair",
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

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(diagnosed.returncode, 0, diagnosed.stderr)
            self.assertEqual(_object(json.loads(diagnosed.stdout))["overall"], "degraded")
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            repair_result = _object(json.loads(repaired.stdout))
            self.assertEqual(
                set(cast(list[str], repair_result["rebuilt"])),
                {"full-text-search", "evidence-relationship-graph", "tree-summary"},
            )
            self.assertEqual(repair_result["maintenance_version"], 1)
            self.assertEqual(_object(json.loads(after.stdout))["overall"], "ok")
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            memories = cast(list[dict[str, object]], json.loads(recalled.stdout)["memories"])
            self.assertIn(memory_id, {item["memory_id"] for item in memories})

            evidence_graph = instance_root / "runtime" / "indexes" / "evidence-graph.json"
            evidence_graph.unlink()
            (instance_root / "runtime" / "indexes" / "tree-summary.json").unlink()
            missing_runtime_projection = run_cli(
                "doctor", "--root", str(instance_root), "--format", "json"
            )
            self.assertEqual(
                missing_runtime_projection.returncode,
                0,
                missing_runtime_projection.stderr,
            )
            missing_report = _object(json.loads(missing_runtime_projection.stdout))
            self.assertEqual(missing_report["overall"], "degraded")
            self.assertIn(
                "runtime-projection-missing",
                {
                    issue["code"]
                    for issue in cast(
                        list[dict[str, object]], missing_report["projection_issues"]
                    )
                },
            )

    def test_doctor_blocks_unpartitioned_capsules_and_source_less_dependency_terminals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "instance"
            initialized = run_cli("init", "--root", str(instance_root))
            dependent = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="dependent",
                name="Dependent rule",
                body="This rule depends on a separately evidenced terminal.",
                scope="Doctor relationship closure",
            )
            terminal = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="terminal",
                name="Terminal rule",
                body="A dependency terminal must keep a source receipt.",
                scope="Doctor relationship closure",
            )
            dependent_id = cast(str, _object(dependent["memory"])["memory_id"])
            terminal_id = cast(str, _object(terminal["memory"])["memory_id"])
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                capsule_id = cast(
                    str,
                    connection.execute(
                        "SELECT primary_capsule_id FROM knowledge_dictionary WHERE memory_id = ?",
                        (dependent_id,),
                    ).fetchone()[0],
                )
                connection.execute(
                    "DELETE FROM capsule_partitions WHERE capsule_id = ?", (capsule_id,)
                )
                connection.execute(
                    "DELETE FROM canonical_memory_version_evidence WHERE memory_id = ?",
                    (terminal_id,),
                )
                connection.execute(
                    "DELETE FROM canonical_memory_version_sources WHERE memory_id = ?",
                    (terminal_id,),
                )
                connection.execute(
                    "DELETE FROM canonical_memory_sources WHERE memory_id = ?",
                    (terminal_id,),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_memory_dependencies
                        (memory_id, version, depends_on_memory_id, depends_on_version,
                         relationship, created_at)
                    VALUES (?, 1, ?, 1, 'depends-on', '2026-07-19T00:00:00+00:00')
                    """,
                    (dependent_id, terminal_id),
                )
                connection.commit()

            diagnosed = run_cli(
                "doctor", "--root", str(instance_root), "--format", "json"
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(diagnosed.returncode, 0, diagnosed.stderr)
            report = _object(json.loads(diagnosed.stdout))
            self.assertEqual(report["overall"], "restricted-read-only")
            codes = {
                issue["code"]
                for issue in cast(list[dict[str, object]], report["canonical_issues"])
            }
            self.assertIn("capsule-partition-membership-missing", codes)
            self.assertIn("dependency-terminal-without-source", codes)

    def test_schema_11_upgrade_assigns_legacy_active_capsules_to_leaf_partitions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "instance"
            initialized = run_cli("init", "--root", str(instance_root))
            approved = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="legacy-capsule",
                name="Legacy capsule",
                body="Schema migration preserves a valid capsule tree.",
                scope="Doctor schema upgrade",
            )
            memory_id = cast(str, _object(approved["memory"])["memory_id"])
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                capsule_id = cast(
                    str,
                    connection.execute(
                        "SELECT primary_capsule_id FROM knowledge_dictionary WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()[0],
                )
                connection.execute(
                    "DELETE FROM capsule_partitions WHERE capsule_id = ?", (capsule_id,)
                )
                connection.execute("DROP TABLE maintenance_writes")
                connection.execute("DROP TABLE maintenance_state")
                connection.execute("PRAGMA user_version = 10")
                connection.commit()

            upgraded = run_cli("init", "--root", str(instance_root))
            diagnosed = run_cli(
                "doctor", "--root", str(instance_root), "--format", "json"
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertEqual(diagnosed.returncode, 0, diagnosed.stderr)
            self.assertEqual(_object(json.loads(diagnosed.stdout))["overall"], "ok")

    def test_canonical_object_corruption_enters_restricted_read_only_without_guessing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "instance"
            initialized = run_cli("init", "--root", str(instance_root))
            approved = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="protected-source",
                name="Canonical evidence rule",
                body="Canonical evidence is never guessed during repair.",
                scope="integrity recovery",
            )
            memory_id = cast(str, _object(approved["memory"])["memory_id"])
            captured = run_cli(
                "capture",
                str(temporary_root / "protected-source.md"),
                "--sensitivity",
                "local-only",
                "--root",
                str(instance_root),
            )
            object_path = next(
                path
                for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            object_path.write_bytes(b"corrupted canonical source")

            diagnosed = run_cli(
                "doctor", "--root", str(instance_root), "--format", "json"
            )
            repair = run_cli(
                "doctor",
                "--repair",
                "--expected-version",
                "0",
                "--idempotency-key",
                "do-not-guess-canonical-content",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            competing_source = temporary_root / "new-source.md"
            competing_source.write_text("A new write must be rejected.\n", encoding="utf-8")
            write = run_cli(
                "propose-source-memory",
                str(competing_source),
                "--name",
                "Rejected while restricted",
                "--body",
                "This write must not enter damaged canonical state.",
                "--scope",
                "integrity recovery",
                "--idempotency-key",
                "write-while-restricted",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "What is never guessed during repair?",
                "--task",
                "restricted-read",
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

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertEqual(diagnosed.returncode, 0, diagnosed.stderr)
            report = _object(json.loads(diagnosed.stdout))
            self.assertEqual(report["overall"], "restricted-read-only")
            self.assertFalse(report["write_allowed"])
            self.assertIn(
                "source-object-hash-mismatched",
                {issue["code"] for issue in cast(list[dict[str, object]], report["canonical_issues"])},
            )
            self.assertEqual(repair.returncode, 0, repair.stderr)
            self.assertTrue(_object(json.loads(repair.stdout))["repair_blocked"])
            self.assertEqual(object_path.read_bytes(), b"corrupted canonical source")
            self.assertEqual(write.returncode, 7, write.stderr)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            memories = cast(list[dict[str, object]], json.loads(recalled.stdout)["memories"])
            self.assertIn(memory_id, {item["memory_id"] for item in memories})

    def test_gc_preview_protects_every_lifecycle_and_apply_deletes_only_true_orphans(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "instance"
            initialized = run_cli("init", "--root", str(instance_root))
            memory_ids: dict[str, str] = {}
            bodies = {
                "current": "Current astronomy notes retain the original telescope receipt.",
                "historical": "Historic bread recipes retain the flour ledger.",
                "superseded": "The retired train timetable retains its printed schedule.",
                "inactive": "Inactive garden planning retains the seed catalogue.",
                "replacement": "The new rail calendar uses a separately approved source.",
            }
            for state, body in bodies.items():
                approved = create_source_backed_memory(
                    temporary_root,
                    instance_root,
                    stem=f"gc-{state}",
                    name=f"GC {state} evidence",
                    body=body,
                    scope=f"garbage collection {state} history",
                )
                memory_ids[state] = cast(str, _object(approved["memory"])["memory_id"])
            historical = run_cli(
                "historicize-memory",
                memory_ids["historical"],
                "--reason",
                "Keep this accepted historical understanding.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "gc-historical",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            superseded = run_cli(
                "supersede-memory",
                memory_ids["superseded"],
                "--replacement-memory-id",
                memory_ids["replacement"],
                "--replacement-version",
                "1",
                "--reason",
                "Keep the superseded version and its evidence.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "gc-superseded",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            inactive = run_cli(
                "deactivate-memory",
                memory_ids["inactive"],
                "--reason",
                "Keep inactive history recoverable.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "gc-inactive",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            protected_source_ids: set[str] = set()
            for state in ("current", "historical", "superseded", "inactive"):
                source_id, _ = _inject_source_object(
                    instance_root,
                    f"Protected {state} lifecycle evidence.\n".encode(),
                    memory_id=memory_ids[state],
                )
                protected_source_ids.add(source_id)
            orphan_body = b"Truly orphaned source object.\n"
            orphan_source_id, orphan_path = _inject_source_object(
                instance_root, orphan_body
            )
            manifest_body = b"Object retained only by a source record manifest.\n"
            manifest_digest = hashlib.sha256(manifest_body).hexdigest()
            manifest_reference = (
                f"sha256/{manifest_digest[:2]}/{manifest_digest[2:4]}/{manifest_digest}"
            )
            manifest_object = instance_root / "store" / "objects" / manifest_reference
            manifest_object.parent.mkdir(parents=True, exist_ok=True)
            manifest_object.write_bytes(manifest_body)
            manifest_source_id = "5b948f7d-6735-4e16-9370-3c9dbcde904f"
            manifest_record = instance_root / "store" / "records" / f"{manifest_source_id}.json"
            manifest_record.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "source",
                        "id": manifest_source_id,
                        "state": "active",
                        "content_hash": f"sha256:{manifest_digest}",
                        "object": manifest_reference,
                    }
                ),
                encoding="utf-8",
            )

            planned = run_cli(
                "gc-plan", "--root", str(instance_root), "--format", "json"
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(historical.returncode, 0, historical.stderr)
            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            self.assertEqual(inactive.returncode, 0, inactive.stderr)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = _object(json.loads(planned.stdout))
            candidates = cast(list[dict[str, object]], plan["candidates"])
            self.assertEqual({item["source_id"] for item in candidates}, {orphan_source_id})
            self.assertTrue(protected_source_ids.isdisjoint({item["source_id"] for item in candidates}))
            self.assertNotIn(manifest_reference, {item["object_reference"] for item in candidates})
            self.assertEqual(candidates[0]["size_bytes"], len(orphan_body))
            self.assertIn("last_reference", candidates[0])
            self.assertIn("deletion_impact", candidates[0])

            wrong = run_cli(
                "gc-apply",
                cast(str, plan["plan_id"]),
                "--confirmation",
                "delete something else",
                "--expected-version",
                "0",
                "--idempotency-key",
                "gc-wrong-confirmation",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            remained_after_wrong = orphan_path.is_file()
            applied = run_cli(
                "gc-apply",
                cast(str, plan["plan_id"]),
                "--confirmation",
                cast(str, plan["required_confirmation"]),
                "--expected-version",
                "0",
                "--idempotency-key",
                "gc-delete-orphan-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            after = run_cli(
                "gc-plan", "--root", str(instance_root), "--format", "json"
            )
            stale_source = temporary_root / "stale-source.txt"
            stale_source.write_bytes(orphan_body)
            restore_attempt = run_cli(
                "remember",
                str(stale_source),
                "--occurred-at",
                "2026-07-19T12:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "gc-restore-attempt",
                "--digest",
                "Do not silently restore garbage-collected evidence.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "gc tombstone acceptance",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(wrong.returncode, 2, wrong.stderr)
            self.assertTrue(remained_after_wrong)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            apply_result = _object(json.loads(applied.stdout))
            self.assertEqual(apply_result["deleted_source_ids"], [orphan_source_id])
            self.assertEqual(apply_result["maintenance_version"], 1)
            self.assertFalse(orphan_path.exists())
            self.assertEqual(_object(json.loads(after.stdout))["candidates"], [])
            self.assertEqual(restore_attempt.returncode, 2, restore_attempt.stderr)

    def _gateway(
        self,
        temporary_root: Path,
        instance_root: Path,
        *,
        operation: str,
        parameters: dict[str, object],
        write: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "protocol": {
                "minimum": {"major": 2, "minor": 0},
                "maximum": {"major": 2, "minor": 3},
            },
            "client": {
                "name": "codex",
                "capabilities": [
                    "backup_create.v1",
                    "backup_verify.v1",
                    "backup_restore.v1",
                    "doctor_read.v1",
                    "doctor_repair.v1",
                    "orphan_gc.v1",
                ],
            },
            "operation": operation,
            "parameters": parameters,
        }
        if write is not None:
            request["write"] = write
        request_path = temporary_root / f"{operation.replace('.', '-')}.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        response = run_cli(
            "gateway", str(request_path), "--root", str(instance_root)
        )
        self.assertEqual(response.returncode, 0, response.stderr or response.stdout)
        return _object(json.loads(response.stdout))


if __name__ == "__main__":
    unittest.main()
