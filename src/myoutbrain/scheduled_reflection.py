from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import uuid

from myoutbrain.core_types import (
    IdempotencyConflict,
    IntegrityError,
    LeaseConflict,
    UserInputError,
    VersionConflict,
)
from myoutbrain.reflection import (
    ImmediateReflectionRequest,
    read_reflection_inputs,
    ReflectionInput,
    stage_immediate_reflection,
)


SCHEDULED_REFLECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reflection_schedules (
    schedule_id TEXT PRIMARY KEY CHECK (schedule_id = 'routine'),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    every_hours INTEGER NOT NULL CHECK (every_hours BETWEEN 1 AND 8760),
    next_due_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reflection_runtime_operations (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_reflection_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('queued', 'claimed', 'completed', 'abandoned')),
    run_version INTEGER NOT NULL CHECK (run_version >= 0),
    trigger TEXT NOT NULL CHECK (trigger = 'scheduled'),
    scheduled_for TEXT NOT NULL,
    closure_hash TEXT NOT NULL,
    frozen_input_count INTEGER NOT NULL CHECK (frozen_input_count > 0),
    queued_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_token TEXT UNIQUE,
    lease_expires_at TEXT,
    result_json TEXT,
    abandonment_reason TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduled_reflection_run_inputs (
    run_id TEXT NOT NULL REFERENCES scheduled_reflection_runs(run_id) ON DELETE CASCADE,
    input_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    frozen_input_json TEXT NOT NULL,
    PRIMARY KEY (run_id, input_id),
    UNIQUE (run_id, ordinal)
);
"""

SCHEDULED_ABANDONMENT_REASONS = frozenset(
    {
        "source-permanently-unavailable",
        "source-permanently-deleted",
        "source-access-permanently-revoked",
        "frozen-input-permanently-corrupt",
    }
)


@dataclass(frozen=True)
class ReflectionSchedule:
    enabled: bool
    every_hours: int
    next_due_at: str
    version: int

    def to_data(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "every_hours": self.every_hours,
            "next_due_at": self.next_due_at,
            "version": self.version,
        }


def stage_reflection_schedule(
    database_path: Path,
    *,
    enabled: bool,
    first_due_at: str,
    every_hours: int,
    expected_version: int,
    idempotency_key: str,
) -> tuple[bytes, dict[str, object]]:
    normalized_key = _idempotency_key(idempotency_key)
    due_at = _time(first_due_at, field="first_due_at")
    if every_hours < 1 or every_hours > 8760:
        raise UserInputError("reflection schedule every_hours must be between 1 and 8760")
    request = {
        "enabled": enabled,
        "first_due_at": due_at.isoformat(),
        "every_hours": every_hours,
        "expected_version": expected_version,
    }
    request_hash = _request_hash(request)
    temporary_path = _copy_database(database_path)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            existing = _existing_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.schedule",
                request_hash=request_hash,
            )
            if existing is not None:
                return temporary_path.read_bytes(), existing
            row = connection.execute(
                "SELECT version FROM reflection_schedules WHERE schedule_id = 'routine'"
            ).fetchone()
            actual_version = cast(int, row[0]) if row is not None else 0
            if expected_version != actual_version:
                raise VersionConflict(
                    "reflection schedule version conflict: "
                    f"expected {expected_version}, actual {actual_version}",
                    expected=expected_version,
                    actual=actual_version,
                )
            schedule = ReflectionSchedule(
                enabled=enabled,
                every_hours=every_hours,
                next_due_at=due_at.isoformat(),
                version=actual_version + 1,
            )
            connection.execute(
                """
                INSERT INTO reflection_schedules
                    (schedule_id, enabled, every_hours, next_due_at, version, updated_at)
                VALUES ('routine', ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    every_hours = excluded.every_hours,
                    next_due_at = excluded.next_due_at,
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (
                    int(schedule.enabled),
                    schedule.every_hours,
                    schedule.next_due_at,
                    schedule.version,
                    datetime.now(due_at.tzinfo).isoformat(),
                ),
            )
            result: dict[str, object] = {"schedule": schedule.to_data()}
            _record_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.schedule",
                request_hash=request_hash,
                result=result,
                run_id=None,
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot configure reflection schedule") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_scheduled_reflection_enqueue(
    database_path: Path,
    *,
    now: str,
    expected_version: int,
    idempotency_key: str,
) -> tuple[bytes, dict[str, object]]:
    normalized_key = _idempotency_key(idempotency_key)
    current_time = _time(now, field="now")
    if expected_version != 0:
        raise UserInputError("reflection.enqueue expected_version must be 0")
    request = {"now": current_time.isoformat(), "expected_version": expected_version}
    request_hash = _request_hash(request)
    temporary_path = _copy_database(database_path)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            existing = _existing_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.enqueue",
                request_hash=request_hash,
            )
            if existing is not None:
                return temporary_path.read_bytes(), existing
            row = connection.execute(
                """
                SELECT enabled, every_hours, next_due_at, version
                FROM reflection_schedules WHERE schedule_id = 'routine'
                """
            ).fetchone()
            if row is None:
                raise UserInputError("reflection schedule is not configured")
            schedule = _schedule_from_row(row)
            next_due = _time(schedule.next_due_at, field="stored next_due_at")
            if not schedule.enabled:
                result = _enqueue_result(schedule, reason="disabled")
            elif current_time < next_due:
                result = _enqueue_result(schedule, reason="not-due")
            else:
                inputs = _available_scheduled_inputs(temporary_path, connection)
                advanced_due = next_due
                while advanced_due <= current_time:
                    advanced_due += timedelta(hours=schedule.every_hours)
                schedule = ReflectionSchedule(
                    enabled=schedule.enabled,
                    every_hours=schedule.every_hours,
                    next_due_at=advanced_due.isoformat(),
                    version=schedule.version + 1,
                )
                connection.execute(
                    """
                    UPDATE reflection_schedules
                    SET next_due_at = ?, version = ?, updated_at = ?
                    WHERE schedule_id = 'routine'
                    """,
                    (schedule.next_due_at, schedule.version, current_time.isoformat()),
                )
                if not inputs:
                    result = _enqueue_result(schedule, reason="empty")
                else:
                    input_ids = tuple(item.input_id for item in inputs)
                    closure_hash = _request_hash(
                        {
                            "scheduled_for": next_due.isoformat(),
                            "input_ids": list(input_ids),
                            "inputs": [item.to_data() for item in inputs],
                        }
                    )
                    run_id = "rfr_" + hashlib.sha256(
                        f"routine:{schedule.version - 1}:{next_due.isoformat()}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO scheduled_reflection_runs
                            (run_id, status, run_version, trigger, scheduled_for,
                             closure_hash, frozen_input_count, queued_at)
                        VALUES (?, 'queued', 0, 'scheduled', ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            next_due.isoformat(),
                            closure_hash,
                            len(inputs),
                            current_time.isoformat(),
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO scheduled_reflection_run_inputs
                            (run_id, input_id, ordinal, frozen_input_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            (run_id, item.input_id, ordinal, _json(item.to_data()))
                            for ordinal, item in enumerate(inputs)
                        ),
                    )
                    result = {
                        "queued": True,
                        "reason": "due",
                        "run": {
                            "run_id": run_id,
                            "trigger": "scheduled",
                            "status": "queued",
                            "version": 0,
                            "scheduled_for": next_due.isoformat(),
                            "frozen_input_count": len(inputs),
                            "input_ids": list(input_ids),
                        },
                        "schedule": schedule.to_data(),
                        "wake_capability_engine": False,
                    }
            _record_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.enqueue",
                request_hash=request_hash,
                result=result,
                run_id=(
                    cast(str, cast(dict[str, object], result["run"])["run_id"])
                    if result["run"] is not None
                    else None
                ),
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot enqueue scheduled reflection") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_scheduled_reflection_claim(
    database_path: Path,
    *,
    now: str,
    lease_seconds: int,
    claimed_by: str,
    expected_version: int,
    idempotency_key: str,
) -> tuple[bytes, dict[str, object]]:
    normalized_key = _idempotency_key(idempotency_key)
    current_time = _time(now, field="now")
    if lease_seconds < 30 or lease_seconds > 3600:
        raise UserInputError("reflection claim lease_seconds must be between 30 and 3600")
    normalized_client = claimed_by.strip()
    if not normalized_client or len(normalized_client) > 200:
        raise UserInputError("reflection claim client must contain 1 to 200 characters")
    if expected_version != 0:
        raise UserInputError("reflection.claim expected_version must be 0")
    request = {
        "now": current_time.isoformat(),
        "lease_seconds": lease_seconds,
        "claimed_by": normalized_client,
        "expected_version": expected_version,
    }
    request_hash = _request_hash(request)
    temporary_path = _copy_database(database_path)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            _requeue_expired_claims(connection, current_time)
            existing = _existing_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.claim",
                request_hash=request_hash,
            )
            if existing is not None:
                connection.commit()
                return temporary_path.read_bytes(), _rehydrate_claim_result(
                    connection, existing
                )
            row = connection.execute(
                """
                SELECT run_id, run_version, scheduled_for, closure_hash,
                       frozen_input_count
                FROM scheduled_reflection_runs
                WHERE status = 'queued'
                ORDER BY scheduled_for, queued_at, run_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                result: dict[str, object] = {
                    "claimed": False,
                    "reason": "no-work",
                    "run": None,
                }
                run_id = None
            else:
                run_id = cast(str, row[0])
                previous_version = cast(int, row[1])
                scheduled_for = cast(str, row[2])
                closure_hash = cast(str, row[3])
                frozen_count = cast(int, row[4])
                inputs = _frozen_inputs(connection, run_id)
                if len(inputs) != frozen_count:
                    raise IntegrityError("scheduled reflection input closure is incomplete")
                calculated_hash = _request_hash(
                    {
                        "scheduled_for": scheduled_for,
                        "input_ids": [cast(str, item["input_id"]) for item in inputs],
                        "inputs": inputs,
                    }
                )
                if calculated_hash != closure_hash:
                    raise IntegrityError("scheduled reflection input closure is invalid")
                lease_token = "lease_" + uuid.uuid4().hex
                lease_expires_at = current_time + timedelta(seconds=lease_seconds)
                run_version = previous_version + 1
                changed = connection.execute(
                    """
                    UPDATE scheduled_reflection_runs
                    SET status = 'claimed', run_version = ?, claimed_by = ?,
                        lease_token = ?, lease_expires_at = ?
                    WHERE run_id = ? AND status = 'queued' AND run_version = ?
                    """,
                    (
                        run_version,
                        normalized_client,
                        lease_token,
                        lease_expires_at.isoformat(),
                        run_id,
                        previous_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise IntegrityError("scheduled reflection claim lost its lease race")
                result = {
                    "claimed": True,
                    "reason": "claimed",
                    "run": {
                        "run_id": run_id,
                        "trigger": "scheduled",
                        "status": "claimed",
                        "version": run_version,
                        "scheduled_for": scheduled_for,
                        "frozen_input_count": frozen_count,
                        "claimed_by": normalized_client,
                        "lease_token": lease_token,
                        "lease_expires_at": lease_expires_at.isoformat(),
                        "inputs": inputs,
                    },
                }
            _record_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.claim",
                request_hash=request_hash,
                result=_claim_result_for_storage(result),
                run_id=run_id,
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot claim scheduled reflection") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_scheduled_reflection_return(
    database_path: Path,
    *,
    run_id: str,
    lease_token: str,
    now: str,
    reason: str,
    returned_by: str,
    expected_version: int,
    idempotency_key: str,
) -> tuple[bytes, dict[str, object]]:
    normalized_key = _idempotency_key(idempotency_key)
    normalized_run_id = _required_text(run_id, field="run_id", maximum=100)
    normalized_token = _required_text(
        lease_token, field="lease_token", maximum=100
    )
    current_time = _time(now, field="now")
    normalized_reason = _required_text(reason, field="return reason", maximum=200)
    normalized_client = _required_text(
        returned_by, field="return client", maximum=200
    )
    request = {
        "run_id": normalized_run_id,
        "lease_token": normalized_token,
        "now": current_time.isoformat(),
        "reason": normalized_reason,
        "returned_by": normalized_client,
        "expected_version": expected_version,
    }
    request_hash = _request_hash(request)
    temporary_path = _copy_database(database_path)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            existing = _existing_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.return",
                request_hash=request_hash,
            )
            if existing is not None:
                return temporary_path.read_bytes(), existing
            row = connection.execute(
                """
                SELECT status, run_version, claimed_by, lease_token
                FROM scheduled_reflection_runs WHERE run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise UserInputError(f"unknown scheduled reflection run: {normalized_run_id}")
            if row[1] != expected_version:
                raise VersionConflict(
                    "scheduled reflection run version conflict: "
                    f"expected {expected_version}, actual {row[1]}",
                    expected=expected_version,
                    actual=cast(int, row[1]),
                )
            if row[0] != "claimed":
                raise LeaseConflict("scheduled reflection run is not claimed")
            if row[2] != normalized_client or row[3] != normalized_token:
                raise LeaseConflict("scheduled reflection lease does not belong to this client")
            new_version = expected_version + 1
            changed = connection.execute(
                """
                UPDATE scheduled_reflection_runs
                SET status = 'queued', run_version = ?, claimed_by = NULL,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'claimed' AND run_version = ?
                      AND claimed_by = ? AND lease_token = ?
                """,
                (
                    new_version,
                    normalized_run_id,
                    expected_version,
                    normalized_client,
                    normalized_token,
                ),
            ).rowcount
            if changed != 1:
                raise IntegrityError("scheduled reflection return lost its lease race")
            _redact_claim_operations(
                connection,
                run_id=normalized_run_id,
                status="queued",
                version=new_version,
                reason="lease-returned",
            )
            result: dict[str, object] = {
                "returned": True,
                "reason": normalized_reason,
                "run": {
                    "run_id": normalized_run_id,
                    "status": "queued",
                    "version": new_version,
                },
            }
            _record_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.return",
                request_hash=request_hash,
                result=result,
                run_id=normalized_run_id,
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot return scheduled reflection") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_scheduled_reflection_completion(
    database_path: Path,
    request: ImmediateReflectionRequest,
    *,
    run_id: str,
    lease_token: str,
    completed_at: str,
    completed_by: str,
    expected_version: int,
    idempotency_key: str,
) -> tuple[bytes, dict[str, object]]:
    normalized_key = _idempotency_key(idempotency_key)
    normalized_run_id = _required_text(run_id, field="run_id", maximum=100)
    normalized_token = _required_text(
        lease_token, field="lease_token", maximum=100
    )
    completion_time = _time(completed_at, field="completed_at")
    normalized_client = _required_text(
        completed_by, field="completion client", maximum=200
    )
    request_data = {
        "run_id": normalized_run_id,
        "lease_token": normalized_token,
        "completed_at": completion_time.isoformat(),
        "completed_by": normalized_client,
        "expected_version": expected_version,
        "reflection": request.to_data(),
    }
    request_hash = _request_hash(request_data)
    temporary_path = _copy_database(database_path)
    internal_key = "scheduled-complete:" + hashlib.sha256(
        normalized_key.encode("utf-8")
    ).hexdigest()
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            existing = _existing_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.complete",
                request_hash=request_hash,
            )
            if existing is not None:
                return temporary_path.read_bytes(), existing
            row = connection.execute(
                """
                SELECT status, run_version, claimed_by, lease_token, lease_expires_at
                FROM scheduled_reflection_runs WHERE run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise UserInputError(f"unknown scheduled reflection run: {normalized_run_id}")
            if row[1] != expected_version:
                raise VersionConflict(
                    "scheduled reflection run version conflict: "
                    f"expected {expected_version}, actual {row[1]}",
                    expected=expected_version,
                    actual=cast(int, row[1]),
                )
            if row[0] != "claimed":
                raise LeaseConflict("scheduled reflection run is not claimed")
            if row[2] != normalized_client or row[3] != normalized_token:
                raise LeaseConflict("scheduled reflection lease does not belong to this client")
            if not isinstance(row[4], str):
                raise IntegrityError("claimed scheduled reflection has no lease expiry")
            if completion_time >= _time(row[4], field="stored lease_expires_at"):
                raise LeaseConflict("scheduled reflection lease has expired")
            frozen_ids = tuple(
                cast(str, item["input_id"])
                for item in _frozen_inputs(connection, normalized_run_id)
            )
            if request.input_ids != frozen_ids:
                raise UserInputError(
                    "scheduled reflection completion must use the frozen input closure"
                )
            connection.execute(
                "DELETE FROM scheduled_reflection_run_inputs WHERE run_id = ?",
                (normalized_run_id,),
            )
            connection.commit()
        staged_database, immediate = stage_immediate_reflection(
            temporary_path,
            request,
            idempotency_key=internal_key,
        )
        temporary_path.write_bytes(staged_database)
        result = immediate.to_data()
        result["run_id"] = normalized_run_id
        new_version = expected_version + 1
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            removed = connection.execute(
                "DELETE FROM reflection_runs WHERE idempotency_key = ?",
                (internal_key,),
            ).rowcount
            if removed != 1:
                raise IntegrityError("scheduled reflection completion record is missing")
            changed = connection.execute(
                """
                UPDATE scheduled_reflection_runs
                SET status = 'completed', run_version = ?, result_json = ?,
                    claimed_by = ?, lease_token = NULL, lease_expires_at = NULL,
                    completed_at = ?
                WHERE run_id = ? AND status = 'claimed' AND run_version = ?
                      AND claimed_by = ? AND lease_token = ?
                """,
                (
                    new_version,
                    _json(result),
                    normalized_client,
                    completion_time.isoformat(),
                    normalized_run_id,
                    expected_version,
                    normalized_client,
                    normalized_token,
                ),
            ).rowcount
            if changed != 1:
                raise IntegrityError("scheduled reflection completion lost its lease race")
            connection.execute(
                "DELETE FROM scheduled_reflection_run_inputs WHERE run_id = ?",
                (normalized_run_id,),
            )
            _redact_claim_operations(
                connection,
                run_id=normalized_run_id,
                status="completed",
                version=new_version,
            )
            _record_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.complete",
                request_hash=request_hash,
                result=result,
                run_id=normalized_run_id,
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot complete scheduled reflection") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_scheduled_reflection_abandonment(
    database_path: Path,
    *,
    run_id: str,
    abandoned_at: str,
    reason: str,
    permanently_missing_input_ids: tuple[str, ...],
    confirm_permanent_missing: bool,
    abandoned_by: str,
    expected_version: int,
    idempotency_key: str,
) -> tuple[bytes, dict[str, object]]:
    normalized_key = _idempotency_key(idempotency_key)
    normalized_run_id = _required_text(run_id, field="run_id", maximum=100)
    abandonment_time = _time(abandoned_at, field="abandoned_at")
    normalized_reason = _required_text(
        reason, field="abandonment reason", maximum=500
    )
    if normalized_reason not in SCHEDULED_ABANDONMENT_REASONS:
        raise UserInputError(
            "reflection abandonment reason must be a supported non-body reason code"
        )
    normalized_client = _required_text(
        abandoned_by, field="abandonment client", maximum=200
    )
    if not confirm_permanent_missing:
        raise UserInputError(
            "reflection abandonment requires explicit permanent-missing confirmation"
        )
    if (
        not permanently_missing_input_ids
        or len(permanently_missing_input_ids)
        != len(set(permanently_missing_input_ids))
        or any(not item.strip() for item in permanently_missing_input_ids)
    ):
        raise UserInputError(
            "reflection abandonment requires unique permanently missing input ids"
        )
    request = {
        "run_id": normalized_run_id,
        "abandoned_at": abandonment_time.isoformat(),
        "reason": normalized_reason,
        "permanently_missing_input_ids": list(permanently_missing_input_ids),
        "confirm_permanent_missing": confirm_permanent_missing,
        "abandoned_by": normalized_client,
        "expected_version": expected_version,
    }
    request_hash = _request_hash(request)
    temporary_path = _copy_database(database_path)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            existing = _existing_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.abandon",
                request_hash=request_hash,
            )
            if existing is not None:
                return temporary_path.read_bytes(), existing
            row = connection.execute(
                """
                SELECT status, run_version
                FROM scheduled_reflection_runs WHERE run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise UserInputError(f"unknown scheduled reflection run: {normalized_run_id}")
            if row[1] != expected_version:
                raise VersionConflict(
                    "scheduled reflection run version conflict: "
                    f"expected {expected_version}, actual {row[1]}",
                    expected=expected_version,
                    actual=cast(int, row[1]),
                )
            if row[0] not in ("queued", "claimed"):
                raise UserInputError("scheduled reflection run is already terminal")
            frozen_ids = tuple(
                cast(str, item["input_id"])
                for item in _frozen_inputs(connection, normalized_run_id)
            )
            missing = set(permanently_missing_input_ids)
            if not missing <= set(frozen_ids):
                raise UserInputError(
                    "permanently missing inputs must belong to the frozen closure"
                )
            new_version = expected_version + 1
            result: dict[str, object] = {
                "run_id": normalized_run_id,
                "status": "abandoned",
                "cleaned_input_ids": list(permanently_missing_input_ids),
                "reason": normalized_reason,
            }
            changed = connection.execute(
                """
                UPDATE scheduled_reflection_runs
                SET status = 'abandoned', run_version = ?, result_json = ?,
                    abandonment_reason = ?, claimed_by = ?, lease_token = NULL,
                    lease_expires_at = NULL, completed_at = ?
                WHERE run_id = ? AND run_version = ?
                      AND status IN ('queued', 'claimed')
                """,
                (
                    new_version,
                    _json(result),
                    normalized_reason,
                    normalized_client,
                    abandonment_time.isoformat(),
                    normalized_run_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise IntegrityError("scheduled reflection abandonment lost its race")
            connection.executemany(
                "DELETE FROM reflection_inputs WHERE input_id = ?",
                ((input_id,) for input_id in permanently_missing_input_ids),
            )
            connection.execute(
                "DELETE FROM scheduled_reflection_run_inputs WHERE run_id = ?",
                (normalized_run_id,),
            )
            _redact_claim_operations(
                connection,
                run_id=normalized_run_id,
                status="abandoned",
                version=new_version,
            )
            _record_operation(
                connection,
                idempotency_key=normalized_key,
                operation="reflection.abandon",
                request_hash=request_hash,
                result=result,
                run_id=normalized_run_id,
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot abandon scheduled reflection") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _enqueue_result(
    schedule: ReflectionSchedule,
    *,
    reason: str,
) -> dict[str, object]:
    return {
        "queued": False,
        "reason": reason,
        "run": None,
        "schedule": schedule.to_data(),
        "wake_capability_engine": False,
    }


def _available_scheduled_inputs(
    database_path: Path,
    connection: sqlite3.Connection,
) -> tuple[ReflectionInput, ...]:
    reserved_rows = connection.execute(
        """
        SELECT input.input_id
        FROM scheduled_reflection_run_inputs AS input
        JOIN scheduled_reflection_runs AS run ON run.run_id = input.run_id
        WHERE run.status IN ('queued', 'claimed')
        """
    ).fetchall()
    reserved = {cast(str, row[0]) for row in reserved_rows}
    available, _, _ = read_reflection_inputs(
        database_path,
        limit=100,
        budget_bytes=128 * 1024,
    )
    selected: list[ReflectionInput] = []
    used_bytes = len(b'{"inputs":[]}')
    for item in available:
        if item.input_id in reserved:
            continue
        input_bytes = len(_json(item.to_data()).encode("utf-8"))
        separator_bytes = 1 if selected else 0
        if len(selected) >= 20 or used_bytes + separator_bytes + input_bytes > 64 * 1024:
            break
        selected.append(item)
        used_bytes += separator_bytes + input_bytes
    return tuple(selected)


def _frozen_inputs(
    connection: sqlite3.Connection,
    run_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT frozen_input_json
        FROM scheduled_reflection_run_inputs
        WHERE run_id = ? ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()
    inputs: list[dict[str, object]] = []
    for row in rows:
        try:
            item = json.loads(cast(str, row[0]))
        except (TypeError, json.JSONDecodeError) as error:
            raise IntegrityError("scheduled reflection input closure is invalid") from error
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise IntegrityError("scheduled reflection input closure is invalid")
        inputs.append(cast(dict[str, object], item))
    return inputs


def _requeue_expired_claims(
    connection: sqlite3.Connection,
    now: datetime,
) -> None:
    rows = connection.execute(
        """
        SELECT run_id, run_version, lease_expires_at
        FROM scheduled_reflection_runs WHERE status = 'claimed'
        """
    ).fetchall()
    for row in rows:
        run_id = cast(str, row[0])
        run_version = cast(int, row[1])
        expires_at = row[2]
        if not isinstance(expires_at, str):
            raise IntegrityError("claimed scheduled reflection has no lease expiry")
        if _time(expires_at, field="stored lease_expires_at") <= now:
            changed = connection.execute(
                """
                UPDATE scheduled_reflection_runs
                SET status = 'queued', run_version = ?, claimed_by = NULL,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'claimed' AND run_version = ?
                """,
                (run_version + 1, run_id, run_version),
            ).rowcount
            if changed == 1:
                _redact_claim_operations(
                    connection,
                    run_id=run_id,
                    status="queued",
                    version=run_version + 1,
                    reason="lease-expired",
                )


def _redact_claim_operations(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    version: int,
    reason: str = "run-finished",
) -> None:
    redacted = {
        "claimed": False,
        "reason": reason,
        "run": {"run_id": run_id, "status": status, "version": version},
    }
    connection.execute(
        """
        UPDATE reflection_runtime_operations
        SET result_json = ?
        WHERE operation = 'reflection.claim' AND run_id = ?
        """,
        (_json(redacted), run_id),
    )


def _claim_result_for_storage(result: dict[str, object]) -> dict[str, object]:
    run = result.get("run")
    if not isinstance(run, dict):
        return result
    stored_run = {
        cast(str, key): value
        for key, value in cast(dict[object, object], run).items()
        if key != "inputs"
    }
    return {**result, "run": stored_run}


def _rehydrate_claim_result(
    connection: sqlite3.Connection,
    result: dict[str, object],
) -> dict[str, object]:
    run = result.get("run")
    if not isinstance(run, dict) or result.get("claimed") is not True:
        return result
    run_data = cast(dict[object, object], run)
    run_id = run_data.get("run_id")
    if not isinstance(run_id, str):
        raise IntegrityError("reflection claim operation is invalid")
    return {
        **result,
        "run": {
            **{cast(str, key): value for key, value in run_data.items()},
            "inputs": _frozen_inputs(connection, run_id),
        },
    }


def _schedule_from_row(row: tuple[object, ...]) -> ReflectionSchedule:
    if (
        len(row) != 4
        or row[0] not in (0, 1)
        or not isinstance(row[1], int)
        or not isinstance(row[2], str)
        or not isinstance(row[3], int)
    ):
        raise IntegrityError("reflection schedule is invalid")
    return ReflectionSchedule(bool(row[0]), row[1], row[2], row[3])


def _existing_operation(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    operation: str,
    request_hash: str,
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT operation, request_hash, result_json
        FROM reflection_runtime_operations WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if row[0] != operation or row[1] != request_hash:
        raise IdempotencyConflict(
            "idempotency key was already used for a different request"
        )
    try:
        result = json.loads(cast(str, row[2]))
    except (TypeError, json.JSONDecodeError) as error:
        raise IntegrityError("reflection runtime operation is invalid") from error
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise IntegrityError("reflection runtime operation is invalid")
    return cast(dict[str, object], result)


def _record_operation(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    operation: str,
    request_hash: str,
    result: dict[str, object],
    run_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO reflection_runtime_operations
            (idempotency_key, operation, request_hash, result_json, run_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            idempotency_key,
            operation,
            request_hash,
            _json(result),
            run_id,
            datetime.now().astimezone().isoformat(),
        ),
    )


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise UserInputError("idempotency key must contain 1 to 200 characters")
    return normalized


def _required_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise UserInputError(
            f"reflection {field} must contain 1 to {maximum} characters"
        )
    return normalized


def _time(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise UserInputError(f"reflection {field} must be ISO-8601") from error
    if parsed.utcoffset() is None:
        raise UserInputError(f"reflection {field} must include an offset")
    return parsed


def _request_hash(data: dict[str, object]) -> str:
    return hashlib.sha256(_json(data).encode("utf-8")).hexdigest()


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _copy_database(database_path: Path) -> Path:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".scheduled-reflection.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        return temporary_path
    except OSError as error:
        raise IntegrityError("cannot stage scheduled reflection") from error
