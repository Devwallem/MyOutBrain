from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import tempfile
import tomllib
from typing import Literal

from myoutbrain.core_types import ConfigurationConflict, IntegrityError
from myoutbrain.library import SourceRecord
from myoutbrain.local_core import MEMORY_DATABASE, MEMORY_SCHEMA_VERSION, LocalMemoryCore
from myoutbrain.persistence import atomic_commit, recover_transactions, writer_lock
from myoutbrain.reconstruction import ReconstructionError, RuntimeReconstructor
from myoutbrain.vault import KnowledgeNoteSnapshot, VaultIntegrityError, scan_knowledge_notes


MIGRATION_ID = "v1-permanent-knowledge"
MigrationDisposition = Literal["migrated", "already-complete"]


@dataclass(frozen=True)
class MigrationSummary:
    status: Literal["not-started", "complete"]
    source_schema_version: int | None = None
    source_fingerprint: str | None = None
    source_count: int = 0
    insight_count: int = 0
    cognition_count: int = 0
    event_count: int = 0
    completed_at: str | None = None
    disposition: MigrationDisposition | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "migration_id": MIGRATION_ID,
            "status": self.status,
            "source_schema_version": self.source_schema_version,
            "source_fingerprint": self.source_fingerprint,
            "source_count": self.source_count,
            "insight_count": self.insight_count,
            "cognition_count": self.cognition_count,
            "event_count": self.event_count,
            "completed_at": self.completed_at,
        }
        if self.disposition is not None:
            data["disposition"] = self.disposition
        return data


@dataclass(frozen=True)
class _LegacySource:
    source_id: str
    content_hash: str
    object_reference: str
    created_at: str
    sensitivity: str
    origins_json: str
    record_path: Path
    object_path: Path


@dataclass(frozen=True)
class _LegacyEvent:
    event_id: str
    event_type: str
    occurred_at: str
    payload_json: str


@dataclass(frozen=True)
class _MigrationInput:
    sources: tuple[_LegacySource, ...]
    notes: tuple[KnowledgeNoteSnapshot, ...]
    events: tuple[_LegacyEvent, ...]
    fingerprint: str


