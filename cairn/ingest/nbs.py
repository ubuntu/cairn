"""NBS: binary packages still published that no current source builds.

Primary data under AGENTS.md section 4: the release pocket's Sources and
Packages indexes, the same files the published report is computed from.
That report (static-reports.ubuntu.com/nbs/, from ubuntu-archive-tools'
cron.NBS) is a report generator, so it is an oracle here and never an
input: cairn/oracles/nbs.py measures the divergence.

The rule follows archive-cruft-check, which cron.NBS runs:

- A binary is a candidate when no source in the series' release pocket
  lists it in its Binary field, in any component.
- A candidate whose newest version equals its source's current version is
  set aside ("dubious" there): the source still builds that version, so
  this is not a dropped binary.

Reverse dependencies follow checkrdepends: Pre-Depends, Depends and
Recommends of every binary, and the Build-Depends of every source, in the
release pocket. A dependency holds an NBS binary on an architecture when
the binary is published there at a version that satisfies it, and no
other alternative in its or-group can: no other package published on that
architecture, and not an NBS binary itself, at a satisfying version, and
no such package providing the name. Version constraints count, and a
provided name with no version satisfies only an unversioned dependency
(Debian policy 7.5). Each architecture is decided on its own index: a
replacement published only on amd64 does not satisfy an arm64 dependency,
and an alternative restricted to other architectures ([amd64]) does not
exist here (review on #26). Build dependencies are checked on every
architecture the binary is published on. A dependency on a name an NBS
binary Provides holds that binary when nothing else satisfies it: the
binary is a dependency's only provider. checkrdepends, as the report runs
it, does not follow Provides; on 23 Nov 2026 this changed nothing.

Deliberate differences from checkrdepends:

- An or-group with another alternative that is still available does not
  hold the binary. checkrdepends compares alternatives as text, version
  constraint included, so "a (>= 1) | b" never matched.
- A binary whose name a still-built package Provides, at a version that
  satisfies the dependency, holds nothing. Measured 23 Nov 2026:
  libmaven-exec-plugin-java, renamed to libexec-maven-plugin-java which
  provides the old name; the report lists 14 sources that build-depend on
  it and calls it not removable, cairn calls it removable.
- Build-Depends-Arch counts. checkrdepends reads only Build-Depends and
  Build-Depends-Indep, so a source needing the binary only to build its
  architecture-specific packages was missed.

"Removable" is nbs-report's rule: nothing depends on the binary except
other NBS binaries that are removable themselves.

The debian-installer indexes are not read: they are empty in tenacious
(32 bytes of xz on every component and architecture, 23 Nov 2026).

Measured 23 Nov 2026, tenacious: Packages.xz is 11 MB per architecture for
universe and 1 MB for main, about 95 MB per run across 8 architectures and
4 components; each file parses in about 0.3 s, and is read twice: once to
find the NBS binaries, once per architecture for what needs them, so that
only one architecture's index is held in memory at a time. Sources.gz is shared with
the merges ingester's URLs, so a run fetches it once.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from debian.debian_support import version_compare

from cairn.ingest.archive import (
    UBUNTU_ARCHIVE,
    BinaryPackage,
    Relation,
    SourcePackage,
    decompress,
    newest,
    packages_url,
    parse_packages,
    parse_relations,
    parse_sources,
    sources_url,
)
from cairn.ingest.base import Fetcher, Ingester, Kind, Signal

log = logging.getLogger(__name__)

# The fields whose names can hold a binary in the archive. Recommends is
# kept, as checkrdepends keeps it: dropping a recommended package changes
# what users get installed.
DEPENDENCY_FIELDS = ("Pre-Depends", "Depends", "Recommends")
PACKAGE_FIELDS = (*DEPENDENCY_FIELDS, "Provides")


@dataclass(frozen=True, slots=True)
class Release:
    architectures: tuple[str, ...]
    components: tuple[str, ...]


def parse_release(data: bytes) -> Release:
    """The suite's Release file: which architectures and components exist."""
    found: dict[str, tuple[str, ...]] = {}
    for line in data.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(":")
        if key in ("Architectures", "Components"):
            found[key] = tuple(value.split())
    return Release(
        architectures=found.get("Architectures", ()),
        components=found.get("Components", ()),
    )


# One architecture's indexes: (component, url, compressed body).
Indexes = Mapping[str, list[tuple[str, str, bytes]]]


def _read(
    files: Iterable[tuple[str, str, bytes]], fields: tuple[str, ...] = ()
) -> Iterator[BinaryPackage]:
    for component, url, body in files:
        yield from parse_packages(
            decompress(body, url), component=component, fields=fields
        )


@dataclass(frozen=True, slots=True)
class Orphan:
    name: str
    source: str
    version: str
    source_version: str
    component: str | None
    # For display: ("all",) when architecture-independent, otherwise every
    # index it is published in, at any version.
    architectures: tuple[str, ...]


