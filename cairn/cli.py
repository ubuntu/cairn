"""The pipeline's front door.

Writes the two logs and nothing else: what to fetch, what changed and how
severe it is are all decided elsewhere. Exit status follows AGENTS.md
section 7 — non-zero means *every* source failed, because one broken source
must not look like a broken run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from cairn import paths
from cairn.build import site
from cairn.core import health
from cairn.core import log as logmod
from cairn.core.reconcile import DEFAULT_MAX_RESOLVE_FRACTION
from cairn.core.runner import Outcome, RunReport, run
from cairn.ingest import metadata, registry
from cairn.ingest.base import Fetcher
from cairn.ingest.http import Cache, FileCache, HttpFetcher, ReadOnlyCache
from cairn.ingest.metadata import IncompleteRefresh
from cairn.ingest.series import development_series

EXIT_OK = 0
EXIT_TOTAL_FAILURE = 1
EXIT_USAGE = 2


def summarise(
    report: RunReport,
    *,
    series: str,
    dry_run: bool = False,
    metadata_refreshed: bool = True,
) -> str:
    lines = [f"### cairn ingest — {series}", ""]
    if dry_run:
        lines += ["_Dry run: nothing was written._", ""]
    lines += [
        f"{len(report.events)} event(s), "
        f"{len(report.succeeded)} of {len(report.outcomes)} source(s) ok.",
        "",
        "| Source | Status | Signals | Events |",
        "|---|---|---:|---:|",
    ]
    if not metadata_refreshed:
        lines.insert(
            2, "_Ownership data could not be refreshed; the previous file stands._"
        )
    for outcome in report.outcomes:
        status = (
            "ok"
            if outcome.ok
            else "FAILED - "
            + (outcome.error or "").replace("|", r"\|").replace("\n", " ")
        )
        lines.append(
            f"| {outcome.source} | {status} | {outcome.signals} | {outcome.events} |"
        )
    return "\n".join(lines)


def _fetcher(args: argparse.Namespace) -> Fetcher:
    if args.no_cache:
        return HttpFetcher(cache=None)
    cache: Cache = FileCache(args.cache_dir)
    if args.dry_run:
        cache = ReadOnlyCache(cache)
    return HttpFetcher(cache=cache)


def _record(report: RunReport, args: argparse.Namespace) -> None:
    if args.dry_run:
        return
    logmod.append(args.signals, report.events)
    health.append(args.health, report)


def _emit(summary: str) -> None:
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as fh:
            fh.write(summary + "\n")


def cmd_ingest(
    args: argparse.Namespace,
    fetcher: Fetcher | None = None,
    connect: Callable[[], object] | None = None,
) -> int:
    known = registry.available()
    names = args.sources or sorted(known)
    unknown = sorted(set(names) - set(known))
    if unknown:
        print(
            f"cairn: unknown source(s): {', '.join(unknown)}; "
            f"known: {', '.join(sorted(known)) or 'none'}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Two identical ingesters reconcile against the same previous state, so
    # both emit OPENED and replay counts one recurrence that never happened.
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        print(
            f"cairn: repeated source(s): {', '.join(repeated)}; "
            "name each source at most once",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if fetcher is None:
        fetcher = _fetcher(args)

    now = datetime.now(UTC)

    try:
        series = args.series or development_series(fetcher)
    except Exception as exc:  # noqa: BLE001 - no series means nothing to ingest
        print(f"cairn: cannot resolve the development series: {exc}", file=sys.stderr)
        print("cairn: pass --series to override", file=sys.stderr)
        # Still a run outcome. Returning silently would leave every source
        # looking as healthy as it was yesterday.
        error = f"{type(exc).__name__}: {exc}"
        failed = RunReport(
            started_at=now,
            outcomes=tuple(Outcome(name, ok=False, error=error) for name in names),
            events=(),
        )
        _record(failed, args)
        _emit(summarise(failed, series="unresolved", dry_run=args.dry_run))
        return EXIT_TOTAL_FAILURE

    report = run(
        [registry.build(name, fetcher, series) for name in names],
        logmod.load(args.signals),
        now=now,
        max_resolve_fraction=(
            None if args.allow_mass_resolve else DEFAULT_MAX_RESOLVE_FRACTION
        ),
    )

    _record(report, args)

    refreshed = _refresh_metadata(args, fetcher, series, connect)
    _emit(
        summarise(
            report, series=series, dry_run=args.dry_run, metadata_refreshed=refreshed
        )
    )
    return EXIT_TOTAL_FAILURE if report.total_failure else EXIT_OK


def _refresh_metadata(
    args: argparse.Namespace,
    fetcher: Fetcher,
    series: str,
    connect: Callable[[], object] | None = None,
) -> bool:
    """Ownership data, kept out of the append-only log.

    A failure here leaves the previous snapshot untouched rather than writing
    a partial one, so an unreachable Launchpad costs freshness and nothing
    else. Returning False lets the caller say so out loud.
    """
    active = [st for st in logmod.load(args.signals).values() if st.is_active]
    wanted = {st.signal.source_package for st in active}
    debian_versions = {
        st.signal.source_package: st.signal.payload["debian_version"]
        for st in active
        if st.signal.payload.get("debian_version")
    }
    try:
        snapshot = metadata.collect(
            fetcher,
            series,
            keep=wanted,
            debian_versions=debian_versions,
            connect=connect,
        )
    except IncompleteRefresh as exc:
        print(f"cairn: keeping the previous ownership data: {exc}", file=sys.stderr)
        return False
    if not args.dry_run:
        metadata.save(args.packages, snapshot)
    return True


def cmd_build(
    args: argparse.Namespace,
    fetcher: Fetcher | None = None,
    connect: Callable[[], object] | None = None,
) -> int:
    state = logmod.load(args.signals)
    if not state:
        print(
            f"cairn: no signals in {args.signals}; run `cairn ingest` first",
            file=sys.stderr,
        )
        return EXIT_USAGE

    index = site.build(
        state,
        health.load(args.health),
        metadata.load(args.packages),
        out=args.out,
        now=datetime.now(UTC),
        series=args.series,
    )
    active = sum(1 for s in state.values() if s.is_active)
    print(f"cairn: wrote {index} ({active} active signals)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn", description="A single view of Ubuntu archive health."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="fetch every source and record what changed")
    ingest.add_argument("--series", help="default: the series open for development")
    ingest.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="NAME",
        help="repeatable; default: every registered source",
    )
    ingest.add_argument("--signals", type=Path, default=paths.SIGNALS_LOG)
    ingest.add_argument("--health", type=Path, default=paths.HEALTH_LOG)
    ingest.add_argument("--packages", type=Path, default=paths.PACKAGES)
    ingest.add_argument("--cache-dir", type=Path, default=paths.CACHE_DIR)
    ingest.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore the on-disk HTTP cache entirely",
    )
    ingest.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    ingest.add_argument(
        "--allow-mass-resolve",
        action="store_true",
        help="accept a run that resolves most of a source's signals",
    )
    ingest.set_defaults(handler=cmd_ingest)

    build = sub.add_parser("build", help="render the log into a static site")
    build.add_argument("--signals", type=Path, default=paths.SIGNALS_LOG)
    build.add_argument("--health", type=Path, default=paths.HEALTH_LOG)
    build.add_argument("--packages", type=Path, default=paths.PACKAGES)
    build.add_argument("--out", type=Path, default=paths.SITE_DIR)
    build.add_argument("--series", help="default: taken from the log")
    build.set_defaults(handler=cmd_build)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: Fetcher | None = None,
    connect: Callable[[], object] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.handler(args, fetcher, connect)
