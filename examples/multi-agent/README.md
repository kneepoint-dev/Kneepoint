# LanternDesk: a controllable multi-agent test target

LanternDesk is a **fictional support desk and demo target**, not a benchmark of
LangGraph, Ollama, or any real product. Its internal bottlenecks are deliberately
planted so Kneepoint can be checked against known causes. No paid model or
external service is needed in the default `sim` mode. The server binds to
localhost and its HTTP fetch tool calls its own local documents route.

The LangGraph path is `planner → researcher → writer`: the planner is the
supervisor; researcher and writer are workers. Each node calls the selected
model backend. Researcher calls a local SQLite FTS5 `search` tool, then an HTTP
`fetch` tool at `/documents/{id}`. A grounded writer answer includes the exact
fact and `[RESOLVED kb-N]`; missing evidence produces `[UNRESOLVED]`. All 200
short articles and 300 questions are synthetic. Each `prompts/*.txt` question
has a known article ID and key fact in `answers.json`. `generate_data.py`
recreates the fixtures deterministically.

This slice accepts incoming `traceparent` without error. The graph and tools do
not propagate it or emit OTel spans yet; that is slice B. Kneepoint's public
`codex/trace-propagation` branch does send trace context to this front door.

## Start the agent

From the public repository root:

```bash
cd examples/multi-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
DEMO_PROFILE=none .venv/bin/uvicorn app:create_app --factory \
  --host 127.0.0.1 --port 8765
```

The example's dependencies stay in this folder's `requirements.txt`; the
Kneepoint package does not depend on LangGraph or FastAPI. The demo uses
LangGraph's named async nodes, which can be wrapped individually for slice B.
`GET http://127.0.0.1:8765/debug/config` reports the active settings;
`/debug/stats` reports request, model and tool counters.

Use `LLM_BACKEND=ollama` to call a local Ollama server at
`http://127.0.0.1:11434`. The default `OLLAMA_MODEL=qwen2.5:3b` is a small
local model suitable for a 24 GB Mac; pull it with `ollama pull qwen2.5:3b`
before starting the agent. Both the Ollama and fetch URLs are restricted to
local HTTP hosts. Sim mode is deterministic and requires no GPU.
For a local-only service, start Ollama with
`OLLAMA_NO_CLOUD=1 OLLAMA_HOST=127.0.0.1:11434 ollama serve`; use a model
already installed when external network access is unavailable.

## Plant a bottleneck

Set `DEMO_PROFILE` to one of the named presets in `profiles.yaml`:

| Preset | Plant | Approximate limiting capacity |
|---|---|---:|
| `none` | 32 model slots, 16 search workers, 5 ms search and fetch | no intentional limit in the 1..16 ramp |
| `search-pool` | 2 search workers, 300 ms per search | 2 / 0.300 ≈ 6.7 searches/s |
| `llm-slots` | 2 model slots, 300 ms base per model call | about 2 / (3 × 0.300) ≈ 2.2 requests/s |
| `fetch-ratelimit` | 7 fetch starts/s | about 7 requests/s |

Environment overrides named `DEMO_MODEL_SLOTS`, `DEMO_MODEL_BASE_MS`,
`DEMO_MODEL_PER_TOKEN_MS`, `DEMO_MODEL_ERROR_RATE`, `DEMO_MODEL_TIMEOUT_S`,
`DEMO_SEARCH_WORKERS`, `DEMO_SEARCH_LATENCY_MS`, `DEMO_SEARCH_ERROR_RATE`,
`DEMO_SEARCH_TIMEOUT_S`, `DEMO_FETCH_RATE_PER_S`, `DEMO_FETCH_LATENCY_MS`,
`DEMO_FETCH_ERROR_RATE`, and `DEMO_FETCH_TIMEOUT_S` override a profile. Rates
are 0..1; durations ending `_MS` are milliseconds. `DEMO_LOCAL_BASE_URL`,
`OLLAMA_URL`, and `OLLAMA_MODEL` are also configurable. Model failures return
HTTP 503. Search or fetch failures keep the request alive but make the answer
unresolved, so quality loss is visible alongside latency and throughput.
Each tool timeout includes time waiting for its pool or rate-limit slot.

## Known fan-out and grading

With the default healthy sim profile, **one request makes three model calls
and two tool calls: one search and one real local HTTP fetch**. This was
measured on the running agent with:

```bash
.venv/bin/python fanout.py
```

It reads `/debug/stats` before and after one request and checks the response
headers. Both reported `model_calls=3`, `search_calls=1`, `fetch_calls=1`; the
answer carried `[RESOLVED kb-42]`. Failure paths may skip fetch.

From the public repository root, run Kneepoint's example scenario:

```bash
.venv/bin/kneepoint run --scenario examples/multi-agent/kneepoint.yaml \
  --out reports/lantern-none
.venv/bin/python examples/multi-agent/grade.py \
  reports/lantern-none/run-<id>-sessions.jsonl
```

The scenario uses ramp 1..16 in steps of 3, 15-second holds, a grounded marker
regex check, explicit zero prices, no spend cap, and no SLO thresholds. The
grade script independently checks both the **correct article ID and key fact**
against `answers.json`; a marker alone does not pass. It reports answer-only
accuracy and all-session accuracy by level. Test the graph and tools with
`examples/multi-agent/.venv/bin/python -m unittest discover -s
examples/multi-agent -p 'test_*.py'` from the public root.

## Try it yourself in Kneepoint Plus

These commands assume the public and private repositories are sibling folders.
Use separate terminals so the app remains up while Plus runs. In terminal A:

```bash
cd /path/to/kneepoint-public/examples/multi-agent
DEMO_PROFILE=none .venv/bin/uvicorn app:create_app --factory \
  --host 127.0.0.1 --port 8765
```

In terminal B, from the public repository root:

```bash
CORE_ROOT=$(pwd)
cd ../kneepoint-plus
.venv/bin/kneepoint-plus run "$CORE_ROOT/examples/multi-agent/kneepoint.yaml" \
  --out reports/lantern-none
```

Watch the live run in the browser Plus opens. After it finishes, stop the Plus
server and terminal A. Restart terminal A with `DEMO_PROFILE=search-pool` and
run the same Plus command with `--out reports/lantern-search-pool`. Confirm
each active setting at `/debug/config`. The search constraint should raise
latency and flatten throughput at lower concurrency. Plus's current
`kneepoint==0.2.0` pin does not include the new public trace headers, and Plus
does not ingest spans yet; this exercise checks its load and dashboard path.
