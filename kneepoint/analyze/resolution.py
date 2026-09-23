"""Session verdict populations, overall and per concurrency level.

A verdict is not necessarily an external judgment: configured execution-failure
rules and deterministic checks also count as evaluated. Unknown stays unknown.
"""

from pydantic import BaseModel, Field

from kneepoint.collect.schemas import SessionRecord


class ResolutionSummary(BaseModel):
    total: int
    transport_ok: int  # session.ok: every turn eventually completed successfully
    transport_failed: int  # failed or incomplete sessions, not request attempts
    judged: int  # legacy name for sessions with any explicit verdict
    resolved: int
    unresolved: int
    unjudged: int
    coverage: float | None
    resolved_rate: float | None  # resolved / judged (legacy contract)
    confirmed_resolved_share: float | None  # resolved / all sessions
    methods: dict[str, int] = Field(default_factory=dict)

    @property
    def slo_resolution(self) -> float | None:
        """A whole-run resolution SLO needs a verdict for every recorded session."""
        return self.resolved_rate if self.total and not self.unjudged else None

    def lines(self) -> list[str]:
        def percent(value: float | None) -> str:
            return f"{value:.1%}" if value is not None else "n/a"

        methods = ", ".join(f"{key}={count}" for key, count in sorted(self.methods.items()))
        return [
            f"Session completion: {self.transport_ok}/{self.total} completed, "
            f"{self.transport_failed} failed/incomplete",
            f"Verdicts: {self.resolved} resolved, {self.unresolved} unresolved, "
            f"{self.unjudged} unjudged; coverage {percent(self.coverage)} "
            f"({self.judged}/{self.total} evaluated)",
            f"Resolution rate: {percent(self.resolved_rate)} among evaluated sessions "
            f"({self.resolved}/{self.judged}); confirmed resolved/all: "
            f"{percent(self.confirmed_resolved_share)} ({self.resolved}/{self.total})",
            f"Verdict methods: {methods or 'none'}",
        ]


class LevelResolution(BaseModel):
    concurrency: int
    judged: int
    resolved_rate: float | None
    # Old callers supplied only the three fields above. Do not infer missing
    # population counts from rounded rates or pretend their coverage is known.
    total: int | None = None
    transport_ok: int | None = None
    transport_failed: int | None = None
    resolved: int | None = None
    unresolved: int | None = None
    unjudged: int | None = None
    coverage: float | None = None
    confirmed_resolved_share: float | None = None
    methods: dict[str, int] = Field(default_factory=dict)


def resolution_summary(sessions: list[SessionRecord]) -> ResolutionSummary:
    total = len(sessions)
    judged = sum(s.resolved is not None for s in sessions)
    resolved = sum(s.resolved is True for s in sessions)
    transport_ok = sum(s.ok for s in sessions)
    methods: dict[str, int] = {}
    for session in sessions:
        if session.resolved is not None:
            method = session.resolution_method or "unspecified"
            methods[method] = methods.get(method, 0) + 1
    return ResolutionSummary(
        total=total, transport_ok=transport_ok, transport_failed=total - transport_ok,
        judged=judged, resolved=resolved, unresolved=judged - resolved,
        unjudged=total - judged, coverage=judged / total if total else None,
        resolved_rate=resolved / judged if judged else None,
        confirmed_resolved_share=resolved / total if total else None, methods=methods,
    )


def resolution_rate(sessions: list[SessionRecord]) -> float | None:
    """Legacy judged-only rate; never treat this alone as whole-run coverage."""
    return resolution_summary(sessions).resolved_rate


def resolution_by_level(sessions: list[SessionRecord]) -> list[LevelResolution]:
    by_level: dict[int, list[SessionRecord]] = {}
    for session in sessions:
        by_level.setdefault(session.concurrency, []).append(session)
    return [
        LevelResolution(concurrency=level, **resolution_summary(group).model_dump())
        for level, group in sorted(by_level.items())
    ]
