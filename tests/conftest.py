"""Keep the suite away from the repository's own data.

cairn's default paths are relative to the working directory, and the
default working directory for pytest is the repository root. A test that
forgets one --flag would otherwise write straight into data/, which is real,
append-only history. This has already happened once: a test without
--packages overwrote data/packages.json with fixture output.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _away_from_the_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
