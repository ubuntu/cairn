"""Append-only event log and its replay into current state."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from cairn.ingest.base import Kind, Signal


class EventType(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Event:
    ts: datetime
    event: EventType
    signal_id: str
    source: str
    kind: Kind
    source_package: str
    series: str | None = None
    binary_package: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    url: str | None = None

    @classmethod
    def of(
        cls,
        event: EventType,
        signal: Signal,
        *,
        source: str,
        ts: datetime,
    ) -> Event:
        return cls(
            ts=ts,
            event=event,
            signal_id=signal.signal_id,
            source=source,
            kind=signal.kind,
            source_package=signal.source_package,
            series=signal.series,
            binary_package=signal.binary_package,
            payload=signal.payload,
            url=signal.url,
        )

    def to_signal(self) -> Signal:
        return Signal(
            kind=self.kind,
            source_package=self.source_package,
            series=self.series,
            binary_package=self.binary_package,
            payload=self.payload,
            url=self.url,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts.isoformat(),
                "event": str(self.event),
                "signal_id": self.signal_id,
                "source": self.source,
                "kind": str(self.kind),
                "source_package": self.source_package,
                "series": self.series,
                "binary_package": self.binary_package,
                "payload": dict(self.payload),
                "url": self.url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> Event:
        d = json.loads(line)
        return cls(
            ts=datetime.fromisoformat(d["ts"]),
            event=EventType(d["event"]),
            signal_id=d["signal_id"],
            source=d["source"],
            kind=Kind(d["kind"]),
            source_package=d["source_package"],
            series=d.get("series"),
            binary_package=d.get("binary_package"),
            payload=d.get("payload") or {},
            url=d.get("url"),
        )


@dataclass(frozen=True, slots=True)
class SignalState:
    signal: Signal
    source: str
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None = None
    occurrences: int = 1

    @property
    def is_active(self) -> bool:
        return self.resolved_at is None


def append(path: Path, events: Iterable[Event]) -> int:
    events = list(events)
    if not events:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(event.to_json() + "\n")
    return len(events)


def read(path: Path) -> Iterator[Event]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Event.from_json(line)


def replay(events: Iterable[Event]) -> dict[str, SignalState]:
    """Fold the log into current state, keyed by signal_id.

    A reopened signal keeps its original first_seen and increments occurrences,
    so a package that has broken three times is distinguishable from a new one.
    """
    state: dict[str, SignalState] = {}
    for event in events:
        current = state.get(event.signal_id)
        if event.event is EventType.OPENED:
            if current is None:
                state[event.signal_id] = SignalState(
                    signal=event.to_signal(),
                    source=event.source,
                    first_seen=event.ts,
                    last_seen=event.ts,
                )
            else:
                state[event.signal_id] = replace(
                    current,
                    signal=event.to_signal(),
                    last_seen=event.ts,
                    resolved_at=None,
                    occurrences=current.occurrences + 1,
                )
        elif event.event is EventType.UPDATED and current is not None:
            state[event.signal_id] = replace(
                current, signal=event.to_signal(), last_seen=event.ts
            )
        elif event.event is EventType.RESOLVED and current is not None:
            state[event.signal_id] = replace(
                current, last_seen=event.ts, resolved_at=event.ts
            )
    return state


def load(path: Path) -> dict[str, SignalState]:
    return replay(read(path))
