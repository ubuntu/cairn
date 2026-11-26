from __future__ import annotations

import lzma
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from cairn.ingest import registry
from cairn.ingest.base import DEVELOPMENT_KINDS, Kind, Signal
from cairn.ingest.migration import (
    EXCUSES_URL,
    MigrationIngester,
    Reason,
    StaleVerdict,
    WrongSeries,
    parse_document,
    referenced_packages,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "update_excuses.yaml").read_bytes()
GENERATED = datetime(2026, 10, 1, 7, 18, 41, 241683, tzinfo=UTC)
SOON_AFTER = GENERATED + timedelta(hours=1)


class FixtureFetcher:
    def __init__(self, body: bytes = FIXTURE):
        self.body = body
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        return lzma.compress(self.body)


def ingester(body: bytes = FIXTURE, *, series="stonking", now=SOON_AFTER):
    return MigrationIngester(FixtureFetcher(body), series=series, now=lambda: now)


@pytest.fixture(scope="module")
def found() -> dict[str, Signal]:
    return {s.source_package: s for s in ingester().run()}


@pytest.fixture(scope="module")
def detailed() -> dict[str, dict]:
    """Each package's detail: what the log does not keep."""
    ing = ingester()
    ing.run()
    return ing.detail()["packages"]


def document(*entries, generated="2026-10-01 07:18:41") -> bytes:
    return yaml.safe_dump(
        {"generated-date": generated, "sources": list(entries)}
    ).encode()


def entry(name="x", **overrides):
    base = {
        "source": name,
        "item-name": name,
        "old-version": "1.0-1",
        "new-version": "1.0-2",
        "is-candidate": False,
        "migration-policy-verdict": "REJECTED_PERMANENTLY",
        "reason": [],
        "policy_info": {
            "age": {"current-age": 2.5, "verdict": "PASS"},
            "autopkgtest": {"verdict": "PASS"},
            "block": {"verdict": "PASS"},
            "depends": {"verdict": "PASS"},
        },
    }
    return base | overrides


class TestFetch:
    def test_reads_britneys_compressed_output(self):
        ing = ingester()
        ing.run()
        assert ing.fetcher.urls == [EXCUSES_URL]
        assert EXCUSES_URL.endswith(".yaml.xz")

    def test_reads_the_generation_time_as_utc(self):
        assert parse_document(FIXTURE).generated == GENERATED


class TestWhichEntriesBecomeSignals:
    def test_only_blocked_uploads(self, found):
        assert "dhcpcd" not in found  # a candidate: about to migrate
        assert len(found) == 9

    def test_emits_the_migration_kind(self, found):
        assert {s.kind for s in found.values()} == {Kind.MIGRATION_BLOCKED}

    def test_is_a_development_kind(self):
        """Britney only covers the open series; a release rolls it over."""
        assert Kind.MIGRATION_BLOCKED in DEVELOPMENT_KINDS

    def test_records_the_series(self, found):
        assert {s.series for s in found.values()} == {"stonking"}

    def test_links_to_the_package_on_launchpad(self, found):
        """Launchpad, the system of record; not britney's page, which cairn
        replaces."""
        assert found["shadow"].url == "https://launchpad.net/ubuntu/+source/shadow"

    def test_an_architecture_item_adds_no_second_signal(self):
        """The upload proper speaks for the source; an architecture item
        becomes the signal only when it is all britney holds."""
        body = document(entry("x"), entry("x", **{"item-name": "x/riscv64"}))
        [signal] = ingester(body).run()
        assert signal.source_package == "x"
        assert "architectures" not in signal.payload

    def test_removal_items_are_not_uploads(self):
        body = document(entry("x", **{"item-name": "-x"}))
        assert ingester(body).run() == []


