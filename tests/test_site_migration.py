"""The "Stuck in proposed" board, and how it joins the merges board."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cairn.build.site import (
    PLUS_ONE,
    SETTLING_DAYS,
    History,
    autopkgtest_url,
    blocking_rows,
    build,
    group_by_package_set,
    group_by_team,
    group_by_uploader,
    merge_rows,
    migration_overview,
    render_migration,
    retry_url,
    stuck_rows,
    uploads_seen,
)
from cairn.core.health import SourceHealth
from cairn.core.log import Event, EventType, replay
from cairn.ingest.base import Kind, Signal
from cairn.ingest.metadata import PackageMetadata, PublishedVersion, Snapshot

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
T0 = NOW - timedelta(days=30)


def stuck(name="libssh2", *, days=11, **payload):
    base = {
        "old_version": "1.0-1",
        "new_version": "1.0-2",
        "component": "main",
        "reasons": ["regression"],
        "britney_reasons": ["autopkgtest"],
        "tests": [
            {"test": "curl", "version": "8.0-1", "results": {"s390x": "regression"}}
        ],
        "missing_builds": [],
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
        # An older event's URL; the board must not depend on it.
        url=f"https://ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses.html#{name}",
    )


def merge(name, *, version="1.0-2", in_proposed=True):
    payload = {
        "ubuntu_version": version,
        "debian_version": "2.0-1",
        "base_version": "1.0",
        "component": "main",
        "new_upstream": True,
    }
    if in_proposed:
        payload["in_proposed"] = True
    return Signal(
        kind=Kind.NEEDS_MERGE, source_package=name, series="stonking", payload=payload
    )


def opened(sig, ts=T0):
    source = "migration" if sig.kind is Kind.MIGRATION_BLOCKED else "merges"
    return Event.of(EventType.OPENED, sig, source=source, ts=ts)


def updated(sig, ts):
    return Event.of(EventType.UPDATED, sig, source="migration", ts=ts)


def pub(version, *, uploader="kat", signer="kat", pocket="Proposed"):
    return PublishedVersion(
        version=version, pocket=pocket, uploader=uploader, signer=signer, published=None
    )


def meta(**packages):
    return Snapshot(collected_at=NOW, packages=packages)


def healthy():
    return {
        s: SourceHealth(s, last_attempt=NOW, last_success=NOW)
        for s in ("merges", "migration")
    }


class TestStuckRows:
    def test_reads_the_signal(self):
        [row] = stuck_rows(replay([opened(stuck())]), now=NOW)
        assert row.package == "libssh2"
        assert row.new_version == "1.0-2"
        assert row.days(NOW) == 11
        assert row.failing_tests[0].test == "curl"

    def test_longest_held_first_unknown_last(self):
        state = replay(
            [
                opened(stuck("young", days=4)),
                opened(stuck("old", days=40)),
                opened(stuck("undated", in_proposed_since=None)),
            ]
        )
        assert [r.package for r in stuck_rows(state, now=NOW)] == [
            "old",
            "young",
            "undated",
        ]

    def test_reasons_lead_with_what_a_person_can_act_on(self):
        sig = stuck(reasons=["tests_running", "needs_approval", "regression"])
        [row] = stuck_rows(replay([opened(sig)]), now=NOW)
        assert row.reasons == ("regression", "needs_approval", "tests_running")

    def test_names_the_uploader_of_the_stuck_version(self):
        """Not whoever uploaded last: that may be a newer, different upload."""
        owner = PackageMetadata(
            uploader="newer",
            publications=(pub("1.0-1", uploader="old", pocket="Release"), pub("1.0-2")),
        )
        [row] = stuck_rows(replay([opened(stuck())]), meta(libssh2=owner), now=NOW)
        assert row.uploader == "kat"
        assert not row.synced

    def test_an_unsigned_upload_is_a_sync(self):
        owner = PackageMetadata(
            publications=(pub("1.0-2", uploader="sanvila", signer=None),)
        )
        [row] = stuck_rows(replay([opened(stuck())]), meta(libssh2=owner), now=NOW)
        assert row.synced
        assert row.owner == PLUS_ONE

    def test_falls_back_to_the_latest_uploader_on_an_old_snapshot(self):
        [row] = stuck_rows(
            replay([opened(stuck())]),
            meta(libssh2=PackageMetadata(uploader="kat")),
            now=NOW,
        )
        assert row.uploader == "kat"
        assert not row.synced


class TestSettling:
    def test_a_young_upload_is_settling(self):
        [row] = stuck_rows(replay([opened(stuck(days=SETTLING_DAYS - 1))]), now=NOW)
        assert row.settling(NOW)

    def test_an_old_upload_is_not(self):
        [row] = stuck_rows(replay([opened(stuck(days=SETTLING_DAYS))]), now=NOW)
        assert not row.settling(NOW)

    def test_only_waiting_for_tests_is_not_settling_once_old(self):
        """Tests re-running on an upload stuck for weeks hide nothing."""
        sig = stuck(days=40, reasons=["tests_running"], tests=[])
        [row] = stuck_rows(replay([opened(sig)]), now=NOW)
        assert not row.settling(NOW)


class TestHistory:
    def test_counts_distinct_uploads_under_one_signal(self):
        first = stuck(new_version="1.0-2")
        second = stuck(new_version="1.0-3")
        events = [opened(first), updated(second, T0 + timedelta(days=5))]
        assert list(uploads_seen(events).values()) == [2]
        [row] = stuck_rows(replay(events), now=NOW, uploads=uploads_seen(events))
        assert row.uploads == 2

    def test_a_reason_change_is_not_a_new_upload(self):
        events = [
            opened(stuck()),
            updated(
                stuck(reasons=["regression", "needs_approval"]), T0 + timedelta(days=1)
            ),
        ]
        assert list(uploads_seen(events).values()) == [1]


class TestBlockingInversion:
    def test_the_tests_owner_sees_the_upload_they_hold_back(self):
        state = replay([opened(stuck())])
        rows = stuck_rows(state, now=NOW)
        owners = meta(curl=PackageMetadata(teams=("foundations-bugs",)))
        [blocking] = blocking_rows(rows, owners)
        assert blocking.test == "curl"
        assert blocking.holding.package == "libssh2"
        assert blocking.teams == ("foundations-bugs",)

    def test_a_package_failing_its_own_tests_is_not_repeated(self):
        sig = stuck(
            tests=[
                {
                    "test": "libssh2",
                    "version": "1.0-2",
                    "results": {"amd64": "regression"},
                }
            ]
        )
        rows = stuck_rows(replay([opened(sig)]), now=NOW)
        assert blocking_rows(rows) == []


class TestOwnerGroups:
    def state_and_meta(self):
        state = replay([opened(stuck()), opened(merge("libssh2"))])
        owners = meta(
            libssh2=PackageMetadata(
                teams=("ubuntu-server",),
                package_sets=("core",),
                publications=(pub("1.0-2"),),
            ),
            curl=PackageMetadata(teams=("foundations-bugs",), package_sets=("core",)),
        )
        return state, owners

    def test_one_page_carries_merges_and_stuck_uploads(self):
        state, owners = self.state_and_meta()
        merges = merge_rows(state, owners, now=NOW)
        rows = stuck_rows(state, owners, now=NOW)
        [team] = [g for g in group_by_team(merges, rows) if g.name == "ubuntu-server"]
        assert team.count == 1
        assert team.stuck_count == 1

    def test_regressing_tests_reach_the_tests_team(self):
        state, owners = self.state_and_meta()
        rows = stuck_rows(state, owners, now=NOW)
        groups = {
            g.name: g for g in group_by_team([], rows, blocking_rows(rows, owners))
        }
        assert groups["foundations-bugs"].blocking_count == 1
        assert groups["foundations-bugs"].stuck_count == 0

    def test_package_sets_get_both(self):
        state, owners = self.state_and_meta()
        rows = stuck_rows(state, owners, now=NOW)
        [core] = group_by_package_set([], rows, blocking_rows(rows, owners))
        assert (core.stuck_count, core.blocking_count) == (1, 1)

    def test_regressing_tests_are_not_routed_by_uploader(self):
        state, owners = self.state_and_meta()
        rows = stuck_rows(state, owners, now=NOW)
        groups = group_by_uploader([], rows, blocking_rows(rows, owners))
        assert all(g.blocking_count == 0 for g in groups)

    def test_syncs_go_to_plus_one_maintenance(self):
        owner = PackageMetadata(
            publications=(pub("1.0-2", uploader="sanvila", signer=None),)
        )
        rows = stuck_rows(replay([opened(stuck())]), meta(libssh2=owner), now=NOW)
        [group] = group_by_uploader([], rows)
        assert group.name == PLUS_ONE
        assert group.title == "+1 maintenance"
        assert "sanvila" not in {g.name for g in group_by_uploader([], rows)}

    def test_plus_one_comes_after_people(self):
        sync = PackageMetadata(publications=(pub("1.0-2", uploader="x", signer=None),))
        state = replay([opened(stuck("a")), opened(stuck("b")), opened(stuck("c"))])
        owners = meta(
            a=sync,
            b=sync,
            c=PackageMetadata(publications=(pub("1.0-2", uploader="kat"),)),
        )
        groups = group_by_uploader([], stuck_rows(state, owners, now=NOW))
        assert [g.name for g in groups] == ["kat", PLUS_ONE]


class TestPages:
    def built(self, tmp_path, events, owners):
        build(
            replay(events),
            healthy(),
            owners,
            out=tmp_path,
            now=NOW,
            series="stonking",
            history=History.of(events),
        )
        return tmp_path

    def owners(self):
        return meta(
            libssh2=PackageMetadata(
                teams=("ubuntu-server",), publications=(pub("1.0-2"),)
            ),
            curl=PackageMetadata(teams=("foundations-bugs",)),
        )

    def test_writes_the_migration_index(self, tmp_path):
        out = self.built(tmp_path, [opened(stuck())], self.owners())
        page = (out / "migration/index.html").read_text()
        assert "Stuck in proposed" in page
        assert "migration/teams/ubuntu-server.html" in page
        assert 'aria-current="page"' in page

    def test_the_index_explains_reasons_as_text_with_a_guide(self):
        html = render_migration(
            replay([opened(stuck())]), healthy(), self.owners(), now=NOW
        )
        why = html.split('id="why"')[1].split("</section>")[0]
        assert "Test regression" in why
        assert "autopkgtest-regressions" in why
        assert '"Reason">regression<' not in why  # the code never reaches the page

    def test_a_freeze_banner_appears_only_when_frozen(self):
        quiet = render_migration(replay([opened(stuck())]), healthy(), now=NOW)
        assert "is frozen" not in quiet
        frozen = stuck(reasons=["needs_approval"], hints=["freeze"])
        html = render_migration(replay([opened(frozen)]), healthy(), now=NOW)
        assert "stonking is frozen" in html
        assert "freeze-exceptions" in html

    def test_owner_page_explains_why_with_working_links(self, tmp_path):
        sig = stuck(
            reasons=["regression", "missing_build"],
            missing_builds=["riscv64"],
            bugs=[2167365],
        )
        out = self.built(tmp_path, [opened(sig)], self.owners())
        page = (out / "migration/teams/ubuntu-server.html").read_text()
        row = page.split('id="libssh2"')[1].split("</tr>")[0]
        assert "Test regression" in row
        assert "https://autopkgtest.ubuntu.com/packages/c/curl/stonking/s390x" in row
        assert "/+source/libssh2/1.0-2/+latestbuild/riscv64" in row
        assert "https://bugs.launchpad.net/bugs/2167365" in row
        # The package opens Launchpad, never britney's page, even though this
        # event stored the old britney URL.
        assert 'href="https://launchpad.net/ubuntu/+source/libssh2"' in row
        assert "update_excuses" not in row

    def test_the_tests_team_page_says_what_it_holds_back(self, tmp_path):
        out = self.built(tmp_path, [opened(stuck())], self.owners())
        page = (out / "migration/teams/foundations-bugs.html").read_text()
        blocking = page.split('id="blocking"')[1]
        assert "curl" in blocking
        assert "libssh2" in blocking

    def test_young_uploads_are_folded_not_hidden(self, tmp_path):
        events = [opened(stuck("old", days=20)), opened(stuck("new", days=1))]
        owners = meta(
            old=PackageMetadata(teams=("t",)), new=PackageMetadata(teams=("t",))
        )
        page = (
            self.built(tmp_path, events, owners) / "migration/teams/t.html"
        ).read_text()
        before, folded = page.split("<details>")
        assert 'id="old"' in before
        assert 'id="new"' in folded
        assert "Still settling (1)" in folded

    def test_reuploads_are_counted(self, tmp_path):
        events = [
            opened(stuck(new_version="1.0-2")),
            updated(stuck(new_version="1.0-3"), T0 + timedelta(days=3)),
        ]
        page = (
            self.built(tmp_path, events, self.owners())
            / "migration/teams/ubuntu-server.html"
        ).read_text()
        assert "2 uploads" in page

    def test_ships_no_javascript(self, tmp_path):
        out = self.built(
            tmp_path, [opened(stuck()), opened(merge("libssh2"))], self.owners()
        )
        for page in out.rglob("*.html"):
            assert "<script" not in page.read_text().lower(), page


class TestSeparatePages:
    """Merges and stuck uploads are separate pages per owner: one page with
    both grew as long as britney's own excuses page."""

    def built(self, tmp_path, events, owners):
        build(
            replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking"
        )
        return tmp_path

    def owners(self):
        return meta(
            libssh2=PackageMetadata(
                teams=("ubuntu-server",), publications=(pub("1.0-2"),)
            )
        )

    def test_each_kind_has_its_own_page(self, tmp_path):
        out = self.built(
            tmp_path, [opened(stuck()), opened(merge("libssh2"))], self.owners()
        )
        merges = (out / "merges/teams/ubuntu-server.html").read_text()
        stuck_page = (out / "migration/teams/ubuntu-server.html").read_text()
        # One row per page, and each is that board's own kind of row.
        assert merges.count('id="libssh2"') == 1
        assert stuck_page.count('id="libssh2"') == 1
        assert 'data-heading="Debian"' in merges.split('id="libssh2"')[1]
        assert "p-table--mobile-card cairn-stuck" not in merges
        assert 'data-heading="Why it is held"' in stuck_page.split('id="libssh2"')[1]
        assert 'data-heading="Debian"' not in stuck_page

    def test_no_empty_page_for_an_owner_with_one_kind(self, tmp_path):
        out = self.built(tmp_path, [opened(stuck())], self.owners())
        assert (out / "migration/teams/ubuntu-server.html").exists()
        assert not (out / "merges/teams/ubuntu-server.html").exists()

    def test_each_page_counts_only_its_own_board(self, tmp_path):
        """No header link to the owner's other page: the rows link to each
        other's row for the same package, and the navigation switches boards."""
        out = self.built(
            tmp_path, [opened(stuck()), opened(merge("libssh2"))], self.owners()
        )
        merges = (out / "merges/teams/ubuntu-server.html").read_text()
        stuck_page = (out / "migration/teams/ubuntu-server.html").read_text()
        merges_header = merges.split("<h1")[1].split("</section>")[0]
        stuck_header = stuck_page.split("<h1")[1].split("</section>")[0]
        assert "stuck in proposed" not in merges_header
        assert "merge candidate" not in stuck_header
        assert 'href="../../migration/teams/ubuntu-server.html' in merges  # row chip

    def test_titles_name_the_kind(self, tmp_path):
        out = self.built(
            tmp_path, [opened(stuck()), opened(merge("libssh2"))], self.owners()
        )
        assert (
            "<title>Cairn | Merges for ubuntu-server</title>"
            in (out / "merges/teams/ubuntu-server.html").read_text()
        )
        assert (
            "<title>Cairn | Stuck in proposed for ubuntu-server</title>"
            in (out / "migration/teams/ubuntu-server.html").read_text()
        )

    def test_the_stuck_page_selects_its_nav_item(self, tmp_path):
        out = self.built(tmp_path, [opened(stuck())], self.owners())
        page = (out / "migration/teams/ubuntu-server.html").read_text()
        nav = page.split('aria-label="Main"')[1].split("</nav>")[0]
        assert 'href="../../migration/index.html" aria-current="page"' in nav


