# Mapping common agent servers

Use `target.type: http` when the server exposes its own HTTP request and reply
shapes. Set `url` to the complete endpoint. Each request is one turn attempt.

## FastAPI JSON reply

**Verified against the example server.** The bundled echo agent reads
`{"input": "..."}` and returns `{"output": {"text": "..."}}`.

```yaml
target:
  type: http
  url: http://127.0.0.1:8000/invoke
  body:
    input: "{prompt}"
  response:
    mode: json
    text_path: /output/text
    error_path: /error
workload:
  ramp: {from: 1, to: 1, hold_seconds: 0}
```

Buffered JSON gives end-to-end latency. TTFT, inter-token latency, and time per
output token are unavailable in this mode. A non-null value at `error_path`
makes the turn fail even if the HTTP status is 200.

## SSE reply with JSON deltas

**Verified against the example server.** Each content frame is
`data: {"delta": "..."}` followed by a blank line. The last frame is
`data: [DONE]` followed by a blank line.

```yaml
target:
  type: http
  url: http://127.0.0.1:8000/stream
  body:
    input: "{prompt}"
  response:
    mode: sse
    text_path: /delta
    end: {data: "[DONE]"}
    error_event: error
workload:
  ramp: {from: 1, to: 1, hold_seconds: 0}
```

The first nonempty delta starts TTFT measurement. A matching `event: error`
fails the attempt and closes the response. Add `error_path` in SSE mode to
select a diagnostic value from that event's JSON data.

## LangGraph-style messages stream

**Illustrative, unverified.** This mapping assumes a custom server route
emits `event: messages` with array data such as
`[{"content": "a delta"}, {"node": "agent"}]`, then sends `event: done`.
Match the configuration to your route's actual request and event shapes.

```yaml
target:
  type: http
  url: http://127.0.0.1:8000/agent/stream
  body:
    messages: "{history}"
  response:
    mode: sse
    event: messages
    text_path: /0/content
    end: {event: done}
    error_event: error
workload:
  ramp: {from: 1, to: 1, hold_seconds: 0}
  conversation:
    turns: {min: 2, max: 2}
```

## Run the bundled example

From a checkout with Kneepoint installed, run these commands in one terminal.

```sh
python -m pip install -r examples/http-agent/requirements.txt
HTTP_AGENT_SHOW_SPANS=1 python examples/http-agent/app.py
```

The server listens on loopback and prints OpenTelemetry spans to stdout when
the flag is set. Run either checked-in scenario from another terminal.

```sh
kneepoint validate examples/http-agent/json.yaml
kneepoint run --scenario examples/http-agent/json.yaml --out reports/http-json
kneepoint validate examples/http-agent/sse.yaml
kneepoint run --scenario examples/http-agent/sse.yaml --out reports/http-sse
```

See the [scenario reference](scenarios.md#http-targets) for the full mapping
syntax, header secrets, and token usage fields.
