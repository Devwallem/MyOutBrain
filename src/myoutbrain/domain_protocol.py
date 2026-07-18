from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import cast

from myoutbrain.core_types import (
    ConfigurationConflict,
    ConstraintConflict,
    IdempotencyConflict,
    IntegrityError,
    RecallRegressionFailure,
    LeaseConflict,
    UserInputError,
    VersionConflict,
    WriterLocked,
)
from myoutbrain.library import KnowledgeWorkflow
from myoutbrain.memory_gateway import MemoryGateway
from myoutbrain.protocol_contract import (
    load_domain_schema,
    SERVER_CAPABILITIES,
    SERVER_MINIMUM_PROTOCOL_VERSION,
    SERVER_PROTOCOL_VERSION,
)
from myoutbrain.reflection import load_immediate_reflection
from myoutbrain.unified_review import ReviewBatchRequest, ReviewDecision


class DomainProtocolError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.details = details or {}


@dataclass(frozen=True, order=True)
class ProtocolVersion:
    major: int
    minor: int

    @classmethod
    def from_data(cls, data: object, *, field: str) -> ProtocolVersion:
        if not isinstance(data, dict):
            raise UserInputError(f"{field} must be an object")
        values = cast(dict[object, object], data)
        _reject_unknown_fields(values, {"major", "minor"}, field)
        major = values.get("major")
        minor = values.get("minor")
        if (
            not isinstance(major, int)
            or isinstance(major, bool)
            or major < 0
            or not isinstance(minor, int)
            or isinstance(minor, bool)
            or minor < 0
        ):
            raise UserInputError(f"{field} must contain non-negative integer major and minor")
        return cls(major=major, minor=minor)

    def to_data(self) -> dict[str, int]:
        return {"major": self.major, "minor": self.minor}


@dataclass(frozen=True)
class DomainRequest:
    minimum_version: ProtocolVersion
    maximum_version: ProtocolVersion
    client_name: str
    capabilities: frozenset[str]
    operation: str
    parameters: dict[str, object]
    write: WriteCondition | None

    @classmethod
    def from_data(cls, data: object) -> DomainRequest:
        if not isinstance(data, dict):
            raise UserInputError("gateway request must be a JSON object")
        request = cast(dict[object, object], data)
        _reject_unknown_fields(
            request,
            {"protocol", "client", "operation", "parameters", "write"},
            "gateway request",
        )
        protocol = request.get("protocol")
        if not isinstance(protocol, dict):
            raise UserInputError("gateway request protocol must be an object")
        protocol_data = cast(dict[object, object], protocol)
        _reject_unknown_fields(
            protocol_data, {"minimum", "maximum"}, "gateway request protocol"
        )
        minimum = ProtocolVersion.from_data(
            protocol_data.get("minimum"), field="protocol.minimum"
        )
        maximum = ProtocolVersion.from_data(
            protocol_data.get("maximum"), field="protocol.maximum"
        )
        if minimum > maximum:
            raise UserInputError("protocol.minimum must not exceed protocol.maximum")
        client = request.get("client")
        if not isinstance(client, dict):
            raise UserInputError("gateway request client must be an object")
        client_data = cast(dict[object, object], client)
        _reject_unknown_fields(
            client_data, {"name", "capabilities"}, "gateway request client"
        )
        name = client_data.get("name")
        raw_capabilities = client_data.get("capabilities")
        if not isinstance(name, str) or not name.strip():
            raise UserInputError("client.name must be non-blank text")
        if not isinstance(raw_capabilities, list) or not all(
            isinstance(capability, str) and capability.strip()
            for capability in raw_capabilities
        ):
            raise UserInputError("client.capabilities must be an array of non-blank text")
        if len(raw_capabilities) != len(set(raw_capabilities)):
            raise UserInputError("client.capabilities must not contain duplicates")
        operation = request.get("operation")
        parameters = request.get("parameters")
        if not isinstance(operation, str) or not operation.strip():
            raise UserInputError("operation must be non-blank text")
        if not isinstance(parameters, dict) or not all(
            isinstance(key, str) for key in parameters
        ):
            raise UserInputError("parameters must be a JSON object")
        raw_write = request.get("write")
        return cls(
            minimum_version=minimum,
            maximum_version=maximum,
            client_name=name.strip(),
            capabilities=frozenset(cast(list[str], raw_capabilities)),
            operation=operation.strip(),
            parameters=cast(dict[str, object], parameters),
            write=(
                WriteCondition.from_data(raw_write)
                if raw_write is not None
                else None
            ),
        )


