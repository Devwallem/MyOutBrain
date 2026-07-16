from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re

from myoutbrain.generation import Citation, GeneratedCandidate


SCHEMA_VERSION = 1
SIMILARITY_THRESHOLD = 0.78


class CandidateWorkspaceError(Exception):
    """Raised when the temporary candidate workspace is internally inconsistent."""


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    text: str
    supporting_evidence: tuple[Citation, ...]
    contrary_evidence: tuple[Citation, ...]
    derivation: str
    occurrence_count: int
    created_at: str
    last_seen_at: str
    expires_at: str

    @classmethod
    def create(
        cls,
        candidate: GeneratedCandidate,
        occurred_at: datetime,
        ttl_days: int,
    ) -> CandidateRecord:
        return cls(
            candidate_id=candidate_identity(candidate),
            text=candidate.text,
            supporting_evidence=candidate.supporting_evidence,
            contrary_evidence=candidate.contrary_evidence,
            derivation=candidate.derivation,
            occurrence_count=1,
            created_at=occurred_at.isoformat(),
            last_seen_at=occurred_at.isoformat(),
            expires_at=(occurred_at + timedelta(days=ttl_days)).isoformat(),
        )

    @classmethod
    def from_data(cls, value: object) -> CandidateRecord:
        if not isinstance(value, dict):
            raise CandidateWorkspaceError("candidate record is not an object")
        try:
            candidate_id = value["id"]
            text = value["text"]
            derivation = value["derivation"]
            occurrence_count = value["occurrence_count"]
            created_at = value["created_at"]
            last_seen_at = value["last_seen_at"]
            expires_at = value["expires_at"]
            if value.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("candidate schema version is invalid")
            if value.get("kind") != "candidate-insight":
                raise ValueError("candidate kind is invalid")
            if value.get("state") != "pending-review":
                raise ValueError("candidate state is invalid")
            if value.get("authorship") != "system":
                raise ValueError("candidate authorship is invalid")
            if not isinstance(candidate_id, str) or re.fullmatch(
                r"cand_[0-9a-f]{64}", candidate_id
            ) is None:
                raise TypeError("candidate identity is invalid")
            if not isinstance(text, str) or not text.strip():
                raise TypeError("candidate text is invalid")
            if not isinstance(derivation, str) or not derivation.strip():
                raise TypeError("candidate derivation is invalid")
            if not isinstance(occurrence_count, int) or occurrence_count < 1:
                raise TypeError("candidate recurrence is invalid")
            for timestamp in (created_at, last_seen_at, expires_at):
                if not isinstance(timestamp, str):
                    raise TypeError("candidate timestamp is invalid")
                parsed_timestamp = datetime.fromisoformat(timestamp)
                if parsed_timestamp.tzinfo is None:
                    raise ValueError("candidate timestamp requires a timezone")
            supporting_evidence = _citations_from_data(value["supporting_evidence"])
            contrary_evidence = _citations_from_data(value["contrary_evidence"])
            if not supporting_evidence:
                raise ValueError("candidate requires supporting evidence")
        except (KeyError, TypeError, ValueError) as error:
            raise CandidateWorkspaceError("candidate record is invalid") from error
        return cls(
            candidate_id=candidate_id,
            text=text,
            supporting_evidence=supporting_evidence,
            contrary_evidence=contrary_evidence,
            derivation=derivation,
            occurrence_count=occurrence_count,
            created_at=created_at,
            last_seen_at=last_seen_at,
            expires_at=expires_at,
        )

    def merge(
        self,
        candidate: GeneratedCandidate,
        occurred_at: datetime,
        ttl_days: int,
    ) -> CandidateRecord:
        return replace(
            self,
            supporting_evidence=_union_citations(
                self.supporting_evidence,
                candidate.supporting_evidence,
            ),
            contrary_evidence=_union_citations(
                self.contrary_evidence,
                candidate.contrary_evidence,
            ),
            derivation=candidate.derivation,
            occurrence_count=self.occurrence_count + 1,
            last_seen_at=occurred_at.isoformat(),
            expires_at=(occurred_at + timedelta(days=ttl_days)).isoformat(),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.candidate_id,
            "kind": "candidate-insight",
            "state": "pending-review",
            "authorship": "system",
            "text": self.text,
            "supporting_evidence": [
                citation.to_data() for citation in self.supporting_evidence
            ],
            "contrary_evidence": [
                citation.to_data() for citation in self.contrary_evidence
            ],
            "derivation": self.derivation,
            "occurrence_count": self.occurrence_count,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "expires_at": self.expires_at,
        }


