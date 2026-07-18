from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import cast

from myoutbrain.core_types import (
    ConfigurationConflict,
    IdempotencyConflict,
    IntegrityError,
    UserInputError,
    WriterLocked,
)
from myoutbrain.library import KnowledgeWorkflow
from myoutbrain.memory_gateway import MemoryGateway
from myoutbrain.protocol_contract import (
    load_domain_schema,
    SERVER_CAPABILITIES,
    SERVER_PROTOCOL_VERSION,
)
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
        protocol = request.get("protocol")
        if not isinstance(protocol, dict):
            raise UserInputError("gateway request protocol must be an object")
        protocol_data = cast(dict[object, object], protocol)
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
        name = client_data.get("name")
        raw_capabilities = client_data.get("capabilities")
        if not isinstance(name, str) or not name.strip():
            raise UserInputError("client.name must be non-blank text")
        if not isinstance(raw_capabilities, list) or not all(
            isinstance(capability, str) and capability
            for capability in raw_capabilities
        ):
            raise UserInputError("client.capabilities must be an array of non-blank text")
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
        server = ProtocolVersion(**SERVER_PROTOCOL_VERSION)
        if request.minimum_version.major != server.major:
            raise DomainProtocolError(
                "protocol_incompatible",
                "client protocol major version is incompatible",
                details=_version_details(request),
            )
        if request.maximum_version.major != server.major:
            raise DomainProtocolError(
                "protocol_incompatible",
                "client protocol major version is incompatible",
                details=_version_details(request),
            )
        if request.minimum_version > server:
            raise DomainProtocolError(
                "protocol_incompatible",
                "client requires a newer protocol minor version",
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
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": error.category,
                    "message": str(error),
                    "details": error.details,
                },
            },
            2,
        )
    except IdempotencyConflict as error:
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": "idempotency_conflict",
                    "message": str(error),
                    "details": {},
                },
            },
            2,
        )
    except ConfigurationConflict as error:
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": "configuration_conflict",
                    "message": str(error),
                    "details": {},
                },
            },
            3,
        )
    except WriterLocked as error:
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": "writer_locked",
                    "message": str(error) or "another MyOutBrain writer is active",
                    "details": {},
                },
            },
            4,
        )
    except IntegrityError as error:
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": "integrity_failure",
                    "message": str(error),
                    "details": {},
                },
            },
            7,
        )
    except OSError as error:
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": "io_failure",
                    "message": str(error),
                    "details": {},
                },
            },
            5,
        )
    except UserInputError as error:
        return (
            {
                "ok": False,
                "operation": operation,
                "server_protocol_version": dict(SERVER_PROTOCOL_VERSION),
                "error": {
                    "category": "invalid_request",
                    "message": str(error),
                    "details": {},
                },
            },
            2,
        )
    return response, 0


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

