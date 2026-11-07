"""Shared result and call contract for one load attempt."""

from typing import Protocol

import httpx
from pydantic import BaseModel

from kneepoint.collect.schemas import RequestRecord

# The wall bounds an entire exchange even when bytes keep resetting the gap timeout.
REQUEST_WALL_S = 120.0


class TurnOutcome(BaseModel):
    record: RequestRecord
    text: str = ""
    retry_after_s: float | None = None


class TurnRunner(Protocol):
    async def __call__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        messages: list[dict],
        *,
        concurrency: int,
        session_id: str,
        turn_index: int = 0,
        attempt: int = 1,
        wall_seconds: float | None = None,
        run_id: str | None = None,
        trace: bool = True,
        run_origin_s: float | None = None,
    ) -> TurnOutcome: ...
