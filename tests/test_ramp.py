import pytest

from kneepoint.generator.ramp import RampSpec, parse_ramp, run_ramp


def test_parse_ramp():
    assert parse_ramp("1..50") == (1, 50)
    assert parse_ramp(" 5..10 ") == (5, 10)


@pytest.mark.parametrize("bad", ["50..1", "1-50", "0..10", "abc", "1..", "..5"])
def test_parse_ramp_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_ramp(bad)


def test_ramp_levels_always_include_stop():
    assert RampSpec(start=1, stop=10, step=4).levels() == [1, 5, 9, 10]
    assert RampSpec(start=1, stop=9, step=4).levels() == [1, 5, 9]
    assert RampSpec(start=3, stop=3, step=5).levels() == [3]


async def test_run_ramp_covers_all_levels(mock_agent_url):
    from kneepoint.generator.corpus import CorpusSampler
    from kneepoint.generator.session import SessionSpec

    spec = RampSpec(start=1, stop=2, step=1, hold_seconds=0.1)
    seen: list[int] = []
    sessions, records, exceeded = await run_ramp(
        mock_agent_url, "mock", spec, CorpusSampler(["hi"], seed=0), SessionSpec(),
        on_level=lambda level, sess, recs: seen.append(level),
    )
    assert {r.concurrency for r in records} == {1, 2}
    assert seen == [1, 2]
    assert all(s.ok for s in sessions)
    assert exceeded is False
