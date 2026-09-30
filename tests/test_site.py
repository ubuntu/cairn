from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cairn.build.site import (
    MARKER,
    NotASiteDirectory,
    age_buckets,
    build,
    group_by_package_set,
    group_by_team,
    group_by_uploader,
    merge_rows,
    overview,
    render,
)
from cairn.core.health import SourceHealth
from cairn.core.log import Event, EventType, replay
from cairn.ingest.base import Kind, Signal
from cairn.ingest.metadata import PackageMetadata, Snapshot

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=90)


def signal(
    name,
    *,
    component="main",
    new_upstream=True,
    uploader="doko",
    published=T0,
    package_sets=(),
    teams=(),
    kind=Kind.NEEDS_MERGE,
):
    payload = {
        "ubuntu_version": "1.0-1ubuntu1",
        "debian_version": "1.0-2",
        "base_version": "1.0-1",
        "component": component,
        "new_upstream": new_upstream,
    }
    if uploader:
        payload["ubuntu_uploader"] = uploader
    if published:
        payload["ubuntu_uploaded"] = published.isoformat()
    if package_sets:
        payload["package_sets"] = list(package_sets)
    if teams:
        payload["teams"] = list(teams)
    return Signal(
        kind=kind,
        source_package=name,
        series="stonking",
        payload=payload,
        url=f"https://launchpad.net/ubuntu/+source/{name}",
    )


def owned(*specs, collected=NOW):
    """owned(("cron", {"uploader": "doko"})) -> a metadata snapshot."""
    packages = {}
    for name, attrs in specs:
        published = attrs.get("published", T0)
        uploaded = attrs.get("debian_uploaded", published)
        packages[name] = PackageMetadata(
            uploader=attrs.get("uploader", "doko"),
            published=published.isoformat() if published else None,
            debian_uploaded=uploaded.isoformat() if uploaded else None,
            package_sets=tuple(attrs.get("package_sets", ())),
            teams=tuple(attrs.get("teams", ())),
        )
    return Snapshot(collected_at=collected, packages=packages)


def just(name="cron", **attrs):
    return owned((name, attrs))


def state_of(*events):
    return replay(events)


def opened(sig, ts=T0):
    return Event.of(EventType.OPENED, sig, source="merges", ts=ts)


def resolved(sig, ts):
    return Event.of(EventType.RESOLVED, sig, source="merges", ts=ts)


def healthy(now=NOW):
    return {"merges": SourceHealth("merges", last_attempt=now, last_success=now)}


def stale_health(now=NOW):
    return {
        "merges": SourceHealth(
            "merges", last_attempt=now, last_success=now - timedelta(days=5)
        )
    }


class TestMergeRows:
    def test_reports_active_signals(self):
        rows = merge_rows(state_of(opened(signal("cron"))), now=NOW)
        assert [r.package for r in rows] == ["cron"]

    def test_omits_resolved_signals(self):
        sig = signal("cron")
        assert merge_rows(state_of(opened(sig), resolved(sig, NOW)), now=NOW) == []

    def test_omits_other_kinds(self):
        meta = just()
        state = state_of(opened(signal("x", kind=Kind.NBS)))
        assert merge_rows(state, meta, now=NOW) == []

    def test_alphabetical(self):
        """Publication dates are per-series, so they cannot carry the order."""
        meta = just()
        state = state_of(
            opened(signal("zlib")),
            opened(signal("acl")),
        )
        assert [r.package for r in merge_rows(state, meta, now=NOW)] == ["acl", "zlib"]

    def test_published_days_is_none_without_a_date(self):
        rows = merge_rows(state_of(opened(signal("x"))), now=NOW)
        assert rows[0].behind_days(NOW) is None

    def test_carries_enrichment_through(self):
        meta = just("cron", package_sets=("core",), teams=("ubuntu-server",))
        state = state_of(opened(signal("cron")))
        row = merge_rows(state, meta, now=NOW)[0]
        assert row.uploader == "doko"
        assert row.package_sets == ("core",)
        assert row.teams == ("ubuntu-server",)

    def test_tolerates_a_bare_payload(self):
        bare = Signal(kind=Kind.NEEDS_MERGE, source_package="x", series="stonking")
        row = merge_rows(state_of(opened(bare)), now=NOW)[0]
        assert row.uploader is None
        assert row.package_sets == ()
        assert row.teams == ()

    def test_recurrence_is_counted(self):
        meta = just()
        sig = signal("cron")
        state = state_of(
            opened(sig), resolved(sig, T0 + timedelta(days=1)), opened(sig)
        )
        assert merge_rows(state, meta, now=NOW)[0].occurrences == 2


