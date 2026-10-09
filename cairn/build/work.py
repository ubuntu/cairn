"""What anyone can pick up, and what a +1 maintenance shift should look at.

Both are rules about whose work something is, kept in one place so the
"Up for grabs" and "+1 maintenance" pages cannot disagree.

Deciding that a piece of work is free to take is a product judgement, like
severity in core/rules.py, so it lives in one reviewable module rather than
in templates or scattered conditions. It is a build-time judgement rather
than a signal rule because it needs ownership and time, which a Signal does
not carry.

Ubuntu's own process supplies the categories:

- A Debian sync has no Ubuntu uploader to own getting it to migrate. Ubuntu's
  proposed-migration docs give those to +1 maintenance, which is to say to
  anyone on the rota, and the +1 maintenance reports list them as such.
- An upload stuck long after its uploader could reasonably have dealt with it
  is treated as abandoned by the +1 maintenance process: britney's by-team
  report holds young items back as "not yet considered late", and older ones
  are fair game.
- Merges belong to +1 maintenance as well, but the person in the changelog
  normally does them, so the merges listed here are the smaller ones (no new
  upstream release) and the page says to check with the uploader first.

What cairn cannot see, it says so rather than guesses. An update-excuse bug
on a stuck upload is not a claim: anyone can file one, and it may sit
unassigned. Whether someone is on it is the bug's assignee, which cairn does
not collect, so uploads with a bug stay listed and their rows link the bug
for the reader to check. cairn has no view of merge proposals or of who has
started a merge locally either.

Measured 5 Oct 2026, for scale: of 278 uploads stuck past settling, 216 were
syncs and 37 more Ubuntu uploads had been stuck over 30 days; 425 of 809
merge candidates had no new upstream release; 52 stuck uploads carried an
update-excuse bug.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cairn.build.site import BlockingRow, Row, StuckRow

# Past this, an upload whose uploader has not moved it is treated as free to
# take. A month: long past the settling window, and short of most of the
# long tail (the median stuck upload had been there 106 days on 5 Oct 2026).
ABANDONED_DAYS = 30


@dataclass(frozen=True, slots=True)
class UpForGrabs:
    """The work anyone can take, by kind of work."""

    syncs: list[StuckRow]
    abandoned: list[StuckRow]
    merges: list[Row]

    @property
    def total(self) -> int:
        return len(self.syncs) + len(self.abandoned) + len(self.merges)

    @property
    def stuck_names(self) -> set[str]:
        return {r.package for r in (*self.syncs, *self.abandoned)}


def up_for_grabs(
    rows: Iterable[Row], stuck: Iterable[StuckRow], *, now: datetime
) -> UpForGrabs:
    syncs: list[StuckRow] = []
    abandoned: list[StuckRow] = []
    for row in stuck:
        if row.settling(now):
            continue
        if row.synced:
            syncs.append(row)
            continue
        days = row.days(now)
        if days is not None and days >= ABANDONED_DAYS:
            abandoned.append(row)
    merges = [r for r in rows if not r.new_upstream]
    return UpForGrabs(syncs=syncs, abandoned=abandoned, merges=merges)


# An item is "high impact" when fixing it lets at least this many other
# uploads migrate. The +1 maintenance guide asks shifts to look for "high-
# impact issues that would unblock large sets of packages" first.
HIGH_IMPACT_MIN = 2


@dataclass(frozen=True, slots=True)
class HighImpact:
    """One package whose fix would let several other uploads migrate."""

    package: str
    # Its own stuck upload, when it has one.
    upload: StuckRow | None
    # Other uploads its tests regress on.
    regressed: tuple[StuckRow, ...]
    # Other stuck uploads waiting for its upload to migrate first.
    waited_by: tuple[StuckRow, ...]

    @property
    def unblocks(self) -> int:
        return len({r.package for r in (*self.regressed, *self.waited_by)})


@dataclass(frozen=True, slots=True)
class PlusOne:
    """A +1 maintenance shift's queue, in the order the guide gives.

    The guide (documentation.ubuntu.com/project, "+1 Maintenance", read
    9 Oct 2026) lists, loosely by priority: the last shift's report,
    transitions and NBS, FTBFS, update-excuses (high-impact items first),
    and universe merges when the last uploader is not around. cairn has
    data for the last three; the page names the rest and says it does not.
    """

    high_impact: list[HighImpact]
    # Stuck past the point its uploader is presumed on it, split as the
    # guide splits them: builds first, being "isolated and can be worked on
    # without interfering much with other contributors".
    missing_builds: list[StuckRow]
    stuck: list[StuckRow]
    merges: list[Row]


def plus_one(
    rows: Iterable[Row],
    stuck: Iterable[StuckRow],
    blocking: Iterable[BlockingRow],
    waiting: Mapping[str, Iterable[StuckRow]],
    *,
    now: datetime,
) -> PlusOne:
    rows = list(rows)
    stuck = list(stuck)
    free = up_for_grabs(rows, stuck, now=now)
    free_stuck = [r for r in stuck if r.package in free.stuck_names]

    by_name = {r.package: r for r in stuck}
    regressed: dict[str, dict[str, StuckRow]] = {}
    for row in blocking:
        regressed.setdefault(row.test, {})[row.holding.package] = row.holding
    waited: dict[str, dict[str, StuckRow]] = {
        name: {r.package: r for r in found} for name, found in waiting.items()
    }
    impact = [
        HighImpact(
            package=name,
            upload=by_name.get(name),
            regressed=tuple(regressed.get(name, {}).values()),
            waited_by=tuple(waited.get(name, {}).values()),
        )
        for name in sorted(set(regressed) | set(waited))
    ]
    impact = sorted(
        (i for i in impact if i.unblocks >= HIGH_IMPACT_MIN),
        key=lambda i: (-i.unblocks, i.package),
    )
    return PlusOne(
        high_impact=impact,
        missing_builds=[r for r in free_stuck if "missing_build" in r.reasons],
        stuck=[r for r in free_stuck if "missing_build" not in r.reasons],
        # Universe only: the guide sends shifts to Merge-o-Matic's universe
        # list, main being looked after by the teams that own it.
        merges=[r for r in rows if r.component == "universe"],
    )