class CandidateWorkspace:
    """Owns temporary candidate validation, merging, and catalog serialization."""

    def __init__(self, root: Path, records: list[CandidateRecord]) -> None:
        self._candidate_directory = root / "runtime" / "workspace" / "candidates"
        self.catalog_path = self._candidate_directory / "catalog.json"
        self._records = records

    @classmethod
    def load(cls, root: Path) -> CandidateWorkspace:
        workspace = cls(root, [])
        if not workspace.catalog_path.is_file():
            return workspace
        try:
            catalog = json.loads(workspace.catalog_path.read_text(encoding="utf-8"))
            if not isinstance(catalog, dict):
                raise TypeError("candidate catalog is not an object")
            if catalog.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("candidate catalog schema version is invalid")
            candidates = catalog["candidates"]
            if not isinstance(candidates, list):
                raise TypeError("candidate catalog entries are not a list")
            records = [CandidateRecord.from_data(value) for value in candidates]
            identities = [record.candidate_id for record in records]
            if len(identities) != len(set(identities)):
                raise ValueError("candidate catalog contains duplicate identities")
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            CandidateWorkspaceError,
        ) as error:
            raise CandidateWorkspaceError(
                f"invalid candidate catalog: {workspace.catalog_path}"
            ) from error
        workspace._records = records
        return workspace

    def merge(
        self,
        candidates: tuple[GeneratedCandidate, ...],
        occurred_at: datetime,
        ttl_days: int,
    ) -> tuple[tuple[str, ...], int]:
        candidate_ids: list[str] = []
        suppressed_count = 0
        for candidate in candidates:
            proposed_id = candidate_identity(candidate)
            if self._is_suppressed(proposed_id, occurred_at):
                suppressed_count += 1
                continue
            matching_index = self._matching_record_index(candidate, proposed_id)
            if matching_index is None:
                record = CandidateRecord.create(candidate, occurred_at, ttl_days)
                self._records.append(record)
            else:
                record = self._records[matching_index].merge(
                    candidate,
                    occurred_at,
                    ttl_days,
                )
                self._records[matching_index] = record
            candidate_ids.append(record.candidate_id)
        return tuple(candidate_ids), suppressed_count

    def catalog_content(self) -> bytes:
        data = {
            "schema_version": SCHEMA_VERSION,
            "candidates": [record.to_data() for record in self._records],
        }
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"

    def _matching_record_index(
        self,
        candidate: GeneratedCandidate,
        proposed_id: str,
    ) -> int | None:
        for index, record in enumerate(self._records):
            if record.candidate_id == proposed_id:
                return index
        for index, record in enumerate(self._records):
            if candidate_similarity(candidate.text, record.text) >= SIMILARITY_THRESHOLD:
                return index
        return None

    def _is_suppressed(self, candidate_id: str, occurred_at: datetime) -> bool:
        fingerprint = candidate_id.removeprefix("cand_")
        rejection_path = (
            self._candidate_directory / "rejected" / f"rej_{fingerprint}.json"
        )
        if not rejection_path.is_file():
            return False
        try:
            rejection = json.loads(rejection_path.read_text(encoding="utf-8"))
            if not isinstance(rejection, dict):
                raise TypeError("rejection fingerprint is not an object")
            if rejection.get("fingerprint") != f"sha256:{fingerprint}":
                raise ValueError("rejection fingerprint does not match its path")
            suppress_until_value = rejection.get("suppress_until")
            if not isinstance(suppress_until_value, str):
                raise TypeError("rejection suppression expiry is invalid")
            suppress_until = datetime.fromisoformat(suppress_until_value)
            if suppress_until.tzinfo is None:
                raise ValueError("rejection suppression expiry requires a timezone")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise CandidateWorkspaceError(
                f"invalid rejection fingerprint: {rejection_path}"
            ) from error
        return suppress_until > occurred_at


def candidate_identity(candidate: GeneratedCandidate) -> str:
    normalized_text = " ".join(candidate.text.casefold().split())
    fingerprint = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    return f"cand_{fingerprint}"


def candidate_similarity(left: str, right: str) -> float:
    normalized_left = _normalized_similarity_text(left)
    normalized_right = _normalized_similarity_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    sequence_similarity = SequenceMatcher(
        None,
        normalized_left,
        normalized_right,
        autojunk=False,
    ).ratio()
    left_terms = set(re.findall(r"\w+", left.casefold()))
    right_terms = set(re.findall(r"\w+", right.casefold()))
    term_similarity = (
        len(left_terms & right_terms) / len(left_terms | right_terms)
        if left_terms and right_terms
        else 0.0
    )
    return max(sequence_similarity, term_similarity)


def _normalized_similarity_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _citations_from_data(value: object) -> tuple[Citation, ...]:
    if not isinstance(value, list):
        raise TypeError("candidate citations are not a list")
    citations: list[Citation] = []
    for citation_data in value:
        if not isinstance(citation_data, dict):
            raise TypeError("candidate citation is not an object")
        source_id = citation_data.get("source_id")
        locator = citation_data.get("locator")
        if not isinstance(source_id, str) or not source_id:
            raise TypeError("candidate citation source is invalid")
        if not isinstance(locator, str) or not locator:
            raise TypeError("candidate citation locator is invalid")
        citations.append(Citation(source_id=source_id, locator=locator))
    return tuple(citations)


def _union_citations(
    existing: tuple[Citation, ...],
    additions: tuple[Citation, ...],
) -> tuple[Citation, ...]:
    merged = list(existing)
    for citation in additions:
        if citation not in merged:
            merged.append(citation)
    return tuple(merged)
