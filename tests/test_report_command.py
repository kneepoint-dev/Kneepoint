"""`kneepoint report`: the HTML is a pure function of the files on disk.

Byte-for-byte equality with the run's own report is the contract; the rest of
this file is what happens when part of the input is missing — the answer is
"unknown", said in the report, never a default dressed up as a fact.
"""

import re

import pytest
from typer.testing import CliRunner

from kneepoint.cli import app
from kneepoint.collect.writer import read_run_meta
from kneepoint.report.html import format_started
from kneepoint.report.rerender import (
    DEFAULT_MIN_SAMPLES,
    ReportInputError,
    render_from_files,
    sibling_paths,
)


@pytest.fixture(scope="module")
def finished_run(mock_agent_url, tmp_path_factory):
    """One real ramp, with prices, chaos and a non-default min_samples so every
    field the sidecar carries is exercised by the re-render."""
    out = tmp_path_factory.mktemp("run")
    result = CliRunner().invoke(
        app,
        [
            "run", "--target", mock_agent_url,
            "--ramp", "1..3", "--step", "1", "--hold-seconds", "1",
            "--price-in", "3", "--price-out", "15", "--chaos", "standard",
            "--min-samples", "3", "--out", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    records = next(p for p in out.glob("run-*.jsonl") if "-sessions" not in p.name)
    return records


def _header(html: str) -> str:
    return re.search(r'<p class="muted">(.*?)</p>', html, re.S).group(1)


def test_report_rerenders_the_runs_own_html_byte_for_byte(finished_run, tmp_path):
    original = finished_run.with_name(finished_run.stem + "-report.html")
    result = CliRunner().invoke(
        app, ["report", str(finished_run), "--out", str(tmp_path / "again.html")]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "again.html").read_bytes() == original.read_bytes()
    assert "Unknown:" not in result.output
    assert f"Report:      {tmp_path / 'again.html'}" in result.output
    # the knee verdict is re-derived and printed, like the run printed it
    assert ("Knee point:" in result.output) or ("No clear knee" in result.output)


def test_report_writes_beside_the_records_by_default(finished_run):
    original = finished_run.with_name(finished_run.stem + "-report.html")
    before = original.read_bytes()
    original.unlink()   # the case the command exists for: the report is gone
    result = CliRunner().invoke(app, ["report", str(finished_run)])
    assert result.exit_code == 0, result.output
    assert original.read_bytes() == before


def test_run_report_header_shows_the_start_time_from_the_sidecar(finished_run):
    """`RunMeta.started` used to be the clock at write time — the finish,
    labelled as the start. It is now the sidecar's `started_at`, formatted once."""
    meta = read_run_meta(finished_run.with_name(finished_run.stem + "-meta.json"))
    html = finished_run.with_name(finished_run.stem + "-report.html").read_text("utf-8")
    assert format_started(meta.started_at) in _header(html)


def test_report_without_sidecar_says_unknown_and_why(finished_run, tmp_path):
    meta_path = finished_run.with_name(finished_run.stem + "-meta.json")
    hidden = tmp_path / "hidden-meta.json"
    meta_path.rename(hidden)
    try:
        result = CliRunner().invoke(
            app, ["report", str(finished_run), "--out", str(tmp_path / "nometa.html")]
        )
        assert result.exit_code == 0, result.output
        html = (tmp_path / "nometa.html").read_text(encoding="utf-8")
        header = _header(html)
        for field in ("target unknown", "model unknown", "start time unknown",
                      "ramp unknown", "chaos unknown"):
            assert field in header, field
        # the counts are facts from the files, not metadata — still shown
        assert re.search(r"\d+ sessions / \d+ requests", header)
        # cost and resilience say *why* they are empty, not "not measured"
        assert "Cost not re-rendered: the metadata sidecar is missing" in html
        assert "--price-in / --price-out" in html
        assert "Resilience not re-rendered: the metadata sidecar is missing" in html
        assert "Cost not measured (set" not in html
        assert "Resilience not measured (run with" not in html
        # the run's min_samples was 3; without the sidecar the command applies its
        # own default and says so, on the console and in the chart caption
        assert f"this report applies {DEFAULT_MIN_SAMPLES}" in result.output
        assert f"without {DEFAULT_MIN_SAMPLES} successful requests" in html
        assert "metadata sidecar" in result.output and "not found" in result.output
    finally:
        hidden.rename(meta_path)


def test_report_overrides_recompute_cost_without_a_sidecar(finished_run, tmp_path):
    meta_path = finished_run.with_name(finished_run.stem + "-meta.json")
    original = finished_run.with_name(finished_run.stem + "-report.html").read_text("utf-8")
    hidden = tmp_path / "hidden-meta.json"
    meta_path.rename(hidden)
    try:
        result = CliRunner().invoke(
            app, ["report", str(finished_run), "--out", str(tmp_path / "override.html"),
                  "--price-in", "3", "--price-out", "15", "--min-samples", "3"]
        )
        assert result.exit_code == 0, result.output
        html = (tmp_path / "override.html").read_text(encoding="utf-8")
        cards = re.search(r"<h2>Cost</h2>(.*?)<h2>", html, re.S).group(1)
        original_cards = re.search(r"<h2>Cost</h2>(.*?)<h2>", original, re.S).group(1)
        assert cards == original_cards           # same rates, same tokens, same money
        assert "Cost not re-rendered" not in html
        assert "min_samples" not in result.output  # the override answered it
        assert "Resilience not re-rendered" in html  # no override exists for chaos
    finally:
        hidden.rename(meta_path)


def test_report_without_sessions_file_says_so(finished_run, tmp_path):
    sessions_path = finished_run.with_name(finished_run.stem + "-sessions.jsonl")
    hidden = tmp_path / "hidden-sessions.jsonl"
    sessions_path.rename(hidden)
    try:
        result = CliRunner().invoke(
            app, ["report", str(finished_run), "--out", str(tmp_path / "nosess.html")]
        )
        assert result.exit_code == 0, result.output
        html = (tmp_path / "nosess.html").read_text(encoding="utf-8")
        assert "0 sessions /" in _header(html)
        assert f"Sessions file {sessions_path.name} not found" in html
        assert "Resilience not re-rendered: sessions file" in html
        assert "sessions file" in result.output and "not found" in result.output
    finally:
        hidden.rename(sessions_path)


def test_report_refuses_the_wrong_file(finished_run, tmp_path):
    sessions_path = finished_run.with_name(finished_run.stem + "-sessions.jsonl")
    meta_path = finished_run.with_name(finished_run.stem + "-meta.json")
    for bad, fragment in (
        (sessions_path, "is the sessions file"),
        (meta_path, "is not a .jsonl records file"),
        (tmp_path / "missing.jsonl", "does not exist"),
    ):
        result = CliRunner().invoke(app, ["report", str(bad)])
        assert result.exit_code == 1, bad
        assert fragment in result.output, bad
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ReportInputError, match="no request records"):
        render_from_files(empty)


def test_sibling_paths_are_derived_by_name(tmp_path):
    sessions, meta, report = sibling_paths(tmp_path / "demo-20260818-1.jsonl")
    assert sessions == tmp_path / "demo-20260818-1-sessions.jsonl"
    assert meta == tmp_path / "demo-20260818-1-meta.json"
    assert report == tmp_path / "demo-20260818-1-report.html"


@pytest.mark.parametrize("legacy_metadata", [False, True, "think_time"])
def test_external_write_report_matches_cli_with_metadata(finished_run, tmp_path, legacy_metadata):
    """External callers pass aggregate(records) with metadata, without timing kwargs."""
    from kneepoint.analyze.cost import CostRates, incomplete_cost_note, summarize_cost
    from kneepoint.analyze.errors import errors_by_level
    from kneepoint.analyze.knee import aggregate, find_knee, knee_report
    from kneepoint.analyze.resilience import resilience
    from kneepoint.analyze.resolution import resolution_by_level, resolution_summary
    from kneepoint.analyze.retry import retry_by_level
    from kneepoint.collect.schemas import SessionRecord
    from kneepoint.collect.writer import read_jsonl, write_run_meta
    from kneepoint.report.html import RunMeta, write_report

    sessions_path, meta_path, _ = sibling_paths(finished_run)
    records = read_jsonl(finished_run)
    sessions = read_jsonl(sessions_path, SessionRecord)
    meta = read_run_meta(meta_path)
    if legacy_metadata == "think_time":
        from kneepoint.collect.schemas import RequestRecord
        records = [RequestRecord(
            session_id=f"{c}-{i}", concurrency=c, started_at=1000 + 4*i + 3*turn,
            started_monotonic_s=4*i + 3*turn, total_ms=1000, ok=True, turn_index=turn,
            input_tokens=1, output_tokens=1,
        ) for c in (1, 2, 3) for i in range(16) for turn in (0, 1)]
        meta = meta.model_copy(update={
            "hold_seconds": 60, "level_starts_monotonic_s": {1: 0, 2: 0, 3: 0},
            "level_status": {1: "completed", 2: "completed", 3: "completed"},
            "think_time_min_ms": 2000, "think_time_max_ms": 2000,
        })
    if legacy_metadata is True:
        meta = meta.model_copy(update={"level_starts_monotonic_s": None,
                                      "level_status": None,
                                      "think_time_min_ms": None, "think_time_max_ms": None})
    # Use copied files: fixture metadata must not be modified for other cases.
    copied = tmp_path / "run-external.jsonl"
    copied.write_text("".join(r.model_dump_json() + "\n" for r in records))
    copied.with_name("run-external-sessions.jsonl").write_bytes(sessions_path.read_bytes())
    write_run_meta(copied.with_name("run-external-meta.json"), meta)
    meta = read_run_meta(copied.with_name("run-external-meta.json"))
    stats = aggregate(records)
    rates = CostRates(input_per_mtok=meta.price.input_per_mtok,
                      output_per_mtok=meta.price.output_per_mtok)
    external = write_report(
        tmp_path / "external.html", meta=RunMeta(
            target=meta.target, model=meta.model, started=format_started(meta.started_at),
            ramp=f"{meta.ramp.start}..{meta.ramp.stop} step {meta.ramp.step}",
            chaos=meta.chaos.profile, total_requests=len(records), total_sessions=len(sessions),
        ), stats=stats, knee=find_knee(stats, min_samples=meta.min_samples),
        knee_summary=knee_report(stats, min_samples=meta.min_samples),
        quality=resolution_by_level(sessions), quality_summary=resolution_summary(sessions),
        cost=summarize_cost(records, sessions, rates),
        cost_note=incomplete_cost_note(records, rates),
        res=resilience(sessions, min_group=meta.min_group),
        retry_levels=retry_by_level(records, sessions), error_levels=errors_by_level(records),
        min_samples=meta.min_samples, run_meta=meta,
    )
    result = CliRunner().invoke(app, ["report", str(copied), "--out", str(tmp_path / "cli.html")])
    assert result.exit_code == 0, result.output
    assert external.read_bytes() == (tmp_path / "cli.html").read_bytes()
    assert "(k=" in external.read_text()
