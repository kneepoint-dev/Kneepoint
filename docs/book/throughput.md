# Throughput & capacity

Latency tells you how it feels. Capacity tells you how many people can feel
it at once before it stops working. This chapter contains Kneepoint's
signature metric.

## `concurrency` — the x-axis of everything

Concurrent **sessions** in flight, not requests per second. Each level of the
ramp holds this many simulated users in conversation simultaneously.

Every other metric in this book is interesting mainly as a *function of
concurrency*. "p95 is 4 seconds" is trivia. "p95 is 1.2 s at concurrency 1
and 4 s at concurrency 8" is engineering.

## `throughput_rps` and `tokens_per_sec`

**Requests per second** and **tokens per second**, aggregate across all
sessions at a level.

These are the numbers inference vendors quote. They're useful for comparing
serving configurations — but they are **not capacity** on their own, because
throughput keeps climbing for a while *after* the experience has become
unacceptable. A server doing 40 requests/sec at 30 seconds of latency each is
technically high-throughput and practically useless.

## `sessions_per_hour` — capacity in planning units

Completed sessions per wall-clock hour at a given level. This is the number
you take to a capacity conversation: *"we can serve 340 support conversations
an hour on this box."* It converts a technical curve into a business
quantity, and pairs with cost to answer "and it costs $X per hour to do so".

## `goodput` — throughput that actually counts

**Definition:** the share of requests that met your latency SLO. Requests
that arrived too late to be useful are excluded.

This is the metric that joins latency and throughput. High throughput with
bad latency is *low goodput* — the system is doing lots of work nobody
values.

**How to learn from it:** recompute it from your run's JSONL at two different
thresholds — say 2 s and 10 s — and compare the curves. The capacity you read
off them will not be the same number. Nothing teaches "capacity depends on your
SLO" faster. There is no such thing as capacity without a latency target.

## `tail_amplification` — the fairness signal

**Definition:** `p99 / p50`. How much worse the unlucky requests are than the
typical one.

At a healthy level this ratio is small (say 1.5–3×). As a system saturates,
the tail stretches *before* the median moves — some requests start waiting
behind others while most still sail through. That makes tail amplification
an **early warning**: it often rises a level or two before p95 visibly bends.

---

## The knee point

The reason this tool exists.

Kneepoint reports the first level where p95 latency doubles against its floor.
That is a declared latency tolerance; the structural bend in a curve and the
service's saturation point can occur at different levels.

The latency knee and service saturation are separate observations. Saturation
starts when throughput stops rising with concurrency; queue wait then grows as
more sessions contend for the same service capacity. A p95-doubling threshold
can be reached several levels later.

In a closed loop with no think time, Little's law gives `N = X × R`. Once
throughput `X` has flattened at capacity, mean response time `R` grows roughly
with concurrency `N`; mean latency reaches twice its pre-saturation value near
twice the saturation concurrency. p95 has its own distribution and need not
double at exactly that point. In the
LanternDesk search-pool run, throughput plateaued around 6.57 requests/s by
c=4, while p95 first doubled at c=7:

| Concurrent sessions | Steady throughput (requests/s) | Successful p95 (ms) |
|---:|---:|---:|
| 1 | 2.571 | 403.57 |
| 4 | 6.571 | 610.29 |
| 7 | 6.571 | 1,215.02 |
| 10 | 6.571 | 1,522.98 |

