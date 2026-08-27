# Glossary

One line each. Chapter links go to the full explanation.

## Load testing

**Concurrency level** — how many sessions run simultaneously; Kneepoint's
primary x-axis. ([Throughput](throughput.md))

**Ramp** — the schedule of concurrency levels, e.g. `1..16 step 3`.

**Hold time** — seconds spent at each level while measurements accumulate.

**Session** — one simulated user's full multi-turn conversation; the unit
that resolves or doesn't. ([Foundations](foundations.md))

**Turn** — one exchange within a session.

**Request** — one HTTP call; a turn is one or more requests if retried.

**Warm-up** — the unrepresentative period at the start of a level before the
system reaches steady state.

**Steady state** — the stable portion of a level; what you actually want to
measure.

**Collocation** — running the load generator and the target on the same
machine; inflates latency at high concurrency.

## Latency

**TTFT** — time to first token; the pause before the response starts.
([Latency](latency.md))

**ITL** — inter-token latency; the gap between successive tokens. Not
currently measurable in Kneepoint.

**TPOT** — time per output token; `(total − TTFT) / output_tokens`. The
practical stand-in for ITL.

**p50 / p95 / p99** — percentiles; the latency X% of requests beat.
([Foundations](foundations.md))

**Tail amplification** — `p99 / p50`; how unfair the experience is. Rises
early. ([Throughput](throughput.md))

**Prefill** — the model processing your input prompt; the main component of
TTFT.

**Decode** — the model generating output tokens one at a time; what TPOT
measures.

## Capacity

**Knee point** — the concurrency where latency stops scaling gracefully.
Kneepoint's signature metric. ([Throughput](throughput.md))

**Kneedle** — a curvature-based knee algorithm; degenerates on near-linear
curves.

**p95 doubling** — Kneepoint's fallback method: first level where p95 is ≥ 2×
baseline. Simple, hand-checkable, unstable near its threshold.

**Goodput** — throughput that met your latency SLO; capacity that actually
counts.

**Sessions per hour** — capacity in planning units.

**SLO** — service level objective; the latency/quality target you promise.
Capacity is meaningless without one.

**Saturation** — the state where added load produces disproportionate
degradation.

## Quality

**Resolution rate** — share of judged sessions where the agent accomplished
the task. ([Quality](quality.md))

**Deterministic judge** — a marker/string check; objective but only proves a
claim of success.

**LLM-judge** — a separate model reading transcripts and answering "was this
accomplished?" — a grader, not a participant.

**Unjudged** — sessions the judge declined to rule on; excluded from the
denominator, never silently failed.

**Format-following baseline** — resolution at concurrency 1 with no chaos;
the ceiling for every later measurement.

**Accuracy drift** — resolution at c=1 minus resolution at c=N; degradation
caused by load.

**Trajectory evaluation** — scoring the agent's *path* (tool choice, step
order). Eval-platform territory, not Kneepoint's.

## Stability

**Chaos / fault injection** — deliberately breaking dependencies to test
recovery. ([Stability](stability.md))

**`llm_rate_limit` / `llm_server_error`** — injected provider failures (429,
5xx).

**`tool_timeout` / `tool_malformed_json`** — injected tool failures; where
agents usually break.

**Resilience score** — resolution of faulted sessions ÷ resolution of clean
sessions × 100. Currently unreliable outside `kneepoint demo`.

**Retry share** — fraction of requests that were retries; invisible cost and
self-inflicted load.

**Attribution** — correctly linking a fault to the session that experienced
it. When it fails, resilience scores are biased toward 100.

## Cost

**Input / output tokens** — prompt (including conversation history) and
generated text; priced differently. ([Cost](cost.md))

**$/resolved task** — spend ÷ resolved sessions; prices the outcome, not the
activity. Kneepoint's second signature metric.

**Projection** — local-run token counts priced at a named provider's rates;
always labelled, never called spend.

**Prompt caching** — provider-side reuse of repeated prompt prefixes; cuts
input cost, and a randomized test won't exercise it.

**Retry waste** — share of spend consumed by failed or retried requests.

## Discipline

**Honest None** — reporting "undetectable" or "insufficient samples" rather
than guessing. Applies to metrics and charts alike.

**Sample count (n)** — how many observations back a number. Without it, a
percentile is a rumour.

**Repetition** — running an identical configuration ≥3 times; one run is an
anecdote.

**Spread** — the disagreement across repetitions; >15% on a headline number
means something is uncontrolled.

**Comparability** — whether two runs are alike enough to compare. Different
model, ramp, hold, or machine means they aren't.
