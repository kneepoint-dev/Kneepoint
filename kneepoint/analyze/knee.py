"""Knee-point detection: Kneedle on the p95 curve, heuristic cross-check.

Two methods, both always reported. Run D showed why: three repetitions of one
scenario under identical conditions gave p95-doubling knees 3/2/2, because the
deciding p95 ratios were 1.85 / 2.48 / 2.63 — straddling a hard 2.0 threshold.
A third repetition printed Kneedle's 5 where p95-doubling said 2, and the user
was never told a second method disagreed.

So: a level whose ratio sits within ±15% of the threshold is a coin flip, and
the knee is reported as a *range with low confidence* rather than a number that
looks measured. Kneedle landing on the top of the ramp is a non-answer (it does
that on any near-linear curve) and is refused rather than reported. p95-doubling
crossing only at the top of the ramp is a *lower bound* — nothing above it was
measured, so a gentle curve that merely reached 2× is indistinguishable from a
break — and is reported as "at least N, low confidence", never as a bare number.
`None` stays a first-class result throughout — "undetectable" is a finding, not
a failure.
"""

import statistics
import warnings

from kneed import KneeLocator
from pydantic import BaseModel

from kneepoint.collect.schemas import RequestRecord

# the p95-doubling heuristic (docs/metrics.md): the knee is the first level
# whose p95 reaches this multiple of the curve's floor
KNEE_RATIO = 2.0
# a ratio this close to the threshold flips between repetitions on run noise
# alone — measured on Run D, where 1.85 and 2.48 came from the same scenario
CONFIDENCE_BAND = 0.15


class LevelStats(BaseModel):
    concurrency: int
    n: int
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    abandoned: int = 0
    # True once this level, or any level before it, abandoned a request. Work the
    # client walked away from can keep running on the server, so from that point
    # on the ramp levels are no longer independent measurements (Run D finding 4).
    contaminated: bool = False
    # Latency breakdown. Every one is None rather than 0 when the underlying
    # field was never reported — no provider usage means no TPOT, a single
    # content chunk means no ITL. See docs/output-format.md.
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    tpot_p50_ms: float | None = None
    itl_p50_ms: float | None = None
    itl_p99_ms: float | None = None


class KneeResult(BaseModel):
    concurrency: int
    method: str  # "kneedle" | "p95_doubling"


class LevelRatio(BaseModel):
    """One level's p95 against the curve's floor — the number the heuristic decides on."""

    concurrency: int
    p95_ms: float
    ratio: float
    near_threshold: bool  # within CONFIDENCE_BAND of KNEE_RATIO: could go either way


class MethodVerdict(BaseModel):
    """What one method concluded, including when it concluded nothing and why."""

    method: str
    concurrency: int | None
    note: str | None = None


class KneeReport(BaseModel):
    """Both methods, the ratios they decided on, and how much to trust the answer."""

    min_samples: int
    usable_levels: int
    kneedle: MethodVerdict
    p95_doubling: MethodVerdict
    agree: bool | None          # None when fewer than two methods produced a number
    knee: int | None            # p95-doubling's crossing: the headline, when there is one
    knee_range: tuple[int, int] | None  # every level the crossing could be under run noise
    confidence: str | None      # "high" | "low" | None when there is no knee
    # True when p95 first doubled at the last usable level. Nothing above it was
    # measured, so the knee is *there or beyond* - a slow linear climb reaches
    # 2x eventually without ever breaking - and the headline says so.
    lower_bound: bool = False
    ratios: list[LevelRatio]
    contaminated_levels: list[int]
    notes: list[str]

    @property
    def recommended(self) -> int | None:
        """The one integer to act on: the low end of the range.

        Anything that must pick a single number — an SLO gate, a capacity
        recommendation, the chart marker — takes this. A capacity number errs
        low: running under the knee costs headroom, running over it costs
        everyone's latency at once.
        """
        return None if self.knee_range is None else self.knee_range[0]

    @property
    def headline(self) -> str:
        if self.knee is None:
            return "No clear knee found."
        low, high = self.knee_range
        if self.lower_bound:
            where = f"{low}-{high}" if low != high else str(low)
            return (f"Knee point: {where} concurrent sessions or beyond, low confidence "
                    f"(plan for {low}; p95 first doubled at the top of the ramp - "
                    f"widen it past {high} to find the knee)")
        if low != high:
            return (f"Knee point: {low}-{high} concurrent sessions, low confidence "
                    f"(plan for {low})")
        return (f"Knee point: {low} concurrent sessions "
                f"(p95_doubling, {self.confidence} confidence)")

    def lines(self) -> list[str]:
        """The headline plus everything needed to argue with it."""
        out = [self.headline]
        for verdict in (self.kneedle, self.p95_doubling):
            value = "none" if verdict.concurrency is None else str(verdict.concurrency)
            note = f" - {verdict.note}" if verdict.note else ""
            out.append(f"  {verdict.method}: {value}{note}")
        if self.ratios:
            shown = ", ".join(
                f"c={r.concurrency} {r.ratio:.2f}x{'*' if r.near_threshold else ''}"
                for r in self.ratios
            )
            out.append(f"  p95 vs curve floor: {shown}"
                       + ("   (* within 15% of the 2.0 threshold)"
                          if any(r.near_threshold for r in self.ratios) else ""))
        out.extend(f"  {note}" for note in self.notes)
        return out


