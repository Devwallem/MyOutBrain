from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import math
import re
import unicodedata
from typing import Protocol

from myoutbrain.retrieval import lexical_terms


class EmbeddingFailure(Exception):
    """Raised when an embedding adapter cannot produce compatible vectors."""


class EmbeddingLocation(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class EmbeddingSpace:
    provider: str
    model: str
    dimensions: int
    normalization_version: int

    def to_data(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
            "normalization_version": self.normalization_version,
        }


class EmbeddingProvider(Protocol):
    @property
    def space(self) -> EmbeddingSpace: ...

    @property
    def location(self) -> EmbeddingLocation: ...

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


DEFAULT_LOCAL_EMBEDDING_SPACE = EmbeddingSpace(
    provider="myoutbrain-local",
    model="multilingual-concept-hash-v1",
    dimensions=256,
    normalization_version=1,
)
SEMANTIC_SIMILARITY_THRESHOLD = 0.5


_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "context-visibility": (
        "missing context",
        "unavailable",
        "unseen",
        "earlier messages",
        "conversation history",
        "context gap",
        "blind spot",
        "缺失上下文",
        "不可见",
        "早期消息",
        "对话历史",
        "上下文缺口",
    ),
    "honest-memory": (
        "explicitly record",
        "state explicitly",
        "pretending",
        "claiming",
        "remembered",
        "avoid claiming",
        "明确记录",
        "如实说明",
        "假装记得",
        "声称知道",
    ),
    "reflection": (
        "reflection",
        "reflect",
        "retrospective",
        "review lessons",
        "反思",
        "复盘",
        "回顾经验",
    ),
    "accumulated-experience": (
        "accumulated experience",
        "past lessons",
        "lessons gathered",
        "over time",
        "experience",
        "lessons",
        "积累经验",
        "过往教训",
        "长期积累",
    ),
    "reuse": (
        "reusable",
        "reuse",
        "useful again",
        "apply again",
        "reusable knowledge",
        "可复用",
        "再次使用",
        "转化为知识",
    ),
}


class LocalMultilingualEmbeddingProvider:
    """Small offline semantic projection used when no model service is configured.

    The adapter combines multilingual concept features with hashed lexical features.
    It is deliberately modest: it broadens candidate recall while downstream policy
    remains responsible for evidence quality and answerability.
    """

    @property
    def space(self) -> EmbeddingSpace:
        return DEFAULT_LOCAL_EMBEDDING_SPACE

    @property
    def location(self) -> EmbeddingLocation:
        return EmbeddingLocation.LOCAL

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        normalized = _normalize(text)
        vector = [0.0] * self.space.dimensions
        for term in lexical_terms(normalized):
            _add_feature(vector, f"term:{term}", 1.0)
        for concept, aliases in _CONCEPT_ALIASES.items():
            hits = sum(alias in normalized for alias in aliases)
            if hits:
                _add_feature(vector, f"concept:{concept}", 3.0 + min(hits, 2))
        compact_words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
        for left, right in zip(compact_words, compact_words[1:]):
            _add_feature(vector, f"pair:{left}:{right}", 0.35)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


def validate_embeddings(
    space: EmbeddingSpace,
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
) -> tuple[tuple[float, ...], ...]:
    if len(vectors) != len(texts):
        raise EmbeddingFailure("embedding provider returned the wrong vector count")
    validated: list[tuple[float, ...]] = []
    for vector in vectors:
        if len(vector) != space.dimensions:
            raise EmbeddingFailure("embedding provider returned an incompatible dimension")
        values = tuple(float(value) for value in vector)
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingFailure("embedding provider returned a non-finite vector")
        validated.append(values)
    return tuple(validated)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingFailure("cannot compare incompatible vector dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _add_feature(vector: list[float], feature: str, weight: float) -> None:
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(vector)
    sign = 1.0 if digest[4] & 1 else -1.0
    vector[index] += sign * weight
