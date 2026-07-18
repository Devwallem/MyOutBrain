from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Literal, cast
import uuid

from myoutbrain.core_types import IntegrityError, UserInputError
from myoutbrain.unified_review import ReviewProposalInput, stage_review_proposal


LearningSignalKind = Literal[
    "user-correction",
    "confirmed-decision",
    "reusable-step",
    "failure-and-resolution",
    "research-question",
]

REFLECTION_INPUT_MAX_BYTES = 8 * 1024
REFLECTION_EXCERPT_MAX_BYTES = 2 * 1024
REFLECTION_RUN_MAX_INPUTS = 20
REFLECTION_RUN_MAX_PROPOSALS = 50
REFLECTION_RUN_MAX_BYTES = 64 * 1024

REFLECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reflection_inputs (
    input_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    signal_kind TEXT NOT NULL CHECK (signal_kind IN (
        'user-correction', 'confirmed-decision', 'reusable-step',
        'failure-and-resolution', 'research-question'
    )),
    entrance TEXT NOT NULL,
    task_pointer TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    source_reference_json TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    applicability_scope TEXT NOT NULL,
    context_coverage_json TEXT NOT NULL,
    blind_spots_json TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('local-only', 'cloud-allowed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reflection_runs (
    run_id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger = 'explicit'),
    status TEXT NOT NULL CHECK (status IN ('completed', 'abandoned')),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    input_count INTEGER NOT NULL CHECK (input_count > 0),
    result_json TEXT NOT NULL,
    abandonment_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ReflectionSourceReference:
    source_id: str
    version: str
    locator: str

    @classmethod
    def from_data(cls, data: object) -> ReflectionSourceReference:
        if not isinstance(data, dict):
            raise UserInputError("learning signal source_reference must be an object")
        return cls(
            source_id=_required_text(data, "source_id", maximum=500),
            version=_required_text(data, "version", maximum=500),
            locator=_required_text(data, "locator", maximum=2000),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "version": self.version,
            "locator": self.locator,
        }


@dataclass(frozen=True)
class LearningSignalSubmission:
    signal_kind: LearningSignalKind | None
    entrance: str
    task_pointer: str
    occurred_at: str | None = None
    excerpt: str | None = None
    source_reference: ReflectionSourceReference | None = None
    source_fingerprint: str | None = None
    applicability_scope: str | None = None
    context_coverage: tuple[str, ...] = ()
    blind_spots: tuple[str, ...] = ()
    sensitivity: Literal["local-only", "cloud-allowed"] | None = None

    @classmethod
    def from_data(cls, data: object) -> LearningSignalSubmission:
        if not isinstance(data, dict):
            raise UserInputError("learning signal payload must be a JSON object")
        signal_kind = data.get("signal_kind")
        allowed = (
            "user-correction",
            "confirmed-decision",
            "reusable-step",
            "failure-and-resolution",
            "research-question",
        )
        if signal_kind is not None and signal_kind not in allowed:
            raise UserInputError("learning signal kind is invalid")
        entrance = _required_text(data, "entrance", maximum=200)
        task_pointer = _required_text(data, "task_pointer", maximum=500)
        if signal_kind is None:
            return cls(
                signal_kind=None,
                entrance=entrance,
                task_pointer=task_pointer,
            )
        occurred_at = _required_text(data, "occurred_at", maximum=100)
        try:
            parsed_time = datetime.fromisoformat(occurred_at)
        except ValueError as error:
            raise UserInputError("learning signal occurred_at must be ISO-8601") from error
        if parsed_time.utcoffset() is None:
            raise UserInputError("learning signal occurred_at must include an offset")
        excerpt = _required_text(data, "excerpt", maximum=REFLECTION_EXCERPT_MAX_BYTES)
        if len(excerpt.encode("utf-8")) > REFLECTION_EXCERPT_MAX_BYTES:
            raise UserInputError("learning signal excerpt exceeds 2048 bytes")
        source_reference = ReflectionSourceReference.from_data(
            data.get("source_reference")
        )
        source_fingerprint = _required_text(
            data, "source_fingerprint", maximum=64
        ).casefold()
        if len(source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in source_fingerprint
        ):
            raise UserInputError("learning signal source_fingerprint must be sha256")
        applicability_scope = _required_text(
            data, "applicability_scope", maximum=1000
        )
        context_coverage = _required_text_list(data, "context_coverage")
        blind_spots = _required_text_list(data, "blind_spots")
        sensitivity = data.get("sensitivity")
        if sensitivity not in ("local-only", "cloud-allowed"):
            raise UserInputError("learning signal sensitivity is invalid")
        return cls(
            signal_kind=cast(LearningSignalKind, signal_kind),
            entrance=entrance,
            task_pointer=task_pointer,
            occurred_at=occurred_at,
            excerpt=excerpt,
            source_reference=source_reference,
            source_fingerprint=source_fingerprint,
            applicability_scope=applicability_scope,
            context_coverage=context_coverage,
            blind_spots=blind_spots,
            sensitivity=cast(Literal["local-only", "cloud-allowed"], sensitivity),
        )

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "signal_kind": self.signal_kind,
            "entrance": self.entrance,
            "task_pointer": self.task_pointer,
        }
        if self.signal_kind is not None:
            if (
                self.occurred_at is None
                or self.excerpt is None
                or self.source_reference is None
                or self.source_fingerprint is None
                or self.applicability_scope is None
                or self.sensitivity is None
            ):
                raise IntegrityError("captured learning signal is incomplete")
            data.update(
                {
                    "occurred_at": self.occurred_at,
                    "excerpt": self.excerpt,
                    "source_reference": self.source_reference.to_data(),
                    "source_fingerprint": self.source_fingerprint,
                    "applicability_scope": self.applicability_scope,
                    "context_coverage": list(self.context_coverage),
                    "blind_spots": list(self.blind_spots),
                    "sensitivity": self.sensitivity,
                }
            )
        return data


