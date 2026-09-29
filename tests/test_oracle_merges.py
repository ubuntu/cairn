from __future__ import annotations

import dataclasses
import gzip
import json
import lzma
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cairn.ingest.base import Kind, Signal
from cairn.ingest.merges import UBUNTU_COMPONENTS
from cairn.ingest.series import SERIES_URL, development_series
from cairn.oracles.merges import (
    Divergence,
    check,
    compare,
    published_candidates,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)

FIXTURES = Path(__file__).parent / "fixtures"
UBUNTU_SOURCES = (FIXTURES / "ubuntu_sources").read_bytes()
DEBIAN_SOURCES = (FIXTURES / "debian_sources").read_bytes()

# Merge candidates the fixtures yield
FIXTURE_MERGES = {
    "cloud-init",
    "cron",
    "devscripts",
    "gtk+3.0",
    "live-build",
    "qemu",
    "shim",
    "wireless-regdb",
}


def signal(name, *, independent=False):
    return Signal(
        kind=Kind.NEEDS_MERGE,
        source_package=name,
        series="stonking",
        payload={"independent_lineage": independent},
    )


def divergence(cairn_names, published, **kw):
    return compare(
        [signal(n, **kw) for n in cairn_names],
        published,
        scope="stonking/main",
        now=NOW,
    )


class TestSetArithmetic:
    def test_agreement(self):
        d = divergence(["a", "b"], ["a", "b"])
        assert d.agree == {"a", "b"}
        assert not d.cairn_only
        assert not d.missing

    def test_cairn_only(self):
        assert divergence(["a", "b"], ["a"]).cairn_only == {"b"}

    def test_missing_is_the_serious_direction(self):
        assert divergence(["a"], ["a", "b"]).missing == {"b"}

    def test_disjoint(self):
        d = divergence(["a"], ["b"])
        assert d.cairn_only == {"a"}
        assert d.missing == {"b"}
        assert not d.agree


class TestExplanation:
    def test_independent_lineage_is_explained(self):
        d = divergence(["a"], [], independent=True)
        assert d.explained == {"a"}
        assert not d.unexplained

    def test_derived_lineage_is_unexplained(self):
        d = divergence(["a"], [], independent=False)
        assert d.unexplained == {"a"}
        assert not d.explained

    def test_agreeing_entries_are_never_flagged(self):
        d = divergence(["a"], ["a"], independent=True)
        assert not d.explained
        assert not d.unexplained


class TestSerialisation:
    def test_as_dict_is_json_serialisable(self):
        payload = divergence(["a", "b"], ["b", "c"]).as_dict()
        assert json.loads(json.dumps(payload))["counts"] == {
            "cairn": 2,
            "published": 2,
            "agree": 1,
            "cairn_only": 1,
            "explained": 0,
            "unexplained": 1,
            "missing": 1,
        }

    def test_lists_are_sorted_for_stable_diffs(self):
        d = divergence(["a"], ["z", "b", "m"]).as_dict()
        assert d["missing"] == ["b", "m", "z"]

    def test_records_provenance(self):
        d = divergence(["a"], ["a"]).as_dict()
        assert d["oracle"] == "merges.ubuntu.com"
        assert d["scope"] == "stonking/main"
        assert d["checked_at"] == NOW.isoformat()


class StubFetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return json.dumps(self.payloads[url]).encode()


class TestPublishedCandidates:
    def test_reads_source_package_names(self):
        fetcher = StubFetcher(
            {
                "https://merges.ubuntu.com/main.json": [
                    {"source_package": "a"},
                    {"source_package": "b"},
                ]
            }
        )
        assert published_candidates(fetcher, ["main"]) == {"a", "b"}

    def test_unions_components(self):
        fetcher = StubFetcher(
            {
                "https://merges.ubuntu.com/main.json": [{"source_package": "a"}],
                "https://merges.ubuntu.com/universe.json": [{"source_package": "b"}],
            }
        )
        assert published_candidates(fetcher, ["main", "universe"]) == {"a", "b"}

    def test_uses_https(self):
        fetcher = StubFetcher({"https://merges.ubuntu.com/main.json": []})
        published_candidates(fetcher, ["main"])
        assert all(u.startswith("https://") for u in fetcher.urls)


def test_divergence_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        divergence(["a"], ["a"]).subject = "other"


def test_detail_drives_explanation_not_the_signal_object():
    """as_dict must not need the original Signals to explain a divergence."""
    d = Divergence(
        subject="needs_merge",
        scope="x",
        oracle="o",
        checked_at=NOW,
        cairn=frozenset({"a"}),
        published=frozenset(),
        detail={"a": {"independent_lineage": True}},
    )
    assert d.as_dict()["counts"]["explained"] == 1


