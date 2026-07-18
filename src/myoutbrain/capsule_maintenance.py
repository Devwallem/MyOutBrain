from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tomllib
from typing import cast

from myoutbrain.core_types import (
    ConstraintConflict,
    IdempotencyConflict,
    IntegrityError,
    RecallRegressionFailure,
    UserInputError,
)
from myoutbrain.local_core import LocalMemoryCore, MEMORY_DATABASE
from myoutbrain.persistence import recover_transactions, writer_lock
from myoutbrain.v2_recall import (
    evaluate_fixed_recall_regression,
    fixed_recall_regression_cases,
)
from myoutbrain.unified_review import ReviewProposalInput


CAPSULE_TARGET_BYTES = 64 * 1024
CAPSULE_HARD_LIMIT_BYTES = 128 * 1024


class _StagedSemanticDifference(Exception):
    def __init__(self, differences: dict[str, dict[str, str]]) -> None:
        super().__init__("staged capsule changed canonical memory semantics")
        self.differences = differences


class CapsuleMaintenanceService:
    """Own capsule structure maintenance behind the shared domain gateway."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def inspect(self) -> dict[str, object]:
        LocalMemoryCore(self._root).initialize()
        database_path = self._root / MEMORY_DATABASE
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                version_row = connection.execute(
                    "SELECT structural_version FROM capsule_structure_state WHERE singleton = 1"
                ).fetchone()
                if version_row is None or not isinstance(version_row[0], int):
                    raise IntegrityError("capsule structure version is missing")
                rows = connection.execute(
                    """
                    SELECT capsule.capsule_id, capsule.topic, capsule.body_bytes,
                           capsule.memory_record_count, capsule.structural_version,
                           capsule.status, partition.partition_id,
                           partition.topic, partition.display_name,
                           partition.pinned, partition.user_named,
                           partition.merge_forbidden,
                           partition.constraint_version,
                           (
                               SELECT json_group_array(dictionary.memory_id)
                               FROM knowledge_dictionary AS dictionary
                               WHERE dictionary.primary_capsule_id = capsule.capsule_id
                               ORDER BY dictionary.memory_id
                           )
                    FROM knowledge_capsules AS capsule
                    JOIN capsule_partitions AS assignment
                      ON assignment.capsule_id = capsule.capsule_id
                    JOIN knowledge_partitions AS partition
                      ON partition.partition_id = assignment.partition_id
                    ORDER BY capsule.created_at, capsule.capsule_id
                    """
                ).fetchall()
                redirect_rows = connection.execute(
                    """
                    SELECT source_capsule_id, target_capsule_id, reorganization_id
                    FROM capsule_redirects
                    ORDER BY source_capsule_id, target_capsule_id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot inspect capsule structure") from error
        return {
            "structural_version": version_row[0],
            "capsules": [
                {
                    "capsule_id": row[0],
                    "topic": row[1],
                    "body_bytes": row[2],
                    "memory_record_count": row[3],
                    "capsule_structural_version": row[4],
                    "status": row[5],
                    "primary_memory_ids": json.loads(cast(str, row[13])),
                    "partition": {
                        "partition_id": row[6],
                        "topic": row[7],
                        "name": row[8] if row[8] is not None else row[7],
                        "pinned": bool(row[9]),
                        "user_named": bool(row[10]),
                        "merge_forbidden": bool(row[11]),
                        "constraint_version": row[12],
                    },
                }
                for row in rows
            ],
            "redirects": [
                {
                    "source_capsule_id": row[0],
                    "target_capsule_id": row[1],
                    "reorganization_id": row[2],
                }
                for row in redirect_rows
            ],
        }

    def plan(self, parameters: dict[str, object]) -> dict[str, object]:
        _reject_unknown_fields(parameters, {"source_capsule_id", "topic_groups"})
        source_value = parameters.get("source_capsule_id")
        topic_groups = _topic_groups(parameters.get("topic_groups"))
        if (source_value is None) != (not topic_groups):
            raise UserInputError(
                "topic planning requires source_capsule_id and topic_groups together"
            )
        source_capsule_id = (
            _required_identifier(source_value, "source_capsule_id", "cap_")
            if source_value is not None
            else None
        )
        if topic_groups and len(topic_groups) < 2:
            raise UserInputError("topic split requires at least two topic groups")
        LocalMemoryCore(self._root).initialize()
        target_bytes, hard_limit_bytes = _capsule_budgets(self._root)
        database_path = self._root / MEMORY_DATABASE
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                version_row = connection.execute(
                    "SELECT structural_version FROM capsule_structure_state WHERE singleton = 1"
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT capsule.capsule_id, capsule.body_bytes,
                           capsule.memory_record_count, partition.parent_partition_id,
                           partition.normalized_topic, partition.pinned,
                           partition.user_named, partition.merge_forbidden
                    FROM knowledge_capsules AS capsule
                    JOIN capsule_partitions AS assignment
                      ON assignment.capsule_id = capsule.capsule_id
                    JOIN knowledge_partitions AS partition
                      ON partition.partition_id = assignment.partition_id
                    WHERE capsule.status = 'active'
                    ORDER BY capsule.capsule_id
                    """
                ).fetchall()
                topic_plan: dict[str, object] | None = None
                if source_capsule_id is not None:
                    source_row = next(
                        (row for row in rows if row[0] == source_capsule_id),
                        None,
                    )
                    if source_row is None:
                        raise UserInputError(
                            f"unknown active source capsule: {source_capsule_id}"
                        )
                    if bool(source_row[5]) or bool(source_row[6]):
                        raise ConstraintConflict(
                            "creator partition constraints forbid automatic capsule split"
                        )
                    memory_rows = connection.execute(
                        """
                        SELECT dictionary.memory_id,
                               length(CAST(version.content AS BLOB))
                        FROM knowledge_dictionary AS dictionary
                        JOIN canonical_memory_versions AS version
                          ON version.memory_id = dictionary.memory_id
                         AND version.version = dictionary.current_version
                        WHERE dictionary.primary_capsule_id = ?
                        ORDER BY dictionary.memory_id
                        """,
                        (source_capsule_id,),
                    ).fetchall()
                    source_memory_ids = {
                        cast(str, memory_row[0]) for memory_row in memory_rows
                    }
                    planned_memory_ids = [
                        cast(str, memory_id)
                        for group in topic_groups
                        for memory_id in cast(
                            tuple[object, ...], group["memory_ids"]
                        )
                    ]
                    if (
                        len(planned_memory_ids) != len(set(planned_memory_ids))
                        or set(planned_memory_ids) != source_memory_ids
                    ):
                        raise UserInputError(
                            "topic groups must contain every source memory exactly once"
                        )
                    memory_bytes = {
                        cast(str, memory_row[0]): cast(int, memory_row[1])
                        for memory_row in memory_rows
                    }
                    if any(
                        sum(memory_bytes[memory_id] for memory_id in cast(tuple[str, ...], group["memory_ids"]))
                        > hard_limit_bytes
                        for group in topic_groups
                    ):
                        raise UserInputError(
                            "topic split target exceeds capsule_hard_limit_bytes"
                        )
                    topic_plan = {
                        "action": "split",
                        "reason": "topic-divergence",
                        "source_capsule_ids": [source_capsule_id],
                        "topic_groups": [
                            {
                                "topic": group["topic"],
                                "memory_ids": list(
                                    cast(tuple[str, ...], group["memory_ids"])
                                ),
                            }
                            for group in topic_groups
                        ],
                    }
        except sqlite3.Error as error:
            raise IntegrityError("cannot plan capsule maintenance") from error
        if version_row is None or not isinstance(version_row[0], int):
            raise IntegrityError("capsule structure version is missing")
        plans: list[dict[str, object]] = []
        if topic_plan is not None:
            plans.append(topic_plan)
        for row in rows:
            if (
                cast(int, row[1]) >= hard_limit_bytes
                and cast(int, row[2]) >= 2
                and not bool(row[5])
                and not bool(row[6])
            ):
                plans.append(
                    {
                        "action": "split",
                        "reason": "capacity-hard-limit",
                        "source_capsule_ids": [row[0]],
                    }
                )
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                if (
                    left[3] == right[3]
                    and left[4] == right[4]
                    and cast(int, left[1]) + cast(int, right[1]) < target_bytes
                    and not any(bool(value) for value in (*left[5:8], *right[5:8]))
                ):
                    plans.append(
                        {
                            "action": "merge",
                            "reason": "sparse-same-topic",
                            "source_capsule_ids": sorted((left[0], right[0])),
                        }
                    )
        return {
            "structural_version": version_row[0],
            "budgets": {
                "target_bytes": target_bytes,
                "hard_limit_bytes": hard_limit_bytes,
            },
            "plans": plans,
        }

    def configure_partition(
        self,
        parameters: dict[str, object],
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        partition_id = _required_identifier(
            parameters.get("partition_id"), "partition_id", "prt_"
        )
        _reject_unknown_fields(
            parameters,
            {"partition_id", "name", "pinned", "merge_forbidden"},
        )
        name_value = parameters.get("name")
        name = None if name_value is None else _required_text(name_value, "name")
        pinned = _optional_boolean(parameters.get("pinned"), "pinned")
        merge_forbidden = _optional_boolean(
            parameters.get("merge_forbidden"), "merge_forbidden"
        )
        normalized_key = _required_text(idempotency_key, "idempotency_key")
        request_hash = _stable_hash(
            {
                "partition_id": partition_id,
                "name": name,
                "pinned": pinned,
                "merge_forbidden": merge_forbidden,
                "expected_version": expected_version,
            }
        )
        LocalMemoryCore(self._root).initialize()
        database_path = self._root / MEMORY_DATABASE
        configured_at = datetime.now(timezone.utc).isoformat()
        detected_difference: dict[str, dict[str, str]] | None = None
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    existing = connection.execute(
                        """
                        SELECT request_hash, result_json
                        FROM partition_constraint_writes
                        WHERE idempotency_key = ?
                        """,
                        (normalized_key,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != request_hash:
                            raise IdempotencyConflict(
                                "idempotency key was already used for a different request"
                            )
                        replay = json.loads(cast(str, existing[1]))
                        if not isinstance(replay, dict):
                            raise IntegrityError("partition constraint replay is invalid")
                        return cast(dict[str, object], replay)
                    row = connection.execute(
                        """
                        SELECT topic, display_name, pinned, user_named,
                               merge_forbidden, constraint_version
                        FROM knowledge_partitions
                        WHERE partition_id = ? AND node_kind = 'leaf'
                        """,
                        (partition_id,),
                    ).fetchone()
                    if row is None:
                        raise UserInputError(f"unknown leaf partition: {partition_id}")
                    actual_version = cast(int, row[5])
                    if actual_version != expected_version:
                        raise ConstraintConflict(
                            "partition constraint version does not match expected_version"
                        )
                    next_name = name if name is not None else cast(str | None, row[1])
                    next_pinned = pinned if pinned is not None else bool(row[2])
                    next_user_named = name is not None or bool(row[3])
                    next_merge_forbidden = (
                        merge_forbidden
                        if merge_forbidden is not None
                        else bool(row[4])
                    )
                    next_version = actual_version + 1
                    connection.execute(
                        """
                        UPDATE knowledge_partitions
                        SET display_name = ?, pinned = ?, user_named = ?,
                            merge_forbidden = ?, constraint_version = ?
                        WHERE partition_id = ? AND constraint_version = ?
                        """,
                        (
                            next_name,
                            int(next_pinned),
                            int(next_user_named),
                            int(next_merge_forbidden),
                            next_version,
                            partition_id,
                            actual_version,
                        ),
                    )
                    result: dict[str, object] = {
                        "constraint": {
                            "partition_id": partition_id,
                            "name": next_name if next_name is not None else row[0],
                            "pinned": next_pinned,
                            "user_named": next_user_named,
                            "merge_forbidden": next_merge_forbidden,
                            "constraint_version": next_version,
                        }
                    }
                    connection.execute(
                        """
                        INSERT INTO partition_constraint_writes
                            (idempotency_key, request_hash, result_json, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            normalized_key,
                            request_hash,
                            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                            configured_at,
                        ),
                    )
                    connection.commit()
                    return result
        except sqlite3.Error as error:
            raise IntegrityError("cannot configure partition constraints") from error

    def reorganize(
        self,
        parameters: dict[str, object],
        *,
        expected_version: int,
        idempotency_key: str,
        entrance: str,
    ) -> dict[str, object]:
        _reject_unknown_fields(
            parameters,
            {"action", "source_capsule_ids", "topic_groups", "proposed_bodies"},
        )
        action = parameters.get("action")
        if action not in ("split", "merge"):
            raise UserInputError("maintenance action must be split or merge")
        source_capsule_ids = _identifier_list(
            parameters.get("source_capsule_ids"), "source_capsule_ids", "cap_"
        )
        if action == "merge" and len(source_capsule_ids) < 2:
            raise UserInputError("capsule merge requires at least two source capsules")
        if action == "split" and len(source_capsule_ids) != 1:
            raise UserInputError("capsule split requires exactly one source capsule")
        topic_groups = _topic_groups(parameters.get("topic_groups"))
        if action == "merge" and topic_groups:
            raise UserInputError("capsule merge does not accept topic_groups")
        proposed_bodies = _proposed_bodies(parameters.get("proposed_bodies"))
        normalized_key = _required_text(idempotency_key, "idempotency_key")
        normalized_entrance = _required_text(entrance, "entrance")
        request_hash = _stable_hash(
            {
                "action": action,
                "source_capsule_ids": source_capsule_ids,
                "topic_groups": topic_groups,
                "proposed_bodies": proposed_bodies,
                "expected_version": expected_version,
            }
        )
        reorganization_id = "reo_" + hashlib.sha256(
            normalized_key.encode("utf-8")
        ).hexdigest()[:32]
        LocalMemoryCore(self._root).initialize()
        capsule_target_bytes, capsule_hard_limit_bytes = _capsule_budgets(self._root)
        database_path = self._root / MEMORY_DATABASE
        if proposed_bodies:
            return self._abort_semantic_difference(
                database_path,
                reorganization_id=reorganization_id,
                action=action,
                source_capsule_ids=source_capsule_ids,
                expected_version=expected_version,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                proposed_bodies=proposed_bodies,
            )
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    existing = connection.execute(
                        """
                        SELECT request_hash, status, result_json
                        FROM capsule_reorganizations
                        WHERE idempotency_key = ?
                        """,
                        (normalized_key,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != request_hash:
                            raise IdempotencyConflict(
                                "idempotency key was already used for a different request"
                            )
                        if existing[1] in ("retired", "aborted") and existing[2] is not None:
                            replay = json.loads(cast(str, existing[2]))
                            if not isinstance(replay, dict):
                                raise IntegrityError(
                                    "capsule reorganization replay is invalid"
                                )
                            return cast(dict[str, object], replay)
                    else:
                        self._plan_reorganization(
                            connection,
                            reorganization_id=reorganization_id,
                            action=action,
                            source_capsule_ids=source_capsule_ids,
                            expected_version=expected_version,
                            idempotency_key=normalized_key,
                            request_hash=request_hash,
                            topic_groups=topic_groups,
                            capsule_target_bytes=capsule_target_bytes,
                            capsule_hard_limit_bytes=capsule_hard_limit_bytes,
                        )
                    try:
                        return self._complete_reorganization(
                            connection,
                            reorganization_id=reorganization_id,
                            entrance=normalized_entrance,
                        )
                    except _StagedSemanticDifference as difference:
                        connection.rollback()
                        self._cleanup_pre_switch(
                            connection,
                            reorganization_id=reorganization_id,
                            reset_to_planned=False,
                        )
                        detected_difference = difference.differences
                    except RecallRegressionFailure:
                        connection.rollback()
                        self._cleanup_pre_switch(
                            connection,
                            reorganization_id=reorganization_id,
                            reset_to_planned=True,
                        )
                        raise
                    except ConstraintConflict:
                        connection.rollback()
                        self._cleanup_pre_switch(
                            connection,
                            reorganization_id=reorganization_id,
                            reset_to_planned=False,
                        )
                        raise
                    except IntegrityError:
                        connection.rollback()
                        self._cleanup_pre_switch(
                            connection,
                            reorganization_id=reorganization_id,
                            reset_to_planned=True,
                        )
                        raise
        except sqlite3.Error as error:
            raise IntegrityError("cannot plan capsule reorganization") from error
        if detected_difference is None:
            raise IntegrityError("capsule reorganization ended without a result")
        return self._abort_semantic_difference(
            database_path,
            reorganization_id=reorganization_id,
            action=action,
            source_capsule_ids=source_capsule_ids,
            expected_version=expected_version,
            idempotency_key=normalized_key,
            request_hash=request_hash,
            proposed_bodies={
                memory_id: difference["before_body"]
                for memory_id, difference in detected_difference.items()
            },
            detected_differences=detected_difference,
        )

    @staticmethod
    def _cleanup_pre_switch(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        reset_to_planned: bool,
    ) -> None:
        row = connection.execute(
            """
            SELECT status, target_capsule_ids_json
            FROM capsule_reorganizations
            WHERE reorganization_id = ?
            """,
            (reorganization_id,),
        ).fetchone()
        if row is None or row[0] not in ("planned", "staged", "validated"):
            return
        target_capsule_ids = _stored_identifiers(row[1], "target capsules")
        placeholders = ", ".join("?" for _ in target_capsule_ids)
        partition_rows = connection.execute(
            f"""
            SELECT partition_id FROM capsule_partitions
            WHERE capsule_id IN ({placeholders})
            """,
            target_capsule_ids,
        ).fetchall()
        target_partition_ids = tuple(cast(str, item[0]) for item in partition_rows)
        connection.execute(
            "DELETE FROM capsule_staged_records WHERE reorganization_id = ?",
            (reorganization_id,),
        )
        connection.execute(
            f"DELETE FROM capsule_partitions WHERE capsule_id IN ({placeholders})",
            target_capsule_ids,
        )
        connection.execute(
            f"DELETE FROM knowledge_capsules WHERE capsule_id IN ({placeholders})",
            target_capsule_ids,
        )
        if target_partition_ids:
            partition_placeholders = ", ".join("?" for _ in target_partition_ids)
            connection.execute(
                f"""
                DELETE FROM knowledge_partitions
                WHERE partition_id IN ({partition_placeholders})
                """,
                target_partition_ids,
            )
        if reset_to_planned:
            connection.execute(
                """
                UPDATE capsule_reorganizations
                SET status = 'planned', updated_at = ?
                WHERE reorganization_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), reorganization_id),
            )
        else:
            connection.execute(
                "DELETE FROM capsule_reorganizations WHERE reorganization_id = ?",
                (reorganization_id,),
            )
        connection.commit()

    def _abort_semantic_difference(
        self,
        database_path: Path,
        *,
        reorganization_id: str,
        action: str,
        source_capsule_ids: tuple[str, ...],
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        proposed_bodies: dict[str, str],
        detected_differences: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, object]:
        placeholders = ", ".join("?" for _ in source_capsule_ids)
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                version_row = connection.execute(
                    "SELECT structural_version FROM capsule_structure_state WHERE singleton = 1"
                ).fetchone()
                if version_row is None or version_row[0] != expected_version:
                    raise ConstraintConflict(
                        "capsule structural version does not match expected_version"
                    )
                rows = connection.execute(
                    f"""
                    SELECT dictionary.memory_id, dictionary.current_version,
                           dictionary.canonical_name, version.content,
                           version.applicability_scope
                    FROM knowledge_dictionary AS dictionary
                    JOIN canonical_memory_versions AS version
                      ON version.memory_id = dictionary.memory_id
                     AND version.version = dictionary.current_version
                    JOIN knowledge_capsules AS capsule
                      ON capsule.capsule_id = dictionary.primary_capsule_id
                    WHERE dictionary.primary_capsule_id IN ({placeholders})
                      AND capsule.status = 'active'
                    ORDER BY dictionary.memory_id
                    """,
                    source_capsule_ids,
                ).fetchall()
        except sqlite3.Error as error:
            raise IntegrityError("cannot inspect proposed capsule semantics") from error
        by_memory = {cast(str, row[0]): row for row in rows}
        unknown = set(proposed_bodies).difference(by_memory)
        if unknown:
            raise UserInputError("proposed body does not belong to a source capsule")
        changed = [
            row
            for memory_id, row in by_memory.items()
            if memory_id in proposed_bodies
            and (
                detected_differences is not None
                or proposed_bodies[memory_id] != row[3]
            )
        ]
        if not changed:
            raise UserInputError(
                "proposed_bodies contains no semantic difference; omit it for maintenance"
            )
        proposals: list[dict[str, object]] = []
        for row in changed:
            memory_id = cast(str, row[0])
            detected = (
                detected_differences.get(memory_id)
                if detected_differences is not None
                else None
            )
            is_detected_drift = detected is not None
            content = proposed_bodies[memory_id]
            if detected is not None:
                content = (
                    "Investigate the semantic change detected during capsule "
                    f"maintenance for {row[2]}.\n\n"
                    f"Before integrity hash: {detected['before_hash']}\n"
                    f"Current integrity hash: {detected['current_hash']}\n\n"
                    f"Before body:\n{detected['before_body']}\n\n"
                    f"Current body:\n{detected['current_body']}"
                )
            payload = ReviewProposalInput.from_data(
                {
                    "title": f"Review semantic drift in {row[2]}",
                    "content": content,
                    "intent": "research" if is_detected_drift else "integrate",
                    "formation": "hypothesis" if is_detected_drift else "derived",
                    "priority": "routine",
                    "applicability_scope": row[4],
                    "approval_effect": {
                        "type": (
                            "create_research_thread"
                            if is_detected_drift
                            else "revise_canonical_memory"
                        ),
                        **({} if is_detected_drift else {"canonical_name": row[2]}),
                        "personal_cognition": False,
                    },
                    "target": {
                        "memory_id": memory_id,
                        "expected_version": row[1],
                    },
                    "supporting_evidence": [
                        {
                            "kind": "canonical-memory",
                            "memory_id": memory_id,
                            "version": row[1],
                        }
                    ],
                    "opposing_evidence": [],
                    "dependencies": [],
                    "context_coverage": ["capsule-maintenance-plan"],
                    "blind_spots": [
                        "Storage maintenance cannot decide semantic correctness.",
                        "The integrity hash may reflect relation, evidence, state, or name changes in addition to body changes.",
                    ],
                    "near_proposal_ids": [],
                    "conflict_proposal_ids": [],
                    "sensitivity": "local-only",
                    "evidence_retention": "receipt",
                    "migration_restrictions": [],
                }
            )
            submission = LocalMemoryCore(self._root).submit_review_proposal(
                payload,
                idempotency_key=f"{idempotency_key}:semantic:{memory_id}",
            )
            proposals.append(submission.proposal.to_data())
        result: dict[str, object] = {
            "reorganization_id": reorganization_id,
            "action": action,
            "status": "aborted",
            "reason": "semantic-change-requires-review",
            "structural_version": expected_version,
            "source_capsule_ids": list(source_capsule_ids),
            "target_capsule_ids": [],
            "proposal": proposals[0],
            "proposals": proposals,
        }
        now = datetime.now(timezone.utc).isoformat()
        try:
            with writer_lock(self._root):
                recover_transactions(self._root)
                with closing(sqlite3.connect(database_path)) as connection:
                    existing = connection.execute(
                        """
                        SELECT request_hash, result_json
                        FROM capsule_reorganizations
                        WHERE idempotency_key = ?
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != request_hash:
                            raise IdempotencyConflict(
                                "idempotency key was already used for a different request"
                            )
                        replay = json.loads(cast(str, existing[1]))
                        if not isinstance(replay, dict):
                            raise IntegrityError("aborted reorganization replay is invalid")
                        return cast(dict[str, object], replay)
                    connection.execute(
                        """
                        INSERT INTO capsule_reorganizations
                            (reorganization_id, idempotency_key, request_hash,
                             action, status, source_capsule_ids_json,
                             target_capsule_ids_json, plan_json,
                             expected_structural_version, result_json,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'aborted', ?, '[]', '[]', ?, ?, ?, ?)
                        """,
                        (
                            reorganization_id,
                            idempotency_key,
                            request_hash,
                            action,
                            json.dumps(source_capsule_ids, separators=(",", ":")),
                            expected_version,
                            json.dumps(
                                result,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            now,
                            now,
                        ),
                    )
                    connection.commit()
        except sqlite3.Error as error:
            raise IntegrityError("cannot record aborted capsule maintenance") from error
        return result

    @staticmethod
    def _plan_reorganization(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        action: str,
        source_capsule_ids: tuple[str, ...],
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        topic_groups: tuple[dict[str, object], ...],
        capsule_target_bytes: int,
        capsule_hard_limit_bytes: int,
    ) -> None:
        version_row = connection.execute(
            "SELECT structural_version FROM capsule_structure_state WHERE singleton = 1"
        ).fetchone()
        if version_row is None or version_row[0] != expected_version:
            raise ConstraintConflict(
                "capsule structural version does not match expected_version"
            )
        placeholders = ", ".join("?" for _ in source_capsule_ids)
        rows = connection.execute(
            f"""
            SELECT capsule.capsule_id, capsule.topic, capsule.body_bytes,
                   partition.partition_id, partition.normalized_topic,
                   partition.pinned, partition.user_named,
                   partition.merge_forbidden
            FROM knowledge_capsules AS capsule
            JOIN capsule_partitions AS assignment
              ON assignment.capsule_id = capsule.capsule_id
            JOIN knowledge_partitions AS partition
              ON partition.partition_id = assignment.partition_id
            WHERE capsule.capsule_id IN ({placeholders})
              AND capsule.status = 'active'
            ORDER BY capsule.capsule_id
            """,
            source_capsule_ids,
        ).fetchall()
        if len(rows) != len(source_capsule_ids):
            raise UserInputError("one or more active source capsules do not exist")
        if action == "merge":
            if any(bool(row[index]) for row in rows for index in (5, 6, 7)):
                raise ConstraintConflict(
                    "creator partition constraints forbid automatic capsule merge"
                )
            if len({row[4] for row in rows}) != 1:
                raise UserInputError(
                    "automatic capsule merge requires one unchanged topic"
                )
            if sum(cast(int, row[2]) for row in rows) >= capsule_target_bytes:
                raise UserInputError(
                    "automatic capsule merge requires combined content below target size"
                )
        source_memory_rows = connection.execute(
            f"""
            SELECT dictionary.memory_id,
                   length(CAST(version.content AS BLOB))
            FROM knowledge_dictionary AS dictionary
            JOIN canonical_memory_versions AS version
              ON version.memory_id = dictionary.memory_id
             AND version.version = dictionary.current_version
            WHERE dictionary.primary_capsule_id IN ({placeholders})
            ORDER BY dictionary.memory_id
            """,
            source_capsule_ids,
        ).fetchall()
        source_memory_ids = tuple(cast(str, row[0]) for row in source_memory_rows)
        memory_bytes = {
            cast(str, row[0]): cast(int, row[1]) for row in source_memory_rows
        }
        if action == "split":
            if any(bool(row[index]) for row in rows for index in (5, 6)):
                raise ConstraintConflict(
                    "creator partition constraints forbid automatic capsule split"
                )
            if not topic_groups:
                if cast(int, rows[0][2]) < capsule_hard_limit_bytes:
                    raise UserInputError(
                        "automatic capsule split requires the capacity hard limit"
                    )
                if len(source_memory_ids) < 2:
                    raise UserInputError(
                        "automatic capsule split requires at least two memories"
                    )
                source_topic = cast(str, rows[0][1])
                topic_groups = _capacity_split_groups(
                    source_memory_ids,
                    memory_bytes,
                    topic=source_topic,
                    hard_limit_bytes=capsule_hard_limit_bytes,
                )
            if len(topic_groups) < 2:
                raise UserInputError("topic split requires at least two topic groups")
            planned_memory_ids = tuple(
                cast(str, memory_id)
                for group in topic_groups
                for memory_id in cast(tuple[object, ...], group["memory_ids"])
            )
            if (
                len(planned_memory_ids) != len(set(planned_memory_ids))
                or set(planned_memory_ids) != set(source_memory_ids)
            ):
                raise UserInputError(
                    "topic groups must contain every source memory exactly once"
                )
            if any(
                sum(
                    memory_bytes[memory_id]
                    for memory_id in cast(tuple[str, ...], group["memory_ids"])
                )
                > capsule_hard_limit_bytes
                for group in topic_groups
            ):
                raise UserInputError(
                    "topic split target exceeds capsule_hard_limit_bytes"
                )
            plan = tuple(
                {
                    **group,
                    "merge_forbidden": bool(rows[0][7]),
                    "semantic_snapshots": {
                        memory_id: _semantic_snapshot(connection, memory_id)
                        for memory_id in cast(tuple[str, ...], group["memory_ids"])
                    },
                }
                for group in topic_groups
            )
        else:
            plan = (
                {
                    "topic": cast(str, rows[0][1]),
                    "memory_ids": source_memory_ids,
                    "merge_forbidden": False,
                    "semantic_snapshots": {
                        memory_id: _semantic_snapshot(connection, memory_id)
                        for memory_id in source_memory_ids
                    },
                },
            )
        target_capsule_ids = tuple(
            "cap_"
            + hashlib.sha256(
                f"{reorganization_id}:target:{index}".encode("utf-8")
            ).hexdigest()[:32]
            for index in range(len(plan))
        )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO capsule_reorganizations
                (reorganization_id, idempotency_key, request_hash, action,
                 status, source_capsule_ids_json, target_capsule_ids_json,
                 plan_json, expected_structural_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?)
            """,
            (
                reorganization_id,
                idempotency_key,
                request_hash,
                action,
                json.dumps(source_capsule_ids, separators=(",", ":")),
                json.dumps(target_capsule_ids, separators=(",", ":")),
                json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                expected_version,
                now,
                now,
            ),
        )
        connection.commit()
        _inject_capsule_fault("planned")

    def _complete_reorganization(
        self,
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        entrance: str,
    ) -> dict[str, object]:
        while True:
            row = connection.execute(
                """
                SELECT action, status, source_capsule_ids_json,
                       target_capsule_ids_json, plan_json,
                       expected_structural_version,
                       recall_regression_json, result_json
                FROM capsule_reorganizations
                WHERE reorganization_id = ?
                """,
                (reorganization_id,),
            ).fetchone()
            if row is None:
                raise IntegrityError("capsule reorganization plan disappeared")
            status = cast(str, row[1])
            source_capsule_ids = _stored_identifiers(row[2], "source capsules")
            target_capsule_ids = _stored_identifiers(row[3], "target capsules")
            if status == "planned":
                plan = _stored_topic_groups(row[4])
                self._stage_copy(
                    connection,
                    reorganization_id=reorganization_id,
                    source_capsule_ids=source_capsule_ids,
                    target_capsule_ids=target_capsule_ids,
                    plan=plan,
                )
                continue
            if status == "staged":
                self._validate_staged_records(
                    connection,
                    reorganization_id=reorganization_id,
                    source_capsule_ids=source_capsule_ids,
                )
                continue
            if status == "validated":
                self._switch_structure(
                    connection,
                    reorganization_id=reorganization_id,
                    source_capsule_ids=source_capsule_ids,
                    expected_version=cast(int, row[5]),
                    entrance=entrance,
                )
                continue
            if status == "switched":
                return self._retire_sources(
                    connection,
                    reorganization_id=reorganization_id,
                    action=cast(str, row[0]),
                    source_capsule_ids=source_capsule_ids,
                    target_capsule_ids=target_capsule_ids,
                    recall_regression_json=cast(str, row[6]),
                )
            if status == "retired" and row[7] is not None:
                result = json.loads(cast(str, row[7]))
                if not isinstance(result, dict):
                    raise IntegrityError("capsule reorganization result is invalid")
                return cast(dict[str, object], result)
            raise IntegrityError(f"capsule reorganization cannot resume from {status}")

    @staticmethod
    def _stage_copy(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        source_capsule_ids: tuple[str, ...],
        target_capsule_ids: tuple[str, ...],
        plan: tuple[dict[str, object], ...],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        target_assignments: dict[str, tuple[str, str]] = {}
        planned_snapshots = {
            memory_id: snapshot
            for group in plan
            for memory_id, snapshot in cast(
                dict[str, dict[str, str]], group["semantic_snapshots"]
            ).items()
        }
        current_snapshots = {
            memory_id: _semantic_snapshot(connection, memory_id)
            for memory_id in planned_snapshots
        }
        differences = {
            memory_id: {
                "before_body": planned_snapshot["body"],
                "before_hash": planned_snapshot["integrity_hash"],
                "current_body": current_snapshots[memory_id]["body"],
                "current_hash": current_snapshots[memory_id]["integrity_hash"],
            }
            for memory_id, planned_snapshot in planned_snapshots.items()
            if planned_snapshot != current_snapshots[memory_id]
        }
        if differences:
            raise _StagedSemanticDifference(differences)
        for target_capsule_id, group in zip(target_capsule_ids, plan, strict=True):
            topic = cast(str, group["topic"])
            target_partition_id = "prt_" + hashlib.sha256(
                target_capsule_id.encode("utf-8")
            ).hexdigest()[:32]
            connection.execute(
                """
                INSERT INTO knowledge_partitions
                    (partition_id, parent_partition_id, node_kind, topic,
                     normalized_topic, merge_forbidden)
                VALUES (?, 'prt_root', 'leaf', ?, ?, ?)
                """,
                (
                    target_partition_id,
                    topic,
                    " ".join(topic.casefold().split()),
                    int(bool(group.get("merge_forbidden", False))),
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_capsules
                    (capsule_id, topic, body_bytes, memory_record_count,
                     structural_version, status, created_at, updated_at)
                VALUES (?, ?, 0, 0, 1, 'staged', ?, ?)
                """,
                (target_capsule_id, topic, now, now),
            )
            connection.execute(
                "INSERT INTO capsule_partitions (capsule_id, partition_id) VALUES (?, ?)",
                (target_capsule_id, target_partition_id),
            )
            for memory_id in cast(tuple[str, ...], group["memory_ids"]):
                target_assignments[memory_id] = (
                    target_capsule_id,
                    target_partition_id,
                )
        placeholders = ", ".join("?" for _ in source_capsule_ids)
        rows = connection.execute(
            f"""
            SELECT dictionary.memory_id, dictionary.current_version,
                   version.content, dictionary.primary_capsule_id
            FROM knowledge_dictionary AS dictionary
            JOIN canonical_memory_versions AS version
              ON version.memory_id = dictionary.memory_id
             AND version.version = dictionary.current_version
            WHERE dictionary.primary_capsule_id IN ({placeholders})
            ORDER BY dictionary.memory_id
            """,
            source_capsule_ids,
        ).fetchall()
        if not rows:
            raise IntegrityError("capsule reorganization cannot stage empty sources")
        for memory_id, version, body, source_capsule_id in rows:
            if (
                not isinstance(memory_id, str)
                or not isinstance(version, int)
                or not isinstance(body, str)
                or not isinstance(source_capsule_id, str)
            ):
                raise IntegrityError("capsule source record is invalid")
            target = target_assignments.get(memory_id)
            if target is None:
                raise IntegrityError("capsule plan omitted a source memory")
            connection.execute(
                """
                INSERT INTO capsule_staged_records
                    (reorganization_id, target_capsule_id,
                     target_partition_id, source_capsule_id,
                     memory_id, memory_version, body, integrity_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reorganization_id,
                    target[0],
                    target[1],
                    source_capsule_id,
                    memory_id,
                    version,
                    body,
                    _memory_integrity_hash(connection, memory_id, version),
                ),
            )
        connection.execute(
            """
            UPDATE capsule_reorganizations
            SET status = 'staged', updated_at = ?
            WHERE reorganization_id = ? AND status = 'planned'
            """,
            (now, reorganization_id),
        )
        connection.commit()
        _inject_capsule_fault("staged")

    @staticmethod
    def _validate_staged_records(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        source_capsule_ids: tuple[str, ...],
    ) -> None:
        CapsuleMaintenanceService._assert_staged_records_intact(
            connection,
            reorganization_id=reorganization_id,
            source_capsule_ids=source_capsule_ids,
        )
        now = datetime.now(timezone.utc).isoformat()
        updated = connection.execute(
            """
            UPDATE capsule_reorganizations
            SET status = 'validated', updated_at = ?
            WHERE reorganization_id = ? AND status = 'staged'
            """,
            (now, reorganization_id),
        )
        if updated.rowcount != 1:
            raise IntegrityError("staged capsule validation state changed")
        connection.commit()
        _inject_capsule_fault("validated")

    @staticmethod
    def _assert_staged_records_intact(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        source_capsule_ids: tuple[str, ...],
    ) -> None:
        placeholders = ", ".join("?" for _ in source_capsule_ids)
        source_rows = connection.execute(
            f"""
            SELECT memory_id, current_version
            FROM knowledge_dictionary
            WHERE primary_capsule_id IN ({placeholders})
            ORDER BY memory_id
            """,
            source_capsule_ids,
        ).fetchall()
        staged_rows = connection.execute(
            """
            SELECT memory_id, memory_version, body, integrity_hash
            FROM capsule_staged_records
            WHERE reorganization_id = ?
            ORDER BY memory_id
            """,
            (reorganization_id,),
        ).fetchall()
        staged_snapshots = {
            row[0]: {
                "body": row[2],
                "integrity_hash": row[3],
            }
            for row in staged_rows
            if isinstance(row[0], str)
            and isinstance(row[2], str)
            and isinstance(row[3], str)
        }
        if [(row[0], row[1]) for row in source_rows] != [
            (row[0], row[1]) for row in staged_rows
        ]:
            differences = {
                memory_id: _semantic_difference(
                    staged_snapshot,
                    _semantic_snapshot(connection, memory_id),
                )
                for memory_id, staged_snapshot in staged_snapshots.items()
                if connection.execute(
                    "SELECT 1 FROM knowledge_dictionary WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                is not None
            }
            raise _StagedSemanticDifference(differences)
        for memory_id, version, body, integrity_hash in staged_rows:
            if not isinstance(memory_id, str) or not isinstance(version, int):
                raise IntegrityError("staged capsule identity is invalid")
            current_body = connection.execute(
                """
                SELECT content FROM canonical_memory_versions
                WHERE memory_id = ? AND version = ?
                """,
                (memory_id, version),
            ).fetchone()
            if (
                current_body is None
                or current_body[0] != body
                or integrity_hash
                != _memory_integrity_hash(connection, memory_id, version)
            ):
                before = staged_snapshots[memory_id]
                current = _semantic_snapshot(connection, memory_id)
                raise _StagedSemanticDifference(
                    {memory_id: _semantic_difference(before, current)}
                )

    @staticmethod
    def _switch_structure(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        source_capsule_ids: tuple[str, ...],
        expected_version: int,
        entrance: str,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        CapsuleMaintenanceService._assert_staged_records_intact(
            connection,
            reorganization_id=reorganization_id,
            source_capsule_ids=source_capsule_ids,
        )
        cases = fixed_recall_regression_cases(connection)
        before = evaluate_fixed_recall_regression(connection, cases)
        staged_rows = connection.execute(
            """
            SELECT memory_id, memory_version, target_capsule_id
            FROM capsule_staged_records
            WHERE reorganization_id = ?
            ORDER BY memory_id
            """,
            (reorganization_id,),
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for memory_id, version, target_capsule_id in staged_rows:
            dictionary_update = connection.execute(
                """
                UPDATE knowledge_dictionary
                SET primary_capsule_id = ?
                WHERE memory_id = ? AND current_version = ?
                """,
                (target_capsule_id, memory_id, version),
            )
            if dictionary_update.rowcount != 1:
                raise _StagedSemanticDifference(
                    CapsuleMaintenanceService._staged_differences(
                        connection, reorganization_id
                    )
                )
            version_update = connection.execute(
                """
                UPDATE canonical_memory_versions
                SET capsule_id = ?
                WHERE memory_id = ? AND version = ?
                """,
                (target_capsule_id, memory_id, version),
            )
            if version_update.rowcount != 1:
                raise _StagedSemanticDifference(
                    CapsuleMaintenanceService._staged_differences(
                        connection, reorganization_id
                    )
                )
            fts_update = connection.execute(
                "UPDATE canonical_memory_fts SET capsule_id = ? WHERE memory_id = ?",
                (target_capsule_id, memory_id),
            )
            if fts_update.rowcount != 1:
                raise IntegrityError("canonical FTS pointer closure changed before switch")
        target_ids = tuple(
            sorted({cast(str, row[2]) for row in staged_rows})
        )
        for target_id in target_ids:
            totals = connection.execute(
                """
                SELECT COALESCE(SUM(length(CAST(body AS BLOB))), 0), COUNT(*)
                FROM capsule_staged_records
                WHERE reorganization_id = ? AND target_capsule_id = ?
                """,
                (reorganization_id, target_id),
            ).fetchone()
            if totals is None:
                raise IntegrityError("staged capsule totals are missing")
            target_update = connection.execute(
                """
                UPDATE knowledge_capsules
                SET body_bytes = ?, memory_record_count = ?, status = 'active',
                    structural_version = ?, updated_at = ?
                WHERE capsule_id = ? AND status = 'staged'
                """,
                (totals[0], totals[1], expected_version + 1, now, target_id),
            )
            if target_update.rowcount != 1:
                raise IntegrityError("staged target capsule changed before switch")
        placeholders = ", ".join("?" for _ in source_capsule_ids)
        source_update = connection.execute(
            f"""
            UPDATE knowledge_capsules
            SET status = 'redirecting', updated_at = ?
            WHERE capsule_id IN ({placeholders}) AND status = 'active'
            """,
            (now, *source_capsule_ids),
        )
        if source_update.rowcount != len(source_capsule_ids):
            raise IntegrityError("source capsule state changed before switch")
        connection.executemany(
            """
            INSERT INTO capsule_redirects
                (source_capsule_id, target_capsule_id,
                 reorganization_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                (source_id, target_id, reorganization_id, now)
                for source_id in source_capsule_ids
                for target_id in target_ids
            ),
        )
        switched = connection.execute(
            """
            UPDATE capsule_structure_state
            SET structural_version = structural_version + 1
            WHERE singleton = 1 AND structural_version = ?
            """,
            (expected_version,),
        )
        if switched.rowcount != 1:
            raise ConstraintConflict(
                "capsule structure changed before atomic pointer switch"
            )
        after = evaluate_fixed_recall_regression(connection, cases)
        injected_regression = (
            os.environ.get("MYOUTBRAIN_CAPSULE_REGRESSION_FAIL") == "1"
        )
        if injected_regression:
            after = {**after, "injected_mismatch": True}
        before_categories = cast(dict[str, object], before["categories"])
        after_categories = cast(dict[str, object], after["categories"])
        comparable_before = _recall_equivalence_projection(before_categories)
        comparable_after = _recall_equivalence_projection(after_categories)
        if injected_regression or comparable_before != comparable_after:
            changed_categories = sorted(
                category
                for category in comparable_before
                if comparable_before[category] != comparable_after.get(category)
            )
            difference = _recall_difference_summary(
                before_categories,
                after_categories,
                changed_categories,
            )
            connection.rollback()
            raise RecallRegressionFailure(
                "recall regression changed in "
                + ", ".join(changed_categories or ("injected-gate",))
                + difference
                + "; capsule pointer switch was rolled back"
            )
        regression: dict[str, object] = {"equivalent": True, **after}
        result_hash = _stable_hash(
            {
                "reorganization_id": reorganization_id,
                "before_version": expected_version,
                "after_version": expected_version + 1,
                "targets": target_ids,
                "regression": regression,
            }
        )
        connection.execute(
            """
            INSERT INTO audit_events
                (event_id, event_type, occurred_at, subject_id, proposal_id,
                 before_version, after_version, entrance, result_hash)
            VALUES (?, 'capsule.reorganized', ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                "aud_" + hashlib.sha256(reorganization_id.encode("utf-8")).hexdigest(),
                now,
                reorganization_id,
                expected_version,
                expected_version + 1,
                entrance,
                result_hash,
            ),
        )
        reorganization_update = connection.execute(
            """
            UPDATE capsule_reorganizations
            SET status = 'switched', recall_regression_json = ?, updated_at = ?
            WHERE reorganization_id = ? AND status = 'validated'
            """,
            (
                json.dumps(regression, ensure_ascii=False, separators=(",", ":")),
                now,
                reorganization_id,
            ),
        )
        if reorganization_update.rowcount != 1:
            raise IntegrityError("capsule reorganization state changed during switch")
        connection.commit()
        _inject_capsule_fault("switched")

    @staticmethod
    def _staged_differences(
        connection: sqlite3.Connection,
        reorganization_id: str,
    ) -> dict[str, dict[str, str]]:
        return {
            cast(str, row[0]): _semantic_difference(
                {
                    "body": cast(str, row[1]),
                    "integrity_hash": cast(str, row[2]),
                },
                _semantic_snapshot(connection, cast(str, row[0])),
            )
            for row in connection.execute(
                """
                SELECT memory_id, body, integrity_hash
                FROM capsule_staged_records
                WHERE reorganization_id = ?
                ORDER BY memory_id
                """,
                (reorganization_id,),
            ).fetchall()
        }

    @staticmethod
    def _retire_sources(
        connection: sqlite3.Connection,
        *,
        reorganization_id: str,
        action: str,
        source_capsule_ids: tuple[str, ...],
        target_capsule_ids: tuple[str, ...],
        recall_regression_json: str,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ", ".join("?" for _ in source_capsule_ids)
        connection.execute(
            f"""
            UPDATE knowledge_capsules
            SET status = 'retired', body_bytes = 0, memory_record_count = 0,
                updated_at = ?
            WHERE capsule_id IN ({placeholders}) AND status = 'redirecting'
            """,
            (now, *source_capsule_ids),
        )
        redirects = [
            {
                "source_capsule_id": row[0],
                "target_capsule_id": row[1],
            }
            for row in connection.execute(
                """
                SELECT source_capsule_id, target_capsule_id
                FROM capsule_redirects
                WHERE reorganization_id = ?
                ORDER BY source_capsule_id, target_capsule_id
                """,
                (reorganization_id,),
            ).fetchall()
        ]
        version_row = connection.execute(
            "SELECT structural_version FROM capsule_structure_state WHERE singleton = 1"
        ).fetchone()
        if version_row is None or not isinstance(version_row[0], int):
            raise IntegrityError("capsule structure version is missing after switch")
        regression = json.loads(recall_regression_json)
        if not isinstance(regression, dict):
            raise IntegrityError("capsule recall regression is invalid")
        staged_rows = connection.execute(
            """
            SELECT memory_id, memory_version, target_capsule_id, integrity_hash
            FROM capsule_staged_records
            WHERE reorganization_id = ?
            ORDER BY memory_id
            """,
            (reorganization_id,),
        ).fetchall()
        for memory_id, memory_version, target_capsule_id, integrity_hash in staged_rows:
            pointer = connection.execute(
                """
                SELECT dictionary.primary_capsule_id, version.capsule_id
                FROM knowledge_dictionary AS dictionary
                JOIN canonical_memory_versions AS version
                  ON version.memory_id = dictionary.memory_id
                 AND version.version = dictionary.current_version
                WHERE dictionary.memory_id = ?
                  AND dictionary.current_version = ?
                """,
                (memory_id, memory_version),
            ).fetchone()
            if (
                pointer != (target_capsule_id, target_capsule_id)
                or not isinstance(memory_id, str)
                or not isinstance(memory_version, int)
                or integrity_hash
                != _memory_integrity_hash(connection, memory_id, memory_version)
            ):
                raise IntegrityError(
                    "retired capsule reorganization lost pointer or semantic integrity"
                )
        result: dict[str, object] = {
            "reorganization_id": reorganization_id,
            "action": action,
            "status": "retired",
            "structural_version": version_row[0],
            "source_capsule_ids": list(source_capsule_ids),
            "target_capsule_ids": list(target_capsule_ids),
            "completed_stages": [
                "planned",
                "staged",
                "validated",
                "switched",
                "retired",
            ],
            "redirects": redirects,
            "recall_regression": cast(dict[str, object], regression),
            "integrity": {
                "records_complete": True,
                "unique_primary_copy": True,
                "dictionary_pointer_closure": True,
                "semantic_hashes_unchanged": True,
                "memory_record_count": len(staged_rows),
            },
        }
        connection.execute(
            "DELETE FROM capsule_staged_records WHERE reorganization_id = ?",
            (reorganization_id,),
        )
        connection.execute(
            """
            UPDATE capsule_reorganizations
            SET status = 'retired', result_json = ?, updated_at = ?
            WHERE reorganization_id = ? AND status = 'switched'
            """,
            (
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                now,
                reorganization_id,
            ),
        )
        connection.commit()
        _inject_capsule_fault("retired")
        return result


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise UserInputError(f"{field} must contain 1 to 200 characters")
    return value.strip()


def _required_identifier(value: object, field: str, prefix: str) -> str:
    normalized = _required_text(value, field)
    if not normalized.startswith(prefix):
        raise UserInputError(f"{field} must start with {prefix}")
    return normalized


def _identifier_list(value: object, field: str, prefix: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise UserInputError(f"{field} must be a non-empty array")
    identifiers = tuple(_required_identifier(item, field, prefix) for item in value)
    if len(identifiers) != len(set(identifiers)):
        raise UserInputError(f"{field} must not contain duplicates")
    return identifiers


def _optional_boolean(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise UserInputError(f"{field} must be boolean")
    return value


def _topic_groups(value: object) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise UserInputError("topic_groups must be an array")
    groups: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or not all(
            isinstance(key, str) for key in item
        ):
            raise UserInputError("topic_groups entries must be objects")
        group = cast(dict[str, object], item)
        _reject_unknown_fields(group, {"topic", "memory_ids"})
        groups.append(
            {
                "topic": _required_text(group.get("topic"), "topic"),
                "memory_ids": _identifier_list(
                    group.get("memory_ids"), "memory_ids", "mem_"
                ),
            }
        )
    return tuple(groups)


def _capacity_split_groups(
    memory_ids: tuple[str, ...],
    memory_bytes: dict[str, int],
    *,
    topic: str,
    hard_limit_bytes: int,
) -> tuple[dict[str, object], ...]:
    if any(memory_bytes[memory_id] > hard_limit_bytes for memory_id in memory_ids):
        raise UserInputError(
            "a canonical memory exceeds capsule_hard_limit_bytes by itself"
        )
    bins: list[tuple[list[str], int]] = []
    for memory_id in sorted(memory_ids, key=lambda item: (-memory_bytes[item], item)):
        for index, (members, used_bytes) in enumerate(bins):
            if used_bytes + memory_bytes[memory_id] <= hard_limit_bytes:
                members.append(memory_id)
                bins[index] = (members, used_bytes + memory_bytes[memory_id])
                break
        else:
            bins.append(([memory_id], memory_bytes[memory_id]))
    if len(bins) == 1:
        members, used_bytes = bins[0]
        moved = members.pop()
        bins[0] = (members, used_bytes - memory_bytes[moved])
        bins.append(([moved], memory_bytes[moved]))
    return tuple(
        {"topic": topic, "memory_ids": tuple(sorted(members))}
        for members, _used_bytes in bins
    )


def _proposed_bodies(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise UserInputError("proposed_bodies must be an object keyed by memory_id")
    proposed: dict[str, str] = {}
    for memory_id, body in cast(dict[str, object], value).items():
        proposed[_required_identifier(memory_id, "memory_id", "mem_")] = (
            _required_memory_body(body)
        )
    return proposed


def _required_memory_body(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UserInputError("proposed body must not be blank")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > 8 * 1024:
        raise UserInputError("proposed body must not exceed 8192 bytes")
    return normalized


def _reject_unknown_fields(
    values: dict[str, object],
    allowed: set[str],
) -> None:
    unknown = sorted(set(values).difference(allowed))
    if unknown:
        raise UserInputError(
            "maintenance parameters contain unknown fields: " + ", ".join(unknown)
        )


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stored_identifiers(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise IntegrityError(f"stored {label} are invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise IntegrityError(f"stored {label} are invalid") from error
    if (
        not isinstance(decoded, list)
        or not decoded
        or not all(isinstance(item, str) for item in decoded)
    ):
        raise IntegrityError(f"stored {label} are invalid")
    return tuple(cast(list[str], decoded))


def _stored_topic_groups(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, str):
        raise IntegrityError("stored capsule topic plan is invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise IntegrityError("stored capsule topic plan is invalid") from error
    if not isinstance(decoded, list):
        raise IntegrityError("stored capsule topic plan is invalid")
    groups: list[dict[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise IntegrityError("stored capsule topic plan is invalid")
        group = cast(dict[object, object], item)
        topic = group.get("topic")
        memory_ids = group.get("memory_ids")
        semantic_snapshots = group.get("semantic_snapshots")
        if (
            not isinstance(topic, str)
            or not isinstance(memory_ids, list)
            or not memory_ids
            or not all(isinstance(memory_id, str) for memory_id in memory_ids)
            or not isinstance(semantic_snapshots, dict)
            or set(semantic_snapshots) != set(memory_ids)
            or not all(
                isinstance(memory_id, str)
                and isinstance(snapshot, dict)
                and isinstance(snapshot.get("body"), str)
                and isinstance(snapshot.get("integrity_hash"), str)
                for memory_id, snapshot in semantic_snapshots.items()
            )
        ):
            raise IntegrityError("stored capsule topic plan is invalid")
        groups.append(
            {
                "topic": topic,
                "memory_ids": tuple(cast(list[str], memory_ids)),
                "merge_forbidden": bool(group.get("merge_forbidden", False)),
                "semantic_snapshots": cast(
                    dict[str, dict[str, str]], semantic_snapshots
                ),
            }
        )
    return tuple(groups)


def _memory_integrity_hash(
    connection: sqlite3.Connection,
    memory_id: str,
    version: int,
) -> str:
    memory = connection.execute(
        """
        SELECT memory.state, dictionary.canonical_name,
               dictionary.current_version, version.content,
               version.applicability_scope
        FROM canonical_memories AS memory
        JOIN knowledge_dictionary AS dictionary
          ON dictionary.memory_id = memory.memory_id
        JOIN canonical_memory_versions AS version
          ON version.memory_id = dictionary.memory_id
         AND version.version = dictionary.current_version
        WHERE memory.memory_id = ? AND dictionary.current_version = ?
        """,
        (memory_id, version),
    ).fetchone()
    if memory is None:
        raise IntegrityError("canonical memory disappeared during capsule staging")
    names = connection.execute(
        """
        SELECT normalized_name, name_kind FROM memory_names
        WHERE memory_id = ? ORDER BY normalized_name, name_kind
        """,
        (memory_id,),
    ).fetchall()
    evidence = connection.execute(
        """
        SELECT source_id, source_version, relationship
        FROM canonical_memory_version_evidence
        WHERE memory_id = ? AND version = ?
        ORDER BY source_id, source_version, relationship
        """,
        (memory_id, version),
    ).fetchall()
    dependencies = connection.execute(
        """
        SELECT depends_on_memory_id, depends_on_version, relationship
        FROM canonical_memory_dependencies
        WHERE memory_id = ? AND version = ?
        ORDER BY depends_on_memory_id, depends_on_version, relationship
        """,
        (memory_id, version),
    ).fetchall()
    relations = connection.execute(
        """
        SELECT memory_id, related_memory_id, relationship
        FROM canonical_memory_relations
        WHERE memory_id = ? OR related_memory_id = ?
        ORDER BY memory_id, related_memory_id, relationship
        """,
        (memory_id, memory_id),
    ).fetchall()
    conflicts = connection.execute(
        """
        SELECT first_memory_id, second_memory_id, status
        FROM canonical_memory_conflicts
        WHERE first_memory_id = ? OR second_memory_id = ?
        ORDER BY first_memory_id, second_memory_id
        """,
        (memory_id, memory_id),
    ).fetchall()
    return _stable_hash(
        {
            "memory_id": memory_id,
            "version": version,
            "memory": list(memory),
            "names": [list(row) for row in names],
            "evidence": [list(row) for row in evidence],
            "dependencies": [list(row) for row in dependencies],
            "relations": [list(row) for row in relations],
            "conflicts": [list(row) for row in conflicts],
        }
    )


def _current_memory_version(
    connection: sqlite3.Connection,
    memory_id: str,
) -> int:
    row = connection.execute(
        "SELECT current_version FROM knowledge_dictionary WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], int):
        raise IntegrityError("canonical memory version disappeared during planning")
    return row[0]


def _current_memory_body(
    connection: sqlite3.Connection,
    memory_id: str,
) -> str:
    row = connection.execute(
        """
        SELECT version.content
        FROM knowledge_dictionary AS dictionary
        JOIN canonical_memory_versions AS version
          ON version.memory_id = dictionary.memory_id
         AND version.version = dictionary.current_version
        WHERE dictionary.memory_id = ?
        """,
        (memory_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise IntegrityError("canonical memory body disappeared during planning")
    return row[0]


def _semantic_snapshot(
    connection: sqlite3.Connection,
    memory_id: str,
) -> dict[str, str]:
    version = _current_memory_version(connection, memory_id)
    return {
        "body": _current_memory_body(connection, memory_id),
        "integrity_hash": _memory_integrity_hash(connection, memory_id, version),
    }


def _semantic_difference(
    before: dict[str, str],
    current: dict[str, str],
) -> dict[str, str]:
    return {
        "before_body": before["body"],
        "before_hash": before["integrity_hash"],
        "current_body": current["body"],
        "current_hash": current["integrity_hash"],
    }


def _recall_equivalence_projection(
    categories: dict[str, object],
) -> dict[str, object]:
    projected = json.loads(json.dumps(categories, ensure_ascii=False))
    if not isinstance(projected, dict):
        raise IntegrityError("recall regression categories are invalid")
    for cases in projected.values():
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict):
                continue
            signature = case.get("signature")
            if isinstance(signature, dict):
                signature.pop("cross_partition_hit", None)
                for memory in cast(list[object], signature.get("memories", [])):
                    if isinstance(memory, dict):
                        memory.pop("candidate_paths", None)
    return cast(dict[str, object], projected)


def _inject_capsule_fault(stage: str) -> None:
    if os.environ.get("MYOUTBRAIN_CAPSULE_FAULT_STAGE") == stage:
        raise IntegrityError(f"injected capsule maintenance fault after {stage}")


def _capsule_budgets(root: Path) -> tuple[int, int]:
    configuration_path = root / "myoutbrain.toml"
    try:
        with configuration_path.open("rb") as configuration_file:
            configuration = tomllib.load(configuration_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise IntegrityError("cannot read capsule budget configuration") from error
    storage = configuration.get("storage")
    if not isinstance(storage, dict):
        raise IntegrityError("capsule budget configuration is invalid")
    target = storage.get("capsule_target_bytes", CAPSULE_TARGET_BYTES)
    hard_limit = storage.get(
        "capsule_hard_limit_bytes",
        CAPSULE_HARD_LIMIT_BYTES,
    )
    if (
        not isinstance(target, int)
        or isinstance(target, bool)
        or target < 1
        or not isinstance(hard_limit, int)
        or isinstance(hard_limit, bool)
        or hard_limit < target
    ):
        raise IntegrityError("capsule budget configuration is invalid")
    return target, hard_limit


def _recall_difference_summary(
    before: dict[str, object],
    after: dict[str, object],
    changed_categories: list[str],
) -> str:
    for category in changed_categories:
        before_cases = before.get(category)
        after_cases = after.get(category)
        if not isinstance(before_cases, list) or not isinstance(after_cases, list):
            continue
        for before_case, after_case in zip(before_cases, after_cases, strict=False):
            if before_case == after_case:
                continue
            if not isinstance(before_case, dict) or not isinstance(after_case, dict):
                continue
            before_signature = before_case.get("signature")
            after_signature = after_case.get("signature")
            if not isinstance(before_signature, dict) or not isinstance(after_signature, dict):
                continue
            return (
                " (query "
                + str(before_case.get("query_hash", "unknown"))[:12]
                + ", memories "
                + repr(_signature_memory_ids(before_signature))
                + " -> "
                + repr(_signature_memory_ids(after_signature))
                + ")"
            )
    return ""


def _signature_memory_ids(signature: dict[object, object]) -> tuple[object, ...]:
    memories = signature.get("memories")
    if not isinstance(memories, list):
        return ()
    return tuple(
        memory.get("memory_id")
        for memory in memories
        if isinstance(memory, dict)
    )
