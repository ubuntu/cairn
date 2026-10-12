"""The entry points: package pages, owners, up for grabs, trends, filters.

Added after user feedback that cairn's features were hard to find: people
arrive with a question ("what is the status of this package?", "what is
mine?", "what can I work on?") and the boards only answered by source.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta

from cairn.build.site import (
    PLUS_ONE,
    History,
    build,
    merge_rows,
    package_views,
    stuck_rows,
    timeline,
    waiting_for,
)
from cairn.build.trends import trends
from cairn.build.work import ABANDONED_DAYS, up_for_grabs
from cairn.core.health import SourceHealth
from cairn.core.log import Event, EventType, replay
from cairn.ingest.base import Kind, Signal
from cairn.ingest.metadata import PackageMetadata, PublishedVersion, Snapshot

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
T0 = NOW - timedelta(days=10)


def stuck(name="libssh2", *, days=40, version="1.0-2", **payload):
    base = {
        "old_version": "1.0-1",
        "new_version": version,
        "component": "main",
        "reasons": ["missing_build"],
        "tests": [],
        "missing_builds": ["riscv64"],
        "waits_for": [],
        "bugs": [],
        "hints": [],
        "in_proposed_since": (NOW - timedelta(days=days)).date().isoformat(),
    }
    return Signal(
        kind=Kind.MIGRATION_BLOCKED,
        source_package=name,
        series="stonking",
        payload=base | payload,
    )


def merge(name, *, new_upstream=False, ubuntu="1.0-1ubuntu1", debian=None):
    # The board reads new upstream from the versions, so they say the same.
    if debian is None:
        debian = "1.1-1" if new_upstream else "1.0-2"
    return Signal(
        kind=Kind.NEEDS_MERGE,
        source_package=name,
        series="stonking",
        payload={
            "ubuntu_version": ubuntu,
            "debian_version": debian,
            "base_version": "1.0-1",
            "component": "universe",
            "new_upstream": new_upstream,
        },
    )


def ev(kind, sig, ts=T0):
    source = "migration" if sig.kind is Kind.MIGRATION_BLOCKED else "merges"
    return Event.of(kind, sig, source=source, ts=ts)


def pub(version, *, uploader="kat", signer="kat"):
    return PublishedVersion(
        version=version,
        pocket="Proposed",
        uploader=uploader,
        signer=signer,
        published=None,
    )


def meta(**packages):
    return Snapshot(collected_at=NOW, packages=packages)


def healthy():
    return {
        s: SourceHealth(s, last_attempt=NOW, last_success=NOW)
        for s in ("merges", "migration")
    }


def site(tmp_path, events, owners=None):
    build(
        replay(events),
        healthy(),
        owners or meta(),
        out=tmp_path,
        now=NOW,
        series="stonking",
        history=History.of(events),
    )
    return tmp_path


def text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


class TestUpForGrabs:
    def rows(self, events, owners):
        state = replay(events)
        return (
            merge_rows(state, owners, now=NOW),
            stuck_rows(state, owners, now=NOW),
        )

    def test_a_stuck_sync_is_up_for_grabs(self):
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),))
        )
        rows, held = self.rows([ev(EventType.OPENED, stuck())], owners)
        found = up_for_grabs(rows, held, now=NOW)
        assert [r.package for r in found.syncs] == ["libssh2"]
        assert found.abandoned == []

    def test_a_settling_upload_is_left_to_its_uploader(self):
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),))
        )
        rows, held = self.rows([ev(EventType.OPENED, stuck(days=1))], owners)
        assert up_for_grabs(rows, held, now=NOW).total == 0

    def test_an_update_excuse_bug_is_not_a_claim(self):
        """Anyone can file one and it may sit unassigned; the assignee is
        not collected, so the upload stays up for grabs."""
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),)),
            old=PackageMetadata(publications=(pub("1.0-2"),)),
        )
        events = [
            ev(EventType.OPENED, stuck(bugs=[123])),
            ev(EventType.OPENED, stuck("old", bugs=[456], days=ABANDONED_DAYS)),
        ]
        rows, held = self.rows(events, owners)
        found = up_for_grabs(rows, held, now=NOW)
        assert [r.package for r in found.syncs] == ["libssh2"]
        assert [r.package for r in found.abandoned] == ["old"]

    def test_an_ubuntu_upload_is_free_only_after_the_threshold(self):
        owners = meta(
            old=PackageMetadata(publications=(pub("1.0-2"),)),
            young=PackageMetadata(publications=(pub("1.0-2"),)),
        )
        events = [
            ev(EventType.OPENED, stuck("old", days=ABANDONED_DAYS)),
            ev(EventType.OPENED, stuck("young", days=ABANDONED_DAYS - 1)),
        ]
        rows, held = self.rows(events, owners)
        found = up_for_grabs(rows, held, now=NOW)
        assert [r.package for r in found.abandoned] == ["old"]

    def test_only_packaging_only_merges_are_listed(self):
        """Not a new upstream, and not Debian's epoch over an older upstream
        either: compiz on 12 Oct 2026 was 1:0.9.14.2 in Ubuntu against
        2:0.8.18 in Debian, and was offered here as a small job."""
        events = [
            ev(EventType.OPENED, merge("small")),
            ev(EventType.OPENED, merge("big", new_upstream=True)),
            ev(
                EventType.OPENED,
                merge(
                    "compiz",
                    ubuntu="1:0.9.14.2+25.10.20250930-0ubuntu3",
                    debian="2:0.8.18-9",
                ),
            ),
            ev(
                EventType.OPENED,
                merge("procmail", ubuntu="3.24-1ubuntu6", debian="3.24+really3.22-6"),
            ),
        ]
        rows, held = self.rows(events, meta())
        by_name = {r.package: r.upstream for r in rows}
        assert by_name == {
            "small": "same",
            "big": "new",
            "compiz": "older",
            "procmail": "older",
        }
        assert [r.package for r in up_for_grabs(rows, held, now=NOW).merges] == [
            "small"
        ]

    def test_a_sync_of_unknown_age_is_not_past_settling(self):
        """No date from britney is not the same as a long wait."""
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),))
        )
        rows, held = self.rows(
            [ev(EventType.OPENED, stuck(in_proposed_since=None))], owners
        )
        assert held[0].days(NOW) is None
        assert up_for_grabs(rows, held, now=NOW).total == 0

    def test_the_page_lists_each_kind_and_says_to_check_for_proposals(self, tmp_path):
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),))
        )
        out = site(
            tmp_path,
            [ev(EventType.OPENED, stuck()), ev(EventType.OPENED, merge("cron"))],
            owners,
        )
        page = (out / "work/index.html").read_text()
        assert 'id="syncs"' in page and 'id="merges"' in page
        assert 'href="../packages/libssh2.html"' in page
        assert 'href="../packages/cron.html"' in page
        assert "look for an open merge proposal" in text(
            page.split('id="how_listed"')[1]
        )

    def test_the_page_says_how_it_is_made_and_whom_to_ask(self, tmp_path):
        """Merges and Ubuntu uploads have an owner; the page says so, and
        its thresholds come from the same constants as the rules."""
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),))
        )
        out = site(
            tmp_path,
            [ev(EventType.OPENED, stuck()), ev(EventType.OPENED, merge("cron"))],
            owners,
        )
        page_html = (out / "work/index.html").read_text()
        how = text(page_html.split('id="how_listed"')[1].split("</details>")[0])
        assert f"stuck {ABANDONED_DAYS} days or more" in how
        assert "stuck more than 3 days" in how
        assert "the person who last uploaded the package" in how
        intro = text(page_html.split("<h1")[1].split('id="how_listed"')[0])
        assert "ask them before you start" in intro
        # What is listed, nothing more: the rest is implied.
        assert "Not listed" not in how


class TestWaitingFor:
    def test_inverts_waits_for(self):
        held = stuck_rows(
            replay(
                [
                    ev(EventType.OPENED, stuck("app", waits_for=["lib"])),
                    ev(EventType.OPENED, stuck("lib")),
                ]
            ),
            now=NOW,
        )
        found = waiting_for(held)
        assert [r.package for r in found["lib"]] == ["app"]
        assert "app" not in found

    def test_the_awaited_uploads_owner_sees_who_waits(self, tmp_path):
        owners = meta(
            lib=PackageMetadata(publications=(pub("1.0-2", uploader="kat"),)),
            app=PackageMetadata(publications=(pub("1.0-2", uploader="bob"),)),
        )
        out = site(
            tmp_path,
            [
                ev(EventType.OPENED, stuck("app", waits_for=["lib"])),
                ev(EventType.OPENED, stuck("lib")),
            ],
            owners,
        )
        mine = (out / "holding/uploaders/kat.html").read_text()
        section = mine.split('id="waiting-on-yours"')[1]
        assert "migration/uploaders/bob.html#app" in section
        # "Waits for" points back at the owner's own stuck row.
        assert 'href="../../migration/uploaders/kat.html#lib"' in section
        assert not (out / "holding/uploaders/bob.html").exists()


class TestPackagePages:
    def test_every_package_in_the_log_gets_a_page_resolved_ones_too(self, tmp_path):
        gone = merge("gone")
        out = site(
            tmp_path,
            [
                ev(EventType.OPENED, gone),
                ev(EventType.RESOLVED, gone, T0 + timedelta(days=2)),
                ev(EventType.OPENED, merge("cron")),
            ],
        )
        page = text((out / "packages/gone.html").read_text())
        assert "No merge needed" in page
        assert "No longer a merge candidate" in page
        assert (out / "packages/cron.html").exists()

    def test_a_test_holding_others_up_gets_a_page(self, tmp_path):
        """Its owners need to find it by name, though it has no signal."""
        sig = stuck(
            reasons=["regression"],
            tests=[
                {"test": "curl", "version": "8.0-1", "results": {"s390x": "regression"}}
            ],
        )
        out = site(tmp_path, [ev(EventType.OPENED, sig)])
        page = text((out / "packages/curl.html").read_text())
        assert "Holding back other uploads" in page
        assert "libssh2" in page

    def test_says_what_britney_cannot_across_reuploads(self, tmp_path):
        events = [
            ev(EventType.OPENED, stuck(version="1.0-2", days=2)),
            ev(
                EventType.UPDATED,
                stuck(version="1.0-3", days=1),
                T0 + timedelta(days=4),
            ),
        ]
        out = site(tmp_path, events)
        page = text((out / "packages/libssh2.html").read_text())
        assert "stuck through 2 different uploads" in page
        assert f"since {T0:%Y-%m-%d}" in page
        assert "New upload 1.0-3 , still stuck." in page

    def test_the_package_name_on_a_board_opens_its_page(self, tmp_path):
        out = site(tmp_path, [ev(EventType.OPENED, merge("cron"))])
        page = (out / "merges/index.html").read_text()
        assert '<a href="../packages/cron.html">cron</a>' in page

    def test_every_internal_link_on_a_package_page_resolves(self, tmp_path):
        out = site(
            tmp_path,
            [
                ev(EventType.OPENED, stuck(waits_for=["zlib"])),
                ev(EventType.OPENED, stuck("zlib")),
                ev(EventType.OPENED, merge("libssh2")),
            ],
            meta(libssh2=PackageMetadata(teams=("t",), package_sets=("core",))),
        )
        for page in (out / "packages").glob("*.html"):
            for href in re.findall(r'href="([^"#?]+)', page.read_text()):
                if href.startswith("http"):
                    continue
                assert (page.parent / href).resolve().exists(), (page, href)


def test_timeline_skips_updates_that_change_nothing_tracked():
    sig = stuck()
    noisy = stuck(
        tests=[{"test": "x", "version": "1", "results": {"amd64": "running"}}]
    )
    moments = timeline(
        [
            ev(EventType.OPENED, sig),
            ev(EventType.UPDATED, noisy, T0 + timedelta(days=1)),
        ]
    )
    assert [m.event for m in moments] == [EventType.OPENED]


class TestTrends:
    def test_counts_after_each_run(self):
        a, b = merge("a"), merge("b")
        found = trends(
            [
                ev(EventType.OPENED, a, T0),
                ev(EventType.OPENED, b, T0),
                ev(EventType.RESOLVED, a, T0 + timedelta(days=1)),
            ]
        ).of(Kind.NEEDS_MERGE)
        assert [p.count for p in found.points] == [2, 1]
        assert found.change == -1

    def test_a_run_that_changes_nothing_adds_no_point(self):
        a = merge("a")
        found = trends(
            [
                ev(EventType.OPENED, a, T0),
                ev(EventType.UPDATED, a, T0 + timedelta(days=1)),
            ]
        ).of(Kind.NEEDS_MERGE)
        assert len(found.points) == 1
        assert not found.drawable

    def test_held_flat_to_the_last_collection_not_to_the_build(self):
        found = trends([ev(EventType.OPENED, merge("a"), T0)]).of(Kind.NEEDS_MERGE)
        extended = found.until(NOW)
        assert extended.drawable
        assert extended.last.ts == NOW and extended.last.count == 1

    def test_the_axis_starts_at_zero(self):
        """A change of 9 on 780 must not be drawn as a cliff."""
        a, b = merge("a"), merge("b")
        found = trends(
            [
                ev(EventType.OPENED, a, T0),
                ev(EventType.OPENED, b, T0),
                ev(EventType.RESOLVED, a, T0 + timedelta(days=1)),
            ]
        ).of(Kind.NEEDS_MERGE)
        path = found.path(100, 100, pad=0)
        # Two of a peak of two sits at the top; one sits half way down.
        assert path.startswith("M0.0,0.0") and "V50.0" in path

    def test_boards_draw_a_trend_with_its_numbers_in_words(self, tmp_path):
        a, b = merge("a"), merge("b")
        out = site(
            tmp_path,
            [
                ev(EventType.OPENED, a, T0),
                ev(EventType.OPENED, b, T0),
                ev(EventType.RESOLVED, a, T0 + timedelta(days=1)),
            ],
        )
        page = text((out / "merges/index.html").read_text())
        assert (
            f"2 open candidates on {T0:%Y-%m-%d}, 1 on {NOW:%Y-%m-%d} (1 fewer)" in page
        )


class TestOwners:
    def test_a_team_and_a_set_sharing_a_name_stay_apart(self, tmp_path):
        """AGENTS.md section 5: ubuntu-desktop is both, covering different
        packages."""
        owners = meta(
            cron=PackageMetadata(teams=("ubuntu-desktop",)),
            gdm=PackageMetadata(package_sets=("ubuntu-desktop",)),
        )
        out = site(
            tmp_path,
            [ev(EventType.OPENED, merge("cron")), ev(EventType.OPENED, merge("gdm"))],
            owners,
        )
        home = (out / "index.html").read_text()
        for axis, path in (("team", "teams"), ("package set", "sets")):
            assert (
                f'value="ubuntu-desktop ({axis})" '
                f'data-href="owners/{path}/ubuntu-desktop.html"'
            ) in home
        directory = (out / "owners/index.html").read_text()
        assert directory.count("ubuntu-desktop.html") >= 2

    def test_owner_pages_tab_between_boards(self, tmp_path):
        owners = meta(libssh2=PackageMetadata(teams=("t",)))
        out = site(
            tmp_path,
            [ev(EventType.OPENED, stuck()), ev(EventType.OPENED, merge("libssh2"))],
            owners,
        )
        merges = (out / "merges/teams/t.html").read_text()
        tabs = merges.split('class="p-tabs"')[1].split("</nav>")[0]
        assert 'href="../../migration/teams/t.html"' in tabs
        assert 'aria-current="page"' in tabs.split("Merges (1)")[0]

    def test_package_sets_show_their_share_teams_do_not(self, tmp_path):
        """Set membership is collected in full; team subscriptions only for
        packages cairn already tracks, so a team total would undercount."""
        owners = meta(
            cron=PackageMetadata(package_sets=("core",), teams=("t",)),
            bash=PackageMetadata(package_sets=("core",)),
            dash=PackageMetadata(package_sets=("core",)),
            zsh=PackageMetadata(package_sets=("core",)),
        )
        out = site(tmp_path, [ev(EventType.OPENED, merge("cron"))], owners)
        assert "1 of 4 packages in this set" in text(
            (out / "merges/sets/core.html").read_text()
        )
        assert "cairn-share" not in (out / "merges/teams/t.html").read_text()

    def test_plus_one_is_an_owner_like_any_other(self, tmp_path):
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),))
        )
        out = site(tmp_path, [ev(EventType.OPENED, stuck())], owners)
        directory = (out / "owners/index.html").read_text()
        assert f"migration/uploaders/{PLUS_ONE}.html" in directory


class TestFiltersAndSorting:
    def test_rows_carry_what_the_filters_select_on(self, tmp_path):
        out = site(
            tmp_path,
            [ev(EventType.OPENED, merge("cron", new_upstream=True))],
            meta(cron=PackageMetadata(uploader="doko", teams=("t",))),
        )
        row = re.search(
            r'<tr id="cron"[^>]*>', (out / "merges/index.html").read_text()
        )[0]
        assert 'data-f-component="universe"' in row
        assert 'data-f-upstream="new"' in row
        assert "doko" in row.split("data-f-q=")[1]

    def test_stats_link_to_the_filtered_list(self, tmp_path):
        out = site(
            tmp_path,
            [
                ev(EventType.OPENED, merge("cron", new_upstream=True)),
                ev(EventType.OPENED, merge("bash")),
            ],
        )
        index = (out / "merges/index.html").read_text()
        assert 'href="?upstream=new#all_candidates"' in index
        assert 'href="?upstream=same#all_candidates"' in index
        # Every filter link names an option the filter on the page offers:
        # the field names and values are a URL contract.
        links = re.findall(r'href="\?(\w+)=([\w-]+)#all_candidates"', index)
        assert links
        for name, value in links:
            assert f'name="{name}"' in index, name
            assert f'value="{value}"' in index, (name, value)

    def test_versions_are_never_sorted_as_text(self, tmp_path):
        """AGENTS.md section 3: a browser cannot compare Debian versions."""
        out = site(
            tmp_path,
            [ev(EventType.OPENED, merge("cron")), ev(EventType.OPENED, stuck())],
        )
        for page, columns in (
            ("merges/index.html", ("Ubuntu", "Debian")),
            ("migration/index.html", ("Upload",)),
        ):
            html = (out / page).read_text()
            for column in columns:
                assert re.search(rf"<th[^>]*data-nosort[^>]*>{column}</th>", html), (
                    page,
                    column,
                )


def test_package_views_cover_every_name_once():
    events = [
        ev(EventType.OPENED, stuck(waits_for=["zlib"])),
        ev(EventType.OPENED, merge("libssh2")),
    ]
    state = replay(events)
    rows, held = merge_rows(state, now=NOW), stuck_rows(state, now=NOW)
    views = package_views(rows, held, [], Snapshot(), History.of(events))
    assert sorted(views) == ["libssh2", "zlib"]
    assert views["libssh2"].merge and views["libssh2"].stuck
    assert [w.package for w in views["zlib"].waiting] == ["libssh2"]


class TestHoldingTab:
    """What an owner holds back is its own tab, not a section under a long
    stuck table that nobody scrolled to."""

    def built(self, tmp_path):
        sig = stuck(
            reasons=["regression"],
            tests=[
                {"test": "curl", "version": "8.0-1", "results": {"s390x": "regression"}}
            ],
        )
        owners = meta(
            libssh2=PackageMetadata(teams=("t",)),
            curl=PackageMetadata(teams=("t", "only-tests")),
        )
        return site(tmp_path, [ev(EventType.OPENED, sig)], owners)

    def test_three_tabs_one_per_question(self, tmp_path):
        page = (self.built(tmp_path) / "migration/teams/t.html").read_text()
        tabs = text(page.split('class="p-tabs"')[1].split("</nav>")[0])
        assert "Merges (0)" in tabs
        assert "Stuck in proposed (1)" in tabs
        assert "Holding others back (1)" in tabs
        assert 'id="blocking"' not in page

    def test_an_owner_with_only_blocking_tests_lands_on_that_tab(self, tmp_path):
        out = self.built(tmp_path)
        assert not (out / "migration/teams/only-tests.html").exists()
        page = (out / "holding/teams/only-tests.html").read_text()
        assert 'id="blocking"' in page
        owners = (out / "owners/index.html").read_text()
        assert 'href="../holding/teams/only-tests.html"' in owners

    def test_every_link_into_the_holding_pages_resolves(self, tmp_path):
        out = self.built(tmp_path)
        for page in out.rglob("*.html"):
            for href in re.findall(r'href="([^"#?]+)', page.read_text()):
                if not href.startswith("http"):
                    assert (page.parent / href).resolve().exists(), (page, href)


class TestPlusOne:
    def test_follows_the_guides_order(self, tmp_path):
        synced = PackageMetadata(publications=(pub("1.0-2", signer=None),))
        owners = meta(ftbfs=synced, broken=synced)
        events = [
            ev(EventType.OPENED, stuck("ftbfs")),
            ev(EventType.OPENED, stuck("broken", reasons=["uninstallable"])),
            ev(EventType.OPENED, merge("cron")),
        ]
        page = (site(tmp_path, events, owners) / "work/plus-one.html").read_text()
        order = [
            page.index(f'id="{section}"')
            for section in ("high-impact", "missing-builds", "stuck", "merges")
        ]
        assert order == sorted(order)
        builds = page.split('id="missing-builds"')[1].split('id="stuck"')[0]
        assert "packages/ftbfs.html" in builds and "packages/broken.html" not in builds

    def test_high_impact_counts_uploads_unblocked(self):
        held = [
            stuck(
                name,
                reasons=["regression"],
                tests=[
                    {"test": "curl", "version": "1", "results": {"amd64": "regression"}}
                ],
            )
            for name in ("a", "b")
        ]
        state = replay([ev(EventType.OPENED, s) for s in held])
        rows = stuck_rows(state, now=NOW)
        from cairn.build.site import blocking_rows
        from cairn.build.work import plus_one

        found = plus_one([], rows, blocking_rows(rows), waiting_for(rows), now=NOW)
        assert [(i.package, i.unblocks) for i in found.high_impact] == [("curl", 2)]

    def test_an_upload_with_a_bug_stays_in_the_queue_with_its_bug(self, tmp_path):
        synced = PackageMetadata(publications=(pub("1.0-2", signer=None),))
        page = (
            site(
                tmp_path,
                [ev(EventType.OPENED, stuck(bugs=[42]))],
                meta(libssh2=synced),
            )
            / "work/plus-one.html"
        ).read_text()
        builds = page.split('id="missing-builds"')[1].split('id="stuck"')[0]
        assert "packages/libssh2.html" in builds
        assert "bugs.launchpad.net/bugs/42" in builds
        assert 'id="claimed"' not in page
        assert "is only taken if it is assigned" in text(page)

    def test_no_duplicate_row_ids(self, tmp_path):
        """A package can be stuck and need a merge; both tables list it."""
        synced = PackageMetadata(publications=(pub("1.0-2", signer=None),))
        out = site(
            tmp_path,
            [ev(EventType.OPENED, stuck()), ev(EventType.OPENED, merge("libssh2"))],
            meta(libssh2=synced),
        )
        for page in ("work/plus-one.html", "work/index.html"):
            ids = re.findall(r'\bid="([^"]+)"', (out / page).read_text())
            assert len(ids) == len(set(ids)), page


def test_the_clear_button_is_outlined(tmp_path):
    """Vanilla's p-button--base has no border and read as plain text."""
    out = site(tmp_path, [ev(EventType.OPENED, merge("cron"))])
    page = (out / "merges/index.html").read_text()
    button = re.search(r'<button type="reset"[^>]*>', page)[0]
    # Outlined and full size, under the fields rather than squeezed beside.
    assert 'class="p-button u-no-margin--bottom"' in button
    form = page.split('<form class="cairn-filters"')[1].split("</form>")[0]
    fields, actions = form.split('class="cairn-filters__actions"')
    assert "<select" in fields and "<select" not in actions
    assert "cairn-filter-count" in actions


