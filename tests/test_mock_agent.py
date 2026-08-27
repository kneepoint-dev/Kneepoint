import json

import httpx

from examples.mock_agent import app as mock_module


async def test_mock_agent_streams_openai_chunks(mock_agent_url):
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST",
            f"{mock_agent_url}/chat/completions",
            json={
                "model": "mock",
                "stream": True,
                "messages": [{"role": "user", "content": "hello world"}],
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in resp.aiter_lines() if line.startswith("data: ")]

    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[len("data: "):]) for line in lines[:-1]]
    contents = [
        c["choices"][0]["delta"]["content"]
        for c in chunks
        if c["choices"] and c["choices"][0]["delta"].get("content")
    ]
    assert len(contents) == mock_module.OUTPUT_TOKENS + 1
    usage_chunks = [c for c in chunks if c.get("usage")]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["completion_tokens"] == mock_module.OUTPUT_TOKENS + 1
    assert usage_chunks[0]["usage"]["prompt_tokens"] == 2  # "hello world"
