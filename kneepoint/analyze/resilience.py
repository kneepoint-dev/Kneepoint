"""Resilience score v0: resolution under chaos vs. clean sessions in the same run.

Clean sessions (no fault encountered) are the baseline; faulted sessions the
treatment. Honest None when either group is too thin to trust — the same
discipline knee detection applies.
"""

from typing import Literal

from pydantic import BaseModel

from kneepoint.collect.schemas import SessionRecord


class FaultRow(BaseModel):
    fault: str
    sessions_hit: int
    resolved_rate: float | None
    verdict: Literal["pass", "degraded", "fail"] | None


class ResilienceSummary(BaseModel):
    clean_sessions: int
    faulted_sessions: int
    clean_resolution_rate: float | None
    faulted_resolution_rate: float | None
    score: float | None  # faulted/clean * 100, capped at 100
    rows: list[FaultRow]


def _rate(group: list[SessionRecord]) -> float:
    return sum(1 for s in group if s.resolved) / len(group)


def _verdict(rate: float | None, clean_rate: float | None):
    if rate is None or not clean_rate:
        return None
    ratio = rate / clean_rate
    if ratio >= 0.95:
        return "pass"
    return "degraded" if ratio >= 0.70 else "fail"


def resilience(sessions: list[SessionRecord], min_group: int = 10) -> ResilienceSummary:
    judged = [s for s in sessions if s.resolved is not None]
    clean = [s for s in judged if not s.faults]
    faulted = [s for s in judged if s.faults]
    thin = len(clean) < min_group or len(faulted) < min_group
    clean_rate = None if thin else _rate(clean)
    faulted_rate = None if thin else _rate(faulted)
    score = None
    if clean_rate:  # not None and not zero
        score = min(100.0, faulted_rate / clean_rate * 100.0)
    by_fault: dict[str, list[SessionRecord]] = {}
    for session in faulted:
        for fault in set(session.faults):
            by_fault.setdefault(fault, []).append(session)
    rows = [
        FaultRow(
            fault=fault,
            sessions_hit=len(group),
            resolved_rate=None if len(group) < min_group else _rate(group),
            verdict=_verdict(None if len(group) < min_group else _rate(group), clean_rate),
        )
        for fault, group in sorted(by_fault.items())
    ]
    return ResilienceSummary(
        clean_sessions=len(clean), faulted_sessions=len(faulted),
        clean_resolution_rate=clean_rate, faulted_resolution_rate=faulted_rate,
        score=score, rows=rows,
    )
