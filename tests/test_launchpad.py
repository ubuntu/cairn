from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

import pytest

from cairn.ingest import debian_uploads, packagesets, publications, teams

FIXTURES = Path(__file__).parent / "fixtures"
PUBLICATIONS = (FIXTURES / "lp_publications_page1.json").read_bytes()
PACKAGE_SETS = (FIXTURES / "lp_package_sets.json").read_bytes()
SOURCES_INCLUDED = (FIXTURES / "lp_sources_included.json").read_bytes()
TEAM_PACKAGES = (FIXTURES / "lp_team_packages.json").read_bytes()

SERIES = "stonking"


class Fetcher:
    """Serves canned bodies and records what was asked for."""

    def __init__(self, responses: dict[str, bytes] | None = None, default=None):
        self.responses = responses or {}
        self.default = default
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        for fragment, body in self.responses.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                return body
        if self.default is None:
            raise AssertionError(f"unexpected url {url}")
        return self.default


class TestPublicationParsing:
    def test_reads_a_real_response(self):
        found, _ = publications.parse_page(PUBLICATIONS)
        by_name = {p.source: p for p in found}
        assert "0ad" in by_name
        assert by_name["0ad"].version == "0.28.0-3build1"
        assert by_name["0ad"].component == "universe"

    def test_extracts_the_person_from_a_launchpad_link(self):
        found, _ = publications.parse_page(PUBLICATIONS)
        assert {p.uploader for p in found} >= {"doko", "pkg-games-devel"}

    def test_parses_the_timestamp(self):
        found, _ = publications.parse_page(PUBLICATIONS)
        assert found[0].uploaded == datetime(
            2026, 9, 22, 13, 28, 14, 106654, tzinfo=UTC
        )

    def test_returns_the_pagination_link(self):
        _, nxt = publications.parse_page(PUBLICATIONS)
        assert nxt and "getPublishedSources" in nxt

    def test_a_null_signer_does_not_break_parsing(self):
        """Over half the archive is synced and has no Ubuntu signer."""
        found, _ = publications.parse_page(PUBLICATIONS)
        assert all(p.source for p in found)

    def test_skips_entries_without_a_name(self):
        raw = json.dumps({"entries": [{"source_package_version": "1-1"}]}).encode()
        found, _ = publications.parse_page(raw)
        assert found == []


class TestPublicationFetch:
    def test_follows_pagination(self):
        page2 = json.dumps(
            {
                "entries": [
                    {
                        "source_package_name": "zlib",
                        "source_package_version": "1:1.3-1",
                        "date_created": "2026-01-01T00:00:00+00:00",
                        "package_creator_link": "https://api.launchpad.net/devel/~kat",
                    }
                ],
                "next_collection_link": None,
            }
        ).encode()
        fetcher = Fetcher({"ws.start": page2}, default=PUBLICATIONS)
        found = publications.fetch(fetcher, SERIES, on_page=None)
        assert "zlib" in found
        assert len(fetcher.urls) == 2

    def test_max_pages_stops_early(self):
        fetcher = Fetcher(default=PUBLICATIONS)
        publications.fetch(fetcher, SERIES, max_pages=1, on_page=None)
        assert len(fetcher.urls) == 1

    def test_keep_discards_everything_else(self):
        fetcher = Fetcher(default=PUBLICATIONS)
        found = publications.fetch(
            fetcher, SERIES, keep={"0ad"}, max_pages=1, on_page=None
        )
        assert set(found) == {"0ad"}

    def test_keeps_the_latest_publication_of_a_source(self):
        """A source is published in several pockets; the newest upload wins."""
        older = {
            "source_package_name": "cron",
            "source_package_version": "3.0-1",
            "date_created": "2020-01-01T00:00:00+00:00",
            "package_creator_link": "https://api.launchpad.net/devel/~old",
        }
        newer = dict(older, source_package_version="3.0-2")
        newer["date_created"] = "2026-01-01T00:00:00+00:00"
        newer["package_creator_link"] = "https://api.launchpad.net/devel/~new"
        raw = json.dumps({"entries": [older, newer]}).encode()
        found = publications.fetch(
            Fetcher(default=raw), SERIES, max_pages=1, on_page=None
        )
        assert found["cron"].uploader == "new"

    def test_reports_progress(self):
        seen = []
        publications.fetch(
            Fetcher(default=PUBLICATIONS),
            SERIES,
            max_pages=1,
            on_page=lambda pages, kept: seen.append((pages, kept)),
        )
        assert seen == [(1, 4)]

    def test_requests_the_right_series(self):
        fetcher = Fetcher(default=PUBLICATIONS)
        publications.fetch(fetcher, SERIES, max_pages=1, on_page=None)
        asked = unquote(fetcher.urls[0])
        assert f"ubuntu/{SERIES}" in asked
        assert "getPublishedSources" in asked


