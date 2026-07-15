from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def cli_invocation(
    arguments: tuple[str, ...],
    environment: dict[str, str] | None,
) -> tuple[list[str], dict[str, str]]:
    command_environment = os.environ.copy()
    command_environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    if environment is not None:
        command_environment.update(environment)
    command = [sys.executable, "-m", "myoutbrain", *arguments]
    return command, command_environment


def run_cli(*arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command, command_environment = cli_invocation(arguments, environment)
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=command_environment,
        capture_output=True,
        text=True,
        check=False,
    )


def start_cli(
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    command, command_environment = cli_invocation(arguments, environment)
    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=command_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_until_lock_is_held(ready_file: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if ready_file.read_text(encoding="ascii").strip():
                return
        except (FileNotFoundError, UnicodeError, OSError):
            pass
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate()
    raise AssertionError(f"Writer never acquired the lock. stdout={stdout!r}, stderr={stderr!r}")
