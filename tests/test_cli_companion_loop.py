from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from myoutbrain.embeddings import DeterministicEmbeddingProvider
from myoutbrain.memory_gateway import (
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)
from tests.cli_support import run_cli
from tests.test_cli_ask import configure_generation


class CompanionLoopCliTests(unittest.TestCase):
    def test_codex_cli_submits_visible_task_then_recalls_minimal_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            visible_task = temporary_root / "visible-codex-task.txt"
            visible_task.write_text(
                "The visible deployment task confirms that packages are signed.",
                encoding="utf-8",
            )

            submitted = run_cli(
                "codex-submit",
                str(visible_task),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-18T11:00:00+08:00",
                "--task-pointer",
                "deployment-signing",
                "--digest",
                "Deployment packages must be signed.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current Codex deployment task",
                "--context-gap",
                "messages before the task are unavailable",
                "--format",
                "json",
            )
            context = run_cli(
                "codex-context",
                "How are deployment packages prepared?",
                "--root",
                str(instance_root),
                "--task-pointer",
                "deployment-signing",
                "--purpose",
                "substantive",
                "--format",
                "json",
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            self.assertEqual(json.loads(submitted.stdout)["entrance"], "codex")
            self.assertEqual(context.returncode, 0, context.stderr)
            package = json.loads(context.stdout)
            self.assertEqual(package["task_pointer"], "deployment-signing")
            self.assertEqual(len(package["evidence_package"]["items"]), 1)
            self.assertEqual(
                package["evidence_package"]["items"][0]["memory_id"],
                json.loads(submitted.stdout)["digest_id"],
            )

    def test_complete_companion_loop_survives_engine_and_projection_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            configure_generation(instance_root, "fake", "engine-v1")
            visible_task = temporary_root / "lumen-task.txt"
            visible_task.write_text(
                "Project Lumen needs the current Nova 2 launch date, which is not visible internally.",
                encoding="utf-8",
            )
            submitted = run_cli(
                "codex-submit",
                str(visible_task),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-18T12:00:00+08:00",
                "--task-pointer",
                "lumen-launch",
                "--digest",
                "Project Lumen still needs a verified Nova 2 launch date.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current Lumen launch task",
                "--context-gap",
                "the launch date and earlier project history are unavailable",
                "--format",
                "json",
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            submitted_data = json.loads(submitted.stdout)
            submitted_memory_id = submitted_data["digest_id"]
            submitted_source_id = submitted_data["source_id"]
            immediate = run_cli(
                "codex-context",
                "What launch information does Project Lumen still need?",
                "--root",
                str(instance_root),
                "--task-pointer",
                "lumen-launch",
                "--purpose",
                "substantive",
                "--format",
                "json",
            )
            self.assertEqual(immediate.returncode, 0, immediate.stderr)
            self.assertEqual(
                json.loads(immediate.stdout)["evidence_package"]["items"][0][
                    "memory_id"
                ],
                submitted_memory_id,
            )

            public_query = "Product Nova 2 official release date"
            url = "https://official.example/products/nova-2"
            web_source_id = f"web_{hashlib.sha256(url.encode()).hexdigest()}"
            public_response = json.dumps(
                {
                    "results": [
                        {
                            "url": url,
                            "title": "Product Nova 2 release",
                            "content": "Product Nova 2 launches on 2026-08-01.",
                            "published_at": "2026-07-17T09:00:00+00:00",
                            "retrieved_at": "2026-07-18T04:00:00+00:00",
                            "source_type": "official",
                            "fact_key": "nova-2-release-date",
                            "fact_value": "2026-08-01",
                        }
                    ]
                }
            )
            generated_response = json.dumps(
                {
                    "claims": [
                        {
                            "text": "Product Nova 2 launches on August 1, 2026.",
                            "source_id": web_source_id,
                            "locator": url,
                        }
                    ],
                    "insufficient_evidence": False,
                }
            )
            public_request_path = temporary_root / "public-search-request.json"
            answered = run_cli(
                "answer",
                "When does Product Nova 2 launch for Project Lumen?",
                "--root",
                str(instance_root),
                "--task",
                "lumen-launch",
                "--access",
                "local-trusted",
                "--time-sensitive",
                "--public-query",
                public_query,
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": public_response,
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                        public_request_path
                    ),
                    "MYOUTBRAIN_FAKE_RESPONSE": generated_response,
                },
            )
            self.assertEqual(answered.returncode, 0, answered.stderr)
            answer = json.loads(answered.stdout)
            self.assertEqual(answer["status"], "answered")
            self.assertEqual(
                answer["claims"][0]["evidence_origins"], ["public-evidence"]
            )
            self.assertRegex(answer["memory_update_id"], r"^mem_[0-9a-f]{64}$")
            self.assertEqual(
                json.loads(public_request_path.read_text(encoding="utf-8")),
                {"query": public_query},
            )
            self.assertNotIn(
                "Project Lumen",
                public_request_path.read_text(encoding="utf-8"),
            )

            consolidated = run_cli(
                "consolidate",
                "--task",
                "lumen-launch",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(consolidated.returncode, 0, consolidated.stderr)
            proposals = json.loads(consolidated.stdout)["proposals"]
            target_proposal = next(
                proposal
                for proposal in proposals
                if submitted_memory_id in proposal["evidence_memory_ids"]
            )
            before_review = run_cli(
                "recall",
                "Project Lumen Nova launch",
                "--root",
                str(instance_root),
                "--task",
                "lumen-launch",
                "--access",
                "local-trusted",
                "--format",
                "json",
            )
            self.assertTrue(
                all(
                    item["memory_state"] == "buffered"
                    for item in json.loads(before_review.stdout)["items"]
                )
            )
            reviewed = run_cli(
                "review-memory",
                target_proposal["proposal_id"],
                "accept",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            canonical_id = json.loads(reviewed.stdout)["canonical_memory_id"]

            configure_generation(instance_root, "fake", "engine-v2")
            after_engine_change = MemoryGateway(
                instance_root,
                embedding_provider=DeterministicEmbeddingProvider(),
            ).recall(
                RecallRequest(
                    query="Project Lumen Nova launch",
                    task="lumen-launch",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    memory_ids=(canonical_id,),
                )
            )
            self.assertEqual(after_engine_change.items[0].memory_id, canonical_id)
            self.assertTrue(after_engine_change.items[0].confirmed)
            self.assertIn(
                submitted_source_id, after_engine_change.items[0].source_ids
            )
            self.assertIn("Project Lumen", after_engine_change.items[0].content)

            built = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            shutil.rmtree(instance_root / "runtime" / "indexes", ignore_errors=True)
            shutil.rmtree(
                instance_root / "vault" / "Knowledge Views", ignore_errors=True
            )
            rebuilt_views = run_cli(
                "build-views",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(rebuilt_views.returncode, 0, rebuilt_views.stderr)
            rebuilt = MemoryGateway(
                instance_root,
                embedding_provider=DeterministicEmbeddingProvider(),
            ).recall(
                RecallRequest(
                    query="Project Lumen Nova launch",
                    task="lumen-launch",
                    access=MemoryAccess.TASK_SCOPED,
                    purpose=QueryPurpose.SUBSTANTIVE,
                    memory_ids=(canonical_id,),
                )
            )
            self.assertEqual(rebuilt.items[0].memory_id, canonical_id)
            self.assertIn(submitted_source_id, rebuilt.items[0].source_ids)
            self.assertTrue(
                (instance_root / "runtime" / "indexes" / "semantic").is_dir()
            )
            self.assertTrue(
                (instance_root / "vault" / "Knowledge Views" / "Index.md").is_file()
            )

    def test_unsuccessful_research_stays_unknown_and_unapproved_is_not_canonical(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            self.assertEqual(
                run_cli("init", "--root", str(instance_root)).returncode,
                0,
            )
            configure_generation(instance_root, "fake", "unknown-engine")
            visible_task = temporary_root / "architecture-task.txt"
            visible_task.write_text(
                "The visible task asks why the final architecture was selected.",
                encoding="utf-8",
            )
            submitted = run_cli(
                "codex-submit",
                str(visible_task),
                "--root",
                str(instance_root),
                "--occurred-at",
                "2026-07-18T13:00:00+08:00",
                "--task-pointer",
                "architecture-history",
                "--digest",
                "The reason for the final architecture remains unknown.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current architecture-history task",
                "--context-gap",
                "the original decision record is unavailable",
                "--format",
                "json",
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            url = "https://reference.example/partial-history"
            web_source_id = f"web_{hashlib.sha256(url.encode()).hexdigest()}"
            researched = run_cli(
                "answer",
                "Why was the final architecture selected?",
                "--root",
                str(instance_root),
                "--task",
                "architecture-history",
                "--risk-level",
                "standard",
                "--freshness",
                "stable",
                "--public-query",
                "project architecture decision history",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": json.dumps(
                        {
                            "results": [
                                {
                                    "url": url,
                                    "title": "Partial project history",
                                    "content": "The project began in 2019.",
                                    "published_at": "2025-01-01T00:00:00+00:00",
                                    "retrieved_at": "2026-07-18T05:00:00+00:00",
                                    "source_type": "reference",
                                    "fact_key": "project-start-year",
                                    "fact_value": "2019",
                                }
                            ]
                        }
                    ),
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "The project began in 2019.",
                                    "source_id": web_source_id,
                                    "locator": url,
                                }
                            ],
                            "insufficient_evidence": True,
                        }
                    ),
                },
            )
            proposal = run_cli(
                "consolidate",
                "--task",
                "architecture-history",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            recalled = run_cli(
                "recall",
                "final architecture reason",
                "--root",
                str(instance_root),
                "--task",
                "architecture-history",
                "--access",
                "task-scoped",
                "--format",
                "json",
            )

            self.assertEqual(researched.returncode, 0, researched.stderr)
            answer = json.loads(researched.stdout)
            self.assertEqual(answer["status"], "unknown")
            self.assertIsNone(answer["memory_update_id"])
            self.assertEqual(proposal.returncode, 0, proposal.stderr)
            self.assertEqual(
                json.loads(proposal.stdout)["proposals"][0]["status"], "pending"
            )
            self.assertTrue(
                all(
                    item["memory_state"] == "buffered"
                    for item in json.loads(recalled.stdout)["items"]
                )
            )


if __name__ == "__main__":
    unittest.main()