class TestPackagesIndex:
    """A grid of names with chips for what is open; no "no" anywhere."""

    def built(self, tmp_path):
        gone = merge("gone")
        events = [
            ev(EventType.OPENED, merge("cron", new_upstream=True)),
            ev(EventType.OPENED, stuck("libssh2")),
            ev(EventType.OPENED, gone),
            ev(EventType.RESOLVED, gone, T0 + timedelta(days=2)),
        ]
        owners = meta(cron=PackageMetadata(uploader="seb128", teams=("t",)))
        return (site(tmp_path, events, owners) / "packages/index.html").read_text()

    def item(self, page, name):
        return page.split(f'href="../packages/{name}.html"')[0].rsplit("<li", 1)[1]

    def test_chips_only_for_what_is_open(self, tmp_path):
        page = self.built(tmp_path)
        cron = page.split('href="../packages/cron.html"')[1].split("</li>")[0]
        assert "needs merge" in cron and "new upstream" in cron
        assert "stuck" not in cron
        libssh2 = page.split('href="../packages/libssh2.html"')[1].split("</li>")[0]
        assert "stuck 40 days" in libssh2 and "needs merge" not in libssh2
        assert ">no<" not in page and "<table" not in page

    def test_closed_packages_are_folded_and_dated(self, tmp_path):
        page = self.built(tmp_path)
        closed = page.split('class="cairn-pkgs-closed"')[1]
        assert "packages/gone.html" in closed
        assert "packages/cron.html" not in closed
        assert f"nothing open since {T0 + timedelta(days=2):%Y-%m-%d}" in closed
        # Opens itself when a search matches inside it.
        assert "data-filter-open" in page.split('class="cairn-pkgs-closed"')[1][:80]

    def test_owners_find_their_packages(self, tmp_path):
        item = self.item(self.built(tmp_path), "cron")
        assert "seb128" in item and " t" in item
        assert 'data-f-has="merge' in item

    def test_stat_links_name_options_the_filter_offers(self, tmp_path):
        page = self.built(tmp_path)
        for value in re.findall(r'href="\?has=(\w+)"', page):
            assert f'<option value="{value}">' in page, value


