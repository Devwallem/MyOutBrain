from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tomllib
from typing import Literal
import uuid

from myoutbrain.generation import (
    Citation,
    CloudAuthorization,
    EvidenceItem,
    EvidencePackage,
    GeneratedClaim,
    GenerationRequest,
    GenerationProvider,
    ProviderFailure,
    create_generation_provider,
)
from myoutbrain.candidates import (
    CandidateRecord,
    CandidateWorkspace,
    CandidateWorkspaceError,
)
from myoutbrain.knowledge import DerivedInsightNote, KnowledgeNoteError
from myoutbrain.obsidian import create_obsidian_adapter
from myoutbrain.reconstruction import (
    RebuildResult,
    ReconstructionError,
    RuntimeProjectionError,
    RuntimeProjectionReader,
    RuntimeProjectionUnavailable,
    RuntimeReconstructor,
)
from myoutbrain.vault import (
    KnowledgeTransitionError,
    VaultIntegrityError,
    prepare_cognition_promotion,
)
from myoutbrain.core_types import (
    ConfigurationConflict,
    IntegrityError,
    Sensitivity,
    UserInputError,
)
from myoutbrain.local_core import LocalMemoryCore
from myoutbrain.persistence import (
    atomic_commit as _atomic_commit,
    atomic_write as _atomic_write,
    event_journal_change as _event_journal_change,
    hold_writer_lock_for_acceptance_test as _hold_writer_lock_for_acceptance_test,
    json_document as _json_document,
    recover_transactions as _recover_transactions,
    writer_lock as _writer_lock,
)


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
    "store/transactions",
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
DEFAULT_GENERATION_PROVIDER = "openai"
DEFAULT_GENERATION_MODEL = "gpt-5-mini"
DEFAULT_CANDIDATE_TTL_DAYS = 30
GIT_IGNORE_MARKER = "# MyOutBrain machine data"
GIT_IGNORE_RULES = (
    "/store/objects/",
    "/store/transactions/",
    "/store/memory.sqlite3",
    "/runtime/",
    "/.myoutbrain.lock",
)


@dataclass(frozen=True)
class CaptureResult:
    source_id: str
    disposition: CaptureDisposition


@dataclass(frozen=True)
class AskResult:
    claims: tuple[GeneratedClaim, ...]
    evidence: tuple[EvidenceItem, ...]
    insufficient_evidence: bool


@dataclass(frozen=True)
class ReflectionResult:
    candidate_ids: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    insufficient_evidence: bool
    suppressed_count: int


@dataclass(frozen=True)
class AcceptResult:
    knowledge_id: str
    note_path: Path
    warning: str | None


@dataclass(frozen=True)
class PromotionResult:
    cognition_id: str
    note_path: Path
    warning: str | None


@dataclass(frozen=True)
class GenerationContext:
    provider: GenerationProvider
    request: GenerationRequest
    configuration: dict[str, object]


@dataclass(frozen=True)
class SourceOrigin:
    path: str
    captured_at: str

    def to_data(self) -> dict[str, str]:
        return {"path": self.path, "captured_at": self.captured_at}


