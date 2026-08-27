"""`kneepoint report`: rebuild the HTML from what a run left on disk.

The records JSONL is the handle; the sessions JSONL and the metadata sidecar are
found beside it by name (docs/output-format.md, "The files"). Everything the
report shows is recomputed from those files with the same functions the run used,
so a run whose report failed to render — or was deleted — is not lost, and so a
report can be regenerated after the renderer improves.

What the sidecar does not say, this module does not guess (standing rule 1).
Files written before the sidecar existed re-render with every unstored field as
*unknown*, and the cost and resilience sections say why they are empty.
"""

from dataclasses import dataclass, field
from pathlib import Path

from kneepoint.analyze.cost import CostRates, CostSummary, summarize_cost
from kneepoint.analyze.errors import errors_by_level
from kneepoint.analyze.knee import KneeReport, aggregate, find_knee, knee_report
from kneepoint.analyze.resilience import resilience
from kneepoint.analyze.resolution import resolution_by_level
from kneepoint.analyze.retry import retry_by_level
from kneepoint.collect.schemas import RequestRecord, RunMetadata, SessionRecord
from kneepoint.collect.writer import read_jsonl, read_run_meta
from kneepoint.report.html import UNKNOWN, RunMeta, format_started, write_report

# `run` and `report` share this default. When the sidecar is missing the run's own
# threshold is unknown, so the report says which threshold *it* applied.
DEFAULT_MIN_SAMPLES = 10


class ReportInputError(ValueError):
    """The path given is not a records JSONL this command can re-render from."""


@dataclass
class RerenderResult:
    report_path: Path
    records_path: Path
    sessions_path: Path
    meta_path: Path
    meta: RunMetadata | None
    min_samples: int
    knee_summary: KneeReport
    cost: CostSummary | None
    unknown: list[str] = field(default_factory=list)   # what could not be recovered, and why


def sibling_paths(records_path: Path) -> tuple[Path, Path, Path]:
    """`<prefix>-<id>.jsonl` → its `-sessions.jsonl`, `-meta.json`, `-report.html`."""
    stem = records_path.stem
    if stem.endswith("-sessions"):
        raise ReportInputError(
            f"{records_path.name} is the sessions file; pass the records file "
            f"({stem[: -len('-sessions')]}.jsonl) and the sessions are found beside it."
        )
    if records_path.suffix != ".jsonl":
        raise ReportInputError(
            f"{records_path.name} is not a .jsonl records file "
            "(the metadata sidecar and the report are derived from it, not the other way)."
        )
    return (
        records_path.with_name(f"{stem}-sessions.jsonl"),
        records_path.with_name(f"{stem}-meta.json"),
        records_path.with_name(f"{stem}-report.html"),
    )


