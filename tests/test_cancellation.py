"""Abandonment: when kneepoint gives up on a request, what does the server see?

Run D finding 4 — a request abandoned at the client wall kept its slot on the
server, so the *next* request paid for work nobody was waiting for (0.8s -> 8.6s
measured on Ollama). These tests pin down the half kneepoint controls (close the
connection the instant we give up, and mark the record) and measure the half it
does not (whether the server acts on the disconnect).
"""

import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn

from kneepoint.analyze.knee import aggregate
from kneepoint.collect.schemas import RequestRecord
from kneepoint.targets.openai_compatible import run_turn

PREFILL_S = 1.5   # server thinks this long before the first byte leaves
GEN_S = 1.0       # ...then streams for this long
WALL_S = 0.5      # client gives up here — inside the prefill gap


class SlowTarget:
    """Capacity-1 OpenAI-compatible target with a long silent prefill.

    `abort_on_disconnect` is the server-side policy under test: True models a
    server that cancels the generation when the client goes away, False models
    one (Ollama, and any ASGI app that never reads `receive()`) that finishes
    the work regardless.
    """

    def __init__(self, *, abort_on_disconnect: bool) -> None:
        self.abort_on_disconnect = abort_on_disconnect
        self.disconnect_times: list[float] = []
        self.finished: list[str] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    # -- ASGI ------------------------------------------------------------
    def _app(self):
        semaphore = asyncio.Semaphore(1)

        async def chat(scope, receive, send):
            body = json.loads((await receive()).get("body") or b"{}")
            slow = "LONG" in json.dumps(body.get("messages") or [])
            prefill, gen = (PREFILL_S, GEN_S) if slow else (0.02, 0.05)
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})

            gone = asyncio.Event()

            async def watch():
                while True:
                    if (await receive())["type"] == "http.disconnect":
                        self.disconnect_times.append(time.perf_counter())
                        gone.set()
                        return

            watcher = asyncio.create_task(watch())

            async def rest(seconds: float) -> bool:
                """Sleep, or bail early if the client left and we honour that."""
                if not self.abort_on_disconnect:
                    await asyncio.sleep(seconds)
                    return False
                try:
                    await asyncio.wait_for(gone.wait(), timeout=seconds)
                except TimeoutError:
                    return False
                return True

            try:
                async with semaphore:  # the queue that makes one request block the next
                    if await rest(prefill):
                        self.finished.append("aborted")
                        return
                    for i in range(10):
                        await send({
                            "type": "http.response.body",
                            "body": ("data: " + json.dumps(
                                {"choices": [{"delta": {"content": f"tok{i} "}}]}
                            ) + "\n\n").encode(),
                            "more_body": True,
                        })
                        if await rest(gen / 10):
                            self.finished.append("aborted")
                            return
                    self.finished.append("complete")
                await send({"type": "http.response.body", "body": b"data: [DONE]\n\n",
                            "more_body": False})
            finally:
                watcher.cancel()

        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
                    return
            await chat(scope, receive, send)

        return app

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "SlowTarget":
        config = uvicorn.Config(self._app(), host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("slow target failed to start within 10s")
            time.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/v1"
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _msg(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


async def _turn(client, url, text, **kw):
    return await run_turn(client, url, "m", _msg(text), concurrency=1,
                          session_id="s", **kw)


# ---------------------------------------------------------------------------
# what kneepoint controls: give up, close the socket, say so
# ---------------------------------------------------------------------------


async def test_wall_abandons_a_stream_that_never_finishes():
    """httpx's timeout is a *gap* timeout, so a trickling stream never trips it.
    The wall is what stops it, and the record says the client gave up."""
    with SlowTarget(abort_on_disconnect=False) as target:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            outcome = await _turn(client, target.url, "LONG one", wall_seconds=0.05)
    record = outcome.record
    assert record.ok is False
    assert record.abandoned is True
    assert "wall" in record.error
    assert record.total_ms < PREFILL_S * 1000, "the wall must fire before the server does"


async def test_read_timeout_is_recorded_as_abandonment():
    with SlowTarget(abort_on_disconnect=False) as target:
        async with httpx.AsyncClient(timeout=httpx.Timeout(WALL_S)) as client:
            outcome = await _turn(client, target.url, "LONG one")
    assert outcome.record.ok is False
    assert outcome.record.abandoned is True
    assert "Timeout" in outcome.record.error


async def test_transport_errors_are_not_abandonment():
    """A connection that never landed left no work running upstream — calling
    that 'abandoned' would flag levels that are perfectly clean."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(2)) as client:
        # port 1 on loopback: refused immediately, no server involved
        outcome = await _turn(client, "http://127.0.0.1:1/v1", "hello")
    assert outcome.record.ok is False
    assert outcome.record.abandoned is False


async def test_server_sees_the_disconnect_when_we_give_up():
    """The proof that we actually terminate the upstream stream rather than
    dropping the task: the target observes http.disconnect at the wall."""
    with SlowTarget(abort_on_disconnect=False) as target:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            t0 = time.perf_counter()
            outcome = await _turn(client, target.url, "LONG one", wall_seconds=WALL_S)
            gave_up_at = time.perf_counter()
        assert outcome.record.abandoned is True
        # uvicorn only surfaces http.disconnect once the socket closes
        deadline = time.time() + 2
        while not target.disconnect_times and time.time() < deadline:
            time.sleep(0.02)
        assert target.disconnect_times, "server never saw the client leave"
        lag = target.disconnect_times[0] - gave_up_at
        assert lag < 0.5, f"disconnect reached the server {lag * 1000:.0f}ms late"
        assert gave_up_at - t0 == pytest.approx(WALL_S, abs=0.4)


# ---------------------------------------------------------------------------
# what the server controls: does the abandoned work actually stop?
# ---------------------------------------------------------------------------


async def _abandon_then_measure(target: SlowTarget) -> tuple[float, float]:
    """Baseline short request on an idle server, then the same request issued
    straight after a long one was abandoned. Returns (baseline_ms, after_ms)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        baseline = await _turn(client, target.url, "short")
        assert baseline.record.ok
        abandoned = await _turn(client, target.url, "LONG one", wall_seconds=WALL_S)
        assert abandoned.record.abandoned
        after = await _turn(client, target.url, "short")
    return baseline.record.total_ms, after.record.total_ms


async def test_next_request_is_clean_when_the_server_honours_the_disconnect():
    """The fix works end to end *if the server cooperates*: the level after an
    abandoned request is measuring itself, not the abandoned generation."""
    with SlowTarget(abort_on_disconnect=True) as target:
        baseline_ms, after_ms = await _abandon_then_measure(target)
    assert target.finished == ["complete", "aborted", "complete"]
    assert after_ms < baseline_ms + 400, (
        f"next request took {after_ms:.0f}ms vs {baseline_ms:.0f}ms baseline - "
        "the abandoned generation was still holding the slot"
    )


async def test_a_server_that_ignores_the_disconnect_contaminates_the_next_request():
    """The honest other half: kneepoint cannot force cancellation. This is the
    behaviour docs/measurement-integrity.md tells users to expect, and the reason
    aggregate() flags the levels instead of pretending they are comparable."""
    with SlowTarget(abort_on_disconnect=False) as target:
        baseline_ms, after_ms = await _abandon_then_measure(target)
    assert target.finished == ["complete", "complete", "complete"]
    assert after_ms > baseline_ms * 3, (
        f"expected the leftover generation to inflate the next request, got "
        f"{after_ms:.0f}ms vs {baseline_ms:.0f}ms"
    )


# ---------------------------------------------------------------------------
# and the flag that carries it into the numbers
# ---------------------------------------------------------------------------


def _rec(level: int, ms: float, *, ok: bool = True, abandoned: bool = False) -> RequestRecord:
    return RequestRecord(session_id="s", concurrency=level, started_at=0.0,
                         total_ms=ms, ok=ok, abandoned=abandoned)


def test_contamination_travels_forward_through_the_ramp():
    stats = aggregate([
        _rec(1, 100.0), _rec(1, 110.0),
        _rec(2, 200.0), _rec(2, 900.0, ok=False, abandoned=True),
        _rec(3, 300.0),
    ])
    assert [s.contaminated for s in stats] == [False, True, True]
    assert [s.abandoned for s in stats] == [0, 1, 0]


def test_a_fully_abandoned_level_still_contaminates_the_next():
    """The level is dropped for having no latency signal — but its abandoned
    work is still running, so the level after it is not independent."""
    stats = aggregate([
        _rec(1, 100.0),
        _rec(2, 900.0, ok=False, abandoned=True),
        _rec(3, 300.0),
    ])
    assert [s.concurrency for s in stats] == [1, 3]
    assert [s.contaminated for s in stats] == [False, True]


def test_a_clean_run_is_not_flagged():
    stats = aggregate([_rec(1, 100.0), _rec(2, 200.0), _rec(2, 300.0, ok=False)])
    assert not any(s.contaminated for s in stats)
    assert sum(s.abandoned for s in stats) == 0
