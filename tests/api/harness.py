"""In-process API harness for the integration tests (port of HealthSwap.Api.Tests ApiFixture.cs, Flows.cs and Contract.Expect).

`running_api()` builds the app with create_app() on a fresh dev database (Development settings, scheduler and cache warm-up off,
rate limits off, Mock LLM), enters a Starlette TestClient (lifespan: workers, background jobs, MCP), seeds the knowledge base
and the demo tenants and warms the hot cache. Webhooks are delivered into the same app (DemoLoopbackTransport -> AppTransport
-> TestClient), so the demo receiver at /demo/webhooks/{name} verifies their signatures.

Every /v1 response the TestClient receives is validated against api/openapi.yaml (status documented for the operation, media
type, body schema) by `ContractHook`; `expect()` additionally asserts the status a test expects and returns the parsed body.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from starlette.routing import Match
from starlette.testclient import TestClient

from healthswap.api.admin_routes import DemoLoopbackTransport
from healthswap.api.app import create_app
from healthswap.api.contract import OpenApiContract, parse_json
from healthswap.paths import repo_root
from healthswap.platform import jsonutil
from healthswap.platform.container import Container
from healthswap.platform.settings import Settings, load_settings

QUICKBASKET = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01"
HOMETOWN = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e02"
STORE = "BLR-KOR-012"
NOODLES = "SKU-NDL-280"
NOODLES_GTIN = "8901234567890"
MILLET_NOODLES = "SKU-MLT-256"
COLA = "SKU-COLA-750"
# data/seed/demo/partners.json
CLIENTS = {
    "quickbasket": ("quickbasket-server", "dev-only-quickbasket-secret-change-me"),
    "hometown": ("hometown-server", "dev-only-hometown-secret-change-me"),
    "admin": ("hs-admin", "dev-only-admin-secret-change-me"),
}

CONTRACT = OpenApiContract.load(repo_root() / "api" / "openapi.yaml")
PROBLEM_SCHEMA = CONTRACT.response_schema("evaluateCart", 400)  # components/schemas/Problem
# Statuses the API returns although api/openapi.yaml does not list them for the operation (bodies are checked as Problem).
UNDOCUMENTED = frozenset({
    ("evaluateCart", 409),  # Idempotency-Key reused with another body (HS-IDEMPOTENCY-CONFLICT); the operation takes the key
    ("evaluateCart", 415),  # not application/json
})


def _scoped_operations() -> frozenset[str]:
    """Operations whose security requirement names OAuth scopes. components/responses/Forbidden is "The token lacks the required
    scope (code HS-AUTH-SCOPE)", but only four operations list it: any scoped operation may answer it (code HS-AUTH-SCOPE)."""
    default = CONTRACT.document.get("security") or []
    return frozenset(
        op["operationId"]
        for item in CONTRACT.document["paths"].values() for op in item.values()
        if isinstance(op, dict) and "operationId" in op and any(scopes for requirement in op.get("security", default) for scopes in requirement.values())
    )


SCOPED = _scoped_operations()


def _documented_responses() -> dict[str, dict[str, str | None]]:
    """operationId -> {status: media type of the documented content, None when it has none}."""
    shared = CONTRACT.document["components"]["responses"]
    result: dict[str, dict[str, str | None]] = {}
    for item in CONTRACT.document["paths"].values():
        for op in item.values():
            if not isinstance(op, dict) or "operationId" not in op:
                continue
            statuses: dict[str, str | None] = {}
            for status, response in op["responses"].items():
                if "$ref" in response:
                    response = shared[response["$ref"].split("/")[-1]]
                statuses[str(status)] = next(iter(response.get("content") or {}), None)
            result[op["operationId"]] = statuses
    return result


DOCUMENTED = _documented_responses()


