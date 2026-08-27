"""Ramp controller: step concurrency up and hold at each level."""

import re

import httpx
from pydantic import BaseModel

from kneepoint.analyze.budget import BudgetExceeded
from kneepoint.collect.schemas import RequestRecord, SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.pool import run_level
from kneepoint.generator.session import SessionSpec

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
    seed: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict | None = None,
    on_record=None,
    on_level=None,
) -> tuple[list[SessionRecord], list[RequestRecord], bool]:
    """Run each ramp level in order; fire on_level(level, sessions, records) after each.

    Returns (sessions, records, budget_exceeded). When the budget cap stops the
    run, records from the aborted level are lost (never returned) — the cap is a
    protective stop, not an accounting system; the CostMeter already counted them.
    """
    all_sessions: list[SessionRecord] = []
    all_records: list[RequestRecord] = []
    for level in spec.levels():
        try:
            sessions, records = await run_level(
                base_url, model, level, spec.hold_seconds, sampler, session_spec,
                seed=seed, transport=transport, headers=headers, on_record=on_record,
            )
        except BudgetExceeded:
            return all_sessions, all_records, True
        all_sessions.extend(sessions)
        all_records.extend(records)
        if on_level is not None:
            on_level(level, sessions, records)
    return all_sessions, all_records, False
