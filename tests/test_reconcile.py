from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cairn.core import log
from cairn.core.log import Event, EventType, SignalState
from cairn.core.reconcile import MassResolve, reconcile
from cairn.ingest.base import Kind, Signal

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)
SOURCE = "merges"


def sig(name="hello", **kw) -> Signal:
    return Signal(**({"kind": Kind.NEEDS_MERGE, "source_package": name} | kw))


def state_from(events) -> dict[str, SignalState]:
    return log.replay(events)


class TestReconcile:
    def test_new_signal_opens(self):
        events = reconcile({}, [sig()], source=SOURCE, now=T0)
        assert [e.event for e in events] == [EventType.OPENED]

    def test_unchanged_signal_emits_nothing(self):
        first = reconcile({}, [sig()], source=SOURCE, now=T0)
        events = reconcile(state_from(first), [sig()], source=SOURCE, now=T1)
        assert events == []

    def test_changed_payload_updates(self):
        first = reconcile({}, [sig(payload={"d": 1})], source=SOURCE, now=T0)
        events = reconcile(
            state_from(first), [sig(payload={"d": 2})], source=SOURCE, now=T1
        )
        assert [e.event for e in events] == [EventType.UPDATED]

    def test_disappeared_signal_resolves(self):
        first = reconcile({}, [sig("a"), sig("b")], source=SOURCE, now=T0)
        events = reconcile(state_from(first), [sig("a")], source=SOURCE, now=T1)
        assert [(e.event, e.source_package) for e in events] == [
            (EventType.RESOLVED, "b")
        ]

    def test_other_sources_are_untouched(self):
        mine = reconcile({}, [sig("a")], source=SOURCE, now=T0)
        theirs = reconcile({}, [sig("b")], source="nbs", now=T0)
        events = reconcile(state_from(mine + theirs), [sig("a")], source=SOURCE, now=T1)
        assert events == []

    def test_empty_ingest_is_guarded(self):
        first = reconcile({}, [sig(str(i)) for i in range(10)], source=SOURCE, now=T0)
        with pytest.raises(MassResolve):
            reconcile(state_from(first), [], source=SOURCE, now=T1)

    def test_a_small_source_may_resolve_most_of_itself(self):
        """Review on #26: an archive admin removing 11 of 21 NBS binaries."""
        first = reconcile({}, [sig(str(i)) for i in range(21)], source=SOURCE, now=T0)
        events = reconcile(
            state_from(first), [sig(str(i)) for i in range(10)], source=SOURCE, now=T1
        )
        assert len(events) == 11

    def test_a_large_source_is_still_guarded(self):
        first = reconcile({}, [sig(str(i)) for i in range(60)], source=SOURCE, now=T0)
        with pytest.raises(MassResolve):
            reconcile(
                state_from(first),
                [sig(str(i)) for i in range(20)],
                source=SOURCE,
                now=T1,
            )

    def test_nbs_rolls_over_with_the_series(self):
        """Review on #26: the new series' NBS binaries are updates, not
        21 resolutions the guard would refuse."""
        names = [f"lib{i}" for i in range(21)]
        first = reconcile(
            {},
            [sig(n, kind=Kind.NBS, series="tenacious") for n in names],
            source="nbs",
            now=T0,
        )
        events = reconcile(
            state_from(first),
            [sig(n, kind=Kind.NBS, series="next") for n in names],
            source="nbs",
            now=T1,
        )
        assert {e.event for e in events} == {EventType.UPDATED}

    def test_guard_can_be_disabled(self):
        first = reconcile({}, [sig(str(i)) for i in range(10)], source=SOURCE, now=T0)
        events = reconcile(
            state_from(first), [], source=SOURCE, now=T1, max_resolve_fraction=None
        )
        assert len(events) == 10


