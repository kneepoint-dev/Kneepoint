import platform

from typer.testing import CliRunner

from kneepoint.chaos.faults import STANDARD_PROFILE
from kneepoint.cli import RUN_MIN_GROUP, app
from kneepoint.collect.schemas import SCHEMA_VERSION
from kneepoint.collect.writer import KNEEPOINT_VERSION, read_jsonl, read_run_meta


def test_run_command_end_to_end(mock_agent_url, tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "run", "--target", mock_agent_url,
            "--ramp", "1..3", "--step", "1", "--hold-seconds", "1",
            "--price-in", "3", "--price-out", "15",
            "--out", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert ("Knee point:" in result.output) or ("No clear knee" in result.output)
    jsonl_files = [p for p in tmp_path.glob("run-*.jsonl") if "-sessions" not in p.name]
    chart_files = list(tmp_path.glob("run-*-report.html"))
    assert len(jsonl_files) == 1
    assert len(chart_files) == 1
    records = read_jsonl(jsonl_files[0])
    assert len(records) >= 3
    assert {r.concurrency for r in records} == {1, 2, 3}
    sessions_files = list(tmp_path.glob("run-*-sessions.jsonl"))
    assert len(sessions_files) == 1
    from kneepoint.collect.schemas import SessionRecord
    sessions = read_jsonl(sessions_files[0], SessionRecord)
    assert len(sessions) >= 3
    assert all(s.transcript for s in sessions)
    assert "Resolution rate:" in result.output
    assert all(s.resolved is not None for s in sessions)
    assert "$/resolved task:" in result.output

    # the fourth artifact: the metadata sidecar, named from the same run id
    run_id = jsonl_files[0].stem[len("run-"):]
    meta_path = tmp_path / f"run-{run_id}-meta.json"
    assert meta_path.exists()
    assert f"Metadata:    {meta_path}" in result.output
    meta = read_run_meta(meta_path)
    assert meta.run_id == run_id and meta.command == "run"
    assert meta.target == mock_agent_url and meta.model == "mock"
    assert (meta.ramp.start, meta.ramp.stop, meta.ramp.step) == (1, 3, 1)
    assert meta.hold_seconds == 1.0 and meta.seed == 0
    assert meta.chaos.profile == "off" and meta.chaos.faults == []
    assert meta.price is not None
    assert (meta.price.input_per_mtok, meta.price.output_per_mtok) == (3.0, 15.0)
    assert meta.price.max_spend is None
    assert meta.min_samples == 10 and meta.min_group == RUN_MIN_GROUP
    assert meta.schema_version == SCHEMA_VERSION
    assert meta.kneepoint_version == KNEEPOINT_VERSION
    assert meta.environment.python == platform.python_version()
    # started_at is the real start, i.e. before the first request; the sidecar
    # is written after the last one
    assert meta.started_at <= min(r.started_at for r in records)
    assert meta.finished_at >= max(r.started_at + r.total_ms / 1000 for r in records)


def test_run_meta_records_the_chaos_profile_and_no_price_when_cost_is_off(
    mock_agent_url, tmp_path,
):
    """A chaos run that injects nothing looks identical to `--chaos off` in the
    request lines; only the sidecar can tell them apart. And `price: null` means
    cost tracking was off — not free."""
    result = CliRunner().invoke(
        app,
        [
            "run", "--target", mock_agent_url,
            "--ramp", "1..2", "--step", "1", "--hold-seconds", "1",
            "--chaos", "standard", "--seed", "7", "--min-samples", "3",
            "--out", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    meta = read_run_meta(next(tmp_path.glob("run-*-meta.json")))
    assert meta.chaos.profile == "standard"
    assert meta.chaos.faults == STANDARD_PROFILE
    assert meta.price is None
    assert meta.seed == 7 and meta.min_samples == 3
    assert (meta.turns.min, meta.turns.max) == (1, 1)
    assert meta.retry.max_attempts == 3


def test_run_aborts_when_estimate_exceeds_cap(mock_agent_url, tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "run", "--target", mock_agent_url,
            "--ramp", "1..2", "--step", "1", "--hold-seconds", "5",
            "--price-in", "100000", "--price-out", "100000",   # absurd prices
            "--max-spend", "0.01", "--out", str(tmp_path),
        ],
    )
    assert result.exit_code == 3
    assert "Estimated spend" in result.output
    assert "--force" in result.output


def test_run_with_scenario_file(mock_agent_url, tmp_path):
    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        f"""
target: {{url: {mock_agent_url}}}
workload:
  ramp: {{from: 1, to: 2, step: 1, hold_seconds: 1}}
  conversation:
    turns: {{min: 1, max: 2}}
resolution:
  check: {{kind: contains, value: "[RESOLVED"}}
slo:
  min_resolution_rate: 0.5
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["run", "--scenario", str(scenario), "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Resolution rate:" in result.output
    assert list(tmp_path.glob("run-*-report.html"))


def test_run_slo_breach_exits_nonzero(mock_agent_url, tmp_path):
    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        f"""
target: {{url: {mock_agent_url}}}
workload:
  ramp: {{from: 1, to: 1, step: 1, hold_seconds: 1}}
resolution:
  check: {{kind: contains, value: "IMPOSSIBLE-MARKER"}}
slo:
  min_resolution_rate: 0.99
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["run", "--scenario", str(scenario), "--out", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "SLO breach" in result.output


def test_run_command_rejects_bad_ramp():
    result = CliRunner().invoke(app, ["run", "--target", "http://x/v1", "--ramp", "50..1"])
    assert result.exit_code != 0
