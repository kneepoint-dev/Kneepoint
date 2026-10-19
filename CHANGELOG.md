# Changelog

All notable changes to kneepoint are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: [SemVer](https://semver.org/) (pre-1.0: minor = features, patch = fixes).

## [0.3.0rc4] - UNRELEASED

**Local candidate; not a published release.**

- Judge the closed-loop law and throughput bound only with at least two steady
  completions per active session. Estimate count resolution from five shifted
  steady windows to avoid false stall readings in synchronized workloads.
- Separate event-loop CPU from whole-process CPU. The injector flag now uses
  loop CPU; reports label both values.
- Add Documentation, Changelog and Issues links to the package metadata.

## [0.3.0rc3] - UNRELEASED

**Local candidate; not a published release.**

- Allow two completion counts of resolution before flagging a throughput bound
  violation, so finite-window counting does not falsely flag healthy levels.
- Measure each load level's event-loop lag, process CPU share of one core and
  request rate. Save the additive evidence in run metadata and show it in CLI
  and HTML. Flag `injector-limited` when lag p95 exceeds the larger of 5 ms or
  2% of median response time, or process CPU exceeds 70% of one core.

### Known limits

- One Python event loop generates a run. Fast, synchronized targets can make
  Kneepoint's own request processing inflate measured latency; the new flag
  discloses measured lag or CPU pressure but is not a full correction for
  synchronized completion bursts. See measurement integrity guidance.

## [0.3.0rc2] - UNRELEASED

**Integration candidate; not a published release.**

- Propagate W3C trace context and per-attempt trace/span IDs without changing
  schema 1; tracing can be disabled.
- Report near-threshold p95 knees as inconclusive. Thin levels make latency SLOs
  unknown unless an eligible level establishes a breach, which remains a fail.
- Use monotonic clocks for run windows and durations, with labeled legacy wall
  fallback and clock/continuity checks. Show steady completion count k, law gap,
  counting resolution and the resolution-aware stall threshold; flag throughput
  above the lowest-level response bound as an estimator or clock problem.
- Derive report timing consistently from records and run metadata for CLI and
  external callers, including Plus, with byte-identical regeneration.
- Add the LanternDesk LangGraph example with trace propagation, queue/service
  step attributes and four planted bottlenecks. Fetch throttling is 25 starts/s
  so saturation occurs inside the ramp.

## [0.3.0rc1] - UNRELEASED

**Local release candidate for review; not a published release.** This proposed
minor release adds lifecycle and SLO APIs and changes how incomplete measurement
evidence is represented. Existing request and session file schemas remain readable.

### Added
- Optional generator callbacks `on_attempt_start` and `on_attempt_complete`, a
  cooperative `stop` event, and caller-owned `RunEvidence` retain completed
  attempts and partial session evidence through failure or cancellation. Observer
  callbacks are synchronous and must not block; exceptions propagate after
  completed evidence is retained. Active calls drain on cooperative stop.
- An atomic `run-<id>-lifecycle.json` checkpoint records load-generation status,
  attempt/session/level evidence, effective prices, and observed-spend accounting.
  It does not certify that judging or report rendering completed.
- `ObservedSpendController` accounts for completed generator attempts and exposes
  known spend, nullable total spend, usage coverage, and known overshoot.
- Explicit `SloConfig`, `SloResult`, and `evaluate_slo_results` APIs in
  `kneepoint.slo` distinguish `pass`, `fail`, and `unknown`. CLI and HTML reports
  use the same run evaluation; declared thresholds persist in metadata for report
  regeneration. Existing imports of `SloConfig` and `evaluate_slo` from
  `kneepoint.config` remain available.

### Changed
- Request collections and CLI request JSONL follow attempt-completion order across
  concurrent sessions. Consumers needing per-session order must use session ID,
  turn index, and attempt number. Legacy `on_record` remains a session-completion
  callback; attach accounting to only one callback path.
- Capped CLI runs require both prices explicitly, skip calibration, and reject
  configured LLM judging before load. Admission stops at observed spend equal to
  or above the cap, or when usage becomes unknown; active calls drain and the CLI
  exits with code 3. `--force` cannot bypass the stop. This is an observed-spend
  threshold, not a billing ceiling: active calls can overshoot, and calibration,
  judging, and provider-side work after disconnect are outside its accounting.
- `LevelStats.n` remains total attempts; `successful_n` supplies the successful
  sample count used for latency eligibility. All-failed levels remain visible
  with null percentiles. Legacy constructors infer successful counts only for
  entirely successful or entirely failed levels; mixed-error counts remain
  unknown. Knee detection refuses to bridge an ineligible interior level.
- Resolution summaries expose transport outcomes separately from task verdicts,
  with evaluated/unjudged counts, coverage, verdict methods, and confirmed
  resolved share. Local `execution_failure` verdicts are distinct from external
  judgments. Sampled judging remains partial coverage; a whole-run resolution SLO
  is unknown unless every recorded session has a verdict.
- SLO thresholds must be finite and within their valid ranges. Equality passes;
  missing or invalid evidence is unknown and cannot pass a configured CLI gate.
  Latency checks use the worst successful-request per-level p95, and a missing
  level prevents a pass unless another observed level already proves a breach.
  Checks describe observed records, not proof the configured ramp completed.
  Old metadata without thresholds reports them as unknown; an explicit empty
  threshold object means no SLO was configured.

### Fixed
- Missing usage no longer produces a falsely complete cost total: original and
  regenerated reports show unknown total spend and the known subtotal instead.
- Verified zero-cap runs write empty request/session files without sending
  requests and can regenerate their reports when matching metadata and lifecycle
  evidence establish the zero-request run. Arbitrary empty request files remain
  invalid.

### Migration notes
- Handle nullable latency percentiles and unknown successful sample counts;
  do not interpret missing evidence as zero or derive counts from rounded rates.
- `resolution_by_level` now retains entirely unjudged levels with null
  `resolved_rate`; consumers must handle these entries without inventing a rate.
- `judge_sessions` applies local `execution_failure` verdicts to terminal failed
  sessions. Python callers must omit interrupted or in-progress sessions from
  judging so incomplete execution remains unjudged.
- Inspect cooperative controller `stop_reason` (`cap_reached` or `unknown_usage`)
  rather than the legacy ramp `budget_exceeded` flag. Legacy `CostMeter` and
  `BudgetExceeded` remain available to Python callers.
- Use tri-state SLO results when distinguishing unmet thresholds from insufficient
  evidence. Cost per confirmed resolution covers completed generator attempts and
  confirmed outcomes only, without extrapolating sampled verdicts.

## [0.2.0] - 2026-08-29

**This is a correctness release.** Three defects in 0.1.0 affected the numbers it printed. If you have a 0.1.0 result you are relying on, re-run it with 0.2.0 rather than re-reading it — the defects are in how the numbers were measured and chosen, not in how they were displayed.

1. **The headline knee came from the wrong method.** 0.1.0 reported Kneedle's answer whenever Kneedle produced one and fell back to p95-doubling only when it did not. On a near-linear latency curve — which most real ramps are — Kneedle lands at or near the top of the ramp, so 0.1.0's headline was often higher than the knee by kneepoint's own stated definition (the first level where p95 reaches twice the curve's floor). One method's number was printed with no sign that a second method existed, disagreed, or how close the call was.
2. **Tool faults were never attributed outside `kneepoint demo`.** The chaos proxy served and counted tool faults, but only the demo — where proxy and ramp share a process — could tie them to the sessions they hit. In every other run the sessions that took a tool fault were counted into the *clean* baseline, so the resilience score was biased toward 100 and the per-fault grid showed LLM faults only. A 0.1.0 resilience score from a `kneepoint run` with the standalone proxy is not a measurement of resilience.
3. **An abandoned request kept its slot upstream, and nothing said so.** 0.1.0's only timeout was httpx's gap timeout, which every arriving chunk resets, so a stream that trickled forever was never abandoned at all. A request that *was* abandoned was recorded as an ordinary failure while the server was free to keep generating — and the next ramp level then queued behind work nobody was waiting for. Levels past the first abandonment were reported as independent measurements of capacity when they were partly measuring the previous level's leftovers.

