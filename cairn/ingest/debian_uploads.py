"""When Debian first got ahead of what Ubuntu ships.

This is the number a merge board actually wants. "How long has Debian been
ahead" is answerable from Debian's side alone, and unlike Launchpad's
publication dates it does not reset when Ubuntu opens a new series and copies
the archive forward.

Dating the *current* Debian version is not the same question, and gets it
badly wrong: measured over the archive, 285 of 780 candidates would have their
wait understated, `live-build` by thirteen years. Every fresh Debian upload
would reset a wait that never stopped. So the answer is the earliest Debian
upload that already outranked Ubuntu's version.

UDD is Debian's own mined upload history, and AGENTS.md section 4 lists it as
a system of record, so reading it is primary rather than scraping someone's
report. It also answers in one query.

Credentials are the published read-only ones for the public mirror. There is
nothing secret here, which is why they can sit in the source.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime

from debian.debian_support import version_compare

log = logging.getLogger(__name__)

UDD = {
    "host": "udd-mirror.debian.net",
    "port": 5432,
    "dbname": "udd",
    "user": "udd-mirror",
    "password": "udd-mirror",
}

CONNECT_TIMEOUT = 20

# connect_timeout only bounds the handshake. Without a statement timeout a
# stalled query would hang until the workflow itself is killed, and a run that
# does not finish is a permanent gap in history.
STATEMENT_TIMEOUT_MS = 60_000

# upload_history.distribution is free text and carries historical variants
# such as "sid", "unstable contrib" and "frozen unstable", so the suite is
# matched as a word rather than compared for equality. Measured over the
# current candidates: equality dates 771 of 774, word matching dates all 774.
SUITE_ALIASES = {"unstable": ("unstable", "sid")}

QUERY = """
    SELECT source, version, MIN(date)
    FROM upload_history
    WHERE source IN %s
      AND distribution ~ %s
    GROUP BY source, version
"""


def suite_pattern(suite: str) -> str:
    names = SUITE_ALIASES.get(suite, (suite,))
    return r"(^| )(" + "|".join(names) + r")( |$)"


def _connect():
    import psycopg2

    return psycopg2.connect(
        connect_timeout=CONNECT_TIMEOUT,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        **UDD,
    )


def ahead_since(
    uploads: list[tuple[str, datetime]], ubuntu: str, debian: str
) -> datetime | None:
    """The earliest upload that outranked Ubuntu and leads to today's version.

    The upper bound matters. A package can carry an epoch that Debian later
    abandoned, and such a version outranks Ubuntu's forever without being on
    the path to what Debian ships now: openldap would otherwise report being
    ahead since 1999 on the strength of a 1:1.2.3-1 nobody has seen since.
    """
    qualifying = [
        date
        for version, date in uploads
        if version_compare(version, ubuntu) > 0
        and version_compare(version, debian) <= 0
    ]
    return min(qualifying) if qualifying else None


def fetch(
    candidates: Mapping[str, tuple[str, str]],
    *,
    suite: str = "unstable",
    connect: Callable[[], object] | None = None,
) -> dict[str, datetime]:
    """Maps source to when Debian first got ahead of the Ubuntu version.

    `candidates` maps a source name to its (ubuntu_version, debian_version).
    """
    wanted = {name: pair for name, pair in candidates.items() if name}
    if not wanted:
        return {}

    connection = (connect or _connect)()
    try:
        cursor = connection.cursor()
        cursor.execute(QUERY, (tuple(sorted(wanted)), suite_pattern(suite)))
        uploads: dict[str, list[tuple[str, datetime]]] = {}
        for source, version, date in cursor.fetchall():
            uploads.setdefault(source, []).append((version, date))
    finally:
        connection.close()

    found = {}
    for name, (ubuntu, debian) in wanted.items():
        when = ahead_since(uploads.get(name, []), ubuntu, debian)
        if when is not None:
            found[name] = when

    log.info(
        "debian uploads: %d of %d candidates dated in %s",
        len(found),
        len(wanted),
        suite,
    )
    return found
