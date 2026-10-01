"""Per-source pipeline health, persisted alongside the signal log.

Signal freshness cannot be inferred from the signal log: reconciliation emits
no event for an unchanged signal, so an old last_seen means either "healthy and
unchanged" or "source has been failing". Run outcomes are recorded separately so
the two are distinguishable, per AGENTS.md section 5.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cairn.core.runner import RunReport

DEFAULT_STALE_AFTER = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    last_attempt: datetime
    last_success: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None

    def age(self, now: datetime) -> timedelta | None:
        """How old the data is. None when the source has never succeeded."""
        return None if self.last_success is None else now - self.last_success

    def is_stale(self, now: datetime, after: timedelta = DEFAULT_STALE_AFTER) -> bool:
        age = self.age(now)
        return age is None or age > after

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "last_attempt": self.last_attempt.isoformat(),
            "last_success": (
                self.last_success.isoformat() if self.last_success else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }
        if now is not None:
            age = self.age(now)
            payload["age_seconds"] = None if age is None else int(age.total_seconds())
            payload["stale"] = self.is_stale(now)
        return payload


def append(path: Path, report: RunReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report.as_dict(), sort_keys=True) + "\n")


def read(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def replay(runs: Iterable[dict[str, Any]]) -> dict[str, SourceHealth]:
    health: dict[str, SourceHealth] = {}
    for record in runs:
        at = datetime.fromisoformat(record["started_at"])
        for outcome in record.get("outcomes", []):
            source = outcome["source"]
            previous = health.get(source)
            failures = (
                0
                if outcome["ok"]
                else (previous.consecutive_failures + 1 if previous else 1)
            )
            health[source] = SourceHealth(
                source=source,
                last_attempt=at,
                last_success=at
                if outcome["ok"]
                else (previous.last_success if previous else None),
                consecutive_failures=failures,
                last_error=None if outcome["ok"] else outcome.get("error"),
            )
    return health


def load(path: Path) -> dict[str, SourceHealth]:
    return replay(read(path))


def latest_series(runs: Iterable[dict[str, Any]]) -> str | None:
    """The series the most recent successful run observed.

    A run in which every source failed observed nothing, so it does not move
    the answer. Records written before runs carried a series yield None.
    """
    found = None
    for record in runs:
        ok = any(o.get("ok") for o in record.get("outcomes", []))
        if record.get("series") and ok:
            found = record["series"]
    return found
