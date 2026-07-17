from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from typing import cast

from myoutbrain.embeddings import (
    DeterministicEmbeddingProvider,
    EmbeddingLocation,
    EmbeddingSpace,
    LocalMultilingualEmbeddingProvider,
    prepare_default_local_embedding_model,
)
from myoutbrain.library import KnowledgeWorkflow
from myoutbrain.local_core import LocalMemoryCore, RecallableMemory
from myoutbrain.memory_gateway import (
    Answerability,
    MemoryAccess,
    MemoryGateway,
    MemoryState,
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


class StaticMemoryReader:
    def __init__(self, memories: tuple[RecallableMemory, ...]) -> None:
        self._memories = memories

    def recallable_memories(self) -> tuple[RecallableMemory, ...]:
        return self._memories


class FakeArray:
    def __init__(self, rows: list[list[float]]) -> None:
        self._rows = rows

    def tolist(self) -> object:
        return self._rows


class FakeSentenceEncoder:
    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
    ) -> object:
        if not normalize_embeddings or not convert_to_numpy:
            raise AssertionError("local adapter must request normalized numeric vectors")
        rows: list[list[float]] = []
        for text in texts:
            normalized = text.casefold()
            vector = [0.0] * 384
            if any(
                phrase in normalized
                for phrase in (
                    "missing context",
                    "unavailable conversation",
                    "unseen earlier messages",
                    "claiming knowledge",
                )
            ):
                vector[0] = 1.0
            elif any(
                phrase in normalized
                for phrase in (
                    "accumulated experience",
                    "reusable knowledge",
                    "lessons gathered",
                    "useful again",
                )
            ):
                vector[1] = 1.0
            else:
                vector[2] = 1.0
            rows.append(vector)
        return FakeArray(rows)


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
        'cloud_send_scope = "cloud-allowed-only"\n'
        "cloud_budget_usd = 5.0\n"
        "cloud_max_texts_per_request = 8\n"
    )
    path.write_text(
        configuration[:start] + replacement + configuration[end + 1 :],
        encoding="utf-8",
    )


