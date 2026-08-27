# Scenario reference

A scenario is one YAML file, validated strictly — unknown top-level keys are
rejected, and `kneepoint validate my.yaml` checks a file without running
anything. Every value below can be overridden by a CLI flag: **flag beats
scenario beats default**.

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
| `max_spend` | float | — | hard cap: estimate-abort before the run, graceful stop mid-run (exit code 3) |

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
| `p95_total_ms` | float | — | max p95 total latency |
| `min_resolution_rate` | float | — | min fraction of sessions resolved |
| `max_cost_per_resolved` | float | — | max $/resolved task |

Any breach exits 1 with a printed reason. **An SLO set but not measured is a
breach** — a gate that silently passes because judging was off isn't a gate.

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
