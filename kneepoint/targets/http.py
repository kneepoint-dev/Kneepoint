"""One configurable JSON or SSE exchange per load attempt."""

import asyncio
import json
import math
import re
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from functools import partial

import httpx

from kneepoint.chaos.faults import known_fault
from kneepoint.collect.schemas import RequestRecord
from kneepoint.collect.writer import version_stamps
from kneepoint.targets.context_errors import is_context_overflow
from kneepoint.targets.http_config import (
    Capture,
    FieldEventFilter,
    HttpTargetConfig,
    JsonResponse,
    NamedEventFilter,
    ResolvedHttp,
    Scalar,
    SseResponse,
    SuccessRule,
    Usage,
    compile_pointer,
    extract,
    render_body,
)
from kneepoint.targets.retry_after import parse_retry_after as _retry_after
from kneepoint.targets.trace_context import TraceSetting, request_trace
from kneepoint.targets.turn import REQUEST_WALL_S, TurnOutcome, TurnRunner

_MISSING = object()
_JSON_LIMIT = 16 * 1024 * 1024
_EVENT_LIMIT = 1024 * 1024
_NEWLINE = re.compile(rb"[\r\n]")
_TRACE_HEADERS = ("traceparent", "baggage", "tracestate")


class _ResponseError(ValueError):
    """A fixed diagnostic safe to publish without the request or response data."""


class _AgentError(ValueError):
    pass


class _ContextOverflow(_AgentError):
    pass


class _BodyFailure(ValueError):
    pass


class _CaptureMissing(ValueError):
    pass


def _invalid_constant(_value: str):
    raise _ResponseError("nonfinite JSON number")


def _json(data: bytes | str):
    try:
        return json.loads(data, parse_constant=_invalid_constant)
    except (UnicodeError, RecursionError):
        raise _ResponseError("invalid JSON encoding or nesting") from None
    except json.JSONDecodeError:
        raise
    except _ResponseError:
        raise
    except ValueError:
        raise _ResponseError("invalid JSON value") from None


def _select(value, path: str | None):
    return extract(value, compile_pointer(path)) if path is not None else None


def _present_value(payload, path: str):
    return extract(payload, compile_pointer(path), missing=_MISSING)


def _equal(value, expected) -> bool:
    # Python compares booleans equal to 0 and 1, which are distinct JSON types.
    return isinstance(value, bool) == isinstance(expected, bool) and value == expected


def _capture_value(payload, capture: Capture | None) -> Scalar | None:
    value = _select(payload, capture.path) if capture is not None else None
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            return None
        return value if value.strip() else None
    if isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _reply_text(value, mode: str) -> str:
    if not isinstance(value, str):
        raise _ResponseError(f"{mode} reply path must select a string")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _ResponseError(f"{mode} reply contains invalid Unicode") from None
    return value


async def _json_body(response: httpx.Response):
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > _JSON_LIMIT:
            raise _ResponseError("JSON body exceeds 16 MiB")
        body.extend(chunk)
    return _json(body)


async def _sse_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    """Split CR, LF and CRLF lines without buffering an unbounded line."""
    line = bytearray()
    after_cr = False
    async for chunk in response.aiter_bytes():
        start = 0
        for match in _NEWLINE.finditer(chunk):
            end = match.start()
            if len(line) + end - start > _EVENT_LIMIT:
                raise _ResponseError("SSE event exceeds 1 MiB")
            line.extend(chunk[start:end])
            if not (after_cr and start == end and match[0] == b"\n"):
                yield bytes(line)
                line.clear()
            after_cr = match[0] == b"\r"
            start = end + 1
        if start < len(chunk):
            if len(line) + len(chunk) - start > _EVENT_LIMIT:
                raise _ResponseError("SSE event exceeds 1 MiB")
            line.extend(chunk[start:])
            after_cr = False


async def _events(response: httpx.Response) -> AsyncIterator[tuple[str, str | None]]:
    event, data, size = "", [], 0
    first_line = True
    async for raw_line in _sse_lines(response):
        try:
            line = raw_line.decode("utf-8-sig" if first_line else "utf-8")
        except UnicodeError:
            raise _ResponseError("invalid SSE UTF-8") from None
        first_line = False
        if not line:
            if data or event:
                yield event or "message", "\n".join(data) if data else None
            event, data, size = "", [], 0
            continue
        size += len(raw_line) + 1
        if size > _EVENT_LIMIT:
            raise _ResponseError("SSE event exceeds 1 MiB")
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


