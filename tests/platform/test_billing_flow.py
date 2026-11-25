"""Orders, FP-1 attribution, month close, disputes and the statement webhook end to end on PostgreSQL (`integration`): the
Python port of steps 1-8 of scripts/smoke-test.ps1 (C# RecommendationFeeFlowTests), driven through the services (no HTTP).

    a. cart evaluation + RA-1 events: impression, ACCEPT (qty 2), another add, an add + undo (VOID), a PDP add without impression
    b. order IN-1 (fee INR 5.80 PENDING), HS-ORDER-EXISTS, Idempotency-Key replay and conflict
    c. delivery accrues the fee; returning 1 of 2 units reverses INR 2.90
    d. time travel (+3 days) and the finalizers (`hs jobs finalize`): 2 FINAL RA-1 charges, the PDP add REJECTED (NO_IMPRESSION)
    e. month close (`hs billing month-close`): ISSUED statement, lines, billed charges, ALREADY_CLOSED on a second close
    f. dispute an RA-1 charge, resolve UPHELD: credit note INR 1.18, the charge CREDITED, the ledger balanced; a payment settles
    g. the billing.statement.issued webhook is queued for the partner's endpoint
    h. ML data: export, ranker simulation and the explanation evaluation harness against the seeded knowledge base

Runs on a fresh dev database of its own (the `fresh_database_url` fixture in tests/conftest.py). The module seeds the demo
tenants once and runs its tests in file order (each step builds on the previous one).
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from healthswap import cli
from healthswap.domain.primitives import iso, parse_iso, period_of
from healthswap.platform import jsonutil
from healthswap.platform.container import Container
from healthswap.platform.db import Db, DbOptions, DbRole
from healthswap.platform.errors import HsError
from healthswap.platform.services.billing import BillingService
from healthswap.platform.services.infrastructure import IdempotentResponse
from healthswap.platform.settings import load_settings
from healthswap.platform.tenancy import PartnerContext

pytestmark = pytest.mark.integration

QB = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")  # QuickBasket (IN)
HOMETOWN = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e02")  # Hometown Grocer (US)
STORE = "BLR-KOR-012"
NOODLES = "SKU-NDL-280"
MILLET_NOODLES = "SKU-MLT-256"
# data/seed/demo/partners.json: the quickbasket-server client.
SERVER_SCOPES = frozenset({
    "catalog.write", "catalog.read", "inventory.write", "products.read", "carts.evaluate", "events.write", "orders.write", "orders.read", "users.write",
    "users.read", "billing.read", "billing.write", "sdk.sessions.write",
})
SERVER = PartnerContext(partner_id=QB, client_id="quickbasket-server", scopes=SERVER_SCOPES)
D = Decimal


def _ref(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def _inr(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "INR"}


def _drain(c: Container) -> None:
    while c.outbox_relay.pump() > 0:
        pass
    assert c.bus.wait_idle(60)


@pytest.fixture(scope="module")
def c(fresh_database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Container]:
    tmp = tmp_path_factory.mktemp("billing")
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
    return {"cart": _ref("c_bill_"), "user": _ref("u_bill_"), "order": _ref("ORD-FLOW-")}


def _now(c: Container, seconds: float = 0) -> str:
    return iso(c.clock.utc_now() + timedelta(seconds=seconds)) or ""


def _event(c: Container, event_type: str, sid: str, sku: str | None = MILLET_NOODLES, seconds: float = -1, quantity: int | None = 1) -> dict[str, Any]:
    e: dict[str, Any] = {"eventId": str(uuid.uuid4()), "type": event_type, "suggestionId": sid, "occurredAt": _now(c, seconds)}
    if sku is not None:
        e["alternativePartnerSku"] = sku
    if quantity is not None and event_type not in ("IMPRESSION", "UNDO"):
        e["quantity"] = quantity
    return e


def _events(c: Container, *events: dict[str, Any]) -> dict[str, Any]:
    return c.event_service.ingest(SERVER, {"events": list(events)})


def _line(attribution: dict[str, Any], line_id: str) -> dict[str, Any]:
    return next(x for x in attribution["lines"] if x["lineId"] == line_id)


def _audit_rows(c: Container, entity_id: str) -> list[dict[str, Any]]:
    """audit.change_log is append-only for every application role: read it as the connection's own user (no role switch)."""
    owner = Db(DbOptions(url=c.settings.database.url, max_pool_size=1, use_roles=False))
    try:
        with owner.tx(DbRole.OPS) as u:
            return u.query("select actor, action, entity_type, details::text as details from audit.change_log where entity_id = @id order by id",
                           {"id": entity_id})
    finally:
        owner.close()


