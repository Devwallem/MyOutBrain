from __future__ import annotations

from typing import Literal


Sensitivity = Literal["local-only", "cloud-allowed"]


class ConfigurationConflict(Exception):
    """Raised when private-instance configuration cannot be used safely."""


class IntegrityError(Exception):
    """Raised when durable state contradicts its recorded identity."""


class UserInputError(Exception):
    """Raised when a command input cannot be accepted."""


class WriterLocked(Exception):
    """Raised when another writer already owns the private-instance lock."""
