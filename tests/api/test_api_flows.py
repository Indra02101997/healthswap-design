"""Partner API flows through HTTP (`integration`): the in-process API on a fresh, seeded dev database (tests/api/harness.py).

Port of the C# RecommendationFeeFlowTests (RA-1 add fees and FP-1 premium fees through settlement, month close, dispute and the
signed statement webhook) plus the other partner operations: transparency, products, catalog items (matching, match overrides,
delisting) and feeds, inventory, shopper preferences and erasure, the kill switch, partner configuration, cart-evaluation
idempotency, data attributions and the MCP tools. Every /v1 response is validated against api/openapi.yaml.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from harness import (
    COLA,
    MILLET_NOODLES,
    NOODLES,
    NOODLES_GTIN,
    QUICKBASKET,
    STORE,
    Api,
    Caller,
    add_to_cart,
    admin_json,
    alternative,
    evaluate,
    events,
    expect,
    impression,
    inr,
    job,
    new_ref,
    noodle_cart,
    now,
    poll,
    recommended_add,
    server_period,
    set_clock,
    suggestion,
)

from healthswap.domain.primitives import gtin_with_check_digit


def test_labeled_recommendation_adds_are_charged_settled_billed_and_disputable(api: Api) -> None:
    partner, admin = api.caller(), api.caller("admin")
    set_clock(admin, 0)
    try:
        cart, user = new_ref("c_flow_"), new_ref("u_flow_")

        # 1. Evaluation: the alternative carries the label; the disclosure explains the add fee.
        evaluation = evaluate(partner, noodle_cart(cart, user))
        noodles = suggestion(evaluation, "1")
        assert noodles is not None, "no suggestion for the noodles line"
        alt = alternative(noodles, MILLET_NOODLES)
        label = alt["recommendation"]
        assert (label["code"], label["chargeable"], label["feeRule"]) == ("HEALTHSWAP_RECOMMENDED", True, "RA-1")
        assert (alt["unitPrice"]["amount"], alt["unitPriceDelta"]["amount"]) == ("89.00", "33.00")
        assert "add a recommended item" in evaluation["disclosures"]["feeDisclosure"]
        assert suggestion(evaluation, "2") is not None  # approved map: soft drink -> flavoured water
        sid = noodles["suggestionId"]

        # 2. Impression + ACCEPT -> one PENDING INR 1.00 charge; a replayed eventId returns the same charge.
        accept = {"eventId": str(uuid.uuid4()), "type": "ACCEPT", "suggestionId": sid, "alternativePartnerSku": MILLET_NOODLES, "quantity": 2,
                  "occurredAt": now(-3)}
        ack = events(partner, impression(sid, 5), accept)
        assert ack["accepted"] == 2
        first = ack["charges"][0]
        assert (first["state"], first["amount"]) == ("PENDING", inr("1.00"))
        replay = events(partner, accept)
        assert replay["duplicates"] == 1 and replay["charges"][0]["chargeId"] == first["chargeId"]

        # 3. A second add is another charge; an add undone within the grace period is VOID; a non-recommended SKU is rejected.
        second = events(partner, add_to_cart(sid, seconds_ago=2))
        assert second["charges"][0]["state"] == "PENDING"
        undone = events(
            partner,
            {"eventId": str(uuid.uuid4()), "type": "ADD_TO_CART", "suggestionId": sid, "alternativePartnerSku": MILLET_NOODLES, "occurredAt": now(-1)},
            {"eventId": str(uuid.uuid4()), "type": "UNDO", "suggestionId": sid, "alternativePartnerSku": MILLET_NOODLES, "occurredAt": now(1)},
        )
        voided = expect(partner.get(f"/v1/billing/recommendation-charges/{undone['charges'][0]['chargeId']}"), "getRecommendationCharge", 200)
        assert (voided["state"], voided["rejectionReason"]) == ("VOID", "UNDONE_WITHIN_GRACE")
        wrong_sku = events(partner, add_to_cart(sid, sku="SKU-CRM-500", seconds_ago=0))
        assert wrong_sku["rejected"][0]["code"] == "HS-EVENT-SKU-NOT-RECOMMENDED"

        # 4. The cart line now holding the recommended product is labeled.
        again = evaluate(partner, {
            "cartRef": cart, "userRef": user, "market": "IN", "storeId": STORE, "currency": "INR",
            "lines": [{"lineId": "1", "partnerSku": MILLET_NOODLES, "quantity": 3, "unitPrice": inr("89.00")}],
        })
        [line_label] = [x for x in again["lineLabels"] if x["partnerSku"] == MILLET_NOODLES]
        assert line_label["label"]["code"] == "HEALTHSWAP_RECOMMENDED"

        # 5. PDP add without an impression: PENDING now, REJECTED (NO_IMPRESSION) at finalization.
        noodle_id = noodles["original"]["productId"]
        pdp = expect(partner.get(f"/v1/products/{noodle_id}/alternatives?market=IN&storeId={STORE}&cartRef={cart}&placement=PDP"),
                     "listProductAlternatives", 200)
        pdp_ack = events(partner, add_to_cart(pdp["suggestionId"], pdp["alternatives"][0]["product"]["partnerSku"], 0))
        no_impression_id = pdp_ack["charges"][0]["chargeId"]

        # 6. FP-1 order (IN-1): INR 89 list, INR 8 coupon -> INR 85 net per unit, fee INR 5.80; idempotent create; delivery; return.
        order_id = new_ref("ORD-IT-")
        order = {
            "orderId": order_id, "cartRef": cart, "userRef": user, "storeId": STORE, "market": "IN", "currency": "INR", "placedAt": now(),
            "fulfillmentType": "QUICK_COMMERCE", "orderDiscount": inr("8.00"),
            "lines": [
                {"lineId": "1", "partnerSku": MILLET_NOODLES, "quantity": 2, "unitPrice": inr("89.00"), "allocatedOrderDiscount": inr("8.00"),
                 "netAmount": inr("170.00"), "suggestionId": sid},
                {"lineId": "2", "partnerSku": COLA, "quantity": 1, "unitPrice": inr("40.00"), "netAmount": inr("40.00")},
            ],
        }
        key = str(uuid.uuid4())
        created = expect(partner.post("/v1/orders", order, key=key), "createOrder", 201)
        [attributed] = [x for x in created["attribution"]["lines"] if x["lineId"] == "1"]
        assert (attributed["state"], attributed["fee"]["amount"]) == ("PENDING", "5.80")
        replayed = partner.post("/v1/orders", order, key=key)
        assert expect(replayed, "createOrder", 201) == created
        assert replayed.headers["Idempotent-Replayed"] == "true"
        assert expect(partner.post("/v1/orders", order, key=str(uuid.uuid4())), "createOrder", 409)["code"] == "HS-ORDER-EXISTS"
        delivered = expect(partner.patch(f"/v1/orders/{order_id}", {"status": "DELIVERED", "occurredAt": now()}), "updateOrderStatus", 200)
        assert [x["state"] for x in delivered["attribution"]["lines"] if x["lineId"] == "1"] == ["ACCRUED"]
        returned = expect(partner.post(f"/v1/orders/{order_id}/adjustments", {"adjustments": [
            {"adjustmentId": new_ref("ret-"), "lineId": "1", "type": "RETURN", "quantity": 1, "occurredAt": now()},
        ]}), "createOrderAdjustments", 202)
        assert [x["fee"]["amount"] for x in returned["lines"] if x["lineId"] == "1"] == ["2.90"]
        expect(partner.get(f"/v1/orders/{order_id}"), "getOrder", 200)
        expect(partner.get(f"/v1/orders/{order_id}/attribution"), "getOrderAttribution", 200)

        # 7. Settlement after the return window: finalizers, then month close of the server's current period.
        set_clock(admin, 3 * 86400)
        job(admin, "finalize-reco-charges")
        job(admin, "finalize-attributions")
        items = expect(partner.get(f"/v1/billing/recommendation-charges?cartRef={cart}&limit=50"), "listRecommendationCharges", 200)["items"]
        finals = [i for i in items if i["state"] == "FINAL"]
        assert len(finals) == 2
        [rejected] = [i for i in items if i["chargeId"] == no_impression_id]
        assert (rejected["state"], rejected["rejectionReason"]) == ("REJECTED", "NO_IMPRESSION")

        period = server_period(admin)
        close = job(admin, "month-close", f"?period={period}&partnerId={QUICKBASKET}")
        assert close[0]["outcome"] == "ISSUED"
        statement = expect(partner.get(f"/v1/billing/statements?period={period}"), "listStatements", 200)["items"][0]
        statement_id, totals = statement["statementId"], statement["totals"]
        assert statement["status"] == "ISSUED"
        assert Decimal(totals["recommendationFee"]["amount"]) >= 2 and Decimal(totals["premiumFee"]["amount"]) >= Decimal("5.80")
        expect(partner.get(f"/v1/billing/statements/{statement_id}"), "getStatement", 200)
        expect(partner.get(f"/v1/billing/statements/{statement_id}/lines"), "listStatementLines", 200)
        billed = expect(partner.get(f"/v1/billing/statements/{statement_id}/recommendation-charges"), "listStatementRecommendationCharges", 200)
        assert len(billed["items"]) == totals["recommendationChargeCount"]

        # 8. Dispute one RA-1 charge; upheld -> credit note of INR 1.00 + 18% GST.
        disputed = finals[0]["chargeId"]
        dispute = expect(partner.post("/v1/billing/disputes", {
            "statementId": statement_id, "reason": "NOT_ADDED_TO_CART", "recommendationChargeIds": [disputed], "comment": "Shopper did not add the item",
        }), "createDispute", 201)
        assert dispute["status"] == "OPEN"
        resolved = admin_json(admin.post(f"/admin/disputes/{dispute['disputeId']}/resolve",
                                         {"decision": "UPHELD", "resolution": "Client logs show a double tap"}))
        assert (resolved["status"], resolved["creditNoteAmount"]) == ("UPHELD", inr("1.18"))
        expect(partner.get(f"/v1/billing/disputes/{dispute['disputeId']}"), "getDispute", 200)
        assert expect(partner.get(f"/v1/billing/recommendation-charges/{disputed}"), "getRecommendationCharge", 200)["state"] == "CREDITED"
        trial = admin_json(admin.get("/admin/ledger/trial-balance"))
        assert trial["balanced"] is True, trial

        # 9. The statement webhook is delivered (in process) with a valid signature.
        def statement_webhook() -> Any:
            job(admin, "dispatch-webhooks")
            hooks = json.loads(api.anonymous().get("/demo/webhooks/quickbasket").text)
            return [h for h in hooks if h["type"] == "billing.statement.issued" and h["signatureValid"] is True]

        received = poll(statement_webhook, timeout=20, interval=0.5)
        assert received, "billing.statement.issued webhook was not received with a valid signature"
        assert received[0]["body"]["data"]["statementId"] == statement_id
    finally:
        set_clock(admin, 0)


def test_recommended_add_fee_is_additive_and_only_for_labeled_alternatives(api: Api) -> None:
    partner = api.caller()
    evaluation = evaluate(partner, noodle_cart(new_ref("c_add_")))
    for s in evaluation["suggestions"]:
        for a in s["alternatives"]:
            label = a["recommendation"]
            assert label["code"] == "HEALTHSWAP_RECOMMENDED" and label["text"].strip()
            assert "1.00" not in label["text"]  # the shopper label never states the partner fee
    # Events for an unknown suggestion are rejected per event; the batch is still accepted.
    ack = events(partner, add_to_cart(str(uuid.uuid4())))
    assert ack["accepted"] == 0 and ack["rejected"][0]["code"] == "HS-EVENT-UNKNOWN-SUGGESTION"


def test_cart_evaluation_is_idempotent_and_readable(api: Api) -> None:
    partner = api.caller()
    request, key = noodle_cart(new_ref("c_idem_")), str(uuid.uuid4())
    first = expect(partner.post("/v1/cart-evaluations", request, key=key), "evaluateCart", 200)
    second = partner.post("/v1/cart-evaluations", request, key=key)
    assert expect(second, "evaluateCart", 200)["evaluationId"] == first["evaluationId"]
    assert second.headers["Idempotent-Replayed"] == "true"
    # api/openapi.yaml does not list 409 for evaluateCart although the operation takes an Idempotency-Key (see harness.UNDOCUMENTED).
    conflict = expect(partner.post("/v1/cart-evaluations", noodle_cart(new_ref("c_other_")), key=key), "evaluateCart", 409)
    assert conflict["code"] == "HS-IDEMPOTENCY-CONFLICT"
    fetched = expect(partner.get(f"/v1/cart-evaluations/{first['evaluationId']}"), "getCartEvaluation", 200)
    assert (fetched["evaluationId"], fetched["cartRef"]) == (first["evaluationId"], first["cartRef"])
    assert expect(partner.get(f"/v1/cart-evaluations/{uuid.uuid4()}"), "getCartEvaluation", 404)["code"] == "HS-NOT-FOUND"


def test_disclosures_attribute_the_sources_of_originals_and_alternatives(api: Api) -> None:
    """Data-source attributions cover the original and the alternative products a cart evaluation shows (the C# port built them
    from the cart lines' profiles only, so alternatives were never attributed). The seeded catalog is brand-portal data only,
    so the test marks the hot-cache profiles of the noodles (original) and the millet noodles (alternative) as OFF / USDA sourced."""
    partner, cache = api.caller(), api.container.hot_cache
    noodles = suggestion(evaluate(partner, noodle_cart(new_ref("c_attr_"))), "1")
    original_id, alternative_id = uuid.UUID(noodles["original"]["productId"]), uuid.UUID(alternative(noodles, MILLET_NOODLES)["product"]["productId"])
    original, alt = cache.get_profile("IN", original_id), cache.get_profile("IN", alternative_id)
    assert original is not None and alt is not None and original.sources == alt.sources == ("BRAND_PORTAL",)
    try:
        cache.set_profile(dataclasses.replace(alt, sources=("BRAND_PORTAL", "USDA_FDC_BRANDED")))
        attributions = evaluate(partner, noodle_cart(new_ref("c_attr_")))["disclosures"]["attributions"]
        assert [a["source"] for a in attributions] == ["USDA FoodData Central"]
        assert attributions[0] == {
            "source": "USDA FoodData Central", "license": "CC0-1.0",
            "attributionText": "U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.",
        }
        cache.set_profile(dataclasses.replace(original, sources=("BRAND_PORTAL", "OFF")))
        attributions = evaluate(partner, noodle_cart(new_ref("c_attr_")))["disclosures"]["attributions"]
        assert [a["source"] for a in attributions] == ["Open Food Facts", "USDA FoodData Central"]
    finally:
        cache.set_profile(original)
        cache.set_profile(alt)
    assert evaluate(partner, noodle_cart(new_ref("c_attr_")))["disclosures"].get("attributions", []) == []


def test_transparency_operations_are_anonymous(api: Api) -> None:
    anonymous, checked = api.anonymous(), api.hook.checked
    status = expect(anonymous.get("/v1/status"), "getServiceStatus", 200)
    assert status["status"] == "OPERATIONAL" and {r["region"]: r["status"] for r in status["regions"]} == {"IN": "OPERATIONAL", "US": "OPERATIONAL"}
    models = expect(anonymous.get("/v1/scoring-models"), "listScoringModels", 200)
    assert {m["market"] for m in models["items"]} == {"IN", "US"}
    india = expect(anonymous.get("/v1/scoring-models?market=IN"), "listScoringModels", 200)
    assert india["items"] and {m["market"] for m in india["items"]} == {"IN"}
    assert expect(anonymous.get("/v1/scoring-models?market=india"), "listScoringModels", 400)["code"] == "HS-REQ-INVALID"
    assert api.hook.checked == checked + 4  # every /v1 response went through the contract hook


def test_product_lookup_by_gtin_and_sku_and_get(api: Api) -> None:
    partner = api.caller()
    by_gtin = expect(partner.get(f"/v1/products/lookup?market=IN&gtin={NOODLES_GTIN}"), "lookupProduct", 200)
    by_sku = expect(partner.get(f"/v1/products/lookup?market=IN&partnerSku={NOODLES}"), "lookupProduct", 200)
    assert by_gtin["productId"] == by_sku["productId"]
    assert "HIGH_SODIUM" in by_gtin["flags"] and by_gtin["hsi"] is not None
    profile = expect(partner.get(f"/v1/products/{by_gtin['productId']}?market=IN&locale=hi-IN"), "getProductHealthProfile", 200)
    assert profile["productId"] == by_gtin["productId"] and profile["profileVersion"] == by_gtin["profileVersion"]
    unknown = gtin_with_check_digit("890999900001")
    assert expect(partner.get(f"/v1/products/lookup?market=IN&gtin={unknown}"), "lookupProduct", 404)["code"] == "HS-NOT-FOUND"
    expect(partner.get(f"/v1/products/{uuid.uuid4()}?market=IN"), "getProductHealthProfile", 404)


def test_catalog_items_are_matched_read_and_delisted(api: Api) -> None:
    partner = api.caller()
    known, unknown = new_ref("SKU-IT-NDL-"), new_ref("SKU-IT-UNK-")
    batch = expect(partner.post("/v1/catalog/item-batches", {"items": [
        {"partnerSku": known, "gtin": NOODLES_GTIN, "title": "Masala instant noodles (club pack)"},
        {"partnerSku": unknown, "gtin": gtin_with_check_digit("890999911111"), "title": "Qwzx vrtp lkjh 77"},
        {"partnerSku": new_ref("SKU-IT-BAD-"), "gtin": "8901234567891", "title": "Bad check digit"},
    ]}), "upsertCatalogItems", 200)
    assert (batch["received"], batch["accepted"], batch["rejected"]) == (3, 2, 1)
    results = {r["partnerSku"]: r for r in batch["results"]}
    noodle_id = expect(partner.get(f"/v1/products/lookup?market=IN&gtin={NOODLES_GTIN}"), "lookupProduct", 200)["productId"]
    assert results[known]["status"] == "ACCEPTED"
    assert (results[known]["match"]["state"], results[known]["match"]["method"], results[known]["match"]["productId"]) == (
        "AUTO_MATCHED", "GTIN_EXACT", noodle_id)
    assert (results[unknown]["match"]["state"], results[unknown]["match"].get("productId")) == ("UNMATCHED", None)
    [bad] = [r for r in batch["results"] if r["status"] == "REJECTED"]
    assert bad["errors"][0]["code"] == "HS-INVALID-GTIN"

    item = expect(partner.get(f"/v1/catalog/items/{known}"), "getCatalogItem", 200)
    assert (item["isListed"], item["match"]["state"], item["health"]["productId"]) == (True, "AUTO_MATCHED", noodle_id)
    unmatched = expect(partner.get(f"/v1/catalog/items/{unknown}"), "getCatalogItem", 200)
    assert unmatched["match"]["state"] == "UNMATCHED" and unmatched.get("health") is None

    delisted = partner.delete(f"/v1/catalog/items/{known}")
    assert expect(delisted, "delistCatalogItem", 204) is None and delisted.content == b""
    assert expect(partner.get(f"/v1/catalog/items/{known}"), "getCatalogItem", 200)["isListed"] is False
    expect(partner.delete(f"/v1/catalog/items/{new_ref('SKU-NONE-')}"), "delistCatalogItem", 404)
    expect(partner.get(f"/v1/catalog/items/{new_ref('SKU-NONE-')}"), "getCatalogItem", 404)


def test_catalog_match_can_be_assigned_confirmed_and_rejected(api: Api) -> None:
    partner = api.caller()
    sku = new_ref("SKU-IT-OVR-")
    batch = expect(partner.post("/v1/catalog/item-batches", {"items": [
        {"partnerSku": sku, "gtin": gtin_with_check_digit("890999922222"), "title": "Zqxv wrtp mnbv 55"},
    ]}), "upsertCatalogItems", 200)
    assert batch["results"][0]["match"]["state"] == "UNMATCHED"
    noodle_id = expect(partner.get(f"/v1/products/lookup?market=IN&gtin={NOODLES_GTIN}"), "lookupProduct", 200)["productId"]

    def override(body: dict[str, Any], partner_sku: str = sku) -> Any:
        return partner.put(f"/v1/catalog/items/{partner_sku}/match", body)

    assert expect(override({"action": "CONFIRM"}), "overrideCatalogMatch", 422)["code"] == "HS-MATCH-NOTHING-TO-CONFIRM"
    assert expect(override({"action": "ASSIGN"}), "overrideCatalogMatch", 422)["code"] == "HS-MATCH-PRODUCT-REQUIRED"
    assert expect(override({"action": "ASSIGN", "productId": str(uuid.uuid4())}), "overrideCatalogMatch", 422)["code"] == "HS-UNKNOWN-PRODUCT"
    assert expect(override({"action": "MERGE"}), "overrideCatalogMatch", 400)["code"] == "HS-REQ-INVALID"
    expect(override({"action": "REJECT"}, new_ref("SKU-NONE-")), "overrideCatalogMatch", 404)

    assigned = expect(override({"action": "ASSIGN", "productId": noodle_id, "comment": "club pack of the masala noodles"}), "overrideCatalogMatch", 200)
    assert (assigned["match"]["state"], assigned["match"]["method"], assigned["match"]["productId"]) == ("CONFIRMED", "MANUAL", noodle_id)
    assert expect(partner.get(f"/v1/catalog/items/{sku}"), "getCatalogItem", 200)["health"]["productId"] == noodle_id
    assert expect(override({"action": "CONFIRM"}), "overrideCatalogMatch", 200)["match"]["productId"] == noodle_id
    rejected = expect(override({"action": "REJECT", "comment": "not the same product"}), "overrideCatalogMatch", 200)
    assert (rejected["match"]["state"], rejected["match"].get("productId")) == ("REJECTED", None)


def test_catalog_feed_upload_is_processed_with_an_error_report(api: Api) -> None:
    partner = api.caller()
    feed = expect(partner.post("/v1/catalog/feeds", {"format": "JSONL", "mode": "DELTA", "compression": "NONE", "expectedItems": 3}),
                  "createCatalogFeed", 201)
    assert feed["status"] == "AWAITING_UPLOAD" and feed["uploadUrl"].startswith(f"http://testserver/v1/catalog/feeds/{feed['feedId']}/upload?token=")
    lines = [
        json.dumps({"partnerSku": new_ref("SKU-FEED-"), "gtin": NOODLES_GTIN, "title": "Masala noodles (feed)"}),
        "this is not JSON",
        json.dumps({"partnerSku": new_ref("SKU-FEED-"), "title": "Zxqv feed only item 31"}),
    ]
    upload = api.client.put(feed["uploadUrl"], content="\n".join(lines).encode())  # pre-signed: no bearer token
    assert upload.status_code == 202, upload.text
    assert api.client.put(feed["uploadUrl"].replace("token=", "token=x"), content=b"{}").status_code == 403

    def finished() -> Any:
        f = expect(partner.get(f"/v1/catalog/feeds/{feed['feedId']}"), "getCatalogFeed", 200)
        return f if f["status"] not in ("AWAITING_UPLOAD", "PROCESSING") else None

    done = poll(finished)
    assert done is not None, "the feed was not processed"
    assert done["status"] == "PARTIAL" and "uploadUrl" not in done
    stats = done["stats"]
    assert (stats["received"], stats["accepted"], stats["rejected"], stats["autoMatched"]) == (3, 2, 1, 1)
    report = partner.get(done["errorReportUrl"])
    assert report.status_code == 200 and report.headers["content-type"].startswith("text/plain")
    assert report.text.splitlines()[0].startswith("line 2: ")
    expect(partner.get(f"/v1/catalog/feeds/{uuid.uuid4()}"), "getCatalogFeed", 404)


def test_store_inventory_upsert(api: Api) -> None:
    partner = api.caller()
    ack = expect(partner.post(f"/v1/stores/{STORE}/inventory-batches", {"snapshotAt": now(), "mode": "UPSERT", "entries": [
        {"partnerSku": NOODLES, "inStock": True, "availableQuantity": 40, "price": inr("56.00"), "mrp": inr("60.00")},
        {"partnerSku": COLA, "inStock": True, "price": {"amount": "0.50", "currency": "USD"}},
    ]}), "upsertStoreInventory", 202)
    assert (ack["accepted"], ack["rejected"]) == (1, 1)
    assert ack["errors"] == [{"partnerSku": COLA, "code": "HS-CURRENCY-MISMATCH", "message": "Price currency must be INR"}]
    unknown_store = partner.post("/v1/stores/NO-SUCH-STORE/inventory-batches", {"snapshotAt": now(), "mode": "UPSERT", "entries": [
        {"partnerSku": NOODLES, "inStock": True, "price": inr("56.00")},
    ]})
    assert expect(unknown_store, "upsertStoreInventory", 422)["code"] == "HS-UNKNOWN-STORE"
    invalid = partner.post(f"/v1/stores/{STORE}/inventory-batches", {"snapshotAt": now(), "mode": "DELTA", "entries": []})
    assert expect(invalid, "upsertStoreInventory", 400)["code"] == "HS-REQ-INVALID"


def test_shopper_preferences_consent_and_erasure(api: Api) -> None:
    partner = api.caller()
    user = new_ref("u_prefs_")
    expect(partner.get(f"/v1/users/{user}/preferences"), "getShopperPreferences", 404)
    preferences = {
        "dietType": "VEGETARIAN", "allergens": ["PEANUTS", "MILK"], "goals": ["LOWER_SUGAR"], "avoidIngredients": ["PALM_OIL"],
        "excludedBrands": ["Fizzo"], "locale": "en-IN", "personalizationConsent": True, "consentNoticeVersion": "2026-01",
    }
    # Without health-data consent the health fields (allergens, goals) are not stored.
    stored = expect(partner.put(f"/v1/users/{user}/preferences", {**preferences, "healthDataConsent": False}), "putShopperPreferences", 200)
    assert (stored["healthDataConsent"], stored["allergens"], stored["goals"]) == (False, [], [])
    assert (stored["dietType"], stored["avoidIngredients"], stored["excludedBrands"]) == ("VEGETARIAN", ["PALM_OIL"], ["Fizzo"])
    stored = expect(partner.put(f"/v1/users/{user}/preferences", {**preferences, "healthDataConsent": True}), "putShopperPreferences", 200)
    assert (stored["healthDataConsent"], stored["allergens"], stored["goals"]) == (True, ["PEANUTS", "MILK"], ["LOWER_SUGAR"])
    read = expect(partner.get(f"/v1/users/{user}/preferences"), "getShopperPreferences", 200)
    assert read == stored
    invalid = partner.put(f"/v1/users/{user}/preferences", {"allergens": ["PEANUT"], "healthDataConsent": True})
    assert expect(invalid, "putShopperPreferences", 400)["code"] == "HS-REQ-INVALID"

    erasure = expect(partner.delete(f"/v1/users/{user}"), "eraseShopper", 202)
    assert erasure["status"] == "COMPLETED"
    expect(partner.get(f"/v1/users/{user}/preferences"), "getShopperPreferences", 404)


def test_kill_switch_stops_suggestions_until_switched_off(api: Api) -> None:
    partner, admin = api.caller(), api.caller("admin")
    switched = admin_json(admin.post(f"/admin/partners/{QUICKBASKET}/kill-switch", {"enabled": True}))
    assert switched["killSwitch"] is True
    try:
        evaluation = evaluate(partner, noodle_cart(new_ref("c_kill_")))
        assert evaluation["degraded"] is True and "KILL_SWITCH" in evaluation["degradationReasons"] and evaluation["suggestions"] == []
    finally:
        admin_json(admin.post(f"/admin/partners/{QUICKBASKET}/kill-switch", {"enabled": False}))
    evaluation = evaluate(partner, noodle_cart(new_ref("c_kill_")))
    assert evaluation["degraded"] is False and suggestion(evaluation, "1") is not None


def test_admin_config_rejects_an_invalid_configuration(api: Api) -> None:
    admin = api.caller("admin")
    [partner] = [p for p in admin_json(admin.get("/admin/partners")) if p["partnerId"] == QUICKBASKET]
    config = partner["config"]
    invalid = admin.put(f"/admin/partners/{QUICKBASKET}/config", {**config, "fee": {**config.get("fee", {}), "rate": 1.5}, "maxAlternativesPerLine": 9})
    assert invalid.status_code == 400 and invalid.headers["content-type"].startswith("application/problem+json")
    problem = invalid.json()
    assert problem["code"] == "HS-REQ-INVALID" and problem["detail"] == "Invalid partner configuration"
    assert [e["message"] for e in problem["errors"]] == ["fee.rate must be between 0 and 1", "maxAlternativesPerLine must be 1-5"]
    assert admin.put(f"/admin/partners/{QUICKBASKET}/config", {"taxRate": "abc"}).status_code == 400
    # Nothing was stored: the configuration is unchanged and evaluations still work.
    [after] = [p for p in admin_json(admin.get("/admin/partners")) if p["partnerId"] == QUICKBASKET]
    assert after["config"] == config
    assert suggestion(evaluate(api.caller(), noodle_cart(new_ref("c_cfg_"))), "1") is not None


def _mcp(caller: Caller, request_id: int, method: str, params: dict[str, Any]) -> Any:
    res = caller.post("/mcp", {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                      headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/event-stream")
    for line in res.text.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:])
            if message.get("id") == request_id:
                return message
    raise AssertionError(f"no JSON-RPC response with id {request_id}: {res.text}")


def test_mcp_tools_over_streamable_http(api: Api) -> None:
    agent = api.caller()
    init = _mcp(agent, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1.0"}})
    assert init["result"]["serverInfo"]["name"] == "healthswap"
    tools = _mcp(agent, 2, "tools/list", {})["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["evaluate_cart", "explain_swap", "find_healthier_alternatives", "get_product_health",
                                                "record_swap_decision"]

    called = _mcp(agent, 3, "tools/call", {"name": "evaluate_cart", "arguments": {
        "market": "IN", "currency": "INR", "cartRef": new_ref("c_mcp_"), "storeId": STORE, "locale": "en-IN",
        "lines": [{"partnerSku": NOODLES, "quantity": 2, "unitPrice": "56.00"}, {"partnerSku": COLA, "quantity": 1, "unitPrice": "40.00"}],
    }})["result"]
    assert called.get("isError") is not True, called
    result = called["structuredContent"]["result"]
    assert json.loads(called["content"][0]["text"])["evaluationId"] == result["evaluationId"]
    [noodles] = [s for s in result["suggestions"] if s["lineId"] == "1"]
    assert (noodles["original"]["partnerSku"], noodles["alternatives"][0]["partnerSku"]) == (NOODLES, MILLET_NOODLES)
    assert all(a["label"] for a in noodles["alternatives"]) and "record_swap_decision" in result["instructions"]

    health = _mcp(agent, 4, "tools/call", {"name": "get_product_health", "arguments": {"market": "IN", "gtin": NOODLES_GTIN}})["result"]
    profile = health["structuredContent"]["result"]
    lookup = expect(agent.get(f"/v1/products/lookup?market=IN&partnerSku={NOODLES}"), "lookupProduct", 200)
    assert health.get("isError") is not True and profile["productId"] == lookup["productId"] and "HIGH_SODIUM" in profile["flags"]
    missing = _mcp(agent, 5, "tools/call", {"name": "get_product_health", "arguments": {"market": "IN", "partnerSku": "SKU-DOES-NOT-EXIST"}})
    assert missing["result"]["isError"] is True and missing["result"]["content"][0]["text"].startswith("HS-NOT-FOUND")


def test_demo_shop_charge_view_shows_the_ra1_charges_of_its_cart(api: Api) -> None:
    """The demo shop (web/demo): a browser SDK session minted by the demo backend, an evaluation, the shopper's adds and an undo,
    and the demo partner view (GET /demo/api/charges) of exactly that cart's RA-1 charges."""
    browser = api.anonymous()
    cart_ref = "c_demo_" + uuid.uuid4().hex[:12]
    session = browser.post("/demo/api/session", {"cartRef": cart_ref, "market": "IN", "storeId": STORE})
    assert session.status_code == 201, session.text
    sdk = api.bearer(session.json()["token"])
    sid = suggestion(evaluate(sdk, noodle_cart(cart_ref)), "1")["suggestionId"]
    accept = {"eventId": str(uuid.uuid4()), "type": "ACCEPT", "suggestionId": sid, "alternativePartnerSku": MILLET_NOODLES, "quantity": 2,
              "occurredAt": now(-3)}
    add = add_to_cart(sid, seconds_ago=2)
    ack = events(sdk, impression(sid, 4), accept, add)
    assert ack["accepted"] == 3 and "charges" not in ack  # a browser session never sees billing data

    def charges(ref: str) -> list[dict[str, Any]]:
        r = browser.get(f"/demo/api/charges?cartRef={ref}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
        assert r.json()["cartRef"] == ref
        return r.json()["items"]

    assert sorted((c["eventType"], c["state"], c["amount"]["amount"], c["cartRef"]) for c in charges(cart_ref)) == [
        ("ACCEPT", "PENDING", "1.00", cart_ref), ("ADD_TO_CART", "PENDING", "1.00", cart_ref)]

    # Undo within the 5 s grace period: the latest add becomes VOID.
    undo_at = datetime.fromisoformat(add["occurredAt"].replace("Z", "+00:00")) + timedelta(seconds=2)
    events(sdk, {"eventId": str(uuid.uuid4()), "type": "UNDO", "suggestionId": sid, "alternativePartnerSku": MILLET_NOODLES,
                 "occurredAt": undo_at.isoformat().replace("+00:00", "Z")})
    assert {c["eventType"]: (c["state"], c.get("rejectionReason")) for c in charges(cart_ref)} == {
        "ACCEPT": ("PENDING", None), "ADD_TO_CART": ("VOID", "UNDONE_WITHIN_GRACE")}

    # Only that cart: another demo cart has no charges, and a partner-API cart is not readable through the demo view.
    assert charges("c_demo_" + uuid.uuid4().hex[:12]) == []
    other_cart, _, _ = recommended_add(api.caller())
    r = browser.get(f"/demo/api/charges?cartRef={other_cart}")
    assert (r.status_code, r.json()["code"]) == (400, "HS-REQ-INVALID")
