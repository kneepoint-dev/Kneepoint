"""Report v1: one self-contained HTML file — knee curve, cost card, resilience grid."""

import time
from pathlib import Path

from jinja2 import Environment, PackageLoader
from pydantic import BaseModel

from kneepoint.analyze.cost import CostSummary
from kneepoint.analyze.errors import ErrorLevel, first_error_level
from kneepoint.analyze.knee import KneeReport, KneeResult, LevelStats
from kneepoint.analyze.resilience import ResilienceSummary
from kneepoint.analyze.resolution import LevelResolution
from kneepoint.analyze.retry import RetryLevel, worst_amplification
from kneepoint.collect.schemas import RunMetadata
from kneepoint.report.chart import (
    drift_figure,
    itl_figure,
    knee_figure,
    quality_figure,
    ttft_tpot_figure,
)

# The value the header shows for anything the run did not record. Never a guess
# and never a default dressed up as a fact (docs/output-format.md, "Reading old
# files").
UNKNOWN = "unknown"


class RunMeta(BaseModel):
    target: str
    model: str
    started: str
    ramp: str
    chaos: str
    total_requests: int
    total_sessions: int


def format_started(started_at: float) -> str:
    """The header's human-readable start time, from the sidecar's epoch float.

    One function so `run`, `demo` and `report` cannot format the same instant
    three ways — the re-rendered report has to match the original byte for byte.
    Local time, like the run's own console output.
    """
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(started_at))


