from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_ask import grounded_response
from tests.test_cli_reflect import initialize_cloud_source
from tests.test_cli_promote import (
    create_another_derived_insight,
    create_derived_insight,
)
from tests.test_cli_reflect import reflection_response


class RebuildRuntimeStateTests(unittest.TestCase):
    def test_creator_can_rebuild_deleted_runtime_and_query_the_same_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            response = grounded_response(
                source_id,
                "Reflection makes accumulated experience reusable.",
            )
            ask_arguments = (
                "ask",
                source_id,
                "What does reflection make reusable?",
                "--allow-cloud",
                "--root",
                str(library_root),
            )
            before = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )
            shutil.rmtree(library_root / "runtime")

            unavailable = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )
            rebuilt = run_cli("rebuild", "--root", str(library_root))
            after = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )

            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertEqual(unavailable.returncode, 2)
            self.assertIn("run myoutbrain rebuild", unavailable.stderr)
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            self.assertEqual(
                rebuilt.stdout,
                "Rebuilt runtime from 1 source, 0 insights, 0 cognitions, "
                "and 0 supersession relationships.\n",
            )
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(after.stdout, before.stdout)

    def test_rebuild_restores_permanent_knowledge_but_not_temporary_candidates(
        self,
    ) -> None:
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
                "Earlier Knowledge",
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )
            first_cognition_match = re.search(
                r"cog_[0-9a-f]{32}", first_promotion.stdout
            )
            first_cognition_id = (
                first_cognition_match.group(0)
                if first_cognition_match is not None
                else ""
            )
            second_promotion = run_cli(
                "promote",
                second_insight_id,
                "--title",
                "Current Knowledge",
                "--supersedes",
                first_cognition_id,
                "--root",
                str(library_root),
                environment={"PATH": ""},
            )
            self.assertEqual(second_promotion.returncode, 0, second_promotion.stderr)
            (library_root / "vault" / "Earlier Knowledge.md").rename(
                library_root / "vault" / "Renamed Earlier Knowledge.md"
            )
            baseline_rebuild = run_cli("rebuild", "--root", str(library_root))
            self.assertEqual(
                baseline_rebuild.returncode,
                0,
                baseline_rebuild.stderr,
            )
            temporary_candidate = run_cli(
                "reflect",
                source_id,
                "Reconsider the source.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(
                        source_id
                    )
                },
            )
            self.assertEqual(temporary_candidate.returncode, 0, temporary_candidate.stderr)
            self.assertIn("Candidate insight", temporary_candidate.stdout)
            response = grounded_response(
                source_id,
                "Reflection makes accumulated experience reusable.",
            )
            ask_arguments = (
                "ask",
                source_id,
                "What does reflection make reusable?",
                "--allow-cloud",
                "--root",
                str(library_root),
            )
            before = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )
            shutil.rmtree(library_root / "runtime")

            rebuilt = run_cli("rebuild", "--root", str(library_root))
            after = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )
            candidates_after = run_cli("review", "--root", str(library_root))

            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            self.assertEqual(rebuilt.stdout, baseline_rebuild.stdout)
            self.assertEqual(
                rebuilt.stdout,
                "Rebuilt runtime from 1 source, 2 insights, 2 cognitions, "
                "and 1 supersession relationships.\n",
            )
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(after.stdout, before.stdout)
            self.assertEqual(candidates_after.returncode, 0, candidates_after.stderr)
            self.assertIn("No candidate insights", candidates_after.stdout)

    def test_rebuild_is_idempotent_and_discards_reclaimable_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            reflected = run_cli(
                "reflect",
                source_id,
                "Create temporary runtime state.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(
                        source_id
                    )
                },
            )
            self.assertEqual(reflected.returncode, 0, reflected.stderr)
            first = run_cli("rebuild", "--root", str(library_root))
            second = run_cli("rebuild", "--root", str(library_root))
            candidates = run_cli("review", "--root", str(library_root))

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout, first.stdout)
            self.assertEqual(candidates.returncode, 0, candidates.stderr)
            self.assertIn("No candidate insights", candidates.stdout)

    def test_corrupt_permanent_event_preserves_valid_records_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            initial_rebuild = run_cli("rebuild", "--root", str(library_root))
            self.assertEqual(initial_rebuild.returncode, 0, initial_rebuild.stderr)
            reflected = run_cli(
                "reflect",
                source_id,
                "Keep this candidate if rebuild fails.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(
                        source_id
                    )
                },
            )
            self.assertEqual(reflected.returncode, 0, reflected.stderr)
            response = grounded_response(
                source_id,
                "Reflection makes accumulated experience reusable.",
            )
            ask_arguments = (
                "ask",
                source_id,
                "What does reflection make reusable?",
                "--allow-cloud",
                "--root",
                str(library_root),
            )
            before = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            with journal_path.open("a", encoding="utf-8") as journal:
                journal.write(
                    json.dumps(
                        {
                            "id": f"evt_{'0' * 32}",
                            "type": "source.captured",
                            "occurred_at": "2026-07-16T00:00:00+00:00",
                        }
                    )
                    + "\n"
                )

            failed = run_cli("rebuild", "--root", str(library_root))
            after = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )
            candidates = run_cli("review", "--root", str(library_root))

            self.assertEqual(failed.returncode, 7)
            self.assertIn("Integrity failure", failed.stderr)
            self.assertIn("events.jsonl", failed.stderr)
            self.assertIn("line", failed.stderr)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(after.stdout, before.stdout)
            self.assertEqual(candidates.returncode, 0, candidates.stderr)
            self.assertIn("Candidate cand_", candidates.stdout)

    def test_interrupted_projection_switch_keeps_previous_runtime_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            initial_rebuild = run_cli("rebuild", "--root", str(library_root))
            self.assertEqual(initial_rebuild.returncode, 0, initial_rebuild.stderr)
            reflected = run_cli(
                "reflect",
                source_id,
                "Keep the previous runtime active.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(
                        source_id
                    )
                },
            )
            self.assertEqual(reflected.returncode, 0, reflected.stderr)
            response = grounded_response(
                source_id,
                "Reflection makes accumulated experience reusable.",
            )
            ask_arguments = (
                "ask",
                source_id,
                "What does reflection make reusable?",
                "--allow-cloud",
                "--root",
                str(library_root),
            )
            before = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )

            interrupted = run_cli(
                "rebuild",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAULT_INJECTION": "rebuild-before-activation"
                },
            )
            candidates = run_cli("review", "--root", str(library_root))
            after = run_cli(
                *ask_arguments,
                environment={"MYOUTBRAIN_FAKE_RESPONSE": response},
            )

            self.assertEqual(interrupted.returncode, 86)
            self.assertEqual(candidates.returncode, 0, candidates.stderr)
            self.assertIn("Candidate cand_", candidates.stdout)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(after.stdout, before.stdout)


if __name__ == "__main__":
    unittest.main()
