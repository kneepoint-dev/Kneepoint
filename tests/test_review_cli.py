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
