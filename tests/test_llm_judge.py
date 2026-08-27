import json

import httpx

from kneepoint.judge.llm_judge import JudgeConfig, judge_sessions
from tests.test_deterministic_judge import _session


def _judge_transport(verdict: dict | str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        content = verdict if isinstance(verdict, str) else json.dumps(verdict)
        body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def _cfg(sample_rate: float = 1.0) -> JudgeConfig:
    return JudgeConfig(base_url="http://judge.test/v1", model="judge", sample_rate=sample_rate)


async def test_judge_marks_resolved_sessions():
    sessions = [_session("all good, ticket closed"), _session("sorry, cannot help")]
    judged = await judge_sessions(
        sessions, _cfg(), transport=_judge_transport({"resolved": True, "reason": "done"})
    )
    assert judged == 2
    assert all(s.resolved is True for s in sessions)
    assert all(s.resolution_method == "llm_judge" for s in sessions)


async def test_sampling_is_seeded_and_partial():
    sessions = [_session(f"answer {i}") for i in range(10)]
    judged = await judge_sessions(
        sessions, _cfg(sample_rate=0.3), seed=1,
        transport=_judge_transport({"resolved": False, "reason": "no"}),
    )
    assert judged == 3
    assert sum(1 for s in sessions if s.resolved is not None) == 3
    picked_a = [i for i, s in enumerate(sessions) if s.resolved is not None]

    sessions2 = [_session(f"answer {i}") for i in range(10)]
    await judge_sessions(
        sessions2, _cfg(sample_rate=0.3), seed=1,
        transport=_judge_transport({"resolved": False, "reason": "no"}),
    )
    assert [i for i, s in enumerate(sessions2) if s.resolved is not None] == picked_a


async def test_garbage_verdict_leaves_session_unjudged():
    sessions = [_session("hello")]
    judged = await judge_sessions(
        sessions, _cfg(), transport=_judge_transport("I think it went well!")
    )
    assert judged == 0
    assert sessions[0].resolved is None


async def test_judge_http_error_never_raises():
    sessions = [_session("hello")]
    judged = await judge_sessions(
        sessions, _cfg(), transport=_judge_transport({"resolved": True}, status=500)
    )
    assert judged == 0
    assert sessions[0].resolved is None


async def test_verdict_json_embedded_in_prose_is_extracted():
    sessions = [_session("hello")]
    judged = await judge_sessions(
        sessions, _cfg(),
        transport=_judge_transport('Sure! Here is my verdict: {"resolved": true, "reason": "ok"}'),
    )
    assert judged == 1
    assert sessions[0].resolved is True
