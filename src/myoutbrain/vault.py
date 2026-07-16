from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from myoutbrain.note_title import NoteTitleError, normalize_note_title


class VaultIntegrityError(Exception):
    """Raised when permanent Vault knowledge cannot be interpreted safely."""


class KnowledgeTransitionError(Exception):
    """Raised when a requested knowledge-state transition is not allowed."""


@dataclass(frozen=True)
class CognitionPromotion:
    insight_path: Path
    insight_content: bytes
    cognition_path: Path
    cognition_content: bytes
    superseded_path: Path | None
    superseded_content: bytes | None

    @property
    def changes(self) -> tuple[tuple[Path, bytes], ...]:
        changes = [
            (self.insight_path, self.insight_content),
            (self.cognition_path, self.cognition_content),
        ]
        if self.superseded_path is not None and self.superseded_content is not None:
            changes.append((self.superseded_path, self.superseded_content))
        return tuple(changes)


def prepare_cognition_promotion(
    vault: Path,
    *,
    insight_id: str,
    cognition_id: str,
    title: str,
    occurred_at: datetime,
    supersedes_id: str | None = None,
) -> CognitionPromotion:
    if re.fullmatch(r"ins_[0-9a-f]{32}", insight_id) is None:
        raise KnowledgeTransitionError(f"invalid derived insight identity: {insight_id}")
    insight_path, insight_text = _find_note(vault, insight_id)
    frontmatter, body = _split_note(insight_path, insight_text)
    kind = _scalar(frontmatter, "kind", insight_path)
    state = _scalar(frontmatter, "state", insight_path)
    if kind != "insight" or state != "active":
        raise KnowledgeTransitionError(
            f"promotion requires an active derived insight: {insight_id}"
        )
    try:
        normalized_title = normalize_note_title(title)
    except NoteTitleError as error:
        raise KnowledgeTransitionError(str(error)) from error
    cognition_path = vault / f"{normalized_title}.md"
    if cognition_path.exists():
        raise KnowledgeTransitionError(
            f"knowledge note already exists: {cognition_path.name}"
        )

    timestamp = occurred_at.isoformat()
    sensitivity = _scalar(frontmatter, "sensitivity", insight_path)
    prior_authorship = _scalar(frontmatter, "authorship", insight_path)
    if sensitivity not in ("local-only", "cloud-allowed"):
        raise VaultIntegrityError(
            f"knowledge note has invalid sensitivity: {insight_path}"
        )
    if prior_authorship not in ("system", "mixed"):
        raise VaultIntegrityError(
            f"derived insight has invalid authorship: {insight_path}"
        )
    sources = _list(frontmatter, "sources", insight_path)
    archived_frontmatter = _replace_scalar(frontmatter, "state", "archived")
    archived_frontmatter = _replace_scalar(
        archived_frontmatter,
        "updated_at",
        timestamp,
    )
    archived_frontmatter = f"{archived_frontmatter}\npromoted_to: {cognition_id}"
    superseded_path: Path | None = None
    superseded_content: bytes | None = None
    if supersedes_id is not None:
        if re.fullmatch(r"cog_[0-9a-f]{32}", supersedes_id) is None:
            raise KnowledgeTransitionError(
                f"invalid personal cognition identity: {supersedes_id}"
            )
        superseded_path, superseded_text = _find_note(vault, supersedes_id)
        superseded_frontmatter, superseded_body = _split_note(
            superseded_path,
            superseded_text,
        )
        superseded_kind = _scalar(superseded_frontmatter, "kind", superseded_path)
        superseded_state = _scalar(
            superseded_frontmatter,
            "state",
            superseded_path,
        )
        if superseded_kind != "cognition" or superseded_state != "active":
            raise KnowledgeTransitionError(
                f"supersession requires an active personal cognition: {supersedes_id}"
            )
        superseded_frontmatter = _replace_scalar(
            superseded_frontmatter,
            "state",
            "superseded",
        )
        superseded_frontmatter = _replace_scalar(
            superseded_frontmatter,
            "updated_at",
            timestamp,
        )
        superseded_frontmatter = _replace_scalar(
            superseded_frontmatter,
            "superseded_by",
            f"[{cognition_id}]",
        )
        superseded_content = _render_note(
            superseded_frontmatter,
            _append_related_note(superseded_body, normalized_title),
        ).encode("utf-8")

    cognition_body = re.sub(
        r"(?m)^# .+$",
        f"# {normalized_title}",
        body,
        count=1,
    )
    cognition_body = _append_related_note(cognition_body, insight_path.stem)
    if superseded_path is not None:
        cognition_body = _append_related_note(cognition_body, superseded_path.stem)
    source_lines = "\n".join(f"  - {source_id}" for source_id in sources)
    supersedes_metadata = f"[{supersedes_id}]" if supersedes_id is not None else "[]"
    cognition_frontmatter = (
        f"id: {cognition_id}\n"
        "kind: cognition\n"
        "state: active\n"
        "authorship: mixed\n"
        f"derived_authorship: {prior_authorship}\n"
        "endorsed_by: user\n"
        f"endorsed_at: {timestamp}\n"
        f"sensitivity: {sensitivity}\n"
        f"created_at: {timestamp}\n"
        f"updated_at: {timestamp}\n"
        "sources:\n"
        f"{source_lines}\n"
        f"derived_from: {insight_id}\n"
        f"supersedes: {supersedes_metadata}\n"
        "superseded_by: []"
    )
    return CognitionPromotion(
        insight_path=insight_path,
        insight_content=_render_note(
            archived_frontmatter,
            _append_related_note(body, normalized_title),
        ).encode("utf-8"),
        cognition_path=cognition_path,
        cognition_content=_render_note(
            cognition_frontmatter,
            cognition_body,
        ).encode("utf-8"),
        superseded_path=superseded_path,
        superseded_content=superseded_content,
    )


