# Foundations

Everything else in this book assumes this chapter.

## 1. The anatomy of a load test

A Kneepoint run has a specific shape. Learn these five words and most of the
tool explains itself.

**Request** — one HTTP call to the agent. Produces one `RequestRecord`:
latency, tokens, success, which turn of the conversation it was.

**Turn** — one exchange in a conversation (user says something, agent
replies). One turn is normally one request; a retry makes it two.

**Session** — one simulated user's whole conversation, start to finish.
Multiple turns. This is the unit that either *resolves* the user's task or
doesn't. **Sessions are what Kneepoint counts; requests are what it times.**

**Concurrency level** — how many sessions run simultaneously. In Kneepoint,
concurrency means *sessions in flight*, not requests per second. A session
occupies a slot for its whole conversation.

**Ramp** — the schedule of concurrency levels. `1..16 step 3` means: run at
1, then 4, then 7, 10, 13, 16. Each level is held for `hold_seconds` while
measurements accumulate.

So a run is: for each level in the ramp, hold that many concurrent sessions
for N seconds, record everything, then step up.

### Why concurrency and not requests-per-second?

Traditional web load testing uses RPS because a web request is short and
independent. An agent conversation is long and stateful — the interesting
question isn't "how many requests can I fire" but "how many *users* can I
serve at once before the experience breaks". Concurrency is the axis the
capacity question is actually asked in.

## 2. Percentiles — the only latency numbers worth reading

Never use averages for latency. Here's why, concretely: ten requests take
1 s each and one takes 30 s. The average is 3.6 s — a number *nobody
experienced*. Nine people saw 1 s, one person saw 30 s and left.

A **percentile** answers: "what latency were X% of requests faster than?"

- **p50 (median)** — the typical experience. Half were faster.
- **p95** — the bad-but-not-rare experience. 1 in 20 users hits this or worse.
- **p99** — the tail. Rare, but at scale it's constant: 1 in 100 requests,
  which on a busy system is many people per minute.

**Kneepoint uses p95 as its primary signal** because p50 hides degradation
(the median stays flat long after the system starts struggling) and p99 is
too noisy at benchmark sample sizes to detect a trend reliably. p95 is the
compromise: sensitive to real degradation, stable enough to compare runs.

!!! note "How Kneepoint computes percentiles"
    `statistics.quantiles(sorted(values), n=100, method="inclusive")`, index
    `p-1`. The exact method matters — different tools disagree by a few
    percent on the same data, so never compare a Kneepoint p95 to another
    tool's p95 without checking their method.

## 3. Sample count (n) decides whether a number is real

This is the single most important idea in this book.

To compute a p95 honestly you need enough samples that the 95th percentile
is actually *observed* rather than interpolated from thin air. Rough
guidance:

| You want | Minimum sensible n | Why |
|---|---|---|
| p50 | ~10 | the median is stable early |
| p95 | ~20, prefer 100+ | below 20, one slow request moves it wildly |
| p99 | 100+, prefer 1000 | with n=50, p99 *is* your single worst request |

Kneepoint defaults to `min_samples: 10` and **refuses to plot percentiles
below it** — you'll see a gap in the line, and the level's tick on the x-axis
reads `n=8 - not plotted` rather than a fake point. That gap is a feature.
It's the tool declining to lie. The same threshold gates the resolution rate:
a rate over three judged sessions can only be 0, 33, 67 or 100%, so it is
left off the quality curve and marked on the axis the same way.

When you read any latency chart, look at the sample count behind each point
before you interpret the shape — in the report it sits under each level on
the axis, so you never have to hover to find it. A curve that "spikes at
level 13" where n=8 is not a spike; it's an absence of data.

## 4. Warm-up, steady state, and why hold time matters

When load first arrives at a level, the system is not yet in the state
you're trying to measure: caches are cold, the model may still be loading,
connection pools are filling. Latency in the first seconds of a level is
usually worse and is **not representative**.

The fix is a long-enough `hold_seconds` that the steady-state portion
dominates. Kneepoint benchmarks use 120 s; a 5–20 s hold is fine for smoke
tests but will fold warm-up noise into your numbers.

You can *see* this: plot latency on the **time axis** (see
[Reading charts](reading-charts.md)) and look at the beginning of each level.

## 5. Repetition — one run is an anecdote

A single run tells you what happened once. Systems vary: thermal state,
background processes, memory pressure, the model's own non-determinism.

Kneepoint's methodology requires **at least three repetitions** of any
configuration you intend to publish or make a decision on, reporting the
median and the observed spread — not the best result.

If three repetitions disagree by more than ~15% on the headline number,
something in your environment is uncontrolled, *or* the metric itself is
unstable near a threshold. Both are findings. Neither is a reason to pick
the run you liked best.

!!! example "This is not hypothetical"
    Kneepoint's own first local benchmark (Run D) ran three identical
    repetitions of the same model and got knee values of 3, 2, 2. The cause
    turned out to be the *detector*, not the machine — see
    [Throughput & capacity](throughput.md#when-the-knee-is-unstable). One run
    would have published a confident wrong number.

## 6. The measurement itself perturbs the system

Two effects to keep in mind:

**Collocation.** If your load generator and your model run on the same
machine, they compete for CPU and memory bandwidth. Measured latency at
high concurrency is inflated relative to a setup where the generator is
remote. This is realistic for a developer laptop and unrealistic for
production — state which one you're doing.

**Abandoned work.** When a client times out, the server may keep generating
the response nobody is waiting for. That stolen capacity inflates the
*next* requests. Any level measured after a timeout wall may be
contaminated — check the error counts before trusting latency past that
point.
