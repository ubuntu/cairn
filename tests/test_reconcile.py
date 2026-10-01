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
        """Only development kinds roll over; an NBS in noble is not one in
        plucky, so a series change there is a different signal."""
        first = reconcile(
            {}, [sig(kind=Kind.NBS, series="noble")], source="nbs", now=T0
        )
        events = reconcile(
            state_from(first),
            [sig(kind=Kind.NBS, series="plucky")],
            source="nbs",
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
