import asyncio
import random
import time
from email.utils import formatdate

import httpx
import pytest

from kneepoint.collect.schemas import RequestRecord
from kneepoint.config import Scenario
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.pool import run_level
from kneepoint.generator.session import RetryPolicy, SessionSpec, run_session
from kneepoint.targets.retry_after import parse_retry_after
from kneepoint.targets.turn import TurnOutcome


def test_config_defaults_and_opt_in():
    raw = {"target": {"url": "http://localhost"}}
    assert not Scenario.model_validate(raw).client.honor_retry_after
    raw["client"] = {"honor_retry_after": True}
    assert Scenario.model_validate(raw).client.honor_retry_after


@pytest.mark.parametrize("value", ["bad", "-1", "nan", "inf", None])
def test_bad_header(value):
    assert parse_retry_after(value) is None


def test_date_header():
    assert 1 < parse_retry_after(formatdate(time.time() + 3, usegmt=True)) <= 3
    assert parse_retry_after(formatdate(time.time() - 3, usegmt=True)) == 0


@pytest.mark.parametrize(
    "enabled,status,delay,expected",
    [
        (False, 503, 50, 0),
        (True, 503, 50, 30),
        (True, 429, 50, 0),
        (True, 503, None, 0),
        (True, 503, -1, 0),
        (True, 503, float("inf"), 0),
        (True, 503, 0.25, 0.25),
        (True, 503, 0, 0),
    ],
)
async def test_final_attempt_backoff_and_record(monkeypatch, enabled, status, delay, expected):
    waits = []

    async def wait(value, stop):
        waits.append(value)

    monkeypatch.setattr("kneepoint.generator.session.wait_or_stop", wait)

    async def turn(*args, **kw):
        return TurnOutcome(
            record=RequestRecord(
                session_id=kw["session_id"],
                concurrency=1,
                started_at=time.time(),
                total_ms=1,
                ok=False,
                status_code=status,
            ),
            retry_after_s=delay,
        )

    async with httpx.AsyncClient() as client:
        _, records = await run_session(
            client,
            "http://localhost",
            "fake",
            CorpusSampler(["q"]),
            SessionSpec(honor_retry_after=enabled, retry=RetryPolicy(max_attempts=1)),
            concurrency=1,
            rng=random.Random(0),
            turn_runner=turn,
        )
    assert waits == ([expected] if expected else [])
    assert records[0].backoff_s == expected
    assert records[0].retry_after_s == delay


async def test_retry_does_not_double_wait(monkeypatch):
    waits = []

    async def wait(value, stop):
        waits.append(value)

    monkeypatch.setattr("kneepoint.generator.session.wait_or_stop", wait)

    async def turn(*args, **kw):
        return TurnOutcome(
            record=RequestRecord(
                session_id=kw["session_id"],
                concurrency=1,
                started_at=time.time(),
                total_ms=1,
                ok=False,
                status_code=503,
            ),
            retry_after_s=0.1,
        )

    async with httpx.AsyncClient() as client:
        await run_session(
            client,
            "http://localhost",
            "fake",
            CorpusSampler(["q"]),
            SessionSpec(honor_retry_after=True, retry=RetryPolicy(max_attempts=2)),
            concurrency=1,
            rng=random.Random(0),
            turn_runner=turn,
        )
    assert waits == [0.1, 0.1]


async def test_pool_waits_across_sessions_and_stop_interrupts():
    starts = []

    async def turn(*args, **kw):
        starts.append(time.perf_counter())
        return TurnOutcome(
            record=RequestRecord(
                session_id=kw["session_id"],
                concurrency=1,
                started_at=time.time(),
                total_ms=1,
                ok=False,
                status_code=503,
            ),
            retry_after_s=0.04,
        )

    await run_level(
        "http://localhost",
        "fake",
        1,
        0.11,
        CorpusSampler(["q"]),
        SessionSpec(honor_retry_after=True, retry=RetryPolicy(max_attempts=1)),
        turn_runner=turn,
        monitor_injector=False,
    )
    assert len(starts) >= 2
    assert all(b - a >= 0.035 for a, b in zip(starts, starts[1:], strict=False))
    starts.clear()
    stop = asyncio.Event()

    async def stop_later():
        await asyncio.sleep(0.01)
        stop.set()

    task = asyncio.create_task(stop_later())
    await asyncio.wait_for(
        run_level(
            "http://localhost",
            "fake",
            1,
            10,
            CorpusSampler(["q"]),
            SessionSpec(honor_retry_after=True, retry=RetryPolicy(max_attempts=1)),
            turn_runner=turn,
            monitor_injector=False,
            stop=stop,
        ),
        0.5,
    )
    await task
    assert len(starts) == 1
