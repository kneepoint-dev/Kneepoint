"""Descriptive scaling metrics for the successful request throughput curve."""

import math
import statistics

from pydantic import BaseModel

from kneepoint.analyze.knee import LevelStats
from kneepoint.collect.schemas import ResourceSample

LAW_TOLERANCE = 0.05
PLATEAU_TOLERANCE = 0.02
IDLE_THRESHOLD = 80.0


class ScalingLevel(BaseModel):
    concurrency: int
    window: str | None
    throughput_rps: float | None
    efficiency: float | None
    little_n: float | None
    little_relative_error: float | None
    little_off_by_more_than_5_pct: bool | None
    flags: list[str]


class ScalingReport(BaseModel):
    x_max_rps: float | None
    plateau_levels: list[int]
    r0_s: float | None
    n_star: float | None
    levels: list[ScalingLevel]
    local_resource_samples: list[ResourceSample] | None


def scaling_report(
    stats: list[LevelStats], *, min_samples: int = 10,
    resources: list[ResourceSample] | None = None,
) -> ScalingReport:
    ordered = sorted(stats, key=lambda level: level.concurrency)
    measured = {
        s.concurrency: s.scaling_throughput_rps for s in ordered
        if not s.contaminated and s.scaling_throughput_rps is not None
        and math.isfinite(s.scaling_throughput_rps)
    }
    usable = {
        s.concurrency: measured[s.concurrency] for s in ordered
        if s.concurrency in measured and measured[s.concurrency] > 0
        and s.scaling_successes >= max(1, min_samples)
    }
    x1 = usable.get(1)
    x_max = max(usable.values(), default=None)
    baseline = next((s for s in ordered if s.concurrency == 1 and not s.contaminated), None)
    p50 = baseline.p50_ms if baseline and baseline.latency_eligible(min_samples) else None
    r0 = p50 / 1000 if p50 is not None and 0 < p50 < math.inf else None

    # The high-load end of the measured curve must stay within 2% of its peak.
    plateau = []
    for s in reversed(ordered):
        if x_max is None or usable.get(s.concurrency, 0) < (1 - PLATEAU_TOLERANCE) * x_max:
            break
        plateau.append(s.concurrency)

    idle_by_level: dict[int, list[float]] = {}
    for sample in resources or []:
        idle_by_level.setdefault(sample.concurrency, []).append(sample.cpu_idle_pct)

    rows = []
    for s in ordered:
        c = s.concurrency
        efficiency = usable[c] / (c * x1) if x1 and c in usable else None
        little_n = s.little_n if c in measured else None
        error = abs(little_n / c - 1) if little_n is not None else None
        flags = []
        idle = idle_by_level.get(c)
        if efficiency is not None and efficiency < 0.8 and idle:
            if statistics.mean(idle) >= IDLE_THRESHOLD and not (
                s.injector and s.injector.injector_limited
            ):
                flags.append("software limit likely")
        if c == 2 and efficiency is not None and r0 and s.p50_ms is not None:
            if 0.95 <= 2 * efficiency <= 1.05 and 1.8 <= s.p50_ms / (1000 * r0) <= 2.2:
                flags.append("single-slot saturation likely")
        rows.append(ScalingLevel(
            concurrency=c, window=s.scaling_window if c in measured else None,
            throughput_rps=measured.get(c), efficiency=efficiency,
            little_n=little_n, little_relative_error=error,
            little_off_by_more_than_5_pct=(
                error > LAW_TOLERANCE and not math.isclose(error, LAW_TOLERANCE)
                if error is not None else None
            ),
            flags=flags,
        ))
    return ScalingReport(
        x_max_rps=x_max, plateau_levels=list(reversed(plateau)) if len(plateau) > 1 else [],
        r0_s=r0, n_star=x_max * r0 if x_max is not None and r0 is not None else None,
        levels=rows, local_resource_samples=resources,
    )