class TestNavigation:
    """Four entries: two menus, then the two boards. User feedback: the bar
    was overloaded for a site with two boards."""

    def nav(self, html):
        return html.split('aria-label="Main"')[1].split("</nav>")[0]

    def test_four_entries_menus_first(self, tmp_path):
        out = site(tmp_path, [ev(EventType.OPENED, stuck())])
        nav = self.nav((out / "index.html").read_text())
        top = re.findall(r'<a class="p-navigation__link"[^>]*>([^<]+)</a>', nav)
        assert top == ["Browse", "Up for grabs", "Merges", "Stuck in proposed"]
        items = re.findall(r'class="p-navigation__dropdown-item"[^>]*>([^<]+)<', nav)
        assert items == [
            "Packages",
            "Teams and people",
            "Anyone can pick up",
            "+1 maintenance shift",
        ]

    def test_a_menu_page_marks_its_menu_and_itself(self, tmp_path):
        out = site(tmp_path, [ev(EventType.OPENED, stuck())])
        for page, menu, item in (
            ("work/plus-one.html", "Up for grabs", "+1 maintenance shift"),
            ("work/index.html", "Up for grabs", "Anyone can pick up"),
            ("owners/index.html", "Browse", "Teams and people"),
            ("packages/index.html", "Browse", "Packages"),
        ):
            nav = self.nav((out / page).read_text())
            selected = nav.split("is-selected")[1].split("</li>")[0]
            assert f">{menu}</a>" in selected, page
            current = re.findall(r'aria-current="page">([^<]+)<', nav)
            assert current == [item], page
        assert not (out / "plus-one").exists()

    def test_menus_open_without_javascript(self):
        from cairn.build.site import ASSETS

        css = (ASSETS / "cairn.css").read_text()
        assert ".cairn-nav-menu:hover > .p-navigation__dropdown" in css
        assert ".cairn-nav-menu:focus-within > .p-navigation__dropdown" in css


