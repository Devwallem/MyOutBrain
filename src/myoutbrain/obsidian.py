from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Protocol


class ObsidianAdapter(Protocol):
    def open_note(self, vault: Path, note_path: Path) -> str | None: ...


class ObsidianCliAdapter:
    """Opens an already-written Vault note without owning its persistence."""

    def open_note(self, vault: Path, note_path: Path) -> str | None:
        executable = shutil.which("obsidian")
        if executable is None:
            return "Obsidian CLI not found; the derived insight was saved but not opened."
        try:
            completed = subprocess.run(
                [executable, "open", f"path={note_path.relative_to(vault).as_posix()}"],
                cwd=vault,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "Obsidian CLI failed; the derived insight was saved but not opened."
        if completed.returncode != 0:
            return "Obsidian CLI failed; the derived insight was saved but not opened."
        return None


@dataclass(frozen=True)
class RecordingObsidianAdapter:
    """Records the CLI contract for deterministic acceptance testing."""

    request_path: Path

    def open_note(self, vault: Path, note_path: Path) -> str | None:
        relative_path = note_path.relative_to(vault).as_posix()
        request = {
            "command": ["obsidian", "open", f"path={relative_path}"],
            "cwd": str(vault),
        }
        self.request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return None


def create_obsidian_adapter() -> ObsidianAdapter:
    recording_path = os.environ.get("MYOUTBRAIN_FAKE_OBSIDIAN_REQUEST")
    if recording_path is not None:
        return RecordingObsidianAdapter(Path(recording_path))
    return ObsidianCliAdapter()
