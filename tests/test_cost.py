from kneepoint.analyze.cost import CostRates, CostSummary, request_cost, summarize_cost
from kneepoint.collect.schemas import RequestRecord, SessionRecord

RATES = CostRates(input_per_mtok=3.0, output_per_mtok=15.0)


def _rec(input_tokens=1000, output_tokens=1000, ok=True) -> RequestRecord:
    return RequestRecord(
        session_id="s", concurrency=1, started_at=0.0, total_ms=1.0,
        input_tokens=input_tokens, output_tokens=output_tokens, ok=ok,
    )


def _sess(resolved: bool | None) -> SessionRecord:
    s = SessionRecord(
        session_id="s", concurrency=1, started_at=0.0, total_ms=1.0,
        turns_requested=1, turns_completed=1, ok=True,
    )
    s.resolved = resolved
    return s


def test_request_cost_math():
    # 1000 in @ $3/MTok = $0.003; 1000 out @ $15/MTok = $0.015
    assert request_cost(_rec(), RATES) == 0.018


def test_request_cost_handles_missing_tokens():
    assert request_cost(_rec(input_tokens=None, output_tokens=None), RATES) == 0.0


def test_summary_splits_waste_and_computes_per_resolved():
    records = [_rec(), _rec(ok=False), _rec()]           # $0.018 x3, one wasted
    sessions = [_sess(True), _sess(False), _sess(None)]  # 1 resolved of 2 judged
    summary = summarize_cost(records, sessions, RATES)
    assert isinstance(summary, CostSummary)
    assert round(summary.total_spend, 6) == 0.054
    assert round(summary.waste_spend, 6) == 0.018
    assert round(summary.retry_waste_pct, 4) == round(1 / 3, 4)
    assert summary.sessions == 3
    assert summary.judged_sessions == 2
    assert summary.resolved_sessions == 1
    assert round(summary.cost_per_session, 6) == 0.018
    assert round(summary.cost_per_resolved, 6) == 0.054


def test_summary_none_safe_when_nothing_resolved():
    summary = summarize_cost([_rec()], [_sess(False)], RATES)
    assert summary.cost_per_resolved is None
    empty = summarize_cost([], [], RATES)
    assert empty.retry_waste_pct == 0.0
    assert empty.cost_per_session is None