class TestAgeBuckets:
    def test_assigns_each_row_once(self):
        state = state_of(opened(signal("a")), opened(signal("b")), opened(signal("c")))
        meta = owned(
            ("a", {"debian_uploaded": NOW - timedelta(days=3)}),
            ("b", {"debian_uploaded": NOW - timedelta(days=200)}),
            ("c", {"debian_uploaded": NOW - timedelta(days=900)}),
        )
        rows = merge_rows(state, meta, now=NOW)
        buckets = {b.label: b.count for b in age_buckets(rows, now=NOW)}
        assert buckets["under a month"] == 1
        assert buckets["6-12 months"] == 1
        assert buckets["over 2 years"] == 1

    def test_rows_without_a_date_are_not_bucketed(self):
        rows = merge_rows(state_of(opened(signal("x"))), now=NOW)
        assert sum(b.count for b in age_buckets(rows, now=NOW)) == 0


class TestOverview:
    def test_counts_work_types(self):
        meta = just()
        state = state_of(
            opened(signal("a", new_upstream=True)),
            opened(signal("b", new_upstream=False)),
        )
        o = overview(state, merge_rows(state, meta, now=NOW), healthy(), now=NOW)
        assert (o.new_upstream, o.revision_only) == (1, 1)

    def test_reports_when_cairn_last_collected(self):
        meta = just()
        state = state_of(opened(signal("a")))
        o = overview(state, merge_rows(state, meta, now=NOW), healthy(), now=NOW)
        assert o.collected == NOW

    def test_counts_recent_movement(self):
        meta = just()
        state = state_of(opened(signal("old"), ts=T0), opened(signal("new"), ts=NOW))
        o = overview(state, merge_rows(state, meta, now=NOW), healthy(), now=NOW)
        assert o.opened_recently == 1

    def test_counts_missing_uploaders(self):
        meta = just("x", uploader=None)
        state = state_of(opened(signal("x")))
        o = overview(state, merge_rows(state, meta, now=NOW), healthy(), now=NOW)
        assert o.unknown_uploader == 1


class TestGrouping:
    def test_uploader_groups_are_largest_first(self):
        meta = owned(
            ("a", {"uploader": "alice"}),
            ("b", {"uploader": "bob"}),
            ("c", {"uploader": "bob"}),
        )
        state = state_of(
            opened(signal("a")),
            opened(signal("b")),
            opened(signal("c")),
        )
        groups = group_by_uploader(merge_rows(state, meta, now=NOW))
        assert [g.title for g in groups] == ["bob", "alice"]

    def test_missing_uploaders_get_their_own_group_last(self):
        meta = owned(("a", {"uploader": "alice"}), ("b", {"uploader": None}))
        state = state_of(
            opened(signal("a")),
            opened(signal("b")),
        )
        groups = group_by_uploader(merge_rows(state, meta, now=NOW))
        assert groups[-1].name == "no-uploader-recorded"
        assert groups[-1].href == "uploaders/no-uploader-recorded.html"

    def test_package_sets_and_teams_stay_separate(self):
        """AGENTS.md section 5: one name can exist on both axes."""
        meta = just(
            "gnome-shell", package_sets=("ubuntu-desktop",), teams=("ubuntu-desktop",)
        )
        state = state_of(opened(signal("gnome-shell")))
        rows = merge_rows(state, meta, now=NOW)
        sets = group_by_package_set(rows)
        teams = group_by_team(rows)
        assert [g.href for g in sets] == ["sets/ubuntu-desktop.html"]
        assert [g.href for g in teams] == ["teams/ubuntu-desktop.html"]

    def test_a_package_can_be_in_several_sets(self):
        meta = just("x", package_sets=("core", "kernel"))
        state = state_of(opened(signal("x")))
        groups = group_by_package_set(merge_rows(state, meta, now=NOW))
        assert {g.title for g in groups} == {"core", "kernel"}

    def test_packages_without_a_team_produce_no_team_group(self):
        meta = just()
        state = state_of(opened(signal("x")))
        assert group_by_team(merge_rows(state, meta, now=NOW)) == []


