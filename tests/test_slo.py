"""Configured checks distinguish measured breaches from gaps, on every surface."""

import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import kneepoint.analyze.slo as slo_analysis
from kneepoint.analyze.knee import aggregate
from kneepoint.analyze.resolution import resolution_summary
from kneepoint.analyze.slo import evaluate_run_slo
from kneepoint.cli import app
from kneepoint.collect.schemas import RequestRecord
from kneepoint.collect.writer import read_run_meta
from kneepoint.config import SloConfig, evaluate_slo
from kneepoint.report.rerender import render_from_files
from kneepoint.slo import evaluate_slo_results
from tests.test_cli_observed_spend import completions as _completions
from tests.test_cli_observed_spend import invoke, saved

completions = _completions

CHECKS = SloConfig(p95_total_ms=2, min_resolution_rate=.5, max_cost_per_resolved=1)


def evaluate(p95=2, resolution=.5, cost=1, slo=CHECKS):
    return evaluate_slo_results(slo, p95_ms=p95, resolution=resolution, cost_per_resolved=cost)


def test_equality_disabled_mixed_and_legacy_contract():
    assert [r.status for r in evaluate()] == ["pass"] * 3
    assert evaluate(slo=SloConfig()) == []
    mixed = evaluate(p95=3, cost=None)
    assert [r.status for r in mixed] == ["fail", "pass", "unknown"]
    assert mixed[2].measured is None
    assert evaluate_slo(CHECKS, p95_ms=3, resolution=.5, cost_per_resolved=None) == [
        "p95_total_ms: measured 3 vs limit 2",
        "max_cost_per_resolved: SLO set but not measured in this run",
    ]
    assert [r.status for r in evaluate(p95=0, resolution=1, cost=0)] == ["pass"] * 3
    assert [r.status for r in evaluate(p95=3, resolution=0, cost=2)] == ["fail"] * 3


@pytest.mark.parametrize("measurement", [None, float("nan"), float("inf"), -float("inf"), -1])
def test_invalid_or_missing_measurements_cannot_pass(measurement):
    results = evaluate(measurement, measurement, measurement)
    assert [r.status for r in results] == ["unknown"] * 3
    assert all(r.measured is None and r.reason for r in results)
    assert evaluate(resolution=1.1)[1].status == "unknown"


@pytest.mark.parametrize("field", list(SloConfig.model_fields))
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), -float("inf")])
def test_invalid_thresholds_rejected(field, value):
    with pytest.raises(ValidationError):
        SloConfig(**{field: value})


def test_resolution_threshold_range_and_zero_thresholds():
    with pytest.raises(ValidationError):
        SloConfig(min_resolution_rate=1.1)
    zero = SloConfig(p95_total_ms=0, min_resolution_rate=0, max_cost_per_resolved=0)
    assert [r.status for r in evaluate(0, 0, 0, zero)] == ["pass"] * 3


@pytest.mark.parametrize("thin_ms,failures", [(900.0, 19), (100.0, 18)])
def test_thin_level_cannot_pass_or_fail_latency_slo(thin_ms, failures):
    records = [RequestRecord(session_id=f"1-{i}", concurrency=1, started_at=0,
                             total_ms=100, ok=True) for i in range(20)]
    records += [RequestRecord(session_id=f"2-{i}", concurrency=2, started_at=0,
                              total_ms=thin_ms, ok=True)
                for i in range(20 - failures)]
    records += [RequestRecord(session_id=f"2-f{i}", concurrency=2, started_at=0,
                              total_ms=5, ok=False) for i in range(failures)]
    result = evaluate_run_slo(SloConfig(p95_total_ms=500), stats=aggregate(records),
                              quality=None, cost=None).results[0]
    assert result.status == "unknown"
    assert result.measured == 100
    assert "c=2" in result.reason


def test_eligible_healthy_and_breaching_levels_keep_their_verdicts():
    stats = aggregate([RequestRecord(session_id=f"{c}-{i}", concurrency=c,
                                     started_at=0, total_ms=ms, ok=True)
                       for c, ms in [(1, 100), (2, 600)] for i in range(20)])
    def result(limit):
        return evaluate_run_slo(SloConfig(p95_total_ms=limit), stats=stats,
                                quality=None, cost=None).results[0]
    assert result(700).status == "pass"
    assert result(500).status == "fail"


def test_opt_in_thin_breach_reports_worst_applicable_p95(monkeypatch):
    monkeypatch.setattr(slo_analysis, "THIN_LEVEL_LATENCY_BREACH_IS_CONCLUSIVE", True)
    records = [RequestRecord(session_id=f"1-{i}", concurrency=1, started_at=0,
                             total_ms=1000, ok=True) for i in range(20)]
    records.append(RequestRecord(session_id="2-0", concurrency=2, started_at=0,
                                 total_ms=600, ok=True))
    result = evaluate_run_slo(SloConfig(p95_total_ms=500), stats=aggregate(records),
                              quality=None, cost=None).results[0]
    assert result.status == "fail" and result.measured == 1000


