"""HTTP adapter contracts through the scenario, attempt and CLI entry points."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from kneepoint.chaos.faults import FaultSpec
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.transport import ChaosTransport
from kneepoint.cli import app
from kneepoint.collect.writer import read_jsonl, read_run_meta
from kneepoint.config import Scenario
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import RunEvidence
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import RetryPolicy, SessionSpec, TurnsSpec
from kneepoint.targets.http import HttpTurn
from kneepoint.targets.http_config import HttpTargetConfig

MESSAGES = [{"role": "user", "content": 'quote " slash \\ newline\n టెస్ట్ {session_id}'}]
JSON = {"mode": "json", "text_path": "/reply"}
SSE = {"mode": "sse", "text_path": "/delta", "end": {"data": "[DONE]"}}


def _config(response=JSON, **kwargs):
    return HttpTargetConfig.model_validate({
        "type": "http", "url": "http://example.test/turn", "response": response, **kwargs,
    })


def _runner(response=JSON, **kwargs):
    config = _config(response, **kwargs)
    return HttpTurn(config, config.resolve())


async def _turn(runner, client, **kwargs):
    return await runner(
        client, "", "", MESSAGES, concurrency=2, session_id="test", **kwargs,
    )


async def _mock(runner, handler, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await _turn(runner, client, **kwargs)


def test_strict_config_and_multi_turn_validation():
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        _config(model="wrong")
    with pytest.raises(ValidationError, match="unescaped brace"):
        _config(body={"bad": "{unknown}"})
    with pytest.raises(ValidationError, match="whole JSON value"):
        _config(body={"bad": "prefix {history}"})
    with pytest.raises(ValidationError, match="JSON Pointer"):
        _config({"mode": "json", "text_path": "/bad~2"})
    with pytest.raises(ValidationError, match="recursive YAML aliases"):
        circular = []
        circular.append(circular)
        _config(body=circular)
    with pytest.raises(ValidationError, match="multi-turn HTTP requires"):
        Scenario.model_validate({
            "target": {"type": "http", "url": "http://example.test", "response": JSON},
            "workload": {"conversation": {"turns": {"min": 1, "max": 2}}},
        })
    with pytest.raises(ValidationError, match="GET requires an omitted body"):
        _config(method="get", body=None)
    with pytest.raises(ValidationError, match="duplicate"):
        _config(headers={"X-Test": "a", "x-test": "b"})
    with pytest.raises(ValidationError, match="less than or equal to 299"):
        _config(success_status=[429])


async def test_structured_body_and_path_extraction(monkeypatch):
    monkeypatch.setenv("HTTP_ADAPTER_TOKEN", "test-header-token")
    runner = _runner(
        {"mode": "json", "text_path": "/a~1b/~0/0/0",
         "usage": {"input_path": "/usage/in", "output_path": "/usage/out"}},
        method="patch", headers={"Authorization": "Bearer ${HTTP_ADAPTER_TOKEN}"},
        body={
            "{literal-key}": "{prompt}", "index": "{turn_index}",
            "text": "turn {turn_index} {{{{x}}}} {session_id}", "history": "{history}",
        },
    )

    def handler(request):
        assert request.method == "PATCH"
        assert str(request.url) == "http://example.test/turn"
        assert request.headers["authorization"] == "Bearer test-header-token"
        assert json.loads(request.content) == {
            "{literal-key}": MESSAGES[0]["content"], "index": 3,
            "text": "turn 3 {{x}} test", "history": MESSAGES,
        }
        return httpx.Response(201, json={"a/b": {"~": [{"0": "ok"}]}, "usage": {"in": 0}})

    result = await _mock(runner, handler, turn_index=3)
    record = result.record
    assert record.ok and record.status_code == 201 and result.text == "ok"
    assert (record.input_tokens, record.output_tokens) == (0, None)
    assert record.ttft_ms is record.itl_mean_ms is record.itl_p99_ms is record.chunk_count is None


@pytest.mark.parametrize(("body", "expected"), [
    ({}, b""),
    ({"body": None}, b"null"),
])
async def test_omitted_body_and_explicit_json_null(body, expected):
    def handler(request):
        assert request.content == expected
        if expected:
            assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, json={"reply": ""})

    outcome = await _mock(_runner(**body), handler)
    assert outcome.record.ok and outcome.text == ""


@pytest.mark.parametrize(("payload", "error"), [
    ({"reply": "ok"}, None),
    ({"reply": "ok", "error": None}, None),
    ({"reply": "ok", "error": {"private": "must not appear"}}, "AgentError: object"),
    ({"error": "bad" * 40}, "AgentError: " + ("bad" * 40)[:80]),
    ({"reply": "ok", "error": False}, "AgentError: boolean"),
    ({"other": "no reply"}, "ValueError: JSON reply path must select a string"),
    ({"reply": {"text": "wrong type"}}, "ValueError: JSON reply path must select a string"),
])
async def test_json_application_errors_and_missing_text(payload, error):
    runner = _runner({**JSON, "error_path": "/error"})
    outcome = await _mock(runner, lambda _: httpx.Response(200, json=payload))
    assert outcome.record.status_code == 200
    assert outcome.record.ok is (error is None)
    assert outcome.record.error == error


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize(("value", "label"), [
    ({"type": "AgentBusy", "code": "E_BUSY", "message": "later"}, "AgentBusy"),
    ({"type": 123, "code": "E_BUSY", "message": "later"}, "E_BUSY"),
    ({"type": None, "code": False, "message": "try later"}, "try later"),
    ({"type": ""}, ""),
    ({"type": 123, "code": False, "message": []}, "object"),
    ({}, "object"),
    ("a" * 200, "a" * 80),
    ({"message": "a" * 200}, "a" * (120 - len("AgentError: "))),
    ("line one\nline two", "line one line two"),
    ("", ""),
    ([], "array"),
    (False, "boolean"),
    (True, "boolean"),
    (123, "number"),
    (1.5, "number"),
])
async def test_application_error_labels_follow_json_and_sse_rules(mode, value, label):
    if mode == "json":
        runner = _runner({**JSON, "error_path": "/error"})
        response = httpx.Response(200, json={"error": value})
    else:
        runner = _runner({**SSE, "error_event": "error"})
        stream = _Fragments([
            ("event: error\ndata: " + json.dumps(value) + "\n\n").encode(),
            b"must not read past the error",
        ])
        response = httpx.Response(200, stream=stream)
    outcome = await _mock(runner, lambda _: response)
    assert outcome.record.error == f"AgentError: {label}"
    assert len(outcome.record.error) <= 120
    assert outcome.record.status_code == 200 and not outcome.record.ok
    assert outcome.text == ""
    if mode == "sse":
        assert stream.closed and stream.read_chunks == 1


async def test_null_sse_error_payload_uses_json_type_name():
    outcome = await _mock(
        _runner({**SSE, "error_event": "error"}),
        lambda _: httpx.Response(200, content=b"event: error\ndata: null\n\n"),
    )
    assert outcome.record.error == "AgentError: null"


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("structured", [False, True])
async def test_error_label_redacts_secret_across_truncation(mode, structured, monkeypatch):
    monkeypatch.setenv("ERROR_LABEL_SECRET", "secret-value")
    # Both the 80-character string cutoff and the 120-character label cutoff
    # would otherwise expose a partial secret.
    prefix = "x" * (105 if structured else 76)
    message = prefix + "secret-value"
    value = {"message": message} if structured else message
    config = _config(
        {**JSON, "error_path": "/error"} if mode == "json"
        else {**SSE, "error_event": "error", "error_path": "/error"},
        headers={"Authorization": "Bearer ${ERROR_LABEL_SECRET}"},
    )
    runner = HttpTurn(config, config.resolve())
    payload = {"error": value}
    response = (httpx.Response(200, json=payload) if mode == "json" else httpx.Response(
        200, content=("event: error\ndata: " + json.dumps(payload) + "\n\n").encode(),
    ))
    result = await _mock(runner, lambda _: response)
    assert result.record.error == ("AgentError: object" if structured else "AgentError: string")


async def test_application_error_has_priority_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("ADAPTER_KEY", "secret-value")
    runner = _runner({
        **JSON, "error_path": "/error",
        "usage": {"input_path": "/in", "output_path": "/out"},
    }, headers={"Authorization": "Bearer ${ADAPTER_KEY}"})
    result = await _mock(runner, lambda _: httpx.Response(
        200, json={"error": "auth failed for secret-value", "in": 0, "out": False},
    ))
    assert result.record.error == "AgentError: string"
    assert (result.record.status_code, result.record.input_tokens, result.record.output_tokens) == (
        200, 0, None,
    )


async def test_status_check_precedes_parsing_and_redirects_are_disabled():
    async def handler(request):
        assert request.url.path == "/turn"
        return httpx.Response(302, headers={"location": "/private"}, content=b"not JSON")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    ) as client:
        outcome = await _turn(_runner(), client)
    assert outcome.record.error == "HTTP 302"

    outcome = await _mock(_runner(success_status=[200]), lambda _: httpx.Response(201))
    assert outcome.record.error == "HTTP 201"


@pytest.mark.parametrize(("header", "seconds"), [("1.5", 1.5), ("bad", None), ("inf", None)])
async def test_retry_after_preserves_only_finite_delay(header, seconds):
    result = await _mock(_runner(), lambda _: httpx.Response(
        429, headers={"retry-after": header}, content=b"not JSON",
    ))
    assert result.record.error == "HTTP 429" and result.retry_after_s == seconds


@pytest.mark.parametrize("count", [False, -1, 1.0, "1"])
async def test_invalid_usage_is_a_failed_attempt_and_retains_known_count(count):
    runner = _runner({**JSON, "usage": {"input_path": "/in", "output_path": "/out"}})
    result = await _mock(runner, lambda _: httpx.Response(
        200, json={"reply": "ok", "in": 0, "out": count},
    ))
    assert result.record.error == "ValueError: output_tokens must be a nonnegative integer"
    assert (result.record.status_code, result.record.input_tokens, result.record.output_tokens) == (
        200, 0, None,
    )


class _Fragments(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.read_chunks = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_sse_framing_and_cumulative_usage():
    stream_data = (
        '\ufeff: comment\r\n\r\n'
        'event: ignored\rdata: plain text\r\r'
        'event: content\r\ndata: {"delta":\r\ndata: "అ"}\r\n\r\n'
        'event: usage\ndata: {"in":0,"out":1}\n\n'
        'event: content\ndata: {"delta":"b"}\n\n'
        'event: usage\ndata: {"in":null,"out":2}\n\n'
        'event: done\n\n'
    ).encode()
    stream = _Fragments([bytes([byte]) for byte in stream_data] + [b"not read"])
    outcome = await _mock(_runner({
        "mode": "sse", "event": "content", "text_path": "/delta", "end": {"event": "done"},
        "usage": {"event": "usage", "input_path": "/in", "output_path": "/out"},
    }), lambda _: httpx.Response(200, stream=stream))
    assert outcome.record.ok and outcome.text == "అb"
    assert stream.closed and stream.read_chunks == len(stream_data)
    record = outcome.record
    assert (record.input_tokens, record.output_tokens, record.chunk_count) == (0, 2, 2)
    assert 0 <= record.ttft_ms <= record.total_ms
    assert record.itl_mean_ms == record.itl_p99_ms


@pytest.mark.parametrize(("wire", "expected"), [
    (b'data: {"delta":"first"}\n\nevent: error\ndata: {"message":"stopped"}\n\n',
     "AgentError: stopped"),
    (b'event: error\n\n', "AgentError: event"),
    (b'data: {bad}\n\n', "JSONDecodeError: malformed response JSON"),
    (b'data: {"delta":42}\n\n', "ValueError: SSE reply path must select a string"),
    (b'data: {"delta":"ok"}\n\ndata: [DONE]\n', "ValueError: SSE stream ended before "
     "its configured marker"),
    (b'data: ' + b"x" * (1024 * 1024), "ValueError: SSE event exceeds 1 MiB"),
])
async def test_sse_failures_close_stream_and_keep_received_status(wire, expected):
    runner = _runner({**SSE, "error_event": "error", "error_path": "/message"})
    stream = _Fragments([wire])
    result = await _mock(runner, lambda _: httpx.Response(200, stream=stream))
    assert stream.closed
    assert result.record.error == expected and result.record.status_code == 200
    assert not result.record.abandoned and not result.record.ok and result.text == ""
    if b'"first"' in wire:
        assert result.record.ttft_ms is not None


async def test_trace_headers_replace_merged_headers_and_can_be_disabled():
    calls = []
    stale = {"TraceParent": "old", "Baggage": "old", "TraceState": "old"}

    def handle(request):
        calls.append(request.headers)
        return httpx.Response(200, json={"reply": "ok"})

    runner = _runner(headers={**stale, "X-Kneepoint-Session": "incorrect"})
    async with httpx.AsyncClient(
        headers=stale, transport=httpx.MockTransport(handle),
    ) as client:
        first = await _turn(runner, client, run_id="run")
        second = await _turn(runner, client, run_id="run", trace=False)

    assert calls[0]["traceparent"] == f"00-{first.record.trace_id}-{first.record.span_id}-01"
    assert "kneepoint.session=test" in calls[0]["baggage"]
    assert "tracestate" not in calls[0]
    assert all(name not in calls[1] for name in ("traceparent", "baggage", "tracestate"))
    assert second.record.trace_id is second.record.span_id is None
    assert [headers["x-kneepoint-session"] for headers in calls] == ["test", "test"]


@pytest.mark.parametrize("response", [JSON, SSE])
async def test_chaos_retries_use_same_session_but_distinct_attempts(response):
    def must_not_reach_target(_):
        pytest.fail("chaos should intercept each attempt")

    runner = _runner(response)
    transport = ChaosTransport(
        httpx.MockTransport(must_not_reach_target),
        ChaosInjector([FaultSpec(type="llm_server_error", probability=1.0)], seed=0),
    )
    _, records, _ = await run_ramp(
        "", "", RampSpec(start=1, stop=1, hold_seconds=0), CorpusSampler(["hello"], seed=0),
        SessionSpec(retry=RetryPolicy(max_attempts=2, backoff_s=0)),
        transport=transport, turn_runner=runner,
    )
    assert [(r.attempt, r.status_code, r.fault) for r in records] == [
        (1, 503, "llm_server_error"), (2, 503, "llm_server_error"),
    ]
    assert records[0].session_id == records[1].session_id
    assert records[0].trace_id != records[1].trace_id


async def test_multi_turn_lifecycle_and_no_retry_for_application_error():
    requests = []

    async def handle(reader, writer):
        try:
            headers = httpx.Headers([
                tuple(line.split(b": ", 1))
                for line in (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")[1:-2]
            ])
            requests.append(json.loads(await reader.readexactly(int(headers["content-length"]))))
            payload = (b'{"reply":"first answer"}' if len(requests) == 1
                       else b'{"error":"second turn failed"}')
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
                .encode() + payload,
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    evidence = RunEvidence()
    observed = []

    def on_start(event):
        assert evidence.attempts[event.key]["status"] == "starting"
        observed.append(("start", event.turn_index))

    def on_complete(record):
        assert evidence.records[-1] is record
        observed.append(("complete", record.turn_index))

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        runner = _runner(
            {**JSON, "error_path": "/error"}, url=f"http://127.0.0.1:{port}",
            body={"history": "{history}", "session": "{session_id}", "turn": "{turn_index}"},
        )
        sessions, records, _ = await asyncio.wait_for(run_ramp(
            "", "", RampSpec(start=1, stop=1, hold_seconds=0), CorpusSampler(["hello"], seed=0),
            SessionSpec(turns=TurnsSpec(min=2, max=2)), turn_runner=runner, evidence=evidence,
            on_attempt_start=on_start, on_attempt_complete=on_complete,
        ), timeout=3)
    assert [body["turn"] for body in requests] == [0, 1]
    assert [body["session"] for body in requests] == [sessions[0].session_id] * 2
    assert requests[1]["history"] == requests[0]["history"] + [
        {"role": "assistant", "content": "first answer"}, {"role": "user", "content": "hello"},
    ]
    assert sessions[0].turns_completed == 1 and len(sessions[0].transcript) == 2
    assert records[1].error == "AgentError: second turn failed"
    assert observed == [("start", 0), ("complete", 0), ("start", 1), ("complete", 1)]


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("stop", ["wall", "read_timeout", "cancel"])
async def test_loopback_partial_response_closes_on_abandonment_and_cancel(mode, stop):
    reached = asyncio.Event()
    disconnected = asyncio.Event()
    first_data = b'{"reply":' if mode == "json" else b'data: {"delta":"first"}\n\n'

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            writer.write(f"{len(first_data):x}\r\n".encode() + first_data + b"\r\n")
            await writer.drain()
            reached.set()
            while True:
                try:
                    if not await asyncio.wait_for(reader.read(1), 0.015):
                        break
                except TimeoutError:
                    if stop == "wall":
                        writer.write(b"1\r\n \r\n")
                        await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    runner = _runner(JSON if mode == "json" else SSE, url=f"http://127.0.0.1:{port}")
    async with server, httpx.AsyncClient(
        timeout=httpx.Timeout(0.08 if stop == "read_timeout" else 5.0), trust_env=False,
    ) as client:
        task = asyncio.create_task(_turn(
            runner, client, wall_seconds=0.15 if stop == "wall" else 3.0,
        ))
        await asyncio.wait_for(reached.wait(), 2)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            outcome = await task
            assert outcome.record.status_code == 200
            assert outcome.record.abandoned and not outcome.record.ok
            assert outcome.record.error.startswith(
                "abandoned at" if stop == "wall" else "ReadTimeout:"
            )
        await asyncio.wait_for(disconnected.wait(), 2)


def _scenario_file(path: Path, target, **kwargs):
    path.write_text(yaml.safe_dump({
        "target": {"type": "http", "response": JSON, **target},
        "workload": {"ramp": {"from": 1, "to": 1, "hold_seconds": 0}},
        **kwargs,
    }))
    return path


def test_validate_resolves_environment_without_disclosing_secrets(tmp_path, monkeypatch):
    scenario = _scenario_file(tmp_path / "scenario.yaml", {
        "url": "${ADAPTER_URL}", "headers": {"Authorization": "Bearer ${ADAPTER_KEY}"},
    })
    cli = CliRunner()
    monkeypatch.setenv("ADAPTER_URL", "http://example.test/turn")
    monkeypatch.delenv("ADAPTER_KEY", raising=False)
    result = cli.invoke(app, ["validate", str(scenario)])
    assert result.exit_code == 1 and "ADAPTER_KEY is missing or empty" in result.output
    monkeypatch.setenv("ADAPTER_KEY", "secret-value\r\ninvalid")
    result = cli.invoke(app, ["validate", str(scenario)])
    assert result.exit_code == 1 and "invalid header value" in result.output
    assert "secret-value" not in result.output
    monkeypatch.setenv("ADAPTER_KEY", "secret-value")
    assert cli.invoke(app, ["validate", str(scenario)]).exit_code == 0


def test_cli_calibration_ramp_and_offline_report_share_http_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTER_KEY", "secret-value")
    monkeypatch.setenv("ADAPTER_URL", "http://example.test/turn?key=url-secret")
    scenario = _scenario_file(tmp_path / "scenario.yaml", {
        "url": "${ADAPTER_URL}", "auth_env": "ADAPTER_KEY", "body": {"prompt": "{prompt}"},
        "response": {**JSON, "usage": {"input_path": "/in", "output_path": "/out"}},
    }, cost={"input_per_mtok": 1, "output_per_mtok": 1})
    calls = []
    original_client = httpx.AsyncClient

    def handle(request):
        assert str(request.url) == "http://example.test/turn?key=url-secret"
        assert request.headers["authorization"] == "Bearer secret-value"
        calls.append(request.headers["x-kneepoint-session"])
        return httpx.Response(200, json={"reply": "hello", "in": 0, "out": 1})

    def client(**kwargs):
        return original_client(**{**kwargs, "transport": httpx.MockTransport(handle)})

    monkeypatch.setattr(httpx, "AsyncClient", client)
    cli = CliRunner()
    command = ["run", "--scenario", str(scenario), "--out", str(tmp_path)]
    rejected = cli.invoke(app, [*command, "--model", "forbidden"])
    assert rejected.exit_code != 0 and not calls
    result = cli.invoke(app, command)
    assert result.exit_code == 0, result.output
    assert len(calls) == 2 and calls[0] == "calibration" and calls[0] != calls[1]
    meta_path = next(tmp_path.glob("run-*-meta.json"))
    meta = read_run_meta(meta_path)
    assert (meta.target_type, meta.response_mode, meta.target) == ("http", "json", "${ADAPTER_URL}")
    records_path = meta_path.with_name(meta_path.name.replace("-meta.json", ".jsonl"))
    records = read_jsonl(records_path)
    assert len(records) == 1 and records[0].ok and records[0].ttft_ms is None
    note = "TTFT not observable: buffered JSON response"
    assert note in result.output
    report = next(tmp_path.glob("run-*-report.html"))
    html = report.read_text()
    assert note in html and "<th>TTFT p50</th>" not in html
    rerendered = tmp_path / "rerendered.html"
    result = cli.invoke(app, ["report", str(records_path), "--out", str(rerendered)])
    assert result.exit_code == 0, result.output
    assert note in result.output and rerendered.read_text() == html
    for secret in ("secret-value", "url-secret"):
        assert all(secret not in path.read_text() for path in tmp_path.glob("run-*"))