The [public LanternDesk example](https://github.com/kneepoint-dev/Kneepoint/blob/codex/demo-agent-traces/examples/multi-agent/README.md)
documents the planted limit of two search workers at 300 ms each. From the
public repository root, start the agent with the search-pool preset:

```bash
cd examples/multi-agent
DEMO_PROFILE=search-pool .venv/bin/uvicorn app:create_app --factory \
  --host 127.0.0.1 --port 8765
```

Then run the [example scenario](https://github.com/kneepoint-dev/Kneepoint/blob/codex/demo-agent-traces/examples/multi-agent/kneepoint.yaml)
from the repository root:

```bash
.venv/bin/kneepoint run --scenario examples/multi-agent/kneepoint.yaml \
  --out reports/lantern-search-pool
```

### How Kneepoint finds it — two methods

**1. p95 doubling.** Walk up the levels; report the first level where
p95(level) ≥ 2 × p95(baseline). Simple, explainable, and reproducible by
hand from the raw data. It marks a latency-tolerance threshold, not the onset
of queueing. Its weakness is that 2.0 is a hard cliff.

**2. Kneedle.** A curve-shape algorithm that finds the point of maximum
curvature — the "elbow". More principled in theory. Its weakness is that on a
near-linear curve there *is* no elbow, and it tends to return the last point
of the ramp, which is a non-answer dressed as an answer.

**p95 doubling is the headline; Kneedle corroborates.** Kneepoint reports both,
always, but the number it leads with is p95 doubling's — it is the stated
definition, and you can check it by hand from the raw JSONL. Kneedle answered on
7 of Run D's 15 repetitions and read *higher* on every one (5, 6, 5, 4, 5, 5, 6
against 2 or 3), which is what a curvature algorithm does on a curve with no
sharp bend. When they disagree, the disagreement is the information: it lowers
the confidence on the headline, and it never replaces it.

### When the knee is unstable

This is worth understanding deeply, because it teaches how thresholds fail.

Kneepoint's own Run D ran three *identical* repetitions of gemma4:12b and got
knees of **3, 2, 2**. No thermal event; run durations within 1.7%. The cause
was visible only in the underlying ratios:

| Rep | p95(c=2) / p95(c=1) | Verdict at threshold 2.0 |
|---|---|---|
| 1 | 1.85 | not yet → knee found later, at 3 |
| 2 | 2.48 | crossed → knee 2 |
| 3 | 2.63 | crossed → knee 2 |

The true degradation was nearly identical in all three runs. What flipped was
which side of an arbitrary line the noise landed on. Meanwhile a different
model whose crossing sat in a clean gap (1.5× then 3.0×) returned the same
knee three times out of three.

**The lesson generalizes far beyond Kneepoint:** any threshold-based detector
is unstable when the measured value sits near its threshold. The fix isn't a
better threshold — it's reporting *confidence*: "knee 2–3, low confidence,
deciding ratio 1.85–2.63 straddles the 2.0 cut" is an honest answer where a
bare "2" is a misleading one.

That is what Kneepoint now prints. A ratio within ±15% of 2.0 is treated as a
coin flip and the knee comes back as a range:

```
Knee point: 2-3 concurrent sessions, low confidence (plan for 2)
```

**Use the low end as a cautious threshold estimate.** `find_knee()` and the
chart marker use the bottom of the range when they need one integer. Choose an
operating load from throughput, quality and your SLO; throughput may have
flattened before p95 doubles.

Two of Run D's five configurations still fail the three-repetition agreement
gate under this detector: their ranges genuinely don't overlap, which is real
run-to-run variance rather than a threshold artifact. That is the gate doing its
job, not the detector failing.

### When the ramp is too short

There is a second way a threshold detector lies, and it has nothing to do with
noise. Suppose p95 first reaches 2× at the **last** level you measured. The
crossing is real — every level below it stayed under 2×, and that part *is* a
measurement. But nothing above it exists to show what the curve did next. A
sharp break at that level and a gentle linear climb that happened to reach 2×
there look identical from inside the ramp; a linear curve reaches 2× eventually
without ever having a knee.

This is exactly the case Kneedle is refused for (it returns the last point on
any near-linear curve), and p95 doubling is not immune just because it is the
headline method. So Kneepoint reports a top-of-ramp crossing as a **lower
bound**, never as a found knee:

```
Knee point: 13 concurrent sessions or beyond, low confidence (plan for 13;
  p95 first doubled at the top of the ramp - widen it past 13 to find the knee)
```

The number survives — "not below 13" is still the one to size from — but the
confidence is low and the remedy is different from the noisy case. A near-
threshold ratio asks for a repetition; a top-of-ramp crossing asks for a
**wider ramp**. Repeating a ramp that was too short just confirms it was too
short.

### What the knee is *not*

**It is not throughput.** Run D tested this directly: the same model with
server parallelism raised (1.55× more throughput) produced the *same* knee.
p95-doubling reads the *shape* of the latency curve, and a uniformly faster
server has the same shape. So a tuning change that genuinely improves
capacity can leave the knee unmoved — you need throughput and goodput
alongside it to see the win.

**It is not a limit you must never exceed.** It's where degradation
accelerates. Run below it with margin; the practical recommendation is
typically knee minus one or two levels, chosen against your SLO, not the knee
itself.

**It is not meaningful without quality.** A system can have a beautiful
latency knee at 12 while its answers stop being correct at 6. Always read the
knee next to [resolution rate](quality.md).
