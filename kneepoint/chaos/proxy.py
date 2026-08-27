"""Tool-layer chaos: a minimal reverse proxy the agent's tool URL points at.

Plain ASGI (no web framework) so the shipping package stays lean; uvicorn
serves it in-process on an ephemeral port. Faults inject BELOW the agent's
resilience layer: the agent sees a hung or garbage tool exactly as production
would deliver it.
"""

import asyncio
import contextlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
import uvicorn
from pydantic import BaseModel

from kneepoint.chaos.injector import ChaosInjector
from kneepoint.collect.schemas import SessionRecord

TOOL_TIMEOUT_HOLD_S = 30.0
_MALFORMED_BODY = b"<<not json%%"


class FaultMerge(BaseModel):
    """What merging a fault log into a run's sessions actually achieved.

    Every count here is a way the resilience grid can be incomplete, and an
    incomplete grid biases the score toward 100 (METHODOLOGY §6b) — so none of
    them is allowed to stay silent.
    """

    attributed: int = 0       # fault events written onto a session of this run
    unattributed: int = 0     # served with no session header: agent didn't echo it
    unmatched: int = 0        # had a session id, but no session in this run owns it
    sessions_touched: int = 0
    by_type: dict[str, int] = {}


class FaultLog:
    """Tool faults the proxy served, and which session each belongs to.

    Attribution runs on the `x-kneepoint-session` header, which the agent must
    echo on outbound tool calls. Faults that arrive without one are counted
    rather than dropped — a fault nobody can attribute is a hole in the grid,
    not a non-event.

    `path` is what lets attribution cross a process boundary: the proxy appends
    one JSON line per fault and the run merges the file afterwards. Before this,
    `merge_into` was only reachable from `kneepoint demo`, the one command where
    proxy and ramp share a process, so every other resilience score was scored
    on LLM faults alone (Run E: 35 tool faults served, zero attributed).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.counts: dict[str, int] = {}
        self.by_session: dict[str, list[str]] = {}
        self.unattributed = 0
        self.path = Path(path) if path is not None else None
        self._write_lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, fault_type: str, session_id: str | None) -> None:
        self.counts[fault_type] = self.counts.get(fault_type, 0) + 1
        if session_id:
            self.by_session.setdefault(session_id, []).append(fault_type)
        else:
            self.unattributed += 1
        if self.path is not None:
            line = json.dumps({
                "ts": time.time(), "fault": fault_type, "session_id": session_id,
            })
            # append-and-close per fault: fault rates are low (Run E served 35 in
            # a whole run) and the proxy serves from a daemon thread, so the
            # simplest crash-safe thing beats a handle we would have to own
            with self._write_lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    @classmethod
    def load(cls, path: Path | str) -> "FaultLog":
        """Rebuild a log from the file a proxy wrote. Unreadable lines are skipped."""
        log = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                fault_type = entry["fault"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            log.record(fault_type, entry.get("session_id"))
        return log

    def merge_into(self, sessions: list[SessionRecord]) -> FaultMerge:
        """Write this log's tool faults onto the sessions that hit them."""
        by_id = {s.session_id: s for s in sessions}
        merge = FaultMerge(unattributed=self.unattributed, by_type={})
        touched: set[str] = set()
        for session_id, faults in self.by_session.items():
            session = by_id.get(session_id)
            if session is None:
                merge.unmatched += len(faults)
                continue
            session.faults.extend(faults)
            touched.add(session_id)
            merge.attributed += len(faults)
            for fault in faults:
                merge.by_type[fault] = merge.by_type.get(fault, 0) + 1
        merge.sessions_touched = len(touched)
        return merge


def merge_fault_log(path: Path | str, sessions: list[SessionRecord]) -> FaultMerge | None:
    """Merge a proxy's on-disk fault log into a run's sessions.

    Returns None when the file isn't there — the caller says so rather than
    quietly reporting a resilience score that never saw the tool faults.
    Entries from other runs simply don't match a session id and land in
    `unmatched`, so a stale log can't invent faults.
    """
    path = Path(path)
    if not path.exists():
        return None
    return FaultLog.load(path).merge_into(sessions)


