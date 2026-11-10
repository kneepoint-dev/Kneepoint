# Prompt AA review notes

Branch `codex/0.5.0-dogfood` starts at HTTP target commit
`179ff5422ef5ca7bb01ebe7993fb3843af357a18`.

## Response success

- `target.success` accepts a JSON Pointer `path` and exactly one nonempty
  `equals` or `not_equals` list of JSON scalars. A missing path fails either
  comparison. Explicit JSON null is a value.
- SSE defaults to the last JSON data event containing the path.
  `event_filter: {event: done}` selects by SSE name.
  `event_filter: {path: /event, equals: workflow_finished}` selects by a JSON
  field. With a filter, the last matching JSON event supplies the value.
  A JSON-bearing end marker can supply the value before the stream closes.
- The HTTP status check runs first. A configured JSON `error_path` or SSE
  `error_event` takes precedence over the success rule. A rule failure retains
  the HTTP status and has `ok=false` and `error_kind=body_failure`. It enters the
  regular error counts and appears by kind and in the HTML Limits section.
- Diagnostics use a bounded scalar preview or a structured value's type.
  Configured secrets and matching capture values are redacted before
  truncation. The rule should point to a public status field.

## Response capture

- `target.capture` accepts `name`, a JSON Pointer `path`, and
  `from: json` or `from: sse_event`. In SSE, the last usable value from a
  JSON data event wins. Usable captures are nonblank strings or finite
  numbers and booleans.
- `{{capture.name}}` works in request body and header values. A standalone
  body placeholder keeps the captured JSON type. The initial turn uses the
  empty string to start a Dify conversation.
- The session runner allocates state for each session and commits the
  capture after a successful response. A successful turn without a usable
  capture clears the previous value and records a `capture_missing` warning.
  The next turn that references it records `error_kind=capture_missing`
  before dispatch and without retrying. Failed attempts do not update state.
- Old records load with a null error kind and an empty warnings list.

## Loopback comparison

The fake endpoint returned HTTP 200 with `data.status=failed` and a synthetic
upstream 429 diagnostic, based on the Z2 `workflow_finished` event. The JSON
variant used the same nested failure and a partial answer. The SSE variant
sent a partial answer event before the terminal workflow event.

| Source | JSON result | SSE result |
| --- | --- | --- |
| Tagged Core `v0.4.0` (`6461c4f0bbe15c3291b952f006e1aec31289496b`), OpenAI-compatible adapter | `200, ok=true`, empty reply | `200, ok=true`, empty reply |
| HTTP adapter base `179ff54` | `200, ok=true`, partial reply | `200, ok=true`, partial reply |
| Dogfood branch with success rule | `200, ok=false, body_failure` | `200, ok=false, body_failure` |

The tagged release predates the configurable HTTP adapter. Its reader ignored
the buffered JSON body and found no OpenAI-compatible deltas in the Dify SSE.
The HTTP adapter base is the like-for-like parser comparison. The branch
recorded `success rule rejected observed value 'failed'` in both modes.
The checked-in loopback regression runs the branch through the CLI and
checks the saved record, error counts, Limits section, and offline rerender.

## Verification

- Full suite: 579 passed, 1 skipped. The 122 HTTP adapter and output-format
  tests also passed as a focused run.
- `mkdocs build --strict` passed. Material emitted its general MkDocs 2.0
  compatibility notice.
- `ruff check .` and `git diff --check` passed.
- The documented Dify Chatflow scenario validated offline.

## Open questions

- The loopback check covers the observed Dify failure shape. Live Dify and
  provider traffic were outside this verification.
- The initial capture value is fixed to an empty string. A future endpoint
  that requires the field to be omitted on turn one would need an explicit
  omission option.
