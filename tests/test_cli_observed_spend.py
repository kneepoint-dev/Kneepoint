"""Spend admission through the CLI and real generator, using only mock completions."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

import kneepoint.cli as cli
import kneepoint.generator.session as session
from kneepoint.collect.schemas import RequestRecord
from kneepoint.report.rerender import render_from_files
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
    lifecycle, _, records = saved(tmp_path)
    assert not completions[0] and not records
    assert lifecycle["accounting"]["stop_reason"] == "cap_reached"
    assert lifecycle["accounting"]["total_spend"] == 0
    assert lifecycle["effective_prices"] == {"input_per_mtok": 0, "output_per_mtok": 0}
    assert lifecycle["calibration"] == "not_run"
    assert next(tmp_path.glob("*-report.html")).exists()
    assert "Calibration skipped" in result.output


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