def _pct(values: list[float], p: int) -> float:
    if len(values) == 1:
        return values[0]
    cuts = statistics.quantiles(sorted(values), n=100, method="inclusive")
    return cuts[p - 1]


def aggregate(records: list[RequestRecord]) -> list[LevelStats]:
    """Group records by ramp level; percentiles use successful requests only.
    Levels where every request failed carry no latency signal and are dropped.

    Contamination travels forward: a level that abandoned a request — and every
    level after it — is flagged, because the abandoned generation can still be
    holding a slot on the server while the next level is being measured. Dropped
    levels still contaminate the ones that follow.
    """
    by_level: dict[int, list[RequestRecord]] = {}
    for record in records:
        by_level.setdefault(record.concurrency, []).append(record)
    stats: list[LevelStats] = []
    seen_abandonment = False
    for level in sorted(by_level):
        recs = by_level[level]
        abandoned = sum(1 for r in recs if r.abandoned)
        seen_abandonment = seen_abandonment or abandoned > 0
        ok = [r for r in recs if r.ok]
        ok_latencies = [r.total_ms for r in ok]
        if not ok_latencies:
            continue

        def pct(values: list[float], p: int) -> float | None:
            """None, not 0, when the field was never reported at this level."""
            return _pct(values, p) if values else None

        ttfts = [r.ttft_ms for r in ok if r.ttft_ms is not None]
        tpots = [t for t in (r.tpot_ms for r in ok) if t is not None]
        itls = [r.itl_mean_ms for r in ok if r.itl_mean_ms is not None]
        itl_tails = [r.itl_p99_ms for r in ok if r.itl_p99_ms is not None]
        stats.append(
            LevelStats(
                concurrency=level,
                n=len(recs),
                error_rate=1 - len(ok_latencies) / len(recs),
                p50_ms=_pct(ok_latencies, 50),
                p95_ms=_pct(ok_latencies, 95),
                p99_ms=_pct(ok_latencies, 99),
                abandoned=abandoned,
                contaminated=seen_abandonment,
                ttft_p50_ms=pct(ttfts, 50),
                ttft_p95_ms=pct(ttfts, 95),
                tpot_p50_ms=pct(tpots, 50),
                itl_p50_ms=pct(itls, 50),
                # p99 of the per-request p99s: the worst stutter a user met at
                # this level, not a p99 over a pooled bag of gaps
                itl_p99_ms=pct(itl_tails, 99),
            )
        )
    return stats


