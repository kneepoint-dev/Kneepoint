# Scenario reference

A scenario is one YAML file, validated strictly — unknown top-level keys are
rejected, and `kneepoint validate my.yaml` checks a file offline. Where a
corresponding flag exists, it takes precedence over the scenario and defaults.

## `trace`

Use `trace: {propagation: sampled}` (the default) to send W3C
`traceparent` with flag `01` and send `baggage`. Choose `unsampled` to
send flag `00` with baggage, or `off` to omit propagation and let the
target make its own sampling decision. All three modes store local trace
and span IDs. `--trace-propagation sampled|unsampled|off` overrides the
scenario setting. The legacy `trace: true` selects sampled propagation.
`trace: false` and `--no-trace` still omit both headers and local IDs.
`--no-trace` takes precedence when both CLI flags are supplied.

## `client`

Both target adapters and the chaos proxy send `kneepoint/<version>` as their
User-Agent. Set `client: {user_agent: my-load-test/1}` to override it. The proxy
reads this setting when invoked with `--scenario`. An explicit
`target.headers.User-Agent` takes precedence for the HTTP adapter.

## `target`

The default type is `openai-compatible`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `type` | `openai-compatible` | `openai-compatible` | select this adapter or use `http` below |
| `url` | string | *(required)* | base URL of the endpoint, e.g. `http://127.0.0.1:8000/v1` |
| `model` | string | `mock` | model name sent in the request body |
| `auth_env` | string | — | name of an env var holding a bearer token (the token itself never lives in the file) |

### HTTP targets

Select `target.type: http` to load test an HTTP endpoint with its own JSON or
SSE contract. See [Mapping common agent servers](http-targets.md) for complete
scenarios.

| Field | Meaning |
|---|---|
| `url` | Full HTTP(S) URL, including the route. `--target` overrides it. Userinfo and fragments are rejected. |
| `method` | `POST` by default. Also accepts `GET`, `PUT`, `PATCH`, and `DELETE`. |
| `headers` | Optional string values. HTTPX supplies host and body framing headers. |
| `auth_env` | Optional env variable for Bearer authentication. Use either this or a configured Authorization header. |
| `body` | Structured YAML value sent as JSON. Omit for no body. Explicit `null` sends JSON null. GET requires an omitted body. |
| `response` | Reply mapping with `mode: json` or `mode: sse` and a required `text_path`. |
| `success_status` | Optional list of accepted 2xx status codes. Defaults to all 2xx. Redirects are disabled. |
| `success` | Optional response-body success rule. |
| `capture` | Optional response field to reuse within a session. |

URL and header values can reference environment variables. For example,
`Authorization: "Bearer ${AGENT_API_KEY}"` reads the token at validation or
run time. Missing or empty variables fail before dispatch. Use `$${` for a
literal `${`. Expansion is single pass. URL values must be URL-encoded where
needed. Saved target URLs retain configured env references and omit query
strings. HTTP targets take no `model` field or `--model` flag.

In `body`, `{prompt}` is the current input, `{session_id}` is stable across
the session, and `{turn_index}` starts at zero. A standalone `{turn_index}`
becomes an integer. A standalone `{history}` becomes a JSON array of
`{role, content}` messages including the pending user turn. Placeholders
apply only to values. Escape literal braces with `{{` and `}}`. For multiple
turns, supply `{history}` or combine `{prompt}` with `{session_id}` or a capture
placeholder for server-side state. A server that mutates session state should deduplicate retries using
the session ID and turn index.

Response paths use JSON Pointer. `""` selects the whole payload,
`/choices/0/text` traverses an array, and `~1` and `~0` escape slash and tilde
in keys. JSON mode requires a string at `text_path` and reads up to 16 MiB of
decoded body. In SSE mode, `event` optionally selects a named event and
`text_path` selects string deltas in its JSON data. Events are limited to
1 MiB. Use `end: {data: "[DONE]"}` or `end: {event: done}` to require an end
marker. Otherwise a clean EOF ends the stream.

