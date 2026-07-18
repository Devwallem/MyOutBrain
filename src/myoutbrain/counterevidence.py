from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Literal, cast

from myoutbrain.core_types import IntegrityError, UserInputError
from myoutbrain.local_core import LocalMemoryCore, MEMORY_DATABASE
from myoutbrain.persistence import atomic_commit, recover_transactions, writer_lock
from myoutbrain.protocol_contract import SERVER_PROTOCOL_VERSION
from myoutbrain.unified_review import (
    ReviewProposalInput,
    ReviewProposalSubmission,
    stage_review_proposal,
)
from myoutbrain.v2_recall import CapabilityAnswerability, V2RecallService


@dataclass(frozen=True)
class CounterevidenceSource:
    kind: Literal["local", "public"]
    source_id: str
    source_version: int
    locator: str
    content_hash: str
    observed_at: str
    applicability_scope: str

    @classmethod
    def from_data(cls, data: object) -> CounterevidenceSource:
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            raise UserInputError("counterevidence source must be a JSON object")
        source = cast(dict[str, object], data)
        kind = source.get("kind")
        if kind not in ("local", "public"):
            raise UserInputError("counterevidence source kind must be local or public")
        source_id = _text(source, "source_id", maximum=200)
        source_version = source.get("source_version")
        if not isinstance(source_version, int) or isinstance(source_version, bool) or source_version < 1:
            raise UserInputError("counterevidence source_version must be positive")
        locator = _text(source, "locator", maximum=2_000)
        content_hash = _text(source, "content_hash", maximum=64).lower()
        if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
            raise UserInputError("counterevidence content_hash must be SHA-256")
        observed_at = _text(source, "observed_at", maximum=100)
        try:
            observed = datetime.fromisoformat(observed_at)
        except ValueError as error:
            raise UserInputError("counterevidence observed_at must be ISO-8601") from error
        if observed.tzinfo is None:
            raise UserInputError("counterevidence observed_at must include a timezone")
        return cls(
            kind=kind,
            source_id=source_id,
            source_version=source_version,
            locator=locator,
            content_hash=content_hash,
            observed_at=observed_at,
            applicability_scope=_text(
                source, "applicability_scope", maximum=2_000
            ),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "locator": self.locator,
            "content_hash": self.content_hash,
            "observed_at": self.observed_at,
            "applicability_scope": self.applicability_scope,
            "relationship": "contradicts",
            "state": (
                "external-unintegrated" if self.kind == "public" else "local-counterevidence"
            ),
        }


@dataclass(frozen=True)
class CounterevidenceRequest:
    recall_id: str
    memory_id: str
    expected_version: int
    proposed_understanding: str
    applicability_scope: str
    source: CounterevidenceSource

    @classmethod
    def from_data(cls, data: object) -> CounterevidenceRequest:
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            raise UserInputError("counterevidence payload must be a JSON object")
        payload = cast(dict[str, object], data)
        expected_version = payload.get("expected_version")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
        ):
            raise UserInputError("counterevidence expected_version must be positive")
        recall_id = _text(payload, "recall_id", maximum=200)
        memory_id = _text(payload, "memory_id", maximum=200)
        if not recall_id.startswith("rec_"):
            raise UserInputError("counterevidence recall_id is invalid")
        if not memory_id.startswith("mem_"):
            raise UserInputError("counterevidence memory_id is invalid")
        return cls(
            recall_id=recall_id,
            memory_id=memory_id,
            expected_version=expected_version,
            proposed_understanding=_text(
                payload, "proposed_understanding", maximum=8 * 1024
            ),
            applicability_scope=_text(
                payload, "applicability_scope", maximum=2_000
            ),
            source=CounterevidenceSource.from_data(payload.get("source")),
        )


def load_counterevidence_request(path: Path) -> CounterevidenceRequest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UserInputError(f"cannot read counterevidence payload: {path}") from error
    return CounterevidenceRequest.from_data(payload)


