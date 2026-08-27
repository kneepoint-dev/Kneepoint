"""Retry amplification: how much load the generator added on top of the workload.

A failed turn is retried, which is what a real client does — but it means the
generator issues *more* requests exactly when the target is struggling, and can
partly manufacture the knee it is measuring. Run D's c=7 level fired 21 requests
through 7 workers and lost every one of them.

Kneepoint keeps the realistic default and makes the amplification visible
instead, so it can be disclosed rather than discovered (METHODOLOGY §4c).
"""

from pydantic import BaseModel

from kneepoint.collect.schemas import RequestRecord, SessionRecord


class RetryLevel(BaseModel):
    concurrency: int
    sessions: int
    requests: int          # every attempt, retries included
    first_attempts: int    # attempt == 1: the workload the ramp actually asked for
    retries: int           # attempt > 1: load the generator added
    abandoned: int
    amplification: float   # requests / first_attempts; 1.0 means no extra load


def retry_by_level(
    records: list[RequestRecord], sessions: list[SessionRecord]
) -> list[RetryLevel]:
    """Requests issued vs work asked for, per ramp level."""
    sessions_by_level: dict[int, int] = {}
    for session in sessions:
        sessions_by_level[session.concurrency] = sessions_by_level.get(
            session.concurrency, 0
        ) + 1
    by_level: dict[int, list[RequestRecord]] = {}
    for record in records:
        by_level.setdefault(record.concurrency, []).append(record)

    out: list[RetryLevel] = []
    for level in sorted(by_level):
        recs = by_level[level]
        first = sum(1 for r in recs if r.attempt == 1)
        out.append(RetryLevel(
            concurrency=level,
            sessions=sessions_by_level.get(level, 0),
            requests=len(recs),
            first_attempts=first,
            retries=len(recs) - first,
            abandoned=sum(1 for r in recs if r.abandoned),
            # first is never 0 when the level has records: every turn starts at
            # attempt 1, so a level with attempts has at least one of them
            amplification=len(recs) / first if first else 1.0,
        ))
    return out


def worst_amplification(levels: list[RetryLevel]) -> RetryLevel | None:
    """The level where the generator added the most load, if any level did."""
    inflated = [lv for lv in levels if lv.retries]
    return max(inflated, key=lambda lv: lv.amplification) if inflated else None
