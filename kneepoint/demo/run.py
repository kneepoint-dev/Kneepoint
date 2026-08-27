"""`kneepoint demo`: a first success with no API key and nothing to install.

Bundled agent + chaos tool proxy in one process, small ramp, full report.
In-process is the point: the proxy's FaultLog can attribute tool faults to
sessions, so the demo resilience grid shows all four fault types — the
standalone three-terminal flow cannot.
"""

import asyncio
import os
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import uvicorn

from kneepoint.analyze.cost import CostRates, summarize_cost
from kneepoint.analyze.errors import errors_by_level
from kneepoint.analyze.knee import aggregate, find_knee, knee_report
from kneepoint.analyze.resilience import resilience
from kneepoint.analyze.resolution import resolution_by_level, resolution_rate
from kneepoint.analyze.retry import retry_by_level
from kneepoint.chaos.faults import FaultSpec
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.proxy import start_proxy
from kneepoint.chaos.transport import ChaosTransport
from kneepoint.collect.schemas import (
    ChaosShape,
    PriceRates,
    RampShape,
    RetryShape,
    RunMetadata,
    TurnsShape,
)
from kneepoint.collect.writer import (
    JsonlWriter,
    current_environment,
    read_run_meta,
    write_run_meta,
)
from kneepoint.demo.agent import create_app
from kneepoint.generator.corpus import DEFAULT_PROMPTS, CorpusSampler
from kneepoint.generator.ramp import RampSpec, run_ramp
from kneepoint.generator.session import RetryPolicy, SessionSpec, TurnsSpec
from kneepoint.judge.deterministic import CheckConfig, apply_deterministic
from kneepoint.report.html import RunMeta, format_started, write_report

# Boosted vs. the standard profile: a ~60s demo must produce enough faulted
# sessions for a visible per-fault grid (DEMO_MIN_GROUP below).
DEMO_LLM_FAULTS = [
    FaultSpec(type="llm_rate_limit", probability=0.08),
    FaultSpec(type="llm_server_error", probability=0.03),
]
DEMO_TOOL_FAULTS = [
    FaultSpec(type="tool_timeout", probability=0.12),
    FaultSpec(type="tool_malformed_json", probability=0.08),
]
# Named once so what the demo *uses* and what it *records* in the metadata
# sidecar cannot drift apart.
DEMO_MIN_SAMPLES = 5
DEMO_MIN_GROUP = 5
DEMO_PRICES = CostRates(input_per_mtok=3.0, output_per_mtok=15.0)  # play money
# The shape of the demo's knee: a fixed-capacity agent against a short ramp.
# See run_demo() for why these values and not others.
DEMO_CAPACITY = 2
DEMO_AGENT = dict(base_delay_ms=150, token_delay_ms=8, output_tokens=36)
DEMO_TOOL_TIMEOUT_MS = 300
DEMO_RAMP = RampShape(start=1, stop=13, step=3)


