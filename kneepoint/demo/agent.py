"""Bundled demo agent: plain ASGI, no web framework — it ships in the wheel.

Deliberately naive: a fixed internal capacity creates the knee; a simulated
tool call ends healthy answers with "[RESOLVED <result>]" and produces naive
error markers on tool trouble — one attempt, no retry, no validation. That
silent-failure behavior is what kneepoint exists to expose. `MOCK_*` env knobs
match the old examples/mock_agent app; create_app() args override them for
in-process embedding (the `kneepoint demo` command).
"""

import asyncio
import json
import os
import time
import uuid
from urllib.parse import parse_qs

import httpx

CAPACITY = int(os.getenv("MOCK_CAPACITY", "8"))
BASE_DELAY_MS = int(os.getenv("MOCK_BASE_DELAY_MS", "200"))
TOKEN_DELAY_MS = int(os.getenv("MOCK_TOKEN_DELAY_MS", "20"))
OUTPUT_TOKENS = int(os.getenv("MOCK_OUTPUT_TOKENS", "40"))

_tool_client: httpx.AsyncClient | None = None
_tool_client_loop: asyncio.AbstractEventLoop | None = None


def _get_tool_client() -> httpx.AsyncClient:
    # shared client: constructing one per call costs up to ~1s on some Windows
    # machines (SSL context build) and would trip the tool timeout on healthy
    # calls; timeout is passed per request instead. The client is bound to the
    # event loop it was created on — when the agent is embedded in-process
    # (kneepoint demo) and later served again on a fresh loop, reuse would hit
    # "Event loop is closed", so recreate per loop.
    global _tool_client, _tool_client_loop
    loop = asyncio.get_running_loop()
    if _tool_client is None or _tool_client_loop is not loop:
        _tool_client = httpx.AsyncClient()
        _tool_client_loop = loop
    return _tool_client


def _tool_env() -> tuple[str, float, float]:
    return (
        os.getenv("MOCK_TOOL_URL", ""),
        int(os.getenv("MOCK_TOOL_TIMEOUT_MS", "1000")) / 1000,
        int(os.getenv("MOCK_TOOL_DELAY_MS", "25")) / 1000,
    )


async def _call_tool(query: str, session_header: str | None) -> str:
    """Naive tool step: one attempt, no retry, no validation of garbage."""
    tool_url, timeout_s, delay_s = _tool_env()
    if not tool_url:
        await asyncio.sleep(delay_s)
        return f"[RESOLVED kb-{len(query) % 7}]"
    headers = {"x-kneepoint-session": session_header} if session_header else {}
    try:
        resp = await _get_tool_client().get(
            tool_url, params={"q": query}, headers=headers, timeout=timeout_s
        )
        return f"[RESOLVED {resp.json()['result']}]"
    except (httpx.TimeoutException, httpx.ConnectError):
        return "[TOOL-ERROR timeout]"
    except (json.JSONDecodeError, KeyError):
        return "[TOOL-ERROR parse]"


async def _send_json(send, status: int, payload: dict) -> None:
    await send({
        "type": "http.response.start", "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            return body


def create_app(
    *,
    capacity: int | None = None,
    base_delay_ms: int | None = None,
    token_delay_ms: int | None = None,
    output_tokens: int | None = None,
):
    cap = capacity if capacity is not None else CAPACITY
    base_delay_s = (base_delay_ms if base_delay_ms is not None else BASE_DELAY_MS) / 1000
    token_delay_s = (token_delay_ms if token_delay_ms is not None else TOKEN_DELAY_MS) / 1000
    n_tokens = output_tokens if output_tokens is not None else OUTPUT_TOKENS
    semaphore = asyncio.Semaphore(cap)

    async def chat(scope, receive, send) -> None:
        body = json.loads((await _read_body(receive)) or b"{}")
        model = body.get("model", "mock")
        messages = body.get("messages") or [{}]
        prompt = str(messages[-1].get("content", ""))
        prompt_tokens = max(1, sum(len(str(m.get("content", "")).split()) for m in messages))
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        session_header = headers.get("x-kneepoint-session")
        comp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        def sse(payload: dict) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode()

        def chunk(delta: dict, finish_reason: str | None = None) -> dict:
            return {
                "id": comp_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache")],
        })

        async def part(data: bytes) -> None:
            await send({"type": "http.response.body", "body": data, "more_body": True})

        async with semaphore:  # waiting in this queue is what creates the knee
            await asyncio.sleep(base_delay_s)
            await part(sse(chunk({"role": "assistant"})))
            for i in range(n_tokens):
                await part(sse(chunk({"content": f"tok{i} "})))
                await asyncio.sleep(token_delay_s)
            await part(sse(chunk({"content": await _call_tool(prompt, session_header)})))
            await part(sse(chunk({}, finish_reason="stop")))
            await part(sse({
                "id": comp_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": n_tokens + 1,
                    "total_tokens": prompt_tokens + n_tokens + 1,
                },
            }))
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        assert scope["type"] == "http"
        method, path = scope["method"], scope["path"]
        if method == "GET" and path == "/":
            await _send_json(send, 200, {
                "status": "ok", "capacity": cap,
                "tool_url": _tool_env()[0] or "in-process",
            })
        elif method == "GET" and path == "/tool/search":
            q = parse_qs(scope["query_string"].decode()).get("q", [""])[0]
            await asyncio.sleep(_tool_env()[2])
            await _send_json(send, 200, {"result": f"kb-{len(q) % 7}"})
        elif method == "POST" and path == "/v1/chat/completions":
            await chat(scope, receive, send)
        else:
            await _send_json(send, 404, {"detail": "Not Found"})

    return app


app = create_app()