Set `error_path` in JSON mode to fail on a non-null application error.
In SSE mode, `error_event` selects an event that fails and closes the stream,
and `error_path` can select its diagnostic value. The original 2xx status
is kept, so the existing retry policy treats this as a terminal turn failure.
For object errors, diagnostics use the first string field among `type`, `code`,
and `message`, in that order. String errors use their first 80 characters.
Other values use JSON type names such as `object`, `array`, or `boolean`.
The complete `AgentError: ...` label is limited to 120 characters. Configured
secrets are redacted.

With `success`, set `path` to a JSON Pointer and supply exactly one nonempty
list, `equals` or `not_equals`. The first accepts any listed value and the
second rejects any listed value. List entries are JSON scalars, including
`null`. Boolean and numeric values compare separately. A missing path
fails either comparison. In SSE mode the default selection is the last JSON
data event containing the path. Set `event_filter: {event: done}` to use the
last JSON data event with that SSE name, or
`event_filter: {path: /event, equals: workflow_finished}` to select by a field
in the JSON data. The configured end marker bounds selection and its JSON
data is eligible.

HTTP status is checked first. A configured application error takes
precedence over a failed success rule. A rule failure keeps the received
HTTP status, records `body_failure`, and ends the turn without retrying.
Use a public status field for the success rule. The diagnostic shows a
bounded preview of scalar values and the type of objects or arrays.
Configured secrets and matching captured values are redacted.

With `capture: {name: conversation_id, path: /conversation_id, from: json}`,
reuse the field as `{{capture.conversation_id}}` in body and header values.
Use `from: sse_event` for the last usable value from an SSE JSON data event.
Captures accept nonblank strings, numbers, and booleans. A capture that
occupies an entire body value retains its JSON type. Embedded values and
headers use text substitution. The first turn substitutes an empty string.
After a successful turn, a missing or unusable capture clears any previous
value and adds a `capture_missing` warning. A later turn that references the
missing capture fails locally with error kind `capture_missing` and does
not retry. Each session keeps its own capture.

Use optional `response.usage.input_path` and `output_path` for token counts.
For SSE, `response.usage.event` can select a separate usage event. Counts are
nonnegative integers. Missing usage remains unknown, and SSE counts are
cumulative snapshots. TTFT and inter-chunk timing require SSE. Both modes
record end-to-end latency and use the same request wall and generator retry
policy.

## `workload.ramp`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `from` | int ≥ 1 | `1` | starting concurrency |
| `to` | int | `50` | final concurrency (must be ≥ `from`) |
| `step` | int ≥ 1 | `5` | concurrency increment between levels |
| `hold_seconds` | float ≥ 0 | `15.0` | how long each level holds — longer = more samples = smoother curves |

## `workload.conversation`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `turns.min` / `turns.max` | int | `1` / `1` | ordered inclusive range of turns per session, sampled uniformly |
| `corpus` | glob | bundled prompts | regular `.txt`, `.md`, `.json`, and `.jsonl` files, resolved relative to the scenario directory |
| `think_time_ms.min_ms` / `.max_ms` | int | `0` / `0` | ordered range for simulated user pauses between turns |
| `retry.max_attempts` | int ≥ 1 | `3` | attempts per turn; 429 `Retry-After` is honored |
| `retry.backoff_s` | float | `0.5` | base backoff between attempts |

Text and Markdown files provide one prompt per file. JSON files contain an array
of prompt strings or ranked item objects. JSONL files provide one prompt string or
ranked item object per line. Scenario corpus files must resolve
inside the scenario directory, including symlink destinations. For trusted
external directories, pass `--allow-corpus-outside` to `validate` or `run`.
An explicit `run --corpus` flag remains relative to the current directory.
Before making a request, `run` prints the corpus directories and the names of
environment variables read by the scenario.

!!! note "Retries are load, and the report says how much"

    Retrying a failed turn is what a real client does, so 3 attempts is the
    default. It also means the generator issues **more** requests exactly when
    the target is already struggling — measured on Run D, one level fired 21
    requests through 7 workers and lost all of them, so a ramp can partly
    manufacture the knee it measures.

    `kneepoint run` prints the worst level's amplification and the report has a
    per-level table. Set `retry.max_attempts: 1` to measure capacity with the
    generator's own contribution removed; running both and comparing is the
    honest way to size the effect.

`workload.sessions` is accepted and validated but reserved (n-run variance
mode, on the roadmap).

## `resolution.check` — deterministic judging

