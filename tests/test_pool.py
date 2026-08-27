import time

from kneepoint.generator.corpus import CorpusSampler
from kneepoint.generator.pool import run_level
from kneepoint.generator.session import SessionSpec

_SAMPLER = CorpusSampler(["hi"], seed=0)
_SPEC = SessionSpec()


async def test_run_level_one_record_per_worker_minimum(mock_agent_url):
    sessions, records = await run_level(
        mock_agent_url, "mock", 3, 0.1, _SAMPLER, _SPEC
    )
    assert len(sessions) >= 3
    assert len(records) >= 3
    assert all(r.concurrency == 3 for r in records)
    assert all(s.ok for s in sessions)


async def test_run_level_runs_concurrently(mock_agent_url):
    t0 = time.perf_counter()
    sessions, records = await run_level(
        mock_agent_url, "mock", 4, 0.1, _SAMPLER, _SPEC
    )
    elapsed = time.perf_counter() - t0
    # each mock request takes ~1s; 4 sequential would be ~4s, 4 concurrent ~1s
    assert len(records) >= 4
    assert elapsed < 3.0
