"""The same observed-run SLO evidence for the CLI and every report rendering."""

from dataclasses import dataclass, replace
from math import isfinite

from kneepoint.analyze.cost import CostSummary
from kneepoint.analyze.knee import LevelStats
from kneepoint.analyze.resolution import ResolutionSummary
from kneepoint.slo import SloConfig, SloResult, evaluate_slo_results

SLO_SCOPE = (
    "Checks cover observed records, not a guarantee that the configured ramp completed. "
    "Latency uses the worst eligible successful-request p95 across observed levels "
    "(not pooled). An eligible-level breach fails even when another level is thin "
    "or invalid; otherwise such a level makes latency unknown. The optional "
    "thin-breach policy can also establish fail but never pass. "
    "Resolution requires verdicts for all recorded sessions. "
    "Cost per confirmed resolution covers completed generator attempts only; "
    "calibration and judging excluded."
)

# Default evidence policy: a thin level cannot establish pass or fail by itself,
# but cannot erase a breach measured at an eligible level. Set True to also let
# a measured thin-level breach establish fail; thin levels never establish pass.
THIN_LEVEL_LATENCY_BREACH_IS_CONCLUSIVE = False


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
    min_samples: int = 10,
) -> RunSloEvaluation:
    if slo is None:
        return RunSloEvaluation([], "SLO unknown: declared thresholds were not recorded.")
    def valid_p95(level: LevelStats) -> bool:
        return (level.p95_ms is not None and isfinite(level.p95_ms)
                and level.p95_ms >= 0)

    eligible = [level for level in stats
                if level.latency_eligible(min_samples) and valid_p95(level)]
    ineligible = [level for level in stats
                  if not (level.latency_eligible(min_samples) and valid_p95(level))]
    observed = [level.p95_ms for level in eligible]
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
    thin = [level for level in ineligible if not level.latency_eligible(min_samples)]
    invalid = [level for level in ineligible if level.latency_eligible(min_samples)]
    gaps = []
    if thin:
        names = ", ".join(f"c={level.concurrency} ({level.successful_n or 0} successful)"
                          for level in thin)
        gaps.append(f"Thin level(s): {names}; need at least {min_samples} successes")
    if invalid:
        gaps.append("Invalid p95 level(s): " + ", ".join(
            f"c={level.concurrency}" for level in invalid))
    gap_reason = "; ".join(gaps)
    thin_p95 = [level.p95_ms for level in thin if valid_p95(level)]
    revised = []
    for result in results:
        if result.metric != "p95_total_ms":
            revised.append(result)
        elif result.status == "fail":
            breaching = ", ".join(
                f"c={level.concurrency} (p95 {level.p95_ms:.4g} ms)"
                for level in eligible if level.p95_ms > result.threshold)
            reason = f"Eligible-level breach: {breaching}."
            revised.append(replace(
                result, reason=f"{reason} {gap_reason}." if gaps else reason))
        elif (THIN_LEVEL_LATENCY_BREACH_IS_CONCLUSIVE
              and any(value > result.threshold for value in thin_p95)):
            revised.append(replace(
                result, status="fail", measured=max([*observed, *thin_p95]),
                reason=f"Thin-level breach accepted by policy. {gap_reason}.",
            ))
        elif ineligible:
            revised.append(replace(result, status="unknown", reason=gap_reason + "."))
        else:
            revised.append(result)
    results = revised
    return RunSloEvaluation(results, None if results else "No SLO configured.")
