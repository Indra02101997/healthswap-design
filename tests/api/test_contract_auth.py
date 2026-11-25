"""API layer unit tests without a database: contract loading and validation, route mapping, OAuth tokens, problems,
rate limiting and the MCP tool surface. The app is built on a Container whose database is a fake (token issuance only)."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from healthswap.api.admin_routes import DemoLoopbackTransport
from healthswap.api.app import create_app
from healthswap.api.auth import InvalidToken, Principal, Scopes
from healthswap.api.contract import OpenApiContract, load_yaml, parse_json
from healthswap.api.http import RateLimiter
from healthswap.api.mcp_tools import create_mcp_server
from healthswap.platform.container import Container
from healthswap.platform.errors import HsError
from healthswap.platform.settings import RateLimitOptions, load_settings
from healthswap.platform.tenancy import PartnerContext

QUICKBASKET = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")
CLIENTS = {
    "quickbasket-server": {"client_id": "quickbasket-server", "partner_id": QUICKBASKET, "status": "ACTIVE", "geography": "IN",
                           "scopes": ["products.read", "carts.evaluate", "events.write", "sdk.sessions.write"]},
}
QB_SECRET = "dev-only-quickbasket-secret-change-me"
VALID_CART = {
    "cartRef": "c_unit_1", "market": "IN", "currency": "INR",
    "lines": [{"lineId": "1", "partnerSku": "SKU-NDL-280", "quantity": 2, "unitPrice": {"amount": "56.00", "currency": "INR"}}],
}


class _FakeUow:
    def one(self, sql: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        assert "tenant.api_client" in sql
        return CLIENTS.get((params or {}).get("clientId"))


class FakeDb:
    """Answers the identity provider's client lookup; anything else would fail loudly."""

    def in_tx(self, role: Any, partner_id: Any, work: Any) -> Any:
        return work(_FakeUow())

    def close(self) -> None:
        pass


def _settings(tmp: Path, **overrides: Any):
    values = {"auth.signing_key_file": str(tmp / "jwt-es256.pem"), "database.url": "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1"}
    values.update(overrides)
    return load_settings("Development", values, env={})


def _app(tmp: Path, **overrides: Any):
    settings = _settings(tmp, **overrides)
    container = Container(settings)
    container.override("db", FakeDb())
    return create_app(settings, container)


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory):
    return _app(tmp_path_factory.mktemp("api"))


@pytest.fixture(scope="module")
def client(app) -> TestClient:
    return TestClient(app)  # no lifespan: workers and the MCP session manager stay off, nothing reaches a database


@pytest.fixture(scope="module")
def partner_token(client: TestClient) -> str:
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "quickbasket-server", "client_secret": QB_SECRET})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def _sign(app, **claims: Any) -> str:
    now = int(time.time())
    payload = {"aud": "healthswap-api", "iss": "http://localhost:8080/oauth2", "iat": now, "nbf": now - 5, "exp": now + 600,
               "client_id": "quickbasket-server", "partner_id": str(QUICKBASKET), "token_use": "partner", "scope": "carts.evaluate"}
    payload.update(claims)
    return app.state.hs.keys.sign({k: v for k, v in payload.items() if v is not None})


# ---------------------------------------------------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------------------------------------------------


def test_yaml_loading_keeps_strings() -> None:
    doc = load_yaml(
        "a: '1.0'\nb: 1.0\nc: 2026-11-01\nd: yes\ne: 42\nf: ~\ng: 1.5e3\nh: true\n200: x\ni: |\n  12\nj: \"true\"\nk: null\nl:\nm: 3.1.0\n"
        "n: -0.50\no: 99999999999999999999\n")
    assert doc["a"] == "1.0"
    assert doc["b"] == Decimal("1.0") and str(doc["b"]) == "1.0"
    assert doc["c"] == "2026-11-01"
    assert doc["d"] == "yes"
    assert doc["e"] == 42 and isinstance(doc["e"], int)
    assert doc["f"] is None and doc["k"] is None and doc["l"] is None
    assert doc["g"] == "1.5e3"
    assert doc["h"] is True and doc["j"] == "true"
    assert doc["200"] == "x" and doc["i"] == "12\n"
    assert doc["m"] == "3.1.0"
    assert str(doc["n"]) == "-0.50"
    assert doc["o"] == Decimal("99999999999999999999")  # beyond Int64: a decimal, not a float


