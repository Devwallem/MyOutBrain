from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path
import sqlite3
import tomllib
from typing import Protocol, cast

from myoutbrain.core_types import (
    IntegrityError,
    MemoryState as MemoryState,
    Sensitivity,
    UserInputError,
)
from myoutbrain.embeddings import (
    EmbeddingFailure,
    EmbeddingLocation,
    EmbeddingProvider,
    LocalMultilingualEmbeddingProvider,
)
from myoutbrain.local_core import LocalMemoryCore, RecallableMemory
from myoutbrain.retrieval import lexical_terms
from myoutbrain.semantic_index import SemanticRecallIndex


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


class RecallMatch(StrEnum):
    STABLE_IDENTITY = "stable-identity"
    SOURCE_RELATION = "source-relation"
    FULL_TEXT = "full-text"
    SEMANTIC_CANDIDATE = "semantic-candidate"


@dataclass(frozen=True)
class RecallRequest:
    query: str
    task: str
    access: MemoryAccess
    purpose: QueryPurpose
    memory_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    limit: int = 5
    query_sensitivity: Sensitivity = "local-only"


class MemoryReader(Protocol):
    def recallable_memories(self) -> tuple[RecallableMemory, ...]: ...


@dataclass(frozen=True)
class MemoryEvidence(RecallableMemory):
    match: RecallMatch

    @classmethod
    def from_memory(
        cls,
        memory: RecallableMemory,
        match: RecallMatch,
    ) -> MemoryEvidence:
        return cls(
            memory_id=memory.memory_id,
            content=memory.content,
            memory_state=memory.memory_state,
            source_ids=memory.source_ids,
            occurred_at=memory.occurred_at,
            sensitivity=memory.sensitivity,
            entrance=memory.entrance,
            task=memory.task,
            match=match,
        )

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


