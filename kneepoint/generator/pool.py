"""Session pool: hold N concurrent multi-turn sessions on the target for a duration."""

import asyncio
import random
import time

import httpx

from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import RunEvidence
from kneepoint.generator.monitor import InjectorMonitor
from kneepoint.generator.session import SessionSpec, run_session
from kneepoint.targets.trace_context import new_run_id

REQUEST_TIMEOUT_S = 120.0


async def run_level(
    base_url: str,
    model: str,
    concurrency: int,
    hold_seconds: float,
    sampler: CorpusSampler,
    spec: SessionSpec,
    *,
    run_id: str | None = None,
    trace: bool = True,
    run_origin_s: float | None = None,
    seed: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict | None = None,
    on_record=None,
    on_attempt_start=None,
    on_attempt_complete=None,
    stop: asyncio.Event | None = None,
    evidence: RunEvidence | None = None,
    monitor_injector: bool = True,
) -> tuple[list[SessionRecord], list[RequestRecord]]:
    """Run `concurrency` workers for `hold_seconds`; each worker loops full
    sessions and always completes at least one."""
    evidence = evidence if evidence is not None else RunEvidence()
    run_id = run_id if run_id is not None else new_run_id()
    first_session, first_record = len(evidence.sessions), len(evidence.records)
    evidence.status = "running"
    evidence.level_status[concurrency] = "running"
    if stop is not None and stop.is_set():
        evidence.status = evidence.level_status[concurrency] = "stopped"
        return [], []
    level_origin = time.perf_counter()
    monitor = InjectorMonitor() if monitor_injector else None
    deadline = level_origin + hold_seconds
    if run_origin_s is not None:
        evidence.level_starts_monotonic_s[concurrency] = level_origin - run_origin_s
    limits = httpx.Limits(
        max_connections=concurrency + 10,
        max_keepalive_connections=concurrency + 10,
    )
    try:
        async with httpx.AsyncClient(
            limits=limits, timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
            transport=transport, headers=headers,
        ) as client:

            async def worker(worker_id: int) -> None:
                # str seed: random.Random(tuple) is a TypeError on Python 3.11+
                rng = random.Random(f"{seed}-{concurrency}-{worker_id}")
                while stop is None or not stop.is_set():
                    session, recs = await run_session(
                        client, base_url, model, sampler, spec,
                        concurrency=concurrency, rng=rng, run_id=run_id, trace=trace,
                        run_origin_s=run_origin_s,
                        on_attempt_start=on_attempt_start, on_attempt_complete=on_attempt_complete,
                        stop=stop, evidence=evidence,
                    )
                    if on_record is not None:
                        for rec in recs:
                            on_record(rec)  # may raise BudgetExceeded
                    if time.perf_counter() >= deadline:
                        return

            tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]
            try:
                await asyncio.gather(*tasks)
            except (Exception, asyncio.CancelledError) as exc:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                evidence.status = evidence.level_status[concurrency] = status
                raise
    except (Exception, asyncio.CancelledError) as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        evidence.status = evidence.level_status[concurrency] = status
        raise
    finally:
        if monitor is not None:
            evidence.injector_levels[concurrency] = await monitor.finish(
                evidence.records[first_record:]
            )
    status = "stopped" if stop is not None and stop.is_set() else "completed"
    evidence.status = evidence.level_status[concurrency] = status
    return evidence.sessions[first_session:], evidence.records[first_record:]
