"""Budget tools: estimates, legacy meter, and cooperative observed-spend control.

Imports only from cost/schemas (never from generator.ramp - ramp imports
BudgetExceeded from here, and the reverse edge would be circular).
"""

import asyncio
import math
from decimal import Decimal

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



class ObservedSpendController:
    """Observe completed generator attempts and stop further admission cooperatively.

    Pass ``stop`` and ``on_attempt_complete`` to ``run_ramp``. Active attempts
    continue and must still be observed after stop. One instance covers one run;
    the synchronous callback must run on the generator's event loop. This is an
    observed threshold, not a guaranteed billing cap. Calibration, judging, and
    attempts without a completed RequestRecord are outside this accounting.
    """

    def __init__(self, rates: CostRates, max_spend: float | None = None) -> None:
        prices = (rates.input_per_mtok, rates.output_per_mtok)
        for name, value in zip(("input_per_mtok", "output_per_mtok", "max_spend"),
                               (*prices, max_spend), strict=True):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
        # Decimal avoids missing a cap at equality through binary float accumulation.
        self._prices = tuple(Decimal(str(price)) for price in prices)
        self._cap = None if max_spend is None else Decimal(str(max_spend))
        self._max_spend = max_spend
        self.stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._seen: dict[tuple[str, int, int, int], dict] = {}
        self._known_spend = Decimal(0)
        self._complete = 0
        self._partial = 0
        self._missing = 0
        self._input_known = 0
        self._output_known = 0
        if max_spend == 0:
            self._request_stop("cap_reached")

    def _request_stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        self.stop.set()

    def on_attempt_complete(self, record: RequestRecord) -> None:
        """Charge each identity once, including retries and failed attempts.

        Identical replay is harmless; a different payload for the same
        (session_id, concurrency, turn_index, attempt) raises ValueError.
        Missing either usage count makes the completed-attempt total unknown,
        even at a zero price; known portions still contribute to known_spend.
        """
        key = (record.session_id, record.concurrency, record.turn_index, record.attempt)
        payload = record.model_dump()
        if key in self._seen:
            if self._seen[key] != payload:
                raise ValueError(f"conflicting completion for attempt {key!r}")
            return
        usage = (record.input_tokens, record.output_tokens)
        if any(tokens is not None and tokens < 0 for tokens in usage):
            raise ValueError("token counts must be nonnegative or None")
        charge = sum((tokens * price / 1_000_000
                      for tokens, price in zip(usage, self._prices, strict=True)
                      if tokens is not None), Decimal(0))
        updated_spend = self._known_spend + charge
        if not math.isfinite(updated_spend):
            raise ValueError("observed spend must remain finite")
        self._seen[key] = payload
        self._known_spend = updated_spend
        known = sum(tokens is not None for tokens in usage)
        self._input_known += record.input_tokens is not None
        self._output_known += record.output_tokens is not None
        self._complete += known == 2
        self._partial += known == 1
        self._missing += known == 0
        if self._max_spend is not None:
            # Unknown usage takes precedence if this completion also reaches the cap.
            if known < 2:
                self._request_stop("unknown_usage")
            elif self._known_spend >= self._cap:
                self._request_stop("cap_reached")

    def snapshot(self) -> dict:
        """Return detached accounting for completed attempts observed so far.

        known_spend/known_overshoot are lower bounds when usage is missing.
        No finite billing upper bound can be inferred: active, interrupted, or
        unobserved upstream work may still incur charges, even after a stop.
        """
        return {
            "scope": "completed_generator_attempts",
            "max_spend": self._max_spend,
            "known_spend": float(self._known_spend),
            "total_spend": None if self._partial or self._missing else float(self._known_spend),
            "known_overshoot": (None if self._cap is None else
                                float(max(Decimal(0), self._known_spend - self._cap))),
            "billing_upper_bound": None,
            "stop_requested": self.stop.is_set(),
            "stop_reason": self._stop_reason,
            "coverage": {
                "completed_attempts": len(self._seen),
                "complete_usage": self._complete,
                "partial_usage": self._partial,
                "missing_usage": self._missing,
                "input_known": self._input_known,
                "output_known": self._output_known,
            },
        }


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
