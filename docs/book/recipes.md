# Diagnostic recipes

Symptom → what to plot → what each outcome means. Work through these with a
real run open; they're the fastest way to internalize the earlier chapters.

---

## Recipe 1 — "p95 doubled. What broke?"

**Plot:** `ttft_ms` p95 and `tpot_ms` p95, both by level, same panel.

| What you see | Interpretation | Next step |
|---|---|---|
| TTFT rises, TPOT flat | **Queueing.** Requests wait to start; once started they generate at full speed. The server is at its concurrency limit. | Look at goodput; consider more replicas rather than a bigger box |
| TPOT rises, TTFT flat | **Decode saturation.** Generation itself slowed — usually memory bandwidth or KV-cache pressure from many simultaneous streams. | A bigger/faster-memory machine helps; more concurrency won't |
| Both rise together | Broad saturation — the server is out of headroom everywhere. | You're past the knee; back off |
| Neither rises much, `total_ms` still up | Not the model. Tool calls, retries, or orchestration. | Check `retry_share`, `session_total_ms` vs `total_ms × turns` |

---

## Recipe 2 — "Is it actually slower, or just wordier?"

**Plot:** scatter `output_tokens` (x) vs `total_ms` (y), request-level,
filtered to one concurrency level.

- **Tight diagonal** → latency is explained by output length. The system
  isn't degrading; the model is writing more. Compare `tpot_ms` across levels
  instead, which normalizes for length.
- **Vertical spread at similar token counts** → same amount of work taking
  wildly different times. That's real server-side variance.
- **Diagonal that steepens at higher levels** → per-token cost is rising with
  load: decode saturation again.

---

## Recipe 3 — "The numbers look weird at one level"

**Plot:** the same metric on the **time axis**, then check `n` per point.

- Spike only in the first seconds of the level → **warm-up**. Increase hold
  time; the level's aggregate is polluted.
- Steady climb across a constant-concurrency level → **drift**. Something
  accumulates: queue depth, memory, heat.
- `n` much lower than neighbouring levels → the level didn't complete enough
  requests. The percentile may be suppressed or unreliable — don't interpret
  it.

---

## Recipe 4 — "Does my agent degrade in long conversations?"

**Plot:** `total_ms` p95 on the **turn axis**; then `input_tokens` mean on the
same axis.

- Both rise together → **context growth**. Each turn re-sends the whole
  history; prefill grows. Expected, but it means long sessions cost more and
  take longer per turn — size capacity in *turns*, not sessions.
- Latency rises but input tokens don't → not context. Look at tool calls
  later in conversations.
- Resolution falling at higher turn indices → the agent is losing the thread.
  A quality problem, not a capacity one.

Check `n` carefully here: few sessions reach the highest turn index.

---

## Recipe 5 — "Where should I actually run this in production?"

**Plot:** `goodput` by level with your real SLO threshold, plus
`resolution_rate` by level, plus `cost_per_resolved` by level.

Read them together:

1. Goodput tells you the highest level still meeting your latency target.
2. Resolution tells you whether quality holds there.
3. `cost_per_resolved` tells you what it costs at that point.

Your operating point is the **lowest** of the three limits, minus margin.
The knee is a guide, not the answer — a system can be past its latency knee
and still perfectly acceptable if your SLO is generous, or unacceptable well
before it if it isn't.

---

## Recipe 6 — "Did my change actually improve anything?"

**Plot:** both runs overlaid, `total_ms` p95 by level; then throughput and
goodput by level.

- Latency curve shifts *down* → genuinely faster.
- Latency curve shifts *right* (same shape, breaks later) → more capacity.
- **Same knee but higher throughput** → real improvement the knee can't see.
  This is not a contradiction; it's the knee measuring curve *shape*, not
  capacity. Trust throughput and goodput here.
- Nothing moves but resolution improved → a quality win with no perf cost.

Always check the comparability warning first. Two runs with different ramps,
hold times, or models aren't a before/after — they're two different
experiments.

---

## Recipe 7 — "My knee is different every run"

**Plot:** the p95 ratio to baseline, by level, for each repetition.

Look at where each repetition's ratio crosses the threshold. If the deciding
values cluster near the threshold (say 1.85 / 2.48 / 2.63 against a 2.0 cut),
your knee is unstable **because the detector is threshold-based**, not
because the machine varied. The honest report is a range with low confidence.

If ratios are far from the threshold and the knee still moves, *then*
suspect the environment: thermal state, background load, model swapping.

---

## Recipe 8 — "Is my measurement itself trustworthy?"

Before believing any of the above, five checks:

1. **Format baseline** measured at c=1? Without it you can't tell load
   damage from a model that never followed instructions.
2. **Unjudged sessions** — how many? A rising unjudged rate means the judge
   is struggling, not necessarily the agent.
3. **Timeouts** — any? Levels after a timeout wall may be contaminated by
   abandoned work still running server-side.
4. **Sample counts** — enough for the percentiles you're reading?
5. **Repetitions** — is this one run or three? One run is an anecdote.