class V1PermanentKnowledgeMigrator:
    """Copies validated V1 permanent knowledge into the canonical local core."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def migrate(self) -> MigrationSummary:
        source_schema_version = self._validated_source_schema_version()
        if source_schema_version != 1:
            raise ConfigurationConflict(
                f"unsupported V1 migration source schema version {source_schema_version}"
            )

        LocalMemoryCore(self._root).initialize()
        database_path = self._root / MEMORY_DATABASE

        with writer_lock(self._root):
            recover_transactions(self._root)
            existing = self._read_status(database_path)
            try:
                # Validate source objects, knowledge relationships,
                # confirmation events, and journal consistency while the same
                # writer lock protects the input snapshot and final commit.
                RuntimeReconstructor(self._root).rebuild()
            except ReconstructionError as error:
                raise IntegrityError(str(error)) from error
            migration_input = self._read_input()
            if existing.status == "complete":
                if existing.source_fingerprint != migration_input.fingerprint:
                    raise ConfigurationConflict(
                        "V1 permanent knowledge changed after migration completed; "
                        "the canonical core was not modified"
                    )
                return _with_disposition(existing, "already-complete")

            completed_at = datetime.now(timezone.utc).isoformat()
            staged_database = self._stage_database(
                database_path,
                migration_input,
                root=self._root,
                source_schema_version=source_schema_version,
                completed_at=completed_at,
            )
            atomic_commit(
                self._root,
                [(database_path, staged_database)],
                fault_injections={0: "legacy-migration-after-database"},
            )

        return MigrationSummary(
            status="complete",
            source_schema_version=source_schema_version,
            source_fingerprint=migration_input.fingerprint,
            source_count=len(migration_input.sources),
            insight_count=sum(note.kind == "insight" for note in migration_input.notes),
            cognition_count=sum(
                note.kind == "cognition" for note in migration_input.notes
            ),
            event_count=len(migration_input.events),
            completed_at=completed_at,
            disposition="migrated",
        )

    def status(self) -> MigrationSummary:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            return MigrationSummary(status="not-started")
        LocalMemoryCore(self._root).initialize()
        with writer_lock(self._root):
            recover_transactions(self._root)
            return self._read_status(database_path)

    def _validated_source_schema_version(self) -> int:
        configuration_path = self._root / "myoutbrain.toml"
        try:
            with configuration_path.open("rb") as configuration_file:
                configuration = tomllib.load(configuration_file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigurationConflict(
                f"invalid migration source configuration: {configuration_path}"
            ) from error
        version = configuration.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ConfigurationConflict("migration source has no valid schema version")
        return version

    def _read_input(self) -> _MigrationInput:
        sources = self._read_sources()
        try:
            notes = scan_knowledge_notes(self._root / "vault")
        except VaultIntegrityError as error:
            raise IntegrityError(str(error)) from error
        journal_path = self._root / "store" / "journal" / "events.jsonl"
        try:
            event_lines = tuple(
                line
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise IntegrityError(f"cannot read V1 audit journal: {journal_path}") from error
        events: list[_LegacyEvent] = []
        for line in event_lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise IntegrityError(
                    f"cannot read V1 audit journal: {journal_path}"
                ) from error
            if not isinstance(event, dict):
                raise IntegrityError(f"invalid V1 audit journal: {journal_path}")
            event_id = event.get("id")
            event_type = event.get("type")
            occurred_at = event.get("occurred_at")
            if (
                not isinstance(event_id, str)
                or not isinstance(event_type, str)
                or not isinstance(occurred_at, str)
            ):
                raise IntegrityError(f"invalid V1 audit journal: {journal_path}")
            events.append(
                _LegacyEvent(
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    payload_json=line,
                )
            )
        fingerprint = _source_fingerprint(
            self._root,
            (
                *(source.record_path for source in sources),
                *(source.object_path for source in sources),
                *(note.path for note in notes),
                journal_path,
            ),
        )
        return _MigrationInput(
            sources=sources,
            notes=notes,
            events=tuple(events),
            fingerprint=fingerprint,
        )

    def _read_sources(self) -> tuple[_LegacySource, ...]:
        sources: list[_LegacySource] = []
        for record_path in sorted((self._root / "store" / "records").glob("*.json")):
            source_id = record_path.stem
            digest = source_id.removeprefix("src_")
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise IntegrityError(f"invalid source record identity: {record_path}")
            object_reference = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
            record = SourceRecord.load(
                record_path,
                expected_source_id=source_id,
                expected_digest=digest,
                expected_object_reference=object_reference,
            )
            sources.append(
                _LegacySource(
                    source_id=source_id,
                    content_hash=record.content_hash,
                    object_reference=record.object_reference,
                    created_at=record.created_at,
                    sensitivity=record.sensitivity,
                    origins_json=json.dumps(
                        [origin.to_data() for origin in record.origins],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    record_path=record_path,
                    object_path=self._root
                    / "store"
                    / "objects"
                    / record.object_reference,
                )
            )
        return tuple(sources)

    @staticmethod
    def _read_status(database_path: Path) -> MigrationSummary:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                version_row = connection.execute("PRAGMA user_version").fetchone()
                version = version_row[0] if version_row is not None else None
                if version != MEMORY_SCHEMA_VERSION:
                    raise ConfigurationConflict(
                        f"unsupported memory schema version {version}: {database_path}"
                    )
                row = connection.execute(
                    """
                    SELECT source_schema_version, source_fingerprint,
                           source_count, insight_count, cognition_count,
                           event_count, completed_at
                    FROM legacy_migration_runs
                    WHERE migration_id = ? AND status = 'complete'
                    """,
                    (MIGRATION_ID,),
                ).fetchone()
        except sqlite3.Error as error:
            raise IntegrityError("cannot read V1 migration status") from error
        if row is None:
            return MigrationSummary(status="not-started")
        return MigrationSummary(
            status="complete",
            source_schema_version=row[0],
            source_fingerprint=row[1],
            source_count=row[2],
            insight_count=row[3],
            cognition_count=row[4],
            event_count=row[5],
            completed_at=row[6],
        )

    @staticmethod
    def _stage_database(
        database_path: Path,
        migration_input: _MigrationInput,
        *,
        root: Path,
        source_schema_version: int,
        completed_at: str,
    ) -> bytes:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=database_path.parent,
                prefix=".legacy-migration.",
                suffix=".sqlite3",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(database_path.read_bytes())
            with closing(sqlite3.connect(temporary_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
                for source in migration_input.sources:
                    existing_source = connection.execute(
                        """
                        SELECT content_hash, object_reference
                        FROM source_objects
                        WHERE source_id = ?
                        """,
                        (source.source_id,),
                    ).fetchone()
                    if existing_source is None:
                        connection.execute(
                            """
                            INSERT INTO source_objects
                                (source_id, content_hash, object_reference, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                source.source_id,
                                source.content_hash,
                                source.object_reference,
                                source.created_at,
                            ),
                        )
                    elif existing_source != (
                        source.content_hash,
                        source.object_reference,
                    ):
                        raise IntegrityError(
                            "existing canonical source contradicts V1 content address: "
                            f"{source.source_id}"
                        )
                    connection.execute(
                        """
                        INSERT INTO legacy_source_metadata
                            (source_id, sensitivity, origins_json,
                             legacy_record_path)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            source.source_id,
                            source.sensitivity,
                            source.origins_json,
                            source.record_path.resolve()
                            .relative_to(root.resolve())
                            .as_posix(),
                        ),
                    )
                for note in migration_input.notes:
                    content = _canonical_content(note)
                    state = "active" if note.state == "active" else "inactive"
                    connection.execute(
                        """
                        INSERT INTO canonical_memories
                            (memory_id, content, current_version, sensitivity,
                             state, created_at, updated_at)
                        VALUES (?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            note.knowledge_id,
                            content,
                            note.sensitivity,
                            state,
                            note.created_at,
                            note.updated_at,
                        ),
                    )
                    superseded_at = note.updated_at if state == "inactive" else None
                    supersession_reason = (
                        f"Migrated from V1 state: {note.state}"
                        if state == "inactive"
                        else None
                    )
                    connection.execute(
                        """
                        INSERT INTO canonical_memory_versions
                            (memory_id, version, content, action, change_reason,
                             created_at, superseded_at, supersession_reason)
                        VALUES (?, 1, ?, 'created', ?, ?, ?, ?)
                        """,
                        (
                            note.knowledge_id,
                            content,
                            "Migrated from V1 permanent knowledge",
                            note.created_at,
                            superseded_at,
                            supersession_reason,
                        ),
                    )
                    for source_id in note.sources:
                        connection.execute(
                            "INSERT INTO canonical_memory_sources VALUES (?, ?)",
                            (note.knowledge_id, source_id),
                        )
                        connection.execute(
                            "INSERT INTO canonical_memory_version_sources VALUES (?, 1, ?)",
                            (note.knowledge_id, source_id),
                        )
                    relations = {
                        "derived_from": note.derived_from,
                        "promoted_to": note.promoted_to,
                        "supersedes": list(note.supersedes),
                        "superseded_by": list(note.superseded_by),
                    }
                    connection.execute(
                        """
                        INSERT INTO legacy_knowledge_metadata
                            (memory_id, legacy_kind, legacy_state, authorship,
                             legacy_path, candidate_id, relations_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            note.knowledge_id,
                            note.kind,
                            note.state,
                            note.authorship,
                            note.path.resolve().relative_to(root.resolve()).as_posix(),
                            note.candidate_id,
                            json.dumps(relations, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                for event in migration_input.events:
                    connection.execute(
                        "INSERT INTO legacy_audit_events VALUES (?, ?, ?, ?)",
                        (
                            event.event_id,
                            event.event_type,
                            event.occurred_at,
                            event.payload_json,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO legacy_migration_runs
                        (migration_id, source_schema_version, source_fingerprint,
                         status, source_count, insight_count, cognition_count,
                         event_count, completed_at)
                    VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, ?)
                    """,
                    (
                        MIGRATION_ID,
                        source_schema_version,
                        migration_input.fingerprint,
                        len(migration_input.sources),
                        sum(note.kind == "insight" for note in migration_input.notes),
                        sum(note.kind == "cognition" for note in migration_input.notes),
                        len(migration_input.events),
                        completed_at,
                    ),
                )
                connection.commit()
            return temporary_path.read_bytes()
        except IntegrityError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise IntegrityError("cannot stage V1 permanent knowledge migration") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _canonical_content(note: KnowledgeNoteSnapshot) -> str:
    lines = note.body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    content_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            break
        content_lines.append(line)
    content = "\n".join(content_lines).strip()
    if not content:
        raise IntegrityError(f"V1 knowledge note has no canonical content: {note.path}")
    return content


def _with_disposition(
    summary: MigrationSummary,
    disposition: MigrationDisposition,
) -> MigrationSummary:
    return MigrationSummary(
        status=summary.status,
        source_schema_version=summary.source_schema_version,
        source_fingerprint=summary.source_fingerprint,
        source_count=summary.source_count,
        insight_count=summary.insight_count,
        cognition_count=summary.cognition_count,
        event_count=summary.event_count,
        completed_at=summary.completed_at,
        disposition=disposition,
    )


def _source_fingerprint(root: Path, paths: tuple[Path, ...]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(set(paths), key=lambda candidate: candidate.as_posix()):
        try:
            relative_path = path.resolve().relative_to(root.resolve()).as_posix()
            content = path.read_bytes()
        except (OSError, ValueError) as error:
            raise IntegrityError(f"cannot fingerprint V1 permanent input: {path}") from error
        encoded_path = relative_path.encode("utf-8")
        hasher.update(len(encoded_path).to_bytes(8, "big"))
        hasher.update(encoded_path)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return f"sha256:{hasher.hexdigest()}"
