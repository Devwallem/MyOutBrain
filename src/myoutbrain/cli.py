from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import shutil
import sys

from myoutbrain.evaluation import (
    evaluate_recall,
    load_recall_dataset,
    report_has_failures,
    report_as_json,
    report_as_text,
)
from myoutbrain.core_types import (
    ConfigurationConflict,
    IntegrityError,
    Sensitivity,
    UserInputError,
    WriterLocked,
)
from myoutbrain.library import KnowledgeWorkflow
from myoutbrain.generation import ProviderFailure
from myoutbrain.local_core import LocalMemoryCore
from myoutbrain.memory_gateway import (
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)


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
    remember_parser = subcommands.add_parser(
        "remember",
        help="Record a visible conversation as buffered memory",
    )
    remember_parser.add_argument("conversation", type=Path)
    remember_parser.add_argument("--root", type=Path, default=Path.cwd())
    remember_parser.add_argument("--occurred-at", required=True)
    remember_parser.add_argument("--entrance", required=True)
    remember_parser.add_argument("--task", required=True)
    remember_parser.add_argument(
        "--digest",
        required=True,
        help="Compact semantic memory derived by the submitting entrance",
    )
    remember_parser.add_argument(
        "--sensitivity",
        required=True,
        choices=("local-only", "cloud-allowed"),
    )
    remember_parser.add_argument("--visible-context", required=True)
    remember_parser.add_argument("--context-gap", action="append", required=True)
    remember_parser.add_argument("--format", choices=("json", "text"), default="text")
    recall_parser = subcommands.add_parser(
        "recall",
        help="Request a task-scoped memory evidence package",
    )
    recall_parser.add_argument("query")
    recall_parser.add_argument("--root", type=Path, default=Path.cwd())
    recall_parser.add_argument("--task", required=True)
    recall_parser.add_argument(
        "--access",
        choices=tuple(level.value for level in MemoryAccess),
        default=MemoryAccess.TASK_SCOPED.value,
    )
    recall_parser.add_argument(
        "--purpose",
        choices=tuple(purpose.value for purpose in QueryPurpose),
        default=QueryPurpose.SUBSTANTIVE.value,
    )
    recall_parser.add_argument("--memory-id", action="append", default=[])
    recall_parser.add_argument("--source-id", action="append", default=[])
    recall_parser.add_argument("--limit", type=int, default=5)
    recall_parser.add_argument("--format", choices=("json", "text"), default="text")
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
    promote_parser = subcommands.add_parser(
        "promote",
        help="Explicitly promote a derived insight to personal cognition",
    )
    promote_parser.add_argument("insight_id")
    promote_parser.add_argument("--title", required=True)
    promote_parser.add_argument("--supersedes")
    promote_parser.add_argument("--root", type=Path, default=Path.cwd())
    rebuild_parser = subcommands.add_parser(
        "rebuild",
        help="Rebuild runtime projections from permanent knowledge",
    )
    rebuild_parser.add_argument("--root", type=Path, default=Path.cwd())
    evaluate_parser = subcommands.add_parser(
        "evaluate-recall",
        help="Evaluate evidence retrieval without generating answers",
    )
    evaluate_parser.add_argument("dataset", type=Path)
    evaluate_parser.add_argument("--format", choices=("json", "text"), default="text")
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


def _remember(
    root: Path,
    conversation: Path,
    *,
    occurred_at: str,
    entrance: str,
    task: str,
    digest: str,
    sensitivity: Sensitivity,
    visible_context: str,
    context_gaps: Sequence[str],
    output_format: str,
) -> int:
    receipt = LocalMemoryCore(root).capture_experience(
        conversation,
        occurred_at=occurred_at,
        entrance=entrance,
        task=task,
        memory_digest=digest,
        sensitivity=sensitivity,
        visible_context=visible_context,
        context_gaps=tuple(context_gaps),
    )
    if output_format == "json":
        print(json.dumps(receipt.to_data(), ensure_ascii=False, sort_keys=True))
    elif receipt.disposition == "duplicate":
        print(f"Already buffered memory {receipt.digest_id} from {receipt.experience_id}")
    else:
        print(f"Buffered memory {receipt.digest_id} from {receipt.experience_id}")
    return 0