def make_proxy_app(
    upstream_base: str,
    injector: ChaosInjector,
    log: FaultLog,
    stop_event: asyncio.Event | None = None,
    state: dict | None = None,
):
    upstream = upstream_base.rstrip("/")
    state = state if state is not None else {}

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            # one shared upstream client for the app's lifetime: constructing an
            # AsyncClient per request costs up to ~1s on some Windows machines
            # (SSL context build), which would push every healthy forwarded tool
            # call past the agent's timeout and fake 100% tool failures
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    state["client"] = httpx.AsyncClient(timeout=30)
                    state["loop"] = asyncio.get_running_loop()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await state["client"].aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        assert scope["type"] == "http"
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        session_id = headers.get("x-kneepoint-session")
        fault = injector.pick("tool")

        async def respond(status: int, body: bytes, content_type: bytes) -> None:
            await send({
                "type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type)],
            })
            await send({"type": "http.response.body", "body": body})

        if fault is not None:
            log.record(fault.type, session_id)
            if fault.type == "tool_timeout":
                try:
                    if stop_event is None:
                        await asyncio.sleep(TOOL_TIMEOUT_HOLD_S)
                    else:
                        # stop() sets the event so held handlers finish NOW and
                        # graceful shutdown has nothing to cancel — otherwise
                        # every `kneepoint demo` would end in uvicorn error spam
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(
                                stop_event.wait(), timeout=TOOL_TIMEOUT_HOLD_S
                            )
                except asyncio.CancelledError:
                    # fallback: shutdown cancelled us anyway; end quietly
                    with contextlib.suppress(BaseException):
                        await respond(504, b"chaos: tool timeout", b"text/plain")
                    return
                await respond(504, b"chaos: tool timeout", b"text/plain")
                return
            if fault.type == "tool_malformed_json":
                await respond(200, _MALFORMED_BODY, b"application/json")
                return

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        target = urljoin(upstream + "/", scope["path"].lstrip("/"))
        if scope["query_string"]:
            target += "?" + scope["query_string"].decode()
        upstream_resp = await state["client"].request(
            scope["method"], target, content=body,
            headers={k: v for k, v in headers.items() if k not in ("host",)},
        )
        await respond(
            upstream_resp.status_code, upstream_resp.content,
            upstream_resp.headers.get("content-type", "application/octet-stream").encode(),
        )

    return app


class ProxyHandle:
    def __init__(self, url: str, log: FaultLog, server: uvicorn.Server,
                 thread: threading.Thread, stop_event: asyncio.Event | None = None,
                 state: dict | None = None) -> None:
        self.url = url
        self.log = log
        self._server = server
        self._thread = thread
        self._stop_event = stop_event
        self._state = state or {}

    def stop(self) -> None:
        # release tool_timeout holds first so graceful shutdown finds no
        # running handlers to cancel (cancellation = uvicorn error spam)
        loop = self._state.get("loop")
        if self._stop_event is not None and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._stop_event.set)
        self._server.should_exit = True
        self._thread.join(timeout=5)


def start_proxy(
    upstream_base: str,
    injector: ChaosInjector,
    *,
    port: int = 0,
    fault_log: Path | str | None = None,
) -> ProxyHandle:
    """Serve the chaos proxy on 127.0.0.1:<port> in a daemon thread.

    `fault_log` makes every served fault durable, so a run in another process
    can attribute it afterwards (`merge_fault_log`).
    """
    log = FaultLog(fault_log)
    stop_event = asyncio.Event()
    state: dict = {}
    config = uvicorn.Config(
        make_proxy_app(upstream_base, injector, log, stop_event=stop_event, state=state),
        host="127.0.0.1", port=port, log_level="warning",
        # stop() releases held handlers via stop_event, so graceful shutdown is
        # normally instant; this is only the fallback if something still lingers
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("chaos proxy failed to start within 10s")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return ProxyHandle(f"http://127.0.0.1:{port}", log, server, thread,
                       stop_event=stop_event, state=state)