class TestPayload:
    def test_versions(self, found):
        payload = found["shadow"].payload
        assert payload["old_version"] == "1:4.19.3-2ubuntu1"
        assert payload["new_version"] == "1:4.19.3-2ubuntu2"

    def test_a_new_package_has_no_old_version(self, found):
        assert found["toil"].payload["old_version"] == "-"

    def test_main_is_named(self, found):
        """Britney leaves the component out for main."""
        assert found["shadow"].payload["component"] == "main"
        assert found["toil"].payload["component"] == "universe"

    def test_a_regression_names_the_test_and_its_architectures(self, found, detailed):
        assert detailed["libssh2"]["tests"] == [
            {
                "test": "curl",
                "version": "8.20.0-2ubuntu11",
                "results": {"s390x": "regression"},
                # The failed run's id, which the log's URL ends in.
                "logs": {"s390x": "20261001_031800_3e724@"},
            }
        ]
        assert Reason.REGRESSION in found["libssh2"].payload["reasons"]

    def test_the_log_keeps_only_which_tests_fail(self, found):
        """Results per architecture and log ids change run to run and were
        most of the log (see the module docstring)."""
        payload = found["libssh2"].payload
        assert payload["failing_tests"] == ["curl"]
        assert "tests" not in payload
        assert "britney_reasons" not in payload

    def test_a_running_test_is_not_a_failing_one(self, found):
        payload = found["golang-github-charlievieth-strcase"].payload
        assert payload["failing_tests"] == []

    def test_detail_names_the_upload_it_describes(self, found, detailed):
        """The build uses it only for that upload."""
        assert (
            detailed["libssh2"]["new_version"]
            == (found["libssh2"].payload["new_version"])
        )

    def test_detail_covers_every_signal_and_nothing_else(self, found, detailed):
        assert set(detailed) == set(found)

    def test_detail_is_dated_by_britney(self):
        ing = ingester()
        ing.run()
        assert ing.detail()["generated"] == GENERATED.isoformat()
        assert ing.detail()["series"] == "stonking"

    def test_no_detail_before_a_run(self):
        assert ingester().detail() is None

    def test_a_running_test_keeps_no_log(self, every_excuse_detail):
        """Its "log" is autopkgtest's generic /running page."""
        [running] = every_excuse_detail["hwloc"]["tests"][:1]
        assert set(running["results"].values()) == {"running"}
        assert "logs" not in running

    def test_missing_builds(self, found):
        assert found["ceph"].payload["missing_builds"] == ["riscv64"]
        assert Reason.MISSING_BUILD in found["ceph"].payload["reasons"]

    def test_running_tests_are_not_a_regression(self, found):
        reasons = found["golang-github-charlievieth-strcase"].payload["reasons"]
        assert reasons == [Reason.TESTS_RUNNING]

    def test_a_freeze_needs_approval(self, found):
        payload = found["shadow"].payload
        assert Reason.NEEDS_APPROVAL in payload["reasons"]
        assert payload["hints"] == ["freeze"]

    def test_waiting_on_another_item(self, found):
        payload = found["golang-k8s-component-helpers"].payload
        assert payload["reasons"] == [Reason.WAITING]
        assert "golang-k8s-apimachinery" in payload["waits_for"]

    def test_uninstallable(self, found):
        assert Reason.UNINSTALLABLE in found["libreoffice"].payload["reasons"]

    def test_no_binaries(self, found):
        assert Reason.NO_BINARIES in found["toil"].payload["reasons"]

    def test_investigation_bugs(self, found):
        """A bug tagged update-excuse says someone is looking at it."""
        assert found["gettext"].payload["bugs"] == [2167365]

    def test_keeps_britneys_own_wording(self, detailed):
        assert detailed["ceph"]["britney_reasons"] == [
            "autopkgtest",
            "block",
            "missingbuild",
        ]

    def test_every_signal_has_a_reason(self, found):
        assert all(s.payload["reasons"] for s in found.values())

    def test_dates_entry_into_proposed_from_britneys_age(self, found):
        # shadow's age was 7.49 days at 2026-10-01 07:18.
        assert found["shadow"].payload["in_proposed_since"] == "2026-09-23"

    def test_payload_does_not_change_between_runs(self):
        """Britney's age grows every run; storing it would log an update for
        every signal every run."""
        later = document(
            entry(policy_info=entry()["policy_info"] | {"age": {"current-age": 3.5}}),
            generated="2026-10-02 07:18:41",
        )
        earlier = document(entry(), generated="2026-10-01 07:18:41")
        now = datetime(2026, 10, 2, 8, tzinfo=UTC)
        assert (
            ingester(earlier, now=now).run()[0].payload
            == ingester(later, now=now).run()[0].payload
        )


