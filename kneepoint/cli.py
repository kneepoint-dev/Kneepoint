import asyncio
import os
import time
from importlib.metadata import version as pkg_version
from pathlib import Path

import httpx
import typer

from kneepoint.analyze.budget import CostMeter, estimate_run_cost
from kneepoint.analyze.cost import CostRates, summarize_cost
from kneepoint.analyze.errors import errors_by_level
from kneepoint.analyze.knee import aggregate, find_knee, knee_report
from kneepoint.analyze.resilience import resilience
from kneepoint.analyze.resolution import resolution_by_level, resolution_rate
from kneepoint.analyze.retry import retry_by_level, worst_amplification
from kneepoint.chaos.faults import STANDARD_PROFILE, TOOL_FAULTS
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.proxy import merge_fault_log, start_proxy
from kneepoint.chaos.transport import ChaosTransport
from kneepoint.collect.schemas import (
    ChaosShape,
    PriceRates,
    RampShape,
    RequestRecord,
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
from kneepoint.config import STARTER_SCENARIO, ScenarioError, evaluate_slo, load_scenario
from kneepoint.generator.corpus import DEFAULT_PROMPTS, CorpusSampler
from kneepoint.generator.ramp import RampSpec, parse_ramp, run_ramp
from kneepoint.generator.session import SessionSpec
from kneepoint.judge.deterministic import CheckConfig, apply_deterministic
from kneepoint.judge.llm_judge import judge_sessions
from kneepoint.report.html import RunMeta, format_started, write_report
from kneepoint.report.rerender import ReportInputError, render_from_files
from kneepoint.targets.openai_compatible import REQUEST_WALL_S, run_turn

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Find where your AI agent breaks: load, cost, and chaos testing for AI agents.",
)

OUT_DIR_OPTION = typer.Option(Path("reports"), "--out", help="Output directory.")
INIT_OUT_OPTION = typer.Option(Path("."), "--out", help="Directory to initialize.")
VALIDATE_PATH_ARGUMENT = typer.Argument(..., help="Scenario YAML to check.")
SCENARIO_OPTION = typer.Option(None, "--scenario", help="Scenario YAML file.")
DEMO_HOLD_OPTION = typer.Option(5.0, "--hold-seconds", help="Hold time per demo ramp level.")
DEMO_SEED_OPTION = typer.Option(0, "--seed", help="Seed for the demo's sampling and chaos.")
DEMO_NO_OPEN_OPTION = typer.Option(False, "--no-open", help="Don't open the report in a browser.")
RUN_FAULT_LOG_OPTION = typer.Option(
    None, "--fault-log",
    help="Fault log written by `kneepoint proxy`; merged so tool faults reach the "
         "resilience grid.",
)
# Smallest faulted/clean group the resilience grid will score. Named so the value
# the run *uses* and the value it *records* in the metadata sidecar are one thing.
RUN_MIN_GROUP = 10
PROXY_FAULT_LOG_OPTION = typer.Option(
    Path("faults.jsonl"), "--fault-log",
    help="Where to append served faults; pass the same path to `kneepoint run`.",
)


async def _calibrate(target: str, model: str, sampler, transport, headers) -> RequestRecord:
    async with httpx.AsyncClient(timeout=120, transport=transport, headers=headers) as client:
        outcome = await run_turn(
            client, target, model,
            [{"role": "user", "content": sampler.sample()}],
            concurrency=1, session_id="calibration",
        )
    return outcome.record


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kneepoint {pkg_version('kneepoint')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False, "--version", help="Show version and exit.", callback=_version_callback, is_eager=True
    ),
) -> None:
    """Kneepoint CLI."""


