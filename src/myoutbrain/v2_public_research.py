from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Protocol, cast

from myoutbrain.core_types import IntegrityError, UserInputError
from myoutbrain.local_core import LocalMemoryCore, MEMORY_DATABASE
from myoutbrain.persistence import recover_transactions, writer_lock
from myoutbrain.public_search import (
    PublicSource,
    public_sources_conflict,
    sanitized_public_query,
    search_public_sources,
)
from myoutbrain.v2_recall import (
    CapabilityAnswerability,
    PROTOCOL_VERSION,
    RecallMaterial,
)


class PublicResearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        time_sensitive: bool,
    ) -> tuple[PublicSource, ...]: ...


class ConfiguredPublicResearchProvider:
    def search(
        self,
        query: str,
        *,
        time_sensitive: bool,
    ) -> tuple[PublicSource, ...]:
        return search_public_sources(query, time_sensitive=time_sensitive)


class PublicAnswerabilityEngine(Protocol):
    def assess(
        self,
        question: str,
        memories: tuple[RecallMaterial, ...],
        public_sources: tuple[PublicSource, ...],
    ) -> CapabilityAnswerability: ...


@dataclass(frozen=True)
class FixedPublicAnswerabilityEngine:
    assessment: CapabilityAnswerability

    def assess(
        self,
        question: str,
        memories: tuple[RecallMaterial, ...],
        public_sources: tuple[PublicSource, ...],
    ) -> CapabilityAnswerability:
        del question, memories, public_sources
        return self.assessment


@dataclass(frozen=True)
class V2PublicResearchRequest:
    recall_id: str
    question: str
    task: str
    public_query: str
    allowed_for_task: bool
    time_sensitive: bool


