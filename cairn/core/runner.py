"""Runs ingesters without letting one failure lose the others.

A source that fails contributes no events, so its previous signals stay in the
log and are carried forward. Silence is never read as "everything got fixed".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cairn.core.log import Event, SignalState
from cairn.core.reconcile import DEFAULT_MAX_RESOLVE_FRACTION, reconcile
from cairn.ingest.base import Ingester

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Outcome:
    source: str
    ok: bool
    signals: int = 0
    events: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "signals": self.signals,
            "events": self.events,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RunReport:
    started_at: datetime
    outcomes: tuple[Outcome, ...]
    events: tuple[Event, ...]

    @property
    def succeeded(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if o.ok)

    @property
    def failed(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if not o.ok)

    @property
    def total_failure(self) -> bool:
        """Every source failed. The only condition that justifies exit 1."""
        return bool(self.outcomes) and not self.succeeded

    @property
    def stale_sources(self) -> tuple[str, ...]:
        return tuple(o.source for o in self.failed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "outcomes": [o.as_dict() for o in self.outcomes],
            "events": len(self.events),
            "stale_sources": list(self.stale_sources),
            "total_failure": self.total_failure,
        }


def run(
    ingesters: Iterable[Ingester],
    previous: Mapping[str, SignalState],
    *,
    now: datetime,
    max_resolve_fraction: float | None = DEFAULT_MAX_RESOLVE_FRACTION,
) -> RunReport:
    """Pass max_resolve_fraction=None to accept a legitimate mass resolution.

    Without an override the guard is a deadlock: a source that genuinely drops
    most of its signals fails on every subsequent run.
    """
    outcomes: list[Outcome] = []
    events: list[Event] = []

    for ingester in ingesters:
        source = ingester.name
        try:
            signals = list(ingester.run())
        except Exception as exc:  # noqa: BLE001 - one source must not lose the rest
            log.warning("ingest failed for %s: %s", source, exc)
            outcomes.append(
                Outcome(source, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
            continue

        try:
            produced = reconcile(
                previous,
                signals,
                source=source,
                now=now,
                max_resolve_fraction=max_resolve_fraction,
            )
        except Exception as exc:  # noqa: BLE001 - includes the MassResolve guard
            log.warning("reconcile refused for %s: %s", source, exc)
            outcomes.append(
                Outcome(
                    source,
                    ok=False,
                    signals=len(signals),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        events.extend(produced)
        outcomes.append(
            Outcome(source, ok=True, signals=len(signals), events=len(produced))
        )

    return RunReport(started_at=now, outcomes=tuple(outcomes), events=tuple(events))
