"""Reading Debian-format archive indexes: Sources and Packages.

Knows about the index files and nothing about merges, NBS or any signal kind.

Packages files are large (universe/binary-amd64 for tenacious, 20 Nov 2026:
11.4 MB of xz, 70 MB of text, 69,470 stanzas), so they are read with a
small stanza parser that keeps only the fields asked for: 0.3 s per file,
where deb822 builds every field of every stanza. Sources keep deb822.

https rather than http: cairn does not verify the Release file's GPG
signature, so TLS is its only integrity guarantee.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from debian.deb822 import Sources
from debian.debian_support import version_compare

UBUNTU_ARCHIVE = "https://archive.ubuntu.com/ubuntu"
# Every architecture but these is published on ports.ubuntu.com. amd64v3 is
# on the main archive: measured from the tenacious Release file and indexes.
UBUNTU_PORTS = "https://ports.ubuntu.com/ubuntu-ports"
MAIN_ARCHIVE_ARCHES = frozenset({"amd64", "amd64v3", "i386"})
DEBIAN_ARCHIVE = "https://deb.debian.org/debian"

# Relationship fields of a source, all of which can need a binary to build.
BUILD_RELATIONS = ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch")


@dataclass(frozen=True, slots=True)
class SourcePackage:
    name: str
    version: str
    binaries: tuple[str, ...] = ()
    component: str | None = None
    # The suite the stanza was read from, e.g. "stonking-proposed".
    suite: str | None = None
    # Build-Depends, -Indep and -Arch together, as or-groups.
    build_depends: tuple[tuple[Relation, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class BinaryPackage:
    """One stanza of a Packages file."""

    name: str
    version: str
    # The stanza's Architecture: "all", or the architecture it was built on.
    architecture: str
    # From the Source field, which names the version only when it differs
    # from the binary's (a binNMU); otherwise the binary's own.
    source: str
    source_version: str
    component: str | None = None
    # Only the fields the caller asked for, raw.
    fields: Mapping[str, str] = field(default_factory=dict)


def decompress(raw: bytes, url: str) -> bytes:
    if url.endswith(".gz"):
        return gzip.decompress(raw)
    if url.endswith(".xz"):
        return lzma.decompress(raw)
    if url.endswith(".bz2"):
        return bz2.decompress(raw)
    return raw


def parse_sources(
    data: bytes, *, component: str | None = None, suite: str | None = None
) -> Iterator[SourcePackage]:
    for stanza in Sources.iter_paragraphs(data, use_apt_pkg=False):
        name = stanza.get("Package")
        version = stanza.get("Version")
        if not name or not version:
            continue
        binaries = tuple(
            b.strip() for b in (stanza.get("Binary") or "").split(",") if b.strip()
        )
        build_depends = tuple(
            group
            for key in BUILD_RELATIONS
            for group in parse_relations(stanza.get(key) or "")
        )
        yield SourcePackage(
            name=name,
            version=version,
            binaries=binaries,
            component=component,
            suite=suite,
            build_depends=build_depends,
        )


def stanzas(data: bytes, fields: Collection[str]) -> Iterator[dict[str, str]]:
    """Each paragraph of a Debian control file, keeping only `fields`.

    Field names are matched as the archive writes them (Package, Depends);
    continuation lines are folded onto their field with a space.
    """
    wanted = {name.encode() for name in fields}
    for paragraph in data.split(b"\n\n"):
        found: dict[bytes, bytes] = {}
        key: bytes | None = None
        for line in paragraph.split(b"\n"):
            if not line:
                continue
            if line[0] in (0x20, 0x09):
                if key is not None:
                    found[key] += b" " + line.strip()
                continue
            name, _, value = line.partition(b":")
            key = name if name in wanted else None
            if key is not None:
                found[key] = value.strip()
        if found:
            yield {k.decode(): v.decode("utf-8", "replace") for k, v in found.items()}


_SOURCE_VERSION = re.compile(r"^(\S+)\s*\((.+)\)$")


def parse_packages(
    data: bytes, *, component: str | None = None, fields: Collection[str] = ()
) -> Iterator[BinaryPackage]:
    """Binary packages, with `fields` kept raw in each one's .fields."""
    extra = set(fields)
    for stanza in stanzas(
        data, {"Package", "Version", "Architecture", "Source", *extra}
    ):
        name = stanza.get("Package")
        version = stanza.get("Version")
        if not name or not version:
            continue
        source, source_version = name, version
        if declared := stanza.get("Source"):
            match = _SOURCE_VERSION.match(declared)
            source, source_version = (
                (match.group(1), match.group(2)) if match else (declared, version)
            )
        yield BinaryPackage(
            name=name,
            version=version,
            architecture=stanza.get("Architecture") or "",
            source=source,
            source_version=source_version,
            component=component,
            fields={k: v for k, v in stanza.items() if k in extra},
        )


