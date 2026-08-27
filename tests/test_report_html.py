from kneepoint.analyze.cost import CostSummary
from kneepoint.analyze.errors import errors_by_level
from kneepoint.analyze.knee import KneeResult, LevelStats
from kneepoint.analyze.resilience import FaultRow, ResilienceSummary
from kneepoint.analyze.resolution import LevelResolution
from kneepoint.analyze.retry import retry_by_level
from kneepoint.chaos.faults import FaultSpec
from kneepoint.collect.schemas import (
    ChaosShape,
    Environment,
    PriceRates,
    RampShape,
    RequestRecord,
    RetryShape,
    RunMetadata,
    TurnsShape,
)
from kneepoint.report.chart import drift_figure, knee_figure, quality_figure
from kneepoint.report.html import RunMeta, metadata_rows, write_report


def _stats():
    return [
        LevelStats(concurrency=c, n=50, error_rate=0.0,
                   p50_ms=v * 0.6, p95_ms=v, p99_ms=v * 1.2)
        for c, v in [(1, 1000.0), (5, 1100.0), (10, 2500.0)]
    ]


def _meta():
    return RunMeta(target="http://x/v1", model="mock", started="2026-08-01 10:00",
                   ramp="1..10 step 5", chaos="standard", total_requests=150, total_sessions=60)


def _cost():
    return CostSummary(total_spend=1.23, waste_spend=0.10, retry_waste_pct=0.081,
                       sessions=60, judged_sessions=60, resolved_sessions=55,
                       cost_per_session=0.0205, cost_per_resolved=0.0224)


def _res():
    return ResilienceSummary(
        clean_sessions=40, faulted_sessions=20, clean_resolution_rate=1.0,
        faulted_resolution_rate=0.85, score=85.0,
        rows=[FaultRow(fault="llm_rate_limit", sessions_hit=12, resolved_rate=1.0, verdict="pass"),
              FaultRow(fault="tool_timeout", sessions_hit=8, resolved_rate=0.62, verdict="fail")],
    )


def test_full_report_is_selfcontained_and_complete(tmp_path):
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=_stats(),
        knee=KneeResult(concurrency=5, method="kneedle"),
        quality=[LevelResolution(concurrency=1, judged=20, resolved_rate=1.0),
                 LevelResolution(concurrency=10, judged=20, resolved_rate=0.8)],
        cost=_cost(), res=_res(),
    )
    html = out.read_text(encoding="utf-8")
    assert "plotly" in html.lower()
    assert "knee @ 5" in html
    assert "$0.0224" in html                       # cost per resolved, the headline
    assert "8.1%" in html                          # retry waste
    assert "85" in html and "llm_rate_limit" in html and "tool_timeout" in html
    assert "fail" in html
    # truly self-contained: no external script/style tags, plotly.js inlined.
    # (raw 'src="http' / 'cdn.plot.ly' substring checks are wrong here — both appear
    # as string literals INSIDE the inlined plotly.js bundle; and a failing not-in
    # assert makes pytest difflib-diff the 4.8MB page, which spins for minutes)
    has_external_refs = '<script src="http' in html or "<link" in html
    assert not has_external_refs
    assert len(html) > 1_000_000  # the plotly bundle is embedded, not referenced


def test_report_renders_placeholders_when_sections_missing(tmp_path):
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
    )
    html = out.read_text(encoding="utf-8")
    assert "No clear knee" in html
    assert "not measured" in html


def test_report_carries_the_latency_breakdown_and_says_what_it_measured(tmp_path):
    stats = _stats()
    for s in stats:
        s.ttft_p50_ms, s.ttft_p95_ms = 150.0, 400.0
        s.tpot_p50_ms, s.itl_p50_ms, s.itl_p99_ms = 9.7, 15.4, 628.0
    stats[-1].tpot_p50_ms = None            # provider sent no usage at this level
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=stats, knee=None,
        quality=[], cost=None, res=None, min_samples=10,
    )
    html = out.read_text(encoding="utf-8")
    assert "Latency breakdown" in html
    assert "15.40 ms" in html and "628.00 ms" in html
    assert "n/a" in html                    # missing TPOT is n/a, never 0.00
    assert "per streamed chunk, not per token" in html


def test_report_shows_retry_amplification_and_names_the_confound(tmp_path):
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
        retry_levels=retry_by_level(
            [RequestRecord(session_id="s", concurrency=7, started_at=0.0,
                           total_ms=1.0, ok=False, attempt=a)
             for a in (1, 2, 3)],
            [],
        ),
    )
    html = out.read_text(encoding="utf-8")
    assert "Retry amplification" in html
    assert "3.00×" in html
    assert "manufacture the knee it measures" in html