@app.command()
def init(out: Path = INIT_OUT_OPTION) -> None:
    """Write a starter kneepoint.yaml and example prompt corpus."""
    scenario_path = out / "kneepoint.yaml"
    if scenario_path.exists():
        typer.echo(f"{scenario_path} already exists - not overwriting.")
        raise typer.Exit(code=1)
    corpus_dir = out / "prompts" / "support"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for i, prompt in enumerate(DEFAULT_PROMPTS, start=1):
        (corpus_dir / f"q{i}.txt").write_text(prompt + "\n", encoding="utf-8")
    scenario_path.write_text(STARTER_SCENARIO, encoding="utf-8")
    typer.echo(f"Wrote {scenario_path} and {len(DEFAULT_PROMPTS)} prompts to {corpus_dir}")
    typer.echo("Next: kneepoint run --scenario kneepoint.yaml")


@app.command()
def demo(
    out_dir: Path = OUT_DIR_OPTION,
    hold_seconds: float = DEMO_HOLD_OPTION,
    seed: int = DEMO_SEED_OPTION,
    no_open: bool = DEMO_NO_OPEN_OPTION,
) -> None:
    """Zero-API-key demo: bundled agent + chaos + the full three-pillar report."""
    from kneepoint.demo.run import run_demo

    run_demo(out_dir, hold_seconds=hold_seconds, seed=seed,
             open_browser=not no_open, echo=typer.echo)


@app.command()
def proxy(
    upstream: str = typer.Option(..., "--upstream", help="Base URL of the real tool service."),
    fault_log: Path = PROXY_FAULT_LOG_OPTION,
    port: int = typer.Option(0, "--port", help="Port to bind (0 = pick a free one)."),
    seed: int = typer.Option(0, "--seed", help="Seed for the fault rolls."),
    scenario_path: Path = SCENARIO_OPTION,
) -> None:
    """Serve the tool-fault chaos proxy until Ctrl-C, logging faults for a later run.

    Point your agent's tool URL at the printed address. The log is what carries
    tool faults into `kneepoint run`'s resilience grid across the process boundary.
    """
    faults = STANDARD_PROFILE
    if scenario_path is not None:
        try:
            faults = load_scenario(scenario_path).chaos.resolved_faults()
        except ScenarioError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    tool_faults = [f for f in faults if f.type in TOOL_FAULTS]
    if not tool_faults:
        typer.echo("This profile has no tool faults - the proxy would forward everything.")
        raise typer.Exit(code=1)
    handle = start_proxy(upstream, ChaosInjector(faults, seed=seed),
                         port=port, fault_log=fault_log)
    typer.echo(f"Chaos proxy: {handle.url}  ->  {upstream}")
    typer.echo(f"Faults: {', '.join(f'{f.type} p={f.probability}' for f in tool_faults)}")
    typer.echo(f"Point the agent's tool URL at {handle.url}/<your tool path> and have it "
               "echo the x-kneepoint-session header.")
    typer.echo(f"Then: kneepoint run --scenario ... --fault-log {fault_log}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        typer.echo(f"\nFaults served: {handle.log.counts or 'none'} "
                   f"({handle.log.unattributed} without a session header)")
    finally:
        handle.stop()


@app.command()
def validate(path: Path = VALIDATE_PATH_ARGUMENT) -> None:
    """Validate a scenario file without running anything."""
    try:
        load_scenario(path)
    except ScenarioError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"{path} is valid.")


REPORT_RECORDS_ARGUMENT = typer.Argument(
    ..., help="The run's records file, run-<id>.jsonl; the sessions file and the "
              "metadata sidecar are found beside it by name.",
)
REPORT_OUT_OPTION = typer.Option(
    None, "--out", help="Where to write the HTML (default: <prefix>-<id>-report.html "
                        "beside the records, overwriting the run's own).",
)
REPORT_PRICE_IN_OPTION = typer.Option(
    None, "--price-in", help="Override the stored input price, USD per million tokens.",
)
REPORT_PRICE_OUT_OPTION = typer.Option(
    None, "--price-out", help="Override the stored output price, USD per million tokens.",
)
REPORT_MIN_SAMPLES_OPTION = typer.Option(
    None, "--min-samples", help="Override the stored min requests per level for knee math.",
)


