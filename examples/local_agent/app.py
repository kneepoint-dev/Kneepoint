"""local_agent: wrap a local model server (Ollama/MLX) as a kneepoint target.

Ramping a bare model server measures inference queueing — that is GuideLLM's
job, not ours. Kneepoint measures *agents*: multi-turn sessions, a tool step
that can fail, and a task that either gets resolved or does not.
This wrapper restores those three things around a raw completions endpoint,
and otherwise preserves the demo agent's contract exactly (plain ASGI,
OpenAI-compatible SSE, role → content → stop → usage → [DONE]).

Two rules it will not bend:

- **The model earns the marker.** The system prompt asks for `[RESOLVED]`; the
  wrapper never appends it. Stamping the marker on model output would
  manufacture the resolution rate and void the entire measurement.
- **Honest `None` over an estimate.** Token counts are the upstream's real
  numbers. If upstream reports no usage, no usage chunk is emitted.

The tool step is deliberately naive — one attempt, no retry, no validation —
matching kneepoint.demo.agent, and is routed through `LOCAL_AGENT_TOOL_URL` so
the chaos proxy's `tool_timeout` / `tool_malformed_json` faults land on it.

Env knobs:
  LOCAL_AGENT_UPSTREAM        default http://127.0.0.1:11434/v1
  LOCAL_AGENT_MODEL           override the model name from the request body
  LOCAL_AGENT_TOOL_URL        tool endpoint (point this at the chaos proxy)
  LOCAL_AGENT_TOOL_TIMEOUT_MS default 1000
  LOCAL_AGENT_TOOL_DELAY_MS   default 25 (in-process tool only)
  LOCAL_AGENT_UPSTREAM_TIMEOUT_S default 300
  LOCAL_AGENT_REASONING_EFFORT  forwarded as `reasoning_effort` when set

On a thinking model (gemma4, qwen3.6) TTFT measures how long the model reasons
before its first visible token, not how long the server took to start serving —
on gemma4:12b that is ~92s vs ~5s with `reasoning_effort=none`. The knob is
unset by default so nothing is hidden; set it explicitly, and say which way it
was set whenever a number is published.

Run standalone:
  LOCAL_AGENT_MODEL=gemma4:12b python -m uvicorn examples.local_agent.app:app --port 8000
"""

import asyncio
import json
import os
import time
import uuid
from urllib.parse import parse_qs

import httpx

SYSTEM_PROMPT = (
    "You are a technical support agent. Answer the user's problem directly and "
    "concisely in plain prose — no preamble, no restating the question.\n"
    "A knowledge base lookup has already been performed for you; its result is "
    "given below. Use it if it helps, and say so plainly if it does not.\n"
    "When you have given the user a complete answer that resolves their problem, "
    "end your reply with the exact marker [RESOLVED] on the final line. "
    "If you cannot resolve it, explain why and do not write the marker."
)

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> httpx.AsyncClient:
    # one shared client per event loop: building an AsyncClient per request can
    # cost ~1s (SSL context) and would trip the tool timeout on healthy calls.
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = httpx.AsyncClient()
        _client_loop = loop
    return _client


def _env() -> dict:
    return {
        "upstream": os.getenv("LOCAL_AGENT_UPSTREAM", "http://127.0.0.1:11434/v1").rstrip("/"),
        "model": os.getenv("LOCAL_AGENT_MODEL", ""),
        "tool_url": os.getenv("LOCAL_AGENT_TOOL_URL", ""),
        "tool_timeout_s": int(os.getenv("LOCAL_AGENT_TOOL_TIMEOUT_MS", "1000")) / 1000,
        "tool_delay_s": int(os.getenv("LOCAL_AGENT_TOOL_DELAY_MS", "25")) / 1000,
        "upstream_timeout_s": float(os.getenv("LOCAL_AGENT_UPSTREAM_TIMEOUT_S", "300")),
        "reasoning_effort": os.getenv("LOCAL_AGENT_REASONING_EFFORT", ""),
    }


# A real model reads its tool output. The demo agent's `kb-4` is information-free
# — it stamps the marker regardless, so the payload never mattered — but a model
# handed `kb-4` correctly refuses to claim the task is resolved, and the
# resolution pillar then measures the fixture rather than the model. These
# snippets carry actual procedure. The tool stays naive in the way that matters
# for chaos: one attempt, no retry, no validation of what comes back.
_KB: list[tuple[tuple[str, ...], str]] = [
    (("password", "reset email", "log in", "login"),
     "Password resets: the reset link expires after 30 minutes and is often "
     "filtered into spam. Have the user check junk mail and allowlist "
     "no-reply@example.com, then re-request from the account page."),
    (("charged twice", "double charge", "refund", "billing", "invoice"),
     "Duplicate charges: a pending authorisation can appear alongside the "
     "settled charge and drops off in 3-5 business days. If both have settled, "
     "refund the later transaction from the billing console; it returns to the "
     "original card in 5-10 business days."),
    (("shipping address", "change address", "building number", "courier"),
     "Address changes: editable from Orders > Manage until the label prints. "
     "After that the parcel must be intercepted with the courier, which adds "
     "one business day."),
    (("ck-209", "checkout fails", "saved addresses", "deleted"),
     "Error CK-209: raised when a saved address predates the v8 address schema "
     "and lost its postal-code field. Re-adding the address at checkout clears "
     "it permanently; no data is recoverable from the old record."),
    (("open tickets", "escalate", "oldest", "summarize"),
     "Ticket triage: tickets untouched for 5+ business days auto-escalate to "
     "tier 2. Anything tagged billing or outage should be escalated by hand "
     "immediately rather than waiting for the timer."),
    (("order", "shipment", "track", "delivery", "where is"),
     "Order tracking: tracking numbers activate at the first courier scan, "
     "usually within 24 hours of dispatch. Until then the order shows "
     "'preparing' and no courier page exists yet."),
]

