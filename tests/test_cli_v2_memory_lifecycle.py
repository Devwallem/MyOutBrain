from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import cast

from myoutbrain.memory_gateway import MemoryGateway
from tests.cli_support import run_cli
from tests.test_cli_memory_evolution import remember_evidence
from tests.test_cli_unified_review import proposal_payload, submit_proposal


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
            rejected_revision = run_cli(
                "revise-memory",
                memory_id,
                "--body",
                "A stale client tried to make historical content current again.",
                "--reason",
                "Exercise the explicit historical transition guard.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "reject-hidden-historical-revision",
                "--entrance",
                "stale-client",
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
            self.assertEqual(rejected_revision.returncode, 2)
            self.assertIn("expected current memory", rejected_revision.stderr)
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertEqual(json.loads(explained.stdout)["state"], "historical-trusted")
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

    def test_legacy_forget_restores_the_historical_state_in_legacy_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="legacy-historical",
                name="Historic compatibility note",
                body="The historic compatibility note applies only to release one.",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            self.assertEqual(
                run_cli(
                    "historicize-memory",
                    memory_id,
                    "--reason",
                    "Release one is no longer current.",
                    "--expected-version",
                    "1",
                    "--idempotency-key",
                    "historicize-legacy-bridge",
                    "--entrance",
                    "codex",
                    "--root",
                    str(instance_root),
                ).returncode,
                0,
            )
            forgotten = run_cli(
                "forget-memory",
                memory_id,
                "forget this",
                "--root",
                str(instance_root),
            )
            restored = run_cli(
                "forget-memory",
                memory_id,
                "restore this",
                "--root",
                str(instance_root),
            )
            recalled = run_cli(
                "recall",
                "Historic compatibility note",
                "--task",
                "legacy-historical-recall",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(forgotten.returncode, 0, forgotten.stderr)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(
                json.loads(recalled.stdout)["items"][0]["memory_state"],
                "historical-trusted",
            )

    def test_v8_inactive_memory_migrates_with_a_restorable_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="inactive-v8-migration",
                name="Migrated inactive note",
                body="An inactive V8 note must remain reversibly restorable.",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    UPDATE canonical_memories
                    SET state = 'inactive', previous_live_state = NULL
                    WHERE memory_id = ?
                    """,
                    (memory_id,),
                )
                connection.execute("PRAGMA user_version = 8")
                connection.commit()

            upgraded = run_cli("init", "--root", str(instance_root))
            restored = run_cli(
                "restore-memory",
                memory_id,
                "--reason",
                "Restore an inactive memory migrated from V8.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "restore-migrated-v8-inactive",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(json.loads(restored.stdout)["to_state"], "current")

    def test_erasure_retains_a_shared_capsule_and_its_unaffected_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            first = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="shared-capsule-first",
                name="Disposable capsule member",
                body="This capsule member may be permanently erased.",
            )
            second = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="shared-capsule-second",
                name="Retained capsule member",
                body="This other capsule member must remain recallable.",
            )
            first_id = cast(str, cast(dict[str, object], first["memory"])["memory_id"])
            second_id = cast(str, cast(dict[str, object], second["memory"])["memory_id"])
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                first_capsule = cast(
                    str,
                    connection.execute(
                        "SELECT primary_capsule_id FROM knowledge_dictionary WHERE memory_id = ?",
                        (first_id,),
                    ).fetchone()[0],
                )
                second_capsule = cast(
                    str,
                    connection.execute(
                        "SELECT primary_capsule_id FROM knowledge_dictionary WHERE memory_id = ?",
                        (second_id,),
                    ).fetchone()[0],
                )
                second_bytes = cast(
                    int,
                    connection.execute(
                        "SELECT body_bytes FROM knowledge_capsules WHERE capsule_id = ?",
                        (second_capsule,),
                    ).fetchone()[0],
                )
                connection.execute(
                    "UPDATE knowledge_dictionary SET primary_capsule_id = ? WHERE memory_id = ?",
                    (first_capsule, second_id),
                )
                connection.execute(
                    "UPDATE canonical_memory_versions SET capsule_id = ? WHERE memory_id = ?",
                    (first_capsule, second_id),
                )
                connection.execute(
                    "UPDATE canonical_memory_fts SET capsule_id = ? WHERE memory_id = ?",
                    (first_capsule, second_id),
                )
                connection.execute(
                    """
                    UPDATE knowledge_capsules
                    SET body_bytes = body_bytes + ?, memory_record_count = 2
                    WHERE capsule_id = ?
                    """,
                    (second_bytes, first_capsule),
                )
                connection.execute(
                    "DELETE FROM capsule_partitions WHERE capsule_id = ?",
                    (second_capsule,),
                )
                connection.execute(
                    "DELETE FROM knowledge_capsules WHERE capsule_id = ?",
                    (second_capsule,),
                )
                connection.commit()

            previewed = run_cli(
                "erase-memory",
                first_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            preview = json.loads(previewed.stdout)
            confirmed = run_cli(
                "erase-memory",
                first_id,
                "--confirm",
                preview["confirmation_token"],
                "--root",
                str(instance_root),
            )
            recalled = run_cli(
                "recall-memory",
                "Retained capsule member",
                "--task",
                "shared-capsule-after-erasure",
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

            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            self.assertEqual(preview["capsule_impacts"][0]["action"], "retain-shared")
            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(json.loads(recalled.stdout)["memories"][0]["memory_id"], second_id)
            with closing(sqlite3.connect(database_path)) as connection:
                capsule = connection.execute(
                    "SELECT memory_record_count FROM knowledge_capsules WHERE capsule_id = ?",
                    (first_capsule,),
                ).fetchone()
            self.assertEqual(capsule, (1,))

    def test_erasure_closes_raw_sources_and_unified_review_cannot_restore_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="erasure-connectors",
                name="Erasure connector memory",
                body="This memory proves every erasure connector is closed.",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            legacy = remember_evidence(
                temporary_root,
                instance_root,
                name="erasure-raw-source",
                digest="Raw source attached to the erasure connector memory.",
                task="erasure-connectors",
            )
            source_id = cast(str, legacy["source_id"])
            database_path = instance_root / "store" / "memory.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                source_row = connection.execute(
                    "SELECT object_reference FROM source_objects WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                self.assertIsNotNone(source_row)
                object_reference = cast(str, source_row[0])
                connection.execute(
                    "INSERT INTO canonical_memory_sources (memory_id, source_id) VALUES (?, ?)",
                    (memory_id, source_id),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_memory_version_sources
                        (memory_id, version, source_id) VALUES (?, 1, ?)
                    """,
                    (memory_id, source_id),
                )
                connection.commit()
            object_path = instance_root / "store" / "objects" / object_reference
            self.assertTrue(object_path.is_file())

            receipt = cast(dict[str, object], materialized["source"])
            pending_payload = proposal_payload(
                intent="derive",
                formation="derived",
                priority="routine",
                title="Pending receipt-derived proposal",
                content="This pending proposal derives from the receipt being erased.",
                effect_type="create_derived_memory",
            )
            pending_payload["supporting_evidence"] = [
                {
                    "kind": "source",
                    "source_id": receipt["source_id"],
                    "source_version": receipt["version"],
                    "locator": receipt["locator"],
                }
            ]
            pending_proposal = submit_proposal(
                instance_root,
                temporary_root / "pending-receipt-proposal.json",
                pending_payload,
                "pending-receipt-proposal",
            )
            target_payload = proposal_payload(
                intent="derive",
                formation="derived",
                priority="routine",
                title="Pending erased-target proposal",
                content="This pending proposal targets the memory being erased.",
                effect_type="create_derived_memory",
            )
            cast(dict[str, object], target_payload["target"])["memory_id"] = memory_id
            target_proposal = submit_proposal(
                instance_root,
                temporary_root / "pending-target-proposal.json",
                target_payload,
                "pending-target-proposal",
            )

            previewed = run_cli(
                "erase-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            preview = json.loads(previewed.stdout)
            confirmed = run_cli(
                "erase-memory",
                memory_id,
                "--confirm",
                preview["confirmation_token"],
                "--root",
                str(instance_root),
            )
            payload = proposal_payload(
                intent="derive",
                formation="derived",
                priority="routine",
                title="Stale erased memory recreation",
                content="A stale client tries to recreate an erased identity.",
                effect_type="create_derived_memory",
            )
            cast(dict[str, object], payload["target"])["memory_id"] = memory_id
            proposal = submit_proposal(
                instance_root,
                temporary_root / "stale-recreation.json",
                payload,
                "stale-erased-recreation-proposal",
            )
            batch_path = temporary_root / "stale-recreation-batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_stale_erased_recreation",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Exercise the erased identity guard.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            decided = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "stale-erased-recreation-batch",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            self.assertEqual(preview["legacy_source_impacts"][0]["action"], "erase-object")
            self.assertTrue(preview["experience_ids"])
            self.assertTrue(preview["digest_ids"])
            self.assertIn(pending_proposal["proposal_id"], preview["proposal_ids"])
            self.assertIn(target_proposal["proposal_id"], preview["proposal_ids"])
            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
            self.assertFalse(object_path.exists())
            self.assertEqual(decided.returncode, 0, decided.stderr)
            outcome = json.loads(decided.stdout)["outcomes"][0]
            self.assertEqual(outcome["status"], "failed")
            self.assertEqual(outcome["error"], "permanently_erased")

    def test_erasure_redacts_an_impacted_outcome_from_a_shared_review_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="shared-review-batch",
                name="Shared batch deletion target",
                body="The original shared-batch memory body.",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            revised_body = "The revised shared-batch body must be erased."
            revision_payload = proposal_payload(
                intent="integrate",
                formation="explicit",
                priority="routine",
                title="Revise shared batch target",
                content=revised_body,
                effect_type="revise_canonical_memory",
            )
            revision_payload["target"] = {
                "memory_id": memory_id,
                "expected_version": 1,
            }
            archive_payload = proposal_payload(
                intent="archive",
                formation="explicit",
                priority="routine",
                title="Unrelated retained archive",
                content="Keep this unrelated creator archive.",
                effect_type="create_human_archive",
            )
            revision = submit_proposal(
                instance_root,
                temporary_root / "shared-revision.json",
                revision_payload,
                "shared-batch-revision",
            )
            archive = submit_proposal(
                instance_root,
                temporary_root / "shared-archive.json",
                archive_payload,
                "shared-batch-archive",
            )
            batch_path = temporary_root / "shared-batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_shared_erasure",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Approve before exercising batch redaction.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                            for proposal in (revision, archive)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            decided = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "shared-erasure-batch",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            previewed = run_cli(
                "erase-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            preview = json.loads(previewed.stdout)
            confirmed = run_cli(
                "erase-memory",
                memory_id,
                "--confirm",
                preview["confirmation_token"],
                "--root",
                str(instance_root),
            )
            replayed = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "shared-erasure-batch",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(decided.returncode, 0, decided.stderr)
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            self.assertEqual(
                preview["review_batch_impacts"],
                [
                    {
                        "batch_id": "bat_shared_erasure",
                        "affected_proposal_ids": [revision["proposal_id"]],
                        "action": "redact-shared",
                    }
                ],
            )
            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            replay = json.loads(replayed.stdout)
            self.assertEqual(
                [outcome["proposal_id"] for outcome in replay["outcomes"]],
                [archive["proposal_id"]],
            )
            self.assertNotIn(memory_id, replayed.stdout)
            self.assertNotIn(revised_body, replayed.stdout)

    def test_legacy_delete_uses_v2_closure_and_removes_views_and_journal_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            secret_body = "The obsolete private launch note says amber lighthouse."
            materialized = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="legacy-delete-v2",
                name="Obsolete private launch note",
                body=secret_body,
            )
            memory_id = cast(
                str,
                cast(dict[str, object], materialized["memory"])["memory_id"],
            )
            built = run_cli("build-views", "--root", str(instance_root))
            manifest = json.loads(
                (instance_root / "runtime" / "knowledge-views" / "manifest.json")
                .read_text(encoding="utf-8")
            )
            view_path = next(
                instance_root / item["path"]
                for item in manifest["views"]
                if item["memory_id"] == memory_id
            )
            self.assertTrue(view_path.is_file())
            forgotten = run_cli(
                "forget-memory",
                memory_id,
                "forget this obsolete private launch note",
                "--root",
                str(instance_root),
            )
            restored = run_cli(
                "forget-memory",
                memory_id,
                "restore this obsolete private launch note",
                "--root",
                str(instance_root),
            )
            previewed = run_cli(
                "delete-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            preview = json.loads(previewed.stdout)
            confirmed = run_cli(
                "delete-memory",
                memory_id,
                "--confirm",
                preview["confirmation_token"],
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(forgotten.returncode, 0, forgotten.stderr)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            self.assertEqual(preview["scope"], "transitive-memory-impact-closure")
            self.assertIn(view_path.relative_to(instance_root).as_posix(), preview["view_paths"])
            self.assertTrue(preview["journal_event_hashes"])
            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
            self.assertEqual(json.loads(confirmed.stdout)["disposition"], "erased")
            self.assertFalse(view_path.exists())
            journal = (
                instance_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8")
            self.assertNotIn(memory_id, journal)
            self.assertNotIn(secret_body, journal)
            self.assertNotIn("obsolete private launch note", journal.casefold())

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