def contract_problems(operation: str, status: int, content_type: str | None, body: bytes) -> list[str]:
    """Why a response violates the contract of `operation` (empty when it conforms)."""
    statuses = DOCUMENTED[operation]
    media = None if not content_type else content_type.split(";", 1)[0].strip().lower()
    code = None
    if str(status) in statuses:
        expected, schema = statuses[str(status)], CONTRACT.response_schema(operation, status)
    elif (operation, status) in UNDOCUMENTED:
        expected, schema = "application/problem+json", PROBLEM_SCHEMA
    elif status == 403 and operation in SCOPED:
        expected, schema, code = "application/problem+json", PROBLEM_SCHEMA, "HS-AUTH-SCOPE"
    else:
        return [f"{operation}: status {status} is not documented in api/openapi.yaml"]
    if expected is None:
        return [] if not body else [f"{operation} {status}: the contract documents no content, got {media}"]
    if media != expected:
        return [f"{operation} {status}: media type {media}, the contract documents {expected}"]
    try:
        instance = parse_json(body)
    except ValueError as ex:
        return [f"{operation} {status}: invalid JSON ({ex})"]
    problems = [] if schema is None else [f"{operation} {status}: {e.field or '/'}: {e.message}" for e in CONTRACT.validate(schema, instance)]
    if code is not None and isinstance(instance, Mapping) and instance.get("code") != code:
        problems.append(f"{operation} {status}: code {instance.get('code')}, the contract's Forbidden response is {code}")
    return problems


class ContractHook:
    """TestClient response hook: validates every /v1 response of a contract operation and records the violations."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.violations: list[str] = []
        self.checked = 0  # responses validated

    def operation(self, method: str, path: str) -> str | None:
        scope = {"type": "http", "method": method, "path": path, "root_path": ""}
        for route in self.app.router.routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                name = getattr(route, "name", None)
                return name if name in DOCUMENTED else None
        return None

    def __call__(self, response: Any) -> None:
        request = response.request
        path = request.url.path
        if not path.startswith("/v1/"):
            return
        operation = self.operation(request.method, path)
        if operation is None:  # the pre-signed feed upload and the feed error report are not contract operations
            return
        response.read()
        self.checked += 1
        for problem in contract_problems(operation, response.status_code, response.headers.get("content-type"), response.content):
            self.violations.append(f"{request.method} {path}: {problem}")


def expect(response: Any, operation: str, status: int) -> Any:
    """Asserts the status and validates the body against the operation's response schema (C# Contract.Expect); the parsed body."""
    assert response.status_code == status, f"{operation}: expected {status}, got {response.status_code}: {response.text}"
    problems = contract_problems(operation, status, response.headers.get("content-type"), response.content)
    assert not problems, f"{operation} {status} violates the contract: {'; '.join(problems)}\n{response.text}"
    return parse_json(response.content) if response.content else None


# ---------------------------------------------------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------------------------------------------------


def dumps(body: Any) -> str:
    """Request JSON: None members dropped (the C# tests serialize with JsonIgnoreCondition.WhenWritingNull), Decimal as numbers."""
    return jsonutil.dumps(body)


_NO_BODY: Any = object()


class Caller:
    """The TestClient with one identity (bearer token or anonymous), like a C# HttpClient with DefaultRequestHeaders."""

    def __init__(self, client: TestClient, token: str | None = None) -> None:
        self.client = client
        self.headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    def request(self, method: str, url: str, body: Any = _NO_BODY, *, key: str | None = None, headers: Mapping[str, str] | None = None,
                content: bytes | str | None = None) -> Any:
        h = {**self.headers, **(headers or {})}
        if key is not None:
            h["Idempotency-Key"] = key
        if body is not _NO_BODY:
            content = dumps(body)
            h.setdefault("Content-Type", "application/json")
        return self.client.request(method, url, content=content, headers=h)

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, body: Any = _NO_BODY, **kw: Any) -> Any:
        return self.request("POST", url, body, **kw)

    def put(self, url: str, body: Any = _NO_BODY, **kw: Any) -> Any:
        return self.request("PUT", url, body, **kw)

    def patch(self, url: str, body: Any = _NO_BODY, **kw: Any) -> Any:
        return self.request("PATCH", url, body, **kw)

    def delete(self, url: str, **kw: Any) -> Any:
        return self.request("DELETE", url, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# The in-process API
# ---------------------------------------------------------------------------------------------------------------------


class AppTransport(httpx.BaseTransport):
    """httpx transport that sends requests into the in-process app through its (entered) TestClient: the webhook dispatcher's
    deliveries to the demo receiver stay in process, like the C# fixture's TestServer handler for the "webhooks" client."""

    _HOP_BY_HOP = frozenset({"host", "content-length", "connection", "transfer-encoding", "keep-alive"})

    def __init__(self) -> None:
        self.client: TestClient | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        client = self.client
        if client is None:
            raise httpx.ConnectError("The in-process API is not running", request=request)
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in self._HOP_BY_HOP]
        res = client.request(request.method, request.url.raw_path.decode("ascii"), content=request.read(), headers=headers)
        return httpx.Response(res.status_code, headers=list(res.headers.multi_items()), content=res.content, request=request)


