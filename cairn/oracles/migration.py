"""Divergence between cairn's migration signals and britney's own count.

britney appends one row per run to update_excuses.csv:
time (ms since epoch), valid candidates, not considered, total, median age,
backlog. "not considered" is the number of blocked uploads, so it is the
count cairn must reproduce. A shortfall means the ingester dropped entries,
for example by skipping items whose name is not their source's.

The row must be the one for the run cairn read. Britney runs every few hours
and both files are fetched separately: measured on 1 Oct 2026, the YAML from
07:18 UTC said 587 while the CSV's newest row already said 586. Comparing
against the newest row would report a divergence that is only timing.

update_excuses_by_team.yaml would also validate the "tests holding back
others" inversion, but it carries !!python/object tags (61 measured) and
cannot be read with a safe loader. It is deliberately not read.

Recorded divergence, 1 Oct 2026, britney run of 07:18:41 UTC: cairn 587,
CSV 586, one unexplained. The YAML has 606 entries where the CSV row says 605
in total, so the CSV omits one entry rather than cairn adding one. eclib, the
only entry with britney's "cruft" reason, is a plausible candidate but this
has not been confirmed against the code that writes the CSV.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cairn.ingest.base import Fetcher
from cairn.ingest.migration import PROPOSED_MIGRATION, MigrationIngester
from cairn.ingest.series import development_series

ORACLE_URL = f"{PROPOSED_MIGRATION}/update_excuses.csv"

# Britney writes the CSV row and the YAML in one run, seconds apart.
SAME_RUN = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Row:
    at: datetime
    candidates: int
    blocked: int


@dataclass(frozen=True, slots=True)
class Divergence:
    generated: datetime | None
    oracle_at: datetime | None
    cairn: int
    published: int | None

    @property
    def matched_run(self) -> bool:
        return self.published is not None

    @property
    def difference(self) -> int | None:
        return None if self.published is None else self.cairn - self.published

    @property
    def agree(self) -> bool:
        return self.difference == 0


def parse(raw: bytes) -> list[Row]:
    rows = []
    for record in csv.DictReader(io.StringIO(raw.decode())):
        try:
            rows.append(
                Row(
                    at=datetime.fromtimestamp(int(record["time"]) / 1000, tz=UTC),
                    candidates=int(record["valid candidates"]),
                    blocked=int(record["not considered"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return rows


def matching(rows: list[Row], generated: datetime | None) -> Row | None:
    if generated is None or not rows:
        return None
    best = min(rows, key=lambda row: abs(row.at - generated))
    return best if abs(best.at - generated) <= SAME_RUN else None


def check(
    fetcher: Fetcher,
    *,
    series: str | None = None,
    now: datetime | None = None,
) -> Divergence:
    series = series or development_series(fetcher)
    clock = (lambda: now) if now is not None else (lambda: datetime.now(UTC))
    ingester = MigrationIngester(fetcher, series=series, now=clock)
    raw = ingester.fetch()
    signals = ingester.parse(raw)
    row = matching(parse(fetcher.get(ORACLE_URL)), raw.generated)
    return Divergence(
        generated=raw.generated,
        oracle_at=row.at if row else None,
        cairn=len(signals),
        published=row.blocked if row else None,
    )
