from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from myoutbrain.generation import ProviderFailure
from myoutbrain.public_search import search_public_sources
from tests.cli_support import run_cli
from tests.test_cli_ask import configure_fake_generation
from tests.test_cli_memory_evolution import accept_new, propose, remember_evidence


_PUBLIC_SEARCH_NOW = datetime.now(timezone.utc)
_RECENTLY_RETRIEVED_AT = (_PUBLIC_SEARCH_NOW - timedelta(minutes=1)).isoformat()
_RECENTLY_PUBLISHED_AT = (_PUBLIC_SEARCH_NOW - timedelta(days=1)).isoformat()
_OLDER_CURRENT_PUBLISHED_AT = (
    _PUBLIC_SEARCH_NOW - timedelta(days=16)
).isoformat()
_HISTORICAL_PUBLISHED_AT = (
    _PUBLIC_SEARCH_NOW - timedelta(days=500)
).isoformat()
_STALE_PUBLISHED_AT = (_PUBLIC_SEARCH_NOW - timedelta(days=366)).isoformat()


class AnswerWithPublicResearchFallbackTests(unittest.TestCase):
    def test_public_search_endpoint_does_not_follow_redirects(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MYOUTBRAIN_PUBLIC_SEARCH_ENDPOINT": (
                    "https://search.example/v1/research"
                ),
                "MYOUTBRAIN_PUBLIC_SEARCH_API_KEY": "secret-token",
            },
            clear=False,
        ):
            os.environ.pop("MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE", None)
            with mock.patch(
                "myoutbrain.public_search.HTTPSConnection"
            ) as connection_factory:
                response = connection_factory.return_value.getresponse.return_value
                response.status = 302

                with self.assertRaisesRegex(ProviderFailure, "HTTP 302"):
                    search_public_sources("safe public query", time_sensitive=False)

            connection_factory.assert_called_once_with(
                "search.example",
                None,
                timeout=15,
            )

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
                "--risk-level",
                "standard",
                "--freshness",
                "stable",
                "--force-consolidation",
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
            self.assertEqual(
                result["forced_consolidation"]["canonical_changes"], 0
            )
            self.assertEqual(
                result["forced_consolidation"]["scope"], "task-related"
            )
            self.assertEqual(len(result["forced_consolidation"]["proposal_ids"]), 1)
            self.assertIsNone(result["public_query"])
            self.assertEqual(
                result["claims"],
                [
                    {
                        "text": "Project Atlas is reviewed every Friday.",
                        "source_ids": [memory_id],
                        "origin": "companion-inference",
                        "evidence_origins": ["common-knowledge"],
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
                            "published_at": _RECENTLY_PUBLISHED_AT,
                            "retrieved_at": _RECENTLY_RETRIEVED_AT,
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
                "--public-query",
                public_query,
                "--format",
                "json",
                environment={
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
            self.assertEqual(result["claims"][0]["origin"], "companion-inference")
            self.assertEqual(
                result["claims"][0]["evidence_origins"],
                ["public-evidence"],
            )
            self.assertEqual(result["claims"][0]["source_ids"], [web_source_id])
            self.assertEqual(
                result["public_sources"],
                [
                    {
                        "source_id": web_source_id,
                        "url": url,
                        "title": "Product Nova 2 release",
                        "published_at": _RECENTLY_PUBLISHED_AT,
                        "retrieved_at": _RECENTLY_RETRIEVED_AT,
                        "source_type": "official",
                        "fact_key": "nova-2-release-date",
                        "fact_value": "2026-08-01",
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
                            "published_at": _HISTORICAL_PUBLISHED_AT,
                            "retrieved_at": _RECENTLY_RETRIEVED_AT,
                            "source_type": "reference",
                            "fact_key": "project-start-year",
                            "fact_value": "2019",
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
                "--risk-level",
                "standard",
                "--freshness",
                "stable",
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
            text_answer = run_cli(
                "answer",
                "Why did the project choose its final architecture?",
                "--root",
                str(instance_root),
                "--task",
                "architecture-history",
                "--risk-level",
                "standard",
                "--freshness",
                "stable",
                "--public-query",
                "project architecture history",
                "--format",
                "text",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": search_response,
                    "MYOUTBRAIN_FAKE_RESPONSE": generated_response,
                },
            )
            self.assertEqual(text_answer.returncode, 0, text_answer.stderr)
            self.assertIn("Public source: Partial history", text_answer.stdout)
            self.assertIn(url, text_answer.stdout)
            self.assertIn(f"published {_HISTORICAL_PUBLISHED_AT}", text_answer.stdout)
            self.assertIn(f"retrieved {_RECENTLY_RETRIEVED_AT}", text_answer.stdout)

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
                            "published_at": _STALE_PUBLISHED_AT,
                            "retrieved_at": _RECENTLY_RETRIEVED_AT,
                            "source_type": "official",
                            "fact_key": "launch-date",
                            "fact_value": "may",
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

    def test_public_research_fails_closed_without_a_trusted_sanitizer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            search_request = temporary_root / "public-search-request.json"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MYOUTBRAIN_FAKE_SANITIZED_QUERY", None)
                answered = run_cli(
                    "answer",
                    "Li Wei's unreleased Project Cinder price is $499. Is it current?",
                    "--root",
                    str(instance_root),
                    "--task",
                    "private-pricing",
                    "--format",
                    "json",
                    environment={
                        "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                            search_request
                        )
                    },
                )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["public_search_performed"])
            self.assertIsNone(result["public_query"])
            self.assertFalse(search_request.exists())

    def test_conflicting_public_sources_cannot_pass_the_second_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            first_url = "https://official.example/nova/schedule"
            second_url = "https://primary.example/nova/calendar"
            first_id = f"web_{hashlib.sha256(first_url.encode()).hexdigest()}"
            search_response = json.dumps(
                {
                    "results": [
                        {
                            "url": first_url,
                            "title": "Nova schedule",
                            "content": "Nova launches on August 1.",
                            "published_at": _RECENTLY_PUBLISHED_AT,
                            "retrieved_at": _RECENTLY_RETRIEVED_AT,
                            "source_type": "official",
                            "fact_key": "nova-launch-date",
                            "fact_value": "2026-08-01",
                        },
                        {
                            "url": second_url,
                            "title": "Nova calendar",
                            "content": "Nova launches on August 8.",
                            "published_at": _RECENTLY_PUBLISHED_AT,
                            "retrieved_at": _RECENTLY_RETRIEVED_AT,
                            "source_type": "primary",
                            "fact_key": "nova-launch-date",
                            "fact_value": "2026-08-08",
                        },
                    ]
                }
            )
            answered = run_cli(
                "answer",
                "What is the current Nova launch date?",
                "--root",
                str(instance_root),
                "--task",
                "nova-launch",
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": "current Nova launch date",
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": search_response,
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "Nova launches on August 1.",
                                    "source_id": first_id,
                                    "locator": first_url,
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
            self.assertTrue(result["public_search_performed"])
            self.assertEqual(len(result["public_sources"]), 2)
            self.assertIsNone(result["memory_update_id"])

    def test_authoritative_public_evidence_can_resolve_an_internal_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            remember_evidence(
                temporary_root,
                instance_root,
                name="weekly-evidence",
                digest="Project Atlas review cadence is weekly.",
                task="weekly-view",
                sensitivity="cloud-allowed",
            )
            weekly_id = accept_new(
                instance_root,
                propose(instance_root, "weekly-view")["proposal_id"],
            )
            remember_evidence(
                temporary_root,
                instance_root,
                name="daily-evidence",
                digest="Project Atlas review cadence is daily.",
                task="daily-view",
            )
            conflict_proposal = propose(instance_root, "daily-view")
            preserved = run_cli(
                "review-memory",
                str(conflict_proposal["proposal_id"]),
                (
                    f"preserve conflict with {weekly_id} because: "
                    "the available evidence disagrees"
                ),
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(preserved.returncode, 0, preserved.stderr)
            url = "https://official.example/atlas/reviews"
            web_source_id = f"web_{hashlib.sha256(url.encode()).hexdigest()}"

            answered = run_cli(
                "answer",
                "What is Project Atlas review cadence?",
                "--root",
                str(instance_root),
                "--task",
                "cadence-answer",
                "--access",
                "local-trusted",
                "--memory-id",
                weekly_id,
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": (
                        "official Project Atlas review cadence"
                    ),
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": json.dumps(
                        {
                            "results": [
                                {
                                    "url": url,
                                    "title": "Official Atlas review policy",
                                    "content": "Project Atlas is reviewed weekly.",
                                    "published_at": _OLDER_CURRENT_PUBLISHED_AT,
                                    "retrieved_at": _RECENTLY_RETRIEVED_AT,
                                    "source_type": "official",
                                    "fact_key": "atlas-review-cadence",
                                    "fact_value": "weekly",
                                }
                            ]
                        }
                    ),
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "Project Atlas is reviewed weekly.",
                                    "source_id": web_source_id,
                                    "locator": url,
                                }
                            ],
                            "insufficient_evidence": False,
                        }
                    ),
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            result = json.loads(answered.stdout)
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result["claims"][0]["source_ids"], [web_source_id])
            self.assertEqual(
                result["claims"][0]["evidence_origins"],
                ["public-evidence"],
            )

    def test_text_answer_names_public_source_and_evidence_times(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "Private Companion"
            initialized = run_cli("init", "--root", str(instance_root))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            configure_fake_generation(instance_root)
            url = "https://official.example/nova/release"
            web_source_id = f"web_{hashlib.sha256(url.encode()).hexdigest()}"
            answered = run_cli(
                "answer",
                "What is the current Nova release date?",
                "--root",
                str(instance_root),
                "--task",
                "nova-release",
                "--format",
                "text",
                environment={
                    "MYOUTBRAIN_FAKE_SANITIZED_QUERY": "current Nova release date",
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": json.dumps(
                        {
                            "results": [
                                {
                                    "url": url,
                                    "title": "Official Nova release",
                                    "content": "Nova releases on August 1.",
                                    "published_at": _RECENTLY_PUBLISHED_AT,
                                    "retrieved_at": _RECENTLY_RETRIEVED_AT,
                                    "source_type": "official",
                                    "fact_key": "nova-release-date",
                                    "fact_value": "2026-08-01",
                                }
                            ]
                        }
                    ),
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "Nova releases on August 1.",
                                    "source_id": web_source_id,
                                    "locator": url,
                                }
                            ],
                            "insufficient_evidence": False,
                        }
                    ),
                },
            )

            self.assertEqual(answered.returncode, 0, answered.stderr)
            self.assertIn("Official Nova release", answered.stdout)
            self.assertIn(url, answered.stdout)
            self.assertIn("Companion inference: Nova releases", answered.stdout)
            self.assertIn("Evidence origin (public-evidence)", answered.stdout)
            self.assertIn(f"published {_RECENTLY_PUBLISHED_AT}", answered.stdout)
            self.assertIn(f"retrieved {_RECENTLY_RETRIEVED_AT}", answered.stdout)

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
                "--risk-level",
                "standard",
                "--freshness",
                "stable",
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
                            "published_at": _RECENTLY_PUBLISHED_AT,
                            "retrieved_at": _RECENTLY_RETRIEVED_AT,
                            "source_type": "blog",
                            "fact_key": "launch-date",
                            "fact_value": "tomorrow",
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
