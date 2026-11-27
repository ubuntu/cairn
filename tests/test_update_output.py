"""Candidates britney tried and skipped, from its run log.

Fixtures: the 26 Nov 2026 19:53 UTC run's update_output.txt and excuses,
trimmed to the same uploads, and the stanzas of the release and -proposed
Packages indexes for the binaries the log names.
"""

from __future__ import annotations

import lzma
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cairn.ingest.migration import (
    EXCUSES_URL,
    MigrationIngester,
    Reason,
)
from cairn.ingest.update_output import OUTPUT_URL, parse

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUT = (FIXTURES / "update_output_2026-11-26.txt").read_bytes()
EXCUSES = (FIXTURES / "update_excuses_2026-11-26.yaml").read_bytes()
GENERATED = datetime(2026, 11, 26, 19, 53, 27, 182607, tzinfo=UTC)
SOON_AFTER = GENERATED + timedelta(hours=1)


class Britney:
    """Serves the excuses, the run log and the Packages indexes."""

    def __init__(self, *, output=OUTPUT, excuses=EXCUSES, indexes=True):
        self.output = output
        self.excuses = excuses
        self.indexes = indexes
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if url == EXCUSES_URL:
            return lzma.compress(self.excuses)
        if url == OUTPUT_URL:
            if isinstance(self.output, Exception):
                raise self.output
            return self.output
        if "/binary-" in url:
            if not self.indexes:
                raise OSError("archive unreachable")
            suite = url.split("/dists/")[1].split("/")[0]
            arch = url.split("/binary-")[1].split("/")[0]
            fixture = FIXTURES / f"update_output_packages_{suite}_{arch}"
            # One file per suite and architecture, served as main.
            body = fixture.read_bytes() if "/main/" in url and fixture.exists() else b""
            return lzma.compress(body)
        raise AssertionError(f"unexpected url {url}")


def run(fetcher=None):
    ing = MigrationIngester(
        fetcher or Britney(), series="tenacious", now=lambda: SOON_AFTER
    )
    signals = {s.source_package: s for s in ing.run()}
    return signals, ing.detail()


@pytest.fixture(scope="module")
def found():
    return run()


@pytest.fixture(scope="module")
def output():
    return parse(OUTPUT)


class TestParse:
    def test_dated_by_britney(self, output):
        assert output.generated == datetime(2026, 11, 26, 19, 57, 20, tzinfo=UTC)

    def test_architecture_order(self, output):
        assert output.architectures[:2] == ("amd64", "amd64v3")
        assert output.architectures[-1] == "s390x"

    def test_what_migrated(self, output):
        assert output.migrated == {"ccache", "dz6/armhf"}

    def test_a_skip_names_what_would_break(self, output):
        found = output.breaks["python-pathable"]
        assert found.arch == "amd64"
        assert found.binaries == (
            "python3-jsonschema-path",
            "python3-openapi-core",
            "python3-openapi-spec-validator",
        )

    def test_the_last_attempt_alone_counts_not_the_group(self, output):
        """rust-libcst broke 828 on s390x with two others; alone, one."""
        found = output.breaks["rust-libcst"]
        assert found.binaries == ("librust-libcst-dev",)
        assert found.together == ()

    def test_a_migrated_item_has_no_breakage(self, output):
        assert "ccache" not in output.breaks

    def test_cruft_removals_are_read_too(self, output):
        """Not candidates, so no signal: but they are britney's results."""
        assert output.breaks["-kmc/amd64v3"].binaries == ("iva", "shovill")

    def test_only_one_items_result_follows_a_skip(self):
        """The "* arch:" summary after endloop belongs to no item."""
        text = (
            "trying: a\nskipped: a (0, 0, 1)\n    got: 1+0: a-1\n"
            "    * amd64: x\n finish: []\nendloop: 0+0: a-0\n"
            "    now: 1+0: a-1\n    * amd64: y, z\n"
        )
        assert parse(text).breaks["a"].binaries == ("x",)

    def test_an_item_tried_only_in_a_group(self):
        text = (
            "trying: a b\nskipped: a b (0, 0, 0)\n    got: 1+0: a-1\n    * arm64: x\n"
        )
        found = parse(text).breaks
        assert found["a"].together == ("b",)
        assert found["b"].arch == "arm64"

    def test_an_attempt_britney_refused(self):
        text = "trying: a\nskipped: a (0, 0, 0)\n    got exception: Boom('x')\n"
        found = parse(text).breaks["a"]
        assert found.arch == ""
        assert found.error == "Boom('x')"

    def test_a_skip_later_accepted_migrated(self):
        text = (
            "trying: a\nskipped: a (0, 0, 1)\n    got: 1+0: a-1\n"
            "    * amd64: x\ntrying: a\naccepted: a\nfinal: a\nSUCCESS (1/0)\n"
        )
        assert parse(text).breaks == {}

    def test_same_run(self, output):
        assert output.same_run(GENERATED)
        # The next run's excuses beside this log.
        assert not output.same_run(GENERATED + timedelta(hours=2))
        # An older log beside newer excuses.
        assert not output.same_run(GENERATED + timedelta(minutes=10))
        assert not output.same_run(None)

    def test_garbage_is_no_run(self):
        assert parse(b"\xfd7zXZ\x00garbage").generated is None


