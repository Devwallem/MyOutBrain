from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal, cast
import uuid

from myoutbrain.core_types import ConfigurationConflict, IntegrityError, UserInputError
from myoutbrain.persistence import (
    atomic_commit,
    event_journal_change,
    json_document,
    recover_transactions,
    writer_lock,
)


SCHEDULED_CONSOLIDATION_STATE = Path("store") / "scheduled-consolidation.json"
AuthorizationStatus = Literal["active", "revoked"]


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
        return {"schema_version": 1, "authorization": None}
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
