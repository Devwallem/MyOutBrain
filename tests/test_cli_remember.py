from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tests.cli_support import run_cli


class CaptureBufferedMemoryTests(unittest.TestCase):
    def test_creator_can_record_visible_conversation_as_buffered_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            body = (
                "We agreed that the companion must preserve explicit context gaps. "
                "That boundary prevents later answers from pretending unseen history was visible."
            )
            conversation.write_text(body, encoding="utf-8")

            result = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T09:30:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "implement-01",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current task transcript",
                "--context-gap",
                "messages before the visible task are unavailable",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["disposition"], "buffered")
            self.assertRegex(receipt["source_id"], r"^src_[0-9a-f]{64}$")
            self.assertRegex(receipt["experience_id"], r"^exp_[0-9a-f]{64}$")
            self.assertRegex(receipt["digest_id"], r"^mem_[0-9a-f]{64}$")
            self.assertEqual(receipt["state"], "buffered")
            self.assertIsNone(receipt["canonical_memory_id"])
            self.assertEqual(receipt["occurred_at"], "2026-07-17T09:30:00+08:00")
            self.assertEqual(receipt["entrance"], "codex")
            self.assertEqual(receipt["task"], "implement-01")
            self.assertEqual(receipt["sensitivity"], "local-only")
            self.assertEqual(receipt["visible_context"], "current task transcript")
            self.assertEqual(
                receipt["context_gaps"],
                ["messages before the visible task are unavailable"],
            )
            self.assertNotEqual(receipt["digest"], body)
            self.assertNotIn(body, receipt["digest"])

            stored_objects = tuple(
                path
                for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            self.assertEqual(len(stored_objects), 1)
            self.assertEqual(stored_objects[0].read_text(encoding="utf-8"), body)
            events = [
                json.loads(line)
                for line in (instance_root / "store" / "journal" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "memory.buffered")
            self.assertNotIn(body, json.dumps(events[0]))

    def test_same_body_from_different_tasks_reuses_source_but_keeps_each_experience(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text(
                "One conversation can inform multiple tasks without being copied.",
                encoding="utf-8",
            )

            common = (
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T10:00:00+08:00",
                "--sensitivity",
                "cloud-allowed",
                "--visible-context",
                "the current conversation",
                "--context-gap",
                "no earlier task history was visible",
                "--format",
                "json",
            )
            first = run_cli(
                *common,
                "--entrance",
                "codex",
                "--task",
                "draft-design",
            )
            second = run_cli(
                *common,
                "--entrance",
                "cli",
                "--task",
                "review-design",
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_receipt = json.loads(first.stdout)
            second_receipt = json.loads(second.stdout)
            self.assertEqual(first_receipt["source_id"], second_receipt["source_id"])
            self.assertNotEqual(
                first_receipt["experience_id"], second_receipt["experience_id"]
            )
            self.assertNotEqual(first_receipt["digest_id"], second_receipt["digest_id"])
            stored_objects = tuple(
                path
                for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                if path.is_file()
            )
            self.assertEqual(len(stored_objects), 1)
            events = (
                instance_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 2)

    def test_exact_resubmission_is_idempotent_across_reinitialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text(
                "An exact retry must not multiply buffered memory.",
                encoding="utf-8",
            )
            arguments = (
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T10:30:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "retry-safe-capture",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current task",
                "--context-gap",
                "earlier tasks unavailable",
                "--format",
                "json",
            )

            first = run_cli(*arguments)
            reinitialization = run_cli("init", "--root", str(instance_root))
            second = run_cli(*arguments)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(reinitialization.returncode, 0, reinitialization.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_receipt = json.loads(first.stdout)
            second_receipt = json.loads(second.stdout)
            self.assertEqual(first_receipt["disposition"], "buffered")
            self.assertEqual(second_receipt["disposition"], "duplicate")
            self.assertEqual(first_receipt["experience_id"], second_receipt["experience_id"])
            self.assertEqual(first_receipt["digest_id"], second_receipt["digest_id"])
            events = (
                instance_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 1)

    def test_interrupted_submission_recovers_without_partial_memory_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialization = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            conversation = temporary_root / "conversation.txt"
            conversation.write_text(
                "The source, relation, digest, and audit must commit together.",
                encoding="utf-8",
            )
            arguments = (
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T11:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "transactional-capture",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current failure test",
                "--context-gap",
                "all prior conversations unavailable",
                "--format",
                "json",
            )

            interrupted = run_cli(
                *arguments,
                environment={"MYOUTBRAIN_FAULT_INJECTION": "remember-after-first-replace"},
            )
            recovery = run_cli("init", "--root", str(instance_root))

            self.assertEqual(interrupted.returncode, 86)
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            self.assertEqual(
                tuple(
                    path
                    for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                    if path.is_file()
                ),
                (),
            )
            journal_path = instance_root / "store" / "journal" / "events.jsonl"
            self.assertFalse(journal_path.exists())

            retry = run_cli(*arguments)

            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertEqual(json.loads(retry.stdout)["disposition"], "buffered")
            self.assertEqual(
                len(
                    tuple(
                        path
                        for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                        if path.is_file()
                    )
                ),
                1,
            )
            self.assertEqual(len(journal_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_invalid_time_or_blank_context_gap_leaves_memory_empty(self) -> None:
        invalid_values = (
            ("not-a-time", "explicit gap"),
            ("2026-07-17T11:30:00+08:00", "   "),
        )
        for occurred_at, context_gap in invalid_values:
            with self.subTest(occurred_at=occurred_at, context_gap=context_gap), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                instance_root = temporary_root / "Private Companion"
                initialization = run_cli("init", "--root", str(instance_root))
                self.assertEqual(initialization.returncode, 0, initialization.stderr)
                conversation = temporary_root / "conversation.txt"
                conversation.write_text("Potential memory.", encoding="utf-8")

                result = run_cli(
                    "remember",
                    str(conversation),
                    "--root",
                    str(instance_root),
                    "--occurred-at",
                    occurred_at,
                    "--entrance",
                    "codex",
                    "--task",
                    "validation",
                    "--sensitivity",
                    "local-only",
                    "--visible-context",
                    "current task",
                    "--context-gap",
                    context_gap,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("Invalid source", result.stderr)
                self.assertEqual(
                    tuple(
                        path
                        for path in (instance_root / "store" / "objects" / "sha256").rglob("*")
                        if path.is_file()
                    ),
                    (),
                )
                self.assertFalse(
                    (instance_root / "store" / "journal" / "events.jsonl").exists()
                )


if __name__ == "__main__":
    unittest.main()