@dataclass
class Api:
    app: FastAPI
    client: TestClient
    container: Container
    settings: Settings
    hook: ContractHook
    _tokens: dict[tuple[str, str | None], str] = field(default_factory=dict)

    def token(self, client_id: str, secret: str, scope: str | None = None) -> str:
        cached = self._tokens.get((client_id, scope))
        if cached is not None:
            return cached
        form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
        if scope is not None:
            form["scope"] = scope
        res = self.client.post("/oauth2/token", data=form)
        assert res.status_code == 200, f"token for {client_id}: {res.status_code} {res.text}"
        token = res.json()["access_token"]
        self._tokens[(client_id, scope)] = token
        return token

    def caller(self, who: str = "quickbasket", scope: str | None = None) -> Caller:
        client_id, secret = CLIENTS[who]
        return Caller(self.client, self.token(client_id, secret, scope))

    def bearer(self, token: str) -> Caller:
        return Caller(self.client, token)

    def anonymous(self) -> Caller:
        return Caller(self.client)


def api_settings(database_url: str, tmp: Path, overrides: Mapping[str, Any] | None = None) -> Settings:
    """Development settings for an in-process API on `database_url` (everything writable under `tmp`)."""
    return load_settings(
        environment="Development",
        overrides={
            "database.url": database_url, "database.max_pool_size": 8, "scheduler.enabled": False, "cache.warm_on_startup": False,
            "rate_limits.enabled": False, "llm.provider": "Mock", "llm.mock_fault_every": 5, "lake.enabled": False,
            "lake.root": str(tmp / "lake"), "webhooks.allow_http_localhost": True, "data_protection.keys_directory": str(tmp / "keys"),
            "auth.signing_key_file": str(tmp / "jwt-es256.pem"), "feeds.storage_root": str(tmp / "feeds"),
            # A slow first evaluation on a loaded test machine must not skip cart lines (TIMEOUT_BUDGET is unit tested).
            "evaluation.internal_budget_ms": 10_000,
            **(overrides or {}),
        },
        env={},
    )


def drain(container: Container) -> None:
    while container.outbox_relay.pump() > 0:
        pass
    assert container.bus.wait_idle(60)


@contextmanager
def running_api(database_url: str, tmp: Path, overrides: Mapping[str, Any] | None = None, seed: bool = True) -> Iterator[Api]:
    """The API in process on `database_url`; with `seed` the knowledge base and the demo tenants are seeded and the cache warmed."""
    settings = api_settings(database_url, tmp, overrides)
    container = Container(settings)
    loopback = AppTransport()
    container.override("webhook_http_client", httpx.Client(
        follow_redirects=False, timeout=Container.WEBHOOK_HTTP_TIMEOUT_SECONDS,
        transport=DemoLoopbackTransport(settings.server.host, settings.server.port, inner=loopback),
    ))
    app = create_app(settings, container)
    hook = ContractHook(app)
    with TestClient(app) as client:
        client.event_hooks = {"request": [], "response": [hook]}
        loopback.client = client
        try:
            if seed:
                container.demo_seeder.seed(container.repo_root, True)
                drain(container)
                container.cache_warmer.warm_all()
            yield Api(app, client, container, settings, hook)
        finally:
            loopback.client = None


