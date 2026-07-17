from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
from typing import cast

from myoutbrain.embeddings import (
    EmbeddingFailure,
    EmbeddingProvider,
    SEMANTIC_SIMILARITY_THRESHOLD,
    cosine_similarity,
    validate_embeddings,
)
from myoutbrain.local_core import RecallableMemory
from myoutbrain.persistence import atomic_write


_SCHEMA_VERSION = 1


class SemanticRecallIndex:
    """Own rebuildable semantic generations without exposing vector storage."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def scores(
        self,
        query: str,
        memories: tuple[RecallableMemory, ...],
        provider: EmbeddingProvider,
    ) -> dict[str, float]:
        if not memories:
            return {}
        query_vector = validate_embeddings(
            provider.space,
            (query,),
            provider.embed((query,)),
        )[0]
        scores: dict[str, float] = {}
        for sensitivity in ("local-only", "cloud-allowed"):
            partition = tuple(
                memory for memory in memories if memory.sensitivity == sensitivity
            )
            if not partition:
                continue
            vectors = self._load_or_rebuild(sensitivity, partition, provider)
            for memory in partition:
                score = cosine_similarity(query_vector, vectors[memory.memory_id])
                if score >= SEMANTIC_SIMILARITY_THRESHOLD:
                    scores[memory.memory_id] = score
        return scores

    def _load_or_rebuild(
        self,
        sensitivity: str,
        memories: tuple[RecallableMemory, ...],
        provider: EmbeddingProvider,
    ) -> dict[str, tuple[float, ...]]:
        path = self._generation_path(sensitivity)
        fingerprint = _memory_fingerprint(memories)
        loaded = _load_generation(path, provider, memories, fingerprint)
        if loaded is not None:
            return loaded
        raw_vectors = provider.embed(tuple(memory.content for memory in memories))
        vectors = validate_embeddings(
            provider.space,
            tuple(memory.content for memory in memories),
            raw_vectors,
        )
        generation_id = _generation_id(provider, sensitivity, fingerprint)
        document = {
            "schema_version": _SCHEMA_VERSION,
            "generation_id": generation_id,
            "space": provider.space.to_data(),
            "sensitivity": sensitivity,
            "source_fingerprint": fingerprint,
            "entries": [
                {
                    "memory_id": memory.memory_id,
                    "content_fingerprint": _content_fingerprint(memory.content),
                    "vector": list(vector),
                }
                for memory, vector in zip(memories, vectors)
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path,
            (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return {
            memory.memory_id: vector for memory, vector in zip(memories, vectors)
        }

    def _generation_path(self, sensitivity: str) -> Path:
        return (
            self._root
            / "runtime"
            / "indexes"
            / "semantic"
            / sensitivity
            / "current.json"
        )


def _load_generation(
    path: Path,
    provider: EmbeddingProvider,
    memories: tuple[RecallableMemory, ...],
    fingerprint: str,
) -> dict[str, tuple[float, ...]] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        document = _mapping(raw)
        if (
            document.get("schema_version") != _SCHEMA_VERSION
            or document.get("space") != provider.space.to_data()
            or document.get("source_fingerprint") != fingerprint
        ):
            return None
        entries = _sequence(document.get("entries"))
        expected_fingerprints = {
            memory.memory_id: _content_fingerprint(memory.content)
            for memory in memories
        }
        result: dict[str, tuple[float, ...]] = {}
        for raw_entry in entries:
            entry = _mapping(raw_entry)
            memory_id = entry.get("memory_id")
            vector = _sequence(entry.get("vector"))
            if (
                not isinstance(memory_id, str)
                or memory_id in result
                or entry.get("content_fingerprint")
                != expected_fingerprints.get(memory_id)
            ):
                raise TypeError
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in vector
            ):
                raise TypeError
            result[memory_id] = validate_embeddings(
                provider.space,
                (memory_id,),
                (tuple(float(cast(int | float, value)) for value in vector),),
            )[0]
        if result.keys() != expected_fingerprints.keys():
            raise TypeError
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise EmbeddingFailure(f"invalid semantic index generation: {path}") from error


def _memory_fingerprint(memories: tuple[RecallableMemory, ...]) -> str:
    data = [
        {
            "memory_id": memory.memory_id,
            "content_fingerprint": _content_fingerprint(memory.content),
            "memory_state": memory.memory_state.value,
            "sensitivity": memory.sensitivity,
        }
        for memory in memories
    ]
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _generation_id(
    provider: EmbeddingProvider,
    sensitivity: str,
    fingerprint: str,
) -> str:
    data = json.dumps(
        {
            "space": provider.space.to_data(),
            "sensitivity": sensitivity,
            "source_fingerprint": fingerprint,
        },
        sort_keys=True,
    ).encode("utf-8")
    return f"sem_{hashlib.sha256(data).hexdigest()}"


def _content_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _mapping(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise TypeError
    return cast(Mapping[str, object], raw)


def _sequence(raw: object) -> Sequence[object]:
    if not isinstance(raw, list):
        raise TypeError
    return cast(Sequence[object], raw)