def test_threshold_provenance_is_distinct_from_disabled_checks():
    args = dict(stats=[], quality=resolution_summary([]), cost=None)
    unknown = evaluate_run_slo(None, **args)
    disabled = evaluate_run_slo(SloConfig(), **args)
    assert not unknown.results and "thresholds were not recorded" in unknown.note
    assert not disabled.results and disabled.note == "No SLO configured."
    assert "pass" not in disabled.note
    assert all(r.status == "unknown" for r in evaluate_run_slo(CHECKS, **args).results)


def scenario(tmp_path, *, check="[RESOLVED]", thresholds=None, prices=True):
    path = tmp_path / "s.yaml"
    path.write_text(
        "target: {url: 'http://mock.invalid/v1'}\n"
        + (f"resolution: {{check: {{kind: contains, value: '{check}'}}}}\n" if check else "")
        + ("cost: {input_per_mtok: 0, output_per_mtok: 0}\n" if prices else "")
        + "slo: " + json.dumps(thresholds if thresholds is not None else {
            "p95_total_ms": 2, "min_resolution_rate": 1, "max_cost_per_resolved": .5,
        }) + "\n"
    )
    return path


def slo_section(path):
    return path.read_text().split('<section id="slo-results">')[1].split('</section>')[0]


@pytest.mark.parametrize("case", ["pass", "fail", "unknown", "zero-cap", "disabled",
                                  "missing-prices", "missing-usage", "no-resolution"])
def test_mocked_cli_and_report_share_states_and_reasons(tmp_path, completions, case):
    path = scenario(tmp_path, check=None if case == "unknown" else
                    "IMPOSSIBLE" if case == "no-resolution" else "[RESOLVED]",
                    thresholds={"p95_total_ms": 0} if case == "fail" else
                    {} if case == "disabled" else None,
                    prices=case != "missing-prices")
    if case == "missing-usage":
        completions[1]["input_tokens"] = None
    result = invoke(tmp_path, "--scenario", str(path), "--turns", "1..1",
                    "--min-samples", "1",
                    *(["--max-spend", "0"] if case == "zero-cap" else []))
    expected = 3 if case == "zero-cap" else 0 if case in ("pass", "disabled") else 1
    assert result.exit_code == expected, result.output
    _, records, _ = saved(tmp_path)
    original = records.with_name(records.stem + "-report.html")
    rerender = render_from_files(records, out=tmp_path / "again.html")
    assert slo_section(original) == slo_section(rerender.report_path)
    for row in rerender.slo.results:
        assert row.line() in result.output
        assert f'class="{row.status}">{row.status}</td>' in slo_section(original)
    if case in ("pass", "fail", "unknown"):
        assert f"SLO {case} -" in result.output
    if case == "disabled":
        assert "No SLO configured." in result.output
        assert "SLO pass" not in result.output
    if case == "zero-cap":
        assert all(row.status == "unknown" for row in rerender.slo.results)
        assert not completions[0]
    if case == "missing-prices":
        assert "Price rates unavailable" in rerender.slo.results[-1].reason
    if case == "missing-usage":
        assert "token usage is incomplete" in rerender.slo.results[-1].reason
    if case == "no-resolution":
        assert "No confirmed resolved sessions" in rerender.slo.results[-1].reason


def test_cli_thin_latency_is_unknown_and_nonpassing(tmp_path, completions):
    path = scenario(tmp_path, thresholds={"p95_total_ms": 2})
    result = invoke(tmp_path, "--scenario", str(path), "--turns", "1..1")
    assert result.exit_code == 1, result.output
    assert "SLO unknown - p95_total_ms" in result.output
    assert "c=1" in result.output


def test_old_metadata_empty_declaration_and_price_override(tmp_path, completions):
    path = scenario(tmp_path)
    assert invoke(tmp_path, "--scenario", str(path), "--turns", "1..1",
                  "--min-samples", "1").exit_code == 0
    _, records, _ = saved(tmp_path)
    meta_path = records.with_name(records.stem + "-meta.json")
    metadata = json.loads(meta_path.read_text())
    assert metadata["schema_version"] == 1
    assert metadata["slo"]["max_cost_per_resolved"] == .5
    rendered = render_from_files(records, out=tmp_path / "override.html", price_in=1000000)
    assert rendered.slo.results[-1].status == "fail"
    assert rendered.slo.results[-1].measured == 1
    assert "Threshold not met." in slo_section(rendered.report_path)
    result = CliRunner().invoke(app, ["report", str(records), "--price-in", "1000000",
                                     "--out", str(tmp_path / "cli-override.html")])
    assert result.exit_code == 0
    assert rendered.slo.results[-1].line() in result.output
    assert json.loads(meta_path.read_text()) == metadata
    metadata.pop("slo")
    meta_path.write_text(json.dumps(metadata))
    assert read_run_meta(meta_path).slo is None
    old = render_from_files(records, out=tmp_path / "old.html")
    assert "SLO unknown: declared thresholds were not recorded." in slo_section(old.report_path)
    assert not old.slo.results
    metadata["slo"] = {}
    meta_path.write_text(json.dumps(metadata))
    empty = render_from_files(records, out=tmp_path / "empty.html")
    assert "No SLO configured." in slo_section(empty.report_path)
    assert not empty.slo.results
