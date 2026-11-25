"""Installed-command coverage for scenario preflight and client identification."""

import json
from pathlib import Path

import pytest
import yaml

from tests.test_saved_evidence_cli import cli, rows, run, serve


@pytest.mark.parametrize("adapter", ["http", "openai-compatible"])
@pytest.mark.parametrize("override", [None, "acme-load-test/1"])
def test_user_agent_identifies_the_client(tmp_path, adapter, override):
    observed = []

    def target(headers, _body):
        observed.append(headers["User-Agent"])
        if adapter == "http":
            return 200, {}, b'{"answer":"ok"}'
        return 200, {}, b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'

    options = {"client": {"user_agent": override}} if override else None
    with serve(target) as url:
        out, _ = run(tmp_path, url, adapter=adapter, options=options)
    version = cli("--version").stdout.decode().split()[-1]
    assert observed == [override or f"kneepoint/{version}"]
    assert rows(out)[0]["ok"]
    assert all(path.stat().st_size for path in out.iterdir())


@pytest.mark.parametrize("field,value", [("step", 0), ("step", -1), ("hold_seconds", -1)])
def test_invalid_ramp_is_rejected_by_validate_and_run(tmp_path, field, value):
    scenario = {
        "target": {"url": "http://127.0.0.1:1/v1"},
        "workload": {"ramp": {"from": 1, "to": 1, field: value}},
    }
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(scenario))
    for args in (("validate", path), ("run", "--scenario", path, "--out", tmp_path / "out")):
        result = cli(*args)
        assert result.returncode == 1
        assert field.encode() in result.stdout
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("conversation,field", [
    ({"retry": {"max_attempts": 0}}, "max_attempts"),
    ({"turns": {"min": 2, "max": 1}}, "turns"),
    ({"think_time_ms": {"min_ms": 2, "max_ms": 1}}, "think_time_ms"),
])
def test_reversed_or_empty_session_settings_are_rejected(tmp_path, conversation, field):
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump({
        "target": {"url": "http://127.0.0.1:1/v1"},
        "workload": {"conversation": conversation},
    }))
    for args in (("validate", path), ("run", "--scenario", path, "--out", tmp_path / "out")):
        result = cli(*args)
        assert result.returncode == 1
        assert field.encode() in result.stdout
    assert not (tmp_path / "out").exists()


def test_corpus_preflight_and_explicit_outside_permission(tmp_path, monkeypatch):
    corpus = tmp_path / "shared"
    corpus.mkdir()
    item = corpus / "prompt.md"
    item.write_text("A local example prompt")
    monkeypatch.setenv("EXAMPLE_CLIENT_KEY", "fixture-only-value")
    seen = []

    def target(_headers, body):
        seen.append(body["input"])
        return 200, {}, json.dumps({"answer": "ok"}).encode()

    directory = tmp_path / "scenario"
    with serve(target) as url:
        out, result = run(
            directory, url, target={"auth_env": "EXAMPLE_CLIENT_KEY"},
            conversation={"corpus": str(item)}, expected_exit=2,
        )
        assert result.returncode == 2 and not out.exists()
        assert not seen
        out, result = run(
            directory, url, target={"auth_env": "EXAMPLE_CLIENT_KEY"},
            conversation={"corpus": str(item)}, flags=("--allow-corpus-outside",),
        )
    assert seen == ["A local example prompt"]
    assert rows(out)[0]["ok"]
    assert b"EXAMPLE_CLIENT_KEY" in result.stdout
    assert str(corpus).encode() in result.stdout
    assert b"fixture-only-value" not in result.stdout + result.stderr
    for file in out.iterdir():
        assert b"fixture-only-value" not in file.read_bytes()


def test_bundled_scenarios_validate_with_local_corpora():
    examples = Path(__file__).resolve().parents[1] / "examples"
    for path in sorted(examples.rglob("kneepoint.yaml")):
        result = cli("validate", path)
        assert result.returncode == 0, result.stdout + result.stderr


def test_scenario_only_accepts_documented_corpus_types(tmp_path):
    scenario = tmp_path / "scenario.yaml"

    def validate_corpus(name):
        (tmp_path / name).write_text("A local example prompt")
        scenario.write_text(yaml.safe_dump({
            "target": {"url": "http://127.0.0.1:1/v1"},
            "workload": {"conversation": {"corpus": name}},
        }))
        return cli("validate", scenario)

    assert validate_corpus("prompt.txt").returncode == 0
    assert validate_corpus("prompt.bin").returncode != 0