class TestTooltips:
    def built(self, tmp_path):
        synced = PackageMetadata(publications=(pub("1.0-2", signer=None),))
        sig = stuck(
            reasons=["regression"],
            bugs=[7],
            tests=[
                {"test": "curl", "version": "1", "results": {"s390x": "regression"}}
            ],
        )
        events = [
            ev(EventType.OPENED, sig),
            ev(EventType.OPENED, merge("libssh2", new_upstream=True)),
            ev(EventType.OPENED, stuck("young", days=1, version="1.0-1ubuntu1")),
            ev(EventType.OPENED, merge("young", ubuntu="1.0-1ubuntu1")),
        ]
        owners = meta(
            libssh2=synced,
            young=PackageMetadata(teams=("t",), package_sets=("core",)),
            curl=PackageMetadata(teams=("t",)),
        )
        return site(tmp_path, events, owners)

    def test_every_label_points_at_a_definition_on_its_page(self, tmp_path):
        """A page that shows a label must carry its definition, so the text
        is there without JavaScript and screen readers can announce it."""
        out = self.built(tmp_path)
        used = 0
        for page in out.rglob("*.html"):
            html = page.read_text()
            ids = set(re.findall(r'\bid="([^"]+)"', html))
            for ref in re.findall(r'aria-describedby="([^"]+)"', html):
                used += 1
                assert ref in ids, (page.relative_to(out), ref)
        assert used > 20

    def test_native_titles_gave_way_to_the_glossary(self, tmp_path):
        """A title attribute is invisible to keyboard and touch users."""
        page = (self.built(tmp_path) / "migration/index.html").read_text()
        table = page.split("<tbody>")[1]
        assert 'title="An update-excuse bug' not in table
        assert 'aria-describedby="term_excuse_bug"' in table
        assert 'aria-describedby="term_regression"' in table

    def test_the_definitions_stay_on_the_page(self, tmp_path):
        page = (self.built(tmp_path) / "merges/index.html").read_text()
        terms = page.split('class="cairn-terms"')[1].split("</details>")[0]
        assert "What the labels mean" in terms
        assert '<dd id="term_new_upstream">' in terms