def render_from_files(
    records_path: Path,
    *,
    out: Path | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    min_samples: int | None = None,
) -> RerenderResult:
    """Re-aggregate and re-render one run. Overrides win over the sidecar; the
    sidecar wins over defaults; and where neither exists the report says *unknown*.
    """
    sessions_path, meta_path, default_out = sibling_paths(records_path)
    if not records_path.exists():
        raise ReportInputError(f"{records_path} does not exist.")
    records: list[RequestRecord] = read_jsonl(records_path)
    if not records:
        raise ReportInputError(f"{records_path} holds no request records.")

    unknown: list[str] = []
    sessions: list[SessionRecord] = []
    if sessions_path.exists():
        sessions = read_jsonl(sessions_path, SessionRecord)
    else:
        unknown.append(
            f"sessions file {sessions_path.name} not found — resolution, retry counts "
            "per session, cost per resolved task and resilience cannot be recomputed"
        )
    meta = read_run_meta(meta_path) if meta_path.exists() else None
    if meta is None:
        unknown.append(
            f"metadata sidecar {meta_path.name} not found (runs made before it existed "
            "have none) — target, model, ramp, hold, seed, chaos profile, price rates and "
            "thresholds are unknown, not defaulted"
        )

    if min_samples is None:
        min_samples = meta.min_samples if meta is not None else DEFAULT_MIN_SAMPLES
        if meta is None:
            unknown.append(
                f"min_samples: the run's own is unknown; this report applies "
                f"{DEFAULT_MIN_SAMPLES} (pass --min-samples to change it)"
            )

    # Cost: explicit rates > the sidecar's rates > nothing, with the reason shown.
    rates = None
    if price_in is not None or price_out is not None:
        rates = CostRates(input_per_mtok=price_in or 0.0, output_per_mtok=price_out or 0.0)
    elif meta is not None and meta.price is not None:
        rates = CostRates(
            input_per_mtok=meta.price.input_per_mtok, output_per_mtok=meta.price.output_per_mtok
        )
    cost = summarize_cost(records, sessions, rates) if rates is not None else None
    cost_note = None
    if cost is None:
        cost_note = (
            "Cost not measured (the run had no price rates)." if meta is not None else
            "Cost not re-rendered: the metadata sidecar is missing, so the price rates "
            "this run used are unknown. Pass --price-in / --price-out to recompute cost "
            "from the stored token counts."
        )

    # Resilience: only the sidecar can say whether chaos ran and which min_group
    # the run scored with. Without it, the fault records are shown as a fact and
    # the score is not computed.
    res = None
    res_note = None
    if meta is not None:
        if meta.chaos.profile != "off" and not sessions_path.exists():
            res_note = (
                f"Resilience not re-rendered: sessions file {sessions_path.name} not "
                "found, and faults are attributed per session."
            )
        elif meta.chaos.profile != "off":
            res = resilience(sessions, min_group=meta.min_group)
    else:
        faulted = sum(1 for s in sessions if s.faults)
        res_note = (
            "Resilience not re-rendered: the metadata sidecar is missing, so whether "
            "chaos ran and the min_group threshold this run scored with are unknown"
            + (f" ({faulted} of {len(sessions)} stored sessions carry fault records)."
               if faulted else ".")
        )

    quality = resolution_by_level(sessions)
    quality_note = (
        f"Sessions file {sessions_path.name} not found — resolution cannot be recomputed."
        if not sessions_path.exists() else None
    )

    stats = aggregate(records)
    knee_summary = knee_report(stats, min_samples=min_samples)
    knee = find_knee(stats, min_samples=min_samples)
    if meta is not None:
        header = RunMeta(
            target=meta.target, model=meta.model, started=format_started(meta.started_at),
            ramp=f"{meta.ramp.start}..{meta.ramp.stop} step {meta.ramp.step}",
            chaos=meta.chaos.profile,
            total_requests=len(records), total_sessions=len(sessions),
        )
    else:
        # the header line labels model/ramp/chaos itself; target and start do not
        # carry a label, so they say what is unknown
        header = RunMeta(
            target=f"target {UNKNOWN}", model=UNKNOWN, started=f"start time {UNKNOWN}",
            ramp=UNKNOWN, chaos=UNKNOWN,
            total_requests=len(records), total_sessions=len(sessions),
        )
    report_path = write_report(
        out if out is not None else default_out,
        meta=header, stats=stats, knee=knee, knee_summary=knee_summary,
        quality=quality, cost=cost, res=res,
        retry_levels=retry_by_level(records, sessions), min_samples=min_samples,
        quality_note=quality_note, cost_note=cost_note, res_note=res_note,
        error_levels=errors_by_level(records), run_meta=meta,
        meta_note=(
            None if meta is not None else
            f"Run metadata not recorded: the sidecar {meta_path.name} was not found "
            "(runs made before it existed have none), so hold, seed, versions and "
            f"thresholds are unknown. This report applied min_samples {min_samples}."
        ),
    )
    return RerenderResult(
        report_path=report_path, records_path=records_path, sessions_path=sessions_path,
        meta_path=meta_path, meta=meta, min_samples=min_samples,
        knee_summary=knee_summary, cost=cost, unknown=unknown,
    )
