"""Keep session completion, verdict coverage and judged-only success distinct."""

import itertools

import httpx
import pytest
from typer.testing import CliRunner

from kneepoint.analyze.resolution import resolution_by_level, resolution_summary
from kneepoint.cli import app
from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.collect.writer import read_jsonl
from kneepoint.judge.llm_judge import judge_sessions
from kneepoint.report.chart import drift_figure, quality_figure
from kneepoint.report.html import write_report
from kneepoint.report.rerender import render_from_files
from tests.test_deterministic_judge import _session
from tests.test_llm_judge import _cfg, _judge_transport
from tests.test_report_html import _meta


def test_two_confirmed_eight_unknown_are_not_whole_run_success():
    sessions = [_session("answer") for _ in range(10)]
    for session in sessions[:2]:
        session.resolved = True
    summary = resolution_summary(sessions)
    assert (summary.total, summary.transport_ok, summary.transport_failed) == (10, 10, 0)
    assert (summary.judged, summary.resolved, summary.unresolved, summary.unjudged) == (2, 2, 0, 8)
    assert summary.resolved_rate == 1
    assert summary.coverage == summary.confirmed_resolved_share == .2
    assert summary.slo_resolution is None
    assert summary.methods == {"unspecified": 2}
    assert "2/10 evaluated" in "\n".join(summary.lines())


def test_population_count_sweep_and_empty_rates():
    for states in itertools.product((True, False, None), repeat=4):
        sessions = [_session("answer", ok=i % 2 == 0) for i in range(4)]
        for session, state in zip(sessions, states, strict=True):
            session.resolved = state
        summary = resolution_summary(sessions)
        assert summary.resolved + summary.unresolved + summary.unjudged == summary.total == 4
        assert summary.transport_ok + summary.transport_failed == summary.total
        assert summary.judged == summary.resolved + summary.unresolved
        assert 0 <= summary.coverage <= 1
        assert 0 <= summary.confirmed_resolved_share <= 1
        assert summary.resolved_rate is None or 0 <= summary.resolved_rate <= 1
        assert summary.slo_resolution == (summary.resolved_rate if None not in states else None)
    empty = resolution_summary([])
    assert empty.total == empty.judged == empty.resolved == 0
    assert empty.coverage is empty.resolved_rate is empty.confirmed_resolved_share is None
    assert empty.slo_resolution is None


async def test_failed_transcripts_never_reach_external_judge_and_cannot_pass():
    failed = [_session("[RESOLVED]", ok=False) for _ in range(8)]
    failed[0].transcript = []
    failed[1].resolved = True  # even a stale success cannot overrule execution failure
    sessions = [_session("good"), _session("good")] + failed
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"resolved": true}',
        }}]})

    assert await judge_sessions(sessions, _cfg(), transport=httpx.MockTransport(handler)) == 10
    assert len(calls) == 2
    assert all(s.resolved is False and s.resolution_method == "execution_failure" for s in failed)
    summary = resolution_summary(sessions)
    assert summary.resolved_rate == summary.confirmed_resolved_share == .2
    assert summary.coverage == 1
    assert summary.methods == {"llm_judge": 2, "execution_failure": 8}


