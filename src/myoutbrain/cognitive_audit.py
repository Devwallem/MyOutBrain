from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from myoutbrain.core_types import MemoryState, UserInputError
from myoutbrain.local_core import CanonicalMemoryAudit, LocalMemoryCore
from myoutbrain.memory_gateway import (
    MemoryAccess,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)


@dataclass(frozen=True)
class CognitiveAuditResult:
    query: str
    audits: tuple[CanonicalMemoryAudit, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "query": self.query,
            "audits": [audit.to_data() for audit in self.audits],
        }


class CognitiveAuditService:
    """Resolve a natural question to canonical provenance and evolution audits."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def query(self, natural_query: str) -> CognitiveAuditResult:
        query = " ".join(natural_query.split())
        if not query:
            raise UserInputError("cognitive audit query must not be blank")
        package = MemoryGateway(self._root).recall(
            RecallRequest(
                query=query,
                task="cognitive-audit",
                access=MemoryAccess.LOCAL_TRUSTED,
                purpose=QueryPurpose.SUBSTANTIVE,
                limit=10,
            )
        )
        canonical_ids = tuple(
            dict.fromkeys(
                item.memory_id
                for item in package.items
                if item.memory_state == MemoryState.CANONICAL
            )
        )
        core = LocalMemoryCore(self._root)
        return CognitiveAuditResult(
            query=query,
            audits=tuple(
                core.explain_canonical_memory(memory_id)
                for memory_id in canonical_ids
            ),
        )
