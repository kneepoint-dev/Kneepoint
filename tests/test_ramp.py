import asyncio
import threading

import pytest

from kneepoint.generator.monitor import InjectorMonitor
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


async def test_injector_monitor_is_saved_per_level_and_can_be_disabled(mock_agent_url):
    from kneepoint.generator.corpus import CorpusSampler
    from kneepoint.generator.lifecycle import RunEvidence
    from kneepoint.generator.session import SessionSpec

    spec = RampSpec(start=1, stop=1, hold_seconds=0.2)
    evidence = RunEvidence()
    await run_ramp(mock_agent_url, "mock", spec, CorpusSampler(["hi"]),
                   SessionSpec(), evidence=evidence)
    measured = evidence.injector_levels[1]
    assert measured.samples >= 1
    assert measured.lag_p95_ms is not None
    assert measured.lag_max_ms >= measured.lag_p95_ms
    assert measured.cpu_core_share >= 0
    assert measured.process_cpu_core_share >= 0
    assert measured.request_rate_rps > 0
    disabled = RunEvidence()
    await run_ramp(mock_agent_url, "mock", spec, CorpusSampler(["hi"]),
                   SessionSpec(), evidence=disabled, monitor_injector=False)
    assert disabled.injector_levels == {}


async def test_background_cpu_thread_does_not_count_as_loop_cpu():
    stop = threading.Event()

    def spin():
        value = 0
        while not stop.is_set():
            value += 1

    monitor = InjectorMonitor()
    worker = threading.Thread(target=spin)
    worker.start()
    try:
        await asyncio.sleep(.5)
    finally:
        stop.set()
        worker.join()
    measured = await monitor.finish([])
    assert measured.process_cpu_core_share > .5
    assert measured.cpu_core_share < .2
