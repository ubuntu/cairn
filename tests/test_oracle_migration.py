from __future__ import annotations

import lzma
from datetime import UTC, datetime
from pathlib import Path

from cairn.ingest.migration import EXCUSES_URL
from cairn.oracles.migration import ORACLE_URL, check, matching, parse

FIXTURES = Path(__file__).parent / "fixtures"
CSV = (FIXTURES / "update_excuses.csv").read_bytes()
EXCUSES = (FIXTURES / "update_excuses.yaml").read_bytes()

# The YAML fixture's generated-date; the CSV fixture's last row is that run.
GENERATED = datetime(2026, 10, 1, 7, 18, 41, 241683, tzinfo=UTC)


def test_parses_britneys_run_history():
    rows = parse(CSV)
    assert len(rows) == 4
    assert rows[-1].blocked == 586
    assert rows[-1].candidates == 19


def test_picks_the_row_for_the_run_cairn_read_not_the_newest():
    """Measured: an older YAML beside a newer CSV row would read as drift."""
    rows = parse(CSV)
    earlier = datetime(2026, 10, 1, 6, 21, 3, tzinfo=UTC)
    assert matching(rows, earlier).blocked == 587
    assert matching(rows, GENERATED).blocked == 586


def test_no_row_from_the_same_run_is_no_comparison():
    assert matching(parse(CSV), datetime(2026, 9, 1, tzinfo=UTC)) is None


class Fetcher:
    def get(self, url):
        if url == ORACLE_URL:
            return CSV
        if url == EXCUSES_URL:
            return lzma.compress(EXCUSES)
        raise AssertionError(url)


def test_check_compares_like_with_like():
    result = check(Fetcher(), series="stonking", now=GENERATED)
    assert result.matched_run
    assert result.oracle_at == GENERATED.replace(microsecond=0)
    # The fixture is trimmed to 9 blocked entries; the CSV counts the archive.
    assert result.cairn == 9
    assert result.published == 586
    assert result.difference == 9 - 586
