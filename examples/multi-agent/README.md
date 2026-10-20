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

The front door extracts Kneepoint's W3C `traceparent` and `baggage`. Its server
span is a child of the exact load-attempt span, with the same trace ID. Context
stays current through all three LangGraph nodes and is injected into the real
localhost fetch request. Tracing is opt-in: `DEMO_TRACING=1` enables a batch
OTLP/HTTP exporter; `DEMO_TRACING=0` (the default) runs without recording spans.
Incoming W3C context is still forwarded to the fetch hop when recording is off.
The exporter accepts only a local HTTP `OTEL_EXPORTER_OTLP_ENDPOINT` (default
`http://127.0.0.1:4318`, with `/v1/traces` appended). No paid endpoint is used.

The GenAI names and attributes follow **OpenTelemetry semantic conventions
v1.41.1**, specifically the [GenAI agent spans](https://github.com/open-telemetry/semantic-conventions/blob/v1.41.1/docs/gen-ai/gen-ai-agent-spans.md)
and [GenAI model/tool spans](https://github.com/open-telemetry/semantic-conventions/blob/v1.41.1/docs/gen-ai/gen-ai-spans.md).
These conventions are marked Development. The custom `demo.*` attributes are
LanternDesk's measurement contract, not OTel standard attributes. The example
pins the Python OTel SDK/exporter/protobuf packages to 1.45.1 separately from
the semantic-convention document version.

| Span | Meaning |
|---|---|
| `POST /v1/chat/completions` (SERVER) | One incoming attempt through final SSE delivery; parent is Kneepoint's sent span ID. |
| `invoke_agent planner/researcher/writer` (INTERNAL) | One named LangGraph node each; provider is `langgraph`. |
| `chat lantern-sim` or `chat <Ollama model>` (CLIENT) | One model call; provider, model and actual token usage are attributes. |
| `execute_tool search/fetch` (INTERNAL) | One tool call each. |
| `wait model slot/search worker/fetch rate limiter` (INTERNAL) | Time queued before service; capacity and in-flight count are recorded. |
| `GET /documents/{article_id}` (CLIENT then SERVER) | Injected fetch hop and local document route. |

Each completed model and tool span has `demo.queue_wait_ms`, `demo.service_time_ms`,
`demo.pool.capacity` and `demo.pool.in_flight`. Service time starts only after
the worker, model slot or rate limiter admits the step. For search and model,
utilization can be reconstructed from spans as `U = X × S / m`, where `X` is
step completions per second, `S` is mean service seconds and `m` is capacity.
`demo.pool.capacity` on fetch is starts per second, so this simple utilization
formula does not apply to fetch. The sink stores no prompt or article content.

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
| `fetch-ratelimit` | 25 fetch starts/s | about 25 requests/s; saturation near c=3 |

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

## Try it yourself: traces and local sink

In three terminals, from the public repository root (after installing the
example requirements above):

```bash
# Terminal A: local OTLP/HTTP receiver
cd examples/multi-agent
.venv/bin/python otlp_sink.py /tmp/lanterndesk-spans.jsonl \
  --first-payload /tmp/lanterndesk-first-otlp-request.pb
```

```bash
# Terminal B: traced app
cd examples/multi-agent
DEMO_PROFILE=search-pool DEMO_TRACING=1 \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
.venv/bin/uvicorn app:create_app --factory --host 127.0.0.1 --port 8765
```

```bash
# Terminal C: public CLI on codex/trace-propagation or a descendant
caffeinate -dimsu .venv/bin/kneepoint run \
  --scenario examples/multi-agent/kneepoint.yaml --out /tmp/lanterndesk-run
```

Each line in `/tmp/lanterndesk-spans.jsonl` is one decoded OTLP span. Join its
`trace_id` to `trace_id` in the run's `run-*.jsonl`; the chat server span's
`parent_span_id` equals the attempt's `span_id`. `baggage.kneepoint.level` on
that server span supplies the concurrency level. The `.pb` file is an actual
OTLP/HTTP request body suitable for later receiver tests. Switch to `DEMO_TRACING=0` for the same ramp without exporter overhead.

## Tool-neutral step measurements

The `chat` and `execute_tool` spans also emit an additive convention usable by
any trace consumer: `kneepoint.step.queue_wait_ms` and
`kneepoint.step.service_ms` are monotonic elapsed milliseconds spent waiting
and serving. `kneepoint.step.capacity` is the number of shared service slots
(model or search). Fetch instead emits `kneepoint.step.rate_per_s`, the allowed
starts per second; zero means the limiter is disabled, not zero capacity.
The existing `demo.*` attributes remain and carry the same measured values.
A timeout before admission has no service measurement; consumers must not
fill missing values with zero. Step identity comes from `gen_ai.operation.name`
and `gen_ai.tool.name` or `gen_ai.request.model`, not the demo attributes.
For steady completion rate X and mean service seconds S, slot utilization is
X × S / capacity; rate-limiter utilization is X / rate_per_s.
