from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import tomllib
from typing import Literal

from myoutbrain.core_types import ConfigurationConflict, Sensitivity, UserInputError
from myoutbrain.generation import (
    Citation,
    CloudAuthorization,
    EvidenceItem,
    EvidencePackage,
    GenerationRequest,
    GenerationProvider,
    GeneratedAnswer,
    GeneratedClaim,
    ProviderFailure,
    create_generation_provider,
)
from myoutbrain.local_core import LocalMemoryCore
from myoutbrain.memory_gateway import (
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)
from myoutbrain.public_search import (
    PublicSource,
    sanitized_public_query,
    search_public_sources,
)


AnswerStatus = Literal["answered", "unknown"]
AnswerOrigin = Literal[
    "common-knowledge",
    "public-evidence",
    "companion-inference",
]


@dataclass(frozen=True)
class AnswerRequest:
    question: str
    task: str
    access: MemoryAccess
    query_sensitivity: Sensitivity = "local-only"
    allow_cloud: bool = False
    high_risk: bool = False
    time_sensitive: bool = False
    memory_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    limit: int = 5


@dataclass(frozen=True)
class TraceableClaim:
    text: str
    source_ids: tuple[str, ...]
    origin: AnswerOrigin

    def to_data(self) -> dict[str, object]:
        return {
            "text": self.text,
            "source_ids": list(self.source_ids),
            "origin": self.origin,
        }


@dataclass(frozen=True)
class CompanionAnswer:
    status: AnswerStatus
    answerability: Literal["sufficient", "insufficient"]
    claims: tuple[TraceableClaim, ...]
    public_search_performed: bool
    public_query: str | None
    public_sources: tuple[PublicSource, ...]
    verified_facts: tuple[str, ...]
    unresolved_gaps: tuple[str, ...]
    next_steps: tuple[str, ...]
    companion_inference: str | None
    memory_update_id: str | None

    def to_data(self) -> dict[str, object]:
        return {
            "status": self.status,
            "answerability": self.answerability,
            "claims": [claim.to_data() for claim in self.claims],
            "public_search_performed": self.public_search_performed,
            "public_query": self.public_query,
            "public_sources": [source.to_data() for source in self.public_sources],
            "verified_facts": list(self.verified_facts),
            "unresolved_gaps": list(self.unresolved_gaps),
            "next_steps": list(self.next_steps),
            "companion_inference": self.companion_inference,
            "memory_update_id": self.memory_update_id,
        }