def test_report_says_so_when_no_retry_fired(tmp_path):
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
        retry_levels=retry_by_level(
            [RequestRecord(session_id="s", concurrency=1, started_at=0.0,
                           total_ms=1.0, ok=True)],
            [],
        ),
    )
    assert "No retries fired" in out.read_text(encoding="utf-8")


def test_thin_levels_are_left_out_of_the_percentile_chart(tmp_path):
    """A p95 over four requests next to a p95 over two hundred invites a
    comparison the data cannot support."""
    stats = _stats()
    stats[1].n = 4
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=stats, knee=None,
        quality=[], cost=None, res=None, min_samples=10,
    )
    assert "1 level(s) below 10 requests not plotted" in out.read_text(encoding="utf-8")


def test_the_drift_chart_puts_quality_on_the_latency_axis(tmp_path):
    out = write_report(
        tmp_path / "report.html", meta=_meta(), stats=_stats(),
        knee=KneeResult(concurrency=5, method="p95_doubling"),
        quality=[LevelResolution(concurrency=1, judged=20, resolved_rate=1.0),
                 LevelResolution(concurrency=10, judged=20, resolved_rate=0.8)],
        cost=None, res=None,
    )
    html = out.read_text(encoding="utf-8")
    assert "Quality under load" in html
    assert "no amount of hardware fixes a wrong" in html


def test_same_inputs_render_identical_bytes(tmp_path):
    """Plotly mints a random div id per figure unless told otherwise; the report
    pins them so `kneepoint report` can reproduce a run's HTML byte for byte."""
    kwargs = dict(
        meta=_meta(), stats=_stats(), knee=KneeResult(concurrency=5, method="kneedle"),
        quality=[LevelResolution(concurrency=1, judged=20, resolved_rate=1.0)],
        cost=_cost(), res=_res(),
    )
    first = write_report(tmp_path / "a.html", **kwargs).read_bytes()
    second = write_report(tmp_path / "b.html", **kwargs).read_bytes()
    assert first == second
    assert b'id="knee-curve"' in first and b'id="quality-under-load"' in first
    assert b'id="quality-curve"' in first


def test_section_notes_replace_the_default_placeholders(tmp_path):
    html = write_report(
        tmp_path / "notes.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
        quality_note="Q reason", cost_note="C reason", res_note="R reason",
    ).read_text(encoding="utf-8")
    assert "Q reason" in html and "C reason" in html and "R reason" in html
    assert "Resolution not measured" not in html
    assert "Cost not measured" not in html and "Resilience not measured" not in html
    plain = write_report(
        tmp_path / "plain.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8")
    assert "Cost not measured" in plain and "Resilience not measured" in plain


def _run_meta(**overrides) -> RunMetadata:
    base = dict(
        run_id="20260821-170000", command="run", target="http://x/v1", model="mock",
        ramp=RampShape(start=1, stop=10, step=5), hold_seconds=15.0,
        turns=TurnsShape(min=1, max=2), retry=RetryShape(max_attempts=3, backoff_s=0.5),
        seed=7, chaos=ChaosShape(profile="standard", faults=[
            FaultSpec(type="llm_rate_limit", probability=0.02),
            FaultSpec(type="tool_timeout", probability=0.05),
        ]),
        price=PriceRates(input_per_mtok=3.0, output_per_mtok=15.0, max_spend=2.5),
        min_samples=10, min_group=10, started_at=1_000_000.0, finished_at=1_000_135.0,
        environment=Environment(python="3.14.6", platform="macOS-26-arm64"),
        schema_version=1, kneepoint_version="0.1.0",
    )
    base.update(overrides)
    return RunMetadata(**base)


def _streamed_stats():
    stats = _stats()
    for s in stats:
        s.ttft_p50_ms, s.ttft_p95_ms = 150.0, 400.0
        s.tpot_p50_ms, s.itl_p50_ms, s.itl_p99_ms = 9.7, 15.4, 628.0
    return stats


