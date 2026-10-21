"""Frozen output schema and writer contract, independent of documentation sources."""

import json

import pytest

from kneepoint.collect.schemas import RequestRecord, RunMetadata, SessionRecord
from kneepoint.collect.writer import JsonlWriter, read_run_meta, write_run_meta

REQUEST_FIELDS = ['schema_version',
 'kneepoint_version',
 'session_id',
 'concurrency',
 'started_at',
 'started_monotonic_s',
 'ttft_ms',
 'total_ms',
 'input_tokens',
 'output_tokens',
 'ok',
 'error',
 'turn_index',
 'attempt',
 'trace_id',
 'span_id',
 'fault',
 'status_code',
 'itl_mean_ms',
 'itl_p99_ms',
 'chunk_count',
 'abandoned']

SESSION_FIELDS = ['schema_version',
 'kneepoint_version',
 'session_id',
 'concurrency',
 'started_at',
 'total_ms',
 'turns_requested',
 'turns_completed',
 'ok',
 'faults',
 'transcript',
 'resolved',
 'resolution_method']

META_FIELDS = ['schema_version',
 'kneepoint_version',
 'run_id',
 'command',
 'target',
 'model',
 'ramp',
 'hold_seconds',
 'turns',
 'retry',
 'seed',
 'chaos',
 'price',
 'min_samples',
 'min_group',
 'slo',
 'started_at',
 'finished_at',
 'environment',
 'started_monotonic_s',
 'finished_monotonic_s',
 'level_starts_monotonic_s',
 'level_status',
 'injector_levels',
 'think_time_min_ms',
 'think_time_max_ms']

@pytest.mark.parametrize(("model", "fields"), [
    (RequestRecord, REQUEST_FIELDS), (SessionRecord, SESSION_FIELDS), (RunMetadata, META_FIELDS),
])
def test_model_fields_match_frozen_contract(model, fields):
    assert list(model.model_fields) == fields


def test_written_lines_follow_frozen_field_order(tmp_path):
    records = [
        (RequestRecord(session_id="s", concurrency=1, started_at=0, total_ms=1, ok=True),
         REQUEST_FIELDS),
        (SessionRecord(session_id="s", concurrency=1, started_at=0, total_ms=1,
                       turns_requested=1, turns_completed=1, ok=True), SESSION_FIELDS),
    ]
    for n, (record, fields) in enumerate(records):
        path = tmp_path / f"{n}.jsonl"
        with JsonlWriter(path) as writer:
            writer.write_many([record])
        assert list(json.loads(path.read_text())) == fields


def test_metadata_round_trip_follows_frozen_contract(tmp_path):
    meta = RunMetadata(
        run_id="contract", command="run", target="http://localhost/v1", model="mock",
        ramp={"start": 1, "stop": 2, "step": 1}, hold_seconds=1,
        turns={"min": 1, "max": 1}, retry={"max_attempts": 1, "backoff_s": 0},
        seed=0, chaos={"profile": "off", "faults": []}, min_samples=1, min_group=1,
        started_at=0, finished_at=1, environment={"python": "3.12", "platform": "test"},
    )
    path = write_run_meta(tmp_path / "meta.json", meta)
    assert list(json.loads(path.read_text())) == META_FIELDS
    restored = read_run_meta(path)
    stamps = {"schema_version", "kneepoint_version"}
    assert restored.model_dump(exclude=stamps) == meta.model_dump(exclude=stamps)
