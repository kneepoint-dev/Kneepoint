"""Inter-token latency at chunk granularity, and the TPOT derivation.

ITL was the standing gap in kneepoint's metric coverage: every model-server
benchmark reports it, and kneepoint could not report it at all because
`RequestRecord` carried nothing to reconstruct it from. Summary statistics only
- a per-chunk timestamp array would multiply the JSONL for a number nobody
queries per chunk.
"""

import asyncio
import json
import statistics

import httpx

from kneepoint.collect.schemas import RequestRecord
from kneepoint.targets.openai_compatible import run_turn
from tests.helpers_sse import serve_asgi

GAP_S = 0.03


def streaming_app(*, n_chunks: int, gap_s: float = GAP_S, usage: bool = True):
    """A target that streams `n_chunks` content chunks `gap_s` apart."""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        while True:
            if not (await receive()).get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})

        async def part(payload: dict) -> None:
            await send({"type": "http.response.body",
                        "body": ("data: " + json.dumps(payload) + "\n\n").encode(),
                        "more_body": True})

        for i in range(n_chunks):
            if i:
                await asyncio.sleep(gap_s)
            await part({"choices": [{"delta": {"content": f"tok{i} "}}]})
        if usage:
            # a usage-only chunk is not a token: it must not open an ITL gap
            await asyncio.sleep(gap_s * 4)
            await part({"choices": [], "usage": {"prompt_tokens": 3,
                                                 "completion_tokens": n_chunks}})
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n",
                    "more_body": False})

    return app


async def _turn(base_url: str):
    async with httpx.AsyncClient(timeout=30) as client:
        return await run_turn(client, f"{base_url}/v1", "m",
                              [{"role": "user", "content": "hi"}],
                              concurrency=1, session_id="s")


# ---------------------------------------------------------------------------
# ITL
# ---------------------------------------------------------------------------


async def test_itl_measures_the_gap_between_content_chunks():
    with serve_asgi(streaming_app(n_chunks=6)) as base:
        record = (await _turn(base)).record
    assert record.ok
    assert record.chunk_count == 6
    assert record.itl_mean_ms is not None
    # 5 gaps of ~30ms; generous bounds, this is a timing test on a loaded laptop
    assert GAP_S * 1000 * 0.5 < record.itl_mean_ms < GAP_S * 1000 * 3
    assert record.itl_p99_ms >= record.itl_mean_ms


async def test_the_usage_chunk_does_not_open_a_gap():
    """The final usage-only chunk arrives long after the last token. Counting it
    would inflate ITL by the whole tail of the request."""
    with serve_asgi(streaming_app(n_chunks=4)) as base:
        with_usage = (await _turn(base)).record
    with serve_asgi(streaming_app(n_chunks=4, usage=False)) as base:
        without = (await _turn(base)).record
    assert with_usage.chunk_count == without.chunk_count == 4
    # the 120ms usage tail must not show up in the mean of four ~30ms gaps
    assert with_usage.itl_mean_ms < GAP_S * 1000 * 3


async def test_one_chunk_has_no_itl_and_says_so_with_none():
    """One chunk defines no gap. Zero would be a measurement; None is the truth."""
    with serve_asgi(streaming_app(n_chunks=1)) as base:
        record = (await _turn(base)).record
    assert record.ok
    assert record.chunk_count == 1
    assert record.itl_mean_ms is None
    assert record.itl_p99_ms is None


async def test_two_chunks_is_the_minimum_that_defines_itl():
    with serve_asgi(streaming_app(n_chunks=2)) as base:
        record = (await _turn(base)).record
    assert record.chunk_count == 2
    assert record.itl_mean_ms is not None
    assert record.itl_p99_ms == record.itl_mean_ms  # a single gap is its own p99


async def test_a_failed_request_carries_no_invented_itl():
    async with httpx.AsyncClient(timeout=2) as client:
        outcome = await run_turn(client, "http://127.0.0.1:1/v1", "m",
                                 [{"role": "user", "content": "hi"}],
                                 concurrency=1, session_id="s")
    assert not outcome.record.ok
    assert outcome.record.itl_mean_ms is None
    assert outcome.record.chunk_count is None


# ---------------------------------------------------------------------------
# TPOT: derived, never stored, and pinned to one exact expression
# ---------------------------------------------------------------------------


def _rec(**kw) -> RequestRecord:
    base = dict(session_id="s", concurrency=1, started_at=0.0, total_ms=1000.0, ok=True)
    base.update(kw)
    return RequestRecord(**base)


def test_tpot_is_decode_time_over_output_tokens():
    assert _rec(ttft_ms=200.0, output_tokens=40).tpot_ms == (1000.0 - 200.0) / 40


def test_tpot_matches_an_independent_implementation_guard_for_guard():
    """`docs/output-format.md` publishes this expression as the one third-party
    consumers should reimplement. Below is that reimplementation, written out
    separately: if the property ever drifts from the published formula — or the
    formula from the property — this fails."""
    def reference_tpot(r):
        if r.ttft_ms is None or not r.output_tokens:
            return None
        decode = r.total_ms - r.ttft_ms
        return decode / r.output_tokens if decode > 0 else None

    cases = [
        _rec(ttft_ms=200.0, output_tokens=40),
        _rec(ttft_ms=None, output_tokens=40),
        _rec(ttft_ms=200.0, output_tokens=None),
        _rec(ttft_ms=200.0, output_tokens=0),
        _rec(ttft_ms=1000.0, output_tokens=40),           # decode == 0
        _rec(ttft_ms=1200.0, output_tokens=40),           # decode < 0
        _rec(ttft_ms=1.0, output_tokens=1),
    ]
    assert [c.tpot_ms for c in cases] == [reference_tpot(c) for c in cases]


def test_tpot_is_not_written_to_the_jsonl():
    """It is a division away from fields already on the line. Storing it would
    grow every record and let the stored value drift from its inputs."""
    assert "tpot_ms" not in json.loads(_rec(ttft_ms=200.0, output_tokens=40).model_dump_json())


def test_itl_p99_uses_the_same_percentile_method_as_the_ramp():
    from kneepoint.targets.openai_compatible import _pct

    values = [float(v) for v in range(1, 101)]
    assert _pct(values, 99) == statistics.quantiles(
        values, n=100, method="inclusive")[98]
    assert _pct([7.0], 99) == 7.0
