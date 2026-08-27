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
        assert f"below {DEFAULT_MIN_SAMPLES} requests" in html
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
