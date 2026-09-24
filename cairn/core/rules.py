"""Severity for every source, assigned in one place.

Sources disagree about wording and urgency; cairn must not inherit that
disagreement. Rules are registered rather than branched on, so adding a kind is
an addition to this module rather than an edit inside it.
"""

from __future__ import annotations

from collections.abc import Callable

from cairn.ingest.base import Kind, Severity, Signal

SeverityRule = Callable[[Signal], Severity]

_RULES: dict[Kind, SeverityRule] = {}

DEFAULT_SEVERITY = Severity.LOW


def rule(kind: Kind) -> Callable[[SeverityRule], SeverityRule]:
    def register(fn: SeverityRule) -> SeverityRule:
        if kind in _RULES:
            raise ValueError(f"duplicate severity rule for {kind}")
        _RULES[kind] = fn
        return fn

    return register


def severity(signal: Signal) -> Severity:
    return _RULES.get(signal.kind, lambda _: DEFAULT_SEVERITY)(signal)


def unclassified_kinds() -> set[Kind]:
    return set(Kind) - set(_RULES)


@rule(Kind.SRU_VERIFICATION_FAILED)
def _sru_verification_failed(signal: Signal) -> Severity:
    """Someone tested it and it failed; it blocks the pocket and will not
    migrate on its own."""
    return Severity.HIGH


@rule(Kind.SRU_PENDING)
def _sru_pending(signal: Signal) -> Severity:
    days = signal.payload.get("days_in_proposed")
    if isinstance(days, int) and days >= 90:
        return Severity.HIGH
    if isinstance(days, int) and days >= 30:
        return Severity.MEDIUM
    return Severity.LOW


@rule(Kind.MIGRATION_BLOCKED)
def _migration_blocked(signal: Signal) -> Severity:
    return Severity.MEDIUM


@rule(Kind.BUILD_FAILED)
def _build_failed(signal: Signal) -> Severity:
    return Severity.HIGH


@rule(Kind.NBS)
def _nbs(signal: Signal) -> Severity:
    return Severity.LOW


@rule(Kind.NEEDS_MERGE)
def _needs_merge(signal: Signal) -> Severity:
    return Severity.LOW


@rule(Kind.TRANSITION_BLOCKED)
def _transition_blocked(signal: Signal) -> Severity:
    return Severity.MEDIUM


@rule(Kind.SPONSORSHIP_PENDING)
def _sponsorship_pending(signal: Signal) -> Severity:
    days = signal.payload.get("days_waiting")
    if isinstance(days, int) and days >= 14:
        return Severity.MEDIUM
    return Severity.LOW
