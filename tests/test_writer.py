import json
import platform

from kneepoint.collect.schemas import (
    SCHEMA_VERSION,
    ChaosShape,
    Environment,
    PriceRates,
    RampShape,
    RequestRecord,
    RetryShape,
    RunMetadata,
    SessionRecord,
    TurnsShape,
)
from kneepoint.collect.writer import (
    KNEEPOINT_VERSION,
    JsonlWriter,
    current_environment,
    read_jsonl,
    read_run_meta,
    write_run_meta,
)


def _rec(level: int, ms: float) -> RequestRecord:
    return RequestRecord(
        session_id=f"s{level}", concurrency=level, started_at=0.0,
        ttft_ms=ms / 2, total_ms=ms, input_tokens=2, output_tokens=40, ok=True,
    )


def _stamped(record):
    """What the writer will have made of a record: same fields, versions filled in."""
    return record.model_copy(update={
        "schema_version": SCHEMA_VERSION, "kneepoint_version": KNEEPOINT_VERSION,
    })


def test_jsonl_roundtrip(tmp_path):
    records = [_rec(1, 100.0), _rec(2, 200.0)]
    path = tmp_path / "out" / "run.jsonl"          # parent dir doesn't exist yet
    with JsonlWriter(path) as writer:
        writer.write_many(records)
    assert read_jsonl(path) == [_stamped(r) for r in records]


def test_jsonl_appends(tmp_path):
    path = tmp_path / "run.jsonl"
    with JsonlWriter(path) as writer:
        writer.write_many([_rec(1, 100.0)])
    with JsonlWriter(path) as writer:
        writer.write_many([_rec(2, 200.0)])
    assert len(read_jsonl(path)) == 2


def test_jsonl_roundtrip_session_records(tmp_path):
    session = SessionRecord(
        session_id="s1", concurrency=2, started_at=0.0, total_ms=10.0,
        turns_requested=1, turns_completed=1, ok=True,
        transcript=[{"role": "user", "content": "hi"}],
    )
    path = tmp_path / "sessions.jsonl"
    with JsonlWriter(path) as writer:
        writer.write_many([session])
    assert read_jsonl(path, SessionRecord) == [_stamped(session)]


# ---------------------------------------------------------------------------
# the version stamp: the JSONL is a public contract (docs/output-format.md)
# ---------------------------------------------------------------------------


def test_every_line_carries_the_version_not_just_the_first(tmp_path):
    """Per record, not a header line: a consumer that tails, splits or greps the
    file still sees the version on the line in front of it."""
    path = tmp_path / "run.jsonl"
    with JsonlWriter(path) as writer:
        writer.write_many([_rec(1, 100.0), _rec(2, 200.0), _rec(3, 300.0)])
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 3
    assert all(line["schema_version"] == SCHEMA_VERSION for line in lines)
    assert all(line["kneepoint_version"] == KNEEPOINT_VERSION for line in lines)
    assert all("session_id" in line for line in lines), "no line is a header"


def test_the_writer_stamps_even_when_the_producer_forgot(tmp_path):
    path = tmp_path / "run.jsonl"
    with JsonlWriter(path) as writer:
        writer.write_many([_rec(1, 100.0)])   # constructed with the v0 default
    assert read_jsonl(path)[0].schema_version == SCHEMA_VERSION


def test_files_written_before_versioning_read_as_v0(tmp_path):
    """Every existing run, all of Run D included, has no version field at all.
    Absent must parse as 0 and keep reading, never fail."""
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps({
        "session_id": "s", "concurrency": 1, "started_at": 0.0,
        "total_ms": 100.0, "ok": True,
    }) + "\n", encoding="utf-8")
    record = read_jsonl(path)[0]
    assert record.schema_version == 0
    assert record.kneepoint_version is None
    assert record.total_ms == 100.0


# ---------------------------------------------------------------------------
# the metadata sidecar: run-<id>-meta.json (docs/output-format.md)
# ---------------------------------------------------------------------------


def _meta(**overrides) -> RunMetadata:
    fields = dict(
        run_id="20260818-210000", command="run", target="http://127.0.0.1:1/v1",
        model="mock", ramp=RampShape(start=1, stop=3, step=1), hold_seconds=2.0,
        turns=TurnsShape(min=1, max=1), retry=RetryShape(max_attempts=3, backoff_s=0.5),
        seed=0, chaos=ChaosShape(profile="off"), price=None,
        min_samples=10, min_group=10, started_at=100.0, finished_at=110.0,
        environment=Environment(python="3.14.0", platform="test"),
    )
    fields.update(overrides)
    return RunMetadata(**fields)


def test_run_meta_roundtrip_is_stamped(tmp_path):
    meta = _meta(price=PriceRates(input_per_mtok=3.0, output_per_mtok=15.0, max_spend=1.0))
    path = tmp_path / "out" / "run-x-meta.json"          # parent dir doesn't exist yet
    write_run_meta(path, meta)
    back = read_run_meta(path)
    assert back == _stamped(meta)
    assert back.schema_version == SCHEMA_VERSION
    assert back.kneepoint_version == KNEEPOINT_VERSION
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["price"] == {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "max_spend": 1.0}
    assert raw["chaos"] == {"profile": "off", "faults": []}


def test_run_meta_is_one_indented_document(tmp_path):
    """A single record a human opens — not a JSONL stream — so it is pretty-printed."""
    path = tmp_path / "m.json"
    write_run_meta(path, _meta())
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n  ")
    assert text.endswith("}\n")
    assert json.loads(text)["run_id"] == "20260818-210000"


def test_run_meta_without_stamps_reads_as_version_zero(tmp_path):
    """Same promise as the JSONL: an unstamped file parses as schema 0, not an error."""
    raw = _meta().model_dump()
    del raw["schema_version"], raw["kneepoint_version"]
    path = tmp_path / "m.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    back = read_run_meta(path)
    assert back.schema_version == 0 and back.kneepoint_version is None


def test_current_environment_is_read_from_the_machine():
    env = current_environment()
    assert env.python == platform.python_version()
    assert env.platform == platform.platform()
