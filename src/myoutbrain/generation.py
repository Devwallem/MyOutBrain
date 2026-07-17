from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Protocol
from urllib import error as url_error
from urllib import request as url_request


class ProviderFailure(Exception):
    """Raised when a generation provider cannot return a valid answer."""


@dataclass(frozen=True)
class Citation:
    source_id: str
    locator: str

    def to_data(self) -> dict[str, str]:
        return {"source_id": self.source_id, "locator": self.locator}


@dataclass(frozen=True)
class EvidenceItem:
    citation: Citation
    content: str

    def to_data(self) -> dict[str, str]:
        return {
            **self.citation.to_data(),
            "content": self.content,
        }


@dataclass(frozen=True)
class EvidencePackage:
    question: str
    items: tuple[EvidenceItem, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "question": self.question,
            "evidence": [item.to_data() for item in self.items],
        }


@dataclass(frozen=True)
class CloudAuthorization:
    allow_cloud: bool

    def to_data(self) -> dict[str, bool]:
        return {"allow_cloud": self.allow_cloud}


@dataclass(frozen=True)
class GenerationRequest:
    purpose: str
    authorization: CloudAuthorization
    evidence_package: EvidencePackage
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "purpose": self.purpose,
            "authorization": self.authorization.to_data(),
            "evidence_package": self.evidence_package.to_data(),
        }
        if self.max_output_tokens is not None:
            data["max_output_tokens"] = self.max_output_tokens
        if self.max_cost_usd is not None:
            data["max_cost_usd"] = self.max_cost_usd
        return data


@dataclass(frozen=True)
class GeneratedClaim:
    text: str
    citation: Citation


@dataclass(frozen=True)
class GeneratedAnswer:
    claims: tuple[GeneratedClaim, ...]
    insufficient_evidence: bool


@dataclass(frozen=True)
class GeneratedCandidate:
    text: str
    supporting_evidence: tuple[Citation, ...]
    contrary_evidence: tuple[Citation, ...]
    derivation: str


@dataclass(frozen=True)
class GeneratedReflection:
    candidates: tuple[GeneratedCandidate, ...]
    insufficient_evidence: bool


class GenerationProvider(Protocol):
    name: str
    model: str

    def generate(self, request: GenerationRequest) -> GeneratedAnswer: ...

    def reflect(self, request: GenerationRequest) -> GeneratedReflection: ...


class FakeGenerationProvider:
    name = "fake"

    def __init__(self, model: str) -> None:
        self.model = model

    def generate(self, request: GenerationRequest) -> GeneratedAnswer:
        response = self._load_response(
            request,
            environment_name="MYOUTBRAIN_FAKE_RESPONSE",
            missing_message="fake provider response is not configured",
        )
        try:
            return _parse_generated_answer(response)
        except (KeyError, TypeError) as error:
            raise ProviderFailure("fake provider returned an invalid result") from error

    def reflect(self, request: GenerationRequest) -> GeneratedReflection:
        response = self._load_response(
            request,
            environment_name="MYOUTBRAIN_FAKE_REFLECTION_RESPONSE",
            missing_message="fake reflection response is not configured",
        )
        try:
            return _parse_generated_reflection(response)
        except (KeyError, TypeError) as error:
            raise ProviderFailure("fake provider returned an invalid result") from error

    def _load_response(
        self,
        request: GenerationRequest,
        *,
        environment_name: str,
        missing_message: str,
    ) -> object:
        request_file = os.environ.get("MYOUTBRAIN_FAKE_REQUEST_FILE")
        if request_file is not None:
            with open(request_file, "w", encoding="utf-8") as recorded_request:
                json.dump(request.to_data(), recorded_request, ensure_ascii=False, indent=2)
                recorded_request.write("\n")
        simulated_error = os.environ.get("MYOUTBRAIN_FAKE_ERROR")
        if simulated_error == "timeout":
            raise ProviderFailure("generation provider timeout")
        if simulated_error == "refusal":
            raise ProviderFailure("generation provider refused the request")
        serialized_response = os.environ.get(environment_name)
        if serialized_response is None:
            raise ProviderFailure(missing_message)
        try:
            return json.loads(serialized_response)
        except json.JSONDecodeError as error:
            raise ProviderFailure("fake provider returned an invalid result") from error


