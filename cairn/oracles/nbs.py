"""Divergence between cairn's NBS and static-reports.ubuntu.com/nbs/.

The report (ubuntu-archive-tools' cron.NBS) is a report generator: an oracle
here, never an input. It publishes one file per NBS binary under NBS/,
holding checkrdepends' output for it, and the binaries it calls removable
in the remove-package command on nbs.html. Measured 23 Nov 2026: 21 files,
9 removable, matching the latest line of nbs.csv.

Only the development series is reported, so only it can be compared.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cairn.ingest.base import Fetcher, Signal
from cairn.ingest.nbs import NbsIngester
from cairn.ingest.series import development_series

ORACLE = "https://static-reports.ubuntu.com/nbs/"
# cron.NBS writes this for binaries a current source lists after all; it is
# not an NBS binary.
FTBFS = "00FTBFS"

_LINK = re.compile(r'<a href="([^"/?]+)">')
_REMOVE = re.compile(r"remove-package[^<]*?-y ([^<]*)")
_HEADER = re.compile(r"^-- \S+/(\S+) (\S+) (?:build )?deps on ")


@dataclass(frozen=True, slots=True)
class Published:
    # NBS binary -> what checkrdepends says needs it: binary names and, for
    # build dependencies, source names.
    needed_by: Mapping[str, frozenset[str]]
    removable: frozenset[str]


def parse_listing(html: str) -> list[str]:
    return [n for n in _LINK.findall(html) if n not in ("..", FTBFS)]


def parse_rdepends(text: str) -> frozenset[str]:
    names = set()
    for line in text.splitlines():
        if not line.strip() or _HEADER.match(line) or line.startswith("-- "):
            continue
        names.add(line.split()[0])
    return frozenset(names)


def parse_removable(html: str) -> frozenset[str]:
    match = _REMOVE.search(html)
    return frozenset(match.group(1).split()) if match else frozenset()


def published(fetcher: Fetcher) -> Published:
    names = parse_listing(fetcher.get(f"{ORACLE}NBS/").decode())
    return Published(
        needed_by={
            name: parse_rdepends(fetcher.get(f"{ORACLE}NBS/{name}").decode())
            for name in names
        },
        removable=parse_removable(fetcher.get(f"{ORACLE}nbs.html").decode()),
    )


@dataclass(frozen=True, slots=True)
class Divergence:
    scope: str
    checked_at: datetime
    cairn: frozenset[str]
    published: frozenset[str]
    cairn_removable: frozenset[str]
    published_removable: frozenset[str]
    # NBS binary -> (needed by in cairn only, needed by in the report only),
    # for binaries both list, where the two differ.
    needed_by: Mapping[str, tuple[frozenset[str], frozenset[str]]]

    @property
    def missing(self) -> frozenset[str]:
        """Reported as NBS, not found by cairn. The serious direction."""
        return self.published - self.cairn

    @property
    def cairn_only(self) -> frozenset[str]:
        return self.cairn - self.published

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": "nbs",
            "scope": self.scope,
            "oracle": ORACLE,
            "checked_at": self.checked_at.isoformat(),
            "counts": {
                "cairn": len(self.cairn),
                "published": len(self.published),
                "cairn_removable": len(self.cairn_removable),
                "published_removable": len(self.published_removable),
            },
            "missing": sorted(self.missing),
            "cairn_only": sorted(self.cairn_only),
            "removable_cairn_only": sorted(
                self.cairn_removable - self.published_removable
            ),
            "removable_published_only": sorted(
                self.published_removable - self.cairn_removable
            ),
            "needed_by": {
                name: {"cairn_only": sorted(c), "published_only": sorted(p)}
                for name, (c, p) in sorted(self.needed_by.items())
            },
        }


def compare(
    signals: Iterable[Signal],
    report: Published,
    *,
    scope: str,
    now: datetime | None = None,
) -> Divergence:
    found = {s.binary_package or "": s.payload for s in signals}
    differ = {}
    for name in set(found) & set(report.needed_by):
        payload = found[name]
        ours = frozenset(
            [r["package"] for r in payload.get("rdepends", ())]
            + [r["package"] for r in payload.get("build_rdepends", ())]
        )
        theirs = report.needed_by[name]
        if ours != theirs:
            differ[name] = (ours - theirs, theirs - ours)
    return Divergence(
        scope=scope,
        checked_at=now or datetime.now(UTC),
        cairn=frozenset(found),
        published=frozenset(report.needed_by),
        cairn_removable=frozenset(n for n, p in found.items() if p.get("removable")),
        published_removable=report.removable,
        needed_by=differ,
    )


def check(fetcher: Fetcher, *, series: str | None = None) -> Divergence:
    current = development_series(fetcher)
    series = series or current
    if series != current:
        raise ValueError(
            f"{series!r} is not open for development ({current!r} is); "
            "the NBS report only covers the development series"
        )
    return compare(
        NbsIngester(fetcher, series=series).run(), published(fetcher), scope=series
    )
