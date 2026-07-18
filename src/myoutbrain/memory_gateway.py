from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from enum import StrEnum
import math
from pathlib import Path
import sqlite3
import tomllib
from typing import Protocol, cast

from myoutbrain.core_types import (
    IntegrityError,
    MemoryState as MemoryState,
    Sensitivity,
    UserInputError,
)
from myoutbrain.counterevidence import (
    CounterevidenceRequest,
    CounterevidenceService,
)
from myoutbrain.embeddings import (
    EmbeddingFailure,
    EmbeddingLocation,
    EmbeddingProvider,
    LocalMultilingualEmbeddingProvider,
)
from myoutbrain.local_core import (
    BufferedMemoryReceipt,
    IntegrationProposal,
    IntegrationReviewResult,
    LocalMemoryCore,
    MemoryRenameResult,
    RecallableMemory,
    SourceMemoryProposal,
    SourceMemoryReuse,
)
from myoutbrain.reflection import (
    ImmediateReflectionRequest,
    LearningSignalSubmission,
    ReflectionAbandonmentRequest,
)
from myoutbrain.retrieval import lexical_terms
from myoutbrain.semantic_index import SemanticRecallIndex
from myoutbrain.v2_recall import (
    AnswerabilityEngine,
    CapabilityAnswerability,
    V2RecallRequest,
    V2RecallService,
)
from myoutbrain.v2_public_search import (
    PublicSearchAnswerabilityEngine,
    PublicSearchProvider,
    PublicQuerySanitizer,
    V2PublicSearchRequest,
    V2PublicSearchService,
)
from myoutbrain.v2_memory_lifecycle import V2MemoryLifecycleService
from myoutbrain.capsule_maintenance import CapsuleMaintenanceService
from myoutbrain.v2_migration import V2MigrationService
from myoutbrain.instance_maintenance import InstanceMaintenanceService
from myoutbrain.unified_review import ReviewBatchRequest


class MemoryAccess(StrEnum):
    LOCAL_TRUSTED = "local-trusted"
    TASK_SCOPED = "task-scoped"
    PUBLIC_EXTERNAL = "public-external"


class QueryPurpose(StrEnum):
    SUBSTANTIVE = "substantive"
    CASUAL = "casual"
    OPERATION = "operation"


