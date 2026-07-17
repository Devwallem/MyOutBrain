from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import cast

from myoutbrain.core_types import MemoryState, Sensitivity
from myoutbrain.embeddings import DeterministicEmbeddingProvider
from myoutbrain.local_core import LocalMemoryCore
from tests.cli_support import run_cli


def remember_digest(
    temporary_root: Path,
    instance_root: Path,
    *,
    name: str,
    digest: str,
    task: str = "weekly-review",
    sensitivity: Sensitivity = "local-only",
) -> dict[str, object]:
    conversation = temporary_root / f"{name}.txt"
    conversation.write_text(f"Conversation evidence for {name}.", encoding="utf-8")
    result = run_cli(
        "remember",
        str(conversation),
        "--root",
        str(instance_root),
        "--occurred-at",
        "2026-07-17T18:00:00+08:00",
        "--entrance",
        "codex",
        "--task",
        task,
        "--digest",
        digest,
        "--sensitivity",
        sensitivity,
        "--visible-context",
        "manual consolidation acceptance",
        "--context-gap",
        "earlier tasks unavailable",
        "--format",
        "json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return cast(dict[str, object], json.loads(result.stdout))


def downgrade_memory_store_to_v2(instance_root: Path) -> None:
    database_path = instance_root / "store" / "memory.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE legacy_knowledge_metadata;
            DROP TABLE legacy_source_metadata;
            DROP TABLE legacy_audit_events;
            DROP TABLE legacy_migration_runs;
            DROP TABLE integration_reviews;
            DROP TABLE integration_proposal_sources;
            DROP TABLE integration_proposal_related;
            DROP TABLE integration_proposal_buffered;
            DROP TABLE integration_proposals;
            DROP TABLE canonical_memory_conflicts;
            DROP TABLE canonical_memory_relations;
            DROP TABLE canonical_memory_version_sources;
            DROP TABLE canonical_memory_versions;
            CREATE TABLE buffered_digests_v2 (
                digest_id TEXT PRIMARY KEY,
                experience_id TEXT NOT NULL UNIQUE REFERENCES experiences(experience_id),
                content TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state = 'buffered'),
                created_at TEXT NOT NULL
            );
            INSERT INTO buffered_digests_v2
                (digest_id, experience_id, content, fingerprint, state, created_at)
            SELECT digest_id, experience_id, content, fingerprint, state, created_at
            FROM buffered_digests;
            DROP TABLE buffered_digests;
            ALTER TABLE buffered_digests_v2 RENAME TO buffered_digests;
            PRAGMA user_version = 2;
            """
        )


class ManualMemoryConsolidationTests(unittest.TestCase):
    def test_default_review_text_displays_evidence_and_related_canonical_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            receipt = remember_digest(
                temporary_root,
                instance_root,
                name="reviewable",
                digest="Weekly reflection makes accumulated lessons reusable.",
            )

            rendered = run_cli(
                "consolidate",
                "--task",
                "weekly-review",
                "--root",
                str(instance_root),
            )

            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertIn(f"Evidence: {receipt['digest_id']}", rendered.stdout)
            self.assertIn("Related canonical memories: none", rendered.stdout)

    def test_manual_consolidation_groups_related_buffered_memory_without_writing_canonical(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            first = remember_digest(
                temporary_root,
                instance_root,
                name="reflection-one",
                digest="Weekly reflection makes accumulated lessons reusable.",
            )
            second = remember_digest(
                temporary_root,
                instance_root,
                name="reflection-two",
                digest="Weekly reflection makes accumulated experience reusable.",
            )

            consolidation = run_cli(
                "consolidate",
                "--task",
                "weekly-review",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            pending = run_cli(
                "review-memory",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "consolidate",
                "--task",
                "weekly-review",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(consolidation.returncode, 0, consolidation.stderr)
            proposals = json.loads(consolidation.stdout)["proposals"]
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            self.assertEqual(proposal["status"], "pending")
            self.assertEqual(proposal["topic"], "weekly-review")
            self.assertEqual(
                set(proposal["evidence_memory_ids"]),
                {first["digest_id"], second["digest_id"]},
            )
            self.assertEqual(
                set(proposal["source_scope"]),
                {first["source_id"], second["source_id"]},
            )
            self.assertEqual(proposal["related_canonical_memory_ids"], [])
            self.assertIn("Weekly reflection", proposal["proposed_understanding"])
            self.assertTrue(proposal["possible_impact"])

            memories = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(
                {memory.memory_state for memory in memories},
                {MemoryState.BUFFERED},
            )
            self.assertEqual(pending.returncode, 0, pending.stderr)
            self.assertEqual(
                json.loads(pending.stdout)["proposals"],
                proposals,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["proposals"], proposals)

    def test_manual_consolidation_uses_the_embedding_seam_for_paraphrases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            first = remember_digest(
                temporary_root,
                instance_root,
                name="context-gap",
                digest=(
                    "Explicitly record missing context instead of pretending "
                    "unavailable conversation history is remembered."
                ),
                task="semantic-group",
            )
            second = remember_digest(
                temporary_root,
                instance_root,
                name="honest-memory",
                digest="Avoid claiming knowledge of unseen earlier messages.",
                task="semantic-group",
            )

            proposals = LocalMemoryCore(instance_root).propose_manual_consolidation(
                "semantic-group",
                embedding_provider=DeterministicEmbeddingProvider(),
            )

            self.assertEqual(len(proposals), 1)
            self.assertEqual(
                set(proposals[0].evidence_memory_ids),
                {first["digest_id"], second["digest_id"]},
            )

    def test_manual_consolidation_separates_unrelated_topics_in_the_same_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            reflection_one = remember_digest(
                temporary_root,
                instance_root,
                name="reflection-one",
                digest="Weekly reflection makes accumulated lessons reusable.",
                task="companion-loop",
            )
            reflection_two = remember_digest(
                temporary_root,
                instance_root,
                name="reflection-two",
                digest="Weekly reflection makes accumulated experience reusable.",
                task="companion-loop",
            )
            deployment = remember_digest(
                temporary_root,
                instance_root,
                name="deployment",
                digest="Project Comet deployment requires signed manifests.",
                task="companion-loop",
            )

            consolidation = run_cli(
                "consolidate",
                "--task",
                "companion-loop",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(consolidation.returncode, 0, consolidation.stderr)
            proposals = json.loads(consolidation.stdout)["proposals"]
            self.assertEqual(len(proposals), 2)
            evidence_groups = {
                frozenset(proposal["evidence_memory_ids"])
                for proposal in proposals
            }
            self.assertEqual(
                evidence_groups,
                {
                    frozenset(
                        (reflection_one["digest_id"], reflection_two["digest_id"])
                    ),
                    frozenset((deployment["digest_id"],)),
                },
            )

    def test_natural_acceptance_transactionally_creates_source_linked_canonical_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            receipt = remember_digest(
                temporary_root,
                instance_root,
                name="accepted-reflection",
                digest="Weekly reflection makes accumulated lessons reusable.",
            )
            consolidation = run_cli(
                "consolidate",
                "--task",
                "weekly-review",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(consolidation.returncode, 0, consolidation.stderr)
            proposal = json.loads(consolidation.stdout)["proposals"][0]

            review = run_cli(
                "review-memory",
                proposal["proposal_id"],
                "I accept this proposal.",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(review.returncode, 0, review.stderr)
            decision = json.loads(review.stdout)
            self.assertEqual(decision["decision"], "accepted")
            self.assertEqual(decision["proposal_id"], proposal["proposal_id"])
            self.assertTrue(decision["canonical_memory_id"].startswith("mem_"))
            self.assertEqual(
                decision["canonical_content"],
                proposal["proposed_understanding"],
            )
            memories = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(len(memories), 1)
            canonical = memories[0]
            self.assertEqual(canonical.memory_state, MemoryState.CANONICAL)
            self.assertEqual(canonical.memory_id, decision["canonical_memory_id"])
            self.assertEqual(canonical.content, proposal["proposed_understanding"])
            self.assertEqual(canonical.source_ids, (receipt["source_id"],))
            self.assertEqual(
                LocalMemoryCore(instance_root).pending_integration_proposals(),
                (),
            )

    def test_exact_duplicate_acceptance_reuses_stable_canonical_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            first = remember_digest(
                temporary_root,
                instance_root,
                name="first-copy",
                digest="Weekly reflection makes accumulated lessons reusable.",
                task="first-pass",
                sensitivity="cloud-allowed",
            )
            initial_proposal = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "first-pass",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]
            initial_review = run_cli(
                "review-memory",
                initial_proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            canonical_id = json.loads(initial_review.stdout)["canonical_memory_id"]
            second = remember_digest(
                temporary_root,
                instance_root,
                name="second-copy",
                digest="Weekly reflection makes accumulated lessons reusable.",
                task="second-pass",
            )
            duplicate_proposal = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "second-pass",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]
            self.assertEqual(duplicate_proposal["suggested_action"], "supplement")
            self.assertEqual(duplicate_proposal["target_memory_id"], canonical_id)

            duplicate_review = run_cli(
                "review-memory",
                duplicate_proposal["proposal_id"],
                "I approve the proposal.",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(duplicate_review.returncode, 0, duplicate_review.stderr)
            self.assertEqual(
                json.loads(duplicate_review.stdout)["canonical_memory_id"],
                canonical_id,
            )
            canonical = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(len(canonical), 1)
            self.assertEqual(canonical[0].memory_id, canonical_id)
            self.assertEqual(
                set(canonical[0].source_ids),
                {first["source_id"], second["source_id"]},
            )
            self.assertEqual(canonical[0].sensitivity, "local-only")

            third = remember_digest(
                temporary_root,
                instance_root,
                name="third-copy",
                digest="Weekly reflection makes accumulated lessons reusable.",
                task="third-pass",
                sensitivity="cloud-allowed",
            )
            third_proposal = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "third-pass",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]
            third_review = run_cli(
                "review-memory",
                third_proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
            )

            self.assertEqual(third_review.returncode, 0, third_review.stderr)
            after_cloud_duplicate = LocalMemoryCore(
                instance_root
            ).recallable_memories()
            self.assertEqual(len(after_cloud_duplicate), 1)
            self.assertEqual(after_cloud_duplicate[0].sensitivity, "local-only")
            self.assertEqual(
                set(after_cloud_duplicate[0].source_ids),
                {first["source_id"], second["source_id"], third["source_id"]},
            )

    def test_natural_edit_and_rejection_preserve_review_history_without_unapproved_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            remember_digest(
                temporary_root,
                instance_root,
                name="editable",
                digest="Weekly reflection might make accumulated lessons reusable.",
                task="edit-review",
            )
            editable = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "edit-review",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]
            edited_text = (
                "Weekly reflection can make accumulated lessons reusable when the "
                "creator deliberately reviews outcomes."
            )

            edited = run_cli(
                "review-memory",
                editable["proposal_id"],
                f"edit: {edited_text}",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertEqual(json.loads(edited.stdout)["decision"], "edited")
            self.assertEqual(
                json.loads(edited.stdout)["canonical_content"],
                edited_text,
            )

            rejected_receipt = remember_digest(
                temporary_root,
                instance_root,
                name="rejected",
                digest="A speculative daily ritual should become mandatory.",
                task="reject-review",
            )
            rejectable = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "reject-review",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]
            rejected = run_cli(
                "review-memory",
                rejectable["proposal_id"],
                "reject because: this remains speculative",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            history = run_cli(
                "review-memory",
                "--history",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertEqual(json.loads(rejected.stdout)["decision"], "rejected")
            self.assertIsNone(json.loads(rejected.stdout)["canonical_memory_id"])
            self.assertEqual(history.returncode, 0, history.stderr)
            reviews = json.loads(history.stdout)["reviews"]
            self.assertEqual(
                {review["decision"] for review in reviews},
                {"edited", "rejected"},
            )
            rejected_history = next(
                review for review in reviews if review["decision"] == "rejected"
            )
            self.assertEqual(
                rejected_history["reason"],
                "this remains speculative",
            )
            memories = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(
                sum(memory.memory_state is MemoryState.CANONICAL for memory in memories),
                1,
            )
            self.assertIn(
                rejected_receipt["digest_id"],
                {memory.memory_id for memory in memories},
            )

    def test_new_proposal_recalls_related_canonical_memory_without_modifying_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            original = remember_digest(
                temporary_root,
                instance_root,
                name="original",
                digest="Weekly reflection makes accumulated lessons reusable.",
                task="initial-review",
            )
            first_proposal = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "initial-review",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]
            accepted = run_cli(
                "review-memory",
                first_proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            canonical_id = json.loads(accepted.stdout)["canonical_memory_id"]
            followup = remember_digest(
                temporary_root,
                instance_root,
                name="followup",
                digest="Weekly reflection makes accumulated experience reusable.",
                task="followup-review",
            )

            proposed = run_cli(
                "consolidate",
                "--task",
                "followup-review",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            proposal = json.loads(proposed.stdout)["proposals"][0]
            self.assertEqual(
                proposal["related_canonical_memory_ids"],
                [canonical_id],
            )
            self.assertEqual(
                set(proposal["source_scope"]),
                {followup["source_id"]},
            )
            canonical_before_review = tuple(
                memory
                for memory in LocalMemoryCore(instance_root).recallable_memories()
                if memory.memory_state is MemoryState.CANONICAL
            )
            self.assertEqual(len(canonical_before_review), 1)
            self.assertEqual(canonical_before_review[0].memory_id, canonical_id)

            accepted_related = run_cli(
                "review-memory",
                proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(
                accepted_related.returncode,
                0,
                accepted_related.stderr,
            )
            accepted_data = json.loads(accepted_related.stdout)
            self.assertNotEqual(accepted_data["canonical_memory_id"], canonical_id)
            self.assertEqual(
                accepted_data["related_canonical_memory_ids"],
                [canonical_id],
            )
            canonical_after_review = {
                memory.memory_id: memory
                for memory in LocalMemoryCore(instance_root).recallable_memories()
            }
            self.assertEqual(
                canonical_after_review[canonical_id].source_ids,
                (original["source_id"],),
            )
            self.assertEqual(
                canonical_after_review[
                    accepted_data["canonical_memory_id"]
                ].source_ids,
                (followup["source_id"],),
            )
            self.assertEqual(
                canonical_after_review[
                    accepted_data["canonical_memory_id"]
                ].related_memory_ids,
                (canonical_id,),
            )

    def test_v2_memory_store_upgrades_without_losing_public_memory_behavior(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            receipt = remember_digest(
                temporary_root,
                instance_root,
                name="legacy-buffer",
                digest="Weekly reflection makes accumulated lessons reusable.",
                task="legacy-review",
            )
            downgrade_memory_store_to_v2(instance_root)

            upgraded = run_cli("init", "--root", str(instance_root))
            proposal_result = run_cli(
                "consolidate",
                "--task",
                "legacy-review",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertEqual(proposal_result.returncode, 0, proposal_result.stderr)
            proposal = json.loads(proposal_result.stdout)["proposals"][0]
            self.assertEqual(proposal["evidence_memory_ids"], [receipt["digest_id"]])
            review = run_cli(
                "review-memory",
                proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
            )
            self.assertEqual(review.returncode, 0, review.stderr)
            memories = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0].memory_state, MemoryState.CANONICAL)

    def test_interrupted_acceptance_restores_the_complete_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            receipt = remember_digest(
                temporary_root,
                instance_root,
                name="interrupted",
                digest="Weekly reflection makes accumulated lessons reusable.",
            )
            proposal = json.loads(
                run_cli(
                    "consolidate",
                    "--task",
                    "weekly-review",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )["proposals"][0]

            interrupted = run_cli(
                "review-memory",
                proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "integration-review-after-database"
                },
            )

            self.assertEqual(interrupted.returncode, 86)
            recovered_memories = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(len(recovered_memories), 1)
            self.assertEqual(recovered_memories[0].memory_id, receipt["digest_id"])
            self.assertEqual(
                recovered_memories[0].memory_state,
                MemoryState.BUFFERED,
            )
            pending = LocalMemoryCore(instance_root).pending_integration_proposals()
            self.assertEqual(
                [candidate.proposal_id for candidate in pending],
                [proposal["proposal_id"]],
            )
            self.assertEqual(
                LocalMemoryCore(instance_root).integration_review_history(),
                (),
            )

            completed = run_cli(
                "review-memory",
                proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            final_memories = LocalMemoryCore(instance_root).recallable_memories()
            self.assertEqual(len(final_memories), 1)
            self.assertEqual(final_memories[0].memory_state, MemoryState.CANONICAL)


if __name__ == "__main__":
    unittest.main()