class TestRender:
    def test_index_summarises_groups_rather_than_listing_packages(self):
        """774 packages inline is unreadable; the index links to group pages."""
        meta = owned(("cron", {"uploader": "alice"}), ("qemu", {"uploader": "alice"}))
        state = state_of(
            opened(signal("cron")),
            opened(signal("qemu")),
        )
        html = render(state, healthy(), meta, now=NOW)
        assert "uploaders/alice.html" in html
        assert "cron" not in html

    def test_ships_no_javascript(self):
        """AGENTS.md: server-rendered, no JS, no build step."""
        meta = just()
        html = render(state_of(opened(signal("cron"))), healthy(), meta, now=NOW)
        assert "<script" not in html.lower()

    def test_uses_the_pinned_vanilla_release(self):
        meta = just()
        html = render(state_of(opened(signal("cron"))), healthy(), meta, now=NOW)
        assert "vanilla_framework_version_4.59.0.min.css" in html

    def test_navbar_carries_the_ubuntu_logo_and_name(self):
        meta = just()
        html = render(state_of(opened(signal("cron"))), healthy(), meta, now=NOW)
        assert "82818827-CoF_white.svg" in html
        assert "Ubuntu Cairn" in html

    def test_the_logo_tag_holds_the_image_not_the_title(self):
        """Nesting the title inside the tag draws it over the orange square."""
        meta = just()
        html = render(state_of(opened(signal("cron"))), healthy(), meta, now=NOW)
        tag = html.split("p-navigation__logo-tag")[1].split("</div>")[0]
        assert "p-navigation__logo-icon" in tag
        assert "p-navigation__logo-title" not in tag

    def test_escapes_untrusted_names(self, tmp_path):
        """Package and uploader names reach the templates verbatim."""
        meta = just("evil", uploader="<script>alert(1)</script>")
        sig = signal("evil")
        build(state_of(opened(sig)), healthy(), meta, out=tmp_path, now=NOW)
        everything = "".join(p.read_text() for p in tmp_path.rglob("*.html"))
        assert "<script>alert(1)</script>" not in everything
        assert "&lt;script&gt;" in everything

    def test_a_healthy_run_shows_no_banner_and_no_relative_time(self):
        """The page is static, so "N min ago" is frozen at build time; the
        footer carries the absolute build time instead."""
        meta = just()
        html = render(state_of(opened(signal("cron"))), healthy(), meta, now=NOW)
        assert "p-notification--positive" not in html
        assert "last collected" not in html
        assert "Built 2026-04-01 00:00 UTC" in html
        assert "1 Apr 2026" in html
        assert "data collected" in html

    def test_tab_titles(self, tmp_path):
        build(
            state_of(opened(signal("cron"))), healthy(), just(), out=tmp_path, now=NOW
        )
        assert "<title>Cairn | Merges</title>" in (tmp_path / "index.html").read_text()
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "<title>Cairn | Merges for doko</title>" in page

    def test_a_stale_source_still_raises_a_banner(self):
        meta = just()
        html = render(state_of(opened(signal("cron"))), stale_health(), meta, now=NOW)
        assert "p-notification--caution" in html
        assert "not about the packages" in html

    def test_explains_its_own_vocabulary(self):
        meta = just()
        html = render(state_of(opened(signal("cron"))), healthy(), meta, now=NOW)
        assert "New upstream" in html
        assert "Merged, then back" in html

    def test_empty_log_renders_a_page_not_a_crash(self):
        assert "<table" not in render({}, healthy(), Snapshot(), now=NOW)