def _duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {rest:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def metadata_rows(meta: RunMetadata) -> list[tuple[str, str]]:
    """The run-metadata block, one labelled line per sidecar field.

    Everything here is read from the `RunMetadata` the run wrote (or `kneepoint
    report` read back), never re-derived — so the block and the sidecar cannot
    disagree. Machine notes have no field yet and are listed as *not recorded*
    rather than omitted, so a reader knows the gap is the tool's, not the run's.
    """
    faults = (
        ", ".join(f"{f.type} p={f.probability:g}" for f in meta.chaos.faults)
        if meta.chaos.faults else "none"
    )
    price = (
        f"${meta.price.input_per_mtok:g} in / ${meta.price.output_per_mtok:g} out per Mtok"
        + (f", cap ${meta.price.max_spend:g}" if meta.price.max_spend is not None else "")
        if meta.price is not None else "not tracked"
    )
    turns = (
        f"{meta.turns.min}" if meta.turns.min == meta.turns.max
        else f"{meta.turns.min}–{meta.turns.max}"
    )
    return [
        ("Run", f"{meta.run_id} (kneepoint {meta.command})"),
        ("Target", f"{meta.target} · model {meta.model}"),
        ("Ramp", f"{meta.ramp.start}..{meta.ramp.stop} step {meta.ramp.step}, "
                f"{meta.hold_seconds:g} s hold per level"),
        ("Sessions", f"{turns} turn(s) each, up to {meta.retry.max_attempts} attempt(s) "
                     f"per turn with {meta.retry.backoff_s:g} s backoff"),
        ("Seed", str(meta.seed)),
        ("Chaos", f"{meta.chaos.profile} — {faults}"),
        ("Prices", price),
        ("Thresholds", f"min_samples {meta.min_samples} (percentiles and knee), "
                       f"min_group {meta.min_group} (resilience grid)"),
        ("Started", format_started(meta.started_at)),
        ("Duration", _duration(meta.finished_at - meta.started_at)),
        ("Versions", f"kneepoint {meta.kneepoint_version or UNKNOWN + ' (source tree)'} · "
                     f"python {meta.environment.python} · schema {meta.schema_version}"),
        ("Platform", meta.environment.platform),
        ("Machine notes", "not recorded"),
    ]


def _env() -> Environment:
    # Unconditionally on. `select_autoescape(["html"])` matches on the file
    # *extension*, and the template is `report.html.j2` — so for a long time
    # every `{{ }}` was interpolated raw and a `<script>` in a provider's error
    # body, the target URL or the sidecar's platform string would have run in
    # the reader's browser. The only markup the template should trust is the
    # Plotly divs, and those are marked `| safe` by hand.
    return Environment(
        loader=PackageLoader("kneepoint.report", "templates"),
        autoescape=True,
    )


def write_report(
    path: Path,
    *,
    meta: RunMeta,
    stats: list[LevelStats],
    knee: KneeResult | None,
    quality: list[LevelResolution],
    cost: CostSummary | None,
    res: ResilienceSummary | None,
    knee_summary: KneeReport | None = None,
    retry_levels: list[RetryLevel] | None = None,
    min_samples: int = 10,
    quality_note: str | None = None,
    cost_note: str | None = None,
    res_note: str | None = None,
    error_levels: list[ErrorLevel] | None = None,
    run_meta: RunMetadata | None = None,
    meta_note: str | None = None,
) -> Path:
    """Render the report. The three `*_note` strings replace a section's default
    "not measured" placeholder with the real reason it is empty — `kneepoint
    report` uses them to say *why* cost or resilience could not be recomputed
    from what is on disk, rather than implying the run never measured them.

    `run_meta` is the sidecar model as written (or read back) — the metadata
    block renders it field by field. None renders the block as *not recorded*,
    with `meta_note` saying why when the caller knows."""
    # Fixed div ids: Plotly would otherwise mint a fresh UUID per figure, and the
    # same run rendered twice would differ for no reason anyone can read.
    knee_div = knee_figure(stats, knee, min_samples=min_samples).to_html(
        full_html=False, include_plotlyjs=True, div_id="knee-curve"
    )
    # Both quality figures are None when no level has `min_samples` judged
    # sessions — the template then says which levels were judged and how thinly,
    # rather than showing the "not measured" placeholder for something that was.
    quality_fig = quality_figure(quality, knee, stats=stats, min_samples=min_samples)
    quality_div = (
        quality_fig.to_html(full_html=False, include_plotlyjs=False, div_id="quality-curve")
        if quality_fig is not None else None
    )
    drift_fig = drift_figure(stats, quality, knee, min_samples=min_samples)
    drift_div = (
        drift_fig.to_html(full_html=False, include_plotlyjs=False, div_id="quality-under-load")
        if drift_fig is not None else None
    )
    ttft_fig = ttft_tpot_figure(stats, min_samples=min_samples)
    ttft_div = (
        ttft_fig.to_html(full_html=False, include_plotlyjs=False, div_id="ttft-tpot")
        if ttft_fig is not None else None
    )
    itl_fig = itl_figure(stats, min_samples=min_samples)
    itl_div = (
        itl_fig.to_html(full_html=False, include_plotlyjs=False, div_id="itl")
        if itl_fig is not None else None
    )
    contaminated = [s for s in stats if s.contaminated]
    quality_thin_note = None
    if quality and drift_fig is None:
        judged = ", ".join(f"c={lv.concurrency} {lv.judged}" for lv in quality)
        quality_thin_note = (
            f"Resolution was judged at {len(quality)} level(s), but none reached "
            f"{min_samples} judged sessions, so no rate is plotted — a rate over a "
            f"handful of sessions can only take a handful of values. Judged per level: "
            f"{judged}. Lengthen the hold, or re-render with a lower --min-samples and "
            f"say so."
        )
    retry_levels = retry_levels or []
    error_levels = error_levels or []
    html = _env().get_template("report.html.j2").render(
        meta=meta, knee=knee, knee_div=knee_div, quality_div=quality_div,
        drift_div=drift_div, ttft_div=ttft_div, itl_div=itl_div,
        cost=cost, res=res, knee_summary=knee_summary,
        contaminated=contaminated,
        abandoned_total=sum(s.abandoned for s in stats),
        stats=stats, min_samples=min_samples, quality=quality,
        latency_rows=[s for s in stats if s.ttft_p50_ms is not None],
        retry_levels=retry_levels,
        worst_retry=worst_amplification(retry_levels),
        error_levels=error_levels,
        first_error=first_error_level(error_levels),
        total_failed=sum(lv.failed for lv in error_levels),
        quality_note=quality_note, quality_thin_note=quality_thin_note,
        cost_note=cost_note, res_note=res_note,
        metadata_rows=metadata_rows(run_meta) if run_meta is not None else None,
        meta_note=meta_note,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
