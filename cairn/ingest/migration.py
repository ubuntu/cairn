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

Every excuse britney can give in Ubuntu's configuration is carried, checked
against britney2-ubuntu's source and the 20 Nov 2026 report (2,791 blocked
items) for issue #18. Most come from structured fields. Three exist only as
britney's text lines and are read from those here, the one place allowed to
branch on britney's wording (AGENTS.md section 5): why a package is
uninstallable (an unsatisfiable dependency, a main/universe mismatch, an
impossible dependency), and which linux-meta a kernel waits for. Fields that
are usually empty are written only when they are not, so adding one appends
an update for the signals it concerns and no others.
"""

from __future__ import annotations

import html
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
    # Binaries the new version no longer builds are still in -proposed
    # (britney's "cruft"): an archive admin has to remove them.
    OLD_BINARIES = "old_binaries"
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


# A finished run's log ends in its run id: .../<series>/<arch>/<prefix>/<test>/
# <run id>/log.gz. The rest of the URL follows from what is stored already,
# so only the id is kept: 22 bytes rather than 123 (measured 20 Nov 2026,
# all 11,830 failed results rebuilt exactly). A test still running points at
# autopkgtest's generic /running page and has no log yet.
_RUN_ID = re.compile(r"/([^/]+)/log\.gz$")


def _results_with_logs(
    entry: Mapping[str, Any],
) -> Iterable[tuple[str, str, str, str | None]]:
    """(test, arch, result, run id of its log) for every result."""
    tests = (entry.get("policy_info") or {}).get("autopkgtest") or {}
    for test, arches in tests.items():
        if not isinstance(arches, dict):
            continue
        for arch, result in arches.items():
            if isinstance(result, list) and result:
                log = str(result[1]) if len(result) > 1 and result[1] else ""
                match = _RUN_ID.search(log)
                yield test, arch, str(result[0]), match.group(1) if match else None


def tests(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tests holding the upload, one record per test package, status per arch.

    Britney keys results by "package/version". The version is sometimes the
    literal "unknown", and is kept as given. Most of these are reverse
    dependencies' tests, triggered by this upload. A failed result also
    keeps its log's run id under "logs", written only when there is one.
    """
    found: dict[tuple[str, str], dict[str, str]] = {}
    logs: dict[tuple[str, str], dict[str, str]] = {}
    for test, arch, result, run in _results_with_logs(entry):
        status = TEST_RESULTS.get(result)
        if status is None:
            continue
        name, _, version = test.partition("/")
        found.setdefault((name, version), {})[arch] = status
        if run and status in FAILED:
            logs.setdefault((name, version), {})[arch] = run
    records = []
    for (name, version), results in sorted(found.items()):
        record: dict[str, Any] = {
            "test": name,
            "version": version,
            "results": dict(sorted(results.items())),
        }
        if logs.get((name, version)):
            record["logs"] = dict(sorted(logs[(name, version)].items()))
        records.append(record)
    return records


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


_TAG = re.compile(r"<[^>]+>")


def excuse_lines(entry: Mapping[str, Any]) -> list[str]:
    """Britney's excuse lines as plain text: links and markup stripped."""
    return [
        html.unescape(_TAG.sub("", str(line))).strip()
        for line in entry.get("excuses") or []
    ]


# Why a package is uninstallable, which the depends policy records only as a
# verdict. Britney's wording, from britney2/policies/policy.py and
# britney2/excuse.py; the kinds match the sections of Ubuntu's "Issues
# preventing migration" guide. Britney repeats each line per architecture,
# so the architecture is taken out of the line and listed beside it: one
# line for libgweather on 20 Nov 2026 instead of seven. Measured that day:
# 81 component lines on 9 packages, 70 unsatisfiable on 16, 67 impossible
# on 8.
_DEPENDS_ISSUES = (
    (
        "component",
        re.compile(
            r"^(?P<bin>\S+)/(?P<arch>[a-z0-9]+)"
            r"(?P<rest> in \w+ cannot depend on \S+ in \w+)$"
        ),
    ),
    (
        "unsatisfiable",
        re.compile(
            r"^(?P<bin>\S+)/(?P<arch>[a-z0-9]+)"
            r"(?P<rest> has unsatisfiable dependency)$"
        ),
    ),
    (
        "impossible",
        re.compile(
            r"^(?P<bin>Impossible .+?: \S+ -> \S+)/(?P<arch>[a-z0-9]+)(?P<rest>)$"
        ),
    ),
)


