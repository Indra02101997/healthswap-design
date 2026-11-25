"""Python server SDK (sdk/python, package healthswap_client).

Unit tests run against httpx.MockTransport with a fake clock and a fake sleep: token caching and refresh, fail-open cart
evaluation (timeout, connection error, 5xx, 429, open circuit), the circuit breaker, the retry policy (backoff, Retry-After,
RateLimit-Reset, no retry for a POST without Idempotency-Key), problem parsing, webhook verification and pagination.

The `integration` test starts the real API in process (uvicorn on a free port, background thread) on a fresh dev database,
seeds it like tests/api/harness.py and runs the RA-1 flow through the SDK.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from healthswap_client import CircuitBreaker, HealthSwapClient, HealthSwapError, events, sign_webhook, verify_webhook
from healthswap_client.errors import retry_after_seconds
from healthswap_client.resilience import backoff_delay

BASE = "https://api.test"
UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMESTAMP_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
CART = {
    "cartRef": "c_unit_1", "market": "IN", "storeId": "BLR-KOR-012", "currency": "INR",
    "lines": [{"lineId": "1", "partnerSku": "SKU-NDL-280", "quantity": 2, "unitPrice": {"amount": "56.00", "currency": "INR"}}],
}
EVALUATION = {"evaluationId": str(uuid.uuid4()), "cartRef": "c_unit_1", "market": "IN", "degraded": False, "suggestions": [{"suggestionId": "s1"}]}


def problem(status: int, code: str, title: str, detail: str | None = None, errors: list[dict[str, Any]] | None = None,
            headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"type": f"https://healthswap.example/problems/{code.lower()}", "title": title, "status": status, "detail": detail,
            "instance": "/v1/x", "code": code, "traceId": "0af7651916cd43dd8448eb211c80319c", "errors": errors}
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"Content-Type": "application/problem+json", **(headers or {})})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeApi:
    """MockTransport handler: issues tok-1, tok-2, ... at /oauth2/token and routes other requests to handlers by
    (method, path). Thread-safe (cart evaluations run on the SDK's evaluation threads)."""

    def __init__(self, expires_in: int = 600) -> None:
        self.expires_in = expires_in
        self.lock = threading.Lock()
        self.requests: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.token_delay = 0.0

    def route(self, method: str, path: str, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception) -> None:
        """Answers with `responses` in order (the last one repeats); an exception is raised, a callable is called."""
        queue = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            with self.lock:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item(request) if callable(item) else item

        self.routes[(method, path)] = handler

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        with self.lock:
            return [r for r in self.requests if r.method == method and r.url.path == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            if self.token_delay:
                time.sleep(self.token_delay)
            with self.lock:
                self.token_requests.append(request)
                n = len(self.token_requests)
            return httpx.Response(200, json={"access_token": f"tok-{n}", "token_type": "Bearer", "expires_in": self.expires_in, "scope": "x"})
        with self.lock:
            self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return problem(404, "HS-NOT-FOUND", "Resource not found", f"{request.method} {request.url.path}")
        return handler(request)


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(api: FakeApi, clock: FakeClock, sleeps: list[float]) -> Iterator[HealthSwapClient]:
    with HealthSwapClient(BASE, "quickbasket-server", "s3cr:et/+", transport=httpx.MockTransport(api), sleep=sleeps.append, clock=clock) as c:
        yield c


def bearer(request: httpx.Request) -> str:
    return request.headers.get("authorization", "")


def ok(body: Any = None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={} if body is None else body)


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


# ---------------------------------------------------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------------------------------------------------


def test_base_url_and_token_url() -> None:
    api = FakeApi()
    api.route("GET", "/v1/orders/ORD-1", ok({"orderId": "ORD-1"}))
    for base in ("https://api.test", "https://api.test/", "https://api.test/v1", "https://api.test/v1/"):
        with HealthSwapClient(base, "id", "secret", transport=httpx.MockTransport(api)) as c:
            assert c.get_order("ORD-1") == {"orderId": "ORD-1"}
    assert {str(r.url) for r in api.token_requests} == {"https://api.test/oauth2/token"}
    with HealthSwapClient(BASE, "id", "secret", token_url="https://auth.test/oauth2/token", transport=httpx.MockTransport(api)) as c:
        c.get_order("ORD-1")
    assert str(api.token_requests[-1].url) == "https://auth.test/oauth2/token"


def test_credentials_never_go_over_plain_http_to_remote_hosts() -> None:
    with pytest.raises(ValueError, match="https"):
        HealthSwapClient("http://api.example.com", "id", "secret")
    with pytest.raises(ValueError, match="https"):
        HealthSwapClient(BASE, "id", "secret", token_url="http://auth.example.com/oauth2/token")
    for local in ("http://localhost:8080", "http://127.0.0.1:8093", "http://[::1]:8080"):
        HealthSwapClient(local, "id", "secret").close()
    HealthSwapClient("http://api.internal:8080", "id", "secret", allow_insecure_http=True).close()
    with pytest.raises(ValueError):
        HealthSwapClient("ftp://api.test", "id", "secret")
    with pytest.raises(ValueError):
        HealthSwapClient(BASE, "id", "")


# ---------------------------------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------------------------------


def test_token_is_fetched_once_with_basic_auth_and_cached(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("GET", "/v1/orders/ORD-1", ok({"orderId": "ORD-1"}))
    assert client.get_order("ORD-1") == {"orderId": "ORD-1"}
    assert client.get_order("ORD-1") == {"orderId": "ORD-1"}
    assert len(api.token_requests) == 1
    token_request = api.token_requests[0]
    assert token_request.method == "POST" and token_request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(token_request.content.decode()) == {"grant_type": ["client_credentials"]}
    scheme, _, encoded = token_request.headers["authorization"].partition(" ")
    assert scheme == "Basic" and base64.b64decode(encoded).decode() == "quickbasket-server:s3cr%3Aet%2F%2B"  # RFC 6749 2.3.1
    assert [bearer(r) for r in api.calls("GET", "/v1/orders/ORD-1")] == ["Bearer tok-1", "Bearer tok-1"]


def test_scopes_are_requested(api: FakeApi) -> None:
    api.route("GET", "/v1/orders/ORD-1", ok())
    with HealthSwapClient(BASE, "id", "secret", scopes=["orders.read", "billing.read"], transport=httpx.MockTransport(api)) as c:
        c.get_order("ORD-1")
    assert parse_qs(api.token_requests[0].content.decode())["scope"] == ["orders.read billing.read"]


def test_token_is_refreshed_in_the_background_before_it_expires(api: FakeApi, client: HealthSwapClient, clock: FakeClock) -> None:
    api.route("GET", "/v1/orders/ORD-1", ok())
    client.get_order("ORD-1")  # tok-1 at t=1000: used until 1570, refreshed from 1480 (expires_in 600)
    clock.advance(479)
    client.get_order("ORD-1")
    assert len(api.token_requests) == 1
    clock.advance(21)  # t=1500: in the refresh window, the request still uses tok-1 and a background thread fetches tok-2
    client.get_order("ORD-1")
    assert bearer(api.calls("GET", "/v1/orders/ORD-1")[-1]) == "Bearer tok-1"

    def uses_tok2() -> bool:
        client.get_order("ORD-1")
        return bearer(api.calls("GET", "/v1/orders/ORD-1")[-1]) == "Bearer tok-2"

    assert wait_for(uses_tok2)
    assert len(api.token_requests) == 2
    clock.advance(10_000)  # tok-2 expired: the next request waits for tok-3
    client.get_order("ORD-1")
    assert bearer(api.calls("GET", "/v1/orders/ORD-1")[-1]) == "Bearer tok-3"
    assert len(api.token_requests) == 3


def test_concurrent_callers_share_one_token_fetch(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("GET", "/v1/orders/ORD-1", ok())
    api.token_delay = 0.1
    threads = [threading.Thread(target=client.get_order, args=("ORD-1",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(api.token_requests) == 1
    assert {bearer(r) for r in api.calls("GET", "/v1/orders/ORD-1")} == {"Bearer tok-1"}


def test_401_fetches_a_new_token_once(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("GET", "/v1/orders/ORD-1", lambda r: ok({"orderId": "ORD-1"}) if bearer(r) == "Bearer tok-2" else problem(401, "HS-AUTH-INVALID", "Unauthorized"))
    assert client.get_order("ORD-1") == {"orderId": "ORD-1"}
    assert len(api.token_requests) == 2
    api.route("GET", "/v1/orders/ORD-2", problem(401, "HS-AUTH-INVALID", "Unauthorized", "The token is revoked"))
    with pytest.raises(HealthSwapError) as e:
        client.get_order("ORD-2")
    assert (e.value.status, e.value.code, e.value.detail) == (401, "HS-AUTH-INVALID", "The token is revoked")
    assert len(api.calls("GET", "/v1/orders/ORD-2")) == 2


def test_token_endpoint_errors_raise_with_the_oauth_error(api: FakeApi) -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client authentication failed"})

    with HealthSwapClient(BASE, "id", "wrong", transport=httpx.MockTransport(deny)) as c, pytest.raises(HealthSwapError) as e:
        c.get_order("ORD-1")
    assert (e.value.status, e.value.code, e.value.detail) == (401, "invalid_client", "Client authentication failed")


# ---------------------------------------------------------------------------------------------------------------------
# Fail-open cart evaluation and the circuit breaker
# ---------------------------------------------------------------------------------------------------------------------


def test_evaluate_cart_returns_the_evaluation(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("POST", "/v1/cart-evaluations", ok(EVALUATION))
    assert client.evaluate_cart(CART, idempotency_key="k-1") == EVALUATION
    [sent] = api.calls("POST", "/v1/cart-evaluations")
    assert json.loads(sent.content) == CART and sent.headers["idempotency-key"] == "k-1" and sent.headers["content-type"] == "application/json"
    assert bearer(sent) == "Bearer tok-1"


def assert_fail_open(result: dict[str, Any], reason: str, status: int | None = None) -> None:
    assert result["degraded"] is True and result["suggestions"] == [] and result["lineLabels"] == []
    assert (result["cartRef"], result["market"], result["storeId"]) == ("c_unit_1", "IN", "BLR-KOR-012")
    assert result["failOpen"]["reason"] == reason and result["failOpen"]["status"] == status, result["failOpen"]


def test_evaluate_cart_fails_open_at_the_deadline(api: FakeApi, client: HealthSwapClient) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(1.0)
        return ok(EVALUATION)

    api.route("POST", "/v1/cart-evaluations", slow)
    started = time.perf_counter()
    result = client.evaluate_cart(CART)  # default evaluation_timeout: 150 ms
    elapsed = time.perf_counter() - started
    assert_fail_open(result, "TIMEOUT")
    assert elapsed < 0.8, f"returned after {elapsed:.3f} s"


def test_evaluate_cart_fails_open_on_transport_errors(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("POST", "/v1/cart-evaluations", httpx.ReadTimeout("read timed out"), httpx.ConnectError("connection refused"))
    assert_fail_open(client.evaluate_cart(CART), "TIMEOUT")
    assert_fail_open(client.evaluate_cart(CART), "CONNECTION_ERROR")


def test_evaluate_cart_fails_open_on_5xx_and_429_without_retrying(api: FakeApi, client: HealthSwapClient, sleeps: list[float]) -> None:
    api.route("POST", "/v1/cart-evaluations", problem(503, "HS-DEGRADED", "Temporarily unavailable", headers={"Retry-After": "1"}),
              problem(429, "HS-RATE-LIMITED", "Too many requests", headers={"Retry-After": "1"}), httpx.Response(502, text="<html>bad gateway</html>"))
    assert_fail_open(client.evaluate_cart(CART), "SERVER_ERROR", 503)
    assert_fail_open(client.evaluate_cart(CART), "RATE_LIMITED", 429)
    assert_fail_open(client.evaluate_cart(CART), "SERVER_ERROR", 502)
    assert len(api.calls("POST", "/v1/cart-evaluations")) == 3 and sleeps == []


def test_evaluate_cart_raises_client_errors(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("POST", "/v1/cart-evaluations", problem(400, "HS-REQ-INVALID", "Request validation failed", "The request body does not match the API contract",
                                                      [{"field": "/lines", "message": "'lines' is a required property"}]))
    with pytest.raises(HealthSwapError) as e:
        client.evaluate_cart({"cartRef": "c", "market": "IN", "currency": "INR"})
    assert e.value.code == "HS-REQ-INVALID" and e.value.errors == [{"field": "/lines", "message": "'lines' is a required property"}]
    assert client.circuit_breaker.consecutive_failures == 0


def test_evaluate_cart_fails_open_when_the_token_endpoint_is_down(clock: FakeClock) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if request.url.path == "/oauth2/token" else ok(EVALUATION)

    with HealthSwapClient(BASE, "id", "secret", transport=httpx.MockTransport(down), sleep=lambda s: None, clock=clock) as c:
        assert_fail_open(c.evaluate_cart(CART), "SERVER_ERROR", 503)


def test_circuit_opens_after_consecutive_failures_and_probes_after_the_cool_down(api: FakeApi, clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=clock)
    api.route("POST", "/v1/cart-evaluations", problem(503, "HS-DEGRADED", "Unavailable"))
    with HealthSwapClient(BASE, "id", "secret", transport=httpx.MockTransport(api), circuit_breaker=breaker, clock=clock) as c:
        for _ in range(3):
            assert_fail_open(c.evaluate_cart(CART), "SERVER_ERROR", 503)
        assert breaker.state == CircuitBreaker.OPEN
        assert_fail_open(c.evaluate_cart(CART), "CIRCUIT_OPEN")
        assert len(api.calls("POST", "/v1/cart-evaluations")) == 3  # the open circuit did not call the API

        clock.advance(30)
        assert breaker.state == CircuitBreaker.HALF_OPEN
        assert_fail_open(c.evaluate_cart(CART), "SERVER_ERROR", 503)  # the probe fails: open for another cool-down
        assert breaker.state == CircuitBreaker.OPEN
        assert_fail_open(c.evaluate_cart(CART), "CIRCUIT_OPEN")

        clock.advance(30)
        api.route("POST", "/v1/cart-evaluations", ok(EVALUATION))
        assert c.evaluate_cart(CART) == EVALUATION  # the probe succeeds: closed
        assert breaker.state == CircuitBreaker.CLOSED and breaker.consecutive_failures == 0
        assert len(api.calls("POST", "/v1/cart-evaluations")) == 5


def test_circuit_breaker_lets_one_probe_through_and_ignores_stragglers(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=clock)
    first, second = breaker.acquire(), breaker.acquire()
    assert first is not None and second is not None
    breaker.record(first, False)
    breaker.record(second, False)
    assert breaker.state == CircuitBreaker.OPEN and breaker.acquire() is None
    straggler = first
    clock.advance(10)
    probe = breaker.acquire()
    assert probe is not None and probe.probe and breaker.acquire() is None  # one probe at a time
    breaker.record(straggler, True)  # started before the breaker opened: ignored
    assert breaker.state == CircuitBreaker.HALF_OPEN
    clock.advance(10)  # the probe never reported: a new one is allowed after another cool-down
    replacement = breaker.acquire()
    assert replacement is not None and replacement.probe
    breaker.record(probe, True)  # the lost probe's late answer is ignored too
    breaker.record(replacement, True)
    assert breaker.state == CircuitBreaker.CLOSED
    closed = breaker.acquire()
    assert closed is not None and not closed.probe
    breaker.record(closed, False)
    breaker.record(closed, True)  # a success resets the consecutive count
    assert breaker.consecutive_failures == 0
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


# ---------------------------------------------------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------------------------------------------------


def test_backoff_is_exponential_with_jitter() -> None:
    assert [backoff_delay(a, rand=lambda: 0.0) for a in range(6)] == [0.25, 0.5, 1.0, 2.0, 4.0, 4.0]
    assert [backoff_delay(a, rand=lambda: 1.0) for a in range(6)] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]


def test_get_is_retried_with_backoff(api: FakeApi, client: HealthSwapClient, sleeps: list[float]) -> None:
    api.route("GET", "/v1/orders/ORD-1", problem(503, "HS-DEGRADED", "Unavailable"), httpx.ConnectError("reset"), ok({"orderId": "ORD-1"}))
    assert client.get_order("ORD-1") == {"orderId": "ORD-1"}
    assert len(api.calls("GET", "/v1/orders/ORD-1")) == 3
    assert len(sleeps) == 2 and 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0


def test_retries_stop_after_max_retries(api: FakeApi, client: HealthSwapClient, sleeps: list[float]) -> None:
    api.route("GET", "/v1/orders/ORD-1", httpx.Response(502, text="bad gateway"))
    with pytest.raises(HealthSwapError) as e:
        client.get_order("ORD-1")
    assert (e.value.status, e.value.code, e.value.title) == (502, None, "Bad Gateway")
    assert len(api.calls("GET", "/v1/orders/ORD-1")) == 3 and len(sleeps) == 2  # max_retries=2
    api.route("GET", "/v1/orders/ORD-2", problem(404, "HS-NOT-FOUND", "Resource not found"))
    with pytest.raises(HealthSwapError):
        client.get_order("ORD-2")
    assert len(api.calls("GET", "/v1/orders/ORD-2")) == 1  # 4xx is not retried


def test_post_without_idempotency_key_is_not_retried(api: FakeApi, client: HealthSwapClient, sleeps: list[float]) -> None:
    api.route("POST", "/v1/orders", problem(503, "HS-DEGRADED", "Unavailable"), ok({"orderId": "ORD-1"}, 201))
    with pytest.raises(HealthSwapError) as e:
        client.request("POST", "/orders", json={"orderId": "ORD-1"})
    assert e.value.status == 503 and len(api.calls("POST", "/v1/orders")) == 1 and sleeps == []
    assert "idempotency-key" not in api.calls("POST", "/v1/orders")[0].headers

    api.route("PATCH", "/v1/orders/ORD-1", httpx.ConnectError("reset"))
    with pytest.raises(HealthSwapError) as e:
        client.update_order_status("ORD-1", {"status": "DELIVERED", "occurredAt": "2026-11-04T10:00:00Z"})
    assert (e.value.status, e.value.code) == (None, "HS-SDK-CONNECTION") and len(api.calls("PATCH", "/v1/orders/ORD-1")) == 1


def test_create_operations_get_one_idempotency_key_reused_on_retries(api: FakeApi, client: HealthSwapClient, sleeps: list[float]) -> None:
    api.route("POST", "/v1/orders", problem(503, "HS-DEGRADED", "Unavailable"), ok({"orderId": "ORD-1"}, 201))
    assert client.create_order({"orderId": "ORD-1"}) == {"orderId": "ORD-1"}
    keys = [r.headers["idempotency-key"] for r in api.calls("POST", "/v1/orders")]
    assert len(keys) == 2 and keys[0] == keys[1] and UUID_RX.match(keys[0]) and len(sleeps) == 1

    api.route("POST", "/v1/billing/disputes", ok({"disputeId": "d"}, 201))
    client.create_dispute({"statementId": str(uuid.uuid4()), "reason": "OTHER", "recommendationChargeIds": [str(uuid.uuid4())]}, idempotency_key="mine")
    assert api.calls("POST", "/v1/billing/disputes")[0].headers["idempotency-key"] == "mine"
    for call, path in ((lambda: client.create_sdk_session({"cartRef": "c", "market": "IN"}), "/v1/sdk/sessions"),
                       (lambda: client.upsert_catalog_items({"items": []}), "/v1/catalog/item-batches"),
                       (lambda: client.create_catalog_feed({"format": "JSONL", "mode": "DELTA"}), "/v1/catalog/feeds"),
                       (lambda: client.upsert_store_inventory("BLR-KOR-012", {"entries": []}), "/v1/stores/BLR-KOR-012/inventory-batches"),
                       (lambda: client.create_order_adjustments("ORD-1", {"adjustments": []}), "/v1/orders/ORD-1/adjustments")):
        api.route("POST", path, ok({}, 201))
        call()
        assert UUID_RX.match(api.calls("POST", path)[0].headers["idempotency-key"]), path


def test_event_batches_are_retried_because_events_are_deduplicated(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("POST", "/v1/events", problem(503, "HS-DEGRADED", "Unavailable"), ok({"accepted": 1, "duplicates": 0}, 202))
    assert client.report_events([events.dismiss(str(uuid.uuid4()))]) == {"accepted": 1, "duplicates": 0}
    sent = [json.loads(r.content) for r in api.calls("POST", "/v1/events")]
    assert len(sent) == 2 and sent[0] == sent[1]  # the same eventIds


def test_retry_after_and_ratelimit_reset_are_honored(api: FakeApi, client: HealthSwapClient, sleeps: list[float]) -> None:
    api.route("GET", "/v1/orders/ORD-1", problem(429, "HS-RATE-LIMITED", "Too many requests", headers={"Retry-After": "2"}), ok())
    client.get_order("ORD-1")
    api.route("GET", "/v1/orders/ORD-2", problem(429, "HS-RATE-LIMITED", "Too many requests", headers={"RateLimit-Reset": "3"}), ok())
    client.get_order("ORD-2")
    assert sleeps == [2.0, 3.0]

    sleeps.clear()
    api.route("GET", "/v1/orders/ORD-3", problem(503, "HS-DEGRADED", "Unavailable", headers={"Retry-After": "120"}))
    with pytest.raises(HealthSwapError) as e:
        client.get_order("ORD-3")
    assert e.value.retry_after == 120.0 and sleeps == []  # longer than the SDK waits: raised, not slept through

    now = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    assert retry_after_seconds({"retry-after": format_datetime(now + timedelta(seconds=7), usegmt=True)}, now) == 7.0
    assert retry_after_seconds({"retry-after": "soon", "ratelimit-reset": "4"}) == 4.0
    assert retry_after_seconds({"retry-after": "\u00b2"}) is None and retry_after_seconds({}) is None


# ---------------------------------------------------------------------------------------------------------------------
# Problems, paths, pagination, events
# ---------------------------------------------------------------------------------------------------------------------


def test_problem_details_are_parsed(api: FakeApi, client: HealthSwapClient) -> None:
    errors = [{"field": "/orderId", "message": "Order exists", "code": "HS-ORDER-EXISTS"}]
    api.route("POST", "/v1/orders", problem(409, "HS-ORDER-EXISTS", "Conflict", "Order ORD-1 already exists", errors))
    with pytest.raises(HealthSwapError) as e:
        client.create_order({"orderId": "ORD-1"})
    err = e.value
    assert (err.status, err.code, err.title, err.detail, err.errors) == (409, "HS-ORDER-EXISTS", "Conflict", "Order ORD-1 already exists", errors)
    assert err.type == "https://healthswap.example/problems/hs-order-exists" and err.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert err.problem["instance"] == "/v1/x" and "409 HS-ORDER-EXISTS: Conflict - Order ORD-1 already exists" in str(err)


def test_path_parameters_and_queries_are_encoded(client: HealthSwapClient) -> None:
    seen: list[bytes] = []
    issuer = FakeApi()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return issuer(request)
        seen.append(request.url.raw_path)
        return ok({})

    with HealthSwapClient(BASE, "id", "secret", transport=httpx.MockTransport(handler)) as c:
        c.get_catalog_item("A/B ../x?y#z")
        c.lookup_product("IN", gtin="8901234567890", partner_sku="SKU 1&2")
        c.list_product_alternatives(uuid.UUID(int=7), "IN", store_id="BLR-KOR-012", limit=3)
    assert seen == [
        b"/v1/catalog/items/A%2FB%20..%2Fx%3Fy%23z",
        b"/v1/products/lookup?market=IN&gtin=8901234567890&partnerSku=SKU+1%262",
        b"/v1/products/00000000-0000-0000-0000-000000000007/alternatives?market=IN&storeId=BLR-KOR-012&limit=3",
    ]
    with pytest.raises(ValueError):
        client.get_order("")


def test_public_operations_need_no_token(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("GET", "/v1/status", ok({"status": "OPERATIONAL", "updatedAt": "2026-11-04T10:00:00Z"}))
    api.route("GET", "/v1/scoring-models", lambda r: ok({"items": [{"market": r.url.params["market"]}]}))
    assert client.get_service_status()["status"] == "OPERATIONAL"
    assert client.list_scoring_models(market="IN") == {"items": [{"market": "IN"}]}
    assert api.token_requests == [] and all("authorization" not in r.headers for r in api.requests)


def test_iterators_follow_next_cursor(api: FakeApi, client: HealthSwapClient) -> None:
    pages = {None: ([1, 2], "c1"), "c1": ([3, 4], "c2"), "c2": ([5], None)}

    def page(request: httpx.Request) -> httpx.Response:
        items, next_cursor = pages[request.url.params.get("cursor")]
        return ok({"items": [{"n": n} for n in items], "nextCursor": next_cursor})

    api.route("GET", "/v1/billing/recommendation-charges", page)
    assert [c["n"] for c in client.iter_recommendation_charges(cart_ref="c_1", state="PENDING", page_size=2)] == [1, 2, 3, 4, 5]
    sent = api.calls("GET", "/v1/billing/recommendation-charges")
    assert [dict(r.url.params) for r in sent] == [
        {"state": "PENDING", "cartRef": "c_1", "limit": "2"}, {"state": "PENDING", "cartRef": "c_1", "cursor": "c1", "limit": "2"},
        {"state": "PENDING", "cartRef": "c_1", "cursor": "c2", "limit": "2"}]

    statement = str(uuid.uuid4())
    api.route("GET", f"/v1/billing/statements/{statement}/lines", page)
    api.route("GET", "/v1/billing/statements", page)
    api.route("GET", f"/v1/billing/statements/{statement}/recommendation-charges", page)
    assert len(list(client.iter_statement_lines(statement))) == 5
    assert len(list(client.iter_statements(period="2026-11"))) == 5
    assert len(list(client.iter_statement_recommendation_charges(statement))) == 5

    api.route("GET", "/v1/billing/statements", ok({"items": [{"n": 1}], "nextCursor": "same"}))
    assert len(list(client.iter_statements())) == 2  # a repeated cursor ends the iteration instead of looping forever


def test_event_builders() -> None:
    sid = uuid.uuid4()
    at = datetime(2026, 11, 4, 10, 15, 30, 123456, tzinfo=UTC)
    assert events.accept(sid, "SKU-MLT-256", quantity=2, occurred_at=at, event_id="e-1", metadata={"placement": "CART"}) == {
        "eventId": "e-1", "type": "ACCEPT", "suggestionId": str(sid), "occurredAt": "2026-11-04T10:15:30.123Z",
        "alternativePartnerSku": "SKU-MLT-256", "quantity": 2, "metadata": {"placement": "CART"}}
    built = [events.impression(sid), events.add_to_cart(sid, "SKU-MLT-256"), events.undo(sid, "SKU-MLT-256"), events.dismiss(sid)]
    assert [e["type"] for e in built] == ["IMPRESSION", "ADD_TO_CART", "UNDO", "DISMISS"]
    assert all(UUID_RX.match(e["eventId"]) and TIMESTAMP_RX.match(e["occurredAt"]) for e in built)
    assert len({e["eventId"] for e in built}) == 4
    assert "alternativePartnerSku" not in built[0] and built[2]["alternativePartnerSku"] == "SKU-MLT-256"
    assert events.timestamp(datetime(2026, 11, 4, 15, 45, tzinfo=UTC).astimezone()) == "2026-11-04T15:45:00.000Z"
    with pytest.raises(ValueError):
        events.impression(sid, occurred_at=datetime(2026, 11, 4))  # naive
    with pytest.raises(ValueError):
        events.add_to_cart(sid, "SKU", quantity=0)
    with pytest.raises(ValueError):
        events.event("BUY", sid)


def test_report_events_batches_by_500_and_merges_results(api: FakeApi, client: HealthSwapClient) -> None:
    def batch(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["events"]
        charges = [{"eventId": e["eventId"], "chargeId": str(uuid.uuid4()), "state": "PENDING", "amount": {"amount": "1.00", "currency": "INR"}}
                   for e in sent if e["type"] == "ADD_TO_CART"]
        body: dict[str, Any] = {"accepted": len(sent) - 1, "duplicates": 1, "rejected": [{"eventId": sent[0]["eventId"], "code": "HS-EVENT-TIME"}]}
        if charges:
            body["charges"] = charges
        return ok(body, 202)

    api.route("POST", "/v1/events", batch)
    sid = str(uuid.uuid4())
    result = client.report_events([events.impression(sid) for _ in range(1200)] + [events.add_to_cart(sid, "SKU-MLT-256")])
    assert [len(json.loads(r.content)["events"]) for r in api.calls("POST", "/v1/events")] == [500, 500, 201]
    assert (result["accepted"], result["duplicates"], len(result["rejected"]), len(result["charges"])) == (1198, 3, 3, 1)
    assert client.report_events([]) == {"accepted": 0, "duplicates": 0}


def test_request_bodies_accept_decimal_datetime_and_uuid(api: FakeApi, client: HealthSwapClient) -> None:
    api.route("PUT", "/v1/users/u_12345678/preferences", lambda r: ok(json.loads(r.content)))
    sent = client.put_shopper_preferences("u_12345678", {"id": uuid.UUID(int=1), "qty": Decimal("2"), "rate": Decimal("0.18"),
                                                          "at": datetime(2026, 11, 4, 10, 0, tzinfo=UTC)})
    assert sent == {"id": "00000000-0000-0000-0000-000000000001", "qty": 2, "rate": 0.18, "at": "2026-11-04T10:00:00Z"}


# ---------------------------------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------------------------------

SECRET = "dev-only-webhook-secret-quickbasket"
BODY = '{"id":"evt_1","type":"billing.statement.issued","data":{"statementId":"s","period":"2026-11","note":"\u20b9 caf\u00e9"}}'.encode()


def test_webhook_signature_round_trip_and_tampering() -> None:
    t = 1_793_000_000
    header = sign_webhook(SECRET, BODY, t)
    assert re.fullmatch(r"t=1793000000,v1=[0-9a-f]{64}", header)
    assert verify_webhook(SECRET, header, BODY, now=t)
    assert verify_webhook(SECRET, header, BODY.decode("utf-8"), now=t + 299)  # str bodies are UTF-8 encoded
    assert verify_webhook(SECRET, header.replace(",", " , "), BODY, now=t)
    assert not verify_webhook(SECRET, header, BODY.replace(b'"s"', b'"S"'), now=t)  # tampered body
    assert not verify_webhook("another-secret", header, BODY, now=t)
    assert not verify_webhook(SECRET, header.replace("t=1793000000", "t=1793000001"), BODY, now=t)  # tampered timestamp
    assert not verify_webhook(SECRET, header, BODY, now=t + 301)  # stale
    assert not verify_webhook(SECRET, header, BODY, now=t - 301)  # from the future
    assert verify_webhook(SECRET, header, BODY, tolerance_seconds=3600, now=t + 3000)
    assert verify_webhook(SECRET, header, BODY, now=datetime.fromtimestamp(t + 10, UTC))


def test_webhook_signature_rejects_malformed_headers() -> None:
    t = 1_793_000_000
    v1 = sign_webhook(SECRET, BODY, t).split("v1=")[1]
    for header in (None, "", "garbage", f"v1={v1}", f"t={t}", f"t={t},t={t},v1={v1}", f"t=+{t},v1={v1}", f"t=abc,v1={v1}", f"t={t},v1={v1.upper()}",
                   f"t={t},v1=\u00e9{v1}", "t=1," + "v1=x," * 2000):
        assert not verify_webhook(SECRET, header, BODY, now=t), header
    assert verify_webhook(SECRET, f"t={t},v1={'0' * 64},v1={v1}", BODY, now=t)  # one of several v1 values (secret rotation)
    with pytest.raises(ValueError):
        verify_webhook("", f"t={t},v1={v1}", BODY, now=t)


def test_webhook_verification_accepts_the_servers_signatures() -> None:
    from healthswap.platform.services.infrastructure import WebhookSignature

    payload = BODY.decode("utf-8")
    t = int(time.time())
    assert verify_webhook(SECRET, WebhookSignature.sign(SECRET, t, payload), payload.encode("utf-8"))
    assert WebhookSignature.verify(SECRET, sign_webhook(SECRET, payload, t), payload, datetime.now(UTC), timedelta(minutes=5))


# ---------------------------------------------------------------------------------------------------------------------
# Integration: the RA-1 flow against the real API (uvicorn in a background thread, fresh dev database)
# ---------------------------------------------------------------------------------------------------------------------

QUICKBASKET = ("quickbasket-server", "dev-only-quickbasket-secret-change-me")


@pytest.fixture(scope="module")
def live_api(fresh_database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """http://127.0.0.1:<port> of the API serving a seeded fresh dev database (seeded and warmed like tests/api/harness.py)."""
    import uvicorn

    from healthswap.api.app import create_app
    from healthswap.platform.container import Container
    from healthswap.platform.settings import load_settings

    tmp: Path = tmp_path_factory.mktemp("sdk-api")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = load_settings(environment="Development", overrides={
        "database.url": fresh_database_url, "database.max_pool_size": 8, "scheduler.enabled": False, "cache.warm_on_startup": False,
        "rate_limits.enabled": False, "llm.provider": "Mock", "llm.mock_fault_every": 5, "lake.enabled": False, "lake.root": str(tmp / "lake"),
        "webhooks.allow_http_localhost": True, "data_protection.keys_directory": str(tmp / "keys"), "auth.signing_key_file": str(tmp / "jwt-es256.pem"),
        "feeds.storage_root": str(tmp / "feeds"), "evaluation.internal_budget_ms": 10_000, "server.host": "127.0.0.1", "server.port": port,
    }, env={})
    container = Container(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(settings, container), log_level="warning", access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="sdk-test-api", daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: server.started or not thread.is_alive(), timeout=60) and server.started, "the API did not start"
        container.demo_seeder.seed(container.repo_root, True)
        while container.outbox_relay.pump() > 0:
            pass
        assert container.bus.wait_idle(60)
        container.cache_warmer.warm_all()
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(60)
        sock.close()


def test_ra1_flow_through_the_sdk(live_api: str) -> None:
    cart, user = "c_sdk_" + uuid.uuid4().hex[:10], "u_sdk_" + uuid.uuid4().hex[:10]
    # A generous evaluation deadline: the first evaluation on a cold, loaded test machine may exceed 150 ms (fail-open is unit tested).
    with HealthSwapClient(live_api, *QUICKBASKET, evaluation_timeout=30.0) as hs:
        evaluation = hs.evaluate_cart({
            "cartRef": cart, "userRef": user, "market": "IN", "storeId": "BLR-KOR-012", "currency": "INR", "locale": "en-IN",
            "lines": [
                {"lineId": "1", "partnerSku": "SKU-NDL-280", "gtin": "8901234567890", "quantity": 2, "unitPrice": {"amount": "56.00", "currency": "INR"}},
                {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": {"amount": "40.00", "currency": "INR"}},
            ],
        })
        assert evaluation["degraded"] is False and "failOpen" not in evaluation
        [noodles] = [s for s in evaluation["suggestions"] if s["lineId"] == "1"]
        [millet] = [a for a in noodles["alternatives"] if a["product"]["partnerSku"] == "SKU-MLT-256"]
        assert millet["recommendation"]["code"] == "HEALTHSWAP_RECOMMENDED" and millet["recommendation"]["text"] == "Recommended by HealthSwap"
        assert "add a recommended item" in evaluation["disclosures"]["feeDisclosure"]
        sid = noodles["suggestionId"]

        # Impression, the accepted swap (FP-1 evidence) and a further add of the recommended product: two RA-1 adds.
        result = hs.report_events([
            events.impression(sid, occurred_at=datetime.now(UTC) - timedelta(seconds=5)),
            events.accept(sid, "SKU-MLT-256", quantity=2, occurred_at=datetime.now(UTC) - timedelta(seconds=3)),
            events.add_to_cart(sid, "SKU-MLT-256", quantity=1),
        ])
        assert result["accepted"] == 3 and "rejected" not in result
        assert [(c["state"], c["amount"]) for c in result["charges"]] == [("PENDING", {"amount": "1.00", "currency": "INR"})] * 2

        page = hs.list_recommendation_charges(cart_ref=cart)
        assert sorted(c["eventType"] for c in page["items"]) == ["ACCEPT", "ADD_TO_CART"]
        assert all(c["state"] == "PENDING" and c["suggestionId"] == sid and c["amount"]["amount"] == "1.00" for c in page["items"])
        assert [c["chargeId"] for c in hs.iter_recommendation_charges(cart_ref=cart, page_size=1)] == [c["chargeId"] for c in page["items"]]
        assert hs.get_recommendation_charge(page["items"][0]["chargeId"])["cartRef"] == cart

        # The order (POST /orders requires an Idempotency-Key: the SDK generates it) is attributed: IN-1, fee INR 5.80.
        order_id = "ORD-SDK-" + uuid.uuid4().hex[:8]
        order_request = {
            "orderId": order_id, "cartRef": cart, "userRef": user, "storeId": "BLR-KOR-012", "market": "IN", "currency": "INR",
            "placedAt": events.timestamp(), "fulfillmentType": "QUICK_COMMERCE", "orderDiscount": {"amount": "8.00", "currency": "INR"},
            "lines": [
                {"lineId": "1", "partnerSku": "SKU-MLT-256", "quantity": 2, "unitPrice": {"amount": "89.00", "currency": "INR"},
                 "allocatedOrderDiscount": {"amount": "8.00", "currency": "INR"}, "netAmount": {"amount": "170.00", "currency": "INR"}, "suggestionId": sid},
                {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": {"amount": "40.00", "currency": "INR"},
                 "netAmount": {"amount": "40.00", "currency": "INR"}},
            ],
        }
        order = hs.create_order(order_request)
        assert order["orderId"] == order_id and order["status"] == "PLACED"
        attribution = hs.get_order_attribution(order_id)
        [line] = [x for x in attribution["lines"] if x["lineId"] == "1"]
        assert (line["state"], line["fee"]["amount"], line["suggestionId"]) == ("PENDING", "5.80", sid)
        assert attribution["orderId"] == order_id and attribution["totalFee"] == {"amount": "5.80", "currency": "INR"}

        with pytest.raises(HealthSwapError) as e:
            hs.create_order(order_request)  # a new Idempotency-Key for the same orderId
        assert (e.value.status, e.value.code) == (409, "HS-ORDER-EXISTS")
        with pytest.raises(HealthSwapError) as e:
            hs.get_order("ORD-SDK-missing")
        assert (e.value.status, e.value.code) == (404, "HS-NOT-FOUND")
