"""Clock and closed-loop checks without a server or a load run."""

import json
import time

import httpx
import pytest

from kneepoint.analyze import timing
from kneepoint.analyze.knee import aggregate, knee_report
from kneepoint.cli import _print_timing, _warn_contamination
from kneepoint.collect.schemas import RequestRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import RunEvidence
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import SessionSpec
from kneepoint.report.rerender import _level_status_from_lifecycle
from kneepoint.targets.openai_compatible import run_turn


def rec(c, sid, wall, mono=None, duration=1.0):
    return RequestRecord(
        session_id=sid, concurrency=c, started_at=wall,
        started_monotonic_s=mono, total_ms=duration * 1000, ok=True,
    )


class FakeClocks:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 0.0

    def tick(self, seconds=1.0, *, wall_step=0.0, monotonic_pause=0.0):
        self.wall += seconds + wall_step
        self.mono += seconds - monotonic_pause

    def request(self, level, session):
        return rec(level, session, self.wall, self.mono)


@pytest.mark.parametrize("change", ["forward_step", "host_sleep"])
def test_fake_clocks_flag_a_step_mid_level(change):
    clock = FakeClocks()
    rows = []
    for i in range(6):
        rows.append(clock.request(1, f"s{i}"))
        clock.tick(seconds=901 if change == "host_sleep" and i == 2 else 1,
                   wall_step=900 if change == "forward_step" and i == 2 else 0,
                   monotonic_pause=900 if change == "host_sleep" and i == 2 else 0)
    level = timing.analyze_timing(rows, hold_seconds=15)[1]
    assert level.exact_clock_step and level.continuity_gap and level.clock_anomaly
    assert level.source == "monotonic"
    assert level.throughput_rps == pytest.approx(1.0)
    stats = aggregate(rows)
    assert stats[0].contaminated and stats[0].clock_anomaly


def test_legacy_records_parse_and_pool_continuity_finds_step():
    rows = [RequestRecord.model_validate({
        "session_id": f"s{i}", "concurrency": 1, "started_at": wall,
        "total_ms": 1000, "ok": True,
    }) for i, wall in enumerate((0, 1, 2, 903, 904))]
    level = timing.analyze_timing(rows, hold_seconds=15)[1]
    assert level.source == "wall fallback"
    assert not level.exact_clock_step and level.continuity_gap
    assert level.max_continuity_gap_s == pytest.approx(900)
    unknown_think = timing.analyze_timing(
        rows, hold_seconds=15, think_time_ms=None,
    )[1]
    assert unknown_think.little_gap_s is None


def test_same_session_continuity_accounts_for_think_time():
    rows = [rec(1, "same", 0), rec(1, "same", 1.5), rec(1, "same", 4)]
    rows[1].turn_index = 1
    rows[2].turn_index = 2
    level = timing.analyze_timing(rows, think_time_ms=(500, 500))[1]
    assert level.continuity_gap is False  # the second gap is exactly 1 s
    rows[-1] = rec(1, "same", 4.1)
    rows[-1].turn_index = 2
    assert timing.analyze_timing(rows, think_time_ms=(500, 500))[1].continuity_gap


def test_unknown_legacy_think_time_does_not_flag_healthy_multi_turns():
    rows = [rec(1, str(i), 4 * i + turn)
            for i in range(16) for turn in (0, 3)]
    for row in rows:
        row.turn_index = 0 if row.started_at % 4 == 0 else 1
    level = timing.analyze_timing(rows, hold_seconds=60, think_time_ms=None)[1]
    assert not level.clock_anomaly
    assert level.little_gap_s is None


def test_law_uses_think_time_only_between_turns():
    rows = [rec(1, str(i), 4 * i + turn, 4 * i + turn)
            for i in range(16) for turn in (0, 3)]
    for row in rows:
        row.turn_index = 0 if row.started_at % 4 == 0 else 1
    level = timing.analyze_timing(
        rows, hold_seconds=60, think_time_ms=(2000, 2000),
        level_starts_monotonic_s={1: 0},
    )[1]
    assert level.steady_throughput_rps == pytest.approx(0.5)
    assert level.little_gap_s == pytest.approx(0)
    assert not level.suspected_stall


