from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys

from myoutbrain.library import (
    ConfigurationConflict,
    IntegrityError,
    KnowledgeWorkflow,
    Sensitivity,
    UserInputError,
    WriterLocked,
)


EXIT_USER = 2
EXIT_CONFIGURATION = 3
EXIT_LOCKED = 4
EXIT_IO = 5
EXIT_INTEGRITY = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myoutbrain")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize_parser = subcommands.add_parser("init", help="Initialize a private cognitive library")
    initialize_parser.add_argument("--root", type=Path, default=Path.cwd())
    capture_parser = subcommands.add_parser("capture", help="Capture a Markdown source")
    capture_parser.add_argument("source", type=Path)
    capture_parser.add_argument("--root", type=Path, default=Path.cwd())
    capture_parser.add_argument(
        "--sensitivity",
        required=True,
        choices=("local-only", "cloud-allowed"),
    )
    return parser


def _initialize(root: Path) -> int:
    KnowledgeWorkflow(root).initialize()
    print(f"Initialized MyOutBrain at {root.resolve()}")
    if shutil.which("obsidian") is None:
        print(
            "Warning: Obsidian CLI not found. Install Obsidian 1.12.7+ on Windows, "
            "then enable Command line interface in Settings > General and register it on PATH.",
            file=sys.stderr,
        )
    return 0


def _capture(root: Path, source: Path, sensitivity: Sensitivity) -> int:
    result = KnowledgeWorkflow(root).capture(source, sensitivity)
    if result.disposition == "captured":
        print(f"Captured source {result.source_id}")
    else:
        detail = result.disposition.replace("-", " ")
        print(f"Already captured source {result.source_id} ({detail})")
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parsed_arguments = build_parser().parse_args(arguments)
    try:
        if parsed_arguments.command == "init":
            return _initialize(parsed_arguments.root)
        if parsed_arguments.command == "capture":
            return _capture(
                parsed_arguments.root,
                parsed_arguments.source,
                parsed_arguments.sensitivity,
            )
    except UserInputError as error:
        print(f"Invalid source: {error}", file=sys.stderr)
        return EXIT_USER
    except ConfigurationConflict as error:
        print(f"Configuration conflict: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    except WriterLocked:
        print("Another MyOutBrain writer is active.", file=sys.stderr)
        return EXIT_LOCKED
    except IntegrityError as error:
        print(f"Integrity failure: {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    except OSError as error:
        operation = "Capture" if parsed_arguments.command == "capture" else "Initialization"
        print(f"{operation} failed: {error}", file=sys.stderr)
        return EXIT_IO
    return EXIT_USER
