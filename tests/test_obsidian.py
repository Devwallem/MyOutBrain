from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from myoutbrain.obsidian import ObsidianCliAdapter


class ObsidianCliAdapterTests(unittest.TestCase):
    def test_open_translates_to_vault_relative_obsidian_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vault = Path(temporary_directory) / "vault"
            note_path = vault / "Folder" / "Durable Insight.md"
            executable = r"C:\Program Files\Obsidian\Obsidian.com"
            completed = subprocess.CompletedProcess([], 0, "", "")

            with (
                patch(
                    "myoutbrain.obsidian.shutil.which",
                    return_value=executable,
                ),
                patch(
                    "myoutbrain.obsidian.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                warning = ObsidianCliAdapter().open_note(vault, note_path)

            self.assertIsNone(warning)
            run.assert_called_once_with(
                [executable, "open", "path=Folder/Durable Insight.md"],
                cwd=vault,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

    def test_nonzero_exit_keeps_the_saved_note_and_returns_warning(self) -> None:
        completed = subprocess.CompletedProcess([], 1, "", "failed")
        with (
            patch("myoutbrain.obsidian.shutil.which", return_value="obsidian"),
            patch(
                "myoutbrain.obsidian.subprocess.run",
                return_value=completed,
            ),
        ):
            warning = ObsidianCliAdapter().open_note(
                Path("vault"),
                Path("vault") / "Insight.md",
            )

        self.assertIsNotNone(warning)
        self.assertIn("saved but not opened", warning or "")

    def test_timeout_keeps_the_saved_note_and_returns_warning(self) -> None:
        with (
            patch("myoutbrain.obsidian.shutil.which", return_value="obsidian"),
            patch(
                "myoutbrain.obsidian.subprocess.run",
                side_effect=subprocess.TimeoutExpired("obsidian", 30),
            ),
        ):
            warning = ObsidianCliAdapter().open_note(
                Path("vault"),
                Path("vault") / "Insight.md",
            )

        self.assertIsNotNone(warning)
        self.assertIn("saved but not opened", warning or "")


if __name__ == "__main__":
    unittest.main()
