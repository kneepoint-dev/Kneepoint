"""Budget rail: pre-run estimate + hard runtime cap.

Imports only from cost/schemas (never from generator.ramp - ramp imports
BudgetExceeded from here, and the reverse edge would be circular).
"""

from kneepoint.analyze.cost import CostRates, request_cost
from kneepoint.collect.schemas import RequestRecord

_GROWTH_MARGIN = 1.5  # multi-turn context growth headroom on the calibration cost


class BudgetExceeded(Exception):
    def __init__(self, spend: float) -> None:
        self.spend = spend
        super().__init__(f"budget cap exceeded at ${spend:.4f}")


class CostMeter:
    """Accumulates real spend as records land; raises once the cap is crossed."""

    def __init__(self, rates: CostRates, max_spend: float | None) -> None:
        self.rates = rates
        self.max_spend = max_spend
        self.spend = 0.0

    def add(self, record: RequestRecord) -> None:
        self.spend += request_cost(record, self.rates)
        if self.max_spend is not None and self.spend > self.max_spend:
            raise BudgetExceeded(self.spend)


def estimate_run_cost(
    levels: list[int],
    hold_seconds: float,
    calibration: RequestRecord,
    rates: CostRates,
) -> float | None:
    """Project total spend from one measured request. An estimate, not a promise:
    assumes the target's latency and token counts stay calibration-like, with a
    1.5x margin for multi-turn context growth. Turn count needs no extra factor -
    a 3-turn session simply makes 3 requests within the hold window."""
    if calibration.input_tokens is None and calibration.output_tokens is None:
        return None
    per_request = request_cost(calibration, rates)
    request_seconds = max(calibration.total_ms / 1000, 0.001)
    projected_requests = sum(level * hold_seconds / request_seconds for level in levels)
    return projected_requests * per_request * _GROWTH_MARGIN
