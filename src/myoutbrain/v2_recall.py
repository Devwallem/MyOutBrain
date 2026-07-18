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
from myoutbrain.retrieval import lexical_terms


DEFAULT_RECALL_BUDGET_BYTES = 16 * 1024
MINIMUM_RECALL_BUDGET_BYTES = 1024
MAXIMUM_RECALL_BUDGET_BYTES = 64 * 1024
PROTOCOL_VERSION = {"major": 2, "minor": 0}
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
                    event_exists = connection.execute(
                        "SELECT 1 FROM recall_events WHERE recall_id = ?",
                        (normalized_recall_id,),
                    ).fetchone()
                    if event_exists is None:
                        raise UserInputError("recall id does not exist")
                    selected_ids = tuple(cast(str, row[0]) for row in selected_rows)
                    unresolved_conflict = _has_unresolved_conflict(
                        connection,
                        selected_ids,
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
                               source.observed_at, source.applicability_scope
                        FROM canonical_memory_version_evidence AS evidence
                        JOIN evidence_source_versions AS source
                          ON source.source_id = evidence.source_id
                         AND source.version = evidence.source_version
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
        FROM knowledge_dictionary
        ORDER BY memory_id
        """
    ).fetchall()
    exact_names: list[str] = []
    for memory_id, normalized_name in dictionary_rows:
        if question == memory_id or cast(str, normalized_name) in normalized_question:
            paths.setdefault(cast(str, memory_id), set()).add("dictionary")
            exact_names.append(cast(str, normalized_name))

    terms = lexical_terms(question)
    partition_rows = connection.execute(
        """
        SELECT partition.partition_id, partition.normalized_topic,
               capsule.capsule_id
        FROM knowledge_partitions AS partition
        JOIN capsule_partitions AS capsule
          ON capsule.partition_id = partition.partition_id
        WHERE partition.node_kind = 'leaf'
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
    return paths, routed_capsules, len(exact_names) > 1


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
                   version.applicability_scope, dictionary.primary_capsule_id
            FROM knowledge_dictionary AS dictionary
            JOIN canonical_memories AS memory
              ON memory.memory_id = dictionary.memory_id
            JOIN canonical_memory_versions AS version
              ON version.memory_id = dictionary.memory_id
             AND version.version = dictionary.current_version
            WHERE dictionary.memory_id = ? AND memory.state = 'active'
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
                state="current",
                canonical_name=cast(str, row[3]),
                body=cast(str, row[4]),
                scope=cast(str, row[5]),
                capsule_id=cast(str, row[6]),
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
    return row is not None


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
