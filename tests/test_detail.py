from __future__ import annotations

from cairn.core import detail


def test_round_trip(tmp_path):
    detail.save(tmp_path, "migration", {"packages": {"a": {"tests": []}}})
    assert detail.load(tmp_path, "migration") == {"packages": {"a": {"tests": []}}}


def test_overwrites_the_last(tmp_path):
    detail.save(tmp_path, "migration", {"run": 1})
    detail.save(tmp_path, "migration", {"run": 2})
    assert detail.load(tmp_path, "migration") == {"run": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["migration.json"]


def test_missing_is_empty(tmp_path):
    assert detail.load(tmp_path, "migration") == {}


def test_unreadable_is_empty(tmp_path):
    """The pages fall back to the log rather than fail the build."""
    detail.path(tmp_path, "migration").write_text("{not json")
    assert detail.load(tmp_path, "migration") == {}
