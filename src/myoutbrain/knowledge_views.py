from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re

from myoutbrain.local_core import CanonicalMemoryAudit, LocalMemoryCore
from myoutbrain.core_types import IntegrityError, UserInputError
from myoutbrain.obsidian import create_obsidian_adapter
from myoutbrain.persistence import atomic_commit, recover_transactions, writer_lock


VIEW_ROOT = Path("vault") / "Knowledge Views"
VIEW_INDEX = VIEW_ROOT / "Index.md"
VIEW_MANIFEST = Path("runtime") / "knowledge-views" / "manifest.json"


@dataclass(frozen=True)
class KnowledgeViewBuild:
    view_paths: tuple[str, ...]
    index_path: str
    obsidian_warning: str | None

    def to_data(self) -> dict[str, object]:
        return {
            "view_count": len(self.view_paths),
            "view_paths": list(self.view_paths),
            "index_path": self.index_path,
            "obsidian_warning": self.obsidian_warning,
        }


@dataclass(frozen=True)
class KnowledgeViewEdit:
    memory_id: str
    digest_id: str
    proposal_ids: tuple[str, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "digest_id": self.digest_id,
            "proposal_ids": list(self.proposal_ids),
        }


@dataclass(frozen=True)
class KnowledgeViewSync:
    edits: tuple[KnowledgeViewEdit, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "edit_count": len(self.edits),
            "edits": [edit.to_data() for edit in self.edits],
        }