class TestPackageSets:
    def test_parses_a_real_response(self):
        sets, nxt = packagesets.parse_sets(PACKAGE_SETS)
        assert sets
        assert all(url.endswith("getSourcesIncluded") for _, url in sets)
        assert nxt is None

    def test_maps_sources_to_their_sets(self):
        fetcher = Fetcher(
            {"getBySeries": PACKAGE_SETS, "getSourcesIncluded": SOURCES_INCLUDED}
        )
        membership = packagesets.fetch(fetcher, SERIES)
        first = json.loads(SOURCES_INCLUDED)[0]
        assert first in membership
        assert len(membership[first]) >= 1

    def test_a_failing_set_does_not_lose_the_others(self):
        names = json.loads(PACKAGE_SETS)["entries"]
        boom = OSError("launchpad said no")
        fetcher = Fetcher(
            {
                "getBySeries": PACKAGE_SETS,
                names[0]["self_link"]: boom,
                "getSourcesIncluded": SOURCES_INCLUDED,
            }
        )
        assert packagesets.fetch(fetcher, SERIES)

    def test_skips_entries_without_a_link(self):
        raw = json.dumps({"entries": [{"name": "orphan"}]}).encode()
        sets, _ = packagesets.parse_sets(raw)
        assert sets == []


class TestTeams:
    def test_parses_a_real_response(self):
        names, nxt = teams.parse_page(TEAM_PACKAGES)
        assert "apparmor" in names
        assert nxt is None

    def test_follows_pagination(self):
        page1 = json.dumps(
            {
                "entries": [{"name": "first"}],
                "next_collection_link": "https://api.launchpad.net/devel/~t?ws.start=75",
            }
        ).encode()
        fetcher = Fetcher({"ws.start": TEAM_PACKAGES}, default=page1)
        found = teams.fetch(fetcher, ["t"], on_team=None)
        assert "first" in found and "apparmor" in found

    def test_the_first_page_is_not_the_whole_answer(self):
        """75 entries is Launchpad's page size, not a package count."""
        fetcher = Fetcher({"ws.start": TEAM_PACKAGES}, default=TEAM_PACKAGES)
        teams.fetch(fetcher, ["t"], on_team=None)
        assert any("ws.start" not in u for u in fetcher.urls)

    def test_keep_filters_to_the_packages_that_matter(self):
        fetcher = Fetcher(default=TEAM_PACKAGES)
        found = teams.fetch(fetcher, ["t"], keep={"apparmor"}, on_team=None)
        assert set(found) == {"apparmor"}

    def test_one_failing_team_does_not_lose_the_rest(self):
        fetcher = Fetcher(
            {"~broken": OSError("503"), "~fine": TEAM_PACKAGES}, default=TEAM_PACKAGES
        )
        found = teams.fetch(fetcher, ["broken", "fine"], on_team=None)
        assert "apparmor" in found
        assert found["apparmor"] == ("fine",)

    def test_a_package_can_have_several_teams(self):
        fetcher = Fetcher(default=TEAM_PACKAGES)
        found = teams.fetch(fetcher, ["a", "b"], on_team=None)
        assert found["apparmor"] == ("a", "b")

    def test_stopping_early_is_an_error_not_a_short_team(self):
        """strict mode promises to reject partial ownership, and a truncated
        walk looks exactly like a team that owns fewer packages."""
        endless = json.dumps(
            {
                "entries": [{"name": "a"}],
                "next_collection_link": "https://api.launchpad.net/devel/~t?ws.start=75",
            }
        ).encode()
        with pytest.raises(teams.TruncatedTeam):
            teams.packages(Fetcher(default=endless), "t", max_pages=2)

    def test_a_truncated_team_fails_the_whole_strict_fetch(self):
        endless = json.dumps(
            {
                "entries": [{"name": "a"}],
                "next_collection_link": "https://api.launchpad.net/devel/~t?ws.start=75",
            }
        ).encode()
        with pytest.raises(teams.TruncatedTeam):
            teams.fetch(Fetcher(default=endless), ["t"], strict=True, on_team=None)

    def test_does_not_raise_ws_size(self):
        """ws.size is rejected with 503 on this operation."""
        assert "ws.size" not in teams.subscriber_url("ubuntu-security")


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://api.launchpad.net/devel/~doko", "doko"),
        ("https://api.launchpad.net/devel/~pkg-games-devel", "pkg-games-devel"),
        (None, None),
        ("", None),
    ],
)
def test_person_name(link, expected):
    assert publications.person_name(link) == expected


