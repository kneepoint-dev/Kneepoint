"""Errors per level: the breakdown the report shows, computed from the records."""

from kneepoint.analyze.errors import (
    NO_STATUS,
    TOP_ERRORS,
    errors_by_level,
    first_error_level,
)
from kneepoint.analyze.knee import aggregate
from kneepoint.collect.schemas import RequestRecord


def _rec(c: int, ok: bool = True, error: str | None = None, status: int | None = None,
         abandoned: bool = False) -> RequestRecord:
    return RequestRecord(
        session_id="s", concurrency=c, started_at=0.0, total_ms=100.0, ok=ok,
        error=error, status_code=status, abandoned=abandoned,
    )


def test_levels_with_no_failures_are_kept_so_the_reader_sees_where_errors_start():
    levels = errors_by_level([_rec(1), _rec(1), _rec(4, ok=False, error="HTTP 503", status=503)])
    assert [lv.concurrency for lv in levels] == [1, 4]
    assert levels[0].failed == 0 and levels[0].error_rate == 0.0
    assert levels[0].status_codes == [] and levels[0].top_errors == []
    assert levels[1].failed == 1 and levels[1].error_rate == 1.0
    assert first_error_level(levels).concurrency == 4


def test_status_mix_and_error_strings_are_counted_most_common_first():
    recs = [_rec(7)] * 6 + [
        _rec(7, ok=False, error="HTTP 429", status=429),
        _rec(7, ok=False, error="HTTP 429", status=429),
        _rec(7, ok=False, error="HTTP 503", status=503),
        _rec(7, ok=False, error="ReadTimeout: timed out"),            # no status at all
    ]
    (lv,) = errors_by_level(recs)
    assert lv.requests == 10 and lv.failed == 4 and lv.error_rate == 0.4
    assert [(s.status, s.count) for s in lv.status_codes] == [
        ("429", 2), ("503", 1), (NO_STATUS, 1),
    ]
    assert [(e.error, e.count) for e in lv.top_errors] == [
        ("HTTP 429", 2), ("HTTP 503", 1), ("ReadTimeout: timed out", 1),
    ]


def test_the_error_tail_is_folded_into_other_not_dropped():
    recs = [_rec(3, ok=False, error=f"err-{i}") for i in range(TOP_ERRORS + 4)]
    recs += [_rec(3, ok=False, error="err-0")] * 2
    (lv,) = errors_by_level(recs)
    assert lv.top_errors[0].error == "err-0" and lv.top_errors[0].count == 3
    assert len(lv.top_errors) == TOP_ERRORS + 1
    assert lv.top_errors[-1].error == "other (4 distinct)"
    assert sum(e.count for e in lv.top_errors) == lv.failed   # the rows still add up


def test_an_all_failed_level_appears_here_even_though_aggregate_drops_it():
    """`aggregate()` has no latency to report for a level where nothing
    succeeded, so it leaves the level out. The errors table must not — that
    level is the whole story."""
    recs = [_rec(1)] * 5 + [_rec(10, ok=False, error="HTTP 500", status=500)] * 5
    assert [s.concurrency for s in aggregate(recs)] == [1]
    assert [lv.concurrency for lv in errors_by_level(recs)] == [1, 10]


def test_abandonments_are_counted_among_the_failures():
    recs = [_rec(5, ok=False, error="abandoned at the 60s wall", abandoned=True),
            _rec(5, ok=False, error="HTTP 502", status=502)]
    (lv,) = errors_by_level(recs)
    assert lv.failed == 2 and lv.abandoned == 1


def test_a_failure_without_an_error_string_still_counts():
    (lv,) = errors_by_level([_rec(2, ok=False)])
    assert lv.top_errors[0].error == "(no error message)" and lv.top_errors[0].count == 1
    assert lv.status_codes[0].status == NO_STATUS


def test_no_records_no_levels_and_no_first_error():
    assert errors_by_level([]) == []
    assert first_error_level([]) is None
    assert first_error_level(errors_by_level([_rec(1), _rec(2)])) is None
