from __future__ import annotations

import gzip
import json
import lzma
from pathlib import Path

import pytest

from cairn.cli import _fetcher, build_parser, main
from cairn.core import health
from cairn.core import log as logmod
from cairn.ingest.http import FileCache, ReadOnlyCache

FIXTURES = Path(__file__).parent / "fixtures"
UBUNTU_SOURCES = (FIXTURES / "ubuntu_sources").read_bytes()
DEBIAN_SOURCES = (FIXTURES / "debian_sources").read_bytes()

# Merge candidates the fixtures yield, as asserted in test_oracle_merges.py.
FIXTURE_MERGES = 8


def without(package: str, data: bytes = UBUNTU_SOURCES) -> bytes:
    """Drop one stanza, leaving the rest intact."""
    needle = f"\nPackage: {package}\n".encode()
    kept = [s for s in data.split(b"\n\n") if needle not in b"\n" + s + b"\n"]
    return b"\n\n".join(kept)


class StubFetcher:
    """Serves the archive indexes and the Launchpad series collection."""

    def __init__(self, *, ubuntu=UBUNTU_SOURCES, fail=None):
        self.ubuntu = ubuntu
        self.fail = fail
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail and "archive.ubuntu.com" in url:
            raise self.fail
        if "api.launchpad.net" in url:
            return json.dumps(
                {"entries": [{"name": "stonking", "status": "Active Development"}]}
            ).encode()
        if "archive.ubuntu.com" in url:
            return gzip.compress(self.ubuntu)
        if "deb.debian.org" in url:
            return lzma.compress(DEBIAN_SOURCES)
        raise AssertionError(f"unexpected url {url}")


class NoSeries:
    """Launchpad answers, but nothing is open for development."""

    def get(self, url: str) -> bytes:
        return json.dumps({"entries": []}).encode()


@pytest.fixture
def logs(tmp_path):
    return tmp_path / "signals.jsonl", tmp_path / "health.jsonl"


def ingest(logs, *extra, fetcher=None):
    signals, healthlog = logs
    return main(
        ["ingest", "--signals", str(signals), "--health", str(healthlog), *extra],
        fetcher=fetcher or StubFetcher(),
    )


class TestFirstRun:
    def test_exits_zero(self, logs):
        assert ingest(logs, "--source", "merges") == 0

    def test_writes_the_signal_log(self, logs):
        ingest(logs, "--source", "merges")
        state = logmod.load(logs[0])
        assert len(state) == FIXTURE_MERGES
        assert all(s.is_active for s in state.values())

    def test_writes_the_health_log(self, logs):
        ingest(logs, "--source", "merges")
        assert health.load(logs[1])["merges"].consecutive_failures == 0

    def test_creates_the_data_directory(self, tmp_path):
        nested = tmp_path / "data"
        main(
            [
                "ingest",
                "--source",
                "merges",
                "--signals",
                str(nested / "signals.jsonl"),
                "--health",
                str(nested / "health.jsonl"),
            ],
            fetcher=StubFetcher(),
        )
        assert (nested / "signals.jsonl").exists()
        assert (nested / "health.jsonl").exists()

    def test_detects_the_development_series(self, logs):
        ingest(logs, "--source", "merges")
        series = {e.series for e in logmod.read(logs[0])}
        assert series == {"stonking"}

    def test_explicit_series_skips_launchpad(self, logs):
        fetcher = StubFetcher()
        ingest(logs, "--source", "merges", "--series", "noble", fetcher=fetcher)
        assert not [u for u in fetcher.urls if "api.launchpad.net" in u]


class TestSecondRun:
    def test_unchanged_run_appends_no_events(self, logs):
        ingest(logs, "--source", "merges")
        before = logs[0].read_text()
        ingest(logs, "--source", "merges")
        assert logs[0].read_text() == before

    def test_health_is_written_even_when_nothing_changed(self, logs):
        ingest(logs, "--source", "merges")
        ingest(logs, "--source", "merges")
        assert len(list(health.read(logs[1]))) == 2

    def test_only_the_change_is_appended(self, logs):
        ingest(logs, "--source", "merges")
        ingest(logs, "--source", "merges", fetcher=StubFetcher(ubuntu=without("cron")))
        added = list(logmod.read(logs[0]))[FIXTURE_MERGES:]
        assert [e.event.value for e in added] == ["resolved"]
        assert added[0].source_package == "cron"

    def test_a_recurrence_keeps_its_original_first_seen(self, logs):
        """The point of the log: a package that breaks twice is not a new one."""
        ingest(logs, "--source", "merges")
        opened = {e.signal_id: e.ts for e in logmod.read(logs[0])}

        ingest(logs, "--source", "merges", fetcher=StubFetcher(ubuntu=without("cron")))
        ingest(logs, "--source", "merges")

        cron = next(
            s
            for s in logmod.load(logs[0]).values()
            if s.signal.source_package == "cron"
        )
        assert cron.is_active
        assert cron.occurrences == 2
        assert cron.first_seen == opened[cron.signal.signal_id]


class TestDryRun:
    def test_writes_nothing(self, logs):
        assert ingest(logs, "--source", "merges", "--dry-run") == 0
        assert not logs[0].exists()
        assert not logs[1].exists()

    def test_still_reports(self, logs, capsys):
        ingest(logs, "--source", "merges", "--dry-run")
        assert "Dry run" in capsys.readouterr().out