class TestBuild:
    def test_writes_an_index(self, tmp_path):
        meta = just()
        out = build(
            state_of(opened(signal("cron"))),
            healthy(),
            meta,
            out=tmp_path / "s",
            now=NOW,
        )
        assert out.name == "index.html"
        assert "uploaders/doko.html" in out.read_text()

    def test_writes_a_page_per_group(self, tmp_path):
        meta = just(
            "cron", uploader="doko", package_sets=("core",), teams=("ubuntu-server",)
        )
        state = state_of(opened(signal("cron")))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        for path in (
            "uploaders/doko.html",
            "sets/core.html",
            "teams/ubuntu-server.html",
        ):
            assert (tmp_path / path).exists(), path
            assert "cron" in (tmp_path / path).read_text()

    def test_links_to_both_trackers(self, tmp_path):
        """Launchpad for the Ubuntu side, tracker.debian.org for the Debian."""
        meta = just()
        build(state_of(opened(signal("cron"))), healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "https://launchpad.net/ubuntu/+source/cron" in page
        assert "https://tracker.debian.org/pkg/cron" in page

    def test_versions_link_to_that_upload(self, tmp_path):
        build(
            state_of(opened(signal("cron"))), healthy(), just(), out=tmp_path, now=NOW
        )
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert 'href="https://launchpad.net/ubuntu/+source/cron/1.0-1ubuntu1"' in page
        assert (
            '<a href="https://tracker.debian.org/pkg/cron" target="_blank"'
            ' rel="noopener noreferrer"><code>1.0-2</code></a>'
        ) in page

    def test_version_links_quote_the_epoch_only(self):
        rows = merge_rows(state_of(opened(signal("cron"))), just(), now=NOW)
        row = replace(
            rows[0],
            ubuntu_version="1:0.9.14.2+25.10-0ubuntu3",
        )
        assert row.ubuntu_version_url.endswith("/cron/1%3A0.9.14.2+25.10-0ubuntu3")

    def test_the_tracker_link_sits_under_the_package_name(self, tmp_path):
        meta = just()
        build(state_of(opened(signal("cron"))), healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "Debian tracker" in page
        assert "p-text--small" in page

    def test_the_tracker_link_is_marked_as_external(self, tmp_path):
        """Muted colour is not the only cue: the icon carries it too."""
        meta = just()
        build(state_of(opened(signal("cron"))), healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "p-icon--external-link" in page

    def test_tracker_links_survive_awkward_names(self, tmp_path):
        meta = just("gtk+3.0")
        build(
            state_of(opened(signal("gtk+3.0"))), healthy(), meta, out=tmp_path, now=NOW
        )
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "https://tracker.debian.org/pkg/gtk+3.0" in page

    def test_group_pages_link_back_up_a_directory(self, tmp_path):
        meta = just()
        build(state_of(opened(signal("cron"))), healthy(), meta, out=tmp_path, now=NOW)
        assert "../index.html" in (tmp_path / "uploaders/doko.html").read_text()


class TestCrossReferences:
    """Each group page shows the other ownership axis, not its own."""

    def test_an_uploader_page_shows_both_other_axes(self, tmp_path):
        meta = just(
            "cron", uploader="doko", teams=("ubuntu-server",), package_sets=("core",)
        )
        state = state_of(opened(signal("cron")))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "Subscribed team" in page and "Package set" in page
        assert "../teams/ubuntu-server.html" in page
        assert "../sets/core.html" in page

    def test_a_page_omits_its_own_axis(self, tmp_path):
        """A column of one repeated value carries no information."""
        meta = just(
            "cron", uploader="doko", teams=("ubuntu-server",), package_sets=("core",)
        )
        state = state_of(opened(signal("cron")))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        assert "Last uploader" not in (tmp_path / "uploaders/doko.html").read_text()
        assert (
            "Subscribed team" not in (tmp_path / "teams/ubuntu-server.html").read_text()
        )
        assert "Package set" not in (tmp_path / "sets/core.html").read_text()

    def test_a_team_page_names_the_last_uploader(self, tmp_path):
        meta = just("cron", uploader="doko", teams=("ubuntu-server",))
        state = state_of(opened(signal("cron")))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "teams/ubuntu-server.html").read_text()
        assert "Last uploader" in page and "Package set" in page
        assert "../uploaders/doko.html" in page

    def test_a_package_set_page_names_the_last_uploader(self, tmp_path):
        meta = just("cron", uploader="doko", package_sets=("core",))
        state = state_of(opened(signal("cron")))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "sets/core.html").read_text()
        assert "../uploaders/doko.html" in page

    def test_a_package_with_no_team_says_so(self, tmp_path):
        meta = just()
        build(state_of(opened(signal("cron"))), healthy(), meta, out=tmp_path, now=NOW)
        assert "none" in (tmp_path / "uploaders/doko.html").read_text()

    def test_an_unknown_uploader_says_so(self, tmp_path):
        meta = just("cron", uploader=None, teams=("ubuntu-server",))
        state = state_of(opened(signal("cron")))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        assert "unknown" in (tmp_path / "teams/ubuntu-server.html").read_text()

    def test_creates_the_directory(self, tmp_path):
        meta = just()
        target = tmp_path / "deep" / "site"
        build(state_of(opened(signal("cron"))), healthy(), meta, out=target, now=NOW)
        assert (target / "index.html").exists()

    def test_rebuilding_overwrites(self, tmp_path):
        out = tmp_path / "s"
        build(
            state_of(opened(signal("cron"))),
            healthy(),
            just("cron", uploader="alice"),
            out=out,
            now=NOW,
        )
        build(
            state_of(opened(signal("qemu"))),
            healthy(),
            just("qemu", uploader="bob"),
            out=out,
            now=NOW,
        )
        index = (out / "index.html").read_text()
        assert "uploaders/bob.html" in index
        assert "uploaders/alice.html" not in index


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, "just now"), (5, "5 d ago"), (800, "2 y ago")],
)
def test_debian_upload_age_reads_as_text(days, expected, tmp_path):
    meta = just("cron", debian_uploaded=NOW - timedelta(days=days))
    state = state_of(opened(signal("cron")))
    build(state, healthy(), meta, out=tmp_path, now=NOW)
    assert expected in (tmp_path / "uploaders/doko.html").read_text()


