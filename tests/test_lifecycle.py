import asyncio

import pytest

from kneepoint.analyze.budget import CostMeter
from kneepoint.analyze.cost import CostRates
from kneepoint.collect.schemas import RequestRecord
from kneepoint.generator import session
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import RunEvidence, wait_or_stop
from kneepoint.generator.pool import run_level
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import RetryPolicy, SessionSpec, ThinkTime, TurnsSpec
from kneepoint.targets.openai_compatible import TurnOutcome


def outcome(kw, *, ok=True):
    return TurnOutcome(record=RequestRecord(
        session_id=kw["session_id"], concurrency=kw["concurrency"],
        turn_index=kw["turn_index"], attempt=kw["attempt"],
        started_at=0, total_ms=10, ok=ok, status_code=200 if ok else 503,
        input_tokens=1_000_000, output_tokens=0), text="done")


def two_turns():
    return SessionSpec(turns=TurnsSpec(min=2, max=2))


async def level(spec=None, **kwargs):
    return await run_level("http://unused", "mock", kwargs.pop("concurrency", 1), 0,
                           CorpusSampler(["q"]), spec or SessionSpec(), **kwargs)


async def test_pre_stopped_level_starts_no_transport_or_session(monkeypatch):
    stop = asyncio.Event()
    stop.set()
    evidence = RunEvidence()

    def forbidden(*args, **kwargs):
        pytest.fail("transport constructed despite pre-stop")

    monkeypatch.setattr("kneepoint.generator.pool.httpx.AsyncClient", forbidden)
    assert await level(stop=stop, evidence=evidence) == ([], [])
    assert evidence.status == "stopped" and not evidence.attempts


async def test_completion_is_visible_before_later_turn_and_legacy_callback(monkeypatch):
    blocked, release = asyncio.Event(), asyncio.Event()
    evidence, order = RunEvidence(), []

    async def turn(*args, **kw):
        if kw["turn_index"] == 1:
            blocked.set()
            await release.wait()
        return outcome(kw)

    monkeypatch.setattr(session, "run_turn", turn)
    task = asyncio.create_task(level(two_turns(), evidence=evidence,
        on_attempt_start=lambda event: order.append(("start", event.turn_index)),
        on_attempt_complete=lambda record: order.append(("complete", record.turn_index)),
        on_record=lambda record: order.append(("legacy", record.turn_index))))
    await asyncio.wait_for(blocked.wait(), 1)
    assert len(evidence.records) == 1 and not evidence.sessions
    assert order == [("start", 0), ("complete", 0), ("start", 1)]
    release.set()
    sessions, records = await asyncio.wait_for(task, 1)
    assert len(sessions) == 1 and len(records) == 2
    assert order[-3:] == [("complete", 1), ("legacy", 0), ("legacy", 1)]
    assert all(a["status"] == "completed" for a in evidence.attempts.values())


@pytest.mark.parametrize("retry", [False, True])
async def test_stop_wakes_retry_or_think_wait_without_next_attempt(monkeypatch, retry):
    waiting, stop = asyncio.Event(), asyncio.Event()
    evidence = RunEvidence()

    async def turn(*args, **kw):
        return outcome(kw, ok=not retry)

    async def observed_wait(seconds, signal):
        waiting.set()
        await wait_or_stop(seconds, signal)

    monkeypatch.setattr(session, "run_turn", turn)
    monkeypatch.setattr(session, "wait_or_stop", observed_wait)
    spec = SessionSpec(turns=TurnsSpec(min=2, max=2),
                       retry=RetryPolicy(backoff_s=3600),
                       think_time=ThinkTime(min_ms=3600000, max_ms=3600000))
    task = asyncio.create_task(level(spec, stop=stop, evidence=evidence))
    await asyncio.wait_for(waiting.wait(), 1)
    stop.set()
    sessions, records = await asyncio.wait_for(task, 1)
    assert len(records) == 1
    assert evidence.status == "stopped"
    assert evidence.session_status[sessions[0].session_id] == "stopped"
    assert sessions[0].resolved is None


