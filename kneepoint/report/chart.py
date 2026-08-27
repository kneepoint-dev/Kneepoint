"""Knee/quality figure builders + self-contained Plotly HTML chart writer.

Every figure here obeys the same honesty rules (docs/book/reading-charts.md,
"Six ways charts mislead"):

* the sample count behind every point is **visible**, not hover-only — each
  level's x-axis tick carries its `n`, and a level left out for being too thin
  still gets a tick saying so, at its own x position, so the gap is a marked gap
  and not a missing level;
* percentiles and rates from fewer than `min_samples` observations are not
  drawn, and the title counts what was left out;
* no smoothing, y from zero;
* contaminated levels are shaded on the chart itself, not only in the banner.
"""

from pathlib import Path

import plotly.graph_objects as go

from kneepoint.analyze.knee import KneeResult, LevelStats
from kneepoint.analyze.resolution import LevelResolution

# Past this many levels, per-tick sample counts collide into an unreadable
# smear; the title states the range instead and the hover keeps each point's n.
TICK_LABEL_LIMIT = 25

CONTAMINATION_COLOR = "#b26b00"


def _plottable(stats: list[LevelStats], min_samples: int) -> list[LevelStats]:
    """Levels with enough requests to carry a percentile.

    A p95 over four requests is the fourth-largest of four, and drawing it next
    to a p95 over two hundred invites the reader to compare them. Thin levels are
    left out of the percentile traces and counted in the caption instead.
    """
    return [s for s in stats if s.n >= min_samples]


def _judged(levels: list[LevelResolution], min_samples: int) -> list[LevelResolution]:
    """Levels whose resolution rate rests on enough judged sessions to mean anything.

    A rate over three sessions can only be 0, 33, 67 or 100% — plotting it with
    the same weight as a rate over forty invents a cliff. Same threshold as the
    percentiles: a rate is a statistic too.
    """
    return [lv for lv in levels if lv.judged >= min_samples]


def _sample_ticks(entries: list[tuple[int, str, bool]], fallback: str) -> tuple[dict, str]:
    """x-axis ticks that carry each level's sample count.

    `entries` is (concurrency, count text such as "n=50", plotted) per level.
    Returns the axis keys to merge into the layout and a title fragment — empty
    when the ticks themselves say it, `fallback` when there are too many levels
    to label without the labels colliding.
    """
    if not entries:
        return {}, ""
    if len(entries) > TICK_LABEL_LIMIT:
        return {}, f"   ({fallback}; hover for each point)"
    return {
        "tickmode": "array",
        "tickvals": [c for c, _, _ in entries],
        "ticktext": [f"{c}<br>{text}" + ("" if ok else " - not plotted")
                     for c, text, ok in entries],
    }, ""


def _count_ticks(stats: list[LevelStats], min_samples: int) -> tuple[dict, str]:
    """The request-count ticks shared by every chart built from `LevelStats`."""
    plotted = [s.n for s in stats if s.n >= min_samples]
    fallback = (
        f"n = {min(plotted)}-{max(plotted)} per plotted level" if plotted
        else "no level plotted"
    )
    return _sample_ticks(
        [(s.concurrency, f"n={s.n}", s.n >= min_samples) for s in stats], fallback
    )


def _hover_data(stats: list[LevelStats]) -> list[tuple[int, str]]:
    """Per-point (n, contamination flag) for the hover — the count in words, and
    the word "contaminated" on the points the shaded band covers."""
    return [(s.n, " · contaminated" if s.contaminated else "") for s in stats]


def _mark_contamination(fig: go.Figure, stats: list[LevelStats]) -> None:
    """Shade the ramp from the first contaminated level onward.

    Contamination travels forward — a level that abandoned a request taints
    every level after it (docs/measurement-integrity.md) — so the mark is a band
    to the end of the ramp, not a dot on one level. Drawn below the traces so the
    data stays legible; labelled so nobody has to know what the colour means.
    """
    bad = sorted(s.concurrency for s in stats if s.contaminated)
    if not bad:
        return
    levels = sorted(s.concurrency for s in stats)
    first = bad[0]
    i = levels.index(first)
    x0 = (levels[i - 1] + first) / 2 if i else first - 0.5
    x1 = (levels[-1] + (levels[-1] - levels[-2]) / 2) if len(levels) > 1 else levels[-1] + 0.5
    fig.add_vrect(
        x0=x0, x1=x1, fillcolor=CONTAMINATION_COLOR, opacity=0.12, line_width=0,
        layer="below",
        annotation_text=f"contaminated from c={first}",
        annotation_position="top left",
        annotation_font={"color": CONTAMINATION_COLOR},
    )


