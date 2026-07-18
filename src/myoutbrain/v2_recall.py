from __future__ import annotations

from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, Protocol, cast
import uuid

from myoutbrain.core_types import IntegrityError, UserInputError
from myoutbrain.local_core import LocalMemoryCore, MEMORY_DATABASE
from myoutbrain.persistence import recover_transactions, writer_lock
from myoutbrain.protocol_contract import SERVER_PROTOCOL_VERSION
from myoutbrain.retrieval import lexical_terms


DEFAULT_RECALL_BUDGET_BYTES = 16 * 1024
MINIMUM_RECALL_BUDGET_BYTES = 1024
MAXIMUM_RECALL_BUDGET_BYTES = 64 * 1024
PROTOCOL_VERSION = SERVER_PROTOCOL_VERSION
RECALL_PATHS = ("dictionary", "partition-tree", "local-fts", "global-fts")

AnswerabilityReason = Literal[
    "covered",
    "coverage-insufficient",
    "freshness-insufficient",
    "missing-dependency",
    "unresolved-conflict",
]


@dataclass(frozen=True)
class CapabilityAnswerability:
    answerable: bool
    reason: AnswerabilityReason

    def validate(self) -> None:
        if self.reason not in {
            "covered",
            "coverage-insufficient",
            "freshness-insufficient",
            "missing-dependency",
            "unresolved-conflict",
        }:
            raise UserInputError("answerability reason is invalid")
        if self.answerable != (self.reason == "covered"):
            raise UserInputError(
                "answerable=true requires reason covered; "
                "answerable=false requires an insufficiency reason"
            )


@dataclass(frozen=True)
class V2RecallRequest:
    question: str
    task: str
    entrance: str
    budget_bytes: int = DEFAULT_RECALL_BUDGET_BYTES


@dataclass(frozen=True)
class RecallMaterial:
    memory_id: str
    version: int
    state: str
    body: str
    scope: str
    has_evidence: bool
    has_unresolved_conflict: bool


@dataclass(frozen=True)
class RecalledCounterevidenceTarget:
    task: str
    memory_id: str
    version: int
    state: str
    body: str
    scope: str
    canonical_name: str


class AnswerabilityEngine(Protocol):
    def assess(
        self,
        question: str,
        memories: tuple[RecallMaterial, ...],
    ) -> CapabilityAnswerability: ...


@dataclass(frozen=True)
class FixedAnswerabilityEngine:
    """Deterministic CLI/test adapter for a capability engine response."""

    assessment: CapabilityAnswerability

    def assess(
        self,
        question: str,
        memories: tuple[RecallMaterial, ...],
    ) -> CapabilityAnswerability:
        del question, memories
        return self.assessment


