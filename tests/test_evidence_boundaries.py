"""Unit checks for evidence formatting and bounded response parsing."""

import tracemalloc

import httpx
import pytest
from pydantic import ValidationError

from kneepoint.collect.schemas import RequestRecord
from kneepoint.targets import openai_compatible, response_limits
from kneepoint.targets.evidence import EvidenceSanitizer, display_url, exception_type_and_path
from kneepoint.targets.http import _Reply
from kneepoint.targets.http_config import Capture


@pytest.mark.parametrize("marker", [
    "sample-bearer", "sample-environment-header", "sample-static-header", "sample-template",
    "sample-http-query", "sample-chat-query", "sample-capture", "sample-judge-key",
])
def test_evidence_formatter_never_emits_configured_markers(marker):
    formatter = EvidenceSanitizer([marker])
    message = f"ordinary diagnostic ({marker})"
    fields = formatter.fields(error=message, warnings=[message], fault="llm_server_error")
    assert marker not in fields["error"] and marker not in fields["warnings"][0]
    assert "ordinary diagnostic" in fields["error"]
    assert fields["fault"] == "llm_server_error"
    assert formatter.fault("unrecognized label") is None
    assert formatter.text(message, fallback="string", limit=10, diagnostic=True) == "string"


def test_target_display_is_separate_from_transport_url():
    url = httpx.URL("https://user:example@example.test/v1?format=sse")
    assert display_url(url) == "https://example.test/v1"
    assert url.params["format"] == "sse"


def test_usage_validation_diagnostic_contains_the_field_location():
    with pytest.raises(ValidationError) as caught:
        RequestRecord(session_id="example", concurrency=1, started_at=0, total_ms=1,
                      input_tokens="many", ok=True)
    assert exception_type_and_path(caught.value) == "ValidationError: usage.prompt_tokens"


def test_sse_capture_memory_stays_flat_as_ids_change():
    config = Capture(name="id", path="/id", **{"from": "sse_event"})
    reply = _Reply()
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        for index in range(10_000):
            reply.read_capture({"id": f"conversation-{index:05d}"}, config)
        after, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    reply.read_capture({"id": None}, config)
    assert reply.captured == "conversation-09999"
    assert after - before < 64 * 1024


@pytest.mark.parametrize("size_kind,content", [
    ("line", b" " * 41 + b"\n"),
    ("body", b"short\n" * 20),
])
async def test_small_configured_response_size_limits(monkeypatch, size_kind, content):
    monkeypatch.setattr(response_limits, "EVENT_LIMIT", 40)
    monkeypatch.setattr(openai_compatible, "BODY_LIMIT", 100)

    async def record(data):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=data))
        async with httpx.AsyncClient(transport=transport) as client:
            return await openai_compatible.run_single_turn(
                client, "http://example.test/v1", "mock", "hello", concurrency=1,
            )

    limited = await record(content)
    assert not limited.ok and limited.error_kind == "response_error"
    assert size_kind in limited.error
    assert (await record(b"data: [DONE]\r\n")).ok


def test_header_environment_preflight_uses_the_same_reference_rules_as_expansion():
    from kneepoint.targets.http_config import HttpTargetConfig

    target = HttpTargetConfig(
        type="http", url="https://example.test/${PATH_COMPONENT}",
        headers={"X-Tenant": "${TENANT_NAME}", "X-Literal": "$${LITERAL}"},
        response={"mode": "json", "text_path": "/text"},
    )
    assert target.environment_names() == {"PATH_COMPONENT", "TENANT_NAME"}
    assert target.environment_names("https://example.test/") == {"TENANT_NAME"}
