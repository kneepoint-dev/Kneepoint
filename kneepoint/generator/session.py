"""Multi-turn session runner: growing context, think time, per-turn retries."""

import asyncio
import random
import time
import uuid

import httpx
from pydantic import BaseModel, model_validator

from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import AttemptStart, RunEvidence, wait_or_stop
from kneepoint.targets.openai_compatible import run_turn
from kneepoint.targets.trace_context import new_run_id

RETRYABLE_STATUS = {429, 500, 502, 503}


class TurnsSpec(BaseModel):
    min: int = 1
    max: int = 1

    @model_validator(mode="after")
    def _ordered(self) -> "TurnsSpec":
        if not 1 <= self.min <= self.max:
            raise ValueError(f"turns need 1 <= min <= max, got {self.min}..{self.max}")
        return self


class ThinkTime(BaseModel):
    min_ms: int = 0
    max_ms: int = 0


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_s: float = 0.5


class SessionSpec(BaseModel):
    turns: TurnsSpec = TurnsSpec()
    think_time: ThinkTime = ThinkTime()
    retry: RetryPolicy = RetryPolicy()


def _retryable(record: RequestRecord) -> bool:
    if record.ok:
        return False
    return record.status_code is None or record.status_code in RETRYABLE_STATUS


async def run_session(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    sampler: CorpusSampler,
    spec: SessionSpec,
    *,
    concurrency: int,
    rng: random.Random,
    run_id: str | None = None,
    trace: bool = True,
    run_origin_s: float | None = None,
    on_attempt_start=None,
    on_attempt_complete=None,
    stop: asyncio.Event | None = None,
    evidence: RunEvidence | None = None,
) -> tuple[SessionRecord, list[RequestRecord]]:
    """One simulated user: sampled prompts, history grows, failed turns retry."""
    session_id = uuid.uuid4().hex[:12]
    run_id = run_id if run_id is not None else new_run_id()
    started_at = time.time()
    t0 = time.perf_counter()
    n_turns = rng.randint(spec.turns.min, spec.turns.max)
    messages: list[dict] = []
    records: list[RequestRecord] = []
    faults: list[str] = []
    turns_completed = 0
    evidence = evidence if evidence is not None else RunEvidence()
    status = "completed"
    active: AttemptStart | None = None
    try:
        for turn_index in range(n_turns):
            if stop is not None and stop.is_set():
                status = "stopped"
                break
            messages.append({"role": "user", "content": sampler.sample()})
            accepted_text: str | None = None
            for attempt in range(1, spec.retry.max_attempts + 1):
                if stop is not None and stop.is_set():
                    status = "stopped"
                    break
                active = AttemptStart(session_id, concurrency, turn_index, attempt, time.time())
                evidence.start(active)
                if on_attempt_start is not None:
                    on_attempt_start(active)
                if stop is not None and stop.is_set():
                    status = "stopped"
                    evidence.finish_attempt(active, "stopped")
                    active = None
                    break
                evidence.attempts[active.key]["dispatched"] = True
                outcome = await run_turn(
                    client, base_url, model, messages,
                    concurrency=concurrency, session_id=session_id,
                    turn_index=turn_index, attempt=attempt, run_id=run_id, trace=trace,
                    run_origin_s=run_origin_s,
                )
                records.append(outcome.record)
                evidence.records.append(outcome.record)
                evidence.finish_attempt(active, "completed")
                active = None
                if outcome.record.fault:
                    faults.append(outcome.record.fault)
                if outcome.record.ok:
                    accepted_text = outcome.text
                    messages.append({"role": "assistant", "content": accepted_text})
                    turns_completed += 1
                if on_attempt_complete is not None:
                    on_attempt_complete(outcome.record)
                if outcome.record.ok:
                    break
                if not _retryable(outcome.record) or attempt == spec.retry.max_attempts:
                    break
                await wait_or_stop(outcome.retry_after_s or spec.retry.backoff_s * attempt, stop)
            if accepted_text is None:
                messages.pop()
                break
            if turn_index < n_turns - 1 and spec.think_time.max_ms > 0:
                await wait_or_stop(
                    rng.randint(spec.think_time.min_ms, spec.think_time.max_ms) / 1000, stop)
    except (Exception, asyncio.CancelledError) as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        if active is not None:
            evidence.finish_attempt(active, status)
        # An unanswered turn is not a completed transcript contribution.
        if messages and messages[-1]["role"] == "user":
            messages.pop()
        raise
    finally:
        session = SessionRecord(
            session_id=session_id, concurrency=concurrency, started_at=started_at,
            total_ms=(time.perf_counter() - t0) * 1000,
            turns_requested=n_turns, turns_completed=turns_completed,
            ok=turns_completed == n_turns, faults=faults, transcript=messages,
        )
        evidence.sessions.append(session)
        evidence.session_status[session_id] = status
    return session, records