def chip(html, text):
    """The <a class="p-chip..."> element whose value is `text`."""
    for part in html.split("<a ")[1:]:
        tag = part.partition("</a>")[0]
        if "p-chip" in tag and f">{text}<" in tag:
            return tag
    raise AssertionError(f"no chip {text!r}")


class TestCrossLinkChips:
    """The two boards point at each other with chips that are real links."""

    def build(self, tmp_path, events, owners):
        build(
            replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking"
        )

    def owners(self):
        return meta(
            libssh2=PackageMetadata(
                teams=("ubuntu-server",), publications=(pub("1.0-2"),)
            )
        )

    def test_a_blocked_proposed_merge_links_to_its_stuck_row(self, tmp_path):
        self.build(tmp_path, [opened(stuck()), opened(merge("libssh2"))], self.owners())
        page = (tmp_path / "merges/teams/ubuntu-server.html").read_text()
        row = page.split('id="libssh2"')[1].split("</tr>")[0]
        tag = chip(row, "stuck in proposed")
        assert 'class="p-chip' in tag
        assert 'href="../../migration/teams/ubuntu-server.html#libssh2"' in tag
        assert "&rarr;" in tag or "\u2192" in tag

    def test_the_link_falls_back_to_the_uploaders_page(self, tmp_path):
        """The merge's team does not own the stuck upload: link where it is."""
        owners = meta(
            libssh2=PackageMetadata(
                teams=("ubuntu-server",), publications=(pub("1.0-2"),)
            )
        )
        events = [opened(stuck()), opened(merge("libssh2"))]
        self.build(tmp_path, events, owners)
        page = (tmp_path / "merges/uploaders/kat.html").read_text()
        tag = chip(page.split('id="libssh2"')[1], "stuck in proposed")
        assert 'href="../../migration/uploaders/kat.html#libssh2"' in tag
        target = (tmp_path / "migration/uploaders/kat.html").read_text()
        assert 'id="libssh2"' in target

    def test_an_unblocked_proposed_merge_is_a_plain_tag(self, tmp_path):
        """The version beside it already links to the upload on Launchpad,
        so a second identical link would add nothing."""
        self.build(tmp_path, [opened(merge("libssh2"))], self.owners())
        page = (tmp_path / "merges/teams/ubuntu-server.html").read_text()
        row = page.split('id="libssh2"')[1].split("</tr>")[0]
        cell = row.split('data-heading="Ubuntu"')[1].split("</td>")[0]
        assert '<span class="p-chip__value">proposed</span>' in cell
        assert cell.count("<a ") == 1
        assert cell.count("+source/libssh2/1.0-2") == 1
        assert ">stuck in proposed<" not in row

    def test_a_stuck_upload_that_still_needs_a_merge_links_to_it(self, tmp_path):
        """shadow: the stuck upload is a delta, and Debian is still ahead."""
        events = [
            opened(stuck(new_version="1.0-1ubuntu2")),
            opened(merge("libssh2", in_proposed=False, version="1.0-1ubuntu1")),
        ]
        self.build(tmp_path, events, self.owners())
        page = (tmp_path / "migration/teams/ubuntu-server.html").read_text()
        row = page.split('id="libssh2"')[1].split("</tr>")[0]
        tag = chip(row, "needs merge")
        assert 'href="../../merges/teams/ubuntu-server.html#libssh2"' in tag

    def test_link_chips_are_neutral_not_coloured(self, tmp_path):
        """A linked chip reads as a tag: no coloured variant, on either board."""
        events = [
            opened(stuck(new_version="1.0-2ubuntu1")),
            opened(merge("libssh2", version="1.0-2ubuntu1")),
        ]
        self.build(tmp_path, events, self.owners())
        for path, text in (
            ("migration/teams/ubuntu-server.html", "needs merge"),
            ("merges/teams/ubuntu-server.html", "stuck in proposed"),
        ):
            tag = chip((tmp_path / path).read_text(), text)
            assert tag.startswith('class="p-chip is-inline'), tag