@app.command()
def report(
    records: Path = REPORT_RECORDS_ARGUMENT,
    out: Path = REPORT_OUT_OPTION,
    price_in: float = REPORT_PRICE_IN_OPTION,
    price_out: float = REPORT_PRICE_OUT_OPTION,
    min_samples: int = REPORT_MIN_SAMPLES_OPTION,
) -> None:
    """Re-render the HTML report from a run's stored records.

    Reads run-<id>.jsonl, run-<id>-sessions.jsonl and run-<id>-meta.json and
    re-aggregates with the same functions the run used. Anything the sidecar
    does not record (files written before it existed) is shown as unknown,
    never defaulted; the overrides above are the only way to supply it.
    """
    try:
        result = render_from_files(
            records, out=out, price_in=price_in, price_out=price_out, min_samples=min_samples,
        )
    except ReportInputError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    for line in result.knee_summary.lines():
        typer.echo(line)
    for reason in result.unknown:
        typer.echo(f"Unknown: {reason}")
    typer.echo(f"Report:      {result.report_path}")


def _warn_contamination(stats, echo=typer.echo) -> None:
    """Say it out loud when abandoned work makes the later levels non-independent."""
    hit = [s for s in stats if s.contaminated]
    if not hit:
        return
    abandoned = sum(s.abandoned for s in stats)
    levels = ", ".join(str(s.concurrency) for s in hit)
    echo(
        f"Contaminated levels: {levels} - {abandoned} request(s) abandoned at the "
        f"{REQUEST_WALL_S:g}s wall. Kneepoint closes the connection immediately, but the "
        "server may keep generating, so these levels can be measuring the previous "
        "level's leftovers. Do not publish latencies or a knee from them."
    )


def _merge_tool_faults(path: Path, sessions: list, echo=typer.echo) -> None:
    """Pull the proxy's tool faults into this run's sessions, and say what got lost."""
    merge = merge_fault_log(path, sessions)
    if merge is None:
        echo(f"Fault log {path} not found - tool faults are missing from the "
             "resilience grid, which biases the score toward 100.")
        return
    grid = ", ".join(f"{fault} x{n}" for fault, n in sorted(merge.by_type.items()))
    echo(f"Tool faults merged: {merge.attributed} across {merge.sessions_touched} "
         f"sessions ({grid or 'none'})")
    if merge.unattributed:
        echo(f"  {merge.unattributed} tool fault(s) carried no session header - your "
             "agent is not echoing x-kneepoint-session on tool calls, so they cannot "
             "be attributed and the grid is incomplete.")
    if merge.unmatched:
        echo(f"  {merge.unmatched} fault(s) belong to sessions outside this run - "
             "the log is shared with another run; use a fresh --fault-log per run.")


def _pick(flag_value, scenario_value, default):
    """Precedence: CLI flag explicitly set > scenario value > built-in default."""
    if flag_value is not None:
        return flag_value
    return scenario_value if scenario_value is not None else default


