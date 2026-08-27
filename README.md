# Kneepoint

> **Find where your AI agent breaks.** Load testing, cost per resolved task, and chaos engineering for AI agents — one command, one self-contained report.

[![PyPI](https://img.shields.io/pypi/v/kneepoint)](https://pypi.org/project/kneepoint/)
[![CI](https://github.com/kneepoint-dev/kneepoint/actions/workflows/ci.yml/badge.svg)](https://github.com/kneepoint-dev/kneepoint/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/kneepoint)](https://pypi.org/project/kneepoint/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/kneepoint-dev/kneepoint/blob/main/LICENSE)

Your agent passes its evals at concurrency 1. Production is not concurrency 1.
Kneepoint ramps concurrent multi-turn sessions against any OpenAI-compatible
endpoint, judges whether each task was actually *resolved*, prices every
token, injects the faults production will inject for you, and answers three
questions:

1. **The knee point** — the concurrency where latency stops being flat and
   starts to climb. Reported as a range with a confidence label, never as a
   bare number the data cannot support.
2. **$ / resolved task** — spend divided by tasks actually solved, retries and
   failures included. Not tokens, not requests.
3. **Resilience score** — how much of your resolution rate survives rate
   limits, server errors, tool timeouts and malformed tool JSON.

Everything runs on your machine. Results are plain JSONL under an open,
versioned [output format](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/output-format.md)
you can build on without asking anyone.

## Install

```bash
pip install kneepoint
```

Python 3.11 or newer. Nothing on this page needs an API key.

## First run: the demo

```bash
kneepoint demo
```

This starts a bundled, deliberately naive support agent, injects chaos into
its LLM calls (429s, 503s) *and* its tool calls (timeouts, garbage JSON),
ramps concurrency against it, and opens the report in your browser. Nothing
leaves your machine and nothing is billed — the cost figures use play-money
prices.

Useful flags: `--hold-seconds 10` (longer holds mean more samples per level
and smoother curves), `--seed N` (reproducible prompt sampling and chaos),
`--no-open`, `--out DIR`.

![kneepoint demo](https://raw.githubusercontent.com/kneepoint-dev/kneepoint/main/docs/assets/demo.gif)

## Against your own agent

```bash
kneepoint init                            # starter kneepoint.yaml + prompt corpus
kneepoint validate kneepoint.yaml         # schema-check the file without running anything
kneepoint run --scenario kneepoint.yaml   # ramp, judge, price, report
```

Any OpenAI-compatible chat endpoint is a target. A scenario is one YAML file,
validated strictly — unknown keys are rejected — and every value in it can be
overridden by a flag (`--ramp 1..20 --chaos off` …): flag beats scenario
beats default.

```yaml
target:
  url: http://127.0.0.1:8000/v1
  model: my-agent
  # auth_env: AGENT_API_KEY                       # env var holding a bearer token

workload:
  ramp: {from: 1, to: 50, step: 5, hold_seconds: 20}
  conversation:
    turns: {min: 1, max: 3}                       # multi-turn: context grows every turn
    corpus: ./prompts/*.txt                       # your real prompt distribution

resolution:
  check: {kind: contains, value: "[RESOLVED"}     # or a regex, or an LLM judge

cost:
  input_per_mtok: 3.00
  output_per_mtok: 15.00
  max_spend: 0.50                                 # hard cap: the run stops if crossed

chaos:
  profile: standard                               # 429s, 503s, tool faults

slo:
  min_resolution_rate: 0.90                       # breach -> exit code 1
```

**Set `cost.max_spend` before pointing at a paid model.** The pre-run
estimate refuses a run that would blow the cap (`--force` overrides), and the
live meter stops a run that crosses it mid-ramp — partial results are still
analysed and reported.

LLM faults need no infrastructure: they are injected inside Kneepoint's own
HTTP client. Tool faults have to sit between your agent and its tools, so
`kneepoint proxy --upstream <your tool service> --fault-log faults.jsonl`
runs a small reverse proxy, and `kneepoint run --fault-log faults.jsonl`
carries what it served into the resilience grid. Details, and the standard
fault profile, in [Chaos testing](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/chaos.md).

Three copy-anywhere example scenarios ship in the repo —
[support bot](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios/support-bot),
[RAG agent](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios/rag-agent),
[agent with MCP tools](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios/mcp-tool-agent)
— and [Local models](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/local-models.md)
covers measuring a local model *behind an agent*, which is not the same as
ramping the model server on its own.

## Reading the report

The report is one HTML file with everything embedded, meant to be read by
someone who has never used Kneepoint. Top to bottom:

- **Verdict** — the knee as a range with a confidence label, and what each of
  the two detection methods concluded. Plan for the low end of the range:
  being under the knee costs headroom; being over it costs everyone's latency
  at once. When the crossing lands on the last level of the ramp, the verdict
  says *N or beyond* and tells you to widen the ramp rather than reporting a
  knee it cannot see. When nothing crossed, it says so instead of inventing
  one.
- **The knee curve** — p50/p95/p99 latency against concurrency, knee marked.
- **TTFT vs. TPOT** and **inter-token latency** — whether the target is slow
  to *start* (queueing) or slow to *continue* (decode contention), when it
  streams.
- **Quality under load** and **the quality curve** — resolution rate against
  concurrency. Quality often cliffs before latency does.
- **Errors** — failures per level with the status-code mix and the most
  common error strings; a `429` and a timeout have different fixes.
- **Retry amplification** — how much load the generator itself added on top
  of the workload, because a ramp with retries can partly manufacture the
  knee it measures.
- **Cost** — total spend, retry waste, and $ / resolved task.
- **Resilience** — per-fault pass / degraded / fail, relative to the sessions
  that took no fault.
- **Run metadata** — target, model, ramp, hold, seed, chaos profile, prices,
  thresholds, versions, read back from the run's own sidecar. Two reports are
  comparable only when these match.

Every chart plays by the same rules: the sample count sits under each level
on the x-axis, a level below `--min-samples` stays on the axis marked *not
plotted* rather than disappearing, y starts at zero, nothing is smoothed, and
levels contaminated by an abandoned request are shaded on the chart itself.
Every section ends with a short *How to read this* line, worded from the
chapter of [the Kneepoint Book](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/index.md)
that owns the metric.

## What a run writes

Four files into `--out` (default `reports/`), all named from one run id, all
four paths printed when the run finishes:

| File | What it is |
|---|---|
| `run-<id>.jsonl` | one line per request attempt |
| `run-<id>-sessions.jsonl` | one line per simulated user session |
| `run-<id>-meta.json` | what the run *was*: target, ramp, seed, chaos, prices, versions |
| `run-<id>-report.html` | the report, derived from the other three |

Lost the HTML, or want it regenerated?

```bash
kneepoint report reports/run-<id>.jsonl
```

rebuilds it from the stored files — byte-identical to the run's own report
when the sidecar is present. Every JSONL line carries `schema_version` and
`kneepoint_version`, and [Output format](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/output-format.md)
is the contract: every field, when it is null and why null never means zero,
and what may change within a schema version (nothing is removed, renamed or
redefined).

## CI gate

Kneepoint is a test: it exits non-zero when your agent regresses.

```yaml
- run: pip install kneepoint
- run: kneepoint run --scenario kneepoint.yaml --out reports
- uses: actions/upload-artifact@v4
  if: always()
  with: {name: kneepoint-report, path: reports/}
```

Exit codes: `0` pass · `1` SLO breach (an SLO that was set but could not be
measured also breaches — never a silent pass) · `2` usage error · `3` budget
cap hit. Pin `seed` so a red build reruns the same prompts and chaos.
[Full CI docs →](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/ci.md)

## Measurement integrity

A capacity number is only worth as much as the measurement behind it, so
Kneepoint says what it cannot know. Percentiles are not computed below
`--min-samples`; a knee is a range when the deciding ratio is a near miss;
and when a request is abandoned at the client wall, every later level is
flagged as *contaminated* — the server may still be generating, so those
levels can be measuring the previous level's leftovers.
[Measurement integrity](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/measurement-integrity.md)
documents what is guaranteed and what is not.

**If you have results from 0.1.0, re-run them.** Three defects in that
release affected the numbers it printed — the headline knee came from the
wrong method, tool faults were never attributed outside the demo, and
abandoned requests were not detected as such. The
[changelog](https://github.com/kneepoint-dev/kneepoint/blob/main/CHANGELOG.md)
states each one plainly.

## Where it fits

Evals tell you the agent *can* do the task. Observability tells you what
happened in production. Kneepoint tells you where it breaks *before*
production does — under concurrency, with growing context, with the tools
misbehaving. Use all three.

## Documentation

[Quickstart](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/quickstart.md) ·
[Scenario reference](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/scenarios.md) ·
[The three metrics](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/metrics.md) ·
[Chaos testing](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/chaos.md) ·
[Output format](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/output-format.md) ·
[CI gate](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/ci.md) ·
[Roadmap](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/roadmap.md)

**The Kneepoint Book** —
[foundations](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/foundations.md),
[latency](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/latency.md),
[throughput & capacity](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/throughput.md),
[quality under load](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/quality.md),
[stability & resilience](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/stability.md),
[cost & tokens](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/cost.md),
[reading charts](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/reading-charts.md),
[diagnostic recipes](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/recipes.md),
[glossary](https://github.com/kneepoint-dev/kneepoint/blob/main/docs/book/glossary.md)
— the metric definitions the CLI implements and the report teaches from.

## Issues and contributions

Bug reports, wrong-looking numbers and agents that Kneepoint mis-measures are
all welcome as [issues](https://github.com/kneepoint-dev/kneepoint/issues).
Pull requests are not being accepted for now.

To reproduce a number or run the suite yourself:

```bash
git clone https://github.com/kneepoint-dev/kneepoint && cd kneepoint
pip install -e ".[dev]"
ruff check . && pytest -q        # runs against the bundled mock agent: no keys, $0
```

Apache 2.0 licensed — see [LICENSE](https://github.com/kneepoint-dev/kneepoint/blob/main/LICENSE)
and [NOTICE](https://github.com/kneepoint-dev/kneepoint/blob/main/NOTICE).
Built in public at [kneepoint.dev](https://kneepoint.dev).
