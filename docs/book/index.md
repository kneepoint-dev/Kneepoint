# The Kneepoint Book

A working engineer's guide to measuring AI agents under load.

This book exists because the vocabulary of AI-system performance is scattered
across inference-server docs, SRE practice, and eval platforms — and none of
them explain how the pieces fit when the thing you're testing is an *agent*:
multi-turn, tool-using, non-deterministic, and expensive per request.

## How to read this

Read in order the first time. Each chapter assumes the previous one.

| Chapter | Answers |
|---|---|
| [Foundations](foundations.md) | What a load test *is*, percentiles, why sample count decides whether a number means anything |
| [Latency](latency.md) | TTFT, TPOT, ITL, total, session time — what the user actually feels |
| [Throughput & capacity](throughput.md) | Concurrency, RPS, goodput, sessions/hour, and the knee point itself |
| [Quality](quality.md) | Resolution rate, judges, accuracy drift — is it still *right* under load |
| [Stability](stability.md) | Errors, retries, faults, resilience |
| [Cost](cost.md) | Tokens, spend, $/resolved task |
| [Reading charts](reading-charts.md) | The three axes, chart types, correlation without fooling yourself |
| [Diagnostic recipes](recipes.md) | "p95 doubled — now what?" Step-by-step investigations |
| [Glossary](glossary.md) | Every term, one line each |

## Three rules this book never breaks

**1. A number without its sample count is a rumour.** A p99 computed from
seven requests is noise wearing a lab coat. Kneepoint refuses to plot
percentiles below a minimum sample count; this book always tells you what n
you need.

**2. "I don't know" is a valid result.** When the knee is genuinely
undetectable, Kneepoint reports it as undetectable rather than guessing. A
tool that always produces a confident number is a tool that is sometimes
confidently wrong.

**3. Measurement is a system too.** Your load generator, your network, and
your judge all have failure modes. Several chapters here exist because
Kneepoint's own measurements turned out to be wrong in ways that were only
visible from the raw data. Those stories are kept, not hidden — they're the
most useful parts.

## What you're measuring

An **agent** is not a model endpoint. A model endpoint takes a prompt and
returns tokens. An agent holds a conversation across turns, calls tools,
accumulates context, retries when things fail, and eventually either
accomplishes the user's task or doesn't.

That difference is why generic load tools mislead here. `k6` can tell you
your endpoint served 200 OK in 340 ms. It cannot tell you the agent gave up
on the user's actual problem at concurrency 9 — which is the only fact that
matters on launch day.
