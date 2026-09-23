"""SLO declarations and primitive tri-state evaluation, independent of run schemas."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SloConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p95_total_ms: float | None = Field(None, ge=0, allow_inf_nan=False)
    min_resolution_rate: float | None = Field(None, ge=0, le=1, allow_inf_nan=False)
    max_cost_per_resolved: float | None = Field(None, ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class SloResult:
    metric: str
    threshold: float
    measured: float | None
    status: Literal["pass", "fail", "unknown"]
    reason: str

    def line(self) -> str:
        measured = f"{self.measured:.4g}" if self.measured is not None else "unknown"
        return (f"SLO {self.status} - {self.metric}: measured {measured} vs limit "
                f"{self.threshold:.4g}; {self.reason}")


def evaluate_slo_results(
    slo: SloConfig,
    *,
    p95_ms: float | None,
    resolution: float | None,
    cost_per_resolved: float | None,
    unknown_reasons: dict[str, str] | None = None,
) -> list[SloResult]:
    """Return one result per configured check; equality passes, missing data is unknown."""
    results = []
    for metric, measured, minimum in (
        ("p95_total_ms", p95_ms, False),
        ("min_resolution_rate", resolution, True),
        ("max_cost_per_resolved", cost_per_resolved, False),
    ):
        threshold = getattr(slo, metric)
        if threshold is None:
            continue
        if measured is None:
            status = "unknown"
            reason = (unknown_reasons or {}).get(metric, "Not measured in this run.")
        elif not isfinite(measured) or measured < 0 or (minimum and measured > 1):
            measured = None
            status = "unknown"
            reason = "Measurement is non-finite or outside the metric's valid range."
        else:
            passed = measured >= threshold if minimum else measured <= threshold
            status = "pass" if passed else "fail"
            reason = "Threshold met." if passed else "Threshold not met."
        results.append(SloResult(metric, threshold, measured, status, reason))
    return results
