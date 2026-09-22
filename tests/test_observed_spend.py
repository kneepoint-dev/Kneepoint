import asyncio

import pytest

from kneepoint.analyze.budget import ObservedSpendController
from kneepoint.analyze.cost import CostRates
from kneepoint.collect.schemas import RequestRecord
from kneepoint.generator import session
from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.lifecycle import RunEvidence
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import RetryPolicy, SessionSpec, TurnsSpec
from kneepoint.targets.openai_compatible import TurnOutcome

RATES = CostRates(input_per_mtok=1, output_per_mtok=2)


def record(**changes):
    values = dict(session_id="s", concurrency=1, started_at=0, total_ms=10,
                  input_tokens=1_000_000, output_tokens=0, ok=True)
    return RequestRecord(**(values | changes))


def outcome(kw, **changes):
    identity = {name: kw[name] for name in
                ("session_id", "concurrency", "turn_index", "attempt")}
    return TurnOutcome(record=record(**(identity | changes)), text="done")


async def ramp(controller, *, concurrency=1, evidence=None, on_start=None):
    return await run_ramp(
        "http://unused", "mock",
        RampSpec(start=concurrency, stop=concurrency + 1, hold_seconds=0),
        CorpusSampler(["q"]),
        SessionSpec(turns=TurnsSpec(min=2, max=2),
                    retry=RetryPolicy(max_attempts=2, backoff_s=0)),
        stop=controller.stop, on_attempt_complete=controller.on_attempt_complete,
        on_attempt_start=on_start, evidence=evidence,
    )


def test_rejects_invalid_prices_and_caps():
    for invalid in (-1, float("nan"), float("inf"), -float("inf")):
        for field in ("input_per_mtok", "output_per_mtok"):
            with pytest.raises(ValueError, match=field):
                ObservedSpendController(CostRates(**{field: invalid}))
        with pytest.raises(ValueError, match="max_spend"):
            ObservedSpendController(RATES, invalid)


def test_prices_are_copied_and_explicit_zeros_remain_known():
    rates = CostRates(input_per_mtok=0, output_per_mtok=2)
    controller = ObservedSpendController(rates, 1)
    rates.input_per_mtok = 100
    controller.on_attempt_complete(record())
    controller.on_attempt_complete(record(attempt=2, input_tokens=0))
    snapshot = controller.snapshot()
    assert snapshot["total_spend"] == snapshot["known_spend"] == 0
    assert snapshot["coverage"]["complete_usage"] == 2
    assert not controller.stop.is_set()
    snapshot["coverage"]["complete_usage"] = 99
    assert controller.snapshot()["coverage"]["complete_usage"] == 2


def test_duplicate_is_idempotent_and_conflicting_payload_fails():
    controller = ObservedSpendController(RATES, 3)
    completion = record()
    controller.on_attempt_complete(completion)
    controller.on_attempt_complete(completion.model_copy())
    completion.total_ms = 20
    with pytest.raises(ValueError, match="conflicting completion"):
        controller.on_attempt_complete(completion)
    assert controller.snapshot()["known_spend"] == 1
    assert controller.snapshot()["coverage"]["completed_attempts"] == 1


def test_identity_distinguishes_retries_turns_levels_and_sessions():
    controller = ObservedSpendController(RATES)
    for changes in ({}, {"attempt": 2, "ok": False}, {"turn_index": 1},
                    {"concurrency": 2}, {"session_id": "other"}):
        controller.on_attempt_complete(record(**changes))
    assert controller.snapshot()["total_spend"] == 5
    assert controller.snapshot()["coverage"]["completed_attempts"] == 5
    assert controller.snapshot()["known_overshoot"] is None


@pytest.mark.parametrize("missing", ["input_tokens", "output_tokens"])
def test_partial_usage_stops_capped_run_and_retains_known_cost(missing):
    controller = ObservedSpendController(RATES, 10)
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, missing: None}
    controller.on_attempt_complete(record(**usage))
    snapshot = controller.snapshot()
    assert snapshot["total_spend"] is None
    assert snapshot["known_spend"] == (2 if missing == "input_tokens" else 1)
    assert snapshot["coverage"]["partial_usage"] == 1
    assert snapshot["coverage"]["input_known"] == (missing != "input_tokens")
    assert snapshot["coverage"]["output_known"] == (missing != "output_tokens")
    assert snapshot["stop_reason"] == "unknown_usage"
    assert snapshot["stop_requested"]
    assert snapshot["billing_upper_bound"] is None


def test_unknown_at_zero_prices_stays_unknown_and_first_reason_is_retained():
    controller = ObservedSpendController(CostRates(), 1)
    controller.on_attempt_complete(record(input_tokens=None, output_tokens=None))
    assert controller.snapshot()["total_spend"] is None
    assert controller.snapshot()["known_spend"] == 0
    assert controller.snapshot()["coverage"]["missing_usage"] == 1
    assert controller.snapshot()["stop_reason"] == "unknown_usage"
    controller.on_attempt_complete(record(attempt=2))
    assert controller.snapshot()["stop_reason"] == "unknown_usage"
    assert controller.snapshot()["coverage"]["completed_attempts"] == 2


