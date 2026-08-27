from kneepoint.analyze.resilience import resilience
from tests.test_deterministic_judge import _session


def _judged(resolved: bool, faults: list[str] | None = None):
    s = _session("x")
    s.resolved = resolved
    s.faults = faults or []
    return s


def test_score_is_faulted_over_clean():
    sessions = (
        [_judged(True) for _ in range(20)]                                  # clean, 100%
        + [_judged(True, ["llm_rate_limit"]) for _ in range(8)]             # faulted…
        + [_judged(False, ["tool_malformed_json"]) for _ in range(2)]       # …80% resolved
    )
    summary = resilience(sessions, min_group=5)
    assert summary.clean_sessions == 20
    assert summary.faulted_sessions == 10
    assert summary.clean_resolution_rate == 1.0
    assert summary.faulted_resolution_rate == 0.8
    assert summary.score == 80.0


def test_score_caps_at_100():
    sessions = (
        [_judged(False) for _ in range(5)] + [_judged(True) for _ in range(5)]  # clean 50%
        + [_judged(True, ["llm_rate_limit"]) for _ in range(10)]                # faulted 100%
    )
    assert resilience(sessions, min_group=5).score == 100.0


def test_none_when_groups_too_thin():
    sessions = [_judged(True)] * 3 + [_judged(True, ["llm_rate_limit"])] * 3
    summary = resilience(sessions, min_group=10)
    assert summary.score is None
    assert summary.clean_resolution_rate is None


def test_per_fault_rows_and_verdicts():
    sessions = (
        [_judged(True) for _ in range(10)]                                   # clean 100%
        + [_judged(True, ["llm_rate_limit"]) for _ in range(10)]             # 100% -> pass
        + [_judged(i < 8, ["tool_timeout"]) for i in range(10)]              # 80% -> degraded
        + [_judged(i < 5, ["tool_malformed_json"]) for i in range(10)]       # 50% -> fail
    )
    summary = resilience(sessions, min_group=5)
    rows = {r.fault: r for r in summary.rows}
    assert rows["llm_rate_limit"].verdict == "pass"
    assert rows["tool_timeout"].verdict == "degraded"
    assert rows["tool_malformed_json"].verdict == "fail"
    assert rows["tool_timeout"].sessions_hit == 10


def test_unjudged_sessions_are_excluded():
    unjudged = _session("x")  # resolved is None
    unjudged.faults = ["llm_rate_limit"]
    summary = resilience([unjudged] * 20, min_group=5)
    assert summary.faulted_sessions == 0
    assert summary.score is None
