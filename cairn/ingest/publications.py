"""Who last uploaded each source, and when.

Launchpad is the system of record for publication history, so this is primary
data under AGENTS.md section 4. The archive indexes carry versions but neither
dates nor uploaders, which is why a merge board cannot honestly order itself
by how long a package has been waiting without this.

Measured: ~300 entries per page at roughly 5 s per page, so a full series is
about ten minutes. Callers should treat failure as degradation, not an error.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Container
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from cairn.ingest.base import Fetcher

log = logging.getLogger(__name__)

LAUNCHPAD = "https://api.launchpad.net/devel"
PRIMARY_ARCHIVE = f"{LAUNCHPAD}/ubuntu/+archive/primary"
PAGE_SIZE = 300


@dataclass(frozen=True, slots=True)
class Publication:
    source: str
    version: str
    uploaded: datetime | None = None
    uploader: str | None = None
    component: str | None = None


def person_name(link: str | None) -> str | None:
    """Launchpad person links look like .../~doko."""
    if not link:
        return None
    tail = link.rstrip("/").rsplit("/", 1)[-1]
    return tail.lstrip("~") or None


def published_sources_url(series: str, *, size: int = PAGE_SIZE) -> str:
    query = urlencode(
        {
            "ws.op": "getPublishedSources",
            "status": "Published",
            "distro_series": f"{LAUNCHPAD}/ubuntu/{series}",
            "ws.size": size,
        }
    )
    return f"{PRIMARY_ARCHIVE}?{query}"


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def parse_page(raw: bytes) -> tuple[list[Publication], str | None]:
    page = json.loads(raw)
    publications = [
        Publication(
            source=entry["source_package_name"],
            version=entry["source_package_version"],
            uploaded=_timestamp(entry.get("date_created")),
            uploader=person_name(entry.get("package_creator_link")),
            component=entry.get("component_name"),
        )
        for entry in page.get("entries", [])
        if entry.get("source_package_name")
    ]
    return publications, page.get("next_collection_link")


def _newer(a: Publication, b: Publication) -> Publication:
    """A source is published in several pockets; keep the latest upload."""
    if a.uploaded is None:
        return b
    if b.uploaded is None:
        return a
    return a if a.uploaded >= b.uploaded else b


def _tty_progress(pages: int, kept: int) -> None:
    """Launchpad takes minutes. Silence for that long reads as a hang."""
    if sys.stderr.isatty():
        print(
            f"\r  publications: {pages} pages, {kept} matched",
            end="",
            file=sys.stderr,
            flush=True,
        )


def fetch(
    fetcher: Fetcher,
    series: str,
    *,
    keep: Container[str] | None = None,
    max_pages: int | None = None,
    on_page: Callable[[int, int], None] | None = _tty_progress,
) -> dict[str, Publication]:
    """Pass `keep` to discard uninteresting sources as pages arrive.

    The archive publishes tens of thousands of sources and a merge board cares
    about a few hundred, so filtering here keeps the result proportional to
    what is actually used.
    """
    latest: dict[str, Publication] = {}
    url: str | None = published_sources_url(series)
    pages = 0

    while url and (max_pages is None or pages < max_pages):
        publications, url = parse_page(fetcher.get(url))
        pages += 1
        for publication in publications:
            if keep is not None and publication.source not in keep:
                continue
            known = latest.get(publication.source)
            latest[publication.source] = (
                publication if known is None else _newer(known, publication)
            )
        if on_page is not None:
            on_page(pages, len(latest))

    if on_page is _tty_progress and sys.stderr.isatty():
        print(file=sys.stderr)
    log.info("publications: %d sources over %d page(s)", len(latest), pages)
    return latest
