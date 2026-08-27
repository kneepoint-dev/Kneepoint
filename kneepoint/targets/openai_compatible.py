"""Target adapter: OpenAI-compatible streaming chat completions."""

import asyncio
import json
import statistics
import time
import uuid

import httpx
from pydantic import BaseModel

from kneepoint.collect.schemas import RequestRecord

_DATA_PREFIX = "data: "

# Total wall for one request, measured from the first byte sent. httpx's own
# timeout is a *gap* timeout — it only fires when no byte arrives for that long,
# so a stream that trickles forever never trips it. This is the wall a request
# is abandoned at; abandonment is recorded, never silently folded into latency.
REQUEST_WALL_S = 120.0


class TurnOutcome(BaseModel):
    record: RequestRecord
    text: str = ""
    retry_after_s: float | None = None


def _pct(values: list[float], p: int) -> float:
    """Same inclusive method the ramp analysis uses, so percentiles agree."""
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(sorted(values), n=100, method="inclusive")[p - 1]


def _content_delta(chunk: dict) -> str | None:
    choices = chunk.get("choices") or []
    if not choices:
        return None
    return (choices[0].get("delta") or {}).get("content")


def _record(session_id, concurrency, turn_index, attempt, started_at, t0, **kw) -> RequestRecord:
    return RequestRecord(
        session_id=session_id, concurrency=concurrency, turn_index=turn_index,
        attempt=attempt, started_at=started_at,
        total_ms=(time.perf_counter() - t0) * 1000, **kw,
    )


async def run_turn(
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
) -> TurnOutcome:
    """Run one streamed chat turn; record TTFT, latency, tokens, and text.

    Never raises: transport failures and HTTP >= 400 come back as ok=False
    records — a load tool must keep generating load while requests die.

    Abandonment (the wall below, or httpx's gap timeout) closes the response
    before anything else happens, which shuts the socket and makes the server
    see a client disconnect immediately. Whether the server then *stops
    generating* is the server's choice, not ours — see docs/measurement-integrity.md.
    The record is marked `abandoned` either way so contaminated levels can be flagged.
    """
    wall = REQUEST_WALL_S if wall_seconds is None else wall_seconds
    started_at = time.time()
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    status_code: int | None = None
    fault: str | None = None
    pieces: list[str] = []
    gaps_ms: list[float] = []
    last_chunk_at = t0
    try:
        # The wall wraps the whole exchange, including the silent prefill gap
        # where no bytes arrive — that is the gap httpx's timeout resets on and
        # the one Run D's requests died in. On expiry the read is cancelled and
        # `client.stream`'s teardown closes the response, which shuts the socket.
        async with asyncio.timeout(wall), client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"x-kneepoint-session": session_id},
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as resp:
            status_code = resp.status_code
            fault = resp.headers.get("x-kneepoint-fault")
            if resp.status_code >= 400:
                retry_after = resp.headers.get("retry-after")
                return TurnOutcome(
                    record=_record(
                        session_id, concurrency, turn_index, attempt, started_at, t0,
                        ok=False, status_code=resp.status_code, fault=fault,
                        error=f"HTTP {resp.status_code}",
                    ),
                    retry_after_s=float(retry_after) if retry_after else None,
                )
            async for line in resp.aiter_lines():
                if not line.startswith(_DATA_PREFIX):
                    continue
                payload = line[len(_DATA_PREFIX):]
                if payload.strip() == "[DONE]":
                    break
                chunk = json.loads(payload)
                content = _content_delta(chunk)
                if content:
                    now = time.perf_counter()
                    if ttft_ms is None:
                        ttft_ms = (now - t0) * 1000
                    else:
                        # gap since the previous *content* chunk; a usage-only
                        # chunk is not a token and must not open a gap
                        gaps_ms.append((now - last_chunk_at) * 1000)
                    last_chunk_at = now
                    pieces.append(content)
                usage = chunk.get("usage")
                if usage:
                    input_tokens = usage.get("prompt_tokens")
                    output_tokens = usage.get("completion_tokens")
        return TurnOutcome(
            record=_record(
                session_id, concurrency, turn_index, attempt, started_at, t0,
                # the status the server sent, not a literal 200: a 2xx that is
                # not 200 still streams, and the record must say what arrived
                ok=True, status_code=status_code, fault=fault, ttft_ms=ttft_ms,
                input_tokens=input_tokens, output_tokens=output_tokens,
                chunk_count=len(pieces),
                # one chunk defines no gap: None, not 0
                itl_mean_ms=statistics.fmean(gaps_ms) if gaps_ms else None,
                itl_p99_ms=_pct(gaps_ms, 99) if gaps_ms else None,
            ),
            text="".join(pieces),
        )
    except TimeoutError:  # the wall
        return TurnOutcome(
            record=_record(
                session_id, concurrency, turn_index, attempt, started_at, t0,
                ok=False, status_code=status_code, fault=fault, ttft_ms=ttft_ms,
                abandoned=status_code is not None,
                error=f"abandoned at the {wall:g}s wall",
            ),
        )
    except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
        # We gave up on a request the server had already accepted. Leaving the
        # `async with` closed the response on the way out, so the disconnect is
        # already on the wire by the time we get here (tests/test_cancellation.py
        # asserts the server observes it). Connect/pool timeouts are NOT
        # abandonment — no upstream work ever started — so they fall through.
        return TurnOutcome(
            record=_record(
                session_id, concurrency, turn_index, attempt, started_at, t0,
                ok=False, ttft_ms=ttft_ms, abandoned=True,
                error=f"{type(exc).__name__}: {exc}"[:200],
            ),
        )
    except Exception as exc:  # noqa: BLE001 - record the failure, keep the run alive
        return TurnOutcome(
            record=_record(
                session_id, concurrency, turn_index, attempt, started_at, t0,
                ok=False, ttft_ms=ttft_ms, error=f"{type(exc).__name__}: {exc}"[:200],
            ),
        )


async def run_single_turn(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    concurrency: int,
) -> RequestRecord:
    """Single-turn wrapper over `run_turn`: one user message, record only."""
    outcome = await run_turn(
        client, base_url, model,
        [{"role": "user", "content": prompt}],
        concurrency=concurrency, session_id=uuid.uuid4().hex[:12],
    )
    return outcome.record
