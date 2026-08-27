"""Tool-fault attribution outside `kneepoint demo`.

The proxy has always served tool faults and counted them; only `kneepoint demo`
could attribute them to sessions, because it is the one command where proxy and
ramp share a process. Every other resilience score was therefore computed from
LLM faults alone, with faulted sessions silently counted as clean — a bias
toward 100 (METHODOLOGY §6b, Run E: 35 tool faults served, zero attributed).

These tests cover the durable fault log that closes the gap, and the whole
cross-process path end to end.
"""

import json
import os

import httpx
import pytest

from kneepoint.analyze.resilience import resilience
from kneepoint.chaos.faults import FaultSpec
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.proxy import FaultLog, merge_fault_log, start_proxy
from kneepoint.chaos.transport import ChaosTransport
from kneepoint.cli import _merge_tool_faults
from kneepoint.collect.schemas import SessionRecord
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import RetryPolicy, SessionSpec, TurnsSpec
from kneepoint.judge.deterministic import CheckConfig, apply_deterministic


def _session(session_id: str, **kw) -> SessionRecord:
    base = dict(concurrency=1, started_at=0.0, total_ms=1.0, turns_requested=1,
                turns_completed=1, ok=True)
    base.update(kw)
    return SessionRecord(session_id=session_id, **base)


# ---------------------------------------------------------------------------
# the log itself
# ---------------------------------------------------------------------------


def test_fault_log_survives_the_process_boundary(tmp_path):
    path = tmp_path / "faults.jsonl"
    log = FaultLog(path)
    log.record("tool_timeout", "aaa")
    log.record("tool_malformed_json", "aaa")
    log.record("tool_timeout", "bbb")

    reloaded = FaultLog.load(path)
    assert reloaded.counts == {"tool_timeout": 2, "tool_malformed_json": 1}
    assert reloaded.by_session == {"aaa": ["tool_timeout", "tool_malformed_json"],
                                   "bbb": ["tool_timeout"]}


def test_fault_log_creates_its_parent_directory(tmp_path):
    log = FaultLog(tmp_path / "nested" / "deeper" / "faults.jsonl")
    log.record("tool_timeout", "aaa")
    assert log.path.exists()


def test_fault_log_skips_lines_it_cannot_read(tmp_path):
    path = tmp_path / "faults.jsonl"
    path.write_text(
        json.dumps({"ts": 1, "fault": "tool_timeout", "session_id": "aaa"}) + "\n"
        + "not json at all\n"
        + json.dumps({"ts": 2, "no_fault_key": True}) + "\n"
        + "\n"
        + json.dumps({"ts": 3, "fault": "tool_timeout", "session_id": "bbb"}) + "\n",
        encoding="utf-8",
    )
    assert FaultLog.load(path).counts == {"tool_timeout": 2}


def test_merge_attributes_faults_to_the_sessions_that_hit_them(tmp_path):
    path = tmp_path / "faults.jsonl"
    log = FaultLog(path)
    log.record("tool_timeout", "aaa")
    log.record("tool_malformed_json", "aaa")
    log.record("tool_timeout", "bbb")

    sessions = [_session("aaa"), _session("bbb"), _session("ccc")]
    merge = merge_fault_log(path, sessions)

    assert merge.attributed == 3
    assert merge.sessions_touched == 2
    assert merge.by_type == {"tool_timeout": 2, "tool_malformed_json": 1}
    assert sessions[0].faults == ["tool_timeout", "tool_malformed_json"]
    assert sessions[2].faults == []


def test_merge_counts_faults_the_agent_never_labelled(tmp_path):
    """No session header means the agent isn't echoing x-kneepoint-session. That
    fault can never be attributed, and pretending it didn't happen is the bias."""
    path = tmp_path / "faults.jsonl"
    log = FaultLog(path)
    log.record("tool_timeout", None)
    log.record("tool_timeout", "aaa")

    merge = merge_fault_log(path, [_session("aaa")])
    assert merge.unattributed == 1
    assert merge.attributed == 1


def test_merge_does_not_invent_faults_from_another_run(tmp_path):
    path = tmp_path / "faults.jsonl"
    FaultLog(path).record("tool_timeout", "from-an-older-run")

    sessions = [_session("aaa")]
    merge = merge_fault_log(path, sessions)
    assert merge.unmatched == 1
    assert merge.attributed == 0
    assert sessions[0].faults == []


def test_merge_reports_a_missing_log_rather_than_scoring_without_it(tmp_path):
    assert merge_fault_log(tmp_path / "nope.jsonl", [_session("aaa")]) is None


def test_merge_keeps_llm_faults_already_on_the_session(tmp_path):
    path = tmp_path / "faults.jsonl"
    FaultLog(path).record("tool_timeout", "aaa")
    sessions = [_session("aaa", faults=["llm_rate_limit"])]
    merge_fault_log(path, sessions)
    assert sessions[0].faults == ["llm_rate_limit", "tool_timeout"]


# ---------------------------------------------------------------------------
# end to end: a run that never shares a process with the proxy
# ---------------------------------------------------------------------------


