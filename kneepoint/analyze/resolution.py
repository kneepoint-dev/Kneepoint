"""Resolution-rate aggregation: overall and per concurrency level (the quality curve)."""

from pydantic import BaseModel

from kneepoint.collect.schemas import SessionRecord


class LevelResolution(BaseModel):
    concurrency: int
    judged: int
    resolved_rate: float


def resolution_rate(sessions: list[SessionRecord]) -> float | None:
    judged = [s for s in sessions if s.resolved is not None]
    if not judged:
        return None
    return sum(1 for s in judged if s.resolved) / len(judged)


def resolution_by_level(sessions: list[SessionRecord]) -> list[LevelResolution]:
    by_level: dict[int, list[SessionRecord]] = {}
    for session in sessions:
        if session.resolved is not None:
            by_level.setdefault(session.concurrency, []).append(session)
    return [
        LevelResolution(
            concurrency=level,
            judged=len(group),
            resolved_rate=sum(1 for s in group if s.resolved) / len(group),
        )
        for level, group in sorted(by_level.items())
    ]