def test_stopped_level_does_not_score_unobserved_steady_window():
    rows = [rec(1, str(i), i, i) for i in range(30)]
    level = timing.analyze_timing(
        rows, hold_seconds=60, level_starts_monotonic_s={1: 0},
        level_status={1: "stopped"},
    )[1]
    assert level.steady_throughput_rps is None
    assert level.little_gap_s is None
    assert not level.suspected_stall
    assert level.throughput_rps == pytest.approx(1)


def test_legacy_partial_level_with_no_status_abstains_from_steady_rate(tmp_path):
    rows = [rec(1, str(i), i) for i in range(30)]
    level = timing.analyze_timing(rows, hold_seconds=60)[1]
    assert level.steady_throughput_rps is None
    assert level.little_gap_s is None

    records_path = tmp_path / "run-old.jsonl"
    lifecycle = tmp_path / "run-old-lifecycle.json"
    lifecycle.write_text(json.dumps({"level_status": {"1": "budget_exceeded"}}))
    assert _level_status_from_lifecycle(records_path) == {1: "budget_exceeded"}


def test_one_turn_pool_ignores_unused_configured_think_time():
    rows = [rec(1, str(i), t, t) for i, t in enumerate((0, 1, 2, 5, 6, 7))]
    level = timing.analyze_timing(rows, think_time_ms=(5000, 5000))[1]
    assert level.continuity_gap
    assert level.max_continuity_gap_s == pytest.approx(2)


def test_steady_completions_exclude_warmup_and_drain_tail():
    rows = [rec(1, f"s{i}", i, i) for i in range(16)]
    rows.append(rec(1, "tail", 14.5, 14.5, duration=2))
    level = timing.analyze_timing(
        rows, hold_seconds=15, level_starts_monotonic_s={1: 0},
    )[1]
    assert level.steady_completions == 7  # completion times 5..11
    assert level.steady_throughput_rps == pytest.approx(1)
    assert level.throughput_rps == pytest.approx(17 / 16.5)


def test_little_law_passes_closed_loop_and_flags_injected_stall():
    healthy = [rec(2, f"worker-{worker}", 1000 + i, i)
               for i in range(16) for worker in range(2)]
    level = timing.analyze_timing(
        healthy, hold_seconds=15, level_starts_monotonic_s={2: 0},
    )[2]
    assert level.steady_completions == 14
    assert level.little_gap_s == pytest.approx(0)
    assert not level.suspected_stall
    stalled = [r for r in healthy if r.started_monotonic_s not in (6, 7, 8)]
    level = timing.analyze_timing(
        stalled, hold_seconds=15, level_starts_monotonic_s={2: 0},
    )[2]
    assert level.suspected_stall
    assert level.little_gap_s > 0.05
    stats = aggregate(stalled, hold_seconds=15, level_starts_monotonic_s={2: 0})
    lines = []
    _print_timing(stats, echo=lines.append)
    assert "suspected client or instrument stall" in lines[0]


def test_clock_flag_lowers_knee_confidence_and_is_named_in_cli():
    rows = []
    for c, duration in ((1, 0.1), (2, 0.25), (3, 0.5)):
        for i in range(12):
            rows.append(rec(c, f"{c}-{i}", 1000 + i * duration
                            + (900 if c == 2 and i >= 6 else 0),
                            i * duration, duration))
    stats = aggregate(rows)
    assert [s.clock_anomaly for s in stats] == [False, True, False]
    assert [s.contaminated for s in stats] == [False, True, True]
    report = knee_report(stats, min_samples=10)
    assert report.confidence == "low"
    lines = []
    _warn_contamination(stats, echo=lines.append)
    assert "clock step, host sleep or client stall at c=2" in lines[0]