def _find_note(vault: Path, knowledge_id: str) -> tuple[Path, str]:
    matches: list[tuple[Path, str]] = []
    for path in sorted(vault.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise VaultIntegrityError(f"cannot read Vault note: {path}") from error
        try:
            frontmatter, _ = _split_note(path, text)
        except VaultIntegrityError:
            continue
        if re.search(rf"(?m)^id: {re.escape(knowledge_id)}$", frontmatter):
            matches.append((path, text))
    if not matches:
        raise KnowledgeTransitionError(f"knowledge note does not exist: {knowledge_id}")
    if len(matches) != 1:
        raise VaultIntegrityError(f"duplicate knowledge identity: {knowledge_id}")
    return matches[0]


def _split_note(path: Path, text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise VaultIntegrityError(f"knowledge note has no YAML frontmatter: {path}")
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise VaultIntegrityError(f"knowledge note has invalid YAML frontmatter: {path}")
    return text[4:closing], text[closing + 5 :]


def _scalar(frontmatter: str, key: str, path: Path) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}: (.+)$", frontmatter)
    if match is None or not match.group(1).strip():
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}")
    return match.group(1).strip()


def _list(frontmatter: str, key: str, path: Path) -> tuple[str, ...]:
    match = re.search(
        rf"(?m)^{re.escape(key)}:\n((?:  - .+\n?)*)",
        frontmatter,
    )
    if match is None:
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}")
    values = tuple(
        line.removeprefix("  - ").strip()
        for line in match.group(1).splitlines()
        if line.startswith("  - ")
    )
    if not values:
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}")
    return values


def _replace_scalar(frontmatter: str, key: str, value: str) -> str:
    updated, count = re.subn(
        rf"(?m)^{re.escape(key)}: .+$",
        f"{key}: {value}",
        frontmatter,
        count=1,
    )
    if count != 1:
        raise VaultIntegrityError(f"knowledge note has no {key} metadata")
    return updated


def _render_note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}\n---\n{body}"


def _append_related_note(body: str, title: str) -> str:
    link = f"[[{title}]]"
    if link in body:
        return body
    if "\n## Related\n" in body:
        return f"{body.rstrip()}\n- {link}\n"
    return f"{body.rstrip()}\n\n## Related\n\n- {link}\n"