class TestComponent:
    def test_group_pages_name_the_component(self, tmp_path):
        """main and universe carry different rules, so it is worth scanning."""
        state = state_of(
            opened(signal("cron", component="main")),
            opened(signal("qemu", component="universe")),
        )
        meta = owned(("cron", {}), ("qemu", {}))
        build(state, healthy(), meta, out=tmp_path, now=NOW)
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert ">Component</th>" in page
        cells = re.findall(r'data-heading="Component"[^>]*>\s*([a-z]+)', page)
        assert sorted(cells) == ["main", "universe"]

    def test_a_missing_component_says_unknown(self, tmp_path):
        state = state_of(opened(signal("cron", component=None)))
        build(state, healthy(), just(), out=tmp_path, now=NOW)
        page = (tmp_path / "uploaders/doko.html").read_text()
        assert "unknown" in page


def test_footer_carries_the_copyright_for_the_build_year():
    """Taken from the render clock, so a daily rebuild keeps it current."""
    html = render(state_of(opened(signal("cron"))), healthy(), just(), now=NOW)
    assert f"Copyright &copy; {NOW.year} Canonical Ltd." in html


def test_footer_links_to_the_issue_tracker():
    html = render(state_of(opened(signal("cron"))), healthy(), just(), now=NOW)
    assert "https://github.com/ubuntu/cairn/issues" in html
    assert "Report a bug" in html