def _order(state: dict[str, Any], placed_at: str) -> dict[str, Any]:
    return {
        "orderId": state["order"], "cartRef": state["cart"], "userRef": state["user"], "storeId": STORE, "market": "IN", "currency": "INR",
        "placedAt": placed_at, "fulfillmentType": "QUICK_COMMERCE", "orderDiscount": _inr("8.00"),
        "lines": [
            {"lineId": "1", "partnerSku": MILLET_NOODLES, "quantity": 2, "unitPrice": _inr("89.00"), "allocatedOrderDiscount": _inr("8.00"),
             "netAmount": _inr("170.00"), "suggestionId": state["sid"]},
            {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": _inr("40.00"), "netAmount": _inr("40.00")},
        ],
    }


def test_a_evaluation_and_ra1_events(c: Container, state: dict[str, Any]) -> None:
    evaluation = c.evaluation_service.evaluate(SERVER, {
        "cartRef": state["cart"], "userRef": state["user"], "market": "IN", "storeId": STORE, "currency": "INR", "locale": "en-IN",
        "lines": [
            {"lineId": "1", "partnerSku": NOODLES, "gtin": "8901234567890", "quantity": 2, "unitPrice": _inr("56.00")},
            {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": _inr("40.00")},
        ],
    })
    noodles = next(s for s in evaluation["suggestions"] if s["lineId"] == "1")
    assert any(a["product"]["partnerSku"] == MILLET_NOODLES for a in noodles["alternatives"])
    sid = noodles["suggestionId"]
    state["sid"] = sid

    ack = _events(c, _event(c, "IMPRESSION", sid, sku=None, seconds=-5), _event(c, "ACCEPT", sid, quantity=2, seconds=-3))
    assert ack["accepted"] == 2 and ack["charges"][0]["state"] == "PENDING" and ack["charges"][0]["amount"] == _inr("1.00")
    second = _events(c, _event(c, "ADD_TO_CART", sid, seconds=-2))
    assert second["charges"][0]["state"] == "PENDING"
    undone = _events(c, _event(c, "ADD_TO_CART", sid, seconds=-1), _event(c, "UNDO", sid, seconds=1))
    assert c.reco_charge_service.get(QB, uuid.UUID(undone["charges"][0]["chargeId"]))["state"] == "VOID"

    pdp = c.evaluation_service.alternatives(SERVER, uuid.UUID(noodles["original"]["productId"]), "IN", store_id=STORE, cart_ref=state["cart"], placement="PDP")
    pdp_ack = _events(c, _event(c, "ADD_TO_CART", pdp["suggestionId"], sku=pdp["alternatives"][0]["product"]["partnerSku"], seconds=0))
    assert pdp_ack["charges"][0]["state"] == "PENDING"
    state["charges"] = [ack["charges"][0]["chargeId"], second["charges"][0]["chargeId"]]
    state["pdp_charge"] = pdp_ack["charges"][0]["chargeId"]


def test_b_order_in1_is_attributed_and_idempotent(c: Container, state: dict[str, Any]) -> None:
    order = _order(state, _now(c))
    body = json.dumps(order, separators=(",", ":")).encode()
    key = str(uuid.uuid4())
    calls: list[int] = []

    def create() -> IdempotentResponse:
        calls.append(1)
        return IdempotentResponse(201, jsonutil.dumps(c.order_service.create(SERVER, order)))

    created = c.idempotency_service.execute(QB, key, "POST", "/v1/orders", body, create)
    assert created.status == 201 and calls == [1]
    o = jsonutil.loads(created.body)
    assert list(o) == ["orderId", "status", "market", "currency", "placedAt", "lines", "attribution"]
    assert (o["orderId"], o["status"], o["currency"]) == (state["order"], "PLACED", "INR")
    assert o["lines"][0]["allocatedOrderDiscount"] == _inr("8.00") and o["lines"][0]["suggestionId"] == state["sid"]
    attribution = o["attribution"]
    assert [x["lineId"] for x in attribution["lines"]] == ["1"]  # line 2 has no suggestion
    line1 = _line(attribution, "1")
    assert (line1["state"], line1["fee"], line1["fairPremium"]) == ("PENDING", _inr("5.80"), _inr("58.00"))
    assert (line1["originalPartnerSku"], line1["alternativePartnerSku"], line1["replacedQuantity"]) == (NOODLES, MILLET_NOODLES, D(2))
    assert (line1["originalUnitPriceSnapshot"], line1["alternativeNetUnitPrice"], line1["packDelta"]) == (_inr("56.00"), _inr("85.00"), _inr("29.00"))
    assert (line1["feeRate"], line1["ruleVersion"]) == (D("0.1"), "FP-1") and "rejectionReason" not in line1 and "finalizesAt" not in line1
    assert attribution["totalFee"] == _inr("5.80") and attribution["totalFairPremium"] == _inr("58.00")

    # Same key and body: the stored response is replayed without running the handler again.
    replayed = c.idempotency_service.execute(QB, key, "POST", "/v1/orders", body, lambda: pytest.fail("handler ran on a replay"))
    assert (replayed.status, replayed.body) == (201, created.body)
    # Same key, different body: conflict.
    changed = json.dumps({**order, "userRef": "someone-else"}, separators=(",", ":")).encode()
    with pytest.raises(HsError) as ei:
        c.idempotency_service.execute(QB, key, "POST", "/v1/orders", changed, lambda: pytest.fail("handler ran on a conflict"))
    assert (ei.value.status, ei.value.code) == (409, "HS-IDEMPOTENCY-CONFLICT")
    # The same orderId reported again (no or a new Idempotency-Key): HS-ORDER-EXISTS.
    with pytest.raises(HsError) as ei:
        c.order_service.create(SERVER, order)
    assert (ei.value.status, ei.value.code) == (409, "HS-ORDER-EXISTS")
    with pytest.raises(HsError) as ei:
        c.idempotency_service.execute(QB, str(uuid.uuid4()), "POST", "/v1/orders", body, create)
    assert (ei.value.status, ei.value.code) == (409, "HS-ORDER-EXISTS")

    assert jsonutil.loads(jsonutil.dumps(c.order_service.get(QB, state["order"]))) == o
    assert c.order_service.attribution_summary(QB, state["order"]) == c.order_service.get(QB, state["order"])["attribution"]
    with pytest.raises(HsError) as ei:
        c.order_service.get(HOMETOWN, state["order"])
    assert ei.value.status == 404


def test_c_delivery_accrues_and_a_return_reverses_half_the_fee(c: Container, state: dict[str, Any]) -> None:
    delivered_at = _now(c)
    with pytest.raises(HsError) as ei:
        c.order_service.update_status(QB, state["order"], {"status": "DELIVERED", "occurredAt": delivered_at, "lineStatuses": [{"lineId": "9"}]})
    assert ei.value.status == 400
    d = c.order_service.update_status(QB, state["order"], {"status": "DELIVERED", "occurredAt": delivered_at})
    line1 = _line(d["attribution"], "1")
    assert d["status"] == "DELIVERED" and d["deliveredAt"] == iso(parse_iso(delivered_at))
    assert (line1["state"], line1["fee"]) == ("ACCRUED", _inr("5.80"))
    assert line1["finalizesAt"] == iso(parse_iso(delivered_at) + timedelta(days=2))  # QUICK_COMMERCE return window
    with pytest.raises(HsError) as ei:
        c.order_service.update_status(QB, state["order"], {"status": "CANCELLED", "occurredAt": _now(c)})
    assert (ei.value.status, ei.value.code) == (409, "HS-ORDER-STATE")

    adjustment = {"adjustments": [{"adjustmentId": _ref("ret-"), "lineId": "1", "type": "RETURN", "quantity": 1, "occurredAt": _now(c)}]}
    summary = c.order_service.adjust(QB, state["order"], adjustment)
    assert (_line(summary, "1")["fee"], _line(summary, "1")["state"], summary["totalFee"]) == (_inr("2.90"), "ACCRUED", _inr("2.90"))
    assert c.order_service.adjust(QB, state["order"], adjustment) == summary  # a replayed adjustmentId is skipped
    with pytest.raises(HsError) as ei:
        c.order_service.adjust(QB, state["order"], {"adjustments": [{"adjustmentId": _ref("x-"), "lineId": "1", "type": "RETURN", "quantity": 5,
                                                                      "occurredAt": _now(c)}]})
    assert ei.value.status == 400

    with c.db.tx(DbRole.OPS) as u:
        kinds = [r["entry_kind"] for r in u.query(
            "select entry_kind from billing.journal_entry where partner_id = @p and reference_type = 'ATTRIBUTION'", {"p": QB})]
    assert sorted(kinds) == ["FEE_ACCRUAL", "FEE_REVERSAL"]


def test_d_time_travel_finalizes_charges_and_attributions(c: Container, state: dict[str, Any]) -> None:
    c.offset_clock.offset = timedelta(days=3)  # reset after the dispute (test_f) and by the fixture
    assert c.evaluation_recorder.flush(10)
    finalized = cli.COMMANDS["jobs finalize"](c, argparse.Namespace())
    assert finalized == {"recoCharges": {"final": 2, "rejected": 1}, "attributionsFinal": 1}
    assert cli.COMMANDS["jobs finalize"](c, argparse.Namespace()) == {"recoCharges": {"final": 0, "rejected": 0}, "attributionsFinal": 0}

    charges = {x["chargeId"]: x for x in c.reco_charge_service.list(QB, cart_ref=state["cart"], limit=50)["items"]}
    assert [charges[x]["state"] for x in state["charges"]] == ["FINAL", "FINAL"]
    assert (charges[state["pdp_charge"]]["state"], charges[state["pdp_charge"]]["rejectionReason"]) == ("REJECTED", "NO_IMPRESSION")
    line1 = _line(c.order_service.attribution_summary(QB, state["order"]), "1")
    assert (line1["state"], line1["fee"]) == ("FINAL", _inr("2.90"))
    with c.db.tx(DbRole.APP, QB) as u:
        assert u.scalar("select user_ref from txn.order_header where order_id = @o", {"o": state["order"]}) is None  # FINAL drops the userRef


def test_e_month_close_issues_the_statement(c: Container, state: dict[str, Any]) -> None:
    period = period_of(c.clock.utc_now())
    results = cli.COMMANDS["billing month-close"](c, argparse.Namespace(period=period, partner=QB))
    assert len(results) == 1
    r = results[0]
    assert (r.partner_id, r.period, r.outcome) == (QB, period, "ISSUED")
    assert re.fullmatch(rf"HS-IN-{period}-\d{{6}}", r.invoice_number or ""), r.invoice_number
    assert (r.premium_fee, r.reco_fee, r.reco_charge_count, r.reversals, r.tax, r.total_due) == (D("5.80"), D("2.00"), 2, D("2.90"), D("0.88"), D("5.78"))

    page = c.billing_service.list(QB, period=period)
    assert "nextCursor" not in page and len(page["items"]) == 1
    st = page["items"][0]
    assert (st["statementId"], st["period"], st["status"], st["invoiceNumber"], st["lineCount"]) == (str(r.statement_id), period, "ISSUED", r.invoice_number, 2)
    assert st["totals"] == {
        "premiumFee": _inr("5.80"), "recommendationFee": _inr("2.00"), "recommendationChargeCount": 2, "grossFee": _inr("7.80"), "reversals": _inr("2.90"),
        "credits": _inr("0.00"), "netFee": _inr("4.90"), "tax": _inr("0.88"), "totalDue": _inr("5.78"),
    }
    assert parse_iso(st["dueAt"]) - parse_iso(st["issuedAt"]) == timedelta(days=30)
    assert c.billing_service.get(QB, r.statement_id) == st
    assert c.billing_service.list(QB, status="DISPUTED")["items"] == [] and c.billing_service.list(QB)["items"] == [st]
    lines = c.billing_service.lines(QB, r.statement_id)["items"]
    assert [(x["lineNo"], x["type"], x["orderId"], x["orderLineId"], x["suggestionId"], x["amount"]) for x in lines] == [
        (1, "FEE", state["order"], "1", state["sid"], _inr("5.80")), (2, "REVERSAL", state["order"], "1", state["sid"], _inr("-2.90")),
    ]
    assert c.billing_service.lines(QB, r.statement_id, limit=1)["nextCursor"]
    billed = c.reco_charge_service.list(QB, statement_id=r.statement_id)["items"]
    assert len(billed) == st["totals"]["recommendationChargeCount"] and {x["chargeId"] for x in billed} == set(state["charges"])

    again = c.billing_service.close_month(period, QB)
    assert [(x.outcome, x.statement_id, x.invoice_number, x.total_due) for x in again] == [("ALREADY_CLOSED", r.statement_id, r.invoice_number, D("5.78"))]
    everyone = {x.partner_id: x.outcome for x in c.billing_service.close_month(period)}
    assert everyone[QB] == "ALREADY_CLOSED" and everyone[HOMETOWN] == "NOTHING_TO_BILL"
    with pytest.raises(HsError):
        c.billing_service.close_month("2026-13", QB)
    with pytest.raises(HsError) as ei:
        c.billing_service.get(HOMETOWN, r.statement_id)
    assert ei.value.status == 404
    state["statement"] = r.statement_id


def test_f_dispute_upheld_credits_the_charge_and_the_ledger_balances(c: Container, state: dict[str, Any]) -> None:
    statement_id = state["statement"]
    charge = state["charges"][0]
    try:
        for bad, status, code in [
            ({"reason": "NOT_ADDED_TO_CART"}, 400, "HS-REQ-INVALID"),
            ({"reason": "NOT_ADDED_TO_CART", "recommendationChargeIds": [str(uuid.uuid4())]}, 422, "HS-DISPUTE-CHARGE-UNKNOWN"),
            ({"reason": "NOT_ADDED_TO_CART", "lines": [{"orderId": state["order"], "orderLineId": "2"}]}, 422, "HS-DISPUTE-LINE-UNKNOWN"),
        ]:
            with pytest.raises(HsError) as ei:
                c.billing_service.open_dispute(QB, {"statementId": str(statement_id), **bad})
            assert (ei.value.status, ei.value.code) == (status, code)

        dispute = c.billing_service.open_dispute(QB, {
            "statementId": str(statement_id), "reason": "NOT_ADDED_TO_CART", "recommendationChargeIds": [charge], "comment": "Shopper did not add the item",
        })
        assert (dispute["status"], dispute["recommendationChargeIds"], dispute["lines"]) == ("OPEN", [charge], [])
        assert "resolvedAt" not in dispute and "creditNoteAmount" not in dispute
        assert c.billing_service.get(QB, statement_id)["status"] == "DISPUTED"
        dispute_id = uuid.UUID(dispute["disputeId"])
        assert c.billing_service.get_dispute(QB, dispute_id) == dispute

        resolved = c.billing_service.resolve_dispute(dispute_id, {"decision": "UPHELD", "resolution": "Client logs show a double tap"}, "test_billing_flow")
        assert (resolved["status"], resolved["creditNoteAmount"], resolved["resolution"]) == ("UPHELD", _inr("1.18"), "Client logs show a double tap")
        assert "resolvedAt" in resolved
        with pytest.raises(HsError) as ei:
            c.billing_service.resolve_dispute(dispute_id, {"decision": "REJECTED"}, "test_billing_flow")
        assert (ei.value.status, ei.value.code) == (409, "HS-DISPUTE-RESOLVED")
        assert c.reco_charge_service.get(QB, uuid.UUID(charge))["state"] == "CREDITED"

        st = c.billing_service.get(QB, statement_id)
        assert (st["status"], st["lineCount"]) == ("ISSUED", 2)
        assert (st["totals"]["credits"], st["totals"]["tax"], st["totals"]["netFee"], st["totals"]["totalDue"]) == (
            _inr("1.00"), _inr("0.70"), _inr("3.90"), _inr("4.60"))
        audit = _audit_rows(c, str(dispute_id))
        assert [(a["actor"], a["action"], a["entity_type"]) for a in audit] == [("test_billing_flow", "DISPUTE_UPHELD", "DISPUTE")]
        assert json.loads(audit[0]["details"]) == {"premiumPart": 0, "recoPart": 1, "tax": 0.18}

        assert BillingService.balanced(c.billing_service.trial_balance()) and BillingService.balanced(c.billing_service.trial_balance(QB))
        assert c.billing_service.record_payment(statement_id, D("4.60"), "pay-" + state["order"], "test_billing_flow")["status"] == "SETTLED"
        assert c.billing_service.record_payment(statement_id, D("4.60"), "pay-" + state["order"], "test_billing_flow")["status"] == "SETTLED"
        balance = c.billing_service.trial_balance(QB)
        assert BillingService.balanced(balance) and balance
    finally:
        c.offset_clock.offset = timedelta(0)


def test_g_statement_webhook_is_queued_for_the_partner_endpoint(c: Container, state: dict[str, Any]) -> None:
    _drain(c)
    with c.db.tx(DbRole.OPS) as u:
        rows = u.query(
            "select event_type, payload::text as payload from txn.webhook_delivery where partner_id = @p and event_type in ('billing.statement.issued', 'billing.dispute.updated')",
            {"p": QB},
        )
    issued = [json.loads(r["payload"]) for r in rows if r["event_type"] == "billing.statement.issued"]
    assert len(issued) == 1, rows
    assert issued[0]["data"] == {"statementId": str(state["statement"]), "period": issued[0]["data"]["period"], "totalDue": _inr("5.78")}
    assert issued[0]["type"] == "billing.statement.issued" and issued[0]["partnerId"] == str(QB) and issued[0]["id"].startswith("evt_")
    assert [json.loads(r["payload"])["data"]["status"] for r in rows if r["event_type"] == "billing.dispute.updated"] == ["UPHELD"]


def test_h_ml_data_exports_simulates_and_evaluates(c: Container, tmp_path: Path) -> None:
    counts = cli.COMMANDS["ml export"](c, argparse.Namespace(out=str(tmp_path)))
    assert list(counts) == ["substitutability_pairs", "er_pairs", "explanation_facts", "category_examples", "products"]
    assert all(v > 0 for v in counts.values()), counts
    for name, n in counts.items():
        lines = (tmp_path / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == n, name
    first = {name: json.loads((tmp_path / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()[0]) for name in counts}
    assert list(first["substitutability_pairs"]) == ["market", "from", "to", "fromName", "toName", "features", "label", "heuristic"]
    assert len(first["substitutability_pairs"]["features"]) > 0
    assert list(first["er_pairs"]) == ["a", "b", "features", "label"]
    assert list(first["explanation_facts"]) == ["market", "locale", "basis", "facts", "template"]
    assert list(first["explanation_facts"]["template"]) == ["short", "bullets"]
    assert [k for k in first["category_examples"] if k != "brand"] == ["name", "market", "category"]
    assert {"market", "productId", "name", "categoryCode", "flags", "dietClass", "physicalState", "brandTier"} <= set(first["products"])

    ranker = tmp_path / "ranker_events.jsonl"
    rows = cli.COMMANDS["ml simulate"](c, argparse.Namespace(partner=QB, sessions=5, out=str(ranker), seed=7))["rows"]
    events = [json.loads(x) for x in ranker.read_text(encoding="utf-8").splitlines()]
    assert rows == len(events) and rows > 0
    assert list(events[0]) == ["suggestionId", "rank", "placement", "features", "label", "propensity"]
    assert events[0]["label"] in (0, 1) and events[0]["propensity"] == 1

    result = cli.COMMANDS["llm eval"](c, argparse.Namespace(input=str(tmp_path / "explanation_facts.jsonl"), max=3))
    report = result["report"]
    assert result["provider"] == "mock" and report.examples == min(3, counts["explanation_facts"])
    assert 0 <= report.accepted <= report.examples and report.acceptance_rate == pytest.approx(report.accepted / report.examples, abs=1e-4)