async def test_cooperative_stop_drains_two_active_requests_once(monkeypatch):
    both_started, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    evidence = RunEvidence()
    calls = 0

    async def turn(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_started.set()
        await release.wait()
        return outcome(kw)

    monkeypatch.setattr(session, "run_turn", turn)
    task = asyncio.create_task(level(two_turns(), concurrency=2, stop=stop, evidence=evidence))
    await asyncio.wait_for(both_started.wait(), 1)
    stop.set()
    assert not task.done()
    release.set()
    sessions, records = await asyncio.wait_for(task, 1)
    assert calls == len(records) == 2
    assert len(sessions) == 2 and all(s.turns_completed == 1 for s in sessions)
    assert len({(r.session_id, r.turn_index, r.attempt) for r in records}) == 2


async def test_worker_failure_joins_sibling_and_retains_completed_evidence(monkeypatch):
    blocked, sibling_closed = asyncio.Event(), asyncio.Event()
    ids = {}
    evidence = RunEvidence()
    failure = RuntimeError("original failure")

    async def turn(*args, **kw):
        worker = ids.setdefault(kw["session_id"], len(ids))
        if worker == 1:
            await blocked.wait()
            raise failure
        if kw["turn_index"] == 0:
            return outcome(kw)
        blocked.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_closed.set()

    monkeypatch.setattr(session, "run_turn", turn)
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(level(two_turns(), concurrency=2, evidence=evidence), 1)
    assert raised.value is failure and sibling_closed.is_set()
    assert len(evidence.records) == 1
    assert set(evidence.session_status.values()) == {"failed", "cancelled"}
    assert sorted(a["status"] for a in evidence.attempts.values()) == [
        "cancelled", "completed", "failed"]
    assert evidence.status == "failed"


async def test_external_cancellation_retains_records_without_fabricating_active_record(monkeypatch):
    blocked = asyncio.Event()
    evidence = RunEvidence()

    async def turn(*args, **kw):
        if kw["turn_index"] == 0:
            return outcome(kw)
        blocked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(session, "run_turn", turn)
    task = asyncio.create_task(level(two_turns(), evidence=evidence))
    await asyncio.wait_for(blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert evidence.status == "cancelled"
    assert len(evidence.records) == 1
    assert len(evidence.sessions) == 1 and evidence.sessions[0].resolved is None
    assert sorted(a["status"] for a in evidence.attempts.values()) == ["cancelled", "completed"]


async def test_completion_observer_failure_preserves_original_exception_and_record(monkeypatch):
    evidence = RunEvidence()
    failure = OSError("writer failed")

    async def turn(*args, **kw):
        return outcome(kw)

    def observer(record):
        assert evidence.records == [record]
        raise failure

    monkeypatch.setattr(session, "run_turn", turn)
    with pytest.raises(OSError) as raised:
        await level(evidence=evidence, on_attempt_complete=observer)
    assert raised.value is failure and len(evidence.records) == 1
    assert evidence.sessions[0].turns_completed == 1
    assert evidence.sessions[0].transcript[-1]["content"] == "done"
    assert evidence.session_status[evidence.sessions[0].session_id] == "failed"


async def test_ramp_budget_stop_keeps_aborted_level_and_does_not_double_meter(monkeypatch):
    async def turn(*args, **kw):
        return outcome(kw)

    monkeypatch.setattr(session, "run_turn", turn)
    evidence, early = RunEvidence(), []
    meter = CostMeter(CostRates(input_per_mtok=1), max_spend=.5)
    sessions, records, exceeded = await run_ramp(
        "http://unused", "mock", RampSpec(start=1, stop=2, hold_seconds=0),
        CorpusSampler(["q"]), two_turns(), evidence=evidence,
        on_record=meter.add, on_attempt_complete=early.append)
    assert exceeded and meter.spend == 1  # legacy callback still stops at its first record
    assert len(records) == len(early) == 2 and len(sessions) == 1
    assert evidence.status == evidence.level_status[1] == "budget_exceeded"
    assert all(r.concurrency == 1 for r in records)


async def test_shared_evidence_does_not_leak_previous_levels_into_returned_slices(monkeypatch):
    async def turn(*args, **kw):
        return outcome(kw)

    monkeypatch.setattr(session, "run_turn", turn)
    evidence = RunEvidence()
    await level(evidence=evidence)
    sessions, records = await level(concurrency=2, evidence=evidence)
    assert len(sessions) == len(records) == 2
    assert len(evidence.records) == len(evidence.sessions) == 3
    assert all(r.concurrency == 2 for r in records)


@pytest.mark.parametrize("where", ["enter", "exit"])
async def test_client_lifecycle_failure_sets_terminal_status(monkeypatch, where):
    evidence = RunEvidence()
    failure = OSError("client lifecycle failed")

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            if where == "enter":
                raise failure
            return self

        async def __aexit__(self, *args):
            if where == "exit":
                raise failure

    async def turn(*args, **kw):
        return outcome(kw)

    monkeypatch.setattr("kneepoint.generator.pool.httpx.AsyncClient", Client)
    monkeypatch.setattr(session, "run_turn", turn)
    with pytest.raises(OSError) as raised:
        await level(evidence=evidence)
    assert raised.value is failure
    assert evidence.status == evidence.level_status[1] == "failed"
    assert len(evidence.records) == (1 if where == "exit" else 0)


def test_cli_persists_each_completed_attempt_once_before_generation_failure(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from kneepoint.cli import app
    from kneepoint.collect.writer import read_jsonl

    failure = RuntimeError("second turn failed")

    async def turn(*args, **kw):
        if kw["turn_index"] == 1:
            # First completion has already been flushed while this session is active.
            records_path = next(tmp_path.glob("run-*.jsonl"))
            assert len(read_jsonl(records_path)) == 1
            raise failure
        return outcome(kw)

    monkeypatch.setattr(session, "run_turn", turn)
    result = CliRunner().invoke(app, ["run", "--target", "http://unused", "--model", "mock",
                                     "--ramp", "1..1", "--hold-seconds", "0", "--turns", "2..2",
                                     "--out", str(tmp_path)])
    assert result.exception is failure
    lifecycle = json.loads(next(tmp_path.glob("*-lifecycle.json")).read_text())
    assert lifecycle["status"] == "failed" and lifecycle["scope"] == "load_generation"
    assert lifecycle["completed_requests"] == 1
    paths = [p for p in tmp_path.glob("run-*.jsonl") if not p.name.endswith("-sessions.jsonl")]
    assert len(read_jsonl(paths[0])) == 1
    saved_sessions = [json.loads(line) for line in next(
        tmp_path.glob("*-sessions.jsonl")).read_text().splitlines()]
    assert len(saved_sessions) == 1
    assert saved_sessions[0]["turns_completed"] == 1
    assert saved_sessions[0]["resolved"] is None


def test_cli_normal_run_has_no_duplicate_records_from_level_callback(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from kneepoint.cli import app
    from kneepoint.collect.writer import read_jsonl

    async def turn(*args, **kw):
        return outcome(kw)

    monkeypatch.setattr(session, "run_turn", turn)
    result = CliRunner().invoke(app, ["run", "--target", "http://unused", "--model", "mock",
                                     "--ramp", "1..1", "--hold-seconds", "0", "--turns", "2..2",
                                     "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    paths = [p for p in tmp_path.glob("run-*.jsonl") if not p.name.endswith("-sessions.jsonl")]
    records = read_jsonl(paths[0])
    assert len(records) == 2
    assert [r.turn_index for r in records] == [0, 1]
    lifecycle = json.loads(next(tmp_path.glob("*-lifecycle.json")).read_text())
    assert lifecycle["status"] == "completed"


async def test_successful_retry_has_start_then_completion_for_each_attempt(monkeypatch):
    order = []

    async def turn(*args, **kw):
        return outcome(kw, ok=kw["attempt"] == 2)

    monkeypatch.setattr(session, "run_turn", turn)
    sessions, records = await level(
        SessionSpec(retry=RetryPolicy(max_attempts=2, backoff_s=0)),
        on_attempt_start=lambda event: order.append(("start", event.attempt)),
        on_attempt_complete=lambda record: order.append(("complete", record.attempt)),
        on_record=lambda record: order.append(("legacy", record.attempt)),
    )
    assert sessions[0].ok
    assert [r.attempt for r in records] == [1, 2]
    assert order == [("start", 1), ("complete", 1), ("start", 2), ("complete", 2),
                     ("legacy", 1), ("legacy", 2)]
