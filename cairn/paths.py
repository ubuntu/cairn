"""Where cairn keeps things on disk.

One module, so the log's location is a fact rather than a convention repeated
in every caller. These are defaults relative to the working directory, which
is the repository root for both the CI job and a normal local run. Override
them with --signals / --health when running from anywhere else, or the log
forks.
"""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path("data")

# A directory of monthly files, <YYYY-MM>.jsonl (cairn.core.log). The single
# data/signals.jsonl it replaced is still read while it exists.
SIGNALS_LOG = DATA_DIR / "signals"

# Written even when no signal changed: the signal log cannot distinguish
# "unchanged" from "not collected".
HEALTH_LOG = DATA_DIR / "health.jsonl"

# Ownership and publication metadata. Derived, so it is overwritten whole and
# a failed refresh simply leaves the previous file in place.
PACKAGES = DATA_DIR / "packages.json"

# Each source's current detail, data/<source>.json (cairn.core.detail):
# overwritten each successful run, never history.
DETAIL_DIR = DATA_DIR

CACHE_DIR = Path(".cache/http")

# Generated. Rebuildable from the log, so never committed.
SITE_DIR = Path("site")