def test_errors_section_shows_the_rate_the_status_mix_and_the_strings(tmp_path):
    recs = [RequestRecord(session_id="s", concurrency=1, started_at=0.0, total_ms=1.0,
                          ok=True)] * 10
    recs += [RequestRecord(session_id="s", concurrency=10, started_at=0.0, total_ms=1.0, ok=False,
                           error="HTTP 429", status_code=429)] * 3
    recs += [RequestRecord(session_id="s", concurrency=10, started_at=0.0, total_ms=1.0, ok=False,
                           error="ReadTimeout: timed out")]
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None, error_levels=errors_by_level(recs),
    ).read_text(encoding="utf-8")
    assert "<h2>Errors</h2>" in html
    assert "4 of 14" in html and "first appear at concurrency 10" in html
    assert "capacity signal" in html
    assert "<code>429</code>&nbsp;×3" in html and "<code>no code</code>&nbsp;×1" in html
    assert "HTTP 429" in html and "ReadTimeout: timed out" in html
    assert "100.0%" in html          # the all-failed level's rate, shown not dropped


def test_errors_from_the_first_level_are_called_a_configuration_problem(tmp_path):
    recs = [RequestRecord(session_id="s", concurrency=1, started_at=0.0, total_ms=1.0, ok=False,
                          error="HTTP 401", status_code=401)]
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None, error_levels=errors_by_level(recs),
    ).read_text(encoding="utf-8")
    assert "present from the first level" in html and "configuration" in html


def test_errors_section_says_so_when_nothing_failed(tmp_path):
    recs = [RequestRecord(session_id="s", concurrency=c, started_at=0.0, total_ms=1.0, ok=True)
            for c in (1, 1, 5)]
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None, error_levels=errors_by_level(recs),
    ).read_text(encoding="utf-8")
    assert "No request failed at any level" in html and "3 requests" in html


def test_ttft_tpot_and_itl_charts_render_with_fixed_ids(tmp_path):
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=_streamed_stats(), knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8")
    assert 'id="ttft-tpot"' in html and 'id="itl"' in html
    assert "Time to first token vs. time per output token" in html
    assert "Inter-token latency (per streamed chunk)" in html
    assert "No TTFT or TPOT to chart" not in html
    assert "No inter-token latency recorded" not in html


def test_ttft_tpot_and_itl_say_why_they_are_empty_for_a_non_streaming_target(tmp_path):
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8")
    assert 'id="ttft-tpot"' not in html and 'id="itl"' not in html
    assert "No TTFT or TPOT to chart" in html
    assert "No inter-token latency recorded" in html


def test_thin_levels_are_left_out_of_the_ttft_and_itl_charts_too(tmp_path):
    stats = _streamed_stats()
    stats[1].n = 4
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=stats, knee=None,
        quality=[], cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8")
    # knee curve, TTFT/TPOT and ITL each carry the caption — three, not one
    assert html.count("1 level(s) below 10 requests not plotted") == 3


def test_metadata_block_renders_every_sidecar_field_and_names_what_is_not_recorded(tmp_path):
    html = write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None, run_meta=_run_meta(),
    ).read_text(encoding="utf-8")
    assert "<h2>Run metadata</h2>" in html
    for needle in (
        "20260821-170000 (kneepoint run)", "1..10 step 5, 15 s hold per level",
        "1–2 turn(s) each, up to 3 attempt(s) per turn with 0.5 s backoff",
        "standard — llm_rate_limit p=0.02, tool_timeout p=0.05",
        "$3 in / $15 out per Mtok, cap $2.5",
        "min_samples 10 (percentiles and knee), min_group 10 (resilience grid)",
        "2 min 15 s", "kneepoint 0.1.0 · python 3.14.6 · schema 1", "macOS-26-arm64",
        "Machine notes", "not recorded",
    ):
        assert needle in html, needle
    assert "Run metadata not recorded" not in html


def test_metadata_block_handles_off_chaos_no_prices_and_an_unstamped_sidecar():
    rows = dict(metadata_rows(_run_meta(
        chaos=ChaosShape(profile="off"), price=None, kneepoint_version=None,
        turns=TurnsShape(min=2, max=2), finished_at=1_000_042.0,
    )))
    assert rows["Chaos"] == "off — none"
    assert rows["Prices"] == "not tracked"
    assert rows["Sessions"].startswith("2 turn(s) each")
    assert rows["Duration"] == "42 s"
    assert rows["Versions"].startswith("kneepoint unknown (source tree)")
    assert rows["Seed"] == "7"


def test_metadata_block_is_a_placeholder_without_a_sidecar(tmp_path):
    plain = write_report(
        tmp_path / "plain.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8")
    assert "Run metadata not recorded" in plain
    noted = write_report(
        tmp_path / "noted.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None, meta_note="M reason",
    ).read_text(encoding="utf-8")
    assert "M reason" in noted and "Run metadata not recorded" not in noted


