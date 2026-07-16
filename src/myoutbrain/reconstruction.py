from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from myoutbrain.vault import KnowledgeNoteSnapshot, VaultIntegrityError, scan_knowledge_notes


SCHEMA_VERSION = 1


class ReconstructionError(Exception):
    """Raised when permanent knowledge cannot produce a valid runtime projection."""


class RuntimeProjectionError(Exception):
    """Raised when an active runtime projection is malformed."""


class RuntimeProjectionUnavailable(Exception):
    """Raised when runtime storage was deleted and must be rebuilt."""


@dataclass(frozen=True)
class RebuildResult:
    source_count: int
    insight_count: int
    cognition_count: int
    supersession_count: int


@dataclass(frozen=True)
class ProjectedSource:
    source_id: str
    sensitivity: str
    text: str


@dataclass(frozen=True)
class _VerifiedSource:
    source_id: str
    sensitivity: str
    record_path: Path
    object_reference: str
    text: str


class RuntimeProjectionReader:
    def __init__(self, root: Path) -> None:
        self._root = root

    def source(self, source_id: str) -> ProjectedSource | None:
        runtime_root = self._root / "runtime"
        if not runtime_root.is_dir():
            raise RuntimeProjectionUnavailable(
                "runtime state is unavailable; run myoutbrain rebuild"
            )
        pointer_path = runtime_root / "active-projection.json"
        if not pointer_path.is_file():
            return None
        pointer = _read_json_object(pointer_path, "runtime projection pointer")
        if pointer.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeProjectionError(
                f"runtime projection pointer has invalid schema: {pointer_path}"
            )
        relative_projection = pointer.get("projection")
        if not isinstance(relative_projection, str):
            raise RuntimeProjectionError(
                f"runtime projection pointer has invalid target: {pointer_path}"
            )
        projection_path = (runtime_root / relative_projection).resolve()
        projections_root = (runtime_root / "projections").resolve()
        if not projection_path.is_relative_to(projections_root):
            raise RuntimeProjectionError(
                f"runtime projection pointer escapes its root: {pointer_path}"
            )
        catalog_path = projection_path / "catalog.json"
        documents_path = projection_path / "indexes" / "fulltext" / "documents.json"
        catalog = _read_json_object(catalog_path, "runtime catalog")
        documents = _read_json_object(documents_path, "runtime fulltext projection")
        knowledge = catalog.get("knowledge")
        projected_documents = documents.get("documents")
        if (
            catalog.get("schema_version") != SCHEMA_VERSION
            or not isinstance(knowledge, list)
            or documents.get("schema_version") != SCHEMA_VERSION
            or not isinstance(projected_documents, list)
        ):
            raise RuntimeProjectionError(
                f"runtime projection has invalid schema: {projection_path}"
            )
        catalog_matches = [
            entry
            for entry in knowledge
            if isinstance(entry, dict)
            and entry.get("id") == source_id
            and entry.get("kind") == "source"
        ]
        document_matches = [
            entry
            for entry in projected_documents
            if isinstance(entry, dict)
            and entry.get("id") == source_id
            and entry.get("kind") == "source"
        ]
        if not catalog_matches and not document_matches:
            return None
        if len(catalog_matches) != 1 or len(document_matches) != 1:
            raise RuntimeProjectionError(
                f"runtime projection has inconsistent source identity: {source_id}"
            )
        sensitivity = catalog_matches[0].get("sensitivity")
        text = document_matches[0].get("text")
        if sensitivity not in ("local-only", "cloud-allowed") or not isinstance(text, str):
            raise RuntimeProjectionError(
                f"runtime projection has invalid source fields: {source_id}"
            )
        digest = source_id.removeprefix("src_")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise RuntimeProjectionError(
                f"runtime fulltext content does not match source identity: {source_id}"
            )
        return ProjectedSource(
            source_id=source_id,
            sensitivity=sensitivity,
            text=text,
        )


