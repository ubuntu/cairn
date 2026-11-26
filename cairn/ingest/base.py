"""Shared types. Nothing here knows what Launchpad or an archive index is."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Kind(StrEnum):
    NEEDS_MERGE = "needs_merge"
    NBS = "nbs"
    SRU_PENDING = "sru_pending"
    SRU_VERIFICATION_FAILED = "sru_verification_failed"
    MIGRATION_BLOCKED = "migration_blocked"
    TRANSITION_BLOCKED = "transition_blocked"
    SPONSORSHIP_PENDING = "sponsorship_pending"
    BUILD_FAILED = "build_failed"


# Kinds observed only in whichever series is open for development. That series
# is a moving pointer: at every release a new one opens and the archive is
# copied forward, but the outstanding work is the same work. Keeping the series
# out of identity lets a merge that waits through a release keep its history,
# rather than resolving as if merged and reopening as if new.
#
# NBS too (review on #26): cairn computes it, and the report it is checked
# against publishes it, only for the development series. A new series is
# opened by copying the archive forward, NBS binaries included, so the same
# binary is the same work; with the series in identity every NBS signal
# resolved at a release and the guard refused the run.
DEVELOPMENT_KINDS: frozenset[Kind] = frozenset(
    {Kind.NEEDS_MERGE, Kind.MIGRATION_BLOCKED, Kind.NBS}
)


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEVERITY_ORDER: dict[str, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def severity_rank(severity: Severity) -> int:
    """StrEnum compares alphabetically, which is meaningless for severity."""
    return _SEVERITY_ORDER[severity]


@dataclass(frozen=True, slots=True)
class Signal:
    """One observation, about one package, from one source.

    Severity is not a field: it is derived by core.rules, so revising a
    judgement re-applies to all history on the next rebuild.
    """

    kind: Kind
    source_package: str
    series: str | None = None
    binary_package: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    url: str | None = None

    def __post_init__(self) -> None:
        if not self.source_package:
            raise ValueError("source_package is required")
        if not isinstance(self.kind, Kind):
            raise TypeError(f"kind must be a Kind, got {type(self.kind).__name__}")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """Excludes payload, so a changing day count does not reset history.

        Excludes the series for DEVELOPMENT_KINDS, which still carry it as an
        observed field: a rollover is then an update, not a resolve and reopen.
        """
        series = "" if self.kind in DEVELOPMENT_KINDS else (self.series or "")
        return (
            str(self.kind),
            self.source_package,
            self.binary_package or "",
            series,
        )

    @property
    def signal_id(self) -> str:
        """Stable across processes; hash() is salted and unusable here."""
        return hashlib.sha256("\x1f".join(self.identity).encode()).hexdigest()[:16]


@runtime_checkable
class Fetcher(Protocol):
    def get(self, url: str) -> bytes: ...


class Ingester[Raw](ABC):
    """Translates one source into Signals.

    Generic over Raw because sources are not all HTTP: UDD yields database
    rows, the archive yields bytes.
    """

    name: str

    @abstractmethod
    def fetch(self) -> Raw: ...

    @abstractmethod
    def parse(self, raw: Raw) -> Sequence[Signal]: ...

    def run(self) -> Sequence[Signal]:
        return self.parse(self.fetch())

    def detail(self) -> Mapping[str, Any] | None:
        """What the last parse found that the log does not keep.

        Detail that changes from run to run without being worth remembering
        (a test finishing on one more architecture) would append an update
        per signal per run. It is kept here instead, and saved whole after a
        successful run, overwriting the last: current state, no history.
        None for a source with nothing beyond its signals.
        """
        return None
