from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
from typing import cast

from myoutbrain.library import UserInputError
from myoutbrain.retrieval import (
    EvidenceKind,
    EvidenceRetriever,
    EvidenceState,
    LexicalNoEmbeddingsRetriever,
    RetrievalEvidence,
)


class RecallCategory(StrEnum):
    ANSWERABLE = "answerable"
    UNANSWERABLE = "unanswerable"
    CONFLICTING = "conflicting"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class RecallCase:
    case_id: str
    category: RecallCategory
    question: str
    answerable: bool
    expected_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RecallDataset:
    evidence: tuple[RetrievalEvidence, ...]
    cases: tuple[RecallCase, ...]


@dataclass(frozen=True)
class RecallCaseResult:
    case_id: str
    category: RecallCategory
    answerable: bool
    retrieved_evidence_ids: tuple[str, ...]
    correct_hits: tuple[str, ...]
    key_omissions: tuple[str, ...]
    incorrect_citations: tuple[str, ...]
    should_refuse_violation: bool

    @property
    def passed(self) -> bool:
        return not (
            self.key_omissions
            or self.incorrect_citations
            or self.should_refuse_violation
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.case_id,
            "category": self.category.value,
            "answerable": self.answerable,
            "retrieved_evidence_ids": list(self.retrieved_evidence_ids),
            "correct_hits": list(self.correct_hits),
            "key_omissions": list(self.key_omissions),
            "incorrect_citations": list(self.incorrect_citations),
            "should_refuse_violation": self.should_refuse_violation,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class RecallSummary:
    case_count: int
    passed: int
    failed: int
    correct_hits: int
    key_omissions: int
    incorrect_citations: int
    should_refuse_violations: int

    def as_dict(self) -> dict[str, int]:
        return {
            "case_count": self.case_count,
            "passed": self.passed,
            "failed": self.failed,
            "correct_hits": self.correct_hits,
            "key_omissions": self.key_omissions,
            "incorrect_citations": self.incorrect_citations,
            "should_refuse_violations": self.should_refuse_violations,
        }


@dataclass(frozen=True)
class RecallReport:
    retriever: str
    cases: tuple[RecallCaseResult, ...]
    summary: RecallSummary

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "retriever": self.retriever,
            "evidence_selection_only": True,
            "cases": [case.as_dict() for case in self.cases],
            "summary": self.summary.as_dict(),
        }


def load_recall_dataset(path: Path) -> RecallDataset:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise UserInputError(
            f"invalid recall dataset JSON at {path}: {error.msg}"
        ) from error
    root = _mapping(raw, "recall dataset")
    if root.get("schema_version") != 1:
        raise UserInputError("recall dataset schema_version must be 1")
    evidence = tuple(
        _evidence(item, index)
        for index, item in enumerate(_sequence(root.get("evidence"), "evidence"))
    )
    cases = tuple(
        _case(item, index)
        for index, item in enumerate(_sequence(root.get("cases"), "cases"))
    )
    if not evidence:
        raise UserInputError("recall dataset must contain at least one evidence item")
    if not cases:
        raise UserInputError("recall dataset must contain at least one case")
    evidence_ids = [item.evidence_id for item in evidence]
    evidence_id_set = set(evidence_ids)
    if len(evidence_ids) != len(evidence_id_set):
        raise UserInputError("recall dataset evidence ids must be unique")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise UserInputError("recall dataset case ids must be unique")
    unknown_evidence = sorted(
        {
            evidence_id
            for case in cases
            for evidence_id in case.expected_evidence_ids
            if evidence_id not in evidence_id_set
        }
    )
    if unknown_evidence:
        raise UserInputError(
            "recall dataset references unknown evidence: " + ", ".join(unknown_evidence)
        )
    return RecallDataset(evidence=evidence, cases=cases)


