"""Reading Debian-format source indexes.

Knows about Sources files and nothing about merges, NBS or any signal kind.

https rather than http: cairn does not verify the Release file's GPG
signature, so TLS is its only integrity guarantee.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from debian.deb822 import Sources
from debian.debian_support import version_compare

UBUNTU_ARCHIVE = "https://archive.ubuntu.com/ubuntu"
DEBIAN_ARCHIVE = "https://deb.debian.org/debian"


@dataclass(frozen=True, slots=True)
class SourcePackage:
    name: str
    version: str
    binaries: tuple[str, ...] = ()
    component: str | None = None


def decompress(raw: bytes, url: str) -> bytes:
    if url.endswith(".gz"):
        return gzip.decompress(raw)
    if url.endswith(".xz"):
        return lzma.decompress(raw)
    if url.endswith(".bz2"):
        return bz2.decompress(raw)
    return raw


def parse_sources(
    data: bytes, *, component: str | None = None
) -> Iterator[SourcePackage]:
    for stanza in Sources.iter_paragraphs(data, use_apt_pkg=False):
        name = stanza.get("Package")
        version = stanza.get("Version")
        if not name or not version:
            continue
        binaries = tuple(
            b.strip() for b in (stanza.get("Binary") or "").split(",") if b.strip()
        )
        yield SourcePackage(
            name=name, version=version, binaries=binaries, component=component
        )


def newest(packages: Iterable[SourcePackage]) -> dict[str, SourcePackage]:
    """Collapse to one entry per source name, keeping the highest version."""
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
