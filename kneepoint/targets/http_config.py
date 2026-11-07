"""Validation and preparation for scenario-defined HTTP targets."""

import math
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictInt,
    field_validator,
    model_validator,
)

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ENV = re.compile(r"\$\$\{|\$\{([^{}]*)\}|\$\{")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_TEMPLATE = re.compile(r"\{\{|\}\}|\{(prompt|session_id|turn_index|history)\}|[{}]")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


@lru_cache(maxsize=256)
def compile_pointer(path: str) -> tuple[str, ...]:
    if path == "":
        return ()
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise ValueError("expected a JSON Pointer with / segments and ~0 or ~1 escapes")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/"))


def _valid_pointer(path: str) -> str:
    compile_pointer(path)
    return path


Pointer = Annotated[str, AfterValidator(_valid_pointer)]
Event = Annotated[str, Field(min_length=1)]


def extract(value, parts: tuple[str, ...]):
    """Read a pointer. Null and missing both mean no value to the response parser."""
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and _ARRAY_INDEX.fullmatch(part):
            # Avoid converting arbitrarily large indices supplied in a scenario.
            if len(part) > len(str(len(value))):
                return None
            index = int(part)
            value = value[index] if index < len(value) else None
        else:
            return None
    return value


def _template_parts(value: str):
    for match in _TEMPLATE.finditer(value):
        if match[0] in ("{", "}"):
            raise ValueError("invalid body placeholder or unescaped brace")
        if match[1] == "history" and value != "{history}":
            raise ValueError("{history} must occupy a whole JSON value")
        yield match


def _validate_body(value, placeholders: set[str], ancestors: set[int]) -> None:
    if isinstance(value, str):
        placeholders.update(match[1] for match in _template_parts(value) if match[1])
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("body strings must be valid UTF-8") from None
    elif isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise ValueError("body cannot contain recursive YAML aliases")
        ancestors.add(id(value))
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("body object keys must be strings")
                try:
                    key.encode("utf-8")
                except UnicodeError:
                    raise ValueError("body object keys must be valid UTF-8") from None
                _validate_body(item, placeholders, ancestors)
        else:
            for item in value:
                _validate_body(item, placeholders, ancestors)
        ancestors.remove(id(value))
    elif value is not None and not (
        isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value)
    ):
        raise ValueError("body must contain only finite JSON values")