async def test_all_failed_needs_no_network_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("All-failed evaluation must not construct an external client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    failed = [_session(None, ok=False)]
    failed[0].transcript = []
    assert await judge_sessions(failed, _cfg(sample_rate=.01)) == 1
    assert failed[0].resolved is False


@pytest.mark.parametrize("population", ["empty", "unjudged", "failed"])
async def test_null_safe_charts_and_reports_for_empty_unknown_and_failed(tmp_path, population):
    sessions = [] if population == "empty" else [_session(None, ok=population != "failed")]
    if population == "failed":
        await judge_sessions(sessions, _cfg())
    levels = resolution_by_level(sessions)
    summary = resolution_summary(sessions)
    if sessions:
        assert len(levels) == 1
        assert levels[0].total == 1
    for figure in (quality_figure(levels, min_samples=0), drift_figure([], levels, min_samples=0)):
        if population == "failed":
            assert figure is not None
        else:
            assert figure is None
    html = write_report(tmp_path / "report.html", meta=_meta(), stats=[], knee=None,
                        quality=levels, quality_summary=summary, cost=None, res=None,
                        min_samples=0).read_text()
    assert 'id="quality-populations"' in html
    assert "Verdict coverage" in html
    if population != "failed":
        assert "Resolution was judged at" not in html
    else:
        assert "execution_failure=1" in html


def test_unjudged_level_keeps_visible_coverage_and_breaks_quality_lines():
    sessions = [_session("answer", concurrency=c) for c in (1, 2, 3)]
    sessions[0].resolved = sessions[2].resolved = True
    levels = resolution_by_level(sessions)
    assert levels[1].judged == 0 and levels[1].resolved_rate is None
    for fig in (quality_figure(levels, min_samples=1), drift_figure([], levels, min_samples=1)):
        trace = fig.data[-1]
        assert list(trace.x) == [1, 2, 3]
        assert list(trace.y) == [100, None, 100]
        assert trace.connectgaps is False
        assert "judged=0 / total=1 · coverage 0% - not plotted" in fig.layout.xaxis.ticktext[1]


@pytest.mark.parametrize("case", ["sampled", "outage", "failed", "interrupted", "full"])
def test_cli_slo_uses_complete_coverage_and_rerender_agrees(tmp_path, monkeypatch, case):
    sessions = [_session("[RESOLVED]", ok=case != "failed") for _ in range(10)]
    for i, session in enumerate(sessions):
        session.session_id = str(i)
    if case == "failed":
        sessions[0].transcript = []
    if case == "interrupted":
        sessions[-1].ok = False
        sessions[-1].turns_completed = 0
    records = [RequestRecord(session_id=s.session_id, concurrency=1, started_at=0,
                             total_ms=1, ok=s.ok, input_tokens=1, output_tokens=1)
               for s in sessions]

    async def fake_ramp(*args, evidence, on_attempt_complete, **kwargs):
        evidence.records.extend(records)
        evidence.sessions.extend(sessions)
        evidence.status = "completed"
        evidence.session_status.update(dict.fromkeys((s.session_id for s in sessions), "completed"))
        if case == "interrupted":
            evidence.session_status[sessions[-1].session_id] = "stopped"
        for record in records:
            on_attempt_complete(record)
        return sessions, records, None

    async def fake_judge(passed_sessions, cfg, **kwargs):
        if case == "interrupted":
            assert sessions[-1] not in passed_sessions
        return await judge_sessions(
            passed_sessions, cfg, transport=_judge_transport(
                {"resolved": True}, status=500 if case == "outage" else 200,
            ),
            **kwargs,
        )

    monkeypatch.setattr("kneepoint.cli.run_ramp", fake_ramp)
    monkeypatch.setattr("kneepoint.cli.judge_sessions", fake_judge)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(f"""
target: {{url: 'http://unused.test/v1', model: mock}}
cost: {{input_per_mtok: 0, output_per_mtok: 0}}
workload:
  ramp: {{from: 1, to: 1, step: 1, hold_seconds: 1}}
resolution:
  judge:
    base_url: http://judge.test/v1
    model: judge
    sample_rate: {0.2 if case == 'sampled' else 1.0}
slo:
  min_resolution_rate: 0.5
""")
    result = CliRunner().invoke(app, ["run", "--scenario", str(scenario), "--out", str(tmp_path)])
    assert result.exit_code == (0 if case == "full" else 1), result.output
    saved = read_jsonl(next(tmp_path.glob("run-*-sessions.jsonl")), SessionRecord)
    summary = resolution_summary(saved)
    if case in ("sampled", "outage", "interrupted"):
        assert summary.unjudged > 0
        assert "Incomplete verdict coverage" in result.output
        assert f"({summary.judged}/10 evaluated)" in result.output
        assert "SLO unknown - min_resolution_rate: measured unknown" in result.output
    elif case == "failed":
        assert summary.unjudged == 0 and summary.resolved_rate == 0
        assert "SLO unknown" not in result.output
        assert "min_resolution_rate: measured 0 vs limit 0.5" in result.output
    else:
        assert summary.slo_resolution == 1
        assert "SLO fail" not in result.output
    if case == "sampled":
        assert "Resolution rate: 100.0% among evaluated sessions (2/2)" in result.output
        assert "confirmed resolved/all: 20.0% (2/10)" in result.output
    original = next(tmp_path.glob("run-*-report.html"))
    record_path = next(p for p in tmp_path.glob("run-*.jsonl") if "-sessions" not in p.name)
    rendered = render_from_files(record_path, out=tmp_path / "again.html").report_path
    assert rendered.read_bytes() == original.read_bytes()