@dataclass(frozen=True)
class ReflectionInput:
    input_id: str
    submission: LearningSignalSubmission
    created_at: str

    def to_data(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            **self.submission.to_data(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LearningSignalCapture:
    captured: bool
    reflection_input: ReflectionInput | None

    def to_data(self) -> dict[str, object]:
        return {
            "captured": self.captured,
            "input": (
                self.reflection_input.to_data()
                if self.reflection_input is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ReflectionCandidate:
    candidate_id: str
    input_ids: tuple[str, ...]
    proposal: ReviewProposalInput
    derivation: str | None
    near_candidate_ids: tuple[str, ...]
    conflict_candidate_ids: tuple[str, ...]

    @classmethod
    def from_data(cls, data: object) -> ReflectionCandidate:
        if not isinstance(data, dict):
            raise UserInputError("reflection proposals must be JSON objects")
        proposal = ReviewProposalInput.from_data(data.get("proposal"))
        raw_derivation = data.get("derivation")
        if proposal.formation == "derived":
            derivation = _required_text(data, "derivation", maximum=2000)
        elif raw_derivation is not None:
            raise UserInputError("only derived reflection candidates use derivation")
        else:
            derivation = None
        return cls(
            candidate_id=_required_text(data, "candidate_id", maximum=200),
            input_ids=_required_text_list(data, "input_ids"),
            proposal=proposal,
            derivation=derivation,
            near_candidate_ids=_optional_text_list(data, "near_candidate_ids"),
            conflict_candidate_ids=_optional_text_list(
                data, "conflict_candidate_ids"
            ),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "input_ids": list(self.input_ids),
            "proposal": self.proposal.to_data(),
            "derivation": self.derivation,
            "near_candidate_ids": list(self.near_candidate_ids),
            "conflict_candidate_ids": list(self.conflict_candidate_ids),
        }


@dataclass(frozen=True)
class ImmediateReflectionRequest:
    input_ids: tuple[str, ...]
    proposals: tuple[ReflectionCandidate, ...]

    @classmethod
    def from_data(cls, data: object) -> ImmediateReflectionRequest:
        if not isinstance(data, dict):
            raise UserInputError("immediate reflection payload must be a JSON object")
        input_ids = _required_text_list(data, "input_ids")
        if len(input_ids) > REFLECTION_RUN_MAX_INPUTS:
            raise UserInputError("immediate reflection exceeds 20 selected inputs")
        raw_proposals = data.get("proposals")
        if not isinstance(raw_proposals, list) or not raw_proposals:
            raise UserInputError("immediate reflection proposals must be non-empty")
        if len(raw_proposals) > REFLECTION_RUN_MAX_PROPOSALS:
            raise UserInputError("immediate reflection exceeds 50 proposals")
        proposals = tuple(
            ReflectionCandidate.from_data(item) for item in raw_proposals
        )
        candidate_ids = tuple(candidate.candidate_id for candidate in proposals)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise UserInputError("reflection candidate ids must be unique")
        known = set(candidate_ids)
        known_inputs = set(input_ids)
        used_inputs: set[str] = set()
        relation_types_by_pair: dict[tuple[str, str], str] = {}
        for candidate in proposals:
            candidate_inputs = set(candidate.input_ids)
            if not candidate_inputs <= known_inputs:
                raise UserInputError(
                    "reflection candidate references an unselected input"
                )
            used_inputs.update(candidate_inputs)
            related = set(candidate.near_candidate_ids) | set(
                candidate.conflict_candidate_ids
            )
            overlap = set(candidate.near_candidate_ids) & set(
                candidate.conflict_candidate_ids
            )
            if overlap:
                raise UserInputError(
                    "reflection candidate relation cannot be both near and conflict"
                )
            for related_id, relation_type in (
                tuple(
                    (related_id, "near")
                    for related_id in candidate.near_candidate_ids
                )
                + tuple(
                    (related_id, "conflict")
                    for related_id in candidate.conflict_candidate_ids
                )
            ):
                first_id, second_id = sorted((candidate.candidate_id, related_id))
                pair = (first_id, second_id)
                existing_type = relation_types_by_pair.get(pair)
                if existing_type is not None and existing_type != relation_type:
                    raise UserInputError(
                        "reflection candidate relation cannot be both near and conflict"
                    )
                relation_types_by_pair[pair] = relation_type
            if candidate.candidate_id in related:
                raise UserInputError("reflection candidate cannot relate to itself")
            missing = related - known
            if missing:
                raise UserInputError(
                    f"reflection relation target does not exist: {sorted(missing)[0]}"
                )
        if used_inputs != known_inputs:
            raise UserInputError(
                "every selected reflection input must support at least one candidate"
            )
        request = cls(input_ids=input_ids, proposals=proposals)
        if len(_json(request.to_data()).encode("utf-8")) > REFLECTION_RUN_MAX_BYTES:
            raise UserInputError("immediate reflection payload exceeds 65536 bytes")
        return request

    def to_data(self) -> dict[str, object]:
        return {
            "input_ids": list(self.input_ids),
            "proposals": [proposal.to_data() for proposal in self.proposals],
        }


@dataclass(frozen=True)
class ImmediateReflectionResult:
    run_id: str
    status: Literal["completed", "abandoned"]
    candidate_proposal_ids: dict[str, str]
    cleaned_input_ids: tuple[str, ...]
    source_status: tuple[dict[str, object], ...]

    def to_data(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "candidate_proposal_ids": self.candidate_proposal_ids,
            "cleaned_input_ids": list(self.cleaned_input_ids),
            "source_status": list(self.source_status),
        }


@dataclass(frozen=True)
class ReflectionAbandonmentRequest:
    input_ids: tuple[str, ...]
    reason: str

    @classmethod
    def from_data(cls, data: object) -> ReflectionAbandonmentRequest:
        if not isinstance(data, dict):
            raise UserInputError("reflection abandonment must be a JSON object")
        return cls(
            input_ids=_required_text_list(data, "input_ids"),
            reason=_required_text(data, "reason", maximum=1000),
        )

    def to_data(self) -> dict[str, object]:
        return {"input_ids": list(self.input_ids), "reason": self.reason}


def load_learning_signal(data: object) -> LearningSignalSubmission:
    return LearningSignalSubmission.from_data(data)


def load_immediate_reflection(data: object) -> ImmediateReflectionRequest:
    return ImmediateReflectionRequest.from_data(data)


def load_reflection_abandonment(data: object) -> ReflectionAbandonmentRequest:
    return ReflectionAbandonmentRequest.from_data(data)


def stage_learning_signal(
    database_path: Path,
    submission: LearningSignalSubmission,
    *,
    idempotency_key: str,
) -> tuple[bytes, LearningSignalCapture]:
    if submission.signal_kind is None:
        return database_path.read_bytes(), LearningSignalCapture(False, None)
    normalized_key = idempotency_key.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise UserInputError("idempotency key must contain 1 to 200 characters")
    payload = submission.to_data()
    encoded = _json(payload).encode("utf-8")
    if len(encoded) > REFLECTION_INPUT_MAX_BYTES:
        raise UserInputError("learning signal input exceeds 8192 bytes")
    request_hash = hashlib.sha256(encoded).hexdigest()
    temporary_path = _copy_database(database_path)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            existing = connection.execute(
                "SELECT * FROM reflection_inputs WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if existing[2] != request_hash:
                    raise UserInputError(
                        "idempotency key was already used for a different request"
                    )
                return temporary_path.read_bytes(), LearningSignalCapture(
                    True, _input_from_row(existing)
                )
            input_id = f"rfi_{uuid.uuid4().hex}"
            created_at = datetime.now(timezone.utc).isoformat()
            source_reference = submission.source_reference
            if source_reference is None:
                raise IntegrityError("captured learning signal has no source reference")
            connection.execute(
                """
                INSERT INTO reflection_inputs
                    (input_id, idempotency_key, request_hash, signal_kind, entrance,
                     task_pointer, occurred_at, excerpt, source_reference_json,
                     source_fingerprint, applicability_scope, context_coverage_json,
                     blind_spots_json, sensitivity, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_id,
                    normalized_key,
                    request_hash,
                    submission.signal_kind,
                    submission.entrance,
                    submission.task_pointer,
                    submission.occurred_at,
                    submission.excerpt,
                    _json(source_reference.to_data()),
                    submission.source_fingerprint,
                    submission.applicability_scope,
                    _json(list(submission.context_coverage)),
                    _json(list(submission.blind_spots)),
                    submission.sensitivity,
                    created_at,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM reflection_inputs WHERE input_id = ?", (input_id,)
            ).fetchone()
            if row is None:
                raise IntegrityError("captured reflection input disappeared")
            return temporary_path.read_bytes(), LearningSignalCapture(
                True, _input_from_row(row)
            )
    except sqlite3.Error as error:
        raise IntegrityError("cannot stage learning signal input") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def read_reflection_inputs(
    database_path: Path,
    *,
    limit: int,
    budget_bytes: int,
) -> tuple[tuple[ReflectionInput, ...], bool, int]:
    if limit < 1 or limit > 100:
        raise UserInputError("reflection input limit must be between 1 and 100")
    if budget_bytes < 1024 or budget_bytes > 128 * 1024:
        raise UserInputError(
            "reflection input budget must be between 1024 and 131072 bytes"
        )
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            rows = connection.execute(
                "SELECT * FROM reflection_inputs ORDER BY created_at, input_id LIMIT ?",
                (limit + 1,),
            ).fetchall()
    except sqlite3.Error as error:
        raise IntegrityError("cannot read reflection inputs") from error
    available = tuple(_input_from_row(row) for row in rows)
    selected: list[ReflectionInput] = []
    used_bytes = len(b'{"inputs":[]}')
    truncated = len(available) > limit
    for reflection_input in available[:limit]:
        input_bytes = len(_json(reflection_input.to_data()).encode("utf-8"))
        separator_bytes = 1 if selected else 0
        if used_bytes + input_bytes + separator_bytes > budget_bytes:
            truncated = True
            break
        selected.append(reflection_input)
        used_bytes += input_bytes + separator_bytes
    return tuple(selected), truncated, used_bytes


def stage_immediate_reflection(
    database_path: Path,
    request: ImmediateReflectionRequest,
    *,
    idempotency_key: str,
) -> tuple[bytes, ImmediateReflectionResult]:
    normalized_key = idempotency_key.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise UserInputError("idempotency key must contain 1 to 200 characters")
    request_hash = hashlib.sha256(
        _json(request.to_data()).encode("utf-8")
    ).hexdigest()
    existing = _existing_reflection_result(
        database_path, idempotency_key=normalized_key, request_hash=request_hash
    )
    if existing is not None:
        return database_path.read_bytes(), existing
    temporary_path = _copy_database(database_path)
    try:
        reflection_inputs = _selected_inputs(temporary_path, request.input_ids)
        inputs_by_id = {
            reflection_input.input_id: reflection_input
            for reflection_input in reflection_inputs
        }
        source_status_by_input: dict[str, dict[str, object]] = {}
        candidate_ids = tuple(candidate.candidate_id for candidate in request.proposals)
        relation_owners = _oriented_candidate_relations(request.proposals)
        candidate_proposal_ids: dict[str, str] = {}
        for candidate in request.proposals:
            candidate_inputs = tuple(inputs_by_id[input_id] for input_id in candidate.input_ids)
            receipts, candidate_status, source_blind_spots = _input_receipts(
                candidate_inputs,
                evidence_retention=candidate.proposal.evidence_retention,
            )
            source_status_by_input.update(
                (cast(str, status["input_id"]), status)
                for status in candidate_status
            )
            relations = relation_owners[candidate.candidate_id]
            near_ids = tuple(
                candidate_proposal_ids[related_id]
                for related_id, relation_type in relations
                if relation_type == "near"
            )
            conflict_ids = tuple(
                candidate_proposal_ids[related_id]
                for related_id, relation_type in relations
                if relation_type == "conflict"
            )
            # Relations always point to candidates staged earlier; exact duplicates
            # are allowed to resolve to the same stable proposal and need no edge.
            derived_evidence: tuple[dict[str, object], ...] = ()
            if candidate.derivation is not None:
                derived_evidence = (
                    {
                        "kind": "reflection-derivation",
                        "explanation": candidate.derivation,
                    },
                )
            local_only = any(
                reflection_input.submission.sensitivity == "local-only"
                for reflection_input in candidate_inputs
            )
            payload = replace(
                candidate.proposal,
                supporting_evidence=_merge_objects(
                    candidate.proposal.supporting_evidence,
                    receipts + derived_evidence,
                ),
                context_coverage=_merge_text(
                    candidate.proposal.context_coverage,
                    tuple(
                        coverage
                        for reflection_input in candidate_inputs
                        for coverage in reflection_input.submission.context_coverage
                    ),
                ),
                blind_spots=_merge_text(
                    candidate.proposal.blind_spots,
                    tuple(
                        blind_spot
                        for reflection_input in candidate_inputs
                        for blind_spot in reflection_input.submission.blind_spots
                    )
                    + source_blind_spots,
                ),
                near_proposal_ids=tuple(dict.fromkeys(near_ids)),
                conflict_proposal_ids=tuple(dict.fromkeys(conflict_ids)),
                sensitivity=(
                    "local-only" if local_only else candidate.proposal.sensitivity
                ),
                migration_restrictions=_merge_text(
                    candidate.proposal.migration_restrictions,
                    (
                        ("contains-local-only-reflection-input",)
                        if local_only
                        else ()
                    ),
                ),
            )
            staged_database, submission = stage_review_proposal(
                temporary_path,
                payload,
                idempotency_key=f"reflection:{normalized_key}:{candidate.candidate_id}",
            )
            temporary_path.write_bytes(staged_database)
            candidate_proposal_ids[candidate.candidate_id] = (
                submission.proposal.proposal_id
            )
        if set(candidate_proposal_ids) != set(candidate_ids):
            raise IntegrityError("reflection did not submit every candidate")
        run_id = f"rfr_{uuid.uuid4().hex}"
        completed_at = datetime.now(timezone.utc).isoformat()
        result = ImmediateReflectionResult(
            run_id=run_id,
            status="completed",
            candidate_proposal_ids=candidate_proposal_ids,
            cleaned_input_ids=request.input_ids,
            source_status=tuple(
                source_status_by_input[input_id] for input_id in request.input_ids
            ),
        )
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executemany(
                "DELETE FROM reflection_inputs WHERE input_id = ?",
                ((input_id,) for input_id in request.input_ids),
            )
            connection.execute(
                """
                INSERT INTO reflection_runs
                    (run_id, trigger, status, idempotency_key, request_hash,
                     input_count, result_json, abandonment_reason, created_at,
                     completed_at)
                VALUES (?, 'explicit', 'completed', ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    run_id,
                    normalized_key,
                    request_hash,
                    len(request.input_ids),
                    _json(result.to_data()),
                    completed_at,
                    completed_at,
                ),
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot complete immediate reflection") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def stage_reflection_abandonment(
    database_path: Path,
    request: ReflectionAbandonmentRequest,
    *,
    idempotency_key: str,
) -> tuple[bytes, ImmediateReflectionResult]:
    normalized_key = idempotency_key.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise UserInputError("idempotency key must contain 1 to 200 characters")
    request_hash = hashlib.sha256(
        _json(request.to_data()).encode("utf-8")
    ).hexdigest()
    existing = _existing_reflection_result(
        database_path, idempotency_key=normalized_key, request_hash=request_hash
    )
    if existing is not None:
        return database_path.read_bytes(), existing
    temporary_path = _copy_database(database_path)
    try:
        _selected_inputs(temporary_path, request.input_ids)
        completed_at = datetime.now(timezone.utc).isoformat()
        result = ImmediateReflectionResult(
            run_id=f"rfr_{uuid.uuid4().hex}",
            status="abandoned",
            candidate_proposal_ids={},
            cleaned_input_ids=request.input_ids,
            source_status=(),
        )
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executemany(
                "DELETE FROM reflection_inputs WHERE input_id = ?",
                ((input_id,) for input_id in request.input_ids),
            )
            connection.execute(
                """
                INSERT INTO reflection_runs
                    (run_id, trigger, status, idempotency_key, request_hash,
                     input_count, result_json, abandonment_reason, created_at,
                     completed_at)
                VALUES (?, 'explicit', 'abandoned', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    normalized_key,
                    request_hash,
                    len(request.input_ids),
                    _json(result.to_data()),
                    request.reason,
                    completed_at,
                    completed_at,
                ),
            )
            connection.commit()
        return temporary_path.read_bytes(), result
    except sqlite3.Error as error:
        raise IntegrityError("cannot abandon reflection inputs") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _existing_reflection_result(
    database_path: Path,
    *,
    idempotency_key: str,
    request_hash: str,
) -> ImmediateReflectionResult | None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT request_hash, result_json FROM reflection_runs "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
    except sqlite3.Error as error:
        raise IntegrityError("cannot inspect reflection run") from error
    if row is None:
        return None
    if row[0] != request_hash:
        raise UserInputError("idempotency key was already used for a different request")
    if not isinstance(row[1], str):
        raise IntegrityError("reflection run result is invalid")
    try:
        data = json.loads(row[1])
    except json.JSONDecodeError as error:
        raise IntegrityError("reflection run result is invalid") from error
    return _reflection_result_from_data(data)


def _selected_inputs(
    database_path: Path, input_ids: tuple[str, ...]
) -> tuple[ReflectionInput, ...]:
    if len(input_ids) != len(set(input_ids)):
        raise UserInputError("immediate reflection input ids must be unique")
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            rows = tuple(
                connection.execute(
                    "SELECT * FROM reflection_inputs WHERE input_id = ?", (input_id,)
                ).fetchone()
                for input_id in input_ids
            )
    except sqlite3.Error as error:
        raise IntegrityError("cannot read immediate reflection inputs") from error
    missing = next(
        (input_id for input_id, row in zip(input_ids, rows, strict=True) if row is None),
        None,
    )
    if missing is not None:
        raise UserInputError(f"reflection input does not exist: {missing}")
    return tuple(
        _input_from_row(cast(tuple[object, ...], row)) for row in rows
    )


def _input_receipts(
    reflection_inputs: tuple[ReflectionInput, ...],
    *,
    evidence_retention: Literal["full", "excerpt", "receipt"],
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[str, ...],
]:
    receipts: list[dict[str, object]] = []
    statuses: list[dict[str, object]] = []
    blind_spots: list[str] = []
    for reflection_input in reflection_inputs:
        submission = reflection_input.submission
        reference = submission.source_reference
        if reference is None or submission.source_fingerprint is None:
            raise IntegrityError("reflection input source receipt is incomplete")
        status = "unavailable"
        locator = Path(reference.locator)
        try:
            if locator.is_file():
                actual = hashlib.sha256(locator.read_bytes()).hexdigest()
                status = (
                    "unchanged"
                    if actual == submission.source_fingerprint
                    else "changed"
                )
        except OSError:
            status = "unavailable"
        if status != "unchanged":
            blind_spots.append(
                f"source {reference.source_id} is {status} since signal capture"
            )
        statuses.append(
            {
                "input_id": reflection_input.input_id,
                "source_id": reference.source_id,
                "status": status,
            }
        )
        receipt: dict[str, object] = {
            "kind": "reflection-input-receipt",
            "source_reference": reference.to_data(),
            "source_fingerprint": submission.source_fingerprint,
            "occurred_at": submission.occurred_at,
            "applicability_scope": submission.applicability_scope,
            "retention": evidence_retention,
        }
        if evidence_retention != "receipt":
            receipt["excerpt"] = submission.excerpt
        receipts.append(receipt)
    return tuple(receipts), tuple(statuses), tuple(blind_spots)


def _oriented_candidate_relations(
    candidates: tuple[ReflectionCandidate, ...],
) -> dict[str, tuple[tuple[str, str], ...]]:
    positions = {
        candidate.candidate_id: index for index, candidate in enumerate(candidates)
    }
    owned: dict[str, list[tuple[str, str]]] = {
        candidate.candidate_id: [] for candidate in candidates
    }
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        relations = tuple(
            (related_id, "near") for related_id in candidate.near_candidate_ids
        ) + tuple(
            (related_id, "conflict")
            for related_id in candidate.conflict_candidate_ids
        )
        for related_id, relation_type in relations:
            first, second = sorted((candidate.candidate_id, related_id))
            key = (first, second, relation_type)
            if key in seen:
                continue
            seen.add(key)
            owner = max(
                (candidate.candidate_id, related_id), key=lambda item: positions[item]
            )
            other = related_id if owner == candidate.candidate_id else candidate.candidate_id
            owned[owner].append((other, relation_type))
    return {key: tuple(value) for key, value in owned.items()}


def _merge_objects(
    first: tuple[dict[str, object], ...],
    second: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    merged: dict[str, dict[str, object]] = {}
    for item in first + second:
        merged[_json(item)] = item
    return tuple(merged[key] for key in sorted(merged))


def _merge_text(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(first + second))


def _reflection_result_from_data(data: object) -> ImmediateReflectionResult:
    if not isinstance(data, dict):
        raise IntegrityError("reflection run result is invalid")
    candidate_map = data.get("candidate_proposal_ids")
    source_status = data.get("source_status")
    run_id = data.get("run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(candidate_map, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in candidate_map.items()
        )
        or not isinstance(source_status, list)
        or not all(_valid_source_status(item) for item in source_status)
    ):
        raise IntegrityError("reflection run result is invalid")
    status = data.get("status")
    if status not in ("completed", "abandoned"):
        raise IntegrityError("reflection run result is invalid")
    return ImmediateReflectionResult(
        run_id=run_id,
        status=cast(Literal["completed", "abandoned"], status),
        candidate_proposal_ids=cast(dict[str, str], candidate_map),
        cleaned_input_ids=_data_text_tuple(data.get("cleaned_input_ids")),
        source_status=tuple(cast(dict[str, object], item) for item in source_status),
    )


def _valid_source_status(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("input_id"), str)
        and isinstance(data.get("source_id"), str)
        and data.get("status") in ("unchanged", "changed", "unavailable")
    )


def _input_from_row(row: tuple[object, ...]) -> ReflectionInput:
    if len(row) != 15 or not all(
        isinstance(row[index], str) for index in range(15)
    ):
        raise IntegrityError("reflection input is invalid")
    try:
        source_reference = json.loads(cast(str, row[8]))
        coverage = json.loads(cast(str, row[11]))
        blind_spots = json.loads(cast(str, row[12]))
    except json.JSONDecodeError as error:
        raise IntegrityError("reflection input JSON is invalid") from error
    submission = LearningSignalSubmission.from_data(
        {
            "signal_kind": row[3],
            "entrance": row[4],
            "task_pointer": row[5],
            "occurred_at": row[6],
            "excerpt": row[7],
            "source_reference": source_reference,
            "source_fingerprint": row[9],
            "applicability_scope": row[10],
            "context_coverage": coverage,
            "blind_spots": blind_spots,
            "sensitivity": row[13],
        }
    )
    return ReflectionInput(
        input_id=cast(str, row[0]),
        submission=submission,
        created_at=cast(str, row[14]),
    )


def _required_text(data: dict[object, object], key: str, *, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UserInputError(f"learning signal {key} must not be blank")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise UserInputError(f"learning signal {key} is too long")
    return normalized


def _required_text_list(data: dict[object, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise UserInputError(f"learning signal {key} must be a non-empty array")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise UserInputError(f"learning signal {key} must contain non-blank text")
    normalized = tuple(cast(str, item).strip() for item in value)
    if any(len(item) > 1000 for item in normalized):
        raise UserInputError(f"learning signal {key} item is too long")
    return normalized


def _optional_text_list(data: dict[object, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise UserInputError(f"reflection {key} must contain non-blank text")
    return tuple(cast(str, item).strip() for item in value)


def _data_text_tuple(data: object) -> tuple[str, ...]:
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise IntegrityError("reflection run result is invalid")
    return tuple(cast(str, item) for item in data)


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _copy_database(database_path: Path) -> Path:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".reflection-input.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        return temporary_path
    except OSError as error:
        raise IntegrityError("cannot stage reflection input") from error
