"""Multi-turn session runner: growing context, think time, per-turn retries."""

import asyncio
import random
import time
import uuid

import httpx
from pydantic import BaseModel, model_validator

from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.targets.openai_compatible import run_turn

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
) -> tuple[SessionRecord, list[RequestRecord]]:
    """One simulated user: sampled prompts, history grows, failed turns retry."""
    session_id = uuid.uuid4().hex[:12]
    started_at = time.time()
    t0 = time.perf_counter()
    n_turns = rng.randint(spec.turns.min, spec.turns.max)
    messages: list[dict] = []
    records: list[RequestRecord] = []
    faults: list[str] = []
    turns_completed = 0
    for turn_index in range(n_turns):
        messages.append({"role": "user", "content": sampler.sample()})
        accepted_text: str | None = None
        for attempt in range(1, spec.retry.max_attempts + 1):
            outcome = await run_turn(
                client, base_url, model, messages,
                concurrency=concurrency, session_id=session_id,
                turn_index=turn_index, attempt=attempt,
            )
            records.append(outcome.record)
            if outcome.record.fault:
                faults.append(outcome.record.fault)
            if outcome.record.ok:
                accepted_text = outcome.text
                break
            if not _retryable(outcome.record) or attempt == spec.retry.max_attempts:
                break
            await asyncio.sleep(outcome.retry_after_s or spec.retry.backoff_s * attempt)
        if accepted_text is None:
            messages.pop()  # the unanswered user turn is not part of the transcript
            break
        messages.append({"role": "assistant", "content": accepted_text})
        turns_completed += 1
        if turn_index < n_turns - 1 and spec.think_time.max_ms > 0:
            await asyncio.sleep(rng.randint(spec.think_time.min_ms, spec.think_time.max_ms) / 1000)
    session = SessionRecord(
        session_id=session_id, concurrency=concurrency, started_at=started_at,
        total_ms=(time.perf_counter() - t0) * 1000,
        turns_requested=n_turns, turns_completed=turns_completed,
        ok=turns_completed == n_turns, faults=faults, transcript=messages,
    )
    return session, records
