"""End-to-end smoke test of the HealthSwap reference implementation (RA-1 recommended-add fee + FP-1 fair-premium fee).

Port of scripts/smoke-test.ps1, plus MCP checks. Runs against a local API (`python scripts/run_local.py`, or `hs serve --env
Development`) with the demo data seeded by `hs seed --demo`. Every step prints PASS/FAIL. The script time-travels the server
clock through the admin plane (Development only) and resets it at the end, also when it fails. Exit code 0 when every executed
check passed, else 1.

    python scripts/smoke_test.py --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

QUICKBASKET_ID = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01"
MCP_TOOLS = {"evaluate_cart", "get_product_health", "find_healthier_alternatives", "explain_swap", "record_swap_decision"}


def _color(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if sys.stdout.isatty() else text


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False)


def g(value: Any, *path: Any) -> Any:
    """Null-safe member access (PowerShell semantics: a missing member is $null)."""
    for key in path:
        if isinstance(key, int):
            value = value[key] if isinstance(value, list) and -len(value) <= key < len(value) else None
        else:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            return None
    return value


def where(items: Any, key: str, expected: Any) -> list[Any]:
    return [i for i in items or [] if isinstance(i, dict) and i.get(key) == expected]


def first(items: list[Any]) -> Any:
    return items[0] if items else None


def now(plus_seconds: float = 0) -> str:
    """Round-trip ("o") UTC timestamp, like (Get-Date).ToUniversalTime().AddSeconds(n).ToString("o")."""
    return (datetime.now(UTC) + timedelta(seconds=plus_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def short_id(n: int) -> str:
    return uuid.uuid4().hex[:n]


class Smoke:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=120.0)
        self.failures = 0
        self.partner: dict[str, str] = {}
        self.admin: dict[str, str] = {}
        self.last_status = 0
        self.last_headers: httpx.Headers = httpx.Headers()

    def check(self, name: str, ok: bool, detail: Any = "") -> None:
        if ok:
            print(_color(f"PASS  {name}", "32"))
        else:
            print(_color(f"FAIL  {name}  {detail}", "31"))
            self.failures += 1

    def token(self, client_id: str, secret: str) -> str:
        r = self.http.post("/oauth2/token", content=f"grant_type=client_credentials&client_id={client_id}&client_secret={secret}",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()["access_token"]

    def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None, key: str | None = None) -> Any:
        h = dict(self.partner if headers is None else headers)
        if key:
            h["Idempotency-Key"] = key
        content = None
        if body is not None:
            content = json.dumps(body, default=str).encode("utf-8")
            h["Content-Type"] = "application/json"
        r = self.http.request(method, path, content=content, headers=h)
        self.last_status = r.status_code
        self.last_headers = r.headers
        return self._parse(r)

    @staticmethod
    def _parse(r: httpx.Response) -> Any:
        if not r.content:
            return None
        try:
            return json.loads(r.text, parse_float=Decimal)
        except ValueError:
            return r.text

    def section(self, title: str) -> None:
        print(_color(f"\n== {title} ==", "36"))

    def note(self, text: str) -> None:
        print(_color(f"      {text}", "33"))

    # ---------- MCP ----------

    def mcp(self, request_id: int, method: str, params: dict[str, Any]) -> Any:
        headers = {**self.partner, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        r = self.http.post("/mcp", headers=headers, content=json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        self.last_status = r.status_code
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    message = json.loads(line[5:], parse_float=Decimal)
                    if isinstance(message, dict) and message.get("id") == request_id:
                        return message
            return None
        return self._parse(r)


def run(s: Smoke) -> bool:
    """Sections 1-8 of the PowerShell smoke test. Returns True when the billing checks were skipped (ALREADY_CLOSED)."""
    cart = "c_smoke_" + short_id(8)
    user = "u_smoke_" + short_id(8)

    s.section("1. Cart evaluation: labeled recommendation")
    lines = [
        {"lineId": "1", "partnerSku": "SKU-NDL-280", "gtin": "8901234567890", "quantity": 2, "unitPrice": {"amount": "56.00", "currency": "INR"}},
        {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": {"amount": "40.00", "currency": "INR"}},
    ]
    ev = s.call("POST", "/v1/cart-evaluations", {"cartRef": cart, "userRef": user, "market": "IN", "storeId": "BLR-KOR-012", "currency": "INR",
                                                 "locale": "en-IN", "lines": lines})
    s.check("evaluation returns 200", s.last_status == 200, s.last_status)
    noodles = first(where(g(ev, "suggestions"), "lineId", "1"))
    alt = first([a for a in g(noodles, "alternatives") or [] if g(a, "product", "partnerSku") == "SKU-MLT-256"])
    s.check("noodles line gets the millet noodles swap", alt is not None)
    s.check("alternative carries the HealthSwap label", g(alt, "recommendation", "code") == "HEALTHSWAP_RECOMMENDED"
            and g(alt, "recommendation", "chargeable") is True and g(alt, "recommendation", "feeRule") == "RA-1")
    s.check("IN-1 price facts: INR 89.00, delta 33.00, ratio 1.74", g(alt, "unitPrice", "amount") == "89.00"
            and g(alt, "unitPriceDelta", "amount") == "33.00" and g(alt, "normalizedPriceRatio") == Decimal("1.74"))
    s.check("fee disclosure mentions adding a recommended item", "add a recommended item" in (g(ev, "disclosures", "feeDisclosure") or ""))
    cola = first(where(g(ev, "suggestions"), "lineId", "2"))
    s.check("cola line gets a swap (approved soft drink -> flavoured water map)", cola is not None, f"suggestions: {len(g(ev, 'suggestions') or [])}")
    sid = g(noodles, "suggestionId")

    s.section("2. RA-1 events: impression, accept, add, undo, duplicates")
    accept = str(uuid.uuid4())
    r = s.call("POST", "/v1/events", {"events": [
        {"eventId": str(uuid.uuid4()), "type": "IMPRESSION", "suggestionId": sid, "occurredAt": now(-5)},
        {"eventId": accept, "type": "ACCEPT", "suggestionId": sid, "alternativePartnerSku": "SKU-MLT-256", "quantity": 2, "occurredAt": now(-3)},
    ]})
    s.check("events accepted (202)", s.last_status == 202 and g(r, "accepted") == 2, _compact(r))
    charges = g(r, "charges") or []
    s.check("ACCEPT creates a PENDING INR 1.00 charge", len(charges) == 1 and g(charges, 0, "state") == "PENDING"
            and g(charges, 0, "amount", "amount") == "1.00")
    r2 = s.call("POST", "/v1/events", {"events": [
        {"eventId": accept, "type": "ACCEPT", "suggestionId": sid, "alternativePartnerSku": "SKU-MLT-256", "occurredAt": now(-3)},
    ]})
    s.check("replayed eventId is a duplicate and returns the same charge", g(r2, "duplicates") == 1
            and g(r2, "charges", 0, "chargeId") is not None and g(r2, "charges", 0, "chargeId") == g(charges, 0, "chargeId"))
    r3 = s.call("POST", "/v1/events", {"events": [
        {"eventId": str(uuid.uuid4()), "type": "ADD_TO_CART", "suggestionId": sid, "alternativePartnerSku": "SKU-MLT-256", "quantity": 1,
         "occurredAt": now(-2)},
    ]})
    s.check("second add (+1) is another INR 1.00 charge", len(g(r3, "charges") or []) == 1 and g(r3, "charges", 0, "state") == "PENDING")
    r4 = s.call("POST", "/v1/events", {"events": [
        {"eventId": str(uuid.uuid4()), "type": "ADD_TO_CART", "suggestionId": sid, "alternativePartnerSku": "SKU-MLT-256", "occurredAt": now(-1)},
        {"eventId": str(uuid.uuid4()), "type": "UNDO", "suggestionId": sid, "alternativePartnerSku": "SKU-MLT-256", "occurredAt": now(1)},
    ]})
    voided = s.call("GET", f"/v1/billing/recommendation-charges/{g(r4, 'charges', 0, 'chargeId')}")
    s.check("ADD_TO_CART undone within 5 s is VOID (UNDONE_WITHIN_GRACE)", g(voided, "state") == "VOID"
            and g(voided, "rejectionReason") == "UNDONE_WITHIN_GRACE", _compact(voided))
    r5 = s.call("POST", "/v1/events", {"events": [
        {"eventId": str(uuid.uuid4()), "type": "ADD_TO_CART", "suggestionId": sid, "alternativePartnerSku": "SKU-CRM-500", "occurredAt": now()},
    ]})
    s.check("adding a SKU that was not recommended is rejected", g(r5, "rejected", 0, "code") == "HS-EVENT-SKU-NOT-RECOMMENDED")

    s.section("3. Line labels on the cart after the swap")
    ev2 = s.call("POST", "/v1/cart-evaluations", {"cartRef": cart, "userRef": user, "market": "IN", "storeId": "BLR-KOR-012", "currency": "INR",
                                                  "lines": [{"lineId": "1", "partnerSku": "SKU-MLT-256", "quantity": 3,
                                                             "unitPrice": {"amount": "89.00", "currency": "INR"}}, lines[1]]})
    label = first(where(g(ev2, "lineLabels"), "partnerSku", "SKU-MLT-256"))
    s.check("cart line holding the recommended product is labeled", g(label, "label", "code") == "HEALTHSWAP_RECOMMENDED")

    s.section("4. Product page (PDP) recommendation without an impression")
    noodle_id = g(noodles, "original", "productId")
    pdp = s.call("GET", f"/v1/products/{noodle_id}/alternatives?market=IN&storeId=BLR-KOR-012&cartRef={cart}&placement=PDP")
    s.check("PDP alternatives mint a suggestionId when a cart is known", g(pdp, "suggestionId") is not None and len(g(pdp, "alternatives") or []) >= 1)
    r6 = s.call("POST", "/v1/events", {"events": [
        {"eventId": str(uuid.uuid4()), "type": "ADD_TO_CART", "suggestionId": g(pdp, "suggestionId"),
         "alternativePartnerSku": g(pdp, "alternatives", 0, "product", "partnerSku"), "occurredAt": now()},
    ]})
    no_impression_charge = g(r6, "charges", 0, "chargeId")
    s.check("PDP add creates a PENDING charge (evidence checked at finalization)", g(r6, "charges", 0, "state") == "PENDING")

    s.section("5. FP-1 order attribution (IN-1: fee INR 5.80)")
    order_id = "ORD-SMOKE-" + short_id(8)
    order = {
        "orderId": order_id, "cartRef": cart, "userRef": user, "storeId": "BLR-KOR-012", "market": "IN", "currency": "INR", "placedAt": now(),
        "fulfillmentType": "QUICK_COMMERCE", "orderDiscount": {"amount": "8.00", "currency": "INR"},
        "lines": [
            {"lineId": "1", "partnerSku": "SKU-MLT-256", "quantity": 2, "unitPrice": {"amount": "89.00", "currency": "INR"},
             "allocatedOrderDiscount": {"amount": "8.00", "currency": "INR"}, "netAmount": {"amount": "170.00", "currency": "INR"}, "suggestionId": sid},
            {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": {"amount": "40.00", "currency": "INR"},
             "netAmount": {"amount": "40.00", "currency": "INR"}},
        ],
    }
    key = str(uuid.uuid4())
    o = s.call("POST", "/v1/orders", order, s.partner, key)
    s.check("order created (201)", s.last_status == 201, s.last_status)
    line1 = first(where(g(o, "attribution", "lines"), "lineId", "1"))
    s.check("attribution PENDING with fee INR 5.80", g(line1, "state") == "PENDING" and g(line1, "fee", "amount") == "5.80", _compact(line1))
    s.call("POST", "/v1/orders", order, s.partner, key)
    s.check("same Idempotency-Key replays the stored response", s.last_status == 201 and s.last_headers.get("Idempotent-Replayed") == "true")
    s.call("POST", "/v1/orders", order, s.partner, str(uuid.uuid4()))
    s.check("same orderId with a new key is a conflict", s.last_status == 409)
    d = s.call("PATCH", f"/v1/orders/{order_id}", {"status": "DELIVERED", "occurredAt": now()})
    s.check("delivery accrues the fee", g(first(where(g(d, "attribution", "lines"), "lineId", "1")), "state") == "ACCRUED")
    adj = s.call("POST", f"/v1/orders/{order_id}/adjustments", {"adjustments": [
        {"adjustmentId": "ret-" + short_id(6), "lineId": "1", "type": "RETURN", "quantity": 1, "occurredAt": now()},
    ]})
    s.check("returning 1 of 2 units reverses INR 2.90", g(first(where(g(adj, "lines"), "lineId", "1")), "fee", "amount") == "2.90", _compact(adj))

    s.section("6. Settlement: time travel, finalizers, month close")
    s.call("POST", "/admin/clock", {"offsetSeconds": 3 * 86400}, s.admin)
    clock = s.call("GET", "/admin/clock", None, s.admin)
    period = str(g(clock, "utcNow"))[:7]  # the business clock's month after time travel
    s.call("POST", "/admin/jobs/finalize-reco-charges", None, s.admin)
    s.call("POST", "/admin/jobs/finalize-attributions", None, s.admin)
    listed = s.call("GET", f"/v1/billing/recommendation-charges?cartRef={cart}&limit=50")
    items = g(listed, "items") or []
    final = where(items, "state", "FINAL")
    s.check("two RA-1 charges FINAL (accept + second add)", len(final) == 2,
            " ".join(f"{g(i, 'state') or ''}:{g(i, 'rejectionReason') or ''}" for i in items))
    rej = first(where(items, "chargeId", no_impression_charge))
    s.check("PDP add without impression is REJECTED (NO_IMPRESSION)", g(rej, "state") == "REJECTED" and g(rej, "rejectionReason") == "NO_IMPRESSION")
    close = s.call("POST", f"/admin/jobs/month-close?period={period}&partnerId={QUICKBASKET_ID}", None, s.admin)
    result = g(close, "result")
    outcome = g(result[0] if isinstance(result, list) and result else result, "outcome")
    if outcome == "ALREADY_CLOSED":
        s.note(f"NOTE: a statement for {period} was already issued by an earlier run; the new charges stay unbilled until the next period.")
        s.note("Restart the API with a fresh dev database (python scripts/run_local.py --reset, or --memory) to replay the billing checks.")
        return True
    s.check("month close issued a statement", outcome == "ISSUED", outcome)
    statements = s.call("GET", f"/v1/billing/statements?period={period}")
    statement = first(g(statements, "items") or [])
    totals = g(statement, "totals")
    print("      statement: FP-1 {0} - reversals {1} + RA-1 {2} ({3} adds) = net {4}; tax {5}; total {6}".format(
        g(totals, "premiumFee", "amount"), g(totals, "reversals", "amount"), g(totals, "recommendationFee", "amount"),
        g(totals, "recommendationChargeCount"), g(totals, "netFee", "amount"), g(totals, "tax", "amount"), g(totals, "totalDue", "amount")))
    s.check("statement issued with RA-1 and FP-1 totals", g(statement, "status") == "ISSUED"
            and Decimal(g(totals, "recommendationFee", "amount") or "0") >= 2 and Decimal(g(totals, "premiumFee", "amount") or "0") >= Decimal("5.80"))
    statement_charges = s.call("GET", f"/v1/billing/statements/{g(statement, 'statementId')}/recommendation-charges")
    s.check("statement lists its RA-1 charges", len(g(statement_charges, "items") or []) == g(totals, "recommendationChargeCount"))

    s.section("7. Dispute an RA-1 charge and resolve it")
    disp = s.call("POST", "/v1/billing/disputes", {"statementId": g(statement, "statementId"), "reason": "NOT_ADDED_TO_CART",
                                                   "recommendationChargeIds": [g(final, 0, "chargeId")], "comment": "Shopper did not add the item"})
    s.check("dispute opened (201)", s.last_status == 201 and g(disp, "status") == "OPEN")
    res = s.call("POST", f"/admin/disputes/{g(disp, 'disputeId')}/resolve", {"decision": "UPHELD", "resolution": "Client logs show a double tap"}, s.admin)
    s.check("dispute upheld with a credit note (INR 1.00 + 18% tax)", g(res, "status") == "UPHELD" and g(res, "creditNoteAmount", "amount") == "1.18")
    credited = s.call("GET", f"/v1/billing/recommendation-charges/{g(final, 0, 'chargeId')}")
    s.check("credited charge shows CREDITED", g(credited, "state") == "CREDITED")
    tb = s.call("GET", "/admin/ledger/trial-balance", None, s.admin)
    s.check("ledger trial balance nets to zero", g(tb, "balanced") is True)

    s.section("8. Webhooks")
    s.call("POST", "/admin/jobs/dispatch-webhooks", None, s.admin)
    time.sleep(2)
    s.call("POST", "/admin/jobs/dispatch-webhooks", None, s.admin)
    hooks = s.call("GET", "/demo/webhooks/quickbasket", None, {})
    signed = [h for h in hooks or [] if isinstance(h, dict) and h.get("type") == "billing.statement.issued" and h.get("signatureValid") is True]
    s.check("signed billing.statement.issued webhook received", len(signed) >= 1)
    return False


def run_mcp(s: Smoke) -> None:
    s.section("9. MCP tools (stateless streamable HTTP)")
    init = s.mcp(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "1.0"}})
    s.check("MCP initialize returns the healthswap server", s.last_status == 200 and g(init, "result", "serverInfo", "name") == "healthswap",
            _compact(init))
    listed = s.mcp(2, "tools/list", {})
    names = {t.get("name") for t in g(listed, "result", "tools") or []}
    s.check("MCP tools/list returns the 5 tools", len(g(listed, "result", "tools") or []) == 5 and names == MCP_TOOLS, sorted(n for n in names if n))
    called = s.mcp(3, "tools/call", {"name": "get_product_health", "arguments": {"market": "IN", "partnerSku": "SKU-NDL-280"}})
    profile = g(called, "result", "structuredContent", "result")
    s.check("MCP get_product_health returns the SKU-NDL-280 profile", g(called, "result", "isError") is not True and g(profile, "productId") is not None
            and g(profile, "hsi") is not None and "HIGH_SODIUM" in (g(profile, "flags") or []), _compact(g(called, "result")))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HealthSwap end-to-end smoke test")
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--client-id", default="quickbasket-server")
    p.add_argument("--client-secret", default="dev-only-quickbasket-secret-change-me")
    p.add_argument("--admin-id", default="hs-admin")
    p.add_argument("--admin-secret", default="dev-only-admin-secret-change-me")
    args = p.parse_args(argv)

    s = Smoke(args.base_url)
    skipped = False
    try:
        s.partner = {"Authorization": "Bearer " + s.token(args.client_id, args.client_secret)}
        s.admin = {"Authorization": "Bearer " + s.token(args.admin_id, args.admin_secret)}
        s.call("POST", "/admin/clock", {"offsetSeconds": 0}, s.admin)
        skipped = run(s)
        run_mcp(s)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as ex:
        s.check("smoke test completed without errors", False, repr(ex))
    finally:
        if s.admin:
            try:
                s.call("POST", "/admin/clock", {"offsetSeconds": 0}, s.admin)
            except httpx.HTTPError as ex:
                print(_color(f"WARN  could not reset the clock offset: {ex!r}", "33"))
        s.http.close()
    print()
    if s.failures == 0:
        print(_color("ALL EXECUTED CHECKS PASSED (billing checks skipped)" if skipped else "ALL CHECKS PASSED", "32"))
        return 0
    print(_color(f"{s.failures} CHECK(S) FAILED", "31"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
