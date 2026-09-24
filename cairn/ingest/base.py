"""Shared types. Nothing here knows what Launchpad or an archive index is."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable


class Kind(StrEnum):
    NEEDS_MERGE = "needs_merge"
    NBS = "nbs"
    SRU_PENDING = "sru_pending"
    SRU_VERIFICATION_FAILED = "sru_verification_failed"
    MIGRATION_BLOCKED = "migration_blocked"
    TRANSITION_BLOCKED = "transition_blocked"
    SPONSORSHIP_PENDING = "sponsorship_pending"
    BUILD_FAILED = "build_failed"


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
        """Excludes payload, so a changing day count does not reset history."""
        return (
            str(self.kind),
            self.source_package,
            self.binary_package or "",
            self.series or "",
        )

    @property
    def signal_id(self) -> str:
        """Stable across processes; hash() is salted and unusable here."""
        return hashlib.sha256("\x1f".join(self.identity).encode()).hexdigest()[:16]


Raw = TypeVar("Raw")


@runtime_checkable
class Fetcher(Protocol):
    def get(self, url: str) -> bytes: ...


class Ingester(ABC, Generic[Raw]):
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