def knee_figure(
    stats: list[LevelStats], knee: KneeResult | None, min_samples: int = 10
) -> go.Figure:
    plotted = _plottable(stats, min_samples)
    xs = [s.concurrency for s in plotted]
    custom = _hover_data(plotted)
    fig = go.Figure()
    for name, ys in (
        ("p50", [s.p50_ms for s in plotted]),
        ("p95", [s.p95_ms for s in plotted]),
        ("p99", [s.p99_ms for s in plotted]),
    ):
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines+markers", name=f"{name} latency",
            customdata=custom,
            hovertemplate="c=%{x}<br>%{y:.0f} ms<br>n=%{customdata[0]}%{customdata[1]}"
                          "<extra></extra>",
        ))
    if knee is not None and plotted:
        fig.add_vline(x=knee.concurrency, line_dash="dash", line_color="red")
        fig.add_annotation(
            x=knee.concurrency,
            y=max(s.p99_ms for s in plotted),
            text=f"knee @ {knee.concurrency}",
            showarrow=False,
            yshift=12,
        )
    _mark_contamination(fig, stats)
    dropped = len(stats) - len(plotted)
    title = "Latency vs. concurrency — the knee curve"
    if dropped:
        title += f"   ({dropped} level(s) below {min_samples} requests not plotted)"
    ticks, tick_note = _count_ticks(stats, min_samples)
    fig.update_layout(
        title=title + tick_note,
        xaxis={"title": "Concurrent sessions", **ticks},
        yaxis_title="Latency (ms)",
        yaxis_rangemode="tozero",   # a truncated y-axis invents a cliff
    )
    return fig


def drift_figure(
    stats: list[LevelStats],
    levels: list[LevelResolution],
    knee: KneeResult | None = None,
    min_samples: int = 10,
) -> go.Figure | None:
    """Quality against load, on the same x-axis as the latency that caused it.

    The product's argument in one picture: p95 climbing on the left axis while
    the share of tasks the agent actually finished falls on the right. Two axes
    because they are different units — never normalise one onto the other.
    Both traces are gated by `min_samples` — requests for the percentile,
    judged sessions for the rate. None when no level has enough judged sessions
    to put a rate on the chart, so the report can say so.
    """
    plotted = _plottable(stats, min_samples)
    judged = _judged(levels, min_samples)
    if not judged:
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[s.concurrency for s in plotted], y=[s.p95_ms for s in plotted],
        mode="lines+markers", name="p95 latency", yaxis="y",
        line={"color": "#4666d1"},
        customdata=_hover_data(plotted),
        hovertemplate="c=%{x}<br>p95 %{y:.0f} ms<br>n=%{customdata[0]}%{customdata[1]}"
                      "<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[lv.concurrency for lv in judged],
        y=[lv.resolved_rate * 100 for lv in judged],
        mode="lines+markers", name="resolved", yaxis="y2",
        line={"color": "#b3261e"},
        customdata=[lv.judged for lv in judged],
        hovertemplate="c=%{x}<br>%{y:.0f}% resolved<br>%{customdata} judged<extra></extra>",
    ))
    if knee is not None:
        fig.add_vline(x=knee.concurrency, line_dash="dash", line_color="red")
    _mark_contamination(fig, stats)
    # one tick per level on either axis, carrying both counts; a level is
    # "plotted" if either trace drew it
    by_level: dict[int, tuple[str, bool]] = {}
    for s in stats:
        by_level[s.concurrency] = (f"n={s.n}", s.n >= min_samples)
    for lv in levels:
        n_text, n_ok = by_level.get(lv.concurrency, ("", False))
        text = f"{n_text} · judged {lv.judged}" if n_text else f"judged {lv.judged}"
        by_level[lv.concurrency] = (text, n_ok or lv.judged >= min_samples)
    ticks, tick_note = _sample_ticks(
        [(c, text, ok) for c, (text, ok) in sorted(by_level.items())],
        f"judged {min(lv.judged for lv in judged)}-{max(lv.judged for lv in judged)} "
        "sessions per plotted level",
    )
    thin = len(levels) - len(judged)
    title = "Quality under load — does it get slower, or does it get wrong?"
    if thin:
        title += f"   ({thin} level(s) below {min_samples} judged sessions not plotted)"
    fig.update_layout(
        title=title + tick_note,
        xaxis={"title": "Concurrent sessions", **ticks},
        yaxis={"title": "p95 latency (ms)", "rangemode": "tozero"},
        yaxis2={"title": "Resolved (%)", "overlaying": "y", "side": "right",
                "range": [0, 105]},
        legend={"orientation": "h", "y": 1.12},
    )
    return fig


def quality_figure(
    levels: list[LevelResolution],
    knee: KneeResult | None = None,
    stats: list[LevelStats] | None = None,
    min_samples: int = 10,
) -> go.Figure | None:
    """Resolution rate alone. Gated by judged sessions per level; `stats` is only
    used to shade contaminated levels. None when no level is plottable."""
    judged = _judged(levels, min_samples)
    if not judged:
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[lv.concurrency for lv in judged],
        y=[lv.resolved_rate * 100 for lv in judged],
        mode="lines+markers", name="resolution rate",
        customdata=[lv.judged for lv in judged],
        hovertemplate="c=%{x}<br>%{y:.0f}% resolved<br>%{customdata} judged<extra></extra>",
    ))
    if knee is not None:
        fig.add_vline(x=knee.concurrency, line_dash="dash", line_color="red")
    if stats:
        _mark_contamination(fig, stats)
    thin = len(levels) - len(judged)
    title = "Resolution rate vs. concurrency — the quality curve"
    if thin:
        title += f"   ({thin} level(s) below {min_samples} judged sessions not plotted)"
    ticks, tick_note = _sample_ticks(
        [(lv.concurrency, f"judged={lv.judged}", lv.judged >= min_samples) for lv in levels],
        f"judged {min(lv.judged for lv in judged)}-{max(lv.judged for lv in judged)} "
        "sessions per plotted level",
    )
    fig.update_layout(
        title=title + tick_note,
        xaxis={"title": "Concurrent sessions", **ticks},
        yaxis_title="Resolved (%)",
        yaxis_range=[0, 105],
    )
    return fig