# A relationship: the package name, its architecture qualifier (foo:any,
# dropped), its version constraint and its architecture restriction
# ([amd64 arm64] or [!i386]) if any. A build profile (<!nocheck>) may
# follow; it is dropped, so a test-only dependency still counts.
_RELATION = re.compile(
    r"^\s*([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?"
    r"\s*(?:\(\s*(<<|<=|>=|>>|=|<|>)\s*([^\s)]+)\s*\))?"
    r"\s*(?:\[([^\]]*)\])?"
)
# dpkg's (abi, libc, os, cpu) tuple for each architecture Ubuntu publishes,
# from dpkg's tupletable. Restrictions match on these, as dpkg's debarch_is
# does. amd64v3 is a build of amd64 (its Packages stanzas say Architecture:
# amd64) and dpkg does not know it, so restrictions evaluate as amd64 there.
_DPKG_ARCH = {"amd64v3": "amd64"}
_DPKG_TUPLE = {
    "amd64": ("base", "gnu", "linux", "amd64"),
    "i386": ("base", "gnu", "linux", "i386"),
    "arm64": ("base", "gnu", "linux", "arm64"),
    "armhf": ("eabihf", "gnu", "linux", "arm"),
    "ppc64el": ("base", "gnu", "linux", "ppc64el"),
    "riscv64": ("base", "gnu", "linux", "riscv64"),
    "s390x": ("base", "gnu", "linux", "s390x"),
}


def arch_matches(architecture: str, restriction: str) -> bool:
    """dpkg's debarch_is: whether `restriction` (an architecture name or
    wildcard) covers `architecture`. A wildcard has "any" in it and is
    padded on the left to four parts (linux-any is any-any-linux-any); any
    other name is an architecture, with a "linux-" prefix dropped, so
    linux-arm is the old arm ABI and does not cover armhf. Checked against
    dpkg-architecture -i for every published architecture."""
    architecture = _DPKG_ARCH.get(architecture, architecture)
    if restriction in (architecture, "any"):
        return True
    real = _DPKG_TUPLE.get(architecture)
    parts = restriction.split("-", 3)
    if real is None or "any" not in parts:
        if restriction.startswith("linux-"):
            return restriction[len("linux-") :].split("-")[0] == architecture
        return False
    wildcard = ("any",) * (4 - len(parts)) + tuple(parts)
    return all(w in ("any", r) for w, r in zip(wildcard, real, strict=True))


# "<" and ">" are the obsolete spellings of "<=" and ">=" (Debian policy 7.1).
_OPERATORS = {"<": "<=", ">": ">="}


@dataclass(frozen=True, slots=True)
class Relation:
    """One alternative of a Depends-style field, or one Provides entry."""

    name: str
    operator: str | None = None
    version: str | None = None
    # The architecture restriction as written, e.g. ("amd64",) or
    # ("!i386",); empty when it applies everywhere.
    architectures: tuple[str, ...] = ()

    def applies_to(self, architecture: str) -> bool:
        """Whether this alternative exists on `architecture` (Debian policy
        7.1): it matches one of the listed architectures, or, when they are
        negated (!i386), none of them."""
        if not self.architectures:
            return True
        negated = [a[1:] for a in self.architectures if a.startswith("!")]
        if negated:
            return not any(arch_matches(architecture, a) for a in negated)
        return any(arch_matches(architecture, a) for a in self.architectures)

    def satisfied_by(self, version: str | None) -> bool:
        """Whether a package at `version` satisfies this. A provided name
        with no version (version None) satisfies only an unversioned
        relationship, as Debian policy 7.5 says."""
        if self.operator is None:
            return True
        if version is None or self.version is None:
            return False
        order = version_compare(version, self.version)
        return {
            "<<": order < 0,
            "<=": order <= 0,
            "=": order == 0,
            ">=": order >= 0,
            ">>": order > 0,
        }[self.operator]


def parse_relations(text: str) -> list[tuple[Relation, ...]]:
    """A Depends-style field as or-groups, constraints kept.

    "a (>= 1) | b, c [amd64]" is [(a >= 1, b), (c [amd64],)]. Build
    profiles are dropped: a dependency only needed to run tests still
    matters.
    """
    groups = []
    for clause in text.split(","):
        group = tuple(
            Relation(
                match.group(1),
                _OPERATORS.get(match.group(2), match.group(2)),
                match.group(3),
                tuple((match.group(4) or "").split()),
            )
            for alternative in clause.split("|")
            if (match := _RELATION.match(alternative))
        )
        if group:
            groups.append(group)
    return groups


def relations(text: str) -> list[tuple[str, ...]]:
    """A Depends-style field as or-groups of names, constraints dropped."""
    return [tuple(r.name for r in group) for group in parse_relations(text)]


def newest(packages: Iterable[SourcePackage]) -> dict[str, SourcePackage]:
    """Collapse to one entry per source name, keeping the highest version.

    On a tie the first one seen is kept, so callers control which suite wins
    by the order they pass them in.
    """
    best: dict[str, SourcePackage] = {}
    for package in packages:
        current = best.get(package.name)
        if current is None or version_compare(package.version, current.version) > 0:
            best[package.name] = package
    return best


def sources_url(
    archive: str, suite: str, component: str, compression: str = "gz"
) -> str:
    return f"{archive}/dists/{suite}/{component}/source/Sources.{compression}"


def packages_url(
    suite: str, component: str, architecture: str, compression: str = "xz"
) -> str:
    """An Ubuntu Packages index, on the main archive or ports by arch."""
    archive = UBUNTU_ARCHIVE if architecture in MAIN_ARCHIVE_ARCHES else UBUNTU_PORTS
    return (
        f"{archive}/dists/{suite}/{component}/binary-{architecture}/"
        f"Packages.{compression}"
    )
