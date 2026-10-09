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
from cairn.ingest.merges import has_ubuntu_delta
from cairn.ingest.metadata import PackageMetadata, Snapshot
from cairn.ingest.migration import FAILED, payload_tests

TEMPLATES = Path(__file__).parent / "templates"
ASSETS = Path(__file__).parent / "assets"

# Everything build() writes, and therefore everything it may remove.
# uploaders/, sets/ and teams/ at the root are where merges owner pages lived
# before each board got its own directory; listed so a rebuild clears them.
GENERATED = (
    "index.html",
    "merges",
    "migration",
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
    "no_binaries",
    "waiting",
    "needs_approval",
    "block_bug",
    "rc_bug",
    "tests_running",
    "other",
)

# Synced uploads have no Ubuntu uploader to route to. Ubuntu's proposed
# migration docs give those to +1 maintenance, so they are grouped there
# rather than under a Debian developer who will never read this page.
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
    url = AUTOPKGTEST_PACKAGE.format(prefix=autopkgtest_prefix(source), source=source)
    if series and arch:
        url += f"/{series}/{arch}"
    return url


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

    def only(self, failed: bool) -> HeldTest | None:
        kept = tuple(r for r in self.results if r.failed is failed)
        return HeldTest(self.test, self.version, kept) if kept else None

    def by_status(self) -> list[tuple[str, tuple[TestResult, ...]]]:
        """Architectures grouped under one tag per status, failures first.

        One tag per group rather than per architecture: a test still running
        on four architectures reads "amd64, amd64v3, arm64, ppc64el" and one
        "Test in progress", not the same words four times over.
        """
        order = ("regression", "reference_running", "running")
        groups: dict[str, list[TestResult]] = {}
        for result in self.results:
            groups.setdefault(result.status, []).append(result)
        return sorted(
            ((status, tuple(found)) for status, found in groups.items()),
            key=lambda g: order.index(g[0]) if g[0] in order else len(order),
        )


def _held_tests(payload: Mapping[str, Any]) -> tuple[HeldTest, ...]:
    return tuple(
        HeldTest(
            test=t.get("test", ""),
            version=t.get("version", ""),
            results=tuple(
                TestResult(arch=arch, status=status)
                for arch, status in (t.get("results") or {}).items()
            ),
        )
        for t in payload_tests(payload)
    )


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
    bugs: tuple[int, ...]
    hints: tuple[str, ...]
    since: date | None
    uploader: str | None
    # No Ubuntu signer: a Debian sync. Routed to +1 maintenance.
    synced: bool
    package_sets: tuple[str, ...]
    teams: tuple[str, ...]
    first_seen: datetime
    occurrences: int
    # Distinct uploads cairn has seen stuck under this one signal. Britney
    # only ever knows about the current one.
    uploads: int
    url: str | None

    @property
    def owner(self) -> str:
        return PLUS_ONE if self.synced else (self.uploader or "")

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
        return HeldTest(self.test, self.version, self.results).by_status()


@dataclass(frozen=True, slots=True)
class Group:
    """One owner — a team, a package set or an uploader — and their work.

    Each kind of work gets its own page: merges at href, stuck uploads at
    migration_href, and what the owner is holding up at holding_href. One
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
    # (waiting upload, this owner's stuck upload it waits for). Filled in
    # _context once every stuck row is known.
    waiting: list[tuple[StuckRow, StuckRow]] = field(default_factory=list)

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
        """What the owner is holding up: their tests regressing other
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
        return len(self.blocking) + len(self.waiting)

    @property
    def primary_href(self) -> str:
        """Where "find your page" lands: the first of the owner's pages
        that has anything on it. Each links to the others."""
        if self.has_merges:
            return self.href
        if self.has_migration:
            return self.migration_href
        return self.holding_href

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

    def settled(self, now: datetime) -> list[StuckRow]:
        return [r for r in self.stuck if not r.settling(now)]

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
    revision_only: int = 0
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
) -> list[StuckRow]:
    """Longest in -proposed first; undated rows trail, as on the merges board."""
    metadata = metadata or Snapshot()
    uploads = uploads or {}
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
                tests=_held_tests(payload),
                missing_builds=tuple(payload.get("missing_builds") or ()),
                waits_for=tuple(payload.get("waits_for") or ()),
                bugs=tuple(payload.get("bugs") or ()),
                hints=tuple(payload.get("hints") or ()),
                since=_since(payload.get("in_proposed_since")),
                uploader=owner.uploader_of(new_version),
                synced=bool(shown and shown.synced),
                package_sets=owner.package_sets,
                teams=owner.teams,
                first_seen=st.first_seen,
                occurrences=st.occurrences,
                uploads=max(uploads.get(sid, 1), 1),
                # Derived, not read from the log: the package's Launchpad
                # page, whatever an older event stored.
                url=LAUNCHPAD_SOURCE.format(source=name),
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
        revision_only=sum(1 for r in rows if not r.new_upstream),
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
            return [item.owner]
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
                    "Ubuntu's migration process gives these to +1 maintenance."
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