class TestMergeRowsNameTheirOwnVersionsUploader:
    def test_the_proposed_version_names_its_uploader(self):
        """Issue #10's follow-up: the row must not borrow another version's uploader."""
        owner = PackageMetadata(
            uploader="whoever-is-newest",
            publications=(
                pub("1.0-1", uploader="nadzeya", pocket="Release"),
                pub("1.0-2", uploader="mwhudson"),
            ),
        )
        rows = merge_rows(
            replay([opened(merge("shadow", version="1.0-2"))]),
            meta(shadow=owner),
            now=NOW,
        )
        assert rows[0].uploader == "mwhudson"
        rows = merge_rows(
            replay([opened(merge("shadow", version="1.0-1", in_proposed=False))]),
            meta(shadow=owner),
            now=NOW,
        )
        assert rows[0].uploader == "nadzeya"


def test_autopkgtest_urls_follow_the_pool_layout():
    assert autopkgtest_url("curl") == "https://autopkgtest.ubuntu.com/packages/c/curl"
    assert autopkgtest_url("libssh2", "stonking", "s390x") == (
        "https://autopkgtest.ubuntu.com/packages/libs/libssh2/stonking/s390x"
    )


class TestSnapshotFile:
    def test_publications_survive_a_round_trip(self, tmp_path):
        from cairn.ingest import metadata

        path = tmp_path / "packages.json"
        owner = PackageMetadata(
            uploader="kat",
            publications=(pub("1.0-2", signer=None), pub("1.0-1", pocket="Release")),
        )
        metadata.save(path, meta(libssh2=owner))
        loaded = metadata.load(path).get("libssh2")
        assert loaded == owner
        assert loaded.publication("1.0-2").synced

    def test_a_snapshot_from_before_publications_still_loads(self, tmp_path):
        import json

        from cairn.ingest import metadata

        path = tmp_path / "packages.json"
        path.write_text(
            json.dumps(
                {
                    "collected_at": NOW.isoformat(),
                    "packages": {"cron": {"uploader": "doko", "teams": []}},
                }
            )
        )
        cron = metadata.load(path).get("cron")
        assert cron.uploader == "doko"
        assert cron.publications == ()
        assert cron.publication("1.0") is None


