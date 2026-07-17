from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import re
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

    def to_data(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "source_type": self.source_type,
        }


def sanitized_public_query(question: str) -> str:
    configured = os.environ.get("MYOUTBRAIN_FAKE_SANITIZED_QUERY")
    if configured is not None:
        sanitized = " ".join(configured.split())
    else:
        sanitized = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", " ", question)
        sanitized = re.sub(r"(?i)\b(?:client|customer)\s+\S+", " ", sanitized)
        sanitized = re.sub(r"(?i)\bproject\s+[A-Z][\w-]*", "project", sanitized)
        sanitized = re.sub(r"\b(?:src|mem|exp|cand)_[0-9a-f]+\b", " ", sanitized)
        sanitized = re.sub(r"[A-Za-z]:\\\S+|/Users/\S+|/home/\S+", " ", sanitized)
        sanitized = " ".join(sanitized.split())
    private_markers = tuple(
        match.group(0)
        for pattern in (
            r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
            r"(?i)\bproject\s+[A-Z][\w-]*",
            r"(?i)\b(?:client|customer)\s+\S+",
            r"\b(?:src|mem|exp|cand)_[0-9a-f]+\b",
            r"[A-Za-z]:\\\S+|/Users/\S+|/home/\S+",
        )
        for match in re.finditer(pattern, question)
    )
    if any(marker.casefold() in sanitized.casefold() for marker in private_markers):
        raise ProviderFailure("sanitized public query still contains private context")
    if not sanitized:
        raise ProviderFailure("cannot create a non-private public search query")
    if len(sanitized) > 200:
        sanitized = sanitized[:200].rsplit(" ", 1)[0]
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
    if time_sensitive:
        sources = tuple(source for source in sources if _is_current(source))
    return sources


def _parse_source(value: object) -> PublicSource:
    if not isinstance(value, dict):
        raise TypeError("public source is not an object")
    url = value.get("url")
    title = value.get("title")
    content = value.get("content")
    published_at = value.get("published_at")
    retrieved_at = value.get("retrieved_at")
    source_type = value.get("source_type")
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
    )


def _is_current(source: PublicSource) -> bool:
    published_at = _parse_time(source.published_at)
    retrieved_at = _parse_time(source.retrieved_at)
    retrieval_age = datetime.now(timezone.utc) - retrieved_at
    return (
        published_at <= retrieved_at
        and timedelta(days=-1) <= retrieval_age <= timedelta(days=1)
        and retrieved_at - published_at <= timedelta(days=30)
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("public source time has no offset")
    return parsed
