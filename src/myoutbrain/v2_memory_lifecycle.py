from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import cast
import uuid

from myoutbrain.core_types import ConfigurationConflict, IntegrityError, UserInputError
from myoutbrain.local_core import LocalMemoryCore, MEMORY_DATABASE
from myoutbrain.persistence import (
    atomic_commit,
    permanent_deletion_cleanup_change,
    recover_transactions,
    writer_lock,
)


class V2MemoryLifecycleService:
    """Apply explicit lifecycle decisions through the canonical V2 write boundary."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def historicize(
        self,
        memory_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        normalized_memory_id = _identifier("memory id", memory_id, "mem_")
        normalized_reason = _text("lifecycle reason", reason, maximum=2_000)
        normalized_key = _text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _text("entrance", entrance, maximum=64)
        if expected_version < 1:
            raise UserInputError("expected version must be at least 1")
        request_hash = _stable_hash(
            {
                "operation": "historicize-memory",
                "memory_id": normalized_memory_id,
                "reason": normalized_reason,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            _raise_if_erased(database_path, normalized_memory_id)
            existing = _historicization_for_key(
                database_path,
                normalized_key,
                normalized_memory_id,
                request_hash,
                normalized_reason,
            )
            if existing is not None:
                return existing
            staged_database, result = _stage_historicization(
                database_path,
                memory_id=normalized_memory_id,
                reason=normalized_reason,
                expected_version=expected_version,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def revise(
        self,
        memory_id: str,
        *,
        body: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        normalized_memory_id = _identifier("memory id", memory_id, "mem_")
        normalized_body = _body(body)
        normalized_reason = _text("revision reason", reason, maximum=2_000)
        normalized_key = _text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _text("entrance", entrance, maximum=64)
        if expected_version < 1:
            raise UserInputError("expected version must be at least 1")
        request_hash = _stable_hash(
            {
                "operation": "revise-memory",
                "memory_id": normalized_memory_id,
                "body": normalized_body,
                "reason": normalized_reason,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            _raise_if_erased(database_path, normalized_memory_id)
            existing = _revision_for_key(
                database_path,
                normalized_key,
                normalized_memory_id,
                request_hash,
                expected_version,
                normalized_reason,
            )
            if existing is not None:
                return existing
            staged_database, result = _stage_revision(
                database_path,
                memory_id=normalized_memory_id,
                body=normalized_body,
                reason=normalized_reason,
                expected_version=expected_version,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def supersede(
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
        normalized_memory_id = _identifier("memory id", memory_id, "mem_")
        normalized_replacement_id = _identifier(
            "replacement memory id", replacement_memory_id, "mem_"
        )
        if normalized_memory_id == normalized_replacement_id:
            raise UserInputError("a memory cannot supersede itself")
        normalized_reason = _text("supersession reason", reason, maximum=2_000)
        normalized_key = _text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _text("entrance", entrance, maximum=64)
        if expected_version < 1 or replacement_version < 1:
            raise UserInputError("memory versions must be at least 1")
        request_hash = _stable_hash(
            {
                "operation": "supersede-memory",
                "memory_id": normalized_memory_id,
                "expected_version": expected_version,
                "replacement_memory_id": normalized_replacement_id,
                "replacement_version": replacement_version,
                "reason": normalized_reason,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            _raise_if_erased(database_path, normalized_memory_id)
            existing = _supersession_for_key(
                database_path,
                normalized_key,
                normalized_memory_id,
                request_hash,
                expected_version,
                normalized_reason,
            )
            if existing is not None:
                return existing
            staged_database, result = _stage_supersession(
                database_path,
                memory_id=normalized_memory_id,
                expected_version=expected_version,
                replacement_memory_id=normalized_replacement_id,
                replacement_version=replacement_version,
                reason=normalized_reason,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def deactivate(
        self,
        memory_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return self._change_availability(
            memory_id,
            restore=False,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def restore(
        self,
        memory_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        return self._change_availability(
            memory_id,
            restore=True,
            reason=reason,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            entrance=entrance,
        )

    def _change_availability(
        self,
        memory_id: str,
        *,
        restore: bool,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        operation = "restore-memory" if restore else "deactivate-memory"
        normalized_memory_id = _identifier("memory id", memory_id, "mem_")
        normalized_reason = _text("lifecycle reason", reason, maximum=2_000)
        normalized_key = _text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _text("entrance", entrance, maximum=64)
        if expected_version < 1:
            raise UserInputError("expected version must be at least 1")
        request_hash = _stable_hash(
            {
                "operation": operation,
                "memory_id": normalized_memory_id,
                "reason": normalized_reason,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            _raise_if_erased(database_path, normalized_memory_id)
            existing = _availability_change_for_key(
                database_path,
                operation,
                normalized_key,
                normalized_memory_id,
                request_hash,
            )
            if existing is not None:
                return existing
            staged_database, result = _stage_availability_change(
                database_path,
                operation=operation,
                memory_id=normalized_memory_id,
                restore=restore,
                reason=normalized_reason,
                expected_version=expected_version,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return result

    def erase(
        self,
        memory_id: str,
        *,
        confirmation: str | None,
        entrance: str,
    ) -> dict[str, object]:
        normalized_memory_id = _identifier("memory id", memory_id, "mem_")
        normalized_entrance = _text("entrance", entrance, maximum=64)
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            existing_marker = _erasure_marker(database_path, normalized_memory_id)
            if existing_marker is not None:
                return existing_marker
            impact = _erasure_impact(database_path, normalized_memory_id)
            if confirmation is None:
                return impact
            if confirmation != impact["confirmation_token"]:
                raise UserInputError(
                    "permanent erasure confirmation does not match the current impact closure"
                )
            staged_database, result, object_references = _stage_erasure(
                database_path,
                impact=impact,
                entrance=normalized_entrance,
            )
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    permanent_deletion_cleanup_change(
                        self._root,
                        object_references=object_references,
                        view_paths=(),
                    ),
                ],
            )
            recover_transactions(self._root)
        return result


@contextmanager
def _staged_database(
    database_path: Path,
    *,
    prefix: str,
) -> Iterator[tuple[Path, sqlite3.Connection]]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=prefix,
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            yield temporary_path, connection
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _stage_historicization(
    database_path: Path,
    *,
    memory_id: str,
    reason: str,
    expected_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
) -> tuple[bytes, dict[str, object]]:
    occurred_at = datetime.now(timezone.utc).isoformat()
    try:
        with _staged_database(
            database_path, prefix=".memory-historicize."
        ) as (temporary_path, connection):
            row = connection.execute(
                "SELECT current_version, state FROM canonical_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise UserInputError(f"canonical memory does not exist: {memory_id}")
            if row != (expected_version, "current"):
                raise UserInputError(
                    "historicize-memory requires the expected current memory version"
                )
            event_id = f"aud_{uuid.uuid4().hex}"
            result_hash = _stable_hash(
                {
                    "memory_id": memory_id,
                    "version": expected_version,
                    "from_state": "current",
                    "to_state": "historical-trusted",
                    "reason": reason,
                }
            )
            connection.execute(
                """
                UPDATE canonical_memories
                SET state = 'historical-trusted', updated_at = ?
                WHERE memory_id = ? AND current_version = ? AND state = 'current'
                """,
                (occurred_at, memory_id, expected_version),
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, 'memory.historicized', ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    occurred_at,
                    memory_id,
                    expected_version,
                    expected_version,
                    entrance,
                    result_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_lifecycle_events
                    (event_id, memory_id, from_state, to_state, reason,
                     previous_live_state)
                VALUES (?, ?, 'current', 'historical-trusted', ?, NULL)
                """,
                (event_id, memory_id, reason),
            )
            connection.execute(
                """
                INSERT INTO idempotent_writes
                    (operation, idempotency_key, subject_id, request_hash,
                     result_hash, created_at)
                VALUES ('historicize-memory', ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    memory_id,
                    request_hash,
                    result_hash,
                    occurred_at,
                ),
            )
            connection.commit()
            staged_database = temporary_path.read_bytes()
        return staged_database, _transition_result(
            memory_id=memory_id,
            version=expected_version,
            reason=reason,
            event_id=event_id,
            occurred_at=occurred_at,
            entrance=entrance,
            result_hash=result_hash,
        )
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot historicize canonical memory") from error


def _stage_revision(
    database_path: Path,
    *,
    memory_id: str,
    body: str,
    reason: str,
    expected_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
) -> tuple[bytes, dict[str, object]]:
    occurred_at = datetime.now(timezone.utc).isoformat()
    try:
        with _staged_database(database_path, prefix=".memory-revise.") as (
            temporary_path,
            connection,
        ):
            row = connection.execute(
                """
                SELECT memory.current_version, memory.state,
                       dictionary.canonical_name, dictionary.primary_capsule_id,
                       version.content, version.applicability_scope
                FROM canonical_memories AS memory
                JOIN knowledge_dictionary AS dictionary
                  ON dictionary.memory_id = memory.memory_id
                JOIN canonical_memory_versions AS version
                  ON version.memory_id = memory.memory_id
                 AND version.version = memory.current_version
                WHERE memory.memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
            if row is None:
                raise UserInputError(f"canonical memory does not exist: {memory_id}")
            if row[0] != expected_version or row[1] != "current":
                raise UserInputError(
                    "revise-memory requires the expected current memory version"
                )
            if not all(isinstance(row[index], str) for index in range(2, 6)):
                raise IntegrityError("canonical memory revision target is invalid")
            canonical_name = cast(str, row[2])
            capsule_id = cast(str, row[3])
            previous_body = cast(str, row[4])
            scope = cast(str, row[5])
            new_version = expected_version + 1
            source_ids = _source_ids(connection, memory_id, expected_version)
            event_id = f"aud_{uuid.uuid4().hex}"
            result_hash = _stable_hash(
                {
                    "memory_id": memory_id,
                    "previous_version": expected_version,
                    "current_version": new_version,
                    "body_hash": _stable_hash(body),
                    "reason": reason,
                    "source_ids": source_ids,
                }
            )
            connection.execute(
                """
                UPDATE canonical_memory_versions
                SET superseded_at = ?, supersession_reason = ?
                WHERE memory_id = ? AND version = ? AND superseded_at IS NULL
                """,
                (occurred_at, reason, memory_id, expected_version),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_versions
                    (memory_id, version, content, applicability_scope, capsule_id,
                     action, change_reason, created_at, superseded_at,
                     supersession_reason)
                VALUES (?, ?, ?, ?, ?, 'revised', ?, ?, NULL, NULL)
                """,
                (memory_id, new_version, body, scope, capsule_id, reason, occurred_at),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_version_evidence
                    (memory_id, version, source_id, source_version, relationship)
                SELECT memory_id, ?, source_id, source_version, relationship
                FROM canonical_memory_version_evidence
                WHERE memory_id = ? AND version = ?
                """,
                (new_version, memory_id, expected_version),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_version_sources
                    (memory_id, version, source_id)
                SELECT memory_id, ?, source_id
                FROM canonical_memory_version_sources
                WHERE memory_id = ? AND version = ?
                """,
                (new_version, memory_id, expected_version),
            )
            connection.execute(
                """
                UPDATE canonical_memories
                SET current_version = ?, state = 'current', content = ?, updated_at = ?
                WHERE memory_id = ? AND current_version = ?
                """,
                (new_version, body, occurred_at, memory_id, expected_version),
            )
            connection.execute(
                """
                UPDATE knowledge_dictionary SET current_version = ?
                WHERE memory_id = ? AND current_version = ?
                """,
                (new_version, memory_id, expected_version),
            )
            connection.execute(
                """
                UPDATE knowledge_capsules
                SET body_bytes = body_bytes - ? + ?, updated_at = ?
                WHERE capsule_id = ?
                """,
                (
                    len(previous_body.encode("utf-8")),
                    len(body.encode("utf-8")),
                    occurred_at,
                    capsule_id,
                ),
            )
            connection.execute("DELETE FROM canonical_memory_fts WHERE memory_id = ?", (memory_id,))
            connection.execute(
                """
                INSERT INTO canonical_memory_fts
                    (memory_id, capsule_id, canonical_name, body,
                     applicability_scope, search_terms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    capsule_id,
                    canonical_name,
                    body,
                    scope,
                    " ".join(sorted({part.casefold() for part in re.findall(r"\w+", f"{canonical_name} {body} {scope}")})),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, 'memory.revised', ?, ?, NULL, ?, ?, ?, ?)
                """,
                (event_id, occurred_at, memory_id, expected_version, new_version, entrance, result_hash),
            )
            connection.execute(
                """
                INSERT INTO idempotent_writes
                    (operation, idempotency_key, subject_id, request_hash,
                     result_hash, created_at)
                VALUES ('revise-memory', ?, ?, ?, ?, ?)
                """,
                (idempotency_key, memory_id, request_hash, result_hash, occurred_at),
            )
            connection.commit()
            staged_database = temporary_path.read_bytes()
            result = _revision_result(
                connection_path=temporary_path,
                memory_id=memory_id,
                previous_version=expected_version,
                current_version=new_version,
                reason=reason,
                event_id=event_id,
                occurred_at=occurred_at,
                entrance=entrance,
                result_hash=result_hash,
            )
        return staged_database, result
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot revise canonical memory") from error


def _stage_supersession(
    database_path: Path,
    *,
    memory_id: str,
    expected_version: int,
    replacement_memory_id: str,
    replacement_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
) -> tuple[bytes, dict[str, object]]:
    occurred_at = datetime.now(timezone.utc).isoformat()
    try:
        with _staged_database(database_path, prefix=".memory-supersede.") as (
            temporary_path,
            connection,
        ):
            target = connection.execute(
                """
                SELECT memory.current_version, memory.state, version.content
                FROM canonical_memories AS memory
                JOIN canonical_memory_versions AS version
                  ON version.memory_id = memory.memory_id
                 AND version.version = memory.current_version
                WHERE memory.memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
            replacement = connection.execute(
                """
                SELECT current_version, state FROM canonical_memories
                WHERE memory_id = ?
                """,
                (replacement_memory_id,),
            ).fetchone()
            if target is None:
                raise UserInputError(f"canonical memory does not exist: {memory_id}")
            if target[0] != expected_version or target[1] not in (
                "current",
                "historical-trusted",
            ):
                raise UserInputError(
                    "supersede-memory requires the expected current or historically trusted version"
                )
            if replacement != (replacement_version, "current"):
                raise UserInputError("replacement memory version is not current")
            if not isinstance(target[2], str):
                raise IntegrityError("superseded memory version is invalid")
            event_id = f"aud_{uuid.uuid4().hex}"
            from_state = cast(str, target[1])
            result_hash = _stable_hash(
                {
                    "memory_id": memory_id,
                    "version": expected_version,
                    "replacement_memory_id": replacement_memory_id,
                    "replacement_version": replacement_version,
                    "reason": reason,
                }
            )
            connection.execute(
                """
                UPDATE canonical_memories
                SET state = 'superseded', updated_at = ?
                WHERE memory_id = ? AND current_version = ?
                  AND state IN ('current', 'historical-trusted')
                """,
                (occurred_at, memory_id, expected_version),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_dependencies
                    (memory_id, version, depends_on_memory_id, depends_on_version,
                     relationship, created_at)
                VALUES (?, ?, ?, ?, 'supersedes', ?)
                """,
                (
                    replacement_memory_id,
                    replacement_version,
                    memory_id,
                    expected_version,
                    occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, 'memory.superseded', ?, ?, NULL, ?, ?, ?, ?)
                """,
                (event_id, occurred_at, memory_id, expected_version, expected_version, entrance, result_hash),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_lifecycle_events
                    (event_id, memory_id, from_state, to_state, reason,
                     previous_live_state)
                VALUES (?, ?, ?, 'superseded', ?, NULL)
                """,
                (event_id, memory_id, from_state, reason),
            )
            connection.execute(
                """
                INSERT INTO idempotent_writes
                    (operation, idempotency_key, subject_id, request_hash,
                     result_hash, created_at)
                VALUES ('supersede-memory', ?, ?, ?, ?, ?)
                """,
                (idempotency_key, memory_id, request_hash, result_hash, occurred_at),
            )
            connection.commit()
            staged_database = temporary_path.read_bytes()
            result = _supersession_result(
                connection_path=temporary_path,
                memory_id=memory_id,
                version=expected_version,
                reason=reason,
                event_id=event_id,
                occurred_at=occurred_at,
                entrance=entrance,
                result_hash=result_hash,
            )
        return staged_database, result
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot supersede canonical memory") from error


def _stage_availability_change(
    database_path: Path,
    *,
    operation: str,
    memory_id: str,
    restore: bool,
    reason: str,
    expected_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
) -> tuple[bytes, dict[str, object]]:
    occurred_at = datetime.now(timezone.utc).isoformat()
    try:
        with _staged_database(database_path, prefix=".memory-availability.") as (
            temporary_path,
            connection,
        ):
            row = connection.execute(
                """
                SELECT current_version, state, previous_live_state
                FROM canonical_memories WHERE memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
            if row is None:
                raise UserInputError(f"canonical memory does not exist: {memory_id}")
            if row[0] != expected_version:
                raise UserInputError(f"{operation} expected version conflict")
            if restore:
                if row[1] != "inactive" or row[2] not in (
                    "current",
                    "historical-trusted",
                    "superseded",
                ):
                    raise UserInputError("restore-memory requires an inactive memory")
                if not _dependencies_complete(connection, memory_id, expected_version):
                    raise UserInputError("inactive memory dependencies are incomplete")
                from_state = "inactive"
                to_state = cast(str, row[2])
                restorable_state: str | None = None
            else:
                if row[1] not in ("current", "historical-trusted", "superseded"):
                    raise UserInputError("deactivate-memory requires a live memory")
                from_state = cast(str, row[1])
                to_state = "inactive"
                restorable_state = from_state
            event_id = f"aud_{uuid.uuid4().hex}"
            event_type = "memory.restored" if restore else "memory.deactivated"
            result_hash = _stable_hash(
                {
                    "memory_id": memory_id,
                    "version": expected_version,
                    "from_state": from_state,
                    "to_state": to_state,
                    "reason": reason,
                }
            )
            connection.execute(
                """
                UPDATE canonical_memories
                SET state = ?, previous_live_state = ?, updated_at = ?
                WHERE memory_id = ? AND current_version = ?
                """,
                (to_state, restorable_state, occurred_at, memory_id, expected_version),
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (event_id, event_type, occurred_at, memory_id, expected_version, expected_version, entrance, result_hash),
            )
            connection.execute(
                """
                INSERT INTO canonical_memory_lifecycle_events
                    (event_id, memory_id, from_state, to_state, reason,
                     previous_live_state)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, memory_id, from_state, to_state, reason, restorable_state),
            )
            connection.execute(
                """
                INSERT INTO idempotent_writes
                    (operation, idempotency_key, subject_id, request_hash,
                     result_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (operation, idempotency_key, memory_id, request_hash, result_hash, occurred_at),
            )
            connection.commit()
            staged_database = temporary_path.read_bytes()
        return staged_database, _availability_result(
            memory_id=memory_id,
            version=expected_version,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            restorable_state=restorable_state,
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            entrance=entrance,
            result_hash=result_hash,
        )
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError(f"cannot apply {operation}") from error


def _historicization_for_key(
    database_path: Path,
    idempotency_key: str,
    memory_id: str,
    request_hash: str,
    reason: str,
) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """
                SELECT write.subject_id, write.request_hash, write.result_hash,
                       audit.event_id, audit.occurred_at, audit.entrance,
                       audit.after_version
                FROM idempotent_writes AS write
                JOIN audit_events AS audit
                  ON audit.result_hash = write.result_hash
                 AND audit.subject_id = write.subject_id
                WHERE write.operation = 'historicize-memory'
                  AND write.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect memory lifecycle idempotency") from error
    if row is None:
        return None
    if row[0] != memory_id or row[1] != request_hash:
        raise UserInputError("idempotency key was already used for a different request")
    if not all(isinstance(row[index], str) for index in range(2, 6)) or not isinstance(
        row[6], int
    ):
        raise IntegrityError("memory lifecycle idempotency record is invalid")
    return _transition_result(
        memory_id=memory_id,
        version=row[6],
        reason=reason,
        event_id=cast(str, row[3]),
        occurred_at=cast(str, row[4]),
        entrance=cast(str, row[5]),
        result_hash=cast(str, row[2]),
    )


def _revision_for_key(
    database_path: Path,
    idempotency_key: str,
    memory_id: str,
    request_hash: str,
    previous_version: int,
    reason: str,
) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """
                SELECT write.subject_id, write.request_hash, write.result_hash,
                       audit.event_id, audit.occurred_at, audit.entrance,
                       audit.after_version
                FROM idempotent_writes AS write
                JOIN audit_events AS audit
                  ON audit.result_hash = write.result_hash
                 AND audit.subject_id = write.subject_id
                WHERE write.operation = 'revise-memory'
                  AND write.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect memory revision idempotency") from error
    if row is None:
        return None
    if row[0] != memory_id or row[1] != request_hash:
        raise UserInputError("idempotency key was already used for a different request")
    if not all(isinstance(row[index], str) for index in range(2, 6)) or not isinstance(
        row[6], int
    ):
        raise IntegrityError("memory revision idempotency record is invalid")
    return _revision_result(
        connection_path=database_path,
        memory_id=memory_id,
        previous_version=previous_version,
        current_version=row[6],
        reason=reason,
        event_id=cast(str, row[3]),
        occurred_at=cast(str, row[4]),
        entrance=cast(str, row[5]),
        result_hash=cast(str, row[2]),
    )


def _supersession_for_key(
    database_path: Path,
    idempotency_key: str,
    memory_id: str,
    request_hash: str,
    version: int,
    reason: str,
) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """
                SELECT write.subject_id, write.request_hash, write.result_hash,
                       audit.event_id, audit.occurred_at, audit.entrance
                FROM idempotent_writes AS write
                JOIN audit_events AS audit
                  ON audit.result_hash = write.result_hash
                 AND audit.subject_id = write.subject_id
                WHERE write.operation = 'supersede-memory'
                  AND write.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect memory supersession idempotency") from error
    if row is None:
        return None
    if row[0] != memory_id or row[1] != request_hash:
        raise UserInputError("idempotency key was already used for a different request")
    if not all(isinstance(row[index], str) for index in range(2, 6)):
        raise IntegrityError("memory supersession idempotency record is invalid")
    return _supersession_result(
        connection_path=database_path,
        memory_id=memory_id,
        version=version,
        reason=reason,
        event_id=row[3],
        occurred_at=row[4],
        entrance=row[5],
        result_hash=row[2],
    )


def _supersession_result(
    *,
    connection_path: Path,
    memory_id: str,
    version: int,
    reason: str,
    event_id: str,
    occurred_at: str,
    entrance: str,
    result_hash: str,
) -> dict[str, object]:
    try:
        with closing(sqlite3.connect(connection_path)) as connection:
            row = connection.execute(
                """
                SELECT version.content, dependency.memory_id, dependency.version,
                       lifecycle.from_state, lifecycle.to_state
                FROM canonical_memory_versions AS version
                JOIN canonical_memory_dependencies AS dependency
                  ON dependency.depends_on_memory_id = version.memory_id
                 AND dependency.depends_on_version = version.version
                 AND dependency.relationship = 'supersedes'
                JOIN canonical_memory_lifecycle_events AS lifecycle
                  ON lifecycle.event_id = ?
                WHERE version.memory_id = ? AND version.version = ?
                """,
                (event_id, memory_id, version),
            ).fetchone()
            sources = _source_ids(connection, memory_id, version)
    except sqlite3.Error as error:
        raise IntegrityError("cannot read memory supersession") from error
    if (
        row is None
        or not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not isinstance(row[2], int)
        or not isinstance(row[3], str)
        or row[4] != "superseded"
    ):
        raise IntegrityError("memory supersession is incomplete")
    return {
        "memory_id": memory_id,
        "version": version,
        "from_state": row[3],
        "to_state": row[4],
        "reason": reason,
        "superseded_by": {"memory_id": row[1], "version": row[2]},
        "preserved_version": {
            "version": version,
            "body": row[0],
            "source_ids": list(sources),
        },
        "audit_event": {
            "event_id": event_id,
            "event_type": "memory.superseded",
            "occurred_at": occurred_at,
            "before_version": version,
            "after_version": version,
            "entrance": entrance,
            "result_hash": result_hash,
        },
    }


def _availability_change_for_key(
    database_path: Path,
    operation: str,
    idempotency_key: str,
    memory_id: str,
    request_hash: str,
) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """
                SELECT write.subject_id, write.request_hash, write.result_hash,
                       audit.event_id, audit.event_type, audit.occurred_at,
                       audit.entrance, audit.after_version, lifecycle.from_state,
                       lifecycle.to_state, lifecycle.reason,
                       lifecycle.previous_live_state
                FROM idempotent_writes AS write
                JOIN audit_events AS audit
                  ON audit.result_hash = write.result_hash
                 AND audit.subject_id = write.subject_id
                JOIN canonical_memory_lifecycle_events AS lifecycle
                  ON lifecycle.event_id = audit.event_id
                WHERE write.operation = ? AND write.idempotency_key = ?
                """,
                (operation, idempotency_key),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect memory availability idempotency") from error
    if row is None:
        return None
    if row[0] != memory_id or row[1] != request_hash:
        raise UserInputError("idempotency key was already used for a different request")
    if (
        not all(isinstance(row[index], str) for index in range(2, 7))
        or not isinstance(row[7], int)
        or not all(isinstance(row[index], str) for index in range(8, 11))
        or (row[11] is not None and not isinstance(row[11], str))
    ):
        raise IntegrityError("memory availability idempotency record is invalid")
    return _availability_result(
        memory_id=memory_id,
        version=row[7],
        from_state=row[8],
        to_state=row[9],
        reason=row[10],
        restorable_state=row[11],
        event_id=row[3],
        event_type=row[4],
        occurred_at=row[5],
        entrance=row[6],
        result_hash=row[2],
    )


def _availability_result(
    *,
    memory_id: str,
    version: int,
    from_state: str,
    to_state: str,
    reason: str,
    restorable_state: str | None,
    event_id: str,
    event_type: str,
    occurred_at: str,
    entrance: str,
    result_hash: str,
) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "version": version,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
        "restorable_state": restorable_state,
        "audit_event": {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "before_version": version,
            "after_version": version,
            "entrance": entrance,
            "result_hash": result_hash,
        },
    }


def _erasure_impact(database_path: Path, memory_id: str) -> dict[str, object]:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            return _erasure_impact_for_connection(connection, memory_id)
    except sqlite3.Error as error:
        raise IntegrityError("cannot preview permanent erasure") from error


def _erasure_impact_for_connection(
    connection: sqlite3.Connection,
    memory_id: str,
) -> dict[str, object]:
    if connection.execute(
        "SELECT 1 FROM canonical_memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone() is None:
        raise UserInputError(f"canonical memory does not exist: {memory_id}")
    closure_rows = connection.execute(
        """
        WITH RECURSIVE affected(memory_id) AS (
            VALUES (?)
            UNION
            SELECT dependency.memory_id
            FROM canonical_memory_dependencies AS dependency
            JOIN affected
              ON dependency.depends_on_memory_id = affected.memory_id
        )
        SELECT memory_id FROM affected ORDER BY memory_id
        """,
        (memory_id,),
    ).fetchall()
    closure = tuple(
        row[0] for row in closure_rows if isinstance(row[0], str)
    )
    if memory_id not in closure:
        raise IntegrityError("permanent erasure closure omitted its target")
    derivatives = tuple(value for value in closure if value != memory_id)
    memory_ids = (memory_id, *derivatives)
    placeholders = ", ".join("?" for _ in memory_ids)
    version_rows = connection.execute(
        f"""
        SELECT memory_id, current_version, state
        FROM canonical_memories
        WHERE memory_id IN ({placeholders})
        ORDER BY memory_id
        """,
        memory_ids,
    ).fetchall()
    dependency_rows = connection.execute(
        f"""
        SELECT memory_id, version, depends_on_memory_id, depends_on_version,
               relationship
        FROM canonical_memory_dependencies
        WHERE memory_id IN ({placeholders})
           OR depends_on_memory_id IN ({placeholders})
        ORDER BY memory_id, version, depends_on_memory_id, depends_on_version,
                 relationship
        """,
        (*memory_ids, *memory_ids),
    ).fetchall()
    source_rows = connection.execute(
        f"""
        SELECT DISTINCT evidence.source_id, evidence.source_version,
               source.content_hash, source.retention
        FROM canonical_memory_version_evidence AS evidence
        JOIN evidence_source_versions AS source
          ON source.source_id = evidence.source_id
         AND source.version = evidence.source_version
        WHERE evidence.memory_id IN ({placeholders})
        ORDER BY evidence.source_id, evidence.source_version
        """,
        memory_ids,
    ).fetchall()
    source_impacts: list[dict[str, object]] = []
    for source_id, source_version, content_hash, retention in source_rows:
        if (
            not isinstance(source_id, str)
            or not isinstance(source_version, int)
            or not isinstance(content_hash, str)
            or not isinstance(retention, str)
        ):
            raise IntegrityError("permanent erasure source closure is invalid")
        shared = connection.execute(
            f"""
            SELECT 1 FROM canonical_memory_version_evidence
            WHERE source_id = ? AND source_version = ?
              AND memory_id NOT IN ({placeholders})
            LIMIT 1
            """,
            (source_id, source_version, *memory_ids),
        ).fetchone() is not None
        source_impacts.append(
            {
                "source_id": source_id,
                "source_version": source_version,
                "content_hash": content_hash,
                "retention": retention,
                "action": "retain-shared" if shared else "erase-receipt",
            }
        )
    erased_receipt_source_ids = {
        cast(str, item["source_id"])
        for item in source_impacts
        if item["action"] == "erase-receipt"
    }
    fully_erased_receipt_source_ids = tuple(
        sorted(
            source_id
            for source_id in erased_receipt_source_ids
            if connection.execute(
                "SELECT COUNT(*) FROM evidence_source_versions WHERE source_id = ?",
                (source_id,),
            ).fetchone()[0]
            == sum(
                item["source_id"] == source_id
                and item["action"] == "erase-receipt"
                for item in source_impacts
            )
        )
    )
    legacy_source_rows = connection.execute(
        f"""
        SELECT DISTINCT object.source_id, object.content_hash,
               object.object_reference
        FROM source_objects AS object
        WHERE object.source_id IN (
            SELECT source_id FROM canonical_memory_sources
            WHERE memory_id IN ({placeholders})
            UNION
            SELECT source_id FROM canonical_memory_version_sources
            WHERE memory_id IN ({placeholders})
        )
        ORDER BY object.source_id
        """,
        (*memory_ids, *memory_ids),
    ).fetchall()
    legacy_source_impacts: list[dict[str, object]] = []
    erased_legacy_source_ids: list[str] = []
    for source_id, content_hash, object_reference in legacy_source_rows:
        if not all(
            isinstance(value, str)
            for value in (source_id, content_hash, object_reference)
        ):
            raise IntegrityError("permanent erasure raw source closure is invalid")
        shared = connection.execute(
            f"""
            SELECT 1 FROM canonical_memory_sources
            WHERE source_id = ? AND memory_id NOT IN ({placeholders})
            UNION
            SELECT 1 FROM canonical_memory_version_sources
            WHERE source_id = ? AND memory_id NOT IN ({placeholders})
            LIMIT 1
            """,
            (source_id, *memory_ids, source_id, *memory_ids),
        ).fetchone() is not None
        action = "retain-shared" if shared else "erase-object"
        if not shared:
            erased_legacy_source_ids.append(source_id)
        legacy_source_impacts.append(
            {
                "source_id": source_id,
                "content_hash": content_hash,
                "object_reference": object_reference,
                "action": action,
            }
        )
    erased_source_ids_tuple = tuple(erased_legacy_source_ids)
    experience_ids = tuple(
        row[0]
        for row in _rows_for_values(
            connection,
            "experiences",
            "experience_id",
            "source_id",
            erased_source_ids_tuple,
        )
        if isinstance(row[0], str)
    )
    digest_ids = tuple(
        row[0]
        for row in _rows_for_values(
            connection,
            "buffered_digests",
            "digest_id",
            "experience_id",
            experience_ids,
        )
        if isinstance(row[0], str)
    )
    proposal_id_set = set(_erasure_proposal_ids(connection, memory_ids))
    for table, column, values in (
        ("integration_proposal_sources", "source_id", erased_source_ids_tuple),
        ("integration_proposal_buffered", "digest_id", digest_ids),
    ):
        proposal_id_set.update(
            row[0]
            for row in _rows_for_values(
                connection, table, "proposal_id", column, values
            )
            if isinstance(row[0], str)
        )
    proposal_ids = tuple(sorted(proposal_id_set))
    batch_ids = tuple(
        sorted(
            row[0]
            for row in _rows_for_values(
                connection,
                "review_batch_items",
                "batch_id",
                "proposal_id",
                proposal_ids,
            )
            if isinstance(row[0], str)
        )
    )
    group_ids = tuple(
        sorted(
            row[0]
            for row in _rows_for_values(
                connection,
                "review_group_members",
                "group_id",
                "proposal_id",
                proposal_ids,
            )
            if isinstance(row[0], str)
        )
    )
    recall_ids = tuple(
        sorted(
            row[0]
            for row in _rows_for_values(
                connection,
                "recall_event_items",
                "recall_id",
                "memory_id",
                memory_ids,
            )
            if isinstance(row[0], str)
        )
    )
    relation_rows = connection.execute(
        f"""
        SELECT 'related', memory_id, related_memory_id
        FROM canonical_memory_relations
        WHERE memory_id IN ({placeholders})
           OR related_memory_id IN ({placeholders})
        UNION ALL
        SELECT 'conflict', first_memory_id, second_memory_id
        FROM canonical_memory_conflicts
        WHERE first_memory_id IN ({placeholders})
           OR second_memory_id IN ({placeholders})
        ORDER BY 1, 2, 3
        """,
        (*memory_ids, *memory_ids, *memory_ids, *memory_ids),
    ).fetchall()
    relation_impacts = [
        {"relationship": row[0], "memory_id": row[1], "related_memory_id": row[2]}
        for row in relation_rows
    ]
    lifecycle_event_ids = tuple(
        sorted(
            row[0]
            for row in _rows_for_values(
                connection,
                "canonical_memory_lifecycle_events",
                "event_id",
                "memory_id",
                memory_ids,
            )
            if isinstance(row[0], str)
        )
    )
    sensitive_subject_ids = (
        *memory_ids,
        *fully_erased_receipt_source_ids,
        *erased_source_ids_tuple,
        *experience_ids,
        *digest_ids,
        *proposal_ids,
    )
    memory_event_ids = tuple(
        sorted(
            row[0]
            for row in _rows_for_values(
                connection,
                "memory_events",
                "event_id",
                "subject_id",
                sensitive_subject_ids,
            )
            if isinstance(row[0], str)
        )
    )
    capsule_rows = connection.execute(
        f"""
        SELECT dictionary.primary_capsule_id, dictionary.memory_id,
               version.content
        FROM knowledge_dictionary AS dictionary
        JOIN canonical_memory_versions AS version
          ON version.memory_id = dictionary.memory_id
         AND version.version = dictionary.current_version
        WHERE dictionary.memory_id IN ({placeholders})
        ORDER BY dictionary.primary_capsule_id, dictionary.memory_id
        """,
        memory_ids,
    ).fetchall()
    capsule_impacts_by_id: dict[str, dict[str, object]] = {}
    for capsule_id, affected_memory_id, body in capsule_rows:
        if not all(isinstance(value, str) for value in (capsule_id, affected_memory_id, body)):
            raise IntegrityError("permanent erasure capsule closure is invalid")
        impact_entry = capsule_impacts_by_id.setdefault(
            capsule_id,
            {
                "capsule_id": capsule_id,
                "affected_memory_ids": [],
                "removed_body_bytes": 0,
            },
        )
        cast(list[str], impact_entry["affected_memory_ids"]).append(affected_memory_id)
        impact_entry["removed_body_bytes"] = cast(
            int, impact_entry["removed_body_bytes"]
        ) + len(body.encode("utf-8"))
    capsule_impacts: list[dict[str, object]] = []
    for capsule_id, capsule_impact in sorted(capsule_impacts_by_id.items()):
        shared = connection.execute(
            f"""
            SELECT 1 FROM knowledge_dictionary
            WHERE primary_capsule_id = ? AND memory_id NOT IN ({placeholders})
            LIMIT 1
            """,
            (capsule_id, *memory_ids),
        ).fetchone() is not None
        capsule_impacts.append(
            {**capsule_impact, "action": "retain-shared" if shared else "delete"}
        )
    dependencies = [
        {
            "memory_id": row[0],
            "version": row[1],
            "depends_on_memory_id": row[2],
            "depends_on_version": row[3],
            "relationship": row[4],
        }
        for row in dependency_rows
    ]
    versions = [
        {"memory_id": row[0], "version": row[1], "state": row[2]}
        for row in version_rows
    ]
    token_material = {
        "target_fingerprint": _deletion_fingerprint(memory_id),
        "memory_versions": versions,
        "source_impacts": source_impacts,
        "fully_erased_receipt_source_ids": fully_erased_receipt_source_ids,
        "dependency_edges": dependencies,
        "legacy_source_impacts": legacy_source_impacts,
        "experience_ids": experience_ids,
        "digest_ids": digest_ids,
        "proposal_ids": proposal_ids,
        "review_batch_ids": batch_ids,
        "review_group_ids": group_ids,
        "recall_ids": recall_ids,
        "relation_impacts": relation_impacts,
        "lifecycle_event_ids": lifecycle_event_ids,
        "memory_event_ids": memory_event_ids,
        "capsule_impacts": capsule_impacts,
    }
    confirmation_token = "erase_" + hashlib.sha256(
        json.dumps(
            token_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "disposition": "preview",
        "scope": "transitive-memory-impact-closure",
        "memory_ids": list(memory_ids),
        "memory_versions": versions,
        "derivative_memory_ids": list(derivatives),
        "source_impacts": source_impacts,
        "fully_erased_receipt_source_ids": list(fully_erased_receipt_source_ids),
        "legacy_source_impacts": legacy_source_impacts,
        "experience_ids": list(experience_ids),
        "digest_ids": list(digest_ids),
        "proposal_ids": list(proposal_ids),
        "review_batch_ids": list(batch_ids),
        "review_group_ids": list(group_ids),
        "recall_ids": list(recall_ids),
        "relation_impacts": relation_impacts,
        "lifecycle_event_ids": list(lifecycle_event_ids),
        "memory_event_ids": list(memory_event_ids),
        "capsule_impacts": capsule_impacts,
        "dependency_edges": dependencies,
        "backup_impact": {
            "future_backups": "excluded",
            "existing_backups": "owner-must-rotate-or-delete",
        },
        "confirmation_token": confirmation_token,
        "requires_confirmation": True,
    }


def _stage_erasure(
    database_path: Path,
    *,
    impact: dict[str, object],
    entrance: str,
) -> tuple[bytes, dict[str, object], tuple[str, ...]]:
    temporary_path: Path | None = None
    deleted_at = datetime.now(timezone.utc).isoformat()
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".memory-erase.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            memory_ids_value = impact.get("memory_ids")
            source_impacts_value = impact.get("source_impacts")
            legacy_source_impacts_value = impact.get("legacy_source_impacts")
            capsule_impacts_value = impact.get("capsule_impacts")
            if (
                not isinstance(memory_ids_value, list)
                or not memory_ids_value
                or not all(isinstance(value, str) for value in memory_ids_value)
                or not isinstance(source_impacts_value, list)
                or not isinstance(legacy_source_impacts_value, list)
                or not isinstance(capsule_impacts_value, list)
            ):
                raise IntegrityError("permanent erasure impact is invalid")
            memory_ids = tuple(cast(list[str], memory_ids_value))
            proposal_ids = _impact_string_tuple(impact, "proposal_ids")
            batch_ids = _impact_string_tuple(impact, "review_batch_ids")
            group_ids = _impact_string_tuple(impact, "review_group_ids")
            experience_ids = _impact_string_tuple(impact, "experience_ids")
            digest_ids = _impact_string_tuple(impact, "digest_ids")
            memory_event_ids = _impact_string_tuple(impact, "memory_event_ids")
            fully_erased_receipt_source_ids = _impact_string_tuple(
                impact, "fully_erased_receipt_source_ids"
            )
            target_memory_id = memory_ids[0]
            current_impact = _erasure_impact_for_connection(connection, target_memory_id)
            if current_impact["confirmation_token"] != impact["confirmation_token"]:
                raise UserInputError("permanent erasure impact closure changed")
            exclusive_capsule_ids: list[str] = []
            shared_capsule_updates: list[tuple[int, int, str]] = []
            for capsule_impact in capsule_impacts_value:
                if not isinstance(capsule_impact, dict):
                    raise IntegrityError("permanent erasure capsule impact is invalid")
                capsule_id = capsule_impact.get("capsule_id")
                action = capsule_impact.get("action")
                removed_body_bytes = capsule_impact.get("removed_body_bytes")
                affected_ids = capsule_impact.get("affected_memory_ids")
                if (
                    not isinstance(capsule_id, str)
                    or action not in ("delete", "retain-shared")
                    or not isinstance(removed_body_bytes, int)
                    or not isinstance(affected_ids, list)
                    or not all(isinstance(value, str) for value in affected_ids)
                ):
                    raise IntegrityError("permanent erasure capsule impact is invalid")
                if action == "delete":
                    exclusive_capsule_ids.append(capsule_id)
                else:
                    shared_capsule_updates.append(
                        (removed_body_bytes, len(affected_ids), capsule_id)
                    )
            capsule_ids = tuple(exclusive_capsule_ids)
            partition_ids = tuple(
                row[0]
                for row in _rows_for_values(
                    connection,
                    "capsule_partitions",
                    "partition_id",
                    "capsule_id",
                    capsule_ids,
                )
                if isinstance(row[0], str)
            )
            _delete_review_proposals(connection, proposal_ids)
            for batch_id in batch_ids:
                if connection.execute(
                    "SELECT 1 FROM review_batch_items WHERE batch_id = ? LIMIT 1",
                    (batch_id,),
                ).fetchone() is None:
                    connection.execute(
                        "DELETE FROM review_batches WHERE batch_id = ?", (batch_id,)
                    )
            for group_id in group_ids:
                if connection.execute(
                    "SELECT 1 FROM review_group_members WHERE group_id = ? LIMIT 1",
                    (group_id,),
                ).fetchone() is None:
                    connection.execute(
                        "DELETE FROM review_groups WHERE group_id = ?", (group_id,)
                    )
            _delete_for_values(connection, "integration_reviews", "canonical_memory_id", memory_ids)
            _delete_for_values(connection, "source_memory_proposal_details", "planned_memory_id", memory_ids)
            for table in (
                "integration_proposal_buffered",
                "integration_proposal_related",
                "integration_proposal_sources",
            ):
                _delete_for_values(connection, table, "proposal_id", proposal_ids)
            _delete_for_values(connection, "integration_proposals", "proposal_id", proposal_ids)
            _delete_for_values(connection, "recall_evidence_expansions", "memory_id", memory_ids)
            _delete_for_values(connection, "recall_event_items", "memory_id", memory_ids)
            _delete_for_values(connection, "canonical_memory_lifecycle_events", "memory_id", memory_ids)
            _delete_for_values(connection, "memory_events", "event_id", memory_event_ids)
            _delete_for_values(connection, "canonical_memory_dependencies", "memory_id", memory_ids)
            _delete_for_values(
                connection,
                "canonical_memory_dependencies",
                "depends_on_memory_id",
                memory_ids,
            )
            for table in ("canonical_memory_relations",):
                _delete_for_values(connection, table, "memory_id", memory_ids)
                _delete_for_values(connection, table, "related_memory_id", memory_ids)
            _delete_for_values(connection, "canonical_memory_conflicts", "first_memory_id", memory_ids)
            _delete_for_values(connection, "canonical_memory_conflicts", "second_memory_id", memory_ids)
            connection.executemany(
                "DELETE FROM canonical_memory_fts WHERE memory_id = ?",
                ((value,) for value in memory_ids),
            )
            for table in (
                "canonical_memory_review_provenance",
                "canonical_memory_version_evidence",
                "canonical_memory_version_sources",
                "canonical_memory_sources",
                "legacy_knowledge_metadata",
                "knowledge_dictionary",
            ):
                _delete_for_values(connection, table, "memory_id", memory_ids)
            _delete_for_values(connection, "canonical_memory_versions", "memory_id", memory_ids)
            _delete_for_values(connection, "canonical_memories", "memory_id", memory_ids)
            _delete_for_values(connection, "capsule_partitions", "capsule_id", capsule_ids)
            _delete_for_values(connection, "knowledge_capsules", "capsule_id", capsule_ids)
            connection.executemany(
                """
                UPDATE knowledge_capsules
                SET body_bytes = body_bytes - ?,
                    memory_record_count = memory_record_count - ?,
                    structural_version = structural_version + 1,
                    updated_at = ?
                WHERE capsule_id = ?
                """,
                (
                    (removed_bytes, removed_count, deleted_at, capsule_id)
                    for removed_bytes, removed_count, capsule_id
                    in shared_capsule_updates
                ),
            )
            for partition_id in partition_ids:
                if connection.execute(
                    "SELECT 1 FROM capsule_partitions WHERE partition_id = ? LIMIT 1",
                    (partition_id,),
                ).fetchone() is None:
                    connection.execute(
                        "DELETE FROM knowledge_partitions WHERE partition_id = ?",
                        (partition_id,),
                    )
            erased_source_ids: set[str] = set()
            for source_impact in source_impacts_value:
                if not isinstance(source_impact, dict):
                    raise IntegrityError("permanent erasure source impact is invalid")
                source_id = source_impact.get("source_id")
                source_version = source_impact.get("source_version")
                action = source_impact.get("action")
                if (
                    not isinstance(source_id, str)
                    or not isinstance(source_version, int)
                    or not isinstance(action, str)
                ):
                    raise IntegrityError("permanent erasure source impact is invalid")
                if action == "erase-receipt":
                    connection.execute(
                        "DELETE FROM evidence_source_versions WHERE source_id = ? AND version = ?",
                        (source_id, source_version),
                    )
                    erased_source_ids.add(source_id)
            deleted_receipt_source_ids: set[str] = set()
            for source_id in erased_source_ids:
                if connection.execute(
                    "SELECT 1 FROM evidence_source_versions WHERE source_id = ? LIMIT 1",
                    (source_id,),
                ).fetchone() is None:
                    connection.execute(
                        "DELETE FROM evidence_sources WHERE source_id = ?",
                        (source_id,),
                    )
                    deleted_receipt_source_ids.add(source_id)
            if deleted_receipt_source_ids != set(fully_erased_receipt_source_ids):
                raise IntegrityError("permanent erasure source identity closure changed")
            erased_legacy_source_ids: set[str] = set()
            object_references: list[str] = []
            for source_impact in legacy_source_impacts_value:
                if not isinstance(source_impact, dict):
                    raise IntegrityError("permanent erasure raw source impact is invalid")
                source_id = source_impact.get("source_id")
                object_reference = source_impact.get("object_reference")
                action = source_impact.get("action")
                if (
                    not isinstance(source_id, str)
                    or not isinstance(object_reference, str)
                    or action not in ("erase-object", "retain-shared")
                ):
                    raise IntegrityError("permanent erasure raw source impact is invalid")
                if action == "erase-object":
                    erased_legacy_source_ids.add(source_id)
                    object_references.append(object_reference)
            _delete_for_values(connection, "buffered_digests", "digest_id", digest_ids)
            _delete_for_values(connection, "experiences", "experience_id", experience_ids)
            legacy_source_ids = tuple(sorted(erased_legacy_source_ids))
            _delete_for_values(
                connection, "legacy_source_metadata", "source_id", legacy_source_ids
            )
            _delete_for_values(connection, "source_objects", "source_id", legacy_source_ids)
            marker_ids: list[str] = []
            for erased_memory_id in memory_ids:
                fingerprint = _deletion_fingerprint(erased_memory_id)
                marker_id = "del_" + hashlib.sha256(
                    f"canonical-memory:{fingerprint}".encode("utf-8")
                ).hexdigest()
                marker_ids.append(marker_id)
                connection.execute(
                    """
                    INSERT INTO deletion_markers
                        (marker_id, subject_kind, subject_fingerprint,
                         deleted_at, backup_exclusion_after)
                    VALUES (?, 'canonical-memory', ?, ?, ?)
                    """,
                    (marker_id, fingerprint, deleted_at, deleted_at),
                )
            erased_all_source_ids = tuple(
                sorted(deleted_receipt_source_ids | erased_legacy_source_ids)
            )
            for erased_source_id in erased_all_source_ids:
                fingerprint = _deletion_fingerprint(erased_source_id)
                source_marker_id = "del_" + hashlib.sha256(
                    f"source:{fingerprint}".encode("utf-8")
                ).hexdigest()
                marker_ids.append(source_marker_id)
                connection.execute(
                    """
                    INSERT INTO deletion_markers
                        (marker_id, subject_kind, subject_fingerprint,
                         deleted_at, backup_exclusion_after)
                    VALUES (?, 'source', ?, ?, ?)
                    """,
                    (source_marker_id, fingerprint, deleted_at, deleted_at),
                )
            event_id = f"aud_{uuid.uuid4().hex}"
            result_hash = _stable_hash(
                {
                    "target_marker": marker_ids[0],
                    "erased_markers": marker_ids,
                    "deleted_at": deleted_at,
                }
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, 'memory.erased', ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (event_id, deleted_at, marker_ids[0], entrance, result_hash),
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("permanent erasure left dangling canonical references")
        return temporary_path.read_bytes(), {
            "disposition": "erased",
            "scope": "transitive-memory-impact-closure",
            "erased_memory_ids": list(memory_ids),
            "tombstone_ids": marker_ids,
            "deleted_at": deleted_at,
            "backup_impact": impact["backup_impact"],
            "audit_event": {
                "event_id": event_id,
                "event_type": "memory.erased",
                "occurred_at": deleted_at,
                "entrance": entrance,
                "result_hash": result_hash,
            },
        }, tuple(sorted(object_references))
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot permanently erase memory") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _erasure_marker(database_path: Path, memory_id: str) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """
                SELECT marker_id, subject_fingerprint, deleted_at,
                       backup_exclusion_after
                FROM deletion_markers
                WHERE subject_kind = 'canonical-memory'
                  AND subject_fingerprint = ?
                """,
                (_deletion_fingerprint(memory_id),),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect permanent erasure tombstone") from error
    if row is None:
        return None
    if not all(isinstance(value, str) for value in row):
        raise IntegrityError("permanent erasure tombstone is invalid")
    return {
        "disposition": "already-erased",
        "tombstone_id": row[0],
        "subject_fingerprint": row[1],
        "deleted_at": row[2],
        "backup_exclusion_after": row[3],
    }


def _raise_if_erased(database_path: Path, memory_id: str) -> None:
    if _erasure_marker(database_path, memory_id) is not None:
        raise UserInputError(
            "canonical memory was permanently erased and cannot be silently restored"
        )


def _erasure_proposal_ids(
    connection: sqlite3.Connection,
    memory_ids: tuple[str, ...],
) -> tuple[str, ...]:
    placeholders = ", ".join("?" for _ in memory_ids)
    rows = connection.execute(
        f"""
        SELECT proposal_id FROM integration_reviews
        WHERE canonical_memory_id IN ({placeholders})
        UNION
        SELECT proposal_id FROM source_memory_proposal_details
        WHERE planned_memory_id IN ({placeholders})
        UNION
        SELECT proposal_id FROM canonical_memory_review_provenance
        WHERE memory_id IN ({placeholders})
        UNION
        SELECT proposal_id FROM review_materializations
        WHERE artifact_kind = 'canonical-memory'
          AND artifact_id IN ({placeholders})
        ORDER BY proposal_id
        """,
        (*memory_ids, *memory_ids, *memory_ids, *memory_ids),
    ).fetchall()
    return tuple(row[0] for row in rows if isinstance(row[0], str))


def _delete_review_proposals(
    connection: sqlite3.Connection,
    proposal_ids: tuple[str, ...],
) -> None:
    if not proposal_ids:
        return
    for column in ("first_proposal_id", "second_proposal_id"):
        _delete_for_values(connection, "review_proposal_relations", column, proposal_ids)
    for table in (
        "review_proposal_submissions",
        "review_group_members",
        "review_batch_items",
        "review_materializations",
        "canonical_memory_review_provenance",
        "human_archives",
        "research_threads",
        "review_expirations",
        "review_proposal_recurrences",
    ):
        _delete_for_values(connection, table, "proposal_id", proposal_ids)
    _delete_for_values(connection, "review_proposals", "proposal_id", proposal_ids)


def _rows_for_values(
    connection: sqlite3.Connection,
    table: str,
    result_column: str,
    filter_column: str,
    values: tuple[str, ...],
) -> list[tuple[object, ...]]:
    if not values:
        return []
    placeholders = ", ".join("?" for _ in values)
    return connection.execute(
        f"SELECT {result_column} FROM {table} WHERE {filter_column} IN ({placeholders})",
        values,
    ).fetchall()


def _impact_string_tuple(
    impact: dict[str, object],
    key: str,
) -> tuple[str, ...]:
    value = impact.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise IntegrityError(f"permanent erasure {key} impact is invalid")
    return tuple(cast(list[str], value))


def _delete_for_values(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: tuple[str, ...],
) -> None:
    if not values:
        return
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
        values,
    )


def _deletion_fingerprint(subject_id: str) -> str:
    return "sha256:" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _dependencies_complete(
    connection: sqlite3.Connection,
    memory_id: str,
    version: int,
) -> bool:
    missing_evidence = connection.execute(
        """
        SELECT 1
        FROM canonical_memory_version_evidence AS evidence
        LEFT JOIN evidence_source_versions AS source
          ON source.source_id = evidence.source_id
         AND source.version = evidence.source_version
        WHERE evidence.memory_id = ? AND evidence.version = ?
          AND source.source_id IS NULL
        LIMIT 1
        """,
        (memory_id, version),
    ).fetchone()
    missing_memory = connection.execute(
        """
        SELECT 1
        FROM canonical_memory_dependencies AS dependency
        LEFT JOIN canonical_memory_versions AS target
          ON target.memory_id = dependency.depends_on_memory_id
         AND target.version = dependency.depends_on_version
        WHERE dependency.memory_id = ? AND dependency.version = ?
          AND target.memory_id IS NULL
        LIMIT 1
        """,
        (memory_id, version),
    ).fetchone()
    return missing_evidence is None and missing_memory is None


def _revision_result(
    *,
    connection_path: Path,
    memory_id: str,
    previous_version: int,
    current_version: int,
    reason: str,
    event_id: str,
    occurred_at: str,
    entrance: str,
    result_hash: str,
) -> dict[str, object]:
    try:
        with closing(sqlite3.connect(connection_path)) as connection:
            rows = connection.execute(
                """
                SELECT version, content FROM canonical_memory_versions
                WHERE memory_id = ? AND version IN (?, ?)
                ORDER BY version
                """,
                (memory_id, previous_version, current_version),
            ).fetchall()
            state_row = connection.execute(
                "SELECT state FROM canonical_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            sources = {
                version: _source_ids(connection, memory_id, version)
                for version in (previous_version, current_version)
            }
    except sqlite3.Error as error:
        raise IntegrityError("cannot read canonical memory revision") from error
    if (
        len(rows) != 2
        or state_row is None
        or state_row[0] != "current"
        or not all(isinstance(row[0], int) and isinstance(row[1], str) for row in rows)
    ):
        raise IntegrityError("canonical memory revision is incomplete")
    bodies = {cast(int, row[0]): cast(str, row[1]) for row in rows}
    return {
        "memory_id": memory_id,
        "state": "current",
        "previous_version": {
            "version": previous_version,
            "body": bodies[previous_version],
            "source_ids": list(sources[previous_version]),
            "supersession_reason": reason,
        },
        "current_version": {
            "version": current_version,
            "body": bodies[current_version],
            "source_ids": list(sources[current_version]),
        },
        "audit_event": {
            "event_id": event_id,
            "event_type": "memory.revised",
            "occurred_at": occurred_at,
            "before_version": previous_version,
            "after_version": current_version,
            "entrance": entrance,
            "result_hash": result_hash,
        },
    }


def _source_ids(
    connection: sqlite3.Connection,
    memory_id: str,
    version: int,
) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT source_id
            FROM canonical_memory_version_evidence
            WHERE memory_id = ? AND version = ?
            UNION
            SELECT DISTINCT source_id
            FROM canonical_memory_version_sources
            WHERE memory_id = ? AND version = ?
            ORDER BY source_id
            """,
            (memory_id, version, memory_id, version),
        ).fetchall()
        if isinstance(row[0], str)
    )


def _transition_result(
    *,
    memory_id: str,
    version: int,
    reason: str,
    event_id: str,
    occurred_at: str,
    entrance: str,
    result_hash: str,
) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "version": version,
        "from_state": "current",
        "to_state": "historical-trusted",
        "reason": reason,
        "audit_event": {
            "event_id": event_id,
            "event_type": "memory.historicized",
            "occurred_at": occurred_at,
            "before_version": version,
            "after_version": version,
            "entrance": entrance,
            "result_hash": result_hash,
        },
    }


def _text(label: str, value: str, *, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise UserInputError(f"{label} must not be blank")
    if len(normalized) > maximum:
        raise UserInputError(f"{label} must not exceed {maximum} characters")
    return normalized


def _body(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserInputError("canonical memory body must not be blank")
    if len(normalized.encode("utf-8")) > 8 * 1024:
        raise UserInputError("canonical memory body exceeds the 8192-byte hard limit")
    return normalized


def _identifier(label: str, value: str, prefix: str) -> str:
    normalized = value.strip()
    if re.fullmatch(rf"{re.escape(prefix)}[0-9a-f]{{32}}", normalized) is None:
        raise UserInputError(f"{label} is invalid")
    return normalized


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