def depends_issues(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What makes the upload uninstallable: kind, britney's line without
    the architecture, and the architectures it applies to."""
    found: dict[tuple[str, str], set[str]] = {}
    for line in excuse_lines(entry):
        for kind, pattern in _DEPENDS_ISSUES:
            match = pattern.match(line)
            if match:
                text = match.group("bin") + match.group("rest")
                found.setdefault((kind, text), set()).add(match.group("arch"))
                break
    return [
        {"kind": kind, "text": text, "arches": sorted(arches)}
        for (kind, text), arches in found.items()
    ]


# The kernel waits for its linux-meta: "<linux-meta-x> is not a candidate".
_NOT_A_CANDIDATE = re.compile(r"^(\S+) is not a candidate$")


def awaited_meta(entry: Mapping[str, Any]) -> list[str]:
    if "linux-meta-not-ready" not in (entry.get("reason") or []):
        return []
    return sorted(
        {
            m.group(1)
            for line in excuse_lines(entry)
            if (m := _NOT_A_CANDIDATE.match(line))
        }
    )


def block_bugs(entry: Mapping[str, Any]) -> list[int]:
    """Launchpad bugs holding the upload (tag block-proposed), from the
    block-bugs policy. Not the update-excuse bugs, which only say someone
    filed a bug about the upload and hold nothing."""
    info = (entry.get("policy_info") or {}).get("block-bugs") or {}
    return sorted(int(bug) for bug in info if str(bug).isdigit())


# "old binaries left on riscv64: libec14 (from 20250627-1)", several versions
# joined by "; ". From britney2/excusefinder.py.
_OLD_BINARIES = re.compile(r"^old binaries left on [a-z0-9]+: (?P<rest>.+)$")
_FROM_VERSION = re.compile(r"^(?P<names>.+?) \(from (?P<version>[^)]+)\)$")


def old_binaries(entry: Mapping[str, Any]) -> dict[str, list[str]]:
    """Binaries left from an older version, by that version.

    Read from britney's "old binaries left" lines, not its old-binaries
    field: that field also holds binaries waiting for a missing build on
    another architecture, which go away when the build lands and must not
    be offered for removal (review on #24: eclib on 20 Nov 2026 was cruft
    on riscv64 only, with two more binaries waiting for builds elsewhere).
    """
    found: dict[str, set[str]] = {}
    for line in excuse_lines(entry):
        match = _OLD_BINARIES.match(line)
        if not match:
            continue
        for part in match.group("rest").split("; "):
            version = _FROM_VERSION.match(part.strip())
            if version:
                names = {n.strip() for n in version.group("names").split(",")}
                found.setdefault(version.group("version"), set()).update(names)
    return {version: sorted(names) for version, names in sorted(found.items())}


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
    if (
        entry.get("migration-policy-verdict") in _WAITING_VERDICTS
        or (entry.get("dependencies") or {}).get("blocked-by")
        or "linux-meta-not-ready" in (entry.get("reason") or [])
    ):
        found.add(Reason.WAITING)
    if "cruft" in (entry.get("reason") or []):
        found.add(Reason.OLD_BINARIES)
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
    found: dict[str, Any] = {
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
            | set(awaited_meta(entry))
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
    # Usually empty, so written only when not (see the module docstring).
    for key, value in (
        ("block_bugs", block_bugs(entry)),
        ("depends_issues", depends_issues(entry)),
        ("old_binaries", old_binaries(entry)),
    ):
        if value:
            found[key] = value
    return found


def arch_only_payload(
    entries: Iterable[Mapping[str, Any]], generated: datetime | None
) -> dict[str, Any]:
    """One payload for a source held only through architecture items, with
    the architectures it is held on.

    Each field is merged by its own shape, not generically: a generic merge
    tried to hash depends issues (dicts holding lists) and failed the whole
    ingest when two architectures both had one (review on #24).
    """
    entries = list(entries)
    found = [payload(entry, generated) for entry in entries]
    merged = dict(found[0])
    for more in found[1:]:
        for key, value in more.items():
            if key not in merged:
                merged[key] = value
            elif key == "tests":
                merged[key] = _merge_tests(merged[key], value)
            elif key == "depends_issues":
                merged[key] = _merge_depends_issues(merged[key], value)
            elif key == "old_binaries":
                merged[key] = {
                    version: sorted(
                        {*merged[key].get(version, ()), *value.get(version, ())}
                    )
                    for version in sorted({*merged[key], *value})
                }
            elif isinstance(value, list):
                # Lists of codes, names or bug numbers.
                merged[key] = sorted({*merged[key], *value})
    merged["architectures"] = sorted(
        str(e.get("item-name")).split("/", 1)[1] for e in entries
    )
    return merged


def _merge_tests(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    results: dict[tuple[str, str], dict[str, str]] = {}
    logs: dict[tuple[str, str], dict[str, str]] = {}
    for test in [*first, *second]:
        key = (test["test"], test["version"])
        results.setdefault(key, {}).update(test["results"])
        logs.setdefault(key, {}).update(test.get("logs") or {})
    return [
        {"test": t, "version": v, "results": dict(sorted(r.items()))}
        | ({"logs": dict(sorted(logs[(t, v)].items()))} if logs[(t, v)] else {})
        for (t, v), r in sorted(results.items())
    ]


def _merge_depends_issues(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The same message from two items is one issue on both items'
    architectures."""
    arches: dict[tuple[str, str], set[str]] = {}
    for issue in [*first, *second]:
        key = (issue["kind"], issue["text"])
        arches.setdefault(key, set()).update(issue.get("arches") or ())
    return [
        {"kind": kind, "text": text, "arches": sorted(found)}
        for (kind, text), found in arches.items()
    ]


def referenced_packages(signal: Signal) -> set[str]:
    """Packages a migration signal names besides its own.

    Their owners are the people who can fix a regression in their own tests,
    or get an awaited upload moving, so ownership has to be collected for
    them too. An awaited package need not be stuck itself (britney's
    blocked-by can name one that is not), and without its ownership the
    upload waiting on it reaches nobody's page.
    """
    if signal.kind is not Kind.MIGRATION_BLOCKED:
        return set()
    tests = {
        r["test"]
        for r in payload_tests(signal.payload)
        if r.get("test") and FAILED & set((r.get("results") or {}).values())
    }
    awaited = set(signal.payload.get("waits_for") or ())
    return (tests | awaited) - {signal.source_package}


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

        # One signal per source keeps identity simple. A source usually has
        # one item, the upload proper. An architecture item ("src/arm64", a
        # rebuild on one architecture) is folded into it, and becomes the
        # signal itself when it is all that is held: imagewmark on 20 Nov
        # 2026 was held only on arm64, armhf and ppc64el, and was missing
        # from the board. Removal items ("-src") are not uploads and stay out.
        whole: dict[str, Mapping[str, Any]] = {}
        by_arch: dict[str, list[Mapping[str, Any]]] = {}
        skipped = 0
        for entry in raw.entries:
            name = entry.get("source")
            if entry.get("is-candidate") or not name:
                continue
            item = str(entry.get("item-name") or "")
            if item == name:
                whole[name] = entry
            elif item.startswith(f"{name}/"):
                by_arch.setdefault(name, []).append(entry)
            else:
                skipped += 1
        if skipped:
            log.info("migration: skipped %d removal item(s)", skipped)

        signals = []
        for name in [*whole, *(n for n in by_arch if n not in whole)]:
            if name in whole:
                found = payload(whole[name], raw.generated)
            else:
                found = arch_only_payload(by_arch[name], raw.generated)
            signals.append(
                Signal(
                    kind=Kind.MIGRATION_BLOCKED,
                    source_package=name,
                    series=self.series,
                    payload=found,
                    # Launchpad, the system of record, not britney's page:
                    # cairn replaces that page rather than pointing at it.
                    url=f"https://launchpad.net/ubuntu/+source/{name}",
                )
            )
        return signals
