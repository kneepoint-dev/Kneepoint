"""Regression coverage for saved evidence from the installed command."""

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml


@contextmanager
def serve(handler):
    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, headers, body = handler(self.headers, request)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Target) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def cli(*args, env=None):
    command = shutil.which("kneepoint", path=str(Path(sys.executable).parent))
    assert command is not None, "install the package before running CLI tests"
    return subprocess.run(
        [command, *map(str, args)], env=env, capture_output=True, check=False, timeout=20,
    )


def run(tmp_path, url, *, adapter="http", target=None, conversation=None, options=None,
        flags=(), env=None, expected_exit=0):
    scenario = {
        "target": {
            "type": adapter, "url": url,
            **({"body": {"input": "{prompt}"},
                "response": {"mode": "json", "text_path": "/answer"}}
               if adapter == "http" else {}),
            **(target or {}),
        },
        "workload": {
            "ramp": {"from": 1, "to": 1, "hold_seconds": 0},
            "conversation": {"retry": {"max_attempts": 1}, **(conversation or {})},
        },
        **(options or {}),
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(scenario), encoding="utf-8")
    out = tmp_path / "out"
    result = cli("run", "--scenario", path, "--out", out, *flags, env=env)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    return out, result


def rows(out, suffix=".jsonl"):
    path, = (p for p in out.glob(f"run-*{suffix}") if suffix != ".jsonl"
             or not p.name.endswith("-sessions.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("adapter", ["http", "openai-compatible"])
def test_fault_header_only_accepts_known_names(tmp_path, adapter):
    marker = b"synthetic-fault-test-value"
    seen_auth = []

    def target(headers, _request):
        seen_auth.append(headers["Authorization"])
        return 500, {"X-Kneepoint-Fault": headers["Authorization"]}, b""

    auth = (
        {"headers": {"Authorization": "Bearer ${TEST_TARGET_TOKEN}"}} if adapter == "http"
        else {"auth_env": "TEST_TARGET_TOKEN"}
    )
    with serve(target) as url:
        out, result = run(tmp_path / "echo", url, adapter=adapter, target=auth,
                          env={**os.environ, "TEST_TARGET_TOKEN": marker.decode()})
        assert seen_auth == [f"Bearer {marker.decode()}"]
        leaks = [p.name for p in out.rglob("*") if p.is_file() and marker in p.read_bytes()]
        assert not leaks
        assert marker not in result.stdout + result.stderr
        assert [r["fault"] for r in rows(out)] == [None]
        assert [s["faults"] for s in rows(out, "-sessions.jsonl")] == [[]]

        out, _ = run(tmp_path / "chaos", url, adapter=adapter, options={
            "chaos": {"profile": "custom", "faults": [
                {"type": "llm_server_error", "probability": 1},
            ]},
        })
    assert [r["fault"] for r in rows(out)] == ["llm_server_error"]
    assert [s["faults"] for s in rows(out, "-sessions.jsonl")] == [["llm_server_error"]]


@pytest.mark.parametrize("mode", ["json", "sse", "sse_prior_event"])
@pytest.mark.parametrize("matches_capture", [True, False])
def test_current_capture_is_redacted_from_agent_errors(tmp_path, mode, matches_capture):
    capture = "synthetic-conversation-value"
    message = capture if matches_capture else "ordinary diagnostic"

    def target(_headers, _request):
        payload = {"id": capture, "error": message}
        if mode == "json":
            return 200, {}, json.dumps(payload).encode()
        prefix = ""
        if mode == "sse_prior_event":
            prefix = f'data: {json.dumps({"id": capture, "delta": "ok"})}\n\n'
            payload.pop("id")
        body = f"{prefix}event: error\ndata: {json.dumps(payload)}\n\n"
        return 200, {"Content-Type": "text/event-stream"}, body.encode()

    response = (
        {"mode": "json", "text_path": "/answer", "error_path": "/error"} if mode == "json"
        else {"mode": "sse", "text_path": "/delta", "error_event": "error", "error_path": "/error"}
    )
    with serve(target) as url:
        out, result = run(tmp_path, url, target={
            "response": response, "capture": {
                "name": "conversation", "path": "/id",
                "from": "json" if mode == "json" else "sse_event",
            },
        })

    record, = rows(out)
    assert record["error_kind"] == "agent_error"
    files = [p.name for p in out.iterdir() if capture.encode() in p.read_bytes()]
    assert not files
    assert capture.encode() not in result.stdout + result.stderr
    expected = "AgentError: string" if matches_capture else f"AgentError: {message}"
    assert record["error"] == expected
    report, = out.glob("*-report.html")
    assert expected.encode() in report.read_bytes()


@pytest.mark.parametrize(("adapter", "status", "payload", "expected"), [
    ("http", 500, {}, "http_error"),
    ("http", 302, {}, "http_error"),
    ("http", 429, {}, "http_error"),
    ("http", 503, {}, "http_error"),
    ("http", 400, {"error": {"code": {"nested": "value"}}}, "http_error"),
    ("http", 400, {"error": {"code": "context_length_exceeded"}}, "context_overflow"),
    ("http", 200, {"answer": "ok"}, None),
    ("openai-compatible", 500, {}, "http_error"),
])
def test_status_error_kind_in_saved_evidence(tmp_path, adapter, status, payload, expected):
    seen = []

    def target(_headers, _request):
        seen.append(status)
        return status, {"Location": "/elsewhere"}, json.dumps(payload).encode()

    with serve(target) as url:
        out, _ = run(tmp_path, url, adapter=adapter)
    assert seen == [status]
    record, = rows(out)
    assert record["ok"] is (expected is None)
    assert record["status_code"] == status
    assert record["error_kind"] == expected


def install_cli_probe(tmp_path, python):
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(python, encoding="utf-8")
    return {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(probe), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
    )}


@pytest.mark.parametrize("fail", [True, False])
def test_health_sampler_warns_once_and_leaves_run_usable(tmp_path, fail):
    import time

    env = install_cli_probe(tmp_path, """
from kneepoint.generator.health import HealthSampler

class SamplerFailure(Exception):
    pass

async def run(self):
    raise SamplerFailure("diagnostic payload")

HealthSampler.run = run
""") if fail else None

    def target(_headers, _request):
        if not fail:
            time.sleep(1.1)
        return 200, {}, b'{"answer":"ok"}'

    log = tmp_path / "health.jsonl"
    with serve(target) as url:
        out, result = run(tmp_path, url, flags=("--health-log", log), env=env)
    record, = rows(out)
    assert record["ok"]
    console = result.stdout + result.stderr
    if fail:
        assert console.count(b"Health sampler stopped (SamplerFailure)") == 1
        assert b"diagnostic payload" not in console
    else:
        assert b"Health sampler stopped" not in console
        sample = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert sample["source"] == "injector"
        assert sample["level"] == sample["inflight"] == 1
        assert "open_connections" in sample


@pytest.mark.parametrize(("honor", "status", "header", "expected"), [
    (False, 503, "90", 30.0),
    (True, 429, "90", 30.0),
    (True, 503, "90", 30.0),
    (False, 503, "1.25", 1.25),
    (False, 503, "0", 0.0),
    (False, 503, "invalid", 0.5),
])
def test_retry_after_wait_is_capped_and_recorded(tmp_path, honor, status, header, expected):
    waits = tmp_path / "waits.txt"
    env = install_cli_probe(tmp_path, f"""
from pathlib import Path
from kneepoint.generator import session

async def wait_or_stop(seconds, stop):
    with Path({str(waits)!r}).open("a", encoding="utf-8") as stream:
        stream.write(f"{{seconds}}\\n")

session.wait_or_stop = wait_or_stop
""")
    seen = []

    def target(_headers, _request):
        seen.append(len(seen))
        if len(seen) == 1:
            return status, {"Retry-After": header}, b""
        return 200, {}, b'{"answer":"ok"}'

    with serve(target) as url:
        out, _ = run(tmp_path, url, options={"client": {"honor_retry_after": honor}},
                     conversation={"retry": {"max_attempts": 2, "backoff_s": 0.5}}, env=env)
    first, second = rows(out)
    assert seen == [0, 1] and second["ok"]
    observed = [float(line) for line in waits.read_text().splitlines()] if waits.exists() else []
    assert observed == ([expected] if expected else [])
    assert first["backoff_s"] == (expected if header != "invalid" else 0.0)
    assert first["retry_after_s"] == (float(header) if header != "invalid" else None)
    assert second["backoff_s"] == 0.0


def test_builtin_without_replacement_uses_unique_items(tmp_path):
    def target(_headers, _request):
        return 200, {}, b'{"answer":"ok"}'

    with serve(target) as url:
        out, _ = run(tmp_path, url, conversation={
            "turns": {"min": 6, "max": 6}, "sampling": {"policy": "without_replacement"},
        }, target={"body": {"input": "{prompt}", "session": "{session_id}"}})
    selections = [record["selection"] for record in rows(out)]
    assert {selection["item_id"] for selection in selections} == set("123456")
    assert all(selection["first_use"] for selection in selections)


def test_priced_calibration_preserves_one_item_measured_corpus(tmp_path):
    corpus = tmp_path / "corpus.txt"
    corpus.write_text("the only item", encoding="utf-8")
    sessions = []

    def target(headers, request):
        assert request["input"] == "the only item"
        sessions.append(headers["X-Kneepoint-Session"])
        return 200, {}, b'{"answer":"ok","input":3,"output":1}'

    with serve(target) as url:
        out, _ = run(tmp_path, url, conversation={
            "corpus": str(corpus), "sampling": {"policy": "without_replacement"},
        }, target={"response": {
            "mode": "json", "text_path": "/answer",
            "usage": {"input_path": "/input", "output_path": "/output"},
        }}, flags=("--price-in", "1", "--price-out", "1", "--force"))
    record, = rows(out)
    assert sessions == ["calibration", record["session_id"]]
    assert record["selection"]["item_id"] == "1" and record["selection"]["first_use"]


def test_unpriced_http_report_replays_byte_for_byte(tmp_path):
    import hashlib

    def target(_headers, _request):
        return 200, {"Content-Type": "text/event-stream"}, b'data: {"delta":"ok"}\n\n'

    with serve(target) as url:
        out, _ = run(tmp_path, url, target={"response": {"mode": "sse", "text_path": "/delta"}})
    assert rows(out)[0]["ok"]
    records, = [
        path for path in out.glob("run-*.jsonl") if not path.name.endswith("-sessions.jsonl")
    ]
    original, = out.glob("run-*-report.html")
    replay = out / "replay.html"
    result = cli("report", records, "--out", replay)
    assert result.returncode == 0, result.stdout + result.stderr
    assert hashlib.sha256(original.read_bytes()).digest() == hashlib.sha256(
        replay.read_bytes()
    ).digest()


@pytest.mark.parametrize("variant", ["documented", "unquoted_off", "yaml_true", "sampled"])
def test_trace_modes_from_yaml_use_expected_headers(tmp_path, variant):
    import re

    docs = (Path(__file__).resolve().parents[1] / "docs/http-targets.md").read_text(
        encoding="utf-8"
    )
    documented = re.search(r"\ntrace:\n  propagation: [^\n]+\n", docs).group()
    propagation = {
        "documented": documented,
        "unquoted_off": "trace: {propagation: off}\n",
        "yaml_true": "trace: {propagation: true}\n",
        "sampled": "trace: {propagation: sampled}\n",
    }[variant]
    seen = []

    def target(headers, _request):
        seen.append(headers.get("traceparent"))
        return 200, {}, b'{"answer":"ok"}'

    scenario = tmp_path / "scenario.yaml"
    out = tmp_path / "out"
    with serve(target) as url:
        scenario.write_text(propagation + f"target:\n  type: http\n  url: {url}\n"
                            "  body: {input: '{prompt}'}\n"
                            "  response: {mode: json, text_path: /answer}\n",
                            encoding="utf-8")
        result = cli("run", "--scenario", scenario, "--ramp", "1..1", "--hold-seconds", 0,
                     "--out", out)
    if variant == "yaml_true":
        assert result.returncode == 1 and b"invalid scenario" in result.stdout
        assert not seen and not out.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        record, = rows(out)
        assert record["ok"] and record["trace_id"]
        assert seen == [record["traceparent"]]
        if variant == "sampled":
            assert record["traceparent"].endswith("-01")
        else:
            assert record["traceparent"] is None


@pytest.mark.parametrize("has_capture", [False, True])
def test_undispatched_capture_failure_has_no_wire_or_first_use_evidence(tmp_path, has_capture):
    seen = []

    def target(headers, _request):
        seen.append(headers["traceparent"])
        return 200, {}, b'{"answer":"ok","id":"usable"}' if has_capture else b'{"answer":"ok"}'

    selection = tmp_path / "selection.jsonl"
    for name in ("one", "two"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")
    with serve(target) as url:
        out, _ = run(tmp_path, url, conversation={
            "turns": {"min": 2, "max": 2}, "sampling": {"policy": "without_replacement"},
            "corpus": "*.txt",
        }, target={
            "body": {"input": "{prompt}", "conversation": "{{capture.id}}"},
            "capture": {"name": "id", "path": "/id", "from": "json"},
        }, flags=("--selection-log", selection))
    first, second = rows(out)
    chosen = [json.loads(line) for line in selection.read_text().splitlines()]
    assert first["traceparent"] == seen[0]
    assert first["selection"]["first_use"] and chosen[0]["first_use"]
    if has_capture:
        assert second["ok"] and second["traceparent"] == seen[1]
        assert second["selection"]["first_use"] and chosen[1]["first_use"]
    else:
        assert len(seen) == 1 and second["error_kind"] == "capture_missing"
        assert (second["traceparent"], second["selection"]["first_use"],
                chosen[1]["first_use"]) == (None, False, False)


def test_knowledge_workload_cli_has_no_single_turn_think_time(tmp_path):
    source = Path(__file__).resolve().parents[1] / "workloads/agent-knowledge.yaml"
    scenario = yaml.safe_load(source.read_text(encoding="utf-8"))
    conversation = scenario["workload"]["conversation"]
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([
        {"prompt": task, "item_id": task, "task_class": task}
        for task in ("lookup", "two_step", "off_topic")
    ]), encoding="utf-8")

    def target(_headers, _request):
        return 200, {}, b'{"output":{"text":"ok"}}'

    with serve(target) as url:
        out, _ = run(tmp_path, url, target={**scenario["target"], "url": url},
                     conversation={**conversation, "corpus": str(corpus)})
    assert rows(out)[0]["ok"]
    assert "think_time_ms" not in conversation


def test_logged_fields_and_http_body_contract_are_documented(tmp_path):
    import time

    def target(_headers, _request):
        time.sleep(1.1)
        return 200, {}, b'{"answer":"ok","accepted":false,"usage":{"input":7,"output":2}}'

    health = tmp_path / "health.jsonl"
    selection = tmp_path / "selection.jsonl"
    with serve(target) as url:
        out, _ = run(tmp_path, url, target={
            "success": {"path": "/accepted", "equals": [True]},
            "response": {"mode": "json", "text_path": "/answer",
                         "usage": {"input_path": "/usage/input", "output_path": "/usage/output"}},
        }, flags=("--health-log", health, "--selection-log", selection))
    record, = rows(out)
    assert record["status_code"] == 200 and record["ok"] is False
    assert record["error_kind"] == "body_failure"
    assert (record["input_tokens"], record["output_tokens"]) == (7, 2)

    docs = (Path(__file__).resolve().parents[1] / "docs/output-format.md").read_text(
        encoding="utf-8"
    )
    for path in (health, selection):
        keys = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert all(f'| `{key}` |' in docs for key in keys)
    for field in ("task_class", "open_connections_reason"):
        assert f'| `{field}` |' in docs
    assert "Remove the machine hostname" in docs
    assert "a parsed HTTP 200 can have `ok=false`" in docs
