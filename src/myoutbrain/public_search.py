from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
from typing import Literal
from urllib.parse import urlparse

from myoutbrain.generation import ProviderFailure


PublicSourceType = Literal["official", "primary", "reference"]


@dataclass(frozen=True)
class PublicSource:
    source_id: str
    url: str
    title: str
    content: str
    published_at: str
    retrieved_at: str
    source_type: PublicSourceType
    fact_key: str
    fact_value: str

    def to_data(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "source_type": self.source_type,
            "fact_key": self.fact_key,
            "fact_value": self.fact_value,
        }


class PublicQueryUnavailable(Exception):
    """Raised when no trusted local adapter can produce a public-safe query."""


def sanitized_public_query(question: str) -> str:
    configured = os.environ.get("MYOUTBRAIN_FAKE_SANITIZED_QUERY")
    if configured is None:
        raise PublicQueryUnavailable(
            "no trusted local sanitizer produced a public-safe query"
        )
    sanitized = " ".join(configured.split())
    if not sanitized:
        raise ProviderFailure("cannot create a non-private public search query")
    if len(sanitized) > 200:
        raise PublicQueryUnavailable("public-safe query exceeds 200 characters")
    return sanitized


def search_public_sources(
    query: str,
    *,
    time_sensitive: bool,
) -> tuple[PublicSource, ...]:
    request_file = os.environ.get("MYOUTBRAIN_FAKE_PUBLIC_SEARCH_REQUEST_FILE")
    if request_file is not None:
        Path(request_file).write_text(
            json.dumps({"query": query}, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    serialized = os.environ.get("MYOUTBRAIN_FAKE_PUBLIC_SEARCH_RESPONSE")
    if serialized is None:
        return ()
    try:
        response = json.loads(serialized)
        if not isinstance(response, dict):
            raise TypeError("response is not an object")
        results = response.get("results")
        if not isinstance(results, list):
            raise TypeError("results is not a list")
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ProviderFailure("public search provider returned invalid evidence") from error
    accepted_sources: list[PublicSource] = []
    for result in results:
        try:
            accepted_sources.append(_parse_source(result))
        except (TypeError, ValueError):
            continue
    sources = tuple(accepted_sources)
    sources = tuple(source for source in sources if _is_current(source, time_sensitive))
    return sources


def public_sources_conflict(sources: tuple[PublicSource, ...]) -> bool:
    values_by_key: dict[str, set[str]] = {}
    for source in sources:
        values_by_key.setdefault(source.fact_key.casefold(), set()).add(
            source.fact_value.casefold()
        )
    return any(len(values) > 1 for values in values_by_key.values())


def _parse_source(value: object) -> PublicSource:
    if not isinstance(value, dict):
        raise TypeError("public source is not an object")
    url = value.get("url")
    title = value.get("title")
    content = value.get("content")
    published_at = value.get("published_at")
    retrieved_at = value.get("retrieved_at")
    source_type = value.get("source_type")
    fact_key = value.get("fact_key")
    fact_value = value.get("fact_value")
    if (
        not isinstance(url, str)
        or urlparse(url).scheme != "https"
        or not urlparse(url).netloc
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(content, str)
        or not content.strip()
        or not isinstance(published_at, str)
        or not isinstance(retrieved_at, str)
        or source_type not in ("official", "primary", "reference")
        or not isinstance(fact_key, str)
        or not fact_key.strip()
        or not isinstance(fact_value, str)
        or not fact_value.strip()
    ):
        raise ValueError("public source fields are invalid")
    _parse_time(published_at)
    _parse_time(retrieved_at)
    return PublicSource(
        source_id=f"web_{hashlib.sha256(url.encode()).hexdigest()}",
        url=url,
        title=title.strip(),
        content=content.strip(),
        published_at=published_at,
        retrieved_at=retrieved_at,
        source_type=source_type,
        fact_key=fact_key.strip(),
        fact_value=fact_value.strip(),
    )


def _is_current(source: PublicSource, time_sensitive: bool) -> bool:
    published_at = _parse_time(source.published_at)
    retrieved_at = _parse_time(source.retrieved_at)
    retrieval_age = datetime.now(timezone.utc) - retrieved_at
    retrieved_now = (
        published_at <= retrieved_at
        and timedelta(days=-1) <= retrieval_age <= timedelta(days=1)
    )
    return retrieved_now and (
        not time_sensitive or retrieved_at - published_at <= timedelta(days=30)
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("public source time has no offset")
    return parsed