@dataclass(frozen=True)
class WriteCondition:
    idempotency_key: str
    expected_version: int

    @classmethod
    def from_data(cls, data: object) -> WriteCondition:
        if not isinstance(data, dict):
            raise UserInputError("write must be a JSON object")
        values = cast(dict[object, object], data)
        _reject_unknown_fields(
            values, {"idempotency_key", "expected_version"}, "write"
        )
        idempotency_key = values.get("idempotency_key")
        expected_version = values.get("expected_version")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
        ):
            raise UserInputError("write.idempotency_key must contain 1 to 200 characters")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise UserInputError("write.expected_version must be a non-negative integer")
        return cls(idempotency_key.strip(), expected_version)


class DomainProtocol:
    def __init__(self, root: Path) -> None:
        self._root = root

    def dispatch(self, request: DomainRequest) -> dict[str, object]:
        negotiated = self._negotiate(request)
        if request.operation == "instance.status":
            result = KnowledgeWorkflow(self._root).instance_status().to_data()
        elif request.operation == "protocol.describe":
            result = load_domain_schema("compatibility-v2.json")
        elif request.operation == "review.list":
            result = MemoryGateway(self._root).review_queue()
        elif request.operation == "review.decide":
            proposal = self._require_understood_review_effect(request)
            result = self._decide_review(request, proposal)
        elif request.operation == "maintenance.inspect":
            result = MemoryGateway(self._root).inspect_capsule_structure()
        elif request.operation == "maintenance.plan":
            result = MemoryGateway(self._root).plan_capsule_maintenance(
                request.parameters
            )
        elif request.operation == "maintenance.configure_partition":
            write = self._require_capsule_maintenance_write(request, negotiated)
            result = MemoryGateway(self._root).configure_partition(
                request.parameters,
                expected_version=write.expected_version,
                idempotency_key=write.idempotency_key,
            )
        elif request.operation == "maintenance.reorganize":
            write = self._require_capsule_maintenance_write(request, negotiated)
            result = MemoryGateway(self._root).reorganize_capsules(
                request.parameters,
                expected_version=write.expected_version,
                idempotency_key=write.idempotency_key,
                entrance=request.client_name,
            )
        elif request.operation == "reflection.schedule":
            result = self._configure_reflection_schedule(request)
        elif request.operation == "reflection.enqueue":
            result = self._enqueue_scheduled_reflection(request)
        elif request.operation == "reflection.claim":
            result = self._claim_scheduled_reflection(request)
        elif request.operation == "reflection.return":
            result = self._return_scheduled_reflection(request)
        elif request.operation == "reflection.complete":
            result = self._complete_scheduled_reflection(request)
        elif request.operation == "reflection.abandon":
            result = self._abandon_scheduled_reflection(request)
        else:
            raise UserInputError(f"unknown gateway operation: {request.operation}")
        return {
            "ok": True,
            "operation": request.operation,
            "protocol_version": negotiated.to_data(),
            "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
            "server_capabilities": list(SERVER_CAPABILITIES),
            "result": result,
        }

    @staticmethod
    def _require_capsule_maintenance_write(
        request: DomainRequest,
        negotiated: ProtocolVersion,
    ) -> WriteCondition:
        if negotiated < ProtocolVersion(major=2, minor=2):
            raise DomainProtocolError(
                "protocol_incompatible",
                "capsule maintenance writes require protocol 2.2",
                details=_version_details(request),
            )
        if "capsule_maintenance.v1" not in request.capabilities:
            raise DomainProtocolError(
                "capability_required",
                "client cannot understand capsule maintenance effects",
                details={"missing": ["capsule_maintenance.v1"]},
            )
        if request.write is None:
            raise DomainProtocolError(
                "write_contract_required",
                "capsule maintenance writes require idempotency_key and expected_version",
            )
        return request.write

    def _configure_reflection_schedule(
        self,
        request: DomainRequest,
    ) -> dict[str, object]:
        write = self._require_runtime_write(request, "reflection_schedule.v1")
        _reject_unknown_fields(
            cast(dict[object, object], request.parameters),
            {"enabled", "first_due_at", "every_hours"},
            "reflection.schedule parameters",
        )
        enabled = request.parameters.get("enabled")
        first_due_at = request.parameters.get("first_due_at")
        every_hours = request.parameters.get("every_hours")
        if not isinstance(enabled, bool):
            raise UserInputError("reflection.schedule enabled must be boolean")
        if not isinstance(first_due_at, str) or not first_due_at.strip():
            raise UserInputError("reflection.schedule first_due_at must be non-blank text")
        if not isinstance(every_hours, int) or isinstance(every_hours, bool):
            raise UserInputError("reflection.schedule every_hours must be an integer")
        return MemoryGateway(self._root).configure_reflection_schedule(
            enabled=enabled,
            first_due_at=first_due_at,
            every_hours=every_hours,
            expected_version=write.expected_version,
            idempotency_key=write.idempotency_key,
        )

    def _enqueue_scheduled_reflection(
        self,
        request: DomainRequest,
    ) -> dict[str, object]:
        write = self._require_runtime_write(request, "reflection_schedule.v1")
        _reject_unknown_fields(
            cast(dict[object, object], request.parameters),
            {"now"},
            "reflection.enqueue parameters",
        )
        now = request.parameters.get("now")
        if not isinstance(now, str) or not now.strip():
            raise UserInputError("reflection.enqueue now must be non-blank text")
        return MemoryGateway(self._root).enqueue_scheduled_reflection(
            now=now,
            expected_version=write.expected_version,
            idempotency_key=write.idempotency_key,
        )

    def _claim_scheduled_reflection(
        self,
        request: DomainRequest,
    ) -> dict[str, object]:
        write = self._require_runtime_write(request, "reflection_claim.v1")
        _reject_unknown_fields(
            cast(dict[object, object], request.parameters),
            {"now", "lease_seconds"},
            "reflection.claim parameters",
        )
        now = request.parameters.get("now")
        lease_seconds = request.parameters.get("lease_seconds")
        if not isinstance(now, str) or not now.strip():
            raise UserInputError("reflection.claim now must be non-blank text")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
            raise UserInputError("reflection.claim lease_seconds must be an integer")
        return MemoryGateway(self._root).claim_scheduled_reflection(
            now=now,
            lease_seconds=lease_seconds,
            claimed_by=request.client_name,
            expected_version=write.expected_version,
            idempotency_key=write.idempotency_key,
        )

    def _return_scheduled_reflection(
        self,
        request: DomainRequest,
    ) -> dict[str, object]:
        write = self._require_runtime_write(request, "reflection_claim.v1")
        _reject_unknown_fields(
            cast(dict[object, object], request.parameters),
            {"run_id", "lease_token", "now", "reason"},
            "reflection.return parameters",
        )
        run_id = request.parameters.get("run_id")
        lease_token = request.parameters.get("lease_token")
        now = request.parameters.get("now")
        reason = request.parameters.get("reason")
        if not all(isinstance(value, str) and value.strip() for value in (
            run_id,
            lease_token,
            now,
            reason,
        )):
            raise UserInputError(
                "reflection.return run_id, lease_token, now and reason must be non-blank text"
            )
        return MemoryGateway(self._root).return_scheduled_reflection(
            run_id=cast(str, run_id),
            lease_token=cast(str, lease_token),
            now=cast(str, now),
            reason=cast(str, reason),
            returned_by=request.client_name,
            expected_version=write.expected_version,
            idempotency_key=write.idempotency_key,
        )

    def _complete_scheduled_reflection(
        self,
        request: DomainRequest,
    ) -> dict[str, object]:
        write = self._require_runtime_write(request, "reflection_complete.v1")
        if "review_payload.v1" not in request.capabilities:
            raise DomainProtocolError(
                "capability_required",
                "reflection.complete requires review_payload.v1",
                details={"missing": ["review_payload.v1"]},
            )
        _reject_unknown_fields(
            cast(dict[object, object], request.parameters),
            {"run_id", "lease_token", "completed_at", "reflection"},
            "reflection.complete parameters",
        )
        run_id = request.parameters.get("run_id")
        lease_token = request.parameters.get("lease_token")
        completed_at = request.parameters.get("completed_at")
        if not all(isinstance(value, str) and value.strip() for value in (
            run_id,
            lease_token,
            completed_at,
        )):
            raise UserInputError(
                "reflection.complete run_id, lease_token and completed_at "
                "must be non-blank text"
            )
        reflection = load_immediate_reflection(
            request.parameters.get("reflection")
        )
        return MemoryGateway(self._root).complete_scheduled_reflection(
            reflection,
            run_id=cast(str, run_id),
            lease_token=cast(str, lease_token),
            completed_at=cast(str, completed_at),
            completed_by=request.client_name,
            expected_version=write.expected_version,
            idempotency_key=write.idempotency_key,
        )

    def _abandon_scheduled_reflection(
        self,
        request: DomainRequest,
    ) -> dict[str, object]:
        write = self._require_runtime_write(request, "reflection_abandon.v1")
        _reject_unknown_fields(
            cast(dict[object, object], request.parameters),
            {
                "run_id",
                "abandoned_at",
                "reason",
                "permanently_missing_input_ids",
                "confirm_permanent_missing",
            },
            "reflection.abandon parameters",
        )
        run_id = request.parameters.get("run_id")
        abandoned_at = request.parameters.get("abandoned_at")
        reason = request.parameters.get("reason")
        raw_missing = request.parameters.get("permanently_missing_input_ids")
        confirmed = request.parameters.get("confirm_permanent_missing")
        if not all(isinstance(value, str) and value.strip() for value in (
            run_id,
            abandoned_at,
            reason,
        )):
            raise UserInputError(
                "reflection.abandon run_id, abandoned_at and reason "
                "must be non-blank text"
            )
        if not isinstance(raw_missing, list) or not all(
            isinstance(value, str) and value.strip() for value in raw_missing
        ):
            raise UserInputError(
                "reflection.abandon permanently_missing_input_ids must be text"
            )
        if not isinstance(confirmed, bool):
            raise UserInputError(
                "reflection.abandon confirm_permanent_missing must be boolean"
            )
        return MemoryGateway(self._root).abandon_scheduled_reflection(
            run_id=cast(str, run_id),
            abandoned_at=cast(str, abandoned_at),
            reason=cast(str, reason),
            permanently_missing_input_ids=tuple(cast(list[str], raw_missing)),
            confirm_permanent_missing=confirmed,
            abandoned_by=request.client_name,
            expected_version=write.expected_version,
            idempotency_key=write.idempotency_key,
        )

    @staticmethod
    def _require_runtime_write(
        request: DomainRequest,
        capability: str,
    ) -> WriteCondition:
        required_version = ProtocolVersion(major=2, minor=2)
        if request.maximum_version < required_version:
            raise DomainProtocolError(
                "protocol_incompatible",
                f"{request.operation} requires protocol 2.2",
                details={"required": required_version.to_data()},
            )
        if capability not in request.capabilities:
            raise DomainProtocolError(
                "capability_required",
                f"{request.operation} requires {capability}",
                details={"missing": [capability]},
            )
        if request.write is None:
            raise DomainProtocolError(
                "write_contract_required",
                "runtime writes require idempotency_key and expected_version",
            )
        return request.write

    def _require_understood_review_effect(
        self,
        request: DomainRequest,
    ) -> dict[object, object]:
        write = request.write
        if write is None:
            raise DomainProtocolError(
                "write_contract_required",
                "semantic writes require idempotency_key and expected_version",
            )
        proposal_id = request.parameters.get("proposal_id")
        decision = request.parameters.get("decision")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise UserInputError("review.decide proposal_id must be non-blank text")
        if decision not in ("approve", "approve-edited", "reject", "defer"):
            raise UserInputError("review.decide decision is invalid")
        proposal = MemoryGateway(self._root).unified_review_proposal(proposal_id)
        if proposal is None:
            raise UserInputError(f"unknown review proposal: {proposal_id}")
        required = {"review_payload.v1", "review_decision.v1"}
        if decision in ("approve", "approve-edited"):
            approval_effect = proposal.get("approval_effect")
            effect_type = (
                approval_effect.get("type")
                if isinstance(approval_effect, dict)
                else None
            )
            if not isinstance(effect_type, str) or not effect_type:
                raise UserInputError("review proposal approval effect is invalid")
            required.add(f"review_effect.{effect_type}.v1")
        missing = sorted(required.difference(request.capabilities))
        if missing:
            raise DomainProtocolError(
                "capability_required",
                "client cannot understand this review decision",
                details={"missing": missing},
            )
        return cast(dict[object, object], proposal)

    def _decide_review(
        self,
        request: DomainRequest,
        proposal: dict[object, object],
    ) -> dict[str, object]:
        write = request.write
        if write is None:
            raise DomainProtocolError(
                "write_contract_required",
                "semantic writes require idempotency_key and expected_version",
            )
        proposal_version = proposal.get("proposal_version")
        if not isinstance(proposal_version, int):
            raise UserInputError("review proposal version is invalid")
        if write.expected_version != proposal_version:
            raise DomainProtocolError(
                "version_conflict",
                "review proposal version does not match expected_version",
                details={
                    "actual_version": proposal_version,
                    "expected_version": write.expected_version,
                },
            )
        decision_data: dict[str, object] = {
            "proposal_id": request.parameters.get("proposal_id"),
            "proposal_version": proposal_version,
            "decision": request.parameters.get("decision"),
            "edited_content": request.parameters.get("edited_content"),
            "reason": request.parameters.get("reason"),
            "defer_until": request.parameters.get("defer_until"),
            "confirm_personal_cognition": request.parameters.get(
                "confirm_personal_cognition", False
            ),
        }
        decision = ReviewDecision.from_data(decision_data)
        batch_id = "bat_protocol_" + hashlib.sha256(
            write.idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        return MemoryGateway(self._root).decide_review_batch(
            ReviewBatchRequest(batch_id=batch_id, decisions=(decision,)),
            idempotency_key=write.idempotency_key,
            entrance=request.client_name,
        )

    @staticmethod
    def _negotiate(request: DomainRequest) -> ProtocolVersion:
        server_minimum = ProtocolVersion(**SERVER_MINIMUM_PROTOCOL_VERSION)
        server = ProtocolVersion(**SERVER_PROTOCOL_VERSION)
        if (
            request.maximum_version < server_minimum
            or request.minimum_version > server
        ):
            raise DomainProtocolError(
                "protocol_incompatible",
                "client protocol range is incompatible",
                details=_version_details(request),
            )
        return min(server, request.maximum_version)


def execute_domain_request(
    root: Path,
    data: object,
) -> tuple[dict[str, object], int]:
    operation = _operation_from_data(data)
    try:
        response = DomainProtocol(root).dispatch(DomainRequest.from_data(data))
    except DomainProtocolError as error:
        return _error_response(
            operation, error.category, str(error), 2, details=error.details
        )
    except IdempotencyConflict as error:
        return _error_response(operation, "idempotency_conflict", str(error), 2)
    except ConstraintConflict as error:
        return _error_response(operation, "constraint_conflict", str(error), 2)
    except RecallRegressionFailure as error:
        return _error_response(operation, "recall_regression_failed", str(error), 2)
    except VersionConflict as error:
        return _error_response(
            operation,
            "version_conflict",
            str(error),
            2,
            details={"expected_version": error.expected, "actual_version": error.actual},
        )
    except LeaseConflict as error:
        return _error_response(operation, "lease_conflict", str(error), 2)
    except ConfigurationConflict as error:
        return _error_response(operation, "configuration_conflict", str(error), 3)
    except WriterLocked as error:
        return _error_response(
            operation,
            "writer_locked",
            str(error) or "another MyOutBrain writer is active",
            4,
        )
    except IntegrityError as error:
        return _error_response(operation, "integrity_failure", str(error), 7)
    except OSError as error:
        return _error_response(operation, "io_failure", str(error), 5)
    except UserInputError as error:
        return _error_response(operation, "invalid_request", str(error), 2)
    return response, 0


def _error_response(
    operation: str | None,
    category: str,
    message: str,
    exit_code: int,
    *,
    details: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    return (
        {
            "ok": False,
            "operation": operation,
            "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
            "error": {
                "category": category,
                "message": message,
                "details": details or {},
            },
        },
        exit_code,
    )


def _operation_from_data(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    operation = cast(dict[object, object], data).get("operation")
    return operation if isinstance(operation, str) else None


def _version_details(request: DomainRequest) -> dict[str, object]:
    return {
        "client_minimum": request.minimum_version.to_data(),
        "client_maximum": request.maximum_version.to_data(),
        "server": dict(SERVER_PROTOCOL_VERSION),
    }


def _reject_unknown_fields(
    values: dict[object, object],
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(
        key if isinstance(key, str) else repr(key)
        for key in values
        if key not in allowed
    )
    if unknown:
        raise UserInputError(f"{field} contains unknown fields: {', '.join(unknown)}")
