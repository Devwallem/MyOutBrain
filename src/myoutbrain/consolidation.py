from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal, cast
import uuid

from myoutbrain.core_types import ConfigurationConflict, IntegrityError, UserInputError
from myoutbrain.local_core import IntegrationProposal, LocalMemoryCore
from myoutbrain.persistence import (
    atomic_commit,
    event_journal_change,
    json_document,
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
        return cls(
            provider=normalized_provider,
            model=normalized_model,
            allowed_sensitivity="cloud-allowed",
            batch_size=batch_size,
            token_limit=token_limit,
            cost_limit_usd=cost_limit_usd,
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

    def to_data(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "task": self.task,
            "next_run_at": self.next_run_at,
            "every_hours": self.every_hours,
            "mode": self.mode,
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
    ) -> ScheduledCloudAuthorization:
        authorization = ScheduledCloudAuthorization.create(
            provider=provider,
            model=model,
            allowed_sensitivity=allowed_sensitivity,
            batch_size=batch_size,
            token_limit=token_limit,
            cost_limit_usd=cost_limit_usd,
        )
        self._write_authorization(authorization, event_type="authorized")
        return authorization

    def revoke_cloud(self) -> ScheduledCloudAuthorization:
        authorization = self.authorization()
        if authorization.status == "revoked":
            return authorization
        revoked = replace(
            authorization,
            status="revoked",
            revoked_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write_authorization(revoked, event_type="revoked")
        return revoked

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
        )
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            schedules = _state_mapping(state, "schedules")
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
        if schedule.mode == "cloud":
            raise UserInputError(
                "scheduled cloud analysis requires a configured capability adapter"
            )
        proposals = LocalMemoryCore(self._root).propose_manual_consolidation(
            schedule.task
        )
        due_at = datetime.fromisoformat(schedule.next_run_at)
        next_run_at = (due_at + timedelta(hours=schedule.every_hours)).isoformat()
        run_id = "run_" + hashlib.sha256(
            f"{schedule.schedule_id}:{schedule.next_run_at}".encode("utf-8")
        ).hexdigest()
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
        )
        updated_schedule = replace(schedule, next_run_at=next_run_at)
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
            schedules = _state_mapping(state, "schedules")
            schedules[schedule.schedule_id] = updated_schedule.to_data()
            runs = _state_mapping(state, "runs")
            runs[run.run_id] = run.to_data()
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
                            "type": "consolidation.schedule-completed",
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                            "run_id": run.run_id,
                            "schedule_id": run.schedule_id,
                            "proposal_ids": [
                                proposal.proposal_id for proposal in proposals
                            ],
                            "canonical_changes": 0,
                        },
                    ),
                ],
            )
        return run

    def _write_authorization(
        self,
        authorization: ScheduledCloudAuthorization,
        *,
        event_type: str,
    ) -> None:
        self._ensure_initialized()
        state_path = self._root / SCHEDULED_CONSOLIDATION_STATE
        event_id = f"evt_{uuid.uuid4().hex}"
        occurred_at = datetime.now(timezone.utc).isoformat()
        with writer_lock(self._root):
            recover_transactions(self._root)
            state = _load_state(state_path)
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
                        },
                    ),
                ],
            )

    def _load_authorization(self) -> ScheduledCloudAuthorization:
        state = _load_state(self._root / SCHEDULED_CONSOLIDATION_STATE)
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


def _load_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {
            "schema_version": 1,
            "authorization": None,
            "schedules": {},
            "runs": {},
            "pending_reviews": [],
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
        )
    except (KeyError, TypeError) as error:
        raise IntegrityError(f"invalid consolidation schedule: {schedule_id}") from error
