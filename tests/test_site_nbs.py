"""The NBS board, and NBS on package pages.

Payloads are shaped as cairn/ingest/nbs.py writes them, from the real
23 Nov 2026 cases: libfmt10 needed by ccache, the giza pair needed only by
each other, libmaven-exec-plugin-java provided by its renamed successor.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from cairn.build.nbs import NbsRow, NeededBy, nbs_rows, needs_nbs
from cairn.build.site import History, build
from cairn.core.health import SourceHealth
from cairn.core.log import Event, EventType, replay
from cairn.ingest.base import Kind, Signal
from cairn.ingest.metadata import Snapshot

NOW = datetime(2026, 11, 23, 20, tzinfo=UTC)
T0 = NOW - timedelta(days=2)
ARCHES = ["amd64", "amd64v3", "arm64", "i386"]


def nbs(binary, source, **payload):
    base = {
        "version": "1.0-1",
        "source_version": "1.0-1",
        "current_version": "2.0-1",
        "component": "universe",
        "architectures": ARCHES,
    }
    return Signal(
        kind=Kind.NBS,
        source_package=source,
        binary_package=binary,
        series="tenacious",
        payload=base | payload,
    )


FMT = nbs(
    "libfmt10",
    "fmtlib",
    rdepends=[
        {
            "package": "ccache",
            "source": "ccache",
            "component": "universe",
            "architectures": ["amd64", "amd64v3", "arm64"],
        },
        {
            "package": "libgenomicsdb1",
            "source": "genomicsdb",
            "component": "universe",
            "architectures": ARCHES,
        },
        {
            "package": "libtorch-rocm-2.12",
            "source": "pytorch-rocm",
            "component": "universe",
            "architectures": ["amd64"],
        },
    ],
    build_rdepends=[{"package": "volk", "component": "universe"}],
)
GIZA = nbs(
    "libgiza0",
    "giza",
    removable=True,
    rdepends=[
        {
            "package": "libcpgplot0",
            "source": "giza",
            "component": "universe",
            "architectures": ARCHES,
            "nbs": True,
        }
    ],
)
CPGPLOT = nbs("libcpgplot0", "giza", removable=True)
MAVEN = nbs(
    "libmaven-exec-plugin-java",
    "exec-maven-plugin",
    architectures=["all"],
    provided_by=["libexec-maven-plugin-java"],
    removable=True,
)


def ev(kind, sig, ts=T0):
    return Event.of(kind, sig, source="nbs", ts=ts)


def site(tmp_path, events):
    build(
        replay(events),
        {
            s: SourceHealth(s, last_attempt=NOW, last_success=NOW)
            for s in ("merges", "migration", "nbs")
        },
        Snapshot(collected_at=NOW),
        out=tmp_path,
        now=NOW,
        series="tenacious",
        history=History.of(events),
    )
    return tmp_path


def text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


EVENTS = [ev(EventType.OPENED, s) for s in (FMT, GIZA, CPGPLOT, MAVEN)]


class TestRows:
    def test_read_from_the_log(self):
        rows = {r.binary: r for r in nbs_rows(replay(EVENTS))}
        assert set(rows) == {
            "libfmt10",
            "libgiza0",
            "libcpgplot0",
            "libmaven-exec-plugin-java",
        }
        assert rows["libfmt10"].sources_needing == (
            "ccache",
            "genomicsdb",
            "pytorch-rocm",
            "volk",
        )
        assert rows["libgiza0"].removable
        # Needed only by an NBS binary that goes with it: no work left.
        assert rows["libgiza0"].needing == ()

    def test_where_names_the_shorter_side(self):
        row = nbs_rows(replay([ev(EventType.OPENED, FMT)]))[0]
        by_package = {n.package: n for n in row.needed_by}
        assert row.where(by_package["ccache"]) == ("not on", ("i386",))
        assert row.where(by_package["libtorch-rocm-2.12"]) == ("on", ("amd64",))
        assert row.where(by_package["libgenomicsdb1"]) is None
        assert row.where(by_package["volk"]) is None

    def test_turned_around_by_the_package_that_needs_it(self):
        found = needs_nbs(nbs_rows(replay(EVENTS)))
        assert [r.binary for r, _ in found["ccache"]] == ["libfmt10"]
        assert found["volk"][0][1].build
        assert "giza" not in found  # only needed by its own NBS binary

    def test_architecture_independent_has_no_where(self):
        row = NbsRow(
            binary="b",
            source="s",
            version="1",
            current_version=None,
            component=None,
            architectures=("all",),
            needed_by=(),
            provided_by=(),
            removable=True,
            first_seen=NOW,
        )
        assert row.where(NeededBy("x", "x", None, ("amd64",))) is None


class TestBoard:
    def test_two_lists_by_who_acts(self, tmp_path):
        page = (site(tmp_path, EVENTS) / "nbs/index.html").read_text()
        assert "<span>Still needed (1)</span>" in page
        assert "<span>Can be removed (3)</span>" in page
        needed = page.split('id="still_needed"')[1].split('id="can_remove"')[0]
        removable = page.split('id="can_remove"')[1].split('id="board_stats"')[0]
        assert 'id="libfmt10"' in needed and 'id="libgiza0"' not in needed
        assert 'id="libgiza0"' in removable and 'id="libfmt10"' not in removable

    def test_what_needs_it(self, tmp_path):
        page = (site(tmp_path, EVENTS) / "nbs/index.html").read_text()
        row = text(page.split('id="libfmt10"')[1].split("</tr>")[0])
        assert "ccache : ccache not on i386" in row
        assert "pytorch-rocm : libtorch-rocm-2.12 on amd64" in row
        assert "volk : to build" in row
        assert "now 2.0-1 , which does not build it" in row

    def test_a_provided_name_says_what_provides_it(self, tmp_path):
        page = (site(tmp_path, EVENTS) / "nbs/index.html").read_text()
        row = text(page.split('id="libmaven-exec-plugin-java"')[1].split("</tr>")[0])
        assert "libexec-maven-plugin-java provides its name" in row
        assert "architecture-independent" in row

    def test_needed_only_by_another_nbs_binary(self, tmp_path):
        page = (site(tmp_path, EVENTS) / "nbs/index.html").read_text()
        row = page.split('id="libgiza0"')[1].split("</tr>")[0]
        assert "also NBS" in row
        assert 'href="#libcpgplot0"' in row

    def test_in_the_navigation_and_on_the_root(self, tmp_path):
        out = site(tmp_path, EVENTS)
        root = (out / "index.html").read_text()
        assert 'href="nbs/index.html"' in root
        assert "4</a>" in root.split("NBS</a></h3>")[1][:300]

    def test_every_label_points_at_a_definition_on_its_page(self, tmp_path):
        out = site(tmp_path, EVENTS)
        for page in ("nbs/index.html", "packages/fmtlib.html", "packages/ccache.html"):
            html = (out / page).read_text()
            ids = set(re.findall(r'\bid="([^"]+)"', html))
            for ref in re.findall(r'aria-describedby="([^"]+)"', html):
                assert ref in ids, (page, ref)


class TestPackagePages:
    def test_the_source_that_left_it_behind(self, tmp_path):
        page = text((site(tmp_path, EVENTS) / "packages/fmtlib.html").read_text())
        assert "NBS: binaries it no longer builds" in page
        assert "libfmt10 1.0-1 : still needed by ccache , genomicsdb" in page

    def test_a_removable_one_says_so(self, tmp_path):
        page = text((site(tmp_path, EVENTS) / "packages/giza.html").read_text())
        assert "nothing needs it, so it can be removed" in page

    def test_the_package_that_still_needs_it(self, tmp_path):
        """ccache has no signal of its own: its page exists for this."""
        out = site(tmp_path, EVENTS)
        page = text((out / "packages/ccache.html").read_text())
        assert "Needs NBS binaries" in page
        assert "ccache needs libfmt10 , which fmtlib no longer builds" in page
        assert "Builds with libfmt10" in text((out / "packages/volk.html").read_text())

    def test_others_say_none(self, tmp_path):
        events = [
            *EVENTS,
            Event.of(
                EventType.OPENED,
                Signal(
                    kind=Kind.NEEDS_MERGE,
                    source_package="zlib",
                    series="tenacious",
                    payload={"ubuntu_version": "1-1ubuntu1", "debian_version": "2-1"},
                ),
                source="merges",
                ts=T0,
            ),
        ]
        page = text((site(tmp_path, events) / "packages/zlib.html").read_text())
        assert "No NBS binaries" in page

    def test_history(self, tmp_path):
        events = [
            *EVENTS,
            ev(EventType.UPDATED, nbs("libfmt10", "fmtlib", removable=True), NOW),
            ev(EventType.RESOLVED, CPGPLOT, NOW),
        ]
        out = site(tmp_path, events)
        fmt = text((out / "packages/fmtlib.html").read_text())
        assert "NBS Binary libfmt10 1.0-1 no longer built" in fmt
        assert "Nothing needs libfmt10 any more: it can be removed." in fmt
        giza = text((out / "packages/giza.html").read_text())
        assert "Binary libcpgplot0 gone from the archive, or built again." in giza

    def test_a_package_here_only_for_needing_one_has_its_component(self, tmp_path):
        """Review on #26: ccache had data-f-component="unknown"."""
        page = (site(tmp_path, EVENTS) / "packages/index.html").read_text()
        item = page.split('href="../packages/ccache.html"')[0].rsplit("<li", 1)[1]
        assert 'data-f-component="universe"' in item

    def test_the_package_list_marks_both(self, tmp_path):
        page = (site(tmp_path, EVENTS) / "packages/index.html").read_text()
        assert "1 NBS" in text(page.split('href="../packages/fmtlib.html"')[1][:600])
        assert "needs 1 NBS" in text(
            page.split('href="../packages/ccache.html"')[1][:600]
        )


