from kneepoint.collect.schemas import RequestRecord, SessionRecord


def test_request_record_defaults_keep_single_turn_lines_parsable():
    # a line as kneepoint wrote it before turn/attempt/fault/status existed
    line = {
        "session_id": "abc", "concurrency": 4, "started_at": 0.0,
        "ttft_ms": 10.0, "total_ms": 20.0, "input_tokens": 2,
        "output_tokens": 40, "ok": True, "error": None,
    }
    rec = RequestRecord.model_validate(line)
    assert rec.turn_index == 0
    assert rec.attempt == 1
    assert rec.fault is None
    assert rec.status_code is None


def test_session_record_roundtrip():
    session = SessionRecord(
        session_id="abc", concurrency=4, started_at=0.0, total_ms=1500.0,
        turns_requested=3, turns_completed=3, ok=True,
        faults=["llm_rate_limit"],
        transcript=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
    )
    again = SessionRecord.model_validate_json(session.model_dump_json())
    assert again == session
    assert again.resolved is None
