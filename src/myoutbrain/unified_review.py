from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Literal, cast
import uuid

from myoutbrain.core_types import IdempotencyConflict, IntegrityError, UserInputError


ReviewIntent = Literal["derive", "integrate", "archive", "research"]
ProposalFormation = Literal["explicit", "derived", "hypothesis"]
ProposalPriority = Literal["blocking", "priority", "routine"]
ReviewDecisionKind = Literal["approve", "approve-edited", "reject", "defer"]
ApprovalEffectType = Literal[
    "create_derived_memory",
    "create_canonical_memory",
    "create_source_backed_canonical_memory",
    "revise_canonical_memory",
    "create_human_archive",
    "create_research_thread",
]

REVIEW_PAYLOAD_SCHEMA_VERSION = 1
AVAILABLE_DECISIONS: tuple[ReviewDecisionKind, ...] = (
    "approve",
    "approve-edited",
    "reject",
    "defer",
)

CONFLICT_AVAILABLE_DECISIONS: tuple[ReviewDecisionKind, ...] = ("reject", "defer")
CONFLICT_APPROVAL_UNAVAILABLE_REASON = (
    "conflict approval materialization is deferred to issue 09"
)
CONFLICT_APPROVAL_ERROR = "conflict_approval_materialization_deferred_to_issue_09"
UNIFIED_REVIEW_SCHEMA = """

CREATE TABLE IF NOT EXISTS review_proposals (
    proposal_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    proposal_version INTEGER NOT NULL CHECK (proposal_version >= 1),
    group_id TEXT,
    title TEXT NOT NULL,
    content TEXT,
    intent TEXT NOT NULL CHECK (intent IN ('derive', 'integrate', 'archive', 'research')),
    formation TEXT NOT NULL CHECK (formation IN ('explicit', 'derived', 'hypothesis')),
    priority TEXT NOT NULL CHECK (priority IN ('blocking', 'priority', 'routine')),
    applicability_scope TEXT NOT NULL,
    approval_effect_json TEXT NOT NULL,
    target_json TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    opposing_evidence_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    context_coverage_json TEXT NOT NULL,
    blind_spots_json TEXT NOT NULL,
    near_proposal_ids_json TEXT NOT NULL,
    conflict_proposal_ids_json TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    evidence_retention TEXT NOT NULL CHECK (evidence_retention IN ('full', 'excerpt', 'receipt')),
    migration_restrictions_json TEXT NOT NULL,
    exact_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'deferred', 'rejected', 'applying', 'applied', 'expired')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deferred_until TEXT,
    expired_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS review_proposals_queue
ON review_proposals(status, priority, created_at, proposal_id);

CREATE TABLE IF NOT EXISTS review_proposal_submissions (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES review_proposals(proposal_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_groups (
    group_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('near', 'conflict', 'mixed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_group_members (
    group_id TEXT NOT NULL REFERENCES review_groups(group_id),
    proposal_id TEXT NOT NULL UNIQUE REFERENCES review_proposals(proposal_id),
    PRIMARY KEY (group_id, proposal_id)
);

CREATE TABLE IF NOT EXISTS review_proposal_relations (
    first_proposal_id TEXT NOT NULL REFERENCES review_proposals(proposal_id),
    second_proposal_id TEXT NOT NULL REFERENCES review_proposals(proposal_id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('near', 'conflict')),
    CHECK (first_proposal_id < second_proposal_id),
    PRIMARY KEY (first_proposal_id, second_proposal_id, relation_type)
);

CREATE TABLE IF NOT EXISTS review_batches (
    batch_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_batch_items (
    batch_id TEXT NOT NULL REFERENCES review_batches(batch_id),
    proposal_id TEXT NOT NULL REFERENCES review_proposals(proposal_id),
    proposal_version INTEGER NOT NULL,
    decision TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, proposal_id)
);

CREATE TABLE IF NOT EXISTS review_materializations (
    proposal_id TEXT PRIMARY KEY REFERENCES review_proposals(proposal_id),
    artifact_kind TEXT NOT NULL CHECK (
        artifact_kind IN ('canonical-memory', 'human-archive', 'research-thread')
    ),
    artifact_id TEXT NOT NULL,
    authorship TEXT NOT NULL,
    personal_cognition INTEGER NOT NULL CHECK (personal_cognition IN (0, 1)),
    final_content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_memory_review_provenance (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES review_proposals(proposal_id),
    FOREIGN KEY (memory_id, version)
        REFERENCES canonical_memory_versions(memory_id, version),
    PRIMARY KEY (memory_id, version, proposal_id)
);

CREATE TABLE IF NOT EXISTS human_archives (
    archive_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    applicability_scope TEXT NOT NULL,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES review_proposals(proposal_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_threads (
    research_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    applicability_scope TEXT NOT NULL,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES review_proposals(proposal_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_expirations (
    proposal_id TEXT PRIMARY KEY REFERENCES review_proposals(proposal_id),
    exact_fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    formation TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    expired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_proposal_recurrences (
    proposal_id TEXT PRIMARY KEY REFERENCES review_proposals(proposal_id),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count >= 1),
    last_seen_at TEXT NOT NULL,
    last_had_new_evidence INTEGER NOT NULL CHECK (last_had_new_evidence IN (0, 1))
);
"""


@dataclass(frozen=True)
class ApprovalEffect:
    effect_type: ApprovalEffectType
    canonical_name: str | None
    personal_cognition: bool

    @classmethod
    def from_data(
        cls,
        data: object,
        *,
        intent: ReviewIntent,
        formation: ProposalFormation,
    ) -> ApprovalEffect:
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            raise UserInputError("review proposal approval_effect must be a JSON object")
        effect = cast(dict[str, object], data)
        effect_type = effect.get("type")
        allowed_effects: dict[ReviewIntent, set[str]] = {
            "derive": {"create_derived_memory"},
            "integrate": {
                "create_canonical_memory",
                "create_source_backed_canonical_memory",
                "revise_canonical_memory",
            },
            "archive": {"create_human_archive"},
            "research": {"create_research_thread"},
        }
        if not isinstance(effect_type, str) or effect_type not in allowed_effects[intent]:
            raise UserInputError(
                f"review proposal approval effect does not match {intent} intent"
            )
        personal_cognition = effect.get("personal_cognition")
        if not isinstance(personal_cognition, bool):
            raise UserInputError(
                "review proposal approval_effect.personal_cognition must be boolean"
            )
        canonical_name = effect.get("canonical_name")
        if canonical_name is not None and (
            not isinstance(canonical_name, str)
            or not canonical_name.strip()
            or len(canonical_name) > 500
        ):
            raise UserInputError(
                "review proposal approval_effect.canonical_name must contain "
                "1 to 500 characters or be null"
            )
        if intent in ("derive", "integrate") and canonical_name is None:
            raise UserInputError(
                "canonical-memory approval effect requires canonical_name"
            )
        if formation == "hypothesis" and intent != "research":
            raise UserInputError(
                "hypothesis proposal must use research intent before verification"
            )
        if personal_cognition and (intent != "integrate" or formation != "explicit"):
            raise UserInputError(
                "personal cognition approval effect requires explicit integration"
            )
        return cls(
            effect_type=cast(ApprovalEffectType, effect_type),
            canonical_name=(
                canonical_name.strip() if isinstance(canonical_name, str) else None
            ),
            personal_cognition=personal_cognition,
        )

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "type": self.effect_type,
            "personal_cognition": self.personal_cognition,
        }
        if self.canonical_name is not None:
            data["canonical_name"] = self.canonical_name
        return data


@dataclass(frozen=True)
class ProposalTarget:
    memory_id: str | None
    expected_version: int

    @classmethod
    def from_data(cls, data: object) -> ProposalTarget:
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            raise UserInputError("review proposal target must be a JSON object")
        target = cast(dict[str, object], data)
        memory_id = target.get("memory_id")
        if memory_id is not None and not isinstance(memory_id, str):
            raise UserInputError("review proposal target.memory_id must be text or null")
        expected_version = target.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 0:
            raise UserInputError(
                "review proposal target.expected_version must be a non-negative integer"
            )
        return cls(memory_id=memory_id, expected_version=expected_version)

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "expected_version": self.expected_version,
        }


