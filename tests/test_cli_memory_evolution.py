from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import cast

from myoutbrain.core_types import Sensitivity
from myoutbrain.memory_gateway import (
    Answerability,
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)
from tests.cli_support import run_cli


def remember_evidence(
    temporary_root: Path,
    instance_root: Path,
    *,
    name: str,
    digest: str,
    task: str,
    sensitivity: Sensitivity = "local-only",
) -> dict[str, object]:
    conversation = temporary_root / f"{name}.txt"
    conversation.write_text(f"Evidence captured for {name}.", encoding="utf-8")
    result = run_cli(
        "remember",
        str(conversation),
        "--root",
        str(instance_root),
        "--occurred-at",
        "2026-07-18T09:00:00+08:00",
        "--entrance",
        "codex",
        "--task",
        task,
        "--digest",
        digest,
        "--sensitivity",
        sensitivity,
        "--visible-context",
        "memory evolution acceptance",
        "--context-gap",
        "earlier tasks unavailable",
        "--format",
        "json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return cast(dict[str, object], json.loads(result.stdout))


def propose(
    instance_root: Path,
    task: str,
) -> dict[str, object]:
    result = run_cli(
        "consolidate",
        "--task",
        task,
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    proposals = json.loads(result.stdout)["proposals"]
    if len(proposals) != 1:
        raise AssertionError(proposals)
    return cast(dict[str, object], proposals[0])


def accept_new(instance_root: Path, proposal_id: object) -> str:
    result = run_cli(
        "review-memory",
        cast(str, proposal_id),
        "accept",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return cast(str, json.loads(result.stdout)["canonical_memory_id"])


def downgrade_memory_store_to_v3(instance_root: Path) -> None:
    database_path = instance_root / "store" / "memory.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE canonical_memory_conflicts;
            DROP TABLE canonical_memory_version_sources;
            DROP TABLE canonical_memory_versions;
            ALTER TABLE integration_reviews DROP COLUMN action;
            ALTER TABLE integration_proposals DROP COLUMN target_memory_id;
            ALTER TABLE integration_proposals DROP COLUMN suggested_action;
            PRAGMA user_version = 3;
            """
        )


class CanonicalMemoryEvolutionTests(unittest.TestCase):
    def test_v3_canonical_memory_upgrades_into_a_queryable_version_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            receipt = remember_evidence(
                temporary_root,
                instance_root,
                name="legacy-canonical",
                digest="Project Atlas review cadence is weekly.",
                task="legacy-canonical",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "legacy-canonical")["proposal_id"],
            )
            downgrade_memory_store_to_v3(instance_root)

            upgraded = run_cli("init", "--root", str(instance_root))
            why = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertEqual(why.returncode, 0, why.stderr)
            audit = json.loads(why.stdout)
            self.assertEqual(audit["current_version"], 1)
            self.assertEqual(audit["versions"][0]["action"], "created")
            self.assertEqual(audit["current_source_ids"], [receipt["source_id"]])

    def test_approved_revision_keeps_identity_and_exposes_versioned_why_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            original = remember_evidence(
                temporary_root,
                instance_root,
                name="weekly-cadence",
                digest="Project Atlas review cadence is weekly.",
                task="initial-cadence",
            )
            original_proposal = propose(instance_root, "initial-cadence")
            memory_id = accept_new(instance_root, original_proposal["proposal_id"])
            correction = remember_evidence(
                temporary_root,
                instance_root,
                name="monthly-cadence",
                digest="Project Atlas review cadence is monthly.",
                task="correct-cadence",
            )
            revision_proposal = propose(instance_root, "correct-cadence")
            self.assertEqual(revision_proposal["suggested_action"], "new")
            self.assertIsNone(revision_proposal["target_memory_id"])

            revised = run_cli(
                "review-memory",
                cast(str, revision_proposal["proposal_id"]),
                f"revise {memory_id} because: the newer outcome corrected the cadence",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            why = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            why_text = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
            )
            review_history = run_cli(
                "review-memory",
                "--history",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(revised.returncode, 0, revised.stderr)
            decision = json.loads(revised.stdout)
            self.assertEqual(decision["action"], "revised")
            self.assertEqual(decision["canonical_memory_id"], memory_id)
            self.assertEqual(why.returncode, 0, why.stderr)
            audit = json.loads(why.stdout)
            self.assertEqual(audit["memory_id"], memory_id)
            self.assertEqual(audit["confirmation_status"], "confirmed")
            self.assertEqual(audit["current_version"], 2)
            self.assertEqual(
                audit["current_content"],
                revision_proposal["proposed_understanding"],
            )
            self.assertEqual(audit["current_source_ids"], [correction["source_id"]])
            self.assertEqual(len(audit["versions"]), 2)
            old_version, current_version = audit["versions"]
            self.assertEqual(old_version["content"], original_proposal["proposed_understanding"])
            self.assertEqual(old_version["status"], "superseded")
            self.assertEqual(
                old_version["supersession_reason"],
                "the newer outcome corrected the cadence",
            )
            self.assertEqual(old_version["source_ids"], [original["source_id"]])
            self.assertEqual(current_version["status"], "current")
            self.assertEqual(current_version["action"], "revised")
            self.assertEqual(review_history.returncode, 0, review_history.stderr)
            self.assertEqual(
                json.loads(review_history.stdout)["reviews"][-1]["action"],
                "revised",
            )
            self.assertEqual(why_text.returncode, 0, why_text.stderr)
            self.assertIn("Confirmation: confirmed", why_text.stdout)
            self.assertIn("Current version: 2", why_text.stdout)
            self.assertIn("Replaced because:", why_text.stdout)

            recalled = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="Project Atlas review cadence monthly",
                    task="audit-current",
                    access=MemoryAccess.LOCAL_TRUSTED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    memory_ids=(memory_id,),
                )
            )
            self.assertEqual([item.memory_id for item in recalled.items], [memory_id])
            self.assertEqual(
                recalled.items[0].content,
                revision_proposal["proposed_understanding"],
            )

    def test_natural_supplement_creates_a_new_version_under_the_same_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="base-method",
                digest="Project Atlas review cadence is weekly.",
                task="base-method",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "base-method")["proposal_id"],
            )
            addition = remember_evidence(
                temporary_root,
                instance_root,
                name="launch-retro",
                digest="Project Atlas review cadence includes launch retrospectives.",
                task="supplement-method",
            )
            supplement = propose(instance_root, "supplement-method")
            combined = (
                "Project Atlas review cadence is weekly and includes launch "
                "retrospectives."
            )

            reviewed = run_cli(
                "review-memory",
                cast(str, supplement["proposal_id"]),
                (
                    f"supplement {memory_id} with: {combined} "
                    "because: the new evidence adds a launch constraint"
                ),
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            why = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["action"], "supplemented")
            audit = json.loads(why.stdout)
            self.assertEqual(audit["current_version"], 2)
            self.assertEqual(audit["current_content"], combined)
            self.assertIn(addition["source_id"], audit["current_source_ids"])
            self.assertEqual(audit["versions"][1]["action"], "supplemented")

    def test_preserved_conflict_keeps_both_sides_and_forces_insufficient_answerability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="weekly-evidence",
                digest="Project Atlas review cadence is weekly.",
                task="weekly-view",
                sensitivity="cloud-allowed",
            )
            weekly_id = accept_new(
                instance_root,
                propose(instance_root, "weekly-view")["proposal_id"],
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="daily-evidence",
                digest="Project Atlas review cadence is daily.",
                task="daily-view",
            )
            conflict_proposal = propose(instance_root, "daily-view")

            preserved = run_cli(
                "review-memory",
                cast(str, conflict_proposal["proposal_id"]),
                (
                    f"preserve conflict with {weekly_id} because: "
                    "the available evidence disagrees"
                ),
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(preserved.returncode, 0, preserved.stderr)
            conflict_result = json.loads(preserved.stdout)
            self.assertEqual(conflict_result["action"], "conflicted")
            daily_id = conflict_result["canonical_memory_id"]
            self.assertNotEqual(daily_id, weekly_id)
            package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="Project Atlas review cadence",
                    task="cadence-answer",
                    access=MemoryAccess.LOCAL_TRUSTED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    memory_ids=(weekly_id,),
                )
            )
            self.assertEqual(package.answerability, Answerability.INSUFFICIENT)
            self.assertEqual(
                {item.memory_id for item in package.items},
                {weekly_id, daily_id},
            )
            self.assertEqual(
                package.unresolved_conflicts,
                (tuple(sorted((daily_id, weekly_id))),),
            )
            why = run_cli(
                "why-memory",
                weekly_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            audit = json.loads(why.stdout)
            self.assertEqual(audit["confirmation_status"], "conflicted")
            self.assertEqual(audit["unresolved_conflicts"][0]["memory_id"], daily_id)
            public_package = MemoryGateway(instance_root).recall(
                RecallRequest(
                    query="Project Atlas review cadence",
                    task="public-cadence",
                    access=MemoryAccess.PUBLIC_EXTERNAL,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    memory_ids=(weekly_id,),
                    query_sensitivity="cloud-allowed",
                )
            )
            self.assertEqual(
                [item.memory_id for item in public_package.items],
                [weekly_id],
            )
            self.assertEqual(public_package.unresolved_conflicts, ())
            self.assertNotIn(daily_id, json.dumps(public_package.to_data()))

    def test_interrupted_revision_restores_the_previous_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="old-state",
                digest="Project Atlas review cadence is weekly.",
                task="old-state",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "old-state")["proposal_id"],
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="new-state",
                digest="Project Atlas review cadence is monthly.",
                task="new-state",
            )
            revision = propose(instance_root, "new-state")

            interrupted = run_cli(
                "review-memory",
                cast(str, revision["proposal_id"]),
                f"revise {memory_id} because: corrected by newer evidence",
                "--root",
                str(instance_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "integration-review-after-database"
                },
            )

            self.assertEqual(interrupted.returncode, 86)
            why = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(why.returncode, 0, why.stderr)
            audit = json.loads(why.stdout)
            self.assertEqual(audit["current_version"], 1)
            self.assertEqual(audit["current_content"], "Project Atlas review cadence is weekly.")
            pending = run_cli(
                "review-memory",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(
                json.loads(pending.stdout)["proposals"][0]["proposal_id"],
                revision["proposal_id"],
            )


if __name__ == "__main__":
    unittest.main()
