"""Trace context is an attempt identity, including retries and failed requests."""

import json
import re
from urllib.parse import unquote

import httpx
from typer.testing import CliRunner

from kneepoint.cli import app as cli_app
from kneepoint.collect.schemas import RequestRecord
from kneepoint.collect.writer import KNEEPOINT_VERSION, JsonlWriter, read_jsonl
from kneepoint.demo.agent import create_app
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import RetryPolicy, SessionSpec
from kneepoint.targets.openai_compatible import run_turn
from kneepoint.targets.trace_context import attempt_context


def _baggage(header: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in (
        unquote(member.strip()) for member in header.split(",")
    ))


def test_ids_are_stable_distinct_and_well_formed():
    first = attempt_context("run/A", "s", 4, 2, 1)
    assert first == attempt_context("run/A", "s", 4, 2, 1)
    second = attempt_context("run/A", "s", 4, 2, 2)
    assert first[:2] != second[:2]
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", first[2])
    assert first[0] != "0" * 32 and first[1] != "0" * 16
    assert _baggage(first[3]) == {
        "kneepoint.run_id": "run/A", "kneepoint.level": "4",
        "kneepoint.session": "s", "kneepoint.turn": "2",
        "kneepoint.attempt": "1",
    }


async def test_retry_attempts_each_send_context_and_round_trip(tmp_path):
    seen = []

    def target(request):
        seen.append(request.headers)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')

    spec = SessionSpec(retry=RetryPolicy(max_attempts=2, backoff_s=0))
    sessions, records, exceeded = await run_ramp(
        "http://target/v1", "mock", RampSpec(start=1, stop=1, hold_seconds=0),
        CorpusSampler(["hello"], seed=1), spec,
        run_id="run-1", transport=httpx.MockTransport(target),
    )
    assert not exceeded and len(sessions) == 1
    assert [r.attempt for r in records] == [1, 2]
    assert len(seen) == 2
    assert records[0].trace_id != records[1].trace_id
    for header, record in zip(seen, records, strict=True):
        assert header["x-kneepoint-session"] == record.session_id
        assert header["traceparent"] == f"00-{record.trace_id}-{record.span_id}-01"
        assert _baggage(header["baggage"]) == {
            "kneepoint.run_id": "run-1", "kneepoint.level": "1",
            "kneepoint.session": record.session_id, "kneepoint.turn": "0",
            "kneepoint.attempt": str(record.attempt),
        }
        assert "tracestate" not in header
    path = tmp_path / "requests.jsonl"
    with JsonlWriter(path) as writer:
        writer.write_many(records)
    assert read_jsonl(path) == [
        record.model_copy(update={"schema_version": 1,
                                  "kneepoint_version": KNEEPOINT_VERSION})
        for record in records
    ]
    assert all(r.schema_version == 1 for r in read_jsonl(path))


async def test_mock_agent_receives_traceparent_and_no_trace_omits_headers():
    seen = []
    agent = create_app(base_delay_ms=0, token_delay_ms=0, output_tokens=1)

    async def receiving_agent(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/v1/chat/completions":
            seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await agent(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiving_agent),
        headers={"traceparent": "stale", "baggage": "stale=1", "tracestate": "stale=1"},
    ) as client:
        traced = await run_turn(
            client, "http://agent/v1", "mock", [{"role": "user", "content": "hi"}],
            concurrency=3, session_id="s1", run_id="r1",
        )
        untraced = await run_turn(
            client, "http://agent/v1", "mock", [{"role": "user", "content": "hi"}],
            concurrency=3, session_id="s2", run_id="r1", trace=False,
        )
    assert traced.record.ok and untraced.record.ok
    assert seen[0]["traceparent"] == (
        f"00-{traced.record.trace_id}-{traced.record.span_id}-01"
    )
    assert "baggage" in seen[0]
    assert "tracestate" not in seen[0]
    assert not {"traceparent", "baggage", "tracestate"}.intersection(seen[1])
    assert untraced.record.trace_id is None and untraced.record.span_id is None


def test_old_record_defaults_and_new_fields_are_additive():
    old = RequestRecord.model_validate({
        "session_id": "s", "concurrency": 1, "started_at": 0,
        "total_ms": 1, "ok": True,
    })
    assert old.trace_id is None and old.span_id is None
    new_line = json.loads(old.model_copy(update={"trace_id": "a" * 32,
                                          "span_id": "b" * 16}).model_dump_json())
    assert RequestRecord.model_validate(new_line).trace_id == "a" * 32


def test_cli_no_trace_and_scenario_option(mock_agent_url, tmp_path):
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        f"trace: false\ntarget: {{url: {mock_agent_url}}}\n"
        "workload:\n  ramp: {from: 1, to: 1, hold_seconds: 0}\n",
        encoding="utf-8",
    )
    for suffix, args in (
        ("flag", ["--target", mock_agent_url, "--no-trace"]),
        ("scenario", ["--scenario", str(scenario)]),
    ):
        out = tmp_path / suffix
        result = CliRunner().invoke(
            cli_app, ["run", *args, "--ramp", "1..1", "--hold-seconds", "0",
                      "--out", str(out)],
        )
        assert result.exit_code == 0, result.output
        path = next(path for path in out.glob("run-*.jsonl") if "-sessions" not in path.name)
        assert read_jsonl(path)
        assert all(r.trace_id is None and r.span_id is None for r in read_jsonl(path))
