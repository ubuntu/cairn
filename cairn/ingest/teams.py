"""Which teams are subscribed to bugs on each source.

This is the *responsibility* axis, and it is not the package set axis. A team
subscription says who gets the bug mail; a package set says who may upload.
AGENTS.md section 5 records that ubuntu-desktop exists on both under one name
while covering different populations, so the two are never merged here.

Two measured constraints shape this module. The collection paginates and the
first page is 75 entries, so a team with 1330 packages looks like 75 unless
next_collection_link is followed. And ws.size is rejected with 503 on this
operation, so the page size cannot be raised: expect ~30 s per team.

The team list is curated because Launchpad has no way to ask "which teams
matter for merges". Adding a name here is a product judgement, which is why it
lives in one reviewable place rather than being inferred.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Container, Iterable

from cairn.ingest.base import Fetcher

log = logging.getLogger(__name__)

LAUNCHPAD = "https://api.launchpad.net/devel"

TRACKED_TEAMS: tuple[str, ...] = (
    "debcrafters-packages",
    "desktop-packages",
    "foundations-bugs",
    "kubuntu-bugs",
    "ubuntu-openstack",
    "ubuntu-security",
    "ubuntu-server",
)


def subscriber_url(team: str) -> str:
    return f"{LAUNCHPAD}/~{team}?ws.op=getBugSubscriberPackages"


def parse_page(raw: bytes) -> tuple[list[str], str | None]:
    page = json.loads(raw)
    names = [entry["name"] for entry in page.get("entries", []) if entry.get("name")]
    return names, page.get("next_collection_link")


def _tty_progress(team: str, found: int) -> None:
    if sys.stderr.isatty():
        print(f"\r  teams: {team} ({found})", end="", file=sys.stderr, flush=True)


class TruncatedTeam(RuntimeError):
    """More pages remained than max_pages allowed."""


def packages(fetcher: Fetcher, team: str, *, max_pages: int = 40) -> list[str]:
    names: list[str] = []
    url: str | None = subscriber_url(team)
    pages = 0
    while url and pages < max_pages:
        page, url = parse_page(fetcher.get(url))
        names.extend(page)
        pages += 1
    if url:
        # Returning what we have would look like a team that simply owns
        # fewer packages, which is exactly the silent reassignment strict
        # mode exists to prevent.
        raise TruncatedTeam(f"{team}: stopped after {pages} pages with more to fetch")
    return names


def fetch(
    fetcher: Fetcher,
    teams: Iterable[str] = TRACKED_TEAMS,
    *,
    keep: Container[str] | None = None,
    on_team: Callable[[str, int], None] | None = _tty_progress,
    strict: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Maps source package name to the teams subscribed to its bugs.

    With strict=True a single failing team fails the whole call. A partial
    answer is worse than none here: it does not look like an error, it looks
    like those packages have no owner.
    """
    membership: dict[str, list[str]] = {}
    for team in teams:
        try:
            names = packages(fetcher, team)
        except Exception as exc:
            if strict:
                raise
            log.warning("team %s: %s", team, exc)
            continue
        kept = 0
        for name in names:
            if keep is not None and name not in keep:
                continue
            membership.setdefault(name, []).append(team)
            kept += 1
        if on_team is not None:
            on_team(team, kept)

    if on_team is _tty_progress and sys.stderr.isatty():
        print(file=sys.stderr)
    log.info("teams: %d sources have a subscribed team", len(membership))
    return {source: tuple(sorted(t)) for source, t in membership.items()}