def test_contract_document_keeps_versions_as_strings(app) -> None:
    contract: OpenApiContract = app.state.hs.contract
    assert contract.document["openapi"] == "3.1.0"
    assert isinstance(contract.document["info"]["version"], str)
    assert "200" in contract.document["paths"]["/cart-evaluations"]["post"]["responses"]
    assert contract.yaml.startswith("openapi")


def test_every_contract_operation_is_mapped_to_exactly_one_route(app) -> None:
    contract: OpenApiContract = app.state.hs.contract
    operations = [op for op in contract.operations if not op.startswith("on")]
    assert len(operations) == 32
    by_name: dict[str, list[Any]] = {}
    for route in app.routes:
        by_name.setdefault(getattr(route, "name", None), []).append(route)
    for op in operations:
        routes = by_name.get(op, [])
        assert len(routes) == 1, f"{op}: {len(routes)} routes"
        method, path = contract.routes[op]
        route = routes[0]
        assert route.methods == {method}, op
        assert route.path.replace(":uuid}", "}") == "/v1" + path, op
    # The extra feed routes of PartnerEndpoints.cs exist as well.
    assert len(by_name["uploadCatalogFeed"]) == 1 and len(by_name["getCatalogFeedErrors"]) == 1


def test_generated_docs_are_disabled(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    r = client.get("/openapi.yaml")
    assert r.status_code == 200 and r.headers["content-type"] == "application/yaml" and r.text.startswith("openapi")


def test_parse_json_rejects_nan_and_deep_documents() -> None:
    assert parse_json(b"\xef\xbb\xbf{\"a\": 1.50}") == {"a": Decimal("1.50")}
    with pytest.raises(ValueError):
        parse_json(b'{"a": NaN}')
    with pytest.raises(ValueError):
        parse_json(b"[" * 65 + b"]" * 65)


# ---------------------------------------------------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------------------------------------------------


def _post_cart(client: TestClient, token: str, body: Any, content_type: str = "application/json") -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post("/v1/cart-evaluations", content=content, headers={**_bearer(token), "Content-Type": content_type})


def test_request_validation_errors(client: TestClient, partner_token: str) -> None:
    r = _post_cart(client, partner_token, {"market": "IN"})
    assert r.status_code == 400
    problem = r.json()
    assert problem["code"] == "HS-REQ-INVALID" and problem["detail"] == "The request body does not match the API contract"
    assert {(e["field"], e["code"]) for e in problem["errors"]} == {("/", "required")}
    assert sorted(e["message"] for e in problem["errors"]) == sorted(f"'{p}' is a required property" for p in ("cartRef", "currency", "lines"))

    body = json.loads(json.dumps(VALID_CART))
    body["lines"][0]["unitPrice"]["extra"] = 1
    body["cartRef"] = 5
    errors = {(e["field"], e["code"]) for e in _post_cart(client, partner_token, body).json()["errors"]}
    assert ("/lines/0/unitPrice/extra", "additionalProperties") in errors
    assert ("/cartRef", "type") in errors

    r = _post_cart(client, partner_token, b"{bad", "application/json")
    assert r.status_code == 400 and r.json()["detail"].startswith("Malformed JSON: ")

    r = _post_cart(client, partner_token, b"hello", "text/plain")
    assert r.status_code == 415 and r.json()["code"] == "HS-UNSUPPORTED-MEDIA-TYPE"


def test_event_formats_are_validated(client: TestClient, partner_token: str) -> None:
    token = _sign(client.app, scope="events.write")
    r = client.post("/v1/events", headers=_bearer(token), json={"events": [
        {"eventId": "not-a-uuid", "type": "IMPRESSION", "suggestionId": str(uuid.uuid4()), "occurredAt": "yesterday"}]})
    assert r.status_code == 400
    assert {(e["field"], e["code"]) for e in r.json()["errors"]} >= {("/events/0/eventId", "format"), ("/events/0/occurredAt", "format")}


def test_query_binding_errors(client: TestClient) -> None:
    token = _sign(client.app, scope="products.read billing.read")
    r = client.get(f"/v1/products/{uuid.uuid4()}", headers=_bearer(token))
    assert r.status_code == 400 and r.json()["detail"] == 'Required parameter "string market" was not provided from query string.'
    r = client.get("/v1/billing/statements?limit=abc", headers=_bearer(token))
    assert r.status_code == 400 and r.json()["detail"] == 'Failed to bind parameter "Nullable<int> limit" from "abc".'
    r = client.get("/v1/billing/recommendation-charges?state=X", headers=_bearer(token))
    assert r.status_code == 400 and r.json()["detail"] == "Unknown state X"
    r = client.get(f"/v1/products/{uuid.uuid4()}/alternatives?market=IN&placement=CART", headers=_bearer(token))
    assert r.status_code == 400 and r.json()["detail"] == "placement must be PDP or SEARCH"
    r = client.get("/v1/scoring-models?market=india")
    assert r.status_code == 400 and r.json()["detail"] == "market must be an ISO 3166-1 alpha-2 code"


def test_routing_failures_are_empty_like_aspnet(client: TestClient, partner_token: str) -> None:
    r = client.get("/v1/nope", headers=_bearer(partner_token))
    assert r.status_code == 404 and r.content == b""
    r = client.get("/v1/products/not-a-guid?market=IN", headers=_bearer(partner_token))
    assert r.status_code == 404 and r.content == b""
    r = client.delete("/v1/status")
    assert r.status_code == 405 and r.headers["allow"] == "GET" and r.content == b""


# ---------------------------------------------------------------------------------------------------------------------
# OAuth 2.0 tokens
# ---------------------------------------------------------------------------------------------------------------------


def test_token_issuance_and_validation_round_trip(client: TestClient, partner_token: str) -> None:
    state = client.app.state.hs
    claims = state.validator.validate(partner_token)
    assert claims["client_id"] == "quickbasket-server" and claims["partner_id"] == str(QUICKBASKET) and claims["token_use"] == "partner"
    assert claims["geo"] == "IN" and claims["aud"] == "healthswap-api" and claims["iss"] == "http://localhost:8080/oauth2"
    assert claims["sub"] == "quickbasket-server" and claims["exp"] - claims["iat"] == 600
    assert jwt.get_unverified_header(partner_token)["kid"] == state.keys.kid
    principal = Principal.from_claims(claims)
    assert principal.partner is not None and principal.partner.partner_id == QUICKBASKET and principal.has_scope("carts.evaluate")
    # The token authorizes: the request reaches contract validation (400), not 401/403.
    assert _post_cart(client, partner_token, {"market": "IN"}).status_code == 400

    r = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "quickbasket-server", "client_secret": QB_SECRET,
                                           "scope": "products.read"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["scope"] == "products.read" and r.json()["token_type"] == "Bearer" and r.json()["expires_in"] == 600

    # A (user, password) tuple works with both httpx and the httpx2 client used by Starlette's TestClient.
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials"}, auth=("quickbasket-server", QB_SECRET))
    assert r.status_code == 200, r.text

    jwks = client.get("/.well-known/jwks.json").json()
    assert jwks["keys"][0]["kid"] == state.keys.kid and jwks["keys"][0]["crv"] == "P-256"


def test_token_endpoint_errors(client: TestClient) -> None:
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "quickbasket-server", "client_secret": "wrong"})
    assert r.status_code == 401 and r.json() == {"error": "invalid_client", "error_description": "Client authentication failed"}
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "nobody", "client_secret": "x"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "quickbasket-server", "client_secret": QB_SECRET,
                                           "scope": "hs.admin"})
    assert r.status_code == 400 and r.json() == {"error": "invalid_scope", "error_description": "Requested scope exceeds the client's grant"}
    r = client.post("/oauth2/token", json={"grant_type": "client_credentials"})
    assert r.status_code == 400 and r.json() == {"error": "invalid_request", "error_description": "Use application/x-www-form-urlencoded"}
    r = client.post("/oauth2/token", data={"grant_type": "password"})
    assert r.status_code == 400 and r.json() == {"error": "unsupported_grant_type"}


