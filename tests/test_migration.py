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

    def test_architecture_specific_items_are_skipped(self):
        body = document(entry("x"), entry("x", **{"item-name": "x/riscv64"}))
        assert [s.source_package for s in ingester(body).run()] == ["x"]


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

    def test_a_regression_names_the_test_and_its_architectures(self, found):
        assert found["libssh2"].payload["tests"] == [
            {
                "test": "curl",
                "version": "8.20.0-2ubuntu11",
                "results": {"s390x": "regression"},
            }
        ]
        assert Reason.REGRESSION in found["libssh2"].payload["reasons"]

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

    def test_keeps_britneys_own_wording(self, found):
        assert found["ceph"].payload["britney_reasons"] == [
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
        policy = entry()["policy_info"] | {
            "autopkgtest": {
                "verdict": "REJECTED_TEMPORARILY",
                "hipsparselt/unknown": {"s390x": [result, "log", "hist", None, None]},
            }
        }
        return ingester(document(entry(policy_info=policy))).run()[0].payload

    def test_is_a_failed_test_with_its_own_status(self):
        payload = self.entry_with("RUNNING-REFERENCE")
        assert payload["tests"][0]["results"] == {"s390x": "reference_running"}
        assert payload["reasons"] == [Reason.REGRESSION]

    def test_a_running_test_is_listed_as_running(self):
        """It holds the upload until it finishes, so the reader needs it."""
        payload = self.entry_with("RUNNING")
        assert payload["tests"][0]["results"] == {"s390x": "running"}
        assert payload["reasons"] == [Reason.TESTS_RUNNING]

    @pytest.mark.parametrize(
        "result", ["PASS", "NEUTRAL", "ALWAYSFAIL", "RUNNING-ALWAYSFAIL", "OLD_PASS"]
    )
    def test_results_britney_does_not_count_are_left_out(self, result):
        """RUNNING-ALWAYSFAIL: britney says it will not be a regression."""
        payload = self.entry_with(result)
        assert payload["tests"] == []
        assert Reason.TESTS_RUNNING not in payload["reasons"]

    def test_a_reverse_dependency_running_on_the_fixture(self, found):
        """A real entry: golang-github-charlievieth-strcase waits on its own
        test, still running on one arch."""
        payload = found["golang-github-charlievieth-strcase"].payload
        assert payload["reasons"] == [Reason.TESTS_RUNNING]
        assert any("running" in t["results"].values() for t in payload["tests"])


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
