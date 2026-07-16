from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from myoutbrain.candidates import CandidateRecord
from myoutbrain.generation import Citation


class KnowledgeNoteError(Exception):
    """Raised when a permanent knowledge note cannot be represented safely."""


@dataclass(frozen=True)
class DerivedInsightNote:
    knowledge_id: str
    title: str
    text: str
    authorship: str
    sensitivity: str
    candidate_id: str
    sources: tuple[str, ...]
    created_at: str
    supporting_evidence: tuple[Citation, ...]
    contrary_evidence: tuple[Citation, ...]
    derivation: str

    @classmethod
    def from_candidate(
        cls,
        candidate: CandidateRecord,
        *,
        knowledge_id: str,
        title: str,
        text: str | None,
        sensitivity: str,
        occurred_at: datetime,
    ) -> DerivedInsightNote:
        normalized_title = _normalize_title(title)
        edited_text = text.strip() if text is not None else candidate.text
        if not edited_text:
            raise KnowledgeNoteError("accepted insight text must not be blank")
        sources = tuple(
            dict.fromkeys(
                citation.source_id
                for citation in (
                    candidate.supporting_evidence + candidate.contrary_evidence
                )
            )
        )
        return cls(
            knowledge_id=knowledge_id,
            title=normalized_title,
            text=edited_text,
            authorship="mixed" if edited_text != candidate.text else "system",
            sensitivity=sensitivity,
            candidate_id=candidate.candidate_id,
            sources=sources,
            created_at=occurred_at.isoformat(),
            supporting_evidence=candidate.supporting_evidence,
            contrary_evidence=candidate.contrary_evidence,
            derivation=candidate.derivation,
        )

    @property
    def filename(self) -> str:
        return f"{self.title}.md"

    def render(self) -> bytes:
        source_lines = "\n".join(f"  - {source_id}" for source_id in self.sources)
        supporting_lines = "\n".join(
            "- Supporting: "
            f"`{citation.source_id}` — `{citation.locator}`"
            for citation in self.supporting_evidence
        )
        contrary_lines = "\n".join(
            "- Contrary: "
            f"`{citation.source_id}` — `{citation.locator}`"
            for citation in self.contrary_evidence
        )
        if not contrary_lines:
            contrary_lines = "- Contrary: none found"
        content = (
            "---\n"
            f"id: {self.knowledge_id}\n"
            "kind: insight\n"
            "state: active\n"
            f"authorship: {self.authorship}\n"
            f"sensitivity: {self.sensitivity}\n"
            f"created_at: {self.created_at}\n"
            f"updated_at: {self.created_at}\n"
            "sources:\n"
            f"{source_lines}\n"
            f"candidate_id: {self.candidate_id}\n"
            "supersedes: []\n"
            "superseded_by: []\n"
            "---\n\n"
            f"# {self.title}\n\n"
            f"{self.text}\n\n"
            "## Derivation\n\n"
            f"{self.derivation}\n\n"
            "## Evidence\n\n"
            f"{supporting_lines}\n"
            f"{contrary_lines}\n"
        )
        return content.encode("utf-8")


def _normalize_title(title: str) -> str:
    normalized = " ".join(title.split())
    if not normalized:
        raise KnowledgeNoteError("knowledge note title must not be blank")
    if normalized in {".", ".."} or re.search(r'[<>:"/\\|?*]', normalized):
        raise KnowledgeNoteError("knowledge note title contains invalid filename characters")
    if normalized.endswith((".", " ")):
        raise KnowledgeNoteError("knowledge note title has an invalid filename ending")
    if len(normalized) > 120:
        raise KnowledgeNoteError("knowledge note title is too long")
    return re.sub(
        r"(?<![\w'])([^\W_])",
        lambda match: match.group(1).upper(),
        normalized,
    )