def test_admin_token(client: TestClient) -> None:
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "hs-admin", "client_secret": "dev-only-admin-secret-change-me"})
    claims = client.app.state.hs.validator.validate(r.json()["access_token"])
    assert claims["scope"] == Scopes.ADMIN and claims["token_use"] == "admin" and "partner_id" not in claims
    assert Principal.from_claims(claims).partner is None


def test_invalid_tokens_are_rejected(client: TestClient) -> None:
    app = client.app
    validator = app.state.hs.validator
    expired = _sign(app, exp=int(time.time()) - 120)
    with pytest.raises(InvalidToken, match="The token expired at"):
        validator.validate(expired)
    within_skew = _sign(app, exp=int(time.time()) - 10)
    assert validator.validate(within_skew)["client_id"] == "quickbasket-server"
    with pytest.raises(InvalidToken, match="The audience 'someone-else' is invalid"):
        validator.validate(_sign(app, aud="someone-else"))
    with pytest.raises(InvalidToken, match="The issuer 'https://evil' is invalid"):
        validator.validate(_sign(app, iss="https://evil"))
    with pytest.raises(InvalidToken, match="The token has no expiration"):
        validator.validate(_sign(app, exp=None))
    claims = jwt.decode(_sign(app), options={"verify_signature": False})
    unsigned = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(InvalidToken):
        validator.validate(unsigned)
    other_key = jwt.encode(claims, "x" * 32, algorithm="HS256")
    with pytest.raises(InvalidToken):
        validator.validate(other_key)
    with pytest.raises(InvalidToken, match="malformed"):
        validator.validate("abc.def.ghi")

    r = client.get("/v1/orders/x", headers=_bearer(expired))
    assert r.status_code == 401 and r.json()["detail"].startswith("The token expired at")
    assert r.headers["www-authenticate"].startswith('Bearer error="invalid_token", error_description="The token expired at')
    r = client.get("/v1/orders/x", headers=_bearer(unsigned))
    assert r.status_code == 401 and r.json()["code"] == "HS-AUTH-INVALID"


