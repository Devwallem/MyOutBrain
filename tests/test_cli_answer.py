from __future__ import annotations

from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from tests.cli_support import run_cli
from tests.test_cli_ask import configure_fake_generation


class AnswerWithPublicResearchFallbackTests(unittest.TestCase):
    def test_sufficient_internal_evidence_answers_without_public_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            conversation = temporary_root / "review-cadence.txt"
            conversation.write_text(
                "We confirmed that Project Atlas is reviewed every Friday.",
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
                "atlas-planning",
                "--digest",
                "Project Atlas review cadence is every Friday.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "current planning conversation",
                "--context-gap",
                "earlier task history unavailable",
                "--format",
                "json",
            )
            self.assertEqual(remembered.returncode, 0, remembered.stderr)
            memory_id = json.loads(remembered.stdout)["digest_id"]
            search_request = temporary_root / "public-search-request.json"
            response = json.dumps(
                {
                    "claims": [
                        {
                            "text": "Project Atlas is reviewed every Friday.",
                            "source_id": memory_id,
                            "locator": f"memory:{memory_id}",
                        }
                    ],
                    "insufficient_evidence": False,
                }
            )

            answered = run_cli(
                "answer",
                "When is Project Atlas reviewed?",
                "--root",
                str(instance_root),
                "--task",
                "atlas-planning",
                "--access",
                "local-trusted",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": response,
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                        search_request
                    ),
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result["answerability"], "sufficient")
            self.assertFalse(result["public_search_performed"])
            self.assertIsNone(result["public_query"])
            self.assertEqual(
                result["claims"],
                [
                    {
                        "text": "Project Atlas is reviewed every Friday.",
                        "source_ids": [memory_id],
                        "origin": "common-knowledge",
                    }
                ],
            )
            self.assertRegex(result["memory_update_id"], r"^mem_[0-9a-f]{64}$")
            self.assertFalse(search_request.exists())
            recalled_update = run_cli(
                "recall",
                "cited evidence",
                "--root",
                str(instance_root),
                "--task",
                "atlas-planning",
                "--access",
                "local-trusted",
                "--memory-id",
                result["memory_update_id"],
                "--format",
                "json",
            )
            self.assertEqual(recalled_update.returncode, 0, recalled_update.stderr)
            updates = [
                item
                for item in json.loads(recalled_update.stdout)["items"]
                if item["memory_id"] == result["memory_update_id"]
            ]
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0]["memory_state"], "buffered")
            self.assertIn(memory_id, updates[0]["content"])

    def test_insufficient_internal_evidence_uses_only_a_sanitized_public_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            search_request = temporary_root / "public-search-request.json"
            public_query = "Product Nova 2 official release date"
            url = "https://official.example/products/nova-2"
            web_source_id = f"web_{hashlib.sha256(url.encode()).hexdigest()}"
            search_response = json.dumps(
                {
                    "results": [
                        {
                            "url": url,
                            "title": "Product Nova 2 release",
                            "content": "Product Nova 2 launches on 2026-08-01.",
                            "published_at": "2026-07-16T09:00:00+00:00",
                            "retrieved_at": "2026-07-17T09:00:00+00:00",
                            "source_type": "official",
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

            answered = run_cli(
                "answer",
                "For client alice@example.com in Project Cinder, when does Product Nova 2 launch?",
                "--root",
                str(instance_root),
                "--task",
                "private-client-planning",
                "--access",
                "local-trusted",
                "--time-sensitive",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": public_query,
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": search_response,
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                        search_request
                    ),
                    "MYOUTBRAIN_FAKE_RESPONSE": generated_response,
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result["answerability"], "sufficient")
            self.assertTrue(result["public_search_performed"])
            self.assertEqual(result["public_query"], public_query)
            self.assertEqual(result["claims"][0]["origin"], "public-evidence")
            self.assertEqual(result["claims"][0]["source_ids"], [web_source_id])
            self.assertEqual(
                result["public_sources"],
                [
                    {
                        "source_id": web_source_id,
                        "url": url,
                        "title": "Product Nova 2 release",
                        "published_at": "2026-07-16T09:00:00+00:00",
                        "retrieved_at": "2026-07-17T09:00:00+00:00",
                        "source_type": "official",
                    }
                ],
            )
            self.assertRegex(result["memory_update_id"], r"^mem_[0-9a-f]{64}$")
            sent_search = json.loads(search_request.read_text(encoding="utf-8"))
            self.assertEqual(sent_search, {"query": public_query})
            serialized_search = search_request.read_text(encoding="utf-8")
            self.assertNotIn("alice@example.com", serialized_search)
            self.assertNotIn("Project Cinder", serialized_search)

    def test_public_research_that_remains_insufficient_reports_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            url = "https://reference.example/partial-history"
            web_source_id = f"web_{hashlib.sha256(url.encode()).hexdigest()}"
            search_response = json.dumps(
                {
                    "results": [
                        {
                            "url": url,
                            "title": "Partial history",
                            "content": "The archive confirms the project began in 2019.",
                            "published_at": "2025-01-01T09:00:00+00:00",
                            "retrieved_at": "2026-07-17T09:00:00+00:00",
                            "source_type": "reference",
                        }
                    ]
                }
            )
            generated_response = json.dumps(
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
            )

            answered = run_cli(
                "answer",
                "Why did the project choose its final architecture?",
                "--root",
                str(instance_root),
                "--task",
                "architecture-history",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": "project architecture history",
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": search_response,
                    "MYOUTBRAIN_FAKE_RESPONSE": generated_response,
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["answerability"], "insufficient")
            self.assertTrue(result["public_search_performed"])
            self.assertEqual(result["claims"], [])
            self.assertEqual(
                result["verified_facts"],
                ["The project began in 2019."],
            )
            self.assertEqual(len(result["unresolved_gaps"]), 1)
            self.assertEqual(len(result["next_steps"]), 1)
            self.assertIsNone(result["memory_update_id"])

    def test_local_only_memory_never_reaches_cloud_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            conversation = temporary_root / "private-plan.txt"
            conversation.write_text(
                "Project SecretFox uses a private Tuesday review cadence.",
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
                "private-plan",
                "--digest",
                "Project SecretFox review cadence is Tuesday.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "private planning conversation",
                "--context-gap",
                "earlier history unavailable",
                "--format",
                "json",
            )
            self.assertEqual(remembered.returncode, 0, remembered.stderr)

            answered = run_cli(
                "answer",
                "When is Project SecretFox reviewed?",
                "--root",
                str(instance_root),
                "--task",
                "private-plan",
                "--access",
                "local-trusted",
                "--allow-cloud",
                "--query-sensitivity",
                "cloud-allowed",
                "--format",
                "json",
                environment={
                    "OPENAI_API_KEY": "",
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": "project review cadence",
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["public_search_performed"])
            self.assertIsNone(result["memory_update_id"])

    def test_stale_public_evidence_cannot_answer_a_time_sensitive_question(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            url = "https://official.example/current-schedule"
            search_response = json.dumps(
                {
                    "results": [
                        {
                            "url": url,
                            "title": "Old schedule",
                            "content": "The launch was once planned for May.",
                            "published_at": "2025-01-01T09:00:00+00:00",
                            "retrieved_at": "2026-07-17T09:00:00+00:00",
                            "source_type": "official",
                        }
                    ]
                }
            )

            answered = run_cli(
                "answer",
                "What is the current launch date?",
                "--root",
                str(instance_root),
                "--task",
                "launch-check",
                "--time-sensitive",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": "current launch date",
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": search_response,
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "claims": [],
                            "insufficient_evidence": True,
                        }
                    ),
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["public_sources"], [])
            self.assertIsNone(result["memory_update_id"])

    def test_answer_update_inherits_the_strongest_cited_sensitivity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            conversation = temporary_root / "private-preference.txt"
            conversation.write_text(
                "The creator privately prefers reviews on Tuesday.",
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
                "private-preference",
                "--digest",
                "The creator privately prefers Tuesday reviews.",
                "--sensitivity",
                "local-only",
                "--visible-context",
                "private preference conversation",
                "--context-gap",
                "earlier history unavailable",
                "--format",
                "json",
            )
            self.assertEqual(remembered.returncode, 0, remembered.stderr)
            memory_id = json.loads(remembered.stdout)["digest_id"]
            generated_response = json.dumps(
                {
                    "claims": [
                        {
                            "text": "The creator prefers Tuesday reviews.",
                            "source_id": memory_id,
                            "locator": f"memory:{memory_id}",
                        }
                    ],
                    "insufficient_evidence": False,
                }
            )
            answered = run_cli(
                "answer",
                "Which review day is preferred?",
                "--root",
                str(instance_root),
                "--task",
                "private-preference",
                "--access",
                "local-trusted",
                "--memory-id",
                memory_id,
                "--query-sensitivity",
                "cloud-allowed",
                "--format",
                "json",
                environment={"MYOUTBRAIN_FAKE_RESPONSE": generated_response},
            )
            self.assertEqual(answered.returncode, 0, answered.stderr)
            update_id = json.loads(answered.stdout)["memory_update_id"]

            public_recall = run_cli(
                "recall",
                "Tuesday reviews",
                "--root",
                str(instance_root),
                "--task",
                "unrelated-public-task",
                "--access",
                "public-external",
                "--memory-id",
                update_id,
                "--query-sensitivity",
                "cloud-allowed",
                "--format",
                "json",
            )
            self.assertEqual(public_recall.returncode, 0, public_recall.stderr)
            public_ids = {
                item["memory_id"]
                for item in json.loads(public_recall.stdout)["items"]
            }
            self.assertNotIn(update_id, public_ids)

    def test_untrusted_public_result_is_rejected_before_answer_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            search_response = json.dumps(
                {
                    "results": [
                        {
                            "url": "https://rumor.example/unverified",
                            "title": "Unverified rumor",
                            "content": "A rumor claims the launch is tomorrow.",
                            "published_at": "2026-07-17T08:00:00+00:00",
                            "retrieved_at": "2026-07-17T09:00:00+00:00",
                            "source_type": "blog",
                        }
                    ]
                }
            )
            answered = run_cli(
                "answer",
                "When is the launch?",
                "--root",
                str(instance_root),
                "--task",
                "launch-rumor",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": "official launch date",
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": search_response,
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "The launch is tomorrow.",
                                    "source_id": "web_untrusted",
                                    "locator": "https://rumor.example/unverified",
                                }
                            ],
                            "insufficient_evidence": False,
                        }
                    ),
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["public_sources"], [])


if __name__ == "__main__":
    unittest.main()