@dataclass(frozen=True)
class ReviewProposalInput:
    title: str
    content: str
    intent: ReviewIntent
    formation: ProposalFormation
    priority: ProposalPriority
    applicability_scope: str
    approval_effect: ApprovalEffect
    target: ProposalTarget
    supporting_evidence: tuple[dict[str, object], ...]
    opposing_evidence: tuple[dict[str, object], ...]
    dependencies: tuple[str, ...]
    context_coverage: tuple[str, ...]
    blind_spots: tuple[str, ...]
    near_proposal_ids: tuple[str, ...]
    conflict_proposal_ids: tuple[str, ...]
    sensitivity: Literal["local-only", "cloud-allowed"]
    evidence_retention: Literal["full", "excerpt", "receipt"]
    migration_restrictions: tuple[str, ...]

    @classmethod
    def from_data(cls, data: object) -> ReviewProposalInput:
        if not isinstance(data, dict):
            raise UserInputError("review proposal payload must be a JSON object")
        payload = cast(dict[object, object], data)
        intent = _choice(payload, "intent", ("derive", "integrate", "archive", "research"))
        formation = _choice(payload, "formation", ("explicit", "derived", "hypothesis"))
        priority = _choice(payload, "priority", ("blocking", "priority", "routine"))
        sensitivity = _choice(payload, "sensitivity", ("local-only", "cloud-allowed"))
        retention = _choice(payload, "evidence_retention", ("full", "excerpt", "receipt"))
        normalized_intent = cast(ReviewIntent, intent)
        normalized_formation = cast(ProposalFormation, formation)
        approval_effect = ApprovalEffect.from_data(
            payload.get("approval_effect"),
            intent=normalized_intent,
            formation=normalized_formation,
        )
        target = ProposalTarget.from_data(payload.get("target"))
        supporting_evidence = _object_list(payload, "supporting_evidence")
        if not supporting_evidence:
            raise UserInputError("review proposal requires supporting evidence")
        return cls(
            title=_text(payload, "title"),
            content=_text(payload, "content"),
            intent=normalized_intent,
            formation=normalized_formation,
            priority=cast(ProposalPriority, priority),
            applicability_scope=_text(payload, "applicability_scope"),
            approval_effect=approval_effect,
            target=target,
            supporting_evidence=supporting_evidence,
            opposing_evidence=_object_list(payload, "opposing_evidence"),
            dependencies=_text_list(payload, "dependencies"),
            context_coverage=_text_list(payload, "context_coverage"),
            blind_spots=_text_list(payload, "blind_spots"),
            near_proposal_ids=_text_list(payload, "near_proposal_ids"),
            conflict_proposal_ids=_text_list(payload, "conflict_proposal_ids"),
            sensitivity=cast(Literal["local-only", "cloud-allowed"], sensitivity),
            evidence_retention=cast(Literal["full", "excerpt", "receipt"], retention),
            migration_restrictions=_text_list(payload, "migration_restrictions"),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "title": self.title,
            "content": self.content,
            "intent": self.intent,
            "formation": self.formation,
            "priority": self.priority,
            "applicability_scope": self.applicability_scope,
            "approval_effect": self.approval_effect.to_data(),
            "target": self.target.to_data(),
            "supporting_evidence": list(self.supporting_evidence),
            "opposing_evidence": list(self.opposing_evidence),
            "dependencies": list(self.dependencies),
            "context_coverage": list(self.context_coverage),
            "blind_spots": list(self.blind_spots),
            "near_proposal_ids": list(self.near_proposal_ids),
            "conflict_proposal_ids": list(self.conflict_proposal_ids),
            "sensitivity": self.sensitivity,
            "evidence_retention": self.evidence_retention,
            "migration_restrictions": list(self.migration_restrictions),
        }


@dataclass(frozen=True)
class ReviewProposal:
    proposal_id: str
    proposal_version: int
    group_id: str | None
    payload: ReviewProposalInput
    status: str
    created_at: str
    deferred_until: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    available_decisions: tuple[ReviewDecisionKind, ...] = AVAILABLE_DECISIONS
    approval_unavailable_reason: str | None = None

    def to_data(self) -> dict[str, object]:
        payload_data = self.payload.to_data()
        if self.approval_unavailable_reason is not None:
            payload_data["approval_effect"] = None
        data: dict[str, object] = {
            "schema_version": REVIEW_PAYLOAD_SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "proposal_version": self.proposal_version,
            "group_id": self.group_id,
            "status": self.status,
            **payload_data,
            "available_decisions": list(self.available_decisions),
            "created_at": self.created_at,
            "deferred_until": self.deferred_until,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
        }

        if self.approval_unavailable_reason is not None:
            data["approval_unavailable_reason"] = self.approval_unavailable_reason
        return data

@dataclass(frozen=True)
class ReviewProposalSubmission:
    proposal: ReviewProposal
    deduplicated: bool

    def to_data(self) -> dict[str, object]:
        return {
            "deduplicated": self.deduplicated,
            "proposal": self.proposal.to_data(),
        }


@dataclass(frozen=True)
class ReviewQueue:
    proposals: tuple[ReviewProposal, ...]
    groups: tuple[ReviewGroup, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_PAYLOAD_SCHEMA_VERSION,
            "groups": [group.to_data() for group in self.groups],
            "proposals": [proposal.to_data() for proposal in self.proposals],
        }


@dataclass(frozen=True)
class ReviewGroup:
    group_id: str
    kind: str
    proposal_ids: tuple[str, ...]
    relations: tuple[tuple[str, str, str], ...]

    def to_data(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "kind": self.kind,
            "proposal_ids": list(self.proposal_ids),
            "relations": [
                {"type": relation_type, "proposal_ids": [first_id, second_id]}
                for first_id, second_id, relation_type in self.relations
            ],
        }


@dataclass(frozen=True)
class ReviewDecision:
    proposal_id: str
    proposal_version: int
    decision: ReviewDecisionKind
    edited_content: str | None
    reason: str | None
    defer_until: str | None
    confirm_personal_cognition: bool

    @classmethod
    def from_data(cls, data: object) -> ReviewDecision:
        if not isinstance(data, dict):
            raise UserInputError("review batch decisions must be JSON objects")
        item = cast(dict[object, object], data)
        proposal_id = _text(item, "proposal_id")
        proposal_version = item.get("proposal_version")
        if not isinstance(proposal_version, int) or proposal_version < 1:
            raise UserInputError(
                "review batch proposal_version must be a positive integer"
            )
        decision = _choice(item, "decision", AVAILABLE_DECISIONS)
        edited_content = _optional_text(item, "edited_content")
        reason = _optional_text(item, "reason")
        defer_until = _optional_text(item, "defer_until")
        confirmation = item.get("confirm_personal_cognition", False)
        if not isinstance(confirmation, bool):
            raise UserInputError(
                "review batch confirm_personal_cognition must be boolean"
            )
        if decision in ("reject", "defer") and edited_content is not None:
            raise UserInputError("only approval may include edited_content")
        if decision == "approve-edited" and edited_content is None:
            raise UserInputError("approve-edited decision requires edited_content")
        if decision == "defer" and defer_until is None:
            raise UserInputError("deferred review decision requires defer_until")
        return cls(
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            decision=cast(ReviewDecisionKind, decision),
            edited_content=edited_content,
            reason=reason,
            defer_until=defer_until,
            confirm_personal_cognition=confirmation,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_version": self.proposal_version,
            "decision": self.decision,
            "edited_content": self.edited_content,
            "reason": self.reason,
            "defer_until": self.defer_until,
            "confirm_personal_cognition": self.confirm_personal_cognition,
        }


@dataclass(frozen=True)
class ReviewBatchRequest:
    batch_id: str
    decisions: tuple[ReviewDecision, ...]

    @classmethod
    def from_data(cls, data: object) -> ReviewBatchRequest:
        if not isinstance(data, dict):
            raise UserInputError("review batch must be a JSON object")
        payload = cast(dict[object, object], data)
        batch_id = _text(payload, "batch_id")
        raw_decisions = payload.get("decisions")
        if not isinstance(raw_decisions, list) or not raw_decisions:
            raise UserInputError("review batch decisions must be a non-empty array")
        decisions = tuple(
            ReviewDecision.from_data(item) for item in cast(list[object], raw_decisions)
        )
        proposal_ids = [decision.proposal_id for decision in decisions]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise UserInputError("review batch contains a duplicate proposal decision")
        return cls(batch_id=batch_id, decisions=decisions)

    def to_data(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "decisions": [decision.to_data() for decision in self.decisions],
        }


@dataclass(frozen=True)
class ReviewBatchResult:
    data: dict[str, object]

    def to_data(self) -> dict[str, object]:
        return self.data


@dataclass(frozen=True)
class ReviewExpirationResult:
    retention_days: int
    as_of: str
    reactivated: tuple[str, ...]
    expired: tuple[dict[str, object], ...]

    def to_data(self) -> dict[str, object]:
        return {
            "retention_days": self.retention_days,
            "as_of": self.as_of,
            "reactivated": list(self.reactivated),
            "expired": list(self.expired),
        }


def load_review_proposal(path: Path) -> ReviewProposalInput:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UserInputError(f"cannot read review proposal payload: {path}") from error
    return ReviewProposalInput.from_data(data)


def load_review_batch(path: Path) -> ReviewBatchRequest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UserInputError(f"cannot read review batch: {path}") from error
    return ReviewBatchRequest.from_data(data)


