from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tomllib
from typing import cast
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from myoutbrain.core_types import ConfigurationConflict, IntegrityError, UserInputError
from myoutbrain.local_core import MEMORY_DATABASE, MEMORY_SCHEMA_VERSION
from myoutbrain.protocol_contract import (
    SERVER_CAPABILITIES,
    SERVER_PROTOCOL_VERSION,
    load_domain_schema,
)
from myoutbrain.persistence import (
    atomic_commit,
    gc_cleanup_change,
    hold_writer_lock_for_acceptance_test,
    recover_transactions,
    writer_lock,
)
from myoutbrain.retrieval import lexical_terms


class InstanceMaintenanceService:
    """Back up and diagnose one complete private instance."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def doctor(self) -> dict[str, object]:
        return diagnose_instance(self._root)

    def repair(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        if expected_version < 0:
            raise UserInputError("expected maintenance version must be non-negative")
        key = _required_text(idempotency_key, "idempotency key", maximum=200)
        normalized_entrance = _required_text(entrance, "entrance", maximum=64)
        request_hash = _stable_hash(
            {
                "operation": "instance.doctor.repair",
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        with writer_lock(self._root):
            recover_transactions(self._root)
            report = diagnose_instance(self._root)
            if cast(list[object], report["canonical_issues"]):
                return {
                    **report,
                    "repair_requested": True,
                    "repair_blocked": True,
                    "rebuilt": [],
                }
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    existing = connection.execute(
                        """
                        SELECT request_hash, result_json FROM maintenance_writes
                        WHERE operation = 'instance.doctor.repair'
                          AND idempotency_key = ?
                        """,
                        (key,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != request_hash:
                            raise UserInputError(
                                "idempotency key was already used for another Doctor repair"
                            )
                        replay = json.loads(cast(str, existing[1]))
                        if not isinstance(replay, dict):
                            raise IntegrityError("Doctor repair receipt is invalid")
                        return cast(dict[str, object], replay)
                    version_row = connection.execute(
                        "SELECT version FROM maintenance_state WHERE singleton = 1"
                    ).fetchone()
            except sqlite3.Error as error:
                raise IntegrityError("cannot inspect Doctor repair state") from error
            actual_version = version_row[0] if version_row is not None else None
            if actual_version != expected_version:
                raise UserInputError(
                    "maintenance version does not match expected version "
                    f"{expected_version}; actual version is {actual_version}"
                )
            rebuilt = [
                "full-text-search",
                "evidence-relationship-graph",
                "tree-summary",
            ]
            new_version = expected_version + 1
            occurred_at = datetime.now(timezone.utc).isoformat()
            result: dict[str, object] = {
                "mode": "repair",
                "overall": "ok",
                "repair_requested": True,
                "repair_blocked": False,
                "rebuilt": rebuilt,
                "maintenance_version": new_version,
            }
            staged_database, evidence_graph, tree_summary = _stage_projection_repair(
                database_path,
                maintenance_version=new_version,
                idempotency_key=key,
                request_hash=request_hash,
                entrance=normalized_entrance,
                occurred_at=occurred_at,
                result=result,
            )
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    (
                        self._root / "runtime" / "indexes" / "evidence-graph.json",
                        evidence_graph,
                    ),
                    (
                        self._root / "runtime" / "indexes" / "tree-summary.json",
                        tree_summary,
                    ),
                ],
            )
        return result

    def create_backup(
        self,
        output_path: Path,
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        destination = output_path.resolve()
        if expected_version < 0:
            raise UserInputError("expected maintenance version must be non-negative")
        key = _required_text(idempotency_key, "idempotency key", maximum=200)
        normalized_entrance = _required_text(entrance, "entrance", maximum=64)
        if destination == self._root or destination.is_relative_to(self._root):
            raise UserInputError("cold backup output must be outside the private instance")
        destination.parent.mkdir(parents=True, exist_ok=True)
        request_hash = _stable_hash(
            {
                "operation": "backup.create",
                "output": str(destination),
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        temporary_archive: Path | None = None
        created_this_attempt = False
        receipt_committed = False
        try:
            with writer_lock(self._root):
                hold_writer_lock_for_acceptance_test()
                recover_transactions(self._root)
                database_path = self._root / MEMORY_DATABASE
                if not database_path.is_file():
                    raise ConfigurationConflict(
                        f"MyOutBrain memory core is not initialized at: {self._root}"
                    )
                replay = _maintenance_replay(
                    database_path,
                    operation="backup.create",
                    idempotency_key=key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    if not destination.is_file() or hashlib.sha256(
                        destination.read_bytes()
                    ).hexdigest() != replay.get("sha256"):
                        raise IntegrityError("completed cold backup no longer matches its receipt")
                    return replay
                if destination.exists():
                    raise UserInputError(f"cold backup output already exists: {destination}")
                actual_version = _maintenance_version(database_path)
                if actual_version != expected_version:
                    raise UserInputError(
                        "maintenance version does not match expected version "
                        f"{expected_version}; actual version is {actual_version}"
                    )
                _checkpoint_and_close(database_path)
                report = diagnose_instance(self._root)
                if report["overall"] != "ok":
                    raise IntegrityError("cold backup requires an intact private instance")
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_archive = Path(temporary_file.name)
                with ZipFile(temporary_archive, "w", compression=ZIP_DEFLATED) as archive:
                    for path in sorted(self._root.rglob("*")):
                        if path == self._root / ".myoutbrain.lock":
                            continue
                        archive.write(path, path.relative_to(self._root).as_posix())
                        if (
                            os.environ.get("MYOUTBRAIN_FAULT_INJECTION")
                            == "backup-during-compression"
                        ):
                            raise OSError("injected backup compression failure")
                os.replace(temporary_archive, destination)
                created_this_attempt = True
                temporary_archive = None
                result: dict[str, object] = {
                    "kind": "cold-full-instance-zip",
                    "path": str(destination),
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "incremental": False,
                    "encrypted": False,
                    "retention_managed": False,
                    "maintenance_version": expected_version + 1,
                }
                staged_database = _stage_maintenance_receipt(
                    database_path,
                    operation="backup.create",
                    event_type="backup.create",
                    subject_id=str(destination),
                    maintenance_version=expected_version + 1,
                    idempotency_key=key,
                    request_hash=request_hash,
                    entrance=normalized_entrance,
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    result=result,
                )
                atomic_commit(self._root, [(database_path, staged_database)])
                receipt_committed = True
        except (BadZipFile, OSError) as error:
            raise IntegrityError("cannot create the cold full-instance backup") from error
        finally:
            if temporary_archive is not None:
                temporary_archive.unlink(missing_ok=True)
            if created_this_attempt and not receipt_committed:
                destination.unlink(missing_ok=True)
        return result

    def plan_gc(self) -> dict[str, object]:
        report = diagnose_instance(self._root)
        if cast(list[object], report["canonical_issues"]):
            raise IntegrityError("garbage collection requires intact canonical content")
        database_path = self._root / MEMORY_DATABASE
        try:
            with closing(_read_only_connection(database_path)) as connection:
                plan = _gc_plan(self._root, connection)
        except sqlite3.Error as error:
            raise IntegrityError("cannot plan orphan source-object garbage collection") from error
        return plan

    def apply_gc(
        self,
        plan_id: str,
        *,
        confirmation: str,
        confirmed_large_source_ids: tuple[str, ...],
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        normalized_plan_id = _required_text(plan_id, "GC plan id", maximum=80)
        normalized_confirmation = _required_text(
            confirmation, "GC confirmation", maximum=200
        )
        normalized_key = _required_text(idempotency_key, "idempotency key", maximum=200)
        normalized_entrance = _required_text(entrance, "entrance", maximum=64)
        if expected_version < 0:
            raise UserInputError("expected maintenance version must be non-negative")
        if len(confirmed_large_source_ids) != len(set(confirmed_large_source_ids)):
            raise UserInputError("large source confirmations must not contain duplicates")
        request_hash = _stable_hash(
            {
                "operation": "maintenance.gc_apply",
                "plan_id": normalized_plan_id,
                "confirmation": normalized_confirmation,
                "confirmed_large_source_ids": sorted(confirmed_large_source_ids),
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        database_path = self._root / MEMORY_DATABASE
        with writer_lock(self._root):
            recover_transactions(self._root)
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    existing = connection.execute(
                        """
                        SELECT request_hash, result_json FROM maintenance_writes
                        WHERE operation = 'maintenance.gc_apply'
                          AND idempotency_key = ?
                        """,
                        (normalized_key,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != request_hash:
                            raise UserInputError(
                                "idempotency key was already used for another GC apply"
                            )
                        replay = json.loads(cast(str, existing[1]))
                        if not isinstance(replay, dict):
                            raise IntegrityError("GC apply receipt is invalid")
                        recover_transactions(self._root)
                        return cast(dict[str, object], replay)
                    plan = _gc_plan(self._root, connection)
            except sqlite3.Error as error:
                raise IntegrityError("cannot inspect garbage-collection state") from error
            if plan["plan_id"] != normalized_plan_id:
                raise UserInputError("GC plan is stale; generate a new preview")
            if plan["required_confirmation"] != normalized_confirmation:
                raise UserInputError("GC apply requires the exact preview confirmation")
            if plan["maintenance_version"] != expected_version:
                raise UserInputError(
                    "maintenance version does not match expected version "
                    f"{expected_version}; actual version is {plan['maintenance_version']}"
                )
            candidates = cast(list[dict[str, object]], plan["candidates"])
            large_ids = {
                cast(str, candidate["source_id"])
                for candidate in candidates
                if candidate["large_original"] is True
            }
            if large_ids != set(confirmed_large_source_ids):
                raise UserInputError(
                    "GC apply requires explicit confirmation of every large original"
                )
            deleted_ids = [cast(str, candidate["source_id"]) for candidate in candidates]
            object_references = tuple(
                cast(str, candidate["object_reference"]) for candidate in candidates
            )
            record_paths = tuple(
                cast(str, candidate["record_path"])
                for candidate in candidates
                if candidate["record_path"] is not None
            )
            occurred_at = datetime.now(timezone.utc).isoformat()
            maintenance_version = expected_version + 1
            result: dict[str, object] = {
                "plan_id": normalized_plan_id,
                "deleted_source_ids": deleted_ids,
                "deleted_bytes": sum(cast(int, item["size_bytes"]) for item in candidates),
                "deletion_markers": [
                    {
                        "subject_kind": "source",
                        "subject_fingerprint": _deletion_fingerprint(source_id),
                    }
                    for source_id in deleted_ids
                ],
                "maintenance_version": maintenance_version,
            }
            staged_database = _stage_gc_apply(
                database_path,
                source_ids=tuple(deleted_ids),
                maintenance_version=maintenance_version,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                entrance=normalized_entrance,
                occurred_at=occurred_at,
                result=result,
            )
            atomic_commit(
                self._root,
                [
                    (database_path, staged_database),
                    gc_cleanup_change(
                        self._root,
                        object_references=object_references,
                        record_paths=record_paths,
                    ),
                ],
            )
            recover_transactions(self._root)
        return result

    @staticmethod
    def verify_backup(archive_path: Path) -> dict[str, object]:
        archive = archive_path.resolve()
        if not archive.is_file():
            raise UserInputError(f"cold backup does not exist: {archive}")
        with tempfile.TemporaryDirectory(prefix="myoutbrain-backup-verify-") as temporary:
            extracted = Path(temporary)
            _extract_archive(archive, extracted)
            report = diagnose_instance(extracted)
        return {
            "kind": "cold-full-instance-zip",
            "path": str(archive),
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "valid": report["overall"] == "ok",
            "doctor": report,
        }

    @staticmethod
    def restore_backup(
        archive_path: Path,
        destination_path: Path,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        archive = archive_path.resolve()
        destination = destination_path.resolve()
        if expected_version != 0:
            raise UserInputError("cold restore to a new directory requires expected version 0")
        _required_text(idempotency_key, "idempotency key", maximum=200)
        if not archive.is_file():
            raise UserInputError(f"cold backup does not exist: {archive}")
        if destination.exists():
            report = diagnose_instance(destination)
            if report["overall"] == "ok" and _directory_matches_archive(
                archive, destination
            ):
                return _restore_result(archive, destination, report)
            raise UserInputError(f"cold backup restore destination is not a matching retry: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent)
        )
        try:
            _extract_archive(archive, staged)
            report = diagnose_instance(staged)
            if report["overall"] != "ok":
                raise IntegrityError("restored private instance did not pass read-only Doctor")
            os.replace(staged, destination)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return _restore_result(archive, destination, report)


@dataclass(frozen=True)
class _SourceRecordManifest:
    source_id: str
    content_hash: str
    object_reference: str
    path: Path


def _diagnose_configuration(
    configuration_path: Path,
    issues: list[dict[str, object]],
) -> None:
    if not configuration_path.is_file():
        issues.append({"code": "configuration-missing", "path": "myoutbrain.toml"})
        return
    try:
        with configuration_path.open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
        storage = configuration.get("storage")
        valid = (
            configuration.get("instance_version") == 2
            and configuration.get("schema_version") == 1
            and configuration.get("single_writer") is True
            and isinstance(storage, dict)
            and storage.get("permanent") == ["vault", "store"]
            and storage.get("rebuildable") == ["runtime"]
        )
    except (OSError, tomllib.TOMLDecodeError):
        valid = False
    if not valid:
        issues.append({"code": "configuration-invalid", "path": "myoutbrain.toml"})


def _diagnose_protocol_contract(issues: list[dict[str, object]]) -> None:
    try:
        request = load_domain_schema("domain-request-v2.json")
        response = load_domain_schema("domain-response-v2.json")
        compatibility = load_domain_schema("compatibility-v2.json")
        current = compatibility.get("current")
        if (
            request.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or response.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or current != SERVER_PROTOCOL_VERSION
        ):
            raise RuntimeError("packaged protocol contract is inconsistent")
    except RuntimeError as error:
        issues.append({"code": "protocol-contract-invalid", "detail": str(error)})


def diagnose_instance(root: Path) -> dict[str, object]:
    resolved = root.resolve()
    database_path = resolved / MEMORY_DATABASE
    configuration_path = resolved / "myoutbrain.toml"
    canonical_issues: list[dict[str, object]] = []
    projection_issues: list[dict[str, object]] = []
    _diagnose_configuration(configuration_path, canonical_issues)
    _diagnose_protocol_contract(canonical_issues)
    if not database_path.is_file():
        canonical_issues.append(
            {"code": "canonical-database-missing", "path": MEMORY_DATABASE}
        )
    else:
        try:
            with closing(_read_only_connection(database_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
                if version != (MEMORY_SCHEMA_VERSION,):
                    canonical_issues.append(
                        {
                            "code": "canonical-schema-unsupported",
                            "actual": version[0] if version else None,
                            "expected": MEMORY_SCHEMA_VERSION,
                        }
                    )
                if quick_check != ("ok",):
                    canonical_issues.append({"code": "canonical-database-corrupt"})
                if foreign_keys:
                    canonical_issues.append(
                        {"code": "canonical-relationship-broken", "count": len(foreign_keys)}
                    )
                _diagnose_memory_content(connection, canonical_issues)
                _diagnose_capsule_invariants(connection, canonical_issues)
                _diagnose_dependency_graph(connection, canonical_issues)
                _diagnose_source_objects(resolved, connection, canonical_issues)
                _diagnose_fts(connection, projection_issues)
                _diagnose_runtime_projections(resolved, connection, projection_issues)
        except sqlite3.Error as error:
            canonical_issues.append(
                {"code": "canonical-database-unreadable", "detail": str(error)}
            )
    _diagnose_source_records(resolved, canonical_issues)
    overall = (
        "restricted-read-only"
        if canonical_issues
        else "degraded"
        if projection_issues
        else "ok"
    )
    return {
        "mode": "read-only",
        "overall": overall,
        "write_allowed": not canonical_issues,
        "canonical_issues": canonical_issues,
        "projection_issues": projection_issues,
        "repairable": bool(projection_issues) and not canonical_issues,
        "maintenance_version": _maintenance_version(database_path),
        "checks": {
            "configuration": "ok" if not any(
                issue["code"] in {"configuration-missing", "configuration-invalid"}
                for issue in canonical_issues
            ) else "failed",
            "protocol_and_entrance_contract": "ok" if not any(
                issue["code"] == "protocol-contract-invalid"
                for issue in canonical_issues
            ) else "failed",
            "capsule_and_relationship_invariants": "ok" if not any(
                cast(str, issue["code"]).startswith(("capsule-", "partition-", "dependency-"))
                for issue in canonical_issues
            ) else "failed",
        },
        "protocol_version": dict(SERVER_PROTOCOL_VERSION),
        "server_capabilities": list(SERVER_CAPABILITIES),
    }


def canonical_write_blocker(root: Path) -> str | None:
    resolved = root.resolve()
    if not (resolved / "myoutbrain.toml").is_file() or not (
        resolved / MEMORY_DATABASE
    ).is_file():
        return None
    try:
        with closing(_read_only_connection(resolved / MEMORY_DATABASE)) as connection:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if (
            version_row is not None
            and isinstance(version_row[0], int)
            and 1 <= version_row[0] < MEMORY_SCHEMA_VERSION
            and quick_check == ("ok",)
        ):
            return None
    except sqlite3.Error:
        pass
    report = diagnose_instance(resolved)
    issues = cast(list[dict[str, object]], report["canonical_issues"])
    if not issues:
        return None
    return cast(str, issues[0]["code"])


def _maintenance_version(database_path: Path) -> int | None:
    if not database_path.is_file():
        return None
    try:
        with closing(_read_only_connection(database_path)) as connection:
            row = connection.execute(
                "SELECT version FROM maintenance_state WHERE singleton = 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row is not None and isinstance(row[0], int) else None


def _maintenance_replay(
    database_path: Path,
    *,
    operation: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """
                SELECT request_hash, result_json FROM maintenance_writes
                WHERE operation = ? AND idempotency_key = ?
                """,
                (operation, idempotency_key),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect maintenance write receipt") from error
    if row is None:
        return None
    if row[0] != request_hash:
        raise UserInputError("idempotency key was already used for another request")
    try:
        result = json.loads(cast(str, row[1]))
    except json.JSONDecodeError as error:
        raise IntegrityError("maintenance write receipt is invalid") from error
    if not isinstance(result, dict):
        raise IntegrityError("maintenance write receipt is invalid")
    return cast(dict[str, object], result)


def _stage_maintenance_receipt(
    database_path: Path,
    *,
    operation: str,
    event_type: str,
    subject_id: str,
    maintenance_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
    occurred_at: str,
    result: dict[str, object],
) -> bytes:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".maintenance-write.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            _record_maintenance_write(
                connection,
                operation=operation,
                event_type=event_type,
                subject_id=subject_id,
                maintenance_version=maintenance_version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                entrance=entrance,
                occurred_at=occurred_at,
                result=result,
            )
            connection.commit()
        return temporary_path.read_bytes()
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot stage maintenance write receipt") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _record_maintenance_write(
    connection: sqlite3.Connection,
    *,
    operation: str,
    event_type: str,
    subject_id: str,
    maintenance_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
    occurred_at: str,
    result: dict[str, object],
) -> None:
    connection.execute(
        "UPDATE maintenance_state SET version = ? WHERE singleton = 1",
        (maintenance_version,),
    )
    result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO audit_events
            (event_id, event_type, occurred_at, subject_id, proposal_id,
             before_version, after_version, entrance, result_hash)
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            f"evt_{uuid.uuid4().hex}",
            event_type,
            occurred_at,
            subject_id,
            maintenance_version - 1,
            maintenance_version,
            entrance,
            result_hash,
        ),
    )
    connection.execute(
        """
        INSERT INTO maintenance_writes
            (operation, idempotency_key, request_hash, result_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (operation, idempotency_key, request_hash, result_json, occurred_at),
    )


