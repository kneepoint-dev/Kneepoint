from kneepoint.analyze.resolution import resolution_by_level, resolution_rate
from tests.test_deterministic_judge import _session


def _judged(text: str, resolved: bool, concurrency: int = 1):
    s = _session(text, concurrency=concurrency)
    s.resolved = resolved
    return s


def test_resolution_rate_ignores_unjudged():
    sessions = [_judged("a", True), _judged("b", False), _session("c")]  # third unjudged
    assert resolution_rate(sessions) == 0.5


def test_resolution_rate_none_when_nothing_judged():
    assert resolution_rate([_session("a")]) is None
    assert resolution_rate([]) is None


def test_resolution_by_level_sorts_and_groups():
    sessions = [
        _judged("a", True, concurrency=5), _judged("b", True, concurrency=1),
        _judged("c", False, concurrency=5),
    ]
    levels = resolution_by_level(sessions)
    assert [(lv.concurrency, lv.judged, lv.resolved_rate) for lv in levels] == [
        (1, 1, 1.0), (5, 2, 0.5),
    ]