class SemanticMemoryRecallTests(unittest.TestCase):
    def test_default_adapter_uses_a_cached_local_multilingual_model(self) -> None:
        constructor_calls: list[tuple[str, bool]] = []

        def sentence_transformer(
            model: str,
            *,
            local_files_only: bool,
        ) -> FakeSentenceEncoder:
            constructor_calls.append((model, local_files_only))
            return FakeSentenceEncoder()

        provider = LocalMultilingualEmbeddingProvider()
        fake_module = SimpleNamespace(SentenceTransformer=sentence_transformer)

        with patch(
            "myoutbrain.embeddings.importlib.import_module",
            return_value=fake_module,
        ):
            vectors = provider.embed(("held-out paraphrase", "跨语言改写"))
            provider.embed(("cached invocation",))

        self.assertEqual(len(vectors), 2)
        self.assertTrue(all(len(vector) == 384 for vector in vectors))
        self.assertEqual(
            constructor_calls,
            [(provider.space.model, True)],
        )

    def test_initialization_prepares_the_default_model_for_offline_recall(self) -> None:
        constructor_calls: list[tuple[str, bool]] = []

        def sentence_transformer(
            model: str,
            *,
            local_files_only: bool,
        ) -> FakeSentenceEncoder:
            constructor_calls.append((model, local_files_only))
            return FakeSentenceEncoder()

        fake_module = SimpleNamespace(SentenceTransformer=sentence_transformer)
        with patch.dict("os.environ", {}, clear=True), patch(
            "myoutbrain.embeddings.importlib.import_module",
            return_value=fake_module,
        ):
            prepared = prepare_default_local_embedding_model()

        self.assertTrue(prepared)
        self.assertEqual(
            constructor_calls,
            [(LocalMultilingualEmbeddingProvider().space.model, False)],
        )

    def test_initialization_prepares_after_durable_state_and_tolerates_unavailability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "Private Companion"
            observed_durable_state: list[bool] = []

            def unavailable_model() -> bool:
                observed_durable_state.append(
                    (instance_root / "myoutbrain.toml").is_file()
                    and (instance_root / "store" / "memory.sqlite3").is_file()
                )
                return False

            with patch(
                "myoutbrain.library.prepare_default_local_embedding_model",
                side_effect=unavailable_model,
            ) as prepare:
                KnowledgeWorkflow(instance_root).initialize()

            prepare.assert_called_once_with()
            self.assertEqual(observed_durable_state, [True])
            self.assertEqual(
                LocalMemoryCore(instance_root).recallable_memories(),
                (),
            )

    def test_default_local_adapter_recalls_synonymous_buffered_and_canonical_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            instance_root = Path(temporary_directory) / "Private Companion"
            reader = StaticMemoryReader(
                (
                    RecallableMemory(
                        memory_id="dig_context_gap",
                        content=(
                            "Explicitly record missing context instead of pretending "
                            "the unavailable conversation history is remembered."
                        ),
                        memory_state=MemoryState.BUFFERED,
                        source_ids=("src_context_gap",),
                        occurred_at="2026-07-17T16:00:00+08:00",
                        sensitivity="local-only",
                        entrance="codex",
                        task="semantic-recall",
                    ),
                    RecallableMemory(
                        memory_id="mem_weekly_reflection",
                        content=(
                            "Weekly reflection turns accumulated experience into "
                            "reusable knowledge."
                        ),
                        memory_state=MemoryState.CANONICAL,
                        source_ids=("src_reflection",),
                        occurred_at="2026-07-17T16:05:00+08:00",
                        sensitivity="local-only",
                        entrance=None,
                        task=None,
                    ),
                )
            )
            gateway = MemoryGateway(
                instance_root,
                memory_reader=reader,
            )
            fake_module = SimpleNamespace(
                SentenceTransformer=lambda _model, **_kwargs: FakeSentenceEncoder()
            )

            with patch(
                "myoutbrain.embeddings.importlib.import_module",
                return_value=fake_module,
            ):
                buffered_package = gateway.recall(
                    RecallRequest(
                        query=(
                            "How do we avoid claiming knowledge of unseen earlier "
                            "messages?"
                        ),
                        task="semantic-recall",
                        access=MemoryAccess.TASK_SCOPED,
                        purpose=QueryPurpose.SUBSTANTIVE,
                    )
                )
                canonical_package = gateway.recall(
                    RecallRequest(
                        query="How can lessons gathered over time become useful again?",
                        task="semantic-recall",
                        access=MemoryAccess.TASK_SCOPED,
                        purpose=QueryPurpose.SUBSTANTIVE,
                    )
                )

            self.assertEqual(
                [(item.memory_id, item.match) for item in buffered_package.items],
                [("dig_context_gap", RecallMatch.SEMANTIC_CANDIDATE)],
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
            gateway = MemoryGateway(
                instance_root,
                embedding_provider=DeterministicEmbeddingProvider(),
            )
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

            shutil.rmtree(instance_root / "runtime" / "indexes" / "semantic")
            rebuilt = gateway.recall(request)

            self.assertEqual(generation["schema_version"], 1)
            self.assertEqual(
                generation["space"],
                DeterministicEmbeddingProvider().space.to_data(),
            )
            self.assertEqual(first.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(rebuilt.items[0].memory_id, receipt["digest_id"])
            self.assertEqual(rebuilt.to_data(), first.to_data())

            generation["entries"] = []
            index_path.write_text(json.dumps(generation), encoding="utf-8")
            degraded = gateway.recall(request)
            self.assertEqual(degraded.items, ())
            self.assertEqual(degraded.answerability, Answerability.INSUFFICIENT)

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
            still_private = MemoryGateway(
                instance_root,
                embedding_provider=provider,
            ).recall(request)
            self.assertEqual(provider.calls, [])
            self.assertEqual(still_private.items, ())

            authorized = MemoryGateway(
                instance_root,
                embedding_provider=provider,
            ).recall(
                RecallRequest(
                    query=request.query,
                    task=request.task,
                    access=request.access,
                    purpose=request.purpose,
                    query_sensitivity="cloud-allowed",
                )
            )

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
                query_sensitivity="cloud-allowed",
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