def stage_review_proposal(
    database_path: Path,
    payload: ReviewProposalInput,
    *,
    idempotency_key: str,
) -> tuple[bytes, ReviewProposalSubmission]:
    normalized_key = idempotency_key.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise UserInputError("idempotency key must contain 1 to 200 characters")
    request_hash = _stable_hash(payload.to_data())
    temporary_path = _copy_database(database_path, ".review-proposal.")
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            existing = connection.execute(
                """
                SELECT submission.request_hash, proposal.*
                FROM review_proposal_submissions AS submission
                JOIN review_proposals AS proposal
                  ON proposal.proposal_id = submission.proposal_id
                WHERE submission.idempotency_key = ?
                """,
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if existing[0] != request_hash:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                return temporary_path.read_bytes(), ReviewProposalSubmission(
                    proposal=_proposal_from_row(existing[1:]),
                    deduplicated=False,
                )
            exact_fingerprint = _stable_hash(
                {
                    "content": " ".join(payload.content.casefold().split()),
                    "scope": " ".join(payload.applicability_scope.casefold().split()),
                    "approval_effect": payload.approval_effect.to_data(),
                    "target": payload.target.to_data(),
                    "intent": payload.intent,
                    "formation": payload.formation,
                }
            )
            duplicate_row = connection.execute(
                """
                SELECT * FROM review_proposals
                WHERE exact_fingerprint = ?
                  AND status IN ('pending', 'deferred')
                ORDER BY created_at, proposal_id
                LIMIT 1
                """,
                (exact_fingerprint,),
            ).fetchone()
            created_at = datetime.now(timezone.utc).isoformat()
            if duplicate_row is not None:
                duplicate = _merge_duplicate_evidence(
                    connection,
                    _proposal_from_row(duplicate_row),
                    payload,
                    updated_at=created_at,
                )
                near_ids = tuple(
                    related_id
                    for related_id in payload.near_proposal_ids
                    if related_id != duplicate.proposal_id
                )
                conflict_ids = tuple(
                    related_id
                    for related_id in payload.conflict_proposal_ids
                    if related_id != duplicate.proposal_id
                )
                if near_ids or conflict_ids:
                    relation_payload = replace(
                        payload,
                        near_proposal_ids=near_ids,
                        conflict_proposal_ids=conflict_ids,
                    )
                    _validate_relation_targets(
                        connection,
                        duplicate.proposal_id,
                        relation_payload,
                    )
                    _group_proposal_relations(
                        connection,
                        proposal_id=duplicate.proposal_id,
                        near_proposal_ids=near_ids,
                        conflict_proposal_ids=conflict_ids,
                        created_at=created_at,
                    )
                    for related_id, relation_type in (
                        tuple((related_id, "near") for related_id in near_ids)
                        + tuple(
                            (related_id, "conflict")
                            for related_id in conflict_ids
                        )
                    ):
                        _append_relation_to_existing_proposal(
                            connection,
                            proposal_id=duplicate.proposal_id,
                            related_id=related_id,
                            relation_type=relation_type,
                            updated_at=created_at,
                        )
                connection.execute(
                    """
                    INSERT INTO review_proposal_submissions
                        (idempotency_key, request_hash, proposal_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_key, request_hash, duplicate.proposal_id, created_at),
                )
                connection.commit()
                refreshed = connection.execute(
                    "SELECT * FROM review_proposals WHERE proposal_id = ?",
                    (duplicate.proposal_id,),
                ).fetchone()
                if refreshed is None:
                    raise IntegrityError("deduplicated review proposal disappeared")
                return temporary_path.read_bytes(), ReviewProposalSubmission(
                    proposal=_proposal_from_row(refreshed),
                    deduplicated=True,
                )
            terminal_row = connection.execute(
                """
                SELECT * FROM review_proposals
                WHERE exact_fingerprint = ?
                  AND status IN ('rejected', 'expired')
                ORDER BY updated_at DESC, proposal_id
                LIMIT 1
                """,
                (exact_fingerprint,),
            ).fetchone()
            if terminal_row is not None:
                terminal_proposal, had_new_evidence = _restore_terminal_duplicate(
                    connection,
                    terminal_row,
                    payload,
                    updated_at=created_at,
                )
                connection.execute(
                    """
                    INSERT INTO review_proposal_recurrences
                        (proposal_id, occurrence_count, last_seen_at,
                         last_had_new_evidence)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(proposal_id) DO UPDATE SET
                        occurrence_count = occurrence_count + 1,
                        last_seen_at = excluded.last_seen_at,
                        last_had_new_evidence = excluded.last_had_new_evidence
                    """,
                    (
                        terminal_proposal.proposal_id,
                        created_at,
                        int(had_new_evidence),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO review_proposal_submissions
                        (idempotency_key, request_hash, proposal_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        normalized_key,
                        request_hash,
                        terminal_proposal.proposal_id,
                        created_at,
                    ),
                )
                connection.commit()
                return temporary_path.read_bytes(), ReviewProposalSubmission(
                    proposal=terminal_proposal,
                    deduplicated=True,
                )
            proposal_id = f"prp_{uuid.uuid4().hex}"
            _validate_relation_targets(connection, proposal_id, payload)
            _insert_proposal(
                connection,
                proposal_id=proposal_id,
                payload=payload,
                exact_fingerprint=exact_fingerprint,
                created_at=created_at,
            )
            _group_proposal_relations(
                connection,
                proposal_id=proposal_id,
                near_proposal_ids=payload.near_proposal_ids,
                conflict_proposal_ids=payload.conflict_proposal_ids,
                created_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO review_proposal_submissions
                    (idempotency_key, request_hash, proposal_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_key, request_hash, proposal_id, created_at),
            )
            connection.commit()
            proposal_row = connection.execute(
                "SELECT * FROM review_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if proposal_row is None:
                raise IntegrityError("submitted unified review proposal disappeared")
            proposal = _proposal_from_row(proposal_row)
        return temporary_path.read_bytes(), ReviewProposalSubmission(
            proposal=proposal,
            deduplicated=False,
        )
    except sqlite3.Error as error:
        raise IntegrityError("cannot stage unified review proposal") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def register_source_memory_proposal(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    planned_memory_id: str,
    canonical_name: str,
    body: str,
    applicability_scope: str,
    source: dict[str, object],
    created_at: str,
    suggested_action: Literal["new", "supplement", "revise", "conflict"] = "new",
    target_memory_id: str | None = None,
    target_version: int = 0,
    near_proposal_ids: tuple[str, ...] = (),
    conflict_proposal_ids: tuple[str, ...] = (),
) -> None:
    approval_effect_type = (
        "create_source_backed_canonical_memory"
        if suggested_action == "new"
        else "revise_canonical_memory"
    )
    effective_target_id = planned_memory_id if suggested_action == "new" else target_memory_id
    payload = ReviewProposalInput.from_data(
        {
            "title": canonical_name,
            "content": body,
            "intent": "integrate",
            "formation": "explicit",
            "priority": "routine",
            "applicability_scope": applicability_scope,
            "approval_effect": {
                "type": approval_effect_type,
                "canonical_name": canonical_name,
                "personal_cognition": False,
            },
            "target": {
                "memory_id": effective_target_id,
                "expected_version": target_version,
            },
            "supporting_evidence": [{"kind": "source", **source}],
            "opposing_evidence": [],
            "dependencies": [],
            "context_coverage": ["submitted local source"],
            "blind_spots": [],
            "near_proposal_ids": list(near_proposal_ids),
            "conflict_proposal_ids": list(conflict_proposal_ids),
            "sensitivity": "local-only",
            "evidence_retention": "receipt",
            "migration_restrictions": [],
        }
    )
    exact_fingerprint = _stable_hash(
        {
            "content": " ".join(body.casefold().split()),
            "scope": " ".join(applicability_scope.casefold().split()),
            "approval_effect": payload.approval_effect.to_data(),
            "target": payload.target.to_data(),
            "intent": payload.intent,
            "formation": payload.formation,
        }
    )
    _validate_relation_targets(connection, proposal_id, payload)
    _insert_proposal(
        connection,
        proposal_id=proposal_id,
        payload=payload,
        exact_fingerprint=exact_fingerprint,
        created_at=created_at,
    )
    _group_proposal_relations(
        connection,
        proposal_id=proposal_id,
        near_proposal_ids=payload.near_proposal_ids,
        conflict_proposal_ids=payload.conflict_proposal_ids,
        created_at=created_at,
    )

def merge_review_proposal_supporting_evidence(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    evidence: dict[str, object],
    updated_at: str,
) -> int:
    row = connection.execute(
        "SELECT * FROM review_proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise UserInputError(f"review proposal does not exist: {proposal_id}")
    proposal = _proposal_from_row(row)
    if proposal.status not in ("pending", "deferred"):
        raise UserInputError(
            f"review proposal is not pending: {proposal_id} ({proposal.status})"
        )
    incoming = replace(
        proposal.payload,
        supporting_evidence=(evidence,),
        opposing_evidence=(),
    )
    merged = _merge_duplicate_evidence(
        connection,
        proposal,
        incoming,
        updated_at=updated_at,
    )
    return merged.proposal_version



def stage_review_batch(
    database_path: Path,
    request: ReviewBatchRequest,
    *,
    idempotency_key: str,
    entrance: str,
) -> tuple[bytes, ReviewBatchResult]:
    normalized_key = idempotency_key.strip()
    normalized_entrance = entrance.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise UserInputError("idempotency key must contain 1 to 200 characters")
    if not normalized_entrance or len(normalized_entrance) > 100:
        raise UserInputError("entrance must contain 1 to 100 characters")
    request_hash = _stable_hash(
        {"request": request.to_data(), "entrance": normalized_entrance}
    )
    temporary_path = _copy_database(database_path, ".review-batch.")
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            existing = connection.execute(
                """
                SELECT request_hash, result_json FROM review_batches
                WHERE idempotency_key = ?
                """,
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if existing[0] != request_hash or not isinstance(existing[1], str):
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                stored_result_data = json.loads(existing[1])
                if not isinstance(stored_result_data, dict):
                    raise IntegrityError("stored review batch result is invalid")
                return temporary_path.read_bytes(), ReviewBatchResult(
                    cast(dict[str, object], stored_result_data)
                )
            if connection.execute(
                "SELECT 1 FROM review_batches WHERE batch_id = ?",
                (request.batch_id,),
            ).fetchone() is not None:
                raise UserInputError(
                    f"review batch id was already used: {request.batch_id}"
                )
            created_at = datetime.now(timezone.utc).isoformat()
            proposal_rows: dict[str, tuple[object, ...]] = {}
            for decision in request.decisions:
                row = connection.execute(
                    "SELECT * FROM review_proposals WHERE proposal_id = ?",
                    (decision.proposal_id,),
                ).fetchone()
                if row is None:
                    raise UserInputError(
                        f"review proposal does not exist: {decision.proposal_id}"
                    )
                proposal_rows[decision.proposal_id] = row
            proposals = {
                proposal_id: _proposal_from_row(row)
                for proposal_id, row in proposal_rows.items()
            }
            indexed_outcomes: dict[str, dict[str, object]] = {}
            for group_index, group in enumerate(
                _decision_dependency_groups(request.decisions, proposals)
            ):
                savepoint = f"review_group_{group_index}"
                connection.execute(f"SAVEPOINT {savepoint}")
                group_outcomes: list[dict[str, object]] = []
                precondition_error = _dependency_precondition_error(
                    connection,
                    group,
                    proposals,
                )
                try:
                    if precondition_error is None:
                        for decision in group:
                            outcome = _apply_review_decision(
                                connection,
                                proposals[decision.proposal_id],
                                decision,
                                entrance=normalized_entrance,
                                decided_at=created_at,
                            )
                            group_outcomes.append(outcome)
                            if outcome.get("status") == "failed" and len(group) > 1:
                                error = outcome.get("error")
                                precondition_error = (
                                    error if isinstance(error, str) else "unknown_failure"
                                )
                                break
                except (IntegrityError, UserInputError) as error:
                    precondition_error = str(error)
                if precondition_error is not None:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    is_dependency_group = len(group) > 1 or any(
                        proposals[decision.proposal_id].payload.dependencies
                        for decision in group
                    )
                    failure_kind = (
                        "dependency_group_failed"
                        if is_dependency_group
                        else "application_failed"
                    )
                    group_error = f"{failure_kind}:{precondition_error}"
                    for decision in group:
                        proposal = proposals[decision.proposal_id]
                        connection.execute(
                            """
                            UPDATE review_proposals
                            SET status = 'pending', deferred_until = NULL, updated_at = ?,
                                retry_count = retry_count + 1, last_error = ?
                            WHERE proposal_id = ?
                              AND status IN ('pending', 'deferred')
                            """,
                            (created_at, group_error, proposal.proposal_id),
                        )
                        indexed_outcomes[proposal.proposal_id] = {
                            "proposal_id": proposal.proposal_id,
                            "proposal_version": decision.proposal_version,
                            "status": "failed",
                            "decision": decision.decision,
                            "final_content": None,
                            "materialization": None,
                            "error": group_error,
                        }
                else:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    for outcome in group_outcomes:
                        proposal_id = outcome.get("proposal_id")
                        if not isinstance(proposal_id, str):
                            raise IntegrityError("review batch outcome has no proposal id")
                        indexed_outcomes[proposal_id] = outcome
            outcomes = [
                indexed_outcomes[decision.proposal_id] for decision in request.decisions
            ]
            failed_count = sum(
                1 for outcome in outcomes if outcome.get("status") == "failed"
            )
            if failed_count == 0:
                status = "complete"
            elif failed_count == len(outcomes):
                status = "failed"
            else:
                status = "partial"
            result_data: dict[str, object] = {
                "batch_id": request.batch_id,
                "status": status,
                "partial_success": 0 < failed_count < len(outcomes),
                "outcomes": outcomes,
            }
            connection.execute(
                """
                INSERT INTO review_batches
                    (batch_id, idempotency_key, request_hash, status, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.batch_id,
                    normalized_key,
                    request_hash,
                    status,
                    _json(result_data),
                    created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO review_batch_items
                    (batch_id, proposal_id, proposal_version, decision, outcome_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        request.batch_id,
                        decision.proposal_id,
                        decision.proposal_version,
                        decision.decision,
                        _json(outcome),
                    )
                    for decision, outcome in zip(request.decisions, outcomes, strict=True)
                ),
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("review batch left a dangling reference")
        return temporary_path.read_bytes(), ReviewBatchResult(result_data)
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise IntegrityError("cannot stage unified review batch") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_review_expiration(
    database_path: Path,
    *,
    as_of: str,
    retention_days: int,
) -> tuple[bytes, ReviewExpirationResult]:
    if retention_days < 1:
        raise UserInputError("review retention days must be positive")
    try:
        as_of_time = datetime.fromisoformat(as_of)
    except ValueError as error:
        raise UserInputError("review expiration as_of must be an ISO timestamp") from error
    if as_of_time.tzinfo is None:
        raise UserInputError("review expiration as_of must include a timezone")
    normalized_as_of = as_of_time.isoformat()
    temporary_path = _copy_database(database_path, ".review-expiration.")
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            deferred_rows = connection.execute(
                """
                SELECT proposal_id, deferred_until
                FROM review_proposals
                WHERE status = 'deferred' AND deferred_until IS NOT NULL
                ORDER BY deferred_until, proposal_id
                """
            ).fetchall()
            reactivated: list[str] = []
            for proposal_id, deferred_until in deferred_rows:
                if not isinstance(proposal_id, str) or not isinstance(
                    deferred_until, str
                ):
                    raise IntegrityError("deferred review proposal is invalid")
                try:
                    review_time = datetime.fromisoformat(deferred_until)
                except ValueError as error:
                    raise IntegrityError(
                        "deferred review proposal time is invalid"
                    ) from error
                if review_time.tzinfo is None:
                    raise IntegrityError(
                        "deferred review proposal time has no timezone"
                    )
                if review_time <= as_of_time:
                    connection.execute(
                        """
                        UPDATE review_proposals
                        SET status = 'pending', deferred_until = NULL, updated_at = ?
                        WHERE proposal_id = ? AND status = 'deferred'
                        """,
                        (normalized_as_of, proposal_id),
                    )
                    reactivated.append(proposal_id)
            rows = connection.execute(
                """
                SELECT proposal_id, title, formation, supporting_evidence_json,
                       exact_fingerprint, created_at
                FROM review_proposals
                WHERE status = 'pending' AND priority = 'routine'
                ORDER BY created_at, proposal_id
                """
            ).fetchall()
            expired: list[dict[str, object]] = []
            for row in rows:
                if not all(isinstance(row[index], str) for index in range(6)):
                    raise IntegrityError("routine review proposal is invalid")
                if row[0] in reactivated:
                    continue
                try:
                    created_at = datetime.fromisoformat(row[5])
                    supporting_evidence = json.loads(row[3])
                except (ValueError, json.JSONDecodeError) as error:
                    raise IntegrityError("routine review proposal is invalid") from error
                if created_at.tzinfo is None:
                    raise IntegrityError("routine review proposal time has no timezone")
                if (as_of_time - created_at).days < retention_days:
                    continue
                if not isinstance(supporting_evidence, list):
                    raise IntegrityError("routine review proposal evidence is invalid")
                compact: dict[str, object] = {
                    "proposal_id": row[0],
                    "status": "expired",
                    "exact_fingerprint": row[4],
                    "title": row[1],
                    "formation": row[2],
                    "supporting_evidence": supporting_evidence,
                    "expired_at": normalized_as_of,
                    "content_retained": False,
                }
                connection.execute(
                    """
                    INSERT INTO review_expirations
                        (proposal_id, exact_fingerprint, title, formation,
                         supporting_evidence_json, expired_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (row[0], row[4], row[1], row[2], row[3], normalized_as_of),
                )
                connection.execute(
                    """
                    UPDATE review_proposals
                    SET status = 'expired', content = NULL, updated_at = ?, expired_at = ?,
                        opposing_evidence_json = '[]', context_coverage_json = '[]',
                        blind_spots_json = '[]', migration_restrictions_json = '[]',
                        last_error = NULL
                    WHERE proposal_id = ? AND status = 'pending'
                    """,
                    (normalized_as_of, normalized_as_of, row[0]),
                )
                expired.append(compact)
            connection.commit()
        return temporary_path.read_bytes(), ReviewExpirationResult(
            retention_days=retention_days,
            as_of=normalized_as_of,
            reactivated=tuple(reactivated),
            expired=tuple(expired),
        )
    except sqlite3.Error as error:
        raise IntegrityError("cannot stage review proposal expiration") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _is_nonapprovable_source_conflict(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> bool:
    return connection.execute(
        """
        SELECT 1
        FROM source_memory_proposal_details AS detail
        JOIN integration_proposals AS proposal
          ON proposal.proposal_id = detail.proposal_id
        WHERE detail.proposal_id = ? AND proposal.suggested_action = 'conflict'
        """,
        (proposal_id,),
    ).fetchone() is not None


def _apply_review_decision(
    connection: sqlite3.Connection,
    proposal: ReviewProposal,
    decision: ReviewDecision,
    *,
    entrance: str,
    decided_at: str,
) -> dict[str, object]:
    base: dict[str, object] = {
        "proposal_id": proposal.proposal_id,
        "proposal_version": decision.proposal_version,
        "error": None,
        "materialization": None,
    }
    if proposal.proposal_version != decision.proposal_version:
        return _record_failed_decision(
            connection,
            proposal,
            base,
            decision=decision.decision,
            error="proposal_version_conflict",
            failed_at=decided_at,
        )
    if proposal.status not in ("pending", "deferred"):
        return {
            **base,
            "status": "failed",
            "decision": decision.decision,
            "final_content": None,
            "error": f"proposal_not_pending:{proposal.status}",
        }
    if decision.decision == "reject":
        connection.execute(
            """
            UPDATE review_proposals
            SET status = 'rejected', updated_at = ?, deferred_until = NULL,
                last_error = NULL
            WHERE proposal_id = ?
            """,
            (decided_at, proposal.proposal_id),
        )
        return {
            **base,
            "status": "rejected",
            "decision": "rejected",
            "final_content": proposal.payload.content,
        }
    if decision.decision == "defer":
        connection.execute(
            """
            UPDATE review_proposals
            SET status = 'deferred', updated_at = ?, deferred_until = ?,
                last_error = NULL
            WHERE proposal_id = ?
            """,
            (decided_at, decision.defer_until, proposal.proposal_id),
        )
        return {
            **base,
            "status": "deferred",
            "decision": "deferred",
            "final_content": proposal.payload.content,
            "defer_until": decision.defer_until,
        }
    if _is_nonapprovable_source_conflict(connection, proposal.proposal_id):
        return _record_failed_decision(
            connection,
            proposal,
            base,
            decision="approve",
            error=CONFLICT_APPROVAL_ERROR,
            failed_at=decided_at,
        )
    personal_cognition = proposal.payload.approval_effect.personal_cognition
    target_error = _target_version_error(connection, proposal)
    if target_error is not None:
        return _record_failed_decision(
            connection,
            proposal,
            base,
            decision="approve",
            error=target_error,
            failed_at=decided_at,
        )
    if personal_cognition is True and not decision.confirm_personal_cognition:
        return _record_failed_decision(
            connection,
            proposal,
            base,
            decision="approve",
            error="personal_cognition_requires_item_confirmation",
            failed_at=decided_at,
        )
    final_content = decision.edited_content or proposal.payload.content
    if len(final_content.encode("utf-8")) > 8 * 1024:
        return _record_failed_decision(
            connection,
            proposal,
            base,
            decision="approve",
            error="approved_content_exceeds_memory_budget",
            failed_at=decided_at,
        )
    connection.execute(
        "UPDATE review_proposals SET status = 'applying', updated_at = ? "
        "WHERE proposal_id = ?",
        (decided_at, proposal.proposal_id),
    )
    materialization = _materialize_proposal(
        connection,
        proposal,
        final_content=final_content,
        entrance=entrance,
        materialized_at=decided_at,
    )
    edited = decision.edited_content is not None
    connection.execute(
        """
        UPDATE review_proposals
        SET status = 'applied', content = ?, updated_at = ?, deferred_until = NULL,
            last_error = NULL,
            proposal_version = proposal_version + ?
        WHERE proposal_id = ? AND status = 'applying'
        """,
        (final_content, decided_at, int(edited), proposal.proposal_id),
    )
    return {
        **base,
        "status": "applied",
        "decision": "edited-approved" if edited else "approved",
        "final_content": final_content,
        "materialization": materialization,
    }


def _decision_dependency_groups(
    decisions: tuple[ReviewDecision, ...],
    proposals: dict[str, ReviewProposal],
) -> tuple[tuple[ReviewDecision, ...], ...]:
    parent = {decision.proposal_id: decision.proposal_id for decision in decisions}

    def find(proposal_id: str) -> str:
        while parent[proposal_id] != proposal_id:
            parent[proposal_id] = parent[parent[proposal_id]]
            proposal_id = parent[proposal_id]
        return proposal_id

    def union(first_id: str, second_id: str) -> None:
        first_root = find(first_id)
        second_root = find(second_id)
        if first_root != second_root:
            parent[second_root] = first_root

    for decision in decisions:
        for dependency_id in proposals[decision.proposal_id].payload.dependencies:
            if dependency_id in parent:
                union(decision.proposal_id, dependency_id)
    grouped: dict[str, list[ReviewDecision]] = {}
    group_order: list[str] = []
    for decision in decisions:
        root = find(decision.proposal_id)
        if root not in grouped:
            grouped[root] = []
            group_order.append(root)
        grouped[root].append(decision)
    return tuple(
        _topologically_ordered_decisions(tuple(grouped[root]), proposals)
        for root in group_order
    )


def _topologically_ordered_decisions(
    decisions: tuple[ReviewDecision, ...],
    proposals: dict[str, ReviewProposal],
) -> tuple[ReviewDecision, ...]:
    remaining = list(decisions)
    remaining_ids = {decision.proposal_id for decision in remaining}
    ordered: list[ReviewDecision] = []
    while remaining:
        ready = next(
            (
                decision
                for decision in remaining
                if not (
                    set(proposals[decision.proposal_id].payload.dependencies)
                    & remaining_ids
                )
            ),
            None,
        )
        if ready is None:
            return decisions
        ordered.append(ready)
        remaining.remove(ready)
        remaining_ids.remove(ready.proposal_id)
    return tuple(ordered)


def _is_approval(decision: ReviewDecisionKind) -> bool:
    return decision in ("approve", "approve-edited")


def _dependency_precondition_error(
    connection: sqlite3.Connection,
    group: tuple[ReviewDecision, ...],
    proposals: dict[str, ReviewProposal],
) -> str | None:
    group_decisions = {decision.proposal_id: decision for decision in group}
    for decision in group:
        if not _is_approval(decision.decision):
            continue
        for dependency_id in proposals[decision.proposal_id].payload.dependencies:
            dependency_decision = group_decisions.get(dependency_id)
            if dependency_decision is not None:
                if not _is_approval(dependency_decision.decision):
                    return f"dependency_not_approved:{dependency_id}"
                continue
            row = connection.execute(
                "SELECT status FROM review_proposals WHERE proposal_id = ?",
                (dependency_id,),
            ).fetchone()
            if row is None or row[0] != "applied":
                return f"dependency_not_applied:{dependency_id}"
    return None


def _record_failed_decision(
    connection: sqlite3.Connection,
    proposal: ReviewProposal,
    base: dict[str, object],
    *,
    decision: str,
    error: str,
    failed_at: str,
) -> dict[str, object]:
    if proposal.status in ("pending", "deferred"):
        connection.execute(
            """
            UPDATE review_proposals
            SET status = 'pending', deferred_until = NULL, updated_at = ?,
                retry_count = retry_count + 1, last_error = ?
            WHERE proposal_id = ?
            """,
            (failed_at, error, proposal.proposal_id),
        )
    return {
        **base,
        "status": "failed",
        "decision": decision,
        "final_content": None,
        "error": error,
    }


def _target_version_error(
    connection: sqlite3.Connection,
    proposal: ReviewProposal,
) -> str | None:
    if proposal.payload.intent not in ("derive", "integrate"):
        return None
    target_memory_id = proposal.payload.target.memory_id
    expected_version = proposal.payload.target.expected_version
    effect_type = proposal.payload.approval_effect.effect_type
    if isinstance(target_memory_id, str) and _has_memory_tombstone(
        connection, target_memory_id
    ):
        return "permanently_erased"
    if effect_type == "revise_canonical_memory":
        if not isinstance(target_memory_id, str) or not isinstance(expected_version, int):
            return "target_version_conflict"
        row = connection.execute(
            "SELECT current_version FROM canonical_memories WHERE memory_id = ?",
            (target_memory_id,),
        ).fetchone()
        return "target_version_conflict" if row is None or row[0] != expected_version else None
    if expected_version != 0:
        return "target_version_conflict"
    if target_memory_id is None:
        return None
    if not isinstance(target_memory_id, str):
        return "target_version_conflict"
    exists = connection.execute(
        "SELECT 1 FROM canonical_memories WHERE memory_id = ?",
        (target_memory_id,),
    ).fetchone() is not None
    return "target_version_conflict" if exists else None


def _materialize_proposal(
    connection: sqlite3.Connection,
    proposal: ReviewProposal,
    *,
    final_content: str,
    entrance: str,
    materialized_at: str,
) -> dict[str, object]:
    personal_cognition = proposal.payload.approval_effect.personal_cognition
    if proposal.payload.intent in ("derive", "integrate"):
        materialization = _materialize_canonical_memory(
            connection,
            proposal,
            final_content=final_content,
            entrance=entrance,
            materialized_at=materialized_at,
        )
        artifact_kind = "canonical-memory"
        artifact_id = cast(str, materialization["memory_id"])
        authorship = cast(str, materialization["authorship"])
    elif proposal.payload.intent == "archive":
        artifact_id = f"arc_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO human_archives
                (archive_id, title, content, applicability_scope, proposal_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                proposal.payload.title,
                final_content,
                proposal.payload.applicability_scope,
                proposal.proposal_id,
                materialized_at,
            ),
        )
        artifact_kind = "human-archive"
        authorship = "creator-approved-archive"
        materialization = {
            "kind": artifact_kind,
            "archive_id": artifact_id,
            "authorship": authorship,
            "personal_cognition": personal_cognition,
        }
    else:
        artifact_id = f"res_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO research_threads
                (research_id, title, question, applicability_scope, proposal_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                proposal.payload.title,
                final_content,
                proposal.payload.applicability_scope,
                proposal.proposal_id,
                materialized_at,
            ),
        )
        artifact_kind = "research-thread"
        authorship = "creator-approved-hypothesis"
        materialization = {
            "kind": artifact_kind,
            "research_id": artifact_id,
            "authorship": authorship,
            "personal_cognition": personal_cognition,
        }
    if os.environ.get("MYOUTBRAIN_FAIL_REVIEW_PROPOSAL") == proposal.proposal_id:
        raise IntegrityError("injected_review_failure")
    connection.execute(
        """
        INSERT INTO review_materializations
            (proposal_id, artifact_kind, artifact_id, authorship,
             personal_cognition, final_content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proposal.proposal_id,
            artifact_kind,
            artifact_id,
            authorship,
            int(personal_cognition),
            _stable_hash(final_content),
            materialized_at,
        ),
    )
    return materialization


def _materialize_canonical_memory(
    connection: sqlite3.Connection,
    proposal: ReviewProposal,
    *,
    final_content: str,
    entrance: str,
    materialized_at: str,
) -> dict[str, object]:
    effect = proposal.payload.approval_effect
    canonical_name = effect.canonical_name
    if canonical_name is None:
        raise UserInputError("canonical-memory approval effect requires canonical_name")
    if effect.effect_type == "revise_canonical_memory":
        return _materialize_canonical_revision(
            connection,
            proposal,
            final_content=final_content,
            entrance=entrance,
            materialized_at=materialized_at,
        )
    target_memory_id = proposal.payload.target.memory_id
    expected_version = proposal.payload.target.expected_version
    if target_memory_id is not None and not isinstance(target_memory_id, str):
        raise UserInputError("canonical-memory target must be text or null")
    if expected_version != 0:
        raise UserInputError(
            "this review tracer bullet only creates canonical memory at expected version 0"
        )
    memory_id = target_memory_id or f"mem_{uuid.uuid4().hex}"
    if connection.execute(
        "SELECT 1 FROM canonical_memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone() is not None:
        raise UserInputError("canonical-memory target version conflict")
    capsule_id = f"cap_{uuid.uuid4().hex}"
    body_bytes = len(final_content.encode("utf-8"))
    connection.execute(
        """
        INSERT INTO knowledge_capsules
            (capsule_id, topic, body_bytes, memory_record_count,
             structural_version, created_at, updated_at)
        VALUES (?, ?, ?, 1, 1, ?, ?)
        """,
        (
            capsule_id,
            proposal.payload.applicability_scope,
            body_bytes,
            materialized_at,
            materialized_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO canonical_memories
            (memory_id, content, current_version, sensitivity, state,
             created_at, updated_at)
        VALUES (?, '', 1, ?, 'current', ?, ?)
        """,
        (memory_id, proposal.payload.sensitivity, materialized_at, materialized_at),
    )
    connection.execute(
        """
        INSERT INTO canonical_memory_versions
            (memory_id, version, content, applicability_scope, capsule_id,
             action, change_reason, created_at, superseded_at, supersession_reason)
        VALUES (?, 1, ?, ?, ?, 'created', ?, ?, NULL, NULL)
        """,
        (
            memory_id,
            final_content,
            proposal.payload.applicability_scope,
            capsule_id,
            f"Approved unified {proposal.payload.intent} proposal.",
            materialized_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO knowledge_dictionary
            (memory_id, canonical_name, normalized_name, current_version,
             primary_capsule_id)
        VALUES (?, ?, ?, 1, ?)
        """,
        (
            memory_id,
            canonical_name.strip(),
            " ".join(canonical_name.casefold().split()),
            capsule_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO memory_names
            (memory_id, name, normalized_name, name_kind, created_at)
        VALUES (?, ?, ?, 'canonical', ?)
        """,
        (
            memory_id,
            canonical_name.strip(),
            " ".join(canonical_name.casefold().split()),
            materialized_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO canonical_memory_review_provenance
            (memory_id, version, proposal_id)
        VALUES (?, 1, ?)
        """,
        (memory_id, proposal.proposal_id),
    )
    if effect.effect_type == "create_source_backed_canonical_memory":
        updated = connection.execute(
            """
            UPDATE integration_proposals
            SET status = 'accepted', reviewed_at = ?
            WHERE proposal_id = ? AND status = 'pending'
            """,
            (materialized_at, proposal.proposal_id),
        )
        if updated.rowcount != 1:
            raise IntegrityError("source memory proposal changed during unified approval")
        connection.execute(
            """
            INSERT INTO integration_reviews
                (review_id, proposal_id, decision, action, reviewed_content,
                 reason, canonical_memory_id, created_at)
            VALUES (?, ?, 'accepted', 'created', NULL, ?, ?, ?)
            """,
            (
                f"rev_{hashlib.sha256(proposal.proposal_id.encode()).hexdigest()}",
                proposal.proposal_id,
                "Approved through unified review batch.",
                memory_id,
                materialized_at,
            ),
        )
    for evidence in proposal.payload.supporting_evidence:
        source_id = evidence.get("source_id")
        source_version = evidence.get("version", evidence.get("source_version"))
        if not isinstance(source_id, str) or not isinstance(source_version, int):
            continue
        if connection.execute(
            """
            SELECT 1 FROM evidence_source_versions
            WHERE source_id = ? AND version = ?
            """,
            (source_id, source_version),
        ).fetchone() is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO canonical_memory_version_evidence
                    (memory_id, version, source_id, source_version, relationship)
                VALUES (?, 1, ?, ?, 'supports')
                """,
                (memory_id, source_id, source_version),
            )
    personal_cognition = proposal.payload.approval_effect.personal_cognition
    if personal_cognition:
        authorship = "creator-personal-cognition"
    elif proposal.payload.formation == "derived":
        authorship = "system-derived"
    else:
        authorship = "creator-approved-integration"
    result_hash = _stable_hash(
        {
            "proposal_id": proposal.proposal_id,
            "memory_id": memory_id,
            "version": 1,
            "content_hash": _stable_hash(final_content),
        }
    )
    connection.execute(
        """
        INSERT INTO audit_events
            (event_id, event_type, occurred_at, subject_id, proposal_id,
             before_version, after_version, entrance, result_hash)
        VALUES (?, 'review.applied', ?, ?, ?, NULL, 1, ?, ?)
        """,
        (
            f"aud_{uuid.uuid4().hex}",
            materialized_at,
            memory_id,
            proposal.proposal_id,
            entrance,
            result_hash,
        ),
    )
    return {
        "kind": "canonical-memory",
        "memory_id": memory_id,
        "version": 1,
        "capsule_id": capsule_id,
        "authorship": authorship,
        "personal_cognition": personal_cognition,
    }


def _materialize_canonical_revision(
    connection: sqlite3.Connection,
    proposal: ReviewProposal,
    *,
    final_content: str,
    entrance: str,
    materialized_at: str,
) -> dict[str, object]:
    target_memory_id = proposal.payload.target.memory_id
    expected_version = proposal.payload.target.expected_version
    canonical_name = proposal.payload.approval_effect.canonical_name
    if (
        not isinstance(target_memory_id, str)
        or not isinstance(expected_version, int)
        or not isinstance(canonical_name, str)
    ):
        raise UserInputError("canonical-memory revision target is invalid")
    row = connection.execute(
        """
        SELECT memory.current_version, dictionary.primary_capsule_id,
               capsule.body_bytes, version.content
        FROM canonical_memories AS memory
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = memory.memory_id
        JOIN knowledge_capsules AS capsule
          ON capsule.capsule_id = dictionary.primary_capsule_id
        JOIN canonical_memory_versions AS version
          ON version.memory_id = memory.memory_id
         AND version.version = memory.current_version
        WHERE memory.memory_id = ?
        """,
        (target_memory_id,),
    ).fetchone()
    if (
        row is None
        or row[0] != expected_version
        or not isinstance(row[1], str)
        or not isinstance(row[2], int)
        or not isinstance(row[3], str)
    ):
        raise UserInputError("canonical-memory target version conflict")
    capsule_id = row[1]
    new_version = expected_version + 1
    previous_body_bytes = len(row[3].encode("utf-8"))
    new_body_bytes = len(final_content.encode("utf-8"))
    connection.execute(
        """
        UPDATE canonical_memory_versions
        SET superseded_at = ?, supersession_reason = ?
        WHERE memory_id = ? AND version = ? AND superseded_at IS NULL
        """,
        (
            materialized_at,
            "Superseded by an approved unified integration proposal.",
            target_memory_id,
            expected_version,
        ),
    )
    connection.execute(
        """
        INSERT INTO canonical_memory_versions
            (memory_id, version, content, applicability_scope, capsule_id,
             action, change_reason, created_at, superseded_at, supersession_reason)
        VALUES (?, ?, ?, ?, ?, 'revised', ?, ?, NULL, NULL)
        """,
        (
            target_memory_id,
            new_version,
            final_content,
            proposal.payload.applicability_scope,
            capsule_id,
            "Approved unified integration revision.",
            materialized_at,
        ),
    )
    connection.execute(
        """
        UPDATE canonical_memories
        SET current_version = ?, updated_at = ?
        WHERE memory_id = ? AND current_version = ?
        """,
        (new_version, materialized_at, target_memory_id, expected_version),
    )
    connection.execute(
        """
        UPDATE knowledge_dictionary
        SET canonical_name = ?, normalized_name = ?, current_version = ?
        WHERE memory_id = ? AND current_version = ?
        """,
        (
            canonical_name.strip(),
            " ".join(canonical_name.casefold().split()),
            new_version,
            target_memory_id,
            expected_version,
        ),
    )
    normalized_name = " ".join(canonical_name.casefold().split())
    connection.execute(
        "UPDATE memory_names SET name_kind = 'alias' WHERE memory_id = ?",
        (target_memory_id,),
    )
    connection.execute(
        """
        INSERT INTO memory_names
            (memory_id, name, normalized_name, name_kind, created_at)
        VALUES (?, ?, ?, 'canonical', ?)
        ON CONFLICT(memory_id, normalized_name) DO UPDATE SET
            name = excluded.name,
            name_kind = 'canonical'
        """,
        (target_memory_id, canonical_name.strip(), normalized_name, materialized_at),
    )
    connection.execute(
        """
        UPDATE knowledge_capsules
        SET body_bytes = body_bytes - ? + ?, updated_at = ?
        WHERE capsule_id = ?
        """,
        (previous_body_bytes, new_body_bytes, materialized_at, capsule_id),
    )
    connection.execute(
        """
        INSERT INTO canonical_memory_review_provenance
            (memory_id, version, proposal_id)
        VALUES (?, ?, ?)
        """,
        (target_memory_id, new_version, proposal.proposal_id),
    )
    for evidence in proposal.payload.supporting_evidence:
        source_id = evidence.get("source_id")
        source_version = evidence.get("version", evidence.get("source_version"))
        if not isinstance(source_id, str) or not isinstance(source_version, int):
            continue
        if connection.execute(
            """
            SELECT 1 FROM evidence_source_versions
            WHERE source_id = ? AND version = ?
            """,
            (source_id, source_version),
        ).fetchone() is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO canonical_memory_version_evidence
                    (memory_id, version, source_id, source_version, relationship)
                VALUES (?, ?, ?, ?, 'supports')
                """,
                (target_memory_id, new_version, source_id, source_version),
            )
    personal_cognition = proposal.payload.approval_effect.personal_cognition
    if personal_cognition:
        authorship = "creator-personal-cognition"
    elif proposal.payload.formation == "derived":
        authorship = "system-derived"
    else:
        authorship = "creator-approved-integration"
    result_hash = _stable_hash(
        {
            "proposal_id": proposal.proposal_id,
            "memory_id": target_memory_id,
            "version": new_version,
            "content_hash": _stable_hash(final_content),
        }
    )
    connection.execute(
        """
        INSERT INTO audit_events
            (event_id, event_type, occurred_at, subject_id, proposal_id,
             before_version, after_version, entrance, result_hash)
        VALUES (?, 'review.applied', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"aud_{uuid.uuid4().hex}",
            materialized_at,
            target_memory_id,
            proposal.proposal_id,
            expected_version,
            new_version,
            entrance,
            result_hash,
        ),
    )
    return {
        "kind": "canonical-memory",
        "memory_id": target_memory_id,
        "version": new_version,
        "capsule_id": capsule_id,
        "authorship": authorship,
        "personal_cognition": personal_cognition,
    }


def read_review_queue(database_path: Path) -> ReviewQueue:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_proposals
                WHERE status IN ('pending', 'deferred')
                ORDER BY CASE priority
                    WHEN 'blocking' THEN 0
                    WHEN 'priority' THEN 1
                    ELSE 2
                END, created_at, proposal_id
                """
            ).fetchall()
            group_rows = connection.execute(
                """
                SELECT group_id, kind FROM review_groups
                WHERE EXISTS (
                    SELECT 1 FROM review_group_members AS member
                    JOIN review_proposals AS proposal
                      ON proposal.proposal_id = member.proposal_id
                    WHERE member.group_id = review_groups.group_id
                      AND proposal.status IN ('pending', 'deferred')
                )
                ORDER BY created_at, group_id
                """
            ).fetchall()
            groups = tuple(
                _review_group_from_row(connection, row) for row in group_rows
            )
            conflict_ids = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT detail.proposal_id
                    FROM source_memory_proposal_details AS detail
                    JOIN integration_proposals AS proposal
                      ON proposal.proposal_id = detail.proposal_id
                    JOIN review_proposals AS review
                      ON review.proposal_id = detail.proposal_id
                    WHERE proposal.suggested_action = 'conflict'
                      AND review.status IN ('pending', 'deferred')
                    """
                ).fetchall()
                if isinstance(row[0], str)
            }
            proposals = tuple(
                replace(
                    _proposal_from_row(row),
                    available_decisions=CONFLICT_AVAILABLE_DECISIONS,
                    approval_unavailable_reason=CONFLICT_APPROVAL_UNAVAILABLE_REASON,
                )
                if row[0] in conflict_ids
                else _proposal_from_row(row)
                for row in rows
            )
    except sqlite3.Error as error:
        raise IntegrityError("cannot read unified review queue") from error
    return ReviewQueue(
        proposals=proposals,
        groups=groups,
    )


def read_review_proposal(
    database_path: Path,
    proposal_id: str,
) -> ReviewProposal | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT * FROM review_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot read unified review proposal") from error
    return _proposal_from_row(row) if row is not None else None


def _merge_duplicate_evidence(
    connection: sqlite3.Connection,
    existing: ReviewProposal,
    incoming: ReviewProposalInput,
    *,
    updated_at: str,
) -> ReviewProposal:
    supporting = _merged_objects(
        existing.payload.supporting_evidence,
        incoming.supporting_evidence,
    )
    opposing = _merged_objects(
        existing.payload.opposing_evidence,
        incoming.opposing_evidence,
    )
    context_coverage = tuple(
        dict.fromkeys(existing.payload.context_coverage + incoming.context_coverage)
    )
    blind_spots = tuple(
        dict.fromkeys(existing.payload.blind_spots + incoming.blind_spots)
    )
    migration_restrictions = tuple(
        dict.fromkeys(
            existing.payload.migration_restrictions
            + incoming.migration_restrictions
        )
    )
    sensitivity = (
        "local-only"
        if "local-only" in (existing.payload.sensitivity, incoming.sensitivity)
        else "cloud-allowed"
    )
    retention_order = {"receipt": 0, "excerpt": 1, "full": 2}
    evidence_retention = max(
        (existing.payload.evidence_retention, incoming.evidence_retention),
        key=retention_order.__getitem__,
    )
    if (
        supporting != existing.payload.supporting_evidence
        or opposing != existing.payload.opposing_evidence
        or context_coverage != existing.payload.context_coverage
        or blind_spots != existing.payload.blind_spots
        or migration_restrictions != existing.payload.migration_restrictions
        or sensitivity != existing.payload.sensitivity
        or evidence_retention != existing.payload.evidence_retention
    ):
        connection.execute(
            """
            UPDATE review_proposals
            SET supporting_evidence_json = ?, opposing_evidence_json = ?,
                context_coverage_json = ?, blind_spots_json = ?,
                migration_restrictions_json = ?, sensitivity = ?,
                evidence_retention = ?,
                proposal_version = proposal_version + 1, updated_at = ?
            WHERE proposal_id = ?
            """,
            (
                _json(list(supporting)),
                _json(list(opposing)),
                _json(list(context_coverage)),
                _json(list(blind_spots)),
                _json(list(migration_restrictions)),
                sensitivity,
                evidence_retention,
                updated_at,
                existing.proposal_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM review_proposals WHERE proposal_id = ?",
            (existing.proposal_id,),
        ).fetchone()
        if row is None:
            raise IntegrityError("deduplicated review proposal disappeared")
        return _proposal_from_row(row)
    return existing


def _restore_terminal_duplicate(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
    incoming: ReviewProposalInput,
    *,
    updated_at: str,
) -> tuple[ReviewProposal, bool]:
    if (
        len(row) < 30
        or not isinstance(row[0], str)
        or not isinstance(row[23], str)
    ):
        raise IntegrityError("terminal review proposal is invalid")
    proposal_id = row[0]
    try:
        existing_supporting = tuple(
            cast(list[dict[str, object]], json.loads(cast(str, row[12])))
        )
        existing_opposing = tuple(
            cast(list[dict[str, object]], json.loads(cast(str, row[13])))
        )
        existing_near = tuple(cast(list[str], json.loads(cast(str, row[17]))))
        existing_conflicts = tuple(cast(list[str], json.loads(cast(str, row[18]))))
    except (json.JSONDecodeError, TypeError) as error:
        raise IntegrityError("terminal review proposal payload is invalid") from error
    supporting = _merged_objects(existing_supporting, incoming.supporting_evidence)
    opposing = _merged_objects(existing_opposing, incoming.opposing_evidence)
    had_new_evidence = (
        supporting != existing_supporting or opposing != existing_opposing
    )
    if not had_new_evidence:
        if row[23] == "expired":
            raise UserInputError(
                "expired review proposal requires materially new evidence to restore"
            )
        return _proposal_from_row(row), False
    near_ids = tuple(dict.fromkeys((*existing_near, *incoming.near_proposal_ids)))
    conflict_ids = tuple(
        dict.fromkeys((*existing_conflicts, *incoming.conflict_proposal_ids))
    )
    restored_payload = replace(
        incoming,
        near_proposal_ids=near_ids,
        conflict_proposal_ids=conflict_ids,
    )
    _validate_relation_targets(connection, proposal_id, restored_payload)
    connection.execute(
        """
        UPDATE review_proposals
        SET proposal_version = proposal_version + 1,
            title = ?, content = ?, intent = ?, formation = ?, priority = ?,
            applicability_scope = ?, approval_effect_json = ?, target_json = ?,
            supporting_evidence_json = ?, opposing_evidence_json = ?,
            dependencies_json = ?, context_coverage_json = ?, blind_spots_json = ?,
            near_proposal_ids_json = ?, conflict_proposal_ids_json = ?,
            sensitivity = ?, evidence_retention = ?, migration_restrictions_json = ?,
            status = 'pending', updated_at = ?, deferred_until = NULL,
            expired_at = NULL, last_error = NULL
        WHERE proposal_id = ? AND status IN ('rejected', 'expired')
        """,
        (
            incoming.title,
            incoming.content,
            incoming.intent,
            incoming.formation,
            incoming.priority,
            incoming.applicability_scope,
            _json(incoming.approval_effect.to_data()),
            _json(incoming.target.to_data()),
            _json(list(supporting)),
            _json(list(opposing)),
            _json(list(incoming.dependencies)),
            _json(list(incoming.context_coverage)),
            _json(list(incoming.blind_spots)),
            _json(list(near_ids)),
            _json(list(conflict_ids)),
            incoming.sensitivity,
            incoming.evidence_retention,
            _json(list(incoming.migration_restrictions)),
            updated_at,
            proposal_id,
        ),
    )
    connection.execute(
        "DELETE FROM review_expirations WHERE proposal_id = ?",
        (proposal_id,),
    )
    _group_proposal_relations(
        connection,
        proposal_id=proposal_id,
        near_proposal_ids=near_ids,
        conflict_proposal_ids=conflict_ids,
        created_at=updated_at,
    )
    restored_row = connection.execute(
        "SELECT * FROM review_proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if restored_row is None:
        raise IntegrityError("restored review proposal disappeared")
    return _proposal_from_row(restored_row), True


def _merged_objects(
    existing: tuple[dict[str, object], ...],
    incoming: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    merged = list(existing)
    fingerprints = {_json(item) for item in merged}
    for item in incoming:
        fingerprint = _json(item)
        if fingerprint not in fingerprints:
            merged.append(item)
            fingerprints.add(fingerprint)
    return tuple(merged)


def _validate_relation_targets(
    connection: sqlite3.Connection,
    proposal_id: str,
    payload: ReviewProposalInput,
) -> None:
    relation_ids = set(payload.near_proposal_ids) | set(payload.conflict_proposal_ids)
    relation_ids.update(payload.dependencies)
    if proposal_id in relation_ids:
        raise UserInputError("review proposal cannot relate to itself")
    for related_id in relation_ids:
        row = connection.execute(
            "SELECT status FROM review_proposals WHERE proposal_id = ?",
            (related_id,),
        ).fetchone()
        if row is None:
            raise UserInputError(f"related review proposal does not exist: {related_id}")


def _group_proposal_relations(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    near_proposal_ids: tuple[str, ...],
    conflict_proposal_ids: tuple[str, ...],
    created_at: str,
) -> None:
    relations = tuple((related_id, "near") for related_id in near_proposal_ids) + tuple(
        (related_id, "conflict") for related_id in conflict_proposal_ids
    )
    if not relations:
        return
    related_ids = tuple(dict.fromkeys(related_id for related_id, _ in relations))
    placeholders = ",".join("?" for _ in related_ids)
    group_rows = connection.execute(
        f"SELECT DISTINCT group_id FROM review_group_members "
        f"WHERE proposal_id IN ({placeholders}) ORDER BY group_id",
        related_ids,
    ).fetchall()
    existing_group_ids = tuple(
        row[0] for row in group_rows if isinstance(row[0], str)
    )
    if existing_group_ids:
        group_id = existing_group_ids[0]
        for merged_group_id in existing_group_ids[1:]:
            connection.execute(
                "UPDATE OR IGNORE review_group_members SET group_id = ? WHERE group_id = ?",
                (group_id, merged_group_id),
            )
            connection.execute(
                "DELETE FROM review_group_members WHERE group_id = ?",
                (merged_group_id,),
            )
            connection.execute(
                "DELETE FROM review_groups WHERE group_id = ?",
                (merged_group_id,),
            )
    else:
        group_id = f"grp_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO review_groups (group_id, kind, created_at) VALUES (?, 'near', ?)",
            (group_id, created_at),
        )
    member_ids = tuple(dict.fromkeys((proposal_id, *related_ids)))
    connection.executemany(
        "INSERT OR IGNORE INTO review_group_members (group_id, proposal_id) VALUES (?, ?)",
        ((group_id, member_id) for member_id in member_ids),
    )
    connection.executemany(
        "UPDATE review_proposals SET group_id = ? WHERE proposal_id = ?",
        ((group_id, member_id) for member_id in member_ids),
    )
    for related_id, relation_type in relations:
        first_id, second_id = sorted((proposal_id, related_id))
        connection.execute(
            """
            INSERT OR IGNORE INTO review_proposal_relations
                (first_proposal_id, second_proposal_id, relation_type)
            VALUES (?, ?, ?)
            """,
            (first_id, second_id, relation_type),
        )
        _append_relation_to_existing_proposal(
            connection,
            proposal_id=related_id,
            related_id=proposal_id,
            relation_type=relation_type,
            updated_at=created_at,
        )
    relation_kinds = {
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT relation.relation_type
            FROM review_proposal_relations AS relation
            JOIN review_group_members AS first_member
              ON first_member.proposal_id = relation.first_proposal_id
            JOIN review_group_members AS second_member
              ON second_member.proposal_id = relation.second_proposal_id
            WHERE first_member.group_id = ? AND second_member.group_id = ?
            """,
            (group_id, group_id),
        ).fetchall()
        if isinstance(row[0], str)
    }
    kind = "mixed" if len(relation_kinds) > 1 else next(iter(relation_kinds))
    connection.execute(
        "UPDATE review_groups SET kind = ? WHERE group_id = ?",
        (kind, group_id),
    )


def _append_relation_to_existing_proposal(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    related_id: str,
    relation_type: str,
    updated_at: str,
) -> None:
    column = (
        "near_proposal_ids_json"
        if relation_type == "near"
        else "conflict_proposal_ids_json"
    )
    row = connection.execute(
        f"SELECT {column} FROM review_proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise IntegrityError("related review proposal payload is invalid")
    try:
        related_ids = json.loads(row[0])
    except json.JSONDecodeError as error:
        raise IntegrityError("related review proposal payload is invalid") from error
    if not isinstance(related_ids, list) or not all(
        isinstance(item, str) for item in related_ids
    ):
        raise IntegrityError("related review proposal payload is invalid")
    if related_id in related_ids:
        return
    related_ids.append(related_id)
    connection.execute(
        f"UPDATE review_proposals SET {column} = ?, "
        "proposal_version = proposal_version + 1, updated_at = ? "
        "WHERE proposal_id = ?",
        (_json(related_ids), updated_at, proposal_id),
    )


def _review_group_from_row(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> ReviewGroup:
    if len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str):
        raise IntegrityError("unified review group is invalid")
    group_id = row[0]
    proposal_ids = tuple(
        member[0]
        for member in connection.execute(
            """
            SELECT proposal_id FROM review_group_members
            WHERE group_id = ? ORDER BY proposal_id
            """,
            (group_id,),
        ).fetchall()
        if isinstance(member[0], str)
    )
    relations = tuple(
        (relation[0], relation[1], relation[2])
        for relation in connection.execute(
            """
            SELECT relation.first_proposal_id, relation.second_proposal_id,
                   relation.relation_type
            FROM review_proposal_relations AS relation
            JOIN review_group_members AS first_member
              ON first_member.proposal_id = relation.first_proposal_id
            JOIN review_group_members AS second_member
              ON second_member.proposal_id = relation.second_proposal_id
            WHERE first_member.group_id = ? AND second_member.group_id = ?
            ORDER BY relation.first_proposal_id, relation.second_proposal_id,
                     relation.relation_type
            """,
            (group_id, group_id),
        ).fetchall()
        if all(isinstance(value, str) for value in relation)
    )
    return ReviewGroup(
        group_id=group_id,
        kind=row[1],
        proposal_ids=proposal_ids,
        relations=relations,
    )


def _insert_proposal(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    payload: ReviewProposalInput,
    exact_fingerprint: str,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO review_proposals
            (proposal_id, schema_version, proposal_version, group_id, title, content,
             intent, formation, priority, applicability_scope, approval_effect_json,
             target_json, supporting_evidence_json, opposing_evidence_json,
             dependencies_json, context_coverage_json, blind_spots_json,
             near_proposal_ids_json, conflict_proposal_ids_json, sensitivity,
             evidence_retention, migration_restrictions_json, exact_fingerprint,
             status, created_at, updated_at)
        VALUES (?, 1, 1, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'pending', ?, ?)
        """,
        (
            proposal_id,
            payload.title,
            payload.content,
            payload.intent,
            payload.formation,
            payload.priority,
            payload.applicability_scope,
            _json(payload.approval_effect.to_data()),
            _json(payload.target.to_data()),
            _json(list(payload.supporting_evidence)),
            _json(list(payload.opposing_evidence)),
            _json(list(payload.dependencies)),
            _json(list(payload.context_coverage)),
            _json(list(payload.blind_spots)),
            _json(list(payload.near_proposal_ids)),
            _json(list(payload.conflict_proposal_ids)),
            payload.sensitivity,
            payload.evidence_retention,
            _json(list(payload.migration_restrictions)),
            exact_fingerprint,
            created_at,
            created_at,
        ),
    )


def _proposal_from_row(row: tuple[object, ...]) -> ReviewProposal:
    if len(row) < 30:
        raise IntegrityError("unified review proposal row is incomplete")
    try:
        payload = ReviewProposalInput.from_data(
            {
                "title": row[4],
                "content": row[5],
                "intent": row[6],
                "formation": row[7],
                "priority": row[8],
                "applicability_scope": row[9],
                "approval_effect": json.loads(cast(str, row[10])),
                "target": json.loads(cast(str, row[11])),
                "supporting_evidence": json.loads(cast(str, row[12])),
                "opposing_evidence": json.loads(cast(str, row[13])),
                "dependencies": json.loads(cast(str, row[14])),
                "context_coverage": json.loads(cast(str, row[15])),
                "blind_spots": json.loads(cast(str, row[16])),
                "near_proposal_ids": json.loads(cast(str, row[17])),
                "conflict_proposal_ids": json.loads(cast(str, row[18])),
                "sensitivity": row[19],
                "evidence_retention": row[20],
                "migration_restrictions": json.loads(cast(str, row[21])),
            }
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise IntegrityError("unified review proposal payload is invalid") from error
    if (
        not isinstance(row[0], str)
        or not isinstance(row[2], int)
        or (row[3] is not None and not isinstance(row[3], str))
        or not isinstance(row[23], str)
        or not isinstance(row[24], str)
        or (row[26] is not None and not isinstance(row[26], str))
        or not isinstance(row[28], int)
        or (row[29] is not None and not isinstance(row[29], str))
    ):
        raise IntegrityError("unified review proposal metadata is invalid")
    return ReviewProposal(
        proposal_id=row[0],
        proposal_version=row[2],
        group_id=row[3],
        payload=payload,
        status=row[23],
        created_at=row[24],
        deferred_until=row[26],
        retry_count=row[28],
        last_error=row[29],
    )


def _copy_database(database_path: Path, prefix: str) -> Path:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=prefix,
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        return temporary_path
    except OSError as error:
        raise IntegrityError("cannot copy unified review database") from error


def _text(data: Mapping[object, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 8_192:
        raise UserInputError(f"review proposal {name} must contain 1 to 8192 characters")
    return value.strip()


def _optional_text(data: Mapping[object, object], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 8_192:
        raise UserInputError(
            f"review batch {name} must contain 1 to 8192 characters or be null"
        )
    return value.strip()


def _choice(
    data: Mapping[object, object],
    name: str,
    choices: tuple[str, ...],
) -> str:
    value = data.get(name)
    if value not in choices:
        raise UserInputError(
            f"review proposal {name} must be one of: {', '.join(choices)}"
        )
    return value


def _object(data: Mapping[object, object], name: str) -> dict[str, object]:
    value = data.get(name)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise UserInputError(f"review proposal {name} must be a JSON object")
    return cast(dict[str, object], value)


def _object_list(
    data: Mapping[object, object],
    name: str,
) -> tuple[dict[str, object], ...]:
    value = data.get(name)
    if not isinstance(value, list):
        raise UserInputError(f"review proposal {name} must be a JSON array")
    result: list[dict[str, object]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise UserInputError(f"review proposal {name} must contain JSON objects")
        result.append(cast(dict[str, object], item))
    return tuple(result)


def _text_list(data: Mapping[object, object], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UserInputError(f"review proposal {name} must be an array of text values")
    return tuple(cast(list[str], value))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _has_memory_tombstone(
    connection: sqlite3.Connection,
    memory_id: str,
) -> bool:
    fingerprint = "sha256:" + hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
    return connection.execute(
        """
        SELECT 1 FROM deletion_markers
        WHERE subject_kind = 'canonical-memory' AND subject_fingerprint = ?
        """,
        (fingerprint,),
    ).fetchone() is not None