class CompanionAnswerService:
    """Answer from the common baseline before considering public research."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def answer(self, request: AnswerRequest) -> CompanionAnswer:
        question = request.question.strip()
        task = request.task.strip()
        if not question:
            raise UserInputError("answer question must not be blank")
        if not task:
            raise UserInputError("answer task must not be blank")
        package = MemoryGateway(self._root).recall(
            RecallRequest(
                query=question,
                task=task,
                access=request.access,
                purpose=QueryPurpose.SUBSTANTIVE,
                memory_ids=request.memory_ids,
                source_ids=request.source_ids,
                limit=request.limit,
                query_sensitivity=request.query_sensitivity,
            )
        )
        evidence = tuple(
            EvidenceItem(
                citation=Citation(
                    source_id=item.memory_id,
                    locator=f"memory:{item.memory_id}",
                ),
                content=item.content,
            )
            for item in package.items
        )
        internal_sensitivity = {
            item.memory_id: item.sensitivity for item in package.items
        }
        verified_facts: tuple[str, ...] = ()
        if (
            evidence
            and not package.unresolved_conflicts
            and not request.high_risk
            and not request.time_sensitive
        ):
            provider = self._generation_provider(request)
            eligible_internal = _eligible_internal_evidence(
                provider,
                evidence,
                tuple(item.sensitivity for item in package.items),
            )
            if eligible_internal:
                generated = _generate(
                    provider,
                    request,
                    question,
                    eligible_internal,
                )
                if not generated.insufficient_evidence:
                    claims = _traceable_claims(
                        generated.claims,
                        public_source_ids=set(),
                    )
                    return self._answered(
                        request,
                        claims,
                        public_query=None,
                        public_sources=(),
                        internal_sensitivity=internal_sensitivity,
                    )
                verified_facts = tuple(claim.text for claim in generated.claims)

        public_query = sanitized_public_query(question)
        public_sources = search_public_sources(
            public_query,
            time_sensitive=request.time_sensitive,
        )
        public_evidence = tuple(
            EvidenceItem(
                citation=Citation(
                    source_id=source.source_id,
                    locator=source.url,
                ),
                content=source.content,
            )
            for source in public_sources
        )
        if not public_evidence:
            return _unknown_answer(
                question,
                verified_facts=verified_facts,
                public_query=public_query,
                public_sources=public_sources,
            )
        provider = self._generation_provider(request)
        eligible_internal = _eligible_internal_evidence(
            provider,
            evidence,
            tuple(item.sensitivity for item in package.items),
        )
        combined_evidence = eligible_internal + public_evidence
        generated = _generate(provider, request, question, combined_evidence)
        if generated.insufficient_evidence or package.unresolved_conflicts:
            return _unknown_answer(
                question,
                verified_facts=verified_facts
                + tuple(claim.text for claim in generated.claims),
                public_query=public_query,
                public_sources=public_sources,
            )
        claims = _traceable_claims(
            generated.claims,
            public_source_ids={source.source_id for source in public_sources},
        )
        return self._answered(
            request,
            claims,
            public_query=public_query,
            public_sources=public_sources,
            internal_sensitivity=internal_sensitivity,
        )

    def _generation_provider(self, request: AnswerRequest) -> GenerationProvider:
        configuration_path = self._root / "myoutbrain.toml"
        try:
            with configuration_path.open("rb") as configuration_file:
                configuration = tomllib.load(configuration_file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigurationConflict(
                f"invalid configuration: {configuration_path}"
            ) from error
        generation = configuration.get("generation")
        if not isinstance(generation, dict):
            raise ConfigurationConflict("generation configuration is missing")
        provider_name = generation.get("provider")
        model = generation.get("model")
        if not isinstance(provider_name, str) or not isinstance(model, str):
            raise ConfigurationConflict("generation configuration is invalid")
        if provider_name == "openai" and (
            not request.allow_cloud or request.query_sensitivity != "cloud-allowed"
        ):
            raise UserInputError(
                "cloud answer generation requires explicit authorization and a "
                "cloud-allowed query"
            )
        return create_generation_provider(provider_name, model)

    def _answered(
        self,
        request: AnswerRequest,
        claims: tuple[TraceableClaim, ...],
        *,
        public_query: str | None,
        public_sources: tuple[PublicSource, ...],
        internal_sensitivity: Mapping[str, Sensitivity],
    ) -> CompanionAnswer:
        update_sensitivity: Sensitivity = request.query_sensitivity
        if any(
            internal_sensitivity.get(source_id) == "local-only"
            for claim in claims
            for source_id in claim.source_ids
        ):
            update_sensitivity = "local-only"
        memory_update_id = self._buffer_answer_update(
            request,
            claims,
            public_sources,
            sensitivity=update_sensitivity,
        )
        return CompanionAnswer(
            status="answered",
            answerability="sufficient",
            claims=claims,
            public_search_performed=public_query is not None,
            public_query=public_query,
            public_sources=public_sources,
            verified_facts=(),
            unresolved_gaps=(),
            next_steps=(),
            companion_inference=(
                "The answer wording is a capability-engine synthesis grounded only "
                "in the cited evidence."
            ),
            memory_update_id=memory_update_id,
        )

    def _buffer_answer_update(
        self,
        request: AnswerRequest,
        claims: tuple[TraceableClaim, ...],
        public_sources: tuple[PublicSource, ...],
        *,
        sensitivity: Sensitivity,
    ) -> str:
        body = json.dumps(
            {
                "question": request.question,
                "claims": [claim.to_data() for claim in claims],
                "public_sources": [
                    source.to_data() for source in public_sources
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cited_source_ids = tuple(
            dict.fromkeys(
                source_id
                for claim in claims
                for source_id in claim.source_ids
            )
        )
        summary = (
            f"Answered with cited evidence [{', '.join(cited_source_ids)}]: "
            + " ".join(claim.text for claim in claims)
        )
        summary = summary[:500]
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="myoutbrain-answer-",
                suffix=".json",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(body)
            receipt = LocalMemoryCore(self._root).capture_experience(
                temporary_path,
                occurred_at=datetime.now(timezone.utc).isoformat(),
                entrance="companion-answer",
                task=request.task,
                memory_digest=summary,
                sensitivity=sensitivity,
                visible_context="substantive question, answer, and cited evidence",
                context_gaps=("conversation outside this answer is unavailable",),
            )
            return receipt.digest_id
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _unknown_answer(
    question: str,
    *,
    verified_facts: tuple[str, ...] = (),
    public_query: str | None = None,
    public_sources: tuple[PublicSource, ...] = (),
) -> CompanionAnswer:
    return CompanionAnswer(
        status="unknown",
        answerability="insufficient",
        claims=(),
        public_search_performed=public_query is not None,
        public_query=public_query,
        public_sources=public_sources,
        verified_facts=verified_facts,
        unresolved_gaps=(f"Evidence does not completely answer: {question}",),
        next_steps=("Verify the unresolved point with an authoritative source.",),
        companion_inference=None,
        memory_update_id=None,
    )


def _generate(
    provider: GenerationProvider,
    request: AnswerRequest,
    question: str,
    evidence: tuple[EvidenceItem, ...],
) -> GeneratedAnswer:
    generation_request = GenerationRequest(
        purpose="answer-with-research",
        authorization=CloudAuthorization(allow_cloud=request.allow_cloud),
        evidence_package=EvidencePackage(question=question, items=evidence),
    )
    generated = provider.generate(generation_request)
    allowed_citations = {item.citation for item in evidence}
    if any(claim.citation not in allowed_citations for claim in generated.claims):
        raise ProviderFailure(
            "generated claim citation is outside the answer evidence package"
        )
    return generated


def _traceable_claims(
    claims: tuple[GeneratedClaim, ...],
    *,
    public_source_ids: set[str],
) -> tuple[TraceableClaim, ...]:
    return tuple(
        TraceableClaim(
            text=claim.text,
            source_ids=(claim.citation.source_id,),
            origin=(
                "public-evidence"
                if claim.citation.source_id in public_source_ids
                else "common-knowledge"
            ),
        )
        for claim in claims
    )


def _eligible_internal_evidence(
    provider: GenerationProvider,
    evidence: tuple[EvidenceItem, ...],
    sensitivities: tuple[Sensitivity, ...],
) -> tuple[EvidenceItem, ...]:
    if provider.name != "openai":
        return evidence
    return tuple(
        item
        for item, sensitivity in zip(evidence, sensitivities, strict=True)
        if sensitivity == "cloud-allowed"
    )