| Field | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `contains` \| `regex` | `contains` | how to test the session's final answer |
| `value` | string | *(required)* | the marker or pattern that means *resolved* |

## `resolution.judge` — sampled LLM-as-judge

| Field | Type | Default | Meaning |
|---|---|---|---|
| `base_url` | string | *(required)* | OpenAI-compatible judge endpoint |
| `model` | string | *(required)* | judge model |
| `api_key_env` | string | `KNEEPOINT_JUDGE_API_KEY` | env var holding the judge's key |
| `sample_rate` | float | `0.2` | fraction of sessions judged (seeded, reproducible) |
| `rubric` | string | built-in support rubric | what *resolved* means for your domain |
| `timeout_s` | float | `30.0` | per-verdict timeout |

Only the sampled subset is judged, and judge failures never kill a run — a
session whose verdict errored stays unjudged rather than counting either way.

## `cost`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `input_per_mtok` | float | `0.0` | $ per million input tokens |
| `output_per_mtok` | float | `0.0` | $ per million output tokens |
| `max_spend` | float | — | observed spend threshold: stop admission at equality or unknown usage; drain active calls (exit code 3) |

Prices and the cap must be finite and nonnegative. A capped run requires both
price fields explicitly (or their CLI flags); explicit zero is valid, while an
omitted field does not declare free usage. Capped runs skip calibration and
reject `resolution.judge` before load until judge cost is accounted for.
Deterministic checks remain available. Active attempts can overshoot the cap;
`--force` does not bypass runtime control. Calibration and judging are outside
completed generator attempt accounting. See [cost control](book/cost.md).

## `chaos`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `profile` | `"off"` \| `standard` \| `custom` | `"off"` | **quote `"off"`** — bare `off` is YAML for `false` |
| `faults` | list | `[]` | required (non-empty) when `profile: custom` |

Each fault: `{type: <fault>, probability: <0..1>}` where `type` is one of
`llm_rate_limit`, `llm_server_error`, `tool_timeout`, `tool_malformed_json`.
See [Chaos testing](chaos.md) for the standard profile's probabilities.

## `slo`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `p95_total_ms` | float | — | max successful-request p95 across observed levels (not a pooled percentile) |
| `min_resolution_rate` | float | — | min fraction of sessions resolved; requires full verdict coverage |
| `max_cost_per_resolved` | float | — | max $/confirmed resolution |

Thresholds must be finite: latency and cost are nonnegative; resolution is in
`[0, 1]`. Equality passes. Each configured check reports **pass**, **fail** (a
measured breach), or **unknown** (the threshold cannot be verified). Both failed
and unknown checks cause `run` to exit **1**; budget control stops retain **3**.
An empty `slo` block produces no checks and no invented pass.

Latency uses the worst observed successful-request per-level p95, without pooling
or adding a sample-size policy. No p95 is unknown. If some observed levels lack a
valid p95, a known breach still fails; otherwise the check is unknown. Resolution
requires verdicts for every recorded session; partial judgment is unknown. Cost
per confirmed resolution covers completed generator attempts only, excluding
calibration and judging. Missing prices, incomplete token usage, or no confirmed
resolutions make that check unknown. Non-finite measurements are unknown.

Checks cover observed records and do not certify that the configured ramp
completed. Declared thresholds are saved in metadata; HTML re-renders recompute
checks from stored evidence and any price overrides. `kneepoint report` retains
rendering-only exit semantics.

## Top level

| Field | Type | Default | Meaning |
|---|---|---|---|
| `seed` | int | `0` | seeds prompt sampling, turn counts, and chaos decisions for reproducible runs |

## Full example

```yaml
target:
  url: http://127.0.0.1:8000/v1
  model: my-agent

workload:
  ramp: {from: 1, to: 50, step: 5, hold_seconds: 20}
  conversation:
    turns: {min: 1, max: 3}
    corpus: ./prompts/*.txt

resolution:
  check: {kind: contains, value: "[RESOLVED"}

cost:
  input_per_mtok: 3.00
  output_per_mtok: 15.00
  max_spend: 0.50

chaos:
  profile: standard

slo:
  min_resolution_rate: 0.90
```

Check any file with `kneepoint validate my-scenario.yaml`.