@app.command()
def run(
    scenario_path: Path = SCENARIO_OPTION,
    target: str = typer.Option(
        None, "--target", help="Base URL of an OpenAI-compatible endpoint, e.g. http://127.0.0.1:8000/v1"
    ),
    model: str = typer.Option(None, "--model", help="Model name sent to the target."),
    ramp: str = typer.Option(None, "--ramp", help="Concurrency ramp 'start..stop'."),
    step: int = typer.Option(None, "--step", help="Concurrency increment between levels."),
    hold_seconds: float = typer.Option(None, "--hold-seconds", help="Hold time per level."),
    turns: str = typer.Option(None, "--turns", help="Turns per session, 'min..max'."),
    corpus: str = typer.Option(
        None, "--corpus", help="Glob of *.txt prompt files (one prompt per file)."
    ),
    seed: int = typer.Option(None, "--seed", help="Sampling seed for reproducible runs."),
    check_contains: str = typer.Option(
        None, "--check-contains",
        help="Deterministic resolution check: substring of the final answer. '' disables.",
    ),
    check_regex: str = typer.Option(
        None, "--check-regex", help="Deterministic resolution check: regex (overrides contains)."
    ),
    chaos: str = typer.Option(
        None, "--chaos", help="Chaos profile: off | standard (llm 429/503 + tool faults)."
    ),
    price_in: float = typer.Option(
        None, "--price-in", help="Input price, USD per million tokens (0 = no cost tracking)."
    ),
    price_out: float = typer.Option(
        None, "--price-out", help="Output price, USD per million tokens."
    ),
    max_spend: float = typer.Option(
        None, "--max-spend", help="Hard budget cap in USD; the run stops when crossed."
    ),
    force: bool = typer.Option(False, "--force", help="Run even if the estimate exceeds the cap."),
    min_samples: int = typer.Option(
        10, "--min-samples", help="Min requests per level for knee math."
    ),
    fault_log: Path = RUN_FAULT_LOG_OPTION,
    out_dir: Path = OUT_DIR_OPTION,
) -> None:
    """Ramp concurrency against the target and find the knee point."""
    sc = None
    if scenario_path is not None:
        try:
            sc = load_scenario(scenario_path)
        except ScenarioError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc

    target_url = _pick(target, sc.target.url if sc else None, None)
    if target_url is None:
        raise typer.BadParameter("provide --target or --scenario")
    model_name = _pick(model, sc.target.model if sc else None, "mock")
    sc_ramp = sc.workload.ramp if sc else None
    sc_conv = sc.workload.conversation if sc else None
    seed = _pick(seed, sc.seed if sc else None, 0)
    chaos = _pick(chaos, sc.chaos.profile if sc else None, "off")
    # CLI --corpus stays CWD-relative (explicit flag, explicit location);
    # scenario-sourced globs anchor to the scenario file so scenario
    # directories are copy-anywhere portable
    if corpus is None and sc_conv and sc_conv.corpus:
        corpus_path = Path(sc_conv.corpus)
        corpus = str(
            corpus_path if corpus_path.is_absolute()
            else scenario_path.parent / corpus_path
        )
    price_in = _pick(price_in, sc.cost.input_per_mtok if sc and sc.cost else None, 0.0)
    price_out = _pick(price_out, sc.cost.output_per_mtok if sc and sc.cost else None, 0.0)
    max_spend = _pick(max_spend, sc.cost.max_spend if sc and sc.cost else None, None)

    try:
        start, stop = parse_ramp(ramp) if ramp else (
            (sc_ramp.from_, sc_ramp.to) if sc_ramp else (1, 50)
        )
        session_spec = SessionSpec()
        if sc_conv:
            session_spec = SessionSpec(
                turns=sc_conv.turns, think_time=sc_conv.think_time_ms, retry=sc_conv.retry
            )
        if turns:
            turns_min, turns_max = parse_ramp(turns)  # reuse: same 'a..b' syntax
            session_spec.turns.min, session_spec.turns.max = turns_min, turns_max
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    spec = RampSpec(
        start=start, stop=stop,
        step=_pick(step, sc_ramp.step if sc_ramp else None, 5),
        hold_seconds=_pick(hold_seconds, sc_ramp.hold_seconds if sc_ramp else None, 15.0),
    )

    sampler = (
        CorpusSampler.from_glob(corpus, seed=seed) if corpus
        else CorpusSampler(DEFAULT_PROMPTS, seed=seed)
    )

    headers = None
    if sc and sc.target.auth_env:
        token = os.getenv(sc.target.auth_env)
        if not token:
            typer.echo(f"auth_env {sc.target.auth_env} is set in the scenario "
                       "but the environment variable is empty.")
            raise typer.Exit(code=1)
        headers = {"Authorization": f"Bearer {token}"}

    transport = None
    faults = []
    if chaos != "off":
        if chaos != "standard" and not (sc and sc.chaos.profile == chaos):
            raise typer.BadParameter("--chaos must be 'off' or 'standard'")
        faults = (
            sc.chaos.resolved_faults() if sc and sc.chaos.profile == chaos
            else STANDARD_PROFILE
        )
        transport = ChaosTransport(httpx.AsyncHTTPTransport(), ChaosInjector(faults, seed=seed))
        typer.echo(f"Chaos: {chaos} profile ({', '.join(f.type for f in faults)})")
        if any(f.type in TOOL_FAULTS for f in faults) and fault_log is None:
            # otherwise the line above advertises four faults while two of them
            # can never fire, and the resilience score is scored on half a grid
            typer.echo(
                "  Tool faults need the proxy: run `kneepoint proxy --upstream <agent> "
                "--fault-log faults.jsonl`, point the agent's tool URL at it, and pass "
                "--fault-log here. Without it only the LLM faults reach the grid."
            )
    run_id = time.strftime("%Y%m%d-%H%M%S")
    jsonl_path = out_dir / f"run-{run_id}.jsonl"
    sessions_path = out_dir / f"run-{run_id}-sessions.jsonl"
    meta_path = out_dir / f"run-{run_id}-meta.json"

    on_record = None
    meter = None
    if price_in or price_out:
        rates = CostRates(input_per_mtok=price_in, output_per_mtok=price_out)
        calibration = asyncio.run(_calibrate(target_url, model_name, sampler, transport, headers))
        estimate = estimate_run_cost(spec.levels(), spec.hold_seconds, calibration, rates)
        if estimate is not None:
            typer.echo(f"Estimated spend for this run: ~${estimate:.2f}")
            if max_spend is not None and estimate > max_spend and not force:
                typer.echo(f"Estimate exceeds --max-spend {max_spend:.2f}; "
                           "re-run with --force to proceed anyway.")
                raise typer.Exit(code=3)
        meter = CostMeter(rates, max_spend)
        on_record = meter.add

    typer.echo(
        f"Ramping {spec.start}..{spec.stop} step {spec.step}, "
        f"holding {spec.hold_seconds}s per level"
    )
    started_at = time.time()  # the calibration request above is not part of the run
    with JsonlWriter(jsonl_path) as writer:

        def on_level(level: int, sessions: list, records: list) -> None:
            writer.write_many(records)
            errors = sum(1 for r in records if not r.ok)
            typer.echo(f"  level {level:>3}: {len(sessions)} sessions, "
                       f"{len(records)} requests, {errors} errors")

        all_sessions, records, budget_exceeded = asyncio.run(
            run_ramp(
                target_url, model_name, spec, sampler, session_spec,
                seed=seed, transport=transport, headers=headers,
                on_record=on_record, on_level=on_level,
            )
        )
    if budget_exceeded:
        typer.echo(f"Budget cap hit at ${meter.spend:.4f} - run stopped early; "
                   "results below are partial.")

    if fault_log is not None:
        _merge_tool_faults(fault_log, all_sessions)

    check = None
    method = "deterministic"
    if check_regex:
        check = CheckConfig(kind="regex", value=check_regex)
    elif check_contains is not None:
        check = CheckConfig(kind="contains", value=check_contains) if check_contains else None
    elif sc and sc.resolution.check:
        check = sc.resolution.check
    elif sc is None:
        check = CheckConfig(kind="contains", value="[RESOLVED")
    if check is not None:
        apply_deterministic(all_sessions, check)
    if sc and sc.resolution.judge:
        asyncio.run(judge_sessions(all_sessions, sc.resolution.judge, seed=seed))
        if check is None:
            method = "llm_judge"
    rate = resolution_rate(all_sessions)
    if rate is not None:
        typer.echo(f"Resolution rate: {rate:.1%} ({len(all_sessions)} sessions, {method})")

    cost = None
    if price_in or price_out:
        rates = CostRates(input_per_mtok=price_in, output_per_mtok=price_out)
        cost = summarize_cost(records, all_sessions, rates)
        typer.echo(f"Total spend:     ${cost.total_spend:.4f}")
        typer.echo(f"Retry waste:     {cost.retry_waste_pct:.1%} (${cost.waste_spend:.4f})")
        if cost.cost_per_resolved is not None:
            typer.echo(f"$/resolved task: ${cost.cost_per_resolved:.4f}")
        else:
            typer.echo("$/resolved task: n/a (no resolved sessions)")

    if chaos != "off":
        res = resilience(all_sessions, min_group=RUN_MIN_GROUP)
        if res.score is not None:
            typer.echo(
                f"Resilience score: {res.score:.0f} "
                f"({res.faulted_sessions} faulted vs {res.clean_sessions} clean sessions)"
            )
        else:
            typer.echo("Resilience score: n/a (not enough faulted or clean sessions - "
                       "run longer holds or raise fault probabilities)")

    # session summaries are small: written once, post-judging, so verdicts land in the file
    with JsonlWriter(sessions_path) as session_writer:
        session_writer.write_many(all_sessions)
    # the sidecar goes down before the report is rendered, so a run whose report
    # failed to write is still re-renderable from what is on disk
    write_run_meta(meta_path, RunMetadata(
        run_id=run_id, command="run", target=target_url, model=model_name,
        ramp=RampShape(start=spec.start, stop=spec.stop, step=spec.step),
        hold_seconds=spec.hold_seconds,
        turns=TurnsShape(min=session_spec.turns.min, max=session_spec.turns.max),
        retry=RetryShape(max_attempts=session_spec.retry.max_attempts,
                         backoff_s=session_spec.retry.backoff_s),
        seed=seed, chaos=ChaosShape(profile=chaos, faults=faults),
        price=(
            PriceRates(input_per_mtok=price_in, output_per_mtok=price_out, max_spend=max_spend)
            if price_in or price_out else None
        ),
        min_samples=min_samples, min_group=RUN_MIN_GROUP,
        started_at=started_at, finished_at=time.time(),
        environment=current_environment(),
    ))
    # read back, not reused: the file carries the version stamps the in-memory
    # model does not, and the report's metadata block must match what
    # `kneepoint report` will later read from the same file
    run_meta = read_run_meta(meta_path)

    stats = aggregate(records)
    _warn_contamination(stats)
    retry_levels = retry_by_level(records, all_sessions)
    worst = worst_amplification(retry_levels)
    if worst is not None:
        typer.echo(
            f"Retry amplification: {worst.amplification:.2f}x at concurrency "
            f"{worst.concurrency} ({worst.requests} requests for {worst.first_attempts} "
            "turns) - the generator adds load where the target is struggling; "
            "disclose it when publishing this curve."
        )
    knee_summary = knee_report(stats, min_samples=min_samples)
    knee = find_knee(stats, min_samples=min_samples)
    for line in knee_summary.lines():
        typer.echo(line)
    report_path = write_report(
        out_dir / f"run-{run_id}-report.html",
        meta=RunMeta(
            target=target_url, model=model_name,
            started=format_started(started_at),  # the run's start, not this line's clock
            ramp=f"{spec.start}..{spec.stop} step {spec.step}", chaos=chaos,
            total_requests=len(records), total_sessions=len(all_sessions),
        ),
        stats=stats, knee=knee, knee_summary=knee_summary,
        quality=resolution_by_level(all_sessions),
        cost=cost,
        res=resilience(all_sessions, min_group=RUN_MIN_GROUP) if chaos != "off" else None,
        retry_levels=retry_levels, min_samples=min_samples,
        error_levels=errors_by_level(records), run_meta=run_meta,
    )
    typer.echo(f"Raw records: {jsonl_path}")
    typer.echo(f"Sessions:    {sessions_path}")
    typer.echo(f"Metadata:    {meta_path}")
    typer.echo(f"Report:      {report_path}")
    if budget_exceeded:
        raise typer.Exit(code=3)
    if sc is not None:
        overall_p95 = max((s.p95_ms for s in stats), default=None)
        breaches = evaluate_slo(
            sc.slo, p95_ms=overall_p95, resolution=rate,
            cost_per_resolved=cost.cost_per_resolved if cost else None,
        )
        for breach in breaches:
            typer.echo(f"SLO breach - {breach}")
        if breaches:
            raise typer.Exit(code=1)
