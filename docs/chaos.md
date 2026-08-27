# Chaos testing

Faults inject **below** your agent's resilience layer, so its retries and
circuit breakers get exercised exactly as production would exercise them.

## The two injection points

**LLM faults** wrap kneepoint's own HTTP client, so *no infrastructure at
all*: with `chaos.profile: standard`, requests randomly become synthetic
`429` (with `Retry-After: 1`) or `503` responses before touching the network.
Kneepoint's session retries then recover — or don't — and every faulted
attempt is recorded with its fault name.

**Tool faults** need to sit between your agent and its tools. `kneepoint proxy`
runs a tiny reverse proxy; point your agent's tool URL at it and it forwards
cleanly except when a fault fires: `tool_timeout` (holds the request 30 s, then
504) and `tool_malformed_json` (200 OK, garbage body — the silent-failure
probe).

## The v0 standard profile

| Fault | Probability | Simulates |
|---|---|---|
| `llm_rate_limit` | 0.02 | provider 429s, Retry-After |
| `llm_server_error` | 0.01 | provider 5xx (stands in for `stream_cut` until that lands — see below) |
| `tool_timeout` | 0.05 | hung tool / MCP server |
| `tool_malformed_json` | 0.02 | 200 OK with garbage — the worst one |

`profile: custom` takes your own `faults:` list. Coming later: `stream_cut`,
`tool_error`, `tool_stale_data`, `slow_tokens` — each is a contained
[good first issue](https://github.com/kneepoint-dev/kneepoint/issues).

## Fault attribution

Sessions are matched to the tool faults that hit them via the
`x-kneepoint-session` header — kneepoint sends it on every request; your
agent should echo it on outbound tool calls (one line in most frameworks).

`kneepoint demo` runs the proxy in-process, so its report grids all four
fault types with nothing to configure. Everywhere else the proxy is a separate
process, and the **fault log** is what carries attribution across that boundary:

```bash
# terminal 1 — the proxy in front of your tool service
kneepoint proxy --upstream http://127.0.0.1:8000 --fault-log faults.jsonl

# terminal 2 — your agent, with its tool URL pointed at the proxy
MOCK_TOOL_URL="http://127.0.0.1:<printed port>/tool/search" python -m uvicorn ...

# terminal 3 — the run, merging the log before it scores resilience
kneepoint run --scenario kneepoint.yaml --chaos standard --fault-log faults.jsonl
```

The proxy appends one line per served fault; `kneepoint run` merges the file
into that run's sessions before judging, so the grid covers all four fault
types. **Use a fresh log per run** — entries from another run belong to session
ids this run has never seen, and are reported as unmatched rather than counted.

!!! warning "Three ways the grid can be incomplete, all of them announced"

    A resilience score computed from a partial grid is biased toward 100,
    because faulted sessions get counted into the clean baseline. So the run
    says out loud when: the log file is missing, a fault arrived with no
    `x-kneepoint-session` header (your agent isn't echoing it), or entries
    belong to a different run. If you see none of those lines, the grid is
    complete.

Without `--fault-log`, tool faults are still served and logged — they just
never reach the score, and `kneepoint run` prints a reminder saying so.