class FakeConnection:
    """Stands in for UDD. Records the query so the shape stays pinned."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries: list[tuple] = []
        self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class TestDebianUploads:
    """The metric is when Debian first got ahead, not the age of its current
    version: dating the current version understates 285 of 780 candidates."""

    UPLOADS: ClassVar = [
        ("cron", "3.0-1", datetime(2020, 1, 1, tzinfo=UTC)),
        ("cron", "3.0-2", datetime(2021, 6, 1, tzinfo=UTC)),
        ("cron", "3.0-3", datetime(2026, 9, 1, tzinfo=UTC)),
    ]

    def test_dates_the_first_upload_that_overtook_ubuntu(self):
        conn = FakeConnection(self.UPLOADS)
        found = debian_uploads.fetch(
            {"cron": ("3.0-1ubuntu1", "3.0-3")}, connect=lambda: conn
        )
        # 3.0-2 is the first newer than 3.0-1ubuntu1, not the current 3.0-3.
        assert found == {"cron": datetime(2021, 6, 1, tzinfo=UTC)}

    def test_a_later_debian_upload_does_not_reset_the_wait(self):
        first = debian_uploads.fetch(
            {"cron": ("3.0-1ubuntu1", "3.0-2")},
            connect=lambda: FakeConnection(self.UPLOADS),
        )
        later = debian_uploads.fetch(
            {"cron": ("3.0-1ubuntu1", "3.0-3")},
            connect=lambda: FakeConnection(self.UPLOADS),
        )
        assert first == later

    def test_ignores_versions_beyond_the_current_debian_one(self):
        """An abandoned epoch outranks Ubuntu forever without being on the
        path to what Debian ships now."""
        uploads = [
            ("openldap", "1:1.2.3-1", datetime(1999, 6, 9, tzinfo=UTC)),
            ("openldap", "2.6.14+dfsg-2", datetime(2026, 8, 25, tzinfo=UTC)),
        ]
        found = debian_uploads.fetch(
            {"openldap": ("2.6.13+dfsg-1ubuntu3", "2.6.14+dfsg-2")},
            connect=lambda: FakeConnection(uploads),
        )
        assert found == {"openldap": datetime(2026, 8, 25, tzinfo=UTC)}

    def test_a_source_with_no_qualifying_upload_is_absent(self):
        conn = FakeConnection([("cron", "3.0-1", datetime(2020, 1, 1, tzinfo=UTC))])
        found = debian_uploads.fetch(
            {"cron": ("9.0-1ubuntu1", "9.0-2")}, connect=lambda: conn
        )
        assert found == {}

    def test_asks_for_every_source_in_one_query(self):
        """774 round trips would take minutes; one takes about a second."""
        conn = FakeConnection()
        debian_uploads.fetch(
            {"a": ("1-1", "1-2"), "b": ("2-1", "2-2")}, connect=lambda: conn
        )
        assert len(conn.queries) == 1
        assert conn.queries[0][1][0] == ("a", "b")

    def test_restricts_the_lookup_to_the_candidate_suite(self):
        conn = FakeConnection()
        debian_uploads.fetch({"a": ("1-1", "1-2")}, connect=lambda: conn)
        sql, params = conn.queries[0]
        assert "distribution ~" in sql
        assert params[1] == r"(^| )(unstable|sid)( |$)"

    def test_unstable_matches_its_historical_aliases(self):
        assert debian_uploads.suite_pattern("unstable") == r"(^| )(unstable|sid)( |$)"
        assert debian_uploads.suite_pattern("testing") == r"(^| )(testing)( |$)"

    def test_no_candidates_means_no_connection(self):
        opened = []
        assert debian_uploads.fetch({}, connect=lambda: opened.append(1)) == {}
        assert opened == []

    def test_closes_the_connection_even_when_the_query_fails(self):
        class Boom(FakeConnection):
            def execute(self, sql, params=None):
                raise RuntimeError("udd is down")

        conn = Boom()
        with pytest.raises(RuntimeError):
            debian_uploads.fetch({"a": ("1-1", "1-2")}, connect=lambda: conn)
        assert conn.closed

    def test_bounds_how_long_a_query_may_run(self):
        """connect_timeout only covers the handshake; a stalled query would
        otherwise run until the workflow itself is killed."""
        assert debian_uploads.STATEMENT_TIMEOUT_MS > 0
