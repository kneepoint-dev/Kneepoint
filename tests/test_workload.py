import asyncio
from collections import Counter

import httpx
import pytest

from kneepoint.collect.schemas import RequestRecord
from kneepoint.generator.corpus import CorpusExhausted, CorpusSampler, SamplingConfig
from kneepoint.generator.health import HealthSampler
from kneepoint.generator.session import RetryPolicy, SessionSpec, run_session
from kneepoint.targets.openai_compatible import run_turn


def test_seeded_policies_and_exhaustion():
    for policy in ('uniform', 'zipf', 'without_replacement'):
        a, b = [CorpusSampler(['a', 'b', 'c'], seed=9,
                             config=SamplingConfig(policy=policy)) for _ in range(2)]
        assert [a.sample() for _ in range(3)] == [b.sample() for _ in range(3)]
        if policy == 'without_replacement':
            with pytest.raises(CorpusExhausted):
                a.sample()


def test_zipf_rank_frequency_100k():
    sampler = CorpusSampler([str(i) for i in range(20)], seed=73,
                            config=SamplingConfig(policy='zipf', exponent=1.0))
    counts = Counter(sampler.sample() for _ in range(100_000))
    normalizer = sum(1 / i for i in range(1, 21))
    for rank in range(1, 21):
        expected = 100_000 / rank / normalizer
        assert abs(counts[str(rank - 1)] - expected) < 6 * expected ** 0.5


@pytest.mark.parametrize('weights', [
    {}, {'a': .4, 'b': .4}, {'a': -.1, 'b': 1.1}, {'a': float('nan')},
])
def test_bad_weights(weights):
    with pytest.raises(ValueError):
        SamplingConfig(task_weights=weights)


def test_weighted_classes():
    items = [{'prompt': x, 'item_id': x, 'task_class': x} for x in ('a', 'b')]
    sampler = CorpusSampler(items, config=SamplingConfig(task_weights={'a': .7, 'b': .3}))
    counts = Counter(sampler.sample() for _ in range(10_000))
    assert .68 < counts['a'] / 10000 < .72
    with pytest.raises(ValueError):
        CorpusSampler(items, config=SamplingConfig(task_weights={'missing': 1}))


async def test_deadline_is_not_retried():
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(.1)
        return httpx.Response(200)

    import random
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, records = await run_session(client, 'http://test', 'mock', CorpusSampler(['hello']),
            SessionSpec(deadline_seconds=.005, retry=RetryPolicy(max_attempts=3)),
            concurrency=1, rng=random.Random(0))
    assert len(calls) == len(records) == 1
    assert records[0].ok is False and records[0].error_kind == 'timeout'


async def test_attempt_identity_traceparent_and_legacy():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
        text='data: [DONE]\n\n'))) as client:
        kwargs = dict(concurrency=1, session_id='same')
        a = await run_turn(client, 'http://test', 'mock', [], **kwargs)
        b = await run_turn(client, 'http://test', 'mock', [], trace=False, **kwargs)
    assert a.record.request_id != b.record.request_id
    assert a.record.traceparent.startswith('00-' + a.record.trace_id)
    assert b.record.traceparent is None
    old = RequestRecord(session_id='old', concurrency=1, started_at=1, total_ms=1, ok=True)
    assert old.request_id is old.traceparent is None


async def test_health_fields(tmp_path):
    import json
    sampler = HealthSampler(tmp_path / 'health.jsonl', population=lambda: 3)
    task = asyncio.create_task(sampler.run())
    await asyncio.sleep(1.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    row = json.loads(sampler.path.read_text().splitlines()[0])
    for key in ('t', 'host', 'source', 'lag_ms', 'loop_delay_ms', 'cpu_pct', 'host_cpu_pct',
                'open_connections', 'inflight'):
        assert key in row
    assert row['inflight'] == 3
    if row['open_connections'] is None:
        assert row['open_connections_reason']


def test_selection_first_use_is_dispatch_based():
    sampler = CorpusSampler(['x'])
    item = sampler.sample_item()
    assert sampler.dispatched(item)['first_use'] is True
    assert sampler.dispatched(item)['first_use'] is False


def test_json_corpus_roundtrip_and_weighted_exhaustion(tmp_path):
    import json
    items = [{'item_id': 'one', 'prompt': 'hello', 'task_class': 'a',
              'popularity_rank': 1, 'template_id': 'T1', 'length_class': 'long',
              'off_topic': False}]
    path = tmp_path / 'corpus.json'
    path.write_text(json.dumps(items))
    sampler = CorpusSampler.from_glob(str(path), config=SamplingConfig(
        policy='without_replacement', task_weights={'a': 1.0}))
    item = sampler.sample_item()
    assert item == items[0]
    assert sampler.dispatched(item)['length_class'] == 'long'
    with pytest.raises(CorpusExhausted):
        sampler.sample_item()


async def test_http_deadline_records_actual_trace_header_and_closes():
    from kneepoint.targets.http import HttpTurn
    from kneepoint.targets.http_config import HttpTargetConfig

    closed = []

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: token\ndata: {"delta":"a"}\n\n'
            await asyncio.sleep(1)

        async def aclose(self):
            closed.append(True)

    headers = []

    def handler(request):
        headers.append(dict(request.headers))
        return httpx.Response(200, stream=Slow())

    config = HttpTargetConfig.model_validate({
        'type': 'http', 'url': 'http://test/stream', 'method': 'POST',
        'body': {'input': '{prompt}'},
        'response': {'mode': 'sse', 'event': 'token', 'text_path': '/delta'},
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpTurn(config, config.resolve())(client, '', '',
            [{'role': 'user', 'content': 'x'}], concurrency=1, session_id='s', wall_seconds=.01)
    assert result.record.error_kind == 'timeout' and not result.record.ok
    assert result.record.traceparent == headers[0]['traceparent']
    assert closed


def test_cli_selection_log(tmp_path, mock_agent_url):
    import json

    from typer.testing import CliRunner

    from kneepoint.cli import app

    selected = tmp_path / 'selection.jsonl'
    result = CliRunner().invoke(app, ['run', '--target', mock_agent_url, '--ramp', '1..1',
        '--hold-seconds', '0', '--turns', '1..1', '--selection-log', str(selected),
        '--out', str(tmp_path / 'run')])
    assert result.exit_code == 0, result.output
    row = json.loads(selected.read_text().splitlines()[0])
    assert row['request_id'] and row['first_use'] is True
    assert all(k in row for k in ('item_id', 'popularity_rank', 'template_id', 'length_class',
                                 'off_topic'))