def test_links_carry_no_arrow_chips_do(tmp_path):
    """A link already looks like a link; a chip does not."""
    out = site(
        tmp_path,
        [
            ev(EventType.OPENED, merge("cron", new_upstream=True)),
            ev(EventType.OPENED, stuck()),
        ],
    )
    for page in ("index.html", "merges/index.html", "work/plus-one.html"):
        html = (out / page).read_text()
        for link in re.findall(r"<a\b[^>]*>.*?</a>", html, flags=re.S):
            if "&rarr;" in link:
                assert "p-chip" in link, (page, link)


class TestOwnersIndex:
    """Names with a chip per kind of work, not wide tables of zeros."""

    def built(self, tmp_path):
        sig = stuck(
            reasons=["regression"],
            tests=[
                {"test": "curl", "version": "1", "results": {"s390x": "regression"}}
            ],
        )
        owners = meta(
            libssh2=PackageMetadata(teams=("t",), package_sets=("core",)),
            cron=PackageMetadata(package_sets=("core",)),
            curl=PackageMetadata(teams=("tests-only",)),
        )
        events = [ev(EventType.OPENED, sig), ev(EventType.OPENED, merge("cron"))]
        return (site(tmp_path, events, owners) / "owners/index.html").read_text()

    def item(self, page, href):
        return page.split(f'class="cairn-pkg__name" href="../{href}"')[1].split(
            "</li>"
        )[0]

    def test_chips_only_for_work_they_have_each_opening_its_tab(self, tmp_path):
        page = self.built(tmp_path)
        assert "<table" not in page
        core = self.item(page, "owners/sets/core.html")
        assert 'href="../merges/sets/core.html"' in core and "1 merge" in core
        assert 'href="../migration/sets/core.html"' in core and "1 stuck" in core
        assert "holding back" not in core
        tests_only = self.item(page, "owners/teams/tests-only.html")
        assert "holding back 1" in tests_only
        assert "merge" not in tests_only and "stuck" not in tests_only

    def test_package_sets_name_their_size(self, tmp_path):
        assert "2 packages" in self.item(self.built(tmp_path), "owners/sets/core.html")

    def test_filters_by_kind_of_work_not_component(self, tmp_path):
        page = self.built(tmp_path)
        form = page.split('<form class="cairn-filters"')[1].split("</form>")[0]
        assert 'name="component"' not in form
        for value in ("merges", "stuck", "holding"):
            assert f'<option value="{value}">' in form
        assert 'data-f-has="merges stuck ' in page