class _Reply:
    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.gaps_ms: list[float] = []
        self.first_at: float | None = None
        self.last_at: float | None = None
        self.success_value = _MISSING
        self.captured: Scalar | None = None
        self.tokens: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}

    def read_usage(self, payload, usage: Usage | None) -> None:
        if usage is None:
            return
        for key, path in (("input_tokens", usage.input_path), ("output_tokens", usage.output_path)):
            count = _select(payload, path)
            if count is None:
                continue
            if type(count) is not int or count < 0:
                raise _ResponseError(f"{key} must be a nonnegative integer")
            previous = self.tokens[key]
            if previous is not None and count < previous:
                raise _ResponseError(f"cumulative SSE {key} decreased")
            self.tokens[key] = count

    def read_capture(self, payload, capture: Capture | None, secrets: set[str]) -> None:
        value = _capture_value(payload, capture)
        if value is not None:
            self.captured = value
            secrets.add(str(value))

    def append_delta(self, content: str) -> None:
        now = time.perf_counter()
        if self.last_at is None:
            self.first_at = now
        else:
            self.gaps_ms.append((now - self.last_at) * 1000)
        self.last_at = now
        self.pieces.append(content)


def _matches(selector: str | None, event: str) -> bool:
    return selector is None or selector == event


def _agent_error(value, secrets: set[str]) -> _AgentError:
    if is_context_overflow(value):
        return _ContextOverflow("context_overflow")
    kind = {
        dict: "object", list: "array", str: "string", bool: "boolean",
        int: "number", float: "number", type(None): "null",
    }[type(value)]
    label = kind
    if isinstance(value, str):
        label = value
    elif isinstance(value, dict):
        for key in ("type", "code", "message"):
            if isinstance(value.get(key), str):
                label = value[key]
                break
    limit = 80 if isinstance(value, str) else 120 - len("AgentError: ")
    preview = "".join(char if char.isprintable() else " " for char in label[:limit])
    # Check before truncation as well, so a secret cut in half cannot leak.
    if any(secret in label or secret in preview for secret in secrets):
        return _AgentError(kind)
    return _AgentError(preview)


def _check_success(value, rule: SuccessRule | None, secrets: set[str]) -> None:
    if rule is None:
        return
    if value is _MISSING:
        raise _BodyFailure("success path missing from the selected response")
    if rule.equals is not None:
        accepted = any(_equal(value, item) for item in rule.equals)
    else:
        accepted = not any(_equal(value, item) for item in rule.not_equals)
    if not accepted:
        # Keep structured payloads out of diagnostics, even when they contain
        # the familiar type/code/message keys used by legacy agent errors.
        if isinstance(value, (dict, list)):
            observed = "object" if isinstance(value, dict) else "array"
        else:
            label = value if isinstance(value, str) else json.dumps(value)
            observed = str(_agent_error(label, secrets))
        raise _BodyFailure(f"success rule rejected observed value {observed!r}")


def _stream_error(
    data: str | None, target: HttpTargetConfig, reply: _Reply, secrets: set[str],
) -> _AgentError:
    if data is None:
        return _AgentError("event")
    path = target.response.error_path
    try:
        payload = _json(data)
    except (json.JSONDecodeError, _ResponseError):
        return _agent_error(data, secrets) if path is None else _AgentError("event")
    reply.read_capture(payload, target.capture, secrets)
    value = _select(payload, path) if path is not None else payload
    return _agent_error(value, secrets)