# ---------------------------------------------------------------------------------------------------------------------
# Flows (Flows.cs): QuickBasket, market IN, store BLR-KOR-012 on the synthetic demo catalog
# ---------------------------------------------------------------------------------------------------------------------


def inr(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "INR"}


def new_ref(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def now(seconds: float = 0) -> str:
    """Wall-clock UTC timestamp (C# Contract.Now)."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def noodle_cart(cart_ref: str, user_ref: str | None = None, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "cartRef": cart_ref, "userRef": user_ref, "market": "IN", "storeId": STORE, "currency": "INR", "locale": "en-IN", "context": context,
        "lines": [
            {"lineId": "1", "partnerSku": NOODLES, "gtin": NOODLES_GTIN, "quantity": 2, "unitPrice": inr("56.00")},
            {"lineId": "2", "partnerSku": COLA, "quantity": 1, "unitPrice": inr("40.00")},
        ],
    }


def evaluate(caller: Caller, request: Mapping[str, Any]) -> Any:
    return expect(caller.post("/v1/cart-evaluations", request), "evaluateCart", 200)


def suggestion(evaluation: Mapping[str, Any], line_id: str) -> Any:
    return next((s for s in evaluation["suggestions"] if s["lineId"] == line_id), None)


def alternative(s: Mapping[str, Any], partner_sku: str) -> Any:
    return next(a for a in s["alternatives"] if a["product"]["partnerSku"] == partner_sku)


def events(caller: Caller, *items: Mapping[str, Any]) -> Any:
    return expect(caller.post("/v1/events", {"events": list(items)}), "reportSuggestionEvents", 202)


def impression(suggestion_id: str, seconds_ago: int = 3) -> dict[str, Any]:
    return {"eventId": str(uuid.uuid4()), "type": "IMPRESSION", "suggestionId": suggestion_id, "occurredAt": now(-seconds_ago)}


def add_to_cart(suggestion_id: str, sku: str = MILLET_NOODLES, seconds_ago: int = 1, event_id: str | None = None, quantity: int = 1) -> dict[str, Any]:
    return {
        "eventId": event_id or str(uuid.uuid4()), "type": "ADD_TO_CART", "suggestionId": suggestion_id, "alternativePartnerSku": sku,
        "quantity": quantity, "occurredAt": now(-seconds_ago),
    }


def recommended_add(caller: Caller) -> tuple[str, str, str]:
    """Evaluates a fresh noodle cart, reports an impression and one add of the recommended product: (cartRef, suggestionId, chargeId)."""
    cart_ref = new_ref("c_it_")
    s = suggestion(evaluate(caller, noodle_cart(cart_ref)), "1")
    ack = events(caller, impression(s["suggestionId"]), add_to_cart(s["suggestionId"]))
    return cart_ref, s["suggestionId"], ack["charges"][0]["chargeId"]


def admin_json(response: Any) -> Any:
    assert 200 <= response.status_code < 300, f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text}"
    return parse_json(response.content)


def server_period(admin: Caller) -> str:
    """The business clock's month (time travel included)."""
    return str(admin_json(admin.get("/admin/clock"))["utcNow"])[:7]


def set_clock(admin: Caller, offset_seconds: float) -> None:
    admin_json(admin.post("/admin/clock", {"offsetSeconds": offset_seconds}))


def job(admin: Caller, name: str, query: str = "") -> Any:
    return admin_json(admin.post(f"/admin/jobs/{name}{query}"))["result"]


def poll(condition: Callable[[], Any], timeout: float = 30.0, interval: float = 0.25) -> Any:
    """The first truthy result of `condition`, retried until `timeout` seconds have passed (then the last result)."""
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if result or time.monotonic() > deadline:
            return result
        time.sleep(interval)
