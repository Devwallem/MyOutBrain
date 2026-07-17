from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import re
import sqlite3
import tempfile
from typing import Literal
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
    recover_transactions,
    writer_lock,
)


MEMORY_SCHEMA_VERSION = 3
MEMORY_DATABASE = "store/memory.sqlite3"


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE source_objects (
    source_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    object_reference TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
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

CREATE TABLE canonical_memory_relations (
    memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    related_memory_id TEXT NOT NULL REFERENCES canonical_memories(memory_id),
    relationship TEXT NOT NULL CHECK (relationship = 'related'),
    created_at TEXT NOT NULL,
    CHECK (memory_id <> related_memory_id),
    PRIMARY KEY (memory_id, related_memory_id)
);

CREATE TABLE integration_proposals (
    proposal_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    proposed_understanding TEXT NOT NULL,
    possible_impact TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
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

CREATE TABLE integration_reviews (
    review_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES integration_proposals(proposal_id),
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'edited', 'rejected')),
    reviewed_content TEXT,
    reason TEXT,
    canonical_memory_id TEXT REFERENCES canonical_memories(memory_id),
    created_at TEXT NOT NULL
);

CREATE TABLE memory_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


MemoryDisposition = Literal["buffered", "duplicate"]


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

    @property
    def confirmed(self) -> bool:
        return self.memory_state is MemoryState.CANONICAL


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
            "status": self.status,
        }


@dataclass(frozen=True)
class IntegrationReviewResult:
    proposal_id: str
    decision: str
    canonical_memory_id: str | None
    canonical_content: str | None
    reason: str | None
    related_canonical_memory_ids: tuple[str, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "decision": self.decision,
            "canonical_memory_id": self.canonical_memory_id,
            "canonical_content": self.canonical_content,
            "reason": self.reason,
            "related_canonical_memory_ids": list(
                self.related_canonical_memory_ids
            ),
        }