async def test_attempt_records_run_relative_monotonic_start():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'))
    origin = time.perf_counter()
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await run_turn(
            client, "http://target/v1", "m", [{"role": "user", "content": "hi"}],
            concurrency=1, session_id="s", run_origin_s=origin,
        )
    assert outcome.record.started_monotonic_s is not None
    assert 0 <= outcome.record.started_monotonic_s < 1
    assert outcome.record.model_dump()["started_monotonic_s"] is not None


async def test_ramp_propagates_the_origin_and_records_level_start():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'))
    origin = time.perf_counter()
    evidence = RunEvidence()
    _, records, _ = await run_ramp(
        "http://target/v1", "m", RampSpec(start=1, stop=1, hold_seconds=0),
        CorpusSampler(["hi"], seed=0), SessionSpec(), transport=transport,
        run_origin_s=origin, evidence=evidence,
    )
    assert len(records) == 1
    assert 0 <= evidence.level_starts_monotonic_s[1] <= records[0].started_monotonic_s


def test_production_size_has_one_record_visit_and_linear_growth(monkeypatch):
    rows = [rec(level, f"s{session}", 1000 + i / 10, i / 10, 0.1)
            for level in range(1, 21) for session in range(50)
            for i in range(25)]
    assert len(rows) == 25_000
    inspected = 0
    original = timing._inspect_record

    def counted(*args):
        nonlocal inspected
        inspected += 1
        return original(*args)

    monkeypatch.setattr(timing, "_inspect_record", counted)
    timing.analyze_timing(rows, hold_seconds=15)
    assert inspected == len(rows)
    monkeypatch.setattr(timing, "_inspect_record", original)
    t0 = time.perf_counter()
    timing.analyze_timing(rows, hold_seconds=15)
    small = time.perf_counter() - t0
    t0 = time.perf_counter()
    timing.analyze_timing(rows * 10, hold_seconds=15)
    large = time.perf_counter() - t0
    assert large < small * 25  # ample runner noise margin; rules out quadratic growth


@pytest.mark.parametrize("hold", [0.5, 1.0, 2.0, 3.0])
def test_short_healthy_closed_loop_respects_counting_resolution(hold):
    # A 301 ms service, one fully occupied worker, and a short nonintegral window.
    rows = [rec(1, f"w-{i}", 1000 + i * .301, i * .301, .301)
            for i in range(20)]
    t = timing.analyze_timing(rows, hold_seconds=hold,
                              level_starts_monotonic_s={1: 0})[1]
    assert t.steady_completions > 0
    assert abs(t.little_gap_s) > .050 or hold > .5
    assert t.stall_threshold_s == max(.050, .05 * .301, 2 * t.little_resolution_s)
    assert not t.suspected_stall


def test_impossible_rate_is_named_as_estimator_or_clock_problem(tmp_path):
    rows = [rec(1, str(i), i, i, 1) for i in range(16)]
    rows += [rec(2, f"fast-{i}", 1000 + i / 10, i / 10, .1) for i in range(160)]
    stats = aggregate(rows, hold_seconds=15, level_starts_monotonic_s={1: 0, 2: 0})
    assert stats[1].throughput_bound_rps == pytest.approx(2)
    assert stats[1].estimator_clock_problem
    lines = []
    _print_timing(stats, echo=lines.append)
    assert "k=70" in lines[1]
    assert "estimator or clock problem" in lines[1]
    from kneepoint.report.html import RunMeta, write_report
    html = write_report(tmp_path / "bounds.html", meta=RunMeta(
        target="x", model="m", started="now", ramp="1..2", chaos="off",
        total_requests=len(rows), total_sessions=0,
    ), stats=stats, knee=None, quality=[], cost=None, res=None).read_text()
    assert "estimator or clock problem" in html
    assert "(k=70)" in html