class EndToEndFetcher:
    """Serves both archives and the oracle, recording every URL."""

    def __init__(self, published=()):
        self.published = list(published)
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if "api.launchpad.net" in url:
            return json.dumps(
                {"entries": [{"name": "stonking", "status": "Pre-release Freeze"}]}
            ).encode()
        if "archive.ubuntu.com" in url:
            return gzip.compress(UBUNTU_SOURCES)
        if "deb.debian.org" in url:
            return lzma.compress(DEBIAN_SOURCES)
        if "merges.ubuntu.com" in url:
            return json.dumps([{"source_package": n} for n in self.published]).encode()
        raise AssertionError(f"unexpected url {url}")


class TestCheckEndToEnd:
    def test_requests_the_series_and_every_component(self):
        fetcher = EndToEndFetcher()
        check(fetcher, series="stonking", components=("main", "universe"))
        ubuntu = [u for u in fetcher.urls if "archive.ubuntu.com" in u]
        assert all("/stonking/" in u for u in ubuntu)
        assert {"main", "universe"} == {u.split("/")[-3] for u in ubuntu}

    def test_queries_the_oracle_per_component(self):
        fetcher = EndToEndFetcher()
        check(fetcher, series="stonking", components=("main", "universe"))
        oracle = [u for u in fetcher.urls if "merges.ubuntu.com" in u]
        assert sorted(oracle) == [
            "https://merges.ubuntu.com/main.json",
            "https://merges.ubuntu.com/universe.json",
        ]

    def test_every_request_is_https(self):
        fetcher = EndToEndFetcher()
        check(fetcher, series="stonking", components=("main",))
        assert all(u.startswith("https://") for u in fetcher.urls)

    def test_runs_the_ingester_and_reports_its_findings(self):
        d = check(EndToEndFetcher(), series="stonking", components=("main",))
        assert d.cairn == FIXTURE_MERGES

    def test_full_agreement_leaves_no_divergence(self):
        d = check(
            EndToEndFetcher(published=FIXTURE_MERGES),
            series="stonking",
            components=("main",),
        )
        assert d.agree == FIXTURE_MERGES
        assert not d.cairn_only
        assert not d.missing

    def test_oracle_entry_cairn_cannot_find_is_missing(self):
        d = check(
            EndToEndFetcher(published=FIXTURE_MERGES | {"ghost"}),
            series="stonking",
            components=("main",),
        )
        assert d.missing == {"ghost"}

    def test_records_scope_and_provenance(self):
        d = check(EndToEndFetcher(), series="stonking", components=("main",))
        assert d.scope == "stonking/main"
        assert d.subject == "needs_merge"
        assert d.oracle == "merges.ubuntu.com"

    def test_detail_carries_payloads_for_explanation(self):
        d = check(EndToEndFetcher(), series="stonking", components=("main",))
        assert "base_version" in d.detail["live-build"]
        assert d.explained | d.unexplained == d.cairn_only


def test_oracle_defaults_to_the_scope_the_ingester_runs_in():
    """A narrower oracle would silently validate half of production."""
    fetcher = EndToEndFetcher()
    check(fetcher, series="stonking")
    checked = {u.split("/")[-3] for u in fetcher.urls if "archive.ubuntu.com" in u}
    assert checked == set(UBUNTU_COMPONENTS)


class TestSeriesGuard:
    def test_rejects_a_non_development_series(self):
        with pytest.raises(ValueError, match="not open for development"):
            check(EndToEndFetcher(), series="noble")

    def test_error_names_the_current_development_series(self):
        with pytest.raises(ValueError, match="stonking"):
            check(EndToEndFetcher(), series="noble")

    def test_accepts_the_development_series(self):
        assert check(EndToEndFetcher(), series="stonking").scope.startswith("stonking/")


class PagedSeriesFetcher:
    """Serves the series collection across two pages."""

    PAGE2 = "https://api.launchpad.net/devel/ubuntu/series?start=1"

    def __init__(self, pages):
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        return json.dumps(self.pages[url]).encode()


class TestSeriesPagination:
    def test_follows_next_collection_link(self):
        fetcher = PagedSeriesFetcher(
            {
                SERIES_URL: {
                    "entries": [{"name": "noble", "status": "Supported"}],
                    "next_collection_link": PagedSeriesFetcher.PAGE2,
                },
                PagedSeriesFetcher.PAGE2: {
                    "entries": [{"name": "stonking", "status": "Active Development"}]
                },
            }
        )
        assert development_series(fetcher) == "stonking"
        assert len(fetcher.urls) == 2

    def test_stops_when_no_next_link(self):
        fetcher = PagedSeriesFetcher(
            {SERIES_URL: {"entries": [{"name": "noble", "status": "Supported"}]}}
        )
        with pytest.raises(LookupError):
            development_series(fetcher)
        assert len(fetcher.urls) == 1

    def test_does_not_page_further_than_needed(self):
        fetcher = PagedSeriesFetcher(
            {
                SERIES_URL: {
                    "entries": [{"name": "stonking", "status": "Pre-release Freeze"}],
                    "next_collection_link": PagedSeriesFetcher.PAGE2,
                }
            }
        )
        assert development_series(fetcher) == "stonking"
        assert fetcher.urls == [SERIES_URL]
