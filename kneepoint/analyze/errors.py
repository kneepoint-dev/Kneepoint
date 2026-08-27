"""Errors per ramp level: how many, which, and with what status code.

The run's console prints "N errors" per level and the JSONL carries `error` and
`status_code` on every attempt, but until this module nothing read them back
out. `LevelStats.error_rate` already exists; what it cannot show is *which*
failures made up the rate — and a 429 (the provider throttling you), a 503 (the
server genuinely failing) and a timeout (no code at all) call for different
fixes, so a bare rate is not enough (docs/book/stability.md).

Computed from the request records, not from `LevelStats`: `aggregate()` drops a
level where every request failed, because it has no latency to report. Such a
level is the most important row of an errors table, not one to omit.
"""

from collections import Counter

from pydantic import BaseModel

from kneepoint.collect.schemas import RequestRecord

# How many distinct error strings a level lists. Beyond this the tail is folded
# into one "other" count rather than dropped, so the rows still sum to `failed`.
TOP_ERRORS = 3

# The status-code label for a failure that never got an HTTP status — a timeout,
# a connection error, a client-side abandonment. Named so it reads as a category
# in the mix, not as a missing value.
NO_STATUS = "no code"


class ErrorCount(BaseModel):
    error: str
    count: int


class StatusCount(BaseModel):
    status: str      # "429", "503", … or NO_STATUS
    count: int


class ErrorLevel(BaseModel):
    concurrency: int
    requests: int
    failed: int
    error_rate: float            # failed / requests
    abandoned: int               # failures that were the client giving up (contamination)
    status_codes: list[StatusCount]   # every failed attempt, by status, most common first
    top_errors: list[ErrorCount]      # up to TOP_ERRORS strings, plus an "other" fold


def errors_by_level(records: list[RequestRecord]) -> list[ErrorLevel]:
    """Failures per ramp level, broken down by error string and status code.

    Every level with at least one record is returned — including levels with no
    failures, so the reader can see where errors *start*, and levels where every
    request failed, which `aggregate()` leaves out of the latency stats.
    """
    by_level: dict[int, list[RequestRecord]] = {}
    for record in records:
        by_level.setdefault(record.concurrency, []).append(record)

    out: list[ErrorLevel] = []
    for level in sorted(by_level):
        recs = by_level[level]
        failed = [r for r in recs if not r.ok]
        statuses = Counter(
            str(r.status_code) if r.status_code is not None else NO_STATUS for r in failed
        )
        errors = Counter((r.error or "(no error message)") for r in failed)
        top = errors.most_common(TOP_ERRORS)
        rest = sum(errors.values()) - sum(n for _, n in top)
        top_errors = [ErrorCount(error=e, count=n) for e, n in top]
        if rest:
            top_errors.append(
                ErrorCount(error=f"other ({len(errors) - len(top)} distinct)", count=rest)
            )
        out.append(ErrorLevel(
            concurrency=level,
            requests=len(recs),
            failed=len(failed),
            error_rate=len(failed) / len(recs),
            abandoned=sum(1 for r in failed if r.abandoned),
            status_codes=[StatusCount(status=s, count=n) for s, n in statuses.most_common()],
            top_errors=top_errors,
        ))
    return out


def first_error_level(levels: list[ErrorLevel]) -> ErrorLevel | None:
    """The lowest level with any failure — the report's "errors start at" line.

    Errors present from the first level are a configuration problem, not a
    capacity one; errors that appear only above some level are the capacity
    signal. Which of the two this is decides what the reader should do next.
    """
    for lv in levels:
        if lv.failed:
            return lv
    return None