def test_the_owner_directory_is_alphabetical(tmp_path):
    """For finding a name; the boards rank owners by size instead."""
    synced = PackageMetadata(publications=(pub("1.0-2", signer=None),))
    owners = meta(
        zlib=PackageMetadata(uploader="Zed", teams=("zz-team",)),
        bash=PackageMetadata(uploader="amy", teams=("AA-team",)),
        cron=PackageMetadata(uploader="bob", teams=("zz-team",)),
        libssh2=synced,
    )
    events = [
        ev(EventType.OPENED, merge("zlib")),
        ev(EventType.OPENED, merge("bash")),
        ev(EventType.OPENED, merge("cron")),
        ev(EventType.OPENED, stuck()),
    ]
    page = (site(tmp_path, events, owners) / "owners/index.html").read_text()

    def names(anchor):
        section = page.split(f'id="{anchor}"')[1].split("</section>")[0]
        return re.findall(r'class="cairn-pkg__name"[^>]*>([^<]+)<', section)

    assert names("teams") == ["AA-team", "zz-team"]  # zz-team has more work
    assert names("uploaders") == ["amy", "bob", "Zed", "+1 maintenance"]


class TestTrendAxis:
    def trend(self):
        a, b = merge("a"), merge("b")
        return trends(
            [
                ev(EventType.OPENED, a, T0),
                ev(EventType.OPENED, b, T0),
                ev(EventType.RESOLVED, a, T0 + timedelta(days=9)),
            ]
        ).of(Kind.NEEDS_MERGE)

    def test_dates_fall_on_midnights_within_the_line(self):
        ticks = self.trend().x_ticks(6)
        assert 2 <= len(ticks) <= 6
        assert all(0 <= at <= 1 for at, _ in ticks)
        days = [d for _, d in ticks]
        assert days == sorted(days)
        steps = {(b - a).days for a, b in itertools.pairwise(days)}
        assert len(steps) == 1  # evenly spaced

    def test_ticks_do_not_move_between_builds(self):
        """Counted from a fixed day, not from the first point."""
        t = self.trend()
        assert all(d.toordinal() % 2 == 0 for _, d in t.x_ticks(6))

    def test_the_board_labels_dates_and_scale_and_keeps_the_words(self, tmp_path):
        a, b = merge("a"), merge("b")
        out = site(
            tmp_path,
            [
                ev(EventType.OPENED, a, T0),
                ev(EventType.OPENED, b, T0),
                ev(EventType.RESOLVED, a, T0 + timedelta(days=9)),
            ],
        )
        page = (out / "merges/index.html").read_text()
        chart = page.split('class="cairn-trend"')[1].split("</figure>")[0]
        assert re.search(r">\d{4}-\d{2}-\d{2}</span>", chart)
        assert 'class="cairn-trend__y-top">2<' in chart
        assert 'class="u-off-screen"' in chart and "(1 fewer)" in chart


