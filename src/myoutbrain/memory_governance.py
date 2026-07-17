from __future__ import annotations

from pathlib import Path

from myoutbrain.core_types import UserInputError
from myoutbrain.local_core import (
    CanonicalMemoryStateChange,
    LocalMemoryCore,
    MemoryDeletionImpact,
    MemoryDeletionResult,
)
from myoutbrain.knowledge_views import KnowledgeViewService, VIEW_MANIFEST


class MemoryGovernanceService:
    """Apply natural, reversible memory lifecycle instructions."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._core = LocalMemoryCore(root)

    def forget(self, memory_id: str, instruction: str) -> CanonicalMemoryStateChange:
        normalized = " ".join(instruction.split())
        folded = normalized.casefold()
        if not normalized:
            raise UserInputError("memory lifecycle instruction must not be blank")
        if any(
            marker in folded
            for marker in ("permanent", "permanently", "永久", "彻底删除")
        ):
            raise UserInputError(
                "permanent deletion requires a separate impact preview and confirmation"
            )
        if any(
            marker in folded
            for marker in ("restore", "reactivate", "remember again", "恢复", "重新启用")
        ):
            return self._core.set_canonical_memory_active(
                memory_id,
                active=True,
                reason=normalized,
            )
        if any(
            marker in folded
            for marker in ("forget", "deactivate", "忘掉", "停用")
        ):
            return self._core.set_canonical_memory_active(
                memory_id,
                active=False,
                reason=normalized,
            )
        raise UserInputError(
            "memory lifecycle instruction must clearly say forget/deactivate or restore"
        )

    def delete(
        self,
        memory_id: str,
        *,
        confirmation: str | None,
    ) -> MemoryDeletionImpact | MemoryDeletionResult:
        impact = self._core.preview_permanent_deletion(memory_id)
        if confirmation is None:
            return impact
        if confirmation != impact.confirmation_token:
            raise UserInputError(
                "permanent deletion confirmation does not match the current impact"
            )
        views = KnowledgeViewService(self._root)
        views_were_built = (self._root / VIEW_MANIFEST).is_file()
        if views_were_built:
            views.rebuild()
        result = self._core.permanently_delete(
            memory_id,
            confirmation_token=confirmation,
        )
        if views_were_built:
            views.rebuild()
        return result
