# The three metrics

## The knee point

Fit p95 latency against concurrency; the knee is where the curve stops being
flat and starts climbing. Two methods, and **both are always reported** — but
only one of them is ever the headline:

* **`p95_doubling`** — the first level whose p95 reaches 2× the curve's floor.
  This is kneepoint's definition of the knee and the number it leads with: it
  has a stated threshold and a ratio you can check by hand from the raw JSONL.
* **`kneedle`** — maximum curvature, as corroboration only. It is refused when
  it lands on the top of the ramp, which is what it does on a near-linear curve;
  that answer is the ramp's own upper bound, not a property of your system. When
  it disagrees, that lowers confidence in the headline — it never replaces it.
  (Measured: Kneedle answers on 7 of Run D's 15 repetitions and reads higher on
  every one, 5/6/5/4/5/5/6 against p95-doubling's 2 or 3.)

Levels with fewer than `--min-samples` successful requests are excluded, and
when there's no trustworthy knee, kneepoint says so instead of inventing one.

### Confidence, and why the knee is sometimes a range

A hard threshold makes near-misses look like measurements. Measured on Run D:
three repetitions of one scenario returned knees 3, 2, 2 because the deciding
p95 ratio was 1.85, 2.48, 2.63 — straddling 2.0. Nothing about the machine
changed; the detector had no way to say "this one was close."

So a level whose ratio lands within **±15% of the threshold** (1.70×–2.30×) is
treated as a coin flip, and the knee comes back as a range:

```
Knee point: 2-3 concurrent sessions, low confidence (plan for 2)
  kneedle: none - returned 6, the top of the ramp - what it does on a
           near-linear curve. Widen the ramp rather than believing it
  p95_doubling: 3
  p95 vs curve floor: c=1 1.00x, c=2 1.85x*, c=3 2.71x, ...   (* within 15%)
  the deciding ratio at c=2 is 1.85x ... a repetition of this run can land
  either side of it
```

**Plan for the low end.** Anything that has to pick one integer — the SLO gate,
`find_knee()`, the chart marker — takes the bottom of the range. Being under the
knee costs headroom; being over it costs everyone's latency at once.

A single number with `high confidence` means `p95_doubling` crossed cleanly, no
level sat near the threshold, Kneedle did not contradict it, **and the crossing
was not the last level of the ramp**. Anything else is a range or a lowered
confidence. Kneedle answering while `p95_doubling` found nothing is **not a
knee** — p95 never doubled, so by kneepoint's own definition nothing broke; the
curvature is reported as a note.

### A crossing at the top of the ramp is a lower bound

When p95 first reaches 2× at the *last* level measured, the crossing is real
but the knee is not located. Every level below stayed under 2× — that much is
measured — but nothing above exists to show whether the curve broke there or is
still climbing gently (a linear curve reaches 2× eventually without a knee at
all). It is the same non-answer Kneedle is refused for, on the method that
leads, so it is never printed as a bare number:

```
Knee point: 13 concurrent sessions or beyond, low confidence (plan for 13;
  p95 first doubled at the top of the ramp - widen it past 13 to find the knee)
  p95_doubling: 13 - first doubled at 13, the top of the ramp - the knee is
           there or beyond, and the ramp was too short to tell which. Widen it
```

The number is kept — "not below 13" is a measurement and is what `find_knee()`
and the chart marker use — but confidence is `low` and the fix is a wider ramp,
not a repetition.

Replaying Run D's fifteen repetitions through this: every headline now lands in
2–4 and matches the project's independently verified `p95_doubling` figures
repetition for repetition, where the old headline reached 7. Three of the five
model configs pass the three-repetition agreement gate; two genuinely disagree
across repetitions and are reported as such.

Why it matters: agents don't degrade linearly. Below the knee you have
headroom; above it every added session makes *everyone* slower — a closed
queueing system hitting its service capacity.

A ramp level that abandoned a request — and every level after it — is flagged
`contaminated`, because abandoned work can still be running on the server while
the next level is measured. See
[Measurement integrity](measurement-integrity.md).

## $ / resolved task

`total spend ÷ sessions judged resolved`. Not per token, not per request:
a retried request costs real money and resolves nothing, and a cheap answer
that didn't solve the task is 100% waste. The report also splits out
**retry waste** — spend on attempts that failed (a 429'd attempt on a
provider that doesn't bill rejections is $0; a timeout after 4,000 output
tokens is not).

When nothing resolved, the metric is honestly `n/a` — division by zero is a
lie, not a number.

## Resilience score

`resolution rate of faulted sessions ÷ resolution rate of fault-free
sessions × 100`, capped at 100, within a single chaos run — probabilistic
injection leaves most sessions clean, so the control group comes free. Both
groups need ≥10 judged sessions (the per-fault grid rows need ≥10 hits each)
or the score is `n/a` with advice to run longer holds.

Per-fault verdicts: **pass** ≥95% of the clean rate, **degraded** ≥70%,
**fail** below.
