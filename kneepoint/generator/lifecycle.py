"""Optional synchronous lifecycle observers and caller-owned partial evidence.

Observers run on the generator event loop and must not block. Exceptions propagate;
completed evidence is retained before a completion observer is invoked.
"""

import asyncio
import time
from dataclasses import asdict, dataclass, field

from kneepoint.collect.schemas import InjectorLevel, RequestRecord, SessionRecord


@dataclass(frozen=True)
class AttemptStart:
    session_id: str
    concurrency: int
    turn_index: int
    attempt: int
    emitted_at: float

    @property
    def key(self) -> str:
        return f"{self.session_id}:{self.concurrency}:{self.turn_index}:{self.attempt}"


@dataclass
class RunEvidence:
    """Survives a generator exception when the caller retains this object.

    `records` contains completed attempts only. Incomplete attempts exist solely
    in `attempts`; no latency, usage or RequestRecord is invented for them.
    Session execution status is separate from task success (`SessionRecord.ok`).
    """

    records: list[RequestRecord] = field(default_factory=list)
    sessions: list[SessionRecord] = field(default_factory=list)
    attempts: dict[str, dict] = field(default_factory=dict)
    session_status: dict[str, str] = field(default_factory=dict)
    level_status: dict[int, str] = field(default_factory=dict)
    level_starts_monotonic_s: dict[int, float] = field(default_factory=dict)
    injector_levels: dict[int, InjectorLevel] = field(default_factory=dict)
    status: str = "running"

    def start(self, event: AttemptStart) -> None:
        self.attempts[event.key] = {**asdict(event), "status": "starting", "dispatched": False}

    def finish_attempt(self, event: AttemptStart, status: str) -> None:
        self.attempts[event.key].update(status=status, finished_at=time.time())

    def snapshot(self) -> dict:
        return {"schema_version": 1, "status": self.status,
                "completed_requests": len(self.records), "sessions": len(self.sessions),
                "session_status": dict(self.session_status),
                "level_status": dict(self.level_status),
                "attempts": list(self.attempts.values())}


async def wait_or_stop(seconds: float, stop: asyncio.Event | None) -> None:
    """Retry/think wait which wakes immediately on cooperative stop."""
    if stop is None:
        await asyncio.sleep(seconds)
        return
    if stop.is_set():
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0, seconds))
    except TimeoutError:
        pass
