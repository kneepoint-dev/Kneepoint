"""The same observed-run SLO evidence for the CLI and every report rendering."""

from dataclasses import dataclass, replace
from math import isfinite

from kneepoint.analyze.cost import CostSummary
from kneepoint.analyze.knee import LevelStats
from kneepoint.analyze.resolution import ResolutionSummary
from kneepoint.slo import SloConfig, SloResult, evaluate_slo_results

SLO_SCOPE = (
    "Checks cover observed records, not a guarantee that the configured ramp completed. "
    "Latency is the worst successful-request p95 across observed levels (not pooled). "
    "Resolution requires verdicts for all recorded sessions. "
    "Cost per confirmed resolution covers completed generator attempts only; "
    "calibration and judging excluded."
)


@dataclass(frozen=True)
class RunSloEvaluation:
    results: list[SloResult]
    note: str | None = None

    @property
    def nonpassing(self) -> bool:
        return any(result.status != "pass" for result in self.results)

    def lines(self) -> list[str]:
        if self.note:
            return [self.note]
        return [SLO_SCOPE, *(result.line() for result in self.results)]


def evaluate_run_slo(
    slo: SloConfig | None,
    *,
    stats: list[LevelStats],
    quality: ResolutionSummary | None,
    cost: CostSummary | None,
    cost_note: str | None = None,
) -> RunSloEvaluation:
    if slo is None:
        return RunSloEvaluation([], "SLO unknown: declared thresholds were not recorded.")
    observed = [level.p95_ms for level in stats
                if level.p95_ms is not None and isfinite(level.p95_ms) and level.p95_ms >= 0]
    p95 = max(observed, default=None)
    quality_reason = (
        "Session evidence unavailable." if quality is None else
        f"Incomplete verdict coverage ({quality.judged}/{quality.total} evaluated); "
        "the whole-run threshold cannot be verified."
    )
    cost_reason = (
        "No confirmed resolved sessions." if cost is not None else
        cost_note if cost_note and cost_note.startswith("Total spend unknown:") else
        "Price rates unavailable; cost cannot be measured."
    )
    results = evaluate_slo_results(
        slo, p95_ms=p95, resolution=quality.slo_resolution if quality else None,
        cost_per_resolved=cost.cost_per_resolved if cost else None,
        unknown_reasons={
            "p95_total_ms": "No valid successful-request p95 was observed.",
            "min_resolution_rate": quality_reason,
            "max_cost_per_resolved": cost_reason,
        },
    )
    # A measured breach is conclusive; passing only the known levels is not.
    if len(observed) < len(stats):
        results = [
            replace(result, status="unknown", reason=(
                f"Successful-request p95 unavailable or invalid at {len(stats) - len(observed)}"
                f"/{len(stats)} observed levels; the threshold cannot be verified."
            )) if result.metric == "p95_total_ms" and result.status == "pass" else result
            for result in results
        ]
    return RunSloEvaluation(results, None if results else "No SLO configured.")