def waiting_for(stuck: Iterable[StuckRow]) -> dict[str, list[StuckRow]]:
    """Turn "this upload waits for those" around: awaited package -> the
    stuck uploads waiting on it.

    Britney names what an upload waits for, never what waits for it, so the
    owner of the awaited upload cannot see from britney's page that they are
    holding anyone up. Measured 5 Oct 2026: 22 stuck uploads waiting on 23
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
                Moment(event.ts, event.kind, event.event, event.payload, changes)
            )
        else:
            moments.append(Moment(event.ts, event.kind, event.event, event.payload))
    moments.reverse()
    return moments


@dataclass(frozen=True, slots=True)
class PackageView:
    """Everything cairn knows about one source package, on one page.

    The answer to "what is the status of this package?", which no single
    upstream page gives: whether it needs a merge, whether its upload is
    stuck and why, whom it is holding up, who owns it, and what has happened
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

    @property
    def href(self) -> str:
        return package_href(self.name)

    @property
    def is_open(self) -> bool:
        return bool(self.merge or self.stuck or self.blocking or self.waiting)

    @property
    def holding_count(self) -> int:
        """Other uploads this package is holding up, counted once each."""
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
        return self.stuck.component if self.stuck else None

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
) -> dict[str, PackageView]:
    """A page for every package cairn has ever had something to say about:
    every package in the log, open or resolved, plus every package holding
    another up, whose owners need to find it by name."""
    stuck = list(stuck)
    merges = {r.package: r for r in rows}
    stucks = {r.package: r for r in stuck}
    blocked: dict[str, list[BlockingRow]] = {}
    for row in blocking:
        blocked.setdefault(row.test, []).append(row)
    waiting = waiting_for(stuck)
    taken = {r.package for r in grabs.merges} | grabs.stuck_names if grabs else set()
    names = set(history.by_package) | set(merges) | set(stucks)
    names |= set(blocked) | set(waiting)
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
) -> dict[str, Any]:
    metadata = metadata or Snapshot()
    rows = merge_rows(state, metadata, now=now)
    history = history or History.approximate(state)
    stuck = stuck_rows(state, metadata, now=now, uploads=history.uploads)
    blocking = blocking_rows(stuck, metadata)
    if series is None:
        series = _observed_series(state)

    uploaders = group_by_uploader(rows, stuck)
    package_sets = group_by_package_set(rows, stuck, blocking)
    teams = group_by_team(rows, stuck, blocking)
    grabs = up_for_grabs(rows, stuck, now=now)
    waiting = waiting_for(stuck)
    packages = package_views(rows, stuck, blocking, metadata, history, grabs)
    collected = collected_for(health)
    cutoff = now - RECENT

    for group in [*uploaders, *package_sets, *teams]:
        group.waiting.extend(
            (row, held) for held in group.stuck for row in waiting.get(held.package, ())
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
) -> str:
    context = _context(state, health, metadata, now=now, series=series, history=history)
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
) -> str:
    context = _context(state, health, metadata, now=now, series=series, history=history)
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
) -> str:
    context = _context(state, health, metadata, now=now, series=series, history=history)
    return _render_migration(context)


def _render_migration(context: Mapping[str, Any]) -> str:
    return (
        _environment()
        .get_template("migration.html")
        .render(**{**context, "root": "../", "section": "migration"})
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


def render_holding_group(group: Group, context: Mapping[str, Any]) -> str:
    """What an owner is holding up."""
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
) -> Path:
    context = _context(state, health, metadata, now=now, series=series, history=history)
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
        for href, render_page in pages:
            page = out / href
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(render_page(group, context), encoding="utf-8")

    (out / "packages").mkdir(exist_ok=True)
    for view in context["packages"].values():
        (out / view.href).write_text(render_package(view, context), encoding="utf-8")
    for href, template, section in (
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