@dataclass
class SourceRecord:
    source_id: str
    sensitivity: Sensitivity
    content_hash: str
    object_reference: str
    created_at: str
    origins: list[SourceOrigin]

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        sensitivity: Sensitivity,
        digest: str,
        object_reference: str,
        captured_at: str,
        origin_path: str,
    ) -> SourceRecord:
        return cls(
            source_id=source_id,
            sensitivity=sensitivity,
            content_hash=f"sha256:{digest}",
            object_reference=object_reference,
            created_at=captured_at,
            origins=[SourceOrigin(path=origin_path, captured_at=captured_at)],
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_source_id: str,
        expected_digest: str,
        expected_object_reference: str,
    ) -> SourceRecord:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("source record is not an object")
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("source record has an invalid schema version")
            if data.get("id") != expected_source_id:
                raise ValueError("source record identity does not match its path")
            if data.get("kind") != "source" or data.get("state") != "active":
                raise ValueError("source record has an invalid kind or state")
            sensitivity = data.get("sensitivity")
            if sensitivity not in ("local-only", "cloud-allowed"):
                raise ValueError("source record has invalid sensitivity")
            content_hash = data.get("content_hash")
            if content_hash != f"sha256:{expected_digest}":
                raise ValueError("source record has an invalid content hash")
            object_reference = data.get("object")
            if object_reference != expected_object_reference:
                raise ValueError("source record has an invalid object reference")
            created_at = data.get("created_at")
            if not isinstance(created_at, str) or not created_at:
                raise TypeError("source record has an invalid creation time")
            origins_data = data.get("origins")
            if not isinstance(origins_data, list) or not origins_data:
                raise TypeError("source record has invalid origins")
            origins: list[SourceOrigin] = []
            for origin_data in origins_data:
                if not isinstance(origin_data, dict):
                    raise TypeError("source origin is not an object")
                origin_path = origin_data.get("path")
                captured_at = origin_data.get("captured_at")
                if not isinstance(origin_path, str) or not isinstance(captured_at, str):
                    raise TypeError("source origin has invalid fields")
                origins.append(SourceOrigin(path=origin_path, captured_at=captured_at))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise IntegrityError(f"invalid source record: {path}") from error
        return cls(
            source_id=expected_source_id,
            sensitivity=sensitivity,
            content_hash=content_hash,
            object_reference=object_reference,
            created_at=created_at,
            origins=origins,
        )

    def add_origin(self, path: str, captured_at: str) -> bool:
        if any(origin.path == path for origin in self.origins):
            return False
        self.origins.append(SourceOrigin(path=path, captured_at=captured_at))
        return True

    def restrict_to_local(self) -> bool:
        if self.sensitivity == "local-only":
            return False
        self.sensitivity = "local-only"
        return True

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.source_id,
            "kind": "source",
            "state": "active",
            "sensitivity": self.sensitivity,
            "content_hash": self.content_hash,
            "object": self.object_reference,
            "created_at": self.created_at,
            "origins": [origin.to_data() for origin in self.origins],
        }


def _render_initial_configuration() -> str:
    permanent = ", ".join(f'"{name}"' for name in PERMANENT_STORAGE)
    rebuildable = ", ".join(f'"{name}"' for name in REBUILDABLE_STORAGE)
    return (
        f"schema_version = {SCHEMA_VERSION}\n"
        "single_writer = true\n\n"
        "[storage]\n"
        f"permanent = [{permanent}]\n"
        f"rebuildable = [{rebuildable}]\n"
        f"\n{_render_default_generation_configuration()}"
        "\n[reflection]\n"
        f"candidate_ttl_days = {DEFAULT_CANDIDATE_TTL_DAYS}\n"
    )


def _render_default_generation_configuration() -> str:
    return (
        "[generation]\n"
        f'provider = "{DEFAULT_GENERATION_PROVIDER}"\n'
        f'model = "{DEFAULT_GENERATION_MODEL}"\n'
    )


def _with_default_generation_configuration(configuration: str) -> str:
    if configuration.endswith("\n\n"):
        separator = ""
    elif configuration.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{configuration}{separator}{_render_default_generation_configuration()}"


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