def test_sdk_session_claims(client: TestClient) -> None:
    state = client.app.state.hs
    server = PartnerContext(partner_id=QUICKBASKET, client_id="quickbasket-server", scopes=frozenset({"sdk.sessions.write"}))
    session = state.idp.issue_sdk_session(server, {"cartRef": "c_42", "market": "IN", "userRef": "u_12345678", "storeId": "BLR-KOR-012", "ttlSeconds": 5})
    assert session["tokenType"] == "Bearer" and session["scopes"] == ["carts.evaluate", "events.write", "products.read"]
    claims = state.validator.validate(session["token"])
    assert claims["token_use"] == "sdk" and claims["sub"] == "sdk:quickbasket-server" and claims["cart_ref"] == "c_42"
    assert claims["market"] == "IN" and claims["user_ref"] == "u_12345678" and claims["store_id"] == "BLR-KOR-012"
    assert claims["exp"] - claims["iat"] == 60  # ttl clamped to at least 60 s
    ctx = Principal.from_claims(claims).partner
    assert ctx is not None and ctx.is_sdk_session and ctx.sdk_cart_ref == "c_42" and ctx.sdk_user_ref == "u_12345678" and ctx.sdk_market == "IN"
    assert ctx.scopes == frozenset(Scopes.SDK_ALLOWED)

    long = state.validator.validate(state.idp.issue_sdk_session(server, {"cartRef": "c", "market": "IN", "ttlSeconds": 86400})["token"])
    assert long["exp"] - long["iat"] == 900 and "user_ref" not in long
    with pytest.raises(HsError):
        state.idp.issue_sdk_session(server, {"cartRef": "c", "market": "IN", "scopes": ["orders.write"]})

    # An SDK session cannot reach server-only operations.
    r = client.get("/v1/orders/x", headers=_bearer(session["token"]))
    assert r.status_code == 403 and r.json()["detail"] == "The token lacks the scope required by this operation"


def test_admin_plane_requires_the_admin_scope(client: TestClient, partner_token: str) -> None:
    r = client.get("/admin/partners", headers=_bearer(partner_token))
    assert r.status_code == 403 and r.json()["code"] == "HS-AUTH-SCOPE"
    assert client.get("/admin/partners").status_code == 401


# ---------------------------------------------------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------------------------------------------------