class TestRefusals:
    def test_a_different_series_is_an_error(self):
        with pytest.raises(WrongSeries):
            ingester(series="noble").run()

    def test_a_stale_verdict_is_an_error(self):
        """Britney not running is a pipeline failure, not an unchanged archive."""
        with pytest.raises(StaleVerdict):
            ingester(now=GENERATED + timedelta(days=3)).run()

    def test_an_undated_verdict_is_an_error(self):
        """Without britney's date, freshness cannot be shown: refuse it,
        rather than accept a cached or reshaped document indefinitely."""
        body = yaml.safe_dump({"sources": [entry()]}).encode()
        with pytest.raises(StaleVerdict, match="no generated-date"):
            ingester(body).run()

    def test_an_undated_verdict_fails_the_source_not_the_run(self):
        """It is reported as a failed source, so the last signals stand."""
        from cairn.core.runner import run

        body = yaml.safe_dump({"sources": [entry()]}).encode()
        report = run([ingester(body)], {}, now=SOON_AFTER)
        [outcome] = report.outcomes
        assert not outcome.ok
        assert "generated-date" in outcome.error
        assert report.events == ()


class TestReferencedPackages:
    def test_names_the_packages_whose_tests_regressed(self, found):
        assert referenced_packages(found["libssh2"]) == {"curl"}

    def test_names_the_packages_an_upload_waits_for(self):
        """Their owners hold the waiting upload back, even when the awaited
        package is not stuck itself (britney's blocked-by)."""
        sig = Signal(
            kind=Kind.MIGRATION_BLOCKED,
            source_package="app",
            payload={"waits_for": ["lib", "app"]},
        )
        assert referenced_packages(sig) == {"lib"}

    def test_other_kinds_name_nothing(self):
        assert (
            referenced_packages(Signal(kind=Kind.NEEDS_MERGE, source_package="x"))
            == set()
        )


def test_is_registered():
    ing = registry.build("migration", FixtureFetcher(), "stonking")
    assert isinstance(ing, MigrationIngester)
    assert ing.series == "stonking"


class TestFailedWhileBaselineReruns:
    """RUNNING-REFERENCE: failing now, baseline being re-run. Britney shows
    it red with a retry link, so it is a failure, not a running test."""

    def entry_with(self, result):
        """The payload, with the detail's tests under "tests"."""
        policy = entry()["policy_info"] | {
            "autopkgtest": {
                "verdict": "REJECTED_TEMPORARILY",
                "hipsparselt/unknown": {"s390x": [result, "log", "hist", None, None]},
            }
        }
        ing = ingester(document(entry(policy_info=policy)))
        [signal] = ing.run()
        return signal.payload | {"tests": ing.detail()["packages"]["x"]["tests"]}

    def test_is_a_failed_test_with_its_own_status(self):
        payload = self.entry_with("RUNNING-REFERENCE")
        assert payload["tests"][0]["results"] == {"s390x": "reference_running"}
        assert payload["reasons"] == [Reason.REGRESSION]
        assert payload["failing_tests"] == ["hipsparselt"]

    def test_a_running_test_is_listed_as_running(self):
        """It holds the upload until it finishes, so the reader needs it."""
        payload = self.entry_with("RUNNING")
        assert payload["tests"][0]["results"] == {"s390x": "running"}
        assert payload["reasons"] == [Reason.TESTS_RUNNING]
        assert payload["failing_tests"] == []

    @pytest.mark.parametrize(
        "result", ["PASS", "NEUTRAL", "ALWAYSFAIL", "RUNNING-ALWAYSFAIL", "OLD_PASS"]
    )
    def test_results_britney_does_not_count_are_left_out(self, result):
        """RUNNING-ALWAYSFAIL: britney says it will not be a regression."""
        payload = self.entry_with(result)
        assert payload["tests"] == []
        assert Reason.TESTS_RUNNING not in payload["reasons"]

    def test_a_reverse_dependency_running_on_the_fixture(self, found, detailed):
        """A real entry: golang-github-charlievieth-strcase waits on its own
        test, still running on one arch."""
        name = "golang-github-charlievieth-strcase"
        assert found[name].payload["reasons"] == [Reason.TESTS_RUNNING]
        tests = detailed[name]["tests"]
        assert any("running" in t["results"].values() for t in tests)


def test_reads_the_payload_shape_written_before_1_oct():
    """The log keeps every shape; data written as "regressions" must still
    show its tests rather than an empty list."""
    from cairn.ingest.migration import payload_tests

    legacy = {"regressions": [{"test": "curl", "version": "8", "arches": ["s390x"]}]}
    assert payload_tests(legacy) == [
        {"test": "curl", "version": "8", "results": {"s390x": "regression"}}
    ]
    legacy_signal = Signal(
        kind=Kind.MIGRATION_BLOCKED, source_package="libssh2", payload=legacy
    )
    assert referenced_packages(legacy_signal) == {"curl"}


