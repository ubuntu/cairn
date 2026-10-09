"""Open counts over time, replayed from the log.

The one thing no upstream page can draw: every source is a snapshot, so
only a record kept over time can say whether a board is shrinking. Counts
are taken after each ingest run's events, keyed by the run's timestamp
(every event a run appends shares it), so a run that changed nothing adds
no point and the line stays flat until the next change.

Python computes coordinates only. The SVG element, its labels and its
accessible description are written in templates, where human words live.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from cairn.core.log import Event, EventType
from cairn.ingest.base import Kind


@dataclass(frozen=True, slots=True)
class Point:
    ts: datetime
    count: int


@dataclass(frozen=True, slots=True)
class Trend:
    points: tuple[Point, ...] = ()

    @property
    def first(self) -> Point | None:
        return self.points[0] if self.points else None

    @property
    def last(self) -> Point | None:
        return self.points[-1] if self.points else None

    @property
    def peak(self) -> int:
        return max((p.count for p in self.points), default=0)

    @property
    def low(self) -> int:
        return min((p.count for p in self.points), default=0)

    @property
    def change(self) -> int:
        if not self.points:
            return 0
        return self.points[-1].count - self.points[0].count

    @property
    def drawable(self) -> bool:
        """Two points at different times: anything less is not a line."""
        return len(self.points) >= 2 and self.points[0].ts != self.points[-1].ts

    def until(self, when: datetime | None) -> Trend:
        """Held flat to `when`, the board's last successful collection: as
        far as cairn can vouch for the count. Never to the build time, which
        would claim a run that did not happen."""
        if not self.points or when is None or when <= self.points[-1].ts:
            return self
        return Trend((*self.points, Point(when, self.points[-1].count)))

    def path(self, width: float, height: float, pad: float = 2.0) -> str:
        """An SVG path through the points, as a step line.

        Steps rather than diagonals: between runs the count did not drift,
        it held until the next run changed it. `pad` keeps the line off the
        top and bottom edges only: horizontally the line spans the full
        width, so the date ticks from x_ticks() line up with it.
        """
        if not self.drawable:
            return ""
        t0 = self.points[0].ts.timestamp()
        span = self.points[-1].ts.timestamp() - t0
        # Start the axis at zero so a change of 9 on 780 looks like one;
        # an axis that starts at the minimum turns noise into a cliff.
        top = max(self.peak, 1)

        def x(p: Point) -> float:
            return (p.ts.timestamp() - t0) / span * width

        def y(p: Point) -> float:
            return pad + (1 - p.count / top) * (height - 2 * pad)

        first = self.points[0]
        parts = [f"M{x(first):.1f},{y(first):.1f}"]
        for p in self.points[1:]:
            parts.append(f"H{x(p):.1f}V{y(p):.1f}")
        return "".join(parts)

    def area(self, width: float, height: float, pad: float = 2.0) -> str:
        """The step line closed down to the baseline, for a light fill."""
        line = self.path(width, height, pad)
        if not line:
            return ""
        # The line ends at the right edge; drop to the baseline, run back to
        # the left edge and close.
        return f"{line}V{height - pad:.1f}H0Z"

    def x_ticks(self, most: int) -> list[tuple[float, date]]:
        """Dates to label along the time axis, as (fraction across, date).

        At UTC midnights, every 1, 2, 7, ... days: the smallest step that
        keeps to `most` labels. Steps are counted from a fixed day rather
        than from the first point, so a tick does not move between builds.
        """
        if not self.drawable:
            return []
        t0, t1 = self.points[0].ts, self.points[-1].ts
        span = (t1 - t0).total_seconds()
        first, last = t0.date(), t1.date()
        days = (last - first).days
        step = next(
            (s for s in TICK_STEPS if days // s + 1 <= most), TICK_STEPS[-1]
        )
        ticks = []
        day = first
        while day <= last:
            midnight = datetime.combine(day, time(), tzinfo=t0.tzinfo or UTC)
            if day.toordinal() % step == 0 and t0 <= midnight <= t1:
                ticks.append(((midnight - t0).total_seconds() / span, day))
            day += timedelta(days=1)
        # Less than a step's worth of history: name where the line starts.
        return ticks or [(0.0, first)]


# Days between date labels, smallest first.
TICK_STEPS = (1, 2, 7, 14, 28, 91, 182, 364)


@dataclass(frozen=True, slots=True)
class Trends:
    by_kind: dict[Kind, Trend] = field(default_factory=dict)

    def of(self, kind: Kind) -> Trend:
        return self.by_kind.get(kind, Trend())


def trends(events: Iterable[Event]) -> Trends:
    """Active signals per kind after each run.

    Identity is recomputed from each event, as replay does, so a revised
    identity rule (DEVELOPMENT_KINDS) folds old lines onto the right signal.
    """
    active: dict[Kind, set[str]] = {}
    points: dict[Kind, list[Point]] = {}
    ordered = sorted(events, key=lambda e: e.ts)
    for i, event in enumerate(ordered):
        sid = event.to_signal().signal_id
        found = active.setdefault(event.kind, set())
        if event.event is EventType.RESOLVED:
            found.discard(sid)
        else:
            found.add(sid)
        # Record once per run: after the last event sharing this timestamp,
        # and only for a kind whose count that run changed.
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        if nxt is None or nxt.ts != event.ts:
            for kind, ids in active.items():
                series = points.setdefault(kind, [])
                if not series or series[-1].count != len(ids):
                    series.append(Point(event.ts, len(ids)))
    return Trends({kind: Trend(tuple(p)) for kind, p in points.items()})
