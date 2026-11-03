import asyncio

import pytest
from typer.testing import CliRunner

from kneepoint.cli import app
from kneepoint.collect.writer import read_run_meta
from kneepoint.generator import resources


async def test_sampler_converts_host_counter_deltas_to_a_level_sample(tmp_path, monkeypatch):
    proc_stat = tmp_path / "stat"
    proc_stat.write_text("cpu  20 10 10 90 0 0 0 1 10 0\nprocs_running 2\n")
    monkeypatch.setattr(resources, "PROC_STAT", proc_stat)
    sleeps = []

    async def advance(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            proc_stat.write_text("cpu  26 14 25 155 5 1 2 3 11 0\nprocs_running 3\n")
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(resources.asyncio, "sleep", advance)
    samples = []
    with pytest.raises(asyncio.CancelledError):
        await resources.sample_local_resources(4, resources.time.perf_counter(), samples)
    sample, = samples
    assert sleeps == [5.0, 5.0]
    assert sample.concurrency == 4 and sample.run_queue == 3
    assert (sample.cpu_user_pct, sample.cpu_system_pct, sample.cpu_idle_pct,
            sample.cpu_steal_pct) == (10, 18, 65, 2)


async def test_unavailable_counters_leave_no_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, "PROC_STAT", tmp_path / "missing")
    samples = []
    await resources.sample_local_resources(1, 0.0, samples)
    assert samples == []


def test_run_flag_saves_samples_by_level_and_report_replays_them(
    mock_agent_url, tmp_path, monkeypatch,
):
    counter = 0

    def read_cpu():
        nonlocal counter
        counter += 1
        return [counter, 0, 0, 99 * counter, 0, 0, 0, 0], 1

    monkeypatch.setattr(resources, "_read_cpu", read_cpu)
    monkeypatch.setattr(resources, "SAMPLE_INTERVAL_S", 0.01)
    result = CliRunner().invoke(app, [
        "run", "--target", mock_agent_url, "--ramp", "1..2", "--step", "1",
        "--hold-seconds", "0.08", "--sample-resources", "--out", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    meta_path, = tmp_path.glob("run-*-meta.json")
    meta = read_run_meta(meta_path)
    samples = meta.local_resource_samples
    assert {sample.concurrency for sample in samples} == {1, 2}
    assert all(sample.cpu_idle_pct == 99 and sample.run_queue == 1 for sample in samples)
    html, = tmp_path.glob("run-*-report.html")
    metrics, = tmp_path.glob("run-*-report-metrics.json")
    records = tmp_path / meta_path.name.replace("-meta.json", ".jsonl")
    result = CliRunner().invoke(app, ["report", str(records), "--out", str(tmp_path / "new.html")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "new-metrics.json").read_bytes() == metrics.read_bytes()
    heading = "<h3>Local load-generator resource samples</h3>"
    original = html.read_text()
    replay = (tmp_path / "new.html").read_text()
    assert heading in original and heading in replay
    assert original.split(heading)[1].split("</table>")[0] == (
        replay.split(heading)[1].split("</table>")[0]
    )
