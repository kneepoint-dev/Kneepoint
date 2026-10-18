# Measurement integrity

## Clocks, steady windows and the closed-loop check

Kneepoint records two clocks. `started_at` is wall time: it says when an
attempt happened on the calendar, but the operating system may move it forward
or backward. `started_monotonic_s` is elapsed seconds from the run's monotonic
origin. Request durations already use that clock. New runs use it for level
windows, throughput and run duration too. Old records have no monotonic start;
their rates use `started_at` and the report says **wall fallback**. Such rates
can be wrong after a clock step.

For a level held for `H` seconds, the steady window is exactly
`[level start + H/3, level start + 4H/5)`. It is 5 through 12 seconds for a
15-second hold. Only attempts that **complete** inside that half-open window
count toward steady throughput `X`; their mean full response time is `R`.
The older raw `throughput_rps` uses all attempts from the first start through
the last completion, including the drain tail. It remains available for
compatibility. New runs record the actual start of each level's hold; older
records approximate it with their first observed attempt.
If a level stops or fails before finishing its hold, the configured steady
window was not fully observed. Its steady rate and law check are unavailable;
the raw rate still describes the attempts that completed.
When old metadata lacks a level outcome, the report uses its lifecycle file
if present. Without one, it requires at least one observed completion at or
after the steady window's end before reporting a steady rate.

In a closed loop, `N` active sessions should produce roughly `N/X = R + Z`,
where `Z` is the configured mean think time **per completed request**. The
generator sleeps between turns of a session, then starts its next session
without a pause. The analysis counts which completed turns were followed by
another turn and applies the configured mean pause only to that fraction.
The report shows `N/X − R − Z` for each level. A magnitude above the larger of
50 ms or 5% of `R` is marked **suspected client or instrument stall**. This is a consistency
check, not a diagnosis: retries, variable think time and short holds can also
affect it. A level without completions in the steady window has no gap value.
Old metadata did not record `Z`, so re-rendering it shows the law gap as `n/a`
instead of guessing zero.

Two clock checks mark a level **clock step, host sleep or client stall** when
they see more than one second of unexplained time. New records compare the
wall and monotonic elapsed time within each level. Both new and old records
also check whether the next request begins much later than the preceding one
could have finished plus think time. For old one-turn sessions, the check uses
the closed-loop pool's last completion because the records have no worker ID.
If old metadata does not say how long multi-turn sessions could think, that
continuity check is unavailable rather than treating normal pauses as stalls.
One-turn sessions never think between requests, even if a nonzero think-time
setting was supplied, so their pool continuity check uses no think allowance.
The marked level and later levels are shaded as contaminated; knee confidence
is lowered. The check cannot prove which of the three causes occurred.

For long runs on macOS, wrap the command with `caffeinate -dimsu` to keep the
host awake. The clock checks still apply.

A capacity number is only worth as much as the measurement behind it. This page
documents the one place where kneepoint's own measurement can be contaminated,
what kneepoint does about it, and what it cannot do.

## Abandoned requests contaminate the levels that follow

Kneepoint gives up on a request in two situations:

* **the gap timeout** — no byte has arrived from the server for
  `REQUEST_TIMEOUT_S` (120 s). This is httpx's timeout, and it is a *gap*
  timeout: every chunk resets it.
* **the wall** — the request has been open for `REQUEST_WALL_S` (120 s) in
  total, counted from the moment it was sent. This one exists because a stream
  that trickles a token every 119 s never trips the gap timeout, and would run
  forever.

Either way the request is recorded with `abandoned: true`, `ok: false`, and it
takes no part in any latency percentile. One qualification: the wall marks a
request abandoned only if a response status had already arrived. A wall that
expires while still waiting for one — the connection or the pool never
delivered it — is recorded as an error, not an abandonment, because nothing
shows that work ever started upstream.

The problem is what happens next. Kneepoint closes the connection the instant
it gives up — but a server is free to keep generating. If it does, the
abandoned generation still holds a slot, and the *next* request queues behind
work nobody is waiting for. Ramp levels stop being independent measurements.

This was measured on Ollama during Run D: a request that took **0.8 s on an idle
server took 8.6 s** when issued right after a long request had been abandoned.

## What kneepoint guarantees, and what it doesn't

**Guaranteed:** the moment kneepoint abandons a request, the response is closed
and the socket shuts, so the server observes a client disconnect immediately.
Measured on a local target, the disconnect is registered within the measurement
resolution of the abandonment itself (≤1 ms). `tests/test_cancellation.py`
holds this in place.

**Not guaranteed — and not ours to guarantee:** whether the server acts on that
disconnect. Reproduced against the same target under both policies (wall 0.5 s,
server prefill 1.5 s + 1.0 s generation, capacity 1, medians of 5 repetitions):

| Server policy | Baseline request | Same request right after an abandonment | Inflation |
|---|---|---|---|
| Aborts generation on disconnect | 86 ms | 87 ms | **1.0×** |
| Ignores the disconnect | 88 ms | 2096 ms | **23.8×** |

Any ASGI application that never reads `receive()` falls in the second row —
which is most of them — as does Ollama, measured: the abandoned generation ran
to completion and held its slot. We have not measured any other server, so if
you do not know which kind yours is, assume the second row.

## So kneepoint flags the levels instead

Because the contamination cannot be fixed from the client, it is made visible.
`aggregate()` marks a level `contaminated` if that level abandoned a request
**or any earlier level did** — abandoned work travels forward in time, and a
level that was dropped entirely for having no successful requests still
contaminates the level after it.

The CLI says so:

```
Contaminated levels: 6, 7, 8 - 39 request(s) abandoned at the 120s wall.
Kneepoint closes the connection immediately, but the server may keep
generating, so these levels can be measuring the previous level's leftovers.
Do not publish latencies or a knee from them.
```

and the HTML report carries the same banner above the knee curve — and shades
every chart from the first contaminated level to the end of the ramp, so a
screenshot of one chart cannot leave the caveat behind. The hover on a
contaminated point says so too.

**Rule of thumb: a ramp that reaches the wall has stopped measuring capacity.**
Stop the ramp below the wall, or state the contamination when you publish the
curve. Every published Run D ramp was kept below the wall for exactly this
reason, which is why all fifteen repetitions have zero errors.

## Retries amplify it

A timeout is retryable, so the session runner retries an abandoned turn up to
`retry.max_attempts` times — each attempt entering the same saturated queue.
Measured against a capacity-1 target, one abandoned turn issued three requests
and lost all three; Run D's `c=7` level issued 21 requests for 7 workers and
lost every one of them.

This is deliberately still the default: retrying a timeout is what a real client
does, and changing it would silently move every number kneepoint has ever
produced. But it is why an abandoned level tends to be *thoroughly* abandoned
rather than partially, and one more reason not to ramp into the wall.
