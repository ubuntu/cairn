"""Turning the log into a static site.

Reads the replayed log, not SQLite. The merges and migration pages are the
first to join two sources, but the join is a lookup by source package name,
which a dict answers. AGENTS.md section 3 names the trigger for SQLite: a page
that needs a join a dict cannot do cheaply, realistically package -> package
sets -> teams for a per-developer view. The seam is render(state, health) —
swapping the provider later touches this module only.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from debian.debian_support import version_compare
from jinja2 import Environment, FileSystemLoader, select_autoescape

from cairn.build.nbs import NbsOverview, NbsRow, NeededBy, nbs_rows, needs_nbs
from cairn.build.trends import Trends, trends
from cairn.build.work import (
    ABANDONED_DAYS,
    HIGH_IMPACT_MIN,
    UpForGrabs,
    plus_one,
    up_for_grabs,
)
from cairn.core.health import SourceHealth
from cairn.core.log import Event, EventType, SignalState
from cairn.core.rules import severity
from cairn.ingest.base import DEVELOPMENT_KINDS, Kind
from cairn.ingest.merges import has_ubuntu_delta, upstream_release
from cairn.ingest.metadata import PackageMetadata, Snapshot
from cairn.ingest.migration import FAILED, failing_tests, payload_tests

TEMPLATES = Path(__file__).parent / "templates"
ASSETS = Path(__file__).parent / "assets"

# Everything build() writes, and therefore everything it may remove.
# uploaders/, sets/ and teams/ at the root are where merges owner pages lived
# before each board got its own directory; listed so a rebuild clears them.
GENERATED = (
    "index.html",
    "merges",
    "migration",
    "nbs",
    "holding",
    # Where the +1 maintenance page lived before it became a tab of
    # Up for grabs; listed so a rebuild clears it.
    "plus-one",
    "packages",
    "owners",
    "work",
    "uploaders",
    "sets",
    "teams",
    "assets",
)

# Dropped into the output directory so a rebuild can tell a site it made from
# a directory that merely happens to be called site/. Without it, --out
# pointing somewhere unintended would delete real work.
MARKER = ".cairn-site"

VANILLA_CSS = "https://assets.ubuntu.com/v1/vanilla_framework_version_4.59.0.min.css"

# Debian's package tracker. A pure function of the source name, so it is
# derived here rather than stored on every event in the log.
DEBIAN_TRACKER = "https://tracker.debian.org/pkg/{source}"

# Circle of Friends, white, for the dark navigation bar.
UBUNTU_LOGO = "https://assets.ubuntu.com/v1/82818827-CoF_white.svg"

RECENT = timedelta(days=7)

# An upload this young, or one only waiting for its tests to finish, is
# probably still being looked after by whoever uploaded it. The +1
# maintenance docs say as much, and britney's by-team report holds such items
# back as "not yet considered late". Shown folded, never hidden.
SETTLING_DAYS = 3

# The order reasons are listed in: what a person can act on first.
REASON_ORDER = (
    "regression",
    "missing_build",
    "uninstallable",
    # A candidate britney tried and skipped (cairn.ingest.update_output).
    "breaks_installs",
    "no_binaries",
    "old_binaries",
    "waiting",
    "needs_approval",
    "block_bug",
    "rc_bug",
    "tests_running",
    # A candidate whose attempt cairn cannot see.
    "will_attempt",
    "other",
)

# Syncs have no Ubuntu uploader to route to. Ubuntu's proposed migration
# docs give those to +1 maintenance, so they are grouped there rather than
# under a Debian developer who will never read this page. A sync someone
# asked for is listed under them as well (issue #14), not instead: on
# 20 Nov 2026, 534 of the 621 requested syncs stuck were one developer's
# bulk syncs early in the cycle, which routing to them alone would have
# taken away from +1 maintenance.
PLUS_ONE = "plus-one-maintenance"

# Not an owner: every row of a board, listed on the board's own front page
# (merges/index.html, migration/index.html) with every ownership column
# shown. Until Oct 2026 it had a page of its own, all.html, behind a small
# link that people did not find; that address now redirects.
EVERYTHING = "all"
NO_UPLOADER = "no-uploader-recorded"

LAUNCHPAD_SOURCE = "https://launchpad.net/ubuntu/+source/{source}"
AUTOPKGTEST_PACKAGE = "https://autopkgtest.ubuntu.com/packages/{prefix}/{source}"
AUTOPKGTEST_REQUEST = "https://autopkgtest.ubuntu.com/request.cgi"

# Days Debian has been ahead, from Debian's own upload record.
AGE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("under a month", 30),
    ("1-6 months", 182),
    ("6-12 months", 365),
    ("1-2 years", 730),
    ("over 2 years", None),
)


@dataclass(frozen=True, slots=True)
class Row:
    package: str
    component: str | None
    ubuntu_version: str
    debian_version: str
    base_version: str
    new_upstream: bool
    # ubuntu_version is the -proposed upload, not yet migrated to release.
    in_proposed: bool
    uploader: str | None
    published: datetime | None
    debian_uploaded: datetime | None
    package_sets: tuple[str, ...]
    teams: tuple[str, ...]
    severity: str
    first_seen: datetime
    tracked_days: int
    occurrences: int
    url: str | None

    @property
    def returning(self) -> bool:
        return self.occurrences > 1

    @property
    def upstream(self) -> str:
        """How Debian's upstream release compares with Ubuntu's: "new",
        "same" (Debian is ahead by packaging only) or "older".

        "older" is real: Debian's version can sort higher through an epoch
        bump or a +really downgrade while carrying an older upstream
        (measured 12 Oct 2026: compiz, epoch 1 to 2 over 0.9.14.2 to
        0.8.18; procmail; llama.cpp). Lumping those with the packaging-only
        merges called them small jobs, which they are not.

        Derived here from the versions, with the ingester's own
        upstream_release() and Debian ordering, rather than stored: a new
        payload field would append an UPDATED event to every signal.
        """
        order = version_compare(
            upstream_release(self.debian_version), upstream_release(self.ubuntu_version)
        )
        return "new" if order > 0 else "same" if order == 0 else "older"

    @property
    def anchor(self) -> str:
        # The package name itself: each board has its own pages, so the URL
        # already says which board (merges/... or migration/...). Debian
        # names are [a-z0-9][a-z0-9+.-]*, unique and safe in a fragment.
        return self.package

    @property
    def page(self) -> str:
        """The one merges page this row is certain to be on: its uploader's.
        Relative to the site root."""
        return f"merges/uploaders/{_slug(self.uploader or NO_UPLOADER)}.html"

    @property
    def debian_url(self) -> str:
        return DEBIAN_TRACKER.format(source=self.package)

    # Launchpad keeps a page per version. Only ':' is quoted, matching the
    # links Launchpad generates itself.
    @property
    def ubuntu_version_url(self) -> str:
        return _version_url(self.package, self.ubuntu_version)

    def behind_days(self, now: datetime) -> int | None:
        """How long Debian has been ahead.

        Measured from Debian's own upload, which is the question a merge board
        is actually asking and, unlike Launchpad's publication dates, does not
        reset when Ubuntu opens a series and copies the archive forward.
        """
        if self.debian_uploaded is None:
            return None
        return max((now - self.debian_uploaded).days, 0)

    def published_days(self, now: datetime) -> int | None:
        """Days since this version was published *into this series*.

        Not an upload date. Launchpad creates a publication record per series,
        so a package copied forward when the archive opens is dated then even
        if it was uploaded years earlier: a third of the archive shares one
        date. Useful as a rough signal, useless as an ordering.
        """
        return None if self.published is None else max((now - self.published).days, 0)


def _version_url(package: str, version: str) -> str:
    """Launchpad keeps a page per version. Only ':' is quoted, matching the
    links Launchpad generates itself."""
    return f"{LAUNCHPAD_SOURCE.format(source=package)}/{quote(version, safe='+~')}"


def autopkgtest_prefix(source: str) -> str:
    """The archive's pool layout: lib packages are filed under four letters."""
    return source[:4] if source.startswith("lib") and len(source) > 3 else source[:1]


def autopkgtest_url(
    source: str, series: str | None = None, arch: str | None = None
) -> str:
    """A test's history: every series and architecture, or one series and
    architecture. autopkgtest.ubuntu.com has no page for a series alone:
    /packages/c/curl/<series> reads as package c, release curl (review on
    #24)."""
    url = AUTOPKGTEST_PACKAGE.format(prefix=autopkgtest_prefix(source), source=source)
    if series and arch:
        url += f"/{series}/{arch}"
    return url


AUTOPKGTEST_LOG = (
    "https://autopkgtest.ubuntu.com/results/autopkgtest-{series}/{series}/{arch}/"
    "{prefix}/{source}/{run}/log.gz"
)


def autopkgtest_log_url(source: str, series: str, arch: str, run: str) -> str:
    """One run's log, the link britney puts on an architecture. Built from
    the run id ingest keeps (see migration._RUN_ID); measured to match
    britney's own link for all 11,830 failed results on 20 Nov 2026."""
    return AUTOPKGTEST_LOG.format(
        series=series,
        arch=arch,
        prefix=autopkgtest_prefix(source),
        source=source,
        run=run,
    )


