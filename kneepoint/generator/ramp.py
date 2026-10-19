"""Ramp controller: step concurrency up and hold at each level."""

import asyncio
import re
import time

import httpx
from pydantic import BaseModel

from kneepoint.analyze.budget import BudgetExceeded
from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import RunEvidence
from kneepoint.generator.pool import run_level
from kneepoint.generator.session import SessionSpec
from kneepoint.targets.trace_context import new_run_id

_RAMP_RE = re.compile(r"^(\d+)\.\.(\d+)$")


def parse_ramp(text: str) -> tuple[int, int]:
    """Parse '1..50' into (1, 50)."""
    m = _RAMP_RE.match(text.strip())
    if not m:
        raise ValueError(f"ramp must look like '1..50', got {text!r}")
    start, stop = int(m.group(1)), int(m.group(2))
    if start < 1 or stop < start:
        raise ValueError(f"ramp needs 1 <= start <= stop, got {text!r}")
    return start, stop


class RampSpec(BaseModel):
    start: int = 1
    stop: int = 50
    step: int = 5
    hold_seconds: float = 15.0

    def levels(self) -> list[int]:
        levels = list(range(self.start, self.stop + 1, self.step))
        if levels[-1] != self.stop:
            levels.append(self.stop)
        return levels


async def run_ramp(
    base_url: str,
    model: str,
    spec: RampSpec,
    sampler: CorpusSampler,
    session_spec: SessionSpec,
    *,
    run_id: str | None = None,
    trace: bool = True,
    run_origin_s: float | None = None,
    seed: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict | None = None,
    on_record=None,
    on_level=None,
    on_attempt_start=None,
    on_attempt_complete=None,
    stop: asyncio.Event | None = None,
    evidence: RunEvidence | None = None,
    monitor_injector: bool = True,
) -> tuple[list[SessionRecord], list[RequestRecord], bool]:
    """Return (sessions, records, budget_exceeded), retaining partial evidence.

    Legacy on_record still fires only when a session returns. New completion
    observers see each completed attempt immediately and must not double-meter it.
    Caller-owned evidence also survives exceptions and external cancellation.
    """
    evidence = evidence if evidence is not None else RunEvidence()
    run_origin_s = time.perf_counter() if run_origin_s is None else run_origin_s
    run_id = run_id if run_id is not None else new_run_id()
    first_session, first_record = len(evidence.sessions), len(evidence.records)
    evidence.status = "running"
    try:
        for level in spec.levels():
            if stop is not None and stop.is_set():
                evidence.status = "stopped"
                break
            sessions, records = await run_level(
                base_url, model, level, spec.hold_seconds, sampler, session_spec,
                run_id=run_id, trace=trace, seed=seed, transport=transport,
                run_origin_s=run_origin_s,
                headers=headers, on_record=on_record,
                on_attempt_start=on_attempt_start, on_attempt_complete=on_attempt_complete,
                stop=stop, evidence=evidence,
                monitor_injector=monitor_injector,
            )
            if on_level is not None:
                on_level(level, sessions, records)
            if stop is not None and stop.is_set():
                evidence.status = "stopped"
                break
        else:
            evidence.status = "completed"
    except BudgetExceeded:
        evidence.status = "budget_exceeded"
        if "level" in locals():
            evidence.level_status[level] = "budget_exceeded"
        return evidence.sessions[first_session:], evidence.records[first_record:], True
    except (Exception, asyncio.CancelledError) as exc:
        evidence.status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        raise
    return evidence.sessions[first_session:], evidence.records[first_record:], False
