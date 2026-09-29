"""Divergence between cairn's merge candidates and merges.ubuntu.com.

The oracle validates the reimplementation; it is never an input. Produces a
Divergence record; formatting belongs to whatever consumes it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cairn.ingest.base import Fetcher, Signal
from cairn.ingest.merges import UBUNTU_COMPONENTS, MergesIngester
from cairn.ingest.series import development_series

ORACLE_URL = "https://merges.ubuntu.com/{component}.json"


@dataclass(frozen=True, slots=True)
class Divergence:
    subject: str
    scope: str
    oracle: str
    checked_at: datetime
    cairn: frozenset[str]
    published: frozenset[str]
    detail: dict[str, dict[str, Any]]

    @property
    def agree(self) -> frozenset[str]:
        return self.cairn & self.published

    @property
    def cairn_only(self) -> frozenset[str]:
        return self.cairn - self.published

    @property
    def missing(self) -> frozenset[str]:
        """Published entries cairn failed to find. The serious direction."""
        return self.published - self.cairn

    @property
    def explained(self) -> frozenset[str]:
        """cairn-only entries Ubuntu packaged independently of Debian."""
        return frozenset(
            name
            for name in self.cairn_only
            if self.detail.get(name, {}).get("independent_lineage")
        )

    @property
    def unexplained(self) -> frozenset[str]:
        return self.cairn_only - self.explained

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "scope": self.scope,
            "oracle": self.oracle,
            "checked_at": self.checked_at.isoformat(),
            "counts": {
                "cairn": len(self.cairn),
                "published": len(self.published),
                "agree": len(self.agree),
                "cairn_only": len(self.cairn_only),
                "explained": len(self.explained),
                "unexplained": len(self.unexplained),
                "missing": len(self.missing),
            },
            "missing": sorted(self.missing),
            "unexplained": sorted(self.unexplained),
        }


def published_candidates(fetcher: Fetcher, components: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for component in components:
        raw = fetcher.get(ORACLE_URL.format(component=component))
        names.update(entry["source_package"] for entry in json.loads(raw))
    return names


def compare(
    signals: Iterable[Signal],
    published: Iterable[str],
    *,
    scope: str,
    now: datetime | None = None,
) -> Divergence:
    found = {s.source_package: dict(s.payload) for s in signals}
    return Divergence(
        subject="needs_merge",
        scope=scope,
        oracle="merges.ubuntu.com",
        checked_at=now or datetime.now(UTC),
        cairn=frozenset(found),
        published=frozenset(published),
        detail=found,
    )


def check(
    fetcher: Fetcher,
    *,
    series: str,
    components: tuple[str, ...] = UBUNTU_COMPONENTS,
) -> Divergence:
    """merges.ubuntu.com publishes no series dimension, so comparing anything
    but the development series silently diffs against the wrong data."""
    current = development_series(fetcher)
    if series != current:
        raise ValueError(
            f"{series!r} is not open for development ({current!r} is); "
            "the merges.ubuntu.com oracle only covers the development series"
        )
    ingester = MergesIngester(fetcher, series=series, ubuntu_components=components)
    return compare(
        ingester.run(),
        published_candidates(fetcher, components),
        scope=f"{series}/{','.join(components)}",
    )
