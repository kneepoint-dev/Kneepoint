"""Explicit context rejection shapes survive HTTP and SSE without payload leaks."""
import asyncio
import json

import httpx
import pytest

from kneepoint.targets.context_errors import is_context_overflow
from kneepoint.targets.http import HttpTurn
from kneepoint.targets.http_config import HttpTargetConfig


@pytest.mark.parametrize("body", [
    {"error": {"message": "This model's maximum context length is 32768 tokens",
               "type": "BadRequestError"}},
    {"error": {"code": 400, "message": "request exceeds the available context size",
               "type": "exceed_context_size_error"}},
])
@pytest.mark.parametrize("stream", [False, True])
def test_context_rejection_record(body, stream):
    config = HttpTargetConfig.model_validate({
        "type": "http", "url": "http://example.test/invoke",
        "response": {"mode": "sse", "text_path": "/delta", "error_event": "error"},
    })

    async def check():
        response = (httpx.Response(200, text="event: error\ndata: " + json.dumps(body) + "\n\n")
                    if stream else httpx.Response(400, json=body))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            result = await HttpTurn(config, config.resolve())(
                client, "", "", [], concurrency=1, session_id="test",
            )
        assert result.record.error_kind == "context_overflow"
        assert result.record.ok is False
        assert result.text == ""

    asyncio.run(check())


def test_unrelated_bad_request_is_not_context_overflow():
    assert not is_context_overflow({"error": {"message": "invalid model name"}})


def test_context_overflow_is_failed_but_not_retried_or_used_for_latency():
    from kneepoint.analyze.knee import aggregate
    from kneepoint.collect.schemas import RequestRecord
    from kneepoint.generator.session import _retryable

    successful = RequestRecord(
        session_id="s", concurrency=1, started_at=0, total_ms=100, ok=True,
    )
    rejected = RequestRecord(
        session_id="s", concurrency=1, started_at=0, total_ms=9000,
        ok=False, error_kind="context_overflow", status_code=503,
    )
    assert not _retryable(rejected)
    assert _retryable(rejected.model_copy(update={"error_kind": "http_error"}))
    stats = aggregate([successful, rejected])[0]
    assert stats.n == 2 and stats.successful_n == 1
    assert stats.error_rate == .5 and stats.p95_ms == 100