class RuntimeReconstructor:
    """Builds, validates, and atomically activates runtime projections."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def rebuild(self) -> RebuildResult:
        sources = self._scan_sources()
        try:
            knowledge_notes = scan_knowledge_notes(self._root / "vault")
        except VaultIntegrityError as error:
            raise ReconstructionError(str(error)) from error
        source_ids = {source.source_id for source in sources}
        notes_by_id = {note.knowledge_id: note for note in knowledge_notes}
        supersession_pairs = self._validate_knowledge(
            knowledge_notes,
            notes_by_id,
            source_ids,
        )
        self._validate_events(source_ids, notes_by_id, supersession_pairs)
        catalog, documents = self._build_projection(sources, knowledge_notes)
        self._activate(catalog, documents)
        return RebuildResult(
            source_count=len(sources),
            insight_count=sum(note.kind == "insight" for note in knowledge_notes),
            cognition_count=sum(note.kind == "cognition" for note in knowledge_notes),
            supersession_count=len(supersession_pairs),
        )

    def _scan_sources(self) -> tuple[_VerifiedSource, ...]:
        records_root = self._root / "store" / "records"
        sources: list[_VerifiedSource] = []
        for record_path in sorted(records_root.glob("*.json")):
            source_id = record_path.stem
            digest = source_id.removeprefix("src_")
            if re.fullmatch(r"src_[0-9a-f]{64}", source_id) is None:
                raise ReconstructionError(f"invalid source record identity: {record_path}")
            record = _read_json_object(record_path, "source record")
            object_reference = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
            if (
                record.get("schema_version") != SCHEMA_VERSION
                or record.get("id") != source_id
                or record.get("kind") != "source"
                or record.get("state") != "active"
                or record.get("content_hash") != f"sha256:{digest}"
                or record.get("object") != object_reference
            ):
                raise ReconstructionError(f"invalid source record: {record_path}")
            sensitivity = record.get("sensitivity")
            created_at = record.get("created_at")
            origins = record.get("origins")
            if sensitivity not in ("local-only", "cloud-allowed"):
                raise ReconstructionError(f"invalid source sensitivity: {record_path}")
            if not isinstance(created_at, str):
                raise ReconstructionError(f"invalid source creation time: {record_path}")
            _validate_timestamp(created_at, f"source creation time: {record_path}")
            if not isinstance(origins, list) or not origins:
                raise ReconstructionError(f"invalid source origins: {record_path}")
            for origin in origins:
                if not isinstance(origin, dict):
                    raise ReconstructionError(f"invalid source origin: {record_path}")
                origin_path = origin.get("path")
                captured_at = origin.get("captured_at")
                if not isinstance(origin_path, str) or not origin_path:
                    raise ReconstructionError(f"invalid source origin path: {record_path}")
                if not isinstance(captured_at, str):
                    raise ReconstructionError(f"invalid source origin time: {record_path}")
                _validate_timestamp(captured_at, f"source origin time: {record_path}")
            object_path = self._root / "store" / "objects" / object_reference
            try:
                source_bytes = object_path.read_bytes()
            except OSError as error:
                raise ReconstructionError(f"cannot read source object: {object_path}") from error
            if hashlib.sha256(source_bytes).hexdigest() != digest:
                raise ReconstructionError(
                    f"source object does not match its content address: {object_path}"
                )
            try:
                source_text = source_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReconstructionError(
                    f"source object is not valid UTF-8: {object_path}"
                ) from error
            sources.append(
                _VerifiedSource(
                    source_id=source_id,
                    sensitivity=sensitivity,
                    record_path=record_path,
                    object_reference=object_reference,
                    text=source_text,
                )
            )
        return tuple(sources)

    def _validate_knowledge(
        self,
        notes: tuple[KnowledgeNoteSnapshot, ...],
        notes_by_id: dict[str, KnowledgeNoteSnapshot],
        source_ids: set[str],
    ) -> set[tuple[str, str]]:
        supersession_pairs: set[tuple[str, str]] = set()
        for note in notes:
            if set(note.sources) - source_ids:
                raise ReconstructionError(
                    f"knowledge note references missing sources: {note.path}"
                )
            if note.kind == "insight":
                if note.derived_from is not None or note.supersedes or note.superseded_by:
                    raise ReconstructionError(
                        f"derived insight has invalid cross-kind relations: {note.path}"
                    )
                if note.promoted_to is not None:
                    promoted_note = notes_by_id.get(note.promoted_to)
                    if (
                        note.state != "archived"
                        or promoted_note is None
                        or promoted_note.kind != "cognition"
                        or promoted_note.derived_from != note.knowledge_id
                    ):
                        raise ReconstructionError(
                            f"derived insight has unresolved promoted_to relation: {note.path}"
                        )
                continue
            if note.promoted_to is not None:
                raise ReconstructionError(
                    f"personal cognition has invalid promoted_to relation: {note.path}"
                )
            source_insight = notes_by_id.get(note.derived_from or "")
            if (
                source_insight is None
                or source_insight.kind != "insight"
                or source_insight.promoted_to != note.knowledge_id
            ):
                raise ReconstructionError(
                    f"personal cognition has unresolved derived_from relation: {note.path}"
                )
            if note.state == "superseded" and not note.superseded_by:
                raise ReconstructionError(
                    f"superseded cognition has no successor: {note.path}"
                )
            if note.state == "active" and note.superseded_by:
                raise ReconstructionError(
                    f"active cognition unexpectedly has a successor: {note.path}"
                )
            for old_id in note.supersedes:
                old_note = notes_by_id.get(old_id)
                if (
                    old_note is None
                    or old_note.kind != "cognition"
                    or note.knowledge_id not in old_note.superseded_by
                ):
                    raise ReconstructionError(
                        f"knowledge note has unresolved supersedes relation: {note.path}"
                    )
                supersession_pairs.add((old_id, note.knowledge_id))
            for new_id in note.superseded_by:
                new_note = notes_by_id.get(new_id)
                if (
                    new_note is None
                    or new_note.kind != "cognition"
                    or note.knowledge_id not in new_note.supersedes
                ):
                    raise ReconstructionError(
                        f"knowledge note has unresolved superseded_by relation: {note.path}"
                    )
        return supersession_pairs

    def _validate_events(
        self,
        source_ids: set[str],
        notes_by_id: dict[str, KnowledgeNoteSnapshot],
        supersession_pairs: set[tuple[str, str]],
    ) -> None:
        journal_path = self._root / "store" / "journal" / "events.jsonl"
        if not journal_path.exists():
            if source_ids or notes_by_id:
                raise ReconstructionError(
                    f"missing knowledge-evolution journal: {journal_path}"
                )
            return
        try:
            lines = journal_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise ReconstructionError(
                f"cannot read knowledge-evolution journal: {journal_path}"
            ) from error
        event_ids: set[str] = set()
        promotion_events: set[tuple[str, str]] = set()
        supersession_events: set[tuple[str, str]] = set()
        accepted_insights: set[str] = set()
        known_types = {
            "source.captured",
            "source.duplicate",
            "source.origin_added",
            "source.sensitivity_restricted",
            "model.external_call",
            "candidate.reviewed",
            "knowledge.promoted",
            "knowledge.superseded",
        }
        for line_number, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise TypeError("event is not an object")
                event_id = _required_string(event, "id")
                event_type = _required_string(event, "type")
                occurred_at = _required_string(event, "occurred_at")
                if (
                    re.fullmatch(r"evt_[0-9a-f]{32}", event_id) is None
                    or event_id in event_ids
                    or event_type not in known_types
                ):
                    raise ValueError("event identity or type is invalid")
                _validate_timestamp(occurred_at, "event time")
                event_ids.add(event_id)
                if event_type.startswith("source."):
                    if _required_string(event, "source_id") not in source_ids:
                        raise ValueError("source event references missing source")
                elif event_type == "model.external_call":
                    for field in ("provider", "model", "purpose", "request_fingerprint"):
                        _required_string(event, field)
                    referenced_sources = event.get("source_ids")
                    if (
                        not isinstance(referenced_sources, list)
                        or any(value not in source_ids for value in referenced_sources)
                    ):
                        raise ValueError("external-call event references missing sources")
                elif event_type == "candidate.reviewed":
                    _required_string(event, "candidate_id")
                    decision = _required_string(event, "decision")
                    if decision not in ("accept", "defer", "reject"):
                        raise ValueError("candidate review decision is invalid")
                    if decision == "accept":
                        knowledge_id = _required_string(event, "knowledge_id")
                        insight = notes_by_id.get(knowledge_id)
                        if (
                            insight is None
                            or insight.kind != "insight"
                            or insight.knowledge_id in accepted_insights
                            or _required_string(event, "candidate_id")
                            != insight.candidate_id
                            or _required_string(event, "authorship")
                            != insight.authorship
                        ):
                            raise ValueError("candidate acceptance contradicts knowledge")
                        note_path = _required_string(event, "note_path")
                        resolved_note_path = (self._root / note_path).resolve()
                        if (
                            not resolved_note_path.is_relative_to(
                                (self._root / "vault").resolve()
                            )
                            or resolved_note_path.suffix.lower() != ".md"
                        ):
                            raise ValueError("candidate acceptance has invalid note path")
                        accepted_insights.add(insight.knowledge_id)
                    if decision == "reject":
                        _required_string(event, "candidate_fingerprint")
                elif event_type == "knowledge.promoted":
                    from_id = _required_string(event, "from_id")
                    to_id = _required_string(event, "to_id")
                    if from_id not in notes_by_id or to_id not in notes_by_id:
                        raise ValueError("promotion event references missing knowledge")
                    if _required_string(event, "actor") != "user":
                        raise ValueError("promotion event actor is invalid")
                    promotion_events.add((from_id, to_id))
                elif event_type == "knowledge.superseded":
                    old_id = _required_string(event, "old_id")
                    new_id = _required_string(event, "new_id")
                    if _required_string(event, "actor") != "user":
                        raise ValueError("supersession event actor is invalid")
                    supersession_events.add((old_id, new_id))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ReconstructionError(
                    f"invalid knowledge-evolution event at {journal_path} "
                    f"line {line_number}"
                ) from error
        expected_promotions = {
            (note.derived_from, note.knowledge_id)
            for note in notes_by_id.values()
            if note.kind == "cognition" and note.derived_from is not None
        }
        if promotion_events != expected_promotions:
            raise ReconstructionError(
                f"knowledge promotion events do not match Vault state: {journal_path}"
            )
        if supersession_events != supersession_pairs:
            raise ReconstructionError(
                f"knowledge supersession events do not match Vault state: {journal_path}"
            )
        expected_insights = {
            note.knowledge_id
            for note in notes_by_id.values()
            if note.kind == "insight"
        }
        if accepted_insights != expected_insights:
            raise ReconstructionError(
                f"candidate acceptance events do not match Vault state: {journal_path}"
            )

    def _build_projection(
        self,
        sources: tuple[_VerifiedSource, ...],
        notes: tuple[KnowledgeNoteSnapshot, ...],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        catalog: list[dict[str, object]] = []
        documents: list[dict[str, object]] = []
        for source in sources:
            catalog.append(
                {
                    "id": source.source_id,
                    "kind": "source",
                    "sensitivity": source.sensitivity,
                    "record": source.record_path.relative_to(self._root).as_posix(),
                    "object": f"store/objects/{source.object_reference}",
                }
            )
            documents.append(
                {
                    "id": source.source_id,
                    "kind": "source",
                    "state": "active",
                    "text": source.text,
                }
            )
        for note in notes:
            catalog.append(
                {
                    "id": note.knowledge_id,
                    "kind": note.kind,
                    "state": note.state,
                    "sensitivity": note.sensitivity,
                    "path": note.path.relative_to(self._root).as_posix(),
                    "sources": list(note.sources),
                    "supersedes": list(note.supersedes),
                    "superseded_by": list(note.superseded_by),
                }
            )
            documents.append(
                {
                    "id": note.knowledge_id,
                    "kind": note.kind,
                    "state": note.state,
                    "text": note.body,
                }
            )
        return catalog, documents

    def _activate(
        self,
        catalog: list[dict[str, object]],
        documents: list[dict[str, object]],
    ) -> None:
        runtime_root = self._root / "runtime"
        projections_root = runtime_root / "projections"
        projections_root.mkdir(parents=True, exist_ok=True)
        previous_projection = _read_previous_projection(runtime_root)
        projection_name = f"proj_{uuid.uuid4().hex}"
        staging_path = runtime_root / f".rebuild_{uuid.uuid4().hex}"
        projection_path = projections_root / projection_name
        try:
            (staging_path / "indexes" / "fulltext").mkdir(parents=True)
            _atomic_write_file(
                staging_path / "catalog.json",
                _json_document({"schema_version": SCHEMA_VERSION, "knowledge": catalog}),
            )
            _atomic_write_file(
                staging_path / "indexes" / "fulltext" / "documents.json",
                _json_document(
                    {"schema_version": SCHEMA_VERSION, "documents": documents}
                ),
            )
            os.replace(staging_path, projection_path)
            if os.environ.get("MYOUTBRAIN_FAULT_INJECTION") == "rebuild-before-activation":
                os._exit(86)
            _atomic_write_file(
                runtime_root / "active-projection.json",
                _json_document(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "projection": f"projections/{projection_name}",
                    }
                ),
            )
        finally:
            if staging_path.exists():
                shutil.rmtree(staging_path, ignore_errors=True)
        self._best_effort_reclaim(
            runtime_root,
            projections_root,
            keep={projection_name, previous_projection},
        )

    def _best_effort_reclaim(
        self,
        runtime_root: Path,
        projections_root: Path,
        *,
        keep: set[str | None],
    ) -> None:
        try:
            for candidate in projections_root.iterdir():
                if candidate.name not in keep:
                    if candidate.is_dir():
                        shutil.rmtree(candidate)
                    else:
                        candidate.unlink()
        except OSError:
            pass
        for relative_path in ("workspace", "cache", "logs"):
            reclaimable_path = runtime_root / relative_path
            try:
                if reclaimable_path.exists():
                    if reclaimable_path.is_dir():
                        shutil.rmtree(reclaimable_path)
                    else:
                        reclaimable_path.unlink()
                reclaimable_path.mkdir(parents=True)
                if relative_path == "workspace":
                    (reclaimable_path / "inbox").mkdir()
                    (reclaimable_path / "candidates").mkdir()
            except OSError:
                pass


def _read_json_object(path: Path, description: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        error_type = RuntimeProjectionError if "runtime" in description else ReconstructionError
        raise error_type(f"invalid {description}: {path}") from error
    if not isinstance(value, dict):
        error_type = RuntimeProjectionError if "runtime" in description else ReconstructionError
        raise error_type(f"invalid {description}: {path}")
    return value


def _required_string(value: dict[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field:
        raise TypeError(f"{key} is invalid")
    return field


def _validate_timestamp(value: str, description: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid {description}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {description}")


def _read_previous_projection(runtime_root: Path) -> str | None:
    pointer_path = runtime_root / "active-projection.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        projection = pointer.get("projection") if isinstance(pointer, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(projection, str):
        return None
    return Path(projection).name


def _atomic_write_file(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _json_document(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