def _restore_result(
    archive: Path,
    destination: Path,
    report: dict[str, object],
) -> dict[str, object]:
    return {
        "kind": "cold-full-instance-zip",
        "archive_path": str(archive),
        "restored_root": str(destination),
        "doctor_mode": "read-only",
        "doctor": report,
        "switch_allowed": True,
    }


def _directory_matches_archive(archive_path: Path, destination: Path) -> bool:
    try:
        with ZipFile(archive_path) as archive:
            archived = {
                member.filename: hashlib.sha256(archive.read(member)).hexdigest()
                for member in archive.infolist()
                if not member.is_dir()
            }
    except (BadZipFile, OSError):
        return False
    restored = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in destination.rglob("*")
        if path.is_file() and path.name != ".myoutbrain.lock"
    }
    return archived == restored


def _gc_plan(root: Path, connection: sqlite3.Connection) -> dict[str, object]:
    version_row = connection.execute(
        "SELECT version FROM maintenance_state WHERE singleton = 1"
    ).fetchone()
    if version_row is None or not isinstance(version_row[0], int):
        raise IntegrityError("private instance has no maintenance version")
    protected, protection_counts = _protected_source_ids(root, connection)
    rows = connection.execute(
        """
        SELECT source_id, content_hash, object_reference, created_at
        FROM source_objects ORDER BY source_id
        """
    ).fetchall()
    row_by_reference = {cast(str, row[2]): row for row in rows}
    manifests = _source_record_manifests(root)
    manifest_by_reference = {manifest.object_reference: manifest for manifest in manifests}
    object_root = root / "store" / "objects" / "sha256"
    candidates: list[dict[str, object]] = []
    for object_path in sorted(path for path in object_root.rglob("*") if path.is_file()):
        digest = object_path.name
        reference = object_path.relative_to(root / "store" / "objects").as_posix()
        row = row_by_reference.get(reference)
        if reference in manifest_by_reference:
            continue
        source_id = cast(str, row[0]) if row is not None else f"src_{digest}"
        record_path = root / "store" / "records" / f"{source_id}.json"
        if source_id in protected or record_path.is_file():
            continue
        size = object_path.stat().st_size
        created_at = cast(str, row[3]) if row is not None else datetime.fromtimestamp(
            object_path.stat().st_mtime, timezone.utc
        ).isoformat()
        candidates.append(
            {
                "source_id": source_id,
                "content_hash": cast(str, row[1]) if row is not None else f"sha256:{digest}",
                "object_reference": reference,
                "record_path": (
                    record_path.relative_to(root / "store" / "records").as_posix()
                    if record_path.is_file()
                    else None
                ),
                "size_bytes": size,
                "last_reference": {
                    "at": created_at,
                    "kind": "source-object-registration" if row is not None else "filesystem-object",
                },
                "deletion_impact": {
                    "removes_object_body": True,
                    "removes_source_registration": row is not None,
                    "preserves_minimal_deletion_marker": True,
                },
                "large_original": size >= 1024 * 1024,
            }
        )
    plan_basis = {
        "maintenance_version": version_row[0],
        "candidates": [
            {
                "source_id": item["source_id"],
                "content_hash": item["content_hash"],
                "object_reference": item["object_reference"],
                "size_bytes": item["size_bytes"],
            }
            for item in candidates
        ],
    }
    plan_id = "gcp_" + _stable_hash(plan_basis)[:32]
    return {
        "plan_id": plan_id,
        "maintenance_version": version_row[0],
        "candidates": candidates,
        "total_bytes": sum(cast(int, item["size_bytes"]) for item in candidates),
        "protected_reference_counts": protection_counts,
        "required_confirmation": f"delete-orphan-objects:{plan_id}",
    }


