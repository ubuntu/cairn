"""Uploads stuck in -proposed, and why, from britney's own output.

Not primary data, and consumed anyway under AGENTS.md section 4's
decision-engine exception: a package migrates *because britney said so*, so
its verdict is the fact rather than a description of one. The policy results
that produced the verdict (autopkgtest per architecture, missing builds,
freeze blocks, items it waits for) are read from the same file. They are how
britney decided, not a report about the decision, and recomputing them from
the autopkgtest API and Launchpad would reproduce britney's inputs without its
judgement. Those primary sources can add detail later; they cannot replace
this.

Measured on 1 Oct 2026, 07:18 UTC run:
  update_excuses.yaml.xz  0.7 MB, application/x-xz despite the name
  entries                 606: 587 blocked, 19 candidates
  primary reasons         missing build 257, regression 137, no binaries 92,
                          needs approval 63, tests running 56, uninstallable
                          39, waiting on another item 21
  reasons per package     one 516, two 62, three 9

Only blocked uploads become signals. A candidate is about to migrate, which is
the opposite of needing attention.

Britney's floating-point age is not stored: it changes every run, and storing
it would append one meaningless update per signal per run. The date it implies
is stored instead, which is constant for one upload.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import yaml

from cairn.ingest.archive import decompress
from cairn.ingest.base import Fetcher, Ingester, Kind, Signal

log = logging.getLogger(__name__)

PROPOSED_MIGRATION = "https://ubuntu-archive-team.ubuntu.com/proposed-migration"
EXCUSES_URL = f"{PROPOSED_MIGRATION}/update_excuses.yaml.xz"

# Britney normally runs every two to three hours. Two days without a new
# verdict means britney is not running, and its last answer is no longer
# the state of the archive: report that as a failed source, so the board says
# the data is stale rather than presenting it as current.
MAX_VERDICT_AGE = timedelta(days=2)

# Each run's autopkgtest links name the series they tested against.
_SERIES_IN_URL = re.compile(r"/results/autopkgtest-([a-z]+)/")


class Reason(StrEnum):
    """Why an upload is held, in cairn's vocabulary.

    Several of britney's fields can say the same thing, and one of its
    reasons ("autopkgtest") covers both a regression and a test that has not
    finished yet, which are very different jobs for whoever reads the board.
    """

    REGRESSION = "regression"
    TESTS_RUNNING = "tests_running"
    MISSING_BUILD = "missing_build"
    NO_BINARIES = "no_binaries"
    NEEDS_APPROVAL = "needs_approval"
    UNINSTALLABLE = "uninstallable"
    WAITING = "waiting"
    BLOCK_BUG = "block_bug"
    RC_BUG = "rc_bug"
    OTHER = "other"


_WAITING_VERDICTS = frozenset(
    {"REJECTED_BLOCKED_BY_ANOTHER_ITEM", "REJECTED_WAITING_FOR_ANOTHER_ITEM"}
)


class WrongSeries(RuntimeError):
    """Britney's output covers a different series than the one requested."""


class StaleVerdict(RuntimeError):
    """Britney has not produced a verdict recently enough to trust."""


@dataclass(frozen=True, slots=True)
class Excuses:
    generated: datetime | None
    entries: list[dict[str, Any]]


def _loader() -> type:
    # Measured on the 13.4 MB document: 1.3 s with libyaml, 6.6 s without.
    # Either is fine; prefer the faster. Both are safe loaders.
    return getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def parse_document(raw: bytes) -> Excuses:
    # _loader() only ever returns a safe loader.
    doc = yaml.load(raw, Loader=_loader()) or {}
    generated = doc.get("generated-date")
    if isinstance(generated, str):
        generated = datetime.fromisoformat(generated)
    if isinstance(generated, datetime) and generated.tzinfo is None:
        # Britney writes UTC without saying so.
        generated = generated.replace(tzinfo=UTC)
    return Excuses(generated=generated, entries=list(doc.get("sources") or []))


