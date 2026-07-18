from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from tests.cli_support import run_cli
from tests.test_cli_unified_review import proposal_payload, submit_proposal
from tests.test_cli_v2_memory_lifecycle import create_source_backed_memory


def rewrite_valid_package(
    source: Path,
    destination: Path,
    *,
    remove_evidence: bool,
) -> None:
    with ZipFile(source) as package:
        entries = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(entries.pop("manifest.json"))
    relationships = json.loads(entries["relationships.json"])
    if remove_evidence:
        relationships["evidence_relationships"] = []
        manifest["closure"]["evidence_relationships"] = []
    entries["relationships.json"] = json.dumps(
        relationships, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    manifest["objects"] = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        for path, content in sorted(entries.items())
    ]
    manifest.pop("package_id")
    unsigned = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    manifest["package_id"] = "pkg_" + hashlib.sha256(unsigned).hexdigest()
    entries["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as package:
        for path, content in sorted(entries.items()):
            package.writestr(path, content)


class V2MigrationTests(unittest.TestCase):
    def test_export_contains_the_transitive_audited_knowledge_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "source-instance"
            package_path = temporary_root / "portable-knowledge.zip"
            initialized = run_cli("init", "--root", str(instance_root))
            earlier = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="earlier",
                name="Paper approval archive",
                body="The archived workflow used signed paper forms.",
                scope="historical approval archive",
            )
            replacement = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="replacement",
                name="Current release rule",
                body="Release candidates require two maintainers.",
                scope="current software release policy",
            )
            earlier_id = cast(
                str, cast(dict[str, object], earlier["memory"])["memory_id"]
            )
            replacement_id = cast(
                str, cast(dict[str, object], replacement["memory"])["memory_id"]
            )
            superseded = run_cli(
                "supersede-memory",
                earlier_id,
                "--replacement-memory-id",
                replacement_id,
                "--replacement-version",
                "1",
                "--reason",
                "The two-maintainer rule replaced the earlier rule.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "supersede-before-transfer",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            planned = run_cli(
                "migration-plan",
                "--memory-id",
                replacement_id,
                "--target",
                "portable-target",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                replacement_id,
                "--target",
                "portable-target",
                "--expected-version",
                "0",
                "--idempotency-key",
                "export-current-release-rule-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            repeated_export = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                replacement_id,
                "--target",
                "portable-target",
                "--expected-version",
                "0",
                "--idempotency-key",
                "export-current-release-rule-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = json.loads(planned.stdout)
            self.assertTrue(plan["allowed"])
            self.assertEqual(plan["selected_memory_ids"], [replacement_id])
            self.assertEqual(
                set(plan["closure"]["memory_ids"]),
                {earlier_id, replacement_id},
            )
            self.assertEqual(len(plan["closure"]["source_versions"]), 2)
            self.assertEqual(
                plan["closure"]["knowledge_relationships"],
                [
                    {
                        "from_memory_id": replacement_id,
                        "from_version": 1,
                        "relationship": "supersedes",
                        "to_memory_id": earlier_id,
                        "to_version": 1,
                    }
                ],
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            export_result = json.loads(exported.stdout)
            self.assertEqual(repeated_export.returncode, 0, repeated_export.stderr)
            self.assertEqual(json.loads(repeated_export.stdout), export_result)
            self.assertEqual(export_result["checkpoint_version"], 1)
            self.assertTrue(package_path.is_file())

            with ZipFile(package_path) as package:
                names = set(package.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("relationships.json", names)
                self.assertTrue(any(name.startswith("objects/memories/") for name in names))
                self.assertTrue(any(name.startswith("objects/sources/") for name in names))
                self.assertFalse(any("sqlite" in name.casefold() for name in names))
                manifest = json.loads(package.read("manifest.json"))
                relationships = json.loads(package.read("relationships.json"))

            self.assertEqual(manifest["format"], "myoutbrain-migration")
            self.assertEqual(manifest["format_version"], 1)
            self.assertEqual(manifest["package_id"], export_result["package_id"])
            self.assertEqual(manifest["checkpoint"]["previous"], None)
            self.assertEqual(
                relationships["knowledge_relationships"],
                plan["closure"]["knowledge_relationships"],
            )

    def test_restricted_dependency_blocks_the_whole_export_and_reports_its_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "source-instance"
            package_path = temporary_root / "must-not-exist.zip"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            evidence_memory = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="licensed-evidence",
                name="Licensed evidence receipt",
                body="The licensed report records the measured result.",
                scope="licensed evidence",
            )
            source = cast(dict[str, object], evidence_memory["source"])
            payload = proposal_payload(
                intent="integrate",
                formation="explicit",
                priority="priority",
                title="Restricted operational conclusion",
                content="The measured result guides the restricted operation.",
                effect_type="create_canonical_memory",
            )
            payload["supporting_evidence"] = [
                {
                    "kind": "source",
                    "source_id": source["source_id"],
                    "source_version": source["version"],
                    "locator": source["locator"],
                }
            ]
            payload["migration_restrictions"] = ["company-confidential"]
            proposal = submit_proposal(
                instance_root,
                payload_path,
                payload,
                "restricted-migration-proposal",
            )
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_restricted_migration",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Keep the restriction with the evidence.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            approved = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "approve-restricted-migration",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            outcome = json.loads(approved.stdout)["outcomes"][0]
            restricted_memory_id = outcome["materialization"]["memory_id"]

            planned = run_cli(
                "migration-plan",
                "--memory-id",
                restricted_memory_id,
                "--target",
                "personal-machine",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                restricted_memory_id,
                "--target",
                "personal-machine",
                "--expected-version",
                "0",
                "--idempotency-key",
                "blocked-restricted-export",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = json.loads(planned.stdout)
            self.assertFalse(plan["allowed"])
            self.assertEqual(
                plan["blockers"],
                [
                    {
                        "kind": "restricted-dependency",
                        "path": (
                            f"target:personal-machine -> memory:{restricted_memory_id}/v1 -> "
                            f"proposal:{proposal['proposal_id']} -> "
                            "restriction:company-confidential"
                        ),
                        "reason": "the provenance explicitly restricts migration",
                    }
                ],
            )
            self.assertEqual(exported.returncode, 2)
            self.assertIn(plan["blockers"][0]["path"], exported.stderr)
            self.assertFalse(package_path.exists())
            replanned = json.loads(
                run_cli(
                    "migration-plan",
                    "--memory-id",
                    restricted_memory_id,
                    "--target",
                    "personal-machine",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            self.assertEqual(replanned["checkpoint_version"], 0)

    def test_dry_run_and_repeated_import_are_hash_checked_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            package_path = temporary_root / "increment.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            source_memory = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="portable",
                name="Portable review rule",
                body="Review imported conflicts before changing local knowledge.",
                scope="portable migration",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], source_memory["memory"])["memory_id"],
            )
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "0",
                "--idempotency-key",
                "export-portable-rule",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
                "--format",
                "json",
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)

            dry_run = run_cli(
                "migration-import-dry-run",
                str(package_path),
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            preview = json.loads(dry_run.stdout)
            self.assertEqual(preview["status"], "ready")
            self.assertEqual(
                preview["checks"],
                {
                    "audited_closure": "passed",
                    "format_version": "passed",
                    "manifest_hash": "passed",
                    "object_hashes": "passed",
                },
            )
            self.assertEqual(preview["changes"]["new_memory_ids"], [memory_id])
            self.assertEqual(len(preview["changes"]["new_source_versions"]), 1)
            self.assertEqual(preview["target_checkpoint_version"], 0)

            imported = run_cli(
                "migration-import",
                str(package_path),
                "--expected-version",
                "0",
                "--idempotency-key",
                "import-portable-rule",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "migration-import",
                str(package_path),
                "--expected-version",
                "0",
                "--idempotency-key",
                "import-portable-rule",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall-memory",
                "Portable review rule",
                "--task",
                "verify-imported-knowledge",
                "--entrance",
                "claude-code",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            after = run_cli(
                "migration-import-dry-run",
                str(package_path),
                "--root",
                str(target_root),
                "--format",
                "json",
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            first_result = json.loads(imported.stdout)
            repeated_result = json.loads(repeated.stdout)
            self.assertEqual(first_result["disposition"], "imported")
            self.assertEqual(first_result["checkpoint_version"], 1)
            self.assertEqual(repeated_result["disposition"], "already-imported")
            self.assertEqual(repeated_result["checkpoint_version"], 1)
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            recall = json.loads(recalled.stdout)
            self.assertEqual(recall["memories"][0]["memory_id"], memory_id)
            self.assertEqual(recall["memories"][0]["version"], 1)
            self.assertEqual(after.returncode, 0, after.stderr)
            after_preview = json.loads(after.stdout)
            self.assertEqual(after_preview["status"], "already-imported")
            self.assertEqual(after_preview["target_checkpoint_version"], 1)
            self.assertEqual(after_preview["changes"]["exact_memory_ids"], [memory_id])

    def test_dry_run_reports_existing_source_content_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            package_path = temporary_root / "same-content.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            body = "Identical source bytes retain distinct stable source receipts."
            source_memory = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="source-hash-origin",
                name="Source hash origin",
                body=body,
                scope="source hash reuse",
            )
            target_memory = create_source_backed_memory(
                temporary_root,
                target_root,
                stem="source-hash-target",
                name="Source hash target",
                body=body,
                scope="source hash reuse",
            )
            memory_id = cast(
                str, cast(dict[str, object], source_memory["memory"])["memory_id"]
            )
            source_receipt = cast(dict[str, object], source_memory["source"])
            target_receipt = cast(dict[str, object], target_memory["source"])
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "0",
                "--idempotency-key",
                "export-same-source-hash",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)

            previewed = run_cli(
                "migration-import-dry-run",
                str(package_path),
                "--root",
                str(target_root),
                "--format",
                "json",
            )

            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["status"], "ready")
            self.assertEqual(
                preview["changes"]["reused_source_hashes"],
                [
                    {
                        "incoming_source_id": source_receipt["source_id"],
                        "incoming_version": source_receipt["version"],
                        "existing_source_id": target_receipt["source_id"],
                        "existing_version": target_receipt["version"],
                        "content_hash": source_receipt["content_hash"],
                    }
                ],
            )

    def test_divergent_target_memory_creates_one_review_proposal_without_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            first_package = temporary_root / "initial.zip"
            divergent_package = temporary_root / "divergent.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            source_memory = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="divergence",
                name="Shared migration policy",
                body="Every migration starts with a dry run.",
                scope="migration conflict review",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], source_memory["memory"])["memory_id"],
            )
            self.assertEqual(
                run_cli(
                    "migration-export",
                    str(first_package),
                    "--memory-id",
                    memory_id,
                    "--target",
                    "target-instance",
                    "--expected-version",
                    "0",
                    "--idempotency-key",
                    "export-shared-v1",
                    "--entrance",
                    "codex",
                    "--root",
                    str(source_root),
                ).returncode,
                0,
            )
            self.assertEqual(
                run_cli(
                    "migration-import",
                    str(first_package),
                    "--expected-version",
                    "0",
                    "--idempotency-key",
                    "import-shared-v1",
                    "--entrance",
                    "opencode",
                    "--root",
                    str(target_root),
                ).returncode,
                0,
            )
            source_revision = run_cli(
                "revise-memory",
                memory_id,
                "--body",
                "Every migration starts with a verified dry run and hash report.",
                "--reason",
                "The source instance adopted explicit hash reporting.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "revise-source-v2",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            target_revision = run_cli(
                "revise-memory",
                memory_id,
                "--body",
                "Every migration starts with a locally approved preview.",
                "--reason",
                "The target instance adopted a different local policy.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "revise-target-v2",
                "--entrance",
                "claude-code",
                "--root",
                str(target_root),
            )
            self.assertEqual(source_revision.returncode, 0, source_revision.stderr)
            self.assertEqual(target_revision.returncode, 0, target_revision.stderr)
            exported = run_cli(
                "migration-export",
                str(divergent_package),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "1",
                "--idempotency-key",
                "export-shared-v2",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
                "--format",
                "json",
            )
            previewed = run_cli(
                "migration-import-dry-run",
                str(divergent_package),
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["status"], "conflict-review")
            self.assertEqual(preview["changes"]["conflict_memory_ids"], [memory_id])

            imported = run_cli(
                "migration-import",
                str(divergent_package),
                "--expected-version",
                "1",
                "--idempotency-key",
                "import-divergent-v2",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "migration-import",
                str(divergent_package),
                "--expected-version",
                "1",
                "--idempotency-key",
                "import-divergent-v2",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            explained = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(target_root),
                "--format",
                "json",
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            import_result = json.loads(imported.stdout)
            self.assertEqual(import_result["disposition"], "conflict-proposed")
            self.assertEqual(import_result["checkpoint_version"], 1)
            self.assertEqual(len(import_result["conflict_proposal_ids"]), 1)
            self.assertEqual(
                json.loads(repeated.stdout)["checkpoint_version"],
                import_result["checkpoint_version"],
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            proposals = json.loads(listed.stdout)["proposals"]
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            self.assertEqual(
                proposal["proposal_id"], import_result["conflict_proposal_ids"][0]
            )
            self.assertEqual(proposal["intent"], "integrate")
            self.assertEqual(proposal["priority"], "blocking")
            self.assertEqual(proposal["target"], {"memory_id": memory_id, "expected_version": 2})
            self.assertEqual(
                proposal["content"],
                "Every migration starts with a verified dry run and hash report.",
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            audit = json.loads(explained.stdout)
            self.assertEqual(audit["current_version"], 2)
            self.assertEqual(
                audit["current_content"],
                "Every migration starts with a locally approved preview.",
            )

    def test_dependency_on_a_conflicting_version_pauses_without_partial_import(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            initial_package = temporary_root / "initial.zip"
            dependent_package = temporary_root / "dependent.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            dependency = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="conflicting-dependency",
                name="Conflicting dependency",
                body="The initial dependency is shared by both instances.",
                scope="dependency conflict migration",
            )
            dependency_id = cast(
                str, cast(dict[str, object], dependency["memory"])["memory_id"]
            )
            self.assertEqual(
                run_cli(
                    "migration-export",
                    str(initial_package),
                    "--memory-id",
                    dependency_id,
                    "--target",
                    "target-instance",
                    "--expected-version",
                    "0",
                    "--idempotency-key",
                    "export-conflict-base",
                    "--entrance",
                    "codex",
                    "--root",
                    str(source_root),
                ).returncode,
                0,
            )
            self.assertEqual(
                run_cli(
                    "migration-import",
                    str(initial_package),
                    "--expected-version",
                    "0",
                    "--idempotency-key",
                    "import-conflict-base",
                    "--entrance",
                    "opencode",
                    "--root",
                    str(target_root),
                ).returncode,
                0,
            )
            source_revision = run_cli(
                "revise-memory",
                dependency_id,
                "--body",
                "The source dependency now requires a signed transfer receipt.",
                "--reason",
                "Source policy changed.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "revise-source-dependency",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            target_revision = run_cli(
                "revise-memory",
                dependency_id,
                "--body",
                "The target dependency now requires local approval.",
                "--reason",
                "Target policy changed independently.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "revise-target-dependency",
                "--entrance",
                "claude-code",
                "--root",
                str(target_root),
            )
            self.assertEqual(source_revision.returncode, 0, source_revision.stderr)
            self.assertEqual(target_revision.returncode, 0, target_revision.stderr)
            dependent = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="blocked-dependent",
                name="Blocked dependent",
                body="This knowledge requires the source dependency version.",
                scope="dependency conflict migration",
            )
            dependent_id = cast(
                str, cast(dict[str, object], dependent["memory"])["memory_id"]
            )
            superseded = run_cli(
                "supersede-memory",
                dependency_id,
                "--replacement-memory-id",
                dependent_id,
                "--replacement-version",
                "1",
                "--reason",
                "The dependent now relies on the source-side version.",
                "--expected-version",
                "2",
                "--idempotency-key",
                "link-conflicting-dependency",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            exported = run_cli(
                "migration-export",
                str(dependent_package),
                "--memory-id",
                dependent_id,
                "--target",
                "target-instance",
                "--expected-version",
                "1",
                "--idempotency-key",
                "export-blocked-dependent",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)

            previewed = run_cli(
                "migration-import-dry-run",
                str(dependent_package),
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            imported = run_cli(
                "migration-import",
                str(dependent_package),
                "--expected-version",
                "1",
                "--idempotency-key",
                "propose-blocked-dependent",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            repeated = run_cli(
                "migration-import",
                str(dependent_package),
                "--expected-version",
                "1",
                "--idempotency-key",
                "propose-blocked-dependent",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )

            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["status"], "conflict-review")
            self.assertEqual(preview["blockers"][0]["kind"], "dependency-conflict")
            self.assertEqual(imported.returncode, 0, imported.stderr)
            result = json.loads(imported.stdout)
            self.assertEqual(result["disposition"], "conflict-proposed")
            self.assertEqual(result["checkpoint_version"], 1)
            self.assertEqual(len(result["conflict_proposal_ids"]), 1)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                json.loads(repeated.stdout)["conflict_proposal_ids"],
                result["conflict_proposal_ids"],
            )
            listed = run_cli(
                "review-list",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            proposal = json.loads(listed.stdout)["proposals"][0]
            batch_path = temporary_root / "resolve-dependency-conflict.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_resolve_migration_dependency",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Accept the source-side dependency version.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            approved = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "approve-migration-dependency-conflict",
                "--entrance",
                "codex",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            completed = run_cli(
                "migration-import",
                str(dependent_package),
                "--expected-version",
                "1",
                "--idempotency-key",
                "propose-blocked-dependent",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            completed_result = json.loads(completed.stdout)
            self.assertEqual(completed_result["disposition"], "imported")
            self.assertEqual(completed_result["checkpoint_version"], 2)
            with closing(sqlite3.connect(target_root / "store" / "memory.sqlite3")) as db:
                dependency_row = db.execute(
                    """
                    SELECT depends_on_version FROM canonical_memory_dependencies
                    WHERE memory_id = ? AND version = 1
                      AND depends_on_memory_id = ?
                    """,
                    (dependent_id, dependency_id),
                ).fetchone()
                self.assertEqual(
                    dependency_row,
                    (3,),
                )

    def test_incremental_source_version_extends_an_unmodified_imported_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            first_package = temporary_root / "v1.zip"
            second_package = temporary_root / "v2.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            source_memory = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="incremental",
                name="Incremental transfer rule",
                body="Transfer one audited version at a time.",
                scope="incremental migration",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], source_memory["memory"])["memory_id"],
            )
            first_export = run_cli(
                "migration-export",
                str(first_package),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "0",
                "--idempotency-key",
                "incremental-export-v1",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            first_import = run_cli(
                "migration-import",
                str(first_package),
                "--expected-version",
                "0",
                "--idempotency-key",
                "incremental-import-v1",
                "--entrance",
                "opencode",
                "--root",
                str(target_root),
            )
            self.assertEqual(first_export.returncode, 0, first_export.stderr)
            self.assertEqual(first_import.returncode, 0, first_import.stderr)
            revised = run_cli(
                "revise-memory",
                memory_id,
                "--body",
                "Transfer each audited version with its predecessor checkpoint.",
                "--reason",
                "Make the incremental checkpoint explicit.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "incremental-source-revision-v2",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            second_export = run_cli(
                "migration-export",
                str(second_package),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "1",
                "--idempotency-key",
                "incremental-export-v2",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            previewed = run_cli(
                "migration-import-dry-run",
                str(second_package),
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(revised.returncode, 0, revised.stderr)
            self.assertEqual(second_export.returncode, 0, second_export.stderr)
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(preview["changes"]["updated_memory_ids"], [memory_id])
            self.assertEqual(preview["changes"]["conflict_memory_ids"], [])

            imported = run_cli(
                "migration-import",
                str(second_package),
                "--expected-version",
                "1",
                "--idempotency-key",
                "incremental-import-v2",
                "--entrance",
                "claude-code",
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            explained = run_cli(
                "why-memory",
                memory_id,
                "--root",
                str(target_root),
                "--format",
                "json",
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(json.loads(imported.stdout)["conflict_proposal_ids"], [])
            self.assertEqual(explained.returncode, 0, explained.stderr)
            audit = json.loads(explained.stdout)
            self.assertEqual(audit["current_version"], 2)
            self.assertEqual(len(audit["versions"]), 2)
            self.assertEqual(
                audit["current_content"],
                "Transfer each audited version with its predecessor checkpoint.",
            )

    def test_all_agent_clients_use_the_same_gateway_migration_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "source-instance"
            request_path = temporary_root / "gateway-request.json"
            run_cli("init", "--root", str(instance_root))
            source_memory = create_source_backed_memory(
                temporary_root,
                instance_root,
                stem="gateway-portable",
                name="Gateway migration contract",
                body="Every agent entrance uses the same migration plan.",
                scope="agent-neutral migration",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], source_memory["memory"])["memory_id"],
            )
            results: list[dict[str, object]] = []
            for client_name in ("codex", "opencode", "claude-code"):
                request_path.write_text(
                    json.dumps(
                        {
                            "protocol": {
                                "minimum": {"major": 2, "minor": 0},
                                "maximum": {"major": 2, "minor": 1},
                            },
                            "client": {
                                "name": client_name,
                                "capabilities": ["migration_plan.v1"],
                            },
                            "operation": "migration.plan",
                            "parameters": {
                                "memory_ids": [memory_id],
                                "target": "portable-target",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                response = run_cli(
                    "gateway",
                    str(request_path),
                    "--root",
                    str(instance_root),
                )
                self.assertEqual(response.returncode, 0, response.stderr)
                envelope = json.loads(response.stdout)
                self.assertTrue(envelope["ok"])
                results.append(envelope["result"])

            self.assertEqual(results[0], results[1])
            self.assertEqual(results[1], results[2])
            self.assertTrue(results[0]["allowed"])
            self.assertEqual(results[0]["selected_memory_ids"], [memory_id])

    def test_dry_run_rejects_unsupported_versions_and_tampered_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            package_path = temporary_root / "valid.zip"
            version_path = temporary_root / "unsupported.zip"
            tampered_path = temporary_root / "tampered.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            source_memory = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="integrity",
                name="Migration integrity rule",
                body="Reject a package whose declared hash no longer matches.",
                scope="migration integrity",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], source_memory["memory"])["memory_id"],
            )
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "0",
                "--idempotency-key",
                "integrity-export",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            with ZipFile(package_path) as package:
                entries = {name: package.read(name) for name in package.namelist()}
            unsupported_manifest = json.loads(entries["manifest.json"])
            unsupported_manifest["format_version"] = 99
            with ZipFile(version_path, "w") as package:
                for name, content in entries.items():
                    package.writestr(
                        name,
                        (
                            json.dumps(unsupported_manifest).encode("utf-8")
                            if name == "manifest.json"
                            else content
                        ),
                    )
            object_path = next(
                name for name in entries if name.startswith("objects/memories/")
            )
            with ZipFile(tampered_path, "w") as package:
                for name, content in entries.items():
                    package.writestr(
                        name,
                        content + b"tampered" if name == object_path else content,
                    )

            unsupported = run_cli(
                "migration-import-dry-run",
                str(version_path),
                "--root",
                str(target_root),
            )
            tampered = run_cli(
                "migration-import-dry-run",
                str(tampered_path),
                "--root",
                str(target_root),
            )

            self.assertEqual(unsupported.returncode, 2)
            self.assertIn("unsupported migration package version", unsupported.stderr)
            self.assertEqual(tampered.returncode, 2)
            self.assertIn("migration object hash mismatch", tampered.stderr)

    def test_dry_run_reaudits_a_self_consistent_package_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            target_root = temporary_root / "target-instance"
            package_path = temporary_root / "valid.zip"
            stripped_path = temporary_root / "provenance-stripped.zip"
            run_cli("init", "--root", str(source_root))
            run_cli("init", "--root", str(target_root))
            source_memory = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="closure-audit",
                name="Closure audit rule",
                body="A valid hash cannot replace an auditable knowledge closure.",
                scope="migration closure audit",
            )
            memory_id = cast(
                str,
                cast(dict[str, object], source_memory["memory"])["memory_id"],
            )
            exported = run_cli(
                "migration-export",
                str(package_path),
                "--memory-id",
                memory_id,
                "--target",
                "target-instance",
                "--expected-version",
                "0",
                "--idempotency-key",
                "export-closure-audit",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            rewrite_valid_package(
                package_path,
                stripped_path,
                remove_evidence=True,
            )

            dry_run = run_cli(
                "migration-import-dry-run",
                str(stripped_path),
                "--root",
                str(target_root),
                "--format",
                "json",
            )

            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            preview = json.loads(dry_run.stdout)
            self.assertEqual(preview["status"], "blocked")
            self.assertEqual(preview["checks"]["audited_closure"], "blocked")
            self.assertIn(
                {
                    "kind": "unauditable-provenance",
                    "path": f"memory:{memory_id}/v1 -> provenance:missing",
                    "reason": (
                        "knowledge version has neither evidence nor a knowledge dependency"
                    ),
                },
                preview["blockers"],
            )

    def test_export_blocks_a_missing_exact_dependency_version_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source-instance"
            run_cli("init", "--root", str(source_root))
            dependency = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="endpoint-dependency",
                name="Endpoint dependency",
                body="This exact version must exist in the exported closure.",
                scope="migration version endpoint",
            )
            origin = create_source_backed_memory(
                temporary_root,
                source_root,
                stem="endpoint-origin",
                name="Endpoint origin",
                body="This knowledge depends on an exact version endpoint.",
                scope="migration version endpoint",
            )
            dependency_id = cast(
                str, cast(dict[str, object], dependency["memory"])["memory_id"]
            )
            origin_id = cast(
                str, cast(dict[str, object], origin["memory"])["memory_id"]
            )
            superseded = run_cli(
                "supersede-memory",
                dependency_id,
                "--replacement-memory-id",
                origin_id,
                "--replacement-version",
                "1",
                "--reason",
                "Create an exact knowledge dependency for the audit test.",
                "--expected-version",
                "1",
                "--idempotency-key",
                "create-version-endpoint",
                "--entrance",
                "codex",
                "--root",
                str(source_root),
            )
            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            with closing(sqlite3.connect(source_root / "store" / "memory.sqlite3")) as db:
                db.execute("PRAGMA foreign_keys = OFF")
                db.execute(
                    """
                    UPDATE canonical_memory_dependencies
                    SET depends_on_version = 99
                    WHERE memory_id = ? AND depends_on_memory_id = ?
                    """,
                    (origin_id, dependency_id),
                )
                db.commit()

            planned = run_cli(
                "migration-plan",
                "--memory-id",
                origin_id,
                "--target",
                "target-instance",
                "--root",
                str(source_root),
                "--format",
                "json",
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = json.loads(planned.stdout)
            self.assertFalse(plan["allowed"])
            self.assertEqual(plan["blockers"][0]["kind"], "missing-memory-version")
            self.assertIn(
                f"memory:{origin_id}/v1 -[supersedes]-> memory:{dependency_id}/v99",
                plan["blockers"][0]["path"],
            )

    def test_missing_and_unauditable_knowledge_report_fail_closed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "source-instance"
            payload_path = temporary_root / "proposal.json"
            batch_path = temporary_root / "batch.json"
            run_cli("init", "--root", str(instance_root))
            proposal = submit_proposal(
                instance_root,
                payload_path,
                proposal_payload(
                    intent="derive",
                    formation="derived",
                    priority="routine",
                    title="Model-only migration claim",
                    content="This claim has no durable source receipt.",
                    effect_type="create_derived_memory",
                ),
                "unauditable-migration-proposal",
            )
            batch_path.write_text(
                json.dumps(
                    {
                        "batch_id": "bat_unauditable_migration",
                        "decisions": [
                            {
                                "proposal_id": proposal["proposal_id"],
                                "proposal_version": proposal["proposal_version"],
                                "decision": "approve",
                                "edited_content": None,
                                "reason": "Retain it locally without inventing provenance.",
                                "defer_until": None,
                                "confirm_personal_cognition": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            approved = run_cli(
                "review-batch",
                str(batch_path),
                "--idempotency-key",
                "approve-unauditable-migration",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            memory_id = json.loads(approved.stdout)["outcomes"][0]["materialization"][
                "memory_id"
            ]

            missing = run_cli(
                "migration-plan",
                "--memory-id",
                "mem_missing_dependency",
                "--target",
                "portable-target",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            unauditable = run_cli(
                "migration-plan",
                "--memory-id",
                memory_id,
                "--target",
                "portable-target",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )

            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(
                json.loads(missing.stdout)["blockers"][0]["path"],
                "memory:mem_missing_dependency",
            )
            self.assertEqual(unauditable.returncode, 0, unauditable.stderr)
            self.assertEqual(
                json.loads(unauditable.stdout)["blockers"][0]["path"],
                f"memory:{memory_id}/v1 -> provenance:missing",
            )


if __name__ == "__main__":
    unittest.main()
