"""HealthSwapClient: every operation of the HealthSwap Partner API (api/openapi.yaml) for server-side Python."""

from __future__ import annotations

import base64
import concurrent.futures
import ipaddress
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from healthswap_client.errors import HealthSwapError, retry_after_seconds
from healthswap_client.resilience import CircuitBreaker, backoff_delay

__all__ = ["VERSION", "HealthSwapClient"]

VERSION = "0.1.0"
log = logging.getLogger("healthswap_client")

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_MAX_RETRY_AFTER = 60.0  # longer server-requested waits are not slept through: the error is raised instead
_EVENT_BATCH_LIMIT = 500  # EventBatchRequest.events maxItems


def _segment(value: Any) -> str:
    """One percent-encoded path segment (a "/" or ".." in an id can never change the route)."""
    text = str(value)
    if not text:
        raise ValueError("path parameters must not be empty")
    return quote(text, safe="")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, uuid.UUID)):
        return str(value) if isinstance(value, uuid.UUID) else value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _encode(body: Any) -> bytes:
    return json.dumps(body, default=_json_default, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(slots=True)
class _Token:
    value: str
    refresh_at: float  # from here on a background refresh runs while requests keep using this token
    expires_at: float  # from here on the token is not used any more


class _TokenCache:
    """Thread-safe OAuth 2.0 client-credentials token cache.

    A token is used until shortly before it expires. In its last part (the last 2 minutes of a 10-minute token) one background
    thread fetches the next token while requests keep using the current one, so a refresh never delays a request; only a
    missing or expired token makes callers wait (one fetch for all waiting threads)."""

    _RETRY_REFRESH_AFTER = 5.0

    def __init__(self, fetch: Callable[[], tuple[str, float]], clock: Callable[[], float]) -> None:
        self._fetch = fetch
        self._clock = clock
        self._token: _Token | None = None
        self._fetch_lock = threading.Lock()  # one fetch at a time
        self._state_lock = threading.Lock()
        self._refreshing = False
        self._next_refresh = 0.0

    def get(self) -> str:
        token = self._token
        now = self._clock()
        if token is not None and now < token.expires_at:
            if now >= token.refresh_at:
                self._refresh_in_background(token, now)
            return token.value
        with self._fetch_lock:
            current = self._token
            if current is not None and self._clock() < current.expires_at:  # another thread fetched it meanwhile
                return current.value
            self._token = self._new_token()
            return self._token.value

    def invalidate(self, value: str) -> None:
        """Forgets `value` (the server rejected it); the next get() fetches a new token."""
        with self._state_lock:
            if self._token is not None and self._token.value == value:
                self._token = None

    def _new_token(self) -> _Token:
        started = self._clock()
        value, expires_in = self._fetch()
        lifetime = max(0.0, expires_in)
        return _Token(value, refresh_at=started + max(lifetime - 120.0, lifetime * 0.5), expires_at=started + lifetime - min(30.0, lifetime * 0.1))

    def _refresh_in_background(self, token: _Token, now: float) -> None:
        with self._state_lock:
            if self._refreshing or now < self._next_refresh:
                return
            self._refreshing = True
            self._next_refresh = now + self._RETRY_REFRESH_AFTER
        threading.Thread(target=self._refresh, args=(token,), name="healthswap-token-refresh", daemon=True).start()

    def _refresh(self, token: _Token) -> None:
        try:
            with self._fetch_lock:
                if self._token is token or self._token is None:
                    self._token = self._new_token()
        except Exception as ex:  # keep using the current token until it expires; the next request retries the refresh
            log.warning("HealthSwap token refresh failed: %s", ex)
        finally:
            with self._state_lock:
                self._refreshing = False


class HealthSwapClient:
    """Client for the HealthSwap Partner API (server-to-server, OAuth 2.0 client credentials).

    `base_url` is the API root, for example https://api.in.healthswap.example (or .../v1) or http://127.0.0.1:8080 for
    the local reference implementation; tokens come from `token_url` (default {base_url}/oauth2/token). Methods take and
    return plain dicts in the API's JSON shape (camelCase); errors raise HealthSwapError with the problem details.

    * `evaluate_cart` fails open: on a timeout (`evaluation_timeout`, default 150 ms, a hard deadline that includes the token
      and DNS), a connection error, 5xx, 429 or an open circuit breaker it returns an evaluation with no suggestions,
      `degraded: True` and `failOpen: {"reason": ..., "detail": ..., "status": ...}` instead of raising. The breaker opens
      after `circuit_breaker.failure_threshold` consecutive failures and lets one probe through after its cool-down.
    * Other calls use `timeout` and are retried up to `max_retries` times with exponential backoff and jitter when they
      are safe to repeat: GET, PUT and DELETE, POSTs with an Idempotency-Key, and event batches (the server deduplicates
      on eventId). Retry-After / RateLimit-Reset are honored. Create operations get an Idempotency-Key automatically
      unless you pass one. A 401 is answered once with a fresh token.

    Thread-safe; use one client per process and close it (or use it as a context manager).
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str, *, scopes: Sequence[str] | None = None, timeout: float = 5.0,
                 evaluation_timeout: float = 0.15, max_retries: int = 2, transport: httpx.BaseTransport | None = None, token_url: str | None = None,
                 circuit_breaker: CircuitBreaker | None = None, allow_insecure_http: bool = False, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required")
        if timeout <= 0 or evaluation_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        root = base_url.strip().rstrip("/")
        parts = urlsplit(root)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        self._api_root = root if root.endswith("/v1") else root + "/v1"
        self._token_url = token_url or (root[: -len("/v1")] if root.endswith("/v1") else root) + "/oauth2/token"
        for url in (self._api_root, self._token_url):
            u = urlsplit(url)
            if u.scheme != "https" and not allow_insecure_http and not _is_loopback(u.hostname or ""):
                raise ValueError(f"{url}: use https (plain http is only allowed for localhost unless allow_insecure_http=True)")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = " ".join(scopes) if scopes else None
        self.timeout = float(timeout)
        self.evaluation_timeout = float(evaluation_timeout)
        self.max_retries = int(max_retries)
        self._sleep = sleep
        self._clock = clock
        self.circuit_breaker = circuit_breaker or CircuitBreaker(clock=clock)
        self._http = httpx.Client(transport=transport, timeout=self.timeout, follow_redirects=False,
                                  headers={"User-Agent": f"healthswap-client-python/{VERSION}", "Accept": "application/json"})
        self._tokens = _TokenCache(self._fetch_token, clock)
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------------------------------------------

    def __enter__(self) -> HealthSwapClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Releases the connection pool and the evaluation threads; in-flight evaluations are abandoned."""
        self._closed = True
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        self._http.close()

    # ------------------------------------------------------------------------------------------------------------------
    # Transport: tokens, retries, problems
    # ------------------------------------------------------------------------------------------------------------------

    def _fetch_token(self) -> tuple[str, float]:
        """POST {token_url} (client_credentials, client_secret_basic): (access_token, expires_in seconds)."""
        credentials = f"{quote(self._client_id, safe='')}:{quote(self._client_secret, safe='')}"
        form = {"grant_type": "client_credentials"}
        if self._scopes:
            form["scope"] = self._scopes
        headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")}
        response = self._send("POST", self._token_url, data=form, headers=headers, retryable=True, authenticate=False)
        try:
            token = response.json()
            value, expires_in = token["access_token"], float(token.get("expires_in") or 300)
        except (ValueError, KeyError, TypeError) as ex:
            raise HealthSwapError(response.status_code, "HS-SDK-TOKEN", "Invalid token response", str(ex)) from None
        if not isinstance(value, str) or not value:
            raise HealthSwapError(response.status_code, "HS-SDK-TOKEN", "Invalid token response", "access_token is missing")
        return value, expires_in

    def _send(self, method: str, url: str, *, params: Mapping[str, Any] | None = None, content: bytes | None = None,
              data: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None, retryable: bool = False, authenticate: bool = True,
              timeout: float | None = None) -> httpx.Response:
        """One logical request: bearer token (re-fetched once after a 401), retries of retryable requests on transport errors
        and 408/425/429/5xx, HealthSwapError for every non-success outcome."""
        attempt = 0
        reauthenticated = False
        while True:
            if self._closed:
                raise RuntimeError("The HealthSwapClient is closed")
            request_headers = dict(headers or {})
            token = None
            if authenticate:
                token = self._tokens.get()
                request_headers["Authorization"] = "Bearer " + token
            if content is not None:
                request_headers["Content-Type"] = "application/json"
            try:
                response = self._http.request(method, url, params=params, content=content, data=data, headers=request_headers,
                                              timeout=self.timeout if timeout is None else timeout)
            except httpx.TransportError as ex:
                if retryable and attempt < self.max_retries:
                    delay = backoff_delay(attempt)
                    log.info("HealthSwap %s %s failed (%s); retry %d in %.2f s", method, url, type(ex).__name__, attempt + 1, delay)
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise HealthSwapError.transport(ex) from ex
            if response.status_code == 401 and token is not None and not reauthenticated:
                self._tokens.invalidate(token)  # rejected before the operation ran: safe to repeat once with a new token
                reauthenticated = True
                continue
            if response.is_success:
                return response
            if retryable and response.status_code in _RETRYABLE_STATUSES and attempt < self.max_retries:
                wait = retry_after_seconds(response.headers)
                delay = backoff_delay(attempt) if wait is None else wait
                if delay <= _MAX_RETRY_AFTER:
                    log.info("HealthSwap %s %s answered %d; retry %d in %.2f s", method, url, response.status_code, attempt + 1, delay)
                    self._sleep(delay)
                    attempt += 1
                    continue
            raise HealthSwapError.from_response(response)

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None, idempotency_key: str | None = None,
                retry: bool | None = None) -> Any:
        """Calls any API operation: `path` below the API root (for example "/orders/ORD-1"), `params` (None values dropped),
        `json` body. Retries follow the client's policy unless `retry` says otherwise. Returns the parsed JSON (None for an
        empty response)."""
        method = method.upper()
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        if retry is None:
            retry = method in _SAFE_METHODS or (method == "POST" and bool(idempotency_key))
        query = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in (params or {}).items() if v is not None}
        response = self._send(method, self._api_root + "/" + path.lstrip("/"), params=query or None,
                              content=None if json is None else _encode(json), headers=headers, retryable=retry)
        return response.json() if response.content else None

    def _create(self, path: str, body: Mapping[str, Any], idempotency_key: str | None) -> Any:
        """POST of a create operation with an Idempotency-Key (generated when not given), so it is safe to retry."""
        return self.request("POST", path, json=body, idempotency_key=idempotency_key or str(uuid.uuid4()))

    # ------------------------------------------------------------------------------------------------------------------
    # SDK sessions
    # ------------------------------------------------------------------------------------------------------------------

    def create_sdk_session(self, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /sdk/sessions: a short-lived token for the browser / app SDK, bound to cartRef (and userRef, storeId)."""
        return self._create("/sdk/sessions", request, idempotency_key)

    # ------------------------------------------------------------------------------------------------------------------
    # Catalog and inventory
    # ------------------------------------------------------------------------------------------------------------------

    def upsert_catalog_items(self, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /catalog/item-batches (up to 1,000 items)."""
        return self._create("/catalog/item-batches", request, idempotency_key)

    def create_catalog_feed(self, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /catalog/feeds: a bulk feed job with a pre-signed upload URL."""
        return self._create("/catalog/feeds", request, idempotency_key)

    def get_catalog_feed(self, feed_id: str | uuid.UUID) -> dict[str, Any]:
        return self.request("GET", f"/catalog/feeds/{_segment(feed_id)}")

    def get_catalog_item(self, partner_sku: str) -> dict[str, Any]:
        return self.request("GET", f"/catalog/items/{_segment(partner_sku)}")

    def delist_catalog_item(self, partner_sku: str) -> None:
        self.request("DELETE", f"/catalog/items/{_segment(partner_sku)}")

    def override_catalog_match(self, partner_sku: str, request: Mapping[str, Any]) -> dict[str, Any]:
        """PUT /catalog/items/{partnerSku}/match (CONFIRM, REJECT or ASSIGN)."""
        return self.request("PUT", f"/catalog/items/{_segment(partner_sku)}/match", json=request)

    def upsert_store_inventory(self, store_id: str, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /stores/{storeId}/inventory-batches (MIRROR availability mode)."""
        return self._create(f"/stores/{_segment(store_id)}/inventory-batches", request, idempotency_key)

    # ------------------------------------------------------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------------------------------------------------------

    def lookup_product(self, market: str, *, gtin: str | None = None, partner_sku: str | None = None, locale: str | None = None) -> dict[str, Any]:
        return self.request("GET", "/products/lookup", params={"market": market, "gtin": gtin, "partnerSku": partner_sku, "locale": locale})

    def get_product_health_profile(self, product_id: str | uuid.UUID, market: str) -> dict[str, Any]:
        return self.request("GET", f"/products/{_segment(product_id)}", params={"market": market})

    def list_product_alternatives(self, product_id: str | uuid.UUID, market: str, *, store_id: str | None = None, cart_ref: str | None = None,
                                  user_ref: str | None = None, placement: str | None = None, currency: str | None = None, locale: str | None = None,
                                  limit: int | None = None) -> dict[str, Any]:
        return self.request("GET", f"/products/{_segment(product_id)}/alternatives", params={
            "market": market, "storeId": store_id, "cartRef": cart_ref, "userRef": user_ref, "placement": placement, "currency": currency,
            "locale": locale, "limit": limit,
        })

    # ------------------------------------------------------------------------------------------------------------------
    # Cart evaluations (fail-open)
    # ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _fail_open(request: Mapping[str, Any], reason: str, detail: str, status: int | None = None) -> dict[str, Any]:
        return {
            "evaluationId": None, "cartRef": request.get("cartRef"), "market": request.get("market"), "storeId": request.get("storeId"),
            "degraded": True, "degradationReasons": [], "suggestions": [], "lineLabels": [],
            "failOpen": {"reason": reason, "detail": detail, "status": status},
        }

    def _pool(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._executor_lock:
            if self._closed:
                raise RuntimeError("The HealthSwapClient is closed")
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="healthswap-evaluate")
            return self._executor

    def _evaluate_once(self, body: bytes, idempotency_key: str | None, budget: float, deadline: float) -> httpx.Response | HealthSwapError:
        """Runs on an evaluation thread: the POST (and the token, a 401 re-authentication within the deadline). Returns the
        response, or the error of a request that got none."""
        url = self._api_root + "/cart-evaluations"
        reauthenticated = False
        while True:
            try:
                token = self._tokens.get()
                headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key
                response = self._http.post(url, content=body, headers=headers, timeout=budget)
            except httpx.TransportError as ex:
                return HealthSwapError.transport(ex)
            except HealthSwapError as ex:  # the token request failed
                return ex
            if response.status_code == 401 and not reauthenticated and self._clock() < deadline:
                self._tokens.invalidate(token)
                reauthenticated = True
                continue
            return response

    def evaluate_cart(self, request: Mapping[str, Any], *, idempotency_key: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        """POST /cart-evaluations, failing open (see the class documentation): never blocks longer than `timeout` (default
        `evaluation_timeout`) and never raises for timeouts, connection errors, 5xx, 429 or an open circuit. Other errors
        (400, 401 after re-authentication, 403, 422) raise HealthSwapError: they are bugs or configuration problems."""
        budget = self.evaluation_timeout if timeout is None else float(timeout)
        if budget <= 0:
            raise ValueError("timeout must be positive")
        body = _encode(request)
        permit = self.circuit_breaker.acquire()
        if permit is None:
            return self._fail_open(request, "CIRCUIT_OPEN", "Cart evaluation is paused after repeated failures")
        deadline = self._clock() + budget
        future = self._pool().submit(self._evaluate_once, body, idempotency_key, budget, deadline)
        try:
            outcome = future.result(timeout=budget)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self.circuit_breaker.record(permit, False)
            return self._fail_open(request, "TIMEOUT", f"No response within {budget * 1000:.0f} ms")
        except Exception as ex:  # an unexpected failure on the evaluation thread
            self.circuit_breaker.record(permit, False)
            log.warning("HealthSwap cart evaluation failed: %r", ex)
            return self._fail_open(request, "CONNECTION_ERROR", f"{type(ex).__name__}: {ex}")
        if isinstance(outcome, HealthSwapError):
            if outcome.status is None:  # no response: timeout or connection failure
                self.circuit_breaker.record(permit, False)
                reason = "TIMEOUT" if outcome.code == "HS-SDK-TIMEOUT" else "CONNECTION_ERROR"
                return self._fail_open(request, reason, outcome.detail or outcome.title)
            if outcome.status == 429 or outcome.status >= 500:  # the token endpoint is overloaded or failing
                self.circuit_breaker.record(permit, False)
                return self._fail_open(request, "RATE_LIMITED" if outcome.status == 429 else "SERVER_ERROR", str(outcome), outcome.status)
            self.circuit_breaker.record(permit, True)
            raise outcome
        response = outcome
        status = response.status_code
        if status == 429 or status >= 500:
            self.circuit_breaker.record(permit, False)
            error = HealthSwapError.from_response(response)
            return self._fail_open(request, "RATE_LIMITED" if status == 429 else "SERVER_ERROR", str(error), status)
        if not response.is_success:
            self.circuit_breaker.record(permit, True)  # the service answered: a client-side problem, not an outage
            raise HealthSwapError.from_response(response)
        try:
            evaluation = response.json()
        except ValueError:
            self.circuit_breaker.record(permit, False)
            return self._fail_open(request, "SERVER_ERROR", "The evaluation response is not JSON", status)
        if not isinstance(evaluation, dict):
            self.circuit_breaker.record(permit, False)
            return self._fail_open(request, "SERVER_ERROR", "The evaluation response is not a JSON object", status)
        self.circuit_breaker.record(permit, True)
        return evaluation

    def get_cart_evaluation(self, evaluation_id: str | uuid.UUID) -> dict[str, Any]:
        return self.request("GET", f"/cart-evaluations/{_segment(evaluation_id)}")

    # ------------------------------------------------------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------------------------------------------------------

    def report_suggestion_events(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """POST /events with an EventBatchRequest ({"events": [...]}, up to 500). Retried like an idempotent call: the
        server deduplicates on eventId."""
        return self.request("POST", "/events", json=request, retry=True)

    def report_events(self, events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Reports any number of events (see healthswap_client.events) in batches of up to 500 and merges the results:
        {"accepted", "duplicates", "rejected" (when any), "charges" (RA-1 charges created, when any)}."""
        batch: list[Mapping[str, Any]] = []
        total: dict[str, Any] = {"accepted": 0, "duplicates": 0}
        rejected: list[Any] = []
        charges: list[Any] = []

        def send() -> None:
            result = self.report_suggestion_events({"events": batch})
            total["accepted"] += int(result.get("accepted") or 0)
            total["duplicates"] += int(result.get("duplicates") or 0)
            rejected.extend(result.get("rejected") or [])
            charges.extend(result.get("charges") or [])

        for e in events:
            batch.append(e)
            if len(batch) == _EVENT_BATCH_LIMIT:
                send()
                batch = []
        if batch:
            send()
        if rejected:
            total["rejected"] = rejected
        if charges:
            total["charges"] = charges
        return total

    # ------------------------------------------------------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------------------------------------------------------

    def create_order(self, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /orders (the Idempotency-Key the operation requires is generated when not given; pass your own, for example
        derived from the orderId, to make retries across process restarts idempotent too)."""
        return self._create("/orders", request, idempotency_key)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"/orders/{_segment(order_id)}")

    def update_order_status(self, order_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        """PATCH /orders/{orderId} (not retried: a repeated status change can be an invalid transition)."""
        return self.request("PATCH", f"/orders/{_segment(order_id)}", json=request)

    def create_order_adjustments(self, order_id: str, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /orders/{orderId}/adjustments (returns, refunds, price adjustments, substitutions, cancellations)."""
        return self._create(f"/orders/{_segment(order_id)}/adjustments", request, idempotency_key)

    def get_order_attribution(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"/orders/{_segment(order_id)}/attribution")

    # ------------------------------------------------------------------------------------------------------------------
    # Shoppers
    # ------------------------------------------------------------------------------------------------------------------

    def get_shopper_preferences(self, user_ref: str) -> dict[str, Any]:
        return self.request("GET", f"/users/{_segment(user_ref)}/preferences")

    def put_shopper_preferences(self, user_ref: str, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/users/{_segment(user_ref)}/preferences", json=request)

    def erase_shopper(self, user_ref: str) -> dict[str, Any]:
        return self.request("DELETE", f"/users/{_segment(user_ref)}")

    # ------------------------------------------------------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _paginate(fetch: Callable[[str | None], Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = fetch(cursor)
            yield from page.get("items") or []
            cursor = page.get("nextCursor")
            if not cursor or cursor in seen:
                return
            seen.add(cursor)

    def list_statements(self, *, period: str | None = None, status: str | None = None, cursor: str | None = None,
                        limit: int | None = None) -> dict[str, Any]:
        return self.request("GET", "/billing/statements", params={"period": period, "status": status, "cursor": cursor, "limit": limit})

    def iter_statements(self, *, period: str | None = None, status: str | None = None, page_size: int = 100) -> Iterator[dict[str, Any]]:
        """Every statement matching the filters, following nextCursor."""
        return self._paginate(lambda cursor: self.list_statements(period=period, status=status, cursor=cursor, limit=page_size))

    def get_statement(self, statement_id: str | uuid.UUID) -> dict[str, Any]:
        return self.request("GET", f"/billing/statements/{_segment(statement_id)}")

    def list_statement_lines(self, statement_id: str | uuid.UUID, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        return self.request("GET", f"/billing/statements/{_segment(statement_id)}/lines", params={"cursor": cursor, "limit": limit})

    def iter_statement_lines(self, statement_id: str | uuid.UUID, *, page_size: int = 100) -> Iterator[dict[str, Any]]:
        return self._paginate(lambda cursor: self.list_statement_lines(statement_id, cursor=cursor, limit=page_size))

    def list_statement_recommendation_charges(self, statement_id: str | uuid.UUID, *, cursor: str | None = None,
                                              limit: int | None = None) -> dict[str, Any]:
        return self.request("GET", f"/billing/statements/{_segment(statement_id)}/recommendation-charges", params={"cursor": cursor, "limit": limit})

    def iter_statement_recommendation_charges(self, statement_id: str | uuid.UUID, *, page_size: int = 100) -> Iterator[dict[str, Any]]:
        return self._paginate(lambda cursor: self.list_statement_recommendation_charges(statement_id, cursor=cursor, limit=page_size))

    def list_recommendation_charges(self, *, period: str | None = None, state: str | None = None, cart_ref: str | None = None,
                                    suggestion_id: str | uuid.UUID | None = None, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """GET /billing/recommendation-charges: RA-1 charges, newest first."""
        return self.request("GET", "/billing/recommendation-charges", params={
            "period": period, "state": state, "cartRef": cart_ref, "suggestionId": suggestion_id, "cursor": cursor, "limit": limit,
        })

    def iter_recommendation_charges(self, *, period: str | None = None, state: str | None = None, cart_ref: str | None = None,
                                    suggestion_id: str | uuid.UUID | None = None, page_size: int = 100) -> Iterator[dict[str, Any]]:
        return self._paginate(lambda cursor: self.list_recommendation_charges(
            period=period, state=state, cart_ref=cart_ref, suggestion_id=suggestion_id, cursor=cursor, limit=page_size))

    def get_recommendation_charge(self, charge_id: str | uuid.UUID) -> dict[str, Any]:
        return self.request("GET", f"/billing/recommendation-charges/{_segment(charge_id)}")

    def create_dispute(self, request: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """POST /billing/disputes (FP-1 statement lines and / or RA-1 recommendationChargeIds)."""
        return self._create("/billing/disputes", request, idempotency_key)

    def get_dispute(self, dispute_id: str | uuid.UUID) -> dict[str, Any]:
        return self.request("GET", f"/billing/disputes/{_segment(dispute_id)}")

    # ------------------------------------------------------------------------------------------------------------------
    # Transparency (public, no token)
    # ------------------------------------------------------------------------------------------------------------------

    def _public(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return self._send("GET", self._api_root + path, params=query or None, retryable=True, authenticate=False).json()

    def list_scoring_models(self, *, market: str | None = None) -> dict[str, Any]:
        return self._public("/scoring-models", {"market": market})

    def get_service_status(self) -> dict[str, Any]:
        return self._public("/status")
