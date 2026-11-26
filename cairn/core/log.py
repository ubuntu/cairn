"""Append-only event log and its replay into current state.

The log is a directory of monthly files, data/signals/<YYYY-MM>.jsonl, each
event in the month of its timestamp (UTC). Only the current month's file is
ever appended to, so a finished month never changes again, and no file
approaches GitHub's 100 MB limit on one file: measured 26 Nov 2026, about
0.4 MB a day once test results moved to the detail, 0.8 MB on a busy one,
so 12 to 25 MB a month. Replay reads every month in order, so splitting
loses nothing. Nothing is ever deleted (AGENTS.md section 2: cairn
remembers).

The single file it replaced, data/signals.jsonl, is read first while it
exists and never written to again. A path ending in .jsonl is one file,
read and written as before, which tests use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
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


_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.jsonl$")


def is_single_file(path: Path) -> bool:
    return path.suffix == ".jsonl"


def month_of(ts: datetime) -> str:
    """The file an event belongs in. Naive timestamps are taken as UTC, as
    the rest of the log writes them."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return ts.strftime("%Y-%m")


def files(path: Path) -> list[Path]:
    """Every file of the log, oldest first.

    Month names sort as dates, and each month only holds events from that
    month, so reading them in name order replays events in the order they
    were written.
    """
    if is_single_file(path):
        return [path] if path.is_file() else []
    found = []
    legacy = path.with_suffix(".jsonl")
    if legacy.is_file():
        found.append(legacy)
    if path.is_dir():
        found += sorted(p for p in path.iterdir() if _MONTH_FILE.match(p.name))
    return found


def _write(path: Path, events: Iterable[Event]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(event.to_json() + "\n")


def append(path: Path, events: Iterable[Event]) -> int:
    events = list(events)
    if not events:
        return 0
    if is_single_file(path):
        _write(path, events)
        return len(events)
    by_month: dict[str, list[Event]] = {}
    for event in events:
        by_month.setdefault(month_of(event.ts), []).append(event)
    for month, found in sorted(by_month.items()):
        _write(path / f"{month}.jsonl", found)
    return len(events)


def read(path: Path) -> Iterator[Event]:
    for part in files(path):
        with part.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield Event.from_json(line)


def replay(events: Iterable[Event]) -> dict[str, SignalState]:
    """Fold the log into current state, keyed by signal_id.

    A reopened signal keeps its original first_seen and increments occurrences,
    so a package that has broken three times is distinguishable from a new one.

    The id is recomputed from each event's fields rather than read from the
    stored signal_id, so revising the identity rule re-applies to all history
    without rewriting the append-only log. Lines written before
    DEVELOPMENT_KINDS existed carry ids that hashed in the series; they still
    fold onto the right signal. The stored field is kept for readers of the
    raw file and is otherwise informational.
    """
    state: dict[str, SignalState] = {}
    for event in events:
        signal = event.to_signal()
        sid = signal.signal_id
        current = state.get(sid)
        if event.event is EventType.OPENED:
            if current is None:
                state[sid] = SignalState(
                    signal=signal,
                    source=event.source,
                    first_seen=event.ts,
                    last_seen=event.ts,
                )
            else:
                state[sid] = replace(
                    current,
                    signal=signal,
                    last_seen=event.ts,
                    resolved_at=None,
                    occurrences=current.occurrences + 1,
                )
        elif event.event is EventType.UPDATED and current is not None:
            state[sid] = replace(current, signal=signal, last_seen=event.ts)
        elif event.event is EventType.RESOLVED and current is not None:
            state[sid] = replace(current, last_seen=event.ts, resolved_at=event.ts)
    return state


def load(path: Path) -> dict[str, SignalState]:
    return replay(read(path))
