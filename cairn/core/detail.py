"""Each source's current detail: what the log does not keep.

The log is history and is never rewritten. Detail is the opposite: the state
of the last successful run, overwritten whole by the next (see
Ingester.detail). A failed run writes nothing, so the previous file stands
and is exactly as stale as the source's signals, which the board already
says. Losing the file loses no history; the pages fall back to what the log
holds.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def path(directory: Path, source: str) -> Path:
    return directory / f"{source}.json"


def save(directory: Path, source: str, found: Mapping[str, Any]) -> Path:
    """Written beside and then moved over the old file, so a run killed
    mid-write leaves the previous detail rather than half of a new one."""
    target = path(directory, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".json.partial")
    partial.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
    os.replace(partial, target)
    return target


def load(directory: Path, source: str) -> dict[str, Any]:
    """Empty when the source has never saved detail, or the file is
    unreadable: the pages then show what the log holds."""
    try:
        found = json.loads(path(directory, source).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}