def _load_configuration(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationConflict(f"invalid configuration: {path}") from error
    return configuration


def _load_validated_configuration(path: Path) -> dict[str, object]:
    configuration = _load_configuration(path)
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
    return configuration


def _generation_configuration(
    configuration: dict[str, object],
    path: Path,
) -> tuple[str, str]:
    try:
        generation = configuration.get("generation")
        if generation is None:
            return DEFAULT_GENERATION_PROVIDER, DEFAULT_GENERATION_MODEL
        if not isinstance(generation, dict):
            raise TypeError("generation configuration is invalid")
        provider = generation.get("provider")
        model = generation.get("model")
        if not isinstance(provider, str) or not provider:
            raise TypeError("generation provider is invalid")
        if not isinstance(model, str) or not model:
            raise TypeError("generation model is invalid")
    except TypeError as error:
        raise ConfigurationConflict(f"invalid generation configuration: {path}") from error
    return provider, model


def _candidate_ttl_days(configuration: dict[str, object], path: Path) -> int:
    reflection = configuration.get("reflection")
    if reflection is None:
        return DEFAULT_CANDIDATE_TTL_DAYS
    if not isinstance(reflection, dict):
        raise ConfigurationConflict(f"invalid reflection configuration: {path}")
    candidate_ttl_days = reflection.get("candidate_ttl_days")
    if (
        not isinstance(candidate_ttl_days, int)
        or isinstance(candidate_ttl_days, bool)
        or candidate_ttl_days < 1
    ):
        raise ConfigurationConflict(f"invalid reflection configuration: {path}")
    return candidate_ttl_days


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
            _hold_writer_lock_for_acceptance_test()
            _recover_transactions(root)
            try:
                existing_git_ignore = (
                    git_ignore.read_text(encoding="utf-8") if git_ignore.exists() else ""
                )
            except UnicodeError as error:
                raise ConfigurationConflict(
                    f"Git ignore file is not valid UTF-8: {git_ignore}"
                ) from error
            migrated_configuration: str | None = None
            if configuration.exists():
                configuration_data = _load_validated_configuration(configuration)
                if "generation" not in configuration_data:
                    try:
                        existing_configuration = configuration.read_text(encoding="utf-8")
                    except (OSError, UnicodeError) as error:
                        raise ConfigurationConflict(
                            f"cannot read configuration for migration: {configuration}"
                        ) from error
                    migrated_configuration = _with_default_generation_configuration(
                        existing_configuration
                    )
            for relative_path in INITIAL_DIRECTORIES:
                (root / relative_path).mkdir(parents=True, exist_ok=True)
            updated_git_ignore = _with_required_git_ignore_rules(existing_git_ignore)
            if updated_git_ignore != existing_git_ignore:
                _atomic_write(git_ignore, updated_git_ignore.encode("utf-8"))
            if not configuration.exists():
                _atomic_write(configuration, _render_initial_configuration().encode("utf-8"))
            elif migrated_configuration is not None:
                _atomic_write(configuration, migrated_configuration.encode("utf-8"))
        LocalMemoryCore(root).initialize()

    def capture(self, source_path: Path, sensitivity: Sensitivity) -> CaptureResult:
        configuration = self._root / "myoutbrain.toml"
        if not configuration.is_file():
            raise ConfigurationConflict(f"MyOutBrain is not initialized at: {self._root}")
        _load_validated_configuration(configuration)
        source_bytes = _read_source(source_path)
        digest = hashlib.sha256(source_bytes).hexdigest()
        source_id = f"src_{digest}"

        with _writer_lock(self._root):
            _hold_writer_lock_for_acceptance_test()
            _recover_transactions(self._root)
            disposition = self._commit_capture(
                source_path=source_path,
                source_bytes=source_bytes,
                sensitivity=sensitivity,
                digest=digest,
                source_id=source_id,
            )
        return CaptureResult(source_id=source_id, disposition=disposition)

    def _prepare_generation_context(
        self,
        source_id: str,
        prompt: str,
        *,
        prompt_name: str,
        purpose: str,
        allow_cloud: bool,
    ) -> GenerationContext:
        configuration_path = self._root / "myoutbrain.toml"
        if not configuration_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        configuration = _load_validated_configuration(configuration_path)
        provider_name, model = _generation_configuration(
            configuration,
            configuration_path,
        )
        if not prompt.strip():
            raise UserInputError(f"{prompt_name} must not be blank")
        if not allow_cloud:
            raise UserInputError("this request requires explicit --allow-cloud authorization")
        digest = source_id.removeprefix("src_")
        if (
            not source_id.startswith("src_")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise UserInputError(f"invalid source identity: {source_id}")

        object_reference = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        record_path = self._root / "store" / "records" / f"{source_id}.json"
        if not record_path.is_file():
            raise UserInputError(f"source does not exist: {source_id}")
        record = SourceRecord.load(
            record_path,
            expected_source_id=source_id,
            expected_digest=digest,
            expected_object_reference=object_reference,
        )
        if record.sensitivity != "cloud-allowed":
            raise UserInputError(
                f"source is not eligible for cloud generation: {source_id}"
            )
        try:
            projected_source = RuntimeProjectionReader(self._root).source(source_id)
        except RuntimeProjectionUnavailable as error:
            raise UserInputError(str(error)) from error
        except RuntimeProjectionError as error:
            raise IntegrityError(str(error)) from error
        object_path = self._root / "store" / "objects" / object_reference
        try:
            source_bytes = object_path.read_bytes()
        except OSError as error:
            raise IntegrityError(f"cannot read source object: {object_path}") from error
        if hashlib.sha256(source_bytes).hexdigest() != digest:
            raise IntegrityError(
                f"source object does not match its content address: {object_path}"
            )
        try:
            source_content = source_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IntegrityError(
                f"source object is not valid UTF-8: {object_path}"
            ) from error
        if projected_source is not None:
            if (
                projected_source.sensitivity != record.sensitivity
                or projected_source.text != source_content
            ):
                raise IntegrityError(
                    f"runtime projection disagrees with permanent source: {source_id}"
                )
            source_content = projected_source.text
        line_count = max(1, len(source_content.splitlines()))
        evidence_package = EvidencePackage(
            question=prompt,
            items=(
                EvidenceItem(
                    citation=Citation(
                        source_id=source_id,
                        locator=(
                            f"store/objects/{object_reference}#L1-L{line_count}"
                        ),
                    ),
                    content=source_content,
                ),
            ),
        )
        return GenerationContext(
            provider=create_generation_provider(provider_name, model),
            request=GenerationRequest(
                purpose=purpose,
                authorization=CloudAuthorization(allow_cloud=allow_cloud),
                evidence_package=evidence_package,
            ),
            configuration=configuration,
        )

    def ask(self, source_id: str, question: str, *, allow_cloud: bool) -> AskResult:
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            context = self._prepare_generation_context(
                source_id,
                question,
                prompt_name="question",
                purpose="answer-question",
                allow_cloud=allow_cloud,
            )
            self._record_external_call(
                provider=context.provider.name,
                model=context.provider.model,
                request=context.request,
            )
            generated = context.provider.generate(context.request)
            evidence = context.request.evidence_package.items
            allowed_citations = {item.citation for item in evidence}
            if any(claim.citation not in allowed_citations for claim in generated.claims):
                raise ProviderFailure(
                    "generated claim citation is outside the evidence package"
                )
        return AskResult(
            claims=generated.claims,
            evidence=evidence,
            insufficient_evidence=generated.insufficient_evidence,
        )

    def reflect(
        self,
        source_id: str,
        prompt: str,
        *,
        allow_cloud: bool,
    ) -> ReflectionResult:
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            context = self._prepare_generation_context(
                source_id,
                prompt,
                prompt_name="reflection prompt",
                purpose="reflect-on-source",
                allow_cloud=allow_cloud,
            )
            configuration_path = self._root / "myoutbrain.toml"
            candidate_ttl_days = _candidate_ttl_days(
                context.configuration,
                configuration_path,
            )
            self._record_external_call(
                provider=context.provider.name,
                model=context.provider.model,
                request=context.request,
            )
            generated = context.provider.reflect(context.request)
            evidence = context.request.evidence_package.items
            allowed_citations = {item.citation for item in evidence}
            for candidate in generated.candidates:
                candidate_citations = (
                    candidate.supporting_evidence + candidate.contrary_evidence
                )
                if any(
                    citation not in allowed_citations
                    for citation in candidate_citations
                ):
                    raise ProviderFailure(
                        "generated candidate citation is outside the evidence package"
                    )
            if generated.insufficient_evidence:
                return ReflectionResult(
                    candidate_ids=(),
                    evidence=evidence,
                    insufficient_evidence=True,
                    suppressed_count=0,
                )
            occurred_at = datetime.now(timezone.utc)
            try:
                workspace = CandidateWorkspace.load(self._root)
                candidate_ids, suppressed_count = workspace.merge(
                    generated.candidates,
                    occurred_at,
                    candidate_ttl_days,
                )
            except CandidateWorkspaceError as error:
                raise IntegrityError(str(error)) from error
            if candidate_ids:
                _atomic_commit(
                    self._root,
                    [(workspace.catalog_path, workspace.catalog_content())],
                    fault_injections={0: "reflect-after-first-replace"},
                )
        return ReflectionResult(
            candidate_ids=tuple(candidate_ids),
            evidence=evidence,
            insufficient_evidence=False,
            suppressed_count=suppressed_count,
        )

    def review_candidates(self) -> tuple[CandidateRecord, ...]:
        configuration = self._root / "myoutbrain.toml"
        if not configuration.is_file():
            raise ConfigurationConflict(f"MyOutBrain is not initialized at: {self._root}")
        _load_validated_configuration(configuration)
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            try:
                return CandidateWorkspace.load(self._root).list_records()
            except CandidateWorkspaceError as error:
                raise IntegrityError(str(error)) from error

    def defer_candidate(self, candidate_id: str) -> None:
        configuration = self._root / "myoutbrain.toml"
        if not configuration.is_file():
            raise ConfigurationConflict(f"MyOutBrain is not initialized at: {self._root}")
        _load_validated_configuration(configuration)
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            try:
                CandidateWorkspace.load(self._root).get_record(candidate_id)
            except CandidateWorkspaceError as error:
                raise UserInputError(str(error)) from error
            event = {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "candidate.reviewed",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "candidate_id": candidate_id,
                "decision": "defer",
            }
            _atomic_commit(
                self._root,
                [_event_journal_change(self._root, event)],
            )

    def reject_candidate(self, candidate_id: str) -> None:
        configuration_path = self._root / "myoutbrain.toml"
        if not configuration_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        configuration = _load_validated_configuration(configuration_path)
        suppression_days = _candidate_ttl_days(configuration, configuration_path)
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            try:
                workspace = CandidateWorkspace.load(self._root)
                occurred_at = datetime.now(timezone.utc)
                fingerprint, rejection_path, rejection_content = workspace.reject(
                    candidate_id,
                    occurred_at,
                    suppression_days,
                )
            except CandidateWorkspaceError as error:
                raise UserInputError(str(error)) from error
            event = {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "candidate.reviewed",
                "occurred_at": occurred_at.isoformat(),
                "candidate_id": candidate_id,
                "candidate_fingerprint": fingerprint,
                "decision": "reject",
            }
            _atomic_commit(
                self._root,
                [
                    (workspace.catalog_path, workspace.catalog_content()),
                    (rejection_path, rejection_content),
                    _event_journal_change(self._root, event),
                ],
            )

    def accept_candidate(
        self,
        candidate_id: str,
        *,
        title: str,
        sensitivity: Sensitivity,
        text: str | None = None,
    ) -> AcceptResult:
        configuration_path = self._root / "myoutbrain.toml"
        if not configuration_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        _load_validated_configuration(configuration_path)
        if sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError(f"invalid sensitivity: {sensitivity}")

        with _writer_lock(self._root):
            _recover_transactions(self._root)
            try:
                workspace = CandidateWorkspace.load(self._root)
                candidate = workspace.remove(candidate_id)
            except CandidateWorkspaceError as error:
                raise UserInputError(str(error)) from error

            occurred_at = datetime.now(timezone.utc)
            knowledge_id = f"ins_{uuid.uuid4().hex}"
            try:
                note = DerivedInsightNote.from_candidate(
                    candidate,
                    knowledge_id=knowledge_id,
                    title=title,
                    text=text,
                    sensitivity=sensitivity,
                    occurred_at=occurred_at,
                )
            except KnowledgeNoteError as error:
                raise UserInputError(str(error)) from error
            note_path = self._root / "vault" / note.filename
            if note_path.exists():
                raise UserInputError(f"knowledge note already exists: {note.filename}")

            event = {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "candidate.reviewed",
                "occurred_at": occurred_at.isoformat(),
                "candidate_id": candidate_id,
                "decision": "accept",
                "knowledge_id": knowledge_id,
                "note_path": note_path.relative_to(self._root).as_posix(),
                "authorship": note.authorship,
            }
            _atomic_commit(
                self._root,
                [
                    (workspace.catalog_path, workspace.catalog_content()),
                    (note_path, note.render()),
                    _event_journal_change(self._root, event),
                ],
                fault_injections={
                    0: "review-after-first-replace",
                    1: "review-after-note-replace",
                },
            )

        warning = create_obsidian_adapter().open_note(
            self._root / "vault",
            note_path,
        )
        return AcceptResult(
            knowledge_id=knowledge_id,
            note_path=note_path,
            warning=warning,
        )

    def promote_insight(
        self,
        insight_id: str,
        *,
        title: str,
        supersedes_id: str | None = None,
    ) -> PromotionResult:
        configuration_path = self._root / "myoutbrain.toml"
        if not configuration_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        _load_validated_configuration(configuration_path)
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            occurred_at = datetime.now(timezone.utc)
            cognition_id = f"cog_{uuid.uuid4().hex}"
            try:
                promotion = prepare_cognition_promotion(
                    self._root / "vault",
                    insight_id=insight_id,
                    cognition_id=cognition_id,
                    title=title,
                    occurred_at=occurred_at,
                    supersedes_id=supersedes_id,
                )
            except KnowledgeTransitionError as error:
                raise UserInputError(str(error)) from error
            except VaultIntegrityError as error:
                raise IntegrityError(str(error)) from error
            promotion_event: dict[str, object] = {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "knowledge.promoted",
                "occurred_at": occurred_at.isoformat(),
                "from_id": insight_id,
                "to_id": cognition_id,
                "actor": "user",
            }
            events: list[dict[str, object]] = [promotion_event]
            if supersedes_id is not None:
                events.append(
                    {
                        "id": f"evt_{uuid.uuid4().hex}",
                        "type": "knowledge.superseded",
                        "occurred_at": occurred_at.isoformat(),
                        "old_id": supersedes_id,
                        "new_id": cognition_id,
                        "actor": "user",
                    }
                )
            changes = list(promotion.changes)
            changes.append(_event_journal_change(self._root, *events))
            _atomic_commit(
                self._root,
                changes,
                fault_injections={
                    0: "promote-after-insight-replace",
                    1: "promote-after-cognition-replace",
                    2: "promote-after-superseded-replace",
                },
            )
        warning = create_obsidian_adapter().open_note(
            self._root / "vault",
            promotion.cognition_path,
        )
        return PromotionResult(
            cognition_id=cognition_id,
            note_path=promotion.cognition_path,
            warning=warning,
        )

    def rebuild_runtime(self) -> RebuildResult:
        configuration_path = self._root / "myoutbrain.toml"
        if not configuration_path.is_file():
            raise ConfigurationConflict(
                f"MyOutBrain is not initialized at: {self._root}"
            )
        _load_validated_configuration(configuration_path)
        with _writer_lock(self._root):
            _recover_transactions(self._root)
            try:
                return RuntimeReconstructor(self._root).rebuild()
            except ReconstructionError as error:
                raise IntegrityError(str(error)) from error

    def _record_external_call(
        self,
        *,
        provider: str,
        model: str,
        request: GenerationRequest,
    ) -> None:
        serialized_request = json.dumps(
            request.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        event = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": "model.external_call",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "purpose": request.purpose,
            "source_ids": [
                item.citation.source_id for item in request.evidence_package.items
            ],
            "request_fingerprint": f"sha256:{hashlib.sha256(serialized_request).hexdigest()}",
        }
        journal_path = self._root / "store" / "journal" / "events.jsonl"
        try:
            existing_journal = journal_path.read_bytes() if journal_path.exists() else b""
        except OSError as error:
            raise IntegrityError(f"cannot read event journal: {journal_path}") from error
        event_line = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        _atomic_commit(self._root, [(journal_path, existing_journal + event_line)])

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
        object_reference = object_path.relative_to(objects_root).as_posix()
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
            record = SourceRecord.create(
                source_id=source_id,
                sensitivity=sensitivity,
                digest=digest,
                object_reference=object_reference,
                captured_at=captured_at,
                origin_path=str(source_path.resolve()),
            )
            changes.append((record_path, _json_document(record.to_data())))
        else:
            record = SourceRecord.load(
                record_path,
                expected_source_id=source_id,
                expected_digest=digest,
                expected_object_reference=object_reference,
            )
            record_changed = False
            origin_path = str(source_path.resolve())
            if record.add_origin(origin_path, captured_at):
                record_changed = True
                disposition = "origin-added"
            else:
                disposition = "duplicate"
            if sensitivity == "local-only" and record.restrict_to_local():
                record_changed = True
                disposition = "sensitivity-restricted"
            if record_changed:
                changes.append((record_path, _json_document(record.to_data())))

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
        _atomic_commit(
            self._root,
            changes,
            fault_injections={0: "capture-after-first-replace"},
        )
        return disposition