class V2PublicResearchService:
    """Continue one insufficient V2 recall with task-authorized public evidence."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def research(
        self,
        request: V2PublicResearchRequest,
        provider: PublicResearchProvider,
        answerability_engine: PublicAnswerabilityEngine,
    ) -> dict[str, object]:
        recall_id = request.recall_id.strip()
        question = " ".join(request.question.strip().split())
        task = request.task.strip()
        if not recall_id.startswith("rec_") or len(recall_id) > 200:
            raise UserInputError("recall id is invalid")
        if not task:
            raise UserInputError("public research task must not be blank")
        if not question or len(question) > 2_000:
            raise UserInputError(
                "public research question must contain 1 to 2000 characters"
            )
        LocalMemoryCore(self._root).inspect_schema_version()
        try:
            with closing(sqlite3.connect(self._root / MEMORY_DATABASE)) as connection:
                row = connection.execute(
                    """
                    SELECT task, answerable, unresolved_conflict
                    FROM recall_events
                    WHERE recall_id = ?
                    """,
                    (recall_id,),
                ).fetchone()
                material_rows = connection.execute(
                    """
                    SELECT item.memory_id, item.version, item.state,
                           version.content, version.applicability_scope,
                           EXISTS (
                               SELECT 1
                               FROM canonical_memory_version_evidence AS evidence
                               WHERE evidence.memory_id = item.memory_id
                                 AND evidence.version = item.version
                           )
                    FROM recall_event_items AS item
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = item.memory_id
                     AND version.version = item.version
                    WHERE item.recall_id = ?
                    ORDER BY item.memory_id
                    """,
                    (recall_id,),
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot continue recall with public research") from error
        if row is None:
            raise UserInputError("recall id does not exist")
        if cast(str, row[0]) != task or not request.allowed_for_task:
            raise UserInputError("public research requires current task authorization")
        if bool(row[1]):
            raise UserInputError(
                "public research is allowed only after internal answerability fails"
            )
        unresolved_internal_conflict = bool(row[2])
        memories = tuple(
            RecallMaterial(
                memory_id=cast(str, material[0]),
                version=cast(int, material[1]),
                state=cast(str, material[2]),
                body=cast(str, material[3]),
                scope=cast(str, material[4]),
                has_evidence=bool(material[5]),
                has_unresolved_conflict=unresolved_internal_conflict,
            )
            for material in material_rows
        )
        public_query = sanitized_public_query(
            "",
            trusted_query=request.public_query,
        )
        public_sources = provider.search(
            public_query,
            time_sensitive=request.time_sensitive,
        )
        answerability = answerability_engine.assess(
            question,
            memories,
            public_sources,
        )
        answerability.validate()
        overridden = False
        if not public_sources:
            overridden = (
                answerability.answerable
                or answerability.reason != "coverage-insufficient"
            )
            answerability = CapabilityAnswerability(
                answerable=False,
                reason="coverage-insufficient",
            )
        elif public_sources_conflict(public_sources):
            overridden = (
                answerability.answerable
                or answerability.reason != "unresolved-conflict"
            )
            answerability = CapabilityAnswerability(
                answerable=False,
                reason="unresolved-conflict",
            )
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(
                    sqlite3.connect(self._root / MEMORY_DATABASE)
                ) as connection:
                    current = connection.execute(
                        "SELECT task, answerable FROM recall_events WHERE recall_id = ?",
                        (recall_id,),
                    ).fetchone()
                    if current is None:
                        raise UserInputError("recall id does not exist")
                    if cast(str, current[0]) != task:
                        raise UserInputError(
                            "public research requires current task authorization"
                        )
                    if bool(current[1]):
                        raise UserInputError(
                            "public research is allowed only after internal "
                            "answerability fails"
                        )
                    connection.execute(
                        """
                        UPDATE recall_events
                        SET answerable = ?, answerability_reason = ?,
                            answerability_overridden = ?
                        WHERE recall_id = ?
                        """,
                        (
                            int(answerability.answerable),
                            answerability.reason,
                            int(overridden),
                            recall_id,
                        ),
                    )
                    connection.commit()
        except sqlite3.Error as error:
            raise IntegrityError("cannot continue recall with public research") from error
        has_internal_evidence = bool(memories)
        declaration_kind = "mixed" if has_internal_evidence else "public"
        declaration_label = (
            "综合你的 MyOutBrain 知识库与公开信息"
            if has_internal_evidence
            else "根据当前任务检索到的公开信息"
        )
        unknown = not answerability.answerable
        return {
            "protocol_version": PROTOCOL_VERSION,
            "recall_id": recall_id,
            "status": "unknown" if unknown else "answered",
            "answerability": {
                "answerable": answerability.answerable,
                "reason": answerability.reason,
                "overridden_by_core": overridden,
            },
            "source_declaration": {
                "kind": declaration_kind,
                "label": declaration_label,
                "evidence_disclosure": "on-request",
            },
            "public_research": {
                "performed": True,
                "query": public_query,
                "sources": [
                    {**source.to_data(), "state": "external-unintegrated"}
                    for source in public_sources
                ],
            },
            "verified_facts": (
                [source.content for source in public_sources] if unknown else []
            ),
            "unresolved_gaps": (
                [_unresolved_gap(answerability.reason)] if unknown else []
            ),
            "next_steps": (
                [_next_step(answerability.reason)] if unknown else []
            ),
        }


def _unresolved_gap(reason: str) -> str:
    return {
        "coverage-insufficient": "Public evidence does not cover the requested conclusion.",
        "freshness-insufficient": "Available evidence does not meet the required freshness.",
        "missing-dependency": "A necessary supporting dependency is still missing.",
        "unresolved-conflict": "The available evidence remains materially conflicted.",
    }.get(reason, "The available evidence is insufficient.")


def _next_step(reason: str) -> str:
    return {
        "coverage-insufficient": "Seek an authoritative source covering the missing scope.",
        "freshness-insufficient": "Verify the point with a current authoritative source.",
        "missing-dependency": "Obtain and verify the missing supporting evidence.",
        "unresolved-conflict": "Resolve the conflicting claims with authoritative evidence.",
    }.get(reason, "Verify the unresolved point with an authoritative source.")
