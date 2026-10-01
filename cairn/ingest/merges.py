"""Merge candidates, computed from the Ubuntu and Debian source indexes.

Merges happen only in the development series; stable series receive SRUs and
syncs instead. The caller supplies the series, so passing a stable one produces
signals that mean nothing.

Ubuntu's side is the newest version across the release *and* -proposed
pockets. Every upload to the development series lands in -proposed first, so
reading release alone reports the version before the last upload: a merge
already uploaded but stuck in -proposed looked outstanding, and the board
paired the release version with the -proposed uploader (issue #10). On equal
versions release wins, so the in_proposed flag only appears when -proposed
really is ahead.

The candidate rule diverges from Merge-o-Matic deliberately: cairn requires
Debian to be ahead of what Ubuntu ships, where MoM only requires it to be ahead
of the base. This tracks MoM's published output more closely than MoM's own
pre-filter does; cairn/oracles/merges.py measures the remaining divergence.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

UBUNTU_COMPONENTS = ("main", "universe")
DEBIAN_COMPONENTS = ("main",)
# Order matters: newest() keeps the first of two equal versions.
UBUNTU_POCKETS = ("", "-proposed")
PROPOSED = "-proposed"


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    ubuntu: dict[str, SourcePackage]
    debian: dict[str, SourcePackage]


# Markers that decorate a version without changing the upstream code.
# Only these are stripped: '+' is legitimate in an upstream version, so
# dropping everything after it would hide real movement such as
# 2.0.0+20250617 -> 2.0.0+20260327.
_REPACK_MARKER = re.compile(r"[+~](?:dfsg|ds|repack)\d*")

# Ubuntu snapshot suffix, as in 0.5.6+22.04.20220217.
_UBUNTU_SNAPSHOT = re.compile(r"\+\d{2}\.\d{2}\.\d{8}")

# Debian's downgrade convention: 3.24+really3.22 ships upstream 3.22 under a
# string that still sorts above 3.24. Taking it literally inverts the answer.
_REALLY = re.compile(r".*\+really")


def upstream_release(version: str) -> str:
    """The upstream release, with packaging decoration removed.

    Strips only markers whose meaning is known: Debian's +dfsg,
    0.5.6+22.04.20220217 and 0.5.6+repack are both release 0.5.6, which is
    the honest answer -- neither carries upstream changes the other lacks.

    Anything else after '+' is preserved, so git and date snapshots still
    read as different releases. A +really prefix is dropped rather than
    preserved: it exists to make a downgrade sort upwards, so keeping it
    would report the older upstream as the newer one. '~' is kept
    too: 1.0~rc1 and 1.0 must not compare equal.

    The comparison itself is still python-debian's; only the choice of what
    to compare is made here.
    """
    without_epoch = version.split(":", 1)[-1]
    upstream = (
        without_epoch.rsplit("-", 1)[0] if "-" in without_epoch else without_epoch
    )
    upstream = _REALLY.sub("", upstream)
    upstream = _UBUNTU_SNAPSHOT.sub("", upstream)
    return _REPACK_MARKER.sub("", upstream)


def has_new_upstream(ubuntu: str, debian: str) -> bool:
    """Debian carries a newer upstream release than Ubuntu.

    Strictly newer, not merely different. A handful of candidates qualify
    through an epoch bump or a +really downgrade while Debian's upstream is
    actually older; calling those "new upstream" would contradict the label
    the board puts on them.
    """
    return version_compare(upstream_release(debian), upstream_release(ubuntu)) > 0


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


def is_candidate(ubuntu: SourcePackage, debian: SourcePackage | None) -> bool:
    """Debian must be ahead of what Ubuntu actually ships.

    This is the deliberate divergence from Merge-o-Matic, which compares
    against the base version instead.
    """
    if debian is None or not has_ubuntu_delta(ubuntu.version):
        return False
    return version_compare(debian.version, ubuntu.version) > 0


def candidate_names(
    ubuntu: dict[str, SourcePackage], debian: dict[str, SourcePackage]
) -> set[str]:
    return {
        name
        for name, source in ubuntu.items()
        if is_candidate(source, debian.get(name))
    }


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
        self,
        archive: str,
        suites: tuple[str, ...],
        components: tuple[str, ...],
        compression: str,
    ) -> dict[str, SourcePackage]:
        packages: list[SourcePackage] = []
        for suite in suites:
            for component in components:
                url = sources_url(archive, suite, component, compression)
                raw = decompress(self.fetcher.get(url), url)
                packages.extend(parse_sources(raw, component=component, suite=suite))
        return newest(packages)

    def fetch(self) -> ArchiveSnapshot:
        return ArchiveSnapshot(
            ubuntu=self._load(
                UBUNTU_ARCHIVE,
                tuple(self.series + pocket for pocket in UBUNTU_POCKETS),
                self.ubuntu_components,
                "gz",
            ),
            debian=self._load(
                DEBIAN_ARCHIVE, (self.debian_suite,), self.debian_components, "xz"
            ),
        )

    def parse(self, raw: ArchiveSnapshot) -> list[Signal]:
        signals = []
        for name, ubuntu in raw.ubuntu.items():
            debian = raw.debian.get(name)
            if not is_candidate(ubuntu, debian):
                continue
            payload = {
                "ubuntu_version": ubuntu.version,
                "debian_version": debian.version,
                "base_version": base_version(ubuntu.version),
                "component": ubuntu.component,
                "debian_suite": self.debian_suite,
                "independent_lineage": is_independent_lineage(ubuntu.version),
                # Distinguishes a packaging-only difference from real merge
                # work: the two are very different jobs.
                "new_upstream": has_new_upstream(ubuntu.version, debian.version),
            }
            # Only present when true. Release is the common case, and an
            # always-present False would change every payload and append one
            # meaningless UPDATED event per signal to the log.
            if ubuntu.suite is not None and ubuntu.suite.endswith(PROPOSED):
                payload["in_proposed"] = True
            signals.append(
                Signal(
                    kind=Kind.NEEDS_MERGE,
                    source_package=name,
                    series=self.series,
                    payload=payload,
                    url=f"https://launchpad.net/ubuntu/+source/{name}",
                )
            )
        return signals
