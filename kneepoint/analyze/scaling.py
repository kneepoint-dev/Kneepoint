"""Descriptive scaling metrics for the successful request throughput curve."""

import math
import statistics
from typing import Literal

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
    cpu_idle_pct: float | None
    busy_cores: float | None
    successes: int
    flags: list[str]


class ScalingReport(BaseModel):
    max_observed_rps: float | None
    x_max_rps: float | None
    plateau_status: Literal["reached", "not reached"]
    plateau_noise_band: float
    baseline_rps: list[float]
    plateau_levels: list[int]
    r0_s: float | None
    n_star: float | None
    levels: list[ScalingLevel]
    local_resource_samples: list[ResourceSample] | None

    def lines(self) -> list[str]:
        def metric(value):
            return f"{value:.3f}" if value is not None else "n/a"

        lines = [f"Max observed throughput: {metric(self.max_observed_rps)} requests/s",
                 f"Plateau noise band: {self.plateau_noise_band:.2%} (sizing estimate)"]
        if self.plateau_levels:
            lines.append(f"Observed plateau at c={', '.join(map(str, self.plateau_levels))}; "
                         f"X_max={metric(self.x_max_rps)} requests/s; N*={metric(self.n_star)}")
        else:
            lines.append("Plateau not reached in the tested range; "
                         "X_max=not reached; N*=not reached")
        for row in self.levels:
            lines.extend(row.flags)
            if self.local_resource_samples is not None:
                lines.append(f"  c={row.concurrency}: local CPU idle={metric(row.cpu_idle_pct)}%; "
                             f"busy cores={metric(row.busy_cores)}")
        return lines


def scaling_report(
    stats: list[LevelStats], *, min_samples: int = 10,
    resources: list[ResourceSample] | None = None,
    baseline_rps: list[float] | None = None,
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
    peak = max(usable.values(), default=None)
    baselines = list(baseline_rps or [])
    if any(not math.isfinite(x) or x <= 0 for x in baselines):
        raise ValueError("Comparison c=1 rates must be finite and positive")
    if x1 is not None:
        baselines.insert(0, x1)
    spread = (max(baselines) / min(baselines) - 1) if len(baselines) > 1 else 0.0
    band = max(PLATEAU_TOLERANCE, spread)
    baseline = next((s for s in ordered if s.concurrency == 1 and not s.contaminated), None)
    p50 = baseline.p50_ms if baseline and baseline.latency_eligible(min_samples) else None
    r0 = p50 / 1000 if p50 is not None and 0 < p50 < math.inf else None

    # Require the actual final two levels, not the last two surviving filters.
    # A flat tail below a previous peak is a regression, not peak capacity.
    plateau = []
    if len(ordered) >= 2:
        previous, last = (usable.get(s.concurrency) for s in ordered[-2:])
        if previous and last and abs(last / previous - 1) < band and not math.isclose(
            abs(last / previous - 1), band,
        ):
            for s in reversed(ordered):
                if peak is None or usable.get(s.concurrency, 0) < (1 - band) * peak:
                    break
                plateau.append(s.concurrency)
    plateau = list(reversed(plateau)) if len(plateau) > 1 else []
    x_max = peak if plateau else None

    idle_by_level: dict[int, list[float]] = {}
    busy_by_level: dict[int, list[float]] = {}
    for sample in resources or []:
        idle_by_level.setdefault(sample.concurrency, []).append(sample.cpu_idle_pct)
        if sample.vcpu_count is not None:
            busy_by_level.setdefault(sample.concurrency, []).append(
                (1 - sample.cpu_idle_pct / 100) * sample.vcpu_count,
            )

    rows = []
    for s in ordered:
        c = s.concurrency
        efficiency = usable[c] / (c * x1) if x1 and c in usable else None
        little_n = s.little_n if c in measured else None
        error = abs(little_n / c - 1) if little_n is not None else None
        flags = []
        idle_samples = idle_by_level.get(c)
        idle = statistics.mean(idle_samples) if idle_samples else None
        busy_samples = busy_by_level.get(c)
        busy = statistics.mean(busy_samples) if busy_samples else None
        evidence = (f"c={c}, E={efficiency:.3f}, " if efficiency is not None else f"c={c}, ")
        evidence += f"local CPU idle={idle:.2f}%" if idle is not None else "local CPU idle=unknown"
        evidence += f", n={s.scaling_successes} successful requests"
        if busy is not None:
            evidence += f", busy cores={busy:.3f}"
        if efficiency is not None and efficiency < 0.8 and idle is not None:
            if idle >= IDLE_THRESHOLD and not (s.injector and s.injector.injector_limited):
                flags.append(f"Hypothesis: software limit likely ({evidence})")
        if c == 2 and efficiency is not None and r0 and s.p50_ms is not None:
            ratio = s.p50_ms / (1000 * r0)
            if 0.95 <= 2 * efficiency <= 1.05 and 1.8 <= ratio <= 2.2:
                flags.append(f"Hypothesis: single-slot saturation likely ({evidence}, "
                             f"p50/R0={ratio:.3f})")
        rows.append(ScalingLevel(
            concurrency=c, window=s.scaling_window if c in measured else None,
            throughput_rps=measured.get(c), efficiency=efficiency,
            little_n=little_n, little_relative_error=error,
            little_off_by_more_than_5_pct=(
                error > LAW_TOLERANCE and not math.isclose(error, LAW_TOLERANCE)
                if error is not None else None
            ),
            cpu_idle_pct=idle, busy_cores=busy, successes=s.scaling_successes, flags=flags,
        ))
    return ScalingReport(
        max_observed_rps=peak, x_max_rps=x_max,
        plateau_status="reached" if plateau else "not reached",
        plateau_noise_band=band, baseline_rps=baselines, plateau_levels=plateau,
        r0_s=r0, n_star=x_max * r0 if x_max is not None and r0 is not None else None,
        levels=rows, local_resource_samples=resources,
    )