class TestHistory:
    def test_first_seen_survives_updates(self):
        e1 = reconcile({}, [sig(payload={"d": 1})], source=SOURCE, now=T0)
        e2 = reconcile(state_from(e1), [sig(payload={"d": 2})], source=SOURCE, now=T1)
        st = state_from(e1 + e2)[sig().signal_id]
        assert st.first_seen == T0
        assert st.last_seen == T1
        assert st.is_active

    def test_resolution_is_recorded(self):
        e1 = reconcile({}, [sig()], source=SOURCE, now=T0)
        e2 = reconcile(
            state_from(e1), [], source=SOURCE, now=T1, max_resolve_fraction=None
        )
        st = state_from(e1 + e2)[sig().signal_id]
        assert not st.is_active
        assert st.resolved_at == T1

    def test_recurrence_keeps_original_first_seen(self):
        e1 = reconcile({}, [sig()], source=SOURCE, now=T0)
        e2 = reconcile(
            state_from(e1), [], source=SOURCE, now=T1, max_resolve_fraction=None
        )
        e3 = reconcile(state_from(e1 + e2), [sig()], source=SOURCE, now=T2)
        st = state_from(e1 + e2 + e3)[sig().signal_id]
        assert st.first_seen == T0
        assert st.occurrences == 2
        assert st.is_active


class TestLogRoundTrip:
    def test_json_round_trip(self):
        original = Event.of(
            EventType.OPENED, sig(payload={"a": [1, 2]}), source=SOURCE, ts=T0
        )
        assert Event.from_json(original.to_json()) == original

    def test_append_and_load(self, tmp_path):
        path = tmp_path / "signals.jsonl"
        events = reconcile({}, [sig("a"), sig("b")], source=SOURCE, now=T0)
        assert log.append(path, events) == 2
        assert set(log.load(path)) == {sig("a").signal_id, sig("b").signal_id}

    def test_append_is_additive(self, tmp_path):
        path = tmp_path / "signals.jsonl"
        e1 = reconcile({}, [sig("a")], source=SOURCE, now=T0)
        log.append(path, e1)
        e2 = reconcile(log.load(path), [sig("a"), sig("b")], source=SOURCE, now=T1)
        log.append(path, e2)
        assert len(path.read_text().splitlines()) == 2

    def test_load_missing_file_is_empty(self, tmp_path):
        assert log.load(tmp_path / "nope.jsonl") == {}


