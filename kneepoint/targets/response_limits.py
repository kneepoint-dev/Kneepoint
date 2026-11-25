"""Bounded reads shared by the HTTP adapters."""

import re
from collections.abc import AsyncIterator

import httpx

BODY_LIMIT = 16 * 1024 * 1024
EVENT_LIMIT = 1024 * 1024
_NEWLINE = re.compile(rb"[\r\n]")


class ResponseError(ValueError):
    """A fixed diagnostic safe to publish without request or response data."""


class SseLineTooLong(ResponseError):
    """A stream line exceeded the byte limit."""


async def sse_lines(
    response: httpx.Response, *, body_limit: int | None = None, keep_final: bool = False,
) -> AsyncIterator[bytes]:
    """Split CR, LF and CRLF without buffering an unbounded line."""
    line = bytearray()
    after_cr = False
    total = 0
    async for chunk in response.aiter_bytes():
        if body_limit is not None:
            total += len(chunk)
            if total > body_limit:
                raise ResponseError("response body exceeds 16 MiB")
        start = 0
        for match in _NEWLINE.finditer(chunk):
            end = match.start()
            if len(line) + end - start > EVENT_LIMIT:
                raise SseLineTooLong("SSE line exceeds 1 MiB")
            line.extend(chunk[start:end])
            if not (after_cr and start == end and match[0] == b"\n"):
                yield bytes(line)
                line.clear()
            after_cr = match[0] == b"\r"
            start = end + 1
        if start < len(chunk):
            if len(line) + len(chunk) - start > EVENT_LIMIT:
                raise SseLineTooLong("SSE line exceeds 1 MiB")
            line.extend(chunk[start:])
            after_cr = False
    if keep_final and line:
        yield bytes(line)
