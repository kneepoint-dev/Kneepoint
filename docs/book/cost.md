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