class TestYoungHistory:
    """cairn cannot know what happened before it started looking."""

    def test_a_new_log_does_not_claim_the_whole_board_is_new(self):
        html = render_migration(replay([opened(stuck(), ts=NOW)]), healthy(), now=NOW)
        assert "stuck this week" not in html
        assert "started tracking" not in html

    def test_a_week_of_history_shows_weekly_movement(self):
        html = render_migration(
            replay([opened(stuck(), ts=NOW - timedelta(days=8))]), healthy(), now=NOW
        )
        assert "stuck this week" in html
        assert "started tracking" not in html


def test_guide_links_name_their_target():
    html = render_migration(replay([opened(stuck())]), healthy(), now=NOW)
    why = html.split('id="why"')[1].split("</section>")[0]
    assert ">Autopkgtest regressions<" in why
    assert "Ubuntu project docs" not in why


def test_a_long_regression_list_folds_after_five(tmp_path):
    many = [
        {"test": f"t{i}", "version": "1", "results": {"s390x": "regression"}}
        for i in range(10)
    ]
    sig = stuck(tests=many, reasons=["regression", "needs_approval"])
    owners = meta(libssh2=PackageMetadata(teams=("t",)))
    build(
        replay([opened(sig)]),
        healthy(),
        owners,
        out=tmp_path,
        now=NOW,
        series="stonking",
    )
    row = (tmp_path / "migration/teams/t.html").read_text().split('id="libssh2"')[1]
    row = row.split("</tr>")[0]
    shown, folded = row.split("<details")
    assert ">t4<" in shown and ">t5<" not in shown
    assert "5 more failing tests" in folded and ">t9<" in folded
    assert "Needs approval" in folded.split("</details>")[1]


