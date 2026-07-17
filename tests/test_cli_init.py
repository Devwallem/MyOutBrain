from __future__ import annotations

from pathlib import Path
import json
import re
import tempfile
import unittest

from tests.cli_support import run_cli, start_cli, wait_until_lock_is_held


class InitializePrivateCognitiveLibraryTests(unittest.TestCase):
    def test_creator_can_initialize_a_private_cognitive_library(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Initialized MyOutBrain", result.stdout)
            expected_directories = (
                "vault",
                "store/objects/sha256",
                "store/records",
                "store/journal",
                "runtime/derived",
                "runtime/indexes/fulltext",
                "runtime/indexes/semantic",
                "runtime/workspace/inbox",
                "runtime/workspace/candidates",
                "runtime/cache",
                "runtime/logs",
            )
            for relative_path in expected_directories:
                self.assertTrue(
                    (library_root / relative_path).is_dir(),
                    f"Expected initialized directory: {relative_path}",
                )
            self.assertTrue((library_root / "myoutbrain.toml").is_file())
            configuration = (library_root / "myoutbrain.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn('[embedding]\nprovider = "myoutbrain-local"', configuration)
            self.assertIn("allow_cloud = false", configuration)
            self.assertTrue((library_root / "store" / "memory.sqlite3").is_file())
            self.assertFalse((library_root / ".git").exists())

    def test_reinitialization_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            first_result = run_cli("init", "--root", str(library_root))
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            note = library_root / "vault" / "My Existing Note.md"
            note.write_text("Do not overwrite me.", encoding="utf-8")
            configuration = library_root / "myoutbrain.toml"
            configuration.write_text(
                configuration.read_text(encoding="utf-8") + "\ncreator = \"me\"\n",
                encoding="utf-8",
            )

            second_result = run_cli("init", "--root", str(library_root))

            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(note.read_text(encoding="utf-8"), "Do not overwrite me.")
            self.assertIn('creator = "me"', configuration.read_text(encoding="utf-8"))

    def test_conflicting_content_is_rejected_before_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            (library_root / "store").write_text("This is a file, not a directory.", encoding="utf-8")

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 3)
            self.assertIn("Configuration conflict", result.stderr)
            self.assertFalse((library_root / "vault").exists())
            self.assertFalse((library_root / "runtime").exists())
            self.assertFalse((library_root / "myoutbrain.toml").exists())


    def test_initialization_preserves_and_extends_git_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            git_ignore = library_root / ".gitignore"
            git_ignore.write_text("custom.log\n", encoding="utf-8")

            first_result = run_cli("init", "--root", str(library_root))
            second_result = run_cli("init", "--root", str(library_root))

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            rules = git_ignore.read_text(encoding="utf-8")
            self.assertIn("custom.log\n", rules)
            self.assertEqual(rules.count("# MyOutBrain machine data"), 1)
            self.assertEqual(rules.count("/store/objects/"), 1)
            self.assertEqual(rules.count("/runtime/"), 1)
            self.assertNotIn("/store/\n", rules)
            self.assertNotIn("/vault/", rules)

    def test_initialization_repairs_an_incomplete_managed_git_ignore_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            git_ignore = library_root / ".gitignore"
            git_ignore.write_text(
                "# MyOutBrain machine data\n/runtime/\n",
                encoding="utf-8",
            )

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            rules = git_ignore.read_text(encoding="utf-8")
            self.assertEqual(rules.count("# MyOutBrain machine data"), 1)
            self.assertEqual(rules.count("/store/objects/"), 1)
            self.assertEqual(rules.count("/runtime/"), 1)

    def test_unreadable_git_ignore_is_rejected_before_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            (library_root / ".gitignore").write_bytes(b"\xff")

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 3)
            self.assertIn("Configuration conflict", result.stderr)
            self.assertFalse((library_root / "vault").exists())
            self.assertFalse((library_root / "store").exists())
            self.assertFalse((library_root / "runtime").exists())
            self.assertFalse((library_root / "myoutbrain.toml").exists())

    def test_invalid_existing_configuration_is_rejected_before_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            (library_root / "myoutbrain.toml").write_text(
                "schema_version = 99\n",
                encoding="utf-8",
            )

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 3)
            self.assertIn("Configuration conflict", result.stderr)
            self.assertIn("schema_version", result.stderr)
            self.assertFalse((library_root / "vault").exists())
            self.assertFalse((library_root / "store").exists())
            self.assertFalse((library_root / "runtime").exists())

    def test_missing_obsidian_cli_produces_actionable_windows_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"

            result = run_cli(
                "init",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Obsidian CLI not found", result.stderr)
            self.assertIn("Obsidian 1.12.7+", result.stderr)
            self.assertIn("Settings > General", result.stderr)
            self.assertIn("PATH", result.stderr)

    def test_active_writer_lock_rejects_initialization_without_partial_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            ready_file = temporary_root / "init-lock-ready"
            first_writer = start_cli(
                "init",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "hold-writer-lock",
                    "MYOUTBRAIN_HOLD_SECONDS": "1",
                    "MYOUTBRAIN_LOCK_READY_FILE": str(ready_file),
                },
            )
            wait_until_lock_is_held(ready_file, first_writer)

            competing_result = run_cli("init", "--root", str(library_root))
            first_stdout, first_stderr = first_writer.communicate(timeout=5)

            self.assertEqual(competing_result.returncode, 4)
            self.assertIn("Another MyOutBrain writer is active", competing_result.stderr)
            self.assertEqual(first_writer.returncode, 0, first_stderr)
            self.assertIn("Initialized MyOutBrain", first_stdout)
            self.assertTrue((library_root / "myoutbrain.toml").is_file())