@pytest.mark.parametrize("policy", ["block", "depends", "block-bugs", "rc-bugs"])
def test_a_hinted_pass_is_not_a_rejection(policy):
    """Copilot review on #13: PASS_HINTED means a hint made the policy pass."""
    policies = entry()["policy_info"] | {policy: {"verdict": "PASS_HINTED"}}
    payload = ingester(document(entry(policy_info=policies))).run()[0].payload
    assert payload["reasons"] == [Reason.OTHER]


def test_any_rejected_verdict_still_counts():
    policies = entry()["policy_info"] | {
        "block": {"verdict": "REJECTED_NEEDS_APPROVAL"},
        "depends": {"verdict": "REJECTED_PERMANENTLY"},
    }
    payload = ingester(document(entry(policy_info=policies))).run()[0].payload
    assert payload["reasons"] == [Reason.NEEDS_APPROVAL, Reason.UNINSTALLABLE]


ISSUE_18 = (
    Path(__file__).parent / "fixtures" / "update_excuses_2026-11-20.yaml"
).read_bytes()
ISSUE_18_NOW = datetime(2026, 11, 20, 7, tzinfo=UTC)


@pytest.fixture(scope="module")
def every_excuse() -> dict[str, Signal]:
    found = ingester(ISSUE_18, series="tenacious", now=ISSUE_18_NOW).run()
    return {s.source_package: s for s in found}


@pytest.fixture(scope="module")
def every_excuse_detail() -> dict[str, dict]:
    ing = ingester(ISSUE_18, series="tenacious", now=ISSUE_18_NOW)
    ing.run()
    return ing.detail()["packages"]


class TestEveryExcuseIsCarried:
    """Issue #18: every excuse britney gives in Ubuntu's configuration
    reaches the payload, checked against britney2-ubuntu's source and the
    20 Nov 2026 report. Real entries, trimmed."""

    def test_a_block_proposed_bug_is_named(self, every_excuse):
        p = every_excuse["netgen-lvs"].payload
        assert Reason.BLOCK_BUG in p["reasons"]
        assert p["block_bugs"] == [2170615]
        # Not an update-excuse bug: those say someone looked, and hold nothing.
        assert p["bugs"] == []

    def test_update_excuse_bugs_stay_apart_from_blocking_ones(self, every_excuse):
        p = every_excuse["hwloc"].payload
        assert p["bugs"] == [2072750]
        assert "block_bugs" not in p

    def test_a_main_universe_mismatch_says_what_cannot_depend_on_what(
        self, every_excuse
    ):
        p = every_excuse["dotnet10"].payload
        assert p["reasons"] == [Reason.UNINSTALLABLE]
        assert {d["kind"] for d in p["depends_issues"]} == {"component"}
        # One line per message, the architectures beside it: britney
        # repeats each line per architecture.
        assert {
            "kind": "component",
            "text": "dotnet-hostfxr-10.0 in main cannot depend on dotnet-host-11.0 "
            "in universe",
            "arches": ["amd64", "amd64v3"],
        } in p["depends_issues"]

    def test_unsatisfiable_and_impossible_dependencies_are_kept(self, every_excuse):
        kinds = {
            name: {d["kind"] for d in every_excuse[name].payload["depends_issues"]}
            for name in ("rust-forgejo-api", "libgweather")
        }
        assert kinds == {
            "rust-forgejo-api": {"unsatisfiable"},
            "libgweather": {"component", "impossible"},
        }
        texts = [
            d["text"] for d in every_excuse["libgweather"].payload["depends_issues"]
        ]
        assert all("<" not in t for t in texts)  # britney's markup stripped

    def test_old_binaries_have_a_reason_of_their_own(self, every_excuse):
        """Britney's "cruft" read as Other before."""
        p = every_excuse["ifcopenshell"].payload
        assert p["reasons"] == [Reason.OLD_BINARIES]
        assert p["old_binaries"] == {"0.8.1+dfsg1-10build1": ["libifcopenshell0.8.1"]}

    def test_a_source_held_only_per_architecture_is_not_dropped(self, every_excuse):
        p = every_excuse["imagewmark"].payload
        assert p["architectures"] == ["arm64", "armhf", "ppc64el"]
        assert p["reasons"] == [Reason.WAITING]
        assert p["waits_for"] == ["openimageio"]

    def test_usually_empty_fields_are_written_only_when_set(self, every_excuse):
        """So adding them updates only the signals they concern."""
        p = every_excuse["hwloc"].payload
        for key in ("block_bugs", "depends_issues", "old_binaries", "architectures"):
            assert key not in p, key