def _recall(
    root: Path,
    query: str,
    *,
    task: str,
    access: str,
    purpose: str,
    memory_ids: Sequence[str],
    source_ids: Sequence[str],
    limit: int,
    output_format: str,
) -> int:
    package = MemoryGateway(root).recall(
        RecallRequest(
            query=query,
            task=task,
            access=MemoryAccess(access),
            purpose=QueryPurpose(purpose),
            memory_ids=tuple(memory_ids),
            source_ids=tuple(source_ids),
            limit=limit,
        )
    )
    if output_format == "json":
        print(json.dumps(package.to_data(), ensure_ascii=False, sort_keys=True))
        return 0
    if not package.retrieval_performed:
        print("Memory retrieval skipped: this query does not require evidence.")
        return 0
    print(f"Answerability: {package.answerability.value}")
    for item in package.items:
        print(
            f"{item.memory_id} ({item.memory_state.value}, {item.match.value}): "
            f"{item.content}"
        )
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
        if parsed_arguments.command == "remember":
            return _remember(
                parsed_arguments.root,
                parsed_arguments.conversation,
                occurred_at=parsed_arguments.occurred_at,
                entrance=parsed_arguments.entrance,
                task=parsed_arguments.task,
                digest=parsed_arguments.digest,
                sensitivity=parsed_arguments.sensitivity,
                visible_context=parsed_arguments.visible_context,
                context_gaps=parsed_arguments.context_gap,
                output_format=parsed_arguments.format,
            )
        if parsed_arguments.command == "recall":
            return _recall(
                parsed_arguments.root,
                parsed_arguments.query,
                task=parsed_arguments.task,
                access=parsed_arguments.access,
                purpose=parsed_arguments.purpose,
                memory_ids=parsed_arguments.memory_id,
                source_ids=parsed_arguments.source_id,
                limit=parsed_arguments.limit,
                output_format=parsed_arguments.format,
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
        if parsed_arguments.command == "promote":
            promotion_result = KnowledgeWorkflow(parsed_arguments.root).promote_insight(
                parsed_arguments.insight_id,
                title=parsed_arguments.title,
                supersedes_id=parsed_arguments.supersedes,
            )
            print(
                f"Promoted personal cognition {promotion_result.cognition_id} "
                f"at {promotion_result.note_path}"
            )
            if promotion_result.warning is not None:
                print(f"Warning: {promotion_result.warning}", file=sys.stderr)
            return 0
        if parsed_arguments.command == "rebuild":
            rebuild_result = KnowledgeWorkflow(parsed_arguments.root).rebuild_runtime()
            print(
                f"Rebuilt runtime from {rebuild_result.source_count} source, "
                f"{rebuild_result.insight_count} insights, "
                f"{rebuild_result.cognition_count} cognitions, and "
                f"{rebuild_result.supersession_count} supersession relationships."
            )
            return 0
        if parsed_arguments.command == "evaluate-recall":
            report = evaluate_recall(load_recall_dataset(parsed_arguments.dataset))
            rendered_report = (
                report_as_json(report)
                if parsed_arguments.format == "json"
                else report_as_text(report)
            )
            print(rendered_report, end="")
            return 1 if report_has_failures(report) else 0
    except UserInputError as error:
        input_name = (
            "evaluation dataset"
            if parsed_arguments.command == "evaluate-recall"
            else "source"
        )
        print(f"Invalid {input_name}: {error}", file=sys.stderr)
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
        operation = {
            "capture": "Capture",
            "remember": "Memory capture",
            "recall": "Memory recall",
            "evaluate-recall": "Evaluation",
        }.get(parsed_arguments.command, "Initialization")
        print(f"{operation} failed: {error}", file=sys.stderr)
        return EXIT_IO
    return EXIT_USER
