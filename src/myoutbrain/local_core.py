from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
from typing import Literal
import uuid

from myoutbrain.core_types import (
    ConfigurationConflict,
    IntegrityError,
    Sensitivity,
    UserInputError,
)
from myoutbrain.persistence import (
    atomic_commit,
    event_journal_change,
    hold_writer_lock_for_acceptance_test,
    recover_transactions,
    writer_lock,
)


MEMORY_SCHEMA_VERSION = 1
MEMORY_DATABASE = "store/memory.sqlite3"


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE source_objects (
    source_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    object_reference TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE experiences (
    experience_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_objects(source_id),
    occurred_at TEXT NOT NULL,
    entrance TEXT NOT NULL,
    task TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    visible_context TEXT NOT NULL,
    context_gaps_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE buffered_digests (
    digest_id TEXT PRIMARY KEY,
    experience_id TEXT NOT NULL UNIQUE REFERENCES experiences(experience_id),
    content TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'buffered'),
    created_at TEXT NOT NULL
);

CREATE TABLE canonical_memories (
    memory_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE memory_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


MemoryDisposition = Literal["buffered", "duplicate"]


@dataclass(frozen=True)
class ExperienceMetadata:
    occurred_at: str
    entrance: str
    task: str
    sensitivity: Sensitivity
    visible_context: str
    context_gaps: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        occurred_at: str,
        entrance: str,
        task: str,
        sensitivity: Sensitivity,
        visible_context: str,
        context_gaps: tuple[str, ...],
    ) -> ExperienceMetadata:
        normalized_gaps = tuple(
            _required_text("context gap", gap) for gap in context_gaps
        )
        if not normalized_gaps:
            raise UserInputError("at least one explicit context gap is required")
        if sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError(f"invalid sensitivity: {sensitivity}")
        return cls(
            occurred_at=_validated_time(occurred_at),
            entrance=_required_text("entrance", entrance),
            task=_required_text("task", task),
            sensitivity=sensitivity,
            visible_context=_required_text("visible context", visible_context),
            context_gaps=normalized_gaps,
        )

    def identity_data(self, source_id: str) -> dict[str, object]:
        return {
            "source_id": source_id,
            "occurred_at": self.occurred_at,
            "entrance": self.entrance,
            "task": self.task,
            "sensitivity": self.sensitivity,
            "visible_context": self.visible_context,
            "context_gaps": self.context_gaps,
        }


@dataclass(frozen=True)
class BufferedMemoryReceipt:
    source_id: str
    experience_id: str
    digest_id: str
    digest: str
    disposition: MemoryDisposition
    metadata: ExperienceMetadata

    def to_data(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "experience_id": self.experience_id,
            "digest_id": self.digest_id,
            "digest": self.digest,
            "disposition": self.disposition,
            "state": "buffered",
            "canonical_memory_id": None,
            "occurred_at": self.metadata.occurred_at,
            "entrance": self.metadata.entrance,
            "task": self.metadata.task,
            "sensitivity": self.metadata.sensitivity,
            "visible_context": self.metadata.visible_context,
            "context_gaps": list(self.metadata.context_gaps),
        }


class LocalMemoryCore:
    """Own the durable private-instance memory state."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def initialize(self) -> None:
        configuration = self._root / "myoutbrain.toml"
        if not configuration.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        database_path = self._root / MEMORY_DATABASE
        with writer_lock(self._root):
            recover_transactions(self._root)
            if database_path.exists():
                self._validate_database(database_path)
                return
            database_content = self._new_database_content(database_path.parent)
            atomic_commit(self._root, [(database_path, database_content)])

    def capture_experience(
        self,
        conversation_path: Path,
        *,
        occurred_at: str,
        entrance: str,
        task: str,
        sensitivity: Sensitivity,
        visible_context: str,
        context_gaps: tuple[str, ...],
    ) -> BufferedMemoryReceipt:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        body = _read_conversation(conversation_path)
        metadata = ExperienceMetadata.create(
            occurred_at=occurred_at,
            entrance=entrance,
            task=task,
            sensitivity=sensitivity,
            visible_context=visible_context,
            context_gaps=context_gaps,
        )

        source_digest = hashlib.sha256(body).hexdigest()
        source_id = f"src_{source_digest}"
        identity_document = json.dumps(
            metadata.identity_data(source_id),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        experience_id = f"exp_{hashlib.sha256(identity_document).hexdigest()}"
        object_path = (
            self._root
            / "store"
            / "objects"
            / "sha256"
            / source_digest[:2]
            / source_digest[2:4]
            / source_digest
        )
        object_reference = object_path.relative_to(
            self._root / "store" / "objects"
        ).as_posix()

        with writer_lock(self._root):
            hold_writer_lock_for_acceptance_test()
            recover_transactions(self._root)
            self._validate_database(database_path)
            _validate_content_object(object_path, body, source_digest)
            duplicate = self._duplicate_receipt(
                database_path,
                experience_id=experience_id,
                source_id=source_id,
                metadata=metadata,
            )
            if duplicate is not None:
                return duplicate

            digest = _compact_digest(metadata, source_id=source_id)
            digest_fingerprint = hashlib.sha256(digest.encode("utf-8")).hexdigest()
            digest_id = f"mem_{hashlib.sha256(f'{experience_id}:{digest_fingerprint}'.encode()).hexdigest()}"
            created_at = datetime.now(timezone.utc).isoformat()
            event_id = f"evt_{uuid.uuid4().hex}"
            payload = {
                "source_id": source_id,
                "experience_id": experience_id,
                "digest_id": digest_id,
                "entrance": metadata.entrance,
                "task": metadata.task,
                "sensitivity": metadata.sensitivity,
                "state": "buffered",
            }
            staged_database = self._database_with_capture(
                database_path,
                source_id=source_id,
                content_hash=f"sha256:{source_digest}",
                object_reference=object_reference,
                experience_id=experience_id,
                metadata=metadata,
                digest_id=digest_id,
                digest=digest,
                digest_fingerprint=f"sha256:{digest_fingerprint}",
                event_id=event_id,
                event_payload=payload,
                created_at=created_at,
            )
            event = {
                "id": event_id,
                "type": "memory.buffered",
                "occurred_at": created_at,
                **payload,
            }
            changes: list[tuple[Path, bytes]] = []
            if not object_path.exists():
                changes.append((object_path, body))
            changes.extend(
                [
                    (database_path, staged_database),
                    event_journal_change(self._root, event),
                ]
            )
            atomic_commit(
                self._root,
                changes,
                fault_injections={0: "remember-after-first-replace"},
            )
        return BufferedMemoryReceipt(
            source_id=source_id,
            experience_id=experience_id,
            digest_id=digest_id,
            digest=digest,
            disposition="buffered",
            metadata=metadata,
        )

    @staticmethod
    def _duplicate_receipt(
        database_path: Path,
        *,
        experience_id: str,
        source_id: str,
        metadata: ExperienceMetadata,
    ) -> BufferedMemoryReceipt | None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT digest_id, content
                    FROM buffered_digests
                    WHERE experience_id = ?
                    """,
                    (experience_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot query the local memory database") from error
        if row is None:
            return None
        digest_id, digest = row
        if not isinstance(digest_id, str) or not isinstance(digest, str):
            raise IntegrityError("buffered memory has invalid persisted fields")
        return BufferedMemoryReceipt(
            source_id=source_id,
            experience_id=experience_id,
            digest_id=digest_id,
            digest=digest,
            disposition="duplicate",
            metadata=metadata,
        )

    @staticmethod
    def _database_with_capture(
        database_path: Path,
        *,
        source_id: str,
        content_hash: str,
        object_reference: str,
        experience_id: str,
        metadata: ExperienceMetadata,
        digest_id: str,
        digest: str,
        digest_fingerprint: str,
        event_id: str,
        event_payload: dict[str, str],
        created_at: str,
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".memory-stage.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO source_objects
                        (source_id, content_hash, object_reference, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_id, content_hash, object_reference, created_at),
                )
                connection.execute(
                    """
                    INSERT INTO experiences
                        (experience_id, source_id, occurred_at, entrance, task,
                         sensitivity, visible_context, context_gaps_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        experience_id,
                        source_id,
                        metadata.occurred_at,
                        metadata.entrance,
                        metadata.task,
                        metadata.sensitivity,
                        metadata.visible_context,
                        json.dumps(metadata.context_gaps, ensure_ascii=False),
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO buffered_digests
                        (digest_id, experience_id, content, fingerprint, state, created_at)
                    VALUES (?, ?, ?, ?, 'buffered', ?)
                    """,
                    (
                        digest_id,
                        experience_id,
                        digest,
                        digest_fingerprint,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_events
                        (event_id, event_type, occurred_at, subject_id, payload_json)
                    VALUES (?, 'memory.buffered', ?, ?, ?)
                    """,
                    (
                        event_id,
                        created_at,
                        digest_id,
                        json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage buffered memory") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _new_database_content(parent: Path) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=parent,
                prefix=".memory.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
                connection.commit()
            return temporary_path.read_bytes()
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot initialize the local memory database") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_database(database_path: Path) -> None:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                version_row = connection.execute("PRAGMA user_version").fetchone()
                integrity_row = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as error:
            raise IntegrityError(
                f"cannot read local memory database: {database_path}"
            ) from error
        version = version_row[0] if version_row is not None else None
        if version != MEMORY_SCHEMA_VERSION:
            raise ConfigurationConflict(
                f"unsupported memory schema version {version}: {database_path}"
            )
        if integrity_row != ("ok",):
            raise IntegrityError(f"local memory database is corrupt: {database_path}")


def _read_conversation(conversation_path: Path) -> bytes:
    if not conversation_path.is_file():
        raise UserInputError(f"conversation does not exist: {conversation_path}")
    try:
        body = conversation_path.read_bytes()
        text = body.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UserInputError(
            f"conversation is not readable UTF-8: {conversation_path}"
        ) from error
    if not text.strip():
        raise UserInputError("conversation must not be blank")
    return body


def _required_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserInputError(f"{name} must not be blank")
    return normalized


def _validated_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise UserInputError("occurred-at must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UserInputError("occurred-at must include a UTC offset")
    return parsed.isoformat()


def _validate_content_object(path: Path, body: bytes, digest: str) -> None:
    if not path.exists():
        return
    try:
        stored = path.read_bytes()
    except OSError as error:
        raise IntegrityError(f"cannot read source object: {path}") from error
    if hashlib.sha256(stored).hexdigest() != digest or stored != body:
        raise IntegrityError(f"source object does not match its content address: {path}")


def _compact_digest(metadata: ExperienceMetadata, *, source_id: str) -> str:
    gaps = "; ".join(metadata.context_gaps)
    return (
        f"{_bounded(metadata.task, 80)} via {_bounded(metadata.entrance, 40)}; "
        f"visible: {_bounded(metadata.visible_context, 100)}; "
        f"gaps: {_bounded(gaps, 120)}; "
        f"sensitivity: {metadata.sensitivity}; "
        f"[evidence: {source_id}]"
    )


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}…"
