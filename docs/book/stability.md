# Stability & resilience

Systems don't only get slow. They fail, retry, and sometimes fail in ways
that look like success. This chapter covers what breaks and how gracefully.

## `error_rate` and `status_code`

**`error_rate`** — share of requests that did not succeed, per level. The
classic. What matters is the *shape*: errors that appear only above a certain
concurrency are a capacity signal; errors present from level 1 are a
configuration bug.

**`status_code`** distribution tells you *which* failure:

| Code | Usually means |
|---|---|
| 429 | rate limited — the provider is throttling you, not struggling |
| 500/502/503 | the server is genuinely failing under load |
| timeout (no code) | the request took longer than the client would wait |

These have different fixes, so never look at a single "error rate" number
without breaking it down.

!!! warning "Timeouts contaminate what follows"
    When a client abandons a request, the server may keep generating the
    response anyway. That work steals capacity from requests that *are* still
    being waited on. Measured in Kneepoint's own testing: 0.8 s → 8.6 s on
    the request following an abandoned one. Once you see timeouts at a level,
    treat latency at that level and above as suspect until you know the
    client actually cancels upstream work.

## `attempt` and `retry_share`

Every `RequestRecord` carries an `attempt` number. `attempt > 1` means this
was a retry.

**`retry_share`** — the fraction of requests that were retries, per level.

Retries are how systems hide instability from users — and how they burn
budget invisibly. A retry costs full price in tokens and capacity while
producing no additional user value. Rising retry share with concurrency
means: your users may not see errors, but you're paying for them, and the
retries themselves are adding load to an already-loaded system (a classic
feedback loop that turns a slowdown into an outage).

Pair `retry_share` with `cost` to see the money being spent on failure.

## Chaos: deliberately breaking things

Real dependencies fail. Testing only the happy path tells you how the system
behaves on its best day. Kneepoint can inject four faults:

| Fault | Simulates |
|---|---|
| `llm_rate_limit` | the model provider returning 429 |
| `llm_server_error` | the model provider returning 5xx |
| `tool_timeout` | a tool the agent calls hanging |
| `tool_malformed_json` | a tool returning garbage the agent must handle |

The last two are the interesting ones, and they're where agents typically
fail: a well-built agent notices its tool returned nonsense and recovers or
tells the user; a naive one silently produces a confident wrong answer.

## `resilience` — does it survive faults?

**Definition:** how much worse the resolution rate is for sessions that hit a
fault, compared to sessions that didn't.

```
resilience = (resolution rate of faulted sessions
              ÷ resolution rate of clean sessions) × 100
```

100 means faults made no difference — the agent absorbed them. A low score
means injected failures translate directly into failed user tasks.

The **per-fault grid** breaks this down by fault type, which is where the
actionable detail lives: an agent might handle rate limits perfectly (because
the HTTP client retries them) and fail completely on malformed tool output
(because nobody wrote that error path).

!!! warning "Attribution needs the fault log outside `kneepoint demo`"
    The score is only as good as the *attribution* behind it. Tool-path faults
    are served by the proxy; if the session record never learns it was hit,
    that session is counted into the "clean" baseline and the score is biased
    toward 100. This happened in Kneepoint's own testing: 35 tool faults served
    in one run, **zero** attributed, and a resilience score of 95 that meant
    nothing.

    `kneepoint demo` runs the proxy in-process and attributes every fault.
    Everywhere else the proxy is a separate process, and `kneepoint run
    --fault-log` is what carries attribution across that boundary — see
    [Fault attribution](../chaos.md#fault-attribution). A chaos run without
    the log still grids the LLM faults; the tool-fault rows are the ones that
    need it, and the report says when a grid is incomplete.

## `fault_count` (per session)

How many faults a single session encountered. Useful for a sanity check: if
your chaos profile says 10% and sessions are averaging three faults each,
your profile is more aggressive than you think — and your resolution numbers
are measuring a much harsher world than production.

## Reading stability as a whole

A healthy system under increasing load degrades in this order: tail
amplification rises → p95 bends → retries appear → errors appear →
resolution falls. When you see that sequence, you're watching normal
saturation.

When the order is *different* — resolution falling while latency and errors
stay flat — something more interesting is happening: context truncation,
quality collapse, or a judge problem. That's the anomaly worth investigating.