def orphans(
    copies: Mapping[str, list[tuple[BinaryPackage, str]]],
    sources: Mapping[str, SourcePackage],
) -> dict[str, Orphan]:
    """Candidates (published, in no current source's Binary field), less
    those whose source still builds their newest version."""
    found = {}
    for name, published in copies.items():
        latest = published[0][0]
        for copy, _ in published[1:]:
            if version_compare(copy.version, latest.version) > 0:
                latest = copy
        current = sources.get(latest.source)
        if (
            current is not None
            and version_compare(latest.source_version, current.version) == 0
        ):
            continue
        # Every index it is in, whatever the version: an older copy left on
        # one architecture is still published there (review on #26).
        independent = all(copy.architecture == "all" for copy, _ in published)
        found[name] = Orphan(
            name=name,
            source=latest.source,
            version=latest.version,
            source_version=latest.source_version,
            component=latest.component,
            architectures=(
                ("all",)
                if independent
                else tuple(sorted({where for _, where in published}))
            ),
        )
    return found


@dataclass(slots=True)
class Available:
    """What one architecture offers without its NBS binaries."""

    versions: dict[str, list[str]] = field(default_factory=dict)
    # Provided name -> the provided versions (None when unversioned).
    provided: dict[str, list[str | None]] = field(default_factory=dict)

    def add(self, package: BinaryPackage) -> None:
        self.versions.setdefault(package.name, []).append(package.version)
        for group in parse_relations(package.fields.get("Provides", "")):
            for provided in group:
                self.provided.setdefault(provided.name, []).append(provided.version)

    def satisfies(self, relation: Relation) -> bool:
        if any(relation.satisfied_by(v) for v in self.versions.get(relation.name, ())):
            return True
        return any(
            relation.satisfied_by(v) for v in self.provided.get(relation.name, ())
        )


@dataclass(frozen=True, slots=True)
class Needed:
    """Something that still needs an NBS binary."""

    kind: str  # "binary" or "source"
    package: str
    source: str
    component: str | None
    architectures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Dependents:
    needed_by: dict[str, list[Needed]]
    # NBS name -> binaries outside NBS that provide it, on any architecture.
    providers: dict[str, set[str]]


def dependents(
    sources: Mapping[str, SourcePackage],
    indexes: Indexes,
    nbs: Mapping[str, Orphan],
    provided: Iterable[str] = (),
) -> Dependents:
    """For each NBS binary, what still needs it, decided per architecture.

    Only one architecture's index is read at a time: a dependency is held on
    an architecture by what that architecture publishes. `provided` names
    what the NBS binaries Provide: a dependency on one of those names holds
    the NBS binary that provides it, when nothing else does (review on #26).
    """
    names = set(nbs)
    watched = names | set(provided)
    edges: dict[str, dict[tuple[str, str], set[str]]] = {}
    about: dict[tuple[str, str], tuple[str, str | None]] = {}
    providers: dict[str, set[str]] = {}
    build = [
        (("source", s.name), group)
        for s in sources.values()
        for group in s.build_depends
        if any(r.name in watched for r in group)
    ]
    for s in sources.values():
        about[("source", s.name)] = (s.name, s.component)

    for architecture, files in indexes.items():
        available = Available()
        present: dict[str, list[str]] = {}
        # Provided name -> (NBS binary, provided version) on this architecture.
        present_provided: dict[str, list[tuple[str, str | None]]] = {}
        pending: list[tuple[tuple[str, str], tuple[Relation, ...]]] = []
        for package in _read(files, PACKAGE_FIELDS):
            if package.name in names:
                present.setdefault(package.name, []).append(package.version)
                for group in parse_relations(package.fields.get("Provides", "")):
                    for virtual in group:
                        present_provided.setdefault(virtual.name, []).append(
                            (package.name, virtual.version)
                        )
            else:
                available.add(package)
                for group in parse_relations(package.fields.get("Provides", "")):
                    for provided in group:
                        if provided.name in names:
                            providers.setdefault(provided.name, set()).add(package.name)
            key = ("binary", package.name)
            for name in DEPENDENCY_FIELDS:
                for group in parse_relations(package.fields.get(name, "")):
                    if any(r.name in watched for r in group):
                        pending.append((key, group))
                        about.setdefault(key, (package.source, package.component))
        for key, written in [*pending, *build]:
            # Only the alternatives that exist here: "gone [arm64] |
            # replacement [amd64]" needs gone on arm64 whatever amd64 has.
            group = tuple(r for r in written if r.applies_to(architecture))
            if not group or any(available.satisfies(r) for r in group):
                continue
            for relation in group:
                # The NBS binaries that satisfy it here, by name or by what
                # they provide, at a version that meets the constraint:
                # otherwise removing them changes nothing.
                holders = {
                    relation.name
                    for v in present.get(relation.name, ())
                    if relation.satisfied_by(v)
                } | {
                    holder
                    for holder, v in present_provided.get(relation.name, ())
                    if relation.satisfied_by(v)
                }
                for holder in holders - {key[1] if key[0] == "binary" else ""}:
                    edges.setdefault(holder, {}).setdefault(key, set()).add(
                        architecture
                    )

    needed_by = {
        name: [
            Needed(
                kind,
                rdep,
                *about.get((kind, rdep), (rdep, None)),
                tuple(sorted(arches)),
            )
            for (kind, rdep), arches in sorted(edges.get(name, {}).items())
        ]
        for name in nbs
    }
    return Dependents(needed_by=needed_by, providers=providers)


