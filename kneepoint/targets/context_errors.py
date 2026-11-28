"""Recognize explicit server context-length rejections without logging payloads."""
import json


def is_context_overflow(value) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "context_overflow" or value.get("code") in (
            "context_length_exceeded", "context_window_exceeded",
        ):
            return True
        return any(is_context_overflow(value.get(k)) for k in ("error", "message", "detail"))
    if not isinstance(value, str):
        return False
    text = value.lower()
    return any(marker in text for marker in (
        "maximum context length", "exceeds the available context size",
        "exceed_context_size_error", "context length exceeded",
        "context window exceeded", "exceeds the context size",
        "prompt is too long", "input is too large for the context"))


def exception_is_context_overflow(exc) -> bool:
    body = getattr(exc, "body", None)
    if body is not None and is_context_overflow(body):
        return True
    try:
        return is_context_overflow(json.loads(str(exc)))
    except (ValueError, TypeError):
        return is_context_overflow(str(exc))
