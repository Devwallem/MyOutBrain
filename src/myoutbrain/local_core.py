from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from typing import Literal, cast
import uuid

from myoutbrain.core_types import (
    ConfigurationConflict,
    IntegrityError,
    MemoryState,
    Sensitivity,
    UserInputError,
)
from myoutbrain.embeddings import (
    EmbeddingFailure,
    EmbeddingProvider,
    LocalMultilingualEmbeddingProvider,
    SEMANTIC_SIMILARITY_THRESHOLD,
    cosine_similarity,
    validate_embeddings,
)
from myoutbrain.persistence import (
    atomic_commit,
    event_journal_change,
    hold_writer_lock_for_acceptance_test,
    permanent_deletion_cleanup_change,
    recover_transactions,
    writer_lock,
)
from myoutbrain.retrieval import lexical_terms
from myoutbrain.reflection import (
    ImmediateReflectionRequest,
    ImmediateReflectionResult,
    LearningSignalCapture,
    LearningSignalSubmission,
    REFLECTION_SCHEMA,
    ReflectionInput,
    ReflectionAbandonmentRequest,
    read_reflection_inputs,
    stage_immediate_reflection,
    stage_reflection_abandonment,
    stage_learning_signal,
)
from myoutbrain.unified_review import (
    ReviewProposalInput,
    ReviewProposalSubmission,
    ReviewBatchRequest,
    ReviewBatchResult,
    ReviewExpirationResult,
    ReviewQueue,
    UNIFIED_REVIEW_SCHEMA,
    read_review_queue,
    merge_review_proposal_supporting_evidence,
    register_source_memory_proposal,
    stage_review_batch,
    stage_review_expiration,
    stage_review_proposal,
)


