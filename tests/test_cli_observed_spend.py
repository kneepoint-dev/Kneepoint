"""Spend admission through the CLI and real generator, using only mock completions."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

import kneepoint.cli as cli
import kneepoint.generator.session as session
from kneepoint.collect.schemas import RequestRecord
from kneepoint.report.rerender import ReportInputError, render_from_files
from kneepoint.targets.openai_compatible import TurnOutcome


def invoke(tmp_path, *args):
    return CliRunner().invoke(cli.app, [
        "run", "--target", "http://mock.invalid/v1", "--ramp", "1..1",
        "--hold-seconds", "0", "--out", str(tmp_path), *args,
    ])


def saved(tmp_path):
    lifecycle = json.loads(next(tmp_path.glob("*-lifecycle.json")).read_text())
    records_path = next(p for p in tmp_path.glob("run-*.jsonl") if "-sessions" not in p.name)
    records = [json.loads(line) for line in records_path.read_text().splitlines()]
    return lifecycle, records_path, records


@pytest.fixture
def completions(monkeypatch):
    calls = []
    options = {"input_tokens": 1, "output_tokens": 1, "ok": True}

    async def turn(*args, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        return TurnOutcome(record=RequestRecord(
            started_at=0, total_ms=1, **kwargs, **options,
        ), text="[RESOLVED]")

    async def calibrate(*args, **kwargs):
        raise AssertionError("capped/unpriced run must not calibrate")

    monkeypatch.setattr(session, "run_turn", turn)
    monkeypatch.setattr(cli, "_calibrate", calibrate)
    return calls, options


@pytest.mark.parametrize("force", [False, True])
def test_zero_cap_dispatches_nothing_and_writes_empty_report(tmp_path, completions, force):
    result = invoke(tmp_path, "--price-in", "0", "--price-out", "0", "--max-spend", "0",
                    *(["--force"] if force else []))
    assert result.exit_code == 3, result.output
    lifecycle, path, records = saved(tmp_path)
    assert not completions[0] and not records
    assert lifecycle["accounting"]["stop_reason"] == "cap_reached"
    assert lifecycle["accounting"]["total_spend"] == 0
    assert lifecycle["effective_prices"] == {"input_per_mtok": 0, "output_per_mtok": 0}
    assert lifecycle["calibration"] == "not_run"
    assert next(tmp_path.glob("*-report.html")).exists()
    assert "Calibration skipped" in result.output
    original = path.with_name(path.stem + "-report.html").read_bytes()
    rerender = CliRunner().invoke(
        cli.app, ["report", str(path), "--out", str(tmp_path / "again.html")]
    )
    assert rerender.exit_code == 0, rerender.output
    assert (tmp_path / "again.html").read_bytes() == original
    assert json.loads(next(tmp_path.glob("*-lifecycle.json")).read_text()) == lifecycle


@pytest.mark.parametrize("damage", [
    "missing_meta", "missing_sessions", "missing_lifecycle", "malformed_lifecycle",
    "invalid_json", "missing_accounting", "metadata_cap", "wrong_run", "running", "attempt",
    "completed_request", "accounted_attempt", "nonzero_cap",
])
def test_empty_report_requires_consistent_zero_cap_evidence(tmp_path, completions, damage):
    result = invoke(tmp_path, "--price-in", "0", "--price-out", "0", "--max-spend", "0")
    assert result.exit_code == 3, result.output
    lifecycle, path, _ = saved(tmp_path)
    lifecycle_path = next(tmp_path.glob("*-lifecycle.json"))
    if damage in ("missing_meta", "missing_sessions", "missing_lifecycle"):
        suffix = {"missing_meta": "-meta.json", "missing_sessions": "-sessions.jsonl",
                  "missing_lifecycle": "-lifecycle.json"}[damage]
        path.with_name(path.stem + suffix).unlink()
    elif damage == "malformed_lifecycle":
        lifecycle_path.write_text("[]")
    elif damage == "invalid_json":
        lifecycle_path.write_text("{")
    elif damage == "metadata_cap":
        meta_path = path.with_name(path.stem + "-meta.json")
        meta = json.loads(meta_path.read_text())
        meta["price"]["max_spend"] = 1
        meta_path.write_text(json.dumps(meta))
    else:
        if damage == "missing_accounting":
            lifecycle["accounting"] = None
        elif damage == "wrong_run":
            lifecycle["run_id"] = "other-run"
        elif damage == "running":
            lifecycle["status"] = "running"
        elif damage == "attempt":
            lifecycle["attempts"] = [{"status": "starting", "dispatched": False}]
        elif damage == "completed_request":
            lifecycle["completed_requests"] = 1
        elif damage == "accounted_attempt":
            lifecycle["accounting"]["coverage"]["completed_attempts"] = 1
        elif damage == "nonzero_cap":
            lifecycle["accounting"]["max_spend"] = 1
        lifecycle_path.write_text(json.dumps(lifecycle))
    out = tmp_path / "again.html"
    with pytest.raises(ReportInputError, match="zero-cap lifecycle evidence"):
        render_from_files(path, out=out)
    assert not out.exists()


@pytest.mark.parametrize("force", [False, True])
def test_exact_cap_stops_before_next_turn(tmp_path, completions, force):
    result = invoke(tmp_path, "--price-in", "1000000", "--price-out", "0",
                    "--max-spend", "1", "--turns", "3..3",
                    *(["--force"] if force else []))
    assert result.exit_code == 3, result.output
    lifecycle, _, records = saved(tmp_path)
    assert len(completions[0]) == len(records) == 1
    assert lifecycle["accounting"]["total_spend"] == 1
    assert lifecycle["accounting"]["known_overshoot"] == 0
    assert lifecycle["status"] == "stopped"


def test_active_attempts_drain_and_overshoot_is_saved(tmp_path, completions):
    result = invoke(tmp_path, "--ramp", "2..2", "--turns", "2..2",
                    "--price-in", "1000000", "--price-out", "0", "--max-spend", "1")
    assert result.exit_code == 3, result.output
    lifecycle, _, records = saved(tmp_path)
    assert len(completions[0]) == len(records) == 2
    assert lifecycle["accounting"]["known_spend"] == 2
    assert lifecycle["accounting"]["known_overshoot"] == 1
    assert lifecycle["accounting"]["coverage"]["completed_attempts"] == 2
    assert "known overshoot $1.0000" in result.output


@pytest.mark.parametrize("missing", ["input_tokens", "output_tokens"])
def test_unknown_usage_stops_retries_and_suppresses_cost_in_run_and_rerender(
    tmp_path, completions, missing,
):
    completions[1].update({missing: None, "ok": False, "status_code": 503})
    result = invoke(tmp_path, "--price-in", "1000000", "--price-out", "1000000",
                    "--max-spend", "10")
    assert result.exit_code == 3, result.output
    lifecycle, path, records = saved(tmp_path)
    assert len(completions[0]) == len(records) == 1
    assert lifecycle["accounting"]["stop_reason"] == "unknown_usage"
    assert lifecycle["accounting"]["total_spend"] is None
    assert lifecycle["accounting"]["known_spend"] == 1
    assert "Total spend unknown" in result.output
    assert "Known subtotal: $1.0000" in result.output
    assert "Total spend:     " not in result.output
    original = path.with_name(path.stem + "-report.html").read_bytes()
    rerender = render_from_files(path, out=tmp_path / "again.html")
    assert rerender.cost is None
    assert rerender.report_path.read_bytes() == original
    assert b"Cost aggregates are suppressed" in original


@pytest.mark.parametrize("args", [
    ["--max-spend", "1"],
    ["--max-spend", "1", "--price-in", "0"],
    ["--price-in", "-1"], ["--price-out", "nan"], ["--max-spend", "inf"],
])
def test_bad_prices_fail_before_transport(tmp_path, completions, monkeypatch, args):
    def no_transport(*args, **kwargs):
        raise AssertionError("preflight must run before transport construction")

    monkeypatch.setattr(cli.httpx, "AsyncHTTPTransport", no_transport)
    result = invoke(tmp_path, "--chaos", "standard", *args)
    assert result.exit_code == 2, result.output
    assert "Invalid value" in result.output
    assert not completions[0]
    assert not list(tmp_path.glob("*-lifecycle.json"))


@pytest.mark.parametrize("cost,judge", [
    ("{max_spend: 1}", ""),
    ("{input_per_mtok: 0, output_per_mtok: 0, max_spend: 1}",
     "resolution: {judge: {base_url: 'http://mock.invalid/v1', model: mock}}"),
    ("{input_per_mtok: .nan}", ""),
    ("{output_per_mtok: -1}", ""),
    ("{max_spend: .inf}", ""),
])
def test_scenario_preflight(tmp_path, completions, cost, judge):
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(f"target: {{url: 'http://mock.invalid/v1'}}\ncost: {cost}\n{judge}\n")
    result = invoke(tmp_path, "--scenario", str(scenario))
    assert result.exit_code in (1, 2), result.output
    assert not completions[0]
    assert not list(tmp_path.glob("*-lifecycle.json"))


def test_unpriced_run_remains_compatible_without_claiming_free_spend(tmp_path, completions):
    result = invoke(tmp_path)
    assert result.exit_code == 0, result.output
    lifecycle, _, records = saved(tmp_path)
    assert len(records) == 1
    assert lifecycle["accounting"] is None
    assert lifecycle["effective_prices"] is None
    assert "Total spend:" not in result.output


def test_uncapped_priced_calibration_is_outside_accounting(tmp_path, completions, monkeypatch):
    calibration_calls = []

    async def calibrate(*args):
        calibration_calls.append(args)
        return RequestRecord(session_id="calibration", concurrency=1, started_at=0,
                             total_ms=1, input_tokens=99, output_tokens=99, ok=True)

    monkeypatch.setattr(cli, "_calibrate", calibrate)
    result = invoke(tmp_path, "--price-in", "1000000")
    assert result.exit_code == 0, result.output
    lifecycle, path, records = saved(tmp_path)
    assert len(calibration_calls) == len(records) == 1
    assert lifecycle["accounting"]["total_spend"] == 1
    assert lifecycle["calibration"] == "outside_generator_accounting"
    assert "Calibration spend is outside" in result.output
    original = path.with_name(path.stem + "-report.html").read_bytes()
    assert render_from_files(path, out=tmp_path / "again.html").report_path.read_bytes() == original


def test_accounting_and_prices_survive_generator_failure(tmp_path, completions, monkeypatch):
    original = session.run_turn
    count = 0

    async def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("mock failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(session, "run_turn", fail_second)
    result = invoke(tmp_path, "--turns", "2..2", "--price-in", "1000000",
                    "--price-out", "0", "--max-spend", "10")
    assert isinstance(result.exception, RuntimeError)
    lifecycle, _, records = saved(tmp_path)
    assert lifecycle["status"] == "failed"
    assert lifecycle["accounting"]["known_spend"] == 1
    assert lifecycle["accounting"]["coverage"]["completed_attempts"] == len(records) == 1
    assert lifecycle["effective_prices"]["input_per_mtok"] == 1000000


def test_record_is_flushed_before_accounting(tmp_path, completions, monkeypatch):
    original = cli.ObservedSpendController.on_attempt_complete

    def observe(self, record):
        _, _, records = saved(tmp_path)
        assert records[-1]["session_id"] == record.session_id
        original(self, record)

    monkeypatch.setattr(cli.ObservedSpendController, "on_attempt_complete", observe)
    result = invoke(tmp_path, "--price-in", "0", "--price-out", "0", "--max-spend", "1")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("mixed", [False, True])
def test_failed_levels_render_and_slo_handles_missing_latency(
    tmp_path, completions, monkeypatch, mixed,
):
    original = session.run_turn

    async def turn(*args, **kwargs):
        completions[1].update(ok=mixed and kwargs["concurrency"] == 1, status_code=400)
        return await original(*args, **kwargs)

    monkeypatch.setattr(session, "run_turn", turn)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text("target: {url: 'http://mock.invalid/v1'}\nslo: {p95_total_ms: 10}\n")
    result = invoke(tmp_path, "--scenario", str(scenario), "--ramp", "1..2", "--turns", "1..1")
    assert result.exit_code == 1, result.output
    if not mixed:
        assert "SLO unknown - p95_total_ms: measured unknown" in result.output
    _, path, records = saved(tmp_path)
    assert records and any(not r["ok"] for r in records)
    original_report = path.with_name(path.stem + "-report.html").read_bytes()
    assert b"successful=0 " in original_report
    rerender = render_from_files(path, out=tmp_path / "again.html")
    assert rerender.knee_summary.usable_levels == 0
    # Unpriced rerenders use different cost-placeholder copy; compare the
    # latency/quality/error portion of the report, which must be reproducible.
    assert rerender.report_path.read_bytes().split(b"<h2>Cost</h2>")[0] == (
        original_report.split(b"<h2>Cost</h2>")[0]
    )