# well above the standard profile: a ~2s hold only issues a couple of dozen
# requests, and the grid assertions below need all four faults to actually fire
ALL_FOUR = [
    FaultSpec(type="llm_rate_limit", probability=0.30),
    FaultSpec(type="llm_server_error", probability=0.30),
    FaultSpec(type="tool_timeout", probability=0.30),
    FaultSpec(type="tool_malformed_json", probability=0.30),
]


@pytest.fixture
def tool_env():
    saved = {k: os.environ.get(k) for k in ("MOCK_TOOL_URL", "MOCK_TOOL_TIMEOUT_MS")}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


async def test_a_run_gets_the_full_four_fault_grid_from_the_log(
    mock_agent_url, tool_env, tmp_path
):
    """The Run E gap, closed: tool faults reach `session.faults` without the
    proxy and the ramp sharing a process — the log file carries them."""
    fault_log = tmp_path / "faults.jsonl"
    agent_base = mock_agent_url.removesuffix("/v1")
    handle = start_proxy(agent_base, ChaosInjector(ALL_FOUR, seed=3), fault_log=fault_log)
    os.environ["MOCK_TOOL_URL"] = f"{handle.url}/tool/search"
    os.environ["MOCK_TOOL_TIMEOUT_MS"] = "300"
    try:
        transport = ChaosTransport(httpx.AsyncHTTPTransport(),
                                   ChaosInjector(ALL_FOUR, seed=1))
        sessions, _records, _ = await run_ramp(
            mock_agent_url, "mock",
            RampSpec(start=2, stop=4, step=2, hold_seconds=2.0),
            CorpusSampler(["how do I reset my password"], seed=0),
            SessionSpec(turns=TurnsSpec(min=1, max=2),
                        retry=RetryPolicy(max_attempts=2, backoff_s=0.05)),
            seed=0, transport=transport,
        )
    finally:
        handle.stop()

    assert handle.log.counts, "the proxy served no tool faults - raise probabilities"

    # before the merge this run is exactly the Run E situation
    assert not any(
        f.startswith("tool_") for s in sessions for f in s.faults
    ), "tool faults must not reach sessions except through the merge"

    merge = merge_fault_log(fault_log, sessions)
    assert merge is not None
    assert merge.attributed > 0, "tool faults served but none attributed"

    apply_deterministic(sessions, CheckConfig(kind="contains", value="[RESOLVED"))
    grid = {row.fault for row in resilience(sessions, min_group=1).rows}
    assert grid == {"llm_rate_limit", "llm_server_error",
                    "tool_timeout", "tool_malformed_json"}


async def test_the_grid_is_llm_only_without_the_merge(mock_agent_url, tool_env, tmp_path):
    """The regression the durable fault log exists to prevent: skip the merge
    and the tool faults are served, logged, and completely invisible to the
    score."""
    fault_log = tmp_path / "faults.jsonl"
    agent_base = mock_agent_url.removesuffix("/v1")
    handle = start_proxy(agent_base, ChaosInjector(ALL_FOUR, seed=3), fault_log=fault_log)
    os.environ["MOCK_TOOL_URL"] = f"{handle.url}/tool/search"
    os.environ["MOCK_TOOL_TIMEOUT_MS"] = "300"
    try:
        transport = ChaosTransport(httpx.AsyncHTTPTransport(),
                                   ChaosInjector(ALL_FOUR, seed=1))
        sessions, _records, _ = await run_ramp(
            mock_agent_url, "mock",
            RampSpec(start=2, stop=2, step=2, hold_seconds=2.0),
            CorpusSampler(["how do I reset my password"], seed=0),
            SessionSpec(turns=TurnsSpec(min=1, max=1)),
            seed=0, transport=transport,
        )
    finally:
        handle.stop()

    apply_deterministic(sessions, CheckConfig(kind="contains", value="[RESOLVED"))
    grid = {row.fault for row in resilience(sessions, min_group=1).rows}
    assert handle.log.counts, "the proxy served no tool faults - raise probabilities"
    assert not (grid & {"tool_timeout", "tool_malformed_json"})


# ---------------------------------------------------------------------------
# what the operator is told
# ---------------------------------------------------------------------------


def test_cli_names_every_way_the_grid_can_be_incomplete(tmp_path):
    path = tmp_path / "faults.jsonl"
    log = FaultLog(path)
    log.record("tool_timeout", "aaa")
    log.record("tool_malformed_json", None)          # agent didn't echo the header
    log.record("tool_timeout", "from-an-older-run")  # shared log

    lines: list[str] = []
    _merge_tool_faults(path, [_session("aaa")], echo=lines.append)
    output = "\n".join(lines)

    assert "tool_timeout x1" in output
    assert "not echoing x-kneepoint-session" in output
    assert "outside this run" in output


def test_cli_refuses_to_stay_quiet_about_a_missing_log(tmp_path):
    lines: list[str] = []
    _merge_tool_faults(tmp_path / "nope.jsonl", [_session("aaa")], echo=lines.append)
    assert "biases the score toward 100" in "\n".join(lines)
