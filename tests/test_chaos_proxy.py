import httpx
import pytest

from kneepoint.chaos.faults import FaultSpec
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.proxy import start_proxy
from tests.helpers_sse import collect_text


def _always(fault_type: str) -> ChaosInjector:
    return ChaosInjector([FaultSpec(type=fault_type, probability=1.0)], seed=0)


def _never() -> ChaosInjector:
    return ChaosInjector([], seed=0)


@pytest.fixture
def upstream(mock_agent_url):
    return mock_agent_url.removesuffix("/v1")


async def test_proxy_forwards_cleanly(upstream):
    handle = start_proxy(upstream, _never())
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{handle.url}/tool/search", params={"q": "abc"})
        assert resp.status_code == 200
        assert resp.json()["result"].startswith("kb-")
    finally:
        handle.stop()


async def test_proxy_forwards_without_per_request_client_cost(upstream):
    """Clean forwards must reuse one upstream client: building an AsyncClient per
    request costs ~1s on some Windows machines (SSL context), which pushes every
    healthy tool call past the agent's timeout and fakes 100% tool failures."""
    import time

    handle = start_proxy(upstream, _never())
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(f"{handle.url}/tool/search", params={"q": "warmup"})
            t0 = time.perf_counter()
            for _ in range(3):
                resp = await client.get(f"{handle.url}/tool/search", params={"q": "x"})
                assert resp.status_code == 200
            elapsed = time.perf_counter() - t0
        assert elapsed < 1.5, f"3 warm forwards took {elapsed:.2f}s - client per request?"
    finally:
        handle.stop()


async def test_proxy_injects_malformed_json(upstream):
    handle = start_proxy(upstream, _always("tool_malformed_json"))
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{handle.url}/tool/search",
                headers={"x-kneepoint-session": "sess42"},
            )
        assert resp.status_code == 200
        with pytest.raises(ValueError):  # json.JSONDecodeError
            resp.json()
        assert handle.log.counts == {"tool_malformed_json": 1}
        assert handle.log.by_session == {"sess42": ["tool_malformed_json"]}
    finally:
        handle.stop()


async def test_agent_through_faulty_proxy_fails_to_resolve(upstream, monkeypatch):
    """The product story end to end: tool garbage in => naive agent's answer
    loses its [RESOLVED] marker => deterministic judging will call it unresolved."""
    handle = start_proxy(upstream, _always("tool_malformed_json"))
    monkeypatch.setenv("MOCK_TOOL_URL", f"{handle.url}/tool/search")
    try:
        text = await collect_text(f"{upstream}/v1", [{"role": "user", "content": "help"}])
        assert "[TOOL-ERROR parse]" in text
        assert "[RESOLVED" not in text
    finally:
        handle.stop()


async def test_stop_with_inflight_timeout_hold_is_quiet(upstream):
    """stop() cancels handlers held in the tool_timeout sleep. That cancellation
    must not surface as uvicorn 'Exception in ASGI application' tracebacks —
    `kneepoint demo` stops the proxy on every run and its output must stay clean."""
    import asyncio
    import contextlib
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture(level=logging.ERROR)
    uvicorn_logger = logging.getLogger("uvicorn.error")
    handle = start_proxy(upstream, _always("tool_timeout"))
    # attach AFTER start_proxy: uvicorn's dictConfig replaces this logger's
    # handlers during startup and would silently drop an earlier attachment
    uvicorn_logger.addHandler(capture)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            task = asyncio.create_task(client.get(f"{handle.url}/tool/search"))
            await asyncio.sleep(0.3)          # let the request reach the 30s hold
            handle.stop()                      # cancels the held handler
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
    finally:
        uvicorn_logger.removeHandler(capture)
        handle.stop()
    assert not records, [r.getMessage() for r in records]


async def test_agent_through_timing_out_proxy(upstream, monkeypatch):
    handle = start_proxy(upstream, _always("tool_timeout"))
    monkeypatch.setenv("MOCK_TOOL_URL", f"{handle.url}/tool/search")
    monkeypatch.setenv("MOCK_TOOL_TIMEOUT_MS", "100")   # keep the test fast
    try:
        text = await collect_text(f"{upstream}/v1", [{"role": "user", "content": "help"}])
        assert "[TOOL-ERROR timeout]" in text
    finally:
        handle.stop()