def _kneedle(usable: list[LevelStats]) -> MethodVerdict:
    """Kneedle's max-curvature knee, refusing its one systematic non-answer.

    On a near-linear p95 curve Kneedle returns the last point of the ramp — it
    did so on both Run D scouts (12 on a 1..12 ramp, 16 on a 1..16). That is not
    a knee, it is "I ran out of data", and reporting it as a capacity number is
    how a ramp's own upper bound gets published as a finding.
    """
    xs = [s.concurrency for s in usable]
    ys = [s.p95_ms for s in usable]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # kneed warns loudly when there is no knee
        locator = KneeLocator(xs, ys, curve="convex", direction="increasing")
    if locator.knee is None:
        return MethodVerdict(method="kneedle", concurrency=None,
                             note="no point of maximum curvature on this curve")
    knee = int(locator.knee)
    if knee == xs[-1]:
        return MethodVerdict(
            method="kneedle", concurrency=None,
            note=(f"returned {knee}, the top of the ramp - what it does on a "
                  "near-linear curve. Widen the ramp rather than believing it"),
        )
    return MethodVerdict(method="kneedle", concurrency=knee)


def _ratios(usable: list[LevelStats]) -> list[LevelRatio]:
    floor = min(s.p95_ms for s in usable)
    out = []
    for s in usable:
        ratio = s.p95_ms / floor if floor else float("inf")
        out.append(LevelRatio(
            concurrency=s.concurrency, p95_ms=s.p95_ms, ratio=ratio,
            near_threshold=abs(ratio - KNEE_RATIO) <= KNEE_RATIO * CONFIDENCE_BAND,
        ))
    return out


def _p95_doubling(ratios: list[LevelRatio]) -> MethodVerdict:
    """The first level whose p95 reaches 2x the floor, with one honesty caveat.

    When that level is the last one measured, the crossing is real but the knee
    is not located: every level below it stayed under 2x, and nothing above it
    exists to show whether the curve broke there or is still climbing gently (a
    linear curve reaches 2x eventually without a knee at all). The number is
    kept, because "not below N" is a measurement, but it is marked as a bound
    so `knee_report` cannot headline it as a found knee.
    """
    crossing = next((r for r in ratios if r.ratio >= KNEE_RATIO), None)
    if crossing is None:
        top = max(r.ratio for r in ratios)
        return MethodVerdict(
            method="p95_doubling", concurrency=None,
            note=f"p95 never doubles across this ramp (peak {top:.2f}x the floor)",
        )
    if crossing is ratios[-1]:
        return MethodVerdict(
            method="p95_doubling", concurrency=crossing.concurrency,
            note=(f"first doubled at {crossing.concurrency}, the top of the ramp - the knee "
                  "is there or beyond, and the ramp was too short to tell which. Widen it"),
        )
    return MethodVerdict(method="p95_doubling", concurrency=crossing.concurrency)


def _at_ramp_top(doubling: MethodVerdict, ratios: list[LevelRatio]) -> bool:
    return doubling.concurrency is not None and doubling.concurrency == ratios[-1].concurrency


