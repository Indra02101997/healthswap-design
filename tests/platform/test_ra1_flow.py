"""RA-1 end-to-end on PostgreSQL (`integration`): cart evaluation with the HEALTHSWAP_RECOMMENDED label, suggestion events
(impression, accept, replay, add, undo, wrong SKU, unknown suggestion), line labels, PDP alternatives, the finalizer after
time travel, the charge list, the ledger trial balance, the kill switch and SDK-session scoping - the Python port of the RA-1
part of the C# RecommendationFeeFlowTests and SecurityTests, driven through the services (no HTTP).

Runs on a fresh dev database of its own (the `fresh_database_url` fixture in tests/conftest.py). The module seeds the demo
tenants once and runs its tests in file order (each step builds on the previous one).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from healthswap.domain.primitives import iso
from healthswap.platform.container import Container
from healthswap.platform.db import DbRole
from healthswap.platform.errors import HsError
from healthswap.platform.settings import load_settings
from healthswap.platform.tenancy import PartnerContext

pytestmark = pytest.mark.integration

QB = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")  # QuickBasket (IN)
STORE = "BLR-KOR-012"
NOODLES = "SKU-NDL-280"
MILLET_NOODLES = "SKU-MLT-256"
# data/seed/demo/partners.json: the quickbasket-server client.
SERVER_SCOPES = frozenset({
    "catalog.write", "catalog.read", "inventory.write", "products.read", "carts.evaluate", "events.write", "orders.write", "orders.read", "users.write",
    "users.read", "billing.read", "billing.write", "sdk.sessions.write",
})
SERVER = PartnerContext(partner_id=QB, client_id="quickbasket-server", scopes=SERVER_SCOPES)


def _ref(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def _inr(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "INR"}


def _noodle_cart(cart_ref: str, user_ref: str | None = None) -> dict[str, Any]:
    return {
        "cartRef": cart_ref, "userRef": user_ref, "market": "IN", "storeId": STORE, "currency": "INR", "locale": "en-IN",
        "lines": [
            {"lineId": "1", "partnerSku": NOODLES, "gtin": "8901234567890", "quantity": 2, "unitPrice": _inr("56.00")},
            {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": _inr("40.00")},
        ],
    }


def _drain(c: Container) -> None:
    while c.outbox_relay.pump() > 0:
        pass
    assert c.bus.wait_idle(60)


@pytest.fixture(scope="module")
def c(fresh_database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Container]:
    tmp = tmp_path_factory.mktemp("ra1")
    settings = load_settings(
        environment="Development",
        overrides={
            "database.url": fresh_database_url, "database.max_pool_size": 4, "llm.provider": "Mock", "lake.root": str(tmp / "lake"),
            "data_protection.keys_directory": str(tmp / "keys"), "feeds.storage_root": str(tmp / "feeds"),
        },
        env={},
    )
    with Container(settings) as container:
        container.start_workers(include_scheduler=False, include_cache_warmer=False, include_webhooks=False)
        container.demo_seeder.seed(container.repo_root, True)
        _drain(container)
        container.cache_warmer.warm_all()
        try:
            yield container
        finally:
            container.offset_clock.offset = timedelta(0)


@pytest.fixture(scope="module")
def state() -> dict[str, Any]:
    return {"cart": _ref("c_flow_"), "user": _ref("u_flow_")}


def _now(c: Container, seconds: float = 0) -> str:
    return iso(c.clock.utc_now() + timedelta(seconds=seconds)) or ""


def _event(c: Container, event_type: str, sid: str, sku: str | None = MILLET_NOODLES, seconds: float = -1, quantity: int | None = 1,
           event_id: uuid.UUID | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"eventId": str(event_id or uuid.uuid4()), "type": event_type, "suggestionId": sid, "occurredAt": _now(c, seconds)}
    if sku is not None:
        e["alternativePartnerSku"] = sku
    if quantity is not None and event_type != "IMPRESSION":
        e["quantity"] = quantity
    return e


def _events(c: Container, ctx: PartnerContext, *events: dict[str, Any]) -> dict[str, Any]:
    return c.event_service.ingest(ctx, {"events": list(events)})


def _suggestion(evaluation: dict[str, Any], line_id: str) -> dict[str, Any] | None:
    return next((s for s in evaluation["suggestions"] if s["lineId"] == line_id), None)


def _alternative(suggestion: dict[str, Any], sku: str) -> dict[str, Any]:
    return next(a for a in suggestion["alternatives"] if a["product"]["partnerSku"] == sku)


def test_1_evaluation_labels_the_alternative_and_discloses_the_add_fee(c: Container, state: dict[str, Any]) -> None:
    evaluation = c.evaluation_service.evaluate(SERVER, _noodle_cart(state["cart"], state["user"]))
    assert "degradationReasons" not in evaluation and evaluation["degraded"] is False, evaluation.get("degradationReasons")
    noodles = _suggestion(evaluation, "1")
    assert noodles is not None, "no suggestion for the noodles line"
    alt = _alternative(noodles, MILLET_NOODLES)
    assert alt["recommendation"]["code"] == "HEALTHSWAP_RECOMMENDED"
    assert alt["recommendation"]["chargeable"] is True and alt["recommendation"]["feeRule"] == "RA-1"
    assert alt["unitPrice"] == _inr("89.00") and alt["unitPriceDelta"] == _inr("33.00")
    assert alt["normalizedPriceRatio"] == Decimal("1.74")
    assert "add a recommended item" in evaluation["disclosures"]["feeDisclosure"]
    assert _suggestion(evaluation, "2") is not None  # approved map: soft drink -> flavoured water
    assert evaluation["lineLabels"] == [] and evaluation["summary"]["linesWithSuggestions"] == 2
    assert list(evaluation) == [
        "evaluationId", "cartRef", "market", "storeId", "createdAt", "expiresAt", "degraded", "summary", "suggestions", "lineLabels", "disclosures",
        "modelVersions",
    ]
    # GET /cart-evaluations/{id} returns the cached result; other partners do not see it.
    assert c.evaluation_service.get(QB, uuid.UUID(evaluation["evaluationId"])) == evaluation
    with pytest.raises(HsError) as ei:
        c.evaluation_service.get(uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e02"), uuid.UUID(evaluation["evaluationId"]))
    assert ei.value.status == 404
    state["sid"] = noodles["suggestionId"]
    state["noodle_product"] = noodles["original"]["productId"]


def test_2_events_create_pending_charges_dedupe_void_and_reject(c: Container, state: dict[str, Any]) -> None:
    sid = state["sid"]
    accept_id = uuid.uuid4()
    accept = _event(c, "ACCEPT", sid, quantity=2, seconds=-3, event_id=accept_id)
    ack = _events(c, SERVER, _event(c, "IMPRESSION", sid, sku=None, seconds=-5), accept)
    assert ack["accepted"] == 2 and "rejected" not in ack
    first = ack["charges"][0]
    assert first["state"] == "PENDING" and first["amount"] == _inr("1.00") and first["eventId"] == str(accept_id) and "finalizesAt" in first
    replay = _events(c, SERVER, accept)
    assert replay["duplicates"] == 1 and replay["accepted"] == 0 and replay["charges"][0]["chargeId"] == first["chargeId"]

    second = _events(c, SERVER, _event(c, "ADD_TO_CART", sid, seconds=-2))
    assert second["charges"][0]["state"] == "PENDING" and second["charges"][0]["chargeId"] != first["chargeId"]

    undone = _events(c, SERVER, _event(c, "ADD_TO_CART", sid, seconds=-1), _event(c, "UNDO", sid, seconds=1, quantity=None))
    assert undone["accepted"] == 2
    voided = c.reco_charge_service.get(QB, uuid.UUID(undone["charges"][0]["chargeId"]))
    assert voided["state"] == "VOID" and voided["rejectionReason"] == "UNDONE_WITHIN_GRACE" and "finalizesAt" not in voided

    wrong = _events(c, SERVER, _event(c, "ADD_TO_CART", sid, sku="SKU-CRM-500", seconds=0))
    assert wrong["accepted"] == 0 and wrong["rejected"][0]["code"] == "HS-EVENT-SKU-NOT-RECOMMENDED"
    unknown = _events(c, SERVER, _event(c, "ADD_TO_CART", str(uuid.uuid4())))
    assert unknown["accepted"] == 0 and unknown["rejected"][0]["code"] == "HS-EVENT-UNKNOWN-SUGGESTION"
    state["accept_charge"] = first["chargeId"]
    state["second_charge"] = second["charges"][0]["chargeId"]
    state["void_charge"] = undone["charges"][0]["chargeId"]


def test_3_the_cart_line_holding_the_recommended_product_is_labeled(c: Container, state: dict[str, Any]) -> None:
    again = c.evaluation_service.evaluate(SERVER, {
        "cartRef": state["cart"], "userRef": state["user"], "market": "IN", "storeId": STORE, "currency": "INR",
        "lines": [{"lineId": "1", "partnerSku": MILLET_NOODLES, "quantity": 3, "unitPrice": _inr("89.00")}],
    })
    labels = [x for x in again["lineLabels"] if x["partnerSku"] == MILLET_NOODLES]
    assert len(labels) == 1 and labels[0]["label"]["code"] == "HEALTHSWAP_RECOMMENDED" and labels[0]["suggestionId"] == state["sid"]
    assert again["suggestions"] == []  # a labeled line is not a swap target


def test_4_pdp_alternatives_mint_a_suggestion_for_the_cart(c: Container, state: dict[str, Any]) -> None:
    pdp = c.evaluation_service.alternatives(SERVER, uuid.UUID(state["noodle_product"]), "IN", store_id=STORE, cart_ref=state["cart"], placement="PDP")
    assert pdp["suggestionId"] and pdp["alternatives"] and pdp["alternatives"][0]["recommendation"]["code"] == "HEALTHSWAP_RECOMMENDED"
    assert list(pdp) == ["productId", "market", "storeId", "suggestionId", "expiresAt", "alternatives", "disclosures"]
    ack = _events(c, SERVER, _event(c, "ADD_TO_CART", pdp["suggestionId"], sku=pdp["alternatives"][0]["product"]["partnerSku"], seconds=0))
    assert ack["accepted"] == 1 and ack["charges"][0]["state"] == "PENDING"
    state["pdp_charge"] = ack["charges"][0]["chargeId"]
    # Without a cart there is no suggestion to charge against.
    anonymous = c.evaluation_service.alternatives(SERVER, uuid.UUID(state["noodle_product"]), "IN", store_id=STORE)
    assert "suggestionId" not in anonymous and anonymous["alternatives"][0]["recommendation"]["chargeable"] is False


def test_5_finalizer_settles_after_time_travel_and_the_ledger_balances(c: Container, state: dict[str, Any]) -> None:
    c.offset_clock.offset = timedelta(days=3)
    try:
        assert c.evaluation_recorder.flush(10)
        final, rejected = c.reco_charge_service.finalize_due()
        assert (final, rejected) == (2, 1)
        assert c.reco_charge_service.finalize_due() == (0, 0)
    finally:
        c.offset_clock.offset = timedelta(0)

    page = c.reco_charge_service.list(QB, cart_ref=state["cart"], limit=50)
    items = {i["chargeId"]: i for i in page["items"]}
    assert "nextCursor" not in page and len(items) == 4
    assert items[state["accept_charge"]]["state"] == "FINAL" and items[state["accept_charge"]]["quantity"] == Decimal("2")
    assert items[state["second_charge"]]["state"] == "FINAL" and "finalizedAt" in items[state["second_charge"]]
    assert items[state["void_charge"]]["state"] == "VOID"
    assert (items[state["pdp_charge"]]["state"], items[state["pdp_charge"]]["rejectionReason"]) == ("REJECTED", "NO_IMPRESSION")
    occurred = [i["occurredAt"] for i in page["items"]]
    assert occurred == sorted(occurred, reverse=True)

    # Keyset pagination walks the same rows one at a time.
    walked, cursor = [], None
    while True:
        p = c.reco_charge_service.list(QB, cart_ref=state["cart"], limit=1, cursor=cursor)
        walked += [i["chargeId"] for i in p["items"]]
        cursor = p.get("nextCursor")
        if cursor is None:
            break
    assert walked == [i["chargeId"] for i in page["items"]]
    assert [i["chargeId"] for i in c.reco_charge_service.list(QB, cart_ref=state["cart"], state="FINAL")["items"]] == [
        i["chargeId"] for i in page["items"] if i["state"] == "FINAL"
    ]
    with pytest.raises(HsError):
        c.reco_charge_service.list(QB, period="2026-13")
    with pytest.raises(HsError) as ei:
        c.reco_charge_service.list(QB, statement_id=uuid.uuid4())
    assert ei.value.status == 404

    with c.db.tx(DbRole.OPS) as u:
        rows = u.query(
            """
            select a.code as account, a.currency, sum(l.debit) as debit, sum(l.credit) as credit, sum(l.debit) - sum(l.credit) as balance
            from billing.journal_line l join billing.account a on a.account_id = l.account_id
            where (@partnerId::uuid is null or l.partner_id = @partnerId)
            group by a.code, a.currency order by a.currency, a.code
            """,
            {"partnerId": None},
        )
        suggestions = u.scalar("select count(*) from txn.suggestion where cart_ref = @cart", {"cart": state["cart"]})
    assert rows and sum(r["debit"] for r in rows) >= Decimal("2.00")
    by_currency: dict[str, Decimal] = {}
    for r in rows:
        by_currency[r["currency"].strip()] = by_currency.get(r["currency"].strip(), Decimal(0)) + r["balance"]
    assert all(b == 0 for b in by_currency.values()), rows
    assert suggestions == 3  # noodles + cola from the evaluation, the PDP suggestion


def _set_kill_switch(c: Container, config_json: str, enabled: bool) -> None:
    """The admin plane's kill-switch endpoint (C# AdminEndpoints UpdateConfigAsync + PartnerDirectory.Invalidate)."""
    node = json.loads(config_json)
    node["killSwitch"] = enabled
    text = json.dumps(node, separators=(",", ":"))
    with c.db.tx(DbRole.OPS) as u:
        assert u.execute("update tenant.partner set config = @json::jsonb where partner_id = @partnerId", {"json": text, "partnerId": QB}) == 1
        u.execute(
            "insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, @action, 'PARTNER_CONFIG', @id, @details::jsonb)",
            {"actor": "test_ra1_flow", "action": "KILL_SWITCH_ON" if enabled else "KILL_SWITCH_OFF", "id": str(QB), "details": text},
        )
    c.partners.invalidate(QB)


