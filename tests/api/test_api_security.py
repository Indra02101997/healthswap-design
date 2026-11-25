"""Authentication, authorization, tenant isolation (RLS), SDK session binding, request validation and rate limiting through HTTP
(`integration`): port of the C# SecurityTests on the in-process API (tests/api/harness.py). Every /v1 response is validated
against api/openapi.yaml.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness import (
    NOODLES,
    STORE,
    Api,
    Caller,
    ContractHook,
    add_to_cart,
    api_settings,
    dumps,
    evaluate,
    events,
    expect,
    impression,
    inr,
    new_ref,
    noodle_cart,
    now,
    recommended_add,
    suggestion,
)
from starlette.testclient import TestClient

from healthswap.api.app import create_app
from healthswap.platform.container import Container


def test_missing_or_invalid_token_is_challenged_with_problem_details(api: Api) -> None:
    res = api.anonymous().post("/v1/cart-evaluations", noodle_cart("c_anon"))
    problem = expect(res, "evaluateCart", 401)
    assert res.headers["www-authenticate"] == "Bearer" and problem["code"] == "HS-AUTH-INVALID"

    res = api.bearer("not-a-jwt").post("/v1/cart-evaluations", noodle_cart("c_anon"))
    expect(res, "evaluateCart", 401)
    assert res.headers["www-authenticate"].startswith('Bearer error="invalid_token", error_description="')


def test_wrong_client_secret_is_rejected(api: Api) -> None:
    res = api.client.post("/oauth2/token", data={"grant_type": "client_credentials", "client_id": "quickbasket-server", "client_secret": "wrong"})
    assert res.status_code == 401 and res.json()["error"] == "invalid_client"


def test_partner_tokens_cannot_use_the_admin_plane_and_admin_tokens_not_the_partner_api(api: Api) -> None:
    partner = api.caller()
    assert partner.get("/admin/partners").status_code == 403
    assert partner.post("/admin/jobs/month-close").status_code == 403
    problem = expect(api.caller("admin").post("/v1/cart-evaluations", noodle_cart(new_ref("c_admin_"))), "evaluateCart", 403)
    assert problem["code"] == "HS-AUTH-SCOPE"


def test_tenants_cannot_see_each_others_charges_orders_evaluations_or_suggestions(api: Api) -> None:
    quickbasket, hometown = api.caller(), api.caller("hometown")
    _, suggestion_id, charge_id = recommended_add(quickbasket)

    expect(hometown.get(f"/v1/billing/recommendation-charges/{charge_id}"), "getRecommendationCharge", 404)
    page = expect(hometown.get("/v1/billing/recommendation-charges?limit=200"), "listRecommendationCharges", 200)
    assert all(i["chargeId"] != charge_id for i in page["items"])
    ack = events(hometown, add_to_cart(suggestion_id))
    assert ack["accepted"] == 0 and ack["rejected"][0]["code"] == "HS-EVENT-UNKNOWN-SUGGESTION"
    expect(hometown.get("/v1/orders/does-not-exist"), "getOrder", 404)

    evaluation_id = evaluate(quickbasket, noodle_cart(new_ref("c_iso_")))["evaluationId"]
    expect(quickbasket.get(f"/v1/cart-evaluations/{evaluation_id}"), "getCartEvaluation", 200)
    expect(hometown.get(f"/v1/cart-evaluations/{evaluation_id}"), "getCartEvaluation", 404)


def test_sdk_sessions_are_bound_to_their_cart_and_scopes(api: Api) -> None:
    server = api.caller()
    cart = new_ref("c_sdk_")
    session = expect(server.post("/v1/sdk/sessions", {"cartRef": cart, "market": "IN", "storeId": STORE}), "createSdkSession", 201)
    assert session["tokenType"] == "Bearer" and set(session["scopes"]) == {"carts.evaluate", "events.write", "products.read"}
    sdk = api.bearer(session["token"])

    evaluation = evaluate(sdk, noodle_cart(cart))
    assert expect(sdk.post("/v1/cart-evaluations", noodle_cart(new_ref("c_other_"))), "evaluateCart", 403)["code"] == "HS-AUTH-SCOPE"
    assert expect(sdk.get("/v1/billing/statements"), "listStatements", 403)["code"] == "HS-AUTH-SCOPE"
    expect(sdk.post("/v1/sdk/sessions", {"cartRef": cart, "market": "IN"}), "createSdkSession", 403)

    # The browser can report RA-1 events for its own cart, but charge acknowledgements are for the partner's server only...
    sid = suggestion(evaluation, "1")["suggestionId"]
    ack = events(sdk, impression(sid), add_to_cart(sid))
    assert ack["accepted"] == 2 and "charges" not in ack and "rejected" not in ack
    # ...which sees the PENDING charge the session's add created.
    page = expect(server.get(f"/v1/billing/recommendation-charges?cartRef={cart}"), "listRecommendationCharges", 200)
    assert [(c["suggestionId"], c["cartRef"], c["state"]) for c in page["items"]] == [(sid, cart, "PENDING")]

    # ...but not for another cart's suggestion.
    _, other_sid, _ = recommended_add(server)
    foreign = events(sdk, add_to_cart(other_sid))
    assert foreign["accepted"] == 0 and foreign["rejected"][0]["code"] == "HS-AUTH-SESSION-SCOPE"


def test_invalid_requests_return_field_errors(api: Api) -> None:
    partner = api.caller()
    problem = expect(partner.post("/v1/cart-evaluations", {"cartRef": "c_bad", "market": "IN", "currency": "INR"}), "evaluateCart", 400)
    assert problem["code"] == "HS-REQ-INVALID" and problem["errors"]

    wrong_type = partner.post("/v1/cart-evaluations", content="{}", headers={"Content-Type": "text/plain"})
    assert expect(wrong_type, "evaluateCart", 415)["code"] == "HS-UNSUPPORTED-MEDIA-TYPE"

    no_key = partner.post("/v1/orders", {
        "orderId": new_ref("ORD-NOKEY-"), "cartRef": "c_x", "storeId": STORE, "market": "IN", "currency": "INR", "placedAt": now(),
        "fulfillmentType": "QUICK_COMMERCE", "lines": [{"lineId": "1", "partnerSku": NOODLES, "quantity": 1, "unitPrice": inr("56.00"), "netAmount": inr("56.00")}],
    })
    assert "Idempotency-Key" in expect(no_key, "createOrder", 400)["detail"]


def test_mcp_requires_a_token_and_advertises_its_resource_metadata(api: Api) -> None:
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    res = api.client.post("/mcp", content=dumps(initialize), headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    assert res.status_code == 401 and res.headers["content-type"].startswith("application/problem+json")
    assert res.headers["www-authenticate"] == 'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
    metadata = api.client.get("/.well-known/oauth-protected-resource").json()
    assert metadata["resource"] == "http://testserver/mcp" and metadata["authorization_servers"] == ["http://testserver/oauth2"]


def test_rate_limits_answer_429_with_ratelimit_headers(api: Api, tmp_path: Path) -> None:
    """A second app on the module's database with one request per second and no burst; no lifespan, so no workers run
    next to the module's API (requests do not need them)."""
    settings = api_settings(api.settings.database.url, tmp_path, {
        "rate_limits.enabled": True, "rate_limits.default_per_second": 1, "rate_limits.cart_evaluations_per_second": 1,
        "rate_limits.burst_multiplier": 1,
    })
    container = Container(settings)
    try:
        app = create_app(settings, container)
        hook = ContractHook(app)
        with contextlib.closing(TestClient(app)) as client:
            client.event_hooks = {"request": [], "response": [hook]}
            container.cache_warmer.warm_all()

            def until_limited(call: Callable[[], Any], operation: str) -> tuple[Any, Any]:
                """(headers, problem) of the first 429; capacity 1 refilled at 1/s answers 429 within a few calls."""
                for _ in range(10):
                    res = call()
                    if res.status_code != 429:
                        assert res.status_code == 200 and res.headers["ratelimit-limit"] == "1", res.text
                        continue
                    return res.headers, expect(res, operation, 429)
                raise AssertionError(f"{operation}: no 429 in 10 calls")

            headers, problem = until_limited(lambda: Caller(client).get("/v1/status"), "getServiceStatus")
            assert (problem["code"], problem["detail"]) == ("HS-RATE-LIMITED", "Limit is 1 requests per second (default)")
            assert (headers["retry-after"], headers["ratelimit-limit"], headers["ratelimit-remaining"], headers["ratelimit-reset"]) == ("1", "1", "0", "1")

            form = {"grant_type": "client_credentials", "client_id": "quickbasket-server", "client_secret": "dev-only-quickbasket-secret-change-me"}
            partner = Caller(client, client.post("/oauth2/token", data=form).json()["access_token"])
            _, problem = until_limited(lambda: partner.post("/v1/cart-evaluations", noodle_cart(new_ref("c_rl_"))), "evaluateCart")
            assert problem["detail"] == "Limit is 1 requests per second (eval)"
            # Buckets are per caller and route group: the partner's default group is untouched.
            expect(partner.get(f"/v1/products/lookup?market=IN&partnerSku={NOODLES}"), "lookupProduct", 200)
        assert hook.checked >= 5 and not hook.violations, hook.violations
    finally:
        container.close()