def test_same_inputs_with_metadata_and_errors_still_render_identical_bytes(tmp_path):
    recs = [RequestRecord(session_id="s", concurrency=1, started_at=0.0, total_ms=1.0, ok=False,
                          error="HTTP 500", status_code=500)]
    kwargs = dict(
        meta=_meta(), stats=_streamed_stats(), knee=None, quality=[], cost=None, res=None,
        error_levels=errors_by_level(recs), run_meta=_run_meta(),
    )
    first = write_report(tmp_path / "a.html", **kwargs).read_bytes()
    second = write_report(tmp_path / "b.html", **kwargs).read_bytes()
    assert first == second


# --- honest-visualization rules, enforced per chart ---------------------------


def _unescape(html: str) -> str:
    """Plotly JSON-escapes `<`/`>` inside the figure; the tests read tick labels
    as written."""
    return html.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u00b7", "·")


def _contaminated_stats():
    stats = _stats()
    stats[1].abandoned, stats[1].contaminated = 2, True
    stats[2].contaminated = True
    return stats


def test_every_chart_carries_the_sample_count_on_its_axis_not_only_in_hover(tmp_path):
    """docs/book/reading-charts.md: check the n on every point. A hover is not
    visible; the tick under each level is."""
    stats = _streamed_stats()
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=stats, knee=None,
        quality=[LevelResolution(concurrency=1, judged=20, resolved_rate=1.0),
                 LevelResolution(concurrency=10, judged=12, resolved_rate=0.8)],
        cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8"))
    # knee curve, TTFT/TPOT and ITL: one request-count tick per level
    assert html.count('"1<br>n=50"') == 3 and html.count('"10<br>n=50"') == 3
    # quality curve: judged sessions; quality under load: both counts on one tick
    assert '"10<br>judged=12"' in html
    assert '"10<br>n=50 · judged 12"' in html


def test_a_thin_level_keeps_its_tick_and_says_it_was_not_plotted(tmp_path):
    """The Book's Foundations chapter promises a gap *and* a marker — the level stays on the
    axis at its own x, labelled, so a gap is never mistaken for a missing level."""
    stats = _streamed_stats()
    stats[1].n = 4
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=stats, knee=None,
        quality=[], cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8"))
    assert html.count('"5<br>n=4 - not plotted"') == 3   # knee, TTFT/TPOT, ITL
    assert '"5<br>n=5"' not in html


def test_resolution_rates_below_min_samples_judged_sessions_are_not_plotted(tmp_path):
    levels = [LevelResolution(concurrency=1, judged=20, resolved_rate=1.0),
              LevelResolution(concurrency=5, judged=3, resolved_rate=0.33),
              LevelResolution(concurrency=10, judged=20, resolved_rate=0.8)]
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=levels, cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8"))
    # both quality charts count the thin level in their title...
    assert html.count("1 level(s) below 10 judged sessions not plotted") == 2
    # ...keep it on the axis, labelled...
    assert '"5<br>judged=3 - not plotted"' in html
    assert '"5<br>n=50 · judged 3"' in html    # p95 plotted (n=50), rate not
    # ...and do not draw the 33% point
    fig = quality_figure(levels, stats=_stats(), min_samples=10)
    assert list(fig.data[0].y) == [100.0, 80.0]
    drift = drift_figure(_stats(), levels, min_samples=10)
    assert list(drift.data[1].x) == [1, 10]


def test_quality_sections_say_why_when_every_judged_level_is_too_thin(tmp_path):
    levels = [LevelResolution(concurrency=1, judged=4, resolved_rate=1.0),
              LevelResolution(concurrency=10, judged=3, resolved_rate=0.67)]
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=levels, cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8"))
    assert 'id="quality-curve"' not in html and 'id="quality-under-load"' not in html
    assert html.count("judged at 2 level(s), but none reached 10 judged sessions") == 2
    assert "c=1 4, c=10 3" in html
    assert "Resolution not measured" not in html      # it *was* measured


