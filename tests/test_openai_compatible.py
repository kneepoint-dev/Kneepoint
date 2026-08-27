import httpx

from examples.mock_agent import app as mock_module
from kneepoint.targets.openai_compatible import run_single_turn, run_turn


async def test_single_turn_records_metrics(mock_agent_url):
    async with httpx.AsyncClient(timeout=30) as client:
        rec = await run_single_turn(client, mock_agent_url, "mock", "hello world", concurrency=1)
    assert rec.ok
    assert rec.error is None
    assert rec.ttft_ms is not None
    assert 0 < rec.ttft_ms <= rec.total_ms
    assert rec.output_tokens == mock_module.OUTPUT_TOKENS + 1
    assert rec.input_tokens == 2
    assert rec.concurrency == 1
    assert rec.session_id


async def test_single_turn_survives_connection_error():
    async with httpx.AsyncClient(timeout=2) as client:
        rec = await run_single_turn(client, "http://127.0.0.1:9/v1", "mock", "hi", concurrency=1)
    assert not rec.ok
    assert rec.error
    assert rec.ttft_ms is None
    assert rec.total_ms > 0


async def test_run_turn_returns_text_and_turn_fields(mock_agent_url):
    async with httpx.AsyncClient(timeout=30) as client:
        outcome = await run_turn(
            client, mock_agent_url, "mock",
            [{"role": "user", "content": "hello world"}],
            concurrency=2, session_id="sess1", turn_index=1, attempt=2,
        )
    assert outcome.record.ok
    assert outcome.record.session_id == "sess1"
    assert outcome.record.turn_index == 1
    assert outcome.record.attempt == 2
    assert outcome.record.concurrency == 2
    assert "tok0" in outcome.text            # mock streams "tok0 tok1 ..."
    assert outcome.record.output_tokens == mock_module.OUTPUT_TOKENS + 1


async def test_success_records_the_status_the_server_sent_not_a_literal_200():
    """A 2xx that is not 200 still streams. The record must carry what arrived —
    `docs/output-format.md` promises `status_code` is the real status, and an
    assumed 200 would be a guessed value on a line that never guesses."""
    body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(201, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await run_turn(
            client, "http://target/v1", "m", [{"role": "user", "content": "hi"}],
            concurrency=1, session_id="sess3",
        )
    assert outcome.record.ok
    assert outcome.record.status_code == 201
    assert outcome.text == "hi"


async def test_run_turn_http_error_is_a_record_not_an_exception(mock_agent_url):
    async with httpx.AsyncClient(timeout=30) as client:
        outcome = await run_turn(
            client, f"{mock_agent_url}/nope", "mock",
            [{"role": "user", "content": "hi"}],
            concurrency=1, session_id="sess2",
        )
    assert not outcome.record.ok
    assert outcome.record.status_code == 404
    assert outcome.text == ""
