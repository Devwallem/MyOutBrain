from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any, Literal
import uuid


Sensitivity = Literal["local-only", "cloud-allowed"]
CaptureDisposition = Literal[
    "captured",
    "duplicate",
    "origin-added",
    "sensitivity-restricted",
]

INITIAL_DIRECTORIES = (
    "vault",
    "store",
    "store/objects",
    "store/objects/sha256",
    "store/records",
    "store/journal",
    "runtime",
    "runtime/derived",
    "runtime/indexes",
    "runtime/indexes/fulltext",
    "runtime/workspace",
    "runtime/workspace/inbox",
    "runtime/workspace/candidates",
    "runtime/cache",
    "runtime/logs",
)

SCHEMA_VERSION = 1
PERMANENT_STORAGE = ("vault", "store")
REBUILDABLE_STORAGE = ("runtime",)
GIT_IGNORE_MARKER = "# MyOutBrain machine data"
GIT_IGNORE_RULES = ("/store/objects/", "/runtime/")


class ConfigurationConflict(Exception):
    """Raised when the library configuration cannot be used safely."""


class IntegrityError(Exception):
    """Raised when permanent storage contradicts its content identity."""


class UserInputError(Exception):
    """Raised when a command input cannot be accepted."""


class WriterLocked(Exception):
    """Raised when another writer already owns the project lock."""


@dataclass(frozen=True)
class CaptureResult:
    source_id: str
    disposition: CaptureDisposition


def _render_initial_configuration() -> str:
    permanent = ", ".join(f'"{name}"' for name in PERMANENT_STORAGE)
    rebuildable = ", ".join(f'"{name}"' for name in REBUILDABLE_STORAGE)
    return (
        f"schema_version = {SCHEMA_VERSION}\n"
        "single_writer = true\n\n"
        "[storage]\n"
        f"permanent = [{permanent}]\n"
        f"rebuildable = [{rebuildable}]\n"
    )


def _atomic_write(path: Path, content: bytes) -> None:
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


def _atomic_commit(changes: Sequence[tuple[Path, bytes]]) -> None:
    previous_contents: dict[Path, bytes | None] = {}
    for path, _content in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_contents[path] = path.read_bytes() if path.exists() else None

    applied: list[Path] = []
    try:
        for path, content in changes:
            _atomic_write(path, content)
            applied.append(path)
            if (
                len(applied) == 1
                and os.environ.get("MYOUTBRAIN_FAULT_INJECTION")
                == "capture-after-first-replace"
            ):
                raise OSError("simulated interruption after first atomic replacement")
    except BaseException:
        for path in reversed(applied):
            previous_content = previous_contents[path]
            if previous_content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, previous_content)
        raise


@contextmanager
def _writer_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".myoutbrain.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise WriterLocked from error
    try:
        os.write(lock_descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)


def _with_required_git_ignore_rules(existing_content: str) -> str:
    existing_lines = set(existing_content.splitlines())
    additions: list[str] = []
    if GIT_IGNORE_MARKER not in existing_lines:
        additions.append(GIT_IGNORE_MARKER)
    additions.extend(rule for rule in GIT_IGNORE_RULES if rule not in existing_lines)
    if not additions:
        return existing_content
    separator = "" if not existing_content or existing_content.endswith("\n") else "\n"
    return f"{existing_content}{separator}{'\n'.join(additions)}\n"


def _validate_configuration(path: Path) -> None:
    try:
        with path.open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationConflict(f"invalid configuration: {path}") from error

    if configuration.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationConflict("unsupported schema_version in existing configuration")
    if configuration.get("single_writer") is not True:
        raise ConfigurationConflict("existing configuration must enable single_writer")
    storage = configuration.get("storage")
    if not isinstance(storage, dict):
        raise ConfigurationConflict("existing configuration is missing storage classification")
    if storage.get("permanent") != list(PERMANENT_STORAGE):
        raise ConfigurationConflict("existing configuration has an invalid permanent storage classification")
    if storage.get("rebuildable") != list(REBUILDABLE_STORAGE):
        raise ConfigurationConflict("existing configuration has an invalid rebuildable storage classification")


def _read_source(source_path: Path) -> bytes:
    if not source_path.exists():
        raise UserInputError(f"source does not exist: {source_path}")
    if not source_path.is_file():
        raise UserInputError(f"source is not a file: {source_path}")
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise UserInputError(f"source is not readable: {source_path}") from error
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UserInputError(f"source is not valid UTF-8: {source_path}") from error
    if source_path.suffix.lower() != ".md" or not source_text.strip():
        raise UserInputError(f"source is not a valid Markdown document: {source_path}")
    return source_bytes


def _load_record(record_path: Path, source_id: str, digest: str) -> dict[str, Any]:
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise TypeError("source record is not an object")
        if record.get("id") != source_id or record.get("content_hash") != f"sha256:{digest}":
            raise ValueError("source record identity does not match its path")
        origins = record.get("origins")
        if not isinstance(origins, list) or any(
            not isinstance(origin, dict) or not isinstance(origin.get("path"), str)
            for origin in origins
        ):
            raise TypeError("source record has invalid origins")
        if record.get("sensitivity") not in ("local-only", "cloud-allowed"):
            raise ValueError("source record has invalid sensitivity")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid source record: {record_path}") from error
    return record