@dataclass(frozen=True)
class _ReviewInstruction:
    decision: Literal["accepted", "edited", "rejected"]
    content: str | None
    reason: str | None


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
            status="pending",
        )


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
            if database_path.exists():
                version = self._database_version(database_path)
                if version == 1:
                    migrated = self._migrate_v1_database(database_path)
                    atomic_commit(self._root, [(database_path, migrated)])
                    version = 2
                if version == 2:
                    migrated = self._migrate_v2_database(database_path)
                    atomic_commit(self._root, [(database_path, migrated)])
                self._validate_database(database_path)
                return
            database_content = self._new_database_content(database_path.parent)
            atomic_commit(self._root, [(database_path, database_content)])

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
                                         FROM canonical_memory_sources AS private_source
                                         JOIN experiences AS private_experience
                                           ON private_experience.source_id = private_source.source_id
                                         WHERE private_source.memory_id = c.memory_id
                                           AND private_experience.sensitivity = 'local-only'
                                     ) THEN 'local-only'
                                   ELSE 'cloud-allowed'
                               END AS effective_sensitivity,
                               GROUP_CONCAT(source.source_id, ',') AS source_ids
                        FROM canonical_memories AS c
                        LEFT JOIN canonical_memory_sources AS source
                          ON source.memory_id = c.memory_id
                        WHERE c.state = 'active'
                        GROUP BY c.memory_id, c.content, c.updated_at, c.sensitivity
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
            )
            for memory_id, content, updated_at, sensitivity, source_ids in canonical_rows
        )
        return canonical + buffered

    def propose_manual_consolidation(
        self,
        task: str,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> tuple[IntegrationProposal, ...]:
        normalized_task = _required_text("consolidation task", task)
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
                        ORDER BY d.created_at, d.digest_id
                        """,
                        (normalized_task,),
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
            canonical_content = (
                proposal.proposed_understanding
                if review.decision == "accepted"
                else review.content
            )
            exact_duplicate_id = self._exact_duplicate_canonical_id(
                database_path,
                proposal=proposal,
                canonical_content=canonical_content,
            )
            canonical_memory_id = None
            if review.decision != "rejected":
                canonical_memory_id = exact_duplicate_id or (
                    f"mem_{hashlib.sha256(proposal.proposal_id.encode()).hexdigest()}"
                )
            reviewed_at = datetime.now(timezone.utc).isoformat()
            staged_database = self._database_with_integration_review(
                database_path,
                proposal=proposal,
                review=review,
                canonical_memory_id=canonical_memory_id,
                canonical_content=canonical_content,
                reviewed_at=reviewed_at,
            )
            event_id = f"evt_{uuid.uuid4().hex}"
            event = {
                "id": event_id,
                "type": f"integration.{review.decision}",
                "occurred_at": reviewed_at,
                "proposal_id": proposal.proposal_id,
                "decision": review.decision,
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
                               review.reason,
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
                related_canonical_memory_ids=_split_group(row[5]),
            )
            for row in rows
        )

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

    @staticmethod
    def _exact_duplicate_canonical_id(
        database_path: Path,
        *,
        proposal: IntegrationProposal,
        canonical_content: str | None,
    ) -> str | None:
        if canonical_content is None or not proposal.related_canonical_memory_ids:
            return None
        placeholders = ", ".join("?" for _ in proposal.related_canonical_memory_ids)
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    f"""
                    SELECT memory_id, content
                    FROM canonical_memories
                    WHERE state = 'active' AND memory_id IN ({placeholders})
                    ORDER BY memory_id
                    """,
                    proposal.related_canonical_memory_ids,
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot classify an integration proposal") from error
        normalized_content = _normalized_memory_body(canonical_content)
        for memory_id, content in rows:
            if (
                isinstance(memory_id, str)
                and isinstance(content, str)
                and _normalized_memory_body(content) == normalized_content
            ):
                return memory_id
        return None

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
                           GROUP_CONCAT(DISTINCT related.memory_id)
                    FROM integration_proposals AS p
                    LEFT JOIN integration_proposal_buffered AS buffered
                      ON buffered.proposal_id = p.proposal_id
                    LEFT JOIN integration_proposal_sources AS source
                      ON source.proposal_id = p.proposal_id
                    LEFT JOIN integration_proposal_related AS related
                      ON related.proposal_id = p.proposal_id
                    WHERE p.status = ?{topic_filter}
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
                             possible_impact, sensitivity, status, created_at)
                        VALUES (?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            draft.proposal_id,
                            draft.topic,
                            draft.proposed_understanding,
                            draft.possible_impact,
                            draft.sensitivity,
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
                        SELECT 1 FROM canonical_memories
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
                        connection.executemany(
                            """
                            INSERT INTO canonical_memory_relations
                                (memory_id, related_memory_id, relationship, created_at)
                            VALUES (?, ?, 'related', ?)
                            """,
                            (
                                (canonical_memory_id, related_memory_id, reviewed_at)
                                for related_memory_id
                                in proposal.related_canonical_memory_ids
                                if related_memory_id != canonical_memory_id
                            ),
                        )
                    elif proposal.sensitivity == "local-only":
                        connection.execute(
                            """
                            UPDATE canonical_memories
                            SET sensitivity = 'local-only', updated_at = ?
                            WHERE memory_id = ?
                            """,
                            (reviewed_at, canonical_memory_id),
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
                        (review_id, proposal_id, decision, reviewed_content,
                         reason, canonical_memory_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        proposal.proposal_id,
                        review.decision,
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
                connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
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
    if not conversation_path.is_file():
        raise UserInputError(f"conversation does not exist: {conversation_path}")
    try:
        body = conversation_path.read_bytes()
        text = body.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UserInputError(
            f"conversation is not readable UTF-8: {conversation_path}"
        ) from error
    if not text.strip():
        raise UserInputError("conversation must not be blank")
    return body


def _required_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserInputError(f"{name} must not be blank")
    return normalized


def _without_evidence_marker(content: str) -> str:
    return re.sub(r"\s*\[evidence:\s+src_[0-9a-f]+\]\s*$", "", content).strip()


def _normalized_memory_body(content: str) -> str:
    return " ".join(_without_evidence_marker(content).casefold().split())


def _parse_review_instruction(instruction: str) -> _ReviewInstruction:
    normalized = _required_text("review instruction", instruction)
    folded = normalized.casefold()
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
