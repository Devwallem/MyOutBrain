from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from myoutbrain.memory_gateway import MemoryGateway
from myoutbrain.public_search import PublicSource
from myoutbrain.v2_public_research import V2PublicResearchRequest
from myoutbrain.v2_recall import CapabilityAnswerability, RecallMaterial
from tests.cli_support import run_cli


class V2PublicResearchTests(unittest.TestCase):
    def test_gateway_combines_recalled_memory_state_with_public_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Policy.md"
            source_path.write_text(
                "The accepted policy needs current public verification.\n",
                encoding="utf-8",
            )
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = json.loads(
                run_cli(
                    "propose-source-memory",
                    str(source_path),
                    "--name",
                    "Release verification policy",
                    "--body",
                    "Release dates require current public verification.",
                    "--scope",
                    "release planning",
                    "--idempotency-key",
                    "propose-release-policy-v1",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            approved = run_cli(
                "approve-source-memory",
                proposed["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-release-policy-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            question = "When is Nova's current release date?"
            recalled = run_cli(
                "recall-memory",
                question,
                "--task",
                "gateway-public-research",
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
            recall_id = json.loads(recalled.stdout)["recall_id"]
            public_source = PublicSource(
                source_id="web_test",
                url="https://official.example/nova/release",
                title="Official Nova release",
                content="Nova releases on August 1, 2026.",
                published_at="2026-07-16T09:00:00+00:00",
                retrieved_at="2026-07-18T09:00:00+00:00",
                source_type="official",
                fact_key="nova-release-date",
                fact_value="2026-08-01",
            )

            class InspectingProvider:
                def __init__(self) -> None:
                    self.query = ""

                def search(
                    self,
                    query: str,
                    *,
                    time_sensitive: bool,
                ) -> tuple[PublicSource, ...]:
                    self.query = query
                    self.assert_time_sensitive = time_sensitive
                    return (public_source,)

            class InspectingEngine:
                def __init__(self) -> None:
                    self.question = ""
                    self.memories: tuple[RecallMaterial, ...] = ()
                    self.public_sources: tuple[PublicSource, ...] = ()

                def assess(
                    self,
                    actual_question: str,
                    memories: tuple[RecallMaterial, ...],
                    public_sources: tuple[PublicSource, ...],
                ) -> CapabilityAnswerability:
                    self.question = actual_question
                    self.memories = memories
                    self.public_sources = public_sources
                    return CapabilityAnswerability(answerable=True, reason="covered")

            provider = InspectingProvider()
            engine = InspectingEngine()
            result = MemoryGateway(instance_root).research_public_v2(
                V2PublicResearchRequest(
                    recall_id=recall_id,
                    question=question,
                    task="gateway-public-research",
                    public_query="Nova official current release date",
                    allowed_for_task=True,
                    time_sensitive=True,
                ),
                provider,
                engine,
            )

            self.assertEqual(result["status"], "answered")
            self.assertEqual(provider.query, "Nova official current release date")
            self.assertTrue(provider.assert_time_sensitive)
            self.assertEqual(engine.question, question)
            self.assertEqual(len(engine.memories), 1)
            self.assertEqual(engine.memories[0].state, "current")
            self.assertEqual(engine.public_sources, (public_source,))

    def test_answerable_internal_recall_never_enters_public_research(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Internal answer.md"
            source_path.write_text(
                "The internal answer is complete.\n",
                encoding="utf-8",
            )
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = json.loads(
                run_cli(
                    "propose-source-memory",
                    str(source_path),
                    "--name",
                    "Complete internal answer",
                    "--body",
                    "The complete internal answer is Friday.",
                    "--scope",
                    "internal planning",
                    "--idempotency-key",
                    "propose-complete-answer-v1",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            approved = run_cli(
                "approve-source-memory",
                proposed["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-complete-answer-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            question = "What is the complete internal answer?"
            recalled = run_cli(
                "recall-memory",
                question,
                "--task",
                "internal-answer",
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
            package = json.loads(recalled.stdout)
            self.assertTrue(package["answerability"]["answerable"])
            search_request = temporary_root / "public-search-request.json"

            researched = run_cli(
                "research-public",
                package["recall_id"],
                question,
                "--task",
                "internal-answer",
                "--public-query",
                "complete internal answer",
                "--allow-public-research",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                        search_request
                    )
                },
            )

            self.assertEqual(researched.returncode, 2)
            self.assertIn("only after internal answerability fails", researched.stderr)
            self.assertFalse(search_request.exists())

    def test_internal_failure_does_not_authorize_public_research_for_the_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            recalled = run_cli(
                "recall-memory",
                "What is Nova's current release date?",
                "--task",
                "nova-release",
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
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            recall_id = json.loads(recalled.stdout)["recall_id"]
            search_request = temporary_root / "public-search-request.json"

            researched = run_cli(
                "research-public",
                recall_id,
                "What is Nova's current release date?",
                "--task",
                "nova-release",
                "--public-query",
                "Nova current release date",
                "--answerable",
                "false",
                "--answerability-reason",
                "coverage-insufficient",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                        search_request
                    )
                },
            )

            self.assertEqual(researched.returncode, 2)
            self.assertIn("current task authorization", researched.stderr)
            self.assertFalse(search_request.exists())

    def test_authorized_public_evidence_reassesses_the_failed_recall_as_mixed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            source_path = temporary_root / "Nova planning.md"
            source_path.write_text(
                "The private launch plan still needs public confirmation.\n",
                encoding="utf-8",
            )
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            proposed = run_cli(
                "propose-source-memory",
                str(source_path),
                "--name",
                "Nova launch planning",
                "--body",
                "Nova's private launch plan needs current public confirmation.",
                "--scope",
                "private launch planning",
                "--idempotency-key",
                "propose-nova-planning-v1",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            proposal = json.loads(proposed.stdout)
            approved = run_cli(
                "approve-source-memory",
                proposal["proposal_id"],
                "--expected-version",
                "0",
                "--idempotency-key",
                "approve-nova-planning-v1",
                "--entrance",
                "codex",
                "--root",
                str(instance_root),
                "--format",
                "json",
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            recalled = run_cli(
                "recall-memory",
                "For alice@example.com, when is Nova's current public release?",
                "--task",
                "private-nova-release",
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
            recall_package = json.loads(recalled.stdout)
            self.assertEqual(len(recall_package["memories"]), 1)
            review_queue_before = json.loads(
                run_cli(
                    "review-list",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            search_request = temporary_root / "public-search-request.json"
            public_query = "Nova official current release date"
            public_response = json.dumps(
                {
                    "results": [
                        {
                            "url": "https://official.example/nova/release",
                            "title": "Official Nova release",
                            "content": "Nova releases publicly on August 1, 2026.",
                            "published_at": "2026-07-16T09:00:00+00:00",
                            "retrieved_at": "2026-07-18T09:00:00+00:00",
                            "source_type": "official",
                            "fact_key": "nova-release-date",
                            "fact_value": "2026-08-01",
                        }
                    ]
                }
            )

            researched = run_cli(
                "research-public",
                recall_package["recall_id"],
                "For alice@example.com, when is Nova's current public release?",
                "--task",
                "private-nova-release",
                "--public-query",
                public_query,
                "--allow-public-research",
                "--time-sensitive",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE": str(
                        search_request
                    ),
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": public_response,
                },
            )

            self.assertEqual(researched.returncode, 0, researched.stderr)
            result = json.loads(researched.stdout)
            self.assertEqual(result["status"], "answered")
            self.assertEqual(
                result["answerability"],
                {
                    "answerable": True,
                    "reason": "covered",
                    "overridden_by_core": False,
                },
            )
            self.assertEqual(
                result["source_declaration"],
                {
                    "kind": "mixed",
                    "label": "综合你的 MyOutBrain 知识库与公开信息",
                    "evidence_disclosure": "on-request",
                },
            )
            self.assertEqual(result["public_research"]["query"], public_query)
            self.assertEqual(
                result["public_research"]["sources"][0]["state"],
                "external-unintegrated",
            )
            sent_search = search_request.read_text(encoding="utf-8")
            self.assertEqual(json.loads(sent_search), {"query": public_query})
            self.assertNotIn("alice@example.com", sent_search)
            activity = json.loads(
                run_cli(
                    "recall-activity",
                    "--root",
                    str(instance_root),
                    "--format",
                    "json",
                ).stdout
            )
            event = next(
                item
                for item in activity["events"]
                if item["recall_id"] == recall_package["recall_id"]
            )
            self.assertTrue(event["answerability"]["answerable"])
            self.assertEqual(
                json.loads(
                    run_cli(
                        "review-list",
                        "--root",
                        str(instance_root),
                        "--format",
                        "json",
                    ).stdout
                ),
                review_queue_before,
            )

            unrecalled_public_result = run_cli(
                "recall-memory",
                "2026-08-01",
                "--task",
                "prove-public-result-was-not-materialized",
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
            self.assertEqual(unrecalled_public_result.returncode, 0)
            self.assertEqual(json.loads(unrecalled_public_result.stdout)["memories"], [])

    def test_conflicting_public_evidence_keeps_the_recall_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            recalled = run_cli(
                "recall-memory",
                "What is Nova's current release date?",
                "--task",
                "nova-conflict",
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
            recall_id = json.loads(recalled.stdout)["recall_id"]
            public_response = json.dumps(
                {
                    "results": [
                        {
                            "url": "https://official.example/nova/release",
                            "title": "Official Nova release",
                            "content": "Nova releases on August 1, 2026.",
                            "published_at": "2026-07-16T09:00:00+00:00",
                            "retrieved_at": "2026-07-18T09:00:00+00:00",
                            "source_type": "official",
                            "fact_key": "nova-release-date",
                            "fact_value": "2026-08-01",
                        },
                        {
                            "url": "https://primary.example/nova/calendar",
                            "title": "Nova public calendar",
                            "content": "Nova releases on August 8, 2026.",
                            "published_at": "2026-07-16T10:00:00+00:00",
                            "retrieved_at": "2026-07-18T09:00:00+00:00",
                            "source_type": "primary",
                            "fact_key": "nova-release-date",
                            "fact_value": "2026-08-08",
                        },
                    ]
                }
            )

            researched = run_cli(
                "research-public",
                recall_id,
                "What is Nova's current release date?",
                "--task",
                "nova-conflict",
                "--public-query",
                "Nova official current release date",
                "--allow-public-research",
                "--time-sensitive",
                "--answerable",
                "true",
                "--answerability-reason",
                "covered",
                "--root",
                str(instance_root),
                "--format",
                "json",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": public_response
                },
            )

            self.assertEqual(researched.returncode, 0, researched.stderr)
            result = json.loads(researched.stdout)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(
                result["answerability"],
                {
                    "answerable": False,
                    "reason": "unresolved-conflict",
                    "overridden_by_core": True,
                },
            )
            self.assertEqual(
                result["verified_facts"],
                [
                    "Nova releases on August 1, 2026.",
                    "Nova releases on August 8, 2026.",
                ],
            )
            self.assertEqual(len(result["unresolved_gaps"]), 1)
            self.assertEqual(len(result["next_steps"]), 1)

    def test_public_research_that_remains_insufficient_presents_only_verified_facts_and_gaps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            instance_root = temporary_root / "MyOutBrain"
            self.assertEqual(run_cli("init", "--root", str(instance_root)).returncode, 0)
            recalled = run_cli(
                "recall-memory",
                "Why did the project choose its final architecture?",
                "--task",
                "architecture-history",
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
            recall_id = json.loads(recalled.stdout)["recall_id"]
            public_response = json.dumps(
                {
                    "results": [
                        {
                            "url": "https://reference.example/partial-history",
                            "title": "Partial architecture history",
                            "content": "The archive confirms the project began in 2019.",
                            "published_at": "2025-01-01T09:00:00+00:00",
                            "retrieved_at": "2026-07-17T09:00:00+00:00",
                            "source_type": "reference",
                            "fact_key": "project-start-year",
                            "fact_value": "2019",
                        }
                    ]
                }
            )

            researched = run_cli(
                "research-public",
                recall_id,
                "Why did the project choose its final architecture?",
                "--task",
                "architecture-history",
                "--public-query",
                "project architecture history",
                "--allow-public-research",
                "--answerable",
                "false",
                "--answerability-reason",
                "coverage-insufficient",
                "--root",
                str(instance_root),
                "--format",
                "text",
                environment={
                    "MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE": public_response
                },
            )

            self.assertEqual(researched.returncode, 0, researched.stderr)
            self.assertIn("公开检索后仍无法形成可靠结论", researched.stdout)
            self.assertIn(
                "已核验：The archive confirms the project began in 2019.",
                researched.stdout,
            )
            self.assertIn("关键未知：", researched.stdout)
            self.assertIn("验证方向：", researched.stdout)
            self.assertIn("Partial architecture history", researched.stdout)
            self.assertIn(
                "https://reference.example/partial-history",
                researched.stdout,
            )
            self.assertNotIn("完整结论：", researched.stdout)


if __name__ == "__main__":
    unittest.main()
