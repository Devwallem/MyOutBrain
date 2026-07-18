from __future__ import annotations

import json
from pathlib import Path
from typing import cast


SERVER_MINIMUM_PROTOCOL_VERSION = {"major": 2, "minor": 0}
SERVER_PROTOCOL_VERSION = {"major": 2, "minor": 2}
SERVER_CAPABILITIES = (
    "instance_status.v1",
    "capsule_maintenance.v1",
    "review_list.v1",
    "review_payload.v1",
    "review_decision.v1",
    "review_effect.create_derived_memory.v1",
    "review_effect.create_canonical_memory.v1",
    "review_effect.create_source_backed_canonical_memory.v1",
    "review_effect.revise_canonical_memory.v1",
    "review_effect.create_human_archive.v1",
    "review_effect.create_research_thread.v1",
)


def load_domain_schema(name: str) -> dict[str, object]:
    path = Path(__file__).with_name("schemas") / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot load packaged domain schema: {name}") from error
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise RuntimeError(f"packaged domain schema is invalid: {name}")
    return cast(dict[str, object], data)
