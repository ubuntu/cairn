"""What britney did with the uploads it said it would attempt.

An upload that passes every policy is a candidate: britney's excuses say
"Will attempt migration". That is not a promise. Britney then tries each
candidate against the release pocket and keeps it only if it leaves no more
packages uninstallable than before; the rest are "skipped". Its excuses do
not say which, so a candidate can sit in -proposed for months looking ready
(python-pathable, 178 days on 26 Nov 2026). Ubuntu's guide sends people to
update_output.txt for that, which it calls terse; this reads it instead.

Consumed under AGENTS.md section 4's decision-engine exception, as the
excuses are: "skipped" is britney's verdict, written by the same run. The
format is britney's run log (britney2-ubuntu britney.py, iter_packages and
do_all, checked at c38d399):

  Generated on: 2026.11.26 19:57:20 +0000
  Arch order is: amd64, amd64v3, ...
  trying: rust-chic rust-annotate-snippets rust-libcst
  skipped: rust-chic rust-annotate-snippets rust-libcst (4, 1, 223)
      got: 871+0: a-12:a-8:a-5:a-7:i-0:p-6:r-5:s-828
      * s390x: librust-abscissa-core-dev, ...
      - splitting the component into single items and retrying them
  trying: rust-libcst
  skipped: rust-libcst (4, 1, 225)
      got: 65+0: ...
      * s390x: librust-libcst-dev
  accepted: ccache
  ...
  final: -astcenc/amd64v3,ccache,...
  SUCCESS (921/859)

A "* arch:" line after a skip lists the packages in the release pocket that
the attempt made uninstallable, on the first architecture where it broke
any: britney stops there, so other architectures may break too. Items
tried together are retried one by one, and an item may be retried after
others are accepted, so the last result for an item alone is the one that
counts. Every "final:" line names items that migrated in this run; the
lines that print it end in SUCCESS. The same "* arch:" form also follows
"endloop:" and "easy:", where it is a summary, not one item's result.

Measured 26 Nov 2026, 19:53 UTC run: 2.9 MB, text/plain, no compressed
copy published. 796 candidates; 29 migrated, 767 skipped, every one with
its own list. First broken architecture: s390x 745 (a perl transition
under way there), amd64 12, amd64v3 7, armhf 2, riscv64 1. 20,995 broken
binaries in all, median 1 per upload, at most 2,385. The 15:37 run that
day: 211 candidates, 25 migrated, 186 skipped, 172 first on amd64v3.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

PROPOSED_MIGRATION = "https://ubuntu-archive-team.ubuntu.com/proposed-migration"
OUTPUT_URL = f"{PROPOSED_MIGRATION}/update_output.txt"

# Britney writes the excuses, then logs "Generated on" as it starts trying
# candidates: four minutes apart on both runs measured on 26 Nov 2026. Runs
# are two to three hours apart, so an hour tells this run from the next.
SAME_RUN = timedelta(hours=1)

_GENERATED = re.compile(r"^Generated on: (.+)$")
# "recur: [already accepted] <items to try>/0": where a pass of britney's
# loop starts. The first one of the main run says how many items it tried.
_RECUR = re.compile(r"^recur: \[([^\]]*)\]\s+(\d+)/\d+$")
# Uninstallable packages per architecture, "64+0: a-12:a-8:...", one
# letter-count pair per architecture in the "Arch order is" order.
_COUNTS = re.compile(r"^\s*(start|end): \d+\+\d+: (.*)$")
_OLD_LIBRARIES = re.compile(r"^List of old libraries in the target suite \(\d+\):$")
_OLD_LIBRARY = re.compile(r"^ (\S+): (.+)$")
_ARCHES = re.compile(r"^Arch order is: (.+)$")
_BROKEN = re.compile(r"^    \* (\S+): (.*)$")
_EXCEPTION = re.compile(r"^    got exception: (.*)$")


@dataclass(frozen=True, slots=True)
class Breakage:
    """What one attempt to migrate an item made uninstallable."""

    # The first architecture it broke anything on; "" when britney refused
    # the attempt outright (MigrationConstraintException).
    arch: str
    binaries: tuple[str, ...] = ()
    # Items it was tried with, when it was never tried alone.
    together: tuple[str, ...] = ()
    # Britney's exception, verbatim, when it refused the attempt.
    error: str | None = None
    # How many times britney tried it in this run: after accepting other
    # items it goes round again, in case they made room.
    tries: int = 1


@dataclass(frozen=True, slots=True)
class Output:
    generated: datetime | None
    architectures: tuple[str, ...] = ()
    tried: frozenset[str] = frozenset()
    migrated: frozenset[str] = frozenset()
    # The last result for each item that did not migrate.
    breaks: dict[str, Breakage] = field(default_factory=dict)
    # Items britney accepted, in the order it did: uploads ("src"), uploads
    # on one architecture ("src/arch") and removals of binaries left
    # behind ("-bin/arch"). Only those in a final: line migrated.
    accepted: tuple[str, ...] = ()
    # How many items the main run set out to try (its first recur line).
    considered: int | None = None
    # Uninstallable packages in the target suite per architecture, when the
    # run started and when it finished.
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    # Binaries the new uploads no longer build that britney could not
    # remove without breaking something, by architecture.
    old_libraries: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def same_run(self, excuses_generated: datetime | None) -> bool:
        """Whether this log continues the run that wrote those excuses."""
        if self.generated is None or excuses_generated is None:
            return False
        gap = self.generated - excuses_generated
        return timedelta(0) <= gap <= SAME_RUN


def _generated(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y.%m.%d %H:%M:%S %z")
    except ValueError:
        return None


def _counts(text: str, arches: tuple[str, ...]) -> dict[str, int]:
    """ "a-12:a-8:i-0" in the run's architecture order; a count is missing
    from the end when britney stopped at an earlier architecture."""
    found = {}
    for arch, part in zip(arches, text.split(":"), strict=False):
        _, _, number = part.partition("-")
        if number.isdigit():
            found[arch] = int(number)
    return found


def parse(text: str | bytes) -> Output:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    generated: datetime | None = None
    arches: tuple[str, ...] = ()
    tried: set[str] = set()
    migrated: set[str] = set()
    alone: dict[str, Breakage] = {}
    grouped: dict[str, Breakage] = {}
    trying: list[str] = []
    tries: dict[str, int] = {}
    accepted: list[str] = []
    considered: int | None = None
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    old_libraries: dict[str, tuple[str, ...]] = {}
    in_old_libraries = False
    # The items of the skip a "* arch:" or exception line belongs to; only
    # "got:" may come between.
    pending: list[str] = []

    def record(found: Breakage) -> None:
        if len(pending) == 1:
            alone[pending[0]] = Breakage(
                arch=found.arch,
                binaries=found.binaries,
                error=found.error,
                tries=tries.get(pending[0], 1),
            )
            return
        for item in pending:
            grouped[item] = Breakage(
                arch=found.arch,
                binaries=found.binaries,
                together=tuple(i for i in pending if i != item),
                error=found.error,
                tries=tries.get(item, 1),
            )

    for line in text.splitlines():
        if in_old_libraries:
            if m := _OLD_LIBRARY.match(line):
                old_libraries[m.group(1)] = tuple(m.group(2).split())
                continue
            in_old_libraries = False
        if _OLD_LIBRARIES.match(line):
            in_old_libraries = True
            pending = []
        elif considered is None and (m := _RECUR.match(line)):
            considered = int(m.group(2))
        elif m := _COUNTS.match(line):
            counts = _counts(m.group(2), arches)
            if m.group(1) == "start" and not before:
                before = counts
            elif m.group(1) == "end":
                after = counts
            pending = []
        elif line.startswith("accepted: "):
            accepted.extend(line[len("accepted: ") :].split())
            pending = []
        elif generated is None and (m := _GENERATED.match(line)):
            generated = _generated(m.group(1))
        elif not arches and (m := _ARCHES.match(line)):
            arches = tuple(a.strip() for a in m.group(1).split(","))
        elif line.startswith("trying: "):
            trying = line[len("trying: ") :].split()
            tried.update(trying)
            for item in trying:
                tries[item] = tries.get(item, 0) + 1
            pending = []
        elif line.startswith("skipped: "):
            pending = list(trying)
        elif line.startswith("final: "):
            migrated.update(i for i in line[len("final: ") :].split(",") if i)
            pending = []
        elif pending and (m := _BROKEN.match(line)):
            record(
                Breakage(
                    arch=m.group(1),
                    binaries=tuple(b for b in m.group(2).split(", ") if b),
                )
            )
            pending = []
        elif pending and (m := _EXCEPTION.match(line)):
            record(Breakage(arch="", error=m.group(1).strip()))
            pending = []
        elif not line.startswith("    got: "):
            pending = []

    breaks = {
        item: alone.get(item) or grouped[item]
        for item in {*alone, *grouped}
        if item not in migrated
    }
    return Output(
        generated=generated,
        architectures=arches,
        tried=frozenset(tried),
        migrated=frozenset(migrated),
        breaks=breaks,
        accepted=tuple(dict.fromkeys(accepted)),
        considered=considered,
        before=before,
        after=after,
        old_libraries=old_libraries,
    )


def architectures_broken(output: Output, items: Iterable[str]) -> set[str]:
    """The architectures whose indexes say which source each broken
    binary comes from, for these items."""
    return {
        found.arch
        for item in items
        if (found := output.breaks.get(item)) is not None and found.arch
    }
