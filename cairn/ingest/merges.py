"""Merge candidates, computed from the Ubuntu and Debian source indexes.

Merges happen only in the development series; stable series receive SRUs and
syncs instead. The caller supplies the series, so passing a stable one produces
signals that mean nothing.

The candidate rule diverges from Merge-o-Matic deliberately: cairn requires
Debian to be ahead of what Ubuntu ships, where MoM only requires it to be ahead
of the base. This tracks MoM's published output more closely than MoM's own
pre-filter does; cairn/oracles/merges.py measures the remaining divergence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from debian.debian_support import version_compare

from cairn.ingest.archive import (
    DEBIAN_ARCHIVE,
    UBUNTU_ARCHIVE,
    SourcePackage,
    decompress,
    newest,
    parse_sources,
    sources_url,
)
from cairn.ingest.base import Fetcher, Ingester, Kind, Signal

UBUNTU_COMPONENTS = ("main", "universe")
DEBIAN_COMPONENTS = ("main",)


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    ubuntu: dict[str, SourcePackage]
    debian: dict[str, SourcePackage]


_REVISION_TAIL = re.compile(r"[\d.]*(?:[~+][A-Za-z0-9.+~]+)?")


def _strip_suffix(text: str, suffix: str) -> str:
    index = text.rfind(suffix)
    if index == -1:
        return text
    tail = text[index + len(suffix) :]
    return text[:index] if _REVISION_TAIL.fullmatch(tail) else text


def base_version(version: str) -> str:
    """The Debian version an Ubuntu version derives from.

    Follows Merge-o-Matic's get_base() -- strip a buildN suffix, then an
    ubuntuN suffix, treating a trailing bare "-" as revision 0 -- but also
    accepts backport and PPA tails (ubuntu1~24.04.3, ubuntu9+24.04.2). MoM
    allows only digits and dots there, so it leaves those unstripped and
    returns the malformed "1.0.5-3build1~" for 1.0.5-3build1~ubuntu0.24.04.1.
    """
    base = _strip_suffix(_strip_suffix(version, "build"), "ubuntu")
    return base + "0" if base.endswith("-") else base


def has_ubuntu_delta(version: str) -> bool:
    """Only an ubuntu revision prevents syncing; a buildN rebuild does not.

    Delta is the presence of the ubuntu string in the revision, as the Ubuntu
    project defines it. ubuntu0.1 is delta: the 0 records that there was none
    *before* that first SRU change, not that the version carries none.
    """
    without_build = _strip_suffix(version, "build")
    return _strip_suffix(without_build, "ubuntu") != without_build


def is_independent_lineage(version: str) -> bool:
    """True when Ubuntu packaged it rather than merging from a Debian revision.

    A -0 Debian revision means the upload is not based on any Debian packaging
    version. Distinct from an ubuntu0 revision, which is ordinary delta.

    A native package has no Debian revision at all, but is still derived from
    Debian's native version, so it is not independent.
    """
    base = base_version(version)
    return "-" in base and base.rsplit("-", 1)[1] == "0"


class MergesIngester(Ingester[ArchiveSnapshot]):
    name = "merges"

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        series: str,
        ubuntu_components: tuple[str, ...] = UBUNTU_COMPONENTS,
        debian_suite: str = "unstable",
        debian_components: tuple[str, ...] = DEBIAN_COMPONENTS,
    ) -> None:
        self.fetcher = fetcher
        self.series = series
        self.ubuntu_components = ubuntu_components
        self.debian_suite = debian_suite
        self.debian_components = debian_components

    def _load(
        self, archive: str, suite: str, components: tuple[str, ...], compression: str
    ) -> dict[str, SourcePackage]:
        packages: list[SourcePackage] = []
        for component in components:
            url = sources_url(archive, suite, component, compression)
            raw = decompress(self.fetcher.get(url), url)
            packages.extend(parse_sources(raw, component=component))
        return newest(packages)

    def fetch(self) -> ArchiveSnapshot:
        return ArchiveSnapshot(
            ubuntu=self._load(
                UBUNTU_ARCHIVE, self.series, self.ubuntu_components, "gz"
            ),
            debian=self._load(
                DEBIAN_ARCHIVE, self.debian_suite, self.debian_components, "xz"
            ),
        )

    def parse(self, raw: ArchiveSnapshot) -> list[Signal]:
        signals = []
        for name, ubuntu in raw.ubuntu.items():
            debian = raw.debian.get(name)
            if debian is None or not has_ubuntu_delta(ubuntu.version):
                continue
            if version_compare(debian.version, ubuntu.version) <= 0:
                continue
            signals.append(
                Signal(
                    kind=Kind.NEEDS_MERGE,
                    source_package=name,
                    series=self.series,
                    payload={
                        "ubuntu_version": ubuntu.version,
                        "debian_version": debian.version,
                        "base_version": base_version(ubuntu.version),
                        "component": ubuntu.component,
                        "debian_suite": self.debian_suite,
                        "independent_lineage": is_independent_lineage(ubuntu.version),
                    },
                    url=f"https://launchpad.net/ubuntu/+source/{name}",
                )
            )
        return signals
