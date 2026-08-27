import httpx

from kneepoint.chaos.faults import STANDARD_PROFILE, FaultSpec
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.transport import ChaosTransport


def _always(fault_type: str) -> ChaosInjector:
    return ChaosInjector([FaultSpec(type=fault_type, probability=1.0)], seed=0)


def test_injector_probability_zero_never_fires():
    injector = ChaosInjector([FaultSpec(type="llm_rate_limit", probability=0.0)], seed=0)
    assert all(injector.pick("llm") is None for _ in range(100))


def test_injector_is_seeded():
    faults = [FaultSpec(type="llm_rate_limit", probability=0.5)]

    def sequence(seed: int) -> list:
        injector = ChaosInjector(faults, seed=seed)
        return [injector.pick("llm") for _ in range(50)]

    a, b = sequence(7), sequence(7)
    assert [f.type if f else None for f in a] == [f.type if f else None for f in b]
    assert any(a) and not all(a)


def test_injector_scopes_llm_vs_tool():
    injector = _always("tool_timeout")
    assert injector.pick("llm") is None
    assert injector.pick("tool").type == "tool_timeout"


def test_standard_profile_has_the_four_v0_faults():
    assert {f.type for f in STANDARD_PROFILE} == {
        "llm_rate_limit", "llm_server_error", "tool_timeout", "tool_malformed_json"
    }


async def test_transport_injects_429_with_headers(mock_agent_url):
    transport = ChaosTransport(httpx.AsyncHTTPTransport(), _always("llm_rate_limit"))
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(f"{mock_agent_url}/chat/completions", json={})
    assert resp.status_code == 429
    assert resp.headers["x-kneepoint-fault"] == "llm_rate_limit"
    assert resp.headers["retry-after"] == "1"


async def test_transport_forwards_when_no_fault(mock_agent_url):
    transport = ChaosTransport(
        httpx.AsyncHTTPTransport(),
        ChaosInjector([FaultSpec(type="llm_rate_limit", probability=0.0)], seed=0),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(mock_agent_url.removesuffix("/v1") + "/")
    assert resp.status_code == 200
    assert "x-kneepoint-fault" not in resp.headers


async def test_session_recovers_from_rate_limits_with_retries(mock_agent_url):
    """End-to-end proof: chaos 429s + retry policy => session still resolves,
    and the wasted attempts show up as records with fault set."""
    import random

    from kneepoint.generator.corpus import CorpusSampler
    from kneepoint.generator.session import RetryPolicy, SessionSpec, run_session

    transport = ChaosTransport(
        httpx.AsyncHTTPTransport(),
        ChaosInjector([FaultSpec(type="llm_rate_limit", probability=0.5)], seed=3),
    )
    spec = SessionSpec(retry=RetryPolicy(max_attempts=5, backoff_s=0.01))
    async with httpx.AsyncClient(transport=transport, timeout=30) as client:
        session, records = await run_session(
            client, mock_agent_url, "mock", CorpusSampler(["hi"], seed=0), spec,
            concurrency=1, rng=random.Random(0),
        )
    faulted = [r for r in records if r.fault == "llm_rate_limit"]
    assert faulted, "seed 3 at p=0.5 must inject at least one 429"
    assert all(r.status_code == 429 and not r.ok for r in faulted)
    assert session.ok, "retries should recover from 429s"
    assert set(session.faults) == {"llm_rate_limit"}
