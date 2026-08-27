# Quickstart

## 1. The 90-second demo (no API keys)

```bash
pip install kneepoint
kneepoint demo
```

This starts a bundled, deliberately naive agent, injects chaos into both its
LLM calls (429s, 503s) and its tool calls (timeouts, malformed JSON), ramps
concurrency, and opens a report with all three metrics. Useful flags:
`--hold-seconds 10` (longer = smoother curves), `--seed N` (reproducible),
`--no-open`, `--out DIR`.

## 2. Your agent

```bash
kneepoint init
```

writes a commented `kneepoint.yaml` and a starter prompt corpus. Point
`target.url` at any OpenAI-compatible endpoint, set a resolution check that
matches how your agent signals success, then:

```bash
kneepoint run --scenario kneepoint.yaml
```

Every scenario value can be overridden by a flag (`--ramp 1..20 --chaos off`
etc.) — flag beats scenario beats default.

## 3. Read the report

- **Verdict**: the knee range and its confidence, with what each of the two methods concluded and why.
- **Knee curve**: p50/p95/p99 latency vs. concurrency; the dashed line is your knee.
- **TTFT vs. TPOT** and **inter-token latency**: whether the target is slow to *start* (queueing) or slow to *continue* (decode contention), when it streams.
- **Quality under load** and the **quality curve**: resolution rate vs. concurrency — quality often cliffs *before* latency.
- **Errors**: failures per level with the status-code mix and the most common error strings — a `429` and a timeout have different fixes.
- **Retry amplification**: how much load the generator added on top of the workload.
- **Cost card**: total spend, retry waste, and the headline $/resolved task.
- **Resilience grid**: per-fault pass/degraded/fail (≥95% / ≥70% / below, relative to fault-free sessions).
- **Run metadata**: ramp, hold, seed, chaos profile, prices, thresholds, versions — read from the run's sidecar, so two reports are comparable only when these match.

Every chart plays by the same rules: the sample count sits under each level on
the x-axis (a level below `min_samples` stays on the axis, marked *not
plotted*), y starts at zero, nothing is smoothed, and contaminated levels are
shaded on the chart itself, not only in the banner. See
[Reading charts](book/reading-charts.md) for why each of those matters.

Every section ends with a short **How to read this** line — what the number
means, which way to read it, and the trap it usually sets — worded from the
chapter of [the Book](book/index.md) that owns the metric, and linking to it.
The report is meant to be readable without having used Kneepoint before.

Lost the HTML, or want it regenerated? `kneepoint report reports/run-<id>.jsonl`
rebuilds it from the run's stored files (see [Output format](output-format.md#re-rendering-the-report)).

!!! tip "Always set `cost.max_spend`"
    The pre-run estimate aborts runs that would blow the cap (override with
    `--force`), and the runtime meter stops a live run that crosses it
    (exit code 3, partial results still reported).
