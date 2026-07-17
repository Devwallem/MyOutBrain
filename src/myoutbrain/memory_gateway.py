from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import sqlite3

from myoutbrain.core_types import IntegrityError, Sensitivity, UserInputError
from myoutbrain.local_core import MEMORY_DATABASE, MEMORY_SCHEMA_VERSION
from myoutbrain.persistence import recover_transactions, writer_lock
from myoutbrain.retrieval import lexical_terms


class MemoryAccess(StrEnum):
    LOCAL_TRUSTED = "local-trusted"
    TASK_SCOPED = "task-scoped"
    PUBLIC_EXTERNAL = "public-external"


class QueryPurpose(StrEnum):
    SUBSTANTIVE = "substantive"
    CASUAL = "casual"
    OPERATION = "operation"


class Answerability(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    NOT_REQUIRED = "not-required"


class MemoryState(StrEnum):
    CANONICAL = "canonical"
    BUFFERED = "buffered"


class RecallMatch(StrEnum):
    STABLE_IDENTITY = "stable-identity"
    SOURCE_RELATION = "source-relation"
    FULL_TEXT = "full-text"


@dataclass(frozen=True)
class RecallRequest:
    query: str
    task: str
    access: MemoryAccess
    purpose: QueryPurpose
    memory_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    limit: int = 5


@dataclass(frozen=True)
class MemoryEvidence:
    memory_id: str
    content: str
    memory_state: MemoryState
    confirmed: bool
    source_ids: tuple[str, ...]
    occurred_at: str
    sensitivity: Sensitivity
    entrance: str | None
    task: str | None
    match: RecallMatch

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "memory_state": self.memory_state.value,
            "confirmed": self.confirmed,
            "source_ids": list(self.source_ids),
            "occurred_at": self.occurred_at,
            "sensitivity": self.sensitivity,
            "entrance": self.entrance,
            "task": self.task,
            "match": self.match.value,
        }


@dataclass(frozen=True)
class MemoryEvidencePackage:
    query: str
    task: str
    access: MemoryAccess
    retrieval_performed: bool
    common_knowledge_queried: bool
    answerability: Answerability
    items: tuple[MemoryEvidence, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "query": self.query,
            "task": self.task,
            "access": self.access.value,
            "retrieval_performed": self.retrieval_performed,
            "common_knowledge_queried": self.common_knowledge_queried,
            "answerability": self.answerability.value,
            "items": [item.to_data() for item in self.items],
        }


@dataclass(frozen=True)
class _Candidate:
    memory_id: str
    content: str
    memory_state: MemoryState
    source_ids: tuple[str, ...]
    occurred_at: str
    sensitivity: Sensitivity
    entrance: str | None
    task: str | None


