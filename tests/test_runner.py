from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cairn.core import log as logmod
from cairn.core.log import EventType
from cairn.core.reconcile import reconcile
from cairn.core.runner import Outcome, RunReport, run
from cairn.ingest.base import Ingester, Kind, Signal

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=1)


def sig(name, payload=None):
    return Signal(
        kind=Kind.NEEDS_MERGE,
        source_package=name,
        series="stonking",
        payload=payload or {},
    )


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


def state(*event_groups):
    return logmod.replay([e for group in event_groups for e in group])


def seed(source, names, now=T0):
    return reconcile({}, [sig(n) for n in names], source=source, now=now)


class TestIsolation:
    def test_one_failure_does_not_lose_the_others(self):
        report = run(
            [
                StubIngester("merges", fail_on=OSError("archive unreachable")),
                StubIngester("nbs", [sig("a")]),
            ],
            {},
            now=T0,
        )
        assert report.stale_sources == ("merges",)
        assert [o.source for o in report.succeeded] == ["nbs"]
        assert len(report.events) == 1

    def test_failed_source_emits_no_events(self):
        previous = state(seed("merges", ["a", "b"]))
        report = run(
            [StubIngester("merges", fail_on=TimeoutError("stalled"))],
            previous,
            now=T1,
        )
        assert report.events == ()

    def test_failed_source_signals_are_carried_forward(self):
        """Silence must never be read as 'everything got fixed'."""
        seeded = seed("merges", ["a", "b"])
        previous = state(seeded)
        report = run(
            [StubIngester("merges", fail_on=OSError("boom"))], previous, now=T1
        )
        after = state(seeded, report.events)
        assert {s.signal.source_package for s in after.values() if s.is_active} == {
            "a",
            "b",
        }

    def test_error_is_recorded_not_raised(self):
        report = run(
            [StubIngester("merges", fail_on=ValueError("bad index"))], {}, now=T0
        )
        assert "ValueError: bad index" in report.failed[0].error


class TestMassResolveIsAFailureNotACrash:
    def test_guard_trip_does_not_abort_the_run(self):
        previous = state(seed("merges", [str(i) for i in range(10)]))
        report = run(
            [StubIngester("merges", []), StubIngester("nbs", [sig("x")])],
            previous,
            now=T1,
        )
        assert report.stale_sources == ("merges",)
        assert [o.source for o in report.succeeded] == ["nbs"]

    def test_guard_trip_produces_no_resolve_events(self):
        seeded = seed("merges", [str(i) for i in range(10)])
        report = run([StubIngester("merges", [])], state(seeded), now=T1)
        assert not [e for e in report.events if e.event is EventType.RESOLVED]

    def test_guard_trip_reports_what_was_seen(self):
        previous = state(seed("merges", [str(i) for i in range(10)]))
        report = run([StubIngester("merges", [])], previous, now=T1)
        assert report.failed[0].signals == 0
        assert "MassResolve" in report.failed[0].error


class TestExitCondition:
    def test_partial_failure_is_not_total(self):
        report = run(
            [StubIngester("a", fail_on=OSError()), StubIngester("b", [sig("x")])],
            {},
            now=T0,
        )
        assert not report.total_failure

    def test_every_source_failing_is_total(self):
        report = run(
            [
                StubIngester("a", fail_on=OSError()),
                StubIngester("b", fail_on=OSError()),
            ],
            {},
            now=T0,
        )
        assert report.total_failure

    def test_no_ingesters_is_not_a_failure(self):
        assert not run([], {}, now=T0).total_failure


class TestReporting:
    def test_counts_signals_and_events(self):
        report = run([StubIngester("merges", [sig("a"), sig("b")])], {}, now=T0)
        assert report.outcomes[0].signals == 2
        assert report.outcomes[0].events == 2

    def test_unchanged_signals_produce_no_events(self):
        previous = state(seed("merges", ["a"]))
        report = run([StubIngester("merges", [sig("a")])], previous, now=T1)
        assert report.outcomes[0].signals == 1
        assert report.outcomes[0].events == 0

    def test_report_is_json_serialisable(self):
        report = run(
            [StubIngester("a", [sig("x")]), StubIngester("b", fail_on=OSError("no"))],
            {},
            now=T0,
        )
        payload = json.loads(json.dumps(report.as_dict()))
        assert payload["stale_sources"] == ["b"]
        assert payload["total_failure"] is False
        assert payload["started_at"] == T0.isoformat()

    def test_outcomes_preserve_order(self):
        report = run(
            [StubIngester("a"), StubIngester("b"), StubIngester("c")], {}, now=T0
        )
        assert [o.source for o in report.outcomes] == ["a", "b", "c"]


def test_report_is_frozen():
    report = RunReport(started_at=T0, outcomes=(), events=())
    assert isinstance(report.outcomes, tuple)
    assert Outcome("x", ok=True).as_dict()["ok"] is True


class TestMassResolveOverride:
    def test_guard_can_be_lifted_for_a_legitimate_drop(self):
        previous = state(seed("merges", [str(i) for i in range(10)]))
        report = run(
            [StubIngester("merges", [])],
            previous,
            now=T1,
            max_resolve_fraction=None,
        )
        assert report.succeeded
        assert len(report.events) == 10

    def test_guard_is_on_by_default(self):
        previous = state(seed("merges", [str(i) for i in range(10)]))
        report = run([StubIngester("merges", [])], previous, now=T1)
        assert report.failed
