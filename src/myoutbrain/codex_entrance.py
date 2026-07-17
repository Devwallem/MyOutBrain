from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile

from myoutbrain.core_types import Sensitivity
from myoutbrain.local_core import BufferedMemoryReceipt
from myoutbrain.memory_gateway import (
    ExperienceSubmission,
    MemoryAccess,
    MemoryEvidencePackage,
    MemoryGateway,
    QueryPurpose,
    RecallRequest,
)


@dataclass(frozen=True)
class CodexTaskRequest:
    question: str
    task_pointer: str
    purpose: QueryPurpose
    access: MemoryAccess = MemoryAccess.TASK_SCOPED
    memory_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    limit: int = 5
    query_sensitivity: Sensitivity = "local-only"


@dataclass(frozen=True)
class CodexTaskContext:
    task_pointer: str
    evidence_package: MemoryEvidencePackage

    def to_data(self) -> dict[str, object]:
        return {
            "entrance": "codex",
            "task_pointer": self.task_pointer,
            "evidence_package": self.evidence_package.to_data(),
        }


@dataclass(frozen=True)
class CodexVisibleExperience:
    visible_text: str
    occurred_at: str
    task_pointer: str
    digest: str
    sensitivity: Sensitivity
    visible_context: str
    context_gaps: tuple[str, ...]


class CodexEntrance:
    """Translate Codex task context into the shared memory-gateway contract."""

    def __init__(self, root: Path) -> None:
        self._gateway = MemoryGateway(root)

    def before_task(self, request: CodexTaskRequest) -> CodexTaskContext:
        package = self._gateway.recall(
            RecallRequest(
                query=request.question,
                task=request.task_pointer,
                access=request.access,
                purpose=request.purpose,
                memory_ids=request.memory_ids,
                source_ids=request.source_ids,
                limit=request.limit,
                query_sensitivity=request.query_sensitivity,
            )
        )
        return CodexTaskContext(
            task_pointer=package.task,
            evidence_package=package,
        )

    def after_task(
        self, experience: CodexVisibleExperience
    ) -> BufferedMemoryReceipt:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="myoutbrain-codex-",
                suffix=".txt",
                delete=False,
            ) as visible_file:
                temporary_path = Path(visible_file.name)
                visible_file.write(experience.visible_text)
            return self._gateway.submit(
                ExperienceSubmission(
                    experience_path=temporary_path,
                    occurred_at=experience.occurred_at,
                    entrance="codex",
                    task_pointer=experience.task_pointer,
                    digest=experience.digest,
                    sensitivity=experience.sensitivity,
                    visible_context=experience.visible_context,
                    context_gaps=experience.context_gaps,
                )
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
