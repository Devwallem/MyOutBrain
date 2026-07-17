from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_memory_evolution import accept_new, propose, remember_evidence


class MemoryGovernanceTests(unittest.TestCase):
    def test_forget_defaults_to_reversible_deactivation_with_an_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            evidence = remember_evidence(
                temporary_root,
                instance_root,
                name="reversible-memory",
                digest="Project Cedar review cadence is weekly.",
                task="reversible-memory",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "reversible-memory")["proposal_id"],
            )

            forgotten = run_cli(
                "forget-memory",
                memory_id,
                "forget this",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            hidden = run_cli(
                "recall",
                "Project Cedar review cadence",
                "--root",
                str(instance_root),
                "--task",
                "cedar-audit",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--format",
                "json",
            )
            inactive_audit = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(forgotten.returncode, 0, forgotten.stderr)
            forgotten_result = json.loads(forgotten.stdout)
            self.assertEqual(forgotten_result["action"], "deactivated")
            self.assertEqual(forgotten_result["memory_id"], memory_id)
            self.assertEqual(hidden.returncode, 0, hidden.stderr)
            self.assertEqual(json.loads(hidden.stdout)["items"], [])
            self.assertEqual(inactive_audit.returncode, 0, inactive_audit.stderr)
            audit = json.loads(inactive_audit.stdout)
            self.assertEqual(audit["state"], "inactive")
            self.assertEqual(audit["current_source_ids"], [evidence["source_id"]])
            self.assertEqual(audit["current_version"], 1)
            self.assertEqual(audit["lifecycle_events"][-1]["action"], "deactivated")

            restored = run_cli(
                "forget-memory",
                memory_id,
                "restore this",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            visible = run_cli(
                "recall",
                "Project Cedar review cadence",
                "--root",
                str(instance_root),
                "--task",
                "cedar-audit",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--format",
                "json",
            )

            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(json.loads(restored.stdout)["action"], "reactivated")
            visible_items = json.loads(visible.stdout)["items"]
            self.assertEqual([item["memory_id"] for item in visible_items], [memory_id])

    def test_permanent_deletion_requires_an_exact_bounded_impact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            evidence = remember_evidence(
                temporary_root,
                instance_root,
                name="deletion-preview",
                digest="Project Birch review cadence is weekly.",
                task="deletion-preview",
            )
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "deletion-preview")["proposal_id"],
            )

            previewed = run_cli(
                "delete-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["disposition"], "preview")
            self.assertEqual(preview["memory_id"], memory_id)
            self.assertEqual(preview["source_ids"], [evidence["source_id"]])
            self.assertEqual(preview["canonical_memory_count"], 1)
            self.assertEqual(preview["scope"], "one-canonical-memory")
            self.assertRegex(
                preview["confirmation_token"],
                r"^delete_[0-9a-f]{64}$",
            )

            rejected = run_cli(
                "delete-memory",
                memory_id,
                "--confirm",
                "delete_wrong",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            still_visible = run_cli(
                "recall",
                "Project Birch review cadence",
                "--root",
                str(instance_root),
                "--task",
                "deletion-check",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--format",
                "json",
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertIn("confirmation", rejected.stderr.casefold())
            self.assertEqual(still_visible.returncode, 0, still_visible.stderr)
            self.assertEqual(
                [item["memory_id"] for item in json.loads(still_visible.stdout)["items"]],
                [memory_id],
            )

    def test_confirmed_permanent_deletion_cascades_and_cannot_be_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            conversation = temporary_root / "delete-me.txt"
            secret_text = "Project Elm private launch phrase is silver lantern."
            conversation.write_text(
                f"Private planning record. {secret_text} End of record.",
                encoding="utf-8",
            )
            remembered = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-17T12:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "delete-elm",
                "--digest",
                secret_text,
                "--sensitivity",
                "local-only",
                "--visible-context",
                "permanent deletion acceptance",
                "--context-gap",
                "earlier history unavailable",
                "--format",
                "json",
            )
            self.assertEqual(remembered.returncode, 0, remembered.stderr)
            receipt = json.loads(remembered.stdout)
            memory_id = accept_new(
                instance_root,
                propose(instance_root, "delete-elm")["proposal_id"],
            )
            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            old_view = instance_root / json.loads(built.stdout)["view_paths"][0]
            previewed = run_cli(
                "delete-memory",
                memory_id,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            token = json.loads(previewed.stdout)["confirmation_token"]

            deleted = run_cli(
                "delete-memory",
                memory_id,
                "--confirm",
                token,
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall",
                "silver lantern",
                "--root",
                str(instance_root),
                "--task",
                "post-delete",
                "--access",
                "local-trusted",
                "--format",
                "json",
            )
            rebuilt = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            reimported = run_cli(
                "remember",
                str(conversation),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-18T12:00:00+08:00",
                "--entrance",
                "codex",
                "--task",
                "reimport-elm",
                "--digest",
                secret_text,
                "--sensitivity",
                "local-only",
                "--visible-context",
                "attempted reimport",
                "--context-gap",
                "earlier history unavailable",
                "--format",
                "json",
            )

            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            deletion = json.loads(deleted.stdout)
            self.assertEqual(deletion["disposition"], "deleted")
            self.assertEqual(deletion["memory_id"], memory_id)
            self.assertEqual(deletion["removed_source_ids"], [receipt["source_id"]])
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertEqual(json.loads(recalled.stdout)["items"], [])
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            self.assertEqual(json.loads(rebuilt.stdout)["view_count"], 0)
            self.assertFalse(old_view.exists())
            self.assertNotEqual(reimported.returncode, 0)
            self.assertIn("permanently deleted", reimported.stderr)
            object_files = [
                path
                for path in (instance_root / "store" / "objects").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(object_files, [])


if __name__ == "__main__":
    unittest.main()
