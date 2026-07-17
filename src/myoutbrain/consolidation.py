from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal, cast
import uuid

from myoutbrain.core_types import (
    ConfigurationConflict,
    IntegrityError,
    UserInputError,
    WriterLocked,
)
from myoutbrain.embeddings import EmbeddingFailure, LocalMultilingualEmbeddingProvider
from myoutbrain.generation import (
    Citation,
    CloudAuthorization,
    EvidenceItem,
    EvidencePackage,
    GenerationProvider,
    GenerationRequest,
    ProviderFailure,
)
from myoutbrain.local_core import IntegrationProposal, LocalMemoryCore
from myoutbrain.memory_gateway import MemoryGateway
from myoutbrain.notifications import (
    LocalNotification,
    LocalNotifier,
    NotificationFailure,
    create_local_notifier,
)
from myoutbrain.semantic_index import SemanticRecallIndex
from myoutbrain.persistence import (
    atomic_commit,
    event_journal_change,
    json_document,
    operation_lock,
    recover_transactions,
    writer_lock,
)


SCHEDULED_CONSOLIDATION_STATE = Path("store") / "scheduled-consolidation.json"
AuthorizationStatus = Literal["active", "revoked"]
ScheduleMode = Literal["local", "cloud"]


@dataclass(frozen=True)
class ScheduledCloudAuthorization:
    provider: str
    model: str
    allowed_sensitivity: Literal["cloud-allowed"]
    batch_size: int
    token_limit: int
    cost_limit_usd: float
    input_cost_per_million_usd: float
    output_cost_per_million_usd: float
    generation: int
    status: AuthorizationStatus
    authorized_at: str
    revoked_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        model: str,
        allowed_sensitivity: str,
        batch_size: int,
        token_limit: int,
        cost_limit_usd: float,
        input_cost_per_million_usd: float,
        output_cost_per_million_usd: float,
        generation: int = 0,
    ) -> ScheduledCloudAuthorization:
        normalized_provider = _required_text("provider", provider)
        normalized_model = _required_text("model", model)
        if allowed_sensitivity != "cloud-allowed":
            raise UserInputError(
                "local-only content cannot be authorized for scheduled cloud analysis"
            )
        if batch_size <= 0:
            raise UserInputError("scheduled cloud batch-size must be positive")
        if token_limit <= 0:
            raise UserInputError("scheduled cloud token-limit must be positive")
        if cost_limit_usd <= 0:
            raise UserInputError("scheduled cloud cost-limit-usd must be positive")
        if input_cost_per_million_usd <= 0 or output_cost_per_million_usd <= 0:
            raise UserInputError("scheduled cloud token pricing must be positive")
        return cls(
            provider=normalized_provider,
            model=normalized_model,
            allowed_sensitivity="cloud-allowed",
            batch_size=batch_size,
            token_limit=token_limit,
            cost_limit_usd=cost_limit_usd,
            input_cost_per_million_usd=input_cost_per_million_usd,
            output_cost_per_million_usd=output_cost_per_million_usd,
            generation=generation,
            status="active",
            authorized_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "allowed_sensitivity": self.allowed_sensitivity,
            "batch_size": self.batch_size,
            "token_limit": self.token_limit,
            "cost_limit_usd": self.cost_limit_usd,
            "input_cost_per_million_usd": self.input_cost_per_million_usd,
            "output_cost_per_million_usd": self.output_cost_per_million_usd,
            "generation": self.generation,
            "status": self.status,
            "authorized_at": self.authorized_at,
            "revoked_at": self.revoked_at,
        }


@dataclass(frozen=True)
class ConsolidationSchedule:
    schedule_id: str
    task: str
    next_run_at: str
    every_hours: int
    mode: ScheduleMode
    version: int

    def to_data(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "task": self.task,
            "next_run_at": self.next_run_at,
            "every_hours": self.every_hours,
            "mode": self.mode,
            "version": self.version,
        }


@dataclass(frozen=True)
class ScheduledConsolidationRun:
    run_id: str
    schedule_id: str
    mode: ScheduleMode
    status: Literal["completed"]
    delivery: Literal["active-conversation", "pending-review-queue"]
    proposals: tuple[IntegrationProposal, ...]
    next_run_at: str
    attempt_count: int
    notification_status: Literal["not-required", "pending", "delivered", "failed"]
    deterministic_maintenance: dict[str, object]

    def to_data(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "trigger": "scheduled",
            "mode": self.mode,
            "status": self.status,
            "delivery": self.delivery,
            "canonical_changes": 0,
            "proposals": [proposal.to_data() for proposal in self.proposals],
            "next_run_at": self.next_run_at,
            "attempt_count": self.attempt_count,
            "notification_status": self.notification_status,
            "deterministic_maintenance": self.deterministic_maintenance,
        }


