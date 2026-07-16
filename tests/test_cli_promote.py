from __future__ import annotations

from pathlib import Path
import json
import re
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_reflect import initialize_cloud_source, reflection_response


def create_derived_insight(root: Path) -> tuple[Path, str, Path, str]:
    library_root, source_id = initialize_cloud_source(root)
    reflected = run_cli(
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
    candidate_match = re.search(r"cand_[0-9a-f]{64}", reflected.stdout)
    if reflected.returncode != 0 or candidate_match is None:
        raise AssertionError(reflected.stderr or reflected.stdout)
    accepted = run_cli(
        "review",
        candidate_match.group(0),
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
    insight_match = re.search(r"ins_[0-9a-f]{32}", accepted.stdout)
    if accepted.returncode != 0 or insight_match is None:
        raise AssertionError(accepted.stderr or accepted.stdout)
    return (
        library_root,
        insight_match.group(0),
        library_root / "vault" / "Reusable Reflection.md",
        source_id,
    )


def create_another_derived_insight(
    library_root: Path,
    source_id: str,
) -> tuple[str, Path]:
    response = json.loads(reflection_response(source_id))
    response["candidates"][0]["text"] = (
        "A changed position should preserve the earlier cognition."
    )
    response["candidates"][0]["derivation"] = "Revises the earlier position."
    reflected = run_cli(
        "reflect",
        source_id,
        "Find a changed position.",
        "--allow-cloud",
        "--root",
        str(library_root),
        environment={
            "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": json.dumps(response)
        },
    )
    candidate_match = re.search(r"cand_[0-9a-f]{64}", reflected.stdout)
    if reflected.returncode != 0 or candidate_match is None:
        raise AssertionError(reflected.stderr or reflected.stdout)
    accepted = run_cli(
        "review",
        candidate_match.group(0),
        "--decision",
        "accept",
        "--title",
        "Changed Position",
        "--sensitivity",
        "cloud-allowed",
        "--root",
        str(library_root),
        environment={"PATH": ""},
    )
    insight_match = re.search(r"ins_[0-9a-f]{32}", accepted.stdout)
    if accepted.returncode != 0 or insight_match is None:
        raise AssertionError(accepted.stderr or accepted.stdout)
    return insight_match.group(0), library_root / "vault" / "Changed Position.md"


class PromotePersonalCognitionTests(unittest.TestCase):
    def test_creator_can_explicitly_promote_an_active_derived_insight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, insight_id, insight_path, source_id = create_derived_insight(
                Path(temporary_directory)
            )

            promoted = run_cli(
                "promote",
                insight_id,
                "--title",
                "Reflection Is Reusable",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )

            self.assertEqual(promoted.returncode, 0, promoted.stderr)
            self.assertIn("Obsidian CLI not found", promoted.stderr)
            cognition_match = re.search(r"cog_[0-9a-f]{32}", promoted.stdout)
            self.assertIsNotNone(cognition_match)
            cognition_id = (
                cognition_match.group(0) if cognition_match is not None else ""
            )
            cognition_path = library_root / "vault" / "Reflection Is Reusable.md"
            cognition = cognition_path.read_text(encoding="utf-8")
            self.assertIn(f"id: {cognition_id}", cognition)
            self.assertIn("kind: cognition", cognition)
            self.assertIn("state: active", cognition)
            self.assertIn("authorship: mixed", cognition)
            self.assertIn("endorsed_by: user", cognition)
            self.assertRegex(cognition, r"(?m)^endorsed_at: .+$")
            self.assertIn(f"derived_from: {insight_id}", cognition)
            self.assertIn(f"  - {source_id}", cognition)
            self.assertIn(
                "Reflection turns experience into reusable guidance.",
                cognition,
            )
            self.assertIn("[[Reusable Reflection]]", cognition)
            insight = insight_path.read_text(encoding="utf-8")
            self.assertIn("kind: insight", insight)
            self.assertIn("state: archived", insight)
            self.assertIn(f"promoted_to: {cognition_id}", insight)
            self.assertIn("[[Reflection Is Reusable]]", insight)
            journal = (
                library_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            event = json.loads(journal.splitlines()[-1])
            self.assertEqual(event["type"], "knowledge.promoted")
            self.assertEqual(event["from_id"], insight_id)
            self.assertEqual(event["to_id"], cognition_id)
            self.assertEqual(event["actor"], "user")

    def test_new_cognition_can_supersede_a_renamed_active_cognition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, first_insight_id, _, source_id = create_derived_insight(
                Path(temporary_directory)
            )
            second_insight_id, _ = create_another_derived_insight(
                library_root,
                source_id,
            )
            first_promotion = run_cli(
                "promote",
                first_insight_id,
                "--title",
                "Earlier Position",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )
            first_cognition_match = re.search(
                r"cog_[0-9a-f]{32}", first_promotion.stdout
            )
            self.assertIsNotNone(first_cognition_match)
            first_cognition_id = (
                first_cognition_match.group(0)
                if first_cognition_match is not None
                else ""
            )
            renamed_path = library_root / "vault" / "Renamed Earlier Position.md"
            (library_root / "vault" / "Earlier Position.md").rename(renamed_path)

            second_promotion = run_cli(
                "promote",
                second_insight_id,
                "--title",
                "Current Position",
                "--supersedes",
                first_cognition_id,
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )

            self.assertEqual(second_promotion.returncode, 0, second_promotion.stderr)
            second_cognition_match = re.search(
                r"cog_[0-9a-f]{32}", second_promotion.stdout
            )
            self.assertIsNotNone(second_cognition_match)
            second_cognition_id = (
                second_cognition_match.group(0)
                if second_cognition_match is not None
                else ""
            )
            old_cognition = renamed_path.read_text(encoding="utf-8")
            self.assertIn("state: superseded", old_cognition)
            self.assertIn(
                f"superseded_by: [{second_cognition_id}]",
                old_cognition,
            )
            new_cognition = (
                library_root / "vault" / "Current Position.md"
            ).read_text(encoding="utf-8")
            self.assertIn(f"supersedes: [{first_cognition_id}]", new_cognition)
            self.assertIn("state: active", new_cognition)
            self.assertTrue(renamed_path.exists())
            events = [
                json.loads(line)
                for line in (
                    library_root / "store" / "journal" / "events.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-2]["type"], "knowledge.promoted")
            self.assertEqual(events[-1]["type"], "knowledge.superseded")
            self.assertEqual(events[-1]["old_id"], first_cognition_id)
            self.assertEqual(events[-1]["new_id"], second_cognition_id)

    def test_interrupted_promotion_recovers_knowledge_and_event_together(self) -> None:
        fault_boundaries = (
            "promote-after-insight-replace",
            "promote-after-cognition-replace",
        )
        for fault_boundary in fault_boundaries:
            with self.subTest(fault_boundary=fault_boundary):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    library_root, insight_id, insight_path, _ = create_derived_insight(
                        Path(temporary_directory)
                    )
                    insight_before = insight_path.read_bytes()
                    journal_path = (
                        library_root / "store" / "journal" / "events.jsonl"
                    )
                    journal_before = journal_path.read_bytes()

                    interrupted = run_cli(
                        "promote",
                        insight_id,
                        "--title",
                        "Interrupted Cognition",
                        "--root",
                        str(library_root),
                        environment={
                            "MYOUTBRAIN_FAULT_INJECTION": fault_boundary,
                            "PATH": "",
                        },
                    )
                    recovered = run_cli("review", "--root", str(library_root))

                    self.assertEqual(interrupted.returncode, 86)
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertEqual(insight_path.read_bytes(), insight_before)
                    self.assertFalse(
                        (library_root / "vault" / "Interrupted Cognition.md").exists()
                    )
                    self.assertEqual(journal_path.read_bytes(), journal_before)
                    self.assertEqual(
                        list((library_root / "store" / "transactions").iterdir()),
                        [],
                    )

    def test_non_active_or_non_insight_targets_leave_knowledge_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, insight_id, _, _ = create_derived_insight(
                Path(temporary_directory)
            )
            first_promotion = run_cli(
                "promote",
                insight_id,
                "--title",
                "Endorsed Reflection",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )
            cognition_match = re.search(r"cog_[0-9a-f]{32}", first_promotion.stdout)
            cognition_id = (
                cognition_match.group(0) if cognition_match is not None else ""
            )
            vault = library_root / "vault"
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            notes_before = {
                path.relative_to(vault): path.read_bytes()
                for path in vault.rglob("*.md")
            }
            journal_before = journal_path.read_bytes()

            archived_insight = run_cli(
                "promote",
                insight_id,
                "--title",
                "Duplicate Endorsement",
                "--root",
                str(library_root),
            )
            cognition_as_insight = run_cli(
                "promote",
                cognition_id,
                "--title",
                "Wrong Kind",
                "--root",
                str(library_root),
            )
            missing_insight = run_cli(
                "promote",
                f"ins_{'0' * 32}",
                "--title",
                "Missing Insight",
                "--root",
                str(library_root),
            )

            self.assertEqual(archived_insight.returncode, 2)
            self.assertIn("requires an active derived insight", archived_insight.stderr)
            self.assertEqual(cognition_as_insight.returncode, 2)
            self.assertIn("invalid derived insight identity", cognition_as_insight.stderr)
            self.assertEqual(missing_insight.returncode, 2)
            self.assertIn("knowledge note does not exist", missing_insight.stderr)
            notes_after = {
                path.relative_to(vault): path.read_bytes()
                for path in vault.rglob("*.md")
            }
            self.assertEqual(notes_after, notes_before)
            self.assertEqual(journal_path.read_bytes(), journal_before)

    def test_invalid_permanent_metadata_is_an_integrity_error_without_mutation(self) -> None:
        invalid_metadata = (
            ("sensitivity: cloud-allowed", "sensitivity: public"),
            ("authorship: system", "authorship: unknown"),
        )
        for existing, invalid in invalid_metadata:
            with self.subTest(invalid=invalid):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    library_root, insight_id, insight_path, _ = create_derived_insight(
                        Path(temporary_directory)
                    )
                    insight_path.write_text(
                        insight_path.read_text(encoding="utf-8").replace(
                            existing,
                            invalid,
                            1,
                        ),
                        encoding="utf-8",
                    )
                    insight_before = insight_path.read_bytes()
                    journal_path = (
                        library_root / "store" / "journal" / "events.jsonl"
                    )
                    journal_before = journal_path.read_bytes()

                    promoted = run_cli(
                        "promote",
                        insight_id,
                        "--title",
                        "Invalid Cognition",
                        "--root",
                        str(library_root),
                    )

                    self.assertEqual(promoted.returncode, 7)
                    self.assertIn("Integrity failure", promoted.stderr)
                    self.assertEqual(insight_path.read_bytes(), insight_before)
                    self.assertFalse(
                        (library_root / "vault" / "Invalid Cognition.md").exists()
                    )
                    self.assertEqual(journal_path.read_bytes(), journal_before)

    def test_interrupted_supersession_restores_both_cognitions_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, first_insight_id, _, source_id = create_derived_insight(
                Path(temporary_directory)
            )
            second_insight_id, second_insight_path = create_another_derived_insight(
                library_root,
                source_id,
            )
            first_promotion = run_cli(
                "promote",
                first_insight_id,
                "--title",
                "Earlier Cognition",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )
            cognition_match = re.search(r"cog_[0-9a-f]{32}", first_promotion.stdout)
            first_cognition_id = (
                cognition_match.group(0) if cognition_match is not None else ""
            )
            first_cognition_path = library_root / "vault" / "Earlier Cognition.md"
            insight_before = second_insight_path.read_bytes()
            cognition_before = first_cognition_path.read_bytes()
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            journal_before = journal_path.read_bytes()

            interrupted = run_cli(
                "promote",
                second_insight_id,
                "--title",
                "Interrupted Successor",
                "--supersedes",
                first_cognition_id,
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "promote-after-superseded-replace",
                    "PATH": "",
                },
            )
            recovered = run_cli("review", "--root", str(library_root))

            self.assertEqual(interrupted.returncode, 86)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(second_insight_path.read_bytes(), insight_before)
            self.assertEqual(first_cognition_path.read_bytes(), cognition_before)
            self.assertFalse(
                (library_root / "vault" / "Interrupted Successor.md").exists()
            )
            self.assertEqual(journal_path.read_bytes(), journal_before)

    def test_wrong_kind_with_insight_identity_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, insight_id, insight_path, _ = create_derived_insight(
                Path(temporary_directory)
            )
            insight_path.write_text(
                insight_path.read_text(encoding="utf-8").replace(
                    "kind: insight",
                    "kind: cognition",
                    1,
                ),
                encoding="utf-8",
            )
            insight_before = insight_path.read_bytes()
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            journal_before = journal_path.read_bytes()

            promoted = run_cli(
                "promote",
                insight_id,
                "--title",
                "Wrong Kind",
                "--root",
                str(library_root),
            )

            self.assertEqual(promoted.returncode, 2)
            self.assertIn("requires an active derived insight", promoted.stderr)
            self.assertEqual(insight_path.read_bytes(), insight_before)
            self.assertEqual(journal_path.read_bytes(), journal_before)

    def test_missing_or_non_active_supersession_target_leaves_no_changes(self) -> None:
        supersession_states = ("missing", "superseded")
        for target_state in supersession_states:
            with self.subTest(target_state=target_state):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    library_root, first_insight_id, _, source_id = (
                        create_derived_insight(Path(temporary_directory))
                    )
                    second_insight_id, second_insight_path = (
                        create_another_derived_insight(library_root, source_id)
                    )
                    first_promotion = run_cli(
                        "promote",
                        first_insight_id,
                        "--title",
                        "Prior Cognition",
                        "--root",
                        str(library_root),
                        environment={"PATH": ""},
                    )
                    cognition_match = re.search(
                        r"cog_[0-9a-f]{32}", first_promotion.stdout
                    )
                    cognition_id = (
                        cognition_match.group(0)
                        if cognition_match is not None
                        else ""
                    )
                    cognition_path = library_root / "vault" / "Prior Cognition.md"
                    target_id = f"cog_{'0' * 32}"
                    if target_state == "superseded":
                        cognition_path.write_text(
                            cognition_path.read_text(encoding="utf-8").replace(
                                "state: active",
                                "state: superseded",
                                1,
                            ),
                            encoding="utf-8",
                        )
                        target_id = cognition_id
                    vault = library_root / "vault"
                    notes_before = {
                        path.relative_to(vault): path.read_bytes()
                        for path in vault.rglob("*.md")
                    }
                    journal_path = (
                        library_root / "store" / "journal" / "events.jsonl"
                    )
                    journal_before = journal_path.read_bytes()

                    promoted = run_cli(
                        "promote",
                        second_insight_id,
                        "--title",
                        "Invalid Successor",
                        "--supersedes",
                        target_id,
                        "--root",
                        str(library_root),
                    )

                    self.assertEqual(promoted.returncode, 2)
                    notes_after = {
                        path.relative_to(vault): path.read_bytes()
                        for path in vault.rglob("*.md")
                    }
                    self.assertEqual(notes_after, notes_before)
                    self.assertEqual(
                        second_insight_path.read_bytes(),
                        notes_before[second_insight_path.relative_to(vault)],
                    )
                    self.assertEqual(journal_path.read_bytes(), journal_before)


if __name__ == "__main__":
    unittest.main()
