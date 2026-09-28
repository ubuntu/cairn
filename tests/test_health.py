from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cairn.core import health
from cairn.core.health import SourceHealth
from cairn.core.runner import run
from cairn.ingest.base import Ingester, Kind, Signal

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
T3 = T0 + timedelta(days=3)


def sig(name):
    return Signal(kind=Kind.NEEDS_MERGE, source_package=name, series="stonking")


class StubIngester(Ingester[list]):
    def __init__(self, name, signals=(), *, fail_on=None):
        self.name = name
        self._signals = list(signals)
        self._fail_on = fail_on

    def fetch(self):
        if self._fail_on:
            raise self._fail_on
        return self._signals

    def parse(self, raw):
        return raw


def history(path, *runs):
    for ingesters, now in runs:
        health.append(path, run(ingesters, {}, now=now))
    return health.load(path)


class TestDistinguishesFreshFromStale:
    def test_success_and_failure_are_not_conflated(self, tmp_path):
        """The gap the signal log alone cannot express."""
        path = tmp_path / "runs.jsonl"
        got = history(
            path,
            ([StubIngester("merges", [sig("a")]), StubIngester("nbs", [sig("b")])], T0),
            (
                [
                    StubIngester("merges", [sig("a")]),
                    StubIngester("nbs", fail_on=OSError("down")),
                ],
                T3,
            ),
        )
        assert got["merges"].last_success == T3
        assert got["nbs"].last_success == T0
        assert not got["merges"].is_stale(T3)
        assert got["nbs"].is_stale(T3)

    def test_unchanged_signals_still_count_as_a_success(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        got = history(path, ([StubIngester("merges", [])], T0))
        assert got["merges"].last_success == T0
        assert got["merges"].consecutive_failures == 0


class TestFailureTracking:
    def test_counts_consecutive_failures(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        got = history(
            path,
            ([StubIngester("merges", fail_on=OSError("1"))], T0),
            ([StubIngester("merges", fail_on=OSError("2"))], T1),
            ([StubIngester("merges", fail_on=OSError("3"))], T3),
        )
        assert got["merges"].consecutive_failures == 3

    def test_success_resets_the_counter(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        got = history(
            path,
            ([StubIngester("merges", fail_on=OSError("x"))], T0),
            ([StubIngester("merges", [sig("a")])], T1),
        )
        assert got["merges"].consecutive_failures == 0
        assert got["merges"].last_error is None

    def test_last_success_survives_later_failures(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        got = history(
            path,
            ([StubIngester("merges", [sig("a")])], T0),
            ([StubIngester("merges", fail_on=OSError("down"))], T3),
        )
        assert got["merges"].last_success == T0
        assert got["merges"].last_attempt == T3
        assert "down" in got["merges"].last_error


class TestStaleness:
    def test_never_succeeded_is_stale(self):
        h = SourceHealth("merges", last_attempt=T0)
        assert h.age(T0) is None
        assert h.is_stale(T0)

    def test_within_window_is_fresh(self):
        h = SourceHealth("merges", last_attempt=T0, last_success=T0)
        assert not h.is_stale(T0 + timedelta(hours=6))

    def test_beyond_window_is_stale(self):
        h = SourceHealth("merges", last_attempt=T0, last_success=T0)
        assert h.is_stale(T3)

    def test_window_is_configurable(self):
        h = SourceHealth("merges", last_attempt=T0, last_success=T0)
        assert not h.is_stale(T3, after=timedelta(days=7))


class TestPersistence:
    def test_survives_a_reload(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        history(path, ([StubIngester("merges", [sig("a")])], T0))
        assert health.load(path)["merges"].last_success == T0

    def test_append_is_additive(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        history(path, ([StubIngester("a")], T0), ([StubIngester("a")], T1))
        assert len(path.read_text().splitlines()) == 2

    def test_missing_file_is_empty(self, tmp_path):
        assert health.load(tmp_path / "none.jsonl") == {}

    def test_renderable_payload(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        got = history(path, ([StubIngester("merges", fail_on=OSError("down"))], T0))
        payload = json.loads(json.dumps(got["merges"].as_dict(now=T3)))
        assert payload["stale"] is True
        assert payload["age_seconds"] is None
        assert payload["consecutive_failures"] == 1