class ConsolidationScheduler:
    """Own explicit schedules, bounded standing authority, runs, and delivery."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def authorize_cloud(
        self,
        *,
        provider: str,
        model: str,
        allowed_sensitivity: str,
        batch_size: int,
        token_limit: int,
        cost_limit_usd: float,
        input_cost_per_million_usd: float,
        output_cost_per_million_usd: float,
    ) -> ScheduledCloudAuthorization:
        authorization = ScheduledCloudAuthorization.create(
            provider=provider,
            model=model,
            allowed_sensitivity=allowed_sensitivity,
            batch_size=batch_size,
            token_limit=token_limit,
            cost_limit_usd=cost_limit_usd,
            input_cost_per_million_usd=input_cost_per_million_usd,
            output_cost_per_million_usd=output_cost_per_million_usd,
        )
        return self._write_authorization(authorization, event_type="authorized")

    def revoke_cloud(self) -> ScheduledCloudAuthorization:
        authorization = self.authorization()
        if authorization.status == "revoked":
            return authorization
        return self._write_authorization(authorization, event_type="revoked")

    def authorization(self) -> ScheduledCloudAuthorization:
        self._ensure_initialized()
        with writer_lock(self._root):
            recover_transactions(self._root)
            return self._load_authorization()

    def schedule(
        self,
        schedule_id: str,
        *,
        task: str,
        run_at: str,
        every_hours: int,
        mode: str,
    ) -> ConsolidationSchedule:
        self._ensure_initialized()
        normalized_schedule_id = _required_text("schedule id", schedule_id)
        normalized_task = _required_text("schedule task", task)
        normalized_run_at = _validated_time(run_at, label="run-at")
        if every_hours <= 0:
            raise UserInputError("scheduled consolidation every-hours must be positive")
        if mode not in ("local", "cloud"):
            raise UserInputError(f"invalid scheduled consolidation mode: {mode}")
        schedule = ConsolidationSchedule(
            schedule_id=normalized_schedule_id,
            task=normalized_task,
            next_run_at=normalized_run_at,
            every_hours=every_hours,
            mode=cast(ScheduleMode, mode),
            version=0,
        )
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            schedules = _state_mapping(state, "schedules")
            existing = schedules.get(normalized_schedule_id)
            current_version = 0
            if existing is not None:
                if not isinstance(existing, dict):
                    raise IntegrityError("scheduled consolidation schedule is invalid")
                raw_version = existing.get("version", 1)
                if not isinstance(raw_version, int) or isinstance(raw_version, bool):
                    raise IntegrityError("scheduled consolidation version is invalid")
                current_version = raw_version
            schedule = replace(schedule, version=current_version + 1)
            schedules[normalized_schedule_id] = schedule.to_data()
            state["schedules"] = schedules
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": "consolidation.scheduled",
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                            **schedule.to_data(),
                        },
                    ),
                ],
            )
        return schedule

    def run_due(
        self,
        schedule_id: str,
        *,
        now: str,
        conversation_state: str,
        notifier: LocalNotifier | None = None,
    ) -> ScheduledConsolidationRun:
        self._ensure_initialized()
        with operation_lock(self._root, ".myoutbrain-consolidation-run.lock"):
            return self._run_due_locked(
                schedule_id,
                now=now,
                conversation_state=conversation_state,
                notifier=notifier,
            )

    def _run_due_locked(
        self,
        schedule_id: str,
        *,
        now: str,
        conversation_state: str,
        notifier: LocalNotifier | None,
    ) -> ScheduledConsolidationRun:
        self._ensure_initialized()
        normalized_schedule_id = _required_text("schedule id", schedule_id)
        normalized_now = _validated_time(now, label="now")
        if conversation_state not in ("active", "inactive"):
            raise UserInputError("conversation-state must be active or inactive")
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            schedule = _schedule_from_state(state, normalized_schedule_id)
            if datetime.fromisoformat(normalized_now) < datetime.fromisoformat(
                schedule.next_run_at
            ):
                raise UserInputError(
                    f"scheduled consolidation is not due: {normalized_schedule_id}"
                )
            run_id = "run_" + hashlib.sha256(
                (
                    f"{schedule.schedule_id}:{schedule.version}:"
                    f"{schedule.next_run_at}"
                ).encode("utf-8")
            ).hexdigest()
            runs = _state_mapping(state, "runs")
            previous = runs.get(run_id)
            previous_attempts = 0
            if previous is not None:
                if not isinstance(previous, dict):
                    raise IntegrityError("scheduled consolidation run is invalid")
                raw_attempts = previous.get("attempt_count")
                if not isinstance(raw_attempts, int) or isinstance(raw_attempts, bool):
                    raise IntegrityError("scheduled consolidation attempt count is invalid")
                previous_attempts = raw_attempts
            interrupted_at = datetime.now(timezone.utc).isoformat()
            external_calls = _state_mapping(state, "external_calls")
            interrupted_calls: list[dict[str, object]] = []
            for external_call_id, raw_call in external_calls.items():
                if (
                    external_call_id.startswith(f"{run_id}:attempt-")
                    and isinstance(raw_call, dict)
                    and raw_call.get("status") == "dispatched"
                ):
                    raw_call.update(
                        {
                            "status": "completed",
                            "completed_at": interrupted_at,
                            "result": "interrupted-unknown",
                            "input_tokens": None,
                            "output_tokens": None,
                            "actual_cost_usd": None,
                            "error": "process ended before provider result was recorded",
                        }
                    )
                    external_calls[external_call_id] = raw_call
                    interrupted_calls.append(raw_call)
            state["external_calls"] = external_calls
            attempt_count = previous_attempts + 1
            started_at = datetime.now(timezone.utc).isoformat()
            runs[run_id] = {
                "run_id": run_id,
                "schedule_id": schedule.schedule_id,
                "due_at": schedule.next_run_at,
                "mode": schedule.mode,
                "status": "running",
                "attempt_count": attempt_count,
                "started_at": started_at,
            }
            state["runs"] = runs
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        *(
                            [
                                {
                                    "id": f"evt_{uuid.uuid4().hex}",
                                    "type": "consolidation.schedule-started",
                                    "occurred_at": started_at,
                                    "run_id": run_id,
                                    "schedule_id": schedule.schedule_id,
                                    "attempt_count": attempt_count,
                                }
                            ]
                            + [
                                {
                                    "id": f"evt_{uuid.uuid4().hex}",
                                    "type": "consolidation.external-call-completed",
                                    "occurred_at": interrupted_at,
                                    **interrupted_call,
                                }
                                for interrupted_call in interrupted_calls
                            ]
                        ),
                    ),
                ],
            )
        external_audit: dict[str, object] | None = None
        try:
            if schedule.mode == "cloud":
                proposals, external_audit = self._run_cloud_analysis(
                    schedule,
                    run_id=run_id,
                    attempt_count=attempt_count,
                )
            else:
                proposals = MemoryGateway(self._root).propose_consolidation(
                    schedule.task
                )
        except (
            IntegrityError,
            ProviderFailure,
            UserInputError,
            WriterLocked,
            EmbeddingFailure,
            OSError,
        ) as error:
            self._record_retryable_run(
                run_id=run_id,
                schedule=schedule,
                attempt_count=attempt_count,
                error=str(error),
            )
            if isinstance(error, EmbeddingFailure):
                raise ProviderFailure(
                    "scheduled consolidation embedding failed"
                ) from error
            raise
        due_at = datetime.fromisoformat(schedule.next_run_at)
        deterministic_maintenance = _run_deterministic_maintenance(self._root)
        next_run_at = (due_at + timedelta(hours=schedule.every_hours)).isoformat()
        delivery: Literal["active-conversation", "pending-review-queue"] = (
            "active-conversation"
            if conversation_state == "active"
            else "pending-review-queue"
        )
        run = ScheduledConsolidationRun(
            run_id=run_id,
            schedule_id=schedule.schedule_id,
            mode=schedule.mode,
            status="completed",
            delivery=delivery,
            proposals=proposals,
            next_run_at=next_run_at,
            attempt_count=attempt_count,
            notification_status=(
                "not-required" if conversation_state == "active" else "pending"
            ),
            deterministic_maintenance=deterministic_maintenance,
        )
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            schedules = _state_mapping(state, "schedules")
            current_schedule = _schedule_from_state(state, schedule.schedule_id)
            if current_schedule.version == schedule.version:
                current_schedule = replace(current_schedule, next_run_at=next_run_at)
                schedules[schedule.schedule_id] = current_schedule.to_data()
            else:
                run = replace(run, next_run_at=current_schedule.next_run_at)
            runs = _state_mapping(state, "runs")
            runs[run.run_id] = run.to_data()
            if conversation_state == "inactive":
                pending_reviews = _state_list(state, "pending_reviews")
                proposal_ids = tuple(
                    proposal.proposal_id for proposal in proposals
                )
                pending_review, outbox_entry = _pending_review_records(
                    run_id=run.run_id,
                    schedule_id=run.schedule_id,
                    trigger="scheduled",
                    proposal_ids=proposal_ids,
                )
                pending_reviews.append(pending_review)
                state["pending_reviews"] = pending_reviews
                outbox = _state_mapping(state, "notification_outbox")
                outbox[outbox_entry[0]] = outbox_entry[1]
                state["notification_outbox"] = outbox
            state["schedules"] = schedules
            state["runs"] = runs
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": "consolidation.deterministic-maintenance",
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                            "run_id": run.run_id,
                            **deterministic_maintenance,
                        },
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": "consolidation.schedule-completed",
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                            "run_id": run.run_id,
                            "schedule_id": run.schedule_id,
                            "proposal_ids": [
                                proposal.proposal_id for proposal in proposals
                            ],
                            "canonical_changes": 0,
                            "external_analysis": external_audit,
                        },
                    ),
                ],
            )
        if conversation_state == "inactive":
            notification_status = self._deliver_pending_review_notification(
                run.run_id,
                notifier=notifier,
            )
            run = replace(run, notification_status=notification_status)
        return run

    def pending_reviews(self) -> tuple[dict[str, object], ...]:
        self._ensure_initialized()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(self._root / SCHEDULED_CONSOLIDATION_STATE)
            return tuple(_state_list(state, "pending_reviews"))

    def queue_forced_review(
        self,
        proposals: tuple[IntegrationProposal, ...],
        *,
        notifier: LocalNotifier | None = None,
    ) -> tuple[str, Literal["delivered", "failed"]]:
        self._ensure_initialized()
        proposal_ids = tuple(proposal.proposal_id for proposal in proposals)
        run_id = "forced_" + hashlib.sha256(
            ":".join(proposal_ids).encode("utf-8")
        ).hexdigest()
        pending_review, outbox_entry = _pending_review_records(
            run_id=run_id,
            schedule_id=None,
            trigger="forced",
            proposal_ids=proposal_ids,
        )
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            pending_reviews = _state_list(state, "pending_reviews")
            if not any(item.get("run_id") == run_id for item in pending_reviews):
                pending_reviews.append(pending_review)
            outbox = _state_mapping(state, "notification_outbox")
            outbox.setdefault(outbox_entry[0], outbox_entry[1])
            state["pending_reviews"] = pending_reviews
            state["notification_outbox"] = outbox
            atomic_commit(self._root, [(state_path, json_document(state))])
        return run_id, self._deliver_pending_review_notification(
            run_id, notifier=notifier
        )

    def retry_pending_notifications(
        self, *, notifier: LocalNotifier | None = None
    ) -> tuple[dict[str, object], ...]:
        self._ensure_initialized()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(self._root / SCHEDULED_CONSOLIDATION_STATE)
            outbox = _state_mapping(state, "notification_outbox")
            run_ids = tuple(
                cast(str, entry.get("run_id"))
                for entry in outbox.values()
                if isinstance(entry, dict) and entry.get("status") != "delivered"
            )
        return tuple(
            {
                "run_id": run_id,
                "notification_status": self._deliver_pending_review_notification(
                    run_id, notifier=notifier
                ),
            }
            for run_id in run_ids
        )

    def _deliver_pending_review_notification(
        self,
        run_id: str,
        *,
        notifier: LocalNotifier | None,
    ) -> Literal["delivered", "failed"]:
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            outbox = _state_mapping(state, "notification_outbox")
            raw_entry = next(
                (
                    entry
                    for entry in outbox.values()
                    if isinstance(entry, dict) and entry.get("run_id") == run_id
                ),
                None,
            )
            if not isinstance(raw_entry, dict):
                raise IntegrityError("pending notification outbox entry is missing")
            notification = _notification_from_outbox(raw_entry)
            raw_attempts = raw_entry.get("attempt_count", 0)
            if not isinstance(raw_attempts, int) or isinstance(raw_attempts, bool):
                raise IntegrityError("notification attempt count is invalid")
            attempt_count = raw_attempts + 1
            proposal_ids = tuple(_string_list(raw_entry.get("proposal_ids")))
        error_message: str | None = None
        try:
            (notifier or create_local_notifier()).notify(notification)
            status: Literal["delivered", "failed"] = "delivered"
        except NotificationFailure as error:
            status = "failed"
            error_message = str(error)
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            runs = _state_mapping(state, "runs")
            raw_run = runs.get(run_id)
            if isinstance(raw_run, dict):
                raw_run["notification_status"] = status
                runs[run_id] = raw_run
            pending_reviews = _state_list(state, "pending_reviews")
            matched = False
            for pending_review in pending_reviews:
                if pending_review.get("run_id") == run_id:
                    pending_review["notification_status"] = status
                    if error_message is not None:
                        pending_review["notification_error"] = error_message
                    else:
                        pending_review.pop("notification_error", None)
                    matched = True
            if not matched:
                raise IntegrityError("pending consolidation review is missing")
            outbox = _state_mapping(state, "notification_outbox")
            raw_outbox = outbox.get(notification.notification_id)
            if not isinstance(raw_outbox, dict):
                raise IntegrityError("pending notification outbox entry is missing")
            raw_outbox.update(
                {
                    "status": status,
                    "attempt_count": attempt_count,
                    "last_attempt_at": occurred_at,
                    "error": error_message,
                }
            )
            outbox[notification.notification_id] = raw_outbox
            state["runs"] = runs
            state["pending_reviews"] = pending_reviews
            state["notification_outbox"] = outbox
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": f"consolidation.notification-{status}",
                            "occurred_at": occurred_at,
                            "run_id": run_id,
                            "notification_id": notification.notification_id,
                            "proposal_ids": list(proposal_ids),
                            "error": error_message,
                        },
                    ),
                ],
            )
        return status

    def _record_retryable_run(
        self,
        *,
        run_id: str,
        schedule: ConsolidationSchedule,
        attempt_count: int,
        error: str,
    ) -> None:
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            runs = _state_mapping(state, "runs")
            runs[run_id] = {
                "run_id": run_id,
                "schedule_id": schedule.schedule_id,
                "due_at": schedule.next_run_at,
                "mode": schedule.mode,
                "status": "retryable",
                "attempt_count": attempt_count,
                "last_error": error,
                "updated_at": occurred_at,
            }
            state["runs"] = runs
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": "consolidation.schedule-retryable",
                            "occurred_at": occurred_at,
                            "run_id": run_id,
                            "schedule_id": schedule.schedule_id,
                            "attempt_count": attempt_count,
                            "error": error,
                        },
                    ),
                ],
            )

    def _run_cloud_analysis(
        self,
        schedule: ConsolidationSchedule,
        *,
        run_id: str,
        attempt_count: int,
        provider: GenerationProvider | None = None,
    ) -> tuple[tuple[IntegrationProposal, ...], dict[str, object]]:
        core = LocalMemoryCore(self._root)
        recovered = self._recover_interrupted_cloud_proposals(
            run_id=run_id,
            task=schedule.task,
            core=core,
        )
        if recovered is not None:
            return recovered
        authorization = self.authorization()
        if authorization.status != "active":
            raise UserInputError("scheduled cloud authorization has been revoked")
        if provider is None:
            from myoutbrain.library import configured_generation_provider

            provider = configured_generation_provider(self._root)
        if (
            provider.name != authorization.provider
            or provider.model != authorization.model
        ):
            raise UserInputError(
                "scheduled cloud provider/model exceeds standing authorization"
            )
        batch = core.buffered_consolidation_batch(
            schedule.task,
            sensitivity="cloud-allowed",
            limit=authorization.batch_size,
        )
        if not batch:
            return (), {
                "provider": provider.name,
                "model": provider.model,
                "evidence_memory_ids": [],
                "result": "no-eligible-evidence",
            }
        evidence = tuple(
            EvidenceItem(
                citation=Citation(
                    source_id=item.digest_id,
                    locator="memory-buffer",
                ),
                content=item.content,
            )
            for item in batch
        )
        package = EvidencePackage(
            question=(
                "Prepare one bounded integration proposal from these memory digests."
            ),
            items=evidence,
        )
        request = GenerationRequest(
            purpose="scheduled-consolidation",
            authorization=CloudAuthorization(allow_cloud=True),
            evidence_package=package,
            max_output_tokens=1,
            max_cost_usd=authorization.cost_limit_usd,
        )
        for _ in range(8):
            input_token_upper_bound = (
                provider.reflection_input_token_upper_bound(request)
            )
            max_output_tokens = authorization.token_limit - input_token_upper_bound
            if max_output_tokens <= 0:
                raise UserInputError(
                    "scheduled cloud request exceeds the authorized token limit"
                )
            if request.max_output_tokens == max_output_tokens:
                break
            request = replace(request, max_output_tokens=max_output_tokens)
        input_token_upper_bound = provider.reflection_input_token_upper_bound(request)
        max_output_tokens = cast(int, request.max_output_tokens)
        if input_token_upper_bound + max_output_tokens > authorization.token_limit:
            raise UserInputError(
                "scheduled cloud request exceeds the authorized token limit"
            )
        max_estimated_cost_usd = (
            input_token_upper_bound
            * authorization.input_cost_per_million_usd
            + max_output_tokens
            * authorization.output_cost_per_million_usd
        ) / 1_000_000
        if max_estimated_cost_usd > authorization.cost_limit_usd:
            raise UserInputError(
                "scheduled cloud request exceeds the authorized cost limit"
            )
        call_id = f"{run_id}:attempt-{attempt_count}"
        evidence_ids = tuple(item.digest_id for item in batch)
        self._claim_external_call(
            call_id=call_id,
            authorization=authorization,
            provider=provider,
            evidence_ids=evidence_ids,
            input_token_upper_bound=input_token_upper_bound,
            max_output_tokens=max_output_tokens,
            max_estimated_cost_usd=max_estimated_cost_usd,
        )
        actual_input_tokens: int | None = None
        actual_output_tokens: int | None = None
        actual_cost_usd: float | None = None
        try:
            reflection = provider.reflect(request)
            if reflection.usage is None:
                raise ProviderFailure(
                    "scheduled cloud provider did not report token usage"
                )
            actual_input_tokens = reflection.usage.input_tokens
            actual_output_tokens = reflection.usage.output_tokens
            actual_cost_usd = (
                reflection.usage.input_tokens
                * authorization.input_cost_per_million_usd
                + reflection.usage.output_tokens
                * authorization.output_cost_per_million_usd
            ) / 1_000_000
            if (
                reflection.usage.input_tokens + reflection.usage.output_tokens
                > authorization.token_limit
                or reflection.usage.output_tokens > max_output_tokens
                or actual_cost_usd > authorization.cost_limit_usd
            ):
                raise ProviderFailure(
                    "scheduled cloud provider usage exceeded standing authorization"
                )
            allowed_citations = {item.citation for item in evidence}
            for candidate in reflection.candidates:
                if any(
                    citation not in allowed_citations
                    for citation in (
                        *candidate.supporting_evidence,
                        *candidate.contrary_evidence,
                    )
                ):
                    raise ProviderFailure(
                        "scheduled cloud analysis cited evidence outside its batch"
                    )
            if reflection.insufficient_evidence:
                proposals: tuple[IntegrationProposal, ...] = ()
            else:
                if len(reflection.candidates) != 1:
                    raise ProviderFailure(
                        "scheduled cloud analysis must return exactly one candidate"
                    )
                proposals = MemoryGateway(self._root).propose_consolidation(
                    schedule.task,
                    digest_ids=evidence_ids,
                    proposed_understanding=reflection.candidates[0].text,
                )
            result = (
                "insufficient-evidence"
                if reflection.insufficient_evidence
                else "proposed"
                if proposals
                else "superseded"
            )
            audit = self._finalize_external_call(
                call_id,
                result=result,
                input_tokens=actual_input_tokens,
                output_tokens=actual_output_tokens,
                actual_cost_usd=actual_cost_usd,
                error=None,
            )
            return proposals, audit
        except Exception as error:
            if (
                actual_input_tokens is None
                and isinstance(error, ProviderFailure)
                and error.usage is not None
            ):
                actual_input_tokens = error.usage.input_tokens
                actual_output_tokens = error.usage.output_tokens
                actual_cost_usd = (
                    actual_input_tokens
                    * authorization.input_cost_per_million_usd
                    + actual_output_tokens
                    * authorization.output_cost_per_million_usd
                ) / 1_000_000
            self._finalize_external_call(
                call_id,
                result="failed",
                input_tokens=actual_input_tokens,
                output_tokens=actual_output_tokens,
                actual_cost_usd=actual_cost_usd,
                error=str(error),
            )
            raise

    def _recover_interrupted_cloud_proposals(
        self,
        *,
        run_id: str,
        task: str,
        core: LocalMemoryCore,
    ) -> tuple[tuple[IntegrationProposal, ...], dict[str, object]] | None:
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(self._root / SCHEDULED_CONSOLIDATION_STATE)
            calls = _state_mapping(state, "external_calls")
            interrupted = [
                raw_call
                for call_id, raw_call in calls.items()
                if call_id.startswith(f"{run_id}:attempt-")
                and isinstance(raw_call, dict)
                and raw_call.get("result") == "interrupted-unknown"
            ]
        for raw_call in reversed(interrupted):
            evidence_ids = tuple(
                _string_list(raw_call.get("evidence_memory_ids"))
            )
            proposals = core.pending_proposals_for_digests(task, evidence_ids)
            if proposals:
                audit = dict(raw_call)
                audit["recovery_result"] = "persisted-proposal-recovered"
                return proposals, audit
        return None

    def _claim_external_call(
        self,
        *,
        call_id: str,
        authorization: ScheduledCloudAuthorization,
        provider: GenerationProvider,
        evidence_ids: tuple[str, ...],
        input_token_upper_bound: int,
        max_output_tokens: int,
        max_estimated_cost_usd: float,
    ) -> None:
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            current = self._authorization_from_state(state)
            if (
                current.status != "active"
                or current.generation != authorization.generation
                or current.provider != provider.name
                or current.model != provider.model
            ):
                raise UserInputError(
                    "scheduled cloud authorization changed before dispatch"
                )
            calls = _state_mapping(state, "external_calls")
            call_audit: dict[str, object] = {
                "call_id": call_id,
                "status": "dispatched",
                "dispatched_at": occurred_at,
                "purpose": "scheduled-consolidation",
                "provider": provider.name,
                "model": provider.model,
                "authorization_generation": current.generation,
                "evidence_memory_ids": list(evidence_ids),
                "input_token_upper_bound": input_token_upper_bound,
                "max_output_tokens": max_output_tokens,
                "max_estimated_cost_usd": max_estimated_cost_usd,
                "cost_limit_usd": current.cost_limit_usd,
            }
            calls[call_id] = call_audit
            state["external_calls"] = calls
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": "consolidation.external-call-dispatched",
                            "occurred_at": occurred_at,
                            **call_audit,
                        },
                    ),
                ],
            )

    def _finalize_external_call(
        self,
        call_id: str,
        *,
        result: str,
        input_tokens: int | None,
        output_tokens: int | None,
        actual_cost_usd: float | None,
        error: str | None,
    ) -> dict[str, object]:
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            calls = _state_mapping(state, "external_calls")
            raw_call = calls.get(call_id)
            if not isinstance(raw_call, dict):
                raise IntegrityError("scheduled external call audit is missing")
            raw_call.update(
                {
                    "status": "completed",
                    "completed_at": occurred_at,
                    "result": result,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "actual_cost_usd": actual_cost_usd,
                    "error": error,
                }
            )
            calls[call_id] = raw_call
            state["external_calls"] = calls
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": f"evt_{uuid.uuid4().hex}",
                            "type": "consolidation.external-call-completed",
                            "occurred_at": occurred_at,
                            **raw_call,
                        },
                    ),
                ],
            )
        return raw_call

    def _write_authorization(
        self,
        authorization: ScheduledCloudAuthorization,
        *,
        event_type: str,
    ) -> ScheduledCloudAuthorization:
        self._ensure_initialized()
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        event_id = f"evt_{uuid.uuid4().hex}"
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            raw_current = state.get("authorization")
            generation = 0
            if isinstance(raw_current, dict):
                raw_generation = raw_current.get("generation", 1)
                if not isinstance(raw_generation, int) or isinstance(
                    raw_generation, bool
                ):
                    raise IntegrityError("scheduled cloud authorization is invalid")
                generation = raw_generation
            if event_type == "authorized":
                authorization = replace(authorization, generation=generation + 1)
            elif event_type == "revoked":
                current = self._authorization_from_state(state)
                authorization = replace(
                    current,
                    generation=generation + 1,
                    status="revoked",
                    revoked_at=occurred_at,
                )
            state["authorization"] = authorization.to_data()
            atomic_commit(
                self._root,
                [
                    (state_path, json_document(state)),
                    event_journal_change(
                        self._root,
                        {
                            "id": event_id,
                            "type": f"consolidation.cloud-{event_type}",
                            "occurred_at": occurred_at,
                            "provider": authorization.provider,
                            "model": authorization.model,
                            "allowed_sensitivity": authorization.allowed_sensitivity,
                            "batch_size": authorization.batch_size,
                            "token_limit": authorization.token_limit,
                            "cost_limit_usd": authorization.cost_limit_usd,
                            "authorization_generation": authorization.generation,
                            "input_cost_per_million_usd": (
                                authorization.input_cost_per_million_usd
                            ),
                            "output_cost_per_million_usd": (
                                authorization.output_cost_per_million_usd
                            ),
                        },
                    ),
                ],
            )
        return authorization

    def _load_authorization(self) -> ScheduledCloudAuthorization:
        state = _load_state(self._root / SCHEDULED_CONSOLIDATION_STATE)
        return self._authorization_from_state(state)

    def _authorization_from_state(
        self, state: dict[str, object]
    ) -> ScheduledCloudAuthorization:
        raw = state.get("authorization")
        if not isinstance(raw, dict):
            raise UserInputError("scheduled cloud authorization is not configured")
        try:
            status = raw["status"]
            sensitivity = raw["allowed_sensitivity"]
            if status not in ("active", "revoked") or sensitivity != "cloud-allowed":
                raise TypeError
            return ScheduledCloudAuthorization(
                provider=cast(str, raw["provider"]),
                model=cast(str, raw["model"]),
                allowed_sensitivity="cloud-allowed",
                batch_size=cast(int, raw["batch_size"]),
                token_limit=cast(int, raw["token_limit"]),
                cost_limit_usd=float(cast(int | float, raw["cost_limit_usd"])),
                input_cost_per_million_usd=float(
                    cast(int | float, raw["input_cost_per_million_usd"])
                ),
                output_cost_per_million_usd=float(
                    cast(int | float, raw["output_cost_per_million_usd"])
                ),
                generation=cast(int, raw.get("generation", 1)),
                status=cast(AuthorizationStatus, status),
                authorized_at=cast(str, raw["authorized_at"]),
                revoked_at=cast(str | None, raw.get("revoked_at")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("scheduled cloud authorization is invalid") from error

    def _ensure_initialized(self) -> None:
        if not (self._root / "myoutbrain.toml").is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )


def _pending_review_records(
    *,
    run_id: str,
    schedule_id: str | None,
    trigger: str,
    proposal_ids: tuple[str, ...],
) -> tuple[dict[str, object], tuple[str, dict[str, object]]]:
    notification_id = "notification_" + hashlib.sha256(
        run_id.encode("utf-8")
    ).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    body = (
        "Review proposals: " + ", ".join(proposal_ids)
        if proposal_ids
        else "Consolidation completed with no proposals."
    )
    pending_review: dict[str, object] = {
        "run_id": run_id,
        "schedule_id": schedule_id,
        "trigger": trigger,
        "proposal_ids": list(proposal_ids),
        "created_at": created_at,
        "notification_id": notification_id,
        "notification_status": "pending",
    }
    outbox: dict[str, object] = {
        "notification_id": notification_id,
        "run_id": run_id,
        "proposal_ids": list(proposal_ids),
        "title": "Memory review is ready",
        "body": body,
        "action": f"myoutbrain://pending-review/{run_id}",
        "status": "pending",
        "attempt_count": 0,
        "created_at": created_at,
    }
    return pending_review, (notification_id, outbox)


def _notification_from_outbox(raw: dict[str, object]) -> LocalNotification:
    notification_id = raw.get("notification_id")
    title = raw.get("title")
    body = raw.get("body")
    action = raw.get("action")
    if not all(
        isinstance(value, str) and bool(value)
        for value in (notification_id, title, body, action)
    ):
        raise IntegrityError("pending notification outbox entry is invalid")
    return LocalNotification(
        notification_id=cast(str, notification_id),
        title=cast(str, title),
        body=cast(str, body),
        action=cast(str, action),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IntegrityError("scheduled consolidation string list is invalid")
    return [cast(str, item) for item in value]


def _load_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {
            "schema_version": 1,
            "authorization": None,
            "schedules": {},
            "runs": {},
            "pending_reviews": [],
            "external_calls": {},
            "notification_outbox": {},
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise TypeError
        return {str(key): value for key, value in raw.items()}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise IntegrityError(f"invalid scheduled consolidation state: {path}") from error


def _required_text(label: str, value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise UserInputError(f"scheduled cloud {label} must not be blank")
    return normalized


def _validated_time(value: str, *, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise UserInputError(f"scheduled consolidation {label} must be ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UserInputError(
            f"scheduled consolidation {label} must include a UTC offset"
        )
    return parsed.isoformat()


def _state_mapping(state: dict[str, object], key: str) -> dict[str, object]:
    raw = state.get(key, {})
    if not isinstance(raw, dict):
        raise IntegrityError(f"scheduled consolidation {key} state is invalid")
    return {str(item_key): value for item_key, value in raw.items()}


def _state_list(state: dict[str, object], key: str) -> list[dict[str, object]]:
    raw = state.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise IntegrityError(f"scheduled consolidation {key} state is invalid")
    return [
        {str(item_key): value for item_key, value in item.items()}
        for item in raw
    ]


def _schedule_from_state(
    state: dict[str, object], schedule_id: str
) -> ConsolidationSchedule:
    raw = _state_mapping(state, "schedules").get(schedule_id)
    if not isinstance(raw, dict):
        raise UserInputError(f"consolidation schedule does not exist: {schedule_id}")
    try:
        mode = raw["mode"]
        if mode not in ("local", "cloud"):
            raise TypeError
        return ConsolidationSchedule(
            schedule_id=cast(str, raw["schedule_id"]),
            task=cast(str, raw["task"]),
            next_run_at=cast(str, raw["next_run_at"]),
            every_hours=cast(int, raw["every_hours"]),
            mode=cast(ScheduleMode, mode),
            version=cast(int, raw.get("version", 1)),
        )
    except (KeyError, TypeError) as error:
        raise IntegrityError(f"invalid consolidation schedule: {schedule_id}") from error


def _run_deterministic_maintenance(root: Path) -> dict[str, object]:
    memories = LocalMemoryCore(root).recallable_memories()
    if not memories:
        index_status = "current-empty"
    else:
        try:
            SemanticRecallIndex(root).scores(
                "deterministic index maintenance",
                memories,
                LocalMultilingualEmbeddingProvider(),
            )
            index_status = "rebuilt"
        except (EmbeddingFailure, OSError, RuntimeError, TypeError, ValueError):
            index_status = "deferred"
    return {
        "semantic_change": False,
        "content_deduplication": "content-addressed-on-write",
        "index_status": index_status,
    }