class Answerability(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    NOT_REQUIRED = "not-required"


class RecallMatch(StrEnum):
    STABLE_IDENTITY = "stable-identity"
    SOURCE_RELATION = "source-relation"
    UNRESOLVED_CONFLICT = "unresolved-conflict"
    FULL_TEXT = "full-text"
    SEMANTIC_CANDIDATE = "semantic-candidate"


@dataclass(frozen=True)
class RecallRequest:
    query: str
    task: str
    access: MemoryAccess
    purpose: QueryPurpose
    memory_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    limit: int = 5
    query_sensitivity: Sensitivity = "local-only"


@dataclass(frozen=True)
class ExperienceSubmission:
    experience_path: Path
    occurred_at: str
    entrance: str
    task_pointer: str
    digest: str
    sensitivity: Sensitivity
    visible_context: str
    context_gaps: tuple[str, ...]


class MemoryReader(Protocol):
    def recallable_memories(self) -> tuple[RecallableMemory, ...]: ...


@dataclass(frozen=True)
class MemoryEvidence(RecallableMemory):
    match: RecallMatch

    @classmethod
    def from_memory(
        cls,
        memory: RecallableMemory,
        match: RecallMatch,
    ) -> MemoryEvidence:
        return cls(
            memory_id=memory.memory_id,
            content=memory.content,
            memory_state=memory.memory_state,
            source_ids=memory.source_ids,
            occurred_at=memory.occurred_at,
            sensitivity=memory.sensitivity,
            entrance=memory.entrance,
            task=memory.task,
            related_memory_ids=memory.related_memory_ids,
            conflict_memory_ids=memory.conflict_memory_ids,
            match=match,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "memory_state": self.memory_state.value,
            "confirmed": self.confirmed,
            "source_ids": list(self.source_ids),
            "occurred_at": self.occurred_at,
            "sensitivity": self.sensitivity,
            "entrance": self.entrance,
            "task": self.task,
            "related_memory_ids": list(self.related_memory_ids),
            "conflict_memory_ids": list(self.conflict_memory_ids),
            "match": self.match.value,
        }


@dataclass(frozen=True)
class MemoryEvidencePackage:
    query: str
    task: str
    access: MemoryAccess
    retrieval_performed: bool
    common_knowledge_queried: bool
    answerability: Answerability
    items: tuple[MemoryEvidence, ...]
    unresolved_conflicts: tuple[tuple[str, str], ...] = ()

    def to_data(self) -> dict[str, object]:
        return {
            "query": self.query,
            "task": self.task,
            "access": self.access.value,
            "retrieval_performed": self.retrieval_performed,
            "common_knowledge_queried": self.common_knowledge_queried,
            "answerability": self.answerability.value,
            "items": [item.to_data() for item in self.items],
            "unresolved_conflicts": [
                list(pair) for pair in self.unresolved_conflicts
            ],
        }


class MemoryGateway:
    """Return task-scoped memory without exposing persistence details."""

    def __init__(
        self,
        root: Path,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        memory_reader: MemoryReader | None = None,
    ) -> None:
        self._root = root
        self._embedding_provider = (
            embedding_provider or LocalMultilingualEmbeddingProvider()
        )
        self._memory_core = LocalMemoryCore(root)
        self._memory_reader = memory_reader or self._memory_core

    def submit(self, submission: ExperienceSubmission) -> BufferedMemoryReceipt:
        return self._memory_core.capture_experience(
            submission.experience_path,
            occurred_at=submission.occurred_at,
            entrance=submission.entrance,
            task=submission.task_pointer,
            memory_digest=submission.digest,
            sensitivity=submission.sensitivity,
            visible_context=submission.visible_context,
            context_gaps=submission.context_gaps,
        )

    def submit_learning_signal(
        self,
        submission: LearningSignalSubmission,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        if submission.signal_kind is None:
            return {"captured": False, "input": None}
        return self._memory_core.submit_learning_signal(
            submission,
            idempotency_key=idempotency_key,
        ).to_data()

    def reflection_inputs(
        self,
        *,
        limit: int = 20,
        budget_bytes: int = 16 * 1024,
    ) -> dict[str, object]:
        inputs, truncated, used_bytes = self._memory_core.reflection_inputs(
            limit=limit,
            budget_bytes=budget_bytes,
        )
        return {
            "inputs": [
                reflection_input.to_data()
                for reflection_input in inputs
            ],
            "budget_bytes": budget_bytes,
            "used_bytes": used_bytes,
            "truncated": truncated,
        }

    def reflect_now(
        self,
        request: ImmediateReflectionRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.reflect_now(
            request,
            idempotency_key=idempotency_key,
        ).to_data()

    def abandon_reflection(
        self,
        request: ReflectionAbandonmentRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.abandon_reflection(
            request,
            idempotency_key=idempotency_key,
        ).to_data()

    def configure_reflection_schedule(
        self,
        *,
        enabled: bool,
        first_due_at: str,
        every_hours: int,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.configure_reflection_schedule(
            enabled=enabled,
            first_due_at=first_due_at,
            every_hours=every_hours,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def enqueue_scheduled_reflection(
        self,
        *,
        now: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.enqueue_scheduled_reflection(
            now=now,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def claim_scheduled_reflection(
        self,
        *,
        now: str,
        lease_seconds: int,
        claimed_by: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.claim_scheduled_reflection(
            now=now,
            lease_seconds=lease_seconds,
            claimed_by=claimed_by,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def return_scheduled_reflection(
        self,
        *,
        run_id: str,
        lease_token: str,
        now: str,
        reason: str,
        returned_by: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.return_scheduled_reflection(
            run_id=run_id,
            lease_token=lease_token,
            now=now,
            reason=reason,
            returned_by=returned_by,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def complete_scheduled_reflection(
        self,
        request: ImmediateReflectionRequest,
        *,
        run_id: str,
        lease_token: str,
        completed_at: str,
        completed_by: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.complete_scheduled_reflection(
            request,
            run_id=run_id,
            lease_token=lease_token,
            completed_at=completed_at,
            completed_by=completed_by,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def abandon_scheduled_reflection(
        self,
        *,
        run_id: str,
        abandoned_at: str,
        reason: str,
        permanently_missing_input_ids: tuple[str, ...],
        confirm_permanent_missing: bool,
        abandoned_by: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._memory_core.abandon_scheduled_reflection(
            run_id=run_id,
            abandoned_at=abandoned_at,
            reason=reason,
            permanently_missing_input_ids=permanently_missing_input_ids,
            confirm_permanent_missing=confirm_permanent_missing,
            abandoned_by=abandoned_by,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def propose_consolidation(
        self,
        task: str,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        digest_ids: tuple[str, ...] | None = None,
        proposed_understanding: str | None = None,
    ) -> tuple[IntegrationProposal, ...]:
        return self._memory_core.propose_manual_consolidation(
            task,
            embedding_provider=embedding_provider,
            digest_ids=digest_ids,
            proposed_understanding=proposed_understanding,
        )

    def review_proposal(
        self,
        proposal_id: str,
        instruction: str,
    ) -> IntegrationReviewResult:
        return self._memory_core.review_integration_proposal(
            proposal_id,
            instruction,
        )

    def recall_v2(
        self,
        request: V2RecallRequest,
        answerability_engine: AnswerabilityEngine,
    ) -> dict[str, object]:
        return V2RecallService(self._root).recall(request, answerability_engine)

    def inspect_capsule_structure(self) -> dict[str, object]:
        return CapsuleMaintenanceService(self._root).inspect()

    def doctor_instance(
        self,
        *,
        repair: bool,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        entrance: str | None = None,
    ) -> dict[str, object]:
        service = InstanceMaintenanceService(self._root)
        if not repair:
            return service.doctor()
        if expected_version is None or idempotency_key is None or entrance is None:
            raise UserInputError("Doctor repair requires a complete write contract")
        return service.repair(
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def create_cold_backup(
        self,
        output_path: Path,
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return InstanceMaintenanceService(self._root).create_backup(
            output_path,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def verify_cold_backup(self, archive_path: Path) -> dict[str, object]:
        return InstanceMaintenanceService.verify_backup(archive_path)

    def restore_cold_backup(
        self,
        archive_path: Path,
        destination_path: Path,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return InstanceMaintenanceService.restore_backup(
            archive_path,
            destination_path,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def plan_orphan_gc(self) -> dict[str, object]:
        return InstanceMaintenanceService(self._root).plan_gc()

    def apply_orphan_gc(
        self,
        plan_id: str,
        *,
        confirmation: str,
        confirmed_large_source_ids: tuple[str, ...],
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return InstanceMaintenanceService(self._root).apply_gc(
            plan_id,
            confirmation=confirmation,
            confirmed_large_source_ids=confirmed_large_source_ids,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def plan_capsule_maintenance(
        self,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        return CapsuleMaintenanceService(self._root).plan(parameters)

    def configure_partition(
        self,
        parameters: dict[str, object],
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        return CapsuleMaintenanceService(self._root).configure_partition(
            parameters,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def reorganize_capsules(
        self,
        parameters: dict[str, object],
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return CapsuleMaintenanceService(self._root).reorganize(
            parameters,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def historicize_v2_memory(
        self,
        memory_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MemoryLifecycleService(self._root).historicize(
            memory_id,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def revise_v2_memory(
        self,
        memory_id: str,
        *,
        body: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MemoryLifecycleService(self._root).revise(
            memory_id,
            body=body,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def supersede_v2_memory(
        self,
        memory_id: str,
        *,
        replacement_memory_id: str,
        replacement_version: int,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MemoryLifecycleService(self._root).supersede(
            memory_id,
            replacement_memory_id=replacement_memory_id,
            replacement_version=replacement_version,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def deactivate_v2_memory(
        self,
        memory_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MemoryLifecycleService(self._root).deactivate(
            memory_id,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def restore_v2_memory(
        self,
        memory_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MemoryLifecycleService(self._root).restore(
            memory_id,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def erase_v2_memory(
        self,
        memory_id: str,
        *,
        confirmation: str | None,
        entrance: str,
    ) -> dict[str, object]:
        return V2MemoryLifecycleService(self._root).erase(
            memory_id,
            confirmation=confirmation,
            entrance=entrance,
        )

    def expand_v2_evidence(
        self,
        recall_id: str,
        memory_id: str,
        *,
        evidence_reference_ids: tuple[str, ...],
        budget_bytes: int,
    ) -> dict[str, object]:
        return V2RecallService(self._root).expand_evidence(
            recall_id,
            memory_id,
            evidence_reference_ids=evidence_reference_ids,
            budget_bytes=budget_bytes,
        )

    def assess_v2_recall(
        self,
        recall_id: str,
        capability_answerability: CapabilityAnswerability,
    ) -> dict[str, object]:
        return V2RecallService(self._root).assess_answerability(
            recall_id,
            capability_answerability,
        )

    def search_public_v2(
        self,
        request: V2PublicSearchRequest,
        sanitizer: PublicQuerySanitizer,
        provider: PublicSearchProvider,
        answerability_engine: PublicSearchAnswerabilityEngine,
    ) -> dict[str, object]:
        return V2PublicSearchService(self._root).search(
            request,
            sanitizer,
            provider,
            answerability_engine,
        )

    def route_counterevidence(
        self,
        request: CounterevidenceRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        return CounterevidenceService(self._root).route(
            request,
            idempotency_key=idempotency_key,
        )

    def v2_recall_activity(self) -> dict[str, object]:
        return V2RecallService(self._root).activity()

    def rename_v2_memory(
        self,
        memory_id: str,
        *,
        canonical_name: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> MemoryRenameResult:
        return self._memory_core.rename_memory(
            memory_id,
            canonical_name=canonical_name,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def propose_v2_source_memory(
        self,
        source_path: Path,
        *,
        source_id: str | None,
        canonical_name: str,
        body: str,
        applicability_scope: str,
        idempotency_key: str,
    ) -> SourceMemoryProposal | SourceMemoryReuse:
        return self._memory_core.propose_source_memory(
            source_path,
            source_id=source_id,
            canonical_name=canonical_name,
            body=body,
            applicability_scope=applicability_scope,
            idempotency_key=idempotency_key,
        )

    def review_queue(self) -> dict[str, object]:
        return self._memory_core.review_queue().to_data()

    def unified_review_proposal(self, proposal_id: str) -> dict[str, object] | None:
        proposal = self._memory_core.review_proposal(proposal_id)
        return proposal.to_data() if proposal is not None else None

    def decide_review_batch(
        self,
        request: ReviewBatchRequest,
        *,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return self._memory_core.decide_review_batch(
            request,
            idempotency_key=idempotency_key,
            entrance=entrance,
        ).to_data()

    def plan_v2_migration(
        self,
        memory_ids: tuple[str, ...],
        *,
        target: str,
    ) -> dict[str, object]:
        return V2MigrationService(self._root).plan(memory_ids, target=target)

    def export_v2_migration(
        self,
        output_path: Path,
        memory_ids: tuple[str, ...],
        *,
        target: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MigrationService(self._root).export(
            output_path,
            memory_ids,
            target=target,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def preview_v2_migration_import(
        self,
        package_path: Path,
    ) -> dict[str, object]:
        return V2MigrationService(self._root).import_dry_run(package_path)

    def import_v2_migration(
        self,
        package_path: Path,
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return V2MigrationService(self._root).import_package(
            package_path,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def recall(self, request: RecallRequest) -> MemoryEvidencePackage:
        query = request.query.strip()
        task = request.task.strip()
        if not query:
            raise UserInputError("recall query must not be blank")
        if not task:
            raise UserInputError("recall task must not be blank")
        if request.limit < 1 or request.limit > 20:
            raise UserInputError("recall limit must be between 1 and 20")
        if request.query_sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError("recall query sensitivity is invalid")
        if request.purpose is not QueryPurpose.SUBSTANTIVE:
            return MemoryEvidencePackage(
                query=query,
                task=task,
                access=request.access,
                retrieval_performed=False,
                common_knowledge_queried=False,
                answerability=Answerability.NOT_REQUIRED,
                items=(),
            )

        memories = self._memory_reader.recallable_memories()
        requested_memory_ids = frozenset(request.memory_ids)
        requested_source_ids = frozenset(request.source_ids)
        canonical = _eligible_for_phase(
            memories,
            state=MemoryState.CANONICAL,
            access=request.access,
            task=task,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        visible_canonical_ids = frozenset(
            memory.memory_id for memory in canonical
        )
        canonical = tuple(
            replace(
                memory,
                related_memory_ids=tuple(
                    related_id
                    for related_id in memory.related_memory_ids
                    if related_id in visible_canonical_ids
                ),
                conflict_memory_ids=tuple(
                    conflict_id
                    for conflict_id in memory.conflict_memory_ids
                    if conflict_id in visible_canonical_ids
                ),
            )
            for memory in canonical
        )
        historical = _eligible_for_phase(
            memories,
            state=MemoryState.HISTORICAL_TRUSTED,
            access=request.access,
            task=task,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        buffered = _eligible_for_phase(
            memories,
            state=MemoryState.BUFFERED,
            access=request.access,
            task=task,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        canonical_matches = _match_phase(
            query,
            canonical,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        historical_matches = _match_phase(
            query,
            historical,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        buffered_matches = _match_phase(
            query,
            buffered,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
        )
        semantic_scores = self._semantic_scores(
            query,
            canonical + historical + buffered,
            query_sensitivity=request.query_sensitivity,
        )
        canonical_matches = _with_semantic_matches(
            canonical,
            canonical_matches,
            semantic_scores,
        )
        historical_matches = _with_semantic_matches(
            historical,
            historical_matches,
            semantic_scores,
        )
        buffered_matches = _with_semantic_matches(
            buffered,
            buffered_matches,
            semantic_scores,
        )
        matched = canonical_matches + historical_matches + buffered_matches
        ordered = tuple(
            sorted(
                matched,
                key=lambda pair: (
                    _MATCH_ORDER[pair[1]],
                    pair[0].memory_state is MemoryState.HISTORICAL_TRUSTED,
                    pair[0].memory_state is MemoryState.BUFFERED,
                    -pair[2],
                    pair[0].memory_id,
                ),
            )
        )[: request.limit]
        selected = tuple(
            MemoryEvidence.from_memory(memory, match)
            for memory, match, _score in ordered
        )
        selected_ids = {item.memory_id for item in selected}
        conflict_ids = {
            conflict_id
            for item in selected
            for conflict_id in item.conflict_memory_ids
        }
        conflict_evidence = tuple(
            MemoryEvidence.from_memory(memory, RecallMatch.UNRESOLVED_CONFLICT)
            for memory in canonical
            if memory.memory_id in conflict_ids
            and memory.memory_id not in selected_ids
        )
        selected = selected + conflict_evidence
        unresolved_conflicts = tuple(
            sorted(
                {
                    _ordered_conflict_pair(item.memory_id, conflict_id)
                    for item in selected
                    for conflict_id in item.conflict_memory_ids
                }
            )
        )
        return MemoryEvidencePackage(
            query=query,
            task=task,
            access=request.access,
            retrieval_performed=True,
            common_knowledge_queried=True,
            answerability=Answerability.INSUFFICIENT,
            items=selected,
            unresolved_conflicts=unresolved_conflicts,
        )

    def _semantic_scores(
        self,
        query: str,
        memories: tuple[RecallableMemory, ...],
        *,
        query_sensitivity: Sensitivity,
    ) -> dict[str, float]:
        provider = self._embedding_provider
        try:
            eligible = memories
            if provider.location is EmbeddingLocation.CLOUD:
                cloud_text_limit = _cloud_embedding_text_limit(self._root, provider)
                if query_sensitivity != "cloud-allowed" or cloud_text_limit is None:
                    return {}
                eligible = tuple(
                    memory
                    for memory in memories
                    if memory.sensitivity == "cloud-allowed"
                )[: cloud_text_limit - 1]
            return SemanticRecallIndex(self._root).scores(query, eligible, provider)
        except (
            EmbeddingFailure,
            OSError,
            RuntimeError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            return {}

_MATCH_ORDER = {
    RecallMatch.STABLE_IDENTITY: 0,
    RecallMatch.SOURCE_RELATION: 1,
    RecallMatch.UNRESOLVED_CONFLICT: 2,
    RecallMatch.FULL_TEXT: 3,
    RecallMatch.SEMANTIC_CANDIDATE: 4,
}


def _ordered_conflict_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _with_semantic_matches(
    memories: tuple[RecallableMemory, ...],
    existing: tuple[tuple[RecallableMemory, RecallMatch, float], ...],
    semantic_scores: Mapping[str, float],
) -> tuple[tuple[RecallableMemory, RecallMatch, float], ...]:
    matched_ids = {memory.memory_id for memory, _match, _score in existing}
    semantic = tuple(
        (memory, RecallMatch.SEMANTIC_CANDIDATE, semantic_scores[memory.memory_id])
        for memory in memories
        if memory.memory_id in semantic_scores and memory.memory_id not in matched_ids
    )
    return existing + semantic


def _cloud_embedding_text_limit(
    root: Path,
    provider: EmbeddingProvider,
) -> int | None:
    try:
        with (root / "myoutbrain.toml").open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
        embedding = configuration.get("embedding")
        if not isinstance(embedding, dict):
            return None
        authorized = (
            embedding.get("allow_cloud") is True
            and embedding.get("provider") == provider.space.provider
            and embedding.get("model") == provider.space.model
            and embedding.get("dimensions") == provider.space.dimensions
            and embedding.get("normalization_version")
            == provider.space.normalization_version
            and embedding.get("cloud_send_scope") == "cloud-allowed-only"
            and isinstance(embedding.get("cloud_budget_usd"), (int, float))
            and not isinstance(embedding.get("cloud_budget_usd"), bool)
            and cast(float, embedding.get("cloud_budget_usd")) > 0
            and isinstance(embedding.get("cloud_max_texts_per_request"), int)
            and not isinstance(embedding.get("cloud_max_texts_per_request"), bool)
            and cast(int, embedding.get("cloud_max_texts_per_request")) >= 2
        )
        if not authorized:
            return None
        return cast(int, embedding.get("cloud_max_texts_per_request"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _eligible_for_phase(
    memories: tuple[RecallableMemory, ...],
    *,
    state: MemoryState,
    access: MemoryAccess,
    task: str,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
) -> tuple[RecallableMemory, ...]:
    return tuple(
        memory
        for memory in memories
        if memory.memory_state is state
        and _allows(
            memory,
            access,
            task,
            explicitly_requested=(
                memory.memory_id in requested_memory_ids
                or bool(requested_source_ids.intersection(memory.source_ids))
            ),
        )
    )


def _allows(
    memory: RecallableMemory,
    access: MemoryAccess,
    task: str,
    *,
    explicitly_requested: bool,
) -> bool:
    if access is MemoryAccess.PUBLIC_EXTERNAL and memory.sensitivity == "local-only":
        return False
    if memory.memory_state in (
        MemoryState.CANONICAL,
        MemoryState.HISTORICAL_TRUSTED,
    ):
        return True
    if access is MemoryAccess.LOCAL_TRUSTED:
        return True
    return memory.task == task or explicitly_requested


def _match_phase(
    query: str,
    memories: tuple[RecallableMemory, ...],
    *,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
) -> tuple[tuple[RecallableMemory, RecallMatch, float], ...]:
    full_text_scores = _full_text_matches(query, memories)
    matches: list[tuple[RecallableMemory, RecallMatch, float]] = []
    for memory in memories:
        match = _match_for(
            memory,
            requested_memory_ids=requested_memory_ids,
            requested_source_ids=requested_source_ids,
            full_text_scores=full_text_scores,
        )
        if match is not None:
            matches.append(
                (memory, match, float(full_text_scores.get(memory.memory_id, 0)))
            )
    return tuple(matches)


def _match_for(
    memory: RecallableMemory,
    *,
    requested_memory_ids: frozenset[str],
    requested_source_ids: frozenset[str],
    full_text_scores: Mapping[str, int],
) -> RecallMatch | None:
    if memory.memory_id in requested_memory_ids:
        return RecallMatch.STABLE_IDENTITY
    if requested_source_ids.intersection(memory.source_ids):
        return RecallMatch.SOURCE_RELATION
    if memory.memory_id in full_text_scores:
        return RecallMatch.FULL_TEXT
    return None


def _full_text_matches(
    query: str,
    memories: tuple[RecallableMemory, ...],
) -> dict[str, int]:
    query_terms = lexical_terms(query)
    terms = tuple(sorted(query_terms))
    if not terms or not memories:
        return {}
    expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                "CREATE VIRTUAL TABLE recall_fts USING fts5(memory_id UNINDEXED, content)"
            )
            connection.executemany(
                "INSERT INTO recall_fts (memory_id, content) VALUES (?, ?)",
                ((memory.memory_id, memory.content) for memory in memories),
            )
            rows = connection.execute(
                """
                SELECT memory_id
                FROM recall_fts
                WHERE recall_fts MATCH ?
                ORDER BY bm25(recall_fts)
                """,
                (expression,),
            ).fetchall()
    except sqlite3.Error as error:
        raise IntegrityError("local full-text recall failed") from error
    fts_ids = {memory_id for (memory_id,) in rows}
    normalized_query = " ".join(query.casefold().split())
    required_overlap = max(1, math.ceil(len(query_terms) * 0.6))
    scores: dict[str, int] = {}
    for memory in memories:
        normalized_content = " ".join(memory.content.casefold().split())
        exact_phrase = normalized_query in normalized_content
        overlap = len(query_terms.intersection(lexical_terms(memory.content)))
        if (
            memory.memory_id in fts_ids or exact_phrase
        ) and (exact_phrase or overlap >= required_overlap):
            scores[memory.memory_id] = overlap + (
                len(query_terms) if exact_phrase else 0
            )
    return scores
