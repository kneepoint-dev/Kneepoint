"""Session pool: hold N concurrent multi-turn sessions on the target for a duration."""

import asyncio
import random
import time

import httpx

from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.session import SessionSpec, run_session

REQUEST_TIMEOUT_S = 120.0


async def run_level(
    base_url: str,
    model: str,
    concurrency: int,
    hold_seconds: float,
    sampler: CorpusSampler,
    spec: SessionSpec,
    *,
    seed: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict | None = None,
    on_record=None,
) -> tuple[list[SessionRecord], list[RequestRecord]]:
    """Run `concurrency` workers for `hold_seconds`; each worker loops full
    sessions and always completes at least one."""
    sessions: list[SessionRecord] = []
    records: list[RequestRecord] = []
    deadline = time.perf_counter() + hold_seconds
    limits = httpx.Limits(
        max_connections=concurrency + 10,
        max_keepalive_connections=concurrency + 10,
    )
    async with httpx.AsyncClient(
        limits=limits, timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        transport=transport, headers=headers,
    ) as client:

        async def worker(worker_id: int) -> None:
            # str seed: random.Random(tuple) is a TypeError on Python 3.11+
            rng = random.Random(f"{seed}-{concurrency}-{worker_id}")
            while True:
                session, recs = await run_session(
                    client, base_url, model, sampler, spec,
                    concurrency=concurrency, rng=rng,
                )
                sessions.append(session)
                records.extend(recs)
                if on_record is not None:
                    for rec in recs:
                        on_record(rec)  # may raise BudgetExceeded
                if time.perf_counter() >= deadline:
                    return

        await asyncio.gather(*(worker(i) for i in range(concurrency)))
    return sessions, records
