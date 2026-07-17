from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import shutil
import sqlite3
import tempfile
import unittest
from typing import cast

from myoutbrain.embeddings import (
    EmbeddingLocation,
    EmbeddingProvider,
    EmbeddingSpace,
    LocalMultilingualEmbeddingProvider,
)
from myoutbrain.memory_gateway import (
    Answerability,
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallMatch,
    RecallRequest,
)
from tests.cli_support import run_cli


def _remember(
    temporary_root: Path,
    instance_root: Path,
    *,
    name: str,
    digest: str,
    sensitivity: str = "local-only",
) -> dict[str, object]:
    conversation = temporary_root / f"{name}.txt"
    conversation.write_text(f"Evidence for {name}.", encoding="utf-8")
    result = run_cli(
        "remember",
        str(conversation),
        "--root",
        str(instance_root),
        "--occurred-at",
        "2026-07-17T16:00:00+08:00",
        "--entrance",
        "codex",
        "--task",
        "semantic-recall",
        "--digest",
        digest,
        "--sensitivity",
        sensitivity,
        "--visible-context",
        "semantic recall acceptance",
        "--context-gap",
        "earlier messages unavailable",
        "--format",
        "json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return cast(dict[str, object], json.loads(result.stdout))


def _seed_canonical(instance_root: Path, *, memory_id: str, content: str) -> None:
    with closing(
        sqlite3.connect(instance_root / "store" / "memory.sqlite3")
    ) as connection:
        connection.execute(
            """
            INSERT INTO canonical_memories
                (memory_id, content, current_version, sensitivity, state,
                 created_at, updated_at)
            VALUES (?, ?, 1, 'local-only', 'active', ?, ?)
            """,
            (
                memory_id,
                content,
                "2026-07-17T16:05:00+08:00",
                "2026-07-17T16:05:00+08:00",
            ),
        )
        connection.commit()


class FailingEmbeddingProvider:
    @property
    def space(self) -> EmbeddingSpace:
        return EmbeddingSpace(
            provider="failing-local",
            model="failure-v1",
            dimensions=8,
            normalization_version=1,
        )

    @property
    def location(self) -> EmbeddingLocation:
        return EmbeddingLocation.LOCAL

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        del texts
        raise RuntimeError("local model unavailable")


class RecordingCloudEmbeddingProvider:
    def __init__(self, *, model: str = "recording-v1") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._space = EmbeddingSpace(
            provider="recording-cloud",
            model=model,
            dimensions=4,
            normalization_version=7,
        )

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def location(self) -> EmbeddingLocation:
        return EmbeddingLocation.CLOUD

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple((1.0, 0.0, 0.0, 0.0) for _text in texts)


def _configure_cloud_embeddings(
    instance_root: Path,
    provider: RecordingCloudEmbeddingProvider,
) -> None:
    path = instance_root / "myoutbrain.toml"
    configuration = path.read_text(encoding="utf-8")
    start = configuration.index("[embedding]\n")
    end = configuration.index("\n[reflection]", start)
    replacement = (
        "[embedding]\n"
        f'provider = "{provider.space.provider}"\n'
        f'model = "{provider.space.model}"\n'
        f"dimensions = {provider.space.dimensions}\n"
        f"normalization_version = {provider.space.normalization_version}\n"
        "allow_cloud = true\n"
    )
    path.write_text(
        configuration[:start] + replacement + configuration[end + 1 :],
        encoding="utf-8",
    )


class SemanticMemoryRecallTests(unittest.TestCase):
    def test_default_local_embeddings_recall_synonymous_buffered_and_canonical_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            buffered = _remember(
                temporary_root,
                instance_root,
                name="context-gap",
                digest=(
                    "Explicitly record missing context instead of pretending the "
                    "unavailable conversation history is remembered."
                ),
            )
            _seed_canonical(
                instance_root,
                memory_id="mem_weekly_reflection",
                content=(
                    "Weekly reflection turns accumulated experience into reusable "
                    "knowledge."
                ),
            )

            buffered_package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="How do we avoid claiming knowledge of unseen earlier messages?",
                    task="semantic-recall",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )
            canonical_package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="How can lessons gathered over time become useful again?",
                    task="semantic-recall",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(
                [(item.memory_id, item.match) for item in buffered_package.items],
                [(buffered["digest_id"], RecallMatch.SEMANTIC_CANDIDATE)],
            )
            self.assertEqual(
                [(item.memory_id, item.match) for item in canonical_package.items],
                [("mem_weekly_reflection", RecallMatch.SEMANTIC_CANDIDATE)],
            )
            self.assertEqual(
                canonical_package.answerability,
                Answerability.INSUFFICIENT,
            )
            self.assertNotIn("vector", json.dumps(canonical_package.to_data()))

    def test_semantic_index_is_versioned_rebuildable_and_does_not_change_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            receipt = _remember(
                temporary_root,
                instance_root,
                name="review",
                digest="Reflect on accumulated experience so lessons remain reusable.",
            )
            gateway = MemoryGateway(instance_root)
            request = RecallRequest(
                query="How do past lessons become useful again?",
                task="semantic-recall",
                access=MemoryAccess.TASK_SCOPED,
                purpose=QueryPurpose.SUBSTANTIVE,
            )

            first = gateway.recall(request)
            index_path = (
                instance_root
                / "runtime"
                / "indexes"
                / "semantic"
                / "local-only"
                / "current.json"
            )
            generation = json.loads(index_path.read_text(encoding="utf-8"))
            with closing(
                sqlite3.connect(instance_root / "store" / "memory.sqlite3")
            ) as connection:
                before = connection.execute(
                    "SELECT digest_id, content FROM buffered_digests"
                ).fetchall()

            shutil.rmtree(instance_root / "runtime" / "indexes" / "semantic")
            rebuilt = gateway.recall(request)
            with closing(
                sqlite3.connect(instance_root / "store" / "memory.sqlite3")
            ) as connection:
                after = connection.execute(
                    "SELECT digest_id, content FROM buffered_digests"
                ).fetchall()

            self.assertEqual(generation["schema_version"], 1)
            self.assertEqual(
                generation["space"],
                LocalMultilingualEmbeddingProvider().space.to_data(),
            )
            self.assertEqual(first.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(rebuilt.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(after, before)

    def test_embedding_failure_falls_back_to_full_text_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            receipt = _remember(
                temporary_root,
                instance_root,
                name="fallback",
                digest="Project Comet deployment requires signed manifests.",
            )

            package = MemoryGateway(
                instance_root,
                embedding_provider=FailingEmbeddingProvider(),
            ).recall(
                RecallRequest(
                    query="Project Comet deployment",
                    task="semantic-recall",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                )
            )

            self.assertEqual(package.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(package.items[0].match, RecallMatch.FULL_TEXT)
            self.assertEqual(package.answerability, Answerability.INSUFFICIENT)

    def test_cloud_embeddings_require_instance_authorization_and_exclude_local_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            local = _remember(
                temporary_root,
                instance_root,
                name="private",
                digest="Private lighthouse context must stay on this machine.",
            )
            cloud = _remember(
                temporary_root,
                instance_root,
                name="shareable",
                digest="Shareable lighthouse context may use an authorized provider.",
                sensitivity="cloud-allowed",
            )
            provider = RecordingCloudEmbeddingProvider()
            request = RecallRequest(
                query="What context is available for the beacon?",
                task="semantic-recall",
                access=MemoryAccess.LOCAL_TRUSTED,
                purpose=QueryPurpose.SUBSTANTIVE,
            )

            unauthorized = MemoryGateway(
                instance_root,
                embedding_provider=provider,
            ).recall(request)
            self.assertEqual(provider.calls, [])
            self.assertEqual(unauthorized.items, ())

            _configure_cloud_embeddings(instance_root, provider)
            authorized = MemoryGateway(
                instance_root,
                embedding_provider=provider,
            ).recall(request)

            sent_text = " ".join(
                text for call in provider.calls for text in call
            )
            self.assertNotIn("Private lighthouse", sent_text)
            self.assertIn("Shareable lighthouse", sent_text)
            self.assertEqual(
                [item.memory_id for item in authorized.items],
                [cloud["digest_id"]],
            )
            self.assertNotIn(local["digest_id"], sent_text)

    def test_incompatible_embedding_spaces_replace_instead_of_mix_generations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            _remember(
                temporary_root,
                instance_root,
                name="generation",
                digest="Shareable lessons can become useful again.",
                sensitivity="cloud-allowed",
            )
            request = RecallRequest(
                query="Can past learning be reused?",
                task="semantic-recall",
                access=MemoryAccess.LOCAL_TRUSTED,
                purpose=QueryPurpose.SUBSTANTIVE,
            )
            first_provider = RecordingCloudEmbeddingProvider(model="recording-v1")
            _configure_cloud_embeddings(instance_root, first_provider)
            MemoryGateway(
                instance_root,
                embedding_provider=first_provider,
            ).recall(request)
            second_provider = RecordingCloudEmbeddingProvider(model="recording-v2")
            _configure_cloud_embeddings(instance_root, second_provider)

            MemoryGateway(
                instance_root,
                embedding_provider=second_provider,
            ).recall(request)

            generation_path = (
                instance_root
                / "runtime"
                / "indexes"
                / "semantic"
                / "cloud-allowed"
                / "current.json"
            )
            generation = json.loads(generation_path.read_text(encoding="utf-8"))
            self.assertEqual(generation["space"], second_provider.space.to_data())
            self.assertNotEqual(
                generation["space"], first_provider.space.to_data()
            )
            self.assertEqual(len(generation["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
