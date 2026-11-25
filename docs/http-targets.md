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

## Dify Chatflow with streaming conversations

Dify Chatflow sends the event type in the JSON data, so match `/event` in
the success rule. The `workflow_finished` event carries the workflow status at
`/data/status`. A failed workflow can arrive with HTTP 200 even when its
upstream provider returned 429. The status rule counts that response as
`body_failure` and keeps the original HTTP status. The default retry policy
does not retry a body failure at HTTP 200.

```yaml
target:
  type: http
  url: http://127.0.0.1/v1/chat-messages
  auth_env: DIFY_APP_KEY
  body:
    inputs: {}
    query: "{prompt}"
    response_mode: streaming
    user: "{session_id}"
    conversation_id: "{{capture.conversation_id}}"
  response:
    mode: sse
    text_path: /answer
  success:
    path: /data/status
    equals: [succeeded]
    event_filter: {path: /event, equals: workflow_finished}
  capture:
    name: conversation_id
    path: /conversation_id
    from: sse_event
workload:
  ramp: {from: 1, to: 1, hold_seconds: 0}
  conversation:
    turns: {min: 3, max: 3}
```

Set the URL to your Chatflow endpoint. The first request sends an empty
conversation ID to start a conversation. The following requests use the ID
captured from the preceding successful turn. If that response supplies no
usable ID, its record carries a `capture_missing` warning and the next turn
that needs the ID fails before dispatch. The mapping was exercised against
a loopback fake using the observed Dify failure shape.

## Tracing and observer effect

Kneepoint sends sampled W3C context by default. Dify with a parent-based
sampler can honor that flag and trace every load-test request, even when
its own root sampling rate is lower. The extra tracing work can change
the performance you measure.

Add this top-level setting to the Dify scenario above to let Dify decide
whether to sample each incoming request.

```yaml
trace:
  propagation: "off"
```

Kneepoint still records its local trace and span IDs in this mode. The
target starts its own trace context. Use `unsampled` to send the same
Kneepoint IDs and baggage with flag `00`, which a parent-based sampler
may honor by skipping the trace.

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

### Virtual-user Retry-After

Set `client: {honor_retry_after: true}` in the scenario to wait after HTTP 503
responses carrying a valid Retry-After (seconds or HTTP date). Default is false.
Each virtual user waits before its next request, including across session
boundaries. Any per-turn retry also uses a valid Retry-After delay, even with
this option disabled. All header delays are capped at 30 seconds and are
interruptible by stop.
Records include `retry_after_s` (parsed, uncapped) and `backoff_s` (applied
delay, zero when no header delay applies). Response latency excludes this wait.
Each attempt uses at most one header delay. A stop interrupts that wait and
prevents the next request from dispatching.

Each Retry-After wait is capped at 30 seconds. A virtual user can finish its
wait after the scheduled end of a hold. A later release will bound these waits
by the remaining hold time.
