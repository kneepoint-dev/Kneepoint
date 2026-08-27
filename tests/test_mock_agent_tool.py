import httpx

from tests.helpers_sse import collect_text


async def test_tool_endpoint_returns_json(mock_agent_url):
    root = mock_agent_url.removesuffix("/v1")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{root}/tool/search", params={"q": "hello"})
    assert resp.status_code == 200
    assert resp.json()["result"].startswith("kb-")


async def test_healthy_agent_emits_resolved_marker(mock_agent_url):
    text = await collect_text(mock_agent_url, [{"role": "user", "content": "help me"}])
    assert "[RESOLVED kb-" in text
    assert "[TOOL-ERROR" not in text


async def test_prompt_tokens_count_full_history(mock_agent_url):
    async with httpx.AsyncClient(timeout=30) as client:
        async def usage_for(messages):
            async with client.stream(
                "POST", f"{mock_agent_url}/chat/completions",
                json={"model": "mock", "stream": True, "messages": messages},
            ) as resp:
                import json
                usages = [
                    json.loads(line[len("data: "):]) async for line in resp.aiter_lines()
                    if line.startswith("data: ") and '"usage"' in line
                ]
            return usages[-1]["usage"]["prompt_tokens"]

        one = await usage_for([{"role": "user", "content": "one two three"}])
        two = await usage_for([
            {"role": "user", "content": "one two three"},
            {"role": "assistant", "content": "four five"},
            {"role": "user", "content": "six"},
        ])
    assert one == 3
    assert two == 6


async def test_tool_timeout_yields_naive_error_marker(mock_agent_url, monkeypatch):
    root = mock_agent_url.removesuffix("/v1")
    monkeypatch.setenv("MOCK_TOOL_URL", f"{root}/tool/search")
    monkeypatch.setenv("MOCK_TOOL_DELAY_MS", "300")
    monkeypatch.setenv("MOCK_TOOL_TIMEOUT_MS", "50")
    text = await collect_text(mock_agent_url, [{"role": "user", "content": "help"}])
    assert "[TOOL-ERROR timeout]" in text