class KnowledgeWorkflow:
    """The public seam for durable personal-knowledge workflows."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def initialize(self) -> None:
        root = self._root
        if root.exists() and not root.is_dir():
            raise ConfigurationConflict(f"project root is not a directory: {root}")
        for relative_path in INITIAL_DIRECTORIES:
            candidate = root / relative_path
            if candidate.exists() and not candidate.is_dir():
                raise ConfigurationConflict(f"expected a directory at: {candidate}")
        configuration = root / "myoutbrain.toml"
        if configuration.exists() and not configuration.is_file():
            raise ConfigurationConflict(f"expected a configuration file at: {configuration}")
        git_ignore = root / ".gitignore"
        if git_ignore.exists() and not git_ignore.is_file():
            raise ConfigurationConflict(f"expected a Git ignore file at: {git_ignore}")

        root.mkdir(parents=True, exist_ok=True)
        with _writer_lock(root):
            try:
                existing_git_ignore = (
                    git_ignore.read_text(encoding="utf-8") if git_ignore.exists() else ""
                )
            except UnicodeError as error:
                raise ConfigurationConflict(
                    f"Git ignore file is not valid UTF-8: {git_ignore}"
                ) from error
            if configuration.exists():
                _validate_configuration(configuration)
            for relative_path in INITIAL_DIRECTORIES:
                (root / relative_path).mkdir(parents=True, exist_ok=True)
            updated_git_ignore = _with_required_git_ignore_rules(existing_git_ignore)
            if updated_git_ignore != existing_git_ignore:
                _atomic_write(git_ignore, updated_git_ignore.encode("utf-8"))
            if not configuration.exists():
                _atomic_write(configuration, _render_initial_configuration().encode("utf-8"))

    def capture(self, source_path: Path, sensitivity: Sensitivity) -> CaptureResult:
        configuration = self._root / "myoutbrain.toml"
        if not configuration.is_file():
            raise ConfigurationConflict(f"MyOutBrain is not initialized at: {self._root}")
        _validate_configuration(configuration)
        source_bytes = _read_source(source_path)
        digest = hashlib.sha256(source_bytes).hexdigest()
        source_id = f"src_{digest}"

        with _writer_lock(self._root):
            disposition = self._commit_capture(
                source_path=source_path,
                source_bytes=source_bytes,
                sensitivity=sensitivity,
                digest=digest,
                source_id=source_id,
            )
        return CaptureResult(source_id=source_id, disposition=disposition)

    def _commit_capture(
        self,
        *,
        source_path: Path,
        source_bytes: bytes,
        sensitivity: Sensitivity,
        digest: str,
        source_id: str,
    ) -> CaptureDisposition:
        captured_at = datetime.now(timezone.utc).isoformat()
        objects_root = self._root / "store" / "objects"
        object_path = objects_root / "sha256" / digest[:2] / digest[2:4] / digest
        record_path = self._root / "store" / "records" / f"{source_id}.json"
        changes: list[tuple[Path, bytes]] = []

        if object_path.exists():
            try:
                stored_digest = hashlib.sha256(object_path.read_bytes()).hexdigest()
            except OSError as error:
                raise IntegrityError(f"cannot read source object: {object_path}") from error
            if stored_digest != digest:
                raise IntegrityError(f"source object does not match its content address: {object_path}")
        else:
            changes.append((object_path, source_bytes))

        duplicate = record_path.is_file()
        disposition: CaptureDisposition = "captured"
        if not duplicate:
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "id": source_id,
                "kind": "source",
                "state": "active",
                "sensitivity": sensitivity,
                "content_hash": f"sha256:{digest}",
                "object": object_path.relative_to(objects_root).as_posix(),
                "created_at": captured_at,
                "origins": [{"path": str(source_path.resolve()), "captured_at": captured_at}],
            }
            changes.append((record_path, _json_document(record)))
        else:
            record = _load_record(record_path, source_id, digest)
            record_changed = False
            origin_path = str(source_path.resolve())
            origins = record["origins"]
            known_origins = {origin["path"] for origin in origins}
            if origin_path not in known_origins:
                origins.append({"path": origin_path, "captured_at": captured_at})
                record_changed = True
                disposition = "origin-added"
            else:
                disposition = "duplicate"
            if sensitivity == "local-only" and record["sensitivity"] != "local-only":
                record["sensitivity"] = "local-only"
                record_changed = True
                disposition = "sensitivity-restricted"
            if record_changed:
                changes.append((record_path, _json_document(record)))

        event = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": f"source.{disposition.replace('-', '_')}",
            "source_id": source_id,
            "occurred_at": captured_at,
        }
        journal_path = self._root / "store" / "journal" / "events.jsonl"
        try:
            existing_journal = journal_path.read_bytes() if journal_path.exists() else b""
        except OSError as error:
            raise IntegrityError(f"cannot read event journal: {journal_path}") from error
        event_line = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        changes.append((journal_path, existing_journal + event_line))
        _atomic_commit(changes)
        return disposition


def _json_document(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
