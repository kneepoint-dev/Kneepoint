import random

import httpx
import pytest

from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.session import (
    RetryPolicy,
    SessionSpec,
    ThinkTime,
    TurnsSpec,
    run_session,
)


def _sampler() -> CorpusSampler:
    # seed 4 draws the short prompt first, then the long one — the growing-context
    # assertion below stays meaningful even while the mock counts only the last
    # message (Task 11 switches it to full-history counting)
    return CorpusSampler(["question one?", "question two, longer this time?"], seed=4)


def _spec(turns_min=2, turns_max=2, max_attempts=3) -> SessionSpec:
    return SessionSpec(
        turns=TurnsSpec(min=turns_min, max=turns_max),
        think_time=ThinkTime(min_ms=0, max_ms=0),
        retry=RetryPolicy(max_attempts=max_attempts, backoff_s=0.01),
    )


async def test_session_runs_turns_with_growing_history(mock_agent_url):
    async with httpx.AsyncClient(timeout=30) as client:
        session, records = await run_session(
            client, mock_agent_url, "mock", _sampler(), _spec(),
            concurrency=1, rng=random.Random(0),
        )
    assert session.ok
    assert session.turns_completed == 2
    assert len(records) == 2
    assert [r.turn_index for r in records] == [0, 1]
    # transcript alternates user/assistant, 2 turns -> 4 entries
    assert [m["role"] for m in session.transcript] == ["user", "assistant", "user", "assistant"]
    # growing context: the mock counts tokens across ALL messages (Task 11 makes
    # this strict; until then input_tokens is at least non-decreasing)
    assert records[1].input_tokens >= records[0].input_tokens


async def test_session_retries_then_gives_up_on_dead_endpoint():
    async with httpx.AsyncClient(timeout=1) as client:
        session, records = await run_session(
            client, "http://127.0.0.1:9/v1", "mock", _sampler(), _spec(max_attempts=2),
            concurrency=1, rng=random.Random(0),
        )
    assert not session.ok
    assert session.turns_completed == 0
    assert [r.attempt for r in records] == [1, 2]
    assert all(not r.ok for r in records)


def test_turns_spec_validates():
    with pytest.raises(ValueError):
        TurnsSpec(min=3, max=2)
    with pytest.raises(ValueError):
        TurnsSpec(min=0, max=2)
