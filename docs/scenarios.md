# Scenario reference

A scenario is one YAML file, validated strictly — unknown top-level keys are
rejected, and `kneepoint validate my.yaml` checks a file without running
anything. Every value below can be overridden by a CLI flag: **flag beats
scenario beats default**.

## `trace`

`trace: true` (default) sends W3C `traceparent` and `baggage` on every load
attempt and stores its trace and span IDs in the request record. Set
`trace: false` or pass `kneepoint run --no-trace` to omit the headers and store
null IDs. The CLI flag takes precedence over the scenario setting.

## `target`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `type` | `openai-compatible` | `openai-compatible` | the only target type in v0 |
| `url` | string | *(required)* | base URL of the endpoint, e.g. `http://127.0.0.1:8000/v1` |
| `model` | string | `mock` | model name sent in the request body |
| `auth_env` | string | — | name of an env var holding a bearer token (the token itself never lives in the file) |

## `workload.ramp`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `from` | int ≥ 1 | `1` | starting concurrency |
| `to` | int | `50` | final concurrency (must be ≥ `from`) |
| `step` | int | `5` | concurrency increment between levels |
| `hold_seconds` | float | `15.0` | how long each level holds — longer = more samples = smoother curves |

## `workload.conversation`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `turns.min` / `turns.max` | int | `1` / `1` | turns per session, sampled uniformly; history grows every turn |
| `corpus` | glob | bundled prompts | `*.txt` files, one prompt per file — **resolved relative to the scenario file**, so scenario directories are copy-anywhere (the CLI `--corpus` flag stays CWD-relative) |
| `think_time_ms.min_ms` / `.max_ms` | int | `0` / `0` | simulated user pause between turns |
| `retry.max_attempts` | int | `3` | attempts per turn; 429 `Retry-After` is honored |
| `retry.backoff_s` | float | `0.5` | base backoff between attempts |

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