def retry_url(test: str, arch: str, series: str, source: str, version: str) -> str:
    """Re-run one test against the upload it regressed on.

    The same request britney's excuses page links as its ♻ next to every
    regression: one trigger, the held upload. autopkgtest.ubuntu.com asks the
    person to log in and checks their upload rights, so this is a link a
    developer chooses to follow, not something cairn does. cairn stays
    read-only.
    """
    query = urlencode(
        {
            "release": series,
            "arch": arch,
            "package": test,
            "trigger": f"{source}/{version}",
        },
        quote_via=quote,
        safe="",
    )
    return f"{AUTOPKGTEST_REQUEST}?{query}"


@dataclass(frozen=True, slots=True)
class TestResult:
    arch: str
    # See migration.TEST_RESULTS: regression, reference_running or running.
    status: str
    # The finished run's id, for its log. None while a test runs, and for
    # results recorded before ingest kept it.
    run: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in FAILED


@dataclass(frozen=True, slots=True)
class HeldTest:
    """One test package holding an upload, with its result per architecture.

    Usually a reverse dependency's test, triggered by the upload.
    """

    test: str
    version: str
    results: tuple[TestResult, ...]
    # Known to fail, from the log, with no results to say where: the
    # migration detail was missing or described another upload.
    failing: bool = False

    def only(self, failed: bool) -> HeldTest | None:
        if not self.results:
            return self if self.failing is failed else None
        kept = tuple(r for r in self.results if r.failed is failed)
        return HeldTest(self.test, self.version, kept) if kept else None

    def by_status(self) -> list[tuple[str, tuple[TestResult, ...]]]:
        """Architectures grouped under one tag per status, failures first.

        One tag per group rather than per architecture: a test still running
        on four architectures reads "amd64, amd64v3, arm64, ppc64el" and one
        "Test in progress", not the same words four times over.
        """
        order = ("regression", "reference_running", "running")
        if not self.results:
            return [("regression", ())] if self.failing else []
        groups: dict[str, list[TestResult]] = {}
        for result in self.results:
            groups.setdefault(result.status, []).append(result)
        return sorted(
            ((status, tuple(found)) for status, found in groups.items()),
            key=lambda g: order.index(g[0]) if g[0] in order else len(order),
        )


def _detail_agrees(payload: Mapping[str, Any], detail: Mapping[str, Any]) -> bool:
    if detail.get("new_version") != payload.get("new_version"):
        return False
    if "failing_tests" not in payload:
        # Written before the split: nothing in the log to disagree with.
        return True
    return set(payload.get("failing_tests") or ()) == set(
        failing_tests(detail.get("tests") or ())
    )


def _held_tests(
    payload: Mapping[str, Any], detail: Mapping[str, Any] | None = None
) -> tuple[HeldTest, ...]:
    """The tests holding an upload, from the most detailed source there is.

    The migration detail has every result per architecture, but only for
    the upload it was taken from. Older log lines carried the results
    themselves. Failing that, the log's names say which tests fail and not
    where, which is still worth showing.

    The log wins a disagreement (review on #28): a run that appended its
    events and then failed to save its detail leaves an older detail for
    the same upload, which could hide a new failure or bring back a cleared
    one. So the detail is used only when it fails the tests the log says.
    """
    if detail is not None and _detail_agrees(payload, detail):
        records = list(detail.get("tests") or ())
    elif "tests" in payload or "regressions" in payload:
        records = payload_tests(payload)
    else:
        return tuple(
            HeldTest(test=str(name), version="", results=(), failing=True)
            for name in payload.get("failing_tests") or ()
        )
    return tuple(
        HeldTest(
            test=t.get("test", ""),
            version=t.get("version", ""),
            results=tuple(
                TestResult(
                    arch=arch, status=status, run=(t.get("logs") or {}).get(arch)
                )
                for arch, status in (t.get("results") or {}).items()
            ),
        )
        for t in records
    )


@dataclass(frozen=True, slots=True)
class Attempt:
    """Britney tried to migrate an upload and backed out: what it would have
    left uninstallable in the release pocket. From the migration detail
    (cairn.ingest.update_output), so only for the upload it describes."""

    # The first architecture with a problem; britney checks no further, so
    # others may break too. "" when britney refused the attempt outright.
    arch: str
    # (source, its binaries), in source order; source None when no index
    # named one.
    broken: tuple[tuple[str | None, tuple[str, ...]], ...] = ()
    # Items it was only ever tried together with.
    together: tuple[str, ...] = ()
    error: str | None = None
    # How many times britney tried it in the run.
    tries: int = 1
    # Britney's item: "src", "src/arch" or "-bin/arch".
    item: str = ""

    @property
    def binaries(self) -> int:
        return sum(len(names) for _, names in self.broken)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(source for source, _ in self.broken if source)

    def others(self, package: str) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
        """What it would break besides the upload's own binaries."""
        return tuple(g for g in self.broken if g[0] != package)

    def own(self, package: str) -> tuple[str, ...]:
        """Its own binaries that would not install: a rebuild that does not
        fit what is already in the release (the s390x perl rebuilds)."""
        return tuple(
            b for source, names in self.broken if source == package for b in names
        )


def _attempt(record: Mapping[str, Any]) -> Attempt:
    return Attempt(
        arch=str(record.get("arch") or ""),
        broken=tuple(
            (g.get("source"), tuple(g.get("binaries") or ()))
            for g in record.get("broken") or ()
        ),
        together=tuple(record.get("together") or ()),
        error=record.get("error"),
        tries=int(record.get("tries") or 1),
        item=str(record.get("item") or ""),
    )


def _attempts(
    payload: Mapping[str, Any], detail: Mapping[str, Any] | None
) -> tuple[Attempt, ...]:
    if detail is None or detail.get("new_version") != payload.get("new_version"):
        return ()
    return tuple(_attempt(b) for b in detail.get("breaks") or ())


