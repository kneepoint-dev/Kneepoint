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
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from kneepoint.targets.evidence import url_secrets

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ENV = re.compile(r"\$\$\{|\$\{([^{}]*)\}|\$\{")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CAPTURE_TOKEN = rf"\{{\{{(capture\.{_NAME})\}}\}}"
_CAPTURE_TEMPLATE = re.compile(_CAPTURE_TOKEN)
_TEMPLATE = re.compile(
    rf"{_CAPTURE_TOKEN}|\{{\{{|\}}\}}|\{{(prompt|session_id|turn_index|history)\}}|[{{}}]",
)
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
Scalar = (
    StrictBool | StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)] | StrictStr
)
NullableScalar = Scalar | None


def extract(value, parts: tuple[str, ...], *, missing=None):
    """Read a pointer, using the caller's default when the path is absent."""
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part, missing)
        elif isinstance(value, list) and _ARRAY_INDEX.fullmatch(part):
            # Avoid converting arbitrarily large indices supplied in a scenario.
            if len(part) > len(str(len(value))):
                return missing
            index = int(part)
            value = value[index] if index < len(value) else missing
        else:
            return missing
    return value


def _template_parts(value: str):
    for match in _TEMPLATE.finditer(value):
        if match[0] in ("{", "}"):
            raise ValueError("invalid body placeholder or unescaped brace")
        if match[2] == "history" and value != "{history}":
            raise ValueError("{history} must occupy a whole JSON value")
        yield match


def _validate_body(value, placeholders: set[str], ancestors: set[int]) -> None:
    if isinstance(value, str):
        for match in _template_parts(value):
            if name := match[1] or match[2]:
                placeholders.add(name)
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
    if capture := _CAPTURE_TEMPLATE.fullmatch(value):
        return variables[capture[1]]

    def replace(match: re.Match[str]) -> str:
        name = match[1] or match[2]
        return str(variables[name]) if name else match[0][0]

    return _TEMPLATE.sub(replace, value)


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


class NamedEventFilter(_Strict):
    event: Event


class FieldEventFilter(_Strict):
    path: Pointer
    equals: NullableScalar


class SuccessRule(_Strict):
    path: Pointer
    equals: list[NullableScalar] | None = Field(default=None, min_length=1)
    not_equals: list[NullableScalar] | None = Field(default=None, min_length=1)
    event_filter: NamedEventFilter | FieldEventFilter | None = None

    @model_validator(mode="after")
    def _one_comparison(self):
        if len({"equals", "not_equals"} & self.model_fields_set) != 1 or (
            self.equals is None and self.not_equals is None
        ):
            raise ValueError("success requires exactly one nonempty equals or not_equals list")
        return self


class Capture(_Strict):
    name: Annotated[str, Field(pattern=rf"^{_NAME}$")]
    path: Pointer
    source: Literal["json", "sse_event"] = Field(alias="from")


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
    header_templates: tuple[tuple[str, tuple[str, ...]], ...] = field(repr=False)
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
    success: SuccessRule | None = None
    capture: Capture | None = None
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
        self._placeholders.clear()
        for name, value in self.headers.items():
            lower = name.lower()
            if not _HEADER_NAME.fullmatch(name) or lower in seen:
                raise ValueError("invalid or duplicate HTTP header name")
            if lower in {"host", "content-length", "transfer-encoding"}:
                raise ValueError(f"header {name} is set by the HTTP client")
            seen.add(lower)
            self._placeholders.update(_CAPTURE_TEMPLATE.findall(value))
        if self.auth_env is not None and "authorization" in seen:
            raise ValueError("auth_env conflicts with an explicit Authorization header")
        try:
            _validate_body(self.body, self._placeholders, set())
        except RecursionError:
            raise ValueError("body is nested too deeply") from None
        capture_refs = {name for name in self._placeholders if name.startswith("capture.")}
        if capture_refs and (
            self.capture is None or capture_refs != {f"capture.{self.capture.name}"}
        ):
            raise ValueError("capture placeholders must reference the configured capture name")
        if self.capture and (self.capture.source == "json") != (self.response.mode == "json"):
            raise ValueError("capture.from must match the response mode")
        if self.success and self.success.event_filter is not None and self.response.mode != "sse":
            raise ValueError("success.event_filter requires SSE response mode")
        return self

    @property
    def has_body(self) -> bool:
        return "body" in self.model_fields_set

    def check_turns(self, maximum: int) -> None:
        server_state = "session_id" in self._placeholders or self.capture_used
        if maximum > 1 and not (
            "history" in self._placeholders or "prompt" in self._placeholders and server_state
        ):
            raise ValueError(
                "multi-turn HTTP requires {history} or {prompt} with {session_id} or a capture",
            )

    @property
    def capture_used(self) -> bool:
        return self.capture is not None and f"capture.{self.capture.name}" in self._placeholders

    def environment_names(self, url_override: str | None = None) -> set[str]:
        values = (self.url if url_override is None else url_override, *self.headers.values())
        return {
            match[1]
            for value in values
            for match in _ENV.finditer(value)
            if match[1] and re.fullmatch(_NAME, match[1])
        }

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
        templates = []
        for name, value in self.headers.items():
            # Split the configured template before env expansion. An env value
            # containing template syntax must remain opaque.
            parts = _CAPTURE_TEMPLATE.split(value)
            label = f"target.headers.{name}"
            literals = tuple(_expand(part, label, secrets) for part in parts[::2])
            resolved = "x".join(literals)
            if any(not (32 <= ord(char) < 127 or char == "\t") for char in resolved):
                raise ValueError(f"target.headers.{name}: invalid header value")
            if resolved != resolved.strip():
                raise ValueError(f"target.headers.{name}: leading or trailing whitespace")
            if len(parts) > 1:
                templates.append((name, literals))
                secrets.update(filter(None, literals))
            else:
                headers[name] = resolved
        if self.auth_env is not None:
            token = _require_env(self.auth_env, "target.auth_env", secrets)
            if any(not 33 <= ord(char) < 127 for char in token):
                raise ValueError("target.auth_env: invalid Bearer token")
            headers["Authorization"] = f"Bearer {token}"
        secrets.update(value for value in headers.values() if value)
        secrets.update(url_secrets(url))
        return ResolvedHttp(
            url, tuple(headers.items()), tuple(templates), frozenset(secrets),
            configured_url.partition("?")[0],
        )
