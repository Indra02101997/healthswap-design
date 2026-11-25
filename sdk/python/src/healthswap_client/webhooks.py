"""Webhook signatures: `HealthSwap-Signature: t=<unix-seconds>,v1=<hex HMAC-SHA256(secret, t + "." + rawBody)>`.

Verify against the raw request body bytes exactly as received (before any JSON parsing), reject stale signatures, then
deduplicate deliveries on the envelope `id` (delivery is at-least-once).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime

__all__ = ["SIGNATURE_HEADER", "sign_webhook", "verify_webhook"]

SIGNATURE_HEADER = "HealthSwap-Signature"
_MAX_HEADER = 4096


def _body_bytes(body: bytes | bytearray | memoryview | str) -> bytes:
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)


def _mac(secret: str | bytes, timestamp: int, body: bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    return hmac.new(key, str(timestamp).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def sign_webhook(secret: str | bytes, body: bytes | str, timestamp: int | None = None) -> str:
    """The HealthSwap-Signature header value for `body` (for testing a webhook receiver)."""
    if not secret:
        raise ValueError("secret is required")
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={ts},v1={_mac(secret, ts, _body_bytes(body))}"


def verify_webhook(secret: str | bytes, signature_header: str | None, body: bytes | str, tolerance_seconds: float = 300,
                   now: float | datetime | None = None) -> bool:
    """True when `signature_header` holds a v1 signature of `body` made with `secret` whose timestamp is within
    `tolerance_seconds` of `now` (default: the current time; both stale and future timestamps are rejected).

    `body` is the raw request body (bytes; a str is encoded as UTF-8). Several v1 values are allowed (secret rotation) and
    every comparison is constant-time. A missing or malformed header, or a repeated t, is invalid.
    """
    if not secret:
        raise ValueError("secret is required")
    if not signature_header or len(signature_header) > _MAX_HEADER:
        return False
    timestamp: int | None = None
    candidates: list[str] = []
    for part in signature_header.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "t":
            if timestamp is not None or not (value.isascii() and value.isdigit()) or len(value) > 12:
                return False
            timestamp = int(value)
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        return False
    current = time.time() if now is None else now.timestamp() if isinstance(now, datetime) else float(now)
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = _mac(secret, timestamp, _body_bytes(body)).encode("ascii")
    matched = False
    for candidate in candidates:  # no early exit: every candidate is compared
        matched |= hmac.compare_digest(expected, candidate.encode("utf-8", "replace"))
    return matched
