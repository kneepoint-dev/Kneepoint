from kneepoint.collect.schemas import SessionRecord
from kneepoint.judge.deterministic import CheckConfig, apply_deterministic, check_session


def _session(text: str | None, ok: bool = True, concurrency: int = 1) -> SessionRecord:
    transcript = [{"role": "user", "content": "q"}]
    if text is not None:
        transcript.append({"role": "assistant", "content": text})
    return SessionRecord(
        session_id="s", concurrency=concurrency, started_at=0.0, total_ms=1.0,
        turns_requested=1, turns_completed=1 if ok else 0, ok=ok, transcript=transcript,
    )


def test_contains_check():
    check = CheckConfig(kind="contains", value="[RESOLVED")
    assert check_session(_session("tok0 tok1 [RESOLVED kb-3]"), check)
    assert not check_session(_session("tok0 [TOOL-ERROR timeout]"), check)


def test_regex_check():
    check = CheckConfig(kind="regex", value=r"\[RESOLVED kb-\d+\]")
    assert check_session(_session("[RESOLVED kb-3]"), check)
    assert not check_session(_session("[RESOLVED kb-x]"), check)


def test_failed_or_empty_sessions_are_unresolved():
    check = CheckConfig(value="[RESOLVED")
    assert not check_session(_session(None), check)
    assert not check_session(_session("[RESOLVED kb-1]", ok=False), check)


def test_apply_sets_fields_on_every_session():
    sessions = [_session("[RESOLVED kb-1]"), _session("[TOOL-ERROR parse]")]
    judged = apply_deterministic(sessions, CheckConfig(value="[RESOLVED"))
    assert judged == 2
    assert [s.resolved for s in sessions] == [True, False]
    assert all(s.resolution_method == "deterministic" for s in sessions)
