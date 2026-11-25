"""HealthSwapError: a failed API call with its RFC 9457 problem details (api/openapi.yaml components/schemas/Problem)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

__all__ = ["HealthSwapError", "retry_after_seconds"]


class HealthSwapError(Exception):
    """An API call failed.

    For HTTP errors the attributes carry the problem details of the response: `status`, `code` (stable, for example
    HS-REQ-INVALID, HS-NOT-FOUND, HS-RATE-LIMITED), `title`, `detail`, `errors` (field errors: dicts with field, message, code),
    `type`, `instance` and `trace_id`. OAuth token endpoint errors use the RFC 6749 `error` as `code` (for example
    invalid_client). Client-side failures (timeouts, connection errors) have `status` None and the codes HS-SDK-TIMEOUT or
    HS-SDK-CONNECTION. `retry_after` is the server's Retry-After / RateLimit-Reset in seconds, when it sent one.
    """

    def __init__(self, status: int | None, code: str | None, title: str, detail: str | None = None,
                 errors: Sequence[Mapping[str, Any]] | None = None, *, type: str | None = None, instance: str | None = None,
                 trace_id: str | None = None, retry_after: float | None = None, problem: Mapping[str, Any] | None = None) -> None:
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.errors: list[dict[str, Any]] = [dict(e) for e in errors or () if isinstance(e, Mapping)]
        self.type = type
        self.instance = instance
        self.trace_id = trace_id
        self.retry_after = retry_after
        self.problem: dict[str, Any] = dict(problem or {})
        super().__init__(self._message())

    def _message(self) -> str:
        head = " ".join(str(p) for p in (self.status, self.code) if p is not None) or "HealthSwap error"
        text = f"{head}: {self.title}"
        if self.detail and self.detail != self.title:
            text += f" - {self.detail}"
        if self.errors:
            text += " (" + "; ".join(f"{e.get('field', '?')}: {e.get('message', '')}" for e in self.errors[:5]) + ")"
        return text

    @classmethod
    def from_response(cls, response: httpx.Response) -> HealthSwapError:
        """The error of a non-success response: problem+json, an OAuth error document, or just the status line."""
        retry_after = retry_after_seconds(response.headers)
        body: Any = None
        if response.content:
            try:
                body = json.loads(response.content)
            except ValueError:
                body = None
        if isinstance(body, Mapping) and ("code" in body or "title" in body):
            status = body.get("status")
            return cls(
                status if isinstance(status, int) and not isinstance(status, bool) else response.status_code,
                _text(body.get("code")), _text(body.get("title")) or response.reason_phrase or "Error", _text(body.get("detail")),
                body.get("errors") if isinstance(body.get("errors"), list) else None,
                type=_text(body.get("type")), instance=_text(body.get("instance")), trace_id=_text(body.get("traceId")),
                retry_after=retry_after, problem=body,
            )
        if isinstance(body, Mapping) and isinstance(body.get("error"), str):  # RFC 6749 section 5.2 (token endpoint)
            return cls(response.status_code, body["error"], body["error"], _text(body.get("error_description")), retry_after=retry_after,
                       problem=body)
        return cls(response.status_code, None, response.reason_phrase or f"HTTP {response.status_code}", retry_after=retry_after)

    @classmethod
    def transport(cls, ex: httpx.TransportError) -> HealthSwapError:
        """A request that got no HTTP response."""
        if isinstance(ex, httpx.TimeoutException):
            return cls(None, "HS-SDK-TIMEOUT", "Request timed out", f"{type(ex).__name__}: {ex}" if str(ex) else type(ex).__name__)
        return cls(None, "HS-SDK-CONNECTION", "Connection failed", f"{type(ex).__name__}: {ex}" if str(ex) else type(ex).__name__)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def retry_after_seconds(headers: httpx.Headers | Mapping[str, str], now: datetime | None = None) -> float | None:
    """Seconds to wait from Retry-After (delta-seconds or an HTTP-date), else from RateLimit-Reset (delta-seconds); None
    when neither header is present and valid."""
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        value = retry_after.strip()
        if _is_seconds(value):
            return float(value)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
    reset = headers.get("ratelimit-reset")
    if reset is not None and _is_seconds(reset.strip()):
        return float(reset.strip())
    return None


def _is_seconds(value: str) -> bool:
    """delta-seconds: 1*DIGIT (ASCII only; str.isdigit alone accepts other Unicode digits)."""
    return 0 < len(value) <= 10 and value.isascii() and value.isdigit()