async def _read_sse(
    response: httpx.Response, target: HttpTargetConfig, reply: _Reply, secrets: set[str],
) -> None:
    config = target.response
    end = config.end
    usage = config.usage
    usage_selector = usage.event if usage and usage.event is not None else config.event
    rule = target.success
    event_filter = rule.event_filter if rule else None
    async for event, data in _events(response):
        if event == config.error_event:
            raise _stream_error(data, target, reply, secrets)
        at_end = end and _matches(end.event, event) and (end.data is None or end.data == data)
        read_text = not at_end and _matches(config.event, event)
        read_usage = not at_end and usage is not None and _matches(usage_selector, event)
        read_success = rule is not None and (
            not isinstance(event_filter, NamedEventFilter) or event_filter.event == event
        )
        if data is not None and (read_text or read_usage or read_success or target.capture):
            try:
                payload = _json(data)
            except (json.JSONDecodeError, _ResponseError):
                if read_text or read_usage:
                    raise
                # Optional extraction can also encounter control frames like [DONE].
            else:
                if read_success and (
                    not isinstance(event_filter, FieldEventFilter)
                    or _equal(_present_value(payload, event_filter.path), event_filter.equals)
                ):
                    value = _present_value(payload, rule.path)
                    if event_filter is not None or value is not _MISSING:
                        reply.success_value = value
                reply.read_capture(payload, target.capture, secrets)
                if read_usage:
                    reply.read_usage(payload, usage)
                if read_text:
                    delta = _select(payload, config.text_path)
                    if delta is not None:
                        text = _reply_text(delta, "SSE")
                        if text:
                            reply.append_delta(text)
        if at_end:
            return
    if end is not None:
        raise _ResponseError("SSE stream ended before its configured marker")


