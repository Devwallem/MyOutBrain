from __future__ import annotations

from pathlib import Path
import json
import re
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_reflect import (
    candidate_records,
    initialize_cloud_source,
    reflection_response,
)


class ReviewCandidateInsightsTests(unittest.TestCase):
    def test_creator_can_list_candidates_with_evidence_and_recurrence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )
            self.assertEqual(reflection.returncode, 0, reflection.stderr)
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            self.assertIsNotNone(candidate_identity)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )

            result = run_cli("review", "--root", str(library_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(candidate_id, result.stdout)
            self.assertIn(
                "Reflection turns experience into reusable guidance.",
                result.stdout,
            )
            self.assertIn(f"Supporting evidence: [{source_id} @", result.stdout)
            self.assertIn("Contrary evidence: none", result.stdout)
            self.assertIn("Derivation: Generalizes the source.", result.stdout)
            self.assertIn("Occurrences: 1", result.stdout)

    def test_creator_can_defer_candidate_without_changing_or_promoting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )
            catalog_path = (
                library_root
                / "runtime"
                / "workspace"
                / "candidates"
                / "catalog.json"
            )
            catalog_before = catalog_path.read_bytes()

            result = run_cli(
                "review",
                candidate_id,
                "--decision",
                "defer",
                "--root",
                str(library_root),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Deferred candidate {candidate_id}", result.stdout)
            self.assertEqual(catalog_path.read_bytes(), catalog_before)
            self.assertEqual(list((library_root / "vault").glob("*.md")), [])
            journal = (
                library_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            events = [json.loads(line) for line in journal.splitlines()]
            decision = events[-1]
            self.assertEqual(decision["type"], "candidate.reviewed")
            self.assertEqual(decision["candidate_id"], candidate_id)
            self.assertEqual(decision["decision"], "defer")

    def test_creator_can_reject_candidate_and_suppress_immediate_repetition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            environment = {
                "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
            }
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment=environment,
            )
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )

            rejected = run_cli(
                "review",
                candidate_id,
                "--decision",
                "reject",
                "--root",
                str(library_root),
            )
            repeated = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment=environment,
            )
            active = run_cli("review", "--root", str(library_root))

            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertIn(f"Rejected candidate {candidate_id}", rejected.stdout)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("recently rejected", repeated.stdout)
            self.assertIn("No candidate insights", active.stdout)
            rejection_files = list(
                (
                    library_root
                    / "runtime"
                    / "workspace"
                    / "candidates"
                    / "rejected"
                ).glob("rej_*.json")
            )
            self.assertEqual(len(rejection_files), 1)
            rejection = rejection_files[0].read_text(encoding="utf-8")
            self.assertIn('"fingerprint": "sha256:', rejection)
            self.assertNotIn("Reflection turns experience", rejection)
            journal = (
                library_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            decision = json.loads(journal.splitlines()[-2])
            self.assertEqual(decision["type"], "candidate.reviewed")
            self.assertEqual(decision["decision"], "reject")
            self.assertIn("candidate_fingerprint", decision)
            self.assertNotIn("Reflection turns experience", journal)

    def test_creator_can_accept_candidate_as_derived_insight_without_obsidian(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )

            accepted = run_cli(
                "review",
                candidate_id,
                "--decision",
                "accept",
                "--title",
                "Reusable Reflection",
                "--sensitivity",
                "cloud-allowed",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("Accepted derived insight ins_", accepted.stdout)
            self.assertIn("Obsidian CLI not found", accepted.stderr)
            notes = list((library_root / "vault").glob("*.md"))
            self.assertEqual([note.name for note in notes], ["Reusable Reflection.md"])
            note = notes[0].read_text(encoding="utf-8")
            self.assertRegex(note, r"(?m)^id: ins_[0-9a-f]{32}$")
            self.assertIn("kind: insight", note)
            self.assertNotIn("kind: cognition", note)
            self.assertIn("state: active", note)
            self.assertIn("authorship: system", note)
            self.assertIn("sensitivity: cloud-allowed", note)
            self.assertIn(f"  - {source_id}", note)
            self.assertIn(f"candidate_id: {candidate_id}", note)
            self.assertIn("# Reusable Reflection", note)
            self.assertIn(
                "Reflection turns experience into reusable guidance.",
                note,
            )
            self.assertEqual(candidate_records(library_root), [])
            journal = (
                library_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            decision = json.loads(journal.splitlines()[-1])
            self.assertEqual(decision["type"], "candidate.reviewed")
            self.assertEqual(decision["decision"], "accept")
            self.assertRegex(decision["knowledge_id"], r"^ins_[0-9a-f]{32}$")

    def test_creator_can_edit_then_accept_with_mixed_authorship_and_open_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )
            request_path = library_root / "obsidian-request.json"

            accepted = run_cli(
                "review",
                candidate_id,
                "--decision",
                "accept",
                "--title",
                "A Creator's Reflection",
                "--text",
                "My edited, durable reflection.",
                "--sensitivity",
                "local-only",
                "--root",
                str(library_root),
                environment={"MYOUTBRAIN_FAKE_OBSIDIAN_REQUEST": str(request_path)},
            )

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(accepted.stderr, "")
            note_path = library_root / "vault" / "A Creator's Reflection.md"
            note = note_path.read_text(encoding="utf-8")
            self.assertIn("authorship: mixed", note)
            self.assertIn("sensitivity: local-only", note)
            self.assertIn("My edited, durable reflection.", note)
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(
                request["command"],
                ["obsidian", "open", "path=A Creator's Reflection.md"],
            )
            self.assertEqual(Path(request["cwd"]), library_root / "vault")
            decision = json.loads(
                (library_root / "store" / "journal" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(decision["authorship"], "mixed")

    def test_accepting_into_an_existing_note_is_rejected_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )
            catalog_path = (
                library_root
                / "runtime"
                / "workspace"
                / "candidates"
                / "catalog.json"
            )
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            conflicting_note = library_root / "vault" / "Existing Note.md"
            conflicting_note.write_text("existing\n", encoding="utf-8")
            catalog_before = catalog_path.read_bytes()
            journal_before = journal_path.read_bytes()

            rejected = run_cli(
                "review",
                candidate_id,
                "--decision",
                "accept",
                "--title",
                "existing note",
                "--sensitivity",
                "cloud-allowed",
                "--root",
                str(library_root),
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertIn("knowledge note already exists", rejected.stderr)
            self.assertEqual(catalog_path.read_bytes(), catalog_before)
            self.assertEqual(journal_path.read_bytes(), journal_before)
            self.assertEqual(conflicting_note.read_text(encoding="utf-8"), "existing\n")

    def test_interrupted_acceptance_recovers_candidate_note_and_event_together(self) -> None:
        fault_boundaries = (
            "review-after-first-replace",
            "review-after-note-replace",
        )
        for fault_boundary in fault_boundaries:
            with self.subTest(fault_boundary=fault_boundary):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    library_root, source_id = initialize_cloud_source(
                        Path(temporary_directory)
                    )
                    reflection = run_cli(
                        "reflect",
                        source_id,
                        "Find a reusable insight.",
                        "--allow-cloud",
                        "--root",
                        str(library_root),
                        environment={
                            "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(
                                source_id
                            )
                        },
                    )
                    candidate_identity = re.search(
                        r"cand_[0-9a-f]{64}", reflection.stdout
                    )
                    candidate_id = (
                        candidate_identity.group(0)
                        if candidate_identity is not None
                        else ""
                    )
                    journal_path = (
                        library_root / "store" / "journal" / "events.jsonl"
                    )
                    journal_before = journal_path.read_bytes()

                    interrupted = run_cli(
                        "review",
                        candidate_id,
                        "--decision",
                        "accept",
                        "--title",
                        "Interrupted Insight",
                        "--sensitivity",
                        "cloud-allowed",
                        "--root",
                        str(library_root),
                        environment={"MYOUTBRAIN_FAULT_INJECTION": fault_boundary},
                    )
                    recovered = run_cli("review", "--root", str(library_root))

                    self.assertEqual(interrupted.returncode, 86)
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertIn(candidate_id, recovered.stdout)
                    self.assertFalse(
                        (library_root / "vault" / "Interrupted Insight.md").exists()
                    )
                    self.assertEqual(journal_path.read_bytes(), journal_before)
                    transactions = library_root / "store" / "transactions"
                    self.assertEqual(list(transactions.iterdir()), [])

    def test_unchanged_text_option_keeps_system_authorship(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            response = reflection_response(source_id)
            reflection = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={"MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": response},
            )
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", reflection.stdout)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )
            candidate_text = json.loads(response)["candidates"][0]["text"]

            accepted = run_cli(
                "review",
                candidate_id,
                "--decision",
                "accept",
                "--title",
                "Unchanged Reflection",
                "--text",
                f"  {candidate_text}  ",
                "--sensitivity",
                "cloud-allowed",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            note = (
                library_root / "vault" / "Unchanged Reflection.md"
            ).read_text(encoding="utf-8")
            self.assertIn("authorship: system", note)


if __name__ == "__main__":
    unittest.main()
