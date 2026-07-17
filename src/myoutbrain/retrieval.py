from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Protocol


class EvidenceKind(StrEnum):
    SOURCE = "source"
    PERSONAL_COGNITION = "personal-cognition"
    BUFFERED_MEMORY = "buffered-memory"


class EvidenceState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class RetrievalEvidence:
    evidence_id: str
    kind: EvidenceKind
    state: EvidenceState
    text: str


@dataclass(frozen=True)
class RetrievalDecision:
    evidence_ids: tuple[str, ...]
    should_refuse: bool


class EvidenceRetriever(Protocol):
    @property
    def name(self) -> str: ...

    def retrieve(
        self,
        question: str,
        evidence: Sequence[RetrievalEvidence],
    ) -> RetrievalDecision: ...


class LexicalNoEmbeddingsRetriever:
    @property
    def name(self) -> str:
        return "lexical-no-embeddings"

    def retrieve(
        self,
        question: str,
        evidence: Sequence[RetrievalEvidence],
    ) -> RetrievalDecision:
        question_terms = lexical_terms(question)
        scores = {
            item.evidence_id: len(question_terms & lexical_terms(item.text))
            for item in evidence
            if item.state is EvidenceState.ACTIVE
        }
        best_score = max(scores.values(), default=0)
        if best_score == 0:
            return RetrievalDecision(evidence_ids=(), should_refuse=True)
        selected_ids = tuple(
            sorted(
                evidence_id
                for evidence_id, score in scores.items()
                if score == best_score
            )
        )
        return RetrievalDecision(evidence_ids=selected_ids, should_refuse=False)


class SemanticCandidateRetriever:
    """Evaluation adapter for the default local semantic candidate model."""

    @property
    def name(self) -> str:
        return "local-semantic-candidates"

    def retrieve(
        self,
        question: str,
        evidence: Sequence[RetrievalEvidence],
    ) -> RetrievalDecision:
        from myoutbrain.embeddings import (
            DeterministicEmbeddingProvider,
            SEMANTIC_SIMILARITY_THRESHOLD,
            cosine_similarity,
        )

        active = tuple(item for item in evidence if item.state is EvidenceState.ACTIVE)
        if not active:
            return RetrievalDecision(evidence_ids=(), should_refuse=True)
        provider = DeterministicEmbeddingProvider()
        vectors = provider.embed((question,) + tuple(item.text for item in active))
        question_vector = vectors[0]
        scores = {
            item.evidence_id: cosine_similarity(question_vector, vector)
            for item, vector in zip(active, vectors[1:])
        }
        best_score = max(scores.values(), default=0.0)
        if best_score < SEMANTIC_SIMILARITY_THRESHOLD:
            return RetrievalDecision(evidence_ids=(), should_refuse=True)
        selected_ids = tuple(
            sorted(
                evidence_id
                for evidence_id, score in scores.items()
                if score >= SEMANTIC_SIMILARITY_THRESHOLD
                and score >= best_score - 0.05
            )
        )
        return RetrievalDecision(evidence_ids=selected_ids, should_refuse=False)


_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "does",
        "for",
        "how",
        "is",
        "of",
        "should",
        "the",
        "to",
        "what",
        "when",
        "which",
        "who",
        "why",
    }
)
_HAN_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def lexical_terms(text: str) -> frozenset[str]:
    normalized = text.casefold()
    han_runs = _HAN_RUN.findall(normalized)
    word_terms = {
        term
        for term in re.findall(
            r"[^\W_]+",
            _HAN_RUN.sub(" ", normalized),
            flags=re.UNICODE,
        )
        if term not in _STOP_WORDS
    }
    han_bigrams = {
        run[index : index + 2]
        for run in han_runs
        for index in range(max(1, len(run) - 1))
    }
    return frozenset(word_terms | han_bigrams)
