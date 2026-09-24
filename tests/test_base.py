from __future__ import annotations

import dataclasses
import os
import subprocess
import sys

import pytest

from cairn.core import rules
from cairn.ingest.base import Kind, Severity, Signal, severity_rank


def sig(**kw) -> Signal:
    base = {"kind": Kind.NEEDS_MERGE, "source_package": "hello"}
    return Signal(**(base | kw))


class TestIdentity:
    def test_stable_across_instances(self):
        assert sig().signal_id == sig().signal_id

    def test_payload_does_not_affect_identity(self):
        assert (
            sig(payload={"days": 1}).signal_id == sig(payload={"days": 783}).signal_id
        )

    def test_url_does_not_affect_identity(self):
        assert sig(url="http://a").signal_id == sig(url="http://b").signal_id

    @pytest.mark.parametrize(
        "field,value",
        [
            ("kind", Kind.NBS),
            ("source_package", "other"),
            ("series", "noble"),
            ("binary_package", "libhello1"),
        ],
    )
    def test_identity_fields_do_affect_identity(self, field, value):
        assert sig().signal_id != sig(**{field: value}).signal_id

    def test_none_and_empty_string_collapse(self):
        assert sig(series=None).signal_id == sig(series="").signal_id

    def test_separator_prevents_field_smear(self):
        a = sig(source_package="ab", binary_package="c")
        b = sig(source_package="a", binary_package="bc")
        assert a.signal_id != b.signal_id

    def test_id_is_not_process_salted(self):
        """A salted hash would silently reset first_seen on every run."""
        code = (
            "from cairn.ingest.base import Kind, Signal;"
            "print(Signal(kind=Kind.NEEDS_MERGE, source_package='hello').signal_id)"
        )
        env = os.environ | {"PYTHONHASHSEED": "random", "PYTHONPATH": os.getcwd()}
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        assert out.stdout.strip() == sig().signal_id


class TestValidation:
    def test_rejects_bare_string_kind(self):
        with pytest.raises(TypeError):
            Signal(kind="needs_merge", source_package="hello")

    def test_rejects_empty_package(self):
        with pytest.raises(ValueError):
            Signal(kind=Kind.NBS, source_package="")

    def test_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            sig().source_package = "other"


class TestSeverity:
    def test_ranking_is_not_alphabetical(self):
        ordered = sorted(Severity, key=severity_rank)
        assert ordered[0] is Severity.INFO
        assert ordered[-1] is Severity.CRITICAL
        assert severity_rank(Severity.HIGH) > severity_rank(Severity.MEDIUM)

    def test_every_kind_has_a_rule(self):
        assert rules.unclassified_kinds() == set()

    def test_verification_failed_is_high(self):
        assert rules.severity(sig(kind=Kind.SRU_VERIFICATION_FAILED)) is Severity.HIGH

    @pytest.mark.parametrize(
        "days,expected",
        [
            (1, Severity.LOW),
            (29, Severity.LOW),
            (30, Severity.MEDIUM),
            (90, Severity.HIGH),
        ],
    )
    def test_sru_pending_escalates_with_age(self, days, expected):
        s = sig(kind=Kind.SRU_PENDING, payload={"days_in_proposed": days})
        assert rules.severity(s) is expected

    def test_missing_payload_does_not_crash(self):
        assert rules.severity(sig(kind=Kind.SRU_PENDING)) is Severity.LOW

    def test_duplicate_rule_registration_is_rejected(self):
        with pytest.raises(ValueError):
            rules.rule(Kind.NBS)(lambda s: Severity.HIGH)
