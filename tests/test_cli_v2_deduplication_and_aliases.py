from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import cast
import unittest

from tests.cli_support import run_cli


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AssertionError("expected an object with string keys")
    return cast(dict[str, object], value)


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [_dict(item) for item in value]


class V2DeduplicationAndAliasTests(unittest.TestCase):
    def test_resubmitting_the_same_source_and_memory_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Recovery.md"
            source_path.write_text(
                "Restore a cold snapshot into a new directory before verification.\n",
                encoding="utf-8",
            )
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal = self._propose(
                instance_root,
                source_path,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="proposal-recovery-v1",
            )
            self._approve(instance_root, proposal, key="approve-recovery-v1")

            repeated = self._propose(
                instance_root,
                source_path,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="proposal-recovery-repeated",
            )
            recalled = self._recall(instance_root, "Cold snapshot recovery")
            reused_key = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Cold snapshot recovery",
                "--body",
                "Different semantic content.",
                "--scope",
                "different scope",
                "--idempotency-key",
                "proposal-recovery-repeated",
                "--root",
                str(instance_root),
            )

            self.assertEqual(repeated["disposition"], "unchanged")
            self.assertEqual(repeated["memory_id"], proposal["planned_memory_id"])
            self.assertIsNone(repeated["proposal_id"])
            memories = _dict_list(recalled["memories"])
            self.assertEqual(len(memories), 1)
            memory = memories[0]
            self.assertEqual(memory["memory_id"], proposal["planned_memory_id"])
            self.assertEqual(memory["version"], 1)
            self.assertEqual(_dict(memory["evidence"])["source_count"], 1)

            self.assertEqual(reused_key.returncode, 2)
            self.assertIn("different request", reused_key.stderr)
    def test_a_new_source_for_the_same_memory_adds_only_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            first_source = temporary_root / "Recovery handbook.md"
            second_source = temporary_root / "Operations checklist.md"
            first_source.write_text("Primary recovery guidance.\n", encoding="utf-8")
            second_source.write_text("Independent recovery evidence.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal = self._propose(
                instance_root,
                first_source,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="proposal-primary-recovery",
            )
            self._approve(instance_root, proposal, key="approve-primary-recovery")

            supported = self._propose(
                instance_root,
                second_source,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="support-secondary-recovery",
            )
            retried = self._propose(
                instance_root,
                second_source,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="support-secondary-recovery",
            )
            recalled = self._recall(instance_root, "Cold snapshot recovery")

            self.assertEqual(retried, supported)
            self.assertEqual(supported["disposition"], "source-linked")
            self.assertEqual(supported["memory_id"], proposal["planned_memory_id"])
            self.assertEqual(supported["relationship"], "supports-added")
            self.assertIsNone(supported["proposal_id"])
            memories = _dict_list(recalled["memories"])
            self.assertEqual(len(memories), 1)
            memory = memories[0]
            self.assertEqual(memory["version"], 1)
            self.assertEqual(_dict(memory["evidence"])["source_count"], 2)

    def test_pending_exact_submission_reuses_the_existing_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Pending.md"
            source_path.write_text("Pending recovery evidence.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)

            first = self._propose(
                instance_root,
                source_path,
                name="Pending recovery rule",
                body="Restore a snapshot into a new directory before verification.",
                scope="storage recovery",
                key="pending-recovery-first",
            )
            repeated = self._propose(
                instance_root,
                source_path,
                name="Pending recovery rule",
                body="restore   a snapshot into a new directory before verification.",
                scope="STORAGE   RECOVERY",
                key="pending-recovery-second",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            reused_key = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Pending recovery rule",
                "--body",
                "A different pending rule.",
                "--scope",
                "different scope",
                "--idempotency-key",
                "pending-recovery-second",
                "--root",
                str(instance_root),
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            queue = _dict(json.loads(listed.stdout))

            self.assertEqual(first["disposition"], "proposal-created")
            self.assertEqual(repeated["disposition"], "proposal-reused")
            self.assertEqual(repeated["proposal_id"], first["proposal_id"])
            self.assertEqual(repeated["planned_memory_id"], first["planned_memory_id"])
            self.assertEqual(
                [item["proposal_id"] for item in _dict_list(queue["proposals"])],
                [first["proposal_id"]],
            )

            self.assertEqual(reused_key.returncode, 2)
            self.assertIn("different request", reused_key.stderr)
    def test_new_source_merges_into_an_exact_pending_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            first_source = temporary_root / "First pending.md"
            second_source = temporary_root / "Second pending.md"
            first_source.write_text("First pending evidence.\n", encoding="utf-8")
            second_source.write_text("Second pending evidence.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)

            first = self._propose(
                instance_root,
                first_source,
                name="Pending recovery rule",
                body="Restore a snapshot into a new directory before verification.",
                scope="storage recovery",
                key="pending-first-source",
            )
            merged = self._propose(
                instance_root,
                second_source,
                name="Pending recovery rule",
                body="Restore a snapshot into a new directory before verification.",
                scope="storage recovery",
                key="pending-second-source",
            )
            repeated = self._propose(
                instance_root,
                second_source,
                name="Pending recovery rule",
                body="Restore a snapshot into a new directory before verification.",
                scope="storage recovery",
                key="pending-second-source-repeat",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            queue = _dict(json.loads(listed.stdout))
            proposals = _dict_list(queue["proposals"])

            self.assertEqual(merged["disposition"], "proposal-reused")
            self.assertEqual(repeated["disposition"], "proposal-reused")
            self.assertEqual(merged["proposal_id"], first["proposal_id"])
            self.assertEqual(repeated["proposal_id"], first["proposal_id"])
            self.assertNotEqual(
                _dict(merged["source"])["source_id"],
                _dict(first["source"])["source_id"],
            )
            self.assertEqual(merged["proposal_version"], 2)
            self.assertEqual(repeated["proposal_version"], 2)
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["proposal_version"], 2)
            self.assertEqual(
                len(cast(list[object], proposals[0]["supporting_evidence"])),
                2,
            )

            self._approve(instance_root, merged, key="approve-merged-pending")
            recalled = self._recall(instance_root, "Pending recovery rule")
            memories = _dict_list(recalled["memories"])
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["version"], 1)
            self.assertEqual(
                _dict(memories[0]["evidence"])["source_count"],
                2,
            )
    def test_near_content_with_a_different_scope_creates_a_supplement_proposal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            first_source = temporary_root / "Recovery handbook.md"
            near_source = temporary_root / "Windows recovery.md"
            first_source.write_text("Primary recovery guidance.\n", encoding="utf-8")
            near_source.write_text("Platform-specific recovery guidance.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal = self._propose(
                instance_root,
                first_source,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="proposal-general-recovery",
            )
            self._approve(instance_root, proposal, key="approve-general-recovery")

            supplement = self._propose(
                instance_root,
                near_source,
                name="Cold snapshot recovery on Windows",
                body=(
                    "Restore a cold snapshot into a new directory before verification "
                    "on Windows."
                ),
                scope="Windows storage recovery",
                key="proposal-windows-recovery",
            )
            queue_result = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(queue_result.returncode, 0, queue_result.stderr)
            queue = _dict(json.loads(queue_result.stdout))
            recalled = self._recall(instance_root, "Cold snapshot recovery")

            self.assertEqual(supplement["disposition"], "proposal-created")
            self.assertEqual(supplement["suggested_action"], "supplement")
            self.assertEqual(supplement["target_memory_id"], proposal["planned_memory_id"])
            queued = next(
                item
                for item in _dict_list(queue["proposals"])
                if item["proposal_id"] == supplement["proposal_id"]
            )
            self.assertEqual(
                _dict(queued["approval_effect"])["type"], "revise_canonical_memory"
            )
            self.assertEqual(
                queued["target"],
                {"memory_id": proposal["planned_memory_id"], "expected_version": 1},
            )
            memories = _dict_list(recalled["memories"])
            self.assertEqual(len(memories), 1)
            memory = memories[0]
            self.assertEqual(memory["version"], 1)
            self.assertEqual(
                memory["body"],
                "Restore a cold snapshot into a new directory before verification.",
            )
            self.assertEqual(_dict(memory["evidence"])["source_count"], 1)

    def test_conflict_is_grouped_reviewable_and_not_falsely_approvable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            first_source = temporary_root / "Always.md"
            conflict_source = temporary_root / "Never.md"
            batch_path = temporary_root / "batch.json"
            first_source.write_text("Positive restore policy.\n", encoding="utf-8")
            conflict_source.write_text("Conflicting restore policy.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            original = self._propose(
                instance_root,
                first_source,
                name="Snapshot restore location",
                body=(
                    "Backups must always be restored into a new directory before "
                    "verification."
                ),
                scope="storage recovery",
                key="proposal-always-restore",
            )
            self._approve(instance_root, original, key="approve-always-restore")

            conflict = self._propose(
                instance_root,
                conflict_source,
                name="Snapshot restore location",
                body=(
                    "Backups must never be restored into a new directory before "
                    "verification."
                ),
                scope="storage recovery",
                key="proposal-never-restore",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            queue = _dict(json.loads(listed.stdout))
            queued = next(
                item
                for item in _dict_list(queue["proposals"])
                if item["proposal_id"] == conflict["proposal_id"]
            )
            group = next(
                item
                for item in _dict_list(queue["groups"])
                if conflict["proposal_id"] in cast(list[object], item["proposal_ids"])
            )

            self.assertEqual(conflict["suggested_action"], "conflict")
            self.assertIsNone(conflict["approval_effect"])
            self.assertEqual(conflict["available_decisions"], ["reject", "defer"])
            self.assertEqual(queued["available_decisions"], ["reject", "defer"])
            self.assertIsNone(queued["approval_effect"])
            self.assertEqual(group["kind"], "conflict")
            self.assertIn(original["proposal_id"], cast(list[object], group["proposal_ids"]))
            relation = _dict(cast(list[object], group["relations"])[0])
            self.assertEqual(relation["type"], "conflict")
            self.assertEqual(
                set(cast(list[str], relation["proposal_ids"])),
                {original["proposal_id"], conflict["proposal_id"]},
            )

            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_conflict_approve",
                        "decisions": [
                            {
                                "proposal_id": conflict["proposal_id"],
                                "proposal_version": conflict["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Exercise the unsupported boundary.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            attempted = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "conflict-approve-attempt",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(attempted.returncode, 0, attempted.stderr)
            attempt = _dict(json.loads(attempted.stdout))
            outcome = _dict(_dict_list(attempt["outcomes"])[0])
            self.assertEqual(attempt["status"], "failed")
            self.assertIn(
                "conflict_approval_materialization_deferred_to_issue_09",
                cast(str, outcome["error"]),
            )

            recalled = self._recall(instance_root, "Snapshot restore location")
            memories = _dict_list(recalled["memories"])
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["version"], 1)
            self.assertEqual(
                memories[0]["body"],
                "Backups must always be restored into a new directory before verification.",
            )
            self.assertEqual(_dict(memories[0]["evidence"])["source_count"], 1)

            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_conflict_defer",
                        "decisions": [
                            {
                                "proposal_id": conflict["proposal_id"],
                                "proposal_version": conflict["proposal_version"],
                                "decision": "defer",
                                "edited_content": None,
                                "reason": "Await issue 09.",
                                "defer_until": "2030-01-01T00:00:00+00:00",
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            deferred = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "conflict-defer",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(deferred.returncode, 0, deferred.stderr)
            deferred_data = _dict(json.loads(deferred.stdout))
            self.assertEqual(_dict_list(deferred_data["outcomes"])[0]["status"], "deferred")

    def test_renaming_back_to_an_old_alias_keeps_direct_non_cyclic_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Recovery.md"
            source_path.write_text("Recovery naming guidance.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposal = self._propose(
                instance_root,
                source_path,
                name="Cold snapshot recovery",
                body="Restore a cold snapshot into a new directory before verification.",
                scope="storage recovery",
                key="proposal-rename-recovery",
            )
            self._approve(instance_root, proposal, key="approve-rename-recovery")
            memory_id = proposal["planned_memory_id"]
            self.assertIsInstance(memory_id, str)
            normalized_memory_id = cast(str, memory_id)

            first_rename = self._rename(
                instance_root,
                normalized_memory_id,
                name="Snapshot restore rule",
                key="rename-recovery-to-rule",
            )
            second_rename = self._rename(
                instance_root,
                normalized_memory_id,
                name="Cold snapshot recovery",
                key="rename-rule-back-to-recovery",
            )
            old_name_recall = self._recall(instance_root, "Snapshot restore rule")
            current_name_recall = self._recall(instance_root, "Cold snapshot recovery")

            self.assertEqual(first_rename["canonical_name"], "Snapshot restore rule")
            self.assertEqual(second_rename["canonical_name"], "Cold snapshot recovery")
            self.assertEqual(second_rename["memory_id"], normalized_memory_id)
            self.assertEqual(second_rename["current_version"], 1)
            self.assertEqual(second_rename["aliases"], ["Snapshot restore rule"])
            for package in (old_name_recall, current_name_recall):
                self.assertFalse(_dict(package["signals"])["ambiguity"])
                memories = _dict_list(package["memories"])
                self.assertEqual(len(memories), 1)
                self.assertEqual(memories[0]["memory_id"], normalized_memory_id)
                self.assertEqual(memories[0]["version"], 1)

    def test_same_name_in_different_domains_returns_partitioned_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            algebra_source = temporary_root / "Algebra.md"
            jewelry_source = temporary_root / "Jewelry.md"
            algebra_source.write_text("Abstract algebra reference.\n", encoding="utf-8")
            jewelry_source.write_text("Jewelry sizing reference.\n", encoding="utf-8")
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            algebra = self._propose(
                instance_root,
                algebra_source,
                name="Ring",
                body="An algebraic ring combines addition with multiplication.",
                scope="abstract algebra",
                key="proposal-algebra-ring",
            )
            self._approve(instance_root, algebra, key="approve-algebra-ring")
            jewelry = self._propose(
                instance_root,
                jewelry_source,
                name="Ring",
                body="A jewelry band is fitted by circumference and material.",
                scope="jewelry design",
                key="proposal-jewelry-ring",
            )
            self._approve(instance_root, jewelry, key="approve-jewelry-ring")

            recalled = self._recall(instance_root, "Ring")

            self.assertTrue(_dict(recalled["signals"])["ambiguity"])
            memories = _dict_list(recalled["memories"])
            self.assertEqual(
                {memory["memory_id"] for memory in memories},
                {algebra["planned_memory_id"], jewelry["planned_memory_id"]},
            )
            self.assertEqual(
                {_dict(memory["partition"])["summary"] for memory in memories},
                {"abstract algebra", "jewelry design"},
            )
            for memory in memories:
                self.assertRegex(
                    cast(str, _dict(memory["partition"])["partition_id"]),
                    r"^prt_[0-9a-f]{32}$",
                )
                self.assertIn("dictionary", cast(list[object], memory["candidate_paths"]))

    def _propose(
        self,
        instance_root: Path,
        source_path: Path,
        *,
        name: str,
        body: str,
        scope: str,
        key: str,
    ) -> dict[str, object]:
        result = run_cli(
            "propose-source-memory",
            str(source_path),
            "--name",
            name,
            "--body",
            body,
            "--scope",
            scope,
            "--idempotency-key",
            key,
            "--root",
            str(instance_root),
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return _dict(json.loads(result.stdout))

    def _approve(
        self,
        instance_root: Path,
        proposal: dict[str, object],
        *,
        key: str,
    ) -> dict[str, object]:
        proposal_id = proposal["proposal_id"]
        self.assertIsInstance(proposal_id, str)
        result = run_cli(
            "approve-source-memory",
            cast(str, proposal_id),
            "--expected-version",
            "0",
            "--idempotency-key",
            key,
            "--entrance",
            "codex",
            "--root",
            str(instance_root),
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return _dict(json.loads(result.stdout))

    def _recall(self, instance_root: Path, question: str) -> dict[str, object]:
        result = run_cli(
            "recall-memory",
            question,
            "--task",
            "deduplication-black-box",
            "--entrance",
            "codex",
            "--answerable",
            "true",
            "--answerability-reason",
            "covered",
            "--root",
            str(instance_root),
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return _dict(json.loads(result.stdout))

    def _rename(
        self,
        instance_root: Path,
        memory_id: str,
        *,
        name: str,
        key: str,
    ) -> dict[str, object]:
        result = run_cli(
            "rename-memory",
            memory_id,
            "--name",
            name,
            "--expected-version",
            "1",
            "--idempotency-key",
            key,
            "--entrance",
            "codex",
            "--root",
            str(instance_root),
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return _dict(json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
