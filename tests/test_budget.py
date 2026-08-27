import pytest

from kneepoint.analyze.budget import BudgetExceeded, CostMeter, estimate_run_cost
from kneepoint.analyze.cost import CostRates
from kneepoint.collect.schemas import RequestRecord

RATES = CostRates(input_per_mtok=3.0, output_per_mtok=15.0)


def _rec(ms=1000.0, input_tokens=1000, output_tokens=1000) -> RequestRecord:
    return RequestRecord(
        session_id="s", concurrency=1, started_at=0.0, total_ms=ms,
        input_tokens=input_tokens, output_tokens=output_tokens, ok=True,
    )


def test_meter_accumulates_and_raises_at_cap():
    meter = CostMeter(RATES, max_spend=0.05)          # each record costs $0.018
    meter.add(_rec())
    meter.add(_rec())
    assert round(meter.spend, 6) == 0.036
    with pytest.raises(BudgetExceeded):
        meter.add(_rec())                              # 0.054 > 0.05


def test_meter_without_cap_never_raises():
    meter = CostMeter(RATES, max_spend=None)
    for _ in range(1000):
        meter.add(_rec())
    assert meter.spend > 0


def test_estimate_projects_requests_per_level():
    # calibration: 1s/request, $0.018/request; levels [1,2]; hold 10s
    # -> level 1: 10 requests, level 2: 20 requests -> 30 x $0.018 x 1.5 = $0.81
    estimate = estimate_run_cost([1, 2], 10.0, _rec(ms=1000.0), RATES)
    assert estimate == pytest.approx(0.81)


def test_estimate_none_without_token_counts():
    rec = _rec(input_tokens=None, output_tokens=None)
    assert estimate_run_cost([1, 2], 10.0, rec, RATES) is None
