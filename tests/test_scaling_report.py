import gzip
import json
from pathlib import Path

import pytest

from kneepoint.analyze.knee import LevelStats, aggregate
from kneepoint.analyze.scaling import scaling_report
from kneepoint.collect.schemas import RequestRecord, ResourceSample
from kneepoint.report.rerender import render_from_files


def _level(c, x, p50, occupancy, **kwargs):
    return LevelStats(
        concurrency=c, n=50, error_rate=0, p50_ms=p50, p95_ms=p50, p99_ms=p50,
        scaling_window="steady", scaling_successes=50, scaling_throughput_rps=x,
        little_n=occupancy, **kwargs,
    )


def _sample(c, idle):
    return ResourceSample(
        concurrency=c, elapsed_s=5, cpu_user_pct=100 - idle, cpu_system_pct=0,
        cpu_idle_pct=idle, cpu_steal_pct=0, run_queue=1,
    )


def test_scaling_and_little_law_use_their_own_metrics():
    stats = [_level(1, 2.5, 400, 0.95), _level(2, 4.9, 400, 2.101), _level(3, 6, 500, 3)]
    result = scaling_report(stats)
    assert result.x_max_rps == 6
    assert result.plateau_levels == []
    assert result.r0_s == 0.4
    assert result.n_star == pytest.approx(2.4)
    assert [row.efficiency for row in result.levels] == pytest.approx([1, 0.98, 0.8])
    assert [row.little_off_by_more_than_5_pct for row in result.levels] == [False, True, False]


def test_flags_require_matching_local_samples_and_a_valid_baseline():
    stats = [_level(1, 2.5, 400, 1), _level(2, 2.5, 800, 2), _level(3, 2.5, 1200, 3)]
    samples = [_sample(1, 99), _sample(2, 95), _sample(2, 85), _sample(3, 70)]
    result = scaling_report(stats, resources=samples)
    assert result.levels[1].flags == ["software limit likely", "single-slot saturation likely"]
    assert result.levels[2].flags == []
    assert scaling_report(stats).levels[1].flags == ["single-slot saturation likely"]
    assert scaling_report(stats, resources=[_sample(3, 99)]).levels[1].flags == [
        "single-slot saturation likely",
    ]
    without_baseline = scaling_report(stats[1:], resources=samples)
    assert without_baseline.r0_s is None
    assert without_baseline.n_star is None
    assert all(row.efficiency is None and not row.flags for row in without_baseline.levels)


def test_timing_does_not_mix_success_only_rates_with_all_attempt_latency():
    records = [
        RequestRecord(
            concurrency=1, session_id=str(i), started_at=float(i), total_ms=1000, ok=i < 4,
        )
        for i in range(5)
    ]
    row, = scaling_report(aggregate(records), min_samples=1).levels
    assert row.window == "observed"
    assert row.throughput_rps == 0.8
    assert row.little_n == 1
    assert row.little_off_by_more_than_5_pct is False
    partial, = scaling_report(
        aggregate(records, level_status={1: "stopped"}), min_samples=1,
    ).levels
    assert partial.throughput_rps is None
    assert partial.little_off_by_more_than_5_pct is None


@pytest.mark.parametrize(
    ("name", "count", "plateau"),
    [("a", 3282, [3, 4, 5]), ("b", 3282, [3, 4, 5]), ("t4", 4077, [3, 4, 5, 6])],
)
def test_report_golden_client_records(name, count, plateau, tmp_path):
    fixture = Path(__file__).with_name("fixtures") / f"public-knee-{name}.jsonl.gz"
    raw = gzip.decompress(fixture.read_bytes())
    records = [RequestRecord.model_validate_json(line) for line in raw.splitlines()]
    assert len(records) == count

    # The recording protocol held each level for 120 seconds. With only client
    # records, the first observed start anchors the interior window.
    result = scaling_report(aggregate(records, hold_seconds=120))
    assert result.plateau_levels == plateau
    assert result.x_max_rps == pytest.approx(6.635, abs=0.015)
    assert 2.6 <= result.n_star <= 2.7
    assert result.levels[2].efficiency == pytest.approx(0.88, abs=0.005)
    assert all(row.little_off_by_more_than_5_pct is False for row in result.levels)
    assert all(not row.flags for row in result.levels)
    # Exercise the diagnostic on the recorded throughput curve with synthetic
    # local headroom. Only request records are imported from the experiment.
    with_samples = scaling_report(
        aggregate(records, hold_seconds=120), resources=[_sample(c, 99) for c in plateau],
    )
    assert with_samples.levels[2].flags == []
    assert all(row.flags == ["software limit likely"] for row in with_samples.levels[3:])

    records_path = tmp_path / "run-golden.jsonl"
    records_path.write_bytes(raw)
    report = render_from_files(records_path)
    companion = tmp_path / "run-golden-report-metrics.json"
    metrics = json.loads(companion.read_text())
    assert metrics["x_max_rps"] == pytest.approx(6.61, abs=0.01)
    assert metrics["plateau_levels"] == plateau
    assert metrics["levels"][2]["window"] == "observed"
    assert metrics["local_resource_samples"] is None
    html = report.report_path.read_text()
    assert f"Observed plateau at c={', '.join(map(str, plateau))}" in html
    assert "Little’s law" in html
