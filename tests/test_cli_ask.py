from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import unittest

from tests.cli_support import run_cli


def configure_generation(library_root: Path, provider: str, model: str) -> None:
    configuration_path = library_root / "myoutbrain.toml"
    configuration = configuration_path.read_text(encoding="utf-8")
    if "[generation]" in configuration:
        configuration = re.sub(
            r"\[generation\]\nprovider = \"[^\"]+\"\nmodel = \"[^\"]+\"",
            f'[generation]\nprovider = "{provider}"\nmodel = "{model}"',
            configuration,
        )
    else:
        configuration += (
            f'\n[generation]\nprovider = "{provider}"\nmodel = "{model}"\n'
        )
    configuration_path.write_text(configuration, encoding="utf-8")


def configure_fake_generation(library_root: Path) -> None:
    configure_generation(library_root, "fake", "deterministic-test")


def source_locator(source_id: str, line_count: int = 1) -> str:
    digest = source_id.removeprefix("src_")
    return (
        f"store/objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        f"#L1-L{line_count}"
    )


def grounded_response(
    source_id: str,
    text: str,
    *,
    insufficient_evidence: bool = False,
) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "text": text,
                    "source_id": source_id,
                    "locator": source_locator(source_id),
                }
            ],
            "insufficient_evidence": insufficient_evidence,
        }
    )


