from __future__ import annotations

import re


class NoteTitleError(Exception):
    """Raised when a title cannot be represented as a safe Vault filename."""


def normalize_note_title(title: str) -> str:
    normalized = " ".join(title.split())
    if not normalized:
        raise NoteTitleError("knowledge note title must not be blank")
    if normalized in {".", ".."} or re.search(r'[<>:"/\\|?*]', normalized):
        raise NoteTitleError("knowledge note title contains invalid filename characters")
    if normalized.endswith((".", " ")):
        raise NoteTitleError("knowledge note title has an invalid filename ending")
    if len(normalized) > 120:
        raise NoteTitleError("knowledge note title is too long")
    return re.sub(
        r"(?<![\w'])([^\W_])",
        lambda match: match.group(1).upper(),
        normalized,
    )