def _protected_source_ids(
    root: Path,
    connection: sqlite3.Connection,
) -> tuple[set[str], dict[str, int]]:
    categories: dict[str, set[str]] = {
        "lifecycle_history": set(),
        "experience": set(),
        "review": set(),
        "audit": set(),
        "migration": set(),
        "source_record": set(),
    }
    for table, category in (
        ("canonical_memory_sources", "lifecycle_history"),
        ("canonical_memory_version_sources", "lifecycle_history"),
        ("experiences", "experience"),
        ("integration_proposal_sources", "review"),
        ("legacy_source_metadata", "lifecycle_history"),
    ):
        categories[category].update(
            row[0]
            for row in connection.execute(f"SELECT DISTINCT source_id FROM {table}")
            if isinstance(row[0], str)
        )
    known_ids = {
        row[0]
        for row in connection.execute("SELECT source_id FROM source_objects")
        if isinstance(row[0], str)
    }
    categories["audit"].update(
        row[0]
        for row in connection.execute(
            "SELECT subject_id FROM audit_events UNION SELECT subject_id FROM memory_events"
        )
        if isinstance(row[0], str) and row[0] in known_ids
    )
    for supporting, opposing in connection.execute(
        "SELECT supporting_evidence_json, opposing_evidence_json FROM review_proposals"
    ):
        categories["review"].update(_source_ids_in_json(supporting, known_ids))
        categories["review"].update(_source_ids_in_json(opposing, known_ids))
    for (source_reference,) in connection.execute(
        "SELECT source_reference_json FROM reflection_inputs"
    ):
        categories["review"].update(_source_ids_in_json(source_reference, known_ids))
    categories["migration"].update(
        row[0]
        for row in connection.execute(
            """
            SELECT subject_id FROM audit_events
            WHERE event_type IN ('migration.export', 'migration.import')
            """
        )
        if isinstance(row[0], str) and row[0] in known_ids
    )
    categories["source_record"].update(
        manifest.source_id for manifest in _source_record_manifests(root)
    )
    protected: set[str] = set().union(*categories.values())
    return protected, {category: len(values) for category, values in categories.items()}


