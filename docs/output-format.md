# Output format

Kneepoint's results are plain JSONL. The CLI is free forever and this format is
open: build your own dashboards, regression gates and analyses on it without
asking anyone. This page is the contract that makes that safe.

## The files

A run writes four files into `--out` (default `reports/`), all named from one
run id, and prints all four paths when it finishes:

| File | One line per | Model |
|---|---|---|
| `run-<id>.jsonl` | request **attempt** | `RequestRecord` |
| `run-<id>-sessions.jsonl` | simulated user **session** | `SessionRecord` |
| `run-<id>-meta.json` | — (one JSON document: what the run *was*) | `RunMetadata` |
| `run-<id>-report.html` | — | self-contained report |

`kneepoint demo` writes the same shapes as `demo-<id>*`.

The report is derived from the other three and can be rebuilt from them at any
time — see [Re-rendering the report](#re-rendering-the-report).

In the two JSONL files every line is one complete JSON object. There is **no
header line** — the first line is a record like any other, so `tail -f`,
`split`, `grep` and partially-written files all behave. Everything about the run
that is not a request or a session — target, ramp, seed, chaos profile, prices —
lives in the metadata sidecar instead, described [below](#run-id-metajson-what-the-run-was).

## Versioning

Every record carries its own version stamp:

```json
{"schema_version": 1, "kneepoint_version": "0.2.0", "session_id": "...", ...}
```

* **`schema_version`** *(int)* — the format contract below. **Absent means 0**:
  every file written before 2026-08-12 predates versioning. Read it as 0 and
  keep going; do not fail.
* **`kneepoint_version`** *(string, nullable)* — the release that produced the
  line. `null` when kneepoint ran from a source tree rather than an install.

The metadata sidecar carries the same two fields at its top level; the three
data files of one run always share one `schema_version`. (The HTML report is
not a data file: it shows `kneepoint_version` in its metadata block and
carries no `schema_version`.)

It is on every line rather than in a header because a JSONL consumer rarely sees
the whole file: it tails it, splits it, or reads it while it is still being
written. A version that only exists on line 1 isn't there when you need it.

### The compatibility promise

Within a `schema_version`, kneepoint **may**:

* add new optional fields (always with a default, always documented here);
* populate a field that was previously always `null`;
* add new values to a free-text field such as `error`.

Within a `schema_version`, kneepoint **will not**:

* remove a field, rename one, or change its type;
* change the meaning or units of an existing field;
* change what makes `ok` true.

Anything in the second list bumps `schema_version`. So: **read by field name,
tolerate unknown fields, and treat a higher `schema_version` than you know as a
reason to warn, not to crash.**

## `run-<id>.jsonl` — one line per request attempt

A session is many turns; a turn is one or more attempts, because a failed turn
is retried. **Every attempt gets a line**, including the ones that failed, which
is what makes retry waste and retry amplification measurable.

The fields, in the order they appear on the line:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | 0 for pre-versioning files |
| `kneepoint_version` | string \| null | null when run from source |
| `session_id` | string | joins to `SessionRecord.session_id` |
| `concurrency` | int | the ramp level this attempt ran at |
| `started_at` | float | unix epoch seconds, wall clock |
| `ttft_ms` | float \| null | time to first *content* chunk. `null` when no content chunk arrived before the request ended — an HTTP error, a failure during the silent prefill gap, or a stream that only ever carried usage. A request that failed *after* its first content chunk keeps its `ttft_ms` |
| `total_ms` | float | full request duration, client-side — from just before the request was sent to the moment the record was made, whether the request succeeded, failed or was abandoned |
| `input_tokens` | int \| null | from the provider's `usage` on a successful request. `null` when the provider didn't send one — kneepoint never estimates tokens — and always `null` when `ok` is false |
| `output_tokens` | int \| null | as above |
| `ok` | bool | true only when the response status was below 400 **and** the stream ran to its end (`[DONE]` or the body closing). A stream that broke off is `ok: false` |
| `error` | string \| null | free text, truncated to 200 chars. Shape is not contractual. `null` whenever `ok` is true |
| `turn_index` | int | 0-based position within the session |
| `attempt` | int | 1-based. `> 1` means this line is a retry |
| `fault` | string \| null | chaos fault injected into this attempt, read from the response: `llm_rate_limit`, `llm_server_error`. Tool faults arrive via the proxy's fault log, not here |
| `status_code` | int \| null | the HTTP status the server sent, whatever it was — on a successful stream that is the real 2xx, not an assumed 200. `null` when no response arrived at all (connection refused, DNS, a wall reached before any status) |
| `itl_mean_ms` | float \| null | mean gap between successive content chunks. `null` when `chunk_count < 2` — one chunk defines no gap — and always `null` when `ok` is false |
| `itl_p99_ms` | float \| null | p99 of the same gaps, inclusive method. Equals the mean when there is exactly one gap. `null` under the same conditions |
| `chunk_count` | int \| null | content-bearing SSE chunks. A usage-only chunk is not counted. `null` when `ok` is false, even if chunks had arrived |
| `abandoned` | bool | **true when the client gave up on a request the server had begun answering** — the read-gap timeout, or the total wall once a response status had arrived. The server may still have been generating. A wall reached while still waiting for a status, and a connection that never landed, are errors but *not* abandonment: nothing shows that work started upstream. See [Measurement integrity](measurement-integrity.md) |

Fields that exist only on a successful request (`input_tokens`,
`output_tokens`, `chunk_count`, `itl_*`) are `null` on a failed one even when
the provider had sent some of them. A partial answer's tokens are not spend
that produced an answer, and a broken stream's chunk gaps would describe the
break, not the server.

### `itl_*` is inter-*chunk*, not strictly inter-*token*

A server decides for itself how many tokens go in one SSE chunk, so the gap
between chunks is the stream's flush cadence and only equals inter-token latency
when every chunk carries one token. Compare `chunk_count` to `output_tokens` on
the same line: when they are close, `itl_mean_ms` is inter-token latency in the
strict sense; when `output_tokens` is several times `chunk_count`, the server is
batching tokens per chunk and `tpot_ms` — per *reported* token — is the number
that tracks decode speed. Kneepoint names the field for what it measures rather
than for what you might want it to be. [Latency metrics](book/latency.md) in
the Book shows the two diverging on a real server.

### `tpot_ms` is derived, not stored

Time per output token is one division away from fields already on the line, so
storing it would grow every record and let a stored value drift from its inputs.
The expression, guards included:

```python
None if ttft_ms is None or not output_tokens else (
    (total_ms - ttft_ms) / output_tokens if total_ms - ttft_ms > 0 else None
)
```

That expression, guards and all, is what any consumer of this file should use.
Kneepoint pins `RequestRecord.tpot_ms` against an independent implementation of
it in the test suite, so the published formula cannot silently drift from the
shipped one. `output_tps` is `1000 / tpot_ms`.

### Why so many nullables

Because kneepoint refuses to invent numbers. `null` always means *not measured*,
never *zero*. A `ttft_ms` of `null` is a request that never produced content; an
`output_tokens` of `null` is a provider that didn't report usage. Treating either
as 0 will silently corrupt any average you compute.

## `run-<id>-sessions.jsonl` — one line per session

| Field | Type | Notes |
|---|---|---|
| `schema_version`, `kneepoint_version` | | as above |
| `session_id` | string | joins to the request lines |
| `concurrency` | int | ramp level |
| `started_at` | float | unix epoch seconds |
| `total_ms` | float | wall time of the whole conversation, think time included |
| `turns_requested` | int | how many turns this simulated user intended |
| `turns_completed` | int | how many got a successful answer |
| `ok` | bool | `turns_completed == turns_requested` |
| `faults` | string[] | every fault this session met, one entry per attempt that took one: LLM-path faults from the request lines' `fault`, and tool-path faults after a `--fault-log` merge (`kneepoint demo` merges in-process). Repeats are meaningful: two entries means it was hit twice |
| `transcript` | object[] | `{"role", "content"}` messages, alternating `user`/`assistant`, for the accepted turns only. A turn that never got a successful attempt is not in here — neither its prompt nor any partial answer |
| `resolved` | bool \| null | **`null` means not judged**, not "unresolved". Any resolution rate must exclude nulls from the denominator |
| `resolution_method` | string \| null | `deterministic` or `llm_judge`; `null` when not judged |

Sessions are written once, after judging, so verdicts are in the file. Request
lines are written per level, as the run proceeds, so the file can be tailed and
read while the run is still going.

## `run-<id>-meta.json` — what the run was

One JSON document (indented, not JSONL), written after the sessions file and
before the report. It holds everything a report or a re-analysis needs that is
not on a request or session line — the things you could otherwise only *infer*
from the JSONL (the ramp), or not recover at all (the hold, the seed, the price
rates, whether chaos was on when no fault happened to fire).

```json
{
  "schema_version": 1,
  "kneepoint_version": "0.2.0",
  "run_id": "20260818-211202",
  "command": "run",
  "target": "http://127.0.0.1:8000/v1",
  "model": "my-agent",
  "ramp": {"start": 1, "stop": 13, "step": 3},
  "hold_seconds": 15.0,
  "turns": {"min": 1, "max": 2},
  "retry": {"max_attempts": 3, "backoff_s": 0.5},
  "seed": 0,
  "chaos": {
    "profile": "standard",
    "faults": [
      {"type": "llm_rate_limit", "probability": 0.02, "target": "*"},
      {"type": "llm_server_error", "probability": 0.01, "target": "*"},
      {"type": "tool_timeout", "probability": 0.05, "target": "*"},
      {"type": "tool_malformed_json", "probability": 0.02, "target": "*"}
    ]
  },
  "price": {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "max_spend": null},
  "min_samples": 10,
  "min_group": 10,
  "started_at": 1787067705.472,
  "finished_at": 1787067722.828,
  "environment": {"python": "3.14.6", "platform": "macOS-26.6.1-arm64-arm-64bit-Mach-O"}
}
```

| Field | Type | Notes |
|---|---|---|
| `schema_version`, `kneepoint_version` | | as above |
| `run_id` | string | the `<id>` in all four filenames |
| `command` | string | `run` or `demo` |
| `target` | string | base URL the requests went to |
| `model` | string | model name sent to the target |
| `ramp` | object | `start`, `stop`, `step` — the levels are `range(start, stop + 1, step)`, plus `stop` if the step skipped it |
| `hold_seconds` | float | hold time per level |
| `turns` | object | `min`, `max` turns per session |
| `retry` | object | `max_attempts` per turn, `backoff_s` between attempts |
| `seed` | int | sampling and chaos seed |
| `chaos` | object | `profile` is the name the run was given (`off`, `standard`, `custom`, `demo`); `faults` is the full list that could fire, each `{type, probability, target}` — `type` is one of `llm_rate_limit`, `llm_server_error`, `tool_timeout`, `tool_malformed_json`; `target` is a tool-name glob, `*` unless a `custom` profile narrowed it, and only meaningful for tool faults. Stored in full so a `custom` profile is reproducible from this file alone, and so a chaos run in which no fault happened to fire is distinguishable from `off` — the request lines cannot tell them apart. `off` always carries an empty list. Tool faults only fire through the proxy |
| `price` | object \| null | `input_per_mtok`, `output_per_mtok` in USD, `max_spend` (nullable). **`null` means cost tracking was off**, not that the run was free |
| `min_samples` | int | fewest requests a level needs before its percentiles enter the knee math |
| `min_group` | int | fewest faulted / clean sessions the resilience grid will score |
| `started_at` | float | unix epoch seconds, taken **before the first request** of the ramp is sent (the cost calibration request is not part of the run) |
| `finished_at` | float | unix epoch seconds, taken as this file is written — after the ramp has finished and the sessions have been judged, before the report is rendered |
| `environment` | object | `python` version and `platform` string, read from the machine that ran it |

Timestamps are epoch floats like the JSONL's `started_at`, and there is no
second, human-formatted copy of them — one representation, nothing to drift.

Files written before the sidecar existed — everything the 0.1.0 PyPI release
ever wrote — have none. Anything that reads one must treat *absent* as
**unknown**: not as `off`, not as zero, not as the defaults.

## Re-rendering the report

```
kneepoint report reports/run-<id>.jsonl [--out FILE] [--price-in N --price-out N] [--min-samples N]
```

The records file is the handle; the sessions file and the metadata sidecar are
found beside it by name, everything is re-aggregated with the same functions the
run used, and the HTML is written to `run-<id>-report.html` (or `--out`). With
the sidecar present the result is **byte for byte** the report the run wrote —
so a report that was deleted, or that failed to render, is not lost, and a run
can be re-rendered after the report itself improves.

Without the sidecar — every file from before it existed — nothing is defaulted:
target, model, ramp, chaos and start time render as *unknown*, the cost section
says the price rates are unknown, the resilience section says whether chaos ran
and the `min_group` threshold are unknown, and the knee math applies the
command's own `min_samples` (10) and says so. `--price-in`/`--price-out` and
`--min-samples` are the only way to supply those; there is no override for
chaos, because nothing on disk can say which faults *could* have fired.
Overrides always win over the sidecar, so a run priced at one rate can be
re-costed at another.

## Deriving the headline metrics

So third-party tools get the same numbers kneepoint prints:

* **p95 per level** — group by `concurrency`, keep `ok == true`, take
  `total_ms`. Kneepoint uses `statistics.quantiles(..., n=100,
  method="inclusive")`, which differs slightly from a naive nearest-rank p95;
  a level with a single successful request reports that request's value. A
  level with no successful request has no percentiles at all and is left out.
* **TPOT / ITL** — see the two sections above; `itl_*` is per chunk, `tpot_ms`
  is per reported token, and they only coincide when `chunk_count ≈
  output_tokens`.
* **the knee** — among levels with at least `min_samples` requests, the first
  whose p95 reaches 2× the curve's floor (the lowest p95 among those levels).
  See [The three metrics](metrics.md) for the confidence range around that
  threshold, why a crossing at the last level is only a lower bound, and why
  Kneedle is a cross-check rather than the answer.
* **resolution rate** — `resolved == true` over `resolved != null`.
* **retry waste** — spend on lines with `ok == false`, over total spend.
* **contamination** — if any line has `abandoned == true`, that level and every
  level after it are not independent measurements. Do not publish them.

## Reading old files

Everything kneepoint has ever written still parses. Records are additive-only
within a version, and every field added since the first release has a default.
`schema_version: 0` files simply lack the fields introduced later — read them,
and treat the missing ones as `null`.
`kneepoint report` reads them too, and renders what they never recorded as
*unknown* rather than as today's defaults.
