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
from myoutbrain.generation import ProviderFailure


EXIT_USER = 2
EXIT_CONFIGURATION = 3
EXIT_LOCKED = 4
EXIT_IO = 5
EXIT_PROVIDER = 6
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
    ask_parser = subcommands.add_parser("ask", help="Answer a question from one captured source")
    ask_parser.add_argument("source_id")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--root", type=Path, default=Path.cwd())
    ask_parser.add_argument("--allow-cloud", action="store_true")
    reflect_parser = subcommands.add_parser(
        "reflect",
        help="Generate temporary candidate insights from one captured source",
    )
    reflect_parser.add_argument("source_id")
    reflect_parser.add_argument("prompt")
    reflect_parser.add_argument("--root", type=Path, default=Path.cwd())
    reflect_parser.add_argument("--allow-cloud", action="store_true")
    review_parser = subcommands.add_parser(
        "review",
        help="List and review temporary candidate insights",
    )
    review_parser.add_argument("candidate_id", nargs="?")
    review_parser.add_argument("--decision", choices=("defer", "reject", "accept"))
    review_parser.add_argument("--title")
    review_parser.add_argument("--text")
    review_parser.add_argument(
        "--sensitivity",
        choices=("local-only", "cloud-allowed"),
    )
    review_parser.add_argument("--root", type=Path, default=Path.cwd())
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


def _ask(root: Path, source_id: str, question: str, allow_cloud: bool) -> int:
    result = KnowledgeWorkflow(root).ask(source_id, question, allow_cloud=allow_cloud)
    if result.insufficient_evidence:
        print("Insufficient evidence: the captured source does not answer this question.")
        for evidence in result.evidence:
            print(
                "Evidence checked: "
                f"[{evidence.citation.source_id} @ {evidence.citation.locator}]"
            )
    else:
        for claim in result.claims:
            print(
                f"{claim.text} "
                f"[{claim.citation.source_id} @ {claim.citation.locator}]"
            )
    return 0


def _reflect(root: Path, source_id: str, prompt: str, allow_cloud: bool) -> int:
    result = KnowledgeWorkflow(root).reflect(
        source_id,
        prompt,
        allow_cloud=allow_cloud,
    )
    if result.insufficient_evidence:
        print("Insufficient evidence: no candidate insight was created.")
        for evidence in result.evidence:
            print(
                "Evidence checked: "
                f"[{evidence.citation.source_id} @ {evidence.citation.locator}]"
            )
    else:
        for candidate_id in result.candidate_ids:
            print(f"Candidate insight {candidate_id}")
        if result.suppressed_count:
            print(
                f"No duplicate created: {result.suppressed_count} recently rejected "
                "candidate suppressed."
            )
    return 0


def _review(
    root: Path,
    candidate_id: str | None,
    decision: str | None,
    title: str | None,
    text: str | None,
    sensitivity: Sensitivity | None,
) -> int:
    if candidate_id is not None or decision is not None:
        if candidate_id is None or decision is None:
            raise UserInputError("review decision requires a candidate identity")
        if decision == "defer":
            KnowledgeWorkflow(root).defer_candidate(candidate_id)
            print(f"Deferred candidate {candidate_id}")
            return 0
        if decision == "reject":
            KnowledgeWorkflow(root).reject_candidate(candidate_id)
            print(f"Rejected candidate {candidate_id}")
            return 0
        if title is None or sensitivity is None:
            raise UserInputError(
                "accepting a candidate requires --title and --sensitivity"
            )
        result = KnowledgeWorkflow(root).accept_candidate(
            candidate_id,
            title=title,
            text=text,
            sensitivity=sensitivity,
        )
        print(f"Accepted derived insight {result.knowledge_id} at {result.note_path}")
        if result.warning is not None:
            print(f"Warning: {result.warning}", file=sys.stderr)
        return 0
    candidates = KnowledgeWorkflow(root).review_candidates()
    if not candidates:
        print("No candidate insights awaiting review.")
        return 0
    for candidate in candidates:
        print(f"Candidate {candidate.candidate_id}")
        print(candidate.text)
        for citation in candidate.supporting_evidence:
            print(f"Supporting evidence: [{citation.source_id} @ {citation.locator}]")
        if candidate.contrary_evidence:
            for citation in candidate.contrary_evidence:
                print(f"Contrary evidence: [{citation.source_id} @ {citation.locator}]")
        else:
            print("Contrary evidence: none")
        print(f"Derivation: {candidate.derivation}")
        print(f"Occurrences: {candidate.occurrence_count}")
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
        if parsed_arguments.command == "ask":
            return _ask(
                parsed_arguments.root,
                parsed_arguments.source_id,
                parsed_arguments.question,
                parsed_arguments.allow_cloud,
            )
        if parsed_arguments.command == "reflect":
            return _reflect(
                parsed_arguments.root,
                parsed_arguments.source_id,
                parsed_arguments.prompt,
                parsed_arguments.allow_cloud,
            )
        if parsed_arguments.command == "review":
            return _review(
                parsed_arguments.root,
                parsed_arguments.candidate_id,
                parsed_arguments.decision,
                parsed_arguments.title,
                parsed_arguments.text,
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
    except ProviderFailure as error:
        print(f"Provider failure: {error}", file=sys.stderr)
        return EXIT_PROVIDER
    except IntegrityError as error:
        print(f"Integrity failure: {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    except OSError as error:
        operation = "Capture" if parsed_arguments.command == "capture" else "Initialization"
        print(f"{operation} failed: {error}", file=sys.stderr)
        return EXIT_IO
    return EXIT_USER
