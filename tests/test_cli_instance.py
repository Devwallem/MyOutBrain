from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tests.cli_support import run_cli, start_cli, wait_until_lock_is_held


class V2InstanceLifecycleTests(unittest.TestCase):
    def test_creator_can_initialize_and_inspect_a_v2_private_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "MyOutBrain"

            initialized = run_cli("init", "--root", str(instance_root))
            status = run_cli(
                "status",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(
                json.loads(status.stdout),
                {
                    "instance_version": 2,
                    "canonical_schema_version": 9,
                    "write": {
                        "available": True,
                        "mode": "single-writer",
                    },
                    "integrity": {
                        "canonical_store": "ok",
                        "object_store": "ok",
                        "overall": "ok",
                    },
                },
            )

    def test_initialization_does_not_implicitly_migrate_v1_semantic_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "MyOutBrain"
            instance_root.mkdir()
            (instance_root / "myoutbrain.toml").write_text(
                "schema_version = 1\n"
                "single_writer = true\n\n"
                "[storage]\n"
                'permanent = ["vault", "store"]\n'
                'rebuildable = ["runtime"]\n',
                encoding="utf-8",
            )
            v1_note = instance_root / "vault" / "Personal Cognition.md"
            v1_note.parent.mkdir()
            v1_note.write_text("Keep this V1 understanding unchanged.\n", encoding="utf-8")

            result = run_cli("init", "--root", str(instance_root))

            self.assertEqual(result.returncode, 3)
            self.assertIn("not a V2 private instance", result.stderr)
            self.assertEqual(
                v1_note.read_text(encoding="utf-8"),
                "Keep this V1 understanding unchanged.\n",
            )
            self.assertFalse((instance_root / "store" / "memory.sqlite3").exists())

    def test_competing_writer_receives_a_stable_error_category(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            ready_file = temporary_root / "writer-ready"
            first_writer = start_cli(
                "init",
                "--root",
                str(instance_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "hold-writer-lock",
                    "MYOUTBRAIN_HOLD_SECONDS": "1",
                    "MYOUTBRAIN_LOCK_READY_FILE": str(ready_file),
                },
            )
            wait_until_lock_is_held(ready_file, first_writer)

            competing = run_cli(
                "init",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            _, first_stderr = first_writer.communicate(timeout=5)

            self.assertEqual(competing.returncode, 4)
            self.assertEqual(
                json.loads(competing.stderr),
                {
                    "error": {
                        "category": "writer_locked",
                        "message": "Another MyOutBrain writer is active.",
                    }
                },
            )
            self.assertEqual(first_writer.returncode, 0, first_stderr)

    def test_interrupted_initialization_recovers_to_a_complete_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "MyOutBrain"

            interrupted = run_cli(
                "init",
                "--root",
                str(instance_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "initialize-after-configuration"
                },
            )
            interrupted_status = run_cli(
                "status",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            configuration_remained = (instance_root / "myoutbrain.toml").exists()
            database_remained = (
                instance_root / "store" / "memory.sqlite3"
            ).exists()
            remaining_transactions = tuple(
                (instance_root / "store" / "transactions").iterdir()
            )
            recovered = run_cli("init", "--root", str(instance_root))
            status = run_cli(
                "status",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(interrupted.returncode, 86)
            self.assertEqual(interrupted_status.returncode, 3)
            self.assertFalse(configuration_remained)
            self.assertFalse(database_remained)
            self.assertEqual(remaining_transactions, ())
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["integrity"]["overall"], "ok")
            self.assertEqual(json.loads(status.stdout)["instance_version"], 2)

    def test_status_reports_a_corrupt_content_addressed_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source = temporary_root / "Source.md"
            source.write_text("Content-addressed evidence.\n", encoding="utf-8")
            initialized = run_cli("init", "--root", str(instance_root))
            captured = run_cli(
                "capture",
                str(source),
                "--root",
                str(instance_root),
                "--sensitivity",
                "local-only",
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            object_path = next(
                path
                for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            object_path.write_bytes(b"corrupt")

            status = run_cli(
                "status",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            integrity = json.loads(status.stdout)["integrity"]
            self.assertEqual(integrity["canonical_store"], "ok")
            self.assertEqual(integrity["object_store"], "corrupt")
            self.assertEqual(integrity["overall"], "degraded")


if __name__ == "__main__":
    unittest.main()