def test_contaminated_levels_are_shaded_on_every_chart_not_only_in_the_banner(tmp_path):
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=_contaminated_stats(), knee=None,
        quality=[LevelResolution(concurrency=1, judged=20, resolved_rate=1.0),
                 LevelResolution(concurrency=10, judged=20, resolved_rate=0.8)],
        cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8"))
    stats = _contaminated_stats()
    for s in stats:
        s.ttft_p50_ms, s.ttft_p95_ms = 150.0, 400.0
        s.tpot_p50_ms, s.itl_p50_ms, s.itl_p99_ms = 9.7, 15.4, 628.0
    html = _unescape(write_report(
        tmp_path / "r2.html", meta=_meta(), stats=stats, knee=None,
        quality=[LevelResolution(concurrency=1, judged=20, resolved_rate=1.0),
                 LevelResolution(concurrency=10, judged=20, resolved_rate=0.8)],
        cost=None, res=None, min_samples=10,
    ).read_text(encoding="utf-8"))
    # banner, plus one shaded band per chart: knee, drift, quality, TTFT/TPOT, ITL
    assert "Contaminated levels: 5, 10." in html
    assert html.count("contaminated from c=5") == 5
    # the band starts between the last clean level and the first contaminated one
    assert '"x0":3.0' in html
    assert "shades the ramp from" in html
    # and the hover names it on the contaminated points only
    hover = [tuple(row) for row in knee_figure(stats, None).data[1].customdata]
    assert hover == [(50, ""), (50, " · contaminated"), (50, " · contaminated")]


def test_clean_runs_draw_no_contamination_band(tmp_path):
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8"))
    assert "contaminated from" not in html


def test_too_many_levels_fall_back_to_a_stated_n_range_instead_of_colliding_ticks(tmp_path):
    stats = [LevelStats(concurrency=c, n=30 + c, error_rate=0.0,
                        p50_ms=100.0, p95_ms=200.0, p99_ms=300.0) for c in range(1, 41)]
    html = _unescape(write_report(
        tmp_path / "r.html", meta=_meta(), stats=stats, knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8"))
    assert "n = 31-70 per plotted level; hover for each point" in html
    assert knee_figure(stats, None).layout.xaxis.ticktext is None


# --- everything the template interpolates is escaped ----------------------------
#
# `select_autoescape(["html"])` matches on the file extension, and the template
# is `report.html.j2`, so for a long time the predicate returned False and every
# `{{ }}` was raw. Strings that come from outside Kneepoint — a provider's error
# body, the target URL, the model name, the platform string — must not become
# markup in the reader's browser.

HOSTILE = "<script>alert('kneepoint')</script>"


def _hostile_report(tmp_path) -> str:
    recs = [RequestRecord(session_id="s", concurrency=1, started_at=0.0, total_ms=1.0,
                          ok=(i < 6), error=None if i < 6 else HOSTILE, status_code=None)
            for i in range(10)]
    return write_report(
        tmp_path / "hostile.html",
        meta=RunMeta(target=f"http://x/v1?q={HOSTILE}", model=HOSTILE, started="2026-08-01 10:00",
                     ramp="1..10 step 5", chaos="standard", total_requests=10, total_sessions=1),
        stats=_stats(), knee=None, quality=[], cost=None, res=None,
        error_levels=errors_by_level(recs),
        run_meta=_run_meta(environment=Environment(python="3.14.6", platform=HOSTILE)),
        quality_note=HOSTILE, cost_note=HOSTILE, res_note=HOSTILE,
    ).read_text(encoding="utf-8")


def test_autoescape_is_on_for_the_report_template():
    from kneepoint.report.html import _env
    env = _env()
    assert env.autoescape is True
    # and it applies to the template as loaded, not only in principle
    assert env.get_template("report.html.j2").render(meta=_meta(), stats=[], knee=None,
                                                      knee_div="", quality=[], min_samples=10,
                                                      contaminated=[], abandoned_total=0,
                                                      retry_levels=[], error_levels=[],
                                                      first_error=None, total_failed=0,
                                                      worst_retry=None, latency_rows=[],
                                                      metadata_rows=None).count("<h2>") == 11


def test_strings_from_outside_kneepoint_are_escaped_in_the_report(tmp_path):
    html = _hostile_report(tmp_path)
    assert HOSTILE not in html
    # every site it was fed to renders it as text, not markup: the <title> and
    # header (target, model), the Errors table, the metadata Platform row, and
    # the four section placeholders (quality_note feeds both quality sections)
    escaped = "&lt;script&gt;alert(&#39;kneepoint&#39;)&lt;/script&gt;"
    assert html.count(escaped) == 9, html.count(escaped)
    # and the Plotly figure is still markup — escaping is not double-applied
    assert '<div id="knee-curve"' in html
    assert "&lt;div" not in html


def test_chapter_titles_with_an_ampersand_render_as_one_entity(tmp_path):
    html = _hostile_report(tmp_path)
    assert "Book: Throughput &amp; capacity" in html
    assert "&amp;amp;" not in html
