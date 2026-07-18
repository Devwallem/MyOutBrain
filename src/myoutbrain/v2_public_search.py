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
    PublicQueryUnavailable,
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


@dataclass(frozen=True)
class SanitizedPublicQuery:
    """A query credential produced by a trusted local sanitizer."""

    value: str

    @classmethod
    def from_trusted_sanitizer(cls, value: str) -> SanitizedPublicQuery:
        return cls(sanitized_public_query("", trusted_query=value))


class PublicQuerySanitizer(Protocol):
    def sanitize(self, question: str) -> SanitizedPublicQuery: ...


class ConfiguredPublicQuerySanitizer:
    def sanitize(self, question: str) -> SanitizedPublicQuery:
        try:
            return SanitizedPublicQuery.from_trusted_sanitizer(
                sanitized_public_query(question)
            )
        except PublicQueryUnavailable as error:
            raise UserInputError(
                "trusted local sanitizer did not produce a public-safe query"
            ) from error


class PublicSearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        time_sensitive: bool,
    ) -> tuple[PublicSource, ...]: ...


class ConfiguredPublicSearchProvider:
    def search(
        self,
        query: str,
        *,
        time_sensitive: bool,
    ) -> tuple[PublicSource, ...]:
        return search_public_sources(query, time_sensitive=time_sensitive)


@dataclass(frozen=True)
class PublicSearchAssessment:
    answerability: CapabilityAnswerability
    verified_facts: tuple[str, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()

    def validate(self) -> None:
        self.answerability.validate()
        for label, values in (
            ("verified fact", self.verified_facts),
            ("unresolved gap", self.unresolved_gaps),
            ("next step", self.next_steps),
        ):
            if any(not value.strip() or len(value) > 2_000 for value in values):
                raise UserInputError(
                    f"public search {label} must contain 1 to 2000 characters"
                )


class PublicSearchAnswerabilityEngine(Protocol):
    def assess(
        self,
        question: str,
        memories: tuple[RecallMaterial, ...],
        public_sources: tuple[PublicSource, ...],
    ) -> PublicSearchAssessment: ...


@dataclass(frozen=True)
class FixedPublicSearchAnswerabilityEngine:
    assessment: PublicSearchAssessment

    def assess(
        self,
        question: str,
        memories: tuple[RecallMaterial, ...],
        public_sources: tuple[PublicSource, ...],
    ) -> PublicSearchAssessment:
        del question, memories, public_sources
        return self.assessment


@dataclass(frozen=True)
class V2PublicSearchRequest:
    recall_id: str
    question: str
    task: str
    allowed_for_task: bool
    time_sensitive: bool


class V2PublicSearchService:
    """Continue one insufficient V2 recall with task-authorized public evidence."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def search(
        self,
        request: V2PublicSearchRequest,
        sanitizer: PublicQuerySanitizer,
        provider: PublicSearchProvider,
        answerability_engine: PublicSearchAnswerabilityEngine,
    ) -> dict[str, object]:
        recall_id = request.recall_id.strip()
        question = " ".join(request.question.strip().split())
        task = request.task.strip()
        if not recall_id.startswith("rec_") or len(recall_id) > 200:
            raise UserInputError("recall id is invalid")
        if not task:
            raise UserInputError("public search task must not be blank")
        if not question or len(question) > 2_000:
            raise UserInputError(
                "public search question must contain 1 to 2000 characters"
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
            raise IntegrityError("cannot continue recall with public search") from error
        _validate_recall_eligibility(
            row,
            task=task,
            allowed_for_task=request.allowed_for_task,
        )
        assert row is not None
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
        public_query = sanitizer.sanitize(question).value
        public_sources = provider.search(
            public_query,
            time_sensitive=request.time_sensitive,
        )
        assessment = answerability_engine.assess(
            question,
            memories,
            public_sources,
        )
        assessment.validate()
        answerability = assessment.answerability
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
        elif unresolved_internal_conflict or public_sources_conflict(public_sources):
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
                    _validate_recall_eligibility(
                        current,
                        task=task,
                        allowed_for_task=request.allowed_for_task,
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
            raise IntegrityError("cannot continue recall with public search") from error
        has_internal_evidence = bool(memories)
        has_public_evidence = bool(public_sources)
        if has_internal_evidence and has_public_evidence:
            declaration_kind = "mixed"
            declaration_label = "综合你的 MyOutBrain 知识库与公开信息"
        elif has_public_evidence:
            declaration_kind = "public"
            declaration_label = "根据当前任务检索到的公开信息"
        elif has_internal_evidence:
            declaration_kind = "myoutbrain"
            declaration_label = "根据你的 MyOutBrain 知识库；公开检索未找到可用证据"
        else:
            declaration_kind = "none"
            declaration_label = "本地知识与公开检索均未找到可用证据"
        unknown = not answerability.answerable
        verified_facts: list[str] = []
        unresolved_gaps: list[str] = []
        next_steps: list[str] = []
        if unknown:
            fallback_gap, fallback_step = _answerability_presentation(
                answerability.reason
            )
            verified_facts = list(assessment.verified_facts)
            unresolved_gaps = list(assessment.unresolved_gaps) or [fallback_gap]
            next_steps = list(assessment.next_steps) or [fallback_step]
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
            "public_search": {
                "performed": True,
                "query": public_query,
                "sources": [
                    {**source.to_data(), "state": "external-unintegrated"}
                    for source in public_sources
                ],
            },
            "verified_facts": verified_facts,
            "unresolved_gaps": unresolved_gaps,
            "next_steps": next_steps,
        }


def _answerability_presentation(reason: str) -> tuple[str, str]:
    return {
        "coverage-insufficient": (
            "Public evidence does not cover the requested conclusion.",
            "Seek an authoritative source covering the missing scope.",
        ),
        "freshness-insufficient": (
            "Available evidence does not meet the required freshness.",
            "Verify the point with a current authoritative source.",
        ),
        "missing-dependency": (
            "A necessary supporting dependency is still missing.",
            "Obtain and verify the missing supporting evidence.",
        ),
        "unresolved-conflict": (
            "The available evidence remains materially conflicted.",
            "Resolve the conflicting claims with authoritative evidence.",
        ),
    }.get(
        reason,
        (
            "The available evidence is insufficient.",
            "Verify the unresolved point with an authoritative source.",
        ),
    )


def _validate_recall_eligibility(
    row: tuple[object, ...] | None,
    *,
    task: str,
    allowed_for_task: bool,
) -> None:
    if row is None:
        raise UserInputError("recall id does not exist")
    if cast(str, row[0]) != task or not allowed_for_task:
        raise UserInputError("public search requires current task authorization")
    if bool(row[1]):
        raise UserInputError(
            "public search is allowed only after internal answerability fails"
        )
