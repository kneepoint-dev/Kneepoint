# Quality under load

A web server either returns 200 or it doesn't. An agent can return 200 and be
useless. Quality is a load metric, and this is the chapter that separates
agent performance testing from every other kind.

## `resolution_rate` — the agent-era error rate

**Definition:** the share of *judged* sessions where the agent actually
accomplished the user's task.

Here "judged" is the legacy field name for **evaluated sessions with an explicit
verdict**, from a deterministic check, external LLM judge or local execution-failure
rule. Sessions with no verdict are excluded from this denominator, never silently
counted as failures. The report and CLI also show all recorded sessions, completed
sessions (`SessionRecord.ok`: every turn eventually succeeded, possibly after
retries), failed/incomplete sessions, and resolved, unresolved and unjudged counts.
These session completion counts are distinct from request-attempt error rates.

**Verdict coverage** is evaluated/all. **Confirmed resolved/all** is resolved/all.
For two resolved and eight unjudged sessions, the judged-only rate is 100%, coverage
is 20%, and confirmed resolved/all is 20%. The remaining eight outcomes are unknown;
the confirmed share is not an estimate of their outcomes. Empty populations have
null rates. All-unjudged levels remain visible with null judged-only rates.

A configured whole-run `min_resolution_rate` SLO requires full verdict coverage.
Partial coverage produces an unknown result and a nonzero exit, even if the
judged-only rate exceeds the threshold. This is separate from a measured rate
below the threshold. Full coverage does not certify judge accuracy or provide
statistically calibrated confidence.

**Why it's the headline quality metric:** HTTP status tells you the plumbing
worked. Resolution tells you the product worked.

## How judging works

Two mechanisms, both running *after* the load test on recorded transcripts —
so neither adds latency or load to what's being measured.

**Deterministic (marker).** The scenario declares a marker, e.g. a reply
containing `[RESOLVED]`. The judge is a string check: objective, free,
instant. Its limit: it proves the agent *claimed* success, not that it
succeeded.

**LLM-judge.** A separate model reads a seeded sample of successful session
transcripts with the configured rubric and returns a boolean `resolved` verdict.
When this policy is configured, supplied terminal failed sessions are explicitly
unresolved with method `execution_failure`, even with an empty or success-looking
transcript. This local rule makes no external judge request and is not subject to
sampling. Only successful executions with transcripts enter the external sample.
The CLI omits interrupted sessions from both judging paths, leaving them unjudged.

Why the narrow question matters: language models are far more reliable as
binary verifiers than as open-ended scorers. "Did this succeed?" is a much
safer thing to ask than "rate this 1–10".

Omitted samples, judge outages and invalid or non-boolean answers create no new
verdict; existing deterministic verdicts remain if both mechanisms are configured.
Valid LLM verdicts replace deterministic verdicts for the selected successful
sessions. The report's method counts describe the final stored verdicts, not
external API call counts or agreement between judges. Check coverage and method
mix at each level before interpreting a change in the quality curve.

## `format-following baseline` — the ceiling

Before measuring resolution under load, measure it at **concurrency 1 with no
chaos**. That's the model's best possible score: no queueing, no faults, no
pressure.

This number is the ceiling for everything that follows, and it separates two
very different failures:

- Baseline 100%, under load 78% → **the system broke the agent.** A real
  capacity finding.
- Baseline 75%, under load 72% → **the model never followed the format.**
  Nothing to do with load. Fix your prompt, not your infrastructure.

Skipping the baseline is how people publish "our agent degrades under load"
when in fact it never worked properly at all. Run D's baselines ranged from
75% to 100% across four models on identical scenarios — the spread is real
and large.

## Accuracy drift — the evidence people ask for

**Definition:** `resolution@concurrency 1 − resolution@concurrency N`, same
scenario, same judge, same model.

At c=1 the agent performs at its best. Everything lost at higher concurrency
is degradation *caused by load* — timeouts landing mid-reasoning, truncated
context assembly, retry storms, tool latency stacking.

This is Kneepoint's answer to "does the agent get dumber under pressure?" —
measured, not asserted. Plot resolution rate on the level axis next to the
latency curve, and mark the knee. When resolution bends at the same level as
latency — or *before* it — that one chart is the entire argument for capacity
testing agents.

!!! danger "Drift needs repetitions too"
    Run D produced a spectacular drift curve in one repetition — resolution
    falling 79% → 42% across levels — while the other two repetitions of the
    same configuration were flat. A single-run drift chart is not evidence.
    Apply the same three-repetition discipline you apply to knees.

## What Kneepoint quality metrics do *not* cover

Resolution answers "did it complete the task". It does not measure factual
accuracy, hallucination, faithfulness to sources, tone, or safety. Those are
semantic evaluation — a different discipline with mature tools (LangSmith,
Langfuse, Arize, and others).

The dividing line worth remembering: **if it scores what the agent said,
that's evals. If it measures what load did to the agent, that's Kneepoint.**
The two are complementary — the interesting future is running your existing
evaluators *under load*, so their scores gain a concurrency axis.

## Related session metrics

- **`turns_completed` vs `turns_requested`** — sessions that ended early.
  A gap means conversations are being abandoned, often a timeout symptom.
- **`transcript_chars`** — how much text the agent produced per session.
  Useful as a control: if resolution drops while transcripts get shorter,
  the agent is being cut off rather than being wrong.