_KB_FALLBACK = (
    "No article matched this query. Escalate to a human agent if the user needs "
    "account-specific data."
)


def _lookup(query: str) -> str:
    """The in-process knowledge base — deterministic keyword match, no model."""
    lowered = query.lower()
    for index, (keywords, article) in enumerate(_KB):
        if any(keyword in lowered for keyword in keywords):
            return f"kb-{index}: {article}"
    return f"kb-{len(_KB)}: {_KB_FALLBACK}"


async def _call_tool(query: str, session_header: str | None, env: dict) -> str:
    """Naive tool step: one attempt, no retry, no validation of garbage."""
    if not env["tool_url"]:
        await asyncio.sleep(env["tool_delay_s"])
        return _lookup(query)
    headers = {"x-kneepoint-session": session_header} if session_header else {}
    try:
        resp = await _get_client().get(
            env["tool_url"], params={"q": query}, headers=headers,
            timeout=env["tool_timeout_s"],
        )
        return str(resp.json()["result"])
    except (httpx.TimeoutException, httpx.ConnectError):
        return "[TOOL-ERROR timeout]"
    except (json.JSONDecodeError, KeyError, ValueError):
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


def create_app():
    async def chat(scope, receive, send) -> None:
        env = _env()
        body = json.loads((await _read_body(receive)) or b"{}")
        messages = list(body.get("messages") or [])
        model = env["model"] or body.get("model", "local")
        prompt = str(messages[-1].get("content", "")) if messages else ""
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        session_header = headers.get("x-kneepoint-session")
        comp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        # Tool step first: its result becomes context for the answer, so a hung
        # or garbage tool shows up as a worse answer — which is the whole point.
        tool_result = await _call_tool(prompt, session_header, env)
        upstream_messages = [
            {"role": "system", "content":
             f"{SYSTEM_PROMPT}\n\nKnowledge base lookup result: {tool_result}"},
            *messages,
        ]

        payload = {
            "model": model, "messages": upstream_messages, "stream": True,
            "stream_options": {"include_usage": True},
        }
        if env["reasoning_effort"]:
            payload["reasoning_effort"] = env["reasoning_effort"]

        client = _get_client()
        request = client.build_request(
            "POST", f"{env['upstream']}/chat/completions",
            json=payload, timeout=env["upstream_timeout_s"],
        )
        try:
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            # nothing has been streamed yet, so a clean HTTP error is still
            # possible — the load generator records it as a failed request
            # rather than a truncated success
            await _send_json(send, 502, {"error": f"upstream unreachable: {exc}"})
            return

        if resp.status_code >= 400:
            await resp.aread()
            await resp.aclose()
            await _send_json(send, resp.status_code, {
                "error": f"upstream returned {resp.status_code}",
            })
            return

        def sse(payload: dict) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode()

        def chunk(delta: dict, finish_reason: str | None = None) -> dict:
            return {
                "id": comp_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache")],
        })

        async def part(data: bytes) -> None:
            await send({"type": "http.response.body", "body": data, "more_body": True})

        await part(sse(chunk({"role": "assistant"})))
        usage: dict | None = None
        finish_reason = "stop"
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                if payload.strip() == "[DONE]":
                    break
                try:
                    upstream_chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if upstream_chunk.get("usage"):
                    usage = upstream_chunk["usage"]
                choices = upstream_chunk.get("choices") or []
                if not choices:
                    continue
                content = (choices[0].get("delta") or {}).get("content")
                if content:
                    await part(sse(chunk({"content": content})))
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
        finally:
            await resp.aclose()

        await part(sse(chunk({}, finish_reason=finish_reason)))
        if usage is not None:  # honest None: no usage upstream, no usage chunk
            await part(sse({
                "id": comp_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [], "usage": usage,
            }))
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n",
                    "more_body": False})

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
        env = _env()
        if method == "GET" and path == "/":
            await _send_json(send, 200, {
                "status": "ok", "upstream": env["upstream"],
                "model": env["model"] or "(from request)",
                "tool_url": env["tool_url"] or "in-process",
            })
        elif method == "GET" and path == "/tool/search":
            q = parse_qs(scope["query_string"].decode()).get("q", [""])[0]
            await asyncio.sleep(env["tool_delay_s"])
            await _send_json(send, 200, {"result": _lookup(q)})
        elif method == "POST" and path == "/v1/chat/completions":
            await chat(scope, receive, send)
        else:
            await _send_json(send, 404, {"detail": "Not Found"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
