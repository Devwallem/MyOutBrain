from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import sys
import tempfile


INITIAL_DIRECTORIES = (
    "vault",
    "store",
    "store/objects",
    "store/objects/sha256",
    "store/records",
    "store/journal",
    "runtime",
    "runtime/derived",
    "runtime/indexes",
    "runtime/indexes/fulltext",
    "runtime/workspace",
    "runtime/workspace/inbox",
    "runtime/workspace/candidates",
    "runtime/cache",
    "runtime/logs",
)

INITIAL_CONFIGURATION = """schema_version = 1
single_writer = true

[storage]
permanent = ["vault", "store"]
rebuildable = ["runtime"]
"""

EXIT_CONFIGURATION = 3
EXIT_LOCKED = 4
EXIT_IO = 5

GIT_IGNORE_BLOCK = """# MyOutBrain machine data
/store/objects/
/runtime/
"""


class ConfigurationConflict(Exception):
    """Raised when existing content cannot be safely initialized."""


class WriterLocked(Exception):
    """Raised when another writer already owns the project lock."""


def atomic_write_text(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myoutbrain")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize_parser = subcommands.add_parser("init", help="Initialize a private cognitive library")
    initialize_parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def initialize(root: Path) -> int:
    if root.exists() and not root.is_dir():
        raise ConfigurationConflict(f"project root is not a directory: {root}")
    for relative_path in INITIAL_DIRECTORIES:
        candidate = root / relative_path
        if candidate.exists() and not candidate.is_dir():
            raise ConfigurationConflict(f"expected a directory at: {candidate}")
    configuration = root / "myoutbrain.toml"
    if configuration.exists() and not configuration.is_file():
        raise ConfigurationConflict(f"expected a configuration file at: {configuration}")
    git_ignore = root / ".gitignore"
    if git_ignore.exists() and not git_ignore.is_file():
        raise ConfigurationConflict(f"expected a Git ignore file at: {git_ignore}")

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".myoutbrain.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise WriterLocked from error

    try:
        os.write(lock_descriptor, str(os.getpid()).encode("ascii"))
        for relative_path in INITIAL_DIRECTORIES:
            (root / relative_path).mkdir(parents=True, exist_ok=True)
        existing_git_ignore = git_ignore.read_text(encoding="utf-8") if git_ignore.exists() else ""
        if "# MyOutBrain machine data" not in existing_git_ignore:
            separator = "" if not existing_git_ignore or existing_git_ignore.endswith("\n") else "\n"
            atomic_write_text(
                git_ignore,
                f"{existing_git_ignore}{separator}{GIT_IGNORE_BLOCK}",
            )
        if not configuration.exists():
            atomic_write_text(configuration, INITIAL_CONFIGURATION)
    finally:
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)
    print(f"Initialized MyOutBrain at {root.resolve()}")
    if shutil.which("obsidian") is None:
        print(
            "Warning: Obsidian CLI not found. Install Obsidian 1.12.7+ on Windows, "
            "then enable Command line interface in Settings > General and register it on PATH.",
            file=sys.stderr,
        )
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parsed_arguments = build_parser().parse_args(arguments)
    try:
        if parsed_arguments.command == "init":
            return initialize(parsed_arguments.root)
    except ConfigurationConflict as error:
        print(f"Configuration conflict: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    except WriterLocked:
        print("Another MyOutBrain writer is active.", file=sys.stderr)
        return EXIT_LOCKED
    except OSError as error:
        print(f"Initialization failed: {error}", file=sys.stderr)
        return EXIT_IO
    return 2
