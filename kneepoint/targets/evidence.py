"""Sanitize target diagnostics before they reach run evidence."""

from collections.abc import Iterable
from urllib.parse import quote, quote_plus

import httpx
from pydantic import ValidationError

from kneepoint.chaos.faults import known_fault

REDACTED = "[redacted]"


def url_secrets(url: httpx.URL) -> set[str]:
    values = {url.username, url.password, url.query.decode("ascii")}
    values.update(value for _, value in url.params.multi_items())
    return values - {""}


def display_url(url: httpx.URL) -> str:
    return str(url.copy_with(username=None, password=None, query=None, fragment=None))


class EvidenceSanitizer:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = set()
        self.add(secrets)

    def add(self, secrets: Iterable[str]) -> None:
        for secret in filter(None, secrets):
            self._secrets.update((secret, quote(secret, safe=""), quote_plus(secret)))

    def text(
        self, value: str, *, fallback: str | None = None, limit: int | None = None,
        diagnostic: bool = False, extra: str | None = None,
    ) -> str:
        clean = (
            "".join(char if char.isprintable() else " " for char in value)
            if diagnostic else value
        )
        # Test the untruncated text so a secret cut by the length cap stays hidden.
        secrets = self._secrets
        if extra:
            secrets = secrets | {extra, quote(extra, safe=""), quote_plus(extra)}
        matches = [secret for secret in secrets if secret in value or secret in clean]
        if matches and fallback is not None:
            clean = fallback
        elif matches:
            for secret in sorted(matches, key=len, reverse=True):
                clean = clean.replace(secret, REDACTED)
        return clean[:limit]

    def fault(self, value: str | None) -> str | None:
        fault = known_fault(value)
        return fault if fault is not None and self.text(fault) == fault else None

    def fields(
        self, *, error: str | None, warnings: Iterable[str] = (), fault: str | None = None,
    ) -> dict:
        return {
            "error": self.text(error, limit=200) if error is not None else None,
            "warnings": [self.text(warning) for warning in warnings],
            "fault": self.fault(fault),
        }


def exception_type_and_path(exc: Exception) -> str:
    """Use fixed validation locations and omit exception messages and rejected values."""
    name = type(exc).__name__
    if not isinstance(exc, ValidationError):
        return name
    fields = {"input_tokens": "usage.prompt_tokens", "output_tokens": "usage.completion_tokens"}
    paths = [
        fields[error["loc"][0]]
        for error in exc.errors(include_input=False, include_context=False, include_url=False)
        if error["loc"] and error["loc"][0] in fields
    ]
    return f"{name}: {', '.join(paths)}" if paths else name
