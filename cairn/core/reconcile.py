"""Diff the signals a source reports now against what the log already knows.

Pure: no I/O, no clock, no filesystem. The caller supplies both sides and the
timestamp, which is what makes history reproducible in tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from cairn.core.log import Event, EventType, SignalState
from cairn.ingest.base import Signal

DEFAULT_MAX_RESOLVE_FRACTION = 0.5


class MassResolve(RuntimeError):
    """Raised when one run would resolve an implausible share of a source."""


def reconcile(
    previous: Mapping[str, SignalState],
    current: Sequence[Signal],
    *,
    source: str,
    now: datetime,
    max_resolve_fraction: float | None = DEFAULT_MAX_RESOLVE_FRACTION,
) -> list[Event]:
    """Emit only what changed.

    Call this only after a *successful* ingest. A failed fetch yields zero
    signals, which is indistinguishable from "everything got fixed" — hence the
    guard below.
    """
    active = {
        sid: st for sid, st in previous.items() if st.source == source and st.is_active
    }
    seen = {signal.signal_id: signal for signal in current}

    events: list[Event] = []

    for sid, signal in seen.items():
        known = active.get(sid)
        if known is None:
            events.append(Event.of(EventType.OPENED, signal, source=source, ts=now))
        elif dict(known.signal.payload) != dict(signal.payload):
            events.append(Event.of(EventType.UPDATED, signal, source=source, ts=now))

    gone = [sid for sid in active if sid not in seen]

    if max_resolve_fraction is not None and active and gone:
        if len(gone) / len(active) > max_resolve_fraction:
            raise MassResolve(
                f"{source}: {len(gone)} of {len(active)} signals would resolve "
                f"in one run. Pass max_resolve_fraction=None if this is real."
            )

    for sid in gone:
        events.append(
            Event.of(
                EventType.RESOLVED,
                active[sid].signal,
                source=source,
                ts=now,
            )
        )

    return events
