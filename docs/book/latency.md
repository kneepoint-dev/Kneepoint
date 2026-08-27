# Latency metrics

What the user feels, decomposed. If you learn one thing from this chapter:
**total latency is a sum of parts that fail for different reasons**, and
telling them apart is most of diagnosis.

## The decomposition

For one streaming request:

```
request sent
   │
   │  ◄── TTFT (time to first token) ──►
   │      queueing + prefill + KV allocation + first forward pass
   │
   ▼ first token arrives
   │  ◄── ITL ─► ◄─ ITL ─► ◄─ ITL ─►      (gap between successive tokens)
   │    tok       tok       tok  ...
   ▼ last token
   
   ◄──────────── total_ms ────────────►
```

`total_ms = TTFT + (output_tokens × average inter-token gap)`

---

## `ttft_ms` — time to first token

**What it is:** milliseconds from sending the request to receiving the first
byte of the response stream.

**Why it matters:** it's the pause the user stares at. In a chat interface,
TTFT *is* responsiveness — everything after it feels like the system working,
but TTFT feels like the system ignoring you.

**What it contains:** queueing time (waiting behind other requests), prefill
(processing your prompt), KV-cache allocation, and the first forward pass.
Notably, **queueing dominates under load** — which is exactly why TTFT is the
most load-sensitive latency metric and rises first as concurrency climbs.

**How to read it:** plot p95 by level. A TTFT curve that stays flat then bends
sharply upward is the classic queueing signature — the server has run out of
capacity to start new work and requests are waiting in line.

!!! danger "The thinking-model trap"
    On a reasoning-capable model, TTFT includes however long the model spent
    *thinking* before emitting the first visible token. Measured on
    gemma4:12b: median TTFT of **92.7 s** with thinking on versus **635 ms**
    with `reasoning_effort=none` — same prompts, same server. Both numbers
    are real; they answer different questions. A knee chart built on
    thinking-on TTFT is a chart of reasoning length, not serving capacity.
    Always record which way the reasoning knob was set — and verify it
    actually took effect, because some models silently ignore it.

---

## `tpot_ms` — time per output token

**What it is:** `(total_ms − ttft_ms) / output_tokens`. The average
milliseconds spent producing each token after generation starts.

**Why it matters:** it measures the *decode* phase — the streaming speed. It's
bounded by memory bandwidth: to produce each token the GPU must read the
entire KV cache. That makes TPOT the metric that degrades when memory, not
compute, is the bottleneck.

**How to read it:** TPOT rising with concurrency means the server is
memory-bandwidth or KV-cache constrained — more requests are sharing the same
bandwidth. TPOT *flat* while TTFT rises means the server is queueing but
decoding at full speed once started. That single comparison is the most
useful diagnostic in this book — see [recipes](recipes.md).

**Inverse:** `output_tps` (output tokens per second) = `1000 / tpot_ms`. Same
information, friendlier units. Serving vendors quote tokens/sec; latency
engineers quote TPOT.

---

## `itl_mean_ms` / `itl_p99_ms` — inter-chunk latency

**What it is:** the gap between successive *content chunks* in the stream,
summarised as a mean and a p99, plus `chunk_count`. Kneepoint stores the
summary, not a per-chunk array — the array would multiply the JSONL for a number
nobody queries per chunk.

**Why it matters:** TPOT is an average and hides stutter. A stream that delivers
smoothly at 40 ms intervals and one that alternates 5 ms and 75 ms have the same
TPOT but feel completely different. `itl_p99_ms` is where the stutter shows.

**`null` when the request produced fewer than two content chunks.** One chunk
defines no gap, and zero would be a claim rather than a measurement.

!!! warning "A chunk is not a token — check before you read this as ITL"
    Every server decides for itself how many tokens to pack into one SSE chunk.
    Measured on Ollama with `nemotron-3-nano:4b`, five requests:

    | Chunks | Reported tokens | `itl_mean_ms` | `tpot_ms` | tokens/chunk |
    |---|---|---|---|---|
    | 66 | 175 | 15.5 | 5.8 | 2.7 |
    | 51 | 82 | 15.2 | 9.5 | 1.6 |
    | 66 | 156 | 15.9 | 6.7 | 2.4 |
    | 42 | 226 | 15.3 | 2.9 | 5.4 |
    | 53 | 222 | 15.4 | 3.7 | 4.2 |

    The inter-chunk gap is nearly constant at ~15.4 ms while the tokens inside
    each chunk vary from 1.6 to 5.4. On this server, `itl_mean_ms` is measuring
    **the stream's flush cadence, not the model's decode speed** — TPOT is the
    number that tracks decode.

    Read the two together. When `itl_mean_ms` is flat across load while `tpot_ms`
    climbs, you are looking at a fixed flush interval hiding a slowing decoder.
    When the gaps themselves stretch, the stream really is stalling. And when
    `chunk_count ≈ output_tokens`, one chunk is one token and `itl_mean_ms` is
    inter-token latency in the strict sense.

---

## `total_ms` — end-to-end request latency

**What it is:** the full wall-clock duration of one request.

**Why it matters:** it's the number Kneepoint's knee detector runs on, and
the one comparable to traditional load-testing tools.

**Its weakness:** it conflates everything. A request can be slow because the
server was busy (TTFT), because decoding was slow (TPOT), *or simply because
the model chose to write more* — a 600-token answer legitimately takes longer
than a 100-token one, with no system problem at all.

That last case is why you should check `output_tokens` alongside latency
whenever a curve moves. Plot them against each other (scatter,
request-level): if latency tracks output length tightly, your "slowdown" is
partly the model being chattier under different prompts, not the server
degrading.

---

## `session_total_ms` — how long the user's whole task took

**What it is:** start of the first turn to end of the last, for one session.

**Why it matters:** this is the agent-level duration — what a real user waits
to get their problem solved. It includes think-time between turns and every
retry. **No other load-testing tool reports this**, because no other tool
models sessions as first-class.

**How to read it:** compare it to `total_ms × turns`. A gap means time is
being spent somewhere other than model inference — tool calls, retries,
orchestration overhead.

---

## Which one do I look at?

| Question | Metric |
|---|---|
| Does the UI feel responsive? | `ttft_ms` p95 |
| Does the text stream smoothly? | `itl_p99_ms` for stutter, `tpot_ms` for decode speed |
| Is the server queueing or decoding slowly? | `ttft_ms` vs `tpot_ms`, same chart |
| How long until the user's task is done? | `session_total_ms` p95 |
| Where does the system break? | `total_ms` p95 by level → the knee |
| Is it slow, or just verbose? | `total_ms` vs `output_tokens` scatter |
