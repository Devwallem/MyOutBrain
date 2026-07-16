from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import tempfile
import unittest

from myoutbrain.evaluation import (
    RecallCase,
    RecallCategory,
    RecallDataset,
    evaluate_recall,
    load_recall_dataset,
)
from myoutbrain.retrieval import (
    EvidenceKind,
    EvidenceState,
    RetrievalDecision,
    RetrievalEvidence,
)
from tests.cli_support import run_cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FixedRetriever:
    @property
    def name(self) -> str:
        return "future-test-adapter"

    def retrieve(
        self,
        question: str,
        evidence: Sequence[RetrievalEvidence],
    ) -> RetrievalDecision:
        del question, evidence
        return RetrievalDecision(
            evidence_ids=("source_reflection",),
            should_refuse=False,
        )


class RelatedButRefusingRetriever:
    @property
    def name(self) -> str:
        return "related-but-insufficient"

    def retrieve(
        self,
        question: str,
        evidence: Sequence[RetrievalEvidence],
    ) -> RetrievalDecision:
        del question, evidence
        return RetrievalDecision(
            evidence_ids=("related_source",),
            should_refuse=True,
        )


class EvaluateEvidenceRecallTests(unittest.TestCase):
    def test_json_report_scores_evidence_selection_without_answer_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dataset_path = Path(temporary_directory) / "recall.json"
            dataset_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "evidence": [
                            {
                                "id": "source_reflection",
                                "kind": "source",
                                "state": "active",
                                "text": "Reflection makes accumulated experience reusable.",
                            },
                            {
                                "id": "source_capture",
                                "kind": "source",
                                "state": "active",
                                "text": "Capture preserves the original source.",
                            },
                        ],
                        "cases": [
                            {
                                "id": "answerable_reflection",
                                "category": "answerable",
                                "question": "What makes accumulated experience reusable?",
                                "answerable": True,
                                "expected_evidence_ids": ["source_reflection"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                "evaluate-recall",
                str(dataset_path),
                "--format",
                "json",
                environment={"OPENAI_API_KEY": "must-not-be-used"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["retriever"], "lexical-no-embeddings")
            self.assertTrue(report["evidence_selection_only"])
            self.assertNotIn("answer", report)
            self.assertEqual(
                report["cases"],
                [
                    {
                        "id": "answerable_reflection",
                        "category": "answerable",
                        "answerable": True,
                        "retrieved_evidence_ids": ["source_reflection"],
                        "correct_hits": ["source_reflection"],
                        "key_omissions": [],
                        "incorrect_citations": [],
                        "should_refuse_violation": False,
                        "passed": True,
                    }
                ],
            )
            self.assertEqual(
                report["summary"],
                {
                    "case_count": 1,
                    "passed": 1,
                    "failed": 0,
                    "correct_hits": 1,
                    "key_omissions": 0,
                    "incorrect_citations": 0,
                    "should_refuse_violations": 0,
                },
            )

    def test_versioned_baseline_covers_four_categories_and_has_stable_reports(self) -> None:
        dataset_path = PROJECT_ROOT / "evaluation" / "recall-baseline.json"

        first_json = run_cli(
            "evaluate-recall", str(dataset_path), "--format", "json"
        )
        second_json = run_cli(
            "evaluate-recall", str(dataset_path), "--format", "json"
        )
        text_report = run_cli(
            "evaluate-recall", str(dataset_path), "--format", "text"
        )

        self.assertEqual(first_json.returncode, 0, first_json.stderr)
        self.assertEqual(second_json.stdout, first_json.stdout)
        report = json.loads(first_json.stdout)
        self.assertEqual(
            {case["category"] for case in report["cases"]},
            {"answerable", "unanswerable", "conflicting", "superseded"},
        )
        self.assertEqual(report["summary"]["case_count"], 4)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(text_report.returncode, 0, text_report.stderr)
        self.assertIn("Evidence selection only; answer generation was not run.", text_report.stdout)
        self.assertIn("Retriever: lexical-no-embeddings", text_report.stdout)
        self.assertIn("Embeddings: disabled", text_report.stdout)
        self.assertIn("Correct hits:", text_report.stdout)
        self.assertIn("Key omissions:", text_report.stdout)
        self.assertIn("Incorrect citations:", text_report.stdout)
        self.assertIn("Should-refuse violations:", text_report.stdout)
        for category in ("answerable", "unanswerable", "conflicting", "superseded"):
            self.assertIn(f"Category: {category}", text_report.stdout)

    def test_recall_failures_are_reported_and_return_a_failing_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dataset_path = Path(temporary_directory) / "failing-recall.json"
            dataset_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "evidence": [
                            {
                                "id": "expected_source",
                                "kind": "source",
                                "state": "active",
                                "text": "The authoritative material uses a different phrase.",
                            },
                            {
                                "id": "wrong_source",
                                "kind": "source",
                                "state": "active",
                                "text": "A weekly review cadence is described here.",
                            },
                        ],
                        "cases": [
                            {
                                "id": "miss_and_wrong_citation",
                                "category": "answerable",
                                "question": "Which review cadence is weekly?",
                                "answerable": True,
                                "expected_evidence_ids": ["expected_source"],
                            },
                            {
                                "id": "should_refuse",
                                "category": "unanswerable",
                                "question": "Is the review cadence weekly?",
                                "answerable": False,
                                "expected_evidence_ids": [],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                "evaluate-recall", str(dataset_path), "--format", "json"
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(
                report["summary"],
                {
                    "case_count": 2,
                    "passed": 0,
                    "failed": 2,
                    "correct_hits": 0,
                    "key_omissions": 1,
                    "incorrect_citations": 2,
                    "should_refuse_violations": 1,
                },
            )
            first_case, second_case = report["cases"]
            self.assertEqual(first_case["key_omissions"], ["expected_source"])
            self.assertEqual(first_case["incorrect_citations"], ["wrong_source"])
            self.assertTrue(second_case["should_refuse_violation"])

    def test_same_dataset_can_be_scored_with_a_future_retrieval_adapter(self) -> None:
        dataset = RecallDataset(
            evidence=(
                RetrievalEvidence(
                    evidence_id="source_reflection",
                    kind=EvidenceKind.SOURCE,
                    state=EvidenceState.ACTIVE,
                    text="The adapter owns selection.",
                ),
            ),
            cases=load_recall_dataset(
                PROJECT_ROOT / "evaluation" / "recall-baseline.json"
            ).cases[:1],
        )

        report = evaluate_recall(dataset, FixedRetriever())

        self.assertEqual(report.retriever, "future-test-adapter")
        self.assertEqual(report.summary.failed, 0)

    def test_related_evidence_does_not_count_as_answer_when_retriever_refuses(self) -> None:
        dataset = RecallDataset(
            evidence=(
                RetrievalEvidence(
                    evidence_id="related_source",
                    kind=EvidenceKind.SOURCE,
                    state=EvidenceState.ACTIVE,
                    text="This is relevant but does not establish an answer.",
                ),
            ),
            cases=(
                RecallCase(
                    case_id="related_but_unanswerable",
                    category=RecallCategory.UNANSWERABLE,
                    question="Can the related source establish the answer?",
                    answerable=False,
                    expected_evidence_ids=("related_source",),
                ),
            ),
        )

        report = evaluate_recall(dataset, RelatedButRefusingRetriever())

        self.assertEqual(report.summary.correct_hits, 1)
        self.assertEqual(report.summary.should_refuse_violations, 0)
        self.assertEqual(report.summary.failed, 0)

    def test_invalid_dataset_is_rejected_before_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dataset_path = Path(temporary_directory) / "invalid.json"
            dataset_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "evidence": [],
                        "cases": [],
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli("evaluate-recall", str(dataset_path))

            self.assertEqual(result.returncode, 2)
            self.assertIn("Invalid evaluation dataset", result.stderr)
            self.assertIn("at least one evidence item", result.stderr)


if __name__ == "__main__":
    unittest.main()
