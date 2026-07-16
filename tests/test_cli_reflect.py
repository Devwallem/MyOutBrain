from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import unittest

from tests.cli_support import run_cli


def configure_fake_generation(library_root: Path) -> None:
    configuration_path = library_root / "myoutbrain.toml"
    configuration = configuration_path.read_text(encoding="utf-8")
    configuration = re.sub(
        r'\[generation\]\nprovider = "[^"]+"\nmodel = "[^"]+"',
        '[generation]\nprovider = "fake"\nmodel = "deterministic-test"',
        configuration,
    )
    configuration_path.write_text(configuration, encoding="utf-8")


def configure_generation(library_root: Path, provider: str, model: str) -> None:
    configuration_path = library_root / "myoutbrain.toml"
    configuration = configuration_path.read_text(encoding="utf-8")
    configuration = re.sub(
        r'\[generation\]\nprovider = "[^"]+"\nmodel = "[^"]+"',
        f'[generation]\nprovider = "{provider}"\nmodel = "{model}"',
        configuration,
    )
    configuration_path.write_text(configuration, encoding="utf-8")


def source_locator(source_id: str, line_count: int = 1) -> str:
    digest = source_id.removeprefix("src_")
    return (
        f"store/objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        f"#L1-L{line_count}"
    )


def initialize_cloud_source(temporary_root: Path) -> tuple[Path, str]:
    library_root = temporary_root / "My Knowledge"
    initialization = run_cli("init", "--root", str(library_root))
    if initialization.returncode != 0:
        raise AssertionError(initialization.stderr)
    configure_fake_generation(library_root)
    source_path = temporary_root / "Reflection.md"
    source_path.write_bytes(b"Reflection makes accumulated experience reusable.\n")
    capture = run_cli(
        "capture",
        str(source_path),
        "--sensitivity",
        "cloud-allowed",
        "--root",
        str(library_root),
    )
    if capture.returncode != 0:
        raise AssertionError(capture.stderr)
    identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
    if identity is None:
        raise AssertionError(f"capture did not return a source identity: {capture.stdout}")
    return library_root, identity.group(0)


def reflection_response(source_id: str) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "text": "Reflection turns experience into reusable guidance.",
                    "supporting_evidence": [
                        {
                            "source_id": source_id,
                            "locator": source_locator(source_id),
                        }
                    ],
                    "contrary_evidence": [],
                    "derivation": "Generalizes the source.",
                }
            ],
            "insufficient_evidence": False,
        }
    )


