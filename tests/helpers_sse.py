import json
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn


@contextmanager
def serve_asgi(app):
    """Serve an ASGI app on 127.0.0.1:<free port> for the duration of the block."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=2
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("test server failed to start within 10s")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def collect_chunks(
    base_url: str, messages: list[dict], model: str = "local", timeout: float = 60
) -> list[dict]:
    """POST a streaming chat request; return every decoded SSE chunk in order.

    `[DONE]` comes back as the sentinel string so tests can assert it terminates
    the stream.
    """
    chunks: list[dict | str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions",
            json={"model": model, "stream": True, "messages": messages,
                  "stream_options": {"include_usage": True}},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                chunks.append("[DONE]" if payload.strip() == "[DONE]" else json.loads(payload))
    return chunks


async def collect_text(base_url: str, messages: list[dict]) -> str:
    """POST a streaming chat request; return the concatenated content deltas."""
    pieces: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions",
            json={"model": "mock", "stream": True, "messages": messages},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[len("data: "):])
                choices = chunk.get("choices") or []
                if choices and (choices[0].get("delta") or {}).get("content"):
                    pieces.append(choices[0]["delta"]["content"])
    return "".join(pieces)
