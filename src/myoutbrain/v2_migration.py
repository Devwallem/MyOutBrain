from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from myoutbrain.core_types import ConfigurationConflict, IntegrityError, UserInputError
from myoutbrain.local_core import LocalMemoryCore, MEMORY_DATABASE, MEMORY_SCHEMA_VERSION
from myoutbrain.persistence import atomic_commit, atomic_write, recover_transactions, writer_lock
from myoutbrain.retrieval import lexical_terms
from myoutbrain.unified_review import ReviewProposalInput, stage_review_proposal


MIGRATION_FORMAT = "myoutbrain-migration"
MIGRATION_FORMAT_VERSION = 1


@dataclass(frozen=True)
class _MigrationPackage:
    manifest: dict[str, object]
    documents: dict[str, dict[str, object]]
    package_id: str

    @property
    def memory_documents(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self.documents[path]
            for path in sorted(self.documents)
            if path.startswith("objects/memories/")
        )

    @property
    def source_documents(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self.documents[path]
            for path in sorted(self.documents)
            if path.startswith("objects/sources/")
        )

    @property
    def relationships(self) -> dict[str, object]:
        return self.documents["relationships.json"]


@dataclass(frozen=True)
class _PackageGraph:
    memories: dict[str, set[int]]
    sources: set[tuple[str, int]]
    knowledge: set[tuple[str, int, str, int, str]]
    evidence: set[tuple[str, int, str, int, str]]


def _restriction_blocks_target(restriction: str, target: str) -> bool:
    """Fail closed without inventing target-name policy that the domain does not define."""
    if not target.strip():
        raise UserInputError("migration target must not be empty")
    return bool(restriction.strip())


