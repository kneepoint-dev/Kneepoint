# Cost & token economics

The unit of work in an AI system is not a request — it's a token stream with
variable length and real price. This chapter covers the metrics no
traditional load-testing tool has, because traditional requests are free.

## `input_tokens`, `output_tokens`, `total_tokens`

The raw material. Reported per request by the model provider, usually in the
final chunk of a stream.

**Input** tokens are your prompt plus accumulated conversation history —
which is why they *grow across turns* in an agent session, and why long
conversations cost more per turn than short ones.

**Output** tokens are what the model generated, and they're typically priced
several times higher than input.

!!! note "Honest nulls"
    If a provider doesn't report usage — or a request failed before
    completing — Kneepoint records `null`, not zero. A null token count means
    "unknown", and averaging zeros into your cost would quietly understate
    it. When you see nulls, check whether they line up with failed requests
    (expected) or successful ones (a provider gap worth knowing about).

## `cost` — spend

`(input_tokens × input_price + output_tokens × output_price)`, using rates
you configure per million tokens.

**Local models cost $0 in API spend.** That's the measured truth. But the
token counts are still real and still meaningful — so Kneepoint lets you
price a local run at a named provider's rates to answer *"what would this
workload cost if we ran it on X?"*

That is a legitimate and useful question. It stops being legitimate the
moment the label is dropped. Always present it as a **projection at named
rates**, never as spend.

## `cost_per_resolved` — $/resolved task

**Definition:** total spend ÷ number of *resolved* sessions.

This is Kneepoint's second signature metric, and the one that makes the tool
speak to people who don't read latency charts.

Why it's better than cost per request: cost per request rewards a system that
fails cheaply. If your agent burns 3,000 tokens and gives up, cost-per-request
looks great and the user got nothing. Dividing by *resolved* sessions prices
the outcome, not the activity.

**Reading it against concurrency** is where it gets interesting. Cost per
resolved task usually rises as you approach the knee, because:

- retries multiply token spend without producing resolutions,
- resolution rate falls, shrinking the denominator,
- longer conversations accumulate more input tokens per turn.

So there is often a **cost knee that arrives before the latency knee** —
the system is getting economically worse before it's noticeably slower. For
a business, that may be the more important number of the two.

## `retry waste`

The share of spend consumed by requests that were retried or failed. Money
that bought nothing. Pair with [`retry_share`](stability.md) — one is the
count, the other is the bill.

## Projecting to production

Once you have `cost_per_resolved` at a concurrency you'd actually run at:

```
projected daily cost = cost_per_resolved × expected resolved tasks per day
```

Crude, but grounded in measurement rather than a pricing-page estimate. Two
caveats worth stating whenever you use it: your production prompt mix will
differ from your test corpus, and prompt caching (which most providers now
offer) can cut input costs substantially for repeated prefixes — a test that
randomizes prompts won't exercise the cache path production actually sees.

## What to check before trusting any cost number

1. Are token counts present, or mostly null?
2. Are the configured rates the ones you're actually billed at?
3. Is this measured spend or a labelled projection?
4. Is the denominator *resolved* sessions, or all sessions?
5. Did retries inflate the numerator?


## Observed spend control in the CLI

`kneepoint run` accounts for each completed generator attempt, including retries
and failures with reported usage. `--max-spend` (or `cost.max_spend`) stops new
attempts, turns, and sessions when observed spend reaches the threshold, or when
either token count is missing. Active attempts drain and may overshoot. The CLI
reports partial results and exits with code 3; `--force` never bypasses this stop.
This is an observed threshold, not a guaranteed billing cap.

Capped runs require both input and output prices explicitly, through flags or
the scenario. Explicit zero prices are supported; omitted prices do not declare
free usage. Prices and the cap must be finite and nonnegative. Capped runs skip
calibration, and a configured LLM judge is rejected before load because its cost
is not accounted for. Deterministic checks remain available. Uncapped runs with
nonzero prices retain calibration, labelled outside generator accounting;
judging is also outside that scope.

The CLI flushes request evidence before accounting and saves the controller
snapshot and effective prices in the lifecycle sidecar, including on generator
failure. Without declared prices, accounting is disabled, not reported as free.
Missing token usage suppresses aggregate cost metrics in both the CLI and
regenerated reports: the total is unknown, and the known subtotal is a lower
bound. No finite billing upper bound can be inferred from completed attempts.

A zero cap sends no request, including calibration, and writes empty records and
an HTML report. The existing `kneepoint report` command rejects empty request
files, so that zero-request report cannot currently be regenerated.

## Observed spend controller (Python component)

Python callers can wire `ObservedSpendController` to the attempt completion hook,
not the legacy session-level `on_record`:

```python
from kneepoint.analyze.budget import ObservedSpendController
from kneepoint.analyze.cost import CostRates
from kneepoint.generator.ramp import run_ramp

controller = ObservedSpendController(
    CostRates(input_per_mtok=3.0, output_per_mtok=15.0), max_spend=1.0,
)
sessions, records, legacy_exceeded = await run_ramp(
    base_url, model, ramp_spec, sampler, session_spec,
    stop=controller.stop, on_attempt_complete=controller.on_attempt_complete,
)
accounting = controller.snapshot()
```

Prices and the optional cap must be finite and nonnegative; zero is valid.
The controller copies the prices and sets `stop` at **spend >= cap**. A zero
cap stops before any request. Active calls drain, their records are retained,
and their costs continue to accumulate, so this is **not a guaranteed billing
cap**. The return tuple's `legacy_exceeded` flag still describes the legacy
`BudgetExceeded` exception; inspect `accounting["stop_reason"]` for this controller.

Each completed generator attempt is charged, including retries and failures
with reported usage. Identity is `(session_id, concurrency, turn_index, attempt)`:
identical replay is ignored; a conflicting payload raises `ValueError`.
Create one controller per run and keep its callback synchronous on the generator
loop. Calibration and judge calls are excluded.

Missing either usage count makes `total_spend` unknown (`None`), including at
zero prices. `known_spend` retains the known portions; `coverage` counts complete,
partial, missing, input-known, and output-known usage. A capped run stops on
unknown usage; an uncapped run continues. `known_overshoot` is the known amount
above the cap (or `None` without a cap), a lower bound when usage is missing.
`stop_reason` preserves the first controller reason (`cap_reached` or
`unknown_usage`). All totals cover only observed completed attempts; interrupted
or unobserved upstream work can still incur charges. `billing_upper_bound` is
therefore always unknown, even when every completed record has full usage.

The legacy `CostMeter` and `on_record` callback remain available to Python callers.
That callback runs after a session returns, and `CostMeter` raises only when spend
is greater than its cap. It is not the CLI admission controller. Do not meter the
same attempt through both callback paths.