class MemoryGateway:
    """Return task-scoped memory without exposing persistence details."""

    def __init__(
        self,
        root: Path,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        memory_reader: MemoryReader | None = None,
    ) -> None:
        self._root = root
        self._embedding_provider = (
            embedding_provider or LocalMultilingualEmbeddingProvider()
        )
        self._memory_reader = memory_reader or LocalMemoryCore(root)

    def recall(self, request: RecallRequest) -> MemoryEvidencePackage:
        query = request.query.strip()
        task = request.task.strip()
        if not query:
            raise UserInputError("recall query must not be blank")
        if not task:
            raise UserInputError("recall task must not be blank")
        if request.limit < 1 or request.limit > 20:
            raise UserInputError("recall limit must be between 1 and 20")
        if request.query_sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError("recall query sensitivity is invalid")
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

        memories = self._memory_reader.recallable_memories()
        requested_memory_ids = frozenset(request.memory_ids)
        requested_source_ids = frozenset(request.source_ids)
        canonical = _eligible_for_phase(
            memories,
            state=MemoryState.CANONICAL,
            access=request.access,
            task=task,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        buffered = _eligible_for_phase(
            memories,
            state=MemoryState.BUFFERED,
            access=request.access,
            task=task,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        canonical_matches = _match_phase(
            query,
            canonical,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        buffered_matches = _match_phase(
            query,
            buffered,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        semantic_scores = self._semantic_scores(
            query,
            canonical + buffered,
            query_sensitivity=request.query_sensitivity,
        )
        canonical_matches = _with_semantic_matches(
            canonical,
            canonical_matches,
            semantic_scores,
        )
        buffered_matches = _with_semantic_matches(
            buffered,
            buffered_matches,
            semantic_scores,
        )
        matched = canonical_matches + buffered_matches
        ordered = tuple(
            sorted(
                matched,
                key=lambda pair: (
                    _MATCH_ORDER[pair[1]],
                    pair[0].memory_state is MemoryState.BUFFERED,
                    -pair[2],
                    pair[0].memory_id,
                ),
            )
        )[: request.limit]
        selected = tuple(
            MemoryEvidence.from_memory(memory, match)
            for memory, match, _score in ordered
        )
        return MemoryEvidencePackage(
            query=query,
            task=task,
            access=request.access,
            retrieval_performed=True,
            common_knowledge_queried=True,
            answerability=Answerability.INSUFFICIENT,
            items=selected,
        )

    def _semantic_scores(
        self,
        query: str,
        memories: tuple[RecallableMemory, ...],
        *,
        query_sensitivity: Sensitivity,
    ) -> dict[str, float]:
        provider = self._embedding_provider
        try:
            eligible = memories
            if provider.location is EmbeddingLocation.CLOUD:
                cloud_text_limit = _cloud_embedding_text_limit(self._root, provider)
                if query_sensitivity != "cloud-allowed" or cloud_text_limit is None:
                    return {}
                eligible = tuple(
                    memory
                    for memory in memories
                    if memory.sensitivity == "cloud-allowed"
                )[: cloud_text_limit - 1]
            return SemanticRecallIndex(self._root).scores(query, eligible, provider)
        except (
            EmbeddingFailure,
            OSError,
            RuntimeError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            return {}

_MATCH_ORDER = {
    RecallMatch.STABLE_IDENTITY: 0,
    RecallMatch.SOURCE_RELATION: 1,
    RecallMatch.FULL_TEXT: 2,
    RecallMatch.SEMANTIC_CANDIDATE: 3,
}


def _with_semantic_matches(
    memories: tuple[RecallableMemory, ...],
    existing: tuple[tuple[RecallableMemory, RecallMatch, float], ...],
    semantic_scores: Mapping[str, float],
) -> tuple[tuple[RecallableMemory, RecallMatch, float], ...]:
    matched_ids = {memory.memory_id for memory, _match, _score in existing}
    semantic = tuple(
        (memory, RecallMatch.SEMANTIC_CANDIDATE, semantic_scores[memory.memory_id])
        for memory in memories
        if memory.memory_id in semantic_scores and memory.memory_id not in matched_ids
    )
    return existing + semantic


def _cloud_embedding_text_limit(
    root: Path,
    provider: EmbeddingProvider,
) -> int | None:
    try:
        with (root / "myoutbrain.toml").open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
        embedding = configuration.get("embedding")
        if not isinstance(embedding, dict):
            return None
        authorized = (
            embedding.get("allow_cloud") is True
            and embedding.get("provider") == provider.space.provider
            and embedding.get("model") == provider.space.model
            and embedding.get("dimensions") == provider.space.dimensions
            and embedding.get("normalization_version")
            == provider.space.normalization_version
            and embedding.get("cloud_send_scope") == "cloud-allowed-only"
            and isinstance(embedding.get("cloud_budget_usd"), (int, float))
            and not isinstance(embedding.get("cloud_budget_usd"), bool)
            and cast(float, embedding.get("cloud_budget_usd")) > 0
            and isinstance(embedding.get("cloud_max_texts_per_request"), int)
            and not isinstance(embedding.get("cloud_max_texts_per_request"), bool)
            and cast(int, embedding.get("cloud_max_texts_per_request")) >= 2
        )
        if not authorized:
            return None
        return cast(int, embedding.get("cloud_max_texts_per_request"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _eligible_for_phase(
    memories: tuple[RecallableMemory, ...],
    *,
    state: MemoryState,
    access: MemoryAccess,
    task: str,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
) -> tuple[RecallableMemory, ...]:
    return tuple(
        memory
        for memory in memories
        if memory.memory_state is state
        and _allows(
            memory,
            access,
            task,
            explicitly_requested=(
                memory.memory_id in requested_memory_ids
                or bool(requested_source_ids.intersection(memory.source_ids))
            ),
        )
    )


def _allows(
    memory: RecallableMemory,
    access: MemoryAccess,
    task: str,
    *,
    explicitly_requested: bool,
) -> bool:
    if access is MemoryAccess.PUBLIC_EXTERNAL and memory.sensitivity == "local-only":
        return False
    if memory.memory_state is MemoryState.CANONICAL:
        return True
    if access is MemoryAccess.LOCAL_TRUSTED:
        return True
    return memory.task == task or explicitly_requested


def _match_phase(
    query: str,
    memories: tuple[RecallableMemory, ...],
    *,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
) -> tuple[tuple[RecallableMemory, RecallMatch, float], ...]:
    full_text_scores = _full_text_matches(query, memories)
    matches: list[tuple[RecallableMemory, RecallMatch, float]] = []
    for memory in memories:
        match = _match_for(
            memory,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
            full_text_scores=full_text_scores,
        )
        if match is not None:
            matches.append(
                (memory, match, float(full_text_scores.get(memory.memory_id, 0)))
            )
    return tuple(matches)


def _match_for(
    memory: RecallableMemory,
    *,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
    full_text_scores: Mapping[str, int],
) -> RecallMatch | None:
    if memory.memory_id in requested_memory_ids:
        return RecallMatch.STABLE_IDENTITY
    if requested_source_ids.intersection(memory.source_ids):
        return RecallMatch.SOURCE_RELATION
    if memory.memory_id in full_text_scores:
        return RecallMatch.FULL_TEXT
    return None


def _full_text_matches(
    query: str,
    memories: tuple[RecallableMemory, ...],
) -> dict[str, int]:
    query_terms = lexical_terms(query)
    terms = tuple(sorted(query_terms))
    if not terms or not memories:
        return {}
    expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                "CREATE VIRTUAL TABLE recall_fts USING fts5(memory_id UNINDEXED, content)"
            )
            connection.executemany(
                "INSERT INTO recall_fts (memory_id, content) VALUES (?, ?)",
                ((memory.memory_id, memory.content) for memory in memories),
            )
            rows = connection.execute(
                """
                SELECT memory_id
                FROM recall_fts
                WHERE recall_fts MATCH ?
                ORDER BY bm25(recall_fts)
                """,
                (expression,),
            ).fetchall()
    except sqlite3.Error as error:
        raise IntegrityError("local full-text recall failed") from error
    fts_ids = {memory_id for (memory_id,) in rows}
    normalized_query = " ".join(query.casefold().split())
    required_overlap = max(1, math.ceil(len(query_terms) * 0.6))
    scores: dict[str, int] = {}
    for memory in memories:
        normalized_content = " ".join(memory.content.casefold().split())
        exact_phrase = normalized_query in normalized_content
        overlap = len(query_terms.intersection(lexical_terms(memory.content)))
        if (
            memory.memory_id in fts_ids or exact_phrase
        ) and (exact_phrase or overlap >= required_overlap):
            scores[memory.memory_id] = overlap + (
                len(query_terms) if exact_phrase else 0
            )
    return scores