class MemoryGateway:
    """Return task-scoped memory without exposing persistence details."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def recall(self, request: RecallRequest) -> MemoryEvidencePackage:
        query = request.query.strip()
        task = request.task.strip()
        if not query:
            raise UserInputError("recall query must not be blank")
        if not task:
            raise UserInputError("recall task must not be blank")
        if request.limit < 1 or request.limit > 20:
            raise UserInputError("recall limit must be between 1 and 20")
        if request.purpose is not QueryPurpose.SUBSTANTIVE:
            return MemoryEvidencePackage(
                query=query,
                task=task,
                access=request.access,
                retrieval_performed=False,
                common_knowledge_queried=False,
                answerability=Answerability.NOT_REQUIRED,
                items=(),
            )

        candidates = self._load_candidates()
        requested_memory_ids = frozenset(request.memory_ids)
        requested_source_ids = frozenset(request.source_ids)
        eligible = tuple(
            candidate
            for candidate in candidates
            if _allows(
                candidate,
                request.access,
                task,
                explicitly_requested=(
                    candidate.memory_id in requested_memory_ids
                    or bool(requested_source_ids.intersection(candidate.source_ids))
                ),
            )
        )
        full_text_ids = _full_text_matches(query, eligible, request.limit)
        matched = tuple(
            (candidate, match)
            for candidate in eligible
            if (
                match := _match_for(
                    candidate,
                    requested_memory_ids=requested_memory_ids,
                    requested_source_ids=requested_source_ids,
                    full_text_ids=full_text_ids,
                )
            )
            is not None
        )
        ordered = tuple(
            sorted(
                matched,
                key=lambda pair: (
                    _MATCH_ORDER[pair[1]],
                    pair[0].memory_state is MemoryState.BUFFERED,
                    pair[0].memory_id,
                ),
            )
        )[: request.limit]
        selected = tuple(
            MemoryEvidence(
                memory_id=candidate.memory_id,
                content=candidate.content,
                memory_state=candidate.memory_state,
                confirmed=candidate.memory_state is MemoryState.CANONICAL,
                source_ids=candidate.source_ids,
                occurred_at=candidate.occurred_at,
                sensitivity=candidate.sensitivity,
                entrance=candidate.entrance,
                task=candidate.task,
                match=match,
            )
            for candidate, match in ordered
        )
        query_terms = lexical_terms(query)
        answerability = Answerability.INSUFFICIENT
        if query_terms and any(
            item.memory_state is MemoryState.CANONICAL
            and query_terms.issubset(lexical_terms(item.content))
            for item in selected
        ):
            answerability = Answerability.SUFFICIENT
        return MemoryEvidencePackage(
            query=query,
            task=task,
            access=request.access,
            retrieval_performed=True,
            common_knowledge_queried=True,
            answerability=answerability,
            items=selected,
        )

    def _load_candidates(self) -> tuple[_Candidate, ...]:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise UserInputError(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    version_row = connection.execute("PRAGMA user_version").fetchone()
                    if version_row != (MEMORY_SCHEMA_VERSION,):
                        raise IntegrityError(
                            f"unsupported memory schema version: {database_path}"
                        )
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
            raise IntegrityError("cannot query memory for recall") from error
        buffered = tuple(
            _Candidate(
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
            _Candidate(
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


_MATCH_ORDER = {
    RecallMatch.STABLE_IDENTITY: 0,
    RecallMatch.SOURCE_RELATION: 1,
    RecallMatch.FULL_TEXT: 2,
}


def _allows(
    candidate: _Candidate,
    access: MemoryAccess,
    task: str,
    *,
    explicitly_requested: bool,
) -> bool:
    if access is MemoryAccess.PUBLIC_EXTERNAL and candidate.sensitivity == "local-only":
        return False
    if candidate.memory_state is MemoryState.CANONICAL:
        return True
    if access is MemoryAccess.LOCAL_TRUSTED:
        return True
    return candidate.task == task or explicitly_requested


def _match_for(
    candidate: _Candidate,
    *,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
    full_text_ids: frozenset[str],
) -> RecallMatch | None:
    if candidate.memory_id in requested_memory_ids:
        return RecallMatch.STABLE_IDENTITY
    if requested_source_ids.intersection(candidate.source_ids):
        return RecallMatch.SOURCE_RELATION
    if candidate.memory_id in full_text_ids:
        return RecallMatch.FULL_TEXT
    return None


def _full_text_matches(
    query: str,
    candidates: tuple[_Candidate, ...],
    limit: int,
) -> frozenset[str]:
    terms = tuple(sorted(lexical_terms(query)))
    if not terms or not candidates:
        return frozenset()
    expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                "CREATE VIRTUAL TABLE recall_fts USING fts5(memory_id UNINDEXED, content)"
            )
            connection.executemany(
                "INSERT INTO recall_fts (memory_id, content) VALUES (?, ?)",
                ((candidate.memory_id, candidate.content) for candidate in candidates),
            )
            rows = connection.execute(
                """
                SELECT memory_id
                FROM recall_fts
                WHERE recall_fts MATCH ?
                ORDER BY bm25(recall_fts)
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
    except sqlite3.Error as error:
        raise IntegrityError("local full-text recall failed") from error
    matched_ids = {memory_id for (memory_id,) in rows}
    normalized_query = " ".join(query.casefold().split())
    matched_ids.update(
        candidate.memory_id
        for candidate in candidates
        if normalized_query in " ".join(candidate.content.casefold().split())
    )
    return frozenset(matched_ids)
