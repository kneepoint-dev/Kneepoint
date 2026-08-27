"""Retry amplification: the load the generator adds on top of the workload.

Run D's c=7 level fired 21 requests through 7 workers and lost every one. Every
retry is real client behaviour, and it is also extra load arriving exactly when
the target is already struggling — a ramp can partly manufacture the knee it
measures. Kneepoint keeps the realistic default and shows the number.
"""

from kneepoint.analyze.retry import retry_by_level, worst_amplification
from kneepoint.collect.schemas import RequestRecord, SessionRecord


def _rec(level: int, *, attempt: int = 1, ok: bool = True,
         abandoned: bool = False) -> RequestRecord:
    return RequestRecord(session_id="s", concurrency=level, started_at=0.0,
                         total_ms=1.0, ok=ok, attempt=attempt, abandoned=abandoned)


def _sess(level: int) -> SessionRecord:
    return SessionRecord(session_id="s", concurrency=level, started_at=0.0,
                         total_ms=1.0, turns_requested=1, turns_completed=1, ok=True)


def test_a_clean_level_has_no_amplification():
    levels = retry_by_level([_rec(1), _rec(1)], [_sess(1), _sess(1)])
    assert levels[0].amplification == 1.0
    assert levels[0].retries == 0
    assert levels[0].first_attempts == levels[0].requests == 2
    assert worst_amplification(levels) is None


def test_run_d_c7_shape_three_attempts_per_turn():
    """7 workers, one turn each, every turn retried to exhaustion: 21 requests."""
    records = [_rec(7, attempt=a, ok=False) for _ in range(7) for a in (1, 2, 3)]
    levels = retry_by_level(records, [_sess(7) for _ in range(7)])
    assert levels[0].requests == 21
    assert levels[0].first_attempts == 7
    assert levels[0].retries == 14
    assert levels[0].amplification == 3.0
    assert levels[0].sessions == 7


def test_amplification_is_reported_per_level():
    records = [_rec(1), _rec(2), _rec(2, attempt=2, ok=False)]
    levels = retry_by_level(records, [_sess(1), _sess(2)])
    assert [lv.concurrency for lv in levels] == [1, 2]
    assert levels[0].amplification == 1.0
    assert levels[1].amplification == 2.0


def test_worst_level_is_the_one_to_disclose():
    records = (
        [_rec(1)]
        + [_rec(4), _rec(4, attempt=2, ok=False)]
        + [_rec(7)] + [_rec(7, attempt=a, ok=False) for a in (2, 3)]
    )
    worst = worst_amplification(retry_by_level(records, [_sess(1), _sess(4), _sess(7)]))
    assert worst.concurrency == 7
    assert worst.amplification == 3.0


def test_abandonment_is_counted_beside_the_amplification():
    """The two confounds compound: an abandoned turn is retried into the same
    queue that its own abandoned generation is still occupying."""
    records = [
        _rec(7, ok=False, abandoned=True),
        _rec(7, attempt=2, ok=False, abandoned=True),
        _rec(7, attempt=3, ok=False, abandoned=True),
    ]
    level = retry_by_level(records, [_sess(7)])[0]
    assert level.abandoned == 3
    assert level.amplification == 3.0


def test_a_level_with_no_sessions_recorded_still_reports_its_requests():
    """Sessions are written after judging; requests stream out per level. A
    partial run must not divide by a session count that isn't there yet."""
    level = retry_by_level([_rec(5), _rec(5, attempt=2, ok=False)], [])[0]
    assert level.sessions == 0
    assert level.requests == 2
    assert level.amplification == 2.0
