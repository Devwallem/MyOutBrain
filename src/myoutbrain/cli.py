from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import shutil
import sys

from myoutbrain.answering import (
    AnswerRequest,
    CompanionAnswer,
    CompanionAnswerService,
    FreshnessRequirement,
    RiskLevel,
)
from myoutbrain.cognitive_audit import CognitiveAuditService
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
from myoutbrain.legacy_migration import MigrationSummary, V1PermanentKnowledgeMigrator
from myoutbrain.knowledge_views import KnowledgeViewService
from myoutbrain.generation import ProviderFailure
from myoutbrain.local_core import (
    CanonicalMemoryAudit,
    IntegrationProposal,
    LocalMemoryCore,
    MemoryDeletionImpact,
)
from myoutbrain.memory_gateway import (
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)
from myoutbrain.memory_governance import MemoryGovernanceService


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
    recall_parser.add_argument(
        "--query-sensitivity",
        choices=("local-only", "cloud-allowed"),
        default="local-only",
        help="Explicitly classify whether the query itself may leave this machine",
    )
    recall_parser.add_argument("--format", choices=("json", "text"), default="text")
    answer_parser = subcommands.add_parser(
        "answer",
        help="Answer from common knowledge with sanitized public-research fallback",
    )
    answer_parser.add_argument("question")
    answer_parser.add_argument("--root", type=Path, default=Path.cwd())
    answer_parser.add_argument("--task", required=True)
    answer_parser.add_argument(
        "--access",
        choices=tuple(level.value for level in MemoryAccess),
        default=MemoryAccess.TASK_SCOPED.value,
    )
    answer_parser.add_argument("--memory-id", action="append", default=[])
    answer_parser.add_argument("--source-id", action="append", default=[])
    answer_parser.add_argument("--limit", type=int, default=5)
    answer_parser.add_argument("--high-risk", action="store_true")
    answer_parser.add_argument("--time-sensitive", action="store_true")
    answer_parser.add_argument(
        "--risk-level",
        choices=("unclassified", "standard", "high-risk"),
        default="unclassified",
        help="Trusted risk classification; unclassified requires public verification",
    )
    answer_parser.add_argument(
        "--freshness",
        choices=("unclassified", "stable", "time-sensitive"),
        default="unclassified",
        help="Trusted freshness classification; unclassified requires current evidence",
    )
    answer_parser.add_argument(
        "--public-query",
        help="Explicit public-safe query; private context must be removed before use",
    )
    answer_parser.add_argument("--allow-cloud", action="store_true")
    answer_parser.add_argument(
        "--query-sensitivity",
        choices=("local-only", "cloud-allowed"),
        default="local-only",
    )
    answer_parser.add_argument("--format", choices=("json", "text"), default="text")
    consolidate_parser = subcommands.add_parser(
        "consolidate",
        help="Manually prepare buffered memory for natural review",
    )
    consolidate_parser.add_argument("--root", type=Path, default=Path.cwd())
    consolidate_parser.add_argument("--task", required=True)
    consolidate_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    memory_review_parser = subcommands.add_parser(
        "review-memory",
        help="List or naturally review memory integration proposals",
    )
    memory_review_parser.add_argument("proposal_id", nargs="?")
    memory_review_parser.add_argument("instruction", nargs="?")
    memory_review_parser.add_argument("--history", action="store_true")
    memory_review_parser.add_argument("--root", type=Path, default=Path.cwd())
    memory_review_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    why_memory_parser = subcommands.add_parser(
        "why-memory",
        help="Explain a canonical memory's current evidence and evolution",
    )
    why_memory_parser.add_argument("memory_id")
    why_memory_parser.add_argument("--root", type=Path, default=Path.cwd())
    why_memory_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    audit_memory_parser = subcommands.add_parser(
        "audit-memory",
        help="Naturally query canonical understanding, sources, and evolution",
    )
    audit_memory_parser.add_argument("query")
    audit_memory_parser.add_argument("--root", type=Path, default=Path.cwd())
    audit_memory_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    forget_memory_parser = subcommands.add_parser(
        "forget-memory",
        help="Naturally deactivate or restore one canonical memory",
    )
    forget_memory_parser.add_argument("memory_id")
    forget_memory_parser.add_argument("instruction")
    forget_memory_parser.add_argument("--root", type=Path, default=Path.cwd())
    forget_memory_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    delete_memory_parser = subcommands.add_parser(
        "delete-memory",
        help="Preview or explicitly confirm permanent deletion of one memory",
    )
    delete_memory_parser.add_argument("memory_id")
    delete_memory_parser.add_argument("--confirm")
    delete_memory_parser.add_argument("--root", type=Path, default=Path.cwd())
    delete_memory_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    migrate_parser = subcommands.add_parser(
        "migrate-v1",
        help="Migrate validated V1 permanent knowledge into canonical memory",
    )
    migrate_parser.add_argument("--root", type=Path, default=Path.cwd())
    migrate_parser.add_argument("--format", choices=("json", "text"), default="text")
    migration_status_parser = subcommands.add_parser(
        "migration-status",
        help="Show V1 permanent-knowledge migration status and audit counts",
    )
    migration_status_parser.add_argument("--root", type=Path, default=Path.cwd())
    migration_status_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    build_views_parser = subcommands.add_parser(
        "build-views",
        help="Generate disposable Obsidian views from canonical memory",
    )
    build_views_parser.add_argument("--root", type=Path, default=Path.cwd())
    build_views_parser.add_argument("--open", action="store_true")
    build_views_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
    )
    sync_views_parser = subcommands.add_parser(
        "sync-view-edits",
        help="Submit edited generated views as buffered evidence and proposals",
    )
    sync_views_parser.add_argument("--root", type=Path, default=Path.cwd())
    sync_views_parser.add_argument(
        "--format", choices=("json", "text"), default="text"
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
    query_sensitivity: Sensitivity,
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
            query_sensitivity=query_sensitivity,
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


def _answer(
    root: Path,
    question: str,
    *,
    task: str,
    access: str,
    memory_ids: Sequence[str],
    source_ids: Sequence[str],
    limit: int,
    high_risk: bool,
    time_sensitive: bool,
    risk_level: RiskLevel,
    freshness: FreshnessRequirement,
    public_query: str | None,
    allow_cloud: bool,
    query_sensitivity: Sensitivity,
    output_format: str,
) -> int:
    result = CompanionAnswerService(root).answer(
        AnswerRequest(
            question=question,
            task=task,
            access=MemoryAccess(access),
            memory_ids=tuple(memory_ids),
            source_ids=tuple(source_ids),
            limit=limit,
            risk_level="high-risk" if high_risk else risk_level,
            freshness="time-sensitive" if time_sensitive else freshness,
            public_query=public_query,
            allow_cloud=allow_cloud,
            query_sensitivity=query_sensitivity,
        )
    )
    if output_format == "json":
        print(json.dumps(result.to_data(), ensure_ascii=False, sort_keys=True))
        return 0
    if result.status == "unknown":
        print("The answer remains unknown.")
        for fact in result.verified_facts:
            print(f"Verified: {fact}")
        _print_public_sources(result)
        for gap in result.unresolved_gaps:
            print(f"Unresolved: {gap}")
        for step in result.next_steps:
            print(f"Next: {step}")
        return 0
    for claim in result.claims:
        print(f"Companion inference: {claim.text}")
        print(
            f"Evidence origin ({', '.join(claim.evidence_origins)}): "
            f"{', '.join(claim.source_ids)}"
        )
    _print_public_sources(result)
    if result.companion_inference is not None:
        print(f"Inference: {result.companion_inference}")
    return 0


def _print_public_sources(result: CompanionAnswer) -> None:
    for source in result.public_sources:
        print(
            f"Public source: {source.title} — {source.url} "
            f"(published {source.published_at}; retrieved {source.retrieved_at})"
        )


def _render_migration_summary(
    summary: MigrationSummary,
    *,
    output_format: str,
) -> int:
    if output_format == "json":
        print(json.dumps(summary.to_data(), ensure_ascii=False, sort_keys=True))
        return 0
    if summary.status == "not-started":
        print("V1 permanent-knowledge migration has not started.")
        return 0
    disposition = (
        "already complete"
        if summary.disposition == "already-complete"
        else "complete"
    )
    print(f"V1 permanent-knowledge migration is {disposition}.")
    print(
        f"Migrated {summary.source_count} sources, {summary.insight_count} insights, "
        f"{summary.cognition_count} cognitions, and {summary.event_count} audit events."
    )
    print(f"Source fingerprint: {summary.source_fingerprint}")
    return 0


def _render_integration_proposals(
    proposals: Sequence[IntegrationProposal],
    *,
    output_format: str,
) -> int:
    proposal_data = [proposal.to_data() for proposal in proposals]
    if output_format == "json":
        print(json.dumps({"proposals": proposal_data}, ensure_ascii=False, sort_keys=True))
        return 0
    if not proposal_data:
        print("No memory integration proposals are pending.")
        return 0
    for proposal in proposal_data:
        print(f"Proposal: {proposal['proposal_id']}")
        print(f"Topic: {proposal['topic']}")
        print(f"Proposed understanding: {proposal['proposed_understanding']}")
        print(f"Possible impact: {proposal['possible_impact']}")
        print(f"Suggested action: {proposal['suggested_action']}")
        print(f"Target memory: {proposal['target_memory_id'] or 'none'}")
        evidence = proposal["evidence_memory_ids"]
        if not isinstance(evidence, list) or not all(
            isinstance(memory_id, str) for memory_id in evidence
        ):
            raise IntegrityError("integration proposal has invalid evidence")
        print("Evidence: " + (", ".join(evidence) if evidence else "none"))
        source_scope = proposal["source_scope"]
        if not isinstance(source_scope, list) or not all(
            isinstance(source, str) for source in source_scope
        ):
            raise IntegrityError("integration proposal has invalid source scope")
        print("Sources: " + ", ".join(source_scope))
        related = proposal["related_canonical_memory_ids"]
        if not isinstance(related, list) or not all(
            isinstance(memory_id, str) for memory_id in related
        ):
            raise IntegrityError("integration proposal has invalid related memory")
        print(
            "Related canonical memories: "
            + (", ".join(related) if related else "none")
        )
    return 0


def _consolidate(root: Path, task: str, output_format: str) -> int:
    proposals = LocalMemoryCore(root).propose_manual_consolidation(task)
    return _render_integration_proposals(proposals, output_format=output_format)


def _review_memory(
    root: Path,
    proposal_id: str | None,
    instruction: str | None,
    history: bool,
    output_format: str,
) -> int:
    if history:
        if proposal_id is not None or instruction is not None:
            raise UserInputError(
                "review-memory --history does not accept a proposal instruction"
            )
        reviews = LocalMemoryCore(root).integration_review_history()
        if output_format == "json":
            print(
                json.dumps(
                    {"reviews": [review.to_data() for review in reviews]},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            if not reviews:
                print("No memory integration reviews have been recorded.")
            for review in reviews:
                print(f"{review.proposal_id}: {review.decision}")
        return 0
    if proposal_id is not None or instruction is not None:
        if proposal_id is None or instruction is None:
            raise UserInputError(
                "review-memory requires both a proposal id and natural instruction"
            )
        result = LocalMemoryCore(root).review_integration_proposal(
            proposal_id,
            instruction,
        )
        if output_format == "json":
            print(json.dumps(result.to_data(), ensure_ascii=False, sort_keys=True))
        else:
            print(f"Integration proposal {result.proposal_id}: {result.decision}")
            if result.canonical_memory_id is not None:
                print(f"Canonical memory: {result.canonical_memory_id}")
        return 0
    proposals = LocalMemoryCore(root).pending_integration_proposals()
    return _render_integration_proposals(proposals, output_format=output_format)


def _why_memory(root: Path, memory_id: str, output_format: str) -> int:
    audit = LocalMemoryCore(root).explain_canonical_memory(memory_id)
    if output_format == "json":
        print(json.dumps(audit.to_data(), ensure_ascii=False, sort_keys=True))
        return 0
    _render_canonical_audit(audit)
    return 0


def _render_canonical_audit(audit: CanonicalMemoryAudit) -> None:
    print(f"Memory: {audit.memory_id}")
    print(f"State: {audit.state}")
    print(f"Confirmation: {audit.confirmation_status}")
    print(f"Current version: {audit.current_version}")
    print(f"Current understanding: {audit.current_content}")
    print("Key sources: " + ", ".join(audit.current_source_ids))
    if audit.unresolved_conflicts:
        print("Unresolved conflicts:")
        for conflict in audit.unresolved_conflicts:
            print(f"- {conflict.memory_id}: {conflict.content} ({conflict.reason})")
    print("Evolution:")
    for version in audit.versions:
        print(f"- v{version.version} {version.status}: {version.content}")
        if version.supersession_reason is not None:
            print(f"  Replaced because: {version.supersession_reason}")


def _audit_memory(root: Path, query: str, output_format: str) -> int:
    result = CognitiveAuditService(root).query(query)
    if output_format == "json":
        print(json.dumps(result.to_data(), ensure_ascii=False, sort_keys=True))
        return 0
    print(f"Cognitive audit: {result.query}")
    if not result.audits:
        print("No canonical understanding matched this question.")
        return 0
    for index, audit in enumerate(result.audits):
        if index:
            print()
        _render_canonical_audit(audit)
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
                query_sensitivity=parsed_arguments.query_sensitivity,
                output_format=parsed_arguments.format,
            )
        if parsed_arguments.command == "answer":
            return _answer(
                parsed_arguments.root,
                parsed_arguments.question,
                task=parsed_arguments.task,
                access=parsed_arguments.access,
                memory_ids=parsed_arguments.memory_id,
                source_ids=parsed_arguments.source_id,
                limit=parsed_arguments.limit,
                high_risk=parsed_arguments.high_risk,
                time_sensitive=parsed_arguments.time_sensitive,
                risk_level=parsed_arguments.risk_level,
                freshness=parsed_arguments.freshness,
                public_query=parsed_arguments.public_query,
                allow_cloud=parsed_arguments.allow_cloud,
                query_sensitivity=parsed_arguments.query_sensitivity,
                output_format=parsed_arguments.format,
            )
        if parsed_arguments.command == "consolidate":
            return _consolidate(
                parsed_arguments.root,
                parsed_arguments.task,
                parsed_arguments.format,
            )
        if parsed_arguments.command == "review-memory":
            return _review_memory(
                parsed_arguments.root,
                parsed_arguments.proposal_id,
                parsed_arguments.instruction,
                parsed_arguments.history,
                parsed_arguments.format,
            )
        if parsed_arguments.command == "why-memory":
            return _why_memory(
                parsed_arguments.root,
                parsed_arguments.memory_id,
                parsed_arguments.format,
            )
        if parsed_arguments.command == "audit-memory":
            return _audit_memory(
                parsed_arguments.root,
                parsed_arguments.query,
                parsed_arguments.format,
            )
        if parsed_arguments.command == "forget-memory":
            state_change = MemoryGovernanceService(
                parsed_arguments.root
            ).forget(
                parsed_arguments.memory_id,
                parsed_arguments.instruction,
            )
            if parsed_arguments.format == "json":
                print(
                    json.dumps(
                        state_change.to_data(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    f"Canonical memory {state_change.memory_id}: "
                    f"{state_change.action}."
                )
            return 0
        if parsed_arguments.command == "delete-memory":
            deletion = MemoryGovernanceService(
                parsed_arguments.root
            ).delete(
                parsed_arguments.memory_id,
                confirmation=parsed_arguments.confirm,
            )
            if parsed_arguments.format == "json":
                print(
                    json.dumps(
                        deletion.to_data(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            elif isinstance(deletion, MemoryDeletionImpact):
                print(f"Permanent deletion preview for {deletion.memory_id}:")
                print(f"Sources: {', '.join(deletion.source_ids) or 'none'}")
                print(
                    "Shared sources retained: "
                    + (", ".join(deletion.shared_source_ids) or "none")
                )
                print(f"Confirm with --confirm {deletion.confirmation_token}")
            else:
                print(
                    f"Permanently deleted {deletion.memory_id}; removed "
                    f"{len(deletion.removed_source_ids)} unshared source(s)."
                )
            return 0
        if parsed_arguments.command == "migrate-v1":
            return _render_migration_summary(
                V1PermanentKnowledgeMigrator(parsed_arguments.root).migrate(),
                output_format=parsed_arguments.format,
            )
        if parsed_arguments.command == "migration-status":
            return _render_migration_summary(
                V1PermanentKnowledgeMigrator(parsed_arguments.root).status(),
                output_format=parsed_arguments.format,
            )
        if parsed_arguments.command == "build-views":
            view_build = KnowledgeViewService(parsed_arguments.root).rebuild(
                open_index=parsed_arguments.open
            )
            if parsed_arguments.format == "json":
                print(
                    json.dumps(
                        view_build.to_data(), ensure_ascii=False, sort_keys=True
                    )
                )
            else:
                print(
                    f"Generated {len(view_build.view_paths)} knowledge views at "
                    f"{view_build.index_path}."
                )
                if view_build.obsidian_warning is not None:
                    print(
                        f"Warning: {view_build.obsidian_warning}", file=sys.stderr
                    )
            return 0
        if parsed_arguments.command == "sync-view-edits":
            view_sync = KnowledgeViewService(parsed_arguments.root).sync_edits()
            if parsed_arguments.format == "json":
                print(
                    json.dumps(
                        view_sync.to_data(), ensure_ascii=False, sort_keys=True
                    )
                )
            else:
                print(f"Submitted {len(view_sync.edits)} edited knowledge views.")
                for edit in view_sync.edits:
                    print(
                        f"{edit.memory_id}: buffered {edit.digest_id}; proposals "
                        + (", ".join(edit.proposal_ids) or "none")
                    )
            return 0
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
