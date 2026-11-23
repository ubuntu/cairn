"""The NBS board's rows: binaries no current source builds, and what still
needs them.

Read from the log like the other boards; the payload is written by
cairn/ingest/nbs.py. Who acts on an NBS binary depends on what needs it:
while something does, the owners of those packages (a rebuild against the
new library, or a port); once nothing does, an archive admin removes it.
So the rows carry what needs them, and package pages list both sides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cairn.core.log import SignalState
from cairn.ingest.base import Kind


@dataclass(frozen=True, slots=True)
class NeededBy:
    """One package that still needs an NBS binary."""

    # A binary name, or for a build dependency the source's name.
    package: str
    # The source package to go to: the binary's source, or itself.
    source: str
    component: str | None
    architectures: tuple[str, ...]
    build: bool = False
    # Itself an NBS binary.
    nbs: bool = False
    # Itself an NBS binary that can be removed: it goes when this one does,
    # so it holds nothing. One still needed is work like any other
    # (review on #26: libcamel-1.2-64t64, needed only by NBS binaries that
    # are still needed themselves, showed "still needed by" no one).
    leaving: bool = False


@dataclass(frozen=True, slots=True)
class NbsRow:
    binary: str
    # The source that used to build it.
    source: str
    version: str
    # The source's version now; None when the source has left the archive.
    current_version: str | None
    component: str | None
    architectures: tuple[str, ...]
    needed_by: tuple[NeededBy, ...]
    # Still-built binaries that provide its name, which satisfy whatever
    # needed it.
    provided_by: tuple[str, ...]
    removable: bool
    first_seen: datetime

    @property
    def anchor(self) -> str:
        # Binary names are [a-z0-9][a-z0-9+.-]*, like source names, so they
        # never clash with the board's section ids, which contain "_".
        return self.binary

    @property
    def needing(self) -> tuple[NeededBy, ...]:
        """What needs it and is not leaving with it: the work left."""
        return tuple(n for n in self.needed_by if not n.leaving)

    @property
    def still_needed_nbs(self) -> frozenset[str]:
        """What needs it that is NBS itself and still needed: removing this
        one waits for that one."""
        return frozenset(n.package for n in self.needing if n.nbs)

    @property
    def sources_needing(self) -> tuple[str, ...]:
        return tuple(sorted({n.source for n in self.needing}))

    def where(self, need: NeededBy) -> tuple[str, tuple[str, ...]] | None:
        """Where `need` needs it, when that is not everywhere the binary is
        published: ("on", arches) or, when only a few are missing,
        ("not on", arches). None when it is needed everywhere."""
        if "all" in self.architectures or need.build:
            return None
        published = set(self.architectures)
        missing = published - set(need.architectures)
        if not missing:
            return None
        present = published & set(need.architectures)
        if len(missing) < len(present):
            return ("not on", tuple(sorted(missing)))
        return ("on", tuple(sorted(present)))

    def needs_by_source(
        self,
    ) -> list[
        tuple[str, list[tuple[tuple[str, ...], tuple[str, tuple[str, ...]] | None]]]
    ]:
        """What needs it, by the source to go to: binaries needing it in
        the same places together, then "to build" as a name of its own."""
        found: dict[str, dict[object, list[str]]] = {}
        for need in self.needing:
            key = ("build", None) if need.build else ("binary", self.where(need))
            found.setdefault(need.source, {}).setdefault(key, []).append(need.package)
        return [
            (
                source,
                [
                    (("to build",) if kind == "build" else tuple(names), where)
                    for (kind, where), names in groups.items()
                ],
            )
            for source, groups in sorted(found.items())
        ]


def _needed_by(
    payload: Mapping[str, Any], leaving: frozenset[str] = frozenset()
) -> tuple[NeededBy, ...]:
    binary = tuple(
        NeededBy(
            package=str(r.get("package", "")),
            source=str(r.get("source") or r.get("package", "")),
            component=r.get("component"),
            architectures=tuple(r.get("architectures") or ()),
            nbs=bool(r.get("nbs")),
            leaving=bool(r.get("nbs")) and r.get("package") in leaving,
        )
        for r in payload.get("rdepends") or ()
    )
    build = tuple(
        NeededBy(
            package=str(r.get("package", "")),
            source=str(r.get("package", "")),
            component=r.get("component"),
            architectures=(),
            build=True,
        )
        for r in payload.get("build_rdepends") or ()
    )
    return binary + build


def nbs_rows(state: Mapping[str, SignalState]) -> list[NbsRow]:
    """By source, then binary: a source's binaries leave together."""
    active = [
        st for st in state.values() if st.is_active and st.signal.kind is Kind.NBS
    ]
    leaving = frozenset(
        st.signal.binary_package or ""
        for st in active
        if st.signal.payload.get("removable")
    )
    rows = []
    for st in active:
        payload = st.signal.payload
        rows.append(
            NbsRow(
                binary=st.signal.binary_package or "",
                source=st.signal.source_package,
                version=str(payload.get("version") or ""),
                current_version=payload.get("current_version"),
                component=payload.get("component"),
                architectures=tuple(payload.get("architectures") or ()),
                needed_by=_needed_by(payload, leaving),
                provided_by=tuple(payload.get("provided_by") or ()),
                removable=bool(payload.get("removable")),
                first_seen=st.first_seen,
            )
        )
    return sorted(rows, key=lambda r: (r.source, r.binary))


def needs_nbs(rows: list[NbsRow]) -> dict[str, list[tuple[NbsRow, NeededBy]]]:
    """Turn "this NBS binary is needed by" around: source package -> the
    NBS binaries it still needs. The report lists them under the binary;
    the person who has to rebuild or port is looking for their package."""
    found: dict[str, list[tuple[NbsRow, NeededBy]]] = {}
    for row in rows:
        for need in row.needing:
            found.setdefault(need.source, []).append((row, need))
    return found


@dataclass(frozen=True, slots=True)
class NbsOverview:
    total: int
    removable: int
    # Distinct source packages that need an NBS binary.
    sources_needing: int
    opened_recently: int
    resolved_recently: int
