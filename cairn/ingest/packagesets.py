"""Which package sets each source belongs to.

Package sets are the *upload rights* axis and are keyed by (name, series).
They are not teams, and the two must never be flattened into one notion of a
group: AGENTS.md section 5 records that ubuntu-desktop exists on both axes
covering different populations. This module only ever reports package sets.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from cairn.ingest.base import Fetcher

log = logging.getLogger(__name__)

LAUNCHPAD = "https://api.launchpad.net/devel"


def by_series_url(series: str, *, size: int = 100) -> str:
    query = urlencode(
        {
            "ws.op": "getBySeries",
            "distroseries": f"{LAUNCHPAD}/ubuntu/{series}",
            "ws.size": size,
        }
    )
    return f"{LAUNCHPAD}/package-sets?{query}"


def parse_sets(raw: bytes) -> tuple[list[tuple[str, str]], str | None]:
    """Returns (name, sources_url) pairs plus the next page."""
    page = json.loads(raw)
    sets = [
        (entry["name"], f"{entry['self_link']}?ws.op=getSourcesIncluded")
        for entry in page.get("entries", [])
        if entry.get("name") and entry.get("self_link")
    ]
    return sets, page.get("next_collection_link")


def fetch(fetcher: Fetcher, series: str) -> dict[str, tuple[str, ...]]:
    """Maps source package name to the package sets that include it."""
    membership: dict[str, list[str]] = {}
    url: str | None = by_series_url(series)
    names: list[tuple[str, str]] = []

    while url:
        page, url = parse_sets(fetcher.get(url))
        names.extend(page)

    for name, sources_url in names:
        try:
            sources = json.loads(fetcher.get(sources_url))
        except Exception as exc:  # noqa: BLE001 - one empty set must not lose the rest
            log.warning("package set %s: %s", name, exc)
            continue
        for source in sources or ():
            membership.setdefault(source, []).append(name)

    log.info("package sets: %d sets, %d sources", len(names), len(membership))
    return {source: tuple(sorted(sets)) for source, sets in membership.items()}