class KnowledgeViewService:
    """Project canonical audit snapshots into disposable human-readable notes."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def rebuild(self, *, open_index: bool = False) -> KnowledgeViewBuild:
        audits = LocalMemoryCore(self._root).canonical_memory_audits()
        relative_paths = {
            audit.memory_id: VIEW_ROOT / _view_filename(audit)
            for audit in audits
        }
        changes: list[tuple[Path, bytes]] = []
        manifest_views: list[dict[str, str]] = []
        for audit in audits:
            relative_path = relative_paths[audit.memory_id]
            content = _render_view(audit, relative_paths)
            encoded = content.encode("utf-8")
            changes.append((self._root / relative_path, encoded))
            manifest_views.append(
                {
                    "memory_id": audit.memory_id,
                    "path": relative_path.as_posix(),
                    "content_hash": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                }
            )
        index_content = _render_index(audits, relative_paths).encode("utf-8")
        changes.append((self._root / VIEW_INDEX, index_content))
        manifest = json.dumps(
            {"schema_version": 1, "views": manifest_views},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        changes.append((self._root / VIEW_MANIFEST, manifest))
        previous_paths = _previous_view_paths(self._root)
        with writer_lock(self._root):
            recover_transactions(self._root)
            atomic_commit(self._root, changes)
            current_paths = {path for path in relative_paths.values()}
            for relative_path in previous_paths - current_paths:
                (self._root / relative_path).unlink(missing_ok=True)
        warning = None
        if open_index:
            warning = create_obsidian_adapter().open_note(
                self._root / "vault",
                self._root / VIEW_INDEX,
            )
        return KnowledgeViewBuild(
            view_paths=tuple(
                relative_paths[audit.memory_id].as_posix() for audit in audits
            ),
            index_path=VIEW_INDEX.as_posix(),
            obsidian_warning=warning,
        )

    def sync_edits(self) -> KnowledgeViewSync:
        manifest = _load_manifest(self._root)
        views = manifest["views"]
        if not isinstance(views, list):
            raise IntegrityError("knowledge view manifest has invalid views")
        core = LocalMemoryCore(self._root)
        edits: list[KnowledgeViewEdit] = []
        for item in views:
            memory_id, relative_path, expected_hash = _manifest_view(item)
            view_path = self._root / relative_path
            if not view_path.is_file():
                continue
            try:
                body = view_path.read_bytes()
                actual_hash = f"sha256:{hashlib.sha256(body).hexdigest()}"
                text = body.decode("utf-8")
            except (OSError, UnicodeError) as error:
                raise IntegrityError(f"cannot read knowledge view: {view_path}") from error
            if actual_hash == expected_hash:
                continue
            edited_understanding = _current_understanding(text, view_path)
            core.explain_canonical_memory(memory_id)
            task = f"knowledge-view-edit:{memory_id}"
            occurred_at = datetime.fromtimestamp(
                view_path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat()
            receipt = core.capture_experience(
                view_path,
                occurred_at=occurred_at,
                entrance="obsidian-view",
                task=task,
                memory_digest=edited_understanding,
                sensitivity="local-only",
                visible_context=f"edited generated knowledge view for {memory_id}",
                context_gaps=(
                    "Only the edited generated view and its canonical identity are visible.",
                ),
            )
            proposals = core.propose_manual_consolidation(task)
            edits.append(
                KnowledgeViewEdit(
                    memory_id=memory_id,
                    digest_id=receipt.digest_id,
                    proposal_ids=tuple(proposal.proposal_id for proposal in proposals),
                )
            )
            if isinstance(item, dict):
                item["content_hash"] = actual_hash
        if edits:
            encoded_manifest = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            with writer_lock(self._root):
                recover_transactions(self._root)
                atomic_commit(
                    self._root,
                    [(self._root / VIEW_MANIFEST, encoded_manifest)],
                )
        return KnowledgeViewSync(edits=tuple(edits))


def _view_filename(audit: CanonicalMemoryAudit) -> str:
    topic = " ".join(audit.current_content.split()[:8])
    safe_topic = re.sub(r'[<>:"/\\|?*]+', "-", topic).strip(" .-")
    if not safe_topic:
        safe_topic = "Knowledge"
    return f"{safe_topic[:72]} — {audit.memory_id[4:12]}.md"


def _render_view(
    audit: CanonicalMemoryAudit,
    relative_paths: dict[str, Path],
) -> str:
    lines = [
        "---",
        "myoutbrain_view: true",
        f"memory_id: {audit.memory_id}",
        f"state: {audit.state}",
        f"confirmation: {audit.confirmation_status}",
        f"current_version: {audit.current_version}",
        "---",
        "",
        f"# {_view_title(audit.current_content)}",
        "",
        "## Current understanding",
        "",
        audit.current_content,
        "",
        "## Key sources",
        "",
    ]
    lines.extend(
        f"- `{source_id}`" for source_id in audit.current_source_ids
    )
    if not audit.current_source_ids:
        lines.append("- None recorded")
    lines.extend(["", "## Evolution", ""])
    for version in audit.versions:
        status = "current" if version.status == "current" else "superseded"
        lines.append(
            f"- Version {version.version} ({version.action}, {status}): "
            f"{version.content}"
        )
        if version.source_ids:
            lines.append("  - Sources: " + ", ".join(version.source_ids))
        if version.change_reason is not None:
            lines.append(f"  - Change reason: {version.change_reason}")
        if version.supersession_reason is not None:
            lines.append(
                f"  - Supersession reason: {version.supersession_reason}"
            )
    lines.extend(["", "## Unresolved conflicts", ""])
    if not audit.unresolved_conflicts:
        lines.append("- None")
    for conflict in audit.unresolved_conflicts:
        conflict_path = relative_paths.get(conflict.memory_id)
        target = (
            f"[[{conflict_path.stem}]]"
            if conflict_path is not None
            else f"`{conflict.memory_id}`"
        )
        lines.append(f"- {target}: {conflict.content} — {conflict.reason}")
    lines.extend(
        [
            "",
            "---",
            "",
            "This is a rebuildable knowledge view. Editing it creates new evidence; "
            "it does not directly change canonical memory.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_index(
    audits: tuple[CanonicalMemoryAudit, ...],
    relative_paths: dict[str, Path],
) -> str:
    lines = [
        "# MyOutBrain Knowledge Views",
        "",
        "Generated from canonical memory. Delete and rebuild these notes at any time.",
        "",
        "## Topics",
        "",
    ]
    if not audits:
        lines.append("- No canonical memory yet")
    for audit in audits:
        status = "conflicted" if audit.unresolved_conflicts else audit.state
        lines.append(
            f"- [[{relative_paths[audit.memory_id].stem}]] — {status}"
        )
    lines.append("")
    return "\n".join(lines)


def _view_title(content: str) -> str:
    compact = " ".join(content.split())
    return compact if len(compact) <= 100 else compact[:97].rstrip() + "..."


def _previous_view_paths(root: Path) -> set[Path]:
    manifest_path = root / VIEW_MANIFEST
    if not manifest_path.is_file():
        return set()
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        views = document["views"]
        if not isinstance(views, list):
            return set()
        return {
            Path(path)
            for item in views
            if isinstance(item, dict)
            and isinstance(path := item.get("path"), str)
            and Path(path).is_relative_to(VIEW_ROOT)
        }
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return set()


def _load_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / VIEW_MANIFEST
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UserInputError(
            "knowledge views have not been built or their manifest is invalid"
        ) from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise IntegrityError("knowledge view manifest has an invalid schema")
    return document


def _manifest_view(item: object) -> tuple[str, Path, str]:
    if not isinstance(item, dict):
        raise IntegrityError("knowledge view manifest entry is invalid")
    memory_id = item.get("memory_id")
    path_value = item.get("path")
    content_hash = item.get("content_hash")
    relative_path = Path(path_value) if isinstance(path_value, str) else Path(".")
    if (
        not isinstance(memory_id, str)
        or re.fullmatch(r"mem_[0-9a-f]{64}", memory_id) is None
        or not isinstance(path_value, str)
        or not relative_path.is_relative_to(VIEW_ROOT)
        or not isinstance(content_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash) is None
    ):
        raise IntegrityError("knowledge view manifest entry is invalid")
    return memory_id, relative_path, content_hash


def _current_understanding(text: str, path: Path) -> str:
    text = text.replace("\r\n", "\n")
    marker = "## Current understanding\n\n"
    if marker not in text:
        raise UserInputError(f"knowledge view is missing current understanding: {path}")
    remainder = text.split(marker, 1)[1]
    understanding = remainder.split("\n\n## ", 1)[0].strip()
    compact = " ".join(understanding.split())
    if not compact:
        raise UserInputError(f"knowledge view current understanding is blank: {path}")
    if len(compact) > 500:
        raise UserInputError(
            f"knowledge view current understanding exceeds 500 characters: {path}"
        )
    return compact
