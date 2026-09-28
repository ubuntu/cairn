"""Guards the rule AGENTS.md section 3 calls load-bearing.

Wrong version ordering raises nothing; it silently publishes wrong answers.
These cases are the ones naive string comparison gets backwards, so a change in
python-debian's semantics fails here rather than in production.
"""

from __future__ import annotations

import pytest
from debian.debian_support import Version, version_compare

# (older, newer) — asserted strictly in that order.
ORDERED_PAIRS = [
    ("3.0~a57-1ubuntu56", "3.0-1"),
    ("1.0~beta1", "1.0"),
    ("1.0~rc1", "1.0"),
    ("2.0", "1:1.0"),
    ("1.0-1ubuntu1", "1.0-10"),
    ("1.0-1", "1.0-1ubuntu1"),
    ("1.0-1ubuntu1", "1.0-1ubuntu2"),
    ("1.0-1ubuntu2", "1.0-1ubuntu10"),
    ("1.0", "1.0.1"),
    ("1.9", "1.10"),
    ("1:1.0", "2:0.1"),
    ("0.1", "1.0"),
    ("1.0-1", "1.0-2"),
    ("7.12.1-3ubuntu0.1", "7.12.1-3ubuntu0.2"),
]

EQUAL_PAIRS = [
    ("1.0", "1.0"),
    ("0:1.0", "1.0"),
    ("1.0-1", "1.0-1"),
]


@pytest.mark.parametrize("older,newer", ORDERED_PAIRS)
def test_ordering(older, newer):
    assert version_compare(older, newer) < 0
    assert version_compare(newer, older) > 0


@pytest.mark.parametrize("older,newer", ORDERED_PAIRS)
def test_version_objects_agree_with_version_compare(older, newer):
    assert Version(older) < Version(newer)


@pytest.mark.parametrize("a,b", EQUAL_PAIRS)
def test_equality(a, b):
    assert version_compare(a, b) == 0
    assert Version(a) == Version(b)


def test_string_comparison_disagrees_on_known_cases():
    """Documents why python-debian is a hard dependency."""
    disagreements = [(a, b) for a, b in ORDERED_PAIRS if not (a < b)]
    assert len(disagreements) >= 4, (
        "expected naive ordering to be wrong on the tilde, epoch and numeric "
        f"cases, got {disagreements}"
    )


def test_epoch_dominates_regardless_of_upstream():
    assert version_compare("1:0.1", "99.0") > 0


def test_tilde_sorts_before_everything():
    assert version_compare("1.0~", "1.0") < 0
    assert version_compare("1.0~~", "1.0~") < 0