class CaptureMarkdownSourceTests(unittest.TestCase):
    def test_creator_can_capture_markdown_as_an_immutable_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            source_path = temporary_root / "Reflection.md"
            source_bytes = b"Knowledge grows through reflection.\n"
            source_path.write_bytes(source_bytes)

            result = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Captured source", result.stdout)
            identity_match = re.search(r"src_[0-9a-f]{64}", result.stdout)
            self.assertIsNotNone(identity_match)
            source_id = identity_match.group(0) if identity_match is not None else ""
            object_files = tuple((library_root / "store" / "objects" / "sha256").rglob("*"))
            stored_objects = tuple(path for path in object_files if path.is_file())
            self.assertEqual(len(stored_objects), 1)
            self.assertEqual(stored_objects[0].read_bytes(), source_bytes)
            record = json.loads(
                (library_root / "store" / "records" / f"{source_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["id"], source_id)
            self.assertEqual(record["kind"], "source")
            self.assertEqual(record["state"], "active")
            self.assertEqual(record["sensitivity"], "local-only")
            self.assertEqual(record["origins"][0]["path"], str(source_path.resolve()))
            self.assertRegex(record["content_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertTrue(record["created_at"])
            journal_lines = (library_root / "store" / "journal" / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(journal_lines), 1)
            event = json.loads(journal_lines[0])
            self.assertEqual(event["type"], "source.captured")
            self.assertEqual(event["source_id"], source_id)

    def test_exact_duplicate_reuses_the_source_object_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            source_path = temporary_root / "Reflection.md"
            source_path.write_text("Knowledge grows through reflection.\n", encoding="utf-8")
            arguments = (
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )

            first_result = run_cli(*arguments)
            second_result = run_cli(*arguments)

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            first_identity = re.search(r"src_[0-9a-f]{64}", first_result.stdout)
            second_identity = re.search(r"src_[0-9a-f]{64}", second_result.stdout)
            self.assertIsNotNone(first_identity)
            self.assertIsNotNone(second_identity)
            first_source_id = first_identity.group(0) if first_identity is not None else ""
            second_source_id = second_identity.group(0) if second_identity is not None else ""
            self.assertEqual(first_source_id, second_source_id)
            self.assertIn("Already captured", second_result.stdout)
            stored_objects = tuple(
                path
                for path in (library_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            self.assertEqual(len(stored_objects), 1)
            journal_lines = (library_root / "store" / "journal" / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(journal_lines), 2)
            self.assertEqual(json.loads(journal_lines[1])["type"], "source.duplicate")

    def test_same_content_at_a_renamed_path_keeps_identity_and_both_origins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            original_path = temporary_root / "First Name.md"
            renamed_path = temporary_root / "Renamed.md"
            content = "A durable thought keeps its identity.\n"
            original_path.write_text(content, encoding="utf-8")
            renamed_path.write_text(content, encoding="utf-8")

            first_result = run_cli(
                "capture",
                str(original_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )
            second_result = run_cli(
                "capture",
                str(renamed_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            first_identity = re.search(r"src_[0-9a-f]{64}", first_result.stdout)
            second_identity = re.search(r"src_[0-9a-f]{64}", second_result.stdout)
            self.assertIsNotNone(first_identity)
            self.assertIsNotNone(second_identity)
            first_source_id = first_identity.group(0) if first_identity is not None else ""
            second_source_id = second_identity.group(0) if second_identity is not None else ""
            self.assertEqual(first_source_id, second_source_id)
            record = json.loads(
                (library_root / "store" / "records" / f"{first_source_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                [origin["path"] for origin in record["origins"]],
                [str(original_path.resolve()), str(renamed_path.resolve())],
            )
            stored_objects = tuple(
                path
                for path in (library_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            self.assertEqual(len(stored_objects), 1)

    def test_missing_source_returns_a_user_error_without_partial_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            missing_path = temporary_root / "Missing.md"

            result = run_cli(
                "capture",
                str(missing_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("Invalid source", result.stderr)
            self.assertIn("does not exist", result.stderr)
            self.assertEqual(
                tuple(
                    path
                    for path in (library_root / "store" / "objects" / "sha256").rglob("*")
                    if path.is_file()
                ),
                (),
            )
            self.assertEqual(tuple((library_root / "store" / "records").iterdir()), ())
            self.assertEqual(tuple((library_root / "store" / "journal").iterdir()), ())

    def test_blank_non_utf8_and_non_markdown_sources_leave_storage_unchanged(self) -> None:
        invalid_sources = {
            "Blank.md": b" \n\t",
            "Unreadable.md": b"\xff\xfe",
            "Not Markdown.txt": b"This has content, but is not Markdown.\n",
        }
        for filename, content in invalid_sources.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                library_root = temporary_root / "My Knowledge"
                initialization = run_cli("init", "--root", str(library_root))
                self.assertEqual(initialization.returncode, 0, initialization.stderr)
                source_path = temporary_root / filename
                source_path.write_bytes(content)

                result = run_cli(
                    "capture",
                    str(source_path),
                    "--root",
                    str(library_root),
                    "--sensitivity",
                    "local-only",
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("Invalid source", result.stderr)
                stored_objects = tuple(
                    path
                    for path in (library_root / "store" / "objects" / "sha256").rglob("*")
                    if path.is_file()
                )
                self.assertEqual(stored_objects, ())
                self.assertEqual(tuple((library_root / "store" / "records").iterdir()), ())
                self.assertEqual(tuple((library_root / "store" / "journal").iterdir()), ())

    def test_competing_captures_allow_exactly_one_writer_to_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            source_path = temporary_root / "Reflection.md"
            source_path.write_text("Only one writer may capture this.\n", encoding="utf-8")
            ready_file = temporary_root / "capture-lock-ready"
            first_writer = start_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "hold-writer-lock",
                    "MYOUTBRAIN_HOLD_SECONDS": "1",
                    "MYOUTBRAIN_LOCK_READY_FILE": str(ready_file),
                },
            )
            wait_until_lock_is_held(ready_file, first_writer)

            competing_result = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )
            first_stdout, first_stderr = first_writer.communicate(timeout=5)

            self.assertEqual(first_writer.returncode, 0, first_stderr)
            self.assertIn("Captured source", first_stdout)
            self.assertEqual(competing_result.returncode, 4)
            self.assertIn("Another MyOutBrain writer is active", competing_result.stderr)
            self.assertEqual(
                len(
                    tuple(
                        path
                        for path in (library_root / "store" / "objects" / "sha256").rglob("*")
                        if path.is_file()
                    )
                ),
                1,
            )
            self.assertEqual(len(tuple((library_root / "store" / "records").iterdir())), 1)
            self.assertEqual(
                len(
                    (library_root / "store" / "journal" / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
                1,
            )

    def test_interrupted_capture_restores_the_complete_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            first_source = temporary_root / "First.md"
            first_source.write_text("The established state.\n", encoding="utf-8")
            first_capture = run_cli(
                "capture",
                str(first_source),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )
            self.assertEqual(first_capture.returncode, 0, first_capture.stderr)
            before = {
                path.relative_to(library_root).as_posix(): path.read_bytes()
                for path in (library_root / "store").rglob("*")
                if path.is_file()
            }
            second_source = temporary_root / "Second.md"
            second_source.write_text("A change that will be interrupted.\n", encoding="utf-8")

            interrupted = run_cli(
                "capture",
                str(second_source),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
                environment={"MYOUTBRAIN_FAULT_INJECTION": "capture-after-first-replace"},
            )

            self.assertEqual(interrupted.returncode, 86)
            recovery = run_cli("init", "--root", str(library_root))
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            after = {
                path.relative_to(library_root).as_posix(): path.read_bytes()
                for path in (library_root / "store").rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertEqual(tuple((library_root / "store" / "transactions").iterdir()), ())

    def test_duplicate_capture_never_loosens_existing_sensitivity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            source_path = temporary_root / "Private.md"
            source_path.write_text("This material becomes private.\n", encoding="utf-8")

            cloud_capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            private_capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )
            attempted_upgrade = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )

            self.assertEqual(cloud_capture.returncode, 0, cloud_capture.stderr)
            self.assertEqual(private_capture.returncode, 0, private_capture.stderr)
            self.assertEqual(attempted_upgrade.returncode, 0, attempted_upgrade.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", cloud_capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            record = json.loads(
                (library_root / "store" / "records" / f"{source_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record["sensitivity"], "local-only")

    def test_capture_refuses_to_overwrite_a_corrupted_content_addressed_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            source_path = temporary_root / "Immutable.md"
            source_path.write_text("Original immutable bytes.\n", encoding="utf-8")
            first_capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )
            self.assertEqual(first_capture.returncode, 0, first_capture.stderr)
            object_path = next(
                path
                for path in (library_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            object_path.write_bytes(b"corrupted")
            records_before = {
                path.name: path.read_bytes()
                for path in (library_root / "store" / "records").iterdir()
                if path.is_file()
            }
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            journal_before = journal_path.read_bytes()

            second_capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )

            self.assertEqual(second_capture.returncode, 7)
            self.assertIn("Integrity failure", second_capture.stderr)
            self.assertEqual(object_path.read_bytes(), b"corrupted")
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (library_root / "store" / "records").iterdir()
                    if path.is_file()
                },
                records_before,
            )
            self.assertEqual(journal_path.read_bytes(), journal_before)


if __name__ == "__main__":
    unittest.main()