def _results(entry: Mapping[str, Any]) -> Iterable[tuple[str, str, str]]:
    """(test, arch, result) for every autopkgtest britney considered."""
    tests = (entry.get("policy_info") or {}).get("autopkgtest") or {}
    for test, arches in tests.items():
        if not isinstance(arches, dict):
            continue  # the policy's own verdict
        for arch, result in arches.items():
            if isinstance(result, list) and result:
                yield test, arch, str(result[0])


# Britney's per-architecture results that can hold an upload back, in
# cairn's words. Measured on 1 Oct 2026 (blocked uploads only): 724
# REGRESSION, 4 RUNNING-REFERENCE, 1019 RUNNING.
#   REGRESSION         passed before, fails now.
#   RUNNING-REFERENCE  fails now; britney is re-running the baseline to see
#                      whether it failed before too. Red with a retry link
#                      on britney's page, so a failure here as well.
#   RUNNING            not finished. Holds the upload until it does.
# Everything else is left out: passes, "not a regression", and tests still
# running that already always failed (RUNNING-ALWAYSFAIL), which britney
# says will not be counted. Listing those would bury the few that matter.
TEST_RESULTS = {
    "REGRESSION": "regression",
    "RUNNING-REFERENCE": "reference_running",
    "RUNNING": "running",
}
FAILED = frozenset({"regression", "reference_running"})