class TestChain:
    """Review on #26, as on 23 Nov 2026: libcamel-1.2-64t64 is needed only
    by libedataserver-1.2-27t64, NBS itself and still needed by folks."""

    CAMEL = nbs(
        "libcamel-1.2-64t64",
        "evolution-data-server",
        rdepends=[
            {
                "package": "libedataserver-1.2-27t64",
                "source": "evolution-data-server",
                "component": "main",
                "architectures": ARCHES,
                "nbs": True,
            }
        ],
    )
    EDS = nbs(
        "libedataserver-1.2-27t64",
        "evolution-data-server",
        rdepends=[
            {
                "package": "libfolks-eds26",
                "source": "folks",
                "component": "universe",
                "architectures": ARCHES,
            }
        ],
    )
    EVENTS = tuple(ev(EventType.OPENED, s) for s in (CAMEL, EDS, GIZA, CPGPLOT))

    def test_a_still_needed_nbs_binary_is_work(self):
        rows = {r.binary: r for r in nbs_rows(replay(self.EVENTS))}
        camel = rows["libcamel-1.2-64t64"]
        assert [n.package for n in camel.needing] == ["libedataserver-1.2-27t64"]
        assert camel.still_needed_nbs == {"libedataserver-1.2-27t64"}
        # A removable one still goes with it.
        assert rows["libgiza0"].needing == ()

    def test_the_board_links_it_and_says_it_is_nbs(self, tmp_path):
        page = (site(tmp_path, list(self.EVENTS)) / "nbs/index.html").read_text()
        row = page.split('id="libcamel-1.2-64t64"')[1].split("</tr>")[0]
        assert 'href="#libedataserver-1.2-27t64"' in row
        assert 'aria-describedby="term_nbs_needed"' in row
        assert "also NBS" not in row
        assert "also NBS" in page.split('id="libgiza0"')[1].split("</tr>")[0]

    def test_the_package_page_names_it(self, tmp_path):
        out = site(tmp_path, list(self.EVENTS))
        page = text((out / "packages/evolution-data-server.html").read_text())
        assert (
            "libcamel-1.2-64t64 1.0-1 : still needed by NBS binary "
            "libedataserver-1.2-27t64 that is still needed itself."
        ) in page
        assert "still needed by folks" in page
