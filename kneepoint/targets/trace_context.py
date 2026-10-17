"""W3C request context derived from the identity of a load attempt."""

import hashlib
import json
import uuid
from urllib.parse import quote


def new_run_id() -> str:
    """Give direct API callers one stable identity per runner invocation."""
    return uuid.uuid4().hex


def attempt_context(
    run_id: str, session_id: str, concurrency: int, turn_index: int, attempt: int,
) -> tuple[str, str, str, str]:
    """Return trace ID, span ID, traceparent and baggage for one attempt.

    JSON frames the tuple unambiguously, including IDs containing separators.
    Separate hash domains prevent a trace ID and span ID from sharing a prefix.
    """
    identity = json.dumps(
        [run_id, session_id, concurrency, turn_index, attempt],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    trace_id = hashlib.sha256(b"kneepoint.trace.v1\0" + identity).hexdigest()[:32]
    span_id = hashlib.sha256(b"kneepoint.span.v1\0" + identity).hexdigest()[:16]
    # W3C forbids all-zero IDs. This also covers that astronomically unlikely digest.
    if trace_id == "0" * 32:
        trace_id = "0" * 31 + "1"
    if span_id == "0" * 16:
        span_id = "0" * 15 + "1"
    traceparent = f"00-{trace_id}-{span_id}-01"
    labels = {
        "kneepoint.run_id": run_id,
        "kneepoint.level": str(concurrency),
        "kneepoint.session": session_id,
        "kneepoint.turn": str(turn_index),
        "kneepoint.attempt": str(attempt),
    }
    baggage = ",".join(f"{key}={quote(value, safe='')}" for key, value in labels.items())
    return trace_id, span_id, traceparent, baggage