class _AgentHandle:
    def __init__(self, url: str, server: uvicorn.Server, thread: threading.Thread) -> None:
        self.url = url
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _start_agent(asgi_app) -> _AgentHandle:
    """Bundled agent on 127.0.0.1:<free port> in a daemon thread (proxy pattern)."""
    config = uvicorn.Config(
        asgi_app, host="127.0.0.1", port=0, log_level="warning",
        # 0 would make uvicorn log "Cancel 0 running task(s)" on every stop —
        # a short fallback keeps shutdown fast without the error line
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("demo agent failed to start within 10s")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return _AgentHandle(f"http://127.0.0.1:{port}", server, thread)


def run_demo(
    out_dir: Path,
    *,
    hold_seconds: float = 5.0,
    seed: int = 0,
    open_browser: bool = True,
    echo=print,
) -> Path:
    # Capacity 2 against a 1..13 ramp so the queue actually forms mid-ramp. At the
    # old capacity of 8 this curve was flat to the top (p95 1173ms at c=1, 1182ms
    # at c=13) and the only thing that ever "found" a knee in it was Kneedle
    # degenerating on a near-linear curve — the exact artifact that made Kneedle
    # stop taking the headline. The demo has to demonstrate a real knee, not the
    # bug we just deleted.
    #
    # The floor matters as much as the capacity. p95 at c=1 is set by the tool-
    # timeout fault (12% of tool calls hang, so p95 always catches one), and the
    # knee is "p95 reaches 2x the floor". With an 800ms agent-side timeout the
    # floor was ~1.2s and the queue had to add that much before the detector
    # fired — which it did at c=7 with a ratio of 2.0x, inside the 15% noise
    # band, so the headline was "7-10, low confidence" at the default hold and
    # "13 or beyond" at a 1s one. A 300ms timeout and a slightly longer answer
    # (36 tokens) make the queue dominate the floor instead: measured over six
    # seeds at the default hold, c=4 stays at 1.3-1.6x and c=7 lands at
    # 2.4-3.2x, so the knee reads "7, high confidence" with levels on both
    # sides of it. Widening the ramp or halving the capacity did not do this -
    # both still put the deciding ratio within a few percent of 2.0.
    agent = _start_agent(create_app(capacity=DEMO_CAPACITY, **DEMO_AGENT))
    proxy = start_proxy(agent.url, ChaosInjector(DEMO_TOOL_FAULTS, seed=seed + 1))
    saved_env = {k: os.environ.get(k) for k in ("MOCK_TOOL_URL", "MOCK_TOOL_TIMEOUT_MS")}
    os.environ["MOCK_TOOL_URL"] = f"{proxy.url}/tool/search"
    os.environ["MOCK_TOOL_TIMEOUT_MS"] = str(DEMO_TOOL_TIMEOUT_MS)
    try:
        spec = RampSpec(start=DEMO_RAMP.start, stop=DEMO_RAMP.stop, step=DEMO_RAMP.step,
                        hold_seconds=hold_seconds)
        sampler = CorpusSampler(DEFAULT_PROMPTS, seed=seed)
        session_spec = SessionSpec(
            turns=TurnsSpec(min=1, max=2),
            retry=RetryPolicy(max_attempts=3, backoff_s=0.2),
        )
        transport = ChaosTransport(
            httpx.AsyncHTTPTransport(), ChaosInjector(DEMO_LLM_FAULTS, seed=seed)
        )
        echo("kneepoint demo: bundled support agent + chaos (429s, 503s, tool "
             "timeouts, garbage JSON) - no API keys, play-money prices.")
        echo(f"Ramping {spec.start}..{spec.stop} step {spec.step}, "
             f"holding {spec.hold_seconds}s per level")

        def on_level(level: int, sessions: list, records: list) -> None:
            errors = sum(1 for r in records if not r.ok)
            echo(f"  level {level:>2}: {len(sessions)} sessions, "
                 f"{len(records)} requests, {errors} errors")

        started_at = time.time()
        all_sessions, records, _ = asyncio.run(
            run_ramp(
                f"{agent.url}/v1", "demo", spec, sampler, session_spec,
                seed=seed, transport=transport, on_level=on_level,
            )
        )
        # in-process, so the log object is right here; `kneepoint run` reaches the
        # same merge through the proxy's on-disk fault log instead
        merge = proxy.log.merge_into(all_sessions)
        if merge.unattributed:
            echo(f"Note: {merge.unattributed} tool fault(s) had no session header "
                 "and could not be attributed.")
        apply_deterministic(all_sessions, CheckConfig(kind="contains", value="[RESOLVED"))
        rate = resolution_rate(all_sessions)
        cost = summarize_cost(records, all_sessions, DEMO_PRICES)
        res = resilience(all_sessions, min_group=DEMO_MIN_GROUP)
        stats = aggregate(records)
        knee_summary = knee_report(stats, min_samples=DEMO_MIN_SAMPLES)
        knee = find_knee(stats, min_samples=DEMO_MIN_SAMPLES)
        if any(s.contaminated for s in stats):
            echo("Note: some requests were abandoned at the client wall - the levels "
                 "after them may be measuring leftover work (see the report banner).")

        run_id = time.strftime("%Y%m%d-%H%M%S")
        jsonl_path = out_dir / f"demo-{run_id}.jsonl"
        sessions_path = out_dir / f"demo-{run_id}-sessions.jsonl"
        meta_path = out_dir / f"demo-{run_id}-meta.json"
        with JsonlWriter(jsonl_path) as w:
            w.write_many(records)
        with JsonlWriter(sessions_path) as w:
            w.write_many(all_sessions)
        write_run_meta(meta_path, RunMetadata(
            run_id=run_id, command="demo", target=f"{agent.url}/v1", model="demo",
            ramp=RampShape(start=spec.start, stop=spec.stop, step=spec.step),
            hold_seconds=spec.hold_seconds,
            turns=TurnsShape(min=session_spec.turns.min, max=session_spec.turns.max),
            retry=RetryShape(max_attempts=session_spec.retry.max_attempts,
                             backoff_s=session_spec.retry.backoff_s),
            seed=seed,
            chaos=ChaosShape(profile="demo", faults=DEMO_LLM_FAULTS + DEMO_TOOL_FAULTS),
            price=PriceRates(input_per_mtok=DEMO_PRICES.input_per_mtok,
                             output_per_mtok=DEMO_PRICES.output_per_mtok),
            min_samples=DEMO_MIN_SAMPLES, min_group=DEMO_MIN_GROUP,
            started_at=started_at, finished_at=time.time(),
            environment=current_environment(),
        ))
        run_meta = read_run_meta(meta_path)   # the stamped file, same as `report` reads
        report = write_report(
            out_dir / f"demo-{run_id}-report.html",
            meta=RunMeta(
                target=f"{agent.url}/v1", model="demo",
                started=format_started(started_at),
                ramp=f"{spec.start}..{spec.stop} step {spec.step}", chaos="demo",
                total_requests=len(records), total_sessions=len(all_sessions),
            ),
            stats=stats, knee=knee, knee_summary=knee_summary,
            quality=resolution_by_level(all_sessions),
            cost=cost, res=res,
            retry_levels=retry_by_level(records, all_sessions), min_samples=DEMO_MIN_SAMPLES,
            error_levels=errors_by_level(records), run_meta=run_meta,
        )

        for line in knee_summary.lines():
            echo(line)
        if knee is None:
            echo("  (a longer demo helps - try --hold-seconds 10)")
        if rate is not None:
            echo(f"Resolution rate: {rate:.1%} ({len(all_sessions)} sessions)")
        if cost.cost_per_resolved is not None:
            echo(f"$/resolved task: ${cost.cost_per_resolved:.4f} (mock tokens, demo prices)")
        else:
            echo("$/resolved task: n/a (no resolved sessions)")
        if res.score is not None:
            echo(f"Resilience score: {res.score:.0f} "
                 f"({res.faulted_sessions} faulted vs {res.clean_sessions} clean sessions)")
        else:
            echo("Resilience score: n/a in this short run - try --hold-seconds 10.")
        echo(f"Raw records: {jsonl_path}")
        echo(f"Sessions:    {sessions_path}")
        echo(f"Metadata:    {meta_path}")
        echo(f"Report:      {report}")
        if open_browser:
            webbrowser.open(report.resolve().as_uri())
        return report
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        proxy.stop()
        agent.stop()