class TestSignals:
    def test_migrated_candidates_are_not_signals(self, found):
        signals, _ = found
        assert "ccache" not in signals
        assert "dz6" not in signals

    def test_skipped_candidates_are(self, found):
        """python-pathable had looked ready for 178 days."""
        signals, _ = found
        for name in (
            "python-pathable",
            "libnet-dns-sec-perl",
            "rust-chic",
            "rust-annotate-snippets",
            "rust-libcst",
        ):
            assert signals[name].payload["reasons"] == [Reason.BREAKS_INSTALLS]

    def test_blocked_uploads_are_unchanged(self, found):
        signals, _ = found
        assert (
            Reason.BREAKS_INSTALLS
            not in signals["qtcontacts-sqlite"].payload["reasons"]
        )

    def test_the_log_keeps_no_binaries(self, found):
        """What breaks changes each run as other uploads land: detail."""
        signals, _ = found
        payload = signals["python-pathable"].payload
        assert not {"breaks", "arch", "binaries"} & set(payload)

    def test_dated_like_any_other_upload(self, found):
        signals, _ = found
        since = signals["python-pathable"].payload["in_proposed_since"]
        # 178.65 days before the run.
        assert since == "2026-06-01"


class TestDetail:
    def test_names_the_run_log(self, found):
        _, detail = found
        assert detail["output_generated"] == "2026-11-26T19:57:20+00:00"

    def test_broken_binaries_grouped_by_source(self, found):
        _, detail = found
        [attempt] = detail["packages"]["python-pathable"]["breaks"]
        assert attempt["item"] == "python-pathable"
        assert attempt["arch"] == "amd64"
        assert attempt["broken"] == [
            {
                "source": "python-jsonschema-path",
                "binaries": ["python3-jsonschema-path"],
            },
            {"source": "python-openapi-core", "binaries": ["python3-openapi-core"]},
            {
                "source": "python-openapi-spec-validator",
                "binaries": ["python3-openapi-spec-validator"],
            },
        ]

    def test_an_upload_breaking_its_own_binaries(self, found):
        """The perl rebuilds on s390x: the new binary itself would not
        install."""
        _, detail = found
        [attempt] = detail["packages"]["libnet-dns-sec-perl"]["breaks"]
        assert attempt["broken"] == [
            {"source": "libnet-dns-sec-perl", "binaries": ["libnet-dns-sec-perl"]}
        ]

    def test_many_binaries_few_sources(self, found):
        _, detail = found
        [attempt] = detail["packages"]["rust-annotate-snippets"]["breaks"]
        binaries = sum(len(g["binaries"]) for g in attempt["broken"])
        assert binaries > 800
        assert len(attempt["broken"]) < binaries
        assert all(g["source"] for g in attempt["broken"])

    def test_without_indexes_the_binaries_stay_ungrouped(self):
        _, detail = run(Britney(indexes=False))
        [attempt] = detail["packages"]["python-pathable"]["breaks"]
        assert attempt["broken"] == [
            {
                "source": None,
                "binaries": [
                    "python3-jsonschema-path",
                    "python3-openapi-core",
                    "python3-openapi-spec-validator",
                ],
            }
        ]

    def test_reads_only_the_broken_architectures(self):
        britney = Britney()
        run(britney)
        indexes = {
            u.split("/binary-")[1].split("/")[0]
            for u in britney.urls
            if "/binary-" in u
        }
        # amd64v3 for the old library kmc, which iva and shovill need.
        assert indexes == {"amd64", "amd64v3", "s390x"}


class TestWithoutTheRunLog:
    """Candidates are kept, claiming nothing: dropping them would log a
    resolution britney never made."""

    @pytest.mark.parametrize(
        "output",
        [OSError("503"), OUTPUT.replace(b"19:57:20", b"23:57:20")],
        ids=["unreachable", "another run"],
    )
    def test_every_candidate_will_attempt(self, output):
        signals, detail = run(Britney(output=output))
        for name in ("ccache", "dz6", "python-pathable", "rust-libcst"):
            assert signals[name].payload["reasons"] == [Reason.WILL_ATTEMPT]
        assert detail["output_generated"] is None
        assert "breaks" not in detail["packages"]["python-pathable"]

    def test_the_excuses_still_count(self):
        signals, _ = run(Britney(output=OSError("503")))
        assert "qtcontacts-sqlite" in signals

    def test_no_index_is_read(self):
        britney = Britney(output=OSError("503"))
        run(britney)
        assert not [u for u in britney.urls if "/binary-" in u]


def test_a_candidate_not_in_the_log_will_attempt():
    """Tried in no run cairn can see: say no more than britney's excuses."""
    text = OUTPUT.replace(b"trying: python-pathable", b"trying: something-else")
    text = text.replace(b"skipped: python-pathable", b"skipped: something-else")
    signals, _ = run(Britney(output=text))
    assert signals["python-pathable"].payload["reasons"] == [Reason.WILL_ATTEMPT]


def test_the_run_is_kept_for_the_update_output_page(found):
    _, detail = found
    run = detail["run"]
    assert run["considered"] == 921
    assert run["before"]["amd64"] == 12 and run["after"]["amd64"] == 9
    assert [a["item"] for a in run["accepted"]] == ["ccache", "dz6/armhf"]
    ccache = run["accepted"][0]
    assert ccache["migrated"]
    assert (ccache["old_version"], ccache["new_version"]) == ("4.13.6-1", "4.14.3-1")
    [kmc] = [o for o in run["old_libraries"] if o["binary"] == "kmc"]
    assert kmc["arches"] == ["amd64v3"]
    assert kmc["breaks"][0]["broken"][0]["binaries"] == ["iva", "shovill"]


def test_tries_count_every_attempt(output):
    """python-pathable was tried again after other items were accepted."""
    assert output.breaks["python-pathable"].tries == 2
    assert output.breaks["rust-chic"].tries == 2  # once in the group


def test_the_run_names_what_it_skipped(found):
    """The page checks this against the log before showing the run."""
    _, detail = found
    assert detail["run"]["skipped"] == [
        "libnet-dns-sec-perl",
        "python-pathable",
        "rust-annotate-snippets",
        "rust-chic",
        "rust-libcst",
    ]