class OpenAIGenerationProvider:
    name = "openai"

    def __init__(self, model: str) -> None:
        self.model = model

    def generate(self, request: GenerationRequest) -> GeneratedAnswer:
        schema = {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "source_id": {"type": "string"},
                            "locator": {"type": "string"},
                        },
                        "required": ["text", "source_id", "locator"],
                        "additionalProperties": False,
                    },
                },
                "insufficient_evidence": {"type": "boolean"},
            },
            "required": ["claims", "insufficient_evidence"],
            "additionalProperties": False,
        }
        response = self._request_structured(
            request,
            format_name="grounded_answer",
            instructions=(
                "Answer only from the supplied evidence package. If it does not support an answer, "
                "set insufficient_evidence to true. Do not use outside knowledge."
            ),
            schema=schema,
        )
        try:
            return _parse_generated_answer(response)
        except (KeyError, TypeError) as error:
            raise ProviderFailure("OpenAI Responses API returned an invalid result") from error

    def reflect(self, request: GenerationRequest) -> GeneratedReflection:
        citation_schema = {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "locator": {"type": "string"},
            },
            "required": ["source_id", "locator"],
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "supporting_evidence": {
                                "type": "array",
                                "items": citation_schema,
                            },
                            "contrary_evidence": {
                                "type": "array",
                                "items": citation_schema,
                            },
                            "derivation": {"type": "string"},
                        },
                        "required": [
                            "text",
                            "supporting_evidence",
                            "contrary_evidence",
                            "derivation",
                        ],
                        "additionalProperties": False,
                    },
                },
                "insufficient_evidence": {"type": "boolean"},
            },
            "required": ["candidates", "insufficient_evidence"],
            "additionalProperties": False,
        }
        response = self._request_structured(
            request,
            format_name="grounded_reflection",
            instructions=(
                "Propose candidate insights only from the supplied evidence package. Include "
                "supporting evidence, contrary evidence when present, and a derivation summary. "
                "If the evidence cannot support a candidate, set insufficient_evidence to true."
            ),
            schema=schema,
        )
        try:
            return _parse_generated_reflection(response)
        except (KeyError, TypeError) as error:
            raise ProviderFailure("OpenAI Responses API returned an invalid result") from error

    def _request_structured(
        self,
        request: GenerationRequest,
        *,
        format_name: str,
        instructions: str,
        schema: dict[str, object],
    ) -> object:
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key is None or not api_key.strip():
            raise ProviderFailure("OPENAI_API_KEY is not configured")
        base_url = os.environ.get("MYOUTBRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1")
        endpoint = f"{base_url.rstrip('/')}/responses"
        body = {
            "model": self.model,
            "store": False,
            "instructions": instructions,
            "input": json.dumps(request.to_data(), ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": format_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        if request.max_output_tokens is not None:
            body["max_output_tokens"] = request.max_output_tokens
        api_request = url_request.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with url_request.urlopen(api_request, timeout=30) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except url_error.HTTPError as error:
            raise ProviderFailure("OpenAI Responses API rejected the request") from error
        except (url_error.URLError, TimeoutError) as error:
            raise ProviderFailure("OpenAI Responses API timeout or connection failure") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProviderFailure("OpenAI Responses API returned invalid JSON") from error

        try:
            if not isinstance(response_data, dict):
                raise TypeError("response is not an object")
            output = response_data["output"]
            if not isinstance(output, list):
                raise TypeError("output is not a list")
            for output_item in output:
                if not isinstance(output_item, dict):
                    continue
                content = output_item.get("content")
                if not isinstance(content, list):
                    continue
                for content_item in content:
                    if not isinstance(content_item, dict):
                        continue
                    if content_item.get("type") == "refusal":
                        raise ProviderFailure("OpenAI Responses API refused the request")
                    if content_item.get("type") != "output_text":
                        continue
                    output_text = content_item.get("text")
                    if not isinstance(output_text, str):
                        raise TypeError("output text is invalid")
                    return json.loads(output_text)
        except ProviderFailure:
            raise
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ProviderFailure("OpenAI Responses API returned an invalid result") from error
        raise ProviderFailure("OpenAI Responses API returned no result")


def _parse_generated_answer(response: object) -> GeneratedAnswer:
    if not isinstance(response, dict):
        raise TypeError("generated answer is not an object")
    claims_data = response["claims"]
    insufficient_evidence = response["insufficient_evidence"]
    if not isinstance(claims_data, list):
        raise TypeError("claims must be a list")
    if not isinstance(insufficient_evidence, bool):
        raise TypeError("insufficient_evidence must be boolean")
    claims: list[GeneratedClaim] = []
    for claim_data in claims_data:
        if not isinstance(claim_data, dict):
            raise TypeError("claim must be an object")
        text = claim_data.get("text")
        source_id = claim_data.get("source_id")
        locator = claim_data.get("locator")
        if not isinstance(text, str) or not text.strip():
            raise TypeError("claim text must be nonblank")
        if not isinstance(source_id, str) or not source_id:
            raise TypeError("claim source identity is invalid")
        if not isinstance(locator, str) or not locator:
            raise TypeError("claim locator is invalid")
        claims.append(
            GeneratedClaim(
                text=text,
                citation=Citation(source_id=source_id, locator=locator),
            )
        )
    if not insufficient_evidence and not claims:
        raise TypeError("answerable result must contain at least one claim")
    return GeneratedAnswer(claims=tuple(claims), insufficient_evidence=insufficient_evidence)


def _parse_citations(value: object, field: str) -> tuple[Citation, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    citations: list[Citation] = []
    for citation_data in value:
        if not isinstance(citation_data, dict):
            raise TypeError(f"{field} citation must be an object")
        source_id = citation_data.get("source_id")
        locator = citation_data.get("locator")
        if not isinstance(source_id, str) or not source_id:
            raise TypeError(f"{field} source identity is invalid")
        if not isinstance(locator, str) or not locator:
            raise TypeError(f"{field} locator is invalid")
        citations.append(Citation(source_id=source_id, locator=locator))
    return tuple(citations)


def _parse_generated_reflection(response: object) -> GeneratedReflection:
    if not isinstance(response, dict):
        raise TypeError("generated reflection is not an object")
    candidates_data = response["candidates"]
    insufficient_evidence = response["insufficient_evidence"]
    if not isinstance(candidates_data, list):
        raise TypeError("candidates must be a list")
    if not isinstance(insufficient_evidence, bool):
        raise TypeError("insufficient_evidence must be boolean")
    candidates: list[GeneratedCandidate] = []
    for candidate_data in candidates_data:
        if not isinstance(candidate_data, dict):
            raise TypeError("candidate must be an object")
        text = candidate_data.get("text")
        derivation = candidate_data.get("derivation")
        if not isinstance(text, str) or not text.strip():
            raise TypeError("candidate text must be nonblank")
        if not isinstance(derivation, str) or not derivation.strip():
            raise TypeError("candidate derivation must be nonblank")
        supporting_evidence = _parse_citations(
            candidate_data.get("supporting_evidence"),
            "supporting_evidence",
        )
        if not supporting_evidence:
            raise TypeError("candidate must contain supporting evidence")
        candidates.append(
            GeneratedCandidate(
                text=text,
                supporting_evidence=supporting_evidence,
                contrary_evidence=_parse_citations(
                    candidate_data.get("contrary_evidence"),
                    "contrary_evidence",
                ),
                derivation=derivation,
            )
        )
    if not insufficient_evidence and not candidates:
        raise TypeError("supported reflection must contain at least one candidate")
    return GeneratedReflection(
        candidates=tuple(candidates),
        insufficient_evidence=insufficient_evidence,
    )


def create_generation_provider(provider_name: str, model: str) -> GenerationProvider:
    if provider_name == "fake":
        return FakeGenerationProvider(model)
    if provider_name == "openai":
        return OpenAIGenerationProvider(model)
    raise ProviderFailure(f"unsupported generation provider: {provider_name}")