class TestExternalLinks:
    """Links off the site open in a new tab, and say so where there is no icon."""

    def all_links(self, tmp_path):
        return "".join(p.read_text() for p in tmp_path.rglob("*.html"))

    def test_every_external_link_opens_in_a_new_tab(self, tmp_path):
        build(
            state_of(opened(signal("cron"))), healthy(), just(), out=tmp_path, now=NOW
        )
        html = self.all_links(tmp_path)
        for anchor in re.findall(r"<a\b[^>]*>", html):
            if 'href="http' not in anchor:
                continue
            assert 'target="_blank"' in anchor, anchor
            assert 'rel="noopener noreferrer"' in anchor, anchor

    def test_internal_links_stay_in_the_same_tab(self, tmp_path):
        build(
            state_of(opened(signal("cron"))), healthy(), just(), out=tmp_path, now=NOW
        )
        page = (tmp_path / "uploaders/doko.html").read_text()
        back = next(a for a in re.findall(r"<a\b[^>]*>", page) if "../index.html" in a)
        assert "target=" not in back

    def test_the_bug_link_announces_the_new_tab(self, tmp_path):
        build(
            state_of(opened(signal("cron"))), healthy(), just(), out=tmp_path, now=NOW
        )
        page = (tmp_path / "index.html").read_text()
        assert "opens in a new tab" in page


class TestOutputDirectorySafety:
    """build() removes the previous site, so it must be sure that is what it
    is looking at."""

    def test_refuses_a_directory_it_did_not_generate(self, tmp_path):
        precious = tmp_path / "home"
        precious.mkdir()
        (precious / "assets").mkdir()
        (precious / "assets" / "thesis.pdf").write_text("years of work")

        with pytest.raises(NotASiteDirectory):
            build(
                state_of(opened(signal("cron"))),
                healthy(),
                just(),
                out=precious,
                now=NOW,
            )
        assert (precious / "assets" / "thesis.pdf").exists()

    def test_accepts_an_empty_directory(self, tmp_path):
        empty = tmp_path / "fresh"
        empty.mkdir()
        build(state_of(opened(signal("cron"))), healthy(), just(), out=empty, now=NOW)
        assert (empty / "index.html").exists()

    def test_rebuilds_over_its_own_output(self, tmp_path):
        for _ in range(2):
            build(
                state_of(opened(signal("cron"))),
                healthy(),
                just(),
                out=tmp_path,
                now=NOW,
            )
        assert (tmp_path / MARKER).exists()

    def test_removes_a_group_page_that_lost_its_last_candidate(self, tmp_path):
        build(
            state_of(opened(signal("cron"))),
            healthy(),
            just("cron", uploader="alice"),
            out=tmp_path,
            now=NOW,
        )
        assert (tmp_path / "uploaders/alice.html").exists()

        build(
            state_of(opened(signal("qemu"))),
            healthy(),
            just("qemu", uploader="bob"),
            out=tmp_path,
            now=NOW,
        )
        assert not (tmp_path / "uploaders/alice.html").exists()
        assert (tmp_path / "uploaders/bob.html").exists()


class TestOutputDirectoryUpgrade:
    def test_adopts_a_site_built_before_the_marker(self, tmp_path):
        """Otherwise the first rebuild after upgrading fails on every site."""
        (tmp_path / "index.html").write_text("old")
        (tmp_path / "uploaders").mkdir()
        (tmp_path / "uploaders" / "alice.html").write_text("stale")
        build(
            state_of(opened(signal("cron"))), healthy(), just(), out=tmp_path, now=NOW
        )
        assert not (tmp_path / "uploaders" / "alice.html").exists()
        assert (tmp_path / MARKER).exists()

    def test_an_assets_directory_alone_is_not_a_site(self, tmp_path):
        """No index.html, so this is somebody else's folder."""
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "keep.txt").write_text("mine")
        with pytest.raises(NotASiteDirectory):
            build(
                state_of(opened(signal("cron"))),
                healthy(),
                just(),
                out=tmp_path,
                now=NOW,
            )
        assert (tmp_path / "assets" / "keep.txt").exists()