@dataclass(frozen=True)
class _Candidate:
    memory_id: str
    version: int
    state: str
    canonical_name: str
    body: str
    scope: str
    capsule_id: str
    partition_id: str
    partition_summary: str
    candidate_paths: tuple[str, ...]
    evidence: tuple[dict[str, object], ...]

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))

    @property
    def payload_bytes(self) -> int:
        return len(
            json.dumps(
                self.to_data(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "version": self.version,
            "state": self.state,
            "name": self.canonical_name,
            "body": self.body,
            "body_bytes": self.body_bytes,
            "scope": self.scope,
            "partition": {
                "partition_id": self.partition_id,
                "summary": self.partition_summary,
            },
            "candidate_paths": list(self.candidate_paths),
            "evidence": {
                "status": "available" if self.evidence else "missing",
                "source_count": len(self.evidence),
                "references": list(self.evidence),
            },
        }


class V2RecallService:
    """Recall V2 canonical memory without exposing SQLite to an entrance."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def recall(
        self,
        request: V2RecallRequest,
        answerability_engine: AnswerabilityEngine,
    ) -> dict[str, object]:
        question = _required_text("recall question", request.question)
        task = _stable_identifier("recall task", request.task, maximum=128)
        entrance = _stable_identifier("recall entrance", request.entrance, maximum=64)
        if not MINIMUM_RECALL_BUDGET_BYTES <= request.budget_bytes <= MAXIMUM_RECALL_BUDGET_BYTES:
            raise UserInputError(
                "recall budget must be between 1024 and 65536 bytes"
            )
        LocalMemoryCore(self._root).inspect_schema_version()
        database_path = self._root / MEMORY_DATABASE
        recall_id = f"rec_{uuid.uuid4().hex}"
        occurred_at = datetime.now(timezone.utc).isoformat()
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    candidate_paths, routed_capsules, ambiguity = _candidate_paths(
                        connection,
                        question,
                    )
                    candidates = _load_candidates(connection, candidate_paths)
                    selected, truncated = _within_budget(
                        candidates,
                        request.budget_bytes,
                        reserved_bytes=_recall_package_overhead_bytes(
                            recall_id,
                            request.budget_bytes,
                        ),
                    )
                    selected_ids = tuple(candidate.memory_id for candidate in selected)
                    unresolved_conflict = _has_unresolved_conflict(
                        connection,
                        selected_ids,
                        task=task,
                    )
                    capability_answerability = answerability_engine.assess(
                        question,
                        tuple(
                            RecallMaterial(
                                memory_id=candidate.memory_id,
                                version=candidate.version,
                                state=candidate.state,
                                body=candidate.body,
                                scope=candidate.scope,
                                has_evidence=bool(candidate.evidence),
                                has_unresolved_conflict=unresolved_conflict,
                            )
                            for candidate in selected
                        ),
                    )
                    capability_answerability.validate()
                    answerable, reason, overridden = _enforce_answerability(
                        capability_answerability,
                        has_memories=bool(selected),
                        unresolved_conflict=unresolved_conflict,
                    )
                    cross_partition_hit = any(
                        "global-fts" in candidate.candidate_paths
                        and candidate.capsule_id not in routed_capsules
                        for candidate in selected
                    )
                    answerability: dict[str, object] = {
                        "answerable": answerable,
                        "reason": reason,
                        "overridden_by_core": overridden,
                    }
                    package = _recall_package(
                        recall_id=recall_id,
                        limit_bytes=request.budget_bytes,
                        truncated=truncated,
                        answerability=answerability,
                        selected=selected,
                        cross_partition_hit=cross_partition_hit,
                        ambiguity=ambiguity,
                        unresolved_conflict=unresolved_conflict,
                    )
                    used_bytes = _measure_recall_package(package)
                    if used_bytes > request.budget_bytes:
                        raise IntegrityError(
                            "recall package exceeded its byte budget"
                        )
                    connection.execute(
                        """
                        INSERT INTO recall_events
                            (recall_id, occurred_at, entrance, task, paths_json,
                             budget_limit_bytes, used_bytes, was_truncated,
                             answerable, answerability_reason,
                             answerability_overridden, cross_partition_hit,
                             ambiguity_detected, missing_dependency,
                             unresolved_conflict)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                        """,
                        (
                            recall_id,
                            occurred_at,
                            entrance,
                            task,
                            json.dumps(RECALL_PATHS, separators=(",", ":")),
                            request.budget_bytes,
                            used_bytes,
                            int(truncated),
                            int(answerable),
                            reason,
                            int(overridden),
                            int(cross_partition_hit),
                            int(ambiguity),
                            int(unresolved_conflict),
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO recall_event_items
                            (recall_id, memory_id, version, state,
                             candidate_paths_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                recall_id,
                                candidate.memory_id,
                                candidate.version,
                                candidate.state,
                                json.dumps(
                                    candidate.candidate_paths,
                                    separators=(",", ":"),
                                ),
                            )
                            for candidate in selected
                        ),
                    )
                    connection.commit()
                    return package
        except sqlite3.Error as error:
            raise IntegrityError("cannot recall V2 canonical memory") from error

    def assess_answerability(
        self,
        recall_id: str,
        capability_answerability: CapabilityAnswerability,
    ) -> dict[str, object]:
        normalized_recall_id = _required_identifier("recall id", recall_id, "rec_")
        capability_answerability.validate()
        LocalMemoryCore(self._root).inspect_schema_version()
        database_path = self._root / MEMORY_DATABASE
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    selected_rows = connection.execute(
                        "SELECT memory_id FROM recall_event_items WHERE recall_id = ?",
                        (normalized_recall_id,),
                    ).fetchall()
                    event_row = connection.execute(
                        "SELECT task FROM recall_events WHERE recall_id = ?",
                        (normalized_recall_id,),
                    ).fetchone()
                    if event_row is None:
                        raise UserInputError("recall id does not exist")
                    selected_ids = tuple(cast(str, row[0]) for row in selected_rows)
                    unresolved_conflict = _has_unresolved_conflict(
                        connection,
                        selected_ids,
                        task=cast(str, event_row[0]),
                    )
                    answerable, reason, overridden = _enforce_answerability(
                        capability_answerability,
                        has_memories=bool(selected_ids),
                        unresolved_conflict=unresolved_conflict,
                    )
                    connection.execute(
                        """
                        UPDATE recall_events
                        SET answerable = ?, answerability_reason = ?,
                            answerability_overridden = ?, unresolved_conflict = ?
                        WHERE recall_id = ?
                        """,
                        (
                            int(answerable),
                            reason,
                            int(overridden),
                            int(unresolved_conflict),
                            normalized_recall_id,
                        ),
                    )
                    connection.commit()
        except sqlite3.Error as error:
            raise IntegrityError("cannot record recall answerability") from error
        return {
            "protocol_version": PROTOCOL_VERSION,
            "recall_id": normalized_recall_id,
            "answerability": {
                "answerable": answerable,
                "reason": reason,
                "overridden_by_core": overridden,
            },
        }

    def counterevidence_target(
        self,
        recall_id: str,
        memory_id: str,
    ) -> RecalledCounterevidenceTarget:
        normalized_recall_id = _required_identifier("recall id", recall_id, "rec_")
        normalized_memory_id = _required_identifier("memory id", memory_id, "mem_")
        LocalMemoryCore(self._root).inspect_schema_version()
        try:
            with closing(sqlite3.connect(self._root / MEMORY_DATABASE)) as connection:
                row = connection.execute(
                    """
                    SELECT event.task, item.version, memory.state,
                           version.content, version.applicability_scope,
                           dictionary.canonical_name
                    FROM recall_events AS event
                    JOIN recall_event_items AS item
                      ON item.recall_id = event.recall_id
                    JOIN canonical_memories AS memory
                      ON memory.memory_id = item.memory_id
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = item.memory_id
                     AND version.version = item.version
                    JOIN knowledge_dictionary AS dictionary
                      ON dictionary.memory_id = item.memory_id
                    WHERE event.recall_id = ? AND item.memory_id = ?
                    """,
                    (normalized_recall_id, normalized_memory_id),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot inspect counterevidence recall") from error
        if row is None:
            raise UserInputError(
                "counterevidence target must be selected by the specified recall"
            )
        if not (
            isinstance(row[0], str)
            and isinstance(row[1], int)
            and all(isinstance(row[index], str) for index in range(2, 6))
        ):
            raise IntegrityError("counterevidence recall target is malformed")
        return RecalledCounterevidenceTarget(
            task=row[0],
            memory_id=normalized_memory_id,
            version=row[1],
            state=row[2],
            body=row[3],
            scope=row[4],
            canonical_name=row[5],
        )

    def expand_evidence(
        self,
        recall_id: str,
        memory_id: str,
        *,
        evidence_reference_ids: tuple[str, ...],
        budget_bytes: int,
    ) -> dict[str, object]:
        normalized_recall_id = _required_identifier("recall id", recall_id, "rec_")
        normalized_memory_id = _required_identifier("memory id", memory_id, "mem_")
        normalized_reference_ids = tuple(
            _required_identifier("evidence reference", reference_id, "evr_")
            for reference_id in evidence_reference_ids
        )
        if not normalized_reference_ids:
            raise UserInputError("at least one evidence reference is required")
        if len(set(normalized_reference_ids)) != len(normalized_reference_ids):
            raise UserInputError("evidence references must be unique")
        if not 1 <= budget_bytes <= MAXIMUM_RECALL_BUDGET_BYTES:
            raise UserInputError(
                "evidence expansion budget must be between 1 and 65536 bytes"
            )
        LocalMemoryCore(self._root).inspect_schema_version()
        database_path = self._root / MEMORY_DATABASE
        evidence_items: list[dict[str, object]] = []
        used_bytes = 0
        truncated = False
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    recalled = connection.execute(
                        """
                        SELECT version
                        FROM recall_event_items
                        WHERE recall_id = ? AND memory_id = ?
                        """,
                        (normalized_recall_id, normalized_memory_id),
                    ).fetchone()
                    if recalled is None:
                        raise UserInputError(
                            "memory was not selected by the specified recall"
                        )
                    memory_version = cast(int, recalled[0])
                    rows = connection.execute(
                        """
                        SELECT evidence.source_id, evidence.source_version,
                               source.retention, source.content_hash, source.locator,
                               source.observed_at, source.applicability_scope,
                               registry.source_kind
                        FROM canonical_memory_version_evidence AS evidence
                        JOIN evidence_source_versions AS source
                          ON source.source_id = evidence.source_id
                         AND source.version = evidence.source_version
                        JOIN evidence_sources AS registry
                          ON registry.source_id = source.source_id
                        WHERE evidence.memory_id = ? AND evidence.version = ?
                        ORDER BY evidence.source_id, evidence.source_version
                        """,
                        (normalized_memory_id, memory_version),
                    ).fetchall()
                    available_references = {
                        _evidence_reference_id(
                            normalized_memory_id,
                            memory_version,
                            cast(str, row[0]),
                            cast(int, row[1]),
                        ): row
                        for row in rows
                    }
                    unknown_references = set(normalized_reference_ids).difference(
                        available_references
                    )
                    if unknown_references:
                        raise UserInputError(
                            "evidence reference does not belong to the recalled memory"
                        )
                    for reference_id in normalized_reference_ids:
                        row = available_references[reference_id]
                        source_id = cast(str, row[0])
                        source_version = cast(int, row[1])
                        locator = cast(str, row[4])
                        remaining = budget_bytes - used_bytes
                        if row[7] == "public":
                            excerpt, source_truncated, status = "", False, "receipt-only"
                        else:
                            excerpt, source_truncated, status = _read_evidence_excerpt(
                                Path(locator),
                                cast(str, row[3]),
                                remaining,
                            )
                        excerpt_bytes = len(excerpt.encode("utf-8"))
                        used_bytes += excerpt_bytes
                        truncated = truncated or source_truncated
                        evidence_items.append(
                            {
                                "reference_id": reference_id,
                                "memory_id": normalized_memory_id,
                                "memory_version": memory_version,
                                "source_id": source_id,
                                "source_version": source_version,
                                "retention": row[2],
                                "content_hash": row[3],
                                "locator": locator,
                                "observed_at": row[5],
                                "scope": row[6],
                                "status": status,
                                "excerpt": excerpt,
                            }
                        )
                        connection.execute(
                            """
                            INSERT INTO recall_evidence_expansions
                                (recall_id, memory_id, source_id, source_version,
                                 expanded_bytes, was_truncated)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (
                                recall_id, memory_id, source_id, source_version
                            ) DO UPDATE SET
                                expanded_bytes = excluded.expanded_bytes,
                                was_truncated = excluded.was_truncated
                            """,
                            (
                                normalized_recall_id,
                                normalized_memory_id,
                                source_id,
                                source_version,
                                excerpt_bytes,
                                int(source_truncated),
                            ),
                        )
                    connection.commit()
        except sqlite3.Error as error:
            raise IntegrityError("cannot expand recall evidence") from error
        return {
            "protocol_version": PROTOCOL_VERSION,
            "recall_id": normalized_recall_id,
            "budget": {
                "limit_bytes": budget_bytes,
                "used_bytes": used_bytes,
                "truncated": truncated,
            },
            "evidence": evidence_items,
        }

    def activity(self) -> dict[str, object]:
        LocalMemoryCore(self._root).inspect_schema_version()
        database_path = self._root / MEMORY_DATABASE
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                events = connection.execute(
                    """
                    SELECT recall_id, occurred_at, entrance, task, paths_json,
                           budget_limit_bytes, used_bytes, was_truncated,
                           answerable, answerability_reason,
                           answerability_overridden, cross_partition_hit,
                           ambiguity_detected, missing_dependency,
                           unresolved_conflict
                    FROM recall_events
                    ORDER BY occurred_at DESC, recall_id DESC
                    """
                ).fetchall()
                result: list[dict[str, object]] = []
                for row in events:
                    items = connection.execute(
                        """
                        SELECT memory_id, version, state, candidate_paths_json
                        FROM recall_event_items
                        WHERE recall_id = ?
                        ORDER BY memory_id
                        """,
                        (row[0],),
                    ).fetchall()
                    result.append(
                        {
                            "recall_id": row[0],
                            "occurred_at": row[1],
                            "entrance": row[2],
                            "task": row[3],
                            "paths": json.loads(row[4]),
                            "selected_memories": [
                                {
                                    "memory_id": item[0],
                                    "version": item[1],
                                    "state": item[2],
                                    "candidate_paths": json.loads(item[3]),
                                }
                                for item in items
                            ],
                            "budget": {
                                "limit_bytes": row[5],
                                "used_bytes": row[6],
                                "truncated": bool(row[7]),
                            },
                            "answerability": {
                                "answerable": bool(row[8]),
                                "reason": row[9],
                                "overridden_by_core": bool(row[10]),
                            },
                            "evidence_expanded": _event_has_expansion(
                                connection,
                                cast(str, row[0]),
                            ),
                            "signals": {
                                "cross_partition_hit": bool(row[11]),
                                "ambiguity": bool(row[12]),
                                "missing_dependency": bool(row[13]),
                                "unresolved_conflict": bool(row[14]),
                            },
                        }
                    )
        except sqlite3.Error as error:
            raise IntegrityError("cannot read recall activity") from error
        return {"protocol_version": PROTOCOL_VERSION, "events": result}


def _candidate_paths(
    connection: sqlite3.Connection,
    question: str,
) -> tuple[dict[str, set[str]], frozenset[str], bool]:
    normalized_question = " ".join(question.casefold().split())
    paths: dict[str, set[str]] = {}
    dictionary_rows = connection.execute(
        """
        SELECT memory_id, normalized_name
        FROM memory_names
        ORDER BY normalized_name, memory_id
        """
    ).fetchall()
    matched_name_targets: dict[str, set[str]] = {}
    for memory_id, normalized_name in dictionary_rows:
        normalized_memory_id = cast(str, memory_id)
        normalized_dictionary_name = cast(str, normalized_name)
        if (
            question == normalized_memory_id
            or normalized_dictionary_name in normalized_question
        ):
            paths.setdefault(normalized_memory_id, set()).add("dictionary")
            if normalized_dictionary_name in normalized_question:
                matched_name_targets.setdefault(
                    normalized_dictionary_name,
                    set(),
                ).add(normalized_memory_id)

    terms = lexical_terms(question)
    partition_rows = connection.execute(
        """
        SELECT partition.partition_id, partition.normalized_topic,
               capsule.capsule_id
        FROM knowledge_partitions AS partition
        JOIN capsule_partitions AS capsule
          ON capsule.partition_id = partition.partition_id
        JOIN knowledge_capsules AS capsule_record
          ON capsule_record.capsule_id = capsule.capsule_id
        WHERE partition.node_kind = 'leaf'
          AND capsule_record.status = 'active'
        ORDER BY partition.partition_id
        """
    ).fetchall()
    ranked_partitions = sorted(
        (
            (len(terms.intersection(lexical_terms(cast(str, row[1])))), cast(str, row[2]))
            for row in partition_rows
        ),
        key=lambda item: (-item[0], item[1]),
    )
    positive_capsules = tuple(
        capsule_id for score, capsule_id in ranked_partitions if score > 0
    )[:3]
    routed_capsules = frozenset(
        positive_capsules
        or tuple(capsule_id for _score, capsule_id in ranked_partitions[:1])
    )
    expression = _fts_expression(terms)
    if expression is not None and routed_capsules:
        placeholders = ", ".join("?" for _ in routed_capsules)
        local_rows = connection.execute(
            f"""
            SELECT memory_id
            FROM canonical_memory_fts
            WHERE canonical_memory_fts MATCH ?
              AND capsule_id IN ({placeholders})
            ORDER BY bm25(canonical_memory_fts), memory_id
            LIMIT 8
            """,
            (expression, *sorted(routed_capsules)),
        ).fetchall()
        for (memory_id,) in local_rows:
            paths.setdefault(cast(str, memory_id), set()).update(
                ("partition-tree", "local-fts")
            )
        global_rows = connection.execute(
            """
            SELECT memory_id
            FROM canonical_memory_fts
            WHERE canonical_memory_fts MATCH ?
            ORDER BY bm25(canonical_memory_fts), memory_id
            LIMIT 8
            """,
            (expression,),
        ).fetchall()
        for (memory_id,) in global_rows:
            paths.setdefault(cast(str, memory_id), set()).add("global-fts")
    ambiguity = any(len(memory_ids) > 1 for memory_ids in matched_name_targets.values())
    return paths, routed_capsules, ambiguity


def _load_candidates(
    connection: sqlite3.Connection,
    paths: dict[str, set[str]],
) -> tuple[_Candidate, ...]:
    if not paths:
        return ()
    candidates: list[_Candidate] = []
    for memory_id, memory_paths in paths.items():
        row = connection.execute(
            """
            SELECT dictionary.memory_id, dictionary.current_version,
                    memory.state, dictionary.canonical_name, version.content,
                    version.applicability_scope, dictionary.primary_capsule_id,
                    partition.partition_id, partition.topic
            FROM knowledge_dictionary AS dictionary
            JOIN canonical_memories AS memory
              ON memory.memory_id = dictionary.memory_id
            JOIN canonical_memory_versions AS version
               ON version.memory_id = dictionary.memory_id
              AND version.version = dictionary.current_version
            JOIN capsule_partitions AS capsule_partition
              ON capsule_partition.capsule_id = dictionary.primary_capsule_id
            JOIN knowledge_partitions AS partition
              ON partition.partition_id = capsule_partition.partition_id
            WHERE dictionary.memory_id = ?
              AND memory.state IN ('current', 'historical-trusted')
            """,
            (memory_id,),
        ).fetchone()
        if row is None:
            continue
        evidence_rows = connection.execute(
            """
            SELECT evidence.source_id, evidence.source_version,
                   source.retention, source.content_hash
            FROM canonical_memory_version_evidence AS evidence
            JOIN evidence_source_versions AS source
              ON source.source_id = evidence.source_id
             AND source.version = evidence.source_version
            WHERE evidence.memory_id = ? AND evidence.version = ?
            ORDER BY evidence.source_id, evidence.source_version
            """,
            (row[0], row[1]),
        ).fetchall()
        evidence = tuple(
            {
                "reference_id": _evidence_reference_id(
                    cast(str, row[0]),
                    cast(int, row[1]),
                    cast(str, evidence_row[0]),
                    cast(int, evidence_row[1]),
                ),
                "source_id": evidence_row[0],
                "source_version": evidence_row[1],
                "role": "supports",
                "retention": evidence_row[2],
                "content_hash": evidence_row[3],
            }
            for evidence_row in evidence_rows
        )
        candidates.append(
            _Candidate(
                memory_id=cast(str, row[0]),
                version=cast(int, row[1]),
                state=cast(str, row[2]),
                canonical_name=cast(str, row[3]),
                body=cast(str, row[4]),
                scope=cast(str, row[5]),
                capsule_id=cast(str, row[6]),
                partition_id=cast(str, row[7]),
                partition_summary=cast(str, row[8]),
                candidate_paths=tuple(
                    path for path in RECALL_PATHS if path in memory_paths
                ),
                evidence=evidence,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                min(RECALL_PATHS.index(path) for path in candidate.candidate_paths),
                candidate.memory_id,
            ),
        )
    )


def _recall_package(
    *,
    recall_id: str,
    limit_bytes: int,
    truncated: bool,
    answerability: dict[str, object],
    selected: tuple[_Candidate, ...],
    cross_partition_hit: bool,
    ambiguity: bool,
    unresolved_conflict: bool,
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "recall_id": recall_id,
        "paths_attempted": list(RECALL_PATHS),
        "budget": {
            "limit_bytes": limit_bytes,
            "used_bytes": 0,
            "truncated": truncated,
        },
        "answerability": answerability,
        "source_declaration": {
            "kind": "myoutbrain" if selected else "none",
            "label": (
                "根据你的 MyOutBrain 知识库" if selected else "未找到可用的本地知识"
            ),
            "evidence_disclosure": "on-request",
        },
        "memories": [candidate.to_data() for candidate in selected],
        "signals": {
            "cross_partition_hit": cross_partition_hit,
            "ambiguity": ambiguity,
            "missing_dependency": False,
            "unresolved_conflict": unresolved_conflict,
        },
    }


def _recall_package_overhead_bytes(recall_id: str, limit_bytes: int) -> int:
    worst_case_shell = {
        "protocol_version": PROTOCOL_VERSION,
        "recall_id": recall_id,
        "paths_attempted": list(RECALL_PATHS),
        "budget": {
            "limit_bytes": limit_bytes,
            "used_bytes": limit_bytes,
            "truncated": False,
        },
        "answerability": {
            "answerable": False,
            "reason": "freshness-insufficient",
            "overridden_by_core": False,
        },
        "source_declaration": {
            "kind": "myoutbrain",
            "label": "根据你的 MyOutBrain 知识库",
            "evidence_disclosure": "on-request",
        },
        "memories": [],
        "signals": {
            "cross_partition_hit": False,
            "ambiguity": False,
            "missing_dependency": False,
            "unresolved_conflict": False,
        },
    }
    return _serialized_bytes(worst_case_shell)


def _measure_recall_package(package: dict[str, object]) -> int:
    budget = package.get("budget")
    if not isinstance(budget, dict):
        raise IntegrityError("recall package budget is malformed")
    previous = -1
    while True:
        measured = _serialized_bytes(package)
        if measured == previous:
            return measured
        budget["used_bytes"] = measured
        previous = measured


def _serialized_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _within_budget(
    candidates: Iterable[_Candidate],
    limit_bytes: int,
    *,
    reserved_bytes: int,
) -> tuple[tuple[_Candidate, ...], bool]:
    selected: list[_Candidate] = []
    used_bytes = reserved_bytes
    truncated = False
    for candidate in candidates:
        separator_bytes = 1 if selected else 0
        if used_bytes + separator_bytes + candidate.payload_bytes > limit_bytes:
            truncated = True
            continue
        selected.append(candidate)
        used_bytes += separator_bytes + candidate.payload_bytes
    return tuple(selected), truncated


def _has_unresolved_conflict(
    connection: sqlite3.Connection,
    selected_ids: tuple[str, ...],
    *,
    task: str,
) -> bool:
    if not selected_ids:
        return False
    placeholders = ", ".join("?" for _ in selected_ids)
    row = connection.execute(
        f"""
        SELECT 1
        FROM canonical_memory_conflicts
        WHERE status = 'unresolved'
          AND (first_memory_id IN ({placeholders})
               OR second_memory_id IN ({placeholders}))
        LIMIT 1
        """,
        (*selected_ids, *selected_ids),
    ).fetchone()
    if row is not None:
        return True
    proposal_rows = connection.execute(
        """
        SELECT target_json, supporting_evidence_json, context_coverage_json
        FROM review_proposals
        WHERE status IN ('pending', 'deferred') AND intent = 'integrate'
        """
    ).fetchall()
    selected = set(selected_ids)
    try:
        for target_json, evidence_json, coverage_json in proposal_rows:
            target = json.loads(cast(str, target_json))
            evidence = json.loads(cast(str, evidence_json))
            coverage = json.loads(cast(str, coverage_json))
            if (
                isinstance(target, dict)
                and target.get("memory_id") in selected
                and isinstance(evidence, list)
                and isinstance(coverage, list)
                and f"task:{task}" in coverage
                and any(
                    isinstance(item, dict)
                    and item.get("relationship") == "contradicts"
                    for item in evidence
                )
            ):
                return True
    except (json.JSONDecodeError, TypeError) as error:
        raise IntegrityError("pending counterevidence proposal is malformed") from error
    return False


def _enforce_answerability(
    capability: CapabilityAnswerability,
    *,
    has_memories: bool,
    unresolved_conflict: bool,
) -> tuple[bool, AnswerabilityReason, bool]:
    if unresolved_conflict:
        return False, "unresolved-conflict", (
            capability.answerable or capability.reason != "unresolved-conflict"
        )
    if not has_memories:
        return False, "coverage-insufficient", (
            capability.answerable or capability.reason != "coverage-insufficient"
        )
    return capability.answerable, capability.reason, False


def _fts_expression(terms: frozenset[str]) -> str | None:
    if not terms:
        return None
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in sorted(terms)
    )


def _evidence_reference_id(
    memory_id: str,
    version: int,
    source_id: str,
    source_version: int,
) -> str:
    value = f"{memory_id}:{version}:{source_id}:{source_version}".encode("utf-8")
    return f"evr_{hashlib.sha256(value).hexdigest()[:32]}"


def _event_has_expansion(connection: sqlite3.Connection, recall_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM recall_evidence_expansions WHERE recall_id = ? LIMIT 1",
        (recall_id,),
    ).fetchone() is not None


RECALL_REGRESSION_CATEGORIES = (
    "name-collision",
    "old-alias",
    "cross-partition-fts",
    "historical-trusted",
    "counterevidence",
    "dependency",
)


def fixed_recall_regression_cases(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    """Freeze the public-recall questions that guard one structure switch."""
    collisions = connection.execute(
        """
        SELECT MIN(name)
        FROM memory_names
        GROUP BY normalized_name
        HAVING COUNT(DISTINCT memory_id) > 1
        ORDER BY normalized_name
        """
    ).fetchall()
    aliases = connection.execute(
        """
        SELECT name FROM memory_names
        WHERE name_kind = 'alias'
        ORDER BY normalized_name, memory_id
        """
    ).fetchall()
    all_live = connection.execute(
        """
        SELECT version.content
        FROM knowledge_dictionary AS dictionary
        JOIN canonical_memories AS memory
          ON memory.memory_id = dictionary.memory_id
        JOIN canonical_memory_versions AS version
          ON version.memory_id = dictionary.memory_id
         AND version.version = dictionary.current_version
        WHERE memory.state IN ('current', 'historical-trusted')
        ORDER BY dictionary.memory_id
        """
    ).fetchall()
    topic_rows = connection.execute(
        """
        SELECT DISTINCT normalized_topic
        FROM knowledge_partitions
        WHERE node_kind = 'leaf'
        ORDER BY normalized_topic
        """
    ).fetchall()
    historical = connection.execute(
        """
        SELECT dictionary.canonical_name
        FROM knowledge_dictionary AS dictionary
        JOIN canonical_memories AS memory
          ON memory.memory_id = dictionary.memory_id
        WHERE memory.state = 'historical-trusted'
        ORDER BY dictionary.memory_id
        """
    ).fetchall()
    counterevidence = connection.execute(
        """
        SELECT dictionary.canonical_name
        FROM canonical_memory_conflicts AS conflict
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = conflict.first_memory_id
          OR dictionary.memory_id = conflict.second_memory_id
        WHERE conflict.status = 'unresolved'
        UNION
        SELECT dictionary.canonical_name
        FROM integration_proposals AS proposal
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = proposal.target_memory_id
        WHERE proposal.suggested_action = 'conflict'
          AND proposal.status = 'pending'
        ORDER BY 1
        """
    ).fetchall()
    dependencies = connection.execute(
        """
        SELECT dictionary.canonical_name
        FROM canonical_memory_dependencies AS dependency
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = dependency.memory_id
        UNION
        SELECT dictionary.canonical_name
        FROM canonical_memory_dependencies AS dependency
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = dependency.depends_on_memory_id
        ORDER BY 1
        """
    ).fetchall()
    return {
        "name-collision": _unique_text_rows(collisions),
        "old-alias": _unique_text_rows(aliases),
        "cross-partition-fts": _cross_partition_regression_queries(
            connection,
            _unique_text_rows(all_live),
            _unique_text_rows(topic_rows),
        ),
        "historical-trusted": _unique_text_rows(historical),
        "counterevidence": _unique_text_rows(counterevidence),
        "dependency": _unique_text_rows(dependencies),
    }


def evaluate_fixed_recall_regression(
    connection: sqlite3.Connection,
    cases: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    categories: dict[str, object] = {}
    for category in RECALL_REGRESSION_CATEGORIES:
        categories[category] = [
            {
                "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "signature": _recall_regression_signature(connection, query),
            }
            for query in cases.get(category, ())
        ]
    return {"categories": categories}


def _recall_regression_signature(
    connection: sqlite3.Connection,
    question: str,
) -> dict[str, object]:
    paths, routed_capsules, ambiguity = _candidate_paths(connection, question)
    candidates = _load_candidates(connection, paths)
    selected, _truncated = _within_budget(
        candidates,
        MAXIMUM_RECALL_BUDGET_BYTES,
        reserved_bytes=0,
    )
    selected_ids = tuple(candidate.memory_id for candidate in selected)
    conflicts: list[list[object]] = []
    dependencies: list[list[object]] = []
    if selected_ids:
        placeholders = ", ".join("?" for _ in selected_ids)
        conflicts = [
            list(row)
            for row in connection.execute(
                f"""
                SELECT first_memory_id, second_memory_id, status
                FROM canonical_memory_conflicts
                WHERE first_memory_id IN ({placeholders})
                   OR second_memory_id IN ({placeholders})
                ORDER BY first_memory_id, second_memory_id
                """,
                (*selected_ids, *selected_ids),
            ).fetchall()
        ]
        dependencies = [
            list(row)
            for row in connection.execute(
                f"""
                SELECT memory_id, version, depends_on_memory_id,
                       depends_on_version, relationship
                FROM canonical_memory_dependencies
                WHERE memory_id IN ({placeholders})
                   OR depends_on_memory_id IN ({placeholders})
                ORDER BY memory_id, depends_on_memory_id, relationship
                """,
                (*selected_ids, *selected_ids),
            ).fetchall()
        ]
    return {
        "memories": [
            {
                "memory_id": candidate.memory_id,
                "version": candidate.version,
                "state": candidate.state,
                "body_hash": hashlib.sha256(
                    candidate.body.encode("utf-8")
                ).hexdigest(),
                "evidence": list(candidate.evidence),
                "candidate_paths": sorted(candidate.candidate_paths),
            }
            for candidate in sorted(selected, key=lambda item: item.memory_id)
        ],
        "ambiguity": ambiguity,
        "cross_partition_hit": any(
            "global-fts" in candidate.candidate_paths
            and candidate.capsule_id not in routed_capsules
            for candidate in selected
        ),
        "global_fts_memory_ids": sorted(
            candidate.memory_id
            for candidate in selected
            if "global-fts" in candidate.candidate_paths
        ),
        "unresolved_conflict": _has_unresolved_conflict(connection, selected_ids),
        "conflicts": conflicts,
        "dependencies": dependencies,
    }


def _cross_partition_regression_queries(
    connection: sqlite3.Connection,
    bodies: tuple[str, ...],
    topics: tuple[str, ...],
) -> tuple[str, ...]:
    topic_prefix = " ".join(topics)
    candidates = tuple(
        dict.fromkeys(
            (*bodies, *(f"{topic_prefix} {body}" for body in bodies))
        )
    )
    actual_cross_partition = tuple(
        query
        for query in candidates
        if cast(
            bool,
            _recall_regression_signature(connection, query)[
                "cross_partition_hit"
            ],
        )
    )
    return actual_cross_partition or bodies


def _unique_text_rows(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if len(row) != 1 or not isinstance(row[0], str):
            raise IntegrityError("recall regression source is invalid")
        if row[0] not in seen:
            seen.add(row[0])
            values.append(row[0])
    return tuple(values)


def _required_text(label: str, value: str) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise UserInputError(f"{label} must not be blank")
    if len(normalized) > 2_000:
        raise UserInputError(f"{label} must not exceed 2000 characters")
    return normalized


def _required_identifier(label: str, value: str, prefix: str) -> str:
    normalized = value.strip()
    if not normalized.startswith(prefix) or len(normalized) > 200:
        raise UserInputError(f"{label} is invalid")
    return normalized


def _stable_identifier(label: str, value: str, *, maximum: int) -> str:
    normalized = value.strip()
    if (
        len(normalized) > maximum
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized) is None
    ):
        raise UserInputError(
            f"{label} must be a stable identifier using letters, digits, '.', '_', ':', or '-'"
        )
    return normalized


def _read_evidence_excerpt(
    path: Path,
    expected_content_hash: str,
    budget_bytes: int,
) -> tuple[str, bool, str]:
    try:
        content = path.read_bytes()
    except OSError:
        return "", False, "unavailable"
    actual_content_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual_content_hash != expected_content_hash:
        return "", False, "content-changed"
    prefix = content[:budget_bytes]
    while prefix:
        try:
            excerpt = prefix.decode("utf-8")
            break
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    else:
        excerpt = ""
    return excerpt, len(prefix) < len(content), "available"
