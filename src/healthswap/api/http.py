"""Http.cs ported: RFC 9457 problems, the problem / authentication / rate-limit middlewares, JSON responses, the
Idempotency-Key helper, query binding, and the per-route pipeline (authorization, then request validation).

Pipeline (C# Program.cs order): CORS ("sdk" policy) -> ProblemMiddleware -> AuthenticationMiddleware -> RateLimitMiddleware ->
route -> `pipeline()` dependency (authorization, then contract validation / JSON binding) -> endpoint. CORS sits outside
the problem middleware so error responses carry the CORS headers too (C# CorsMiddleware applies them when the response
starts). Headers that must survive a failure - RateLimit-*, Retry-After, WWW-Authenticate - are queued with
`add_response_header` and merged into whatever response is finally sent, like C# headers set before the failure.

Endpoints are synchronous `def handler(request) -> Response` functions (FastAPI runs them on the thread pool) because the
platform services are synchronous; the pipeline dependency is async and buffers / validates the body on the event loop.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from healthswap.api.auth import DevIdentityProvider, InvalidToken, Principal, TokenKeys, TokenValidator
from healthswap.api.contract import OpenApiContract, parse_json
from healthswap.platform import jsonutil, netjson
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.services.infrastructure import IdempotentResponse
from healthswap.platform.settings import RateLimitOptions, Settings
from healthswap.platform.tenancy import PartnerContext

if TYPE_CHECKING:
    from healthswap.api.partner_routes import BackgroundJobs
    from healthswap.platform.container import Container

log = logging.getLogger(__name__)

PRINCIPAL = "hs.principal"
AUTH_FAILURE = "hs.auth_failure"
TRACE_ID = "hs.trace_id"
RESPONSE_HEADERS = "hs.response_headers"

MAX_BODY_BYTES = 10 * 1024 * 1024
JSON_MEDIA_TYPE = "application/json; charset=utf-8"
PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://healthswap.example/problems/"


class BadRequest(Exception):
    """A binding failure (C# BadHttpRequestException): written as a problem with code HS-REQ-INVALID."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------------------------------------------------
# Shared state (the C# service provider as the endpoints see it)
# ---------------------------------------------------------------------------------------------------------------------


class ApiState:
    """Everything the endpoints share: settings, the platform container, the contract, keys, token validation, jobs."""

    def __init__(self, settings: Settings, container: Container, contract: OpenApiContract, keys: TokenKeys, validator: TokenValidator,
                 jobs: BackgroundJobs) -> None:
        self.settings = settings
        self.container = container
        self.contract = contract
        self.keys = keys
        self.validator = validator
        self.jobs = jobs
        self._idp: DevIdentityProvider | None = None
        self._lock = threading.Lock()

    @property
    def idp(self) -> DevIdentityProvider:
        """The development identity provider, created on first use (hashing the client secrets takes ~0.1 s each)."""
        if self._idp is None:
            with self._lock:
                if self._idp is None:
                    self._idp = DevIdentityProvider(self.container.db, self.keys, self.settings.auth, self.settings.repo_root)
        return self._idp


def api_state(request: Request) -> ApiState:
    return request.app.state.hs


# ---------------------------------------------------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------------------------------------------------

_TRACEPARENT_RX = re.compile(r"([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _incoming_trace_id(scope: Scope) -> str | None:
    """The trace id of a valid W3C traceparent header (C# Activity.Current.TraceId continues the caller's trace)."""
    value = _header(scope, b"traceparent")
    if value is None:
        return None
    m = _TRACEPARENT_RX.fullmatch(value.strip().lower())
    if m is None or m.group(1) == "ff" or (m.group(1) == "00" and m.group(5)):
        return None
    if set(m.group(2)) == {"0"} or set(m.group(3)) == {"0"}:
        return None
    return m.group(2)


def trace_id(scope: Scope) -> str:
    """The request's trace id (32 hex digits), assigned once per request."""
    value = scope.get(TRACE_ID)
    if value is None:
        value = _incoming_trace_id(scope) or uuid.uuid4().hex
        scope[TRACE_ID] = value
    return value


def add_response_header(scope: Scope, name: str, value: str) -> None:
    """Queues a header for the response that is eventually sent, including an error response written later."""
    scope.setdefault(RESPONSE_HEADERS, []).append((name.lower().encode("latin-1"), value.encode("latin-1")))


def problem(scope: Scope, status: int, code: str, title: str, detail: str | None, errors: Sequence[FieldError] | None = None) -> dict[str, Any]:
    """The C# Problem record (property order type, title, status, detail, instance, code, traceId, errors)."""
    return {
        "type": PROBLEM_TYPE_BASE + code.lower(), "title": title, "status": status, "detail": detail, "instance": scope.get("path") or "/",
        "code": code, "traceId": trace_id(scope), "errors": None if errors is None else [e.to_api() for e in errors],
    }


def problem_response(scope: Scope, status: int, code: str, title: str, detail: str | None, errors: Sequence[FieldError] | None = None) -> Response:
    body = netjson.serialize_hs(problem(scope, status, code, title, detail, errors))
    return Response(body.encode("utf-8"), status_code=status, media_type=PROBLEM_MEDIA_TYPE)


def error_response(scope: Scope, ex: HsError) -> Response:
    return problem_response(scope, ex.status, ex.code, ex.title, ex.detail, ex.errors)


class ProblemMiddleware:
    """Turns exceptions into RFC 9457 problems (C# ProblemMiddleware) and merges the queued response headers."""

    def __init__(self, app: ASGIApp, is_development: bool = False) -> None:
        self.app = app
        self.is_development = is_development

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id(scope)
        extra: list[tuple[bytes, bytes]] = scope.setdefault(RESPONSE_HEADERS, [])
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                if extra:
                    headers = list(message.get("headers") or ())
                    present = {k.lower() for k, _ in headers}
                    headers.extend((k, v) for k, v in extra if k not in present)
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except ClientDisconnect:
            return  # the client went away
        except Exception as ex:
            status, code, title, detail, errors = self._map(scope, ex)
            if started:
                log.warning("%s %s failed after the response started", scope.get("method"), scope.get("path"), exc_info=status >= 500)
                return
            await problem_response(scope, status, code, title, detail, errors)(scope, receive, send_with_headers)

    def _map(self, scope: Scope, ex: Exception) -> tuple[int, str, str, str | None, list[FieldError] | None]:
        if isinstance(ex, HsError):
            return ex.status, ex.code, ex.title, ex.detail, ex.errors
        if isinstance(ex, BadRequest):
            return ex.status, "HS-REQ-INVALID", "Request validation failed", str(ex), None
        if isinstance(ex, json.JSONDecodeError):
            return 400, "HS-REQ-INVALID", "Request validation failed", str(ex), None
        log.error("Unhandled error for %s %s", scope.get("method"), scope.get("path"), exc_info=ex)
        detail = (str(ex) or type(ex).__name__) if self.is_development else "An unexpected error occurred"
        return 500, "HS-INTERNAL", "Internal error", detail, None


# ---------------------------------------------------------------------------------------------------------------------
# Authentication and authorization
# ---------------------------------------------------------------------------------------------------------------------


def _bearer_token(scope: Scope) -> str | None:
    value = _header(scope, b"authorization")
    if value is None or not value[:7].lower() == "bearer ":
        return None
    return value[7:].strip() or None


class AuthenticationMiddleware:
    """Validates a bearer token when one is sent (C# UseAuthentication): success stores the Principal in the scope, a
    failure stores the reason for the 401 challenge. Requests are never rejected here (anonymous endpoints stay open)."""

    def __init__(self, app: ASGIApp, validator: TokenValidator) -> None:
        self.app = app
        self.validator = validator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = _bearer_token(scope)
            if token is not None:
                try:
                    if self.validator.uses_authority:  # may fetch the authority's metadata and keys
                        claims = await anyio.to_thread.run_sync(self.validator.validate, token)
                    else:
                        claims = self.validator.validate(token)
                    scope[PRINCIPAL] = Principal.from_claims(claims)
                except InvalidToken as ex:
                    scope[AUTH_FAILURE] = ex.description
        await self.app(scope, receive, send)


def principal_of(request: Request | Scope) -> Principal | None:
    scope = request.scope if isinstance(request, Request) else request
    return scope.get(PRINCIPAL)


def partner_of(request: Request) -> PartnerContext:
    """The calling partner (C# http.Partner()): 403 for tokens without a partner (admin tokens)."""
    principal = principal_of(request)
    ctx = principal.partner if principal is not None else None
    if ctx is None:
        raise HsError(403, "HS-AUTH-SCOPE", "Forbidden", "This endpoint requires a partner token")
    return ctx


def _quoted(text: str) -> str:
    """An RFC 6750 quoted-string value (printable ASCII without '"' and '\\')."""
    return "".join(c if " " <= c <= "~" and c not in '"\\' else "?" for c in text)


def base_url(request: Request | Scope) -> str:
    """{scheme}://{host}{root path} of the request (C# Http.BaseUrl)."""
    scope = request.scope if isinstance(request, Request) else request
    host = _header(scope, b"host")
    if not host:
        server = scope.get("server") or ("localhost", None)
        host = server[0] if server[1] in (None, 80, 443) else f"{server[0]}:{server[1]}"
    return f"{scope.get('scheme', 'http')}://{host}{scope.get('root_path', '')}"


def challenge(scope: Scope) -> HsError:
    """The 401 problem for a missing or invalid token. Sets WWW-Authenticate (RFC 6750; /mcp adds the RFC 9728
    resource_metadata URL)."""
    failure: str | None = scope.get(AUTH_FAILURE)
    params: list[str] = []
    path = (scope.get("path") or "").lower()
    if path == "/mcp" or path.startswith("/mcp/"):
        params.append(f'resource_metadata="{base_url(scope)}/.well-known/oauth-protected-resource"')
    if failure is not None:
        params.extend(('error="invalid_token"', f'error_description="{_quoted(failure)}"'))
    add_response_header(scope, "WWW-Authenticate", "Bearer" + (" " + ", ".join(params) if params else ""))
    return HsError(401, "HS-AUTH-INVALID", "Unauthorized", failure or "A valid bearer token is required")


def authorize(scope: Scope, any_of: tuple[str, ...]) -> Principal:
    """RequireAuthenticatedUser + a scope assertion (C# "scope:x", "admin" and "mcp" policies)."""
    principal: Principal | None = scope.get(PRINCIPAL)
    if principal is None:
        raise challenge(scope)
    if not principal.has_any_scope(any_of):
        raise HsError(403, "HS-AUTH-SCOPE", "Forbidden", "The token lacks the scope required by this operation")
    return principal


# ---------------------------------------------------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Bucket:
    tokens: float
    stamp: float
    gate: threading.Lock


class RateLimiter:
    """Token buckets per caller and route group (thread-safe; the clock is injectable for tests)."""

    def __init__(self, options: RateLimitOptions, clock: Callable[[], float] = time.monotonic) -> None:
        self.options = options
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def limits(self, path: str) -> tuple[str, int, int]:
        """(group, rate per second, capacity) for a /v1 path."""
        group = "eval" if path.startswith("/v1/cart-evaluations") else "default"
        rate = self.options.cart_evaluations_per_second if group == "eval" else self.options.default_per_second
        return group, rate, rate * max(1, self.options.burst_multiplier)

    def take(self, key: str, rate: int, capacity: int) -> tuple[bool, float]:
        """Refills by elapsed time x rate (capped at capacity) and takes one token. Returns (allowed, remaining)."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(float(capacity), self.clock(), threading.Lock())
        with bucket.gate:
            now = self.clock()
            bucket.tokens = min(float(capacity), bucket.tokens + (now - bucket.stamp) * rate)
            bucket.stamp = now
            allowed = bucket.tokens >= 1
            if allowed:
                bucket.tokens -= 1
            return allowed, bucket.tokens

    @staticmethod
    def headers(rate: int, capacity: int, remaining: float) -> dict[str, str]:
        """IETF RateLimit-* headers as the C# middleware writes them."""
        reset = max(1, math.ceil((capacity - remaining) / rate)) if rate > 0 else 1
        return {"RateLimit-Limit": str(capacity), "RateLimit-Remaining": str(math.floor(remaining)), "RateLimit-Reset": str(reset)}


class RateLimitMiddleware:
    """Token bucket per partner (or client id, or client IP) and route group for /v1 paths; 429 HS-RATE-LIMITED."""

    def __init__(self, app: ASGIApp, options: RateLimitOptions, limiter: RateLimiter | None = None) -> None:
        self.app = app
        self.options = options
        self.limiter = limiter or RateLimiter(options)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path") or ""
        if scope["type"] != "http" or not self.options.enabled or not path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        group, rate, capacity = self.limiter.limits(path)
        principal: Principal | None = scope.get(PRINCIPAL)
        who = None
        if principal is not None:
            who = principal.claims.get("partner_id") or principal.claims.get("client_id")
        if not who:
            client = scope.get("client")
            who = client[0] if client else "anonymous"
        allowed, remaining = self.limiter.take(f"{who}:{group}", rate, capacity)
        for name, value in RateLimiter.headers(rate, capacity, remaining).items():
            add_response_header(scope, name, value)
        if not allowed:
            add_response_header(scope, "Retry-After", "1")
            response = problem_response(scope, 429, "HS-RATE-LIMITED", "Too many requests", f"Limit is {rate} requests per second ({group})")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------------------------------------------------


def serialize(body: Any) -> str:
    """HsJson text of an API value: camelCase dicts as given, None members dropped, decimals with their scale, UTF-8."""
    return netjson.serialize_hs(jsonutil.to_plain(body, keep_decimal=True))


def json_response(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    """C# Http.Json(body, status): application/json; charset=utf-8."""
    return Response(serialize(body).encode("utf-8"), status_code=status, media_type=JSON_MEDIA_TYPE, headers=headers)


def empty_response(status: int) -> Response:
    """Results.Accepted() / Results.NoContent() / Results.NotFound(): no body."""
    return Response(status_code=status)


def idempotent(request: Request, required: bool, handler: Callable[[], tuple[int, Any]]) -> Response:
    """Runs a resource-creating handler under the Idempotency-Key contract (HLD 8.2). The raw body was buffered by the
    pipeline so replays are compared byte for byte; a replay carries `Idempotent-Replayed: true`."""
    key = request.headers.get("idempotency-key")
    if key is None or not key.strip():
        if required:
            raise HsError.invalid("The Idempotency-Key header is required for this operation")
        status, body = handler()
        return json_response(body, status)
    partner = partner_of(request)
    raw: bytes = getattr(request.state, "raw_body", None) or b""
    replayed = True

    def run() -> IdempotentResponse:
        nonlocal replayed
        replayed = False
        status, body = handler()
        return IdempotentResponse(status, serialize(body))

    service = api_state(request).container.idempotency_service
    response = service.execute(partner.partner_id, key, request.method, request.scope.get("path") or "", raw, run)
    headers = {"Idempotent-Replayed": "true"} if replayed else None
    media_type = PROBLEM_MEDIA_TYPE if response.status >= 400 else "application/json"
    return Response(response.body.encode("utf-8"), status_code=response.status, media_type=media_type, headers=headers)


def body_of(request: Request) -> Any:
    """The JSON body buffered by the pipeline (contract-validated for /v1 operations)."""
    return getattr(request.state, "body", None)


# ---------------------------------------------------------------------------------------------------------------------
# Query binding (C# minimal API binding errors)
# ---------------------------------------------------------------------------------------------------------------------

_INT_RX = re.compile(r"\s*[+-]?[0-9]+\s*")


def query_str(request: Request, name: str, *, required: bool = False) -> str | None:
    values = request.query_params.getlist(name)
    if not values or (required and values == [""]):
        if required:
            raise BadRequest(f'Required parameter "string {name}" was not provided from query string.')
        return None
    return ",".join(values)


def query_int(request: Request, name: str) -> int | None:
    raw = query_str(request, name)
    if raw is None or raw == "":
        return None
    if _INT_RX.fullmatch(raw) is None or not -(2**31) <= int(raw) <= 2**31 - 1:
        raise BadRequest(f'Failed to bind parameter "Nullable<int> {name}" from "{raw}".')
    return int(raw)


def query_uuid(request: Request, name: str, *, required: bool = False) -> uuid.UUID | None:
    raw = query_str(request, name)
    label = "Guid" if required else "Nullable<Guid>"
    if raw is None or raw == "":
        if required:
            raise BadRequest(f'Required parameter "{label} {name}" was not provided from query string.')
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise BadRequest(f'Failed to bind parameter "{label} {name}" from "{raw}".') from None


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


# ---------------------------------------------------------------------------------------------------------------------
# Body buffering and the per-route pipeline
# ---------------------------------------------------------------------------------------------------------------------


async def read_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The request body, at most `limit` bytes (HS-REQ-TOO-LARGE above it)."""
    cached = getattr(request.state, "raw_body", None)
    if cached is not None:
        return cached
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HsError.too_large("Request bodies are limited to 10 MB")
        chunks.append(chunk)
    raw = b"".join(chunks)
    request.state.raw_body = raw
    return raw


def _is_json_media_type(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


async def _validate_contract(state: ApiState, request: Request, operation_id: str) -> None:
    """C# RequestValidationMiddleware: 415 for non-JSON, 400 for malformed JSON or contract violations."""
    content_type = request.headers.get("content-type") or ""
    if not content_type.lower().startswith("application/json"):
        raise HsError(415, "HS-UNSUPPORTED-MEDIA-TYPE", "Unsupported media type", "Send application/json")
    raw = await read_body(request)
    try:
        document = parse_json(raw)
    except ValueError as ex:
        raise HsError.invalid(f"Malformed JSON: {ex}") from None
    errors = await anyio.to_thread.run_sync(state.contract.validate_request, operation_id, document)
    if errors:
        raise HsError.invalid("The request body does not match the API contract", errors)
    request.state.body = document


async def _bind_json(request: Request, parameter: str, allow_null: bool) -> None:
    """C# [FromBody] binding for routes without a contract schema (admin and demo endpoints). `parameter` is the C#
    parameter as the binding errors name it, for example "KillSwitchRequest req"."""
    raw = await read_body(request)
    content_type = request.headers.get("content-type") or ""
    if not raw.strip():
        raise BadRequest(f'Required parameter "{parameter}" was not provided from body.')
    if not _is_json_media_type(content_type):
        raise BadRequest(f'Expected a supported JSON media type but got "{content_type}".', 415)
    try:
        document = parse_json(raw)
    except ValueError:
        raise BadRequest(f'Failed to read parameter "{parameter}" from the request body as JSON.') from None
    if document is None and not allow_null:
        raise BadRequest(f'Required parameter "{parameter}" was not provided from body.')
    request.state.body = document


def pipeline(operation_id: str | None, scopes: tuple[str, ...] | None, body: str | None = None,
             allow_null_body: bool = False) -> Callable[[Request], Awaitable[None]]:
    """The per-route part of the C# pipeline: authorization (None = anonymous), then request validation against the
    contract (POST/PUT/PATCH operations with a request schema) or JSON binding (`body` = the C# [FromBody] parameter,
    for example "ClockRequest req")."""

    async def run(request: Request) -> None:
        if scopes is not None:
            authorize(request.scope, scopes)
        state = api_state(request)
        if request.method in ("POST", "PUT", "PATCH") and operation_id is not None and state.contract.request_schema(operation_id) is not None:
            await _validate_contract(state, request, operation_id)
        elif body is not None:
            await _bind_json(request, body, allow_null_body)

    return run
