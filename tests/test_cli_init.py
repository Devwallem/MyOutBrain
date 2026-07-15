from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_environment = os.environ.copy()
    command_environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    if environment is not None:
        command_environment.update(environment)

    return subprocess.run(
        [sys.executable, "-m", "myoutbrain", *arguments],
        cwd=PROJECT_ROOT,
        env=command_environment,
        capture_output=True,
        text=True,
        check=False,
    )


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
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            (library_root / ".myoutbrain.lock").write_text("another writer", encoding="utf-8")

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 4)
            self.assertIn("Another MyOutBrain writer is active", result.stderr)
            self.assertFalse((library_root / "vault").exists())
            self.assertFalse((library_root / "runtime").exists())
            self.assertFalse((library_root / "myoutbrain.toml").exists())


if __name__ == "__main__":
    unittest.main()