def test_6_kill_switch_stops_suggestions(c: Container, state: dict[str, Any]) -> None:
    original = c.partners.get(QB).config_json
    _set_kill_switch(c, original, True)
    try:
        assert c.partners.get(QB).config.kill_switch
        evaluation = c.evaluation_service.evaluate(SERVER, _noodle_cart(_ref("c_kill_")))
        assert evaluation["suggestions"] == [] and evaluation["degraded"] is True and "KILL_SWITCH" in evaluation["degradationReasons"]
        pdp = c.evaluation_service.alternatives(SERVER, uuid.UUID(state["noodle_product"]), "IN", store_id=STORE, cart_ref=_ref("c_kill_"))
        assert pdp["alternatives"] == [] and "suggestionId" not in pdp
    finally:
        _set_kill_switch(c, original, False)
    assert not c.partners.get(QB).config.kill_switch
    assert c.evaluation_service.evaluate(SERVER, _noodle_cart(_ref("c_back_")))["suggestions"]


def test_7_sdk_sessions_are_bound_to_their_cart(c: Container) -> None:
    cart = _ref("c_sdk_")
    sdk = PartnerContext(partner_id=QB, client_id="quickbasket-server", scopes=frozenset({"carts.evaluate", "events.write", "products.read"}),
                         is_sdk_session=True, sdk_cart_ref=cart, sdk_store_id=STORE, sdk_market="IN")
    evaluation = c.evaluation_service.evaluate(sdk, _noodle_cart(cart))
    with pytest.raises(HsError) as ei:
        c.evaluation_service.evaluate(sdk, _noodle_cart(_ref("c_other_")))
    assert (ei.value.status, ei.value.code) == (403, "HS-AUTH-SCOPE")

    sid = _suggestion(evaluation, "1")["suggestionId"]  # type: ignore[index]
    ack = _events(c, sdk, _event(c, "IMPRESSION", sid, sku=None, seconds=-3), _event(c, "ADD_TO_CART", sid))
    assert ack["accepted"] == 2 and "charges" not in ack  # charge acks go to server-to-server callers only (C# EventService)

    other = c.evaluation_service.evaluate(SERVER, _noodle_cart(_ref("c_it_")))
    foreign = _events(c, sdk, _event(c, "ADD_TO_CART", _suggestion(other, "1")["suggestionId"]))  # type: ignore[index]
    assert foreign["accepted"] == 0 and foreign["rejected"][0]["code"] == "HS-AUTH-SESSION-SCOPE"
