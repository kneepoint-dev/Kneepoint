"""Deterministic resolution checks: contains/regex on the final assistant message."""

import re
from typing import Literal

from pydantic import BaseModel

from kneepoint.collect.schemas import SessionRecord


class CheckConfig(BaseModel):
    kind: Literal["contains", "regex"] = "contains"
    value: str


def _last_assistant_text(session: SessionRecord) -> str | None:
    for message in reversed(session.transcript):
        if message.get("role") == "assistant":
            return str(message.get("content", ""))
    return None


def check_session(session: SessionRecord, check: CheckConfig) -> bool:
    """A session resolves only if it completed AND its final answer passes the check."""
    if not session.ok:
        return False
    text = _last_assistant_text(session)
    if text is None:
        return False
    if check.kind == "contains":
        return check.value in text
    return re.search(check.value, text) is not None


def apply_deterministic(sessions: list[SessionRecord], check: CheckConfig) -> int:
    for session in sessions:
        session.resolved = check_session(session, check)
        session.resolution_method = "deterministic"
    return len(sessions)