def test_a_kernel_waits_for_its_meta():
    """linux-meta-not-ready, which read as Other: no real entry has been
    captured, so this one follows britney's wording in policy.py."""
    body = document(
        entry(
            "linux-oem",
            reason=["linux-meta-not-ready"],
            excuses=['<a href="#linux-meta-oem">linux-meta-oem</a> is not a candidate'],
        )
    )
    [signal] = ingester(body).run()
    assert Reason.WAITING in signal.payload["reasons"]
    assert signal.payload["waits_for"] == ["linux-meta-oem"]


class TestArchitectureItemsMerge:
    """Review on #24: a source held only per architecture merges each field
    by its shape. A generic merge failed the whole ingest on two
    architectures' depends issues."""

    def item(self, arch, **overrides):
        return entry(
            "p",
            **{
                "item-name": f"p/{arch}",
                "reason": ["depends", "cruft"],
                "policy_info": {"depends": {"verdict": "REJECTED_PERMANENTLY"}},
                "excuses": [
                    f"libp/{arch} has unsatisfiable dependency",
                    f"old binaries left on {arch}: libp-{arch} (from 1.0-1)",
                ],
                "old-binaries": {"1.0-1": [f"libp-{arch}"]},
            }
            | overrides,
        )

    def test_two_architectures_with_depends_issues_do_not_fail_the_ingest(self):
        [signal] = ingester(document(self.item("arm64"), self.item("armhf"))).run()
        assert signal.payload["depends_issues"] == [
            {
                "kind": "unsatisfiable",
                "text": "libp has unsatisfiable dependency",
                "arches": ["arm64", "armhf"],
            }
        ]
        assert signal.payload["architectures"] == ["arm64", "armhf"]

    def test_every_architectures_old_binaries_are_kept(self):
        [signal] = ingester(document(self.item("arm64"), self.item("armhf"))).run()
        assert signal.payload["old_binaries"] == {"1.0-1": ["libp-arm64", "libp-armhf"]}

    def test_every_architectures_tests_are_kept(self):
        def failing(arch):
            return {
                "autopkgtest": {
                    "verdict": "REJECTED_PERMANENTLY",
                    "q/1": {arch: ["REGRESSION", f"x/{arch}/run{arch}/log.gz"]},
                }
            }

        ing = ingester(
            document(
                self.item("arm64", policy_info=failing("arm64"), reason=["x"]),
                self.item("armhf", policy_info=failing("armhf"), reason=["y"]),
            )
        )
        [signal] = ing.run()
        assert signal.payload["failing_tests"] == ["q"]
        found = ing.detail()["packages"]["p"]
        assert found["tests"] == [
            {
                "test": "q",
                "version": "1",
                "results": {"arm64": "regression", "armhf": "regression"},
                "logs": {"arm64": "runarm64", "armhf": "runarmhf"},
            }
        ]
        assert found["britney_reasons"] == ["x", "y"]


def test_binaries_waiting_for_a_build_are_not_old_binaries():
    """Review on #24: britney's old-binaries field mixes cruft with binaries
    waiting for a missing build. eclib, 20 Nov 2026, trimmed."""
    body = document(
        entry(
            "eclib",
            reason=["cruft", "missingbuild"],
            **{
                "old-binaries": {"20250627-1": ["eclib-tools", "libec-dev", "libec14"]},
                "missing-builds": {"on-architectures": ["amd64"]},
                "excuses": [
                    "old binaries left on riscv64: libec14 (from 20250627-1)",
                    "missing build on amd64: eclib-tools, libec-dev, libec14 "
                    "(from 20250627-1)",
                ],
            },
        )
    )
    [signal] = ingester(body).run()
    assert signal.payload["old_binaries"] == {"20250627-1": ["libec14"]}
    assert {"old_binaries", "missing_build"} <= set(signal.payload["reasons"])


def test_a_test_history_url_never_names_a_series_alone():
    """Review on #24: /packages/c/curl/<series> reads as package c."""
    from cairn.build.site import autopkgtest_url

    assert autopkgtest_url("curl", "tenacious") == (
        "https://autopkgtest.ubuntu.com/packages/c/curl"
    )
    assert autopkgtest_url("curl", "tenacious", "amd64").endswith(
        "/packages/c/curl/tenacious/amd64"
    )
