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


@dataclass(frozen=True)
class KnowledgeNoteSnapshot:
    knowledge_id: str
    kind: str
    state: str
    authorship: str
    sensitivity: str
    created_at: str
    updated_at: str
    sources: tuple[str, ...]
    supersedes: tuple[str, ...]
    superseded_by: tuple[str, ...]
    derived_from: str | None
    promoted_to: str | None
    candidate_id: str | None
    path: Path
    body: str


def scan_knowledge_notes(vault: Path) -> tuple[KnowledgeNoteSnapshot, ...]:
    notes: list[KnowledgeNoteSnapshot] = []
    identities: set[str] = set()
    for path in sorted(vault.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise VaultIntegrityError(f"cannot read Vault note: {path}") from error
        if not text.startswith("---\n"):
            continue
        frontmatter, body = _split_note(path, text)
        if re.search(r"(?m)^id: ", frontmatter) is None:
            continue
        knowledge_id = _scalar(frontmatter, "id", path)
        kind = _scalar(frontmatter, "kind", path)
        state = _scalar(frontmatter, "state", path)
        authorship = _scalar(frontmatter, "authorship", path)
        sensitivity = _scalar(frontmatter, "sensitivity", path)
        created_at = _scalar(frontmatter, "created_at", path)
        updated_at = _scalar(frontmatter, "updated_at", path)
        sources = _list(frontmatter, "sources", path)
        supersedes = _inline_list(frontmatter, "supersedes", path)
        superseded_by = _inline_list(frontmatter, "superseded_by", path)
        if kind not in ("insight", "cognition"):
            raise VaultIntegrityError(f"knowledge note has invalid kind: {path}")
        expected_prefix = "ins_" if kind == "insight" else "cog_"
        if re.fullmatch(rf"{expected_prefix}[0-9a-f]{{32}}", knowledge_id) is None:
            raise VaultIntegrityError(f"knowledge note has invalid identity: {path}")
        if knowledge_id in identities:
            raise VaultIntegrityError(f"duplicate knowledge identity: {knowledge_id}")
        identities.add(knowledge_id)
        if state not in ("active", "superseded", "archived"):
            raise VaultIntegrityError(f"knowledge note has invalid state: {path}")
        if authorship not in ("user", "system", "mixed"):
            raise VaultIntegrityError(f"knowledge note has invalid authorship: {path}")
        if sensitivity not in ("local-only", "cloud-allowed"):
            raise VaultIntegrityError(f"knowledge note has invalid sensitivity: {path}")
        _validate_timestamp(created_at, "created_at", path)
        _validate_timestamp(updated_at, "updated_at", path)
        if any(re.fullmatch(r"src_[0-9a-f]{64}", value) is None for value in sources):
            raise VaultIntegrityError(f"knowledge note has invalid sources: {path}")
        derived_from = _optional_scalar(frontmatter, "derived_from")
        promoted_to = _optional_scalar(frontmatter, "promoted_to")
        candidate_id = _optional_scalar(frontmatter, "candidate_id")
        if kind == "insight" and (
            candidate_id is None
            or re.fullmatch(r"cand_[0-9a-f]{64}", candidate_id) is None
        ):
            raise VaultIntegrityError(f"derived insight has invalid candidate_id: {path}")
        if kind == "cognition" and candidate_id is not None:
            raise VaultIntegrityError(f"personal cognition has invalid candidate_id: {path}")
        notes.append(
            KnowledgeNoteSnapshot(
                knowledge_id=knowledge_id,
                kind=kind,
                state=state,
                authorship=authorship,
                sensitivity=sensitivity,
                created_at=created_at,
                updated_at=updated_at,
                sources=sources,
                supersedes=supersedes,
                superseded_by=superseded_by,
                derived_from=derived_from,
                promoted_to=promoted_to,
                candidate_id=candidate_id,
                path=path,
                body=body,
            )
        )
    return tuple(notes)


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


def _inline_list(frontmatter: str, key: str, path: Path) -> tuple[str, ...]:
    value = _scalar(frontmatter, key, path)
    if not value.startswith("[") or not value.endswith("]"):
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}")
    inner = value[1:-1].strip()
    if not inner:
        return ()
    values = tuple(item.strip() for item in inner.split(","))
    if any(re.fullmatch(r"cog_[0-9a-f]{32}", item) is None for item in values):
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}")
    return values


def _optional_scalar(frontmatter: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}: (.+)$", frontmatter)
    return match.group(1).strip() if match is not None else None


def _validate_timestamp(value: str, key: str, path: Path) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}") from error
    if parsed.tzinfo is None:
        raise VaultIntegrityError(f"knowledge note has invalid {key}: {path}")


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
