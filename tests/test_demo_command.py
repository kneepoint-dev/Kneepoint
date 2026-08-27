from typer.testing import CliRunner

from kneepoint.cli import app
from kneepoint.collect.schemas import SessionRecord
from kneepoint.collect.writer import read_jsonl, read_run_meta
from kneepoint.demo.run import (
    DEMO_LLM_FAULTS,
    DEMO_MIN_GROUP,
    DEMO_MIN_SAMPLES,
    DEMO_PRICES,
    DEMO_TOOL_FAULTS,
)


def test_demo_end_to_end(tmp_path):
    result = CliRunner().invoke(
        app, ["demo", "--no-open", "--hold-seconds", "0.5", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    reports = list(tmp_path.glob("demo-*-report.html"))
    assert len(reports) == 1
    html = reports[0].read_text(encoding="utf-8")
    assert "Resilience" in html and "quality curve" in html
    assert "Resolution rate:" in result.output
    assert "$/resolved task:" in result.output
    assert "Report:" in result.output
    sessions = read_jsonl(next(tmp_path.glob("demo-*-sessions.jsonl")), SessionRecord)
    assert len(sessions) >= 5
    assert all(s.resolved is not None for s in sessions)   # demo always judges

    # all four artifacts share one id and all four paths are printed
    run_id = reports[0].name[len("demo-"):-len("-report.html")]
    for suffix in (".jsonl", "-sessions.jsonl", "-meta.json", "-report.html"):
        path = tmp_path / f"demo-{run_id}{suffix}"
        assert path.exists(), suffix
        assert str(path) in result.output, suffix
    assert "Metadata:    " in result.output
    meta = read_run_meta(tmp_path / f"demo-{run_id}-meta.json")
    assert meta.command == "demo" and meta.model == "demo"
    assert meta.target.endswith("/v1")
    assert (meta.ramp.start, meta.ramp.stop, meta.ramp.step) == (1, 13, 3)
    assert meta.hold_seconds == 0.5 and meta.seed == 0
    assert meta.chaos.profile == "demo"
    assert meta.chaos.faults == DEMO_LLM_FAULTS + DEMO_TOOL_FAULTS
    assert meta.price is not None
    assert meta.price.input_per_mtok == DEMO_PRICES.input_per_mtok
    assert meta.price.output_per_mtok == DEMO_PRICES.output_per_mtok
    assert meta.min_samples == DEMO_MIN_SAMPLES and meta.min_group == DEMO_MIN_GROUP
    assert (meta.turns.min, meta.turns.max) == (1, 2)
    assert (meta.retry.max_attempts, meta.retry.backoff_s) == (3, 0.2)
    assert meta.started_at <= min(s.started_at for s in sessions)
    assert meta.finished_at >= meta.started_at

    # the demo's report re-renders from its own files, byte for byte — the demo
    # profile, play-money prices and min_group all travel through the sidecar
    rerender = CliRunner().invoke(
        app, ["report", str(tmp_path / f"demo-{run_id}.jsonl"),
              "--out", str(tmp_path / "again.html")]
    )
    assert rerender.exit_code == 0, rerender.output
    assert (tmp_path / "again.html").read_bytes() == reports[0].read_bytes()


def test_demo_finds_its_own_knee_at_the_default_hold(tmp_path):
    """The demo exists to demonstrate a real knee (M1.1g).

    At the default hold the crossing must land mid-ramp, decisively: not the
    top-of-ramp lower bound, not a range smeared by the 15% noise band. Runs
    the real thing (~40s) because a shorter hold drops the c=1 level below
    `min_samples` and changes the floor the ratios are measured against.
    """
    result = CliRunner().invoke(app, ["demo", "--no-open", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Knee point: 7 concurrent sessions (p95_doubling, high confidence)" in result.output
    assert "or beyond" not in result.output
    assert "within 15% of the 2.0 threshold" not in result.output


def test_demo_never_opens_browser_in_tests(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    result = CliRunner().invoke(
        app, ["demo", "--no-open", "--hold-seconds", "0.5", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert opened == []
