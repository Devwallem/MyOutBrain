from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from typing import cast

from myoutbrain.memory_gateway import MemoryGateway
from tests.cli_support import run_cli


def create_source_backed_memory(
    temporary_root: Path,
    instance_root: Path,
    *,
    stem: str,
    name: str,
    body: str,
    scope: str = "memory lifecycle",
) -> dict[str, object]:
    source_path = temporary_root / f"{stem}.md"
    source_path.write_text(f"Evidence for {body}\n", encoding="utf-8")
    proposed = run_cli(
        "propose-source-memory",
        str(source_path),
        "--name",
        name,
        "--body",
        body,
        "--scope",
        scope,
        "--idempotency-key",
        f"propose-{stem}",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if proposed.returncode != 0:
        raise AssertionError(proposed.stderr)
    proposal = cast(dict[str, object], json.loads(proposed.stdout))
    approved = run_cli(
        "approve-source-memory",
        cast(str, proposal["proposal_id"]),
        "--expected-version",
        "0",
        "--idempotency-key",
        f"approve-{stem}",
        "--entrance",
        "codex",
        "--root",
        str(instance_root),
        "--format",
        "json",
    )
    if approved.returncode != 0:
        raise AssertionError(approved.stderr)
    return cast(dict[str, object], json.loads(approved.stdout))


class V2MemoryLifecycleTests(unittest.TestCase):
    def test_historically_trusted_memory_remains_recallable_with_its_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            initialized = run_cli("init", "--root", str(instance_root))
            memory = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="historicize",
                name="Historic release rule",
                body="The historic release rule required two maintainers.",
            )
            memory_id = cast(str, cast(dict[str, object], memory["memory"])["memory_id"])

            historicized = run_cli(
                "historicize-memory",
                memory_id,
                "--reason",
                "The rule lacks evidence that it is still current.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "historicize-rule-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "historicize-memory",
                memory_id,
                "--reason",
                "The rule lacks evidence that it is still current.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "historicize-rule-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "Historic release rule",
                "--task",
                "historical-recall",
                "--entrance",
                "codex",
                "--answerable",
                "false",
                "--answerability-reason",
                "freshness-insufficient",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            explained = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(historicized.returncode, 0, historicized.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            transition = json.loads(historicized.stdout)
            self.assertEqual(json.loads(repeated.stdout), transition)
            self.assertEqual(
                transition,
                {
                    "memory_id": memory_id,
                    "version": 1,
                    "from_state": "current",
                    "to_state": "historical-trusted",
                    "reason": "The rule lacks evidence that it is still current.",
                    "audit_event": transition["audit_event"],
                },
            )
            self.assertEqual(
                transition["audit_event"]["event_type"],
                "memory.historicized",
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            package = json.loads(recalled.stdout)
            self.assertEqual(package["memories"][0]["memory_id"], memory_id)
            self.assertEqual(package["memories"][0]["state"], "historical-trusted")
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertEqual(
                json.loads(explained.stdout)["lifecycle_events"][0]["reason"],
                "The rule lacks evidence that it is still current.",
            )

    def test_gateway_revision_keeps_old_version_reason_and_source_relationships(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="revise",
                name="Release review cadence",
                body="Release review happens every Friday.",
            )
            memory = cast(dict[str, object], materialized["memory"])
            source = cast(dict[str, object], materialized["source"])
            memory_id = cast(str, memory["memory_id"])

            revised = run_cli(
                "revise-memory",
                memory_id,
                "--body",
                "Release review happens on the last Friday of each month.",
                "--reason",
                "The approved operating agreement changed the cadence.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "revise-cadence-v2",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated = MemoryGateway(instance_root).revise_v2_memory(
                memory_id,
                body="Release review happens on the last Friday of each month.",
                reason="The approved operating agreement changed the cadence.",
                expected_version=1,
                idempotency_key="revise-cadence-v2",
                entrance="codex",
            )
            self.assertEqual(revised.returncode, 0, revised.stderr)
            revision = cast(dict[str, object], json.loads(revised.stdout))
            explained = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "Release review cadence",
                "--task",
                "revised-recall",
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

            self.assertEqual(repeated, revision)
            self.assertEqual(revision["memory_id"], memory_id)
            self.assertEqual(revision["state"], "current")
            previous_version = cast(dict[str, object], revision["previous_version"])
            current_version = cast(dict[str, object], revision["current_version"])
            self.assertEqual(previous_version["version"], 1)
            self.assertEqual(
                previous_version["body"],
                "Release review happens every Friday.",
            )
            self.assertEqual(
                previous_version["source_ids"],
                [source["source_id"]],
            )
            self.assertEqual(current_version["version"], 2)
            self.assertEqual(
                current_version["source_ids"],
                [source["source_id"]],
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            history = json.loads(explained.stdout)
            self.assertEqual(history["current_version"], 2)
            self.assertEqual(history["versions"][0]["status"], "superseded")
            self.assertEqual(
                history["versions"][0]["supersession_reason"],
                "The approved operating agreement changed the cadence.",
            )
            self.assertEqual(
                history["versions"][0]["source_ids"],
                [source["source_id"]],
            )
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            recalled_memory = json.loads(recalled.stdout)["memories"][0]
            self.assertEqual(recalled_memory["version"], 2)
            self.assertEqual(
                recalled_memory["body"],
                "Release review happens on the last Friday of each month.",
            )

    def test_supersession_keeps_the_replaced_memory_and_relation_out_of_recall(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            replaced = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="superseded-rule",
                name="Original publishing rule",
                body="Every draft must wait seven days before publication.",
            )
            replacement = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="replacement-rule",
                name="Current publishing rule",
                body="A draft may publish after its owner completes review.",
            )
            replaced_id = cast(
                str,
                cast(dict[str, object], replaced["memory"])["memory_id"],
            )
            replacement_id = cast(
                str,
                cast(dict[str, object], replacement["memory"])["memory_id"],
            )

            superseded = run_cli(
                "supersede-memory",
                replaced_id,
                "--replacement-memory-id",
                replacement_id,
                "--replacement-version",
                "1",
                "--reason",
                "The current publishing rule explicitly replaces the waiting period.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "supersede-publishing-rule-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled_old = run_cli(
                "recall-memory",
                "Original publishing rule",
                "--task",
                "superseded-recall",
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
            explained = run_cli(
                "why-memory",
                replaced_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            result = json.loads(superseded.stdout)
            self.assertEqual(result["memory_id"], replaced_id)
            self.assertEqual(result["from_state"], "current")
            self.assertEqual(result["to_state"], "superseded")
            self.assertEqual(
                result["superseded_by"],
                {"memory_id": replacement_id, "version": 1},
            )
            self.assertEqual(
                result["preserved_version"]["body"],
                "Every draft must wait seven days before publication.",
            )
            self.assertEqual(recalled_old.returncode, 0, recalled_old.stderr)
            recalled_ids = {
                item["memory_id"]
                for item in json.loads(recalled_old.stdout)["memories"]
            }
            self.assertNotIn(replaced_id, recalled_ids)
            self.assertIn(replacement_id, recalled_ids)
            self.assertEqual(explained.returncode, 0, explained.stderr)
            superseded_history = json.loads(explained.stdout)
            self.assertEqual(superseded_history["state"], "superseded")
            self.assertEqual(
                superseded_history["lifecycle_events"][0]["reason"],
                "The current publishing rule explicitly replaces the waiting period.",
            )

    def test_deactivation_leaves_recall_and_restores_the_previous_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="deactivate",
                name="Historical deployment note",
                body="The former deployment flow required a release branch.",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            historicized = run_cli(
                "historicize-memory",
                memory_id,
                "--reason",
                "This flow is not confirmed for current deployments.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "historicize-deployment-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
            )
            deactivated = run_cli(
                "deactivate-memory",
                memory_id,
                "--reason",
                "Forget this from ordinary recall, but keep its history.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "deactivate-deployment-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            while_inactive = run_cli(
                "recall-memory",
                "Historical deployment note",
                "--task",
                "inactive-recall",
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
            restored = run_cli(
                "restore-memory",
                memory_id,
                "--reason",
                "Restore the historical record for explicit historical recall.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "restore-deployment-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            after_restore = run_cli(
                "recall-memory",
                "Historical deployment note",
                "--task",
                "restored-recall",
                "--entrance",
                "codex",
                "--answerable",
                "false",
                "--answerability-reason",
                "freshness-insufficient",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(historicized.returncode, 0, historicized.stderr)
            self.assertEqual(deactivated.returncode, 0, deactivated.stderr)
            deactivation = json.loads(deactivated.stdout)
            self.assertEqual(deactivation["from_state"], "historical-trusted")
            self.assertEqual(deactivation["to_state"], "inactive")
            self.assertEqual(deactivation["restorable_state"], "historical-trusted")
            self.assertEqual(while_inactive.returncode, 0, while_inactive.stderr)
            self.assertEqual(json.loads(while_inactive.stdout)["memories"], [])
            self.assertEqual(restored.returncode, 0, restored.stderr)
            restoration = json.loads(restored.stdout)
            self.assertEqual(restoration["from_state"], "inactive")
            self.assertEqual(restoration["to_state"], "historical-trusted")
            self.assertEqual(after_restore.returncode, 0, after_restore.stderr)
            self.assertEqual(
                json.loads(after_restore.stdout)["memories"][0]["state"],
                "historical-trusted",
            )

    def test_permanent_erasure_requires_the_current_impact_closure_and_leaves_a_tombstone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            original_body = "The retired secret launch phrase is violet harbor."
            derivative_body = "The replacement launch procedure no longer uses a phrase."
            original = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="erase-original",
                name="Retired launch phrase",
                body=original_body,
            )
            derivative = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="erase-derivative",
                name="Replacement launch procedure",
                body=derivative_body,
            )
            original_id = cast(
                str,
                cast(dict[str, object], original["memory"])["memory_id"],
            )
            derivative_id = cast(
                str,
                cast(dict[str, object], derivative["memory"])["memory_id"],
            )
            superseded = run_cli(
                "supersede-memory",
                original_id,
                "--replacement-memory-id",
                derivative_id,
                "--replacement-version",
                "1",
                "--reason",
                "The replacement procedure supersedes the secret phrase.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "supersede-before-erasure",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
            )
            previewed = run_cli(
                "erase-memory",
                original_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            preview = json.loads(previewed.stdout)
            wrong_confirmation = run_cli(
                "erase-memory",
                original_id,
                "--confirm",
                "erase_wrong",
                "--root",
                str(instance_root),
            )
            confirmed = run_cli(
                "erase-memory",
                original_id,
                "--confirm",
                preview["confirmation_token"],
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "launch phrase procedure violet harbor",
                "--task",
                "after-erasure",
                "--entrance",
                "codex",
                "--answerable",
                "false",
                "--answerability-reason",
                "coverage-insufficient",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            stale_restore = run_cli(
                "restore-memory",
                original_id,
                "--reason",
                "A stale cache attempted to restore erased content.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "stale-restore-after-erasure",
                "--entrance",
                "old-cache",
                "--root",
                str(instance_root),
            )
            tombstone = run_cli(
                "erase-memory",
                original_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            self.assertEqual(preview["disposition"], "preview")
            self.assertEqual(preview["memory_ids"], [original_id, derivative_id])
            self.assertEqual(preview["derivative_memory_ids"], [derivative_id])
            self.assertEqual(
                preview["dependency_edges"],
                [
                    {
                        "memory_id": derivative_id,
                        "version": 1,
                        "depends_on_memory_id": original_id,
                        "depends_on_version": 1,
                        "relationship": "supersedes",
                    }
                ],
            )
            self.assertEqual(preview["backup_impact"]["future_backups"], "excluded")
            self.assertTrue(preview["requires_confirmation"])
            serialized_preview = json.dumps(preview, ensure_ascii=False)
            self.assertNotIn(original_body, serialized_preview)
            self.assertNotIn(derivative_body, serialized_preview)
            self.assertEqual(wrong_confirmation.returncode, 2)
            self.assertIn("current impact closure", wrong_confirmation.stderr)
            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
            erasure = json.loads(confirmed.stdout)
            self.assertEqual(erasure["disposition"], "erased")
            self.assertEqual(erasure["erased_memory_ids"], [original_id, derivative_id])
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(json.loads(recalled.stdout)["memories"], [])
            self.assertEqual(stale_restore.returncode, 2)
            self.assertIn("permanently erased", stale_restore.stderr)
            self.assertEqual(tombstone.returncode, 0, tombstone.stderr)
            marker = json.loads(tombstone.stdout)
            self.assertEqual(marker["disposition"], "already-erased")
            self.assertNotIn(original_id, json.dumps(marker))
            self.assertNotIn(original_body, json.dumps(marker))

    def test_time_recall_frequency_and_invalid_commands_cannot_change_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="explicit-only",
                name="Explicit lifecycle rule",
                body="Lifecycle changes require an explicit creator decision.",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            for index in range(3):
                recalled = run_cli(
                    "recall-memory",
                    "Explicit lifecycle rule",
                    "--task",
                    f"recall-frequency-{index}",
                    "--entrance",
                    "codex",
                    "--answerable",
                    "true",
                    "--answerability-reason",
                    "covered",
                    "--root",
                    str(instance_root),
                )
                self.assertEqual(recalled.returncode, 0, recalled.stderr)
            expired = run_cli(
                "review-expire",
                "--as-of",
                "2099-01-01T00:00:00+00:00",
                "--root",
                str(instance_root),
            )
            invalid_restore = run_cli(
                "restore-memory",
                memory_id,
                "--reason",
                "This is not inactive.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "invalid-current-restore",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
            )
            explained = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(expired.returncode, 0, expired.stderr)
            self.assertEqual(invalid_restore.returncode, 2)
            self.assertIn("requires an inactive memory", invalid_restore.stderr)
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertEqual(json.loads(explained.stdout)["state"], "current")


if __name__ == "__main__":
    unittest.main()