class HttpTurn:
    """Bind one validated HTTP scenario to the generator's turn interface."""

    def __init__(self, config: HttpTargetConfig, resolved: ResolvedHttp) -> None:
        self.config = config
        self.resolved = resolved

    def for_session(self) -> TurnRunner:
        if self.config.capture is None:
            return self
        return partial(self, capture_state={})

    async def __call__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        messages: list[dict],
        *,
        concurrency: int,
        session_id: str,
        turn_index: int = 0,
        attempt: int = 1,
        wall_seconds: float | None = None,
        run_id: str | None = None,
        trace: TraceSetting = True,
        run_origin_s: float | None = None,
        capture_state: dict[str, Scalar] | None = None,
    ) -> TurnOutcome:
        wall = REQUEST_WALL_S if wall_seconds is None else wall_seconds
        trace_id, span_id, trace_headers = request_trace(
            trace, run_id, session_id, concurrency, turn_index, attempt,
        )

        config = self.config
        started_at, t0 = time.time(), time.perf_counter()
        status = fault = error = retry_after_s = None
        error_kind = None
        abandoned = False
        reply = _Reply()
        try:
            async with asyncio.timeout(wall):
                request = self._request(
                    client, messages, session_id, turn_index, trace_headers, capture_state,
                )
                secrets = set(self.resolved.secrets)
                if config.capture is not None and capture_state:
                    secrets.update(str(value) for value in capture_state.values())
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    status = response.status_code
                    fault = known_fault(response.headers.get("x-kneepoint-fault"))
                    accepted = (
                        200 <= status <= 299 if config.success_status is None
                        else status in config.success_status
                    )
                    if not accepted:
                        error = f"HTTP {status}"
                        if status == 400:
                            try:
                                if is_context_overflow(await _json_body(response)):
                                    error_kind = "context_overflow"
                            except (ValueError, _ResponseError):
                                pass
                        retry_after_s = _retry_after(response.headers.get("retry-after"))
                    elif isinstance(config.response, JsonResponse):
                        payload = await _json_body(response)
                        agent_error = _select(payload, config.response.error_path)
                        if config.success is not None:
                            reply.success_value = _present_value(payload, config.success.path)
                        reply.read_capture(payload, config.capture, secrets)
                        try:
                            reply.read_usage(payload, config.response.usage)
                        except _ResponseError:
                            if agent_error is None:
                                _check_success(reply.success_value, config.success, secrets)
                                raise
                        if agent_error is not None:
                            raise _agent_error(agent_error, secrets)
                        _check_success(reply.success_value, config.success, secrets)
                        text = _reply_text(_select(payload, config.response.text_path), "JSON")
                        reply.pieces.append(text)
                    else:
                        await _read_sse(response, config, reply, secrets)
                        _check_success(reply.success_value, config.success, secrets)
                finally:
                    await response.aclose()
        except TimeoutError:
            error_kind = "timeout"
            error = f"abandoned at the {wall:g}s wall"
            abandoned = status is not None
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            error_kind = "timeout"
            error = f"{type(exc).__name__}: HTTP gap timeout"
            abandoned = True
        except httpx.HTTPError as exc:
            error_kind = "http_error"
            error = f"{type(exc).__name__}: HTTP exchange failed"
        except json.JSONDecodeError:
            error_kind = "response_error"
            error = "JSONDecodeError: malformed response JSON"
        except _ResponseError as exc:
            error_kind = "response_error"
            error = f"ValueError: {exc}"
        except _AgentError as exc:
            error_kind = "context_overflow" if isinstance(exc, _ContextOverflow) else "agent_error"
            error = f"AgentError: {exc}"[:120]
        except _BodyFailure as exc:
            error_kind = "body_failure"
            error = str(exc)
        except _CaptureMissing as exc:
            error_kind = "capture_missing"
            error = str(exc)
        except (ValueError, TypeError) as exc:
            error_kind = "request_error"
            # Request encoding can fail on caller-supplied messages; their
            # content is not safe to include in a record.
            error = f"{type(exc).__name__}: invalid HTTP request data"

        warnings = []
        if error is None and config.capture is not None:
            if reply.captured is None:
                warnings.append("capture_missing")
                if capture_state is not None:
                    capture_state.pop(config.capture.name, None)
            elif capture_state is not None:
                capture_state[config.capture.name] = reply.captured

        gaps = reply.gaps_ms if error is None else []
        record = RequestRecord(
            **version_stamps(),
            session_id=session_id, concurrency=concurrency, turn_index=turn_index,
            attempt=attempt, started_at=started_at,
            started_monotonic_s=t0 - run_origin_s if run_origin_s is not None else None,
            total_ms=(time.perf_counter() - t0) * 1000,
            ok=error is None, error=error[:200] if error is not None else None,
            error_kind=error_kind, warnings=warnings,
            abandoned=abandoned, status_code=status, fault=fault,
            trace_id=trace_id, span_id=span_id,
            request_id=uuid.uuid4().hex, traceparent=trace_headers.get("traceparent"),
            ttft_ms=(reply.first_at - t0) * 1000 if reply.first_at is not None else None,
            chunk_count=(len(reply.pieces) if error is None
                         and isinstance(config.response, SseResponse) else None),
            itl_mean_ms=statistics.fmean(gaps) if gaps else None,
            itl_p99_ms=(statistics.quantiles(gaps, n=100, method="inclusive")[-1]
                        if len(gaps) > 1 else gaps[0] if gaps else None),
            **reply.tokens,
        )
        return TurnOutcome(
            record=record, text="".join(reply.pieces) if record.ok else "",
            retry_after_s=retry_after_s,
        )

    def _request(self, client, messages, session_id, turn_index, trace_headers, capture_state):
        config = self.config
        headers = httpx.Headers(self.resolved.headers)
        variables = {}
        if config.capture_used:
            name = config.capture.name
            value = (capture_state or {}).get(name)
            if turn_index > 0 and value is None:
                raise _CaptureMissing(f"missing required capture {name!r} before HTTP dispatch")
            variables[f"capture.{name}"] = "" if value is None else value
            for header, parts in self.resolved.header_templates:
                rendered = str(variables[f"capture.{name}"]).join(parts)
                if rendered != rendered.strip() or any(
                    not (32 <= ord(char) < 127 or char == "\t") for char in rendered
                ):
                    raise _ResponseError("capture produced an invalid HTTP header value")
                headers[header] = rendered
        kwargs = {}
        if config.has_body:
            variables.update(
                prompt=messages[-1]["content"],
                session_id=session_id,
                turn_index=turn_index,
                history=deepcopy(messages),
            )
            body = render_body(config.body, variables)
            if body is None:
                # HTTPX treats json=None as an omitted body.
                kwargs["content"] = b"null"
                headers.setdefault("content-type", "application/json")
            else:
                kwargs["json"] = body
        request = client.build_request(config.method, self.resolved.url, headers=headers, **kwargs)
        request.headers["x-kneepoint-session"] = session_id
        for name in _TRACE_HEADERS:
            request.headers.pop(name, None)
        request.headers.update(trace_headers)
        return request
