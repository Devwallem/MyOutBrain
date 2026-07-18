from __future__ import annotations

from enum import StrEnum
import re
from typing import Literal


Sensitivity = Literal["local-only", "cloud-allowed"]


def is_canonical_memory_id(value: str) -> bool:
    """Accept native and migrated stable canonical-memory identities."""
    return re.fullmatch(
        r"(?:mem_[0-9a-f]{64}|ins_[0-9a-f]{32}|cog_[0-9a-f]{32})",
        value,
    ) is not None


class MemoryState(StrEnum):
    CANONICAL = "canonical"
    HISTORICAL_TRUSTED = "historical-trusted"
    BUFFERED = "buffered"


class ConfigurationConflict(Exception):
    """Raised when private-instance configuration cannot be used safely."""


class IntegrityError(Exception):
    """Raised when durable state contradicts its recorded identity."""


class UserInputError(Exception):
    """Raised when a command input cannot be accepted."""


class IdempotencyConflict(UserInputError):
    """Raised when one idempotency key is reused for another request."""


class WriterLocked(Exception):
    """Raised when another writer already owns the private-instance lock."""
