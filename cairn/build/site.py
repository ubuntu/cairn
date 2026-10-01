"""Turning the log into a static site.

Reads the replayed log, not SQLite: with one source there are no joins to
justify a database, and replay already yields exactly what the templates
need. The seam is render(state, health) — swapping the provider later
touches this module only.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cairn.core.health import SourceHealth
from cairn.core.log import SignalState
from cairn.core.rules import severity
from cairn.ingest.base import Kind
from cairn.ingest.metadata import Snapshot

TEMPLATES = Path(__file__).parent / "templates"
ASSETS = Path(__file__).parent / "assets"

# Everything build() writes, and therefore everything it may remove.
GENERATED = ("index.html", "uploaders", "sets", "teams", "assets")

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
    def debian_url(self) -> str:
        return DEBIAN_TRACKER.format(source=self.package)

    # Launchpad keeps a page per version. Only ':' is quoted, matching the
    # links Launchpad generates itself.
    @property
    def ubuntu_version_url(self) -> str:
        return (
            f"https://launchpad.net/ubuntu/+source/{self.package}/"
            f"{quote(self.ubuntu_version, safe='+~')}"
        )

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


@dataclass(frozen=True, slots=True)
class Group:
    """One owner's rows, rendered on its own page.

    A group page keeps the index scannable and means a developer can bookmark
    the one view that is theirs.
    """

    kind: str
    name: str
    subtitle: str
    rows: list[Row]
    note: str = ""

    @property
    def title(self) -> str:
        return self.name

    @property
    def slug(self) -> str:
        return _slug(self.name)

    @property
    def href(self) -> str:
        return f"{self.kind}/{self.slug}.html"

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def new_upstream(self) -> int:
        return sum(1 for r in self.rows if r.new_upstream)

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
        rows.append(
            Row(
                package=st.signal.source_package,
                component=payload.get("component"),
                ubuntu_version=payload.get("ubuntu_version", ""),
                debian_version=payload.get("debian_version", ""),
                base_version=payload.get("base_version", ""),
                new_upstream=bool(payload.get("new_upstream")),
                in_proposed=bool(payload.get("in_proposed")),
                uploader=owner.uploader,
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
) -> Overview:
    cutoff = now - RECENT
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
        opened_recently=sum(
            1
            for s in state.values()
            if s.signal.kind is Kind.NEEDS_MERGE and s.first_seen >= cutoff
        ),
        resolved_recently=sum(
            1
            for s in state.values()
            if s.signal.kind is Kind.NEEDS_MERGE
            and s.resolved_at is not None
            and s.resolved_at >= cutoff
        ),
        returning=sum(1 for r in rows if r.returning),
        unknown_uploader=sum(1 for r in rows if not r.uploader),
        undated=sum(1 for r in rows if r.debian_uploaded is None),
        buckets=age_buckets(rows, now=now),
        collected=max(
            (h.last_success for h in health.values() if h.last_success), default=None
        ),
    )


def group_by_uploader(rows: list[Row]) -> list[Group]:
    """The last uploader is how Ubuntu routes merge responsibility.

    Ubuntu's +1 maintenance guide makes the last person to touch a package
    responsible for merging it, and unlike team subscriptions that axis is
    populated for universe as well as main.
    """
    by_uploader: dict[str, list[Row]] = {}
    for row in rows:
        by_uploader.setdefault(row.uploader or "", []).append(row)

    groups = [
        Group(kind="uploaders", name=uploader, subtitle="last uploader", rows=owned)
        for uploader, owned in by_uploader.items()
        if uploader
    ]
    groups.sort(key=lambda g: (-g.count, g.title))

    unknown = by_uploader.get("")
    if unknown:
        groups.append(
            Group(
                kind="uploaders",
                name="no-uploader-recorded",
                subtitle="not found in Launchpad",
                rows=unknown,
                note=(
                    "Launchpad returned no current publication for these, so "
                    "they have no owner to route to."
                ),
            )
        )
    return groups


def group_by_package_set(rows: list[Row]) -> list[Group]:
    """Package sets are upload rights, keyed by (name, series).

    They are not teams. AGENTS.md section 5 records that ubuntu-desktop exists
    on both axes covering different populations, so this never mixes the two.
    """
    by_set: dict[str, list[Row]] = {}
    for row in rows:
        for name in row.package_sets:
            by_set.setdefault(name, []).append(row)

    groups = [
        Group(kind="sets", name=name, subtitle="package set", rows=members)
        for name, members in by_set.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.title))
    return groups


def group_by_team(rows: list[Row]) -> list[Group]:
    """Bug subscription, the responsibility axis. Not package sets."""
    by_team: dict[str, list[Row]] = {}
    for row in rows:
        for name in row.teams:
            by_team.setdefault(name, []).append(row)

    groups = [
        Group(kind="teams", name=name, subtitle="subscribed team", rows=members)
        for name, members in by_team.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.title))
    return groups


def _ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "never"
    delta = (now or datetime.now(UTC)) - when
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} min ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} h ago"
    if delta.days < 365:
        return f"{delta.days} d ago"
    return f"{delta.days // 365} y ago"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ago"] = _ago
    env.filters["slug"] = _slug
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
            if st.is_active and st.signal.kind is Kind.NEEDS_MERGE and st.signal.series
        ),
        key=lambda st: st.last_seen,
        default=None,
    )
    return latest.signal.series if latest and latest.signal.series else "unknown"


def _context(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None,
    *,
    now: datetime,
    series: str | None,
) -> dict[str, Any]:
    metadata = metadata or Snapshot()
    rows = merge_rows(state, metadata, now=now)
    if series is None:
        series = _observed_series(state)
    return {
        "rows": rows,
        "overview": overview(state, rows, health, now=now),
        "uploaders": group_by_uploader(rows),
        "package_sets": group_by_package_set(rows),
        "teams": group_by_team(rows),
        "series": series,
        "now": now,
        "health": sorted(health.values(), key=lambda h: h.source),
        "stale": [h for h in health.values() if h.is_stale(now)],
        "vanilla_css": VANILLA_CSS,
        "ubuntu_logo": UBUNTU_LOGO,
        "metadata_age": metadata.age(now),
        "root": "",
    }


def render(
    state: Mapping[str, SignalState],
    health: Mapping[str, SourceHealth],
    metadata: Snapshot | None = None,
    *,
    now: datetime,
    series: str | None = None,
) -> str:
    context = _context(state, health, metadata, now=now, series=series)
    return _environment().get_template("index.html").render(**context)


def render_group(group: Group, context: Mapping[str, Any]) -> str:
    """Group pages live one directory down, so links need a prefix."""
    return (
        _environment()
        .get_template("group.html")
        .render(**{**context, "group": group, "root": "../"})
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
) -> Path:
    context = _context(state, health, metadata, now=now, series=series)
    env = _environment()

    _clear(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("Generated by cairn build. Safe to delete.\n")
    index = out / "index.html"
    index.write_text(env.get_template("index.html").render(**context), encoding="utf-8")

    groups = [
        *context["uploaders"],
        *context["package_sets"],
        *context["teams"],
    ]
    for group in groups:
        page = out / group.href
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(render_group(group, context), encoding="utf-8")

    if ASSETS.is_dir():
        shutil.copytree(ASSETS, out / "assets", dirs_exist_ok=True)
    return index