@dataclass(frozen=True, slots=True)
class StuckRow:
    """One upload britney will not migrate, with what cairn knows of it."""

    package: str
    component: str | None
    old_version: str
    new_version: str
    reasons: tuple[str, ...]
    tests: tuple[HeldTest, ...]
    missing_builds: tuple[str, ...]
    waits_for: tuple[str, ...]
    # update-excuse bugs: someone filed a bug about this upload. They hold
    # nothing; block_bugs (tag block-proposed) do.
    bugs: tuple[int, ...]
    hints: tuple[str, ...]
    since: date | None
    # The changelog person. For a sync, the Debian uploader: shown, never
    # routed to.
    uploader: str | None
    # No Ubuntu signer: a sync. Routed to +1 maintenance, and to its
    # requester as well when someone asked for it.
    synced: bool
    package_sets: tuple[str, ...]
    teams: tuple[str, ...]
    first_seen: datetime
    occurrences: int
    # Distinct uploads cairn has seen stuck under this one signal. Britney
    # only ever knows about the current one.
    uploads: int
    url: str | None
    # Bugs tagged block-proposed, which britney will not migrate past.
    block_bugs: tuple[int, ...] = ()
    # Why it is uninstallable: (kind, britney's line without its
    # architecture, architectures). Kinds are "component", "unsatisfiable"
    # and "impossible".
    depends_issues: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    # Old binaries still in -proposed, as (from version, binaries).
    old_binaries: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Set when britney holds it only on these architectures (a rebuild on
    # some, not the whole upload).
    architectures: tuple[str, ...] = ()
    # The series britney tested this upload in, which test log URLs name.
    series: str = ""
    # A sync: who asked for it, if a person did, and whether the auto-sync
    # copied it. See publications.requester().
    requester: str | None = None
    auto_synced: bool = False
    # Britney tried it and backed out (reason breaks_installs).
    attempts: tuple[Attempt, ...] = ()

    @property
    def broken_sources(self) -> tuple[str, ...]:
        """Every source an attempt would break, each once, in order."""
        return tuple(dict.fromkeys(s for a in self.attempts for s in a.sources))

    @property
    def owner(self) -> str:
        """The uploader page this row is certain to be on. A requested sync
        is on +1 maintenance's too: see owners."""
        if self.synced:
            return self.requester or PLUS_ONE
        return self.uploader or ""

    @property
    def owners(self) -> list[str]:
        """Every uploader page this row is on."""
        return [self.owner, PLUS_ONE] if self.requester else [self.owner]

    @property
    def origin(self) -> str:
        """Who made the upload: "ubuntu" (an Ubuntu uploader is known),
        "requested" (a sync someone asked for), "sync" (a sync no one did:
        the auto-sync's, or a copy Launchpad names no one for), or "unknown"
        (Launchpad returned no publication for this version). Not synced is
        not the same as an Ubuntu upload: an upload Launchpad does not
        describe is neither."""
        if self.synced:
            return "requested" if self.requester else "sync"
        return "ubuntu" if self.uploader else "unknown"

    @property
    def anchor(self) -> str:
        # The package name itself: each board has its own pages, so the URL
        # already says which board (merges/... or migration/...). Debian
        # names are [a-z0-9][a-z0-9+.-]*, unique and safe in a fragment.
        return self.package

    @property
    def failing_tests(self) -> tuple[HeldTest, ...]:
        return tuple(t for x in self.tests if (t := x.only(failed=True)))

    @property
    def running_tests(self) -> tuple[HeldTest, ...]:
        return tuple(t for x in self.tests if (t := x.only(failed=False)))

    @property
    def page(self) -> str:
        """The one stuck-in-proposed page this row is certain to be on: its
        owner's. Relative to the site root."""
        return f"migration/uploaders/{_slug(self.owner or NO_UPLOADER)}.html"

    @property
    def new_version_url(self) -> str:
        return _version_url(self.package, self.new_version)

    @property
    def old_version_url(self) -> str | None:
        """The version in release, which this upload would replace."""
        return (
            None
            if self.is_new_package
            else _version_url(self.package, self.old_version)
        )

    @property
    def is_new_package(self) -> bool:
        return self.old_version in ("", "-")

    def missing_build_url(self, arch: str) -> str:
        return f"{self.new_version_url}/+latestbuild/{arch}"

    def days(self, now: datetime) -> int | None:
        if self.since is None:
            return None
        return max((now.date() - self.since).days, 0)

    def tracked_days(self, now: datetime) -> int:
        return max((now - self.first_seen).days, 0)

    def settling(self, now: datetime) -> bool:
        """Young enough that its uploader is probably still on it.

        Age only. "Only waiting for tests" looked like a second signal, but
        measured on 1 Oct 2026, 31 of the 34 such uploads were under three
        days old anyway, and the rest had been stuck up to 83 days: tests
        re-running on an old upload is not a reason to fold it away.
        """
        days = self.days(now)
        return days is not None and days < SETTLING_DAYS


@dataclass(frozen=True, slots=True)
class BlockingRow:
    """A package whose own tests regress against someone else's upload.

    The by-team report calls this "regressing other". Its owners are the
    people who can fix the test; the upload's owners usually cannot.
    """

    test: str
    version: str
    results: tuple[TestResult, ...]
    holding: StuckRow
    package_sets: tuple[str, ...]
    teams: tuple[str, ...]

    def by_status(self) -> list[tuple[str, tuple[TestResult, ...]]]:
        # Every blocking row is a failure. Without results (no migration
        # detail for this upload) the status is still "regression", not
        # blank (review on #28).
        return HeldTest(self.test, self.version, self.results, failing=True).by_status()


@dataclass(frozen=True, slots=True)
class Group:
    """One owner — a team, a package set or an uploader — and their work.

    Each kind of work gets its own page: merges at href, stuck uploads at
    migration_href, and what the owner is holding back at holding_href. One
    page holding everything grew as long as britney's own excuses page,
    which is the problem the board exists to fix. The pages carry tabs to
    each other, so an owner is never more than one click from the rest of
    their work.
    """

    kind: str
    name: str
    subtitle: str
    rows: list[Row] = field(default_factory=list)
    stuck: list[StuckRow] = field(default_factory=list)
    blocking: list[BlockingRow] = field(default_factory=list)
    note: str = ""
    label: str | None = None
    # (waiting upload, what it waits for): this owner's stuck upload, or the
    # awaited package's name when it has no stuck upload of its own. Filled
    # in _context once every stuck row is known.
    waiting: list[tuple[StuckRow, StuckRow | str]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.label or self.name

    @property
    def slug(self) -> str:
        return _slug(self.name)

    @property
    def is_everything(self) -> bool:
        return self.kind == EVERYTHING

    @property
    def href(self) -> str:
        """The owner's merges page, relative to the site root. Under merges/,
        like the stuck page under migration/: neither board is the default."""
        if self.is_everything:
            return "merges/index.html"
        return f"merges/{self.kind}/{self.slug}.html"

    @property
    def migration_href(self) -> str:
        """The owner's stuck-in-proposed page, relative to the site root."""
        if self.is_everything:
            return "migration/index.html"
        return f"migration/{self.kind}/{self.slug}.html"

    @property
    def holding_href(self) -> str:
        """What the owner is holding back: their tests regressing other
        uploads, and stuck uploads waiting for theirs. Its own page because
        it answers a different question from "what of mine is stuck", and
        below a long stuck table nobody scrolled to it."""
        return f"holding/{self.kind}/{self.slug}.html"

    @property
    def has_merges(self) -> bool:
        return bool(self.rows)

    @property
    def has_migration(self) -> bool:
        return bool(self.stuck)

    @property
    def has_holding(self) -> bool:
        return not self.is_everything and bool(self.blocking or self.waiting)

    @property
    def holding_count(self) -> int:
        """Other uploads this owner holds back, each counted once.

        Not the number of rows: an upload can be regressed by two of the
        owner's tests, wait for two of their uploads, or both, and is still
        one upload held back. Same rule as PackageView.holding_count.
        """
        return len(
            {b.holding.package for b in self.blocking}
            | {row.package for row, _ in self.waiting}
        )

    @property
    def waiting_count(self) -> int:
        """Uploads waiting for one of this owner's, each counted once."""
        return len({row.package for row, _ in self.waiting})

    @property
    def overview_href(self) -> str:
        """The owner's overview: one block per kind of work, like a package
        page, with tabs to the full lists."""
        return f"owners/{self.kind}/{self.slug}.html"

    @property
    def primary_href(self) -> str:
        """Where "find your page" lands: the overview, which every owner
        has and which links to the rest."""
        return self.overview_href

    @property
    def reason_counts(self) -> list[tuple[str, int]]:
        """Why this owner's uploads are stuck, most actionable reason
        first, as on the board."""
        counts: dict[str, int] = {}
        for row in self.stuck:
            for reason in row.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return sorted(counts.items(), key=lambda kv: _reason_key(kv[0]))

    @property
    def blocking_tests(self) -> list[str]:
        """This owner's packages whose tests hold others back, once each."""
        return sorted({b.test for b in self.blocking})

    @property
    def count(self) -> int:
        """Merge candidates. The merges index counts these."""
        return len(self.rows)

    @property
    def new_upstream(self) -> int:
        return sum(1 for r in self.rows if r.new_upstream)

    @property
    def stuck_count(self) -> int:
        return len(self.stuck)

    @property
    def blocking_count(self) -> int:
        return len(self.blocking)

    @property
    def size(self) -> int:
        return self.count + self.stuck_count + self.blocking_count

    @property
    def merge_names(self) -> set[str]:
        return {r.package for r in self.rows}

    @property
    def stuck_names(self) -> set[str]:
        return {r.package for r in self.stuck}

    def settling(self, now: datetime) -> list[StuckRow]:
        return [r for r in self.stuck if r.settling(now)]

    def longest_wait(self, now: datetime) -> int | None:
        ages = [r.behind_days(now) for r in self.rows]
        known = [a for a in ages if a is not None]
        return max(known) if known else None


@dataclass(frozen=True, slots=True)
class Bucket:
    label: str
    count: int
    share: float = 0.0


@dataclass(frozen=True, slots=True)
class Overview:
    total: int = 0
    new_upstream: int = 0
    # Same upstream on both sides: Debian is ahead by packaging only.
    revision_only: int = 0
    # Debian's version sorts higher, its upstream is older.
    older_upstream: int = 0
    components: dict[str, int] = field(default_factory=dict)
    opened_recently: int = 0
    resolved_recently: int = 0
    returning: int = 0
    unknown_uploader: int = 0
    undated: int = 0
    collected: datetime | None = None
    buckets: list[Bucket] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MigrationOverview:
    total: int = 0
    settling: int = 0
    # (reason, uploads held by it), in REASON_ORDER, absent reasons left out.
    reasons: list[tuple[str, int]] = field(default_factory=list)
    frozen: int = 0
    synced: int = 0
    opened_recently: int = 0
    resolved_recently: int = 0
    returning: int = 0
    reuploaded: int = 0
    blocking: int = 0
    # When cairn first saw this kind. Until a full week has passed, "this
    # week" would count the whole board as new, so the page says so instead.
    tracked_since: datetime | None = None

    def has_a_week(self, now: datetime) -> bool:
        return self.tracked_since is not None and now - self.tracked_since >= RECENT


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c == "-" else "-" for c in value.lower())


def merge_rows(
    state: Mapping[str, SignalState],
    metadata: Snapshot | None = None,
    *,
    now: datetime,
) -> list[Row]:
    """Longest-waiting first.

    Rows with no Debian upload date sort last rather than first: absent data
    must not look like the most urgent thing on the page.
    """
    metadata = metadata or Snapshot()
    rows = []
    for st in state.values():
        if not st.is_active or st.signal.kind is not Kind.NEEDS_MERGE:
            continue
        payload = st.signal.payload
        owner = metadata.get(st.signal.source_package)
        published = owner.published
        debian_uploaded = owner.debian_uploaded
        ubuntu_version = payload.get("ubuntu_version", "")
        rows.append(
            Row(
                package=st.signal.source_package,
                component=payload.get("component"),
                ubuntu_version=ubuntu_version,
                debian_version=payload.get("debian_version", ""),
                base_version=payload.get("base_version", ""),
                new_upstream=bool(payload.get("new_upstream")),
                in_proposed=bool(payload.get("in_proposed")),
                # The uploader of the version on the row, never borrowed
                # from another version. See PackageMetadata.uploader_of.
                uploader=owner.uploader_of(ubuntu_version),
                published=datetime.fromisoformat(published) if published else None,
                debian_uploaded=(
                    datetime.fromisoformat(debian_uploaded) if debian_uploaded else None
                ),
                package_sets=owner.package_sets,
                teams=owner.teams,
                severity=str(severity(st.signal)),
                first_seen=st.first_seen,
                tracked_days=max((now - st.first_seen).days, 0),
                occurrences=st.occurrences,
                url=st.signal.url,
            )
        )
    # None sorts to 1, real ages to their negation, so the longest waits
    # lead and the unknowns trail.
    rows.sort(key=lambda r: (-(r.behind_days(now) or -1), r.package))
    return rows


def uploads_seen(events: Iterable[Event]) -> dict[str, int]:
    """How many distinct uploads each migration signal is stuck through now.

    Only the log can answer this: britney describes the current upload and
    forgets the last one the moment it is superseded.

    Counted within the current blocking episode. An opened event, first or
    reopened, starts the count again and a resolved one ends it, so a
    package that migrated and later got stuck on a new upload reads as
    "seen 2 times", not as one upload stuck through two.
    """
    versions: dict[str, set[str]] = {}
    for event in events:
        if event.kind is not Kind.MIGRATION_BLOCKED:
            continue
        sid = event.to_signal().signal_id
        if event.event is EventType.RESOLVED:
            versions.pop(sid, None)
            continue
        if event.event is EventType.OPENED:
            versions[sid] = set()
        version = event.payload.get("new_version")
        if version and sid in versions:
            versions[sid].add(version)
    return {sid: len(seen) for sid, seen in versions.items() if seen}


@dataclass(frozen=True, slots=True)
class History:
    """What the event stream says that replayed end state cannot.

    Replay keeps a signal's original first_seen when it reopens and clears
    resolved_at, which is right for the row but wrong for "this week": a
    package that migrated and got stuck again within the week would count
    as neither. These figures are read from the events themselves.
    """

    uploads: Mapping[str, int] = field(default_factory=dict)
    # Per kind, (signal_id, when) for every opened or resolved event.
    opened: Mapping[Kind, list[tuple[str, datetime]]] = field(default_factory=dict)
    resolved: Mapping[Kind, list[tuple[str, datetime]]] = field(default_factory=dict)
    # Per kind, (source package, when) for every opened event: what the
    # "new this week" filter selects rows by.
    opened_packages: Mapping[Kind, list[tuple[str, datetime]]] = field(
        default_factory=dict
    )
    # Every event, per source package, in log order: a package page's
    # timeline. Empty when built from replayed state alone.
    by_package: Mapping[str, tuple[Event, ...]] = field(default_factory=dict)
    trends: Trends = field(default_factory=Trends)

    @staticmethod
    def _since(found: Iterable[tuple[str, datetime]], cutoff: datetime) -> int:
        return len({sid for sid, when in found if when >= cutoff})

    def opened_since(self, kind: Kind, cutoff: datetime) -> int:
        """Signals that opened, or reopened, at or after the cutoff."""
        return self._since(self.opened.get(kind, ()), cutoff)

    def resolved_since(self, kind: Kind, cutoff: datetime) -> int:
        return self._since(self.resolved.get(kind, ()), cutoff)

    def opened_packages_since(self, kind: Kind, cutoff: datetime) -> set[str]:
        return {
            name for name, when in self.opened_packages.get(kind, ()) if when >= cutoff
        }

    @classmethod
    def of(cls, events: Iterable[Event]) -> History:
        events = list(events)
        opened: dict[Kind, list[tuple[str, datetime]]] = {}
        resolved: dict[Kind, list[tuple[str, datetime]]] = {}
        opened_packages: dict[Kind, list[tuple[str, datetime]]] = {}
        by_package: dict[str, list[Event]] = {}
        for event in events:
            by_package.setdefault(event.source_package, []).append(event)
            if event.event is EventType.OPENED:
                opened_packages.setdefault(event.kind, []).append(
                    (event.source_package, event.ts)
                )
            target = {EventType.OPENED: opened, EventType.RESOLVED: resolved}.get(
                event.event
            )
            if target is not None:
                target.setdefault(event.kind, []).append(
                    (event.to_signal().signal_id, event.ts)
                )
        return cls(
            uploads=uploads_seen(events),
            opened=opened,
            resolved=resolved,
            opened_packages=opened_packages,
            by_package={name: tuple(found) for name, found in by_package.items()},
            trends=trends(events),
        )

    @classmethod
    def approximate(cls, state: Mapping[str, SignalState]) -> History:
        """For callers with no events: one opening at first_seen, one
        resolution at resolved_at. Misses reopenings, as replay does."""
        opened: dict[Kind, list[tuple[str, datetime]]] = {}
        resolved: dict[Kind, list[tuple[str, datetime]]] = {}
        opened_packages: dict[Kind, list[tuple[str, datetime]]] = {}
        for sid, st in state.items():
            opened.setdefault(st.signal.kind, []).append((sid, st.first_seen))
            opened_packages.setdefault(st.signal.kind, []).append(
                (st.signal.source_package, st.first_seen)
            )
            if st.resolved_at is not None:
                resolved.setdefault(st.signal.kind, []).append((sid, st.resolved_at))
        return cls(opened=opened, resolved=resolved, opened_packages=opened_packages)


def _since(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _reason_key(reason: str) -> int:
    return REASON_ORDER.index(reason) if reason in REASON_ORDER else len(REASON_ORDER)


def stuck_rows(
    state: Mapping[str, SignalState],
    metadata: Snapshot | None = None,
    *,
    now: datetime,
    uploads: Mapping[str, int] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> list[StuckRow]:
    """Longest in -proposed first; undated rows trail, as on the merges board.

    `detail` is the migration source's saved detail (cairn.core.detail).
    """
    metadata = metadata or Snapshot()
    uploads = uploads or {}
    detailed = (detail or {}).get("packages") or {}
    rows = []
    for sid, st in state.items():
        if not st.is_active or st.signal.kind is not Kind.MIGRATION_BLOCKED:
            continue
        payload = st.signal.payload
        name = st.signal.source_package
        owner = metadata.get(name)
        new_version = payload.get("new_version") or ""
        shown = owner.publication(new_version)
        rows.append(
            StuckRow(
                package=name,
                component=payload.get("component"),
                old_version=payload.get("old_version") or "",
                new_version=new_version,
                reasons=tuple(sorted(payload.get("reasons") or (), key=_reason_key)),
                tests=_held_tests(payload, detailed.get(name)),
                attempts=_attempts(payload, detailed.get(name)),
                missing_builds=tuple(payload.get("missing_builds") or ()),
                waits_for=tuple(payload.get("waits_for") or ()),
                bugs=tuple(payload.get("bugs") or ()),
                hints=tuple(payload.get("hints") or ()),
                since=_since(payload.get("in_proposed_since")),
                uploader=owner.uploader_of(new_version),
                synced=bool(shown and shown.synced),
                requester=shown.requester if shown and shown.synced else None,
                auto_synced=bool(shown and shown.synced and shown.auto_synced),
                package_sets=owner.package_sets,
                teams=owner.teams,
                first_seen=st.first_seen,
                occurrences=st.occurrences,
                uploads=max(uploads.get(sid, 1), 1),
                # Derived, not read from the log: the package's Launchpad
                # page, whatever an older event stored.
                url=LAUNCHPAD_SOURCE.format(source=name),
                block_bugs=tuple(payload.get("block_bugs") or ()),
                depends_issues=tuple(
                    (
                        str(d.get("kind", "")),
                        str(d.get("text", "")),
                        tuple(d.get("arches") or ()),
                    )
                    for d in payload.get("depends_issues") or ()
                ),
                old_binaries=tuple(
                    (str(version), tuple(names))
                    for version, names in (payload.get("old_binaries") or {}).items()
                ),
                architectures=tuple(payload.get("architectures") or ()),
                series=st.signal.series or "",
            )
        )
    rows.sort(
        key=lambda r: (-(r.days(now) if r.days(now) is not None else -1), r.package)
    )
    return rows


def blocking_rows(
    stuck: Iterable[StuckRow], metadata: Snapshot | None = None
) -> list[BlockingRow]:
    """Turn "this upload regresses those tests" around, so the tests' owners
    see it on their own page. A package's own tests failing on its own
    upload is the uploader's problem and is not repeated here."""
    metadata = metadata or Snapshot()
    rows = []
    for row in stuck:
        for test in row.failing_tests:
            if test.test == row.package:
                continue
            failed = test
            owner: PackageMetadata = metadata.get(failed.test)
            rows.append(
                BlockingRow(
                    test=failed.test,
                    version=failed.version,
                    results=failed.results,
                    holding=row,
                    package_sets=owner.package_sets,
                    teams=owner.teams,
                )
            )
    rows.sort(key=lambda r: (r.test, r.holding.package))
    return rows


def age_buckets(rows: list[Row], *, now: datetime) -> list[Bucket]:
    counts = dict.fromkeys((label for label, _ in AGE_BUCKETS), 0)
    for row in rows:
        days = row.behind_days(now)
        if days is None:
            continue
        for label, ceiling in AGE_BUCKETS:
            if ceiling is None or days < ceiling:
                counts[label] += 1
                break
    widest = max(counts.values(), default=0)
    if not widest:
        return []
    return [
        Bucket(label, count, count / widest * 100) for label, count in counts.items()
    ]


def overview(
    state: Mapping[str, SignalState],
    rows: list[Row],
    health: Mapping[str, SourceHealth],
    *,
    now: datetime,
    history: History | None = None,
) -> Overview:
    cutoff = now - RECENT
    history = history or History.approximate(state)
    components: dict[str, int] = {}
    for row in rows:
        key = row.component or "unknown"
        components[key] = components.get(key, 0) + 1
    return Overview(
        total=len(rows),
        new_upstream=sum(1 for r in rows if r.new_upstream),
        revision_only=sum(1 for r in rows if r.upstream == "same"),
        older_upstream=sum(1 for r in rows if r.upstream == "older"),
        components=dict(sorted(components.items())),
        # Restricted to this board's kind: once a second ingester lands,
        # unrelated NBS or SRU events would otherwise inflate these.
        opened_recently=history.opened_since(Kind.NEEDS_MERGE, cutoff),
        resolved_recently=history.resolved_since(Kind.NEEDS_MERGE, cutoff),
        returning=sum(1 for r in rows if r.returning),
        unknown_uploader=sum(1 for r in rows if not r.uploader),
        undated=sum(1 for r in rows if r.debian_uploaded is None),
        buckets=age_buckets(rows, now=now),
        collected=max(
            (h.last_success for h in health.values() if h.last_success), default=None
        ),
    )


def migration_overview(
    state: Mapping[str, SignalState],
    rows: list[StuckRow],
    blocking: list[BlockingRow],
    *,
    now: datetime,
    history: History | None = None,
) -> MigrationOverview:
    cutoff = now - RECENT
    history = history or History.approximate(state)
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    mine = [s for s in state.values() if s.signal.kind is Kind.MIGRATION_BLOCKED]
    return MigrationOverview(
        total=len(rows),
        settling=sum(1 for r in rows if r.settling(now)),
        reasons=sorted(counts.items(), key=lambda kv: _reason_key(kv[0])),
        frozen=sum(1 for r in rows if "freeze" in r.hints),
        synced=sum(1 for r in rows if r.synced),
        opened_recently=history.opened_since(Kind.MIGRATION_BLOCKED, cutoff),
        resolved_recently=history.resolved_since(Kind.MIGRATION_BLOCKED, cutoff),
        returning=sum(1 for r in rows if r.occurrences > 1),
        reuploaded=sum(1 for r in rows if r.uploads > 1),
        blocking=len({b.test for b in blocking}),
        tracked_since=min((s.first_seen for s in mine), default=None),
    )


def _groups(
    kind: str,
    subtitle: str,
    rows: Iterable[Row],
    stuck: Iterable[StuckRow],
    blocking: Iterable[BlockingRow],
    key: Any,
) -> dict[str, Group]:
    """Bucket all three kinds of work under the names `key` gives each row."""
    found: dict[str, Group] = {}

    def add(item: Any, field_name: str) -> None:
        for name in key(item):
            group = found.setdefault(
                name, Group(kind=kind, name=name, subtitle=subtitle)
            )
            getattr(group, field_name).append(item)

    for row in rows:
        add(row, "rows")
    for row in stuck:
        add(row, "stuck")
    for row in blocking:
        add(row, "blocking")
    return found


def _alphabetical(groups: Iterable[Group]) -> list[Group]:
    """By name, ignoring case, with the two catch-all uploader groups
    (+1 maintenance, no uploader recorded) last: they are not people, and
    "+1" would otherwise sort ahead of everyone."""
    catchall = {PLUS_ONE, NO_UPLOADER}
    return sorted(groups, key=lambda g: (g.name in catchall, g.title.casefold()))


def _route_waiting_without_upload(
    waiting: Mapping[str, list[StuckRow]],
    stuck_names: set[str],
    metadata: Snapshot,
    teams: list[Group],
    package_sets: list[Group],
) -> None:
    """Give uploads waiting on a package that is not itself stuck to that
    package's teams and package sets, creating their groups if needed."""
    for kind, subtitle, groups, axis in (
        ("teams", "subscribed team", teams, "teams"),
        ("sets", "package set", package_sets, "package_sets"),
    ):
        by_name = {g.name: g for g in groups}
        for awaited, rows in sorted(waiting.items()):
            if awaited in stuck_names:
                continue
            for name in getattr(metadata.get(awaited), axis):
                group = by_name.get(name)
                if group is None:
                    group = Group(kind=kind, name=name, subtitle=subtitle)
                    by_name[name] = group
                    groups.append(group)
                group.waiting.extend((row, awaited) for row in rows)


def _ordered(groups: Iterable[Group]) -> list[Group]:
    return sorted(groups, key=lambda g: (-g.size, g.title))


def group_by_uploader(
    rows: list[Row],
    stuck: Iterable[StuckRow] = (),
    blocking: Iterable[BlockingRow] = (),
) -> list[Group]:
    """The uploader of the version on the row is how Ubuntu routes work.

    Ubuntu's +1 maintenance guide makes the person who last touched a package
    responsible for merging it, and unlike team subscriptions that axis is
    populated for universe as well as main. Ubuntu's migration docs make the
    uploader responsible for getting their upload to migrate.

    Regressing tests are not routed by uploader: the last person to upload a
    test package did not cause someone else's upload to break it. Teams and
    package sets carry those.
    """

    def key(item: Any) -> list[str]:
        if isinstance(item, StuckRow):
            return item.owners
        return [item.uploader or ""]

    found = _groups("uploaders", "uploader", rows, stuck, (), key)
    unknown = found.pop("", None)
    plus_one = found.pop(PLUS_ONE, None)
    groups = _ordered(found.values())

    if plus_one is not None:
        groups.append(
            Group(
                kind="uploaders",
                name=PLUS_ONE,
                label="+1 maintenance",
                subtitle="Debian syncs",
                rows=plus_one.rows,
                stuck=plus_one.stuck,
                note=(
                    "Synced from Debian with no Ubuntu uploader to route to. "
                    "Ubuntu's migration process gives these to +1 maintenance. "
                    "A sync someone asked for is also listed under them."
                ),
            )
        )
    if unknown is not None:
        groups.append(
            Group(
                kind="uploaders",
                name=NO_UPLOADER,
                subtitle="not found in Launchpad",
                rows=unknown.rows,
                stuck=unknown.stuck,
                note=(
                    "Launchpad returned no current publication for these, so "
                    "they have no owner to route to."
                ),
            )
        )
    return groups


def group_by_package_set(
    rows: list[Row],
    stuck: Iterable[StuckRow] = (),
    blocking: Iterable[BlockingRow] = (),
) -> list[Group]:
    """Package sets are upload rights, keyed by (name, series).

    They are not teams. AGENTS.md section 5 records that ubuntu-desktop exists
    on both axes covering different populations, so this never mixes the two.
    """
    found = _groups(
        "sets", "package set", rows, stuck, blocking, lambda r: r.package_sets
    )
    return _ordered(found.values())


def group_by_team(
    rows: list[Row],
    stuck: Iterable[StuckRow] = (),
    blocking: Iterable[BlockingRow] = (),
) -> list[Group]:
    """Bug subscription, the responsibility axis. Not package sets."""
    found = _groups(
        "teams", "subscribed team", rows, stuck, blocking, lambda r: r.teams
    )
    return _ordered(found.values())


def _iso_date(when: date | datetime | None) -> str:
    """ISO 8601 date, the one format for a date shown on its own."""
    return "" if when is None else when.strftime("%Y-%m-%d")


def _iso_time(when: datetime | None) -> str:
    """ISO 8601 date and time to the minute, UTC: the one format for a
    timestamp. Every page uses this or _iso_date, never its own."""
    if when is None:
        return ""
    if when.tzinfo is not None:
        when = when.astimezone(UTC)
    return when.strftime("%Y-%m-%d %H:%M UTC")


@cache
def asset(name: str) -> str:
    """An asset's URL, relative to the site root, versioned by its content.

    Every page asks for the same stylesheet and script, so without a version
    a browser keeps the copy it fetched first and shows new markup with old
    styles until its cache expires. The hash changes only when the file
    does, so an unchanged asset stays cached across rebuilds.
    """
    path = ASSETS / name
    if not path.is_file():
        return f"assets/{name}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    return f"assets/{name}?v={digest}"


@cache
def _environment() -> Environment:
    """One environment per process. Templates import shared macro files,
    and a fresh environment recompiles all of them for every page: with a
    page per package that was most of the build time."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["slug"] = _slug
    env.filters["iso_date"] = _iso_date
    env.filters["iso_time"] = _iso_time
    env.globals["autopkgtest_url"] = autopkgtest_url
    env.globals["autopkgtest_log_url"] = autopkgtest_log_url
    env.globals["retry_url"] = retry_url
    env.globals["age_bucket"] = age_bucket
    env.globals["package_href"] = package_href
    env.globals["asset"] = asset
    return env


def _observed_series(state: Mapping[str, SignalState]) -> str:
    """Fallback when no run record names the series.

    The most recently observed active signal, never the first in the log:
    the log outlives every series, so its first line names whichever series
    cairn started in, long after that series has been released.
    """
    latest = max(
        (
            st
            for st in state.values()
            if st.is_active and st.signal.kind in DEVELOPMENT_KINDS and st.signal.series
        ),
        key=lambda st: st.last_seen,
        default=None,
    )
    return latest.signal.series if latest and latest.signal.series else "unknown"


# Which ingest source each board's signals come from.
BOARD_SOURCES: dict[str, tuple[str, ...]] = {
    "merges": ("merges",),
    "migration": ("migration",),
    "nbs": ("nbs",),
}


def collected_for(health: Mapping[str, SourceHealth]) -> dict[str | None, datetime]:
    """When each board's data was last collected, for its footer.

    Per board, not the newest success overall: after a partial run, a fresh
    merges collection must not vouch for migration data that failed to
    refresh. The site root, which summarises every board, takes the oldest.
    """
    found: dict[str | None, datetime] = {}
    for board, sources in BOARD_SOURCES.items():
        times = [
            health[s].last_success
            for s in sources
            if s in health and health[s].last_success
        ]
        if times:
            found[board] = min(times)
    # The root summarises every board, so it has a date only once every
    # board has one. Until then its footer says nothing was collected yet,
    # rather than lending one board's date to another that never ran.
    if found and all(board in found for board in BOARD_SOURCES):
        found[None] = min(found[board] for board in BOARD_SOURCES)
    return found


def stale_for(
    health: Mapping[str, SourceHealth], *, now: datetime
) -> dict[str | None, list[SourceHealth]]:
    """Out-of-date sources per board, so a board warns only about its own.

    A stale migration ingest says nothing about the merges rows, and the
    other way round. The root, which summarises every board, lists every
    out-of-date source. A source that has never run has no health record and
    is not listed here; the footer says so instead.
    """
    found: dict[str | None, list[SourceHealth]] = {}
    for board, sources in BOARD_SOURCES.items():
        found[board] = sorted(
            (health[s] for s in sources if s in health and health[s].is_stale(now)),
            key=lambda h: h.source,
        )
    found[None] = sorted(
        {h.source: h for found_ in found.values() for h in found_}.values(),
        key=lambda h: h.source,
    )
    return found


def merges_for_stuck(rows: Iterable[Row], stuck: Iterable[StuckRow]) -> dict[str, Row]:
    """Stuck uploads that still need a merge, mapped to that merge's row.

    Only when Debian is ahead of the stuck upload itself, by Debian version
    ordering, and that upload carries an Ubuntu delta. A merge signal carried
    forward after a failed merges ingest may describe an older upload, and
    must not be read as saying anything about this one.
    """
    by_package = {row.package: row for row in rows}
    found = {}
    for upload in stuck:
        merge = by_package.get(upload.package)
        if (
            merge is not None
            and has_ubuntu_delta(upload.new_version)
            and version_compare(merge.debian_version, upload.new_version) > 0
        ):
            found[upload.package] = merge
    return found


@dataclass(frozen=True, slots=True)
class Skipped:
    """The board's view of uploads britney tried and backed out of."""

    # Longest in -proposed first, as on the board.
    rows: tuple[StuckRow, ...] = ()
    # (first architecture that breaks, uploads), most first.
    arches: tuple[tuple[str, int], ...] = ()
    # (source, the skipped uploads that would break it), most first: the
    # packages most often in the way, which is where a fix helps most.
    most_broken: tuple[tuple[str, tuple[StuckRow, ...]], ...] = ()


def skipped(stuck: Iterable[StuckRow]) -> Skipped:
    rows = tuple(r for r in stuck if "breaks_installs" in r.reasons)
    arches: dict[str, int] = {}
    broken: dict[str, list[StuckRow]] = {}
    for row in rows:
        for arch in dict.fromkeys(a.arch for a in row.attempts if a.arch):
            arches[arch] = arches.get(arch, 0) + 1
        for source in row.broken_sources:
            # An upload breaking its own binaries is in nobody else's way.
            if source != row.package:
                broken.setdefault(source, []).append(row)
    return Skipped(
        rows=rows,
        arches=tuple(sorted(arches.items(), key=lambda kv: (-kv[1], kv[0]))),
        most_broken=tuple(
            (source, tuple(found))
            for source, found in sorted(
                broken.items(), key=lambda kv: (-len(kv[1]), kv[0])
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class Accepted:
    """An item britney accepted in its run: an upload, an upload on one
    architecture ("src/arch"), or a removal of binaries left behind
    ("-bin/arch")."""

    item: str
    migrated: bool = True
    old_version: str | None = None
    new_version: str | None = None

    @property
    def removal(self) -> bool:
        return self.item.startswith("-")

    @property
    def name(self) -> str:
        return self.item.lstrip("-").split("/", 1)[0]

    @property
    def arch(self) -> str | None:
        _, _, arch = self.item.partition("/")
        return arch or None


@dataclass(frozen=True, slots=True)
class OldLibrary:
    """A binary its source no longer builds, which britney could not remove
    from the release without breaking what still needs it."""

    binary: str
    arches: tuple[str, ...]
    attempts: tuple[Attempt, ...] = ()
    # Other architectures where removing it would break something too.
    also_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunView:
    """Britney's last run, from its run log (update_output.txt)."""

    generated: datetime | None = None
    architectures: tuple[str, ...] = ()
    considered: int | None = None
    before: Mapping[str, int] = field(default_factory=dict)
    after: Mapping[str, int] = field(default_factory=dict)
    accepted: tuple[Accepted, ...] = ()
    old_libraries: tuple[OldLibrary, ...] = ()
    skipped: Skipped = field(default_factory=Skipped)
    # False when the saved run does not describe the board's rows: a run
    # that logged its events and then failed to save its detail leaves the
    # previous run's detail behind (review on #30).
    matches: bool = True

    @property
    def available(self) -> bool:
        return self.generated is not None and self.matches

    @property
    def migrated(self) -> tuple[Accepted, ...]:
        return tuple(a for a in self.accepted if a.migrated)

    def total(self, counts: Mapping[str, int]) -> int:
        return sum(counts.values())

    def checked(self, attempt: Attempt) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(passed, not checked) around the architecture an attempt broke.

        Britney checks the architectures in its "Arch order is" order and
        stops at the first that gets worse (britney2/migration.py,
        migrate_items_to_target_suite): those before it passed, those after
        it were never checked. Only for a whole upload; an item on one
        architecture is checked on that one alone.
        """
        if "/" in attempt.item or attempt.arch not in self.architectures:
            return (), ()
        i = self.architectures.index(attempt.arch)
        return self.architectures[:i], self.architectures[i + 1 :]


def run_view(detail: Mapping[str, Any] | None, stuck: Iterable[StuckRow]) -> RunView:
    """The run as saved, joined to the board's rows only when they agree.

    The skipped list comes from the run itself, and must name the same
    sources the log says are skipped now; otherwise the detail is from
    another run, and the page says so rather than mixing two runs.
    """
    run = (detail or {}).get("run") or {}
    generated = run.get("generated")
    stuck = list(stuck)
    logged = {r.package for r in stuck if "breaks_installs" in r.reasons}
    saved = set(run.get("skipped") or ())
    return RunView(
        generated=datetime.fromisoformat(generated) if generated else None,
        architectures=tuple(run.get("architectures") or ()),
        considered=run.get("considered"),
        before=dict(run.get("before") or {}),
        after=dict(run.get("after") or {}),
        accepted=tuple(
            Accepted(
                item=str(a.get("item") or ""),
                migrated=bool(a.get("migrated", True)),
                old_version=a.get("old_version"),
                new_version=a.get("new_version"),
            )
            for a in run.get("accepted") or ()
        ),
        old_libraries=tuple(
            OldLibrary(
                binary=str(o.get("binary") or ""),
                arches=tuple(o.get("arches") or ()),
                attempts=tuple(_attempt(b) for b in o.get("breaks") or ()),
                also_on=tuple(o.get("also_on") or ()),
            )
            for o in run.get("old_libraries") or ()
        ),
        skipped=skipped(r for r in stuck if r.package in saved),
        matches="skipped" in run and saved == logged,
    )


def waiting_for(stuck: Iterable[StuckRow]) -> dict[str, list[StuckRow]]:
    """Turn "this upload waits for those" around: awaited package -> the
    stuck uploads waiting on it.

    Britney names what an upload waits for, never what waits for it, so the
    owner of the awaited upload cannot see from britney's page that they are
    holding anyone back. Measured 5 Oct 2026: 22 stuck uploads waiting on 23
    packages.
    """
    found: dict[str, list[StuckRow]] = {}
    for row in stuck:
        for name in row.waits_for:
            if name != row.package:
                found.setdefault(name, []).append(row)
    return found


# The payload fields whose change is worth a line in a package's history.
# Others, such as which architectures a test is still running on, change on
# most runs and would drown the moments that matter.
TIMELINE_FIELDS: dict[Kind, tuple[str, ...]] = {
    Kind.NEEDS_MERGE: ("ubuntu_version", "debian_version", "in_proposed"),
    Kind.MIGRATION_BLOCKED: ("new_version", "reasons"),
    Kind.NBS: ("removable",),
}


@dataclass(frozen=True, slots=True)
class Moment:
    """One line of a package's history. Codes and values only; the template
    says what each means."""

    ts: datetime
    kind: Kind
    event: EventType
    payload: Mapping[str, Any]
    # (field, before, after) for each tracked field an update changed.
    changes: tuple[tuple[str, Any, Any], ...] = ()
    # Which binary, for kinds about one (NBS).
    binary: str | None = None


def timeline(events: Iterable[Event]) -> list[Moment]:
    """What happened to one package, newest first.

    Openings and resolutions always; an update only when it changed a field
    in TIMELINE_FIELDS. Diffed per signal, so a merge update is never
    compared against a migration payload.
    """
    last: dict[str, Mapping[str, Any]] = {}
    moments = []
    for event in events:
        sid = event.to_signal().signal_id
        before = last.get(sid, {})
        last[sid] = event.payload
        if event.event is EventType.UPDATED:
            changes = tuple(
                (name, before.get(name), event.payload.get(name))
                for name in TIMELINE_FIELDS.get(event.kind, ())
                if before.get(name) != event.payload.get(name)
            )
            if not changes:
                continue
            moments.append(
                Moment(
                    event.ts,
                    event.kind,
                    event.event,
                    event.payload,
                    changes,
                    event.binary_package,
                )
            )
        else:
            moments.append(
                Moment(
                    event.ts,
                    event.kind,
                    event.event,
                    event.payload,
                    binary=event.binary_package,
                )
            )
    moments.reverse()
    return moments


@dataclass(frozen=True, slots=True)
class PackageView:
    """Everything cairn knows about one source package, on one page.

    The answer to "what is the status of this package?", which no single
    upstream page gives: whether it needs a merge, whether its upload is
    stuck and why, whom it is holding back, who owns it, and what has happened
    to it since cairn started watching.
    """

    name: str
    merge: Row | None
    stuck: StuckRow | None
    # This package's tests regressing someone else's upload.
    blocking: tuple[BlockingRow, ...]
    # Stuck uploads that name this package among what they wait for.
    waiting: tuple[StuckRow, ...]
    owners: PackageMetadata
    history: tuple[Moment, ...]
    up_for_grabs: bool = False
    # Binaries this source no longer builds that are still published.
    nbs: tuple[NbsRow, ...] = ()
    # NBS binaries from other sources that this package still needs.
    needs_nbs: tuple[tuple[NbsRow, NeededBy], ...] = ()

    @property
    def href(self) -> str:
        return package_href(self.name)

    @property
    def is_open(self) -> bool:
        return bool(
            self.merge
            or self.stuck
            or self.blocking
            or self.waiting
            or self.nbs
            or self.needs_nbs
        )

    @property
    def holding_count(self) -> int:
        """Other uploads this package is holding back, counted once each."""
        return len(
            {b.holding.package for b in self.blocking}
            | {w.package for w in self.waiting}
        )

    @property
    def last_change(self) -> datetime | None:
        return self.history[0].ts if self.history else None

    @property
    def component(self) -> str | None:
        if self.merge and self.merge.component:
            return self.merge.component
        if self.stuck and self.stuck.component:
            return self.stuck.component
        # An NBS binary's component, failing anything better: the source's
        # own may have moved since. Then that of what it needs one with, for
        # a package on the site only for that (review on #26).
        if self.nbs:
            return self.nbs[0].component
        return self.needs_nbs[0][1].component if self.needs_nbs else None

    @property
    def launchpad_url(self) -> str:
        return LAUNCHPAD_SOURCE.format(source=self.name)

    @property
    def debian_url(self) -> str:
        return DEBIAN_TRACKER.format(source=self.name)

    @property
    def watched_since(self) -> datetime | None:
        return self.history[-1].ts if self.history else None

    @property
    def stuck_since(self) -> datetime | None:
        """When the current stuck episode began, across re-uploads.

        Britney dates only the current upload, so a package re-uploaded
        three times reads as stuck for a day. The latest opening in the
        log is when it last went from migrating to stuck.
        """
        if self.stuck is None:
            return None
        return next(
            (
                m.ts
                for m in self.history
                if m.kind is Kind.MIGRATION_BLOCKED and m.event is EventType.OPENED
            ),
            None,
        )


def package_href(name: str) -> str:
    """A package's own page, relative to the site root. Debian source names
    are [a-z0-9][a-z0-9+.-]*, all safe in a path."""
    return f"packages/{name}.html"


def package_views(
    rows: Iterable[Row],
    stuck: Iterable[StuckRow],
    blocking: Iterable[BlockingRow],
    metadata: Snapshot,
    history: History,
    grabs: UpForGrabs | None = None,
    nbs: Iterable[NbsRow] = (),
) -> dict[str, PackageView]:
    """A page for every package cairn has ever had something to say about:
    every package in the log, open or resolved, plus every package holding
    another back, whose owners need to find it by name, and every package
    still needing a binary no longer built."""
    nbs = list(nbs)
    unbuilt: dict[str, list[NbsRow]] = {}
    for row in nbs:
        unbuilt.setdefault(row.source, []).append(row)
    needing = needs_nbs(nbs)
    stuck = list(stuck)
    merges = {r.package: r for r in rows}
    stucks = {r.package: r for r in stuck}
    blocked: dict[str, list[BlockingRow]] = {}
    for row in blocking:
        blocked.setdefault(row.test, []).append(row)
    waiting = waiting_for(stuck)
    taken = {r.package for r in grabs.merges} | grabs.stuck_names if grabs else set()
    names = set(history.by_package) | set(merges) | set(stucks)
    names |= set(blocked) | set(waiting) | set(unbuilt) | set(needing)
    return {
        name: PackageView(
            name=name,
            merge=merges.get(name),
            stuck=stucks.get(name),
            blocking=tuple(blocked.get(name, ())),
            waiting=tuple(waiting.get(name, ())),
            owners=metadata.get(name),
            history=tuple(timeline(history.by_package.get(name, ()))),
            up_for_grabs=name in taken,
            nbs=tuple(unbuilt.get(name, ())),
            needs_nbs=tuple(needing.get(name, ())),
        )
        for name in sorted(names)
    }


def set_sizes(metadata: Snapshot) -> dict[str, int]:
    """Packages per package set, for "N of M need attention".

    Package sets only. The snapshot holds every member of every set, but
    team subscriptions are collected only for packages cairn already has a
    signal for, so a team's total would be an undercount presented as a
    denominator.
    """
    sizes: dict[str, int] = {}
    for owner in metadata.packages.values():
        for name in owner.package_sets:
            sizes[name] = sizes.get(name, 0) + 1
    return sizes


def age_bucket(row: Row, now: datetime) -> str:
    """The AGE_BUCKETS slug a merge row falls in, for the age filter."""
    days = row.behind_days(now)
    if days is None:
        return "unknown"
    for label, ceiling in AGE_BUCKETS:
        if ceiling is None or days < ceiling:
            return _slug(label)
    return "unknown"


def _context(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None,
    *,
    now: datetime,
    series: str | None,
    history: History | None = None,
    series_status: str | None = None,
    migration_detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or Snapshot()
    rows = merge_rows(state, metadata, now=now)
    history = history or History.approximate(state)
    stuck = stuck_rows(
        state, metadata, now=now, uploads=history.uploads, detail=migration_detail
    )
    blocking = blocking_rows(stuck, metadata)
    if series is None:
        series = _observed_series(state)

    uploaders = group_by_uploader(rows, stuck)
    package_sets = group_by_package_set(rows, stuck, blocking)
    teams = group_by_team(rows, stuck, blocking)
    grabs = up_for_grabs(rows, stuck, now=now)
    waiting = waiting_for(stuck)
    unbuilt = nbs_rows(state)
    packages = package_views(rows, stuck, blocking, metadata, history, grabs, unbuilt)
    collected = collected_for(health)
    cutoff = now - RECENT

    for group in [*uploaders, *package_sets, *teams]:
        group.waiting.extend(
            (row, held) for held in group.stuck for row in waiting.get(held.package, ())
        )
    # An awaited package with no stuck upload of its own: no uploader to
    # route to (there is no upload on a row), so its teams and package sets
    # hear about it, as for a failing test.
    _route_waiting_without_upload(
        waiting, {r.package for r in stuck}, metadata, teams, package_sets
    )

    def merging(groups: list[Group]) -> list[Group]:
        """The merges index lists only owners with merges, largest first,
        keeping the two catch-all groups last."""
        catchall = {PLUS_ONE, NO_UPLOADER}
        found = [g for g in groups if g.rows]
        return sorted(found, key=lambda g: (g.name in catchall, -g.count, g.title))

    def stuck_in(groups: list[Group]) -> list[Group]:
        catchall = {PLUS_ONE, NO_UPLOADER}
        found = [g for g in groups if g.stuck or g.has_holding]
        return sorted(
            found,
            key=lambda g: (
                g.name in catchall,
                -g.stuck_count,
                -g.blocking_count,
                g.title,
            ),
        )

    return {
        "rows": rows,
        "overview": overview(state, rows, health, now=now, history=history),
        "uploaders": merging(uploaders),
        "package_sets": merging(package_sets),
        "teams": merging(teams),
        "stuck": stuck,
        "blocking": blocking,
        "migration": migration_overview(
            state, stuck, blocking, now=now, history=history
        ),
        "migration_uploaders": stuck_in(uploaders),
        "migration_package_sets": stuck_in(package_sets),
        "migration_teams": stuck_in(teams),
        "all_groups": [*uploaders, *package_sets, *teams],
        # Tests holding back others are left out of the all-packages page:
        # each is already listed under the upload it holds.
        "everything": Group(
            kind=EVERYTHING,
            name=EVERYTHING,
            subtitle="every package",
            rows=rows,
            stuck=stuck,
        ),
        # Cross-links between the boards: a lookup by package, no more.
        "stuck_by_package": {r.package: r for r in stuck},
        "run": run_view(migration_detail, stuck),
        "merge_for_stuck": merges_for_stuck(rows, stuck),
        "waiting_for": waiting,
        "packages": packages,
        "grabs": grabs,
        "plus_one": plus_one(rows, stuck, blocking, waiting, now=now),
        "high_impact_min": HIGH_IMPACT_MIN,
        "merges_trend": history.trends.of(Kind.NEEDS_MERGE).until(
            collected.get("merges")
        ),
        "migration_trend": history.trends.of(Kind.MIGRATION_BLOCKED).until(
            collected.get("migration")
        ),
        "nbs_rows": unbuilt,
        "nbs": NbsOverview(
            total=len(unbuilt),
            removable=sum(1 for r in unbuilt if r.removable),
            sources_needing=len({s for r in unbuilt for s in r.sources_needing}),
            opened_recently=history.opened_since(Kind.NBS, cutoff),
            resolved_recently=history.resolved_since(Kind.NBS, cutoff),
        ),
        "nbs_trend": history.trends.of(Kind.NBS).until(collected.get("nbs")),
        "recent_merges": history.opened_packages_since(Kind.NEEDS_MERGE, cutoff),
        "recent_stuck": history.opened_packages_since(Kind.MIGRATION_BLOCKED, cutoff),
        # The directory is for finding a name, so it is alphabetical; the
        # boards' "who has the most" panels rank by size instead.
        "owners": {
            "teams": _alphabetical(teams),
            "sets": _alphabetical(package_sets),
            "uploaders": _alphabetical(uploaders),
        },
        "set_sizes": set_sizes(metadata),
        "owner_lookup": {
            (g.kind, g.name): g for g in [*uploaders, *package_sets, *teams]
        },
        # When cairn's record of each board begins: an opening on that date
        # means "already open", not "opened then".
        "watching_since": {
            str(kind): first.ts
            for kind, trend in history.trends.by_kind.items()
            if (first := trend.first) is not None
        },
        "series": series,
        # Launchpad's word for the series, lower case for display beside
        # its name: "pre-release freeze". Replaces a banner that inferred a
        # freeze from britney's hints.
        "series_status": series_status.lower() if series_status else None,
        "now": now,
        # The latest successful collection by any source, for the footer.
        "collected_for": collected,
        "health": sorted(health.values(), key=lambda h: h.source),
        "stale_for": stale_for(health, now=now),
        "vanilla_css": VANILLA_CSS,
        "ubuntu_logo": UBUNTU_LOGO,
        "metadata_age": metadata.age(now),
        "root": "",
        "section": "merges",
        "settling_days": SETTLING_DAYS,
        "abandoned_days": ABANDONED_DAYS,
    }


def render(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None = None,
    *,
    now: datetime,
    series: str | None = None,
    history: History | None = None,
    series_status: str | None = None,
) -> str:
    context = _context(
        state,
        health,
        metadata,
        now=now,
        series=series,
        history=history,
        series_status=series_status,
    )
    return _render_merges(context)


def _render_merges(context: Mapping[str, Any]) -> str:
    return (
        _environment()
        .get_template("merges.html")
        .render(**{**context, "root": "../", "section": "merges"})
    )


def render_home(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None = None,
    *,
    now: datetime,
    series: str | None = None,
    history: History | None = None,
    series_status: str | None = None,
) -> str:
    context = _context(
        state,
        health,
        metadata,
        now=now,
        series=series,
        history=history,
        series_status=series_status,
    )
    return _render_home(context)


def _render_home(context: Mapping[str, Any]) -> str:
    """The site root: every board, none of them the default."""
    return (
        _environment()
        .get_template("index.html")
        .render(**{**context, "root": "", "section": None})
    )


def render_migration(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None = None,
    *,
    now: datetime,
    series: str | None = None,
    history: History | None = None,
    series_status: str | None = None,
) -> str:
    context = _context(
        state,
        health,
        metadata,
        now=now,
        series=series,
        history=history,
        series_status=series_status,
    )
    return _render_migration(context)


def _render_migration(context: Mapping[str, Any]) -> str:
    return (
        _environment()
        .get_template("migration.html")
        .render(
            **{
                **context,
                "root": "../",
                "section": "migration",
                "current_href": "migration/index.html",
            }
        )
    )


def _depth(href: str) -> str:
    """The prefix from a page at `href` back to the site root."""
    return "../" * href.count("/")


def render_group(group: Group, context: Mapping[str, Any]) -> str:
    """An owner's merges, or every merge. Links are prefixed back to the root."""
    return (
        _environment()
        .get_template("group.html")
        .render(
            **{
                **context,
                "group": group,
                "root": _depth(group.href),
                "section": "merges",
            }
        )
    )


def render_migration_group(group: Group, context: Mapping[str, Any]) -> str:
    """An owner's stuck uploads, or every stuck upload."""
    return (
        _environment()
        .get_template("migration_group.html")
        .render(
            **{
                **context,
                "group": group,
                "root": _depth(group.migration_href),
                "section": "migration",
            }
        )
    )


def render_owner(group: Group, context: Mapping[str, Any]) -> str:
    """An owner's overview."""
    return _render_page(
        "owner.html", group.overview_href, context, group=group, section="owners"
    )


def render_holding_group(group: Group, context: Mapping[str, Any]) -> str:
    """What an owner is holding back."""
    return _render_page(
        "holding_group.html",
        group.holding_href,
        context,
        group=group,
        section="migration",
    )


def _render_page(
    template: str, href: str, context: Mapping[str, Any], **extra: Any
) -> str:
    """Any other page, at `href` relative to the site root."""
    return (
        _environment()
        .get_template(template)
        .render(**{**context, "root": _depth(href), "current_href": href, **extra})
    )


def render_package(view: PackageView, context: Mapping[str, Any]) -> str:
    return _render_page(
        "package.html", view.href, context, package=view, section="packages"
    )


class NotASiteDirectory(RuntimeError):
    """The output directory holds something cairn did not generate."""


def _clear(out: Path) -> None:
    """Remove the previous build, so a group that loses its last candidate
    does not leave a stale page behind for a bookmarked URL to find.

    Refuses on a non-empty directory cairn has not written to before. The
    alternative is trusting whatever --out was given, which is one typo away
    from deleting someone's work.
    """
    if not out.exists():
        return
    entries = list(out.iterdir())
    recognised = (out / MARKER).exists() or (
        # A site built before the marker existed: an index plus nothing but
        # the directories this module writes.
        (out / "index.html").exists()
        and all(entry.name in GENERATED or entry.name == MARKER for entry in entries)
    )
    if entries and not recognised:
        raise NotASiteDirectory(
            f"{out} is not empty and was not generated by cairn; "
            f"refusing to remove its contents"
        )
    for owned in GENERATED:
        stale = out / owned
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()


def build(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None = None,
    *,
    out: Path,
    now: datetime,
    series: str | None = None,
    history: History | None = None,
    series_status: str | None = None,
    migration_detail: Mapping[str, Any] | None = None,
) -> Path:
    context = _context(
        state,
        health,
        metadata,
        now=now,
        series=series,
        history=history,
        series_status=series_status,
        migration_detail=migration_detail,
    )
    _clear(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("Generated by cairn build. Safe to delete.\n")
    index = out / "index.html"
    index.write_text(_render_home(context), encoding="utf-8")
    (out / "merges").mkdir(exist_ok=True)
    (out / "merges" / "index.html").write_text(
        _render_merges(context), encoding="utf-8"
    )
    (out / "migration").mkdir(exist_ok=True)
    (out / "migration" / "index.html").write_text(
        _render_migration(context), encoding="utf-8"
    )

    # The board front pages above list every row; all.html only redirects
    # there, for links made before the move.
    for board in ("merges", "migration"):
        (out / board / "all.html").write_text(
            _render_page("moved.html", f"{board}/all.html", context, section=board),
            encoding="utf-8",
        )

    for group in context["all_groups"]:
        pages = []
        if group.has_merges:
            pages.append((group.href, render_group))
        if group.has_migration:
            pages.append((group.migration_href, render_migration_group))
        if group.has_holding:
            pages.append((group.holding_href, render_holding_group))
        pages.append((group.overview_href, render_owner))
        for href, render_page in pages:
            page = out / href
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(render_page(group, context), encoding="utf-8")

    (out / "packages").mkdir(exist_ok=True)
    for view in context["packages"].values():
        (out / view.href).write_text(render_package(view, context), encoding="utf-8")
    for href, template, section in (
        ("migration/update-output.html", "update_output.html", "migration"),
        ("nbs/index.html", "nbs.html", "nbs"),
        ("packages/index.html", "packages.html", "packages"),
        ("owners/index.html", "owners.html", "owners"),
        ("work/index.html", "work.html", "work"),
        ("work/plus-one.html", "plus_one.html", "work"),
    ):
        page = out / href
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            _render_page(template, href, context, section=section), encoding="utf-8"
        )

    if ASSETS.is_dir():
        shutil.copytree(ASSETS, out / "assets", dirs_exist_ok=True)
    return index
