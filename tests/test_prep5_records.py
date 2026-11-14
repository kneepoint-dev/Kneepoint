"""Live custom sinks receive producer versions and categorized HTTP failures."""
import asyncio
from importlib.metadata import version

import httpx
import pytest

from kneepoint.collect.schemas import RequestRecord, RunMetadata
from kneepoint.collect.writer import version_stamps
from kneepoint.targets.http import HttpTurn
from kneepoint.targets.http_config import HttpTargetConfig
from kneepoint.targets.openai_compatible import _record


@pytest.mark.parametrize(('mode', 'body', 'expected'), [
    ('json', {'output': {'text': ''}, 'error': {'message': 'retained detail'}}, 'agent_error'),
    ('sse', 'event: error\ndata: {"message":"retained detail"}\n\n', 'agent_error'),
    ('json', 'malformed', 'response_error'),
    ('sse', 'event: token\ndata: {"delta": 42}\n\n', 'response_error'),
    ('http', None, 'http_error'),
])
def test_failed_http_attempt_kind_and_live_version(mode, body, expected):
    cfg = HttpTargetConfig.model_validate({
        'type': 'http', 'url': 'http://example.test',
        'response': ({'mode': 'sse', 'text_path': '/delta', 'error_event': 'error'}
                     if mode == 'sse' else
                     {'mode': 'json', 'text_path': '/output/text', 'error_path': '/error'}),
    })

    def respond(request):
        if mode == 'http':
            raise httpx.ConnectError('unreachable', request=request)
        return (httpx.Response(200, json=body) if isinstance(body, dict)
                else httpx.Response(200, text=body))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpTurn(cfg, cfg.resolve())(
                client, '', '', [], concurrency=1, session_id='test')
        rec = result.record
        assert rec.ok is False
        assert rec.error_kind == expected
        if expected == 'agent_error':
            assert rec.error == 'AgentError: retained detail'
        assert rec.kneepoint_version == version('kneepoint')
        assert rec.model_dump()['schema_version'] == 1
    asyncio.run(run())


def test_openai_failure_and_live_version():
    import time
    rec = _record('test', 1, 0, 1, 0, time.perf_counter(), ok=False, error='test')
    assert rec.error_kind == 'target_error'
    assert rec.kneepoint_version == version('kneepoint')


def test_custom_metadata_sink_and_legacy_unknown():
    meta = RunMetadata.model_construct(**version_stamps(), run_id='test')
    assert meta.model_dump()['kneepoint_version'] == version('kneepoint')
    legacy = RequestRecord(session_id='old', concurrency=1, started_at=0, total_ms=1, ok=True)
    assert legacy.kneepoint_version is None and legacy.schema_version == 0