def tests(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tests holding the upload, one record per test package, status per arch.

    Britney keys results by "package/version". The version is sometimes the
    literal "unknown", and is kept as given. Most of these are reverse
    dependencies' tests, triggered by this upload.
    """
    found: dict[tuple[str, str], dict[str, str]] = {}
    for test, arch, result in _results(entry):
        status = TEST_RESULTS.get(result)
        if status is None:
            continue
        name, _, version = test.partition("/")
        found.setdefault((name, version), {})[arch] = status
    return [
        {"test": name, "version": version, "results": dict(sorted(results.items()))}
        for (name, version), results in sorted(found.items())
    ]


def payload_tests(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The tests in a stored payload, whichever shape it was written in.

    The log is append-only, so every shape ever written is read forever.
    Payloads before 1 Oct 2026 10:00 UTC stored only regressions, as
    "regressions": [{"test", "version", "arches"}].
    """
    if "tests" in payload:
        return list(payload.get("tests") or ())
    return [
        {
            "test": r.get("test", ""),
            "version": r.get("version", ""),
            "results": dict.fromkeys(r.get("arches") or (), "regression"),
        }
        for r in payload.get("regressions") or ()
    ]


def reasons(entry: Mapping[str, Any]) -> list[str]:
    policy = entry.get("policy_info") or {}

    def rejected(name: str) -> bool:
        # Britney's verdicts are PASS, PASS_HINTED (a hint made the policy
        # pass) or REJECTED_*. Only the last holds an upload back.
        verdict = (policy.get(name) or {}).get("verdict")
        return str(verdict or "").startswith("REJECTED")

    found: set[Reason] = set()
    results = [result for _, _, result in _results(entry)]
    statuses = {TEST_RESULTS.get(result) for result in results}
    if statuses & FAILED:
        found.add(Reason.REGRESSION)
    if "running" in statuses:
        found.add(Reason.TESTS_RUNNING)
    if (entry.get("missing-builds") or {}).get("on-architectures"):
        found.add(Reason.MISSING_BUILD)
    if "no-binaries" in (entry.get("reason") or []):
        found.add(Reason.NO_BINARIES)
    if rejected("block"):
        found.add(Reason.NEEDS_APPROVAL)
    if rejected("depends"):
        found.add(Reason.UNINSTALLABLE)
    if entry.get("migration-policy-verdict") in _WAITING_VERDICTS or (
        entry.get("dependencies") or {}
    ).get("blocked-by"):
        found.add(Reason.WAITING)
    if rejected("block-bugs"):
        found.add(Reason.BLOCK_BUG)
    if rejected("rc-bugs"):
        found.add(Reason.RC_BUG)
    if not found:
        found.add(Reason.OTHER)
    return sorted(str(r) for r in found)


def in_proposed_since(
    entry: Mapping[str, Any], generated: datetime | None
) -> str | None:
    """The day this upload entered -proposed, by britney's own clock.

    Britney's age survives the archive being copied into a new series, which
    Launchpad's publication date does not.
    """
    age = ((entry.get("policy_info") or {}).get("age") or {}).get("current-age")
    if generated is None or not isinstance(age, int | float):
        return None
    return (generated - timedelta(days=age)).date().isoformat()


def payload(entry: Mapping[str, Any], generated: datetime | None) -> dict[str, Any]:
    policy = entry.get("policy_info") or {}
    dependencies = entry.get("dependencies") or {}
    bugs = (policy.get("update-excuse") or {}).keys() - {"verdict"}
    return {
        "old_version": entry.get("old-version"),
        "new_version": entry.get("new-version"),
        # Britney leaves out the component for main.
        "component": entry.get("component") or "main",
        "reasons": reasons(entry),
        # Britney's own wording, for display and debugging only.
        "britney_reasons": sorted(entry.get("reason") or []),
        "tests": tests(entry),
        "missing_builds": sorted(
            (entry.get("missing-builds") or {}).get("on-architectures") or []
        ),
        "waits_for": sorted(
            set(dependencies.get("blocked-by") or [])
            | set(dependencies.get("migrate-after") or [])
        ),
        "bugs": sorted(int(bug) for bug in bugs if str(bug).isdigit()),
        "hints": sorted(
            {
                str(hint.get("hint-from"))
                for hint in entry.get("hints") or []
                if hint.get("hint-from")
            }
        ),
        "in_proposed_since": in_proposed_since(entry, generated),
    }


def referenced_packages(signal: Signal) -> set[str]:
    """Packages a migration signal names besides its own.

    Their owners are the people who can fix a regression in their own tests,
    so ownership has to be collected for them too.
    """
    if signal.kind is not Kind.MIGRATION_BLOCKED:
        return set()
    return {
        r["test"]
        for r in payload_tests(signal.payload)
        if r.get("test") and FAILED & set((r.get("results") or {}).values())
    }


def series_tested(entries: Iterable[Mapping[str, Any]]) -> set[str]:
    seen: set[str] = set()
    for entry in entries:
        for arches in (
            (entry.get("policy_info") or {}).get("autopkgtest") or {}
        ).values():
            if not isinstance(arches, dict):
                continue
            for result in arches.values():
                if isinstance(result, list) and len(result) > 1 and result[1]:
                    match = _SERIES_IN_URL.search(str(result[1]))
                    if match:
                        seen.add(match.group(1))
    return seen


class MigrationIngester(Ingester[Excuses]):
    name = "migration"

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        series: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.fetcher = fetcher
        self.series = series
        self._now = now

    def fetch(self) -> Excuses:
        return parse_document(decompress(self.fetcher.get(EXCUSES_URL), EXCUSES_URL))

    def parse(self, raw: Excuses) -> list[Signal]:
        # Britney only publishes the development series. Labelling its
        # verdicts with any other series would be silently wrong.
        tested = series_tested(raw.entries)
        if tested and self.series not in tested:
            raise WrongSeries(
                f"britney tested {', '.join(sorted(tested))}, not {self.series}"
            )
        # Freshness cannot be established without britney's own date, so an
        # undated document is refused like a stale one: the run records a
        # failed source and the last trusted signals stay as they were.
        if raw.generated is None:
            raise StaleVerdict("britney's output carries no generated-date")
        if self._now() - raw.generated > MAX_VERDICT_AGE:
            raise StaleVerdict(f"britney last ran {raw.generated.isoformat()}")

        signals = []
        skipped = 0
        for entry in raw.entries:
            name = entry.get("source")
            if entry.get("is-candidate") or not name:
                continue
            # Architecture-specific and removal items share a source with the
            # upload proper; one signal per source keeps identity simple.
            if entry.get("item-name") != name:
                skipped += 1
                continue
            signals.append(
                Signal(
                    kind=Kind.MIGRATION_BLOCKED,
                    source_package=name,
                    series=self.series,
                    payload=payload(entry, raw.generated),
                    # Launchpad, the system of record, not britney's page:
                    # cairn replaces that page rather than pointing at it.
                    url=f"https://launchpad.net/ubuntu/+source/{name}",
                )
            )
        if skipped:
            log.info(
                "migration: skipped %d item(s) that are not a whole source", skipped
            )
        return signals