MEMORY_SCHEMA_VERSION = 9
MEMORY_DATABASE = "store/memory.sqlite3"
MEMORY_BODY_TARGET_BYTES = 4 * 1024
MEMORY_BODY_HARD_LIMIT_BYTES = 8 * 1024


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE source_objects (
    source_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    object_reference TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE evidence_sources (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind = 'local'),
    current_locator TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE evidence_source_versions (
    source_id TEXT NOT NULL REFERENCES evidence_sources(source_id),
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    locator TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    applicability_scope TEXT NOT NULL,
    retention TEXT NOT NULL CHECK (retention = 'receipt'),
    PRIMARY KEY (source_id, version)
);

CREATE TABLE knowledge_capsules (
    capsule_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    body_bytes INTEGER NOT NULL CHECK (body_bytes >= 0),
    memory_record_count INTEGER NOT NULL CHECK (memory_record_count >= 0),
    structural_version INTEGER NOT NULL CHECK (structural_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE knowledge_partitions (
    partition_id TEXT PRIMARY KEY,
    parent_partition_id TEXT REFERENCES knowledge_partitions(partition_id),
    node_kind TEXT NOT NULL CHECK (node_kind IN ('root', 'leaf')),
    topic TEXT NOT NULL,
    normalized_topic TEXT NOT NULL,
    CHECK (
        (node_kind = 'root' AND parent_partition_id IS NULL)
        OR (node_kind = 'leaf' AND parent_partition_id IS NOT NULL)
    )
);

CREATE TABLE capsule_partitions (
    capsule_id TEXT PRIMARY KEY REFERENCES knowledge_capsules(capsule_id),
    partition_id TEXT NOT NULL REFERENCES knowledge_partitions(partition_id)
);

CREATE TABLE experiences (
    experience_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_objects(source_id),
    occurred_at TEXT NOT NULL,
    entrance TEXT NOT NULL,
    task TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    visible_context TEXT NOT NULL,
    context_gaps_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE buffered_digests (
    digest_id TEXT PRIMARY KEY,
    experience_id TEXT NOT NULL UNIQUE REFERENCES experiences(experience_id),
    content TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('buffered', 'integrated')),
    created_at TEXT NOT NULL
);

CREATE TABLE canonical_memories (
    memory_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    state TEXT NOT NULL CHECK (state IN ('active', 'inactive')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE canonical_memory_sources (
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    source_id TEXT NOT NULL REFERENCES source_objects(source_id),
    PRIMARY KEY (memory_id, source_id)
);

CREATE TABLE canonical_memory_versions (
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    applicability_scope TEXT,
    capsule_id TEXT REFERENCES knowledge_capsules(capsule_id),
    action TEXT NOT NULL
        CHECK (action IN ('created', 'supplemented', 'revised')),
    change_reason TEXT,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    supersession_reason TEXT,
    PRIMARY KEY (memory_id, version)
);

CREATE TABLE canonical_memory_version_sources (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES source_objects(source_id),
    FOREIGN KEY (memory_id, version)
        REFERENCES canonical_memory_versions(memory_id, version),
    PRIMARY KEY (memory_id, version, source_id)
);

CREATE TABLE canonical_memory_version_evidence (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    relationship TEXT NOT NULL CHECK (relationship = 'supports'),
    FOREIGN KEY (memory_id, version)
        REFERENCES canonical_memory_versions(memory_id, version),
    FOREIGN KEY (source_id, source_version)
        REFERENCES evidence_source_versions(source_id, version),
    PRIMARY KEY (memory_id, version, source_id, source_version, relationship)
);

CREATE TABLE canonical_memory_relations (
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    related_memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    relationship TEXT NOT NULL CHECK (relationship = 'related'),
    created_at TEXT NOT NULL,
    CHECK (memory_id <> related_memory_id),
    PRIMARY KEY (memory_id, related_memory_id)
);

CREATE TABLE canonical_memory_conflicts (
    conflict_id TEXT PRIMARY KEY,
    first_memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    second_memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unresolved', 'resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (first_memory_id < second_memory_id),
    UNIQUE (first_memory_id, second_memory_id)
);

CREATE TABLE integration_proposals (
    proposal_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    proposed_understanding TEXT NOT NULL,
    possible_impact TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    suggested_action TEXT NOT NULL
        CHECK (suggested_action IN ('new', 'supplement', 'revise', 'conflict')),
    target_memory_id TEXT REFERENCES canonical_memories(memory_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE integration_proposal_buffered (
    proposal_id TEXT NOT NULL REFERENCES integration_proposals(proposal_id),
    digest_id TEXT NOT NULL REFERENCES buffered_digests(digest_id),
    PRIMARY KEY (proposal_id, digest_id)
);

CREATE TABLE integration_proposal_related (
    proposal_id TEXT NOT NULL REFERENCES integration_proposals(proposal_id),
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    PRIMARY KEY (proposal_id, memory_id)
);

CREATE TABLE integration_proposal_sources (
    proposal_id TEXT NOT NULL REFERENCES integration_proposals(proposal_id),
    source_id TEXT NOT NULL REFERENCES source_objects(source_id),
    PRIMARY KEY (proposal_id, source_id)
);

CREATE TABLE knowledge_dictionary (
    memory_id TEXT PRIMARY KEY REFERENCES canonical_memories(memory_id),
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    primary_capsule_id TEXT NOT NULL REFERENCES knowledge_capsules(capsule_id),
    FOREIGN KEY (memory_id, current_version)
        REFERENCES canonical_memory_versions(memory_id, version)
);

CREATE TABLE memory_names (
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    name_kind TEXT NOT NULL CHECK (name_kind IN ('canonical', 'alias')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, normalized_name)
);

CREATE INDEX memory_names_lookup ON memory_names(normalized_name, memory_id);
CREATE UNIQUE INDEX memory_names_one_canonical
ON memory_names(memory_id) WHERE name_kind = 'canonical';

CREATE TABLE memory_name_changes (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE canonical_memory_fts USING fts5(
    memory_id UNINDEXED,
    capsule_id UNINDEXED,
    canonical_name,
    body,
    applicability_scope,
    search_terms,
    tokenize = 'unicode61'
);

CREATE TABLE source_memory_proposal_details (
    proposal_id TEXT PRIMARY KEY REFERENCES integration_proposals(proposal_id),
    proposal_version INTEGER NOT NULL CHECK (proposal_version = 1),
    planned_memory_id TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    applicability_scope TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    FOREIGN KEY (source_id, source_version)
        REFERENCES evidence_source_versions(source_id, version)
);

CREATE TABLE source_memory_reuse_submissions (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE integration_reviews (
    review_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES integration_proposals(proposal_id),
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'edited', 'rejected')),
    action TEXT NOT NULL
        CHECK (action IN ('created', 'supplemented', 'revised', 'conflicted', 'rejected')),
    reviewed_content TEXT,
    reason TEXT,
    canonical_memory_id TEXT REFERENCES canonical_memories(memory_id),
    created_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    proposal_id TEXT,
    before_version INTEGER,
    after_version INTEGER,
    entrance TEXT NOT NULL,
    result_hash TEXT NOT NULL
);

CREATE TABLE idempotent_writes (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation, idempotency_key)
);

CREATE TABLE recall_events (
    recall_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    entrance TEXT NOT NULL,
    task TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    budget_limit_bytes INTEGER NOT NULL CHECK (budget_limit_bytes > 0),
    used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
    was_truncated INTEGER NOT NULL CHECK (was_truncated IN (0, 1)),
    answerable INTEGER NOT NULL CHECK (answerable IN (0, 1)),
    answerability_reason TEXT NOT NULL,
    answerability_overridden INTEGER NOT NULL
        CHECK (answerability_overridden IN (0, 1)),
    cross_partition_hit INTEGER NOT NULL CHECK (cross_partition_hit IN (0, 1)),
    ambiguity_detected INTEGER NOT NULL CHECK (ambiguity_detected IN (0, 1)),
    missing_dependency INTEGER NOT NULL CHECK (missing_dependency IN (0, 1)),
    unresolved_conflict INTEGER NOT NULL CHECK (unresolved_conflict IN (0, 1))
);

CREATE TABLE recall_event_items (
    recall_id TEXT NOT NULL REFERENCES recall_events(recall_id),
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    candidate_paths_json TEXT NOT NULL,
    PRIMARY KEY (recall_id, memory_id),
    FOREIGN KEY (memory_id, version)
        REFERENCES canonical_memory_versions(memory_id, version)
);

CREATE TABLE recall_evidence_expansions (
    recall_id TEXT NOT NULL REFERENCES recall_events(recall_id),
    memory_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    expanded_bytes INTEGER NOT NULL CHECK (expanded_bytes >= 0),
    was_truncated INTEGER NOT NULL CHECK (was_truncated IN (0, 1)),
    PRIMARY KEY (recall_id, memory_id, source_id, source_version),
    FOREIGN KEY (recall_id, memory_id)
        REFERENCES recall_event_items(recall_id, memory_id),
    FOREIGN KEY (source_id, source_version)
        REFERENCES evidence_source_versions(source_id, version)
);

CREATE TABLE memory_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE legacy_migration_runs (
    migration_id TEXT PRIMARY KEY,
    source_schema_version INTEGER NOT NULL,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'complete'),
    source_count INTEGER NOT NULL,
    insight_count INTEGER NOT NULL,
    cognition_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE legacy_audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE legacy_source_metadata (
    source_id TEXT PRIMARY KEY REFERENCES source_objects(source_id),
    sensitivity TEXT NOT NULL
        CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    origins_json TEXT NOT NULL,
    legacy_record_path TEXT NOT NULL
);

CREATE TABLE legacy_knowledge_metadata (
    memory_id TEXT PRIMARY KEY REFERENCES canonical_memories(memory_id),
    legacy_kind TEXT NOT NULL CHECK (legacy_kind IN ('insight', 'cognition')),
    legacy_state TEXT NOT NULL
        CHECK (legacy_state IN ('active', 'superseded', 'archived')),
    authorship TEXT NOT NULL CHECK (authorship IN ('user', 'system', 'mixed')),
    legacy_path TEXT NOT NULL,
    candidate_id TEXT,
    relations_json TEXT NOT NULL
);

CREATE TABLE deletion_markers (
    marker_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL
        CHECK (subject_kind IN ('canonical-memory', 'source')),
    subject_fingerprint TEXT NOT NULL UNIQUE,
    deleted_at TEXT NOT NULL,
    backup_exclusion_after TEXT NOT NULL
);
""" + UNIFIED_REVIEW_SCHEMA + REFLECTION_SCHEMA


MemoryDisposition = Literal["buffered", "duplicate"]
IntegrationAction = Literal["new", "supplement", "revise", "conflict"]
AppliedIntegrationAction = Literal[
    "created", "supplemented", "revised", "conflicted", "rejected"
]
MemoryLifecycleAction = Literal["deactivated", "reactivated"]


@dataclass(frozen=True)
class ExperienceMetadata:
    occurred_at: str
    entrance: str
    task: str
    sensitivity: Sensitivity
    visible_context: str
    context_gaps: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        occurred_at: str,
        entrance: str,
        task: str,
        sensitivity: Sensitivity,
        visible_context: str,
        context_gaps: tuple[str, ...],
    ) -> ExperienceMetadata:
        normalized_gaps = tuple(
            _required_text("context gap", gap) for gap in context_gaps
        )
        if not normalized_gaps:
            raise UserInputError("at least one explicit context gap is required")
        if sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError(f"invalid sensitivity: {sensitivity}")
        return cls(
            occurred_at=_validated_time(occurred_at),
            entrance=_required_text("entrance", entrance),
            task=_required_text("task", task),
            sensitivity=sensitivity,
            visible_context=_required_text("visible context", visible_context),
            context_gaps=normalized_gaps,
        )

    def identity_data(self, source_id: str) -> dict[str, object]:
        return {
            "source_id": source_id,
            "occurred_at": self.occurred_at,
            "entrance": self.entrance,
            "task": self.task,
            "sensitivity": self.sensitivity,
            "visible_context": self.visible_context,
            "context_gaps": self.context_gaps,
        }


@dataclass(frozen=True)
class BufferedMemoryReceipt:
    source_id: str
    experience_id: str
    digest_id: str
    digest: str
    disposition: MemoryDisposition
    metadata: ExperienceMetadata

    def to_data(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "experience_id": self.experience_id,
            "digest_id": self.digest_id,
            "digest": self.digest,
            "disposition": self.disposition,
            "state": "buffered",
            "canonical_memory_id": None,
            "occurred_at": self.metadata.occurred_at,
            "entrance": self.metadata.entrance,
            "task": self.metadata.task,
            "sensitivity": self.metadata.sensitivity,
            "visible_context": self.metadata.visible_context,
            "context_gaps": list(self.metadata.context_gaps),
        }


@dataclass(frozen=True)
class RecallableMemory:
    memory_id: str
    content: str
    memory_state: MemoryState
    source_ids: tuple[str, ...]
    occurred_at: str
    sensitivity: Sensitivity
    entrance: str | None
    task: str | None
    related_memory_ids: tuple[str, ...] = field(default=(), kw_only=True)
    conflict_memory_ids: tuple[str, ...] = field(default=(), kw_only=True)

    @property
    def confirmed(self) -> bool:
        return self.memory_state is MemoryState.CANONICAL


@dataclass(frozen=True)
class BufferedConsolidationItem:
    digest_id: str
    content: str
    source_id: str
    task: str
    sensitivity: Sensitivity


@dataclass(frozen=True)
class IntegrationProposal:
    proposal_id: str
    topic: str
    proposed_understanding: str
    evidence_memory_ids: tuple[str, ...]
    source_scope: tuple[str, ...]
    related_canonical_memory_ids: tuple[str, ...]
    possible_impact: str
    sensitivity: Sensitivity
    suggested_action: IntegrationAction
    target_memory_id: str | None
    status: str

    def to_data(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "topic": self.topic,
            "proposed_understanding": self.proposed_understanding,
            "evidence_memory_ids": list(self.evidence_memory_ids),
            "source_scope": list(self.source_scope),
            "related_canonical_memory_ids": list(
                self.related_canonical_memory_ids
            ),
            "possible_impact": self.possible_impact,
            "sensitivity": self.sensitivity,
            "suggested_action": self.suggested_action,
            "target_memory_id": self.target_memory_id,
            "status": self.status,
        }


@dataclass(frozen=True)
class SourceReceipt:
    source_id: str
    version: int
    content_hash: str
    locator: str
    observed_at: str
    applicability_scope: str
    retention: Literal["receipt"] = "receipt"

    def to_data(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "locator": self.locator,
            "observed_at": self.observed_at,
            "applicability_scope": self.applicability_scope,
            "retention": self.retention,
        }

def _register_local_evidence_source(
    connection: sqlite3.Connection,
    *,
    locator: str,
    content_hash: str,
    applicability_scope: str,
    existing_source_id: str | None,
    observed_at: str,
) -> SourceReceipt:
    if existing_source_id is None:
        source_row = connection.execute(
            "SELECT source_id FROM evidence_sources WHERE current_locator = ?",
            (locator,),
        ).fetchone()
    else:
        source_row = connection.execute(
            "SELECT source_id FROM evidence_sources WHERE source_id = ?",
            (existing_source_id,),
        ).fetchone()
        if source_row is None:
            raise UserInputError(
                f"local source does not exist: {existing_source_id}"
            )
    if source_row is None:
        source_id = f"src_{uuid.uuid4().hex}"
        source_version = 1
        receipt_observed_at = observed_at
        connection.execute(
            """
            INSERT INTO evidence_sources
                (source_id, source_kind, current_locator, created_at)
            VALUES (?, 'local', ?, ?)
            """,
            (source_id, locator, observed_at),
        )
        add_source_version = True
    else:
        source_id = source_row[0]
        if not isinstance(source_id, str):
            raise IntegrityError("local source identity is invalid")
        latest = connection.execute(
            """
            SELECT version, content_hash, applicability_scope, locator, observed_at
            FROM evidence_source_versions
            WHERE source_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (source_id,),
        ).fetchone()
        if (
            latest is None
            or not isinstance(latest[0], int)
            or not all(isinstance(latest[index], str) for index in (1, 2, 3, 4))
        ):
            raise IntegrityError("local source version is invalid")
        if (
            latest[1] == content_hash
            and latest[2] == applicability_scope
            and latest[3] == locator
        ):
            source_version = latest[0]
            receipt_observed_at = latest[4]
            add_source_version = False
        else:
            source_version = latest[0] + 1
            receipt_observed_at = observed_at
            add_source_version = True
        if latest[3] != locator:
            connection.execute(
                "UPDATE evidence_sources SET current_locator = ? WHERE source_id = ?",
                (locator, source_id),
            )
    if add_source_version:
        connection.execute(
            """
            INSERT INTO evidence_source_versions
                (source_id, version, content_hash, locator, observed_at,
                 applicability_scope, retention)
            VALUES (?, ?, ?, ?, ?, ?, 'receipt')
            """,
            (
                source_id,
                source_version,
                content_hash,
                locator,
                observed_at,
                applicability_scope,
            ),
        )
    return SourceReceipt(
        source_id=source_id,
        version=source_version,
        content_hash=content_hash,
        locator=locator,
        observed_at=receipt_observed_at,
        applicability_scope=applicability_scope,
    )



@dataclass(frozen=True)
class SourceMemoryProposal:
    proposal_id: str
    planned_memory_id: str
    canonical_name: str
    body: str
    applicability_scope: str
    source: SourceReceipt
    suggested_action: IntegrationAction = "new"
    target_memory_id: str | None = None
    target_version: int = 0
    disposition: Literal["proposal-created", "proposal-reused"] = "proposal-created"
    status: Literal["pending"] = "pending"
    proposal_version: int = 1

    def to_data(self) -> dict[str, object]:
        body_bytes = len(self.body.encode("utf-8"))
        approval_effect: str | None = (
            "create_source_backed_canonical_memory"
            if self.suggested_action == "new"
            else (
                None
                if self.suggested_action == "conflict"
                else "revise_canonical_memory"
            )
        )
        data: dict[str, object] = {
            "disposition": self.disposition,
            "proposal_id": self.proposal_id,
            "proposal_version": self.proposal_version,
            "status": self.status,
            "intent": "integrate",
            "formation": "explicit",
            "approval_effect": approval_effect,
            "suggested_action": self.suggested_action,
            "target_memory_id": self.target_memory_id,
            "target_version": self.target_version,
            "planned_memory_id": self.planned_memory_id,
            "proposed_memory": {
                "name": self.canonical_name,
                "body": self.body,
                "body_bytes": body_bytes,
                "scope": self.applicability_scope,
            },
            "body_budget": {
                "target_bytes": MEMORY_BODY_TARGET_BYTES,
                "hard_limit_bytes": MEMORY_BODY_HARD_LIMIT_BYTES,
                "within_target": body_bytes <= MEMORY_BODY_TARGET_BYTES,
            },
            "source": self.source.to_data(),
        }
        if self.suggested_action == "conflict":
            data["available_decisions"] = ["reject", "defer"]
            data["approval_unavailable_reason"] = (
                "conflict approval materialization is deferred to issue 09"
            )
        return data


@dataclass(frozen=True)
class SourceMemoryReuse:
    disposition: Literal["unchanged", "source-linked"]
    memory_id: str
    canonical_name: str
    current_version: int
    source: SourceReceipt

    def to_data(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "proposal_id": None,
            "memory_id": self.memory_id,
            "canonical_name": self.canonical_name,
            "current_version": self.current_version,
            "source": self.source.to_data(),
            "relationship": (
                "already-present"
                if self.disposition == "unchanged"
                else "supports-added"
            ),
        }


@dataclass(frozen=True)
class AuditEventReceipt:
    event_id: str
    event_type: str
    occurred_at: str
    before_version: int | None
    after_version: int
    entrance: str
    result_hash: str

    def to_data(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "entrance": self.entrance,
            "result_hash": self.result_hash,
        }


@dataclass(frozen=True)
class SourceMemoryApproval:
    proposal_id: str
    memory_id: str
    canonical_name: str
    body: str
    applicability_scope: str
    source: SourceReceipt
    capsule_id: str
    audit_event: AuditEventReceipt

    def to_data(self) -> dict[str, object]:
        body_bytes = len(self.body.encode("utf-8"))
        return {
            "proposal_id": self.proposal_id,
            "status": "applied",
            "decision": "approved",
            "memory": {
                "memory_id": self.memory_id,
                "current_version": 1,
                "state": "current",
                "name": self.canonical_name,
                "body": self.body,
                "body_bytes": body_bytes,
                "scope": self.applicability_scope,
            },
            "dictionary": {
                "memory_id": self.memory_id,
                "canonical_name": self.canonical_name,
                "current_version": 1,
                "primary_capsule_id": self.capsule_id,
            },
            "primary_capsule": {
                "capsule_id": self.capsule_id,
                "body_bytes": body_bytes,
                "memory_record_count": 1,
            },
            "source": self.source.to_data(),
            "audit_event": self.audit_event.to_data(),
        }


@dataclass(frozen=True)
class MemoryRenameResult:
    memory_id: str
    current_version: int
    previous_name: str
    canonical_name: str
    aliases: tuple[str, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "current_version": self.current_version,
            "previous_name": self.previous_name,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "alias_resolution": "direct-to-memory-id",
        }


@dataclass(frozen=True)
class IntegrationReviewResult:
    proposal_id: str
    decision: str
    canonical_memory_id: str | None
    canonical_content: str | None
    reason: str | None
    related_canonical_memory_ids: tuple[str, ...]
    action: AppliedIntegrationAction

    def to_data(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "decision": self.decision,
            "canonical_memory_id": self.canonical_memory_id,
            "canonical_content": self.canonical_content,
            "reason": self.reason,
            "action": self.action,
            "related_canonical_memory_ids": list(
                self.related_canonical_memory_ids
            ),
        }


@dataclass(frozen=True)
class _ReviewInstruction:
    decision: Literal["accepted", "edited", "rejected"]
    content: str | None
    reason: str | None
    action: IntegrationAction | None = None
    target_memory_id: str | None = None


@dataclass(frozen=True)
class _IntegrationProposalDraft:
    proposal_id: str
    topic: str
    proposed_understanding: str
    possible_impact: str
    sensitivity: Sensitivity
    digest_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    related_memory_ids: tuple[str, ...]
    suggested_action: IntegrationAction
    target_memory_id: str | None
    created_at: str

    def as_proposal(self) -> IntegrationProposal:
        return IntegrationProposal(
            proposal_id=self.proposal_id,
            topic=self.topic,
            proposed_understanding=self.proposed_understanding,
            evidence_memory_ids=self.digest_ids,
            source_scope=self.source_ids,
            related_canonical_memory_ids=self.related_memory_ids,
            possible_impact=self.possible_impact,
            sensitivity=self.sensitivity,
            suggested_action=self.suggested_action,
            target_memory_id=self.target_memory_id,
            status="pending",
        )


@dataclass(frozen=True)
class CanonicalMemoryVersion:
    version: int
    content: str
    action: str
    change_reason: str | None
    status: str
    supersession_reason: str | None
    source_ids: tuple[str, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "version": self.version,
            "content": self.content,
            "action": self.action,
            "change_reason": self.change_reason,
            "status": self.status,
            "supersession_reason": self.supersession_reason,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class UnresolvedMemoryConflict:
    memory_id: str
    content: str
    source_ids: tuple[str, ...]
    reason: str

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "source_ids": list(self.source_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MemoryLifecycleEvent:
    action: MemoryLifecycleAction
    occurred_at: str
    reason: str

    def to_data(self) -> dict[str, str]:
        return {
            "action": self.action,
            "occurred_at": self.occurred_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CanonicalMemoryStateChange:
    memory_id: str
    action: MemoryLifecycleAction
    occurred_at: str
    reason: str

    def to_data(self) -> dict[str, str]:
        return {
            "memory_id": self.memory_id,
            "action": self.action,
            "occurred_at": self.occurred_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MemoryDeletionImpact:
    memory_id: str
    source_ids: tuple[str, ...]
    shared_source_ids: tuple[str, ...]
    derived_digest_ids: tuple[str, ...]
    related_memory_ids: tuple[str, ...]
    conflict_memory_ids: tuple[str, ...]
    pending_proposal_ids: tuple[str, ...]
    proposal_ids_to_delete: tuple[str, ...]
    review_ids_to_delete: tuple[str, ...]

    @property
    def confirmation_token(self) -> str:
        scope = json.dumps(
            {
                "memory_id": self.memory_id,
                "source_ids": self.source_ids,
                "shared_source_ids": self.shared_source_ids,
                "derived_digest_ids": self.derived_digest_ids,
                "related_memory_ids": self.related_memory_ids,
                "conflict_memory_ids": self.conflict_memory_ids,
                "pending_proposal_ids": self.pending_proposal_ids,
                "proposal_ids_to_delete": self.proposal_ids_to_delete,
                "review_ids_to_delete": self.review_ids_to_delete,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"delete_{hashlib.sha256(scope).hexdigest()}"

    def to_data(self) -> dict[str, object]:
        return {
            "disposition": "preview",
            "scope": "one-canonical-memory",
            "memory_id": self.memory_id,
            "canonical_memory_count": 1,
            "source_ids": list(self.source_ids),
            "shared_source_ids": list(self.shared_source_ids),
            "derived_digest_ids": list(self.derived_digest_ids),
            "related_memory_ids": list(self.related_memory_ids),
            "conflict_memory_ids": list(self.conflict_memory_ids),
            "pending_proposal_ids": list(self.pending_proposal_ids),
            "proposal_ids_to_delete": list(self.proposal_ids_to_delete),
            "review_ids_to_delete": list(self.review_ids_to_delete),
            "confirmation_token": self.confirmation_token,
            "requires_confirmation": True,
        }


@dataclass(frozen=True)
class MemoryDeletionResult:
    memory_id: str
    removed_source_ids: tuple[str, ...]
    retained_shared_source_ids: tuple[str, ...]
    removed_digest_ids: tuple[str, ...]
    removed_proposal_ids: tuple[str, ...]
    deleted_at: str
    backup_exclusion_after: str
    existing_backup_clearance: str

    def to_data(self) -> dict[str, object]:
        return {
            "disposition": "deleted",
            "scope": "one-canonical-memory",
            "memory_id": self.memory_id,
            "removed_source_ids": list(self.removed_source_ids),
            "retained_shared_source_ids": list(self.retained_shared_source_ids),
            "removed_digest_ids": list(self.removed_digest_ids),
            "removed_proposal_ids": list(self.removed_proposal_ids),
            "deleted_at": self.deleted_at,
            "backup_exclusion_after": self.backup_exclusion_after,
            "existing_backup_clearance": self.existing_backup_clearance,
        }


@dataclass(frozen=True)
class MemoryStorageReport:
    evidence_source_ids: tuple[str, ...]
    evidence_bytes: int
    canonical_count: int
    canonical_version_count: int
    canonical_bytes: int
    buffer_count: int
    buffer_bytes: int
    rebuildable_index_count: int
    rebuildable_index_bytes: int

    def to_data(self) -> dict[str, object]:
        return {
            "evidence": {
                "count": len(self.evidence_source_ids),
                "bytes": self.evidence_bytes,
                "source_ids": list(self.evidence_source_ids),
            },
            "canonical": {
                "count": self.canonical_count,
                "version_count": self.canonical_version_count,
                "bytes": self.canonical_bytes,
            },
            "buffer": {
                "count": self.buffer_count,
                "bytes": self.buffer_bytes,
            },
            "rebuildable_indexes": {
                "count": self.rebuildable_index_count,
                "bytes": self.rebuildable_index_bytes,
            },
            "destructive_maintenance": "requires-explicit-approval",
        }


@dataclass(frozen=True)
class CanonicalMemoryAudit:
    memory_id: str
    state: Literal["active", "inactive"]
    confirmation_status: Literal["confirmed", "conflicted"]
    current_version: int
    current_content: str
    current_source_ids: tuple[str, ...]
    versions: tuple[CanonicalMemoryVersion, ...]
    unresolved_conflicts: tuple[UnresolvedMemoryConflict, ...]
    lifecycle_events: tuple[MemoryLifecycleEvent, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "state": self.state,
            "confirmation_status": self.confirmation_status,
            "current_version": self.current_version,
            "current_content": self.current_content,
            "current_source_ids": list(self.current_source_ids),
            "versions": [version.to_data() for version in self.versions],
            "unresolved_conflicts": [
                conflict.to_data() for conflict in self.unresolved_conflicts
            ],
            "lifecycle_events": [
                event.to_data() for event in self.lifecycle_events
            ],
        }


class LocalMemoryCore:
    """Own the durable private-instance memory state."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def initialize(self) -> None:
        configuration = self._root / "myoutbrain.toml"
        if not configuration.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        database_path = self._root / MEMORY_DATABASE
        with writer_lock(self._root):
            recover_transactions(self._root)
            database_change = self._database_initialization_change(database_path)
            if database_change is not None:
                atomic_commit(self._root, [database_change])

    def commit_v2_initialization(
        self,
        *,
        configuration_content: bytes | None,
        git_ignore_content: bytes | None,
    ) -> None:
        """Atomically coordinate every durable file in V2 initialization."""
        changes: list[tuple[Path, bytes]] = []
        if configuration_content is not None:
            changes.append((self._root / "myoutbrain.toml", configuration_content))
        database_path = self._root / MEMORY_DATABASE
        database_change = self._database_initialization_change(database_path)
        if database_change is not None:
            changes.append(database_change)
        if git_ignore_content is not None:
            changes.append((self._root / ".gitignore", git_ignore_content))
        if changes:
            atomic_commit(
                self._root,
                changes,
                fault_injections={0: "initialize-after-configuration"},
            )

    def inspect_schema_version(self) -> int:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        self._validate_database(database_path)
        return MEMORY_SCHEMA_VERSION

    def inspect_object_store(self) -> None:
        object_store = self._root / "store" / "objects" / "sha256"
        if not object_store.is_dir():
            raise IntegrityError(
                f"content-addressed object store is missing: {object_store}"
            )
        for object_path in object_store.rglob("*"):
            if not object_path.is_file():
                continue
            digest = object_path.name
            relative_parts = object_path.relative_to(object_store).parts
            if (
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or relative_parts != (digest[:2], digest[2:4], digest)
            ):
                raise IntegrityError(
                    f"invalid content-addressed object path: {object_path}"
                )
            try:
                actual_digest = hashlib.sha256(object_path.read_bytes()).hexdigest()
            except OSError as error:
                raise IntegrityError(
                    f"cannot read content-addressed object: {object_path}"
                ) from error
            if actual_digest != digest:
                raise IntegrityError(
                    f"content-addressed object hash mismatch: {object_path}"
                )

    def propose_source_memory(
        self,
        source_path: Path,
        *,
        source_id: str | None = None,
        canonical_name: str,
        body: str,
        applicability_scope: str,
        idempotency_key: str,
    ) -> SourceMemoryProposal | SourceMemoryReuse:
        source_body = _read_local_source(source_path)
        normalized_name = _bounded_text("memory name", canonical_name, maximum=500)
        normalized_body = _validated_memory_body(body)
        normalized_scope = _bounded_text(
            "memory applicability scope", applicability_scope, maximum=1_000
        )
        normalized_key = _bounded_text(
            "idempotency key", idempotency_key, maximum=200
        )
        normalized_source_id = (
            _validated_source_id(source_id) if source_id is not None else None
        )
        locator = str(source_path.resolve())
        content_hash = f"sha256:{hashlib.sha256(source_body).hexdigest()}"
        request_hash = _stable_hash(
            {
                "operation": "propose-source-memory",
                "locator": locator,
                "content_hash": content_hash,
                "source_id": normalized_source_id,
                "canonical_name": normalized_name,
                "body": normalized_body,
                "applicability_scope": normalized_scope,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            hold_writer_lock_for_acceptance_test()
            recover_transactions(self._root)
            self._validate_database(database_path)
            existing = self._source_memory_proposal_for_key(
                database_path,
                normalized_key,
            )
            if existing is not None:
                proposal, existing_request_hash = existing
                if existing_request_hash != request_hash:
                    raise UserInputError(
                        "idempotency key was already used for a different request"
                    )
                return proposal
            existing_reuse = self._source_memory_reuse_for_key(
                database_path,
                normalized_key,
            )
            if existing_reuse is not None:
                reuse, existing_request_hash = existing_reuse
                if existing_request_hash != request_hash:
                    raise UserInputError(
                        "idempotency key was already used for a different request"
                    )
                return reuse
            exact = self._exact_source_memory_match(
                database_path,
                locator=locator,
                content_hash=content_hash,
                body=normalized_body,
                applicability_scope=normalized_scope,
                existing_source_id=normalized_source_id,
            )
            if exact is not None:
                staged_database = self._database_with_source_memory_replay_record(
                    database_path,
                    idempotency_key=normalized_key,
                    request_hash=request_hash,
                    result=exact,
                )
                atomic_commit(self._root, [(database_path, staged_database)])
                return exact
            exact_memory = self._exact_memory_match(
                database_path,
                body=normalized_body,
                applicability_scope=normalized_scope,
            )
            if exact_memory is not None:
                staged_database, reuse = self._database_with_supporting_source(
                    database_path,
                    locator=locator,
                    content_hash=content_hash,
                    applicability_scope=normalized_scope,
                    existing_source_id=normalized_source_id,
                    memory_id=exact_memory[0],
                    canonical_name=exact_memory[1],
                    current_version=exact_memory[2],
                    idempotency_key=normalized_key,
                    request_hash=request_hash,
                )
                atomic_commit(self._root, [(database_path, staged_database)])
                return reuse
            near_memory = self._near_memory_match(
                database_path,
                body=normalized_body,
                applicability_scope=normalized_scope,
            )
            suggested_action: IntegrationAction = "new"
            target_memory_id: str | None = None
            target_version = 0
            near_proposal_ids: tuple[str, ...] = ()
            conflict_proposal_ids: tuple[str, ...] = ()
            if near_memory is not None:
                suggested_action = (
                    "conflict"
                    if _memory_bodies_conflict(normalized_body, near_memory[4])
                    else (
                        "revise"
                        if near_memory[3]
                        == " ".join(normalized_scope.casefold().split())
                        else "supplement"
                    )
                )
                target_memory_id = near_memory[0]
                target_version = near_memory[2]
                related_ids = self._source_proposal_ids_for_memory(
                    database_path,
                    memory_id=target_memory_id,
                )
                if suggested_action == "conflict":
                    conflict_proposal_ids = related_ids
                else:
                    near_proposal_ids = related_ids
            pending = self._pending_source_memory_proposal(
                database_path,
                canonical_name=normalized_name,
                body=normalized_body,
                applicability_scope=normalized_scope,
                suggested_action=suggested_action,
                target_memory_id=target_memory_id,
                target_version=target_version,
            )
            if pending is not None:
                pending_source = self._pending_source_memory_evidence_receipt(
                    database_path,
                    proposal_id=pending.proposal_id,
                    locator=locator,
                    content_hash=content_hash,
                    source_id=normalized_source_id,
                )
                if pending_source is not None:
                    replay = replace(
                        pending,
                        source=pending_source,
                        disposition="proposal-reused",
                    )
                    staged_database = self._database_with_source_memory_replay_record(
                        database_path,
                        idempotency_key=normalized_key,
                        request_hash=request_hash,
                        result=replay,
                    )
                    atomic_commit(self._root, [(database_path, staged_database)])
                    return replay
                staged_database, merged = (
                    self._database_with_pending_source_memory_evidence(
                        database_path,
                        pending=pending,
                        locator=locator,
                        content_hash=content_hash,
                        applicability_scope=normalized_scope,
                        existing_source_id=normalized_source_id,
                        idempotency_key=normalized_key,
                        request_hash=request_hash,
                    )
                )
                atomic_commit(
                    self._root,
                    [(database_path, staged_database)],
                    fault_injections={0: "source-memory-evidence-after-database"},
                )
                return merged
            staged_database, proposal = self._database_with_source_memory_proposal(
                database_path,
                locator=locator,
                content_hash=content_hash,
                canonical_name=normalized_name,
                body=normalized_body,
                applicability_scope=normalized_scope,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                existing_source_id=normalized_source_id,
                suggested_action=suggested_action,
                target_memory_id=target_memory_id,
                target_version=target_version,
                near_proposal_ids=near_proposal_ids,
                conflict_proposal_ids=conflict_proposal_ids,
            )
            atomic_commit(
                self._root,
                [(database_path, staged_database)],
                fault_injections={0: "source-memory-proposal-after-database"},
            )
        return proposal

    @staticmethod
    def _exact_source_memory_match(
        database_path: Path,
        *,
        locator: str,
        content_hash: str,
        body: str,
        applicability_scope: str,
        existing_source_id: str | None,
    ) -> SourceMemoryReuse | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                if existing_source_id is None:
                    source_row = connection.execute(
                        "SELECT source_id FROM evidence_sources WHERE current_locator = ?",
                        (locator,),
                    ).fetchone()
                else:
                    source_row = connection.execute(
                        "SELECT source_id FROM evidence_sources WHERE source_id = ?",
                        (existing_source_id,),
                    ).fetchone()
                if source_row is None or not isinstance(source_row[0], str):
                    return None
                source_id = source_row[0]
                rows = connection.execute(
                    """
                    SELECT dictionary.memory_id, dictionary.canonical_name,
                           dictionary.current_version, version.content,
                           version.applicability_scope, evidence.source_version,
                           source.content_hash, source.locator, source.observed_at,
                           source.applicability_scope
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = dictionary.memory_id
                     AND version.version = dictionary.current_version
                    JOIN canonical_memory_version_evidence AS evidence
                      ON evidence.memory_id = version.memory_id
                     AND evidence.version = version.version
                    JOIN evidence_source_versions AS source
                      ON source.source_id = evidence.source_id
                     AND source.version = evidence.source_version
                    WHERE evidence.source_id = ? AND source.content_hash = ?
                    ORDER BY dictionary.memory_id, evidence.source_version
                    """,
                    (source_id, content_hash),
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot check exact source memory reuse") from error
        normalized_body = _normalized_memory_body(body)
        normalized_scope = " ".join(applicability_scope.casefold().split())
        for row in rows:
            if (
                not all(isinstance(row[index], str) for index in (0, 1, 3, 4, 6, 7, 8, 9))
                or not isinstance(row[2], int)
                or not isinstance(row[5], int)
            ):
                raise IntegrityError("exact source memory reuse state is invalid")
            if (
                _normalized_memory_body(row[3]) == normalized_body
                and " ".join(row[4].casefold().split()) == normalized_scope
            ):
                return SourceMemoryReuse(
                    disposition="unchanged",
                    memory_id=row[0],
                    canonical_name=row[1],
                    current_version=row[2],
                    source=SourceReceipt(
                        source_id=source_id,
                        version=row[5],
                        content_hash=row[6],
                        locator=row[7],
                        observed_at=row[8],
                        applicability_scope=row[9],
                    ),
                )
        return None

    @staticmethod
    def _source_memory_reuse_for_key(
        database_path: Path,
        idempotency_key: str,
    ) -> tuple[SourceMemoryProposal | SourceMemoryReuse, str] | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT request_hash, result_json
                    FROM source_memory_reuse_submissions
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot read idempotent source memory reuse") from error
        if row is None:
            return None
        if not isinstance(row[0], str) or not isinstance(row[1], str):
            raise IntegrityError("idempotent source memory reuse is invalid")
        data = json.loads(row[1])
        if not isinstance(data, dict):
            raise IntegrityError("stored source memory reuse result is invalid")
        source = data.get("source")
        stored_disposition = data.get("disposition")
        if stored_disposition in ("proposal-created", "proposal-reused"):
            proposed_memory = data.get("proposed_memory")
            if (
                not isinstance(data.get("proposal_id"), str)
                or not isinstance(data.get("proposal_version"), int)
                or not isinstance(data.get("planned_memory_id"), str)
                or data.get("suggested_action")
                not in ("new", "supplement", "revise", "conflict")
                or (
                    data.get("target_memory_id") is not None
                    and not isinstance(data.get("target_memory_id"), str)
                )
                or not isinstance(data.get("target_version"), int)
                or not isinstance(proposed_memory, dict)
                or not isinstance(proposed_memory.get("name"), str)
                or not isinstance(proposed_memory.get("body"), str)
                or not isinstance(proposed_memory.get("scope"), str)
                or not isinstance(source, dict)
                or not isinstance(source.get("source_id"), str)
                or not isinstance(source.get("version"), int)
                or not isinstance(source.get("content_hash"), str)
                or not isinstance(source.get("locator"), str)
                or not isinstance(source.get("observed_at"), str)
                or not isinstance(source.get("applicability_scope"), str)
            ):
                raise IntegrityError("stored source memory proposal replay is invalid")
            return (
                SourceMemoryProposal(
                    proposal_id=data["proposal_id"],
                    proposal_version=data["proposal_version"],
                    planned_memory_id=data["planned_memory_id"],
                    canonical_name=proposed_memory["name"],
                    body=proposed_memory["body"],
                    applicability_scope=proposed_memory["scope"],
                    source=SourceReceipt(
                        source_id=source["source_id"],
                        version=source["version"],
                        content_hash=source["content_hash"],
                        locator=source["locator"],
                        observed_at=source["observed_at"],
                        applicability_scope=source["applicability_scope"],
                    ),
                    suggested_action=data["suggested_action"],
                    target_memory_id=data["target_memory_id"],
                    target_version=data["target_version"],
                    disposition=stored_disposition,
                ),
                row[0],
            )
        if (
            data.get("disposition") not in ("unchanged", "source-linked")
            or not isinstance(data.get("memory_id"), str)
            or not isinstance(data.get("canonical_name"), str)
            or not isinstance(data.get("current_version"), int)
            or not isinstance(source, dict)
            or not isinstance(source.get("source_id"), str)
            or not isinstance(source.get("version"), int)
            or not isinstance(source.get("content_hash"), str)
            or not isinstance(source.get("locator"), str)
            or not isinstance(source.get("observed_at"), str)
            or not isinstance(source.get("applicability_scope"), str)
        ):
            raise IntegrityError("stored source memory reuse result is invalid")
        disposition: Literal["unchanged", "source-linked"] = data["disposition"]
        return (
            SourceMemoryReuse(
                disposition=disposition,
                memory_id=data["memory_id"],
                canonical_name=data["canonical_name"],
                current_version=data["current_version"],
                source=SourceReceipt(
                    source_id=source["source_id"],
                    version=source["version"],
                    content_hash=source["content_hash"],
                    locator=source["locator"],
                    observed_at=source["observed_at"],
                    applicability_scope=source["applicability_scope"],
                ),
            ),
            row[0],
        )

    @staticmethod
    def _database_with_source_memory_replay_record(
        database_path: Path,
        *,
        idempotency_key: str,
        request_hash: str,
        result: SourceMemoryProposal | SourceMemoryReuse,
    ) -> bytes:
        temporary_path: Path | None = None
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".source-memory-replay.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO source_memory_reuse_submissions
                        (idempotency_key, request_hash, result_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        request_hash,
                        json.dumps(
                            result.to_data(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        created_at,
                    ),
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage source memory replay record") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _exact_memory_match(
        database_path: Path,
        *,
        body: str,
        applicability_scope: str,
    ) -> tuple[str, str, int] | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT dictionary.memory_id, dictionary.canonical_name,
                           dictionary.current_version, version.content,
                           version.applicability_scope
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = dictionary.memory_id
                     AND version.version = dictionary.current_version
                    JOIN canonical_memories AS memory
                      ON memory.memory_id = dictionary.memory_id
                    WHERE memory.state = 'active'
                    ORDER BY dictionary.memory_id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot check exact memory reuse") from error
        normalized_body = _normalized_memory_body(body)
        normalized_scope = " ".join(applicability_scope.casefold().split())
        matches: list[tuple[str, str, int]] = []
        for row in rows:
            if (
                not all(isinstance(row[index], str) for index in (0, 1, 3, 4))
                or not isinstance(row[2], int)
            ):
                raise IntegrityError("exact memory reuse state is invalid")
            if (
                _normalized_memory_body(row[3]) == normalized_body
                and " ".join(row[4].casefold().split()) == normalized_scope
            ):
                matches.append((row[0], row[1], row[2]))
        if len(matches) > 1:
            raise IntegrityError("multiple canonical memories have identical content and scope")
        return matches[0] if matches else None

    @staticmethod
    def _near_memory_match(
        database_path: Path,
        *,
        body: str,
        applicability_scope: str,
    ) -> tuple[str, str, int, str, str] | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT dictionary.memory_id, dictionary.canonical_name,
                           dictionary.current_version, version.content,
                           version.applicability_scope
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = dictionary.memory_id
                     AND version.version = dictionary.current_version
                    JOIN canonical_memories AS memory
                      ON memory.memory_id = dictionary.memory_id
                    WHERE memory.state = 'active'
                    ORDER BY dictionary.memory_id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot check near memory variants") from error
        incoming_terms = lexical_terms(body)
        if not incoming_terms:
            return None
        ranked: list[tuple[float, str, str, int, str, str]] = []
        for row in rows:
            if (
                not all(isinstance(row[index], str) for index in (0, 1, 3, 4))
                or not isinstance(row[2], int)
            ):
                raise IntegrityError("near memory state is invalid")
            existing_terms = lexical_terms(row[3])
            if not existing_terms:
                continue
            overlap = len(incoming_terms.intersection(existing_terms))
            similarity = overlap / min(len(incoming_terms), len(existing_terms))
            if similarity >= 0.6:
                ranked.append(
                    (
                        similarity,
                        row[0],
                        row[1],
                        row[2],
                        " ".join(row[4].casefold().split()),
                        row[3],
                    )
                )
        if not ranked:
            return None
        best = sorted(ranked, key=lambda item: (-item[0], item[1]))[0]
        return best[1], best[2], best[3], best[4], best[5]

    @staticmethod
    def _database_with_supporting_source(
        database_path: Path,
        *,
        locator: str,
        content_hash: str,
        applicability_scope: str,
        existing_source_id: str | None,
        memory_id: str,
        canonical_name: str,
        current_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[bytes, SourceMemoryReuse]:
        temporary_path: Path | None = None
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".source-memory-support.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                source = _register_local_evidence_source(
                    connection,
                    locator=locator,
                    content_hash=content_hash,
                    applicability_scope=applicability_scope,
                    existing_source_id=existing_source_id,
                    observed_at=created_at,
                )
                source_id = source.source_id
                source_version = source.version
                connection.execute(
                    """
                    INSERT INTO canonical_memory_version_evidence
                        (memory_id, version, source_id, source_version, relationship)
                    VALUES (?, ?, ?, ?, 'supports')
                    """,
                    (memory_id, current_version, source_id, source_version),
                )
                result_hash = _stable_hash(
                    {
                        "memory_id": memory_id,
                        "version": current_version,
                        "source_id": source_id,
                        "source_version": source_version,
                        "relationship": "supports",
                    }
                )
                connection.execute(
                    """
                    INSERT INTO audit_events
                        (event_id, event_type, occurred_at, subject_id, proposal_id,
                         before_version, after_version, entrance, result_hash)
                    VALUES (?, 'source.relationship-added', ?, ?, NULL, ?, ?,
                            'source-submission', ?)
                    """,
                    (
                        f"aud_{uuid.uuid4().hex}",
                        created_at,
                        memory_id,
                        current_version,
                        current_version,
                        result_hash,
                    ),
                )
                result = SourceMemoryReuse(
                    disposition="source-linked",
                    memory_id=memory_id,
                    canonical_name=canonical_name,
                    current_version=current_version,
                    source=source,
                )
                connection.execute(
                    """
                    INSERT INTO source_memory_reuse_submissions
                        (idempotency_key, request_hash, result_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        request_hash,
                        json.dumps(
                            result.to_data(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        created_at,
                    ),
                )
                connection.commit()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise IntegrityError("source support left a dangling reference")
            return temporary_path.read_bytes(), result
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage source support") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def approve_source_memory(
        self,
        proposal_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> SourceMemoryApproval:
        normalized_proposal_id = _bounded_text(
            "integration proposal id", proposal_id, maximum=200
        )
        normalized_key = _bounded_text(
            "idempotency key", idempotency_key, maximum=200
        )
        normalized_entrance = _bounded_text("entrance", entrance, maximum=100)
        if expected_version != 0:
            raise UserInputError(
                "first-memory approval expected_version must be 0"
            )
        request_hash = _stable_hash(
            {
                "operation": "approve-source-memory",
                "proposal_id": normalized_proposal_id,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            hold_writer_lock_for_acceptance_test()
            recover_transactions(self._root)
            self._validate_database(database_path)
            existing = self._source_memory_approval_for_key(
                database_path,
                normalized_key,
            )
            if existing is not None:
                approval, existing_request_hash = existing
                if existing_request_hash != request_hash:
                    raise UserInputError(
                        "idempotency key was already used for a different request"
                    )
                return approval
            staged_database, approval = self._database_with_source_memory_approval(
                database_path,
                proposal_id=normalized_proposal_id,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
            )
            atomic_commit(
                self._root,
                [(database_path, staged_database)],
                fault_injections={0: "source-memory-approval-after-database"},
            )
        return approval

    def rename_memory(
        self,
        memory_id: str,
        *,
        canonical_name: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> MemoryRenameResult:
        normalized_memory_id = _bounded_text("memory id", memory_id, maximum=200)
        normalized_name = _bounded_text("memory name", canonical_name, maximum=500)
        normalized_key = _bounded_text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _bounded_text("entrance", entrance, maximum=100)
        if expected_version < 1:
            raise UserInputError("memory rename expected_version must be positive")
        request_hash = _stable_hash(
            {
                "operation": "rename-memory",
                "memory_id": normalized_memory_id,
                "canonical_name": normalized_name,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            existing = self._memory_rename_for_key(database_path, normalized_key)
            if existing is not None:
                result, existing_request_hash = existing
                if existing_request_hash != request_hash:
                    raise UserInputError(
                        "idempotency key was already used for a different request"
                    )
                return result
            staged_database, result = self._database_with_memory_rename(
                database_path,
                memory_id=normalized_memory_id,
                canonical_name=normalized_name,
                expected_version=expected_version,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    @staticmethod
    def _memory_rename_for_key(
        database_path: Path,
        idempotency_key: str,
    ) -> tuple[MemoryRenameResult, str] | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT request_hash, result_json
                    FROM memory_name_changes
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot read idempotent memory rename") from error
        if row is None:
            return None
        if not isinstance(row[0], str) or not isinstance(row[1], str):
            raise IntegrityError("idempotent memory rename is invalid")
        data = json.loads(row[1])
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("memory_id"), str)
            or not isinstance(data.get("current_version"), int)
            or not isinstance(data.get("previous_name"), str)
            or not isinstance(data.get("canonical_name"), str)
            or not isinstance(data.get("aliases"), list)
            or not all(isinstance(alias, str) for alias in data["aliases"])
        ):
            raise IntegrityError("stored memory rename result is invalid")
        return (
            MemoryRenameResult(
                memory_id=data["memory_id"],
                current_version=data["current_version"],
                previous_name=data["previous_name"],
                canonical_name=data["canonical_name"],
                aliases=tuple(data["aliases"]),
            ),
            row[0],
        )

    @staticmethod
    def _database_with_memory_rename(
        database_path: Path,
        *,
        memory_id: str,
        canonical_name: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        entrance: str,
    ) -> tuple[bytes, MemoryRenameResult]:
        temporary_path: Path | None = None
        renamed_at = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-rename.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                row = connection.execute(
                    """
                    SELECT dictionary.canonical_name, dictionary.current_version
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memories AS memory
                      ON memory.memory_id = dictionary.memory_id
                    WHERE dictionary.memory_id = ? AND memory.state = 'active'
                    """,
                    (memory_id,),
                ).fetchone()
                if (
                    row is None
                    or not isinstance(row[0], str)
                    or row[1] != expected_version
                ):
                    raise UserInputError("memory rename target version conflict")
                previous_name = row[0]
                normalized_name = " ".join(canonical_name.casefold().split())
                connection.execute(
                    "UPDATE memory_names SET name_kind = 'alias' WHERE memory_id = ?",
                    (memory_id,),
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
                    (memory_id, canonical_name, normalized_name, renamed_at),
                )
                connection.execute(
                    """
                    UPDATE knowledge_dictionary
                    SET canonical_name = ?, normalized_name = ?
                    WHERE memory_id = ? AND current_version = ?
                    """,
                    (canonical_name, normalized_name, memory_id, expected_version),
                )
                connection.execute(
                    "UPDATE canonical_memory_fts SET canonical_name = ? WHERE memory_id = ?",
                    (canonical_name, memory_id),
                )
                alias_rows = connection.execute(
                    """
                    SELECT name FROM memory_names
                    WHERE memory_id = ? AND name_kind = 'alias'
                    ORDER BY normalized_name
                    """,
                    (memory_id,),
                ).fetchall()
                aliases = tuple(row[0] for row in alias_rows if isinstance(row[0], str))
                result = MemoryRenameResult(
                    memory_id=memory_id,
                    current_version=expected_version,
                    previous_name=previous_name,
                    canonical_name=canonical_name,
                    aliases=aliases,
                )
                result_json = json.dumps(
                    result.to_data(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                result_hash = _stable_hash(result.to_data())
                connection.execute(
                    """
                    INSERT INTO audit_events
                        (event_id, event_type, occurred_at, subject_id, proposal_id,
                         before_version, after_version, entrance, result_hash)
                    VALUES (?, 'memory.renamed', ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        f"aud_{uuid.uuid4().hex}",
                        renamed_at,
                        memory_id,
                        expected_version,
                        expected_version,
                        entrance,
                        result_hash,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_name_changes
                        (idempotency_key, request_hash, memory_id, result_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, request_hash, memory_id, result_json, renamed_at),
                )
                connection.commit()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise IntegrityError("memory rename left a dangling reference")
            return temporary_path.read_bytes(), result
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage memory rename") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def submit_review_proposal(
        self,
        payload: ReviewProposalInput,
        *,
        idempotency_key: str,
    ) -> ReviewProposalSubmission:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            staged_database, submission = stage_review_proposal(
                database_path,
                payload,
                idempotency_key=idempotency_key,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return submission

    def submit_learning_signal(
        self,
        submission: LearningSignalSubmission,
        *,
        idempotency_key: str,
    ) -> LearningSignalCapture:
        if submission.signal_kind is None:
            return LearningSignalCapture(False, None)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            staged_database, capture = stage_learning_signal(
                database_path,
                submission,
                idempotency_key=idempotency_key,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return capture

    def reflection_inputs(
        self,
        *,
        limit: int,
        budget_bytes: int,
    ) -> tuple[tuple[ReflectionInput, ...], bool, int]:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        self._validate_database(database_path)
        return read_reflection_inputs(
            database_path,
            limit=limit,
            budget_bytes=budget_bytes,
        )

    def reflect_now(
        self,
        request: ImmediateReflectionRequest,
        *,
        idempotency_key: str,
    ) -> ImmediateReflectionResult:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            staged_database, result = stage_immediate_reflection(
                database_path,
                request,
                idempotency_key=idempotency_key,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def abandon_reflection(
        self,
        request: ReflectionAbandonmentRequest,
        *,
        idempotency_key: str,
    ) -> ImmediateReflectionResult:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            staged_database, result = stage_reflection_abandonment(
                database_path,
                request,
                idempotency_key=idempotency_key,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def review_queue(self) -> ReviewQueue:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        self._validate_database(database_path)
        return read_review_queue(database_path)

    def decide_review_batch(
        self,
        request: ReviewBatchRequest,
        *,
        idempotency_key: str,
        entrance: str,
    ) -> ReviewBatchResult:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            staged_database, result = stage_review_batch(
                database_path,
                request,
                idempotency_key=idempotency_key,
                entrance=entrance,
            )
            atomic_commit(
                self._root,
                [(database_path, staged_database)],
                fault_injections={0: "review-batch-after-database"},
            )
        return result

    def expire_review_proposals(
        self,
        *,
        as_of: str,
        retention_days: int = 90,
    ) -> ReviewExpirationResult:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            staged_database, result = stage_review_expiration(
                database_path,
                as_of=as_of,
                retention_days=retention_days,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def _database_initialization_change(
        self,
        database_path: Path,
    ) -> tuple[Path, bytes] | None:
        if not database_path.exists():
            return database_path, self._new_database_content(database_path.parent)
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"expected a canonical database at: {database_path}"
            )
        version = self._database_version(database_path)
        if version == MEMORY_SCHEMA_VERSION:
            self._validate_database(database_path)
            return None
        return database_path, self._upgraded_database_content(database_path)

    def _upgraded_database_content(self, database_path: Path) -> bytes:
        temporary_path: Path | None = None
        migrators = {
            1: self._migrate_v1_database,
            2: self._migrate_v2_database,
            3: self._migrate_v3_database,
            4: self._migrate_v4_database,
            5: self._migrate_v5_database,
            6: self._migrate_v6_database,
            7: self._migrate_v7_database,
            8: self._migrate_v8_database,
        }
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-upgrade.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            version = self._database_version(temporary_path)
            while version != MEMORY_SCHEMA_VERSION:
                migrator = migrators.get(version)
                if migrator is None:
                    raise ConfigurationConflict(
                        f"unsupported memory schema version {version}: {database_path}"
                    )
                temporary_path.write_bytes(migrator(temporary_path))
                version = self._database_version(temporary_path)
            self._validate_database(temporary_path)
            return temporary_path.read_bytes()
        except OSError as error:
            raise IntegrityError("cannot stage the local memory database upgrade") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def capture_experience(
        self,
        conversation_path: Path,
        *,
        occurred_at: str,
        entrance: str,
        task: str,
        memory_digest: str,
        sensitivity: Sensitivity,
        visible_context: str,
        context_gaps: tuple[str, ...],
    ) -> BufferedMemoryReceipt:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        body = _read_conversation(conversation_path)
        metadata = ExperienceMetadata.create(
            occurred_at=occurred_at,
            entrance=entrance,
            task=task,
            sensitivity=sensitivity,
            visible_context=visible_context,
            context_gaps=context_gaps,
        )

        source_digest = hashlib.sha256(body).hexdigest()
        source_id = f"src_{source_digest}"
        identity_document = json.dumps(
            metadata.identity_data(source_id),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        experience_id = f"exp_{hashlib.sha256(identity_document).hexdigest()}"
        object_path = (
            self._root
            / "store"
            / "objects"
            / "sha256"
            / source_digest[:2]
            / source_digest[2:4]
            / source_digest
        )
        object_reference = object_path.relative_to(
            self._root / "store" / "objects"
        ).as_posix()
        digest = _validated_digest(memory_digest, body.decode("utf-8"), source_id)

        with writer_lock(self._root):
            hold_writer_lock_for_acceptance_test()
            recover_transactions(self._root)
            self._validate_database(database_path)
            if self._has_deletion_marker(
                database_path,
                subject_kind="source",
                subject_id=source_id,
            ):
                raise UserInputError(
                    "source was permanently deleted and cannot be re-imported"
                )
            _validate_content_object(object_path, body, source_digest)
            duplicate = self._duplicate_receipt(
                database_path,
                experience_id=experience_id,
                source_id=source_id,
                metadata=metadata,
                expected_digest=digest,
            )
            if duplicate is not None:
                return duplicate

            digest_fingerprint = hashlib.sha256(digest.encode("utf-8")).hexdigest()
            digest_id = f"mem_{hashlib.sha256(f'{experience_id}:{digest_fingerprint}'.encode()).hexdigest()}"
            created_at = datetime.now(timezone.utc).isoformat()
            event_id = f"evt_{uuid.uuid4().hex}"
            payload = {
                "source_id": source_id,
                "experience_id": experience_id,
                "digest_id": digest_id,
                "entrance": metadata.entrance,
                "task": metadata.task,
                "sensitivity": metadata.sensitivity,
                "state": "buffered",
            }
            staged_database = self._database_with_capture(
                database_path,
                source_id=source_id,
                content_hash=f"sha256:{source_digest}",
                object_reference=object_reference,
                experience_id=experience_id,
                metadata=metadata,
                digest_id=digest_id,
                digest=digest,
                digest_fingerprint=f"sha256:{digest_fingerprint}",
                event_id=event_id,
                event_payload=payload,
                created_at=created_at,
            )
            event = {
                "id": event_id,
                "type": "memory.buffered",
                "occurred_at": created_at,
                **payload,
            }
            changes: list[tuple[Path, bytes]] = []
            if not object_path.exists():
                changes.append((object_path, body))
            changes.extend(
                [
                    (database_path, staged_database),
                    event_journal_change(self._root, event),
                ]
            )
            atomic_commit(
                self._root,
                changes,
                fault_injections={0: "remember-after-first-replace"},
            )
        return BufferedMemoryReceipt(
            source_id=source_id,
            experience_id=experience_id,
            digest_id=digest_id,
            digest=digest,
            disposition="buffered",
            metadata=metadata,
        )

    def recallable_memories(self) -> tuple[RecallableMemory, ...]:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise UserInputError(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                self._validate_database(database_path)
                with closing(sqlite3.connect(database_path)) as connection:
                    buffered_rows = connection.execute(
                        """
                        SELECT d.digest_id, d.content, e.source_id, e.occurred_at,
                               CASE
                                   WHEN EXISTS (
                                       SELECT 1
                                       FROM experiences AS private_experience
                                       WHERE private_experience.source_id = e.source_id
                                         AND private_experience.sensitivity = 'local-only'
                                   ) THEN 'local-only'
                                   ELSE e.sensitivity
                               END AS effective_sensitivity,
                               e.entrance, e.task
                        FROM buffered_digests AS d
                        JOIN experiences AS e
                          ON e.experience_id = d.experience_id
                        WHERE d.state = 'buffered'
                        """
                    ).fetchall()
                    canonical_rows = connection.execute(
                        """
                        SELECT c.memory_id, c.content, c.updated_at,
                               CASE
                                   WHEN c.sensitivity = 'local-only'
                                     OR EXISTS (
                                         SELECT 1
                                         FROM canonical_memory_version_sources
                                              AS private_source
                                         JOIN experiences AS private_experience
                                           ON private_experience.source_id = private_source.source_id
                                         WHERE private_source.memory_id = c.memory_id
                                           AND private_source.version = c.current_version
                                           AND private_experience.sensitivity = 'local-only'
                                     ) THEN 'local-only'
                                   ELSE 'cloud-allowed'
                               END AS effective_sensitivity,
                               GROUP_CONCAT(source.source_id, ',') AS source_ids
                        FROM canonical_memories AS c
                        LEFT JOIN canonical_memory_version_sources AS source
                          ON source.memory_id = c.memory_id
                         AND source.version = c.current_version
                        WHERE c.state = 'active'
                        GROUP BY c.memory_id, c.content, c.updated_at, c.sensitivity
                        """
                    ).fetchall()
                    relation_rows = connection.execute(
                        """
                        SELECT memory_id, related_memory_id
                        FROM canonical_memory_relations
                        ORDER BY memory_id, related_memory_id
                        """
                    ).fetchall()
                    conflict_rows = connection.execute(
                        """
                        SELECT first_memory_id, second_memory_id
                        FROM canonical_memory_conflicts
                        WHERE status = 'unresolved'
                        ORDER BY first_memory_id, second_memory_id
                        """
                    ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot query recallable memory") from error
        buffered = tuple(
            RecallableMemory(
                memory_id=memory_id,
                content=content,
                memory_state=MemoryState.BUFFERED,
                source_ids=(source_id,),
                occurred_at=occurred_at,
                sensitivity=sensitivity,
                entrance=entrance,
                task=task,
            )
            for (
                memory_id,
                content,
                source_id,
                occurred_at,
                sensitivity,
                entrance,
                task,
            ) in buffered_rows
        )
        canonical = tuple(
            RecallableMemory(
                memory_id=memory_id,
                content=content,
                memory_state=MemoryState.CANONICAL,
                source_ids=(
                    tuple(source_ids.split(",")) if source_ids is not None else ()
                ),
                occurred_at=updated_at,
                sensitivity=sensitivity,
                entrance=None,
                task=None,
                related_memory_ids=tuple(
                    related_memory_id
                    for relation_memory_id, related_memory_id in relation_rows
                    if relation_memory_id == memory_id
                ),
                conflict_memory_ids=tuple(
                    second_memory_id
                    if first_memory_id == memory_id
                    else first_memory_id
                    for first_memory_id, second_memory_id in conflict_rows
                    if memory_id in (first_memory_id, second_memory_id)
                ),
            )
            for memory_id, content, updated_at, sensitivity, source_ids in canonical_rows
        )
        return canonical + buffered

    def propose_manual_consolidation(
        self,
        task: str,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        digest_ids: tuple[str, ...] | None = None,
        proposed_understanding: str | None = None,
    ) -> tuple[IntegrationProposal, ...]:
        normalized_task = _required_text("consolidation task", task)
        normalized_digest_ids = (
            tuple(_required_text("digest id", value) for value in digest_ids)
            if digest_ids is not None
            else None
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    digest_filter = ""
                    parameters: list[str] = [normalized_task]
                    if normalized_digest_ids is not None:
                        if not normalized_digest_ids:
                            return ()
                        placeholders = ", ".join("?" for _ in normalized_digest_ids)
                        digest_filter = f" AND d.digest_id IN ({placeholders})"
                        parameters.extend(normalized_digest_ids)
                    rows = connection.execute(
                        f"""
                        SELECT d.digest_id, d.content, e.source_id, e.sensitivity
                        FROM buffered_digests AS d
                        JOIN experiences AS e
                          ON e.experience_id = d.experience_id
                        WHERE d.state = 'buffered'
                          AND e.task = ?
                          AND NOT EXISTS (
                              SELECT 1
                              FROM integration_proposal_buffered AS proposed
                              WHERE proposed.digest_id = d.digest_id
                          )
                          {digest_filter}
                        ORDER BY d.created_at, d.digest_id
                        """,
                        parameters,
                    ).fetchall()
                    canonical_rows = connection.execute(
                        """
                        SELECT c.memory_id, c.content, c.sensitivity,
                               GROUP_CONCAT(source.source_id, ',')
                        FROM canonical_memories AS c
                        LEFT JOIN canonical_memory_sources AS source
                          ON source.memory_id = c.memory_id
                        WHERE c.state = 'active'
                        GROUP BY c.memory_id, c.content, c.sensitivity
                        ORDER BY c.memory_id
                        """
                    ).fetchall()
            except sqlite3.Error as error:
                raise IntegrityError("cannot select memory for consolidation") from error
            if not rows:
                if normalized_digest_ids is not None:
                    requested = frozenset(normalized_digest_ids)
                    return tuple(
                        proposal
                        for proposal in self._query_integration_proposals(
                            database_path,
                            status="pending",
                            topic=normalized_task,
                        )
                        if frozenset(proposal.evidence_memory_ids) == requested
                    )
                return self._query_integration_proposals(
                    database_path,
                    status="pending",
                    topic=normalized_task,
                )
            candidates = _validated_consolidation_rows(rows)
            canonical_candidates = _validated_canonical_rows(canonical_rows)
            drafts = _integration_proposal_drafts(
                normalized_task,
                candidates,
                canonical_candidates,
                embedding_provider or LocalMultilingualEmbeddingProvider(),
            )
            if proposed_understanding is not None:
                normalized_understanding = _required_text(
                    "proposed understanding", proposed_understanding
                )
                if len(normalized_understanding) > 500:
                    raise UserInputError(
                        "proposed understanding must not exceed 500 characters"
                    )
                if len(drafts) != 1:
                    raise UserInputError(
                        "cloud analysis must map to exactly one bounded proposal"
                    )
                drafts = (
                    replace(
                        drafts[0], proposed_understanding=normalized_understanding
                    ),
                )
            staged_database = self._database_with_integration_proposals(
                database_path,
                drafts=drafts,
            )
            events = tuple(
                {
                    "id": f"evt_{uuid.uuid4().hex}",
                    "type": "integration.proposed",
                    "occurred_at": draft.created_at,
                    "proposal_id": draft.proposal_id,
                    "topic": draft.topic,
                    "evidence_memory_ids": list(draft.digest_ids),
                    "source_scope": list(draft.source_ids),
                }
                for draft in drafts
            )
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    event_journal_change(self._root, *events),
                ],
            )
        return tuple(draft.as_proposal() for draft in drafts)

    def buffered_consolidation_batch(
        self,
        task: str,
        *,
        sensitivity: Sensitivity,
        limit: int,
    ) -> tuple[BufferedConsolidationItem, ...]:
        normalized_task = _required_text("consolidation task", task)
        if sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError(f"invalid sensitivity: {sensitivity}")
        if limit <= 0:
            raise UserInputError("consolidation batch limit must be positive")
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    rows = connection.execute(
                        """
                        SELECT d.digest_id, d.content, e.source_id, e.task,
                               e.sensitivity
                        FROM buffered_digests AS d
                        JOIN experiences AS e
                          ON e.experience_id = d.experience_id
                        WHERE d.state = 'buffered'
                          AND e.task = ?
                          AND e.sensitivity = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM integration_proposal_buffered AS proposed
                              WHERE proposed.digest_id = d.digest_id
                          )
                        ORDER BY d.created_at, d.digest_id
                        LIMIT ?
                        """,
                        (normalized_task, sensitivity, limit),
                    ).fetchall()
            except sqlite3.Error as error:
                raise IntegrityError(
                    "cannot select bounded consolidation batch"
                ) from error
        return tuple(
            BufferedConsolidationItem(
                digest_id=row[0],
                content=row[1],
                source_id=row[2],
                task=row[3],
                sensitivity=row[4],
            )
            for row in rows
        )

    def review_integration_proposal(
        self,
        proposal_id: str,
        instruction: str,
    ) -> IntegrationReviewResult:
        normalized_proposal_id = _required_text("integration proposal id", proposal_id)
        review = _parse_review_instruction(instruction)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            proposals = self._query_integration_proposals(
                database_path,
                status="pending",
            )
            proposal = next(
                (
                    candidate
                    for candidate in proposals
                    if candidate.proposal_id == normalized_proposal_id
                ),
                None,
            )
            if proposal is None:
                raise UserInputError(
                    f"pending integration proposal does not exist: {normalized_proposal_id}"
                )
            action: IntegrationAction = review.action or (
                proposal.suggested_action
                if review.decision == "accepted"
                else "new"
            )
            target_memory_id = review.target_memory_id or (
                proposal.target_memory_id
                if review.decision == "accepted"
                else None
            )
            if action != "new":
                if target_memory_id is None:
                    raise UserInputError(
                        f"{action} review requires a target canonical memory"
                    )
                if target_memory_id not in proposal.related_canonical_memory_ids:
                    raise UserInputError(
                        "integration target must be a related canonical memory "
                        "shown by the proposal"
                    )
            canonical_content = None
            canonical_memory_id = None
            applied_action: AppliedIntegrationAction = "rejected"
            if review.decision != "rejected":
                canonical_content = review.content or proposal.proposed_understanding
                canonical_memory_id = (
                    target_memory_id
                    if action in ("supplement", "revise")
                    else f"mem_{hashlib.sha256(proposal.proposal_id.encode()).hexdigest()}"
                )
                if action == "new":
                    applied_action = "created"
                elif action == "supplement":
                    applied_action = "supplemented"
                elif action == "revise":
                    applied_action = "revised"
                else:
                    applied_action = "conflicted"
            reviewed_at = datetime.now(timezone.utc).isoformat()
            staged_database = self._database_with_integration_review(
                database_path,
                proposal=proposal,
                review=review,
                canonical_memory_id=canonical_memory_id,
                canonical_content=canonical_content,
                action=action,
                applied_action=applied_action,
                target_memory_id=target_memory_id,
                reviewed_at=reviewed_at,
            )
            event_id = f"evt_{uuid.uuid4().hex}"
            event = {
                "id": event_id,
                "type": f"integration.{review.decision}",
                "occurred_at": reviewed_at,
                "proposal_id": proposal.proposal_id,
                "decision": review.decision,
                "action": applied_action,
                "canonical_memory_id": canonical_memory_id,
                "source_scope": list(proposal.source_scope),
            }
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    event_journal_change(self._root, event),
                ],
                fault_injections={0: "integration-review-after-database"},
            )
        return IntegrationReviewResult(
            proposal_id=proposal.proposal_id,
            decision=review.decision,
            canonical_memory_id=canonical_memory_id,
            canonical_content=canonical_content,
            reason=review.reason,
            related_canonical_memory_ids=proposal.related_canonical_memory_ids,
            action=applied_action,
        )

    def integration_review_history(self) -> tuple[IntegrationReviewResult, ...]:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    rows = connection.execute(
                        """
                        SELECT review.proposal_id, review.decision,
                               review.canonical_memory_id, review.reviewed_content,
                               review.reason, review.action,
                               GROUP_CONCAT(DISTINCT related.memory_id)
                        FROM integration_reviews AS review
                        LEFT JOIN integration_proposal_related AS related
                          ON related.proposal_id = review.proposal_id
                        GROUP BY review.review_id
                        ORDER BY review.created_at, review.review_id
                        """
                    ).fetchall()
            except sqlite3.Error as error:
                raise IntegrityError("cannot read integration review history") from error
        return tuple(
            IntegrationReviewResult(
                proposal_id=row[0],
                decision=row[1],
                canonical_memory_id=row[2],
                canonical_content=row[3],
                reason=row[4],
                related_canonical_memory_ids=_split_group(row[6]),
                action=row[5],
            )
            for row in rows
        )

    def explain_canonical_memory(self, memory_id: str) -> CanonicalMemoryAudit:
        normalized_memory_id = _required_text("canonical memory id", memory_id)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    return _canonical_memory_audit(
                        connection,
                        normalized_memory_id,
                    )
            except sqlite3.Error as error:
                raise IntegrityError("cannot explain canonical memory") from error

    def canonical_memory_audits(self) -> tuple[CanonicalMemoryAudit, ...]:
        """Return complete audit snapshots without consulting human projections."""
        with self.canonical_memory_audit_snapshot() as audits:
            return audits

    @contextmanager
    def canonical_memory_audit_snapshot(
        self,
    ) -> Iterator[tuple[CanonicalMemoryAudit, ...]]:
        """Hold the single-writer boundary while a projection consumes one snapshot."""
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("BEGIN")
                    memory_ids = tuple(
                        row[0]
                        for row in connection.execute(
                            "SELECT memory_id FROM canonical_memories ORDER BY memory_id"
                        ).fetchall()
                    )
                    audits = tuple(
                        _canonical_memory_audit(connection, memory_id)
                        for memory_id in memory_ids
                    )
                    yield audits
            except sqlite3.Error as error:
                raise IntegrityError("cannot list canonical memory audits") from error

    def set_canonical_memory_active(
        self,
        memory_id: str,
        *,
        active: bool,
        reason: str,
    ) -> CanonicalMemoryStateChange:
        normalized_memory_id = _required_text("canonical memory id", memory_id)
        normalized_reason = _required_text("memory lifecycle reason", reason)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        target_state = "active" if active else "inactive"
        action: MemoryLifecycleAction = "reactivated" if active else "deactivated"
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    row = connection.execute(
                        "SELECT state FROM canonical_memories WHERE memory_id = ?",
                        (normalized_memory_id,),
                    ).fetchone()
            except sqlite3.Error as error:
                raise IntegrityError("cannot inspect canonical memory state") from error
            if row is None:
                raise UserInputError(
                    f"canonical memory does not exist: {normalized_memory_id}"
                )
            if row[0] == target_state:
                return CanonicalMemoryStateChange(
                    memory_id=normalized_memory_id,
                    action=action,
                    occurred_at=occurred_at,
                    reason=normalized_reason,
                )
            event_id = f"evt_{uuid.uuid4().hex}"
            payload = {
                "memory_id": normalized_memory_id,
                "action": action,
                "state": target_state,
                "reason": normalized_reason,
            }
            staged_database = self._database_with_state_change(
                database_path,
                memory_id=normalized_memory_id,
                state=target_state,
                event_id=event_id,
                event_type=f"memory.{action}",
                occurred_at=occurred_at,
                payload=payload,
            )
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    (
                        event_journal_change(
                            self._root,
                            {
                                "id": event_id,
                                "type": f"memory.{action}",
                                "occurred_at": occurred_at,
                                **payload,
                            },
                        )
                    ),
                ],
            )
        return CanonicalMemoryStateChange(
            memory_id=normalized_memory_id,
            action=action,
            occurred_at=occurred_at,
            reason=normalized_reason,
        )

    def preview_permanent_deletion(self, memory_id: str) -> MemoryDeletionImpact:
        normalized_memory_id = _required_text("canonical memory id", memory_id)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    impact = self._deletion_impact_for_connection(
                        connection,
                        normalized_memory_id,
                    )
            except sqlite3.Error as error:
                raise IntegrityError("cannot preview permanent deletion") from error
        return impact

    def permanently_delete(
        self,
        memory_id: str,
        *,
        confirmation_token: str,
    ) -> MemoryDeletionResult:
        normalized_memory_id = _required_text("canonical memory id", memory_id)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    impact = self._deletion_impact_for_connection(
                        connection,
                        normalized_memory_id,
                    )
                    if confirmation_token != impact.confirmation_token:
                        raise UserInputError(
                            "permanent deletion confirmation does not match "
                            "the current impact"
                        )
                    removed_source_ids = tuple(
                        source_id
                        for source_id in impact.source_ids
                        if source_id not in impact.shared_source_ids
                    )
                    object_references = tuple(
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT object_reference FROM source_objects
                            WHERE source_id IN (
                                SELECT source_id FROM canonical_memory_sources
                                WHERE memory_id = ?
                                EXCEPT
                                SELECT source_id FROM canonical_memory_sources
                                WHERE memory_id <> ?
                            )
                            ORDER BY object_reference
                            """,
                            (normalized_memory_id, normalized_memory_id),
                        ).fetchall()
                    )
                    removed_digest_ids = impact.derived_digest_ids
                    removed_experience_ids = _select_ids_for_values(
                        connection,
                        table="experiences",
                        result_column="experience_id",
                        filter_column="source_id",
                        values=removed_source_ids,
                    )
                    removed_proposal_ids = impact.proposal_ids_to_delete
            except sqlite3.Error as error:
                raise IntegrityError("cannot plan permanent deletion") from error

            deleted_at = datetime.now(timezone.utc).isoformat()
            deletion_event = {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "memory.permanently-deleted",
                "occurred_at": deleted_at,
                "subject_fingerprint": _deletion_fingerprint(
                    normalized_memory_id
                ),
                "removed_source_count": len(removed_source_ids),
            }
            staged_database = self._database_with_permanent_deletion(
                database_path,
                impact=impact,
                removed_source_ids=removed_source_ids,
                removed_digest_ids=removed_digest_ids,
                removed_proposal_ids=removed_proposal_ids,
                deleted_at=deleted_at,
            )
            view_paths = _knowledge_view_paths_for_memory(
                self._root,
                normalized_memory_id,
            )
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    _redacted_event_journal_change(
                        self._root,
                        sensitive_ids=(
                            normalized_memory_id,
                            *removed_source_ids,
                            *removed_experience_ids,
                            *removed_digest_ids,
                            *removed_proposal_ids,
                            *impact.review_ids_to_delete,
                        ),
                        deletion_event=deletion_event,
                    ),
                    permanent_deletion_cleanup_change(
                        self._root,
                        object_references=object_references,
                        view_paths=view_paths,
                    ),
                ],
            )
            if (
                os.environ.get("MYOUTBRAIN_FAULT_INJECTION")
                == "permanent-deletion-before-cleanup"
            ):
                os._exit(86)
            recover_transactions(self._root)
        return MemoryDeletionResult(
            memory_id=normalized_memory_id,
            removed_source_ids=removed_source_ids,
            retained_shared_source_ids=impact.shared_source_ids,
            removed_digest_ids=removed_digest_ids,
            removed_proposal_ids=removed_proposal_ids,
            deleted_at=deleted_at,
            backup_exclusion_after=deleted_at,
            existing_backup_clearance=(
                "external-backups-must-be-rotated-or-deleted-by-owner"
            ),
        )

    def storage_report(self) -> MemoryStorageReport:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    source_rows = connection.execute(
                        """
                        SELECT source_id, object_reference FROM source_objects
                        ORDER BY source_id
                        """
                    ).fetchall()
                    canonical_rows = connection.execute(
                        "SELECT content FROM canonical_memory_versions"
                    ).fetchall()
                    canonical_count_row = connection.execute(
                        "SELECT COUNT(*) FROM canonical_memories"
                    ).fetchone()
                    buffer_rows = connection.execute(
                        """
                        SELECT content FROM buffered_digests
                        WHERE state = 'buffered'
                        """
                    ).fetchall()
            except sqlite3.Error as error:
                raise IntegrityError("cannot read memory storage usage") from error
            evidence_bytes = 0
            for _, object_reference in source_rows:
                object_path = _resolved_object_reference(
                    self._root,
                    object_reference,
                )
                try:
                    evidence_bytes += object_path.stat().st_size
                except OSError as error:
                    raise IntegrityError(
                        f"cannot measure source object: {object_path}"
                    ) from error
            index_files = tuple(
                path
                for path in (self._root / "runtime" / "indexes").rglob("*")
                if path.is_file()
            )
            try:
                index_bytes = sum(path.stat().st_size for path in index_files)
            except OSError as error:
                raise IntegrityError("cannot measure rebuildable indexes") from error
        canonical_count = (
            canonical_count_row[0] if canonical_count_row is not None else 0
        )
        if not isinstance(canonical_count, int):
            raise IntegrityError("canonical memory count is invalid")
        return MemoryStorageReport(
            evidence_source_ids=tuple(row[0] for row in source_rows),
            evidence_bytes=evidence_bytes,
            canonical_count=canonical_count,
            canonical_version_count=len(canonical_rows),
            canonical_bytes=sum(
                len(row[0].encode("utf-8")) for row in canonical_rows
            ),
            buffer_count=len(buffer_rows),
            buffer_bytes=sum(len(row[0].encode("utf-8")) for row in buffer_rows),
            rebuildable_index_count=len(index_files),
            rebuildable_index_bytes=index_bytes,
        )

    @staticmethod
    def _deletion_impact_for_connection(
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> MemoryDeletionImpact:
        if connection.execute(
            "SELECT 1 FROM canonical_memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone() is None:
            raise UserInputError(f"canonical memory does not exist: {memory_id}")
        source_ids = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT source_id FROM canonical_memory_sources
                WHERE memory_id = ? ORDER BY source_id
                """,
                (memory_id,),
            ).fetchall()
        )
        shared_source_ids = tuple(
            source_id
            for source_id in source_ids
            if connection.execute(
                """
                SELECT 1 FROM canonical_memory_sources
                WHERE source_id = ? AND memory_id <> ? LIMIT 1
                """,
                (source_id, memory_id),
            ).fetchone()
            is not None
        )
        related_memory_ids = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT CASE WHEN memory_id = ? THEN related_memory_id ELSE memory_id END
                FROM canonical_memory_relations
                WHERE memory_id = ? OR related_memory_id = ? ORDER BY 1
                """,
                (memory_id, memory_id, memory_id),
            ).fetchall()
        )
        conflict_memory_ids = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT CASE WHEN first_memory_id = ? THEN second_memory_id
                            ELSE first_memory_id END
                FROM canonical_memory_conflicts
                WHERE first_memory_id = ? OR second_memory_id = ? ORDER BY 1
                """,
                (memory_id, memory_id, memory_id),
            ).fetchall()
        )
        pending_proposal_ids = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT proposal.proposal_id
                FROM integration_proposals AS proposal
                LEFT JOIN integration_proposal_related AS related
                  ON related.proposal_id = proposal.proposal_id
                LEFT JOIN integration_proposal_sources AS source
                  ON source.proposal_id = proposal.proposal_id
                WHERE proposal.status = 'pending'
                  AND (proposal.target_memory_id = ? OR related.memory_id = ?
                       OR source.source_id IN (
                           SELECT source_id FROM canonical_memory_sources
                           WHERE memory_id = ?))
                ORDER BY proposal.proposal_id
                """,
                (memory_id, memory_id, memory_id),
            ).fetchall()
        )
        unshared_source_ids = tuple(
            source_id
            for source_id in source_ids
            if source_id not in shared_source_ids
        )
        removed_digest_ids = LocalMemoryCore._digest_ids_for_sources(
            connection,
            unshared_source_ids,
        )
        proposal_ids_to_delete = set(pending_proposal_ids)
        proposal_ids_to_delete.update(
            row[0]
            for row in connection.execute(
                """
                SELECT proposal_id FROM integration_proposals
                WHERE target_memory_id = ?
                UNION
                SELECT proposal_id FROM integration_reviews
                WHERE canonical_memory_id = ?
                """,
                (memory_id, memory_id),
            ).fetchall()
        )
        for table, column, values in (
            ("integration_proposal_buffered", "digest_id", removed_digest_ids),
            ("integration_proposal_sources", "source_id", unshared_source_ids),
        ):
            if not values:
                continue
            placeholders = ", ".join("?" for _ in values)
            proposal_ids_to_delete.update(
                row[0]
                for row in connection.execute(
                    f"SELECT proposal_id FROM {table} "
                    f"WHERE {column} IN ({placeholders})",
                    values,
                ).fetchall()
            )
        ordered_proposal_ids = tuple(sorted(proposal_ids_to_delete))
        proposal_digest_rows = connection.execute(
            """
            SELECT proposal_id, digest_id
            FROM integration_proposal_buffered
            ORDER BY proposal_id, digest_id
            """
        ).fetchall()
        retained_proposal_digest_ids = {
            digest_id
            for proposal_id, digest_id in proposal_digest_rows
            if proposal_id not in proposal_ids_to_delete
        }
        exclusive_target_digest_ids = {
            digest_id
            for proposal_id, digest_id in proposal_digest_rows
            if proposal_id in proposal_ids_to_delete
            and digest_id not in retained_proposal_digest_ids
        }
        derived_digest_ids = tuple(
            sorted(set(removed_digest_ids).union(exclusive_target_digest_ids))
        )
        review_ids_to_delete = _select_ids_for_values(
            connection,
            table="integration_reviews",
            result_column="review_id",
            filter_column="proposal_id",
            values=ordered_proposal_ids,
        )
        return MemoryDeletionImpact(
            memory_id=memory_id,
            source_ids=source_ids,
            shared_source_ids=shared_source_ids,
            derived_digest_ids=derived_digest_ids,
            related_memory_ids=related_memory_ids,
            conflict_memory_ids=conflict_memory_ids,
            pending_proposal_ids=pending_proposal_ids,
            proposal_ids_to_delete=ordered_proposal_ids,
            review_ids_to_delete=review_ids_to_delete,
        )

    @staticmethod
    def _digest_ids_for_sources(
        connection: sqlite3.Connection,
        source_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not source_ids:
            return ()
        placeholders = ", ".join("?" for _ in source_ids)
        return tuple(
            row[0]
            for row in connection.execute(
                f"""
                SELECT digest.digest_id
                FROM buffered_digests AS digest
                JOIN experiences AS experience
                  ON experience.experience_id = digest.experience_id
                WHERE experience.source_id IN ({placeholders})
                ORDER BY digest.digest_id
                """,
                source_ids,
            ).fetchall()
        )

    @staticmethod
    def _database_with_permanent_deletion(
        database_path: Path,
        *,
        impact: MemoryDeletionImpact,
        removed_source_ids: tuple[str, ...],
        removed_digest_ids: tuple[str, ...],
        removed_proposal_ids: tuple[str, ...],
        deleted_at: str,
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-delete.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                experience_ids = _select_ids_for_values(
                    connection,
                    table="experiences",
                    result_column="experience_id",
                    filter_column="source_id",
                    values=removed_source_ids,
                )
                for table in (
                    "integration_reviews",
                    "integration_proposal_buffered",
                    "integration_proposal_related",
                    "integration_proposal_sources",
                ):
                    _delete_rows_for_ids(
                        connection,
                        table=table,
                        column="proposal_id",
                        values=removed_proposal_ids,
                    )
                _delete_rows_for_ids(
                    connection,
                    table="integration_proposals",
                    column="proposal_id",
                    values=removed_proposal_ids,
                )
                connection.execute(
                    "DELETE FROM integration_proposal_related WHERE memory_id = ?",
                    (impact.memory_id,),
                )
                connection.execute(
                    """
                    DELETE FROM canonical_memory_conflicts
                    WHERE first_memory_id = ? OR second_memory_id = ?
                    """,
                    (impact.memory_id, impact.memory_id),
                )
                connection.execute(
                    """
                    DELETE FROM canonical_memory_relations
                    WHERE memory_id = ? OR related_memory_id = ?
                    """,
                    (impact.memory_id, impact.memory_id),
                )
                connection.execute(
                    "DELETE FROM legacy_knowledge_metadata WHERE memory_id = ?",
                    (impact.memory_id,),
                )
                connection.execute(
                    "DELETE FROM canonical_memory_version_sources WHERE memory_id = ?",
                    (impact.memory_id,),
                )
                connection.execute(
                    "DELETE FROM canonical_memory_versions WHERE memory_id = ?",
                    (impact.memory_id,),
                )
                connection.execute(
                    "DELETE FROM canonical_memory_sources WHERE memory_id = ?",
                    (impact.memory_id,),
                )
                connection.execute(
                    "DELETE FROM canonical_memories WHERE memory_id = ?",
                    (impact.memory_id,),
                )
                _delete_rows_for_ids(
                    connection,
                    table="memory_events",
                    column="subject_id",
                    values=(
                        impact.memory_id,
                        *removed_source_ids,
                        *removed_digest_ids,
                        *experience_ids,
                    ),
                )
                _delete_rows_for_ids(
                    connection,
                    table="buffered_digests",
                    column="digest_id",
                    values=removed_digest_ids,
                )
                _delete_rows_for_ids(
                    connection,
                    table="experiences",
                    column="experience_id",
                    values=experience_ids,
                )
                _delete_rows_for_ids(
                    connection,
                    table="legacy_source_metadata",
                    column="source_id",
                    values=removed_source_ids,
                )
                _delete_rows_for_ids(
                    connection,
                    table="source_objects",
                    column="source_id",
                    values=removed_source_ids,
                )
                for subject_kind, subject_id in (
                    ("canonical-memory", impact.memory_id),
                    *(("source", source_id) for source_id in removed_source_ids),
                ):
                    fingerprint = _deletion_fingerprint(subject_id)
                    marker_id = "del_" + hashlib.sha256(
                        f"{subject_kind}:{fingerprint}".encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO deletion_markers
                            (marker_id, subject_kind, subject_fingerprint,
                             deleted_at, backup_exclusion_after)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            marker_id,
                            subject_kind,
                            fingerprint,
                            deleted_at,
                            deleted_at,
                        ),
                    )
                connection.commit()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise IntegrityError(
                        "permanent deletion would leave dangling memory references"
                    )
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage permanent deletion") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _has_deletion_marker(
        database_path: Path,
        *,
        subject_kind: str,
        subject_id: str,
    ) -> bool:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                return connection.execute(
                    """
                    SELECT 1 FROM deletion_markers
                    WHERE subject_kind = ? AND subject_fingerprint = ?
                    """,
                    (subject_kind, _deletion_fingerprint(subject_id)),
                ).fetchone() is not None
        except sqlite3.Error as error:
            raise IntegrityError("cannot check permanent deletion markers") from error

    @staticmethod
    def _pending_source_memory_proposal(
        database_path: Path,
        *,
        canonical_name: str,
        body: str,
        applicability_scope: str,
        suggested_action: IntegrationAction,
        target_memory_id: str | None,
        target_version: int,
    ) -> SourceMemoryProposal | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT detail.proposal_id, detail.planned_memory_id,
                           detail.canonical_name, proposal.proposed_understanding,
                           detail.applicability_scope, detail.source_id,
                           detail.source_version, version.content_hash,
                           version.locator, version.observed_at,
                           version.applicability_scope, detail.request_hash,
                           proposal.suggested_action, proposal.target_memory_id,
                           review.target_json, review.proposal_version
                    FROM source_memory_proposal_details AS detail
                    JOIN integration_proposals AS proposal
                      ON proposal.proposal_id = detail.proposal_id
                    JOIN evidence_source_versions AS version
                      ON version.source_id = detail.source_id
                     AND version.version = detail.source_version
                    JOIN review_proposals AS review
                      ON review.proposal_id = detail.proposal_id
                    WHERE review.status IN ('pending', 'deferred')
                    ORDER BY review.created_at, detail.proposal_id
                    """,
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot check pending source memory proposals") from error
        normalized_body = _normalized_memory_body(body)
        normalized_scope = " ".join(applicability_scope.casefold().split())
        for row in rows:
            proposal, _ = LocalMemoryCore._source_memory_proposal_from_row(row)
            if (
                proposal.canonical_name == canonical_name
                and _normalized_memory_body(proposal.body) == normalized_body
                and " ".join(proposal.applicability_scope.casefold().split())
                == normalized_scope
                and proposal.suggested_action == suggested_action
                and proposal.target_memory_id == target_memory_id
                and proposal.target_version == target_version
            ):
                return proposal
        return None

    @staticmethod
    def _pending_source_memory_evidence_receipt(
        database_path: Path,
        *,
        proposal_id: str,
        locator: str,
        content_hash: str,
        source_id: str | None,
    ) -> SourceReceipt | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                proposal_row = connection.execute(
                    """
                    SELECT supporting_evidence_json
                    FROM review_proposals
                    WHERE proposal_id = ? AND status IN ('pending', 'deferred')
                    """,
                    (proposal_id,),
                ).fetchone()
                if proposal_row is None or not isinstance(proposal_row[0], str):
                    return None
                evidence = json.loads(proposal_row[0])
                receipt_rows = connection.execute(
                    """
                    SELECT version.source_id, version.version, version.content_hash,
                           version.locator, version.observed_at,
                           version.applicability_scope
                    FROM evidence_source_versions AS version
                    WHERE version.content_hash = ?
                      AND ((? IS NULL AND version.locator = ?)
                           OR (? IS NOT NULL AND version.source_id = ?))
                    ORDER BY version.source_id, version.version DESC
                    """,
                    (content_hash, source_id, locator, source_id, source_id),
                ).fetchall()
        except (sqlite3.Error, json.JSONDecodeError) as error:
            raise IntegrityError("cannot inspect pending proposal evidence") from error
        if not isinstance(evidence, list):
            raise IntegrityError("pending proposal supporting evidence is invalid")
        for row in receipt_rows:
            if (
                not isinstance(row[0], str)
                or not isinstance(row[1], int)
                or not all(isinstance(row[index], str) for index in (2, 3, 4, 5))
            ):
                raise IntegrityError("pending proposal source receipt is invalid")
            if any(
                isinstance(item, dict)
                and item.get("kind") == "source"
                and item.get("source_id") == row[0]
                and item.get("version") == row[1]
                for item in evidence
            ):
                return SourceReceipt(
                    source_id=row[0],
                    version=row[1],
                    content_hash=row[2],
                    locator=row[3],
                    observed_at=row[4],
                    applicability_scope=row[5],
                )
        return None

    @staticmethod
    def _database_with_pending_source_memory_evidence(
        database_path: Path,
        *,
        pending: SourceMemoryProposal,
        locator: str,
        content_hash: str,
        applicability_scope: str,
        existing_source_id: str | None,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[bytes, SourceMemoryProposal]:
        temporary_path: Path | None = None
        updated_at = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".source-memory-pending-evidence.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                source = _register_local_evidence_source(
                    connection,
                    locator=locator,
                    content_hash=content_hash,
                    applicability_scope=applicability_scope,
                    existing_source_id=existing_source_id,
                    observed_at=updated_at,
                )
                source_evidence: dict[str, object] = {
                    "kind": "source",
                    **source.to_data(),
                }
                proposal_version = merge_review_proposal_supporting_evidence(
                    connection,
                    proposal_id=pending.proposal_id,
                    evidence=source_evidence,
                    updated_at=updated_at,
                )
                result = replace(
                    pending,
                    source=source,
                    proposal_version=proposal_version,
                    disposition="proposal-reused",
                )
                connection.execute(
                    """
                    INSERT INTO source_memory_reuse_submissions
                        (idempotency_key, request_hash, result_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        request_hash,
                        json.dumps(
                            result.to_data(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        updated_at,
                    ),
                )
                connection.commit()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise IntegrityError("pending source evidence left a dangling reference")
            return temporary_path.read_bytes(), result
        except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
            raise IntegrityError("cannot stage pending source memory evidence") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _source_proposal_ids_for_memory(
        database_path: Path,
        *,
        memory_id: str,
    ) -> tuple[str, ...]:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT detail.proposal_id
                    FROM source_memory_proposal_details AS detail
                    WHERE detail.planned_memory_id = ?
                    UNION
                    SELECT proposal.proposal_id
                    FROM integration_proposals AS proposal
                    JOIN review_proposals AS review
                      ON review.proposal_id = proposal.proposal_id
                    WHERE proposal.target_memory_id = ?
                      AND review.status IN ('pending', 'deferred')
                    ORDER BY 1
                    """,
                    (memory_id, memory_id),
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot find related source memory proposals") from error
        proposal_ids: list[str] = []
        for row in rows:
            if not isinstance(row[0], str):
                raise IntegrityError("related source memory proposal is invalid")
            proposal_ids.append(row[0])
        return tuple(proposal_ids)

    @staticmethod
    def _source_memory_proposal_for_key(
        database_path: Path,
        idempotency_key: str,
    ) -> tuple[SourceMemoryProposal, str] | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT detail.proposal_id, detail.planned_memory_id,
                           detail.canonical_name, proposal.proposed_understanding,
                           detail.applicability_scope, detail.source_id,
                           detail.source_version, version.content_hash,
                           version.locator, version.observed_at,
                           version.applicability_scope, detail.request_hash,
                           proposal.suggested_action, proposal.target_memory_id,
                           review.target_json
                    FROM source_memory_proposal_details AS detail
                    JOIN integration_proposals AS proposal
                      ON proposal.proposal_id = detail.proposal_id
                    JOIN evidence_source_versions AS version
                      ON version.source_id = detail.source_id
                     AND version.version = detail.source_version
                    JOIN review_proposals AS review
                      ON review.proposal_id = detail.proposal_id
                    WHERE detail.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot read source memory proposal") from error
        if row is None:
            return None
        return LocalMemoryCore._source_memory_proposal_from_row(row)

    @staticmethod
    def _source_memory_proposal_from_row(
        row: tuple[object, ...],
    ) -> tuple[SourceMemoryProposal, str]:
        if (
            not all(
                isinstance(row[index], str)
                for index in (0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 14)
            )
            or not isinstance(row[6], int)
            or row[12] not in ("new", "supplement", "revise", "conflict")
            or (row[13] is not None and not isinstance(row[13], str))
            or (len(row) > 15 and not isinstance(row[15], int))
        ):
            raise IntegrityError("source memory proposal is invalid")
        target_data = json.loads(cast(str, row[14]))
        if (
            not isinstance(target_data, dict)
            or not isinstance(target_data.get("expected_version"), int)
        ):
            raise IntegrityError("source memory proposal target is invalid")
        return (
            SourceMemoryProposal(
                proposal_id=cast(str, row[0]),
                planned_memory_id=cast(str, row[1]),
                canonical_name=cast(str, row[2]),
                body=cast(str, row[3]),
                applicability_scope=cast(str, row[4]),
                source=SourceReceipt(
                    source_id=cast(str, row[5]),
                    version=row[6],
                    content_hash=cast(str, row[7]),
                    locator=cast(str, row[8]),
                    observed_at=cast(str, row[9]),
                    applicability_scope=cast(str, row[10]),
                ),
                suggested_action=row[12],
                target_memory_id=row[13],
                target_version=cast(int, target_data["expected_version"]),
                proposal_version=cast(int, row[15]) if len(row) > 15 else 1,
            ),
            cast(str, row[11]),
        )

    @staticmethod
    def _database_with_source_memory_proposal(
        database_path: Path,
        *,
        locator: str,
        content_hash: str,
        canonical_name: str,
        body: str,
        applicability_scope: str,
        idempotency_key: str,
        request_hash: str,
        existing_source_id: str | None,
        suggested_action: IntegrationAction,
        target_memory_id: str | None,
        target_version: int,
        near_proposal_ids: tuple[str, ...],
        conflict_proposal_ids: tuple[str, ...],
    ) -> tuple[bytes, SourceMemoryProposal]:
        temporary_path: Path | None = None
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".source-memory-proposal.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                source = _register_local_evidence_source(
                    connection,
                    locator=locator,
                    content_hash=content_hash,
                    applicability_scope=applicability_scope,
                    existing_source_id=existing_source_id,
                    observed_at=created_at,
                )
                source_id = source.source_id
                source_version = source.version
                proposal_id = f"prp_{uuid.uuid4().hex}"
                planned_memory_id = f"mem_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO integration_proposals
                        (proposal_id, topic, proposed_understanding, possible_impact,
                         sensitivity, suggested_action, target_memory_id, status,
                         created_at, reviewed_at)
                    VALUES (?, ?, ?, ?, 'local-only', ?, ?, 'pending', ?, NULL)
                    """,
                    (
                        proposal_id,
                        applicability_scope,
                        body,
                        (
                            "Records a reviewable conflict; approval materialization is deferred to issue 09."
                            if suggested_action == "conflict"
                            else "Creates or revises source-backed canonical knowledge after explicit approval."
                        ),
                        suggested_action,
                        target_memory_id,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO source_memory_proposal_details
                        (proposal_id, proposal_version, planned_memory_id,
                         canonical_name, applicability_scope, source_id,
                         source_version, idempotency_key, request_hash)
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        planned_memory_id,
                        canonical_name,
                        applicability_scope,
                        source_id,
                        source_version,
                        idempotency_key,
                        request_hash,
                    ),
                )
                register_source_memory_proposal(
                    connection,
                    proposal_id=proposal_id,
                    planned_memory_id=planned_memory_id,
                    canonical_name=canonical_name,
                    body=body,
                    applicability_scope=applicability_scope,
                    source=source.to_data(),
                    created_at=created_at,
                    suggested_action=suggested_action,
                    target_memory_id=target_memory_id,
                    target_version=target_version,
                    near_proposal_ids=near_proposal_ids,
                    conflict_proposal_ids=conflict_proposal_ids,
                )
                connection.commit()
            proposal = SourceMemoryProposal(
                proposal_id=proposal_id,
                planned_memory_id=planned_memory_id,
                canonical_name=canonical_name,
                body=body,
                applicability_scope=applicability_scope,
                source=source,
                suggested_action=suggested_action,
                target_memory_id=target_memory_id,
                target_version=target_version,
            )
            return temporary_path.read_bytes(), proposal
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage source memory proposal") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _source_memory_approval_for_key(
        database_path: Path,
        idempotency_key: str,
    ) -> tuple[SourceMemoryApproval, str] | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT write.request_hash, detail.proposal_id,
                           detail.planned_memory_id, detail.canonical_name,
                           proposal.proposed_understanding,
                           detail.applicability_scope, detail.source_id,
                           detail.source_version, version.content_hash,
                           version.locator, version.observed_at,
                           version.applicability_scope,
                           dictionary.primary_capsule_id,
                           audit.event_id, audit.event_type, audit.occurred_at,
                           audit.before_version, audit.after_version,
                           audit.entrance, audit.result_hash
                    FROM idempotent_writes AS write
                    JOIN source_memory_proposal_details AS detail
                      ON detail.proposal_id = write.subject_id
                    JOIN integration_proposals AS proposal
                      ON proposal.proposal_id = detail.proposal_id
                    JOIN evidence_source_versions AS version
                      ON version.source_id = detail.source_id
                     AND version.version = detail.source_version
                    JOIN knowledge_dictionary AS dictionary
                      ON dictionary.memory_id = detail.planned_memory_id
                    JOIN audit_events AS audit
                      ON audit.proposal_id = detail.proposal_id
                     AND audit.event_type = 'review.applied'
                    WHERE write.operation = 'approve-source-memory'
                      AND write.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot read idempotent memory approval") from error
        if row is None:
            return None
        string_indexes = (
            0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 18, 19
        )
        if (
            not all(isinstance(row[index], str) for index in string_indexes)
            or not isinstance(row[7], int)
            or row[16] is not None
            or not isinstance(row[17], int)
        ):
            raise IntegrityError("idempotent memory approval is invalid")
        return (
            SourceMemoryApproval(
                proposal_id=row[1],
                memory_id=row[2],
                canonical_name=row[3],
                body=row[4],
                applicability_scope=row[5],
                source=SourceReceipt(
                    source_id=row[6],
                    version=row[7],
                    content_hash=row[8],
                    locator=row[9],
                    observed_at=row[10],
                    applicability_scope=row[11],
                ),
                capsule_id=row[12],
                audit_event=AuditEventReceipt(
                    event_id=row[13],
                    event_type=row[14],
                    occurred_at=row[15],
                    before_version=None,
                    after_version=row[17],
                    entrance=row[18],
                    result_hash=row[19],
                ),
            ),
            row[0],
        )

    @staticmethod
    def _database_with_source_memory_approval(
        database_path: Path,
        *,
        proposal_id: str,
        idempotency_key: str,
        request_hash: str,
        entrance: str,
    ) -> tuple[bytes, SourceMemoryApproval]:
        temporary_path: Path | None = None
        applied_at = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".source-memory-approval.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                row = connection.execute(
                    """
                    SELECT detail.planned_memory_id, detail.canonical_name,
                           proposal.proposed_understanding,
                           detail.applicability_scope, detail.source_id,
                           detail.source_version, version.content_hash,
                           version.locator, version.observed_at,
                           version.applicability_scope, proposal.status,
                           proposal.suggested_action,
                           review.supporting_evidence_json
                    FROM source_memory_proposal_details AS detail
                    JOIN integration_proposals AS proposal
                      ON proposal.proposal_id = detail.proposal_id
                    JOIN review_proposals AS review
                      ON review.proposal_id = detail.proposal_id
                    JOIN evidence_source_versions AS version
                      ON version.source_id = detail.source_id
                     AND version.version = detail.source_version
                    WHERE detail.proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if row is None or row[10] != "pending":
                    raise UserInputError(
                        f"pending source memory proposal does not exist: {proposal_id}"
                    )
                if (
                    not all(
                        isinstance(row[index], str)
                        for index in (0, 1, 2, 3, 4, 6, 7, 8, 9)
                    )
                    or not isinstance(row[5], int)
                    or row[11] not in ("new", "supplement", "revise", "conflict")
                    or not isinstance(row[12], str)
                ):
                    raise IntegrityError("source memory proposal is invalid")
                if row[11] != "new":
                    if row[11] == "conflict":
                        raise UserInputError(
                            "conflict approval materialization is deferred to issue 09"
                        )
                    raise UserInputError(
                        "near source-memory variants must be decided through unified review"
                    )
                memory_id = row[0]
                canonical_name = row[1]
                body = row[2]
                applicability_scope = row[3]
                source = SourceReceipt(
                    source_id=row[4],
                    version=row[5],
                    content_hash=row[6],
                    locator=row[7],
                    observed_at=row[8],
                    applicability_scope=row[9],
                )
                body_bytes = len(body.encode("utf-8"))
                if body_bytes > MEMORY_BODY_HARD_LIMIT_BYTES:
                    raise IntegrityError("pending canonical memory exceeds its byte budget")
                if connection.execute(
                    "SELECT 1 FROM canonical_memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone() is not None:
                    raise UserInputError(
                        "first-memory approval expected_version conflict: memory already exists"
                    )
                capsule_id = f"cap_{uuid.uuid4().hex}"
                event_id = f"aud_{uuid.uuid4().hex}"
                result_hash = _stable_hash(
                    {
                        "proposal_id": proposal_id,
                        "memory_id": memory_id,
                        "version": 1,
                        "canonical_name": canonical_name,
                        "body_hash": _stable_hash(body),
                        "scope": applicability_scope,
                        "source_id": source.source_id,
                        "source_version": source.version,
                        "capsule_id": capsule_id,
                    }
                )
                audit_event = AuditEventReceipt(
                    event_id=event_id,
                    event_type="review.applied",
                    occurred_at=applied_at,
                    before_version=None,
                    after_version=1,
                    entrance=entrance,
                    result_hash=result_hash,
                )
                approval = SourceMemoryApproval(
                    proposal_id=proposal_id,
                    memory_id=memory_id,
                    canonical_name=canonical_name,
                    body=body,
                    applicability_scope=applicability_scope,
                    source=source,
                    capsule_id=capsule_id,
                    audit_event=audit_event,
                )
                partition_id = (
                    "prt_"
                    + hashlib.sha256(capsule_id.encode("utf-8")).hexdigest()[:32]
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_capsules
                        (capsule_id, topic, body_bytes, memory_record_count,
                         structural_version, created_at, updated_at)
                    VALUES (?, ?, ?, 1, 1, ?, ?)
                    """,
                    (capsule_id, applicability_scope, body_bytes, applied_at, applied_at),
                )
                supporting_evidence = json.loads(row[12])
                if not isinstance(supporting_evidence, list):
                    raise IntegrityError("source memory supporting evidence is invalid")
                supporting_sources = {(source.source_id, source.version)}
                for evidence in supporting_evidence:
                    if not isinstance(evidence, dict) or evidence.get("kind") != "source":
                        continue
                    evidence_source_id = evidence.get("source_id")
                    evidence_source_version = evidence.get(
                        "version", evidence.get("source_version")
                    )
                    if (
                        not isinstance(evidence_source_id, str)
                        or not isinstance(evidence_source_version, int)
                    ):
                        raise IntegrityError(
                            "source memory supporting evidence receipt is invalid"
                        )
                    supporting_sources.add(
                        (evidence_source_id, evidence_source_version)
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_partitions
                        (partition_id, parent_partition_id, node_kind, topic,
                         normalized_topic)
                    VALUES ('prt_root', NULL, 'root', 'All knowledge', 'all knowledge')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_partitions
                        (partition_id, parent_partition_id, node_kind, topic,
                         normalized_topic)
                    VALUES (?, 'prt_root', 'leaf', ?, ?)
                    """,
                    (
                        partition_id,
                        applicability_scope,
                        " ".join(applicability_scope.casefold().split()),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO capsule_partitions (capsule_id, partition_id)
                    VALUES (?, ?)
                    """,
                    (capsule_id, partition_id),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_memories
                        (memory_id, content, current_version, sensitivity, state,
                         created_at, updated_at)
                    VALUES (?, '', 1, 'local-only', 'active', ?, ?)
                    """,
                    (memory_id, applied_at, applied_at),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_memory_versions
                        (memory_id, version, content, applicability_scope, capsule_id,
                         action, change_reason, created_at, superseded_at,
                         supersession_reason)
                    VALUES (?, 1, ?, ?, ?, 'created', ?, ?, NULL, NULL)
                    """,
                    (
                        memory_id,
                        body,
                        applicability_scope,
                        capsule_id,
                        "Explicit source-memory proposal approved.",
                        applied_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO canonical_memory_version_evidence
                        (memory_id, version, source_id, source_version, relationship)
                    VALUES (?, 1, ?, ?, 'supports')
                    """,
                    (
                        (memory_id, source_id, source_version)
                        for source_id, source_version in sorted(supporting_sources)
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
                        canonical_name,
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
                        canonical_name,
                        " ".join(canonical_name.casefold().split()),
                        applied_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_memory_fts
                        (memory_id, capsule_id, canonical_name, body,
                         applicability_scope, search_terms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        capsule_id,
                        canonical_name,
                        body,
                        applicability_scope,
                        " ".join(
                            sorted(
                                lexical_terms(
                                    f"{canonical_name} {body} {applicability_scope}"
                                )
                            )
                        ),
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE integration_proposals
                    SET status = 'accepted', reviewed_at = ?
                    WHERE proposal_id = ? AND status = 'pending'
                    """,
                    (applied_at, proposal_id),
                )
                if updated.rowcount != 1:
                    raise IntegrityError("source memory proposal changed during approval")
                connection.execute(
                    """
                    INSERT INTO integration_reviews
                        (review_id, proposal_id, decision, action, reviewed_content,
                         reason, canonical_memory_id, created_at)
                    VALUES (?, ?, 'accepted', 'created', NULL, ?, ?, ?)
                    """,
                    (
                        f"rev_{hashlib.sha256(proposal_id.encode()).hexdigest()}",
                        proposal_id,
                        "Explicit approval.",
                        memory_id,
                        applied_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO audit_events
                        (event_id, event_type, occurred_at, subject_id, proposal_id,
                         before_version, after_version, entrance, result_hash)
                    VALUES (?, 'review.applied', ?, ?, ?, NULL, 1, ?, ?)
                    """,
                    (
                        event_id,
                        applied_at,
                        memory_id,
                        proposal_id,
                        entrance,
                        result_hash,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO idempotent_writes
                        (operation, idempotency_key, subject_id, request_hash,
                         result_hash, created_at)
                    VALUES ('approve-source-memory', ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        proposal_id,
                        request_hash,
                        result_hash,
                        applied_at,
                    ),
                )
                if connection.execute(
                    "SELECT 1 FROM review_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone() is not None:
                    connection.execute(
                        """
                        UPDATE review_proposals
                        SET status = 'applied', updated_at = ?, last_error = NULL
                        WHERE proposal_id = ? AND status = 'pending'
                        """,
                        (applied_at, proposal_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO review_materializations
                            (proposal_id, artifact_kind, artifact_id, authorship,
                             personal_cognition, final_content_hash, created_at)
                        VALUES (?, 'canonical-memory', ?,
                                'creator-approved-integration', 0, ?, ?)
                        """,
                        (proposal_id, memory_id, _stable_hash(body), applied_at),
                    )
                connection.commit()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise IntegrityError("memory approval left a dangling reference")
            return temporary_path.read_bytes(), approval
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage source memory approval") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def pending_integration_proposals(self) -> tuple[IntegrationProposal, ...]:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            return self._query_integration_proposals(
                database_path,
                status="pending",
            )

    def pending_proposals_for_digests(
        self, task: str, digest_ids: tuple[str, ...]
    ) -> tuple[IntegrationProposal, ...]:
        normalized_task = _required_text("consolidation task", task)
        normalized_ids = tuple(
            _required_text("memory digest", digest_id)
            for digest_id in digest_ids
        )
        if not normalized_ids:
            return ()
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            self._validate_database(database_path)
            requested = frozenset(normalized_ids)
            return tuple(
                proposal
                for proposal in self._query_integration_proposals(
                    database_path,
                    status="pending",
                    topic=normalized_task,
                )
                if frozenset(proposal.evidence_memory_ids) == requested
            )

    @staticmethod
    def _query_integration_proposals(
        database_path: Path,
        *,
        status: str,
        topic: str | None = None,
    ) -> tuple[IntegrationProposal, ...]:
        parameters: list[str] = [status]
        topic_filter = ""
        if topic is not None:
            topic_filter = " AND p.topic = ?"
            parameters.append(topic)
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    f"""
                    SELECT p.proposal_id, p.topic, p.proposed_understanding,
                           p.possible_impact, p.sensitivity, p.status,
                           GROUP_CONCAT(DISTINCT buffered.digest_id),
                           GROUP_CONCAT(DISTINCT source.source_id),
                           GROUP_CONCAT(DISTINCT related.memory_id),
                           p.suggested_action, p.target_memory_id
                    FROM integration_proposals AS p
                    LEFT JOIN integration_proposal_buffered AS buffered
                      ON buffered.proposal_id = p.proposal_id
                    LEFT JOIN integration_proposal_sources AS source
                      ON source.proposal_id = p.proposal_id
                    LEFT JOIN integration_proposal_related AS related
                      ON related.proposal_id = p.proposal_id
                    WHERE p.status = ?{topic_filter}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM source_memory_proposal_details AS source_memory
                          WHERE source_memory.proposal_id = p.proposal_id
                      )
                    GROUP BY p.proposal_id
                    ORDER BY p.created_at, p.proposal_id
                    """,
                    parameters,
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot read integration proposals") from error
        return tuple(
            IntegrationProposal(
                proposal_id=row[0],
                topic=row[1],
                proposed_understanding=row[2],
                possible_impact=row[3],
                sensitivity=row[4],
                status=row[5],
                evidence_memory_ids=_split_group(row[6]),
                source_scope=_split_group(row[7]),
                related_canonical_memory_ids=_split_group(row[8]),
                suggested_action=row[9],
                target_memory_id=row[10],
            )
            for row in rows
        )

    @staticmethod
    def _duplicate_receipt(
        database_path: Path,
        *,
        experience_id: str,
        source_id: str,
        metadata: ExperienceMetadata,
        expected_digest: str,
    ) -> BufferedMemoryReceipt | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT digest_id, content
                    FROM buffered_digests
                    WHERE experience_id = ?
                    """,
                    (experience_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot query the local memory database") from error
        if row is None:
            return None
        digest_id, digest = row
        if not isinstance(digest_id, str) or not isinstance(digest, str):
            raise IntegrityError("buffered memory has invalid persisted fields")
        if digest != expected_digest:
            raise UserInputError(
                "this experience already has a different buffered-memory digest"
            )
        return BufferedMemoryReceipt(
            source_id=source_id,
            experience_id=experience_id,
            digest_id=digest_id,
            digest=digest,
            disposition="duplicate",
            metadata=metadata,
        )

    @staticmethod
    def _database_with_state_change(
        database_path: Path,
        *,
        memory_id: str,
        state: str,
        event_id: str,
        event_type: str,
        occurred_at: str,
        payload: dict[str, str],
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-state.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                updated = connection.execute(
                    """
                    UPDATE canonical_memories
                    SET state = ?, updated_at = ?
                    WHERE memory_id = ?
                    """,
                    (state, occurred_at, memory_id),
                )
                if updated.rowcount != 1:
                    raise sqlite3.IntegrityError("canonical memory state was not updated")
                connection.execute(
                    """
                    INSERT INTO memory_events
                        (event_id, event_type, occurred_at, subject_id, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        occurred_at,
                        memory_id,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage canonical memory state change") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _database_with_capture(
        database_path: Path,
        *,
        source_id: str,
        content_hash: str,
        object_reference: str,
        experience_id: str,
        metadata: ExperienceMetadata,
        digest_id: str,
        digest: str,
        digest_fingerprint: str,
        event_id: str,
        event_payload: dict[str, str],
        created_at: str,
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-stage.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO source_objects
                        (source_id, content_hash, object_reference, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_id, content_hash, object_reference, created_at),
                )
                connection.execute(
                    """
                    INSERT INTO experiences
                        (experience_id, source_id, occurred_at, entrance, task,
                         sensitivity, visible_context, context_gaps_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        experience_id,
                        source_id,
                        metadata.occurred_at,
                        metadata.entrance,
                        metadata.task,
                        metadata.sensitivity,
                        metadata.visible_context,
                        json.dumps(metadata.context_gaps, ensure_ascii=False),
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO buffered_digests
                        (digest_id, experience_id, content, fingerprint, state, created_at)
                    VALUES (?, ?, ?, ?, 'buffered', ?)
                    """,
                    (
                        digest_id,
                        experience_id,
                        digest,
                        digest_fingerprint,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_events
                        (event_id, event_type, occurred_at, subject_id, payload_json)
                    VALUES (?, 'memory.buffered', ?, ?, ?)
                    """,
                    (
                        event_id,
                        created_at,
                        digest_id,
                        json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage buffered memory") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _database_with_integration_proposals(
        database_path: Path,
        *,
        drafts: tuple[_IntegrationProposalDraft, ...],
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-proposal.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for draft in drafts:
                    connection.execute(
                        """
                        INSERT INTO integration_proposals
                            (proposal_id, topic, proposed_understanding,
                             possible_impact, sensitivity, suggested_action,
                             target_memory_id, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            draft.proposal_id,
                            draft.topic,
                            draft.proposed_understanding,
                            draft.possible_impact,
                            draft.sensitivity,
                            draft.suggested_action,
                            draft.target_memory_id,
                            draft.created_at,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO integration_proposal_buffered
                            (proposal_id, digest_id)
                        VALUES (?, ?)
                        """,
                        (
                            (draft.proposal_id, digest_id)
                            for digest_id in draft.digest_ids
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO integration_proposal_sources
                            (proposal_id, source_id)
                        VALUES (?, ?)
                        """,
                        (
                            (draft.proposal_id, source_id)
                            for source_id in draft.source_ids
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO integration_proposal_related
                            (proposal_id, memory_id)
                        VALUES (?, ?)
                        """,
                        (
                            (draft.proposal_id, memory_id)
                            for memory_id in draft.related_memory_ids
                        ),
                    )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage integration proposal") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _database_with_integration_review(
        database_path: Path,
        *,
        proposal: IntegrationProposal,
        review: _ReviewInstruction,
        canonical_memory_id: str | None,
        canonical_content: str | None,
        action: IntegrationAction,
        applied_action: AppliedIntegrationAction,
        target_memory_id: str | None,
        reviewed_at: str,
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-review.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                proposal_status = (
                    "rejected" if review.decision == "rejected" else "accepted"
                )
                if canonical_memory_id is not None and canonical_content is not None:
                    existing = connection.execute(
                        """
                        SELECT content, current_version, sensitivity
                        FROM canonical_memories
                        WHERE memory_id = ? AND state = 'active'
                        """,
                        (canonical_memory_id,),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO canonical_memories
                                (memory_id, content, current_version, sensitivity,
                                 state, created_at, updated_at)
                            VALUES (?, ?, 1, ?, 'active', ?, ?)
                            """,
                            (
                                canonical_memory_id,
                                canonical_content,
                                proposal.sensitivity,
                                reviewed_at,
                                reviewed_at,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO canonical_memory_versions
                                (memory_id, version, content, action, change_reason,
                                 created_at, superseded_at, supersession_reason)
                            VALUES (?, 1, ?, 'created', ?, ?, NULL, NULL)
                            """,
                            (
                                canonical_memory_id,
                                canonical_content,
                                review.reason,
                                reviewed_at,
                            ),
                        )
                        if action == "new":
                            connection.executemany(
                                """
                                INSERT INTO canonical_memory_relations
                                    (memory_id, related_memory_id, relationship,
                                     created_at)
                                VALUES (?, ?, 'related', ?)
                                """,
                                (
                                    (
                                        canonical_memory_id,
                                        related_memory_id,
                                        reviewed_at,
                                    )
                                    for related_memory_id
                                    in proposal.related_canonical_memory_ids
                                    if related_memory_id != canonical_memory_id
                                ),
                            )
                        if action == "conflict" and target_memory_id is not None:
                            first_memory_id, second_memory_id = sorted(
                                (canonical_memory_id, target_memory_id)
                            )
                            conflict_identity = (
                                f"{first_memory_id}:{second_memory_id}".encode()
                            )
                            connection.execute(
                                """
                                INSERT INTO canonical_memory_conflicts
                                    (conflict_id, first_memory_id, second_memory_id,
                                     reason, status, created_at, resolved_at)
                                VALUES (?, ?, ?, ?, 'unresolved', ?, NULL)
                                """,
                                (
                                    "con_"
                                    + hashlib.sha256(conflict_identity).hexdigest(),
                                    first_memory_id,
                                    second_memory_id,
                                    review.reason,
                                    reviewed_at,
                                ),
                            )
                    else:
                        existing_content, current_version, existing_sensitivity = (
                            existing
                        )
                        if not isinstance(existing_content, str) or not isinstance(
                            current_version, int
                        ):
                            raise IntegrityError(
                                "canonical memory has invalid revision state"
                            )
                        effective_sensitivity = (
                            "local-only"
                            if proposal.sensitivity == "local-only"
                            or existing_sensitivity == "local-only"
                            else "cloud-allowed"
                        )
                        if (
                            _normalized_memory_body(existing_content)
                            == _normalized_memory_body(canonical_content)
                        ):
                            connection.execute(
                                """
                                UPDATE canonical_memories
                                SET sensitivity = ?, updated_at = ?
                                WHERE memory_id = ?
                                """,
                                (
                                    effective_sensitivity,
                                    reviewed_at,
                                    canonical_memory_id,
                                ),
                            )
                        else:
                            next_version = current_version + 1
                            superseded = connection.execute(
                                """
                                UPDATE canonical_memory_versions
                                SET superseded_at = ?, supersession_reason = ?
                                WHERE memory_id = ? AND version = ?
                                  AND superseded_at IS NULL
                                """,
                                (
                                    reviewed_at,
                                    review.reason,
                                    canonical_memory_id,
                                    current_version,
                                ),
                            )
                            if superseded.rowcount != 1:
                                raise IntegrityError(
                                    "canonical memory current version is missing"
                                )
                            connection.execute(
                                """
                                UPDATE canonical_memories
                                SET content = ?, current_version = ?, sensitivity = ?,
                                    updated_at = ?
                                WHERE memory_id = ?
                                """,
                                (
                                    canonical_content,
                                    next_version,
                                    effective_sensitivity,
                                    reviewed_at,
                                    canonical_memory_id,
                                ),
                            )
                            version_action = (
                                "supplemented"
                                if action == "supplement"
                                else "revised"
                            )
                            connection.execute(
                                """
                                INSERT INTO canonical_memory_versions
                                    (memory_id, version, content, action,
                                     change_reason, created_at, superseded_at,
                                     supersession_reason)
                                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                                """,
                                (
                                    canonical_memory_id,
                                    next_version,
                                    canonical_content,
                                    version_action,
                                    review.reason,
                                    reviewed_at,
                                ),
                            )
                            if action == "supplement":
                                connection.execute(
                                    """
                                    INSERT INTO canonical_memory_version_sources
                                        (memory_id, version, source_id)
                                    SELECT memory_id, ?, source_id
                                    FROM canonical_memory_version_sources
                                    WHERE memory_id = ? AND version = ?
                                    """,
                                    (
                                        next_version,
                                        canonical_memory_id,
                                        current_version,
                                    ),
                                )
                    current_version_row = connection.execute(
                        """
                        SELECT current_version FROM canonical_memories
                        WHERE memory_id = ?
                        """,
                        (canonical_memory_id,),
                    ).fetchone()
                    if current_version_row is None or not isinstance(
                        current_version_row[0], int
                    ):
                        raise IntegrityError("canonical memory has no current version")
                    current_version = current_version_row[0]
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO canonical_memory_version_sources
                            (memory_id, version, source_id)
                        VALUES (?, ?, ?)
                        """,
                        (
                            (canonical_memory_id, current_version, source_id)
                            for source_id in proposal.source_scope
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO canonical_memory_sources
                            (memory_id, source_id)
                        VALUES (?, ?)
                        """,
                        (
                            (canonical_memory_id, source_id)
                            for source_id in proposal.source_scope
                        ),
                    )
                    connection.executemany(
                        """
                        UPDATE buffered_digests
                        SET state = 'integrated'
                        WHERE digest_id = ?
                        """,
                        ((digest_id,) for digest_id in proposal.evidence_memory_ids),
                    )
                connection.execute(
                    """
                    UPDATE integration_proposals
                    SET status = ?, reviewed_at = ?
                    WHERE proposal_id = ? AND status = 'pending'
                    """,
                    (proposal_status, reviewed_at, proposal.proposal_id),
                )
                review_id = f"rev_{hashlib.sha256(proposal.proposal_id.encode()).hexdigest()}"
                connection.execute(
                    """
                    INSERT INTO integration_reviews
                        (review_id, proposal_id, decision, action, reviewed_content,
                         reason, canonical_memory_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        proposal.proposal_id,
                        review.decision,
                        applied_action,
                        canonical_content,
                        review.reason,
                        canonical_memory_id,
                        reviewed_at,
                    ),
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage integration review") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _new_database_content(parent: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=parent,
                prefix=".memory.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot initialize the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v8_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(REFLECTION_SCHEMA)
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memory_names (
                        memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL,
                        name_kind TEXT NOT NULL
                            CHECK (name_kind IN ('canonical', 'alias')),
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (memory_id, normalized_name)
                    );
                    CREATE INDEX IF NOT EXISTS memory_names_lookup
                    ON memory_names(normalized_name, memory_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS memory_names_one_canonical
                    ON memory_names(memory_id) WHERE name_kind = 'canonical';
                    CREATE TABLE IF NOT EXISTS memory_name_changes (
                        idempotency_key TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS source_memory_reuse_submissions (
                        idempotency_key TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO memory_names
                        (memory_id, name, normalized_name, name_kind, created_at)
                    SELECT dictionary.memory_id, dictionary.canonical_name,
                           dictionary.normalized_name, 'canonical', memory.created_at
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memories AS memory
                      ON memory.memory_id = dictionary.memory_id;
                    """
                )
                connection.execute("PRAGMA user_version = 9")
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v7_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_partitions (
                        partition_id TEXT PRIMARY KEY,
                        parent_partition_id TEXT
                            REFERENCES knowledge_partitions(partition_id),
                        node_kind TEXT NOT NULL
                            CHECK (node_kind IN ('root', 'leaf')),
                        topic TEXT NOT NULL,
                        normalized_topic TEXT NOT NULL,
                        CHECK (
                            (node_kind = 'root' AND parent_partition_id IS NULL)
                            OR (node_kind = 'leaf'
                                AND parent_partition_id IS NOT NULL)
                        )
                    );
                    CREATE TABLE IF NOT EXISTS capsule_partitions (
                        capsule_id TEXT PRIMARY KEY
                            REFERENCES knowledge_capsules(capsule_id),
                        partition_id TEXT NOT NULL
                            REFERENCES knowledge_partitions(partition_id)
                    );
                    INSERT OR IGNORE INTO knowledge_partitions
                        (partition_id, parent_partition_id, node_kind, topic,
                         normalized_topic)
                    VALUES ('prt_root', NULL, 'root', 'All knowledge', 'all knowledge');
                    INSERT OR IGNORE INTO knowledge_partitions
                        (partition_id, parent_partition_id, node_kind, topic,
                         normalized_topic)
                    SELECT 'prt_' || substr(capsule_id, 5, 32), 'prt_root', 'leaf',
                           topic, lower(trim(topic))
                    FROM knowledge_capsules;
                    INSERT OR IGNORE INTO capsule_partitions
                        (capsule_id, partition_id)
                    SELECT capsule_id, 'prt_' || substr(capsule_id, 5, 32)
                    FROM knowledge_capsules;

                    CREATE VIRTUAL TABLE IF NOT EXISTS canonical_memory_fts USING fts5(
                        memory_id UNINDEXED,
                        capsule_id UNINDEXED,
                        canonical_name,
                        body,
                        applicability_scope,
                        search_terms,
                        tokenize = 'unicode61'
                    );

                    CREATE TABLE IF NOT EXISTS recall_events (
                        recall_id TEXT PRIMARY KEY,
                        occurred_at TEXT NOT NULL,
                        entrance TEXT NOT NULL,
                        task TEXT NOT NULL,
                        paths_json TEXT NOT NULL,
                        budget_limit_bytes INTEGER NOT NULL
                            CHECK (budget_limit_bytes > 0),
                        used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
                        was_truncated INTEGER NOT NULL
                            CHECK (was_truncated IN (0, 1)),
                        answerable INTEGER NOT NULL CHECK (answerable IN (0, 1)),
                        answerability_reason TEXT NOT NULL,
                        answerability_overridden INTEGER NOT NULL
                            CHECK (answerability_overridden IN (0, 1)),
                        cross_partition_hit INTEGER NOT NULL
                            CHECK (cross_partition_hit IN (0, 1)),
                        ambiguity_detected INTEGER NOT NULL
                            CHECK (ambiguity_detected IN (0, 1)),
                        missing_dependency INTEGER NOT NULL
                            CHECK (missing_dependency IN (0, 1)),
                        unresolved_conflict INTEGER NOT NULL
                            CHECK (unresolved_conflict IN (0, 1))
                    );
                    CREATE TABLE IF NOT EXISTS recall_event_items (
                        recall_id TEXT NOT NULL REFERENCES recall_events(recall_id),
                        memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        version INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        candidate_paths_json TEXT NOT NULL,
                        PRIMARY KEY (recall_id, memory_id),
                        FOREIGN KEY (memory_id, version)
                            REFERENCES canonical_memory_versions(memory_id, version)
                    );
                    CREATE TABLE IF NOT EXISTS recall_evidence_expansions (
                        recall_id TEXT NOT NULL REFERENCES recall_events(recall_id),
                        memory_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL,
                        expanded_bytes INTEGER NOT NULL CHECK (expanded_bytes >= 0),
                        was_truncated INTEGER NOT NULL
                            CHECK (was_truncated IN (0, 1)),
                        PRIMARY KEY (
                            recall_id, memory_id, source_id, source_version
                        ),
                        FOREIGN KEY (recall_id, memory_id)
                            REFERENCES recall_event_items(recall_id, memory_id),
                        FOREIGN KEY (source_id, source_version)
                            REFERENCES evidence_source_versions(source_id, version)
                    );
                    """
                )
                fts_rows = connection.execute(
                    """
                    SELECT dictionary.memory_id, dictionary.primary_capsule_id,
                           dictionary.canonical_name, version.content,
                           version.applicability_scope
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = dictionary.memory_id
                     AND version.version = dictionary.current_version
                    """
                ).fetchall()
                connection.execute("DELETE FROM canonical_memory_fts")
                connection.executemany(
                    """
                    INSERT INTO canonical_memory_fts
                        (memory_id, capsule_id, canonical_name, body,
                         applicability_scope, search_terms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            row[0],
                            row[1],
                            row[2],
                            row[3],
                            row[4],
                            " ".join(
                                sorted(
                                    lexical_terms(
                                        f"{row[2]} {row[3]} {row[4]}"
                                    )
                                )
                            ),
                        )
                        for row in fts_rows
                    ),
                )
                connection.executescript(UNIFIED_REVIEW_SCHEMA)
                pending_source_rows = connection.execute(
                    """
                    SELECT detail.proposal_id, detail.planned_memory_id,
                           detail.canonical_name, proposal.proposed_understanding,
                           detail.applicability_scope, detail.source_id,
                           detail.source_version, version.content_hash,
                           version.locator, version.observed_at,
                           version.applicability_scope, version.retention,
                           proposal.created_at
                    FROM source_memory_proposal_details AS detail
                    JOIN integration_proposals AS proposal
                      ON proposal.proposal_id = detail.proposal_id
                    JOIN evidence_source_versions AS version
                      ON version.source_id = detail.source_id
                     AND version.version = detail.source_version
                    WHERE proposal.status = 'pending'
                    ORDER BY proposal.created_at, proposal.proposal_id
                    """
                ).fetchall()
                for row in pending_source_rows:
                    if (
                        not all(
                            isinstance(row[index], str)
                            for index in (0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12)
                        )
                        or not isinstance(row[6], int)
                    ):
                        raise IntegrityError(
                            "cannot migrate an invalid source memory proposal"
                        )
                    register_source_memory_proposal(
                        connection,
                        proposal_id=row[0],
                        planned_memory_id=row[1],
                        canonical_name=row[2],
                        body=row[3],
                        applicability_scope=row[4],
                        source={
                            "source_id": row[5],
                            "version": row[6],
                            "content_hash": row[7],
                            "locator": row[8],
                            "observed_at": row[9],
                            "applicability_scope": row[10],
                            "retention": row[11],
                        },
                        created_at=row[12],
                    )
                connection.execute("PRAGMA user_version = 8")
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v6_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_sources (
                        source_id TEXT PRIMARY KEY,
                        source_kind TEXT NOT NULL CHECK (source_kind = 'local'),
                        current_locator TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS evidence_source_versions (
                        source_id TEXT NOT NULL
                            REFERENCES evidence_sources(source_id),
                        version INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        locator TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        applicability_scope TEXT NOT NULL,
                        retention TEXT NOT NULL CHECK (retention = 'receipt'),
                        PRIMARY KEY (source_id, version)
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_capsules (
                        capsule_id TEXT PRIMARY KEY,
                        topic TEXT NOT NULL,
                        body_bytes INTEGER NOT NULL CHECK (body_bytes >= 0),
                        memory_record_count INTEGER NOT NULL
                            CHECK (memory_record_count >= 0),
                        structural_version INTEGER NOT NULL
                            CHECK (structural_version >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_dictionary (
                        memory_id TEXT PRIMARY KEY
                            REFERENCES canonical_memories(memory_id),
                        canonical_name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL,
                        current_version INTEGER NOT NULL,
                        primary_capsule_id TEXT NOT NULL
                            REFERENCES knowledge_capsules(capsule_id),
                        FOREIGN KEY (memory_id, current_version)
                            REFERENCES canonical_memory_versions(memory_id, version)
                    );
                    CREATE TABLE IF NOT EXISTS canonical_memory_version_evidence (
                        memory_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL,
                        relationship TEXT NOT NULL
                            CHECK (relationship = 'supports'),
                        FOREIGN KEY (memory_id, version)
                            REFERENCES canonical_memory_versions(memory_id, version),
                        FOREIGN KEY (source_id, source_version)
                            REFERENCES evidence_source_versions(source_id, version),
                        PRIMARY KEY (
                            memory_id, version, source_id, source_version, relationship
                        )
                    );
                    CREATE TABLE IF NOT EXISTS source_memory_proposal_details (
                        proposal_id TEXT PRIMARY KEY
                            REFERENCES integration_proposals(proposal_id),
                        proposal_version INTEGER NOT NULL
                            CHECK (proposal_version = 1),
                        planned_memory_id TEXT NOT NULL UNIQUE,
                        canonical_name TEXT NOT NULL,
                        applicability_scope TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        FOREIGN KEY (source_id, source_version)
                            REFERENCES evidence_source_versions(source_id, version)
                    );
                    CREATE TABLE IF NOT EXISTS audit_events (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        subject_id TEXT NOT NULL,
                        proposal_id TEXT,
                        before_version INTEGER,
                        after_version INTEGER,
                        entrance TEXT NOT NULL,
                        result_hash TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS idempotent_writes (
                        operation TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        subject_id TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        result_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (operation, idempotency_key)
                    );
                    PRAGMA user_version = 7;
                    """
                )
                version_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(canonical_memory_versions)"
                    ).fetchall()
                    if isinstance(row[1], str)
                }
                if "applicability_scope" not in version_columns:
                    connection.execute(
                        "ALTER TABLE canonical_memory_versions "
                        "ADD COLUMN applicability_scope TEXT"
                    )
                if "capsule_id" not in version_columns:
                    connection.execute(
                        "ALTER TABLE canonical_memory_versions "
                        "ADD COLUMN capsule_id TEXT "
                        "REFERENCES knowledge_capsules(capsule_id)"
                    )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v5_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    CREATE TABLE deletion_markers (
                        marker_id TEXT PRIMARY KEY,
                        subject_kind TEXT NOT NULL
                            CHECK (subject_kind IN
                                ('canonical-memory', 'source')),
                        subject_fingerprint TEXT NOT NULL UNIQUE,
                        deleted_at TEXT NOT NULL,
                        backup_exclusion_after TEXT NOT NULL
                    );
                    PRAGMA user_version = 6;
                    """
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v4_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    CREATE TABLE legacy_migration_runs (
                        migration_id TEXT PRIMARY KEY,
                        source_schema_version INTEGER NOT NULL,
                        source_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status = 'complete'),
                        source_count INTEGER NOT NULL,
                        insight_count INTEGER NOT NULL,
                        cognition_count INTEGER NOT NULL,
                        event_count INTEGER NOT NULL,
                        completed_at TEXT NOT NULL
                    );
                    CREATE TABLE legacy_audit_events (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE legacy_source_metadata (
                        source_id TEXT PRIMARY KEY REFERENCES source_objects(source_id),
                        sensitivity TEXT NOT NULL
                            CHECK (sensitivity IN
                                ('local-only', 'cloud-allowed')),
                        origins_json TEXT NOT NULL,
                        legacy_record_path TEXT NOT NULL
                    );
                    CREATE TABLE legacy_knowledge_metadata (
                        memory_id TEXT PRIMARY KEY
                            REFERENCES canonical_memories(memory_id),
                        legacy_kind TEXT NOT NULL
                            CHECK (legacy_kind IN ('insight', 'cognition')),
                        legacy_state TEXT NOT NULL
                            CHECK (legacy_state IN
                                ('active', 'superseded', 'archived')),
                        authorship TEXT NOT NULL
                            CHECK (authorship IN ('user', 'system', 'mixed')),
                        legacy_path TEXT NOT NULL,
                        candidate_id TEXT,
                        relations_json TEXT NOT NULL
                    );
                    PRAGMA user_version = 5;
                    """
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v3_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    ALTER TABLE integration_proposals
                    ADD COLUMN suggested_action TEXT NOT NULL DEFAULT 'new'
                        CHECK (suggested_action IN
                            ('new', 'supplement', 'revise', 'conflict'));
                    ALTER TABLE integration_proposals
                    ADD COLUMN target_memory_id TEXT
                        REFERENCES canonical_memories(memory_id);
                    ALTER TABLE integration_reviews
                    ADD COLUMN action TEXT NOT NULL DEFAULT 'created'
                        CHECK (action IN
                            ('created', 'supplemented', 'revised',
                             'conflicted', 'rejected'));
                    UPDATE integration_reviews
                    SET action = 'rejected'
                    WHERE decision = 'rejected';

                    CREATE TABLE canonical_memory_versions (
                        memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        version INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        action TEXT NOT NULL
                            CHECK (action IN ('created', 'supplemented', 'revised')),
                        change_reason TEXT,
                        created_at TEXT NOT NULL,
                        superseded_at TEXT,
                        supersession_reason TEXT,
                        PRIMARY KEY (memory_id, version)
                    );
                    CREATE TABLE canonical_memory_version_sources (
                        memory_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        source_id TEXT NOT NULL REFERENCES source_objects(source_id),
                        FOREIGN KEY (memory_id, version)
                            REFERENCES canonical_memory_versions(memory_id, version),
                        PRIMARY KEY (memory_id, version, source_id)
                    );
                    INSERT INTO canonical_memory_versions
                        (memory_id, version, content, action, change_reason,
                         created_at, superseded_at, supersession_reason)
                    SELECT memory_id, current_version, content, 'created', NULL,
                           created_at, NULL, NULL
                    FROM canonical_memories;
                    INSERT INTO canonical_memory_version_sources
                        (memory_id, version, source_id)
                    SELECT source.memory_id, memory.current_version, source.source_id
                    FROM canonical_memory_sources AS source
                    JOIN canonical_memories AS memory
                      ON memory.memory_id = source.memory_id;
                    CREATE TABLE canonical_memory_conflicts (
                        conflict_id TEXT PRIMARY KEY,
                        first_memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        second_memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        reason TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK (status IN ('unresolved', 'resolved')),
                        created_at TEXT NOT NULL,
                        resolved_at TEXT,
                        CHECK (first_memory_id < second_memory_id),
                        UNIQUE (first_memory_id, second_memory_id)
                    );
                    PRAGMA user_version = 4;
                    """
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v2_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript(
                    """
                    CREATE TABLE buffered_digests_v3 (
                        digest_id TEXT PRIMARY KEY,
                        experience_id TEXT NOT NULL UNIQUE REFERENCES experiences(experience_id),
                        content TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('buffered', 'integrated')),
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO buffered_digests_v3
                        (digest_id, experience_id, content, fingerprint, state, created_at)
                    SELECT digest_id, experience_id, content, fingerprint, state, created_at
                    FROM buffered_digests;
                    DROP TABLE buffered_digests;
                    ALTER TABLE buffered_digests_v3 RENAME TO buffered_digests;

                    CREATE TABLE integration_proposals (
                        proposal_id TEXT PRIMARY KEY,
                        topic TEXT NOT NULL,
                        proposed_understanding TEXT NOT NULL,
                        possible_impact TEXT NOT NULL,
                        sensitivity TEXT NOT NULL
                            CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
                        status TEXT NOT NULL
                            CHECK (status IN ('pending', 'accepted', 'rejected')),
                        created_at TEXT NOT NULL,
                        reviewed_at TEXT
                    );
                    CREATE TABLE canonical_memory_relations (
                        memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
                        related_memory_id TEXT NOT NULL
                            REFERENCES canonical_memories(memory_id),
                        relationship TEXT NOT NULL CHECK (relationship = 'related'),
                        created_at TEXT NOT NULL,
                        CHECK (memory_id <> related_memory_id),
                        PRIMARY KEY (memory_id, related_memory_id)
                    );
                    CREATE TABLE integration_proposal_buffered (
                        proposal_id TEXT NOT NULL
                            REFERENCES integration_proposals(proposal_id),
                        digest_id TEXT NOT NULL REFERENCES buffered_digests(digest_id),
                        PRIMARY KEY (proposal_id, digest_id)
                    );
                    CREATE TABLE integration_proposal_related (
                        proposal_id TEXT NOT NULL
                            REFERENCES integration_proposals(proposal_id),
                        memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
                        PRIMARY KEY (proposal_id, memory_id)
                    );
                    CREATE TABLE integration_proposal_sources (
                        proposal_id TEXT NOT NULL
                            REFERENCES integration_proposals(proposal_id),
                        source_id TEXT NOT NULL REFERENCES source_objects(source_id),
                        PRIMARY KEY (proposal_id, source_id)
                    );
                    CREATE TABLE integration_reviews (
                        review_id TEXT PRIMARY KEY,
                        proposal_id TEXT NOT NULL UNIQUE
                            REFERENCES integration_proposals(proposal_id),
                        decision TEXT NOT NULL
                            CHECK (decision IN ('accepted', 'edited', 'rejected')),
                        reviewed_content TEXT,
                        reason TEXT,
                        canonical_memory_id TEXT REFERENCES canonical_memories(memory_id),
                        created_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute("PRAGMA user_version = 3")
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_database(database_path: Path) -> None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                version_row = connection.execute("PRAGMA user_version").fetchone()
                integrity_row = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as error:
            raise IntegrityError(
                f"cannot read local memory database: {database_path}"
            ) from error
        version = version_row[0] if version_row is not None else None
        if version != MEMORY_SCHEMA_VERSION:
            raise ConfigurationConflict(
                f"unsupported memory schema version {version}: {database_path}"
            )
        if integrity_row != ("ok",):
            raise IntegrityError(f"local memory database is corrupt: {database_path}")

    @staticmethod
    def _database_version(database_path: Path) -> int:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as error:
            raise IntegrityError(
                f"cannot read local memory database version: {database_path}"
            ) from error
        if row is None or not isinstance(row[0], int):
            raise IntegrityError(f"local memory database has no schema version: {database_path}")
        return row[0]

    @staticmethod
    def _migrate_v1_database(database_path: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-migrate.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    """
                    ALTER TABLE canonical_memories
                    ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'local-only'
                    CHECK (sensitivity IN ('local-only', 'cloud-allowed'));
                    ALTER TABLE canonical_memories
                    ADD COLUMN state TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active', 'inactive'));
                    CREATE TABLE canonical_memory_sources (
                        memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
                        source_id TEXT NOT NULL REFERENCES source_objects(source_id),
                        PRIMARY KEY (memory_id, source_id)
                    );
                    """
                )
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot migrate the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _read_conversation(conversation_path: Path) -> bytes:
    return _read_required_utf8_file(conversation_path, label="conversation")


def _read_local_source(source_path: Path) -> bytes:
    return _read_required_utf8_file(source_path, label="local source")


def _read_required_utf8_file(path: Path, *, label: str) -> bytes:
    if not path.is_file():
        raise UserInputError(f"{label} does not exist: {path}")
    try:
        body = path.read_bytes()
        text = body.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UserInputError(f"{label} is not readable UTF-8: {path}") from error
    if not text.strip():
        raise UserInputError(f"{label} must not be blank")
    return body


def _required_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserInputError(f"{name} must not be blank")
    return normalized


def _bounded_text(name: str, value: str, *, maximum: int) -> str:
    normalized = _required_text(name, value)
    if len(normalized) > maximum:
        raise UserInputError(f"{name} must not exceed {maximum} characters")
    return normalized


def _validated_source_id(value: str) -> str:
    source_id = _required_text("source id", value)
    if re.fullmatch(r"src_[0-9a-f]{32}", source_id) is None:
        raise UserInputError(f"invalid local source id: {source_id}")
    return source_id


def _validated_memory_body(value: str) -> str:
    body = _required_text("canonical memory body", value)
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > MEMORY_BODY_HARD_LIMIT_BYTES:
        raise UserInputError(
            "canonical memory body exceeds the 8192-byte hard limit; "
            "keep excess detail in the evidence source"
        )
    return body


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _without_evidence_marker(content: str) -> str:
    return re.sub(r"\s*\[evidence:\s+src_[0-9a-f]+\]\s*$", "", content).strip()


def _canonical_memory_audit(
    connection: sqlite3.Connection,
    memory_id: str,
) -> CanonicalMemoryAudit:
    current = connection.execute(
        """
        SELECT current_version, state
        FROM canonical_memories
        WHERE memory_id = ?
        """,
        (memory_id,),
    ).fetchone()
    version_rows = connection.execute(
        """
        SELECT version.version, version.content, version.action,
               version.change_reason, version.superseded_at,
               version.supersession_reason,
               GROUP_CONCAT(
                   DISTINCT COALESCE(source.source_id, receipt.source_id)
               )
        FROM canonical_memory_versions AS version
        LEFT JOIN canonical_memory_version_sources AS source
          ON source.memory_id = version.memory_id
         AND source.version = version.version
        LEFT JOIN canonical_memory_version_evidence AS receipt
          ON receipt.memory_id = version.memory_id
         AND receipt.version = version.version
        WHERE version.memory_id = ?
        GROUP BY version.memory_id, version.version
        ORDER BY version.version
        """,
        (memory_id,),
    ).fetchall()
    conflict_rows = connection.execute(
        """
        SELECT other.memory_id, other.content, conflict.reason,
               GROUP_CONCAT(source.source_id, ',')
        FROM canonical_memory_conflicts AS conflict
        JOIN canonical_memories AS other
          ON other.memory_id = CASE
              WHEN conflict.first_memory_id = ?
              THEN conflict.second_memory_id
              ELSE conflict.first_memory_id
          END
        LEFT JOIN canonical_memory_version_sources AS source
          ON source.memory_id = other.memory_id
         AND source.version = other.current_version
        WHERE conflict.status = 'unresolved'
          AND (? = conflict.first_memory_id OR ? = conflict.second_memory_id)
        GROUP BY conflict.conflict_id, other.memory_id
        ORDER BY other.memory_id
        """,
        (memory_id, memory_id, memory_id),
    ).fetchall()
    lifecycle_rows = connection.execute(
        """
        SELECT event_type, occurred_at, payload_json
        FROM memory_events
        WHERE subject_id = ?
          AND event_type IN ('memory.deactivated', 'memory.reactivated')
        ORDER BY occurred_at, event_id
        """,
        (memory_id,),
    ).fetchall()
    if (
        current is None
        or not isinstance(current[0], int)
        or current[1] not in ("active", "inactive")
    ):
        raise UserInputError(f"canonical memory does not exist: {memory_id}")
    versions = tuple(
        CanonicalMemoryVersion(
            version=row[0],
            content=row[1],
            action=row[2],
            change_reason=row[3],
            status="superseded" if row[4] is not None else "current",
            supersession_reason=row[5],
            source_ids=_split_group(row[6]),
        )
        for row in version_rows
    )
    conflicts = tuple(
        UnresolvedMemoryConflict(
            memory_id=row[0],
            content=row[1],
            reason=row[2],
            source_ids=_split_group(row[3]),
        )
        for row in conflict_rows
    )
    lifecycle_events: list[MemoryLifecycleEvent] = []
    for event_type, occurred_at, payload_json in lifecycle_rows:
        try:
            payload = json.loads(payload_json)
            reason = payload["reason"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise IntegrityError("canonical memory lifecycle audit is invalid") from error
        if not isinstance(reason, str):
            raise IntegrityError("canonical memory lifecycle audit is invalid")
        lifecycle_events.append(
            MemoryLifecycleEvent(
                action=(
                    "deactivated"
                    if event_type == "memory.deactivated"
                    else "reactivated"
                ),
                occurred_at=occurred_at,
                reason=reason,
            )
        )
    current_version = current[0]
    current_content = next(
        (
            version.content
            for version in versions
            if version.version == current_version
        ),
        None,
    )
    if current_content is None:
        raise IntegrityError("canonical memory current version is missing")
    current_sources = next(
        (
            version.source_ids
            for version in versions
            if version.version == current_version
        ),
        (),
    )
    return CanonicalMemoryAudit(
        memory_id=memory_id,
        state=current[1],
        confirmation_status="conflicted" if conflicts else "confirmed",
        current_version=current_version,
        current_content=current_content,
        current_source_ids=current_sources,
        versions=versions,
        unresolved_conflicts=conflicts,
        lifecycle_events=tuple(lifecycle_events),
    )


def _memory_bodies_conflict(left_body: str, right_body: str) -> bool:
    left = _normalized_memory_body(left_body)
    right = _normalized_memory_body(right_body)
    opposing_markers = (
        ("always", "never"),
        ("enabled", "disabled"),
        ("required", "forbidden"),
        ("allow", "forbid"),
        ("increase", "decrease"),
        ("始终", "绝不"),
        ("启用", "禁用"),
        ("允许", "禁止"),
        ("必须", "不得"),
    )
    if any(
        (positive in left and negative in right)
        or (negative in left and positive in right)
        for positive, negative in opposing_markers
    ):
        return True
    for modal in ("must", "should"):
        left_negative = re.search(rf"\b{modal}\s+not\b", left) is not None
        right_negative = re.search(rf"\b{modal}\s+not\b", right) is not None
        left_positive = re.search(rf"\b{modal}\b(?!\s+not\b)", left) is not None
        right_positive = re.search(rf"\b{modal}\b(?!\s+not\b)", right) is not None
        if (left_positive and right_negative) or (left_negative and right_positive):
            return True
    return False


def _normalized_memory_body(content: str) -> str:
    return " ".join(_without_evidence_marker(content).casefold().split())


def _parse_review_instruction(instruction: str) -> _ReviewInstruction:
    normalized = _required_text("review instruction", instruction)
    folded = normalized.casefold()
    evolution_match = re.fullmatch(
        r"(revise|supplement)\s+(mem_[0-9a-f]{64})"
        r"(?:\s+with\s*[:：]\s*(.*?))?\s+because\s*[:：]\s*(.+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if evolution_match is not None:
        action_text, target_memory_id, edited_content, reason_text = (
            evolution_match.groups()
        )
        action: IntegrationAction = (
            "revise" if action_text.casefold() == "revise" else "supplement"
        )
        if action == "supplement" and edited_content is None:
            raise UserInputError(
                "a supplement review must state the complete updated wording"
            )
        content = (
            _required_text("updated canonical understanding", edited_content)
            if edited_content is not None
            else None
        )
        if content is not None and len(content) > 500:
            raise UserInputError(
                "updated canonical understanding must not exceed 500 characters"
            )
        return _ReviewInstruction(
            decision="accepted",
            content=content,
            reason=_required_text("integration reason", reason_text),
            action=action,
            target_memory_id=target_memory_id,
        )
    chinese_evolution_match = re.fullmatch(
        r"(修订|补充)\s*(mem_[0-9a-f]{64})"
        r"(?:\s*(?:改为|完整表述为)\s*[:：]\s*(.*?))?"
        r"\s*(?:因为|原因是)\s*[:：]?\s*(.+)",
        normalized,
    )
    if chinese_evolution_match is not None:
        action_text, target_memory_id, edited_content, reason_text = (
            chinese_evolution_match.groups()
        )
        action = "revise" if action_text == "修订" else "supplement"
        if action == "supplement" and edited_content is None:
            raise UserInputError("补充审阅必须给出完整的新表述")
        return _ReviewInstruction(
            decision="accepted",
            content=(
                _required_text("updated canonical understanding", edited_content)
                if edited_content is not None
                else None
            ),
            reason=_required_text("integration reason", reason_text),
            action=action,
            target_memory_id=target_memory_id,
        )
    conflict_match = re.fullmatch(
        r"preserve\s+conflict\s+with\s+(mem_[0-9a-f]{64})"
        r"\s+because\s*[:：]\s*(.+)",
        normalized,
        flags=re.IGNORECASE,
    ) or re.fullmatch(
        r"(?:保留|并列保留)冲突\s*(?:与|和)\s*(mem_[0-9a-f]{64})"
        r"\s*(?:因为|原因是)\s*[:：]?\s*(.+)",
        normalized,
    )
    if conflict_match is not None:
        return _ReviewInstruction(
            decision="accepted",
            content=None,
            reason=_required_text("conflict reason", conflict_match.group(2)),
            action="conflict",
            target_memory_id=conflict_match.group(1),
        )
    if re.fullmatch(
        r"(?:i\s+)?(?:accept|approve)(?:\s+(?:this|the|it))?"
        r"(?:\s+proposal)?[.!]?",
        folded,
    ) or re.fullmatch(r"(?:我)?(?:接受|同意|批准)(?:这个|该)?提案?[。！]?", folded):
        return _ReviewInstruction(decision="accepted", content=None, reason=None)
    edit_match = re.fullmatch(
        r"(?:edit|accept\s+with\s+changes|"
        r"(?:i\s+)?(?:accept|approve)(?:\s+(?:this|the|it))?"
        r"(?:\s+proposal)?\s+with\s+(?:this\s+)?(?:wording|changes?))"
        r"\s*[:：]\s*(.+)",
        normalized,
        flags=re.IGNORECASE,
    ) or re.fullmatch(
        r"(?:我)?(?:(?:接受|同意|批准)(?:这个|该)?提案?并)?(?:修改为|改为)"
        r"\s*[:：]?\s*(.+)",
        normalized,
    )
    if edit_match is not None:
        content = _required_text(
            "edited canonical understanding",
            edit_match.group(1),
        )
        if len(content) > 500:
            raise UserInputError(
                "edited canonical understanding must not exceed 500 characters"
            )
        return _ReviewInstruction(
            decision="edited",
            content=content,
            reason=None,
        )
    if re.fullmatch(
        r"(?:i\s+)?reject(?:\s+(?:this|the|it))?(?:\s+proposal)?[.!]?",
        folded,
    ) or re.fullmatch(r"(?:我)?拒绝(?:这个|该)?提案?[。！]?", folded):
        return _ReviewInstruction(decision="rejected", content=None, reason=None)
    rejection_match = re.fullmatch(
        r"(?:i\s+)?reject(?:\s+(?:this|the|it))?(?:\s+proposal)?"
        r"\s+because\s*[:：]?\s*(.+)",
        normalized,
        flags=re.IGNORECASE,
    ) or re.fullmatch(
        r"(?:我)?拒绝(?:这个|该)?提案?\s*(?:因为|原因是)?\s*[:：]\s*(.+)",
        normalized,
    )
    if rejection_match is not None:
        reason = _required_text("rejection reason", rejection_match.group(1))
        return _ReviewInstruction(
            decision="rejected",
            content=None,
            reason=reason,
        )
    raise UserInputError(
        "review instruction must naturally accept, edit, or reject the proposal"
    )


def _split_group(value: str | None) -> tuple[str, ...]:
    if value is None or not value:
        return ()
    return tuple(sorted(value.split(",")))


_ConsolidationRow = tuple[str, str, str, Sensitivity]
_CanonicalCandidate = tuple[str, str, Sensitivity, tuple[str, ...]]


def _validated_consolidation_rows(
    rows: list[tuple[object, ...]],
) -> tuple[_ConsolidationRow, ...]:
    validated: list[_ConsolidationRow] = []
    for row in rows:
        if (
            len(row) != 4
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
            or not isinstance(row[2], str)
            or row[3] not in ("local-only", "cloud-allowed")
        ):
            raise IntegrityError("buffered memory has invalid consolidation fields")
        validated.append((row[0], row[1], row[2], row[3]))
    return tuple(validated)


def _validated_canonical_rows(
    rows: list[tuple[object, ...]],
) -> tuple[_CanonicalCandidate, ...]:
    validated: list[_CanonicalCandidate] = []
    for row in rows:
        if (
            len(row) != 4
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
            or row[2] not in ("local-only", "cloud-allowed")
            or (row[3] is not None and not isinstance(row[3], str))
        ):
            raise IntegrityError("canonical memory has invalid consolidation fields")
        validated.append(
            (
                row[0],
                row[1],
                row[2],
                _split_group(row[3]),
            )
        )
    return tuple(validated)


def _proposal_impact(
    related_memory_ids: tuple[str, ...],
    exact_memory_ids: tuple[str, ...],
) -> str:
    if exact_memory_ids:
        return (
            "Adds the buffered sources to existing canonical memory "
            f"{exact_memory_ids[0]} without changing its content."
        )
    if related_memory_ids:
        return (
            "Creates a separate canonical understanding related to "
            f"{', '.join(related_memory_ids)} without revising existing content."
        )
    return (
        "Creates one canonical understanding from the approved buffered evidence; "
        "no semantic change occurs before review."
    )


def _integration_proposal_drafts(
    task: str,
    candidates: tuple[_ConsolidationRow, ...],
    canonical_candidates: tuple[_CanonicalCandidate, ...],
    embedding_provider: EmbeddingProvider,
) -> tuple[_IntegrationProposalDraft, ...]:
    semantic_vectors = _semantic_vectors(
        tuple(row[1] for row in candidates)
        + tuple(row[1] for row in canonical_candidates),
        embedding_provider,
    )
    groups = _group_related_buffered_memory(candidates, semantic_vectors)
    drafts: list[_IntegrationProposalDraft] = []
    for group in groups:
        digest_ids = tuple(sorted(row[0] for row in group))
        bodies = tuple(
            dict.fromkeys(_without_evidence_marker(row[1]) for row in group)
        )
        related = tuple(
            candidate
            for candidate in canonical_candidates
            if any(
                _memory_bodies_are_related(
                    row[1], candidate[1], semantic_vectors
                )
                for row in group
            )
        )
        source_ids = tuple(sorted({row[2] for row in group}))
        proposed_understanding = " ".join(bodies)
        exact_related = tuple(
            candidate
            for candidate in related
            if _normalized_memory_body(candidate[1])
            == _normalized_memory_body(proposed_understanding)
        )
        suggested_action: IntegrationAction = (
            "supplement" if exact_related else "new"
        )
        target_memory_id = exact_related[0][0] if exact_related else None
        sensitivity: Sensitivity = (
            "local-only"
            if any(row[3] == "local-only" for row in group)
            or any(candidate[2] == "local-only" for candidate in related)
            else "cloud-allowed"
        )
        identity = json.dumps(
            {"task": task, "digest_ids": digest_ids},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        topic = task
        if len(groups) > 1:
            topic = f"{task}: {' '.join(bodies[0].split()[:6])}"
        drafts.append(
            _IntegrationProposalDraft(
                proposal_id=f"prp_{hashlib.sha256(identity).hexdigest()}",
                topic=topic,
                proposed_understanding=proposed_understanding,
                possible_impact=_proposal_impact(
                    tuple(candidate[0] for candidate in related),
                    tuple(candidate[0] for candidate in exact_related),
                ),
                sensitivity=sensitivity,
                digest_ids=digest_ids,
                source_ids=source_ids,
                related_memory_ids=tuple(candidate[0] for candidate in related),
                suggested_action=suggested_action,
                target_memory_id=target_memory_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    return tuple(drafts)


def _group_related_buffered_memory(
    candidates: tuple[_ConsolidationRow, ...],
    semantic_vectors: dict[str, tuple[float, ...]],
) -> tuple[tuple[_ConsolidationRow, ...], ...]:
    remaining = list(candidates)
    groups: list[tuple[_ConsolidationRow, ...]] = []
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for candidate in tuple(remaining):
                if any(
                    _memory_bodies_are_related(
                        candidate[1], row[1], semantic_vectors
                    )
                    for row in group
                ):
                    group.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        groups.append(tuple(group))
    return tuple(groups)


def _semantic_vectors(
    bodies: tuple[str, ...],
    provider: EmbeddingProvider,
) -> dict[str, tuple[float, ...]]:
    unique_bodies = tuple(
        dict.fromkeys(_without_evidence_marker(body) for body in bodies)
    )
    if not unique_bodies:
        return {}
    try:
        vectors = validate_embeddings(
            provider.space,
            unique_bodies,
            provider.embed(unique_bodies),
        )
    except EmbeddingFailure:
        return {}
    return dict(zip(unique_bodies, vectors))


def _memory_bodies_are_related(
    left: str,
    right: str,
    semantic_vectors: dict[str, tuple[float, ...]],
) -> bool:
    from myoutbrain.retrieval import lexical_terms

    left_body = _without_evidence_marker(left)
    right_body = _without_evidence_marker(right)
    if " ".join(left_body.casefold().split()) == " ".join(
        right_body.casefold().split()
    ):
        return True
    left_terms = lexical_terms(left_body)
    right_terms = lexical_terms(right_body)
    smaller = min(len(left_terms), len(right_terms))
    overlap = len(left_terms.intersection(right_terms))
    if smaller >= 2 and overlap >= math.ceil(smaller * 0.6):
        return True
    left_vector = semantic_vectors.get(left_body)
    right_vector = semantic_vectors.get(right_body)
    return (
        left_vector is not None
        and right_vector is not None
        and cosine_similarity(left_vector, right_vector)
        >= SEMANTIC_SIMILARITY_THRESHOLD
    )


def _validated_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise UserInputError("occurred-at must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UserInputError("occurred-at must include a UTC offset")
    return parsed.isoformat()


def _deletion_fingerprint(subject_id: str) -> str:
    return "sha256:" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _select_ids_for_values(
    connection: sqlite3.Connection,
    *,
    table: str,
    result_column: str,
    filter_column: str,
    values: tuple[str, ...],
) -> tuple[str, ...]:
    if not values:
        return ()
    placeholders = ", ".join("?" for _ in values)
    return tuple(
        row[0]
        for row in connection.execute(
            f"SELECT {result_column} FROM {table} "
            f"WHERE {filter_column} IN ({placeholders}) ORDER BY {result_column}",
            values,
        ).fetchall()
    )


def _delete_rows_for_ids(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    values: tuple[str, ...],
) -> None:
    if not values:
        return
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
        values,
    )


def _resolved_object_reference(root: Path, object_reference: str) -> Path:
    object_root = (root / "store" / "objects").resolve()
    candidate = (object_root / object_reference).resolve()
    if candidate == object_root or object_root not in candidate.parents:
        raise IntegrityError("source object reference escapes the object store")
    return candidate


def _knowledge_view_paths_for_memory(root: Path, memory_id: str) -> tuple[str, ...]:
    manifest_path = root / "runtime" / "knowledge-views" / "manifest.json"
    if not manifest_path.is_file():
        return ()
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        views = document["views"]
        if not isinstance(views, list):
            raise TypeError
        paths: list[str] = []
        for item in views:
            if not isinstance(item, dict):
                raise TypeError
            item_memory_id = item.get("memory_id")
            item_path = item.get("path")
            if not isinstance(item_memory_id, str) or not isinstance(item_path, str):
                raise TypeError
            if item_memory_id == memory_id:
                paths.append(item_path)
        return tuple(paths)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise IntegrityError(
            f"cannot read knowledge view cleanup scope: {manifest_path}"
        ) from error


def _redacted_event_journal_change(
    root: Path,
    *,
    sensitive_ids: tuple[str, ...],
    deletion_event: dict[str, object],
) -> tuple[Path, bytes]:
    journal_path = root / "store" / "journal" / "events.jsonl"
    retained: list[dict[str, object]] = []
    try:
        if journal_path.is_file():
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise TypeError
                event = {str(key): value for key, value in raw.items()}
                if not _contains_sensitive_id(event, frozenset(sensitive_ids)):
                    retained.append(event)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise IntegrityError(f"cannot redact event journal: {journal_path}") from error
    retained.append(deletion_event)
    return (
        journal_path,
        b"".join(
            json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
            for event in retained
        ),
    )


def _contains_sensitive_id(value: object, sensitive_ids: frozenset[str]) -> bool:
    if isinstance(value, str):
        return value in sensitive_ids
    if isinstance(value, dict):
        return any(_contains_sensitive_id(item, sensitive_ids) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_id(item, sensitive_ids) for item in value)
    return False


def _validate_content_object(path: Path, body: bytes, digest: str) -> None:
    if not path.exists():
        return
    try:
        stored = path.read_bytes()
    except OSError as error:
        raise IntegrityError(f"cannot read source object: {path}") from error
    if hashlib.sha256(stored).hexdigest() != digest or stored != body:
        raise IntegrityError(f"source object does not match its content address: {path}")


def _validated_digest(value: str, body: str, source_id: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise UserInputError("memory digest must not be blank")
    if len(normalized) > 500:
        raise UserInputError("memory digest must not exceed 500 characters")
    normalized_body = " ".join(body.split())
    if normalized_body.casefold() in normalized.casefold():
        raise UserInputError("memory digest must not copy the complete conversation")
    return f"{normalized} [evidence: {source_id}]"