class TestReviewOnPR17:
    """Copilot's review of #17."""

    def test_an_upload_held_two_ways_is_counted_once(self, tmp_path):
        """One upload regressed by two of the owner's tests, and waiting for
        an upload of theirs, is one upload held back, not three."""
        app = stuck(
            "app",
            reasons=["regression", "waiting"],
            waits_for=["lib"],
            tests=[
                {"test": "t1", "version": "1", "results": {"amd64": "regression"}},
                {"test": "t2", "version": "1", "results": {"amd64": "regression"}},
            ],
        )
        owners = meta(
            t1=PackageMetadata(teams=("team",)),
            t2=PackageMetadata(teams=("team",)),
            lib=PackageMetadata(teams=("team",)),
        )
        events = [ev(EventType.OPENED, app), ev(EventType.OPENED, stuck("lib"))]
        out = site(tmp_path, events, owners)
        holding = (out / "holding/teams/team.html").read_text()
        assert "Holding others back (1)" in holding
        assert "Uploads waiting for yours (1)" in holding
        assert "holding back 1" in (out / "owners/index.html").read_text()

    def test_an_upload_of_unknown_origin_is_not_called_ubuntus(self, tmp_path):
        """No publication on Launchpad: neither a sync nor an Ubuntu upload."""
        owners = meta(
            known=PackageMetadata(publications=(pub("1.0-2", uploader="kat"),)),
            mystery=PackageMetadata(publications=(pub("0.9-1", uploader="kat"),)),
        )
        events = [
            ev(EventType.OPENED, stuck("known", days=ABANDONED_DAYS)),
            ev(EventType.OPENED, stuck("mystery", days=ABANDONED_DAYS)),
        ]
        state = replay(events)
        held = stuck_rows(state, owners, now=NOW)
        by_name = {r.package: r for r in held}
        assert by_name["known"].origin == "ubuntu"
        assert by_name["mystery"].origin == "unknown"
        found = up_for_grabs([], held, now=NOW)
        assert [r.package for r in found.abandoned] == ["known"]
        assert [r.package for r in found.unknown] == ["mystery"]
        out = site(tmp_path, events, owners)
        page = (out / "work/index.html").read_text()
        ubuntu = page.split('id="abandoned"')[1].split('class="cairn-tab-panel"')[0]
        assert "packages/mystery.html" not in ubuntu
        assert "packages/mystery.html" in page.split('id="unknown"')[1]
        board = (out / "migration/index.html").read_text()
        assert 'data-f-origin="unknown"' in board
        assert '<option value="unknown">' in board

    def test_the_plus_one_bug_link_uses_the_shared_definition(self, tmp_path):
        synced = PackageMetadata(publications=(pub("1.0-2", signer=None),))
        regressed = [
            stuck(
                name,
                reasons=["regression"],
                tests=[
                    {"test": "curl", "version": "1", "results": {"s390x": "regression"}}
                ],
            )
            for name in ("libssh2", "other")
        ]
        # curl holds back both, and its own stuck upload has a bug filed.
        curl = stuck("curl", bugs=[42])
        out = site(
            tmp_path,
            [ev(EventType.OPENED, s) for s in (*regressed, curl)],
            meta(libssh2=synced),
        )
        page = (out / "work/plus-one.html").read_text()
        impact = page.split('id="high-impact"')[1].split("</table>")[0]
        link = re.search(r"<a [^>]*bugs/42[^>]*>", impact)[0]
        assert "title=" not in link
        assert 'aria-describedby="term_excuse_bug"' in link
        assert (
            "investigating"
            not in page.split("<main>")[1].split('id="term_excuse_bug"')[0]
        )

    def test_menus_claim_no_expanded_state_without_javascript(self, tmp_path):
        """CSS opens a menu on hover and focus but cannot set an attribute,
        so the HTML must not state one; cairn.js keeps it true."""
        from cairn.build.site import ASSETS

        out = site(tmp_path, [ev(EventType.OPENED, merge("cron"))])
        nav = (out / "index.html").read_text().split('aria-label="Main"')[1]
        assert "aria-expanded" not in nav.split("</nav>")[0]
        js = (ASSETS / "cairn.js").read_text()
        assert 'menu.matches(":focus-within")' in js
        assert 'setAttribute("aria-expanded"' in js


class TestUpForGrabsTabs:
    """One tab per kind of work, one filter above them, the rules first."""

    def page(self, tmp_path):
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", signer=None),)),
            sudo=PackageMetadata(publications=(pub("1.0-2"),)),
        )
        events = [
            ev(EventType.OPENED, stuck()),
            ev(EventType.OPENED, stuck("sudo", days=ABANDONED_DAYS)),
            ev(EventType.OPENED, merge("cron")),
        ]
        return (site(tmp_path, events, owners) / "work/index.html").read_text()

    def test_tabs_name_each_kind_without_counts(self, tmp_path):
        page = self.page(tmp_path)
        tabs = page.split('aria-label="Kinds of work"')[1].split("</nav>")[0]
        labels = re.findall(r'data-tab="(\w+)">([^<]+)</a>', tabs)
        assert [i for i, _ in labels] == ["syncs", "abandoned", "merges"]
        assert not any(re.search(r"\d+\)", label) for _, label in labels)
        for tab_id, _ in labels:
            assert f'class="cairn-tab-panel" id="{tab_id}"' in page

    def test_rules_then_one_filter_then_the_panels(self, tmp_path):
        page = self.page(tmp_path)
        order = [
            page.index('id="how_listed"'),
            page.index('aria-label="Kinds of work"'),
            page.index('<form class="cairn-filters"'),
            page.index('class="cairn-tab-panel"'),
        ]
        assert order == sorted(order)
        assert page.count('<form class="cairn-filters"') == 1
        assert "cairn-stat" not in page.split("<main>")[1]