class ReflectOnEvidenceTests(unittest.TestCase):
    def test_creator_can_reflect_without_writing_permanent_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Reflection.md"
            source_path.write_text(
                "Reflection makes accumulated experience reusable.\n",
                encoding="utf-8",
            )
            capture = run_cli(
                "capture",
                str(source_path),
                "--sensitivity",
                "cloud-allowed",
                "--root",
                str(library_root),
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            vault_before = sorted(
                path.relative_to(library_root / "vault")
                for path in (library_root / "vault").rglob("*")
                if path.is_file()
            )

            result = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": json.dumps(
                        {
                            "candidates": [
                                {
                                    "text": "Reflection turns experience into reusable guidance.",
                                    "supporting_evidence": [
                                        {
                                            "source_id": source_id,
                                            "locator": source_locator(source_id),
                                        }
                                    ],
                                    "contrary_evidence": [],
                                    "derivation": (
                                        "Generalizes the source's stated benefit of reflection."
                                    ),
                                }
                            ],
                            "insufficient_evidence": False,
                        }
                    )
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            candidate_identity = re.search(r"cand_[0-9a-f]{64}", result.stdout)
            self.assertIsNotNone(candidate_identity)
            candidate_id = (
                candidate_identity.group(0) if candidate_identity is not None else ""
            )
            candidate_path = (
                library_root / "runtime" / "workspace" / "candidates" / f"{candidate_id}.json"
            )
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(candidate["id"], candidate_id)
            self.assertEqual(candidate["kind"], "candidate-insight")
            self.assertEqual(candidate["state"], "pending-review")
            self.assertEqual(candidate["authorship"], "system")
            self.assertEqual(candidate["occurrence_count"], 1)
            self.assertEqual(candidate["supporting_evidence"][0]["source_id"], source_id)
            self.assertEqual(candidate["contrary_evidence"], [])
            self.assertIn("Generalizes", candidate["derivation"])
            self.assertIn("expires_at", candidate)
            vault_after = sorted(
                path.relative_to(library_root / "vault")
                for path in (library_root / "vault").rglob("*")
                if path.is_file()
            )
            self.assertEqual(vault_after, vault_before)

    def test_materially_similar_candidate_reuses_identity_and_increments_recurrence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Reflection.md"
            source_path.write_text(
                "Reflection makes accumulated experience reusable.\n",
                encoding="utf-8",
            )
            capture = run_cli(
                "capture",
                str(source_path),
                "--sensitivity",
                "cloud-allowed",
                "--root",
                str(library_root),
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            source_id = identity.group(0) if identity is not None else ""
            first_response = json.dumps(
                {
                    "candidates": [
                        {
                            "text": "Reflection turns experience into reusable guidance.",
                            "supporting_evidence": [
                                {
                                    "source_id": source_id,
                                    "locator": source_locator(source_id),
                                }
                            ],
                            "contrary_evidence": [],
                            "derivation": "Generalizes the source.",
                        }
                    ],
                    "insufficient_evidence": False,
                }
            )
            similar_response = json.dumps(
                {
                    "candidates": [
                        {
                            "text": (
                                "Reflection turns accumulated experience into reusable guidance."
                            ),
                            "supporting_evidence": [
                                {
                                    "source_id": source_id,
                                    "locator": source_locator(source_id),
                                }
                            ],
                            "contrary_evidence": [],
                            "derivation": "Restates the same generalization.",
                        }
                    ],
                    "insufficient_evidence": False,
                }
            )
            arguments = (
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
            )

            first = run_cli(
                *arguments,
                environment={"MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": first_response},
            )
            repeated = [
                run_cli(
                    *arguments,
                    environment={
                        "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": similar_response
                    },
                )
                for _ in range(4)
            ]

            self.assertEqual(first.returncode, 0, first.stderr)
            for result in repeated:
                self.assertEqual(result.returncode, 0, result.stderr)
            candidate_files = list(
                (library_root / "runtime" / "workspace" / "candidates").glob(
                    "cand_*.json"
                )
            )
            self.assertEqual(len(candidate_files), 1)
            candidate = json.loads(candidate_files[0].read_text(encoding="utf-8"))
            self.assertEqual(candidate["occurrence_count"], 5)
            self.assertGreater(candidate["last_seen_at"], candidate["created_at"])

    def test_candidate_expiry_uses_the_configured_retention_period(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            configuration_path = library_root / "myoutbrain.toml"
            configuration = configuration_path.read_text(encoding="utf-8")
            configuration = configuration.replace(
                "candidate_ttl_days = 30",
                "candidate_ttl_days = 7",
            )
            configuration_path.write_text(configuration, encoding="utf-8")

            result = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            candidate_path = next(
                (library_root / "runtime" / "workspace" / "candidates").glob(
                    "cand_*.json"
                )
            )
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(candidate["created_at"])
            expires_at = datetime.fromisoformat(candidate["expires_at"])
            self.assertEqual((expires_at - created_at).days, 7)

    def test_provider_failure_does_not_create_partial_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )

            result = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_ERROR": "timeout",
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id),
                },
            )

            self.assertEqual(result.returncode, 6)
            self.assertIn("timeout", result.stderr)
            candidate_files = list(
                (library_root / "runtime" / "workspace" / "candidates").glob(
                    "cand_*.json"
                )
            )
            self.assertEqual(candidate_files, [])
            self.assertEqual(list((library_root / "vault").rglob("*.md")), [])

    def test_recent_rejection_fingerprint_suppresses_duplicate_without_retaining_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            fingerprint = (
                "c8d659080567a0ef646c1fa9a72200dbc93766993139204783809f0b5743b86b"
            )
            rejected_directory = (
                library_root
                / "runtime"
                / "workspace"
                / "candidates"
                / "rejected"
            )
            rejected_directory.mkdir()
            rejection_path = rejected_directory / f"rej_{fingerprint}.json"
            rejection_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fingerprint": f"sha256:{fingerprint}",
                        "rejected_at": "2026-07-16T00:00:00+00:00",
                        "suppress_until": "2099-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                "reflect",
                source_id,
                "Find a reusable insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id)
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recently rejected", result.stdout)
            candidate_files = list(
                (library_root / "runtime" / "workspace" / "candidates").glob(
                    "cand_*.json"
                )
            )
            self.assertEqual(candidate_files, [])
            rejection_data = rejection_path.read_text(encoding="utf-8")
            self.assertNotIn("Reflection turns experience", rejection_data)

    def test_reflection_sends_only_the_evidence_package_and_writes_compact_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root, source_id = initialize_cloud_source(temporary_root)
            request_file = temporary_root / "request.json"
            prompt = "Find a reusable insight."

            result = run_cli(
                "reflect",
                source_id,
                prompt,
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id),
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_file),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            request = json.loads(request_file.read_text(encoding="utf-8"))
            self.assertEqual(request["purpose"], "reflect-on-source")
            self.assertEqual(request["authorization"], {"allow_cloud": True})
            self.assertEqual(request["evidence_package"]["question"], prompt)
            evidence = request["evidence_package"]["evidence"]
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["source_id"], source_id)
            self.assertEqual(
                evidence[0]["content"],
                "Reflection makes accumulated experience reusable.\n",
            )
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            events = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
            ]
            external_call = events[-1]
            self.assertEqual(external_call["type"], "model.external_call")
            self.assertEqual(external_call["purpose"], "reflect-on-source")
            self.assertEqual(external_call["source_ids"], [source_id])
            serialized_audit = json.dumps(external_call, ensure_ascii=False)
            self.assertNotIn(prompt, serialized_audit)
            self.assertNotIn("accumulated experience", serialized_audit)

    def test_insufficient_evidence_creates_no_candidate_from_model_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            response = json.dumps(
                {
                    "candidates": [
                        {
                            "text": "The Moon is made of cheese.",
                            "supporting_evidence": [
                                {
                                    "source_id": source_id,
                                    "locator": source_locator(source_id),
                                }
                            ],
                            "contrary_evidence": [],
                            "derivation": "Unsupported guess.",
                        }
                    ],
                    "insufficient_evidence": True,
                }
            )

            result = run_cli(
                "reflect",
                source_id,
                "What is the Moon made of?",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={"MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": response},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Insufficient evidence", result.stdout)
            self.assertNotIn("Moon", result.stdout)
            self.assertEqual(
                list(
                    (library_root / "runtime" / "workspace" / "candidates").glob(
                        "cand_*.json"
                    )
                ),
                [],
            )

    def test_interrupted_multi_candidate_write_recovers_without_partial_candidates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            response = json.dumps(
                {
                    "candidates": [
                        {
                            "text": "Reflection turns experience into reusable guidance.",
                            "supporting_evidence": [
                                {
                                    "source_id": source_id,
                                    "locator": source_locator(source_id),
                                }
                            ],
                            "contrary_evidence": [],
                            "derivation": "Generalizes the source.",
                        },
                        {
                            "text": "Reusable guidance may compound future learning.",
                            "supporting_evidence": [
                                {
                                    "source_id": source_id,
                                    "locator": source_locator(source_id),
                                }
                            ],
                            "contrary_evidence": [],
                            "derivation": "Extends the reuse implication.",
                        },
                    ],
                    "insufficient_evidence": False,
                }
            )

            interrupted = run_cli(
                "reflect",
                source_id,
                "Find reusable insights.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": response,
                    "MYOUTBRAIN_FAULT_INJECTION": "reflect-after-first-replace",
                },
            )
            recovered = run_cli("init", "--root", str(library_root))

            self.assertEqual(interrupted.returncode, 86)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(
                list(
                    (library_root / "runtime" / "workspace" / "candidates").glob(
                        "cand_*.json"
                    )
                ),
                [],
            )

    def test_local_only_source_never_reaches_reflection_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Private.md"
            source_path.write_bytes(b"Private reflection.\n")
            capture = run_cli(
                "capture",
                str(source_path),
                "--sensitivity",
                "local-only",
                "--root",
                str(library_root),
            )
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            source_id = identity.group(0) if identity is not None else ""
            request_file = temporary_root / "request.json"

            result = run_cli(
                "reflect",
                source_id,
                "Find an insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": reflection_response(source_id),
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_file),
                },
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("not eligible for cloud use", result.stderr)
            self.assertFalse(request_file.exists())

    def test_candidate_citations_must_belong_to_reflection_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root, source_id = initialize_cloud_source(
                Path(temporary_directory)
            )
            response = json.dumps(
                {
                    "candidates": [
                        {
                            "text": "Unsupported candidate.",
                            "supporting_evidence": [
                                {
                                    "source_id": "src_" + ("0" * 64),
                                    "locator": "store/objects/sha256/00/00/invalid#L1-L1",
                                }
                            ],
                            "contrary_evidence": [],
                            "derivation": "Invented association.",
                        }
                    ],
                    "insufficient_evidence": False,
                }
            )

            result = run_cli(
                "reflect",
                source_id,
                "Find an insight.",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={"MYOUTBRAIN_FAKE_REFLECTION_RESPONSE": response},
            )

            self.assertEqual(result.returncode, 6)
            self.assertIn("citation is outside the evidence package", result.stderr)
            self.assertEqual(
                list(
                    (library_root / "runtime" / "workspace" / "candidates").glob(
                        "cand_*.json"
                    )
                ),
                [],
            )

    def test_openai_adapter_translates_reflection_to_responses_api(self) -> None:
        requests: list[dict[str, object]] = []

        class ResponsesHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                request_data = json.loads(body["input"])
                evidence = request_data["evidence_package"]["evidence"][0]
                structured_reflection = json.dumps(
                    {
                        "candidates": [
                            {
                                "text": "Reflection makes guidance reusable.",
                                "supporting_evidence": [
                                    {
                                        "source_id": evidence["source_id"],
                                        "locator": evidence["locator"],
                                    }
                                ],
                                "contrary_evidence": [],
                                "derivation": "Generalizes the evidence.",
                            }
                        ],
                        "insufficient_evidence": False,
                    }
                )
                response = json.dumps(
                    {
                        "output": [
                            {
                                "content": [
                                    {"type": "output_text", "text": structured_reflection}
                                ]
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                library_root, source_id = initialize_cloud_source(
                    Path(temporary_directory)
                )
                configure_generation(library_root, "openai", "contract-model")

                result = run_cli(
                    "reflect",
                    source_id,
                    "Find an insight.",
                    "--allow-cloud",
                    "--root",
                    str(library_root),
                    environment={
                        "OPENAI_API_KEY": "contract-secret",
                        "MYOUTBRAIN_OPENAI_BASE_URL": (
                            f"http://127.0.0.1:{server.server_port}/v1"
                        ),
                    },
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["path"], "/v1/responses")
                self.assertEqual(
                    requests[0]["authorization"],
                    "Bearer contract-secret",
                )
                body = requests[0]["body"]
                self.assertIsInstance(body, dict)
                if not isinstance(body, dict):
                    self.fail("request body is not an object")
                self.assertEqual(body["model"], "contract-model")
                self.assertIs(body["store"], False)
                output_format = body["text"]["format"]
                self.assertEqual(output_format["type"], "json_schema")
                self.assertEqual(output_format["name"], "grounded_reflection")
                self.assertIs(output_format["strict"], True)
                required = output_format["schema"]["properties"]["candidates"][
                    "items"
                ]["required"]
                self.assertEqual(
                    required,
                    [
                        "text",
                        "supporting_evidence",
                        "contrary_evidence",
                        "derivation",
                    ],
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()


if __name__ == "__main__":
    unittest.main()