class V2MigrationService:
    """Transfer audited logical knowledge without exposing the SQLite layout."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def plan(
        self,
        memory_ids: tuple[str, ...],
        *,
        target: str,
    ) -> dict[str, object]:
        selected = _memory_ids(memory_ids)
        normalized_target = _text("migration target", target, maximum=500)
        database_path = self._database_path()
        LocalMemoryCore(self._root).inspect_schema_version()
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                closure = _knowledge_closure(
                    connection, selected, target=normalized_target
                )
                checkpoint_version, previous_checkpoint = _export_checkpoint(connection)
        except sqlite3.Error as error:
            raise IntegrityError("cannot audit the migration closure") from error
        blockers = cast(list[dict[str, object]], closure.pop("blockers"))
        return {
            "allowed": not blockers,
            "target": normalized_target,
            "selected_memory_ids": list(selected),
            "checkpoint_version": checkpoint_version,
            "previous_checkpoint": previous_checkpoint,
            "closure": closure,
            "blockers": blockers,
        }

    def export(
        self,
        output_path: Path,
        memory_ids: tuple[str, ...],
        *,
        target: str,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        selected = _memory_ids(memory_ids)
        normalized_target = _text("migration target", target, maximum=500)
        normalized_key = _text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _text("entrance", entrance, maximum=64)
        if expected_version < 0:
            raise UserInputError("expected migration checkpoint version must be non-negative")
        destination = output_path.resolve()
        database_path = self._database_path()
        request_hash = _stable_hash(
            {
                "operation": "migration.export",
                "output": str(destination),
                "memory_ids": list(selected),
                "target": normalized_target,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    checkpoint_version, previous_checkpoint = _export_checkpoint(connection)
                    existing = connection.execute(
                        """
                        SELECT request_hash, result_hash, subject_id
                        FROM idempotent_writes
                        WHERE operation = 'migration.export' AND idempotency_key = ?
                        """,
                        (normalized_key,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != request_hash:
                            raise UserInputError(
                                "idempotency key was already used for another migration export"
                            )
                        if not destination.is_file():
                            raise IntegrityError(
                                "completed migration export package is missing"
                            )
                        package = _load_package(destination)
                        if package.package_id != existing[1]:
                            raise IntegrityError(
                                "completed migration export package no longer matches its receipt"
                            )
                        return _completed_export_result(
                            package,
                            path=destination,
                            checkpoint=cast(str, existing[2]),
                            checkpoint_version=checkpoint_version,
                        )
                    if destination.exists():
                        raise UserInputError(
                            f"migration package output already exists: {destination}"
                        )
                    if checkpoint_version != expected_version:
                        raise UserInputError(
                            "migration export checkpoint version does not match "
                            f"expected version {expected_version}; actual version is "
                            f"{checkpoint_version}"
                        )
                    closure = _knowledge_closure(
                        connection, selected, target=normalized_target
                    )
                    documents = _package_documents(connection, closure)
            except sqlite3.Error as error:
                raise IntegrityError("cannot read the migration closure") from error
            blockers = cast(list[dict[str, object]], closure.pop("blockers"))
            if blockers:
                paths = "; ".join(cast(str, item["path"]) for item in blockers)
                raise UserInputError(f"migration closure is blocked: {paths}")
            created_at = datetime.now(timezone.utc).isoformat()
            entries = _manifest_entries(documents)
            checkpoint_id = "chk_" + _digest(
                _json_bytes(
                    {
                        "previous": previous_checkpoint,
                        "created_at": created_at,
                        "selected_memory_ids": list(selected),
                        "entries": entries,
                    }
                )
            )[:32]
            manifest_without_id: dict[str, object] = {
                "format": MIGRATION_FORMAT,
                "format_version": MIGRATION_FORMAT_VERSION,
                "minimum_canonical_schema_version": MEMORY_SCHEMA_VERSION,
                "created_at": created_at,
                "target": normalized_target,
                "selected_memory_ids": list(selected),
                "checkpoint": {
                    "previous": previous_checkpoint,
                    "current": checkpoint_id,
                    "version": checkpoint_version + 1,
                },
                "objects": entries,
                "closure": closure,
            }
            package_id = "pkg_" + _digest(_json_bytes(manifest_without_id))
            manifest = {**manifest_without_id, "package_id": package_id}
            package_bytes = _zip_bytes({**documents, "manifest.json": _json_bytes(manifest)})
            staged_database = _stage_export_checkpoint(
                database_path,
                checkpoint_id=checkpoint_id,
                package_id=package_id,
                checkpoint_version=checkpoint_version + 1,
                request_hash=request_hash,
                idempotency_key=normalized_key,
                entrance=normalized_entrance,
                occurred_at=created_at,
            )
            atomic_write(destination, package_bytes)
            atomic_commit(self._root, [(database_path, staged_database)])
        return {
            "package_id": package_id,
            "path": str(destination),
            "checkpoint": checkpoint_id,
            "checkpoint_version": checkpoint_version + 1,
            "previous_checkpoint": previous_checkpoint,
            "memory_count": len(cast(list[object], closure["memory_ids"])),
            "source_version_count": len(
                cast(list[object], closure["source_versions"])
            ),
            "relationship_count": len(
                cast(list[object], closure["knowledge_relationships"])
            )
            + len(cast(list[object], closure["evidence_relationships"])),
        }

    def import_dry_run(self, package_path: Path) -> dict[str, object]:
        package = _load_package(package_path)
        database_path = self._database_path()
        LocalMemoryCore(self._root).inspect_schema_version()
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                preview = _classify_import(connection, package)
                checkpoint_version, imported_packages = _import_checkpoint(connection)
        except sqlite3.Error as error:
            raise IntegrityError("cannot preview the migration import") from error
        already_imported = package.package_id in imported_packages
        blockers = cast(list[dict[str, object]], preview["blockers"])
        changes = cast(dict[str, object], preview["changes"])
        conflict_memory_ids = cast(list[str], changes["conflict_memory_ids"])
        return {
            "package_id": package.package_id,
            "package_target": package.manifest["target"],
            "format_version": MIGRATION_FORMAT_VERSION,
            "status": (
                "already-imported"
                if already_imported
                else "blocked"
                if blockers and not conflict_memory_ids
                else "conflict-review"
                if conflict_memory_ids
                else "ready"
            ),
            "checks": {
                "format_version": "passed",
                "manifest_hash": "passed",
                "object_hashes": "passed",
                "audited_closure": "blocked" if blockers else "passed",
            },
            "target_checkpoint_version": checkpoint_version,
            "changes": preview["changes"],
            "blockers": blockers,
        }

    def import_package(
        self,
        package_path: Path,
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        if expected_version < 0:
            raise UserInputError("expected migration checkpoint version must be non-negative")
        normalized_key = _text("idempotency key", idempotency_key, maximum=200)
        normalized_entrance = _text("entrance", entrance, maximum=64)
        package = _load_package(package_path)
        database_path = self._database_path()
        request_hash = _stable_hash(
            {
                "operation": "migration.import",
                "package_id": package.package_id,
                "expected_version": expected_version,
                "entrance": normalized_entrance,
            }
        )
        with writer_lock(self._root):
            recover_transactions(self._root)
            LocalMemoryCore(self._root).inspect_schema_version()
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    checkpoint_version, imported_packages = _import_checkpoint(connection)
                    if package.package_id in imported_packages:
                        return {
                            "package_id": package.package_id,
                            "disposition": "already-imported",
                            "checkpoint_version": checkpoint_version,
                            "created_memory_ids": [],
                            "created_source_versions": [],
                            "conflict_proposal_ids": [],
                        }
                    if checkpoint_version != expected_version:
                        raise UserInputError(
                            "migration import checkpoint version does not match "
                            f"expected version {expected_version}; actual version is "
                            f"{checkpoint_version}"
                        )
                    existing_write = connection.execute(
                        """
                        SELECT request_hash FROM idempotent_writes
                        WHERE operation = 'migration.import' AND idempotency_key = ?
                        """,
                        (normalized_key,),
                    ).fetchone()
                    if existing_write is not None:
                        if existing_write != (request_hash,):
                            raise UserInputError(
                                "idempotency key was already used for another migration import"
                            )
                        raise IntegrityError("migration import receipt is incomplete")
                    preview = _classify_import(connection, package)
            except sqlite3.Error as error:
                raise IntegrityError("cannot inspect the migration import") from error
            blockers = cast(list[dict[str, object]], preview["blockers"])
            changes = cast(dict[str, object], preview["changes"])
            conflict_memory_ids = cast(list[str], changes["conflict_memory_ids"])
            non_conflict_blockers = [
                item for item in blockers if item.get("kind") != "dependency-conflict"
            ]
            if conflict_memory_ids and not non_conflict_blockers:
                staged_database, proposal_ids = _stage_conflict_only(
                    database_path,
                    package,
                    conflict_memory_ids=conflict_memory_ids,
                )
                atomic_commit(self._root, [(database_path, staged_database)])
                return {
                    "package_id": package.package_id,
                    "disposition": "conflict-proposed",
                    "checkpoint_version": checkpoint_version,
                    "created_memory_ids": [],
                    "created_source_versions": [],
                    "conflict_proposal_ids": proposal_ids,
                    "blocked_paths": [item["path"] for item in blockers],
                }
            if blockers:
                paths = "; ".join(cast(str, item["path"]) for item in blockers)
                raise UserInputError(f"migration import is blocked: {paths}")
            occurred_at = datetime.now(timezone.utc).isoformat()
            staged_database, conflict_proposal_ids = _stage_import(
                database_path,
                package,
                changes=changes,
                checkpoint_version=checkpoint_version + 1,
                request_hash=request_hash,
                idempotency_key=normalized_key,
                entrance=normalized_entrance,
                occurred_at=occurred_at,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        return {
            "package_id": package.package_id,
            "disposition": "imported",
            "checkpoint_version": checkpoint_version + 1,
            "created_memory_ids": changes["new_memory_ids"],
            "created_source_versions": changes["new_source_versions"],
            "conflict_proposal_ids": conflict_proposal_ids,
        }

    def _database_path(self) -> Path:
        database_path = self._root / MEMORY_DATABASE
        if not database_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain memory core is not initialized at: {self._root}"
            )
        return database_path


def _knowledge_closure(
    connection: sqlite3.Connection,
    selected: tuple[str, ...],
    *,
    target: str,
) -> dict[str, object]:
    blockers: list[dict[str, object]] = []
    memory_ids: set[str] = set()
    memory_paths: dict[str, str] = {}
    pending = [(memory_id, f"memory:{memory_id}") for memory_id in selected]
    knowledge_relationships: set[tuple[str, int, str, int, str]] = set()
    while pending:
        memory_id, dependency_path = pending.pop(0)
        if memory_id in memory_ids:
            continue
        row = connection.execute(
            "SELECT 1 FROM canonical_memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            blockers.append(
                {
                    "kind": "missing-memory",
                    "path": dependency_path,
                    "reason": "selected or transitive knowledge dependency is missing",
                }
            )
            continue
        memory_ids.add(memory_id)
        memory_paths[memory_id] = dependency_path
        rows = connection.execute(
            """
            SELECT memory_id, version, depends_on_memory_id, depends_on_version,
                   relationship
            FROM canonical_memory_dependencies
            WHERE memory_id = ?
            ORDER BY version, depends_on_memory_id, depends_on_version, relationship
            """,
            (memory_id,),
        ).fetchall()
        for relation in rows:
            if (
                not isinstance(relation[0], str)
                or not isinstance(relation[1], int)
                or not isinstance(relation[2], str)
                or not isinstance(relation[3], int)
                or not isinstance(relation[4], str)
            ):
                raise IntegrityError("knowledge dependency is invalid")
            typed_relation = cast(tuple[str, int, str, int, str], relation)
            relation_path = (
                f"{dependency_path} -> memory:{typed_relation[0]}/v{typed_relation[1]} "
                f"-[{typed_relation[4]}]-> "
                f"memory:{typed_relation[2]}/v{typed_relation[3]}"
            )
            origin_exists = connection.execute(
                """
                SELECT 1 FROM canonical_memory_versions
                WHERE memory_id = ? AND version = ?
                """,
                (typed_relation[0], typed_relation[1]),
            ).fetchone()
            target_exists = connection.execute(
                """
                SELECT 1 FROM canonical_memory_versions
                WHERE memory_id = ? AND version = ?
                """,
                (typed_relation[2], typed_relation[3]),
            ).fetchone()
            if origin_exists is None or target_exists is None:
                blockers.append(
                    {
                        "kind": "missing-memory-version",
                        "path": relation_path,
                        "reason": "knowledge dependency version endpoint is missing",
                    }
                )
                continue
            knowledge_relationships.add(typed_relation)
            pending.append((typed_relation[2], relation_path))

    version_identities: list[dict[str, object]] = []
    evidence_relationships: list[dict[str, object]] = []
    source_versions: set[tuple[str, int]] = set()
    source_paths: dict[tuple[str, int], str] = {}
    dependency_origins = {
        (item[0], item[1]) for item in knowledge_relationships
    }
    for memory_id in sorted(memory_ids):
        version_rows = connection.execute(
            """
            SELECT version FROM canonical_memory_versions
            WHERE memory_id = ? ORDER BY version
            """,
            (memory_id,),
        ).fetchall()
        if not version_rows:
            blockers.append(
                {
                    "kind": "missing-memory-version",
                    "path": f"{memory_paths[memory_id]} -> version:*",
                    "reason": "canonical knowledge has no auditable version",
                }
            )
            continue
        for version_row in version_rows:
            version = version_row[0]
            if not isinstance(version, int):
                raise IntegrityError("canonical memory version is invalid")
            version_identities.append({"memory_id": memory_id, "version": version})
            version_path = (
                f"memory:{memory_id}/v{version}"
                if memory_paths[memory_id] == f"memory:{memory_id}"
                else f"{memory_paths[memory_id]} -> memory:{memory_id}/v{version}"
            )
            evidence_rows = connection.execute(
                """
                SELECT source_id, source_version, relationship
                FROM canonical_memory_version_evidence
                WHERE memory_id = ? AND version = ?
                ORDER BY source_id, source_version, relationship
                """,
                (memory_id, version),
            ).fetchall()
            if not evidence_rows and (memory_id, version) not in dependency_origins:
                blockers.append(
                    {
                        "kind": "unauditable-provenance",
                        "path": f"{version_path} -> provenance:missing",
                        "reason": "knowledge version has neither evidence nor a knowledge dependency",
                    }
                )
            for evidence in evidence_rows:
                if (
                    not isinstance(evidence[0], str)
                    or not isinstance(evidence[1], int)
                    or not isinstance(evidence[2], str)
                ):
                    raise IntegrityError("canonical evidence relationship is invalid")
                source_id = evidence[0]
                source_version = evidence[1]
                source_versions.add((source_id, source_version))
                source_paths.setdefault(
                    (source_id, source_version),
                    f"{version_path} -[{evidence[2]}]-> "
                    f"source:{source_id}/v{source_version}",
                )
                evidence_relationships.append(
                    {
                        "memory_id": memory_id,
                        "memory_version": version,
                        "relationship": evidence[2],
                        "source_id": source_id,
                        "source_version": source_version,
                    }
                )
            restriction_rows = connection.execute(
                """
                SELECT proposal.proposal_id, proposal.migration_restrictions_json
                FROM canonical_memory_review_provenance AS provenance
                JOIN review_proposals AS proposal
                  ON proposal.proposal_id = provenance.proposal_id
                WHERE provenance.memory_id = ? AND provenance.version = ?
                ORDER BY proposal.proposal_id
                """,
                (memory_id, version),
            ).fetchall()
            for proposal_id, raw_restrictions in restriction_rows:
                restrictions = _string_list(raw_restrictions, "migration restrictions")
                for restriction in restrictions:
                    if not _restriction_blocks_target(restriction, target):
                        continue
                    blockers.append(
                        {
                            "kind": "restricted-dependency",
                            "path": (
                                f"target:{target} -> {version_path} -> "
                                f"proposal:{proposal_id} -> restriction:{restriction}"
                            ),
                            "reason": "the provenance explicitly restricts migration",
                        }
                    )

    source_identities: list[dict[str, object]] = []
    for source_id, version in sorted(source_versions):
        row = connection.execute(
            """
            SELECT version.content_hash, version.locator, version.observed_at,
                   version.applicability_scope, version.retention,
                   source.source_kind
            FROM evidence_source_versions AS version
            JOIN evidence_sources AS source ON source.source_id = version.source_id
            WHERE version.source_id = ? AND version.version = ?
            """,
            (source_id, version),
        ).fetchone()
        path = source_paths.get(
            (source_id, version), f"source:{source_id}/v{version}"
        )
        if row is None:
            blockers.append(
                {
                    "kind": "missing-source-version",
                    "path": path,
                    "reason": "evidence source version is missing",
                }
            )
            continue
        if not all(isinstance(value, str) and value.strip() for value in row):
            blockers.append(
                {
                    "kind": "unauditable-source",
                    "path": path,
                    "reason": "evidence source receipt is incomplete",
                }
            )
            continue
        source_identities.append({"source_id": source_id, "version": version})

    _append_cycle_blockers(knowledge_relationships, blockers)
    return {
        "memory_ids": sorted(memory_ids),
        "memory_versions": version_identities,
        "source_versions": source_identities,
        "knowledge_relationships": [
            {
                "from_memory_id": item[0],
                "from_version": item[1],
                "relationship": item[4],
                "to_memory_id": item[2],
                "to_version": item[3],
            }
            for item in sorted(knowledge_relationships)
        ],
        "evidence_relationships": evidence_relationships,
        "blockers": blockers,
    }


def _completed_export_result(
    package: _MigrationPackage,
    *,
    path: Path,
    checkpoint: str,
    checkpoint_version: int,
) -> dict[str, object]:
    closure_value = package.manifest.get("closure")
    checkpoint_value = package.manifest.get("checkpoint")
    if not isinstance(closure_value, dict) or not isinstance(checkpoint_value, dict):
        raise IntegrityError("completed migration export manifest is invalid")
    closure = cast(dict[str, object], closure_value)
    checkpoint_data = cast(dict[str, object], checkpoint_value)
    memories = closure.get("memory_ids")
    sources = closure.get("source_versions")
    knowledge = closure.get("knowledge_relationships")
    evidence = closure.get("evidence_relationships")
    if not all(isinstance(value, list) for value in (memories, sources, knowledge, evidence)):
        raise IntegrityError("completed migration export closure is invalid")
    return {
        "package_id": package.package_id,
        "path": str(path),
        "checkpoint": checkpoint,
        "checkpoint_version": checkpoint_version,
        "previous_checkpoint": checkpoint_data.get("previous"),
        "memory_count": len(cast(list[object], memories)),
        "source_version_count": len(cast(list[object], sources)),
        "relationship_count": len(cast(list[object], knowledge))
        + len(cast(list[object], evidence)),
    }


def _append_cycle_blockers(
    relationships: set[tuple[str, int, str, int, str]],
    blockers: list[dict[str, object]],
) -> None:
    graph: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for from_id, from_version, to_id, to_version, _relationship in relationships:
        graph.setdefault((from_id, from_version), set()).add((to_id, to_version))
    visiting: list[tuple[str, int]] = []
    visited: set[tuple[str, int]] = set()

    def visit(node: tuple[str, int]) -> None:
        if node in visiting:
            cycle = visiting[visiting.index(node) :] + [node]
            blockers.append(
                {
                    "kind": "cyclic-knowledge-dependency",
                    "path": " -> ".join(
                        f"memory:{memory_id}/v{version}"
                        for memory_id, version in cycle
                    ),
                    "reason": "knowledge dependencies must terminate in provenance",
                }
            )
            return
        if node in visited:
            return
        visiting.append(node)
        for target in sorted(graph.get(node, set())):
            visit(target)
        visiting.pop()
        visited.add(node)

    for origin in sorted(graph):
        visit(origin)


def _package_documents(
    connection: sqlite3.Connection,
    closure: dict[str, object],
) -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    for memory_id in cast(list[str], closure["memory_ids"]):
        memory = connection.execute(
            """
            SELECT memory.memory_id, memory.current_version, memory.sensitivity,
                   memory.state, memory.previous_live_state, memory.created_at,
                   memory.updated_at, dictionary.canonical_name,
                   capsule.topic
            FROM canonical_memories AS memory
            JOIN knowledge_dictionary AS dictionary
              ON dictionary.memory_id = memory.memory_id
            JOIN knowledge_capsules AS capsule
              ON capsule.capsule_id = dictionary.primary_capsule_id
            WHERE memory.memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
        if memory is None:
            raise IntegrityError(f"migration memory disappeared: {memory_id}")
        versions = connection.execute(
            """
            SELECT version, content, applicability_scope, action, change_reason,
                   created_at, superseded_at, supersession_reason
            FROM canonical_memory_versions
            WHERE memory_id = ? ORDER BY version
            """,
            (memory_id,),
        ).fetchall()
        names = connection.execute(
            """
            SELECT name, normalized_name, name_kind, created_at
            FROM memory_names WHERE memory_id = ?
            ORDER BY name_kind, normalized_name
            """,
            (memory_id,),
        ).fetchall()
        document = {
            "kind": "canonical-memory",
            "memory_id": memory[0],
            "current_version": memory[1],
            "sensitivity": memory[2],
            "state": memory[3],
            "previous_live_state": memory[4],
            "created_at": memory[5],
            "updated_at": memory[6],
            "canonical_name": memory[7],
            "capsule_topic": memory[8],
            "versions": [
                {
                    "version": row[0],
                    "content": row[1],
                    "applicability_scope": row[2],
                    "action": row[3],
                    "change_reason": row[4],
                    "created_at": row[5],
                    "superseded_at": row[6],
                    "supersession_reason": row[7],
                }
                for row in versions
            ],
            "names": [
                {
                    "name": row[0],
                    "normalized_name": row[1],
                    "name_kind": row[2],
                    "created_at": row[3],
                }
                for row in names
            ],
        }
        documents[f"objects/memories/{memory_id}.json"] = _json_bytes(document)
    for source in cast(list[dict[str, object]], closure["source_versions"]):
        source_id = cast(str, source["source_id"])
        version = cast(int, source["version"])
        row = connection.execute(
            """
            SELECT source.source_kind, source.current_locator, source.created_at,
                   version.content_hash, version.locator, version.observed_at,
                   version.applicability_scope, version.retention
            FROM evidence_sources AS source
            JOIN evidence_source_versions AS version
              ON version.source_id = source.source_id
            WHERE source.source_id = ? AND version.version = ?
            """,
            (source_id, version),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"migration source disappeared: {source_id}/v{version}")
        document = {
            "kind": "evidence-source-version",
            "source_id": source_id,
            "version": version,
            "source_kind": row[0],
            "current_locator": row[1],
            "source_created_at": row[2],
            "content_hash": row[3],
            "locator": row[4],
            "observed_at": row[5],
            "applicability_scope": row[6],
            "retention": row[7],
        }
        documents[f"objects/sources/{source_id}-v{version}.json"] = _json_bytes(document)
    documents["relationships.json"] = _json_bytes(
        {
            "knowledge_relationships": closure["knowledge_relationships"],
            "evidence_relationships": closure["evidence_relationships"],
        }
    )
    return documents


