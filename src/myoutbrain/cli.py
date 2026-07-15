from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib


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

SCHEMA_VERSION = 1
PERMANENT_STORAGE = ("vault", "store")
REBUILDABLE_STORAGE = ("runtime",)

EXIT_CONFIGURATION = 3
EXIT_LOCKED = 4
EXIT_IO = 5

GIT_IGNORE_MARKER = "# MyOutBrain machine data"
GIT_IGNORE_RULES = ("/store/objects/", "/runtime/")


class ConfigurationConflict(Exception):
    """Raised when existing content cannot be safely initialized."""


class WriterLocked(Exception):
    """Raised when another writer already owns the project lock."""


def render_initial_configuration() -> str:
    permanent = ", ".join(f'"{name}"' for name in PERMANENT_STORAGE)
    rebuildable = ", ".join(f'"{name}"' for name in REBUILDABLE_STORAGE)
    return (
        f"schema_version = {SCHEMA_VERSION}\n"
        "single_writer = true\n\n"
        "[storage]\n"
        f"permanent = [{permanent}]\n"
        f"rebuildable = [{rebuildable}]\n"
    )


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


def with_required_git_ignore_rules(existing_content: str) -> str:
    existing_lines = set(existing_content.splitlines())
    additions: list[str] = []
    if GIT_IGNORE_MARKER not in existing_lines:
        additions.append(GIT_IGNORE_MARKER)
    additions.extend(rule for rule in GIT_IGNORE_RULES if rule not in existing_lines)
    if not additions:
        return existing_content
    separator = "" if not existing_content or existing_content.endswith("\n") else "\n"
    return f"{existing_content}{separator}{'\n'.join(additions)}\n"


def validate_configuration(path: Path) -> None:
    try:
        with path.open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationConflict(f"invalid configuration: {path}") from error

    if configuration.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationConflict("unsupported schema_version in existing configuration")
    if configuration.get("single_writer") is not True:
        raise ConfigurationConflict("existing configuration must enable single_writer")
    storage = configuration.get("storage")
    if not isinstance(storage, dict):
        raise ConfigurationConflict("existing configuration is missing storage classification")
    if storage.get("permanent") != list(PERMANENT_STORAGE):
        raise ConfigurationConflict("existing configuration has an invalid permanent storage classification")
    if storage.get("rebuildable") != list(REBUILDABLE_STORAGE):
        raise ConfigurationConflict("existing configuration has an invalid rebuildable storage classification")


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
        try:
            existing_git_ignore = git_ignore.read_text(encoding="utf-8") if git_ignore.exists() else ""
        except UnicodeError as error:
            raise ConfigurationConflict(f"Git ignore file is not valid UTF-8: {git_ignore}") from error
        if configuration.exists():
            validate_configuration(configuration)
        for relative_path in INITIAL_DIRECTORIES:
            (root / relative_path).mkdir(parents=True, exist_ok=True)
        updated_git_ignore = with_required_git_ignore_rules(existing_git_ignore)
        if updated_git_ignore != existing_git_ignore:
            atomic_write_text(git_ignore, updated_git_ignore)
        if not configuration.exists():
            atomic_write_text(configuration, render_initial_configuration())
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