class TestFailure:
    def test_total_failure_exits_one(self, logs):
        code = ingest(
            logs, "--source", "merges", fetcher=StubFetcher(fail=OSError("unreachable"))
        )
        assert code == 1

    def test_failed_source_appends_no_events(self, logs):
        ingest(logs, "--source", "merges", fetcher=StubFetcher(fail=OSError("no")))
        assert list(logmod.read(logs[0])) == []

    def test_failed_source_is_recorded_as_unhealthy(self, logs):
        ingest(logs, "--source", "merges", fetcher=StubFetcher(fail=OSError("no")))
        state = health.load(logs[1])["merges"]
        assert state.consecutive_failures == 1
        assert state.last_success is None

    def test_unresolvable_series_exits_one(self, logs):
        assert ingest(logs, "--source", "merges", fetcher=NoSeries()) == 1

    def test_unresolvable_series_is_still_a_recorded_run(self, logs):
        """Returning silently would leave the source looking healthy."""
        ingest(logs, "--source", "merges")
        ingest(logs, "--source", "merges", fetcher=NoSeries())
        state = health.load(logs[1])["merges"]
        assert state.consecutive_failures == 1
        assert "LookupError" in state.last_error
        assert len(list(health.read(logs[1]))) == 2

    def test_unresolvable_series_appends_no_events(self, logs):
        ingest(logs, "--source", "merges")
        before = logs[0].read_text()
        ingest(logs, "--source", "merges", fetcher=NoSeries())
        assert logs[0].read_text() == before

    def test_unresolvable_series_writes_nothing_on_a_dry_run(self, logs):
        assert ingest(logs, "--source", "merges", "--dry-run", fetcher=NoSeries()) == 1
        assert not logs[1].exists()

    def test_unknown_source_is_a_usage_error(self, logs):
        fetcher = StubFetcher()
        assert ingest(logs, "--source", "nosuch", fetcher=fetcher) == 2
        assert fetcher.urls == []

    def test_repeated_source_is_a_usage_error(self, logs):
        """Two identical ingesters would both emit OPENED, faking a recurrence."""
        fetcher = StubFetcher()
        code = ingest(logs, "--source", "merges", "--source", "merges", fetcher=fetcher)
        assert code == 2
        assert fetcher.urls == []
        assert not logs[0].exists()


class TestMassResolveGuard:
    def test_guard_makes_a_wholesale_drop_a_failure(self, logs):
        ingest(logs, "--source", "merges")
        assert ingest(logs, "--source", "merges", fetcher=StubFetcher(ubuntu=b"")) == 1

    def test_override_accepts_it(self, logs):
        ingest(logs, "--source", "merges")
        code = ingest(
            logs,
            "--source",
            "merges",
            "--allow-mass-resolve",
            fetcher=StubFetcher(ubuntu=b""),
        )
        assert code == 0
        assert not any(s.is_active for s in logmod.load(logs[0]).values())


class TestSummary:
    def test_names_every_source_and_the_series(self, logs, capsys):
        ingest(logs, "--source", "merges")
        out = capsys.readouterr().out
        assert "stonking" in out
        assert "| merges | ok |" in out

    def test_reports_failure_in_the_table(self, logs, capsys):
        ingest(logs, "--source", "merges", fetcher=StubFetcher(fail=OSError("boom")))
        assert "FAILED" in capsys.readouterr().out

    def test_appends_to_github_step_summary(self, logs, tmp_path, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        ingest(logs, "--source", "merges")
        assert "| merges | ok |" in summary.read_text()

    def test_absent_env_var_is_not_an_error(self, logs, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        assert ingest(logs, "--source", "merges") == 0


class TestDefaults:
    def test_runs_every_registered_source(self, logs):
        ingest(logs)
        assert {e.source for e in logmod.read(logs[0])} == {"merges"}


class TestFetcherConstruction:
    """cmd_ingest takes an injected fetcher in every other test, so the real
    construction path needs exercising directly."""

    def args(self, *extra, tmp_path):
        return build_parser().parse_args(
            ["ingest", "--cache-dir", str(tmp_path / "cache"), *extra]
        )

    def test_caches_by_default(self, tmp_path):
        assert isinstance(_fetcher(self.args(tmp_path=tmp_path)).cache, FileCache)

    def test_no_cache_disables_the_cache(self, tmp_path):
        assert _fetcher(self.args("--no-cache", tmp_path=tmp_path)).cache is None

    def test_dry_run_reads_the_cache_but_does_not_write_it(self, tmp_path):
        cache = _fetcher(self.args("--dry-run", tmp_path=tmp_path)).cache
        assert isinstance(cache, ReadOnlyCache)
        assert isinstance(cache.inner, FileCache)

    def test_no_cache_wins_over_dry_run(self, tmp_path):
        args = self.args("--dry-run", "--no-cache", tmp_path=tmp_path)
        assert _fetcher(args).cache is None

    def test_cache_dir_is_honoured(self, tmp_path):
        fetcher = _fetcher(self.args(tmp_path=tmp_path))
        assert fetcher.cache.directory == tmp_path / "cache"