def knee_report(stats: list[LevelStats], min_samples: int = 10) -> KneeReport:
    """Both methods on the p95-vs-concurrency curve, with a confidence signal.

    The knee is only reported as a single number when nothing plausible competes
    with it: the two methods agree (or only one of them answered at all) and no
    level's ratio sits close enough to the 2.0 threshold to flip on run noise.
    Otherwise it comes back as a range labelled low confidence, which is what
    Run D's 3/2/2 sequence actually was.
    """
    usable = [s for s in stats if s.n >= min_samples]
    contaminated = [s.concurrency for s in usable if s.contaminated]
    if len(usable) < 3:
        return KneeReport(
            min_samples=min_samples, usable_levels=len(usable),
            kneedle=MethodVerdict(method="kneedle", concurrency=None,
                                  note="not enough levels to fit a curve"),
            p95_doubling=MethodVerdict(method="p95_doubling", concurrency=None,
                                       note="not enough levels to fit a curve"),
            agree=None, knee=None, knee_range=None, confidence=None, ratios=[],
            contaminated_levels=contaminated,
            notes=[f"fewer than 3 levels have at least {min_samples} requests - "
                   "no knee is claimed"],
        )

    ratios = _ratios(usable)
    kneedle = _kneedle(usable)
    doubling = _p95_doubling(ratios)
    notes: list[str] = []

    agree = None
    disputed = False
    if kneedle.concurrency is not None and doubling.concurrency is not None:
        agree = kneedle.concurrency == doubling.concurrency
        disputed = not agree
        if disputed:
            notes.append(
                f"kneedle disagrees, reading {kneedle.concurrency} against p95-doubling's "
                f"{doubling.concurrency}. Kneedle answers on 7 of Run D's 15 repetitions "
                "and reads higher on every one of them, so it corroborates the headline "
                "or it doesn't - it never replaces it"
            )
    elif kneedle.concurrency is not None and doubling.concurrency is None:
        # Kneedle finding curvature where p95 never doubled is not a knee: by
        # kneepoint's stated definition nothing broke. It gets said, not scored.
        notes.append(
            f"kneedle sees curvature at c={kneedle.concurrency}, but {doubling.note} - "
            "no knee is claimed. Widen the ramp if you need to find one"
        )

    # The range is the crossing's own uncertainty: which level the 2.0 threshold
    # could land on under run noise. Kneedle's disagreement is a different kind of
    # doubt — it lowers confidence and gets its own note, but it does not widen the
    # range, or one unreliable method would smear every answer across the ramp.
    knee = doubling.concurrency
    if knee is None:
        return KneeReport(
            min_samples=min_samples, usable_levels=len(usable),
            kneedle=kneedle, p95_doubling=doubling, agree=agree,
            knee=None, knee_range=None, confidence=None, ratios=ratios,
            contaminated_levels=contaminated,
            notes=notes + ([] if kneedle.concurrency is not None else [
                "neither method finds a knee - the curve never breaks from its "
                "floor. Widen the ramp or hold longer"
            ]),
        )

    candidates = {knee}
    crossing_ratio = next(r for r in ratios if r.concurrency == knee)
    candidates |= {r.concurrency for r in ratios if r.near_threshold and r.concurrency < knee}
    if crossing_ratio.near_threshold:
        # it barely crossed; had it not, the next level would have been the knee
        later = [r.concurrency for r in ratios if r.concurrency > knee]
        if later:
            candidates.add(later[0])
        notes.append(
            f"the deciding ratio at c={knee} is {crossing_ratio.ratio:.2f}x, within 15% "
            "of the 2.0 threshold - a repetition of this run can land either side of it"
        )

    knee_range = (min(candidates), max(candidates))
    confidence = "high" if knee_range[0] == knee_range[1] and not disputed else "low"
    lower_bound = _at_ramp_top(doubling, ratios)
    if lower_bound:
        # The same non-answer Kneedle is refused for, on the method that leads.
        # It is not refused outright because "p95 stayed under 2x through c=N-1"
        # is a real finding - it is just a floor on the knee, not the knee.
        confidence = "low"
        notes.append(
            f"p95 first doubled at c={knee}, the last level measured - no level above "
            "it exists to confirm a break rather than a gentle climb. Treat this as a "
            f"lower bound and widen the ramp past {knee}"
        )
    if contaminated:
        confidence = "low"
        notes.append(
            f"levels {', '.join(str(c) for c in contaminated)} are contaminated by "
            "abandoned work - this knee is not publishable whatever its confidence"
        )
    return KneeReport(
        min_samples=min_samples, usable_levels=len(usable),
        kneedle=kneedle, p95_doubling=doubling, agree=agree,
        knee=knee, knee_range=knee_range, confidence=confidence,
        lower_bound=lower_bound, ratios=ratios,
        contaminated_levels=contaminated, notes=notes,
    )


def find_knee(stats: list[LevelStats], min_samples: int = 10) -> KneeResult | None:
    """The one integer, or None. `knee_report` is the surface humans should read.

    Always `p95_doubling` now: it is kneepoint's stated definition (docs/metrics.md),
    it is hand-checkable from raw JSONL, and it is what every published kneepoint
    figure is derived with. Kneedle answered on 7 of Run D's 15 repetitions and
    read higher on every one — so the old kneedle-first precedence made the CLI
    headline contradict that definition on nearly half of real runs.

    Returns the *conservative* end of the confidence range, because callers that
    want one integer are sizing something. It carries no confidence signal; for
    anything a person reads, call `knee_report`.
    """
    report = knee_report(stats, min_samples=min_samples)
    if report.recommended is None:
        return None
    return KneeResult(concurrency=report.recommended, method="p95_doubling")