def test_problem_json_shape(client: TestClient) -> None:
    r = client.get("/v1/orders/x", headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    assert r.status_code == 401 and r.headers["content-type"] == "application/problem+json"
    assert r.headers["www-authenticate"] == "Bearer"
    body = r.json()
    assert list(body) == ["type", "title", "status", "detail", "instance", "code", "traceId"]
    assert body == {
        "type": "https://healthswap.example/problems/hs-auth-invalid", "title": "Unauthorized", "status": 401,
        "detail": "A valid bearer token is required", "instance": "/v1/orders/x", "code": "HS-AUTH-INVALID",
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
    }
    other = client.get("/v1/orders/x").json()["traceId"]
    assert len(other) == 32 and int(other, 16) >= 0 and other != body["traceId"]


def test_mcp_requires_a_token_with_resource_metadata(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
    admin = client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "hs-admin",
                                               "client_secret": "dev-only-admin-secret-change-me"}).json()["access_token"]
    assert client.post("/mcp", headers=_bearer(admin), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 403
    prm = client.get("/.well-known/oauth-protected-resource").json()
    assert prm == {"resource": "http://testserver/mcp", "authorization_servers": ["http://testserver/oauth2"],
                   "scopes_supported": ["carts.evaluate", "products.read", "events.write"], "bearer_methods_supported": ["header"]}


# ---------------------------------------------------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------------------------------------------------


def test_rate_limiter_bucket_maths() -> None:
    now = [100.0]
    limiter = RateLimiter(RateLimitOptions(cart_evaluations_per_second=4, default_per_second=2, burst_multiplier=2), clock=lambda: now[0])
    assert limiter.limits("/v1/cart-evaluations") == ("eval", 4, 8)
    assert limiter.limits("/v1/orders/x") == ("default", 2, 4)
    results = [limiter.take("p:default", 2, 4) for _ in range(5)]
    assert [ok for ok, _ in results] == [True, True, True, True, False]
    assert results[3][1] == 0 and results[4][1] == 0
    now[0] += 0.25  # half a token at 2/s
    assert limiter.take("p:default", 2, 4) == (False, 0.5)
    now[0] += 0.25
    assert limiter.take("p:default", 2, 4) == (True, 0.0)
    now[0] += 60  # refill is capped at the capacity
    assert limiter.take("p:default", 2, 4) == (True, 3.0)
    assert RateLimiter.headers(2, 4, 3.0) == {"RateLimit-Limit": "4", "RateLimit-Remaining": "3", "RateLimit-Reset": "1"}
    assert RateLimiter.headers(2, 4, 0.5) == {"RateLimit-Limit": "4", "RateLimit-Remaining": "0", "RateLimit-Reset": "2"}
    assert limiter.take("q:default", 2, 4) == (True, 3.0)  # buckets are per key


def test_rate_limit_middleware(tmp_path: Path) -> None:
    app = _app(tmp_path, **{"rate_limits.default_per_second": 1, "rate_limits.burst_multiplier": 1})
    client = TestClient(app)
    first = client.get("/v1/nope")
    assert first.status_code == 404 and first.headers["ratelimit-limit"] == "1" and first.headers["ratelimit-remaining"] == "0"
    second = client.get("/v1/nope")
    assert second.status_code == 429 and second.headers["retry-after"] == "1"
    assert second.json()["code"] == "HS-RATE-LIMITED" and second.json()["detail"] == "Limit is 1 requests per second (default)"
    assert client.get("/healthz").status_code == 200  # only /v1 is limited


# ---------------------------------------------------------------------------------------------------------------------
# MCP tools and the demo webhook transport
# ---------------------------------------------------------------------------------------------------------------------


def test_mcp_tools_match_the_csharp_surface(app) -> None:
    tools = {t.name: t for t in anyio.run(create_mcp_server(app.state.hs).list_tools)}
    assert list(tools) == ["evaluate_cart", "get_product_health", "find_healthier_alternatives", "explain_swap", "record_swap_decision"]

    def hints(name: str) -> dict[str, Any]:
        return tools[name].annotations.model_dump(by_alias=True, exclude_none=True)

    assert hints("evaluate_cart") == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
    assert hints("get_product_health") == {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
    assert hints("find_healthier_alternatives") == {"readOnlyHint": False, "idempotentHint": False, "openWorldHint": False}
    assert hints("explain_swap") == {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
    assert hints("record_swap_decision") == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    assert tools["evaluate_cart"].input_schema["required"] == ["market", "currency", "lines", "cartRef"]
    assert tools["evaluate_cart"].input_schema["properties"]["lines"]["items"]["properties"]["quantity"] == {"description": "Quantity in the cart",
                                                                                                           "type": "number"}
    assert tools["get_product_health"].input_schema["properties"]["productId"] == {
        "description": "Canonical product id", "type": ["string", "null"], "format": "uuid", "default": None}
    assert tools["find_healthier_alternatives"].input_schema["properties"]["limit"]["default"] == 3
    assert tools["record_swap_decision"].input_schema["required"] == ["suggestionId", "decision"]
    for tool in tools.values():
        assert tool.output_schema == {"type": "object", "$defs": {"output": True}, "anyOf": [
            {"$ref": "#/$defs/output"}, {"properties": {"result": {"$ref": "#/$defs/output"}}, "required": ["result"]}]}


def test_demo_loopback_transport() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url) + " " + request.headers["host"])
        return httpx.Response(204)

    transport = DemoLoopbackTransport("localhost", 8081, inner=httpx.MockTransport(handler))
    with httpx.Client(transport=transport) as http:
        http.post("http://localhost:8080/demo/webhooks/quickbasket", content=b"{}")
        http.post("http://localhost:8080/other", content=b"{}")
        http.post("https://partner.example/demo/webhooks/x", content=b"{}")
        http.post("http://127.0.0.1:8081/demo/webhooks/hometown", content=b"{}")
    assert seen == [
        "http://127.0.0.1:8081/demo/webhooks/quickbasket 127.0.0.1:8081",
        "http://localhost:8080/other localhost:8080",
        "https://partner.example/demo/webhooks/x partner.example",
        "http://127.0.0.1:8081/demo/webhooks/hometown 127.0.0.1:8081",
    ]


def test_demo_routes_only_in_development_or_when_enabled(tmp_path: Path) -> None:
    def demo_routes(environment: str, enabled: bool) -> list[str]:
        settings = load_settings(environment, {"auth.signing_key_file": str(tmp_path / "k.pem"), "demo.enabled": enabled}, env={})
        app = create_app(settings, Container(settings))
        return sorted(f"{getattr(r, 'name', '')} {r.path}" for r in app.routes if str(getattr(r, "path", "")).startswith("/demo"))

    expected = sorted([
        "demo:index /demo", "demo:session /demo/api/session", "demo:charges /demo/api/charges", "demo:webhooks:receive /demo/webhooks/{name}",
        "demo:webhooks:list /demo/webhooks/{name}", "demo:sdk /demo/sdk", "demo:ui /demo",
    ])
    assert demo_routes("Production", False) == []
    assert demo_routes("Production", True) == expected
    assert demo_routes("Development", False) == expected


def test_demo_shop_static_files(client: TestClient) -> None:
    r = client.get("/demo", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/demo/index.html"
    # Explicit types (the Windows MIME registry may say text/plain for .js, which browsers refuse for module scripts) and a CSP.
    for path, content_type in {
        "/demo/index.html": "text/html; charset=utf-8", "/demo/demo.js": "text/javascript; charset=utf-8", "/demo/demo.css": "text/css; charset=utf-8",
        "/demo/sdk/healthswap-web.js": "text/javascript; charset=utf-8", "/demo/sdk/healthswap-web.css": "text/css; charset=utf-8",
    }.items():
        r = client.get(path)
        assert (r.status_code, r.headers["content-type"]) == (200, content_type), path
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-cache", path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self';" in csp and "connect-src 'self';" in csp and "frame-ancestors 'none'" in csp, path
    page = client.get("/demo/index.html").text
    assert '<script type="module" src="/demo/demo.js"></script>' in page and "<style" not in page and " style=" not in page
    assert client.get("/demo/missing.js").status_code == 404
    assert client.get("/demo/sdk/%2e%2e/%2e%2e/config/settings.toml").status_code == 404


def test_demo_charge_view_only_reads_demo_shop_carts(client: TestClient) -> None:
    r = client.get("/demo/api/charges")
    assert r.status_code == 400 and r.json()["detail"] == 'Required parameter "string cartRef" was not provided from query string.'
    for bad in ("c_it_0123456789", "c_demo_SHOUT1234", "c_demo_abc", "c_demo_" + "a" * 33, "c_demo_abcdefgh/../x", " c_demo_abcdefgh"):
        r = client.get("/demo/api/charges", params={"cartRef": bad})
        assert (r.status_code, r.json()["code"]) == (400, "HS-REQ-INVALID"), bad
    r = client.get("/demo/api/charges", params=[("cartRef", "c_demo_abcdefgh"), ("cartRef", "c_demo_abcdefgi")])
    assert r.status_code == 400 and r.json()["code"] == "HS-REQ-INVALID"