def _source_ids_in_json(value: object, known_ids: set[str]) -> set[str]:
    try:
        document = json.loads(cast(str, value))
    except (json.JSONDecodeError, TypeError):
        raise IntegrityError("retained reference JSON is invalid")
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key == "source_id" and isinstance(nested, str) and nested in known_ids:
                    found.add(nested)
                else:
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(document)
    return found


def _stage_gc_apply(
    database_path: Path,
    *,
    source_ids: tuple[str, ...],
    maintenance_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
    occurred_at: str,
    result: dict[str, object],
) -> bytes:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".gc-apply.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for source_id in source_ids:
                connection.execute("DELETE FROM source_objects WHERE source_id = ?", (source_id,))
                fingerprint = _deletion_fingerprint(source_id)
                marker_id = "del_" + hashlib.sha256(
                    f"source:{fingerprint}".encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO deletion_markers
                        (marker_id, subject_kind, subject_fingerprint, deleted_at,
                         backup_exclusion_after)
                    VALUES (?, 'source', ?, ?, ?)
                    """,
                    (marker_id, fingerprint, occurred_at, occurred_at),
                )
            _record_maintenance_write(
                connection,
                operation="maintenance.gc_apply",
                event_type="maintenance.gc",
                subject_id="private-instance",
                maintenance_version=maintenance_version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                entrance=entrance,
                occurred_at=occurred_at,
                result=result,
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("garbage collection broke canonical references")
        return temporary_path.read_bytes()
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot stage garbage-collection apply") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _deletion_fingerprint(subject_id: str) -> str:
    return "sha256:" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _stage_projection_repair(
    database_path: Path,
    *,
    maintenance_version: int,
    idempotency_key: str,
    request_hash: str,
    entrance: str,
    occurred_at: str,
    result: dict[str, object],
) -> tuple[bytes, bytes, bytes]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".doctor-repair.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            rows = connection.execute(
                """
                SELECT dictionary.memory_id, dictionary.primary_capsule_id,
                       dictionary.canonical_name, version.content,
                       version.applicability_scope
                FROM knowledge_dictionary AS dictionary
                JOIN canonical_memory_versions AS version
                  ON version.memory_id = dictionary.memory_id
                 AND version.version = dictionary.current_version
                ORDER BY dictionary.memory_id
                """
            ).fetchall()
            connection.execute("DROP TABLE IF EXISTS canonical_memory_fts")
            connection.execute(
                """
                CREATE VIRTUAL TABLE canonical_memory_fts USING fts5(
                    memory_id UNINDEXED,
                    capsule_id UNINDEXED,
                    canonical_name,
                    body,
                    applicability_scope,
                    search_terms,
                    tokenize = 'unicode61'
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO canonical_memory_fts
                    (memory_id, capsule_id, canonical_name, body,
                     applicability_scope, search_terms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        " ".join(
                            sorted(
                                lexical_terms(f"{row[2]} {row[3]} {row[4] or ''}")
                            )
                        ),
                    )
                    for row in rows
                ),
            )
            evidence_graph = _json_bytes(_evidence_graph_projection(connection))
            tree_summary = _json_bytes(_tree_summary_projection(connection))
            _record_maintenance_write(
                connection,
                operation="instance.doctor.repair",
                event_type="instance.doctor.repair",
                subject_id="private-instance",
                maintenance_version=maintenance_version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                entrance=entrance,
                occurred_at=occurred_at,
                result=result,
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("Doctor projection repair broke references")
        return temporary_path.read_bytes(), evidence_graph, tree_summary
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot stage Doctor projection repair") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _evidence_graph_projection(connection: sqlite3.Connection) -> dict[str, object]:
    evidence = connection.execute(
        """
        SELECT memory_id, version, source_id, source_version, relationship
        FROM canonical_memory_version_evidence
        ORDER BY memory_id, version, source_id, source_version, relationship
        """
    ).fetchall()
    dependencies = connection.execute(
        """
        SELECT memory_id, version, depends_on_memory_id, depends_on_version,
               relationship
        FROM canonical_memory_dependencies
        ORDER BY memory_id, version, depends_on_memory_id, depends_on_version,
                 relationship
        """
    ).fetchall()
    return {
        "schema_version": 1,
        "evidence": [list(row) for row in evidence],
        "dependencies": [list(row) for row in dependencies],
    }


def _tree_summary_projection(connection: sqlite3.Connection) -> dict[str, object]:
    rows = connection.execute(
        """
        SELECT partition.partition_id, partition.parent_partition_id,
               partition.node_kind, partition.topic, capsule.capsule_id,
               capsule.body_bytes, capsule.memory_record_count
        FROM knowledge_partitions AS partition
        LEFT JOIN capsule_partitions AS membership
          ON membership.partition_id = partition.partition_id
        LEFT JOIN knowledge_capsules AS capsule
          ON capsule.capsule_id = membership.capsule_id
        ORDER BY partition.partition_id, capsule.capsule_id
        """
    ).fetchall()
    return {"schema_version": 1, "nodes": [list(row) for row in rows]}


def _required_text(value: str, field: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise UserInputError(f"{field} must contain 1 to {maximum} characters")
    return normalized


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)


def _checkpoint_and_close(database_path: Path) -> None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            connection.commit()
    except sqlite3.Error as error:
        raise IntegrityError("cannot checkpoint the canonical database for backup") from error


def _diagnose_memory_content(
    connection: sqlite3.Connection,
    issues: list[dict[str, object]],
) -> None:
    missing = connection.execute(
        """
        SELECT memory.memory_id
        FROM canonical_memories AS memory
        LEFT JOIN canonical_memory_versions AS version
          ON version.memory_id = memory.memory_id
         AND version.version = memory.current_version
        WHERE version.memory_id IS NULL
        ORDER BY memory.memory_id
        """
    ).fetchall()
    for (memory_id,) in missing:
        issues.append({"code": "canonical-memory-content-missing-or-mismatched", "memory_id": memory_id})
    dictionary = connection.execute(
        """
        SELECT dictionary.memory_id
        FROM knowledge_dictionary AS dictionary
        JOIN canonical_memories AS memory ON memory.memory_id = dictionary.memory_id
        WHERE dictionary.current_version <> memory.current_version
        ORDER BY dictionary.memory_id
        """
    ).fetchall()
    for (memory_id,) in dictionary:
        issues.append({"code": "canonical-dictionary-pointer-mismatched", "memory_id": memory_id})


def _diagnose_capsule_invariants(
    connection: sqlite3.Connection,
    issues: list[dict[str, object]],
) -> None:
    active_capsules = connection.execute(
        """
        SELECT capsule.capsule_id, capsule.body_bytes, capsule.memory_record_count,
               COUNT(dictionary.memory_id), COALESCE(SUM(LENGTH(CAST(version.content AS BLOB))), 0)
        FROM knowledge_capsules AS capsule
        LEFT JOIN knowledge_dictionary AS dictionary
          ON dictionary.primary_capsule_id = capsule.capsule_id
        LEFT JOIN canonical_memory_versions AS version
          ON version.memory_id = dictionary.memory_id
         AND version.version = dictionary.current_version
        WHERE capsule.status = 'active'
        GROUP BY capsule.capsule_id
        ORDER BY capsule.capsule_id
        """
    ).fetchall()
    for capsule_id, body_bytes, memory_count, actual_count, actual_bytes in active_capsules:
        if body_bytes != actual_bytes or memory_count != actual_count:
            issues.append(
                {
                    "code": "capsule-summary-mismatched",
                    "capsule_id": capsule_id,
                    "expected_body_bytes": actual_bytes,
                    "expected_memory_record_count": actual_count,
                }
            )
    invalid_primary = connection.execute(
        """
        SELECT dictionary.memory_id
        FROM knowledge_dictionary AS dictionary
        JOIN knowledge_capsules AS capsule
          ON capsule.capsule_id = dictionary.primary_capsule_id
        WHERE capsule.status <> 'active'
        ORDER BY dictionary.memory_id
        """
    ).fetchall()
    for (memory_id,) in invalid_primary:
        issues.append({"code": "capsule-primary-not-active", "memory_id": memory_id})
    invalid_memberships = connection.execute(
        """
        SELECT membership.capsule_id
        FROM capsule_partitions AS membership
        JOIN knowledge_partitions AS partition
          ON partition.partition_id = membership.partition_id
        WHERE partition.node_kind <> 'leaf'
        ORDER BY membership.capsule_id
        """
    ).fetchall()
    for (capsule_id,) in invalid_memberships:
        issues.append({"code": "capsule-partition-membership-invalid", "capsule_id": capsule_id})
    missing_memberships = connection.execute(
        """
        SELECT capsule.capsule_id
        FROM knowledge_capsules AS capsule
        LEFT JOIN capsule_partitions AS membership
          ON membership.capsule_id = capsule.capsule_id
        WHERE capsule.status = 'active' AND membership.capsule_id IS NULL
        ORDER BY capsule.capsule_id
        """
    ).fetchall()
    for (capsule_id,) in missing_memberships:
        issues.append({"code": "capsule-partition-membership-missing", "capsule_id": capsule_id})

    parents = {
        cast(str, partition_id): cast(str | None, parent_id)
        for partition_id, parent_id in connection.execute(
            "SELECT partition_id, parent_partition_id FROM knowledge_partitions"
        )
    }
    roots = {partition_id for partition_id, parent_id in parents.items() if parent_id is None}
    if parents and roots != {"prt_root"}:
        issues.append({"code": "partition-root-invalid", "roots": sorted(roots)})
    for partition_id in sorted(parents):
        visited: set[str] = set()
        current: str | None = partition_id
        while current is not None and current not in visited:
            visited.add(current)
            current = parents.get(current)
        if current is not None or "prt_root" not in visited:
            issues.append({"code": "partition-ancestry-invalid", "partition_id": partition_id})


def _diagnose_dependency_graph(
    connection: sqlite3.Connection,
    issues: list[dict[str, object]],
) -> None:
    graph: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for memory_id, version, target_id, target_version in connection.execute(
        """
        SELECT memory_id, version, depends_on_memory_id, depends_on_version
        FROM canonical_memory_dependencies
        ORDER BY memory_id, version, depends_on_memory_id, depends_on_version
        """
    ):
        graph.setdefault((cast(str, memory_id), cast(int, version)), set()).add(
            (cast(str, target_id), cast(int, target_version))
        )

    visiting: set[tuple[str, int]] = set()
    visited: set[tuple[str, int]] = set()

    def visit(node: tuple[str, int]) -> bool:
        if node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        valid = all(visit(target) for target in graph.get(node, set()))
        visiting.remove(node)
        visited.add(node)
        return valid

    for node in sorted(graph):
        if not visit(node):
            issues.append(
                {"code": "dependency-cycle", "memory_id": node[0], "version": node[1]}
            )
            break
    sourced = {
        (cast(str, memory_id), cast(int, version))
        for memory_id, version in connection.execute(
            """
            SELECT memory_id, version FROM canonical_memory_version_evidence
            UNION
            SELECT memory_id, version FROM canonical_memory_version_sources
            """
        )
    }
    dependency_nodes = set(graph).union(
        target for targets in graph.values() for target in targets
    )
    for memory_id, version in sorted(dependency_nodes):
        if not graph.get((memory_id, version)) and (memory_id, version) not in sourced:
            issues.append(
                {
                    "code": "dependency-terminal-without-source",
                    "memory_id": memory_id,
                    "version": version,
                }
            )


def _diagnose_source_objects(
    root: Path,
    connection: sqlite3.Connection,
    issues: list[dict[str, object]],
) -> None:
    object_root = (root / "store" / "objects").resolve()
    rows = connection.execute(
        "SELECT source_id, content_hash, object_reference FROM source_objects ORDER BY source_id"
    ).fetchall()
    for source_id, content_hash, object_reference in rows:
        if not isinstance(content_hash, str) or not content_hash.startswith("sha256:"):
            issues.append({"code": "source-object-hash-invalid", "source_id": source_id})
            continue
        candidate = (object_root / cast(str, object_reference)).resolve()
        if candidate == object_root or not candidate.is_relative_to(object_root):
            issues.append({"code": "source-object-path-invalid", "source_id": source_id})
            continue
        try:
            content = candidate.read_bytes()
        except OSError:
            issues.append({"code": "source-object-missing", "source_id": source_id})
            continue
        if hashlib.sha256(content).hexdigest() != content_hash.removeprefix("sha256:"):
            issues.append({"code": "source-object-hash-mismatched", "source_id": source_id})


def _diagnose_source_records(
    root: Path,
    issues: list[dict[str, object]],
) -> None:
    try:
        manifests = _source_record_manifests(root)
    except IntegrityError as error:
        issues.append({"code": "source-record-invalid", "detail": str(error)})
        return
    object_root = (root / "store" / "objects").resolve()
    for manifest in manifests:
        try:
            candidate = (object_root / manifest.object_reference).resolve()
            if candidate == object_root or not candidate.is_relative_to(object_root):
                raise ValueError
            content = candidate.read_bytes()
        except (OSError, ValueError):
            issues.append(
                {"code": "source-record-or-object-missing", "path": manifest.path.name}
            )
            continue
        if hashlib.sha256(content).hexdigest() != manifest.content_hash.removeprefix("sha256:"):
            issues.append(
                {"code": "source-object-hash-mismatched", "source_id": manifest.source_id}
            )


def _source_record_manifests(root: Path) -> tuple[_SourceRecordManifest, ...]:
    records_root = root / "store" / "records"
    if not records_root.is_dir():
        return ()
    manifests: list[_SourceRecordManifest] = []
    for record_path in sorted(path for path in records_root.glob("*.json") if path.is_file()):
        try:
            document = json.loads(record_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise TypeError
            source_id = document.get("id")
            content_hash = document.get("content_hash")
            object_reference = document.get("object")
            if (
                document.get("schema_version") != 1
                or document.get("kind") != "source"
                or document.get("state") != "active"
                or not isinstance(source_id, str)
                or record_path.name != f"{source_id}.json"
                or not isinstance(content_hash, str)
                or not content_hash.startswith("sha256:")
                or not isinstance(object_reference, str)
            ):
                raise TypeError
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
            raise IntegrityError(f"source record manifest is invalid: {record_path.name}") from error
        manifests.append(
            _SourceRecordManifest(
                source_id=source_id,
                content_hash=content_hash,
                object_reference=object_reference,
                path=record_path,
            )
        )
    return tuple(manifests)


def _diagnose_fts(
    connection: sqlite3.Connection,
    issues: list[dict[str, object]],
) -> None:
    try:
        canonical_rows = connection.execute(
            """
            SELECT dictionary.memory_id, dictionary.primary_capsule_id,
                   dictionary.canonical_name, version.content,
                   version.applicability_scope
            FROM knowledge_dictionary AS dictionary
            JOIN canonical_memory_versions AS version
              ON version.memory_id = dictionary.memory_id
             AND version.version = dictionary.current_version
            ORDER BY dictionary.memory_id
            """
        ).fetchall()
        expected = [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                " ".join(sorted(lexical_terms(f"{row[2]} {row[3]} {row[4] or ''}"))),
            )
            for row in canonical_rows
        ]
        actual = connection.execute(
            """
            SELECT memory_id, capsule_id, canonical_name, body,
                   applicability_scope, search_terms
            FROM canonical_memory_fts ORDER BY memory_id
            """
        ).fetchall()
    except sqlite3.Error:
        issues.append({"code": "fts-projection-missing"})
        return
    if expected != actual:
        issues.append(
            {
                "code": "fts-projection-out-of-date",
                "expected_count": len(expected),
                "actual_count": len(actual),
            }
        )


def _diagnose_runtime_projections(
    root: Path,
    connection: sqlite3.Connection,
    issues: list[dict[str, object]],
) -> None:
    expected = {
        "evidence-graph.json": _evidence_graph_projection(connection),
        "tree-summary.json": _tree_summary_projection(connection),
    }
    indexes = root / "runtime" / "indexes"
    present = {name for name in expected if (indexes / name).is_file()}
    if not present:
        materialized = connection.execute(
            """
            SELECT 1 FROM maintenance_writes
            WHERE operation = 'instance.doctor.repair'
            LIMIT 1
            """
        ).fetchone()
        if materialized is not None:
            for name in expected:
                issues.append(
                    {
                        "code": "runtime-projection-missing",
                        "path": f"runtime/indexes/{name}",
                    }
                )
        return
    for name, expected_document in expected.items():
        path = indexes / name
        if not path.is_file():
            issues.append({"code": "runtime-projection-missing", "path": f"runtime/indexes/{name}"})
            continue
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            issues.append({"code": "runtime-projection-corrupt", "path": f"runtime/indexes/{name}"})
            continue
        if actual != expected_document:
            issues.append({"code": "runtime-projection-out-of-date", "path": f"runtime/indexes/{name}"})


def _extract_archive(archive_path: Path, destination: Path) -> None:
    try:
        with ZipFile(archive_path) as archive:
            seen: set[str] = set()
            for member in archive.infolist():
                _validate_archive_member(member, seen)
            archive.extractall(destination)
    except (BadZipFile, OSError) as error:
        raise IntegrityError("cannot read the cold full-instance backup") from error


def _validate_archive_member(member: ZipInfo, seen: set[str]) -> None:
    normalized = member.filename.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or normalized in seen
        or path.is_absolute()
        or ".." in path.parts
        or (member.external_attr >> 16) & 0o170000 == 0o120000
    ):
        raise UserInputError(f"cold backup contains an unsafe path: {member.filename}")
    seen.add(normalized)