def render_body(value, variables: dict):
    """Substitute only values, before JSON encoding. Variables are opaque data."""
    if isinstance(value, dict):
        return {key: render_body(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render_body(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    if value in ("{turn_index}", "{history}"):
        return variables[value[1:-1]]
    return _TEMPLATE.sub(
        lambda match: str(variables[match[1]]) if match[1] else match[0][0], value,
    )


def _expand(value: str, label: str, secrets: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match[0] == "$${":
            return "${"
        name = match[1]
        if name is None or not re.fullmatch(_NAME, name):
            raise ValueError(f"{label}: invalid environment reference")
        return _require_env(name, label, secrets)

    return _ENV.sub(replace, value)


def _require_env(name: str, label: str, secrets: set[str]) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{label}: environment variable {name} is missing or empty")
    secrets.add(value)
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Usage(_Strict):
    input_path: Pointer | None = None
    output_path: Pointer | None = None

    @model_validator(mode="after")
    def _require_path(self):
        if self.input_path is None and self.output_path is None:
            raise ValueError("usage needs input_path, output_path, or both")
        return self


class StreamUsage(Usage):
    event: Event | None = None


class StreamEnd(_Strict):
    event: Event | None = None
    data: str | None = None

    @model_validator(mode="after")
    def _require_marker(self):
        if self.event is None and self.data is None:
            raise ValueError("end needs event, data, or both")
        return self


class JsonResponse(_Strict):
    mode: Literal["json"]
    text_path: Pointer
    error_path: Pointer | None = None
    usage: Usage | None = None


class SseResponse(_Strict):
    mode: Literal["sse"]
    text_path: Pointer
    event: Event | None = None
    end: StreamEnd | None = None
    error_event: Event | None = None
    error_path: Pointer | None = None
    usage: StreamUsage | None = None

    @model_validator(mode="after")
    def _check_error_path(self):
        if self.error_path is not None and self.error_event is None:
            raise ValueError("SSE error_path requires error_event")
        return self


@dataclass(frozen=True)
class ResolvedHttp:
    url: httpx.URL = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    secrets: frozenset[str] = field(repr=False)
    display_url: str


class HttpTargetConfig(_Strict):
    type: Literal["http"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth_env: Annotated[str, Field(pattern=rf"^{_NAME}$")] | None = None
    body: object = None
    response: Annotated[JsonResponse | SseResponse, Field(discriminator="mode")]
    success_status: list[Annotated[StrictInt, Field(ge=200, le=299)]] | None = Field(
        default=None, min_length=1,
    )
    _placeholders: set[str] = PrivateAttr(default_factory=set)

    @field_validator("method", mode="before")
    @classmethod
    def _uppercase(cls, value):
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_request(self):
        if self.method == "GET" and self.has_body:
            raise ValueError("GET requires an omitted body")
        seen: set[str] = set()
        for name in self.headers:
            lower = name.lower()
            if not _HEADER_NAME.fullmatch(name) or lower in seen:
                raise ValueError("invalid or duplicate HTTP header name")
            if lower in {"host", "content-length", "transfer-encoding"}:
                raise ValueError(f"header {name} is set by the HTTP client")
            seen.add(lower)
        if self.auth_env is not None and "authorization" in seen:
            raise ValueError("auth_env conflicts with an explicit Authorization header")
        self._placeholders.clear()
        try:
            _validate_body(self.body, self._placeholders, set())
        except RecursionError:
            raise ValueError("body is nested too deeply") from None
        return self

    @property
    def has_body(self) -> bool:
        return "body" in self.model_fields_set

    def check_turns(self, maximum: int) -> None:
        if maximum > 1 and not (
            "history" in self._placeholders or {"prompt", "session_id"} <= self._placeholders
        ):
            raise ValueError("multi-turn HTTP requires {history} or both {prompt} and {session_id}")

    def resolve(self, url_override: str | None = None) -> ResolvedHttp:
        secrets: set[str] = set()
        configured_url = self.url if url_override is None else url_override
        raw_url = _expand(configured_url, "target.url", secrets)
        try:
            url_parts = urlsplit(raw_url)
            if (
                url_parts.scheme not in {"http", "https"} or not url_parts.hostname
                or "@" in url_parts.netloc or "#" in raw_url
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw_url)
            ):
                raise ValueError
            url = httpx.URL(raw_url)
            if not url.host or not url.is_absolute_url:
                raise ValueError
        except (ValueError, httpx.InvalidURL):
            raise ValueError("target.url: expected an absolute HTTP(S) URL without userinfo "
                             "or a fragment") from None

        headers = {}
        for name, value in self.headers.items():
            resolved = _expand(value, f"target.headers.{name}", secrets)
            if any(not (32 <= ord(char) < 127 or char == "\t") for char in resolved):
                raise ValueError(f"target.headers.{name}: invalid header value")
            if resolved != resolved.strip():
                raise ValueError(f"target.headers.{name}: leading or trailing whitespace")
            headers[name] = resolved
        if self.auth_env is not None:
            token = _require_env(self.auth_env, "target.auth_env", secrets)
            if any(not 33 <= ord(char) < 127 for char in token):
                raise ValueError("target.auth_env: invalid Bearer token")
            headers["Authorization"] = f"Bearer {token}"
        secrets.update(value for value in headers.values() if value)
        if url.query:
            secrets.update(value for _, value in url.params.multi_items() if value)
            secrets.add(url.query.decode("ascii"))
        return ResolvedHttp(
            url, tuple(headers.items()), frozenset(secrets), configured_url.partition("?")[0],
        )