class TestMonthlyFiles:
    """A directory of monthly files, so no file reaches GitHub's 100 MB."""

    def test_each_event_goes_in_its_month(self, tmp_path):
        path = tmp_path / "signals"
        log.append(path, reconcile({}, [sig("a")], source=SOURCE, now=T0))
        feb = reconcile(
            log.load(path), [sig("b")], source=SOURCE, now=T0 + timedelta(days=40)
        )
        log.append(path, feb)
        assert sorted(p.name for p in path.iterdir()) == [
            "2026-01.jsonl",
            "2026-02.jsonl",
        ]
        assert len((path / "2026-01.jsonl").read_text().splitlines()) == 1
        # b opened, a resolved: both February.
        assert len((path / "2026-02.jsonl").read_text().splitlines()) == 2

    def test_a_finished_month_is_never_written_again(self, tmp_path):
        path = tmp_path / "signals"
        log.append(path, reconcile({}, [sig("a")], source=SOURCE, now=T0))
        before = (path / "2026-01.jsonl").read_text()
        later = T0 + timedelta(days=40)
        log.append(
            path,
            reconcile(log.load(path), [sig("a"), sig("b")], source=SOURCE, now=later),
        )
        assert (path / "2026-01.jsonl").read_text() == before

    def test_replay_crosses_months(self, tmp_path):
        """History survives the split: first_seen comes from January."""
        path = tmp_path / "signals"
        log.append(path, reconcile({}, [sig("a")], source=SOURCE, now=T0))
        later = T0 + timedelta(days=40)
        log.append(
            path,
            reconcile(
                log.load(path), [sig("a", payload={"x": 1})], source=SOURCE, now=later
            ),
        )
        [state] = log.load(path).values()
        assert state.first_seen == T0
        assert state.signal.payload == {"x": 1}

    def test_months_are_read_in_date_order(self, tmp_path):
        path = tmp_path / "signals"
        path.mkdir()
        for name in ("2026-10.jsonl", "2025-12.jsonl", "2026-02.jsonl"):
            (path / name).touch()
        assert [p.name for p in log.files(path)] == [
            "2025-12.jsonl",
            "2026-02.jsonl",
            "2026-10.jsonl",
        ]

    def test_other_files_are_not_the_log(self, tmp_path):
        path = tmp_path / "signals"
        path.mkdir()
        (path / "2026-01.jsonl").touch()
        (path / "notes.txt").touch()
        (path / "2026-01.jsonl.orig").touch()
        assert [p.name for p in log.files(path)] == ["2026-01.jsonl"]

    def test_the_single_file_it_replaced_is_read_first(self, tmp_path):
        """data/signals.jsonl keeps its history until it is deliberately
        removed, and is never written to again."""
        legacy = tmp_path / "signals.jsonl"
        path = tmp_path / "signals"
        log.append(legacy, reconcile({}, [sig("a")], source=SOURCE, now=T0))
        before = legacy.read_text()
        later = T0 + timedelta(days=40)
        log.append(
            path,
            reconcile(log.load(path), [sig("a"), sig("b")], source=SOURCE, now=later),
        )
        assert legacy.read_text() == before
        states = log.load(path)
        assert states[sig("a").signal_id].first_seen == T0
        assert len(states) == 2

    def test_month_is_utc(self):
        from datetime import timezone

        late = datetime(2026, 1, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert log.month_of(late) == "2026-02"

    def test_missing_directory_is_empty(self, tmp_path):
        assert log.load(tmp_path / "signals") == {}


class TestSeriesRollover:
    def test_a_new_series_updates_rather_than_reopens(self):
        first = reconcile({}, [sig(series="stonking")], source=SOURCE, now=T0)
        events = reconcile(
            state_from(first), [sig(series="tumbling")], source=SOURCE, now=T1
        )
        assert [(e.event, e.series) for e in events] == [
            (EventType.UPDATED, "tumbling")
        ]

    def test_a_whole_board_moving_series_is_not_a_mass_resolve(self):
        names = [str(i) for i in range(10)]
        first = reconcile(
            {}, [sig(n, series="stonking") for n in names], source=SOURCE, now=T0
        )
        events = reconcile(
            state_from(first),
            [sig(n, series="tumbling") for n in names],
            source=SOURCE,
            now=T1,
        )
        assert {e.event for e in events} == {EventType.UPDATED}

    def test_series_bound_kinds_still_split_by_series(self):
        """Only development kinds roll over; an SRU in noble is not one in
        plucky, so a series change there is a different signal."""
        first = reconcile(
            {}, [sig(kind=Kind.SRU_PENDING, series="noble")], source="sru", now=T0
        )
        events = reconcile(
            state_from(first),
            [sig(kind=Kind.SRU_PENDING, series="plucky")],
            source="sru",
            now=T1,
            max_resolve_fraction=None,
        )
        assert sorted(e.event for e in events) == [
            EventType.OPENED,
            EventType.RESOLVED,
        ]


class TestReplayRecomputesIdentity:
    def test_lines_written_under_an_older_identity_rule_still_fold(self):
        """Log lines from before DEVELOPMENT_KINDS stored an id that hashed in
        the series. Replay must not trust it, or the next run would see every
        signal as new and every old one as gone."""
        opened = Event.of(
            EventType.OPENED, sig(series="stonking"), source=SOURCE, ts=T0
        )
        legacy = replace(opened, signal_id="0123456789abcdef")
        updated = Event.of(
            EventType.UPDATED,
            sig(series="stonking", payload={"d": 1}),
            source=SOURCE,
            ts=T1,
        )
        state = state_from([legacy, updated])
        assert list(state) == [sig().signal_id]
        assert state[sig().signal_id].first_seen == T0
        assert state[sig().signal_id].signal.payload == {"d": 1}