Two consequences for stored 0.1.0 files: they carry no `schema_version` (readers treat absent as 0 and keep going) and no metadata sidecar, so `kneepoint report` will render them with every unstored field — target, model, ramp, hold, chaos, prices — marked *unknown* rather than guessed.

### Added
- `kneepoint report <run.jsonl>`: re-render the HTML report from a run's stored records, sessions and metadata sidecar. Byte-identical to the run's own report when the sidecar is present; anything the sidecar does not record renders as *unknown*, never as a default. `--price-in`/`--price-out`/`--min-samples` override the stored values.
- `run` and `demo` write a `run-<id>-meta.json` sidecar (target, model, ramp, hold, seed, chaos profile with its full fault list, price rates, thresholds, timestamps, environment) beside the two JSONLs; `demo` now prints all four artifact paths.
- Every JSONL record and the sidecar carry `schema_version` (now 1) and `kneepoint_version`. Per record rather than as a header line, so a tailed, split or half-written file still shows its version on the line in front of you. `docs/output-format.md` is the contract: every field, its type, when it is null and why null never means zero, how to derive p95, the knee, resolution rate and retry waste, and the compatibility promise (fields may be added within a version; nothing is removed, renamed, retyped or redefined).
- `kneepoint proxy` is a command, with `--fault-log` writing one JSON line per served fault, and `kneepoint run --fault-log` merges that log into the run's sessions before judging — the cross-process fault attribution that fixes defect 2. Every hole in attribution is announced rather than absorbed: faults served without a session header, entries from another run, a missing log file, and `--chaos standard` without a fault log — which now says that without the proxy only the LLM faults reach the grid, instead of advertising four faults of which two could never fire.
- Inter-chunk latency on every request record — `itl_mean_ms`, `itl_p99_ms`, `chunk_count` — with time per output token (`tpot_ms`) derived from fields already on the line rather than stored. Null, never zero, when fewer than two content chunks arrived. Named *inter-chunk* on purpose: a streamed chunk is not guaranteed to be one token, and the docs say to compare `chunk_count` against `output_tokens` before reading it as inter-token.
- Abandonment is recorded and its consequences are shown. `abandoned` on every request record; `abandoned` and `contaminated` on every level, where contamination travels forward from the first abandonment to the end of the ramp; the CLI and the report both refuse to let a contaminated level pass quietly. `docs/measurement-integrity.md` documents what kneepoint guarantees (the socket is closed the instant it gives up) and what it cannot (whether the server acts on the disconnect).
- Retry amplification per level — sessions, turns asked for, requests issued, retries — printed for the worst level and explained in the report, because a ramp with retries can partly manufacture the knee it measures. `conversation.retry` is documented, with `max_attempts: 1` as the control.
- The HTML report gains seven sections. **Quality under load**: p95 and resolution rate on two axes, knee marked, never normalised onto each other. **Latency breakdown**: TTFT, TPOT and inter-chunk latency per level, `n/a` never zero. **Retry amplification**. **Errors**: failures per level with the status-code mix and the most common error strings, including levels where every request failed (the latency charts cannot show those — they have no latency). **TTFT vs. TPOT** and **inter-token latency** charts, gated by `min_samples` like the knee curve, with a stated reason when a non-streaming target has nothing to chart. **Run metadata**: every sidecar field, read from the file the run wrote; machine notes are listed as *not recorded* rather than omitted. Reports rendered without a sidecar say so instead of guessing.
- Every report section — the verdict and all eleven below it — ends with a *How to read this* line: what the figure means, which direction to read it in, and the mistake it invites (plan for the low end of a knee range; TTFT-up-TPOT-flat is queueing; $/resolved prices the outcome, not the activity; a local model's cost is a projection at named rates, not spend). Each is worded from the Book chapter that owns the metric and links to it.
- **The Kneepoint Book** (`docs/book/`): foundations, throughput, latency, quality, stability, cost, reading charts, recipes, glossary — the metric definitions the CLI implements and the report teaches from.
- `examples/local_agent`: wraps a local OpenAI-compatible model server as an agent kneepoint can measure — multi-turn sessions, a tool step chaos can break, and a task the model either resolves or does not — with `docs/local-models.md` on how to run against it. Ramping a bare model server measures inference queueing, not agent behaviour.

### Changed
- **Licence: MIT → Apache License 2.0**, from this release onward. `LICENSE` is the full, unmodified Apache 2.0 text; a `NOTICE` file is added at the repo root and both ship in the wheel's `.dist-info/licenses/`. Apache 2.0 grants everything MIT did plus an express patent licence. **0.1.0 and 0.0.1 remain MIT-licensed, and that grant is irrevocable**: anyone who received those releases under MIT keeps every right MIT gave them, permanently — a licence change only governs what is released after it. The change is made with no external contributors to consult; every line was written by the copyright holder.
- The knee is a **range with a confidence label**, and both methods are always reported. p95-doubling is the headline; Kneedle is corroboration that lowers confidence when it disagrees and never replaces the number. A crossing within 15% of the 2× threshold is a coin flip and widens the range; a contaminated level can never carry high confidence. `knee_report()` is the surface that carries all of this; `find_knee()` remains for callers that want one integer, returns the conservative (low) end of the range, refuses a Kneedle answer at the top of the ramp, and is documented as carrying no confidence.
- `kneepoint run` and `kneepoint demo` print both knee verdicts with the per-level p95 ratios the heuristic decides on, a contamination warning naming the affected levels, and the worst level's retry amplification.

### Fixed
- **Defect 1**, the knee headline: see above. `KneeReport` and `knee_report()` replace the silent pick.
- **Defect 2**, fault attribution: see above and `--fault-log` under Added.
- **Defect 3**, abandonment: the wall is now a real total deadline around the whole exchange, not a gap timeout; on expiry the response is closed and the socket shut, so the server sees the disconnect immediately (held in place by `tests/test_cancellation.py`). Connect and pool timeouts are explicitly not abandonment — no upstream work ever started.
- p95-doubling crossing only at the **last** level of the ramp was headlined as a found knee with high confidence. Nothing above that level was measured, so it is now reported as a lower bound — "N concurrent sessions or beyond, low confidence" with a widen-the-ramp note — the same refusal Kneedle already had for top-of-ramp answers. `KneeReport` gains a `lower_bound` flag.
- `kneepoint demo` now finds its own knee, for two reasons that were fixed separately. At the bundled agent's old capacity of 8 its curve was flat to the top of the ramp, so the only knee it ever found was Kneedle's top-of-ramp artifact — defect 1 in miniature; capacity is now 2 so the queue forms mid-ramp. Then its p95 floor was set by the agent's 800ms tool timeout, so the queue only reached 2× at the noise threshold ("7-10, low confidence" at the default hold, "13 or beyond" at a short one); the tool timeout is now 300ms and its answers slightly longer. At the default hold the demo reports "7 concurrent sessions, high confidence" with measured levels on both sides. The ramp (1..13 step 3) and the chaos profile are unchanged.
- Every report chart now obeys the honest-visualization rules the Book teaches, not just the knee curve. Sample counts are on the x-axis under each level instead of hover-only, and a level below `min_samples` keeps its tick, labelled *not plotted*, so a gap cannot be mistaken for a missing level. The resolution rate is gated by `min_samples` judged sessions like the percentiles (a rate over three sessions was previously drawn with the same weight as one over forty); when no level qualifies the quality sections say which levels were judged and how thinly. Contaminated levels are shaded on every chart from the first one onward, with the hover saying so — previously only the banner knew.
- The report header's start time was the clock at report-write time — the finish, labelled as the start. It is now the run's actual start.
- Two renders of the same run produced different HTML (Plotly minted a random div id per figure); the ids are now fixed.
- `status_code` on a successful request was a literal `200` rather than the status the server sent; a `201` or `202` that streamed was recorded as `200`. It is now the real status. `docs/output-format.md` was also made exact about what a failed request carries (`ttft_ms` survives a failure after the first chunk; `input_tokens`, `output_tokens`, `chunk_count` and `itl_*` are always `null` on it), when the wall counts as abandonment, and where `finished_at` is taken; the field tables now follow the order the fields are written, and a test holds each table to its model field for field.

### Security
- The HTML report was rendered with autoescaping **off**: the template environment selected it by file extension and the template is `report.html.j2`, so every interpolated string went into the page raw. Strings that come from outside Kneepoint — a provider's error body in the Errors table, the target URL, the model name, the platform string in Run metadata — could therefore carry markup, and a `<script>` in an upstream error message would have run in whoever opened the report. Autoescaping is now on unconditionally; only the Plotly figure divs are trusted. Re-rendering with `kneepoint report` stays byte-identical.

## [0.1.0] - 2026-07-18

### Added
- Knee-point detection: ramp concurrency against any OpenAI-compatible agent endpoint, find the latency knee (Kneedle + p95-doubling fallback), self-contained Plotly chart.
- Multi-turn sessions: growing message history, seeded prompt-corpus sampling, think time, per-turn retries honoring `Retry-After`.
- Resolution judging: deterministic contains/regex checks on the final answer, plus a sampled LLM-as-judge (never called in CI; scenario-configured).
- Cost engine: $/resolved task, retry-waste %, per-request token pricing.
- Chaos v0: seeded fault injection — `llm_rate_limit`, `llm_server_error` at the transport; `tool_timeout`, `tool_malformed_json` via an in-process reverse proxy for the agent's tool calls.
- Resilience score v0: resolution rate of faulted vs. fault-free sessions within one chaos run, with a per-fault pass/degraded/fail grid.
- Budget rail: pre-run cost estimate from a calibration request, hard `--max-spend` cap with graceful mid-run stop (exit code 3).
- HTML report v1: knee curve + quality curve + cost card + resilience grid in one self-contained file.
- Scenario YAML: full Pydantic-validated config, `kneepoint init` starter, `kneepoint validate`, SLO evaluation with non-zero exit codes for CI gates.
- `kneepoint demo`: zero-API-key first run — bundled agent + chaos + full report in one command.

## [0.0.1] - 2026-07-12

### Added
- PyPI name reservation stub. Nothing worked yet; that was the point.
