"""Which Ubuntu series is open for development.

Launchpad is the system of record for series status, so this is primary data
under AGENTS.md section 4. It lives here rather than beside the oracle that
first needed it, because ingest must never import from cairn.oracles.
"""

from __future__ import annotations

import json

from cairn.ingest.base import Fetcher

SERIES_URL = "https://api.launchpad.net/devel/ubuntu/series"

# Statuses a series passes through while still open for development.
DEVELOPMENT_STATUSES = frozenset({"Active Development", "Pre-release Freeze"})


def development_series(fetcher: Fetcher) -> str:
    """The collection paginates and is not ordered by status."""
    url: str | None = SERIES_URL
    while url:
        page = json.loads(fetcher.get(url))
        for entry in page["entries"]:
            if entry.get("status") in DEVELOPMENT_STATUSES:
                return entry["name"]
        url = page.get("next_collection_link")
    raise LookupError("no Ubuntu series is open for development")
