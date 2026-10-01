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
from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from debian.debian_support import version_compare

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
    # package_creator: the person in the changelog, who made the change. Not
    # the sponsor. Measured: shadow 1:4.19.3-2ubuntu1 has creator nadzeya and
    # signer seb128, who sponsored it. This is the uploader cairn shows.
    uploader: str | None = None
    component: str | None = None
    # package_signer: whoever signed the upload, which is the sponsor when
    # there is one. Never shown, and never used as the uploader. cairn only
    # asks whether it is None: an upload Launchpad copied rather than one a
    # developer signed, which is an auto-sync from Debian (measured on gettext
    # and sphinx), whose creator is the *Debian* uploader.
    signer: str | None = None
    pocket: str | None = None

    @property
    def synced(self) -> bool:
        return self.signer is None


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
            signer=person_name(entry.get("package_signer_link")),
            pocket=entry.get("pocket"),
        )
        for entry in page.get("entries", [])
        if entry.get("source_package_name")
    ]
    return publications, page.get("next_collection_link")


def _newer(a: Publication, b: Publication) -> Publication:
    """A source is published in several pockets; keep the highest version.

    Not the latest date: when a series opens, every package is copied into
    both pockets at one instant, so dates tie and the answer would depend on
    the order Launchpad happened to list them in. On equal versions the later
    publication wins.
    """
    order = version_compare(a.version, b.version)
    if order:
        return a if order > 0 else b
    if a.uploaded is None:
        return b
    if b.uploaded is None:
        return a
    return a if a.uploaded >= b.uploaded else b


def latest(found: Iterable[Publication]) -> Publication | None:
    best = None
    for publication in found:
        best = publication if best is None else _newer(best, publication)
    return best


# In CI there is no terminal to redraw, so progress becomes a log line every
# LOG_EVERY pages: often enough to tell a slow crawl from a stuck one.
LOG_EVERY = 10


def _progress(pages: int, kept: int) -> None:
    """Launchpad takes minutes. Silence for that long reads as a hang."""
    if sys.stderr.isatty():
        print(
            f"\r  publications: {pages} pages, {kept} matched",
            end="",
            file=sys.stderr,
            flush=True,
        )
    elif pages % LOG_EVERY == 0:
        log.info("publications: %d pages, %d matched", pages, kept)


def fetch_all(
    fetcher: Fetcher,
    series: str,
    *,
    keep: Container[str] | None = None,
    max_pages: int | None = None,
    on_page: Callable[[int, int], None] | None = _progress,
) -> dict[str, tuple[Publication, ...]]:
    """Every current publication of each source: release and -proposed.

    Pass `keep` to discard uninteresting sources as pages arrive.

    The archive publishes tens of thousands of sources and the board cares
    about a few hundred, so filtering here keeps the result proportional to
    what is actually used. All publications are kept, not only the newest,
    because a row has to name the uploader of the version it shows.
    """
    found: dict[str, list[Publication]] = {}
    url: str | None = published_sources_url(series)
    pages = 0
    log.info("publications: crawling %s", series)

    while url and (max_pages is None or pages < max_pages):
        page, url = parse_page(fetcher.get(url))
        pages += 1
        for publication in page:
            if keep is not None and publication.source not in keep:
                continue
            found.setdefault(publication.source, []).append(publication)
        if on_page is not None:
            on_page(pages, len(found))

    if on_page is _progress and sys.stderr.isatty():
        print(file=sys.stderr)
    log.info("publications: %d sources over %d page(s)", len(found), pages)
    return {name: tuple(pubs) for name, pubs in found.items()}


def fetch(
    fetcher: Fetcher,
    series: str,
    *,
    keep: Container[str] | None = None,
    max_pages: int | None = None,
    on_page: Callable[[int, int], None] | None = _progress,
) -> dict[str, Publication]:
    """The newest publication of each source."""
    every = fetch_all(fetcher, series, keep=keep, max_pages=max_pages, on_page=on_page)
    return {name: best for name, pubs in every.items() if (best := latest(pubs))}