def removable(needs: Mapping[str, list[Needed]]) -> set[str]:
    """NBS binaries that only other removable NBS binaries need.

    A fixed point rather than nbs-report's recursion: start from every NBS
    binary and drop any that something outside the set needs, until none is.
    """
    keep = set(needs)
    changed = True
    while changed:
        changed = False
        for name in list(keep):
            if any(n.kind == "source" or n.package not in keep for n in needs[name]):
                keep.discard(name)
                changed = True
    return keep


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The release pocket, compressed: Packages are parsed one architecture
    at a time, since every architecture at once is several hundred MB of
    objects."""

    release: Release
    sources: list[SourcePackage]
    packages: dict[str, list[tuple[str, str, bytes]]]


class NbsIngester(Ingester[Snapshot]):
    name = "nbs"

    def __init__(self, fetcher: Fetcher, *, series: str) -> None:
        self.fetcher = fetcher
        self.series = series

    def fetch(self) -> Snapshot:
        release = parse_release(
            self.fetcher.get(f"{UBUNTU_ARCHIVE}/dists/{self.series}/Release")
        )
        if not release.architectures or not release.components:
            raise ValueError(f"no architectures or components in {self.series}")
        sources = []
        for component in release.components:
            url = sources_url(UBUNTU_ARCHIVE, self.series, component)
            raw = decompress(self.fetcher.get(url), url)
            sources.extend(parse_sources(raw, component=component, suite=self.series))
        packages: dict[str, list[tuple[str, str, bytes]]] = {}
        for architecture in release.architectures:
            for component in release.components:
                url = packages_url(self.series, component, architecture)
                packages.setdefault(architecture, []).append(
                    (component, url, self.fetcher.get(url))
                )
        return Snapshot(release=release, sources=sources, packages=packages)

    def parse(self, raw: Snapshot) -> list[Signal]:
        current = newest(raw.sources)
        built = {b for s in current.values() for b in s.binaries}
        copies: dict[str, list[tuple[BinaryPackage, str]]] = {}
        for architecture, files in raw.packages.items():
            for package in _read(files, ("Provides",)):
                if package.name not in built:
                    copies.setdefault(package.name, []).append((package, architecture))
        nbs = orphans(copies, current)
        provided = {
            virtual.name
            for name in nbs
            for copy, _ in copies[name]
            for group in parse_relations(copy.fields.get("Provides", ""))
            for virtual in group
        }
        found = dependents(current, raw.packages, nbs, provided)
        free = removable(found.needed_by)
        signals = []
        for name, orphan in sorted(nbs.items()):
            source = current.get(orphan.source)
            payload: dict[str, object] = {
                "version": orphan.version,
                "source_version": orphan.source_version,
                "component": orphan.component,
                "architectures": list(orphan.architectures),
            }
            # Absent when the source left the archive altogether.
            if source is not None:
                payload["current_version"] = source.version
            if providers := sorted(found.providers.get(name, ())):
                payload["provided_by"] = providers
            needs = found.needed_by.get(name, [])
            binary = [n for n in needs if n.kind == "binary"]
            build = [n for n in needs if n.kind == "source"]
            if binary:
                payload["rdepends"] = [
                    {
                        "package": n.package,
                        "source": n.source,
                        "component": n.component,
                        "architectures": list(n.architectures),
                    }
                    | ({"nbs": True} if n.package in nbs else {})
                    for n in binary
                ]
            if build:
                payload["build_rdepends"] = [
                    {"package": n.package, "component": n.component} for n in build
                ]
            # Only when true, so the common case adds nothing to the log.
            if name in free:
                payload["removable"] = True
            signals.append(
                Signal(
                    kind=Kind.NBS,
                    source_package=orphan.source,
                    binary_package=name,
                    series=self.series,
                    payload=payload,
                    url=f"https://launchpad.net/ubuntu/+source/{orphan.source}",
                )
            )
        return signals


def referenced_packages(signal: Signal) -> set[str]:
    """Source packages an NBS signal names besides its own: what still needs
    the binary. Their owners are the ones to tell, so the ownership refresh
    collects them too (review on #26)."""
    if signal.kind is not Kind.NBS:
        return set()
    payload = signal.payload
    return {
        str(r.get("source") or r.get("package"))
        for r in payload.get("rdepends") or ()
        if r.get("source") or r.get("package")
    } | {
        str(r["package"])
        for r in payload.get("build_rdepends") or ()
        if r.get("package")
    }