class AskFromEvidencePackageTests(unittest.TestCase):
    def test_new_library_configures_openai_as_the_first_generation_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"

            initialization = run_cli("init", "--root", str(library_root))

            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configuration = (library_root / "myoutbrain.toml").read_text(encoding="utf-8")
            self.assertIn("[generation]", configuration)
            self.assertIn('provider = "openai"', configuration)
            self.assertRegex(configuration, r'model = "[^\"]+"')
            self.assertNotIn("api_key", configuration.lower())
            self.assertNotIn("sk-", configuration)

    def test_library_from_before_generation_config_uses_the_openai_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configuration_path = library_root / "myoutbrain.toml"
            legacy_configuration = re.sub(
                r'\n\[generation\]\nprovider = "openai"\nmodel = "[^"]+"\n',
                "\n",
                configuration_path.read_text(encoding="utf-8"),
            )
            configuration_path.write_text(legacy_configuration, encoding="utf-8")
            source_path = temporary_root / "legacy.md"
            source_path.write_text("# Legacy\n\nStill readable.\n", encoding="utf-8")
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

            result = run_cli(
                "ask",
                source_id,
                "What is readable?",
                "--allow-cloud",
                "--root",
                str(library_root),
                environment={"OPENAI_API_KEY": ""},
            )

            self.assertEqual(result.returncode, 6)
            self.assertIn("OPENAI_API_KEY", result.stderr)
            self.assertNotIn("generation configuration", result.stderr)

    def test_reinitializing_a_legacy_library_persists_generation_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            library_root.mkdir()
            configuration_path = library_root / "myoutbrain.toml"
            configuration_path.write_text(
                "# retained user comment\n"
                "schema_version = 1\n"
                "single_writer = true\n\n"
                "[storage]\n"
                'permanent = ["vault", "store"]\n'
                'rebuildable = ["runtime"]\n',
                encoding="utf-8",
            )

            result = run_cli("init", "--root", str(library_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            migrated = configuration_path.read_text(encoding="utf-8")
            self.assertIn("# retained user comment", migrated)
            self.assertIn("[generation]", migrated)
            self.assertIn('provider = "openai"', migrated)
            self.assertIn('model = "gpt-5-mini"', migrated)

    def test_creator_can_ask_a_captured_source_and_verify_the_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Reflection.md"
            source_path.write_text(
                "Reflection turns accumulated experience into reusable knowledge.\n",
                encoding="utf-8",
            )
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            fake_response = grounded_response(
                source_id,
                "Reflection makes accumulated experience reusable.",
            )

            result = run_cli(
                "ask",
                source_id,
                "What does reflection do?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={"MYOUTBRAIN_FAKE_RESPONSE": fake_response},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Reflection makes accumulated experience reusable.", result.stdout)
            self.assertIn(source_id, result.stdout)
            self.assertRegex(result.stdout, r"store/objects/sha256/.+#L1-L1")

    def test_cloud_call_receives_only_the_evidence_package_and_writes_a_compact_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Private Notes.md"
            source_content = "The smallest useful context is the evidence package.\n"
            source_path.write_text(source_content, encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            request_file = temporary_root / "fake-request.json"
            question = "What is the smallest useful context?"

            result = run_cli(
                "ask",
                source_id,
                question,
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": grounded_response(
                        source_id,
                        "It is the evidence package.",
                    ),
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_file),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            recorded_request = json.loads(request_file.read_text(encoding="utf-8"))
            self.assertEqual(recorded_request["purpose"], "answer-question")
            self.assertEqual(recorded_request["authorization"], {"allow_cloud": True})
            self.assertEqual(recorded_request["evidence_package"]["question"], question)
            self.assertEqual(len(recorded_request["evidence_package"]["evidence"]), 1)
            recorded_evidence = recorded_request["evidence_package"]["evidence"][0]
            self.assertEqual(recorded_evidence["source_id"], source_id)
            self.assertEqual(recorded_evidence["content"], source_path.read_bytes().decode("utf-8"))
            self.assertRegex(recorded_evidence["locator"], r"store/objects/sha256/.+#L1-L1")
            journal_path = library_root / "store" / "journal" / "events.jsonl"
            audit = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(audit["type"], "model.external_call")
            self.assertEqual(audit["provider"], "fake")
            self.assertEqual(audit["model"], "deterministic-test")
            self.assertEqual(audit["purpose"], "answer-question")
            self.assertEqual(audit["source_ids"], [source_id])
            self.assertRegex(audit["request_fingerprint"], r"^sha256:[0-9a-f]{64}$")
            serialized_audit = json.dumps(audit, ensure_ascii=False)
            self.assertNotIn(source_content.strip(), serialized_audit)
            self.assertNotIn(question, serialized_audit)

    def test_unanswerable_question_reports_insufficient_evidence_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Reflection.md"
            source_path.write_text("Reflection makes experience reusable.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""

            result = run_cli(
                "ask",
                source_id,
                "What is the capital of France?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": grounded_response(
                        source_id,
                        "Paris.",
                        insufficient_evidence=True,
                    )
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Insufficient evidence", result.stdout)
            self.assertNotIn("Paris", result.stdout)
            self.assertIn(source_id, result.stdout)

    def test_cloud_authorization_applies_to_only_one_ask_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Eligible.md"
            source_path.write_text("Cloud use requires current permission.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            fake_response = grounded_response(
                source_id,
                "Current permission is required.",
            )
            first_request = run_cli(
                "ask",
                source_id,
                "What is required?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={"MYOUTBRAIN_FAKE_RESPONSE": fake_response},
            )

            second_request_file = temporary_root / "second-request.json"
            second_request = run_cli(
                "ask",
                source_id,
                "What is required?",
                "--root",
                str(library_root),
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": fake_response,
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(second_request_file),
                },
            )

            self.assertEqual(first_request.returncode, 0, first_request.stderr)
            self.assertEqual(second_request.returncode, 2)
            self.assertIn("explicit --allow-cloud authorization", second_request.stderr)
            self.assertFalse(second_request_file.exists())
            journal_lines = (
                library_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            external_calls = [
                json.loads(line) for line in journal_lines if json.loads(line)["type"] == "model.external_call"
            ]
            self.assertEqual(len(external_calls), 1)

    def test_local_only_source_never_reaches_the_generation_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Local.md"
            source_path.write_text("This material must remain local.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "local-only",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            request_file = temporary_root / "forbidden-request.json"

            result = run_cli(
                "ask",
                source_id,
                "May this leave the device?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {"answer": "No.", "insufficient_evidence": False}
                    ),
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_file),
                },
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("not eligible for cloud generation", result.stderr)
            self.assertFalse(request_file.exists())
            journal_lines = (
                library_root / "store" / "journal" / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertFalse(
                any(json.loads(line)["type"] == "model.external_call" for line in journal_lines)
            )

    def test_missing_sensitivity_is_an_integrity_error_and_is_never_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Unknown Sensitivity.md"
            source_path.write_text("Sensitivity must be explicit.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            record_path = library_root / "store" / "records" / f"{source_id}.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            del record["sensitivity"]
            record_path.write_text(json.dumps(record), encoding="utf-8")
            request_file = temporary_root / "forbidden-request.json"

            result = run_cli(
                "ask",
                source_id,
                "Can this be sent?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {"answer": "No.", "insufficient_evidence": False}
                    ),
                    "MYOUTBRAIN_FAKE_REQUEST_FILE": str(request_file),
                },
            )

            self.assertEqual(result.returncode, 7)
            self.assertIn("Integrity failure", result.stderr)
            self.assertFalse(request_file.exists())

    def test_provider_timeout_refusal_and_invalid_result_use_stable_failure(self) -> None:
        failure_scenarios = (
            ("timeout", {"MYOUTBRAIN_FAKE_ERROR": "timeout"}, "timeout"),
            ("refusal", {"MYOUTBRAIN_FAKE_ERROR": "refusal"}, "refused"),
            ("invalid", {"MYOUTBRAIN_FAKE_RESPONSE": "{}"}, "invalid result"),
        )
        for scenario, provider_environment, expected_error in failure_scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                library_root = temporary_root / "My Knowledge"
                initialization = run_cli("init", "--root", str(library_root))
                self.assertEqual(initialization.returncode, 0, initialization.stderr)
                configure_fake_generation(library_root)
                source_path = temporary_root / "Failure.md"
                source_path.write_text("Provider errors must not become knowledge.\n", encoding="utf-8")
                capture = run_cli(
                    "capture",
                    str(source_path),
                    "--root",
                    str(library_root),
                    "--sensitivity",
                    "cloud-allowed",
                )
                self.assertEqual(capture.returncode, 0, capture.stderr)
                identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
                self.assertIsNotNone(identity)
                source_id = identity.group(0) if identity is not None else ""
                permanent_paths = (
                    library_root / "vault",
                    library_root / "store" / "objects",
                    library_root / "store" / "records",
                )
                before = {
                    path.relative_to(library_root).as_posix(): path.read_bytes()
                    for permanent_root in permanent_paths
                    for path in permanent_root.rglob("*")
                    if path.is_file()
                }
                secret = "sk-test-secret-that-must-never-appear"
                environment = {"OPENAI_API_KEY": secret, **provider_environment}

                result = run_cli(
                    "ask",
                    source_id,
                    "What must provider errors not become?",
                    "--root",
                    str(library_root),
                    "--allow-cloud",
                    environment=environment,
                )

                self.assertEqual(result.returncode, 6)
                self.assertIn("Provider failure", result.stderr)
                self.assertIn(expected_error, result.stderr)
                self.assertNotIn(secret, result.stdout)
                self.assertNotIn(secret, result.stderr)
                after = {
                    path.relative_to(library_root).as_posix(): path.read_bytes()
                    for permanent_root in permanent_paths
                    for path in permanent_root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                audit_text = (
                    library_root / "store" / "journal" / "events.jsonl"
                ).read_text(encoding="utf-8")
                self.assertIn("model.external_call", audit_text)
                self.assertNotIn(secret, audit_text)

    def test_openai_adapter_translates_the_generation_contract_to_responses_api(self) -> None:
        recorded_requests: list[dict[str, object]] = []

        class ResponsesHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                recorded_requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                request_data = json.loads(body["input"])
                evidence = request_data["evidence_package"]["evidence"][0]
                structured_answer = json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The evidence package is the smallest useful context.",
                                "source_id": evidence["source_id"],
                                "locator": evidence["locator"],
                            }
                        ],
                        "insufficient_evidence": False,
                    }
                )
                response = json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": structured_answer}
                                ],
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
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                temporary_root = Path(temporary_directory)
                library_root = temporary_root / "My Knowledge"
                initialization = run_cli("init", "--root", str(library_root))
                self.assertEqual(initialization.returncode, 0, initialization.stderr)
                configure_generation(library_root, "openai", "gpt-5-mini")
                source_path = temporary_root / "Evidence.md"
                source_path.write_text(
                    "The evidence package is the smallest useful context.\n",
                    encoding="utf-8",
                )
                capture = run_cli(
                    "capture",
                    str(source_path),
                    "--root",
                    str(library_root),
                    "--sensitivity",
                    "cloud-allowed",
                )
                self.assertEqual(capture.returncode, 0, capture.stderr)
                identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
                self.assertIsNotNone(identity)
                source_id = identity.group(0) if identity is not None else ""
                api_key = "sk-contract-test-only"

                result = run_cli(
                    "ask",
                    source_id,
                    "What is the smallest useful context?",
                    "--root",
                    str(library_root),
                    "--allow-cloud",
                    environment={
                        "OPENAI_API_KEY": api_key,
                        "MYOUTBRAIN_OPENAI_BASE_URL": (
                            f"http://127.0.0.1:{server.server_address[1]}/v1"
                        ),
                    },
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("The evidence package is the smallest useful context.", result.stdout)
                self.assertEqual(len(recorded_requests), 1)
                request = recorded_requests[0]
                self.assertEqual(request["path"], "/v1/responses")
                self.assertEqual(request["authorization"], f"Bearer {api_key}")
                self.assertEqual(request["content_type"], "application/json")
                body = request["body"]
                self.assertIsInstance(body, dict)
                if not isinstance(body, dict):
                    self.fail("OpenAI request body was not an object")
                self.assertEqual(body["model"], "gpt-5-mini")
                self.assertIs(body["store"], False)
                self.assertIn("evidence_package", body["input"])
                text_format = body["text"]["format"]
                self.assertEqual(text_format["type"], "json_schema")
                self.assertEqual(text_format["name"], "grounded_answer")
                self.assertIs(text_format["strict"], True)
                self.assertNotIn(api_key, result.stdout)
                self.assertNotIn(api_key, result.stderr)
                audit_text = (
                    library_root / "store" / "journal" / "events.jsonl"
                ).read_text(encoding="utf-8")
                self.assertNotIn(api_key, audit_text)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_openai_adapter_requires_the_api_key_from_the_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            source_path = temporary_root / "Evidence.md"
            source_path.write_text("Credentials belong in the environment.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""

            result = run_cli(
                "ask",
                source_id,
                "Where do credentials belong?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={"OPENAI_API_KEY": ""},
            )

            self.assertEqual(result.returncode, 6)
            self.assertIn("OPENAI_API_KEY is not configured", result.stderr)

    def test_invalid_source_identity_is_rejected_before_storage_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            library_root = Path(temporary_directory) / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            crafted_identity = "src_../../" + ("a" * 58)

            result = run_cli(
                "ask",
                crafted_identity,
                "Can this escape the record directory?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {"answer": "No.", "insufficient_evidence": False}
                    )
                },
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid source identity", result.stderr)

    def test_answer_without_claim_evidence_associations_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Grounded.md"
            source_path.write_text("Reflection makes experience reusable.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""

            result = run_cli(
                "ask",
                source_id,
                "What does reflection do?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": json.dumps(
                        {
                            "answer": (
                                "Reflection makes experience reusable, and the Moon is made of cheese."
                            ),
                            "insufficient_evidence": False,
                        }
                    )
                },
            )

            self.assertEqual(result.returncode, 6)
            self.assertIn("invalid result", result.stderr)
            self.assertNotIn("Moon", result.stdout)

    def test_claim_citation_must_belong_to_the_current_evidence_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            library_root = temporary_root / "My Knowledge"
            initialization = run_cli("init", "--root", str(library_root))
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            configure_fake_generation(library_root)
            source_path = temporary_root / "Grounded.md"
            source_path.write_text("Reflection makes experience reusable.\n", encoding="utf-8")
            capture = run_cli(
                "capture",
                str(source_path),
                "--root",
                str(library_root),
                "--sensitivity",
                "cloud-allowed",
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            identity = re.search(r"src_[0-9a-f]{64}", capture.stdout)
            self.assertIsNotNone(identity)
            source_id = identity.group(0) if identity is not None else ""
            unrelated_source_id = "src_" + ("0" * 64)

            result = run_cli(
                "ask",
                source_id,
                "What does reflection do?",
                "--root",
                str(library_root),
                "--allow-cloud",
                environment={
                    "MYOUTBRAIN_FAKE_RESPONSE": grounded_response(
                        unrelated_source_id,
                        "The Moon is made of cheese.",
                    )
                },
            )

            self.assertEqual(result.returncode, 6)
            self.assertIn("citation is outside the evidence package", result.stderr)
            self.assertNotIn("Moon", result.stdout)


if __name__ == "__main__":
    unittest.main()
