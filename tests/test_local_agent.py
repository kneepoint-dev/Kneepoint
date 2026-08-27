"""local_agent wraps a real local model as a kneepoint target.

Unit tests drive it against a fake upstream so CI needs no model and spends $0.
The one integration test against a live model is gated on KNEEPOINT_LOCAL_MODEL.
"""

import json
import os
import time

import httpx
import pytest

from examples.local_agent.app import create_app
from tests.helpers_sse import collect_chunks, collect_text, serve_asgi


class FakeUpstream:
    """Minimal OpenAI-compatible streaming server standing in for Ollama.

    Records every request body so tests can assert what the wrapper forwarded.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.content = "Restart the router. [RESOLVED]"
        self.usage: dict | None = {
            "prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38,
        }
        self.status = 200

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append(json.loads(body or b"{}"))

        if self.status >= 400:
            await send({
                "type": "http.response.start", "status": self.status,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b'{"error":"upstream boom"}'})
            return

        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })

        def chunk(delta: dict, finish_reason: str | None = None) -> dict:
            return {
                "id": "chatcmpl-fake", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "fake",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        async def part(payload: dict) -> None:
            await send({
                "type": "http.response.body",
                "body": f"data: {json.dumps(payload)}\n\n".encode(),
                "more_body": True,
            })

        await part(chunk({"role": "assistant"}))
        for word in self.content.split(" "):
            await part(chunk({"content": word + " "}))
        await part(chunk({}, finish_reason="stop"))
        if self.usage is not None:
            await part({
                "id": "chatcmpl-fake", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "fake",
                "choices": [], "usage": self.usage,
            })
        await send({
            "type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False,
        })


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    with serve_asgi(fake) as url:
        fake.base_url = f"{url}/v1"
        yield fake


@pytest.fixture
def agent_url(upstream, monkeypatch):
    """local_agent serving on a free port, pointed at the fake upstream."""
    monkeypatch.setenv("LOCAL_AGENT_UPSTREAM", upstream.base_url)
    monkeypatch.delenv("LOCAL_AGENT_TOOL_URL", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_MODEL", raising=False)
    with serve_asgi(create_app()) as url:
        yield f"{url}/v1"


def _last_user_content(body: dict) -> str:
    return str(body["messages"][-1]["content"])


def _system_content(body: dict) -> str:
    return "".join(
        str(m.get("content", "")) for m in body["messages"] if m.get("role") == "system"
    )


# --- contract: chunk shape ------------------------------------------------


async def test_streams_openai_chunk_sequence(agent_url):
    chunks = await collect_chunks(agent_url, [{"role": "user", "content": "wifi is down"}])

    assert chunks[-1] == "[DONE]"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    body = [c for c in chunks[1:-1] if isinstance(c, dict)]
    contents = [c for c in body if (c["choices"] or [{}])[0].get("delta", {}).get("content")]
    assert contents, "expected at least one content chunk"
    stops = [c for c in body if c["choices"] and c["choices"][0]["finish_reason"] == "stop"]
    assert len(stops) == 1
    assert body.index(stops[0]) > body.index(contents[-1])


async def test_relays_model_text_verbatim(agent_url, upstream):
    upstream.content = "Try turning it off and on. [RESOLVED]"
    text = await collect_text(agent_url, [{"role": "user", "content": "help"}])
    assert text.strip() == "Try turning it off and on. [RESOLVED]"


# --- contract: the model earns the marker, the wrapper never stamps it ----


async def test_wrapper_does_not_add_resolved_marker(agent_url, upstream):
    upstream.content = "I am not sure what to suggest."
    text = await collect_text(agent_url, [{"role": "user", "content": "help"}])
    assert "[RESOLVED" not in text


async def test_system_prompt_asks_the_model_for_the_marker(agent_url, upstream):
    await collect_text(agent_url, [{"role": "user", "content": "help"}])
    assert "[RESOLVED]" in _system_content(upstream.requests[-1])


# --- contract: multi-turn history ----------------------------------------


async def test_forwards_full_message_history(agent_url, upstream):
    history = [
        {"role": "user", "content": "my wifi is down"},
        {"role": "assistant", "content": "have you rebooted?"},
        {"role": "user", "content": "yes, twice"},
    ]
    await collect_text(agent_url, history)
    forwarded = [m for m in upstream.requests[-1]["messages"] if m["role"] != "system"]
    assert forwarded == history


async def test_model_name_passes_through_and_env_overrides(agent_url, upstream, monkeypatch):
    await collect_chunks(agent_url, [{"role": "user", "content": "hi"}], model="gemma4:12b")
    assert upstream.requests[-1]["model"] == "gemma4:12b"

    monkeypatch.setenv("LOCAL_AGENT_MODEL", "qwen3.6:27b")
    await collect_chunks(agent_url, [{"role": "user", "content": "hi"}], model="gemma4:12b")
    assert upstream.requests[-1]["model"] == "qwen3.6:27b"


async def test_reasoning_effort_is_off_by_default_and_forwarded_when_set(
    agent_url, upstream, monkeypatch
):
    await collect_text(agent_url, [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in upstream.requests[-1]

    monkeypatch.setenv("LOCAL_AGENT_REASONING_EFFORT", "none")
    await collect_text(agent_url, [{"role": "user", "content": "hi"}])
    assert upstream.requests[-1]["reasoning_effort"] == "none"


# --- contract: tool step routes through the chaos proxy URL ---------------


async def test_tool_result_reaches_the_model(agent_url, upstream, monkeypatch):
    root = agent_url.removesuffix("/v1")
    monkeypatch.setenv("LOCAL_AGENT_TOOL_URL", f"{root}/tool/search")
    await collect_text(agent_url, [{"role": "user", "content": "printer jam"}])
    assert "kb-" in _system_content(upstream.requests[-1])


async def test_lookup_returns_usable_article_not_just_an_id(agent_url, upstream):
    """A model handed a bare `kb-4` correctly declines to claim resolution, which
    would make the resolution pillar measure the fixture instead of the model."""
    await collect_text(
        agent_url,
        [{"role": "user", "content": "How do I reset my password? No reset email arrives."}],
    )
    kb_line = _system_content(upstream.requests[-1]).split("Knowledge base lookup result:")[-1]
    assert "spam" in kb_line.lower()
    assert len(kb_line.split()) > 10


async def test_lookup_is_deterministic_and_falls_back(agent_url, upstream):
    root = agent_url.removesuffix("/v1")
    async with httpx.AsyncClient(timeout=10) as client:
        first = await client.get(f"{root}/tool/search", params={"q": "charged twice"})
        again = await client.get(f"{root}/tool/search", params={"q": "charged twice"})
        unmatched = await client.get(f"{root}/tool/search", params={"q": "zzz nothing"})
    assert first.json()["result"] == again.json()["result"]
    assert "refund" in first.json()["result"].lower()
    assert "no article matched" in unmatched.json()["result"].lower()


async def test_tool_call_goes_to_the_configured_url(agent_url, upstream, monkeypatch):
    seen: list[dict] = []

    async def recorder(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        seen.append({
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "headers": {k.decode(): v.decode() for k, v in scope["headers"]},
        })
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b'{"result":"kb-proxied"}'})

    with serve_asgi(recorder) as tool_root:
        monkeypatch.setenv("LOCAL_AGENT_TOOL_URL", f"{tool_root}/tool/search")
        await collect_text(agent_url, [{"role": "user", "content": "printer jam"}])

    assert len(seen) == 1
    assert seen[0]["path"] == "/tool/search"
    assert "printer" in seen[0]["query"]
    assert "kb-proxied" in _system_content(upstream.requests[-1])


async def test_tool_timeout_is_reported_to_the_model_not_hidden(agent_url, upstream, monkeypatch):
    async def hang(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        import asyncio
        await asyncio.sleep(5)

    with serve_asgi(hang) as tool_root:
        monkeypatch.setenv("LOCAL_AGENT_TOOL_URL", f"{tool_root}/tool/search")
        monkeypatch.setenv("LOCAL_AGENT_TOOL_TIMEOUT_MS", "150")
        await collect_text(agent_url, [{"role": "user", "content": "printer jam"}])

    assert "[TOOL-ERROR timeout]" in _system_content(upstream.requests[-1])


async def test_tool_malformed_json_is_reported_to_the_model(agent_url, upstream, monkeypatch):
    async def garbage(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b"<<not json%%"})

    with serve_asgi(garbage) as tool_root:
        monkeypatch.setenv("LOCAL_AGENT_TOOL_URL", f"{tool_root}/tool/search")
        await collect_text(agent_url, [{"role": "user", "content": "printer jam"}])

    assert "[TOOL-ERROR parse]" in _system_content(upstream.requests[-1])


# --- contract: usage passthrough, honest None ----------------------------


async def test_usage_chunk_carries_upstream_token_counts(agent_url, upstream):
    upstream.usage = {"prompt_tokens": 412, "completion_tokens": 88, "total_tokens": 500}
    chunks = await collect_chunks(agent_url, [{"role": "user", "content": "hi"}])
    usages = [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert len(usages) == 1
    assert usages[0]["usage"]["prompt_tokens"] == 412
    assert usages[0]["usage"]["completion_tokens"] == 88


async def test_no_usage_chunk_when_upstream_reports_none(agent_url, upstream):
    upstream.usage = None
    chunks = await collect_chunks(agent_url, [{"role": "user", "content": "hi"}])
    assert not [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert chunks[-1] == "[DONE]"


# --- contract: upstream failure is a clean error, not a broken stream ----


async def test_upstream_error_status_surfaces_as_http_error(agent_url, upstream):
    upstream.status = 503
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{agent_url}/chat/completions",
            json={"model": "local", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503
    assert "upstream" in resp.json()["error"].lower()


async def test_unreachable_upstream_surfaces_as_502(agent_url, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_UPSTREAM", "http://127.0.0.1:9/v1")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{agent_url}/chat/completions",
            json={"model": "local", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 502
    assert "error" in resp.json()


# --- housekeeping endpoints ----------------------------------------------


async def test_health_reports_wiring(agent_url, upstream):
    root = agent_url.removesuffix("/v1")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(root + "/")
    assert resp.status_code == 200
    assert resp.json()["upstream"] == upstream.base_url


async def test_builtin_tool_endpoint_returns_json(agent_url):
    root = agent_url.removesuffix("/v1")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{root}/tool/search", params={"q": "hello"})
    assert resp.status_code == 200
    assert resp.json()["result"].startswith("kb-")


# --- integration: only with a real local model ---------------------------


@pytest.mark.skipif(
    not os.getenv("KNEEPOINT_LOCAL_MODEL"),
    reason="set KNEEPOINT_LOCAL_MODEL (and serve Ollama) to run the live-model test",
)
async def test_integration_against_a_real_local_model(monkeypatch):
    model = os.environ["KNEEPOINT_LOCAL_MODEL"]
    monkeypatch.setenv(
        "LOCAL_AGENT_UPSTREAM",
        os.getenv("KNEEPOINT_LOCAL_UPSTREAM", "http://127.0.0.1:11434/v1"),
    )
    monkeypatch.delenv("LOCAL_AGENT_TOOL_URL", raising=False)
    with serve_asgi(create_app()) as url:
        chunks = await collect_chunks(
            f"{url}/v1",
            [{"role": "user", "content": "My wifi keeps dropping every few minutes. Help."}],
            model=model,
            timeout=300,
        )

    assert chunks[-1] == "[DONE]"
    text = "".join(
        (c["choices"][0]["delta"].get("content") or "")
        for c in chunks
        if isinstance(c, dict) and c.get("choices")
    )
    assert text.strip(), "the model produced no content"
    usages = [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert len(usages) == 1, "ollama should return exactly one usage chunk"
    assert usages[0]["usage"]["prompt_tokens"] > 0
    assert usages[0]["usage"]["completion_tokens"] > 0