def evaluate_recall(
    dataset: RecallDataset,
    retriever: EvidenceRetriever | None = None,
) -> RecallReport:
    selected_retriever = retriever or LexicalNoEmbeddingsRetriever()
    results: list[RecallCaseResult] = []
    for case in dataset.cases:
        decision = selected_retriever.retrieve(case.question, dataset.evidence)
        expected = set(case.expected_evidence_ids)
        retrieved = set(decision.evidence_ids)
        results.append(
            RecallCaseResult(
                case_id=case.case_id,
                category=case.category,
                answerable=case.answerable,
                retrieved_evidence_ids=decision.evidence_ids,
                correct_hits=tuple(sorted(expected & retrieved)),
                key_omissions=tuple(sorted(expected - retrieved)),
                incorrect_citations=tuple(sorted(retrieved - expected)),
                should_refuse_violation=(
                    not case.answerable and not decision.should_refuse
                ),
            )
        )
    passed = sum(result.passed for result in results)
    summary = RecallSummary(
        case_count=len(results),
        passed=passed,
        failed=len(results) - passed,
        correct_hits=sum(len(result.correct_hits) for result in results),
        key_omissions=sum(len(result.key_omissions) for result in results),
        incorrect_citations=sum(len(result.incorrect_citations) for result in results),
        should_refuse_violations=sum(
            result.should_refuse_violation for result in results
        ),
    )
    return RecallReport(
        retriever=selected_retriever.name,
        cases=tuple(results),
        summary=summary,
    )


def report_as_json(report: RecallReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"


def report_has_failures(report: RecallReport) -> bool:
    return report.summary.failed != 0


def report_as_text(report: RecallReport) -> str:
    lines = [
        "Evidence selection only; answer generation was not run.",
        f"Retriever: {report.retriever}",
        "Embeddings: disabled",
        "",
    ]
    for case in report.cases:
        lines.extend(
            [
                f"Case: {case.case_id}",
                f"Category: {case.category.value}",
                f"Result: {'PASS' if case.passed else 'FAIL'}",
                "Correct hits: " + _display_ids(case.correct_hits),
                "Key omissions: " + _display_ids(case.key_omissions),
                "Incorrect citations: " + _display_ids(case.incorrect_citations),
                "Should-refuse violation: "
                + ("yes" if case.should_refuse_violation else "no"),
                "",
            ]
        )
    lines.extend(
        [
            f"Cases: {report.summary.case_count}",
            f"Passed: {report.summary.passed}",
            f"Failed: {report.summary.failed}",
            f"Correct hits: {report.summary.correct_hits}",
            f"Key omissions: {report.summary.key_omissions}",
            f"Incorrect citations: {report.summary.incorrect_citations}",
            "Should-refuse violations: "
            f"{report.summary.should_refuse_violations}",
        ]
    )
    return "\n".join(lines) + "\n"


def _display_ids(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def _evidence(raw: object, index: int) -> RetrievalEvidence:
    value = _mapping(raw, f"evidence[{index}]")
    return RetrievalEvidence(
        evidence_id=_nonempty_string(value.get("id"), f"evidence[{index}].id"),
        kind=_enum_value(
            EvidenceKind,
            value.get("kind"),
            f"evidence[{index}].kind",
        ),
        state=_enum_value(
            EvidenceState,
            value.get("state"),
            f"evidence[{index}].state",
        ),
        text=_nonempty_string(value.get("text"), f"evidence[{index}].text"),
    )


def _case(raw: object, index: int) -> RecallCase:
    value = _mapping(raw, f"cases[{index}]")
    answerable = value.get("answerable")
    if not isinstance(answerable, bool):
        raise UserInputError(f"cases[{index}].answerable must be a boolean")
    evidence = tuple(
        _nonempty_string(item, f"cases[{index}].expected_evidence_ids")
        for item in _sequence(
            value.get("expected_evidence_ids"),
            f"cases[{index}].expected_evidence_ids",
        )
    )
    if answerable and not evidence:
        raise UserInputError(
            f"cases[{index}] is answerable but has no expected evidence"
        )
    if len(evidence) != len(set(evidence)):
        raise UserInputError(f"cases[{index}] repeats an expected evidence id")
    return RecallCase(
        case_id=_nonempty_string(value.get("id"), f"cases[{index}].id"),
        category=_enum_value(
            RecallCategory,
            value.get("category"),
            f"cases[{index}].category",
        ),
        question=_nonempty_string(value.get("question"), f"cases[{index}].question"),
        answerable=answerable,
        expected_evidence_ids=evidence,
    )


def _enum_value[EnumValue: StrEnum](
    enum_type: type[EnumValue],
    raw: object,
    location: str,
) -> EnumValue:
    value = _nonempty_string(raw, location)
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise UserInputError(f"{location} must be one of: {allowed}") from error


def _mapping(raw: object, location: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise UserInputError(f"{location} must be an object")
    return cast(Mapping[str, object], raw)


def _sequence(raw: object, location: str) -> Sequence[object]:
    if not isinstance(raw, list):
        raise UserInputError(f"{location} must be an array")
    return cast(Sequence[object], raw)


def _nonempty_string(raw: object, location: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise UserInputError(f"{location} must be a non-empty string")
    return raw