def ttft_tpot_figure(stats: list[LevelStats], min_samples: int = 10) -> go.Figure | None:
    """Time-to-first-token against time-per-output-token, per level.

    Two axes because they are different units and answer different questions:
    TTFT (ms) rising is queueing and prefill — the server is slow to *start*;
    TPOT (ms per token) rising is decode contention — it is slow to *continue*.
    Normalising one onto the other would hide which one moved. Levels under
    `min_samples` are left out, like the knee curve, and a level whose provider
    never reported the field is simply absent from that trace — never drawn at
    zero. None when nothing is plottable, so the report can say so.
    """
    plotted = _plottable(stats, min_samples)
    ttft = [s for s in plotted if s.ttft_p50_ms is not None]
    tpot = [s for s in plotted if s.tpot_p50_ms is not None]
    if not ttft and not tpot:
        return None
    fig = go.Figure()
    ttft_custom = _hover_data(ttft)
    for name, ys in (
        ("TTFT p50", [s.ttft_p50_ms for s in ttft]),
        ("TTFT p95", [s.ttft_p95_ms for s in ttft]),
    ):
        fig.add_trace(go.Scatter(
            x=[s.concurrency for s in ttft], y=ys, mode="lines+markers", name=name,
            yaxis="y", customdata=ttft_custom,
            hovertemplate="c=%{x}<br>" + name + " %{y:.0f} ms<br>n=%{customdata[0]}"
                          "%{customdata[1]}<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=[s.concurrency for s in tpot], y=[s.tpot_p50_ms for s in tpot],
        mode="lines+markers", name="TPOT p50", yaxis="y2",
        line={"color": "#b26b00"},
        customdata=_hover_data(tpot),
        hovertemplate="c=%{x}<br>TPOT p50 %{y:.2f} ms/tok<br>n=%{customdata[0]}"
                      "%{customdata[1]}<extra></extra>",
    ))
    _mark_contamination(fig, stats)
    dropped = len(stats) - len(plotted)
    title = "Time to first token vs. time per output token"
    if dropped:
        title += f"   ({dropped} level(s) below {min_samples} requests not plotted)"
    ticks, tick_note = _count_ticks(stats, min_samples)
    fig.update_layout(
        title=title + tick_note,
        xaxis={"title": "Concurrent sessions", **ticks},
        yaxis={"title": "TTFT (ms)", "rangemode": "tozero"},
        yaxis2={"title": "TPOT (ms / token)", "overlaying": "y", "side": "right",
                "rangemode": "tozero"},
        legend={"orientation": "h", "y": 1.12},
    )
    return fig


def itl_figure(stats: list[LevelStats], min_samples: int = 10) -> go.Figure | None:
    """Inter-token latency per level: the median gap and the worst stutter.

    `itl_p99_ms` is the p99 of each request's own p99 — the worst pause a user
    met at that level — so the two traces are not two quantiles of one bag.
    Same gating as the other charts; None when no level carries ITL (a single
    content chunk defines no gap, so non-streaming targets have none).
    """
    plotted = [
        s for s in _plottable(stats, min_samples)
        if s.itl_p50_ms is not None and s.itl_p99_ms is not None
    ]
    if not plotted:
        return None
    fig = go.Figure()
    xs = [s.concurrency for s in plotted]
    custom = _hover_data(plotted)
    for name, ys in (
        ("ITL p50", [s.itl_p50_ms for s in plotted]),
        ("ITL p99", [s.itl_p99_ms for s in plotted]),
    ):
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines+markers", name=name, customdata=custom,
            hovertemplate="c=%{x}<br>" + name + " %{y:.2f} ms<br>n=%{customdata[0]}"
                          "%{customdata[1]}<extra></extra>",
        ))
    _mark_contamination(fig, stats)
    dropped = len(stats) - len(_plottable(stats, min_samples))
    title = "Inter-token latency (per streamed chunk)"
    if dropped:
        title += f"   ({dropped} level(s) below {min_samples} requests not plotted)"
    ticks, tick_note = _count_ticks(stats, min_samples)
    fig.update_layout(
        title=title + tick_note,
        xaxis={"title": "Concurrent sessions", **ticks},
        yaxis_title="Gap between chunks (ms)",
        yaxis_rangemode="tozero",
    )
    return fig


def write_knee_chart(
    stats: list[LevelStats], knee: KneeResult | None, path: Path, min_samples: int = 10
) -> Path:
    fig = knee_figure(stats, knee, min_samples=min_samples)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(path, include_plotlyjs=True)
    return path