class TestUploaderIsTheChangelogPersonNotTheSponsor:
    """Measured: shadow 1:4.19.3-2ubuntu1, creator nadzeya, signer seb128."""

    def sponsored(self):
        return PackageMetadata(
            publications=(pub("1.0-2", uploader="nadzeya", signer="seb128"),)
        )

    def test_a_stuck_row_names_the_changelog_person(self):
        [row] = stuck_rows(
            replay([opened(stuck())]), meta(libssh2=self.sponsored()), now=NOW
        )
        assert row.uploader == "nadzeya"
        assert not row.synced

    def test_the_sponsor_gets_no_page(self, tmp_path):
        build(
            replay([opened(stuck()), opened(merge("libssh2"))]),
            healthy(),
            meta(libssh2=self.sponsored()),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        assert (tmp_path / "migration/uploaders/nadzeya.html").exists()
        assert (tmp_path / "merges/uploaders/nadzeya.html").exists()
        assert not list(tmp_path.rglob("seb128.html"))
        everything = "".join(p.read_text() for p in tmp_path.rglob("*.html"))
        assert "seb128" not in everything

    def test_launchpad_creator_is_read_as_the_uploader(self):
        import json

        from cairn.ingest import publications

        raw = json.dumps(
            {
                "entries": [
                    {
                        "source_package_name": "shadow",
                        "source_package_version": "1:4.19.3-2ubuntu1",
                        "package_creator_link": "https://api.launchpad.net/devel/~nadzeya",
                        "package_signer_link": "https://api.launchpad.net/devel/~seb128",
                    }
                ]
            }
        ).encode()
        [found], _ = publications.parse_page(raw)
        assert found.uploader == "nadzeya"
        assert found.signer == "seb128"


class TestRetryLinks:
    """The ♻ britney's excuses page puts beside every regression."""

    def page(self, tmp_path, path="migration/teams/t.html", **extra):
        owners = meta(
            libssh2=PackageMetadata(teams=("t",)), curl=PackageMetadata(teams=("t",))
        )
        build(
            replay([opened(stuck(**extra))]),
            healthy(),
            owners,
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        return (tmp_path / path).read_text()

    def test_matches_britneys_own_link(self):
        """Byte for byte: measured equal on all 724 regressions, 1 Oct 2026."""
        assert retry_url("curl", "s390x", "stonking", "libssh2", "1.11.1-6") == (
            "https://autopkgtest.ubuntu.com/request.cgi?release=stonking"
            "&arch=s390x&package=curl&trigger=libssh2%2F1.11.1-6"
        )

    def test_quotes_epochs_and_plus_like_britney(self):
        url = retry_url("x", "amd64", "stonking", "shadow", "1:4.19.3+dfsg-2")
        assert url.endswith("trigger=shadow%2F1%3A4.19.3%2Bdfsg-2")

    def test_every_regressed_arch_gets_one_on_the_stuck_row(self, tmp_path):
        sig = {
            "tests": [
                {
                    "test": "curl",
                    "version": "8",
                    "results": {"arm64": "regression", "s390x": "regression"},
                }
            ]
        }
        row = self.page(tmp_path, **sig).split('id="libssh2"')[1].split("</tr>")[0]
        for arch in ("arm64", "s390x"):
            assert f"arch={arch}&amp;package=curl&amp;trigger=libssh2%2F1.0-2" in row
        assert row.count('class="cairn-retry"') == 2
        assert '<span class="u-off-screen"> curl on s390x (opens in a new tab)' in row

    def test_says_rerun_in_words_with_a_decorative_icon(self, tmp_path):
        """Text carries the meaning; the icon is hidden from screen readers."""
        row = self.page(tmp_path).split('id="libssh2"')[1].split("</tr>")[0]
        link = row.split('class="cairn-retry"')[1].split("</a>")[0]
        assert "</svg>rerun<" in link
        assert 'aria-hidden="true"' in link
        assert "&#9851;" not in link and "♻" not in link

    def test_the_tests_owner_can_retry_too(self, tmp_path):
        page = self.page(tmp_path)
        blocking = page.split('id="blocking"')[1]
        assert "package=curl&amp;trigger=libssh2%2F1.0-2" in blocking

    def test_only_regressions_get_one(self, tmp_path):
        row = self.page(
            tmp_path,
            reasons=["missing_build"],
            tests=[],
            missing_builds=["riscv64"],
        )
        assert "request.cgi" not in row


class TestUploadColumn:
    def row(self, tmp_path, **extra):
        build(
            replay([opened(stuck(**extra))]),
            healthy(),
            meta(libssh2=PackageMetadata(teams=("t",))),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        page = (tmp_path / "migration/teams/t.html").read_text()
        return page.split('id="libssh2"')[1].split("</tr>")[0]

    def test_both_versions_link_to_launchpad(self, tmp_path):
        row = self.row(tmp_path, old_version="1:1.0-1")
        assert 'href="https://launchpad.net/ubuntu/+source/libssh2/1%3A1.0-1"' in row
        assert 'href="https://launchpad.net/ubuntu/+source/libssh2/1.0-2"' in row

    def test_a_new_package_has_nothing_to_link(self, tmp_path):
        row = self.row(tmp_path, old_version="-")
        upload = row.split('data-heading="Upload"')[1].split("</td>")[0]
        assert ">none<" in upload
        assert "+source/libssh2/-" not in row

    def test_shows_the_component(self, tmp_path):
        row = self.row(tmp_path, component="universe")
        assert 'data-heading="Component" class="cairn-component">' in row
        assert "universe" in row.split('data-heading="Component"')[1].split("</td>")[0]


class TestWhyColumn:
    def row(self, tmp_path, **extra):
        build(
            replay([opened(stuck(**extra))]),
            healthy(),
            meta(libssh2=PackageMetadata(teams=("t",))),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        page = (tmp_path / "migration/teams/t.html").read_text()
        return page.split('id="libssh2"')[1].split("</tr>")[0]

    def test_one_line_per_failing_test(self, tmp_path):
        tests = [
            {"test": "curl", "version": "8", "results": {"s390x": "regression"}},
            {"test": "wget", "version": "1", "results": {"amd64": "regression"}},
        ]
        row = self.row(tmp_path, tests=tests)
        items = row.split('class="cairn-tests"')[1].split("</ul>")[0].split("<li>")[1:]
        assert len(items) == 2
        assert ">curl<" in items[0] and ">wget<" in items[1]

    def test_reasons_are_plain_text_not_coloured_badges(self, tmp_path):
        row = self.row(
            tmp_path,
            reasons=["regression", "needs_approval", "tests_running"],
            hints=["freeze"],
        )
        assert "<strong>Test regression</strong>" in row
        assert "<strong>Needs approval</strong>" in row
        assert "freeze block" in row
        assert "p-status-label" not in row

    def test_the_test_name_is_bold_and_only_architectures_link(self, tmp_path):
        """Package and architecture must not look alike."""
        tests = [
            {"test": "curl", "version": "8.0-1", "results": {"s390x": "regression"}}
        ]
        row = self.row(tmp_path, tests=tests)
        line = row.split('class="cairn-tests"')[1].split("</li>")[0]
        assert (
            '<strong>curl</strong><span class="cairn-test-version">/8.0-1</span>:'
            in line
        )
        assert 'packages/c/curl"' not in line  # no link on the name
        assert (
            'href="https://autopkgtest.ubuntu.com/packages/c/curl/stonking/s390x"'
            in line
        )

    def test_rerun_comes_before_the_regression_tag(self, tmp_path):
        tests = [{"test": "curl", "version": "8", "results": {"s390x": "regression"}}]
        line = self.row(tmp_path, tests=tests).split('class="cairn-tests"')[1]
        line = line.split("</li>")[0]
        assert (
            line.index(">s390x<")
            < line.index("cairn-retry")
            < line.index(">Regression<")
        )
        assert (
            'class="p-chip--negative is-readonly is-inline is-dense cairn-status"'
            in line
        )

    def test_architectures_with_one_status_share_one_tag(self, tmp_path):
        tests = [
            {
                "test": "neutron",
                "version": "1",
                "results": dict.fromkeys(("amd64", "arm64", "ppc64el"), "running"),
            }
        ]
        row = self.row(tmp_path, tests=tests, reasons=["tests_running"])
        line = row.split('class="cairn-tests"')[1].split("</li>")[0]
        assert line.count(">Test in progress<") == 1
        assert "p-chip--information" in line
        for arch in ("amd64", "arm64", "ppc64el"):
            assert f">{arch}</a>" in line
        assert "cairn-retry" not in line

    def test_mixed_failures_get_a_tag_each_regressions_first(self, tmp_path):
        tests = [
            {
                "test": "curl",
                "version": "8",
                "results": {"amd64": "reference_running", "s390x": "regression"},
            }
        ]
        line = self.row(tmp_path, tests=tests).split('class="cairn-tests"')[1]
        line = line.split("</li>")[0]
        assert line.index(">Regression<") < line.index(
            ">Regression, rechecking baseline<"
        )
        assert line.count('class="cairn-retry"') == 2

    def test_running_tests_are_listed_under_their_reason(self, tmp_path):
        tests = [
            {"test": "curl", "version": "8", "results": {"s390x": "regression"}},
            {"test": "wget", "version": "1", "results": {"arm64": "running"}},
        ]
        row = self.row(tmp_path, tests=tests, reasons=["regression", "tests_running"])
        failing, running = row.split("Tests still running")
        assert ">curl<" in failing and ">wget<" not in failing
        assert ">wget<" in running and "Test in progress" in running
        # A running test has nothing to rerun yet.
        assert "cairn-retry" not in running

    def test_missing_builds_link_to_launchpad_per_arch(self, tmp_path):
        row = self.row(
            tmp_path,
            reasons=["missing_build"],
            tests=[],
            missing_builds=["amd64", "riscv64"],
        )
        assert row.count("+latestbuild/") == 2
        assert "<strong>Missing build</strong>" in row

    def test_both_kinds_of_failure_can_be_rerun(self, tmp_path):
        """Britney offers the retry on both kinds of failure; measured 724 + 4."""
        tests = [
            {
                "test": "curl",
                "version": "8",
                "results": {"amd64": "reference_running", "s390x": "regression"},
            }
        ]
        row = self.row(tmp_path, tests=tests)
        assert row.count('class="cairn-retry"') == 2

    def test_waiting_links_to_the_stuck_row_it_waits_for(self, tmp_path):
        build(
            replay(
                [
                    opened(
                        stuck(
                            "a",
                            reasons=["waiting"],
                            tests=[],
                            waits_for=["b", "elsewhere"],
                        )
                    ),
                    opened(stuck("b")),
                ]
            ),
            healthy(),
            meta(a=PackageMetadata(teams=("t",))),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        page = (tmp_path / "migration/teams/t.html").read_text()
        row = page.split('id="a"')[1].split("</tr>")[0]
        assert 'href="../../migration/uploaders/no-uploader-recorded.html#b"' in row
        assert 'href="https://launchpad.net/ubuntu/+source/elsewhere"' in row

    def test_a_new_package_reads_none_to_version_with_a_tag(self, tmp_path):
        row = self.row(tmp_path, old_version="-", new_version="9.5.0-1")
        upload = row.split('data-heading="Upload"')[1].split("</td>")[0]
        assert ">none<" in upload
        assert "new package" not in upload
        new = upload.split("&rarr;")[1]
        assert "9.5.0-1" in new
        assert '<span class="p-chip__value">new</span>' in new


def test_no_page_links_to_britneys_excuses(tmp_path):
    """cairn replaces that page; it does not send readers back to it."""
    events = [opened(stuck()), opened(merge("libssh2")), opened(merge("other"))]
    owners = meta(
        libssh2=PackageMetadata(teams=("t",), publications=(pub("1.0-2"),)),
        other=PackageMetadata(teams=("t",)),
    )
    build(replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking")
    for page in tmp_path.rglob("*.html"):
        assert "update_excuses" not in page.read_text(), page


def resolved_event(sig, ts):
    return Event.of(EventType.RESOLVED, sig, source="migration", ts=ts)


class TestUploaderNeverBorrowed:
    """Copilot review on #13: an absent version must not take the newest
    upload's uploader."""

    def test_an_unrecorded_version_has_no_uploader(self):
        owner = PackageMetadata(
            uploader="newest-uploader",
            publications=(pub("1.0-9", uploader="newest-uploader"),),
        )
        [row] = stuck_rows(replay([opened(stuck())]), meta(libssh2=owner), now=NOW)
        assert row.uploader is None
        [merge_row] = merge_rows(
            replay([opened(merge("libssh2"))]), meta(libssh2=owner), now=NOW
        )
        assert merge_row.uploader is None

    def test_an_old_snapshot_without_publications_still_falls_back(self):
        owner = PackageMetadata(uploader="kat")
        assert owner.uploader_of("anything") == "kat"


class TestUploadsArePerEpisode:
    """Copilot review on #13: migrating in between ends the count."""

    def test_a_resolve_and_reopen_starts_again(self):
        first = stuck(new_version="1.0-2")
        second = stuck(new_version="1.0-5")
        events = [
            opened(first, T0),
            resolved_event(first, T0 + timedelta(days=2)),
            opened(second, T0 + timedelta(days=10)),
        ]
        assert list(uploads_seen(events).values()) == [1]
        [row] = stuck_rows(replay(events), now=NOW, uploads=uploads_seen(events))
        assert row.uploads == 1
        assert row.occurrences == 2

    def test_reuploads_within_one_episode_still_count(self):
        events = [
            opened(stuck(new_version="1.0-2"), T0),
            updated(stuck(new_version="1.0-3"), T0 + timedelta(days=1)),
            updated(stuck(new_version="1.0-4"), T0 + timedelta(days=2)),
        ]
        assert list(uploads_seen(events).values()) == [3]

    def test_a_resolved_signal_has_no_count(self):
        sig = stuck()
        events = [opened(sig, T0), resolved_event(sig, T0 + timedelta(days=1))]
        assert uploads_seen(events) == {}


class TestWeeklyMovementFromEvents:
    """Copilot review on #13: replayed state keeps first_seen on reopen and
    clears resolved_at, so a reopening this week counted as neither."""

    def events(self):
        sig = stuck()
        return [
            opened(sig, NOW - timedelta(days=30)),
            resolved_event(sig, NOW - timedelta(days=3)),
            opened(sig, NOW - timedelta(days=1)),
        ]

    def test_a_package_that_left_and_returned_this_week_counts_both_ways(self):
        events = self.events()
        state = replay(events)
        overview = migration_overview(
            state,
            stuck_rows(state, now=NOW),
            [],
            now=NOW,
            history=History.of(events),
        )
        assert overview.opened_recently == 1
        assert overview.resolved_recently == 1

    def test_end_state_alone_would_have_missed_it(self):
        state = replay(self.events())
        overview = migration_overview(state, stuck_rows(state, now=NOW), [], now=NOW)
        assert (overview.opened_recently, overview.resolved_recently) == (0, 0)

    def test_the_merges_board_counts_from_events_too(self):
        from cairn.build.site import overview

        sig = merge("cron")
        events = [
            Event.of(EventType.OPENED, sig, source="merges", ts=NOW - timedelta(30)),
            Event.of(EventType.RESOLVED, sig, source="merges", ts=NOW - timedelta(3)),
            Event.of(EventType.OPENED, sig, source="merges", ts=NOW - timedelta(1)),
        ]
        state = replay(events)
        found = overview(
            state, merge_rows(state, now=NOW), {}, now=NOW, history=History.of(events)
        )
        assert (found.opened_recently, found.resolved_recently) == (1, 1)


class TestReviewFollowUps:
    def test_a_newer_merge_version_is_not_shown_as_blocked(self, tmp_path):
        """Copilot review on #13: a carried-forward verdict about 1.0-2 says
        nothing about 1.0-3."""
        events = [opened(stuck()), opened(merge("libssh2", version="1.0-3"))]
        owners = meta(libssh2=PackageMetadata(teams=("t",)))
        build(
            replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking"
        )
        row = (tmp_path / "merges/teams/t.html").read_text().split('id="libssh2"')[1]
        row = row.split("</tr>")[0]
        assert ">stuck in proposed<" not in row
        assert ">proposed<" in row

    def test_leaving_the_list_is_not_called_migrating(self):
        html = render_migration(
            replay([opened(stuck(), ts=NOW - timedelta(days=8))]), healthy(), now=NOW
        )
        assert "left the list this week" in html
        assert "migrated this week" not in html

    def test_settling_is_described_as_age_only(self):
        html = render_migration(replay([opened(stuck())]), healthy(), now=NOW)
        assert "only waiting for tests" not in html


def test_a_stuck_page_has_no_footer_link_back(tmp_path):
    """The breadcrumb at the top already leads back to the board."""
    build(
        replay([opened(stuck())]),
        healthy(),
        meta(libssh2=PackageMetadata(teams=("t",))),
        out=tmp_path,
        now=NOW,
        series="stonking",
    )
    page = (tmp_path / "migration/teams/t.html").read_text()
    assert "Everything stuck in proposed" not in page
    assert 'href="../../migration/index.html">Stuck in proposed</a> /' in page


class TestOwnershipWarningOnTheMigrationBoard:
    """Copilot review on #13: the stuck board routes by ownership too."""

    def test_says_so_when_ownership_is_old(self):
        old = Snapshot(collected_at=NOW - timedelta(days=5), packages={})
        html = render_migration(replay([opened(stuck())]), healthy(), old, now=NOW)
        assert "Ownership data is 5 days old" in html

    def test_says_so_when_ownership_was_never_collected(self):
        html = render_migration(
            replay([opened(stuck())]), healthy(), Snapshot(), now=NOW
        )
        assert "No ownership data" in html

    def test_quiet_when_ownership_is_fresh(self):
        html = render_migration(replay([opened(stuck())]), healthy(), meta(), now=NOW)
        assert "Ownership data is" not in html
        assert "No ownership data" not in html


def test_contents_list_only_the_groupings():
    owners = meta(libssh2=PackageMetadata(teams=("t",)))
    html = render_migration(replay([opened(stuck())]), healthy(), owners, now=NOW)
    nav = html.split('<nav aria-label="Contents">')[1].split("</nav>")[0]
    assert "#why" not in nav
    assert "#by-team" in nav
    assert 'id="why"' in html  # the section itself stays


class TestThirdReview:
    """Copilot's third review on #13."""

    def footer(self, html):
        return html.split("<footer")[1]

    def test_each_board_dates_its_own_data(self, tmp_path):
        """A fresh merges run must not vouch for stale migration data."""
        health = {
            "merges": SourceHealth("merges", last_attempt=NOW, last_success=NOW),
            "migration": SourceHealth(
                "migration",
                last_attempt=NOW,
                last_success=NOW - timedelta(days=3),
            ),
        }
        events = [opened(stuck()), opened(merge("libssh2"))]
        build(replay(events), health, meta(), out=tmp_path, now=NOW, series="stonking")
        merges = self.footer((tmp_path / "merges/index.html").read_text())
        stuck_page = self.footer((tmp_path / "migration/index.html").read_text())
        home = self.footer((tmp_path / "index.html").read_text())
        assert "Data collected 2026-10-01 12:00 UTC." in merges
        assert "Data collected 2026-09-28 12:00 UTC." in stuck_page
        assert "Data collected 2026-09-28 12:00 UTC." in home  # the oldest

    def test_a_board_whose_source_never_ran_says_so(self, tmp_path):
        health = {"merges": SourceHealth("merges", last_attempt=NOW, last_success=NOW)}
        build(
            replay([opened(stuck())]), health, meta(), out=tmp_path, now=NOW, series="s"
        )
        page = (tmp_path / "migration/index.html").read_text()
        assert "No data collected yet." in self.footer(page)

    def test_needs_merge_requires_debian_ahead_of_the_stuck_upload(self, tmp_path):
        """A carried-forward merge about an older upload says nothing here."""
        events = [
            opened(stuck(new_version="2.0-1ubuntu1")),
            opened(merge("libssh2", in_proposed=False, version="1.0-1ubuntu1")),
        ]
        build(
            replay(events),
            healthy(),
            meta(libssh2=PackageMetadata(teams=("t",))),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        row = (tmp_path / "migration/teams/t.html").read_text()
        row = row.split('id="libssh2"')[1].split("</tr>")[0]
        assert "needs merge" not in row  # Debian 2.0-1 is not ahead of 2.0-1ubuntu1

    def test_a_stuck_sync_does_not_need_a_merge(self, tmp_path):
        """No Ubuntu delta: syncing it is not a merge, whatever Debian has."""
        events = [
            opened(stuck(new_version="1.5-1")),
            opened(merge("libssh2", in_proposed=False, version="1.0-1ubuntu1")),
        ]
        build(
            replay(events),
            healthy(),
            meta(libssh2=PackageMetadata(teams=("t",))),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        row = (tmp_path / "migration/teams/t.html").read_text()
        row = row.split('id="libssh2"')[1].split("</tr>")[0]
        assert "needs merge" not in row

    def test_the_caption_does_not_claim_every_link_leaves(self, tmp_path):
        build(
            replay([opened(stuck())]),
            healthy(),
            meta(libssh2=PackageMetadata(teams=("t",))),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        page = (tmp_path / "migration/teams/t.html").read_text()
        caption = page.split('<table class="p-table--mobile-card cairn-stuck">')[1]
        caption = caption.split("</caption>")[0]
        assert "Links open in a new tab" not in caption
        assert "Package, version, test and build links open in a new tab" in caption


def test_the_stuck_count_is_not_a_link_to_its_own_page(tmp_path):
    build(
        replay([opened(stuck())]),
        healthy(),
        meta(libssh2=PackageMetadata(teams=("t",))),
        out=tmp_path,
        now=NOW,
        series="stonking",
    )
    header = (tmp_path / "migration/teams/t.html").read_text()
    header = header.split("<h1")[1].split("</section>")[0]
    assert "1 stuck in proposed" in header
    assert 'href="#stuck"' not in header


def test_proposed_chips_name_the_two_states(tmp_path):
    """ "proposed" when nothing holds it; "stuck in proposed", linking to the
    board of that name, when britney does."""
    events = [
        opened(stuck("held", new_version="1.0-2")),
        opened(merge("held", version="1.0-2")),
        opened(merge("free", version="1.0-2")),
    ]
    owners = meta(
        held=PackageMetadata(teams=("t",)), free=PackageMetadata(teams=("t",))
    )
    build(replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking")
    page = (tmp_path / "merges/teams/t.html").read_text()
    held = page.split('id="held"')[1].split("</tr>")[0]
    free = page.split('id="free"')[1].split("</tr>")[0]
    assert "#held" in chip(held, "stuck in proposed")
    assert '<span class="p-chip__value">proposed</span>' in free
    assert ">stuck in proposed<" not in free
    assert "·" not in page


def test_row_anchors_are_the_package_name(tmp_path):
    """The directory already names the board; the fragment needs no prefix."""
    events = [
        opened(stuck(new_version="1.0-2ubuntu1")),
        opened(merge("libssh2", version="1.0-2ubuntu1")),
    ]
    owners = meta(libssh2=PackageMetadata(teams=("t",)))
    build(replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking")
    merges = (tmp_path / "merges/teams/t.html").read_text()
    stuck_page = (tmp_path / "migration/teams/t.html").read_text()
    assert 'href="../../migration/teams/t.html#libssh2"' in merges
    assert 'href="../../merges/teams/t.html#libssh2"' in stuck_page
    for page in (merges, stuck_page):
        assert 'id="merge-' not in page and 'id="stuck-' not in page


def test_page_section_ids_cannot_be_mistaken_for_a_package():
    """Row ids are package names, so a section id on an owner page must not
    be one. Measured on 1 Oct 2026: of the section ids, only "age" is also a
    source package, and it sits on the merges index, which has no rows."""
    import re
    from pathlib import Path

    templates = Path(__file__).parent.parent / "cairn/build/templates"
    owner_pages = ("base.html", "group.html", "migration_group.html", "_migration.html")
    ids = set()
    for name in owner_pages:
        ids |= set(re.findall(r'id="([a-z-]+)"', (templates / name).read_text()))
    assert ids <= {"navigation", "stuck", "blocking", "merges"}


class TestFourthReview:
    """Copilot's fourth review on #13: freshness is per board."""

    def health(self, *, merges_days=0, migration_days=0, migration=True):
        found = {
            "merges": SourceHealth(
                "merges",
                last_attempt=NOW,
                last_success=NOW - timedelta(days=merges_days),
            )
        }
        if migration:
            found["migration"] = SourceHealth(
                "migration",
                last_attempt=NOW,
                last_success=NOW - timedelta(days=migration_days),
            )
        return found

    def build(self, tmp_path, health):
        events = [opened(stuck()), opened(merge("libssh2"))]
        build(replay(events), health, meta(), out=tmp_path, now=NOW, series="stonking")
        return {
            name: (tmp_path / path).read_text()
            for name, path in (
                ("home", "index.html"),
                ("merges", "merges/index.html"),
                ("migration", "migration/index.html"),
            )
        }

    def banner(self, html):
        main = html.split("<main>")[1].split("<footer")[0]
        if "Data may be out of date" not in main:
            return None
        return main.split("Data may be out of date")[1].split("</p>")[0]

    def test_a_stale_migration_ingest_does_not_mark_merges_stale(self, tmp_path):
        pages = self.build(tmp_path, self.health(migration_days=3))
        assert self.banner(pages["merges"]) is None
        assert "migration" in self.banner(pages["migration"])
        assert "merges" not in self.banner(pages["migration"])
        assert "migration" in self.banner(pages["home"])

    def test_a_stale_merges_ingest_does_not_mark_migration_stale(self, tmp_path):
        pages = self.build(tmp_path, self.health(merges_days=3))
        assert self.banner(pages["migration"]) is None
        assert "merges" in self.banner(pages["merges"])

    def test_the_root_has_no_date_until_every_board_has_one(self, tmp_path):
        """Migration has never run: the root must not borrow merges' date."""
        pages = self.build(tmp_path, self.health(migration=False))
        assert "No data collected yet." in pages["home"].split("<footer")[1]
        assert "Data collected 2026-10-01" in pages["merges"].split("<footer")[1]
        assert "No data collected yet." in pages["migration"].split("<footer")[1]


class TestAllPackages:
    """Every row of a board on one page, reached from the board's total."""

    def built(self, tmp_path):
        events = [
            opened(stuck("libssh2")),
            opened(stuck("sudo")),
            opened(merge("cron", in_proposed=False)),
            opened(merge("grub2", in_proposed=False)),
        ]
        owners = meta(
            libssh2=PackageMetadata(teams=("a",)),
            sudo=PackageMetadata(package_sets=("core",)),
            cron=PackageMetadata(teams=("a",)),
        )
        build(
            replay(events), healthy(), owners, out=tmp_path, now=NOW, series="stonking"
        )
        return tmp_path

    def test_each_board_lists_every_package(self, tmp_path):
        out = self.built(tmp_path)
        merges = (out / "merges/all.html").read_text()
        stuck_page = (out / "migration/all.html").read_text()
        assert 'id="cron"' in merges and 'id="grub2"' in merges
        assert 'id="libssh2"' in stuck_page and 'id="sudo"' in stuck_page
        assert "<title>Cairn | All merge candidates</title>" in merges
        assert "<title>Cairn | All uploads stuck in proposed</title>" in stuck_page

    def test_every_ownership_column_is_shown(self, tmp_path):
        page = (self.built(tmp_path) / "migration/all.html").read_text()
        for heading in ("Uploader", "Subscribed team", "Package set"):
            assert f">{heading}</th>" in page

    def test_all_leads_the_contents_row(self, tmp_path):
        """A peer of the groupings, first in the row; not a button and not a
        link on a statistic."""
        out = self.built(tmp_path)
        for board, label in (
            ("merges", "All candidates"),
            ("migration", "All packages"),
        ):
            index = (out / f"{board}/index.html").read_text()
            nav = index.split('<nav aria-label="Contents">')[1].split("</nav>")[0]
            assert nav.index(f'<a href="all.html">{label}</a>') < nav.index("#by-")
            assert index.count('href="all.html"') == 1
            assert "p-button" not in index.split("<main>")[1]

    def test_links_from_the_all_page_resolve(self, tmp_path):
        import re

        out = self.built(tmp_path)
        for page in (out / "merges/all.html", out / "migration/all.html"):
            for href in re.findall(r'href="([^"#]+)', page.read_text()):
                if href.startswith("http"):
                    continue
                assert (page.parent / href).resolve().exists(), (page, href)

    def test_an_empty_board_does_not_link_to_an_empty_page(self, tmp_path):
        build(
            replay([opened(merge("cron", in_proposed=False))]),
            healthy(),
            meta(),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        index = (tmp_path / "migration/index.html").read_text()
        assert 'href="all.html"' not in index
        assert not (tmp_path / "migration/all.html").exists()


class TestHoldingBackLinksToCairn:
    def page(self, tmp_path, owners):
        build(
            replay([opened(stuck())]),
            healthy(),
            owners,
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        page = (tmp_path / "migration/teams/tests.html").read_text()
        return page.split('id="blocking"')[1].split("</table>")[0]

    def test_links_to_the_uploaders_page_row(self, tmp_path):
        owners = meta(
            libssh2=PackageMetadata(publications=(pub("1.0-2", uploader="kat"),)),
            curl=PackageMetadata(teams=("tests",)),
        )
        cell = self.page(tmp_path, owners).split('data-heading="Holding back"')[1]
        cell = cell.split("</td>")[0]
        assert 'href="../../migration/uploaders/kat.html#libssh2"' in cell
        assert 'launchpad.net/ubuntu/+source/libssh2"' not in cell

    def test_links_on_the_same_page_when_the_owner_has_it(self, tmp_path):
        owners = meta(
            libssh2=PackageMetadata(teams=("tests",)),
            curl=PackageMetadata(teams=("tests",)),
        )
        cell = self.page(tmp_path, owners).split('data-heading="Holding back"')[1]
        assert (
            'href="../../migration/teams/tests.html#libssh2"' in cell.split("</td>")[0]
        )


def test_settling_says_what_it_means():
    html = render_migration(replay([opened(stuck())]), healthy(), now=NOW)
    assert "uploader's hands" not in html
    assert "uploaded to -proposed less than" in " ".join(html.split())


def test_old_and_new_version_share_one_line(tmp_path):
    """No block element between them: old → new reads on one line, and each
    version is kept whole so a long pair can only break after the arrow."""
    build(
        replay([opened(stuck(old_version="1:1.0-1", new_version="1:1.0-2"))]),
        healthy(),
        meta(libssh2=PackageMetadata(teams=("t",))),
        out=tmp_path,
        now=NOW,
        series="stonking",
    )
    page = (tmp_path / "migration/teams/t.html").read_text()
    cell = page.split('data-heading="Upload"')[1].split("</td>")[0]
    assert "<div" not in cell
    assert cell.count('class="cairn-version"') == 2
    old, new = cell.split('class="cairn-version"')[1:]
    assert "1:1.0-1" in old and "&rarr;" not in old
    assert "&rarr;</span>&nbsp;<a" in new and "1:1.0-2" in new


class TestPlusOneLink:
    def index(self, tmp_path, signer):
        owner = PackageMetadata(
            publications=(pub("1.0-2", uploader="sanvila", signer=signer),)
        )
        build(
            replay([opened(stuck())]),
            healthy(),
            meta(libssh2=owner),
            out=tmp_path,
            now=NOW,
            series="stonking",
        )
        index = (tmp_path / "migration/index.html").read_text()
        return index.split('<nav aria-label="Contents">')[1].split("</nav>")[0]

    def test_the_board_links_to_plus_one_maintenance(self, tmp_path):
        nav = self.index(tmp_path, signer=None)
        assert (
            '<a href="../migration/uploaders/plus-one-maintenance.html">'
            "+1 maintenance</a>"
        ) in nav
        target = tmp_path / "migration/uploaders/plus-one-maintenance.html"
        assert target.exists()

    def test_no_link_when_nothing_is_routed_there(self, tmp_path):
        assert "+1 maintenance" not in self.index(tmp_path, signer="seb128")
