"""Package ownership and publication metadata.

This is deliberately *not* part of a signal. A signal is an observation that
something needs attention; who last uploaded a package is a property of the
package, true whether or not anything is wrong with it.

Keeping them apart matters because the signal log is append-only. Folding
metadata into a payload means a Launchpad outage appends an event per signal
stripping the columns, then appends another per signal when it recovers, and
the log permanently records changes that never happened.

So metadata is a whole-file snapshot instead. It is derived data and can be
refetched at any time, which is what makes overwriting it safe -- and what
makes a failed refresh harmless: the previous file stays exactly where it is.
Refreshes are all-or-nothing for the same reason. A partial answer, where one
team returned 503, would quietly reassign ownership rather than keep the last
known good value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairn.ingest import debian_uploads, packagesets, publications, teams
from cairn.ingest.base import Fetcher


class IncompleteRefresh(RuntimeError):
    """A source could not be refreshed in full, so nothing is written."""


@dataclass(frozen=True, slots=True)
class PublishedVersion:
    """One version of a package as published in one pocket of the series."""

    version: str
    pocket: str | None = None
    # The changelog person, never the sponsor. See publications.Publication.
    uploader: str | None = None
    # The sponsor when there is one. Only ever tested for None, which marks a
    # Debian sync or a copy; never displayed in place of the uploader.
    signer: str | None = None
    published: str | None = None

    @property
    def synced(self) -> bool:
        return self.signer is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "pocket": self.pocket,
            "uploader": self.uploader,
            "signer": self.signer,
            "published": self.published,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PublishedVersion:
        return cls(
            version=raw.get("version") or "",
            pocket=raw.get("pocket"),
            uploader=raw.get("uploader"),
            signer=raw.get("signer"),
            published=raw.get("published"),
        )


@dataclass(frozen=True, slots=True)
class PackageMetadata:
    uploader: str | None = None
    published: str | None = None
    debian_uploaded: str | None = None
    package_sets: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    # Every current publication, so a row names the uploader of the version
    # it actually shows rather than whichever upload is newest. Snapshots
    # written before this existed have none, and fall back to the fields above.
    publications: tuple[PublishedVersion, ...] = ()

    def publication(self, version: str | None) -> PublishedVersion | None:
        for found in self.publications:
            if found.version == version:
                return found
        return None

    def uploader_of(self, version: str | None) -> str | None:
        """Who uploaded this version: the changelog person, never the sponsor.

        With per-version publications recorded, only an exact match counts.
        A version absent from them, say because this snapshot predates the
        upload, has no known uploader: borrowing the newest one would route
        the row to someone who did not make it. Only a snapshot written
        before publications were recorded falls back to the newest uploader,
        since that is all it knows.
        """
        if not self.publications:
            return self.uploader
        found = self.publication(version)
        return found.uploader if found else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "uploader": self.uploader,
            "published": self.published,
            "debian_uploaded": self.debian_uploaded,
            "package_sets": list(self.package_sets),
            "teams": list(self.teams),
            "publications": [p.as_dict() for p in self.publications],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PackageMetadata:
        return cls(
            uploader=raw.get("uploader"),
            published=raw.get("published"),
            debian_uploaded=raw.get("debian_uploaded"),
            package_sets=tuple(raw.get("package_sets") or ()),
            teams=tuple(raw.get("teams") or ()),
            publications=tuple(
                PublishedVersion.from_dict(p) for p in raw.get("publications") or ()
            ),
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    collected_at: datetime | None = None
    packages: dict[str, PackageMetadata] = field(default_factory=dict)

    def get(self, package: str) -> PackageMetadata:
        return self.packages.get(package, PackageMetadata())

    def age(self, now: datetime) -> int | None:
        """Days since the last complete refresh. None when never collected."""
        if self.collected_at is None:
            return None
        return max((now - self.collected_at).days, 0)


def collect(
    fetcher: Fetcher,
    series: str,
    *,
    keep: Container[str] | None = None,
    candidates: Mapping[str, tuple[str, str]] | None = None,
    debian_suite: str = "unstable",
    connect: Callable[[], object] | None = None,
    now: datetime | None = None,
) -> Snapshot:
    """Fetch every axis, or raise. Callers keep the previous snapshot on error."""
    candidates = candidates or {}
    try:
        published = publications.fetch_all(fetcher, series, keep=keep)
        sets = packagesets.fetch(fetcher, series, strict=True)
        subscribed = teams.fetch(fetcher, keep=keep, strict=True)
        uploaded = debian_uploads.fetch(candidates, suite=debian_suite, connect=connect)
    except Exception as exc:
        raise IncompleteRefresh(f"{type(exc).__name__}: {exc}") from exc

    names = set(published) | set(sets) | set(subscribed) | set(candidates)
    packages = {}
    for name in names:
        every = published.get(name, ())
        publication = publications.latest(every)
        debian_date = uploaded.get(name)
        packages[name] = PackageMetadata(
            uploader=publication.uploader if publication else None,
            published=(
                publication.uploaded.isoformat()
                if publication and publication.uploaded
                else None
            ),
            debian_uploaded=debian_date.isoformat() if debian_date else None,
            package_sets=tuple(sets.get(name, ())),
            teams=tuple(subscribed.get(name, ())),
            publications=tuple(
                PublishedVersion(
                    version=p.version,
                    pocket=p.pocket,
                    uploader=p.uploader,
                    signer=p.signer,
                    published=p.uploaded.isoformat() if p.uploaded else None,
                )
                for p in sorted(every, key=lambda p: (p.pocket or "", p.version))
            ),
        )
    return Snapshot(collected_at=now or datetime.now(UTC), packages=packages)


def save(path: Path, snapshot: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "collected_at": (
            snapshot.collected_at.isoformat() if snapshot.collected_at else None
        ),
        "packages": {
            name: meta.as_dict() for name, meta in sorted(snapshot.packages.items())
        },
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def load(path: Path) -> Snapshot:
    if not path.exists():
        return Snapshot()
    raw = json.loads(path.read_text())
    collected = raw.get("collected_at")
    return Snapshot(
        collected_at=datetime.fromisoformat(collected) if collected else None,
        packages={
            name: PackageMetadata.from_dict(meta)
            for name, meta in (raw.get("packages") or {}).items()
        },
    )