def test_late_unknown_after_cap_preserves_known_overshoot_and_first_reason():
    controller = ObservedSpendController(RATES, 1)
    controller.on_attempt_complete(record())
    controller.on_attempt_complete(record(attempt=2, output_tokens=None))
    snapshot = controller.snapshot()
    assert snapshot["stop_reason"] == "cap_reached"
    assert snapshot["total_spend"] is None
    assert snapshot["known_spend"] == 2
    assert snapshot["known_overshoot"] == 1
    assert snapshot["billing_upper_bound"] is None


def test_negative_usage_is_rejected_without_mutating_accounting():
    controller = ObservedSpendController(RATES)
    with pytest.raises(ValueError, match="nonnegative"):
        controller.on_attempt_complete(record(input_tokens=-1))
    assert controller.snapshot()["coverage"]["completed_attempts"] == 0
    assert controller.snapshot()["known_spend"] == 0


async def test_zero_cap_stops_before_any_request_or_session(monkeypatch):
    controller = ObservedSpendController(RATES, 0)
    evidence = RunEvidence()

    def forbidden(*args, **kwargs):
        pytest.fail("a zero cap must prevent client creation and all attempt starts")

    monkeypatch.setattr("kneepoint.generator.pool.httpx.AsyncClient", forbidden)
    assert await ramp(controller, evidence=evidence, on_start=forbidden) == ([], [], False)
    assert evidence.status == "stopped"
    assert not evidence.attempts
    assert controller.snapshot()["stop_reason"] == "cap_reached"


@pytest.mark.parametrize("retry", [False, True])
async def test_cap_at_equality_drains_active_attempts_without_retry_or_next_turn(
    monkeypatch, retry,
):
    controller = ObservedSpendController(RATES, 1)
    evidence = RunEvidence()
    both_started, release_first, release_second = (asyncio.Event() for _ in range(3))
    starts = []

    async def turn(*args, **kw):
        index = len(starts)
        starts.append((kw["session_id"], kw["turn_index"], kw["attempt"]))
        assert index < 2, "admitted a retry, next turn, session, or ramp level after stop"
        if len(starts) == 2:
            both_started.set()
        await (release_first if index == 0 else release_second).wait()
        return outcome(kw, ok=not retry, status_code=503 if retry else 200)

    monkeypatch.setattr(session, "run_turn", turn)
    task = asyncio.create_task(ramp(controller, concurrency=2, evidence=evidence))
    try:
        await asyncio.wait_for(both_started.wait(), 1)
        release_first.set()
        await asyncio.wait_for(controller.stop.wait(), 1)
        assert not task.done()  # the second request must remain active and drain
        assert controller.snapshot()["known_spend"] == 1
        assert controller.snapshot()["known_overshoot"] == 0
        assert len(evidence.records) == 1
        release_second.set()
        sessions, records, legacy_exceeded = await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not legacy_exceeded  # the tuple flag remains the legacy exception signal
    assert len(sessions) == len(records) == len(starts) == 2
    assert records == evidence.records
    assert all(turn == 0 and attempt == 1 for _, turn, attempt in starts)
    assert evidence.status == "stopped"
    snapshot = controller.snapshot()
    assert snapshot["total_spend"] == 2
    assert snapshot["known_overshoot"] == 1
    assert snapshot["stop_reason"] == "cap_reached"
    assert snapshot["coverage"]["complete_usage"] == 2


async def test_uncapped_unknown_usage_continues_through_retries_turns_and_levels(monkeypatch):
    controller = ObservedSpendController(RATES)
    evidence = RunEvidence()

    async def turn(*args, **kw):
        retry = kw["turn_index"] == 0 and kw["attempt"] == 1
        return outcome(kw, ok=not retry, status_code=503 if retry else 200,
                       output_tokens=None if retry else 0)

    monkeypatch.setattr(session, "run_turn", turn)
    sessions, records, exceeded = await ramp(controller, evidence=evidence)
    assert not exceeded and not controller.stop.is_set()
    assert len(sessions) == 3 and len(records) == 9
    assert all(s.turns_completed == 2 for s in sessions)
    snapshot = controller.snapshot()
    assert snapshot["total_spend"] is None
    assert snapshot["known_spend"] == 9
    assert snapshot["coverage"]["partial_usage"] == 3
    assert snapshot["coverage"]["complete_usage"] == 6
    assert snapshot["stop_reason"] is None
    assert evidence.status == "completed"


def test_decimal_threshold_stops_at_equality_without_spurious_overshoot():
    controller = ObservedSpendController(CostRates(input_per_mtok=0.1), 1)
    for attempt in range(1, 11):
        controller.on_attempt_complete(record(attempt=attempt))
        assert controller.stop.is_set() == (attempt == 10)
    assert controller.snapshot()["total_spend"] == 1
    assert controller.snapshot()["known_overshoot"] == 0


async def test_capped_unknown_completion_prevents_retry_and_later_levels(monkeypatch):
    controller = ObservedSpendController(RATES, 10)
    evidence = RunEvidence()

    async def turn(*args, **kw):
        return outcome(kw, ok=False, status_code=503, output_tokens=None)

    monkeypatch.setattr(session, "run_turn", turn)
    sessions, records, exceeded = await ramp(controller, evidence=evidence)
    assert not exceeded
    assert len(sessions) == len(records) == len(evidence.attempts) == 1
    assert records == evidence.records
    assert records[0].attempt == 1
    assert evidence.status == "stopped"
    assert controller.snapshot()["stop_reason"] == "unknown_usage"
    assert controller.snapshot()["known_spend"] == 1
    assert controller.snapshot()["total_spend"] is None
