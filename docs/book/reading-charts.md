# Reading charts without fooling yourself

Same data, three axes, four chart types — and a dozen ways to reach a wrong
conclusion. This chapter is about looking correctly.

## The three axes

Choosing the axis *is* choosing the question.

### `level` — by concurrency (the default)

**Question:** how does this metric change as load increases?

This is the capacity view and where the knee lives. Each point aggregates all
requests at that concurrency level.

Use it for: knee detection, degradation shape, resolution drift, goodput
collapse — essentially every "how much load can we take" question.

### `time` — by wall clock

**Question:** what happened *during* the run?

Each point is a time bucket. Because the ramp steps up over time, you'll see
level boundaries as steps — but you also see what the level view averages
away:

- **Warm-up** — the first seconds of a level slower than the rest. If
  present, your hold time is too short and your level averages are polluted.
- **Drift within a level** — latency creeping upward while concurrency is
  constant. That's a leak: growing queues, memory pressure, thermal
  throttling, or context accumulation.
- **Recovery** — how quickly things return to normal after a fault burst.

Rule of thumb: if a level-axis chart looks odd, switch to the time axis
before theorizing. Half of strange level-axis shapes are warm-up artifacts.

### `turn` — by conversation position

**Question:** does the agent get slower or worse *deeper into a conversation*?

Each point is a turn index: turn 0 is the opening exchange, turn 1 the
follow-up, and so on. Latency climbing with turn index is **context-growth
cost** — every turn carries the whole conversation history as input, so
prefill grows.

This axis is specific to agent testing and effectively unique to Kneepoint.
It answers a question that concurrency can't: your system may be fine with 20
simultaneous *short* conversations and fall over on 5 long ones.

Two cautions: sample counts shrink at higher turn indices (not every session
runs the maximum number of turns), so check `n`; and turn-axis aggregates
that need a wall-clock span (like throughput) don't apply here.

## Chart types and when each lies

**Line** — trends across an ordered axis. The default, and correct for
level/time/turn. Its failure mode: connecting points across a gap implies
data you don't have. Kneepoint draws gaps rather than bridging them —
if you see a break, that's a suppressed point, not a rendering bug.

**Bar** — comparing discrete groups. Better than a line when the x-axis
isn't really continuous. Note that overlapping bars from multiple runs hide
each other — group or stack them per run rather than overlaying them.

**Distribution / histogram** — the shape of the data behind a percentile.
This is the antidote to percentile tunnel vision. A p95 of 4 s could be a
tight cluster around 4 s or a bimodal split of "fast" and "catastrophic" —
completely different problems, identical percentile. **Whenever a percentile
surprises you, look at the distribution before believing it.**

**Heatmap (level × time)** — density across two dimensions at once. Good for
spotting where in the run a problem concentrated.

**Scatter** — two metrics against each other, one dot per request or
session. The correlation tool. Covered below, carefully.

## Correlation: the useful trap

Scatter plots are the most powerful and most dangerous view here.

**Genuinely useful pairs:**

- `output_tokens` vs `total_ms` — is latency explained by verbosity? A tight
  diagonal means the model is just writing more; a vertical spread at fixed
  token count means real server variance.
- `concurrency` vs `resolution_rate` — the drift relationship.
- `ttft_ms` vs `tpot_ms` — do queueing and decode degrade together?
- `input_tokens` vs `ttft_ms` — is prefill cost driving your latency?

**The rules:**

1. **Correlation is not causation** — the phrase is a cliché because people
   keep needing it. Both metrics may be driven by a third thing (usually
   load itself).
2. **Check n.** A correlation coefficient from 15 points is decoration.
3. **Look at the plot, not just the number.** Anscombe's quartet: four
   datasets with identical correlation coefficients and completely different
   shapes. A single outlier can manufacture a strong r.
4. **Beware self-correlation.** `total_ms` and `tpot_ms` share a term —
   of course they correlate. Correlating a metric with its own ingredients
   proves nothing.

## Six ways charts mislead — and the defenses

| Trap | Defense |
|---|---|
| Truncated y-axis exaggerates a small change | keep y starting at zero unless you have a reason |
| Percentile from tiny n looks like a spike | check the `n` on every point; suppressed points show as gaps |
| Smoothing invents a trend | leave smoothing off; it's cosmetic |
| Comparing runs with different setups | check the comparability warning before believing a delta |
| One outlier drags the mean | use percentiles, and look at the distribution |
| Averaging away warm-up | switch to the time axis |

## A default reading routine

1. **Sanity first** — total requests, errors, unjudged sessions. Is this run
   even valid?
2. **Latency by level** — p50 and p95 together. Where does p95 bend?
3. **Knee, both methods** — do they agree? If not, why not?
4. **Resolution by level** — does quality bend at the same place, earlier, or
   not at all?
5. **Decompose** — TTFT vs TPOT to separate queueing from decoding.
6. **Cost by level** — does $/resolved rise before the latency knee?
7. **Time axis** — any warm-up or drift that would invalidate steps 2–6?
8. **Only then** form a hypothesis, and check it against a second repetition.