class TestOwnerOverview:
    """Every owner's first tab, laid out like a package page."""

    def built(self, tmp_path):
        sig = stuck(
            reasons=["regression"],
            tests=[
                {"test": "curl", "version": "1", "results": {"s390x": "regression"}}
            ],
        )
        owners = meta(
            libssh2=PackageMetadata(teams=("t",)),
            cron=PackageMetadata(package_sets=("core",)),
            curl=PackageMetadata(teams=("tests-only",)),
        )
        events = [ev(EventType.OPENED, sig), ev(EventType.OPENED, merge("cron"))]
        return site(tmp_path, events, owners)

    def test_every_owner_has_one_and_searches_land_there(self, tmp_path):
        out = self.built(tmp_path)
        for path in (
            "owners/teams/t.html",
            "owners/sets/core.html",
            "owners/teams/tests-only.html",
        ):
            assert (out / path).exists(), path
        home = (out / "index.html").read_text()
        assert 'data-href="owners/teams/t.html"' in home

    def test_a_line_per_kind_of_work_open_or_not(self, tmp_path):
        page = text((self.built(tmp_path) / "owners/sets/core.html").read_text())
        assert "1 merge from Debian" in page
        assert "Nothing stuck in proposed" in page
        assert "Not holding anything back" in page
        page = text(
            (self.built(tmp_path / "b") / "owners/teams/tests-only.html").read_text()
        )
        assert "Holding back 1 other upload" in page
        assert "No merges waiting" in page

    def test_overview_is_the_first_tab_and_marked_current(self, tmp_path):
        html = (self.built(tmp_path) / "owners/teams/t.html").read_text()
        tabs = html.split('class="p-tabs"')[1].split("</nav>")[0]
        assert re.findall(r">([A-Z][a-z]+)", tabs)[0] == "Overview"
        assert 'aria-current="page"' in tabs.split("Overview")[0]


class TestUpForGrabsEmptyTabs:
    def test_an_empty_main_tab_is_greyed_with_zero_and_unknown_is_left_out(
        self, tmp_path
    ):
        out = site(tmp_path, [ev(EventType.OPENED, merge("cron"))])
        page = (out / "work/index.html").read_text()
        tabs = page.split('aria-label="Kinds of work"')[1].split("</nav>")[0]
        assert re.search(r'cairn-tab--empty"[^>]*>Debian syncs \(0\)<', tabs)
        assert 'data-tab="merges">Packaging-only merges<' in tabs
        assert "Uploader not known" not in tabs
        assert 'id="syncs"' not in page  # no panel behind an empty tab
        how = text(page.split('id="how_listed"')[1].split("</details>")[0])
        assert "Uploads with no known uploader (when there are any)" in how


def test_the_board_statistics_tab_holds_the_numbers(tmp_path):
    out = site(tmp_path, [ev(EventType.OPENED, stuck())])
    page = (out / "migration/index.html").read_text()
    main = page.split("<main>")[1]
    assert main.index('aria-label="Stuck in proposed board"') < main.index("cairn-stat")
    assert "cairn-stat" in main.split('id="board_stats"')[1]
    tabs = main.split('aria-label="Stuck in proposed board"')[1].split("</nav>")[0]
    assert re.findall(r'data-tab="(\w+)"', tabs) == [
        "stuck_list",
        "still_settling",
        "board_stats",
    ]
    assert ">Statistics</a>" in tabs


class TestReviewOnPR17Second:
    """Copilot's second review of #17."""

    def test_an_older_upstream_is_named_and_kept_off_the_queues(self, tmp_path):
        events = [
            ev(
                EventType.OPENED,
                merge("compiz", ubuntu="1:0.9.14.2-0ubuntu3", debian="2:0.8.18-9"),
            )
        ]
        out = site(tmp_path, events)
        board = (out / "merges/index.html").read_text()
        row = board.split('id="compiz"')[1].split("</tr>")[0]
        assert 'aria-describedby="term_older_upstream"' in row
        assert 'data-f-upstream="older"' in row
        assert '<option value="older">' in board
        assert "packages/compiz.html" not in (out / "work/plus-one.html").read_text()
        assert "packages/compiz.html" not in (out / "work/index.html").read_text()
        page = text((out / "packages/compiz.html").read_text())
        assert "its upstream release is older" in page

    def test_an_upload_waiting_on_a_package_that_is_not_stuck(self, tmp_path):
        """Its teams and package sets still hear that they hold it back."""
        owners = meta(
            app=PackageMetadata(publications=(pub("1.0-2", uploader="bob"),)),
            lib=PackageMetadata(teams=("libteam",), package_sets=("core",)),
        )
        out = site(
            tmp_path, [ev(EventType.OPENED, stuck("app", waits_for=["lib"]))], owners
        )
        for path in ("holding/teams/libteam.html", "holding/sets/core.html"):
            page = (out / path).read_text()
            section = page.split('id="waiting-on-yours"')[1]
            assert "migration/uploaders/bob.html#app" in section
            assert 'href="../../packages/lib.html"' in section
        assert "Holding back 1 other upload" in text(
            (out / "owners/teams/libteam.html").read_text()
        )

    def test_nothing_matches_only_when_a_filter_is_on(self):
        from cairn.build.site import ASSETS

        js = (ASSETS / "cairn.js").read_text()
        assert "none.hidden = !(filtering && any && visible === 0)" in js

    def test_a_bare_owner_name_on_two_axes_is_not_guessed(self):
        """ubuntu-desktop is a team and a package set: only the labelled
        value, or a unique bare name, jumps."""
        from cairn.build.site import ASSETS

        js = (ASSETS / "cairn.js").read_text()
        assert "bare.length === 1" in js
        assert "exact.length === 1" in js