def _manifest_entries(documents: dict[str, bytes]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path, content in sorted(documents.items()):
        entries.append(
            {
                "path": path,
                "sha256": _digest(content),
                "size": len(content),
            }
        )
    return entries


def _zip_bytes(documents: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as package:
        for path, content in sorted(documents.items()):
            package.writestr(path, content)
    return buffer.getvalue()


def _export_checkpoint(connection: sqlite3.Connection) -> tuple[int, str | None]:
    rows = connection.execute(
        """
        SELECT subject_id FROM audit_events
        WHERE event_type = 'migration.export'
        ORDER BY occurred_at, event_id
        """
    ).fetchall()
    if any(not isinstance(row[0], str) for row in rows):
        raise IntegrityError("migration export checkpoint is invalid")
    return len(rows), cast(str, rows[-1][0]) if rows else None


def _stage_export_checkpoint(
    database_path: Path,
    *,
    checkpoint_id: str,
    package_id: str,
    checkpoint_version: int,
    request_hash: str,
    idempotency_key: str,
    entrance: str,
    occurred_at: str,
) -> bytes:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".migration-export.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            current_version, _checkpoint = _export_checkpoint(connection)
            if current_version + 1 != checkpoint_version:
                raise UserInputError("migration export checkpoint changed during export")
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, 'migration.export', ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    f"aud_{uuid.uuid4().hex}",
                    occurred_at,
                    checkpoint_id,
                    checkpoint_version - 1,
                    checkpoint_version,
                    entrance,
                    package_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotent_writes
                    (operation, idempotency_key, subject_id, request_hash,
                     result_hash, created_at)
                VALUES ('migration.export', ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    checkpoint_id,
                    request_hash,
                    package_id,
                    occurred_at,
                ),
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("migration export checkpoint broke references")
        return temporary_path.read_bytes()
    except sqlite3.Error as error:
        raise IntegrityError("cannot stage the migration export checkpoint") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _load_package(package_path: Path) -> _MigrationPackage:
    resolved = package_path.resolve()
    if not resolved.is_file():
        raise UserInputError(f"migration package does not exist: {resolved}")
    try:
        with ZipFile(resolved) as package:
            names = package.namelist()
            if len(names) != len(set(names)):
                raise UserInputError("migration package contains duplicate paths")
            if any(
                not name
                or name.startswith(("/", "\\"))
                or ".." in Path(name).parts
                for name in names
            ):
                raise UserInputError("migration package contains an unsafe path")
            manifest_value = json.loads(package.read("manifest.json"))
            if not isinstance(manifest_value, dict) or not all(
                isinstance(key, str) for key in manifest_value
            ):
                raise UserInputError("migration manifest must be a JSON object")
            manifest = cast(dict[str, object], manifest_value)
            if manifest.get("format") != MIGRATION_FORMAT:
                raise UserInputError("unsupported migration package format")
            if manifest.get("format_version") != MIGRATION_FORMAT_VERSION:
                raise UserInputError(
                    f"unsupported migration package version: {manifest.get('format_version')}"
                )
            minimum_schema = manifest.get("minimum_canonical_schema_version")
            if not isinstance(minimum_schema, int) or minimum_schema > MEMORY_SCHEMA_VERSION:
                raise UserInputError(
                    "migration package requires a newer canonical schema version"
                )
            target_value = manifest.get("target")
            if not isinstance(target_value, str):
                raise UserInputError("migration target must be text")
            _text("migration target", target_value, maximum=500)
            checkpoint = manifest.get("checkpoint")
            if not isinstance(checkpoint, dict):
                raise UserInputError("migration checkpoint is invalid")
            checkpoint_data = cast(dict[object, object], checkpoint)
            previous_checkpoint = checkpoint_data.get("previous")
            current_checkpoint = checkpoint_data.get("current")
            checkpoint_version = checkpoint_data.get("version")
            if (
                (previous_checkpoint is not None and not isinstance(previous_checkpoint, str))
                or not isinstance(current_checkpoint, str)
                or not current_checkpoint.startswith("chk_")
                or not isinstance(checkpoint_version, int)
                or isinstance(checkpoint_version, bool)
                or checkpoint_version < 1
            ):
                raise UserInputError("migration checkpoint is invalid")
            package_id = manifest.get("package_id")
            if not isinstance(package_id, str) or not package_id.startswith("pkg_"):
                raise UserInputError("migration package id is invalid")
            unsigned_manifest = dict(manifest)
            del unsigned_manifest["package_id"]
            expected_package_id = "pkg_" + _digest(_json_bytes(unsigned_manifest))
            if package_id != expected_package_id:
                raise UserInputError("migration manifest hash does not match package id")
            raw_entries = manifest.get("objects")
            if not isinstance(raw_entries, list):
                raise UserInputError("migration manifest objects must be an array")
            documents: dict[str, dict[str, object]] = {}
            declared_paths: set[str] = set()
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    raise UserInputError("migration manifest object entry is invalid")
                entry = cast(dict[object, object], raw_entry)
                path = entry.get("path")
                digest = entry.get("sha256")
                size = entry.get("size")
                if (
                    not isinstance(path, str)
                    or not isinstance(digest, str)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                ):
                    raise UserInputError("migration manifest object entry is invalid")
                if path in declared_paths:
                    raise UserInputError("migration manifest repeats an object path")
                declared_paths.add(path)
                content = package.read(path)
                if len(content) != size or _digest(content) != digest:
                    raise UserInputError(f"migration object hash mismatch: {path}")
                value = json.loads(content)
                if not isinstance(value, dict) or not all(
                    isinstance(key, str) for key in value
                ):
                    raise UserInputError(f"migration object is not a JSON object: {path}")
                documents[path] = cast(dict[str, object], value)
            if set(names) != declared_paths | {"manifest.json"}:
                raise UserInputError("migration package entries do not match the manifest")
            if "relationships.json" not in documents:
                raise UserInputError("migration package has no relationships object")
    except KeyError as error:
        raise UserInputError(f"migration package entry is missing: {error.args[0]}") from error
    except (BadZipFile, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UserInputError(f"cannot read migration package: {resolved}") from error
    return _MigrationPackage(manifest, documents, package_id)


def _audit_package_closure(
    package: _MigrationPackage,
    blockers: list[dict[str, object]],
) -> _PackageGraph:
    memories: dict[str, set[int]] = {}
    sources: set[tuple[str, int]] = set()
    knowledge: set[tuple[str, int, str, int, str]] = set()
    evidence: set[tuple[str, int, str, int, str]] = set()

    for path, document in sorted(package.documents.items()):
        if path.startswith("objects/memories/"):
            memory_id = _document_text(document, "memory_id")
            if path != f"objects/memories/{memory_id}.json":
                raise UserInputError(
                    f"migration memory path does not match stable identity: {path}"
                )
            if memory_id in memories:
                raise UserInputError(f"migration repeats memory identity: {memory_id}")
            if document.get("kind") != "canonical-memory":
                raise UserInputError(f"migration memory kind is invalid: {memory_id}")
            for field in (
                "sensitivity",
                "state",
                "created_at",
                "updated_at",
                "canonical_name",
                "capsule_topic",
            ):
                _document_text(document, field)
            current_version = _document_int(document, "current_version", minimum=1)
            versions = _document_list(document, "versions")
            version_numbers: set[int] = set()
            for raw_version in versions:
                if not isinstance(raw_version, dict):
                    raise UserInputError(
                        f"migration memory version is invalid: {memory_id}"
                    )
                version = cast(dict[str, object], raw_version)
                number = _document_int(version, "version", minimum=1)
                if number in version_numbers:
                    raise UserInputError(
                        f"migration repeats memory version: {memory_id}/v{number}"
                    )
                version_numbers.add(number)
                for field in ("content", "action", "created_at"):
                    _document_text(version, field)
                scope = version.get("applicability_scope")
                if scope is not None and not isinstance(scope, str):
                    raise UserInputError(
                        f"migration memory applicability scope is invalid: "
                        f"{memory_id}/v{number}"
                    )
            if current_version not in version_numbers:
                raise UserInputError(
                    f"migration current memory version is missing: {memory_id}"
                )
            names = _document_list(document, "names")
            for raw_name in names:
                if not isinstance(raw_name, dict):
                    raise UserInputError(f"migration memory name is invalid: {memory_id}")
                name = cast(dict[str, object], raw_name)
                for field in ("name", "normalized_name", "name_kind", "created_at"):
                    _document_text(name, field)
            memories[memory_id] = version_numbers
        elif path.startswith("objects/sources/"):
            source_id = _document_text(document, "source_id")
            source_version_number = _document_int(document, "version", minimum=1)
            if path != f"objects/sources/{source_id}-v{source_version_number}.json":
                raise UserInputError(
                    f"migration source path does not match stable identity: {path}"
                )
            identity = (source_id, source_version_number)
            if identity in sources:
                raise UserInputError(
                    f"migration repeats source identity: {source_id}/v{source_version_number}"
                )
            if document.get("kind") != "evidence-source-version":
                raise UserInputError(
                    f"migration source kind is invalid: {source_id}/v{source_version_number}"
                )
            for field in (
                "source_kind",
                "current_locator",
                "source_created_at",
                "content_hash",
                "locator",
                "observed_at",
                "applicability_scope",
                "retention",
            ):
                _document_text(document, field)
            sources.add(identity)

    relationships = package.relationships
    for raw_relation in _document_list(relationships, "knowledge_relationships"):
        if not isinstance(raw_relation, dict):
            raise UserInputError("migration knowledge relationship is invalid")
        relation = cast(dict[str, object], raw_relation)
        typed_relation = (
            _document_text(relation, "from_memory_id"),
            _document_int(relation, "from_version", minimum=1),
            _document_text(relation, "to_memory_id"),
            _document_int(relation, "to_version", minimum=1),
            _document_text(relation, "relationship"),
        )
        if typed_relation in knowledge:
            raise UserInputError("migration repeats a knowledge relationship")
        knowledge.add(typed_relation)
        origin = (typed_relation[0], typed_relation[1])
        target = (typed_relation[2], typed_relation[3])
        if (
            origin[0] not in memories
            or origin[1] not in memories[origin[0]]
            or target[0] not in memories
            or target[1] not in memories[target[0]]
        ):
            blockers.append(
                {
                    "kind": "missing-package-dependency",
                    "path": (
                        f"memory:{origin[0]}/v{origin[1]} -> "
                        f"memory:{target[0]}/v{target[1]}"
                    ),
                    "reason": "knowledge relationship points outside the package closure",
                }
            )

    for raw_relation in _document_list(relationships, "evidence_relationships"):
        if not isinstance(raw_relation, dict):
            raise UserInputError("migration evidence relationship is invalid")
        relation = cast(dict[str, object], raw_relation)
        typed_relation = (
            _document_text(relation, "memory_id"),
            _document_int(relation, "memory_version", minimum=1),
            _document_text(relation, "source_id"),
            _document_int(relation, "source_version", minimum=1),
            _document_text(relation, "relationship"),
        )
        if typed_relation in evidence:
            raise UserInputError("migration repeats an evidence relationship")
        evidence.add(typed_relation)
        memory = (typed_relation[0], typed_relation[1])
        source = (typed_relation[2], typed_relation[3])
        if (
            memory[0] not in memories
            or memory[1] not in memories[memory[0]]
            or source not in sources
        ):
            blockers.append(
                {
                    "kind": "missing-package-evidence",
                    "path": (
                        f"memory:{memory[0]}/v{memory[1]} -> "
                        f"source:{source[0]}/v{source[1]}"
                    ),
                    "reason": "evidence relationship points outside the package closure",
                }
            )

    knowledge_origins = {(item[0], item[1]) for item in knowledge}
    evidence_origins = {(item[0], item[1]) for item in evidence}
    for memory_id, packaged_versions in sorted(memories.items()):
        for packaged_version in sorted(packaged_versions):
            if (memory_id, packaged_version) not in knowledge_origins | evidence_origins:
                blockers.append(
                    {
                        "kind": "unauditable-provenance",
                        "path": (
                            f"memory:{memory_id}/v{packaged_version} -> provenance:missing"
                        ),
                        "reason": (
                            "knowledge version has neither evidence nor a knowledge "
                            "dependency"
                        ),
                    }
                )

    _append_cycle_blockers(knowledge, blockers)
    expected_closure: dict[str, object] = {
        "memory_ids": sorted(memories),
        "memory_versions": [
            {"memory_id": memory_id, "version": version}
            for memory_id, versions in sorted(memories.items())
            for version in sorted(versions)
        ],
        "source_versions": [
            {"source_id": source_id, "version": version}
            for source_id, version in sorted(sources)
        ],
        "knowledge_relationships": [
            {
                "from_memory_id": item[0],
                "from_version": item[1],
                "relationship": item[4],
                "to_memory_id": item[2],
                "to_version": item[3],
            }
            for item in sorted(knowledge)
        ],
        "evidence_relationships": [
            {
                "memory_id": item[0],
                "memory_version": item[1],
                "relationship": item[4],
                "source_id": item[2],
                "source_version": item[3],
            }
            for item in sorted(evidence)
        ],
    }
    if package.manifest.get("closure") != expected_closure:
        blockers.append(
            {
                "kind": "manifest-closure-mismatch",
                "path": "manifest:closure",
                "reason": "manifest closure does not match packaged objects and relations",
            }
        )
    selected = package.manifest.get("selected_memory_ids")
    if (
        not isinstance(selected, list)
        or not all(isinstance(value, str) for value in selected)
        or any(value not in memories for value in selected)
    ):
        blockers.append(
            {
                "kind": "missing-selected-memory",
                "path": "manifest:selected_memory_ids",
                "reason": "selected migration root is absent from the packaged closure",
            }
        )
    return _PackageGraph(memories, sources, knowledge, evidence)


def _classify_import(
    connection: sqlite3.Connection,
    package: _MigrationPackage,
) -> dict[str, object]:
    new_sources: list[dict[str, object]] = []
    exact_sources: list[dict[str, object]] = []
    reused_source_hashes: list[dict[str, object]] = []
    new_memories: list[str] = []
    exact_memories: list[str] = []
    updated_memories: list[str] = []
    reconciled_memories: list[str] = []
    remapped_memory_versions: list[dict[str, object]] = []
    conflict_memories: list[str] = []
    blockers: list[dict[str, object]] = []
    graph = _audit_package_closure(package, blockers)

    for document in package.source_documents:
        source_id = _document_text(document, "source_id")
        version = _document_int(document, "version", minimum=1)
        row = connection.execute(
            """
            SELECT source.source_kind, source.current_locator, source.created_at,
                   version.content_hash, version.locator, version.observed_at,
                   version.applicability_scope, version.retention
            FROM evidence_source_versions AS version
            JOIN evidence_sources AS source ON source.source_id = version.source_id
            WHERE version.source_id = ? AND version.version = ?
            """,
            (source_id, version),
        ).fetchone()
        expected = (
            _document_text(document, "source_kind"),
            _document_text(document, "current_locator"),
            _document_text(document, "source_created_at"),
            _document_text(document, "content_hash"),
            _document_text(document, "locator"),
            _document_text(document, "observed_at"),
            _document_text(document, "applicability_scope"),
            _document_text(document, "retention"),
        )
        identity = {"source_id": source_id, "version": version}
        if row is not None:
            if row == expected:
                exact_sources.append(identity)
            else:
                blockers.append(
                    {
                        "kind": "source-identity-conflict",
                        "path": f"source:{source_id}/v{version}",
                        "reason": "target source identity has different receipt content",
                    }
                )
            continue
        locator = _document_text(document, "current_locator")
        locator_owner = connection.execute(
            "SELECT source_id FROM evidence_sources WHERE current_locator = ?",
            (locator,),
        ).fetchone()
        if locator_owner is not None and locator_owner != (source_id,):
            blockers.append(
                {
                    "kind": "source-locator-conflict",
                    "path": f"source:{source_id}/v{version} -> locator:{locator}",
                    "reason": "target locator belongs to another stable source identity",
                }
            )
        else:
            new_sources.append(identity)
            reused = connection.execute(
                """
                SELECT source_id, version FROM evidence_source_versions
                WHERE content_hash = ? ORDER BY source_id, version LIMIT 1
                """,
                (_document_text(document, "content_hash"),),
            ).fetchone()
            if (
                reused is not None
                and isinstance(reused[0], str)
                and isinstance(reused[1], int)
            ):
                reused_source_hashes.append(
                    {
                        "incoming_source_id": source_id,
                        "incoming_version": version,
                        "existing_source_id": reused[0],
                        "existing_version": reused[1],
                        "content_hash": _document_text(document, "content_hash"),
                    }
                )

    for document in package.memory_documents:
        memory_id = _document_text(document, "memory_id")
        exists = connection.execute(
            "SELECT 1 FROM canonical_memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if exists is None:
            new_memories.append(memory_id)
            continue
        local_document = _memory_document(connection, memory_id)
        if local_document == document:
            exact_memories.append(memory_id)
        elif _is_incremental_extension(local_document, document):
            updated_memories.append(memory_id)
        elif (
            target_version := _reconciled_current_version(local_document, document)
        ) is not None:
            incoming_version = _document_int(document, "current_version", minimum=1)
            reconciled_memories.append(memory_id)
            remapped_memory_versions.append(
                {
                    "memory_id": memory_id,
                    "incoming_version": incoming_version,
                    "target_version": target_version,
                }
            )
        else:
            conflict_memories.append(memory_id)

    conflict_set = set(conflict_memories)
    dependency_conflicts: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for origin_id, origin_version, target_id, target_version, relationship in sorted(
            graph.knowledge
        ):
            target_path = dependency_conflicts.get(target_id)
            if target_id in conflict_set:
                target_path = f"memory:{target_id}/v{target_version} -> target:conflict"
            if (
                target_path is not None
                and origin_id not in conflict_set
                and origin_id not in dependency_conflicts
            ):
                dependency_conflicts[origin_id] = (
                    f"memory:{origin_id}/v{origin_version} -[{relationship}]-> "
                    f"{target_path}"
                )
                changed = True
    for memory_id, path in sorted(dependency_conflicts.items()):
        blockers.append(
            {
                "kind": "dependency-conflict",
                "path": path,
                "reason": (
                    "dependent knowledge cannot be imported until its target conflict "
                    "is reviewed"
                ),
            }
        )
        if memory_id in new_memories:
            new_memories.remove(memory_id)
        if memory_id in updated_memories:
            updated_memories.remove(memory_id)
        if memory_id in exact_memories:
            exact_memories.remove(memory_id)
    return {
        "changes": {
            "new_memory_ids": sorted(new_memories),
            "updated_memory_ids": sorted(updated_memories),
            "reconciled_memory_ids": sorted(reconciled_memories),
            "remapped_memory_versions": sorted(
                remapped_memory_versions,
                key=lambda item: (
                    cast(str, item["memory_id"]),
                    cast(int, item["incoming_version"]),
                ),
            ),
            "exact_memory_ids": sorted(exact_memories),
            "conflict_memory_ids": sorted(conflict_memories),
            "new_source_versions": sorted(
                new_sources, key=lambda item: (cast(str, item["source_id"]), cast(int, item["version"]))
            ),
            "exact_source_versions": sorted(
                exact_sources,
                key=lambda item: (cast(str, item["source_id"]), cast(int, item["version"])),
            ),
            "reused_source_hashes": sorted(
                reused_source_hashes,
                key=lambda item: (
                    cast(str, item["incoming_source_id"]),
                    cast(int, item["incoming_version"]),
                ),
            ),
        },
        "blockers": blockers,
    }


def _is_incremental_extension(
    local: dict[str, object],
    incoming: dict[str, object],
) -> bool:
    stable_fields = (
        "memory_id",
        "sensitivity",
        "state",
        "previous_live_state",
        "created_at",
        "canonical_name",
        "capsule_topic",
        "names",
    )
    if any(local.get(field) != incoming.get(field) for field in stable_fields):
        return False
    local_current = local.get("current_version")
    incoming_current = incoming.get("current_version")
    if (
        not isinstance(local_current, int)
        or not isinstance(incoming_current, int)
        or incoming_current <= local_current
    ):
        return False
    local_versions = local.get("versions")
    incoming_versions = incoming.get("versions")
    if (
        not isinstance(local_versions, list)
        or not isinstance(incoming_versions, list)
        or len(incoming_versions) <= len(local_versions)
    ):
        return False
    immutable_fields = (
        "version",
        "content",
        "applicability_scope",
        "action",
        "change_reason",
        "created_at",
    )
    for local_value, incoming_value in zip(local_versions, incoming_versions, strict=False):
        if not isinstance(local_value, dict) or not isinstance(incoming_value, dict):
            return False
        if any(
            local_value.get(field) != incoming_value.get(field)
            for field in immutable_fields
        ):
            return False
    return True


def _reconciled_current_version(
    local: dict[str, object],
    incoming: dict[str, object],
) -> int | None:
    local_current = local.get("current_version")
    incoming_current = incoming.get("current_version")
    if (
        not isinstance(local_current, int)
        or not isinstance(incoming_current, int)
        or local_current <= incoming_current
        or local.get("canonical_name") != incoming.get("canonical_name")
        or local.get("sensitivity") != incoming.get("sensitivity")
    ):
        return None
    local_version = _find_version_document(local, local_current)
    incoming_version = _find_version_document(incoming, incoming_current)
    if local_version is None or incoming_version is None:
        return None
    semantic_fields = ("content", "applicability_scope")
    if any(
        local_version.get(field) != incoming_version.get(field)
        for field in semantic_fields
    ):
        return None
    return local_current


def _find_version_document(
    document: dict[str, object],
    version: int,
) -> dict[str, object] | None:
    values = document.get("versions")
    if not isinstance(values, list):
        return None
    return next(
        (
            cast(dict[str, object], value)
            for value in values
            if isinstance(value, dict) and value.get("version") == version
        ),
        None,
    )


def _memory_document(
    connection: sqlite3.Connection,
    memory_id: str,
) -> dict[str, object]:
    closure: dict[str, object] = {
        "memory_ids": [memory_id],
        "source_versions": [],
        "knowledge_relationships": [],
        "evidence_relationships": [],
    }
    documents = _package_documents(connection, closure)
    return cast(
        dict[str, object],
        json.loads(documents[f"objects/memories/{memory_id}.json"]),
    )


def _stage_import(
    database_path: Path,
    package: _MigrationPackage,
    *,
    changes: dict[str, object],
    checkpoint_version: int,
    request_hash: str,
    idempotency_key: str,
    entrance: str,
    occurred_at: str,
) -> tuple[bytes, list[str]]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".migration-import.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for document in package.source_documents:
                identity = {
                    "source_id": _document_text(document, "source_id"),
                    "version": _document_int(document, "version", minimum=1),
                }
                if identity not in cast(list[dict[str, object]], changes["new_source_versions"]):
                    continue
                source_id = cast(str, identity["source_id"])
                version = cast(int, identity["version"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence_sources
                        (source_id, source_kind, current_locator, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        _document_text(document, "source_kind"),
                        _document_text(document, "current_locator"),
                        _document_text(document, "source_created_at"),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO evidence_source_versions
                        (source_id, version, content_hash, locator, observed_at,
                         applicability_scope, retention)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        version,
                        _document_text(document, "content_hash"),
                        _document_text(document, "locator"),
                        _document_text(document, "observed_at"),
                        _document_text(document, "applicability_scope"),
                        _document_text(document, "retention"),
                    ),
                )
            for document in package.memory_documents:
                memory_id = _document_text(document, "memory_id")
                if memory_id in cast(list[str], changes["new_memory_ids"]):
                    _insert_memory(connection, package.package_id, document, occurred_at)
                elif memory_id in cast(list[str], changes["updated_memory_ids"]):
                    _extend_memory(connection, document, occurred_at)
                elif memory_id in cast(list[str], changes["reconciled_memory_ids"]):
                    _reconcile_memory(connection, document, occurred_at)
            included_memory_ids = set(cast(list[str], changes["new_memory_ids"]))
            included_memory_ids.update(cast(list[str], changes["updated_memory_ids"]))
            included_memory_ids.update(
                cast(list[str], changes["reconciled_memory_ids"])
            )
            included_memory_ids.update(cast(list[str], changes["exact_memory_ids"]))
            version_remaps = {
                (
                    cast(str, item["memory_id"]),
                    cast(int, item["incoming_version"]),
                ): cast(int, item["target_version"])
                for item in cast(
                    list[dict[str, object]], changes["remapped_memory_versions"]
                )
            }
            _insert_package_relationships(
                connection,
                package,
                included_memory_ids=included_memory_ids,
                version_remaps=version_remaps,
                occurred_at=occurred_at,
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, subject_id, proposal_id,
                     before_version, after_version, entrance, result_hash)
                VALUES (?, 'migration.import', ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    f"aud_{uuid.uuid4().hex}",
                    occurred_at,
                    package.package_id,
                    checkpoint_version - 1,
                    checkpoint_version,
                    entrance,
                    package.package_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotent_writes
                    (operation, idempotency_key, subject_id, request_hash,
                     result_hash, created_at)
                VALUES ('migration.import', ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    package.package_id,
                    request_hash,
                    package.package_id,
                    occurred_at,
                ),
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("migration import broke canonical references")
        conflict_proposal_ids = _append_conflict_proposals(
            temporary_path,
            package,
            conflict_memory_ids=cast(list[str], changes["conflict_memory_ids"]),
        )
        return temporary_path.read_bytes(), conflict_proposal_ids
    except sqlite3.Error as error:
        raise IntegrityError("cannot stage the migration import") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _stage_conflict_only(
    database_path: Path,
    package: _MigrationPackage,
    *,
    conflict_memory_ids: list[str],
) -> tuple[bytes, list[str]]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".migration-conflict.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        proposal_ids = _append_conflict_proposals(
            temporary_path,
            package,
            conflict_memory_ids=conflict_memory_ids,
        )
        return temporary_path.read_bytes(), proposal_ids
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _append_conflict_proposals(
    database_path: Path,
    package: _MigrationPackage,
    *,
    conflict_memory_ids: list[str],
) -> list[str]:
    proposal_ids: list[str] = []
    documents = {
        _document_text(document, "memory_id"): document
        for document in package.memory_documents
    }
    for memory_id in conflict_memory_ids:
        document = documents.get(memory_id)
        if document is None:
            raise IntegrityError(f"migration conflict object is missing: {memory_id}")
        with closing(sqlite3.connect(database_path)) as connection:
            local_row = connection.execute(
                """
                SELECT current_version FROM canonical_memories
                WHERE memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
        if local_row is None or not isinstance(local_row[0], int):
            raise IntegrityError("migration conflict target disappeared")
        payload = _migration_conflict_payload(
            package,
            document,
            expected_version=local_row[0],
        )
        staged_database, submission = stage_review_proposal(
            database_path,
            payload,
            idempotency_key=f"migration-conflict:{package.package_id}:{memory_id}",
        )
        database_path.write_bytes(staged_database)
        proposal_ids.append(submission.proposal.proposal_id)
    return proposal_ids


def _migration_conflict_payload(
    package: _MigrationPackage,
    document: dict[str, object],
    *,
    expected_version: int,
) -> ReviewProposalInput:
    memory_id = _document_text(document, "memory_id")
    incoming_version = _document_int(document, "current_version", minimum=1)
    versions = _document_list(document, "versions")
    current = next(
        (
            cast(dict[str, object], value)
            for value in versions
            if isinstance(value, dict) and value.get("version") == incoming_version
        ),
        None,
    )
    if current is None:
        raise UserInputError(f"migration current memory version is missing: {memory_id}")
    source_documents = {
        (
            _document_text(source, "source_id"),
            _document_int(source, "version", minimum=1),
        ): source
        for source in package.source_documents
    }
    supporting_evidence: list[dict[str, object]] = []
    for value in _document_list(package.relationships, "evidence_relationships"):
        if not isinstance(value, dict):
            raise UserInputError("migration evidence relationship is invalid")
        relation = cast(dict[str, object], value)
        if (
            relation.get("memory_id") != memory_id
            or relation.get("memory_version") != incoming_version
        ):
            continue
        source_key = (
            _document_text(relation, "source_id"),
            _document_int(relation, "source_version", minimum=1),
        )
        source = source_documents.get(source_key)
        if source is None:
            raise UserInputError("migration conflict evidence is missing")
        supporting_evidence.append(
            {
                "kind": "source",
                "source_id": source_key[0],
                "source_version": source_key[1],
                "locator": _document_text(source, "locator"),
                "content_hash": _document_text(source, "content_hash"),
            }
        )
    if not supporting_evidence:
        supporting_evidence.append(
            {
                "kind": "migration-package",
                "reference": package.package_id,
            }
        )
    canonical_name = _document_text(document, "canonical_name")
    return ReviewProposalInput.from_data(
        {
            "title": f"Imported conflict: {canonical_name}",
            "content": _document_text(current, "content"),
            "intent": "integrate",
            "formation": "derived",
            "priority": "blocking",
            "applicability_scope": (
                current.get("applicability_scope")
                if isinstance(current.get("applicability_scope"), str)
                else "migration import"
            ),
            "approval_effect": {
                "type": "revise_canonical_memory",
                "canonical_name": canonical_name,
                "personal_cognition": False,
            },
            "target": {
                "memory_id": memory_id,
                "expected_version": expected_version,
            },
            "supporting_evidence": supporting_evidence,
            "opposing_evidence": [
                {
                    "kind": "target-memory",
                    "memory_id": memory_id,
                    "version": expected_version,
                }
            ],
            "dependencies": [],
            "context_coverage": [
                f"migration package {package.package_id}",
                f"incoming memory version {incoming_version}",
            ],
            "blind_spots": ["source and target revisions diverged"],
            "near_proposal_ids": [],
            "conflict_proposal_ids": [],
            "sensitivity": _document_text(document, "sensitivity"),
            "evidence_retention": "receipt",
            "migration_restrictions": [],
        }
    )


def _insert_memory_version(
    connection: sqlite3.Connection,
    *,
    memory_id: str,
    capsule_id: str,
    document: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO canonical_memory_versions
            (memory_id, version, content, applicability_scope, capsule_id,
             action, change_reason, created_at, superseded_at,
             supersession_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            _document_int(document, "version", minimum=1),
            _document_text(document, "content"),
            document.get("applicability_scope"),
            capsule_id,
            _document_text(document, "action"),
            document.get("change_reason"),
            _document_text(document, "created_at"),
            document.get("superseded_at"),
            document.get("supersession_reason"),
        ),
    )


def _replace_fts_projection(
    connection: sqlite3.Connection,
    *,
    memory_id: str,
    capsule_id: str,
    canonical_name: str,
    content: str,
    applicability_scope: str,
    replace_existing: bool,
) -> None:
    if replace_existing:
        connection.execute(
            "DELETE FROM canonical_memory_fts WHERE memory_id = ?", (memory_id,)
        )
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
            content,
            applicability_scope,
            " ".join(
                sorted(
                    lexical_terms(
                        f"{canonical_name} {content} {applicability_scope}"
                    )
                )
            ),
        ),
    )


def _insert_memory(
    connection: sqlite3.Connection,
    package_id: str,
    document: dict[str, object],
    occurred_at: str,
) -> None:
    memory_id = _document_text(document, "memory_id")
    current_version = _document_int(document, "current_version", minimum=1)
    versions = _document_list(document, "versions")
    current_document: dict[str, object] | None = None
    for value in versions:
        if not isinstance(value, dict):
            raise UserInputError(f"migration memory version is invalid: {memory_id}")
        version_document = cast(dict[str, object], value)
        if _document_int(version_document, "version", minimum=1) == current_version:
            current_document = version_document
    if current_document is None:
        raise UserInputError(f"migration current memory version is missing: {memory_id}")
    topic = _document_text(document, "capsule_topic")
    capsule_id = _ensure_import_capsule(connection, package_id, topic, occurred_at)
    current_content = _document_text(current_document, "content", allow_empty=False)
    connection.execute(
        """
        INSERT INTO canonical_memories
            (memory_id, content, current_version, sensitivity, state,
             previous_live_state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            current_content,
            current_version,
            _document_text(document, "sensitivity"),
            _document_text(document, "state"),
            document.get("previous_live_state"),
            _document_text(document, "created_at"),
            _document_text(document, "updated_at"),
        ),
    )
    for value in versions:
        version_document = cast(dict[str, object], value)
        _insert_memory_version(
            connection,
            memory_id=memory_id,
            capsule_id=capsule_id,
            document=version_document,
        )
    canonical_name = _document_text(document, "canonical_name")
    connection.execute(
        """
        INSERT INTO knowledge_dictionary
            (memory_id, canonical_name, normalized_name, current_version,
             primary_capsule_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            canonical_name,
            " ".join(canonical_name.casefold().split()),
            current_version,
            capsule_id,
        ),
    )
    for value in _document_list(document, "names"):
        if not isinstance(value, dict):
            raise UserInputError(f"migration memory name is invalid: {memory_id}")
        name = cast(dict[str, object], value)
        connection.execute(
            """
            INSERT INTO memory_names
                (memory_id, name, normalized_name, name_kind, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                _document_text(name, "name"),
                _document_text(name, "normalized_name"),
                _document_text(name, "name_kind"),
                _document_text(name, "created_at"),
            ),
        )
    scope = current_document.get("applicability_scope")
    scope_text = scope if isinstance(scope, str) else ""
    _replace_fts_projection(
        connection,
        memory_id=memory_id,
        capsule_id=capsule_id,
        canonical_name=canonical_name,
        content=current_content,
        applicability_scope=scope_text,
        replace_existing=False,
    )
    connection.execute(
        """
        UPDATE knowledge_capsules
        SET body_bytes = body_bytes + ?,
            memory_record_count = memory_record_count + 1,
            updated_at = ?
        WHERE capsule_id = ?
        """,
        (len(current_content.encode("utf-8")), occurred_at, capsule_id),
    )


def _extend_memory(
    connection: sqlite3.Connection,
    document: dict[str, object],
    occurred_at: str,
) -> None:
    memory_id = _document_text(document, "memory_id")
    row = connection.execute(
        """
        SELECT memory.current_version, version.content,
               dictionary.primary_capsule_id
        FROM canonical_memories AS memory
        JOIN canonical_memory_versions AS version
          ON version.memory_id = memory.memory_id
         AND version.version = memory.current_version
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = memory.memory_id
        WHERE memory.memory_id = ?
        """,
        (memory_id,),
    ).fetchone()
    if (
        row is None
        or not isinstance(row[0], int)
        or not isinstance(row[1], str)
        or not isinstance(row[2], str)
    ):
        raise IntegrityError("migration update target is invalid")
    previous_version = row[0]
    previous_content = row[1]
    capsule_id = row[2]
    incoming_version = _document_int(document, "current_version", minimum=1)
    versions = _document_list(document, "versions")
    current_document: dict[str, object] | None = None
    for value in versions:
        if not isinstance(value, dict):
            raise UserInputError(f"migration memory version is invalid: {memory_id}")
        version_document = cast(dict[str, object], value)
        version = _document_int(version_document, "version", minimum=1)
        if version <= previous_version:
            connection.execute(
                """
                UPDATE canonical_memory_versions
                SET superseded_at = ?, supersession_reason = ?
                WHERE memory_id = ? AND version = ?
                """,
                (
                    version_document.get("superseded_at"),
                    version_document.get("supersession_reason"),
                    memory_id,
                    version,
                ),
            )
            continue
        _insert_memory_version(
            connection,
            memory_id=memory_id,
            capsule_id=capsule_id,
            document=version_document,
        )
        if version == incoming_version:
            current_document = version_document
    if current_document is None:
        raise UserInputError(f"migration update current version is missing: {memory_id}")
    current_content = _document_text(current_document, "content")
    canonical_name = _document_text(document, "canonical_name")
    connection.execute(
        """
        UPDATE canonical_memories
        SET content = ?, current_version = ?, sensitivity = ?, state = ?,
            previous_live_state = ?, updated_at = ?
        WHERE memory_id = ? AND current_version = ?
        """,
        (
            current_content,
            incoming_version,
            _document_text(document, "sensitivity"),
            _document_text(document, "state"),
            document.get("previous_live_state"),
            _document_text(document, "updated_at"),
            memory_id,
            previous_version,
        ),
    )
    connection.execute(
        """
        UPDATE knowledge_dictionary
        SET canonical_name = ?, normalized_name = ?, current_version = ?
        WHERE memory_id = ? AND current_version = ?
        """,
        (
            canonical_name,
            " ".join(canonical_name.casefold().split()),
            incoming_version,
            memory_id,
            previous_version,
        ),
    )
    scope = current_document.get("applicability_scope")
    scope_text = scope if isinstance(scope, str) else ""
    _replace_fts_projection(
        connection,
        memory_id=memory_id,
        capsule_id=capsule_id,
        canonical_name=canonical_name,
        content=current_content,
        applicability_scope=scope_text,
        replace_existing=True,
    )
    connection.execute(
        """
        UPDATE knowledge_capsules
        SET body_bytes = body_bytes - ? + ?, updated_at = ?
        WHERE capsule_id = ?
        """,
        (
            len(previous_content.encode("utf-8")),
            len(current_content.encode("utf-8")),
            occurred_at,
            capsule_id,
        ),
    )


def _reconcile_memory(
    connection: sqlite3.Connection,
    document: dict[str, object],
    occurred_at: str,
) -> None:
    memory_id = _document_text(document, "memory_id")
    row = connection.execute(
        """
        SELECT memory.current_version, version.content,
               version.applicability_scope, dictionary.primary_capsule_id
        FROM canonical_memories AS memory
        JOIN canonical_memory_versions AS version
          ON version.memory_id = memory.memory_id
         AND version.version = memory.current_version
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = memory.memory_id
        WHERE memory.memory_id = ?
        """,
        (memory_id,),
    ).fetchone()
    if (
        row is None
        or not isinstance(row[0], int)
        or not isinstance(row[1], str)
        or (row[2] is not None and not isinstance(row[2], str))
        or not isinstance(row[3], str)
    ):
        raise IntegrityError("reconciled migration target is invalid")
    incoming_current = _document_int(document, "current_version", minimum=1)
    current_document = _find_version_document(document, incoming_current)
    if current_document is None:
        raise UserInputError(f"migration current memory version is missing: {memory_id}")
    if (
        _document_text(current_document, "content") != row[1]
        or current_document.get("applicability_scope") != row[2]
    ):
        raise IntegrityError("reconciled migration target changed before import")
    canonical_name = _document_text(document, "canonical_name")
    connection.execute(
        """
        UPDATE canonical_memories
        SET sensitivity = ?, state = ?, previous_live_state = ?, updated_at = ?
        WHERE memory_id = ? AND current_version = ?
        """,
        (
            _document_text(document, "sensitivity"),
            _document_text(document, "state"),
            document.get("previous_live_state"),
            occurred_at,
            memory_id,
            row[0],
        ),
    )
    connection.execute(
        """
        UPDATE knowledge_dictionary
        SET canonical_name = ?, normalized_name = ?
        WHERE memory_id = ? AND current_version = ?
        """,
        (
            canonical_name,
            " ".join(canonical_name.casefold().split()),
            memory_id,
            row[0],
        ),
    )
    for raw_name in _document_list(document, "names"):
        if not isinstance(raw_name, dict):
            raise UserInputError(f"migration memory name is invalid: {memory_id}")
        name = cast(dict[str, object], raw_name)
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_names
                (memory_id, name, normalized_name, name_kind, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                _document_text(name, "name"),
                _document_text(name, "normalized_name"),
                _document_text(name, "name_kind"),
                _document_text(name, "created_at"),
            ),
        )
    scope = row[2] if isinstance(row[2], str) else ""
    _replace_fts_projection(
        connection,
        memory_id=memory_id,
        capsule_id=row[3],
        canonical_name=canonical_name,
        content=row[1],
        applicability_scope=scope,
        replace_existing=True,
    )


def _ensure_import_capsule(
    connection: sqlite3.Connection,
    package_id: str,
    topic: str,
    occurred_at: str,
) -> str:
    normalized_topic = " ".join(topic.casefold().split())
    row = connection.execute(
        """
        SELECT capsule.capsule_id
        FROM knowledge_capsules AS capsule
        JOIN capsule_partitions AS placement
          ON placement.capsule_id = capsule.capsule_id
        JOIN knowledge_partitions AS partition
          ON partition.partition_id = placement.partition_id
        WHERE partition.normalized_topic = ?
        ORDER BY capsule.capsule_id LIMIT 1
        """,
        (normalized_topic,),
    ).fetchone()
    if row is not None and isinstance(row[0], str):
        return row[0]
    root = connection.execute(
        "SELECT partition_id FROM knowledge_partitions WHERE node_kind = 'root'"
    ).fetchone()
    if root is None:
        connection.execute(
            """
            INSERT INTO knowledge_partitions
                (partition_id, parent_partition_id, node_kind, topic,
                 normalized_topic)
            VALUES ('prt_root', NULL, 'root', 'All knowledge', 'all knowledge')
            """
        )
        root = ("prt_root",)
    if not isinstance(root[0], str):
        raise IntegrityError("target knowledge partition root is invalid")
    suffix = _digest(f"{package_id}:{normalized_topic}".encode("utf-8"))[:32]
    partition_id = f"part_{suffix}"
    capsule_id = f"cap_{suffix}"
    connection.execute(
        """
        INSERT INTO knowledge_partitions
            (partition_id, parent_partition_id, node_kind, topic, normalized_topic)
        VALUES (?, ?, 'leaf', ?, ?)
        """,
        (partition_id, root[0], topic, normalized_topic),
    )
    connection.execute(
        """
        INSERT INTO knowledge_capsules
            (capsule_id, topic, body_bytes, memory_record_count,
             structural_version, created_at, updated_at)
        VALUES (?, ?, 0, 0, 1, ?, ?)
        """,
        (capsule_id, topic, occurred_at, occurred_at),
    )
    connection.execute(
        "INSERT INTO capsule_partitions (capsule_id, partition_id) VALUES (?, ?)",
        (capsule_id, partition_id),
    )
    return capsule_id


def _insert_package_relationships(
    connection: sqlite3.Connection,
    package: _MigrationPackage,
    *,
    included_memory_ids: set[str],
    version_remaps: dict[tuple[str, int], int],
    occurred_at: str,
) -> None:
    for value in _document_list(package.relationships, "knowledge_relationships"):
        relation = cast(dict[str, object], value)
        from_id = _document_text(relation, "from_memory_id")
        to_id = _document_text(relation, "to_memory_id")
        if from_id not in included_memory_ids or to_id not in included_memory_ids:
            continue
        from_version = _document_int(relation, "from_version", minimum=1)
        to_version = _document_int(relation, "to_version", minimum=1)
        connection.execute(
            """
            INSERT OR IGNORE INTO canonical_memory_dependencies
                (memory_id, version, depends_on_memory_id, depends_on_version,
                 relationship, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                from_id,
                version_remaps.get((from_id, from_version), from_version),
                to_id,
                version_remaps.get((to_id, to_version), to_version),
                _document_text(relation, "relationship"),
                occurred_at,
            ),
        )
    for value in _document_list(package.relationships, "evidence_relationships"):
        relation = cast(dict[str, object], value)
        memory_id = _document_text(relation, "memory_id")
        if memory_id not in included_memory_ids:
            continue
        memory_version = _document_int(relation, "memory_version", minimum=1)
        connection.execute(
            """
            INSERT OR IGNORE INTO canonical_memory_version_evidence
                (memory_id, version, source_id, source_version, relationship)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                version_remaps.get((memory_id, memory_version), memory_version),
                _document_text(relation, "source_id"),
                _document_int(relation, "source_version", minimum=1),
                _document_text(relation, "relationship"),
            ),
        )


def _import_checkpoint(
    connection: sqlite3.Connection,
) -> tuple[int, set[str]]:
    rows = connection.execute(
        """
        SELECT result_hash FROM audit_events
        WHERE event_type = 'migration.import'
        ORDER BY occurred_at, event_id
        """
    ).fetchall()
    if any(not isinstance(row[0], str) for row in rows):
        raise IntegrityError("migration import checkpoint is invalid")
    return len(rows), {cast(str, row[0]) for row in rows}


def _document_text(
    document: dict[str, object],
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = document.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise UserInputError(f"migration object field is invalid: {field}")
    return value


def _document_int(
    document: dict[str, object],
    field: str,
    *,
    minimum: int,
) -> int:
    value = document.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise UserInputError(f"migration object field is invalid: {field}")
    return value


def _document_list(
    document: dict[str, object],
    field: str,
) -> list[object]:
    value = document.get(field)
    if not isinstance(value, list):
        raise UserInputError(f"migration object field is invalid: {field}")
    return cast(list[object], value)


def _memory_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise UserInputError("migration requires at least one memory id")
    normalized = tuple(_text("memory id", value, maximum=200) for value in values)
    if len(normalized) != len(set(normalized)):
        raise UserInputError("migration memory ids must not contain duplicates")
    return tuple(sorted(normalized))


def _text(label: str, value: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise UserInputError(f"{label} must contain 1 to {maximum} characters")
    return normalized


def _string_list(value: object, label: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError as error:
        raise IntegrityError(f"{label} are invalid") from error
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise IntegrityError(f"{label} are invalid")
    return tuple(cast(list[str], decoded))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_hash(value: object) -> str:
    return "sha256:" + _digest(_json_bytes(value))