class CounterevidenceService:
    """Route one task-scoped contradiction into recall and unified review."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def route(
        self,
        request: CounterevidenceRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        core = LocalMemoryCore(self._root)
        core.inspect_schema_version()
        database_path = self._root / MEMORY_DATABASE
        recall_service = V2RecallService(self._root)
        target = recall_service.counterevidence_target(
            request.recall_id,
            request.memory_id,
        )
        if target.version != request.expected_version:
            raise UserInputError("counterevidence target version conflict")
        source_evidence = request.source.to_data()
        proposal = ReviewProposalInput.from_data(
            {
                "title": f"Counterevidence for {target.canonical_name}",
                "content": request.proposed_understanding,
                "intent": "integrate",
                "formation": "derived",
                "priority": "blocking",
                "applicability_scope": request.applicability_scope,
                "approval_effect": {
                    "type": "revise_canonical_memory",
                    "canonical_name": target.canonical_name,
                    "personal_cognition": False,
                },
                "target": {
                    "memory_id": request.memory_id,
                    "expected_version": request.expected_version,
                },
                "supporting_evidence": [source_evidence],
                "opposing_evidence": [
                    {
                        "kind": "canonical-memory",
                        "relationship": "supports-current-understanding",
                        "memory_id": request.memory_id,
                        "version": request.expected_version,
                        "state": target.state,
                        "content": target.body,
                        "applicability_scope": target.scope,
                    }
                ],
                "dependencies": [],
                "context_coverage": [
                    f"task:{target.task}",
                    f"recall:{request.recall_id}",
                ],
                "blind_spots": [],
                "near_proposal_ids": [],
                "conflict_proposal_ids": [],
                "sensitivity": (
                    "cloud-allowed" if request.source.kind == "public" else "local-only"
                ),
                "evidence_retention": "receipt",
                "migration_restrictions": [],
            }
        )
        with writer_lock(self._root):
            recover_transactions(self._root)
            staged_database, submission = _stage_counterevidence_proposal(
                database_path,
                proposal,
                source=request.source,
                idempotency_key=idempotency_key,
            )
            atomic_commit(self._root, [(database_path, staged_database)])
        answerability = recall_service.assess_answerability(
            request.recall_id,
            CapabilityAnswerability(answerable=True, reason="covered"),
        )["answerability"]
        if not isinstance(answerability, dict):
            raise IntegrityError("counterevidence answerability is malformed")
        unresolved_conflict = answerability.get("reason") == "unresolved-conflict"
        return {
            "protocol_version": SERVER_PROTOCOL_VERSION,
            "recall": {
                "recall_id": request.recall_id,
                "answerability": answerability,
                "counterevidence": [
                    {
                        **source_evidence,
                        "task": target.task,
                        "recall_id": request.recall_id,
                    }
                ],
                "signals": {"unresolved_conflict": unresolved_conflict},
            },
            "review_proposal": submission.proposal.to_data(),
            "deduplicated": submission.deduplicated,
        }


def _stage_counterevidence_proposal(
    database_path: Path,
    proposal: ReviewProposalInput,
    *,
    source: CounterevidenceSource,
    idempotency_key: str,
) -> tuple[bytes, ReviewProposalSubmission]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=".counterevidence.",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(database_path.read_bytes())
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            _register_counterevidence_source(connection, source)
            connection.commit()
        return stage_review_proposal(
            temporary_path,
            proposal,
            idempotency_key=idempotency_key,
        )
    except (OSError, sqlite3.Error) as error:
        raise IntegrityError("cannot stage counterevidence review") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _register_counterevidence_source(
    connection: sqlite3.Connection,
    source: CounterevidenceSource,
) -> None:
    source_row = connection.execute(
        """
        SELECT source_kind, current_locator
        FROM evidence_sources
        WHERE source_id = ?
        """,
        (source.source_id,),
    ).fetchone()
    version_row = connection.execute(
        """
        SELECT content_hash, locator, observed_at, applicability_scope, retention
        FROM evidence_source_versions
        WHERE source_id = ? AND version = ?
        """,
        (source.source_id, source.source_version),
    ).fetchone()
    expected_version = (
        connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM evidence_source_versions "
            "WHERE source_id = ?",
            (source.source_id,),
        ).fetchone()
        or (1,)
    )[0]
    if source_row is None:
        if source.source_version != 1:
            raise UserInputError("new counterevidence source must start at version 1")
        connection.execute(
            """
            INSERT INTO evidence_sources
                (source_id, source_kind, current_locator, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (source.source_id, source.kind, source.locator, source.observed_at),
        )
    elif source_row[0] != source.kind:
        raise UserInputError("counterevidence source kind conflicts with its identity")
    if version_row is not None:
        if version_row != (
            source.content_hash,
            source.locator,
            source.observed_at,
            source.applicability_scope,
            "receipt",
        ):
            raise UserInputError(
                "counterevidence source version conflicts with its stored receipt"
            )
        return
    if source.source_version != expected_version:
        raise UserInputError("counterevidence source versions must be contiguous")
    connection.execute(
        """
        INSERT INTO evidence_source_versions
            (source_id, version, content_hash, locator, observed_at,
             applicability_scope, retention)
        VALUES (?, ?, ?, ?, ?, ?, 'receipt')
        """,
        (
            source.source_id,
            source.source_version,
            source.content_hash,
            source.locator,
            source.observed_at,
            source.applicability_scope,
        ),
    )
    connection.execute(
        "UPDATE evidence_sources SET current_locator = ? WHERE source_id = ?",
        (source.locator, source.source_id),
    )


def _text(data: dict[str, object], name: str, *, maximum: int) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise UserInputError(
            f"counterevidence {name} must contain 1 to {maximum} characters"
        )
    return value.strip()
