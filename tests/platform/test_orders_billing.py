"""Unit tests for the orders, attribution, billing and ML-data ports (Services/OrderServices.cs, Services/BillingService.cs,
Seeding/MlData.cs): request parsing, the FP-1 evidence chain and reversal maths, invoice numbers, statement totals, dispute
windows, credit notes and the synthetic ML labels. No database: only the pure static helpers are exercised here (the SQL
paths are covered by test_billing_flow.py against a fresh database)."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from healthswap.domain.commerce.fees import FairPremiumRule
from healthswap.domain.primitives import NetQuantity
from healthswap.domain.scoring.health_profiles import HealthProfileData
from healthswap.platform import jsonutil, netjson
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.hot_cache import AlternativeSnapshot, SuggestionSnapshot
from healthswap.platform.seeding.ml_data import EvalReport, MlData, _features
from healthswap.platform.services.billing import BillingService, DisputedLine, DisputeRequest, DisputeResolution, TrialBalanceRow
from healthswap.platform.services.evaluation import MoneyInput
from healthswap.platform.services.orders import (
    AttrRow,
    AttributionEngine,
    EventRow,
    LineRow,
    LineStatus,
    OrderAdjustmentRequest,
    OrderCreateRequest,
    OrderLine,
    OrderService,
    OrderStatusUpdate,
    _try_parse_currency,
)

D = Decimal
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
SID = uuid.UUID("0b8f6f52-7a4e-4f7e-9c1d-5a1b2c3d4e5f")
OTHER_SID = uuid.UUID("0b8f6f52-7a4e-4f7e-9c1d-5a1b2c3d4e60")
ALT_SKU = "SKU-MLT-256"
FROM_SKU = "SKU-NDL-280"


def _status(e: HsError) -> tuple[int, str]:
    return e.status, e.code


# ---------------------------------------------------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------------------------------------------------


def _order_request(**overrides: Any) -> dict[str, Any]:
    req: dict[str, Any] = {
        "orderId": "IN-1", "market": "IN", "currency": "INR", "placedAt": "2026-11-02T10:05:00Z", "cartRef": "cart-1", "userRef": "u-1",
        "storeId": "BLR-KOR-012", "fulfillmentType": "QUICK_COMMERCE", "orderDiscount": {"amount": "8.00", "currency": "INR"},
        "lines": [
            {"lineId": "1", "partnerSku": ALT_SKU, "quantity": 2, "unitPrice": {"amount": "89.00", "currency": "INR"},
             "allocatedOrderDiscount": {"amount": "8.00", "currency": "INR"}, "netAmount": {"amount": "170.00", "currency": "INR"}, "suggestionId": str(SID)},
            {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": {"amount": "40.00", "currency": "INR"},
             "netAmount": {"amount": "40.00", "currency": "INR"}},
        ],
    }
    req.update(overrides)
    return req


def test_order_create_request_from_api_reads_the_contract_members() -> None:
    req = OrderCreateRequest.from_api(_order_request())
    assert (req.order_id, req.market, req.currency, req.placed_at) == ("IN-1", "IN", "INR", datetime(2026, 11, 2, 10, 5, tzinfo=UTC))
    assert (req.cart_ref, req.user_ref, req.store_id, req.fulfillment_type, req.experiment_arm) == ("cart-1", "u-1", "BLR-KOR-012", "QUICK_COMMERCE", None)
    assert req.order_discount == MoneyInput("8.00", "INR")
    first, second = req.lines
    assert (first.line_id, first.partner_sku, first.quantity, first.suggestion_id) == ("1", ALT_SKU, 2, SID)
    assert first.allocated_order_discount == MoneyInput("8.00", "INR") and first.line_discount is None and first.tax_amount is None
    assert second.suggestion_id is None and second.net_amount == MoneyInput("40.00", "INR")
    assert OrderCreateRequest.from_api(req) is req


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"orderId": None}, "request.orderId is required"),
        ({"placedAt": None}, "request.placedAt is required"),
        ({"placedAt": "yesterday"}, "request.placedAt must be an RFC 3339 timestamp"),
        ({"lines": None}, "request.lines is required"),
        ({"lines": "x"}, "request.lines must be an array"),
        ({"market": 5}, "request.market must be a string"),
        ({"lines": [{"lineId": "1", "partnerSku": "A", "unitPrice": {"amount": "1", "currency": "INR"}, "netAmount": {"amount": "1", "currency": "INR"}}]},
         "lines[0].quantity is required"),
        ({"lines": [{"lineId": "1", "partnerSku": "A", "quantity": True, "unitPrice": {"amount": "1", "currency": "INR"},
                     "netAmount": {"amount": "1", "currency": "INR"}}]}, "lines[0].quantity must be a number"),
        ({"lines": [{"lineId": "1", "partnerSku": "A", "quantity": 1, "unitPrice": {"amount": "1", "currency": "INR"}}]}, "lines[0].netAmount is required"),
        ({"lines": [{"lineId": "1", "partnerSku": "A", "quantity": 1, "unitPrice": {"amount": "1", "currency": "INR"},
                     "netAmount": {"amount": "1", "currency": "INR"}, "suggestionId": "nope"}]}, "lines[0].suggestionId must be a UUID"),
    ],
)
def test_order_create_request_from_api_rejects_malformed_requests(overrides: dict[str, Any], detail: str) -> None:
    with pytest.raises(HsError) as e:
        OrderCreateRequest.from_api(_order_request(**overrides))
    assert _status(e.value) == (400, "HS-REQ-INVALID") and e.value.detail == detail


def test_status_update_and_adjustment_requests_from_api() -> None:
    update = OrderStatusUpdate.from_api({"status": "PARTIALLY_FULFILLED", "occurredAt": "2026-11-02T12:00:00+05:30",
                                         "lineStatuses": [{"lineId": "1", "deliveredQuantity": 1}, {"lineId": "2", "cancelledQuantity": 1}]})
    assert update.status == "PARTIALLY_FULFILLED" and update.occurred_at == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
    assert update.line_statuses == (LineStatus("1", D(1), None), LineStatus("2", None, D(1)))
    assert OrderStatusUpdate.from_api({"status": "DELIVERED", "occurredAt": "2026-11-02T12:00:00Z"}).line_statuses is None
    with pytest.raises(HsError, match="request.occurredAt is required"):
        OrderStatusUpdate.from_api({"status": "DELIVERED"})

    req = OrderAdjustmentRequest.from_api({"adjustments": [
        {"adjustmentId": "adj-1", "lineId": "1", "type": "RETURN", "occurredAt": "2026-11-03T09:00:00Z", "quantity": 1},
        {"adjustmentId": "adj-2", "lineId": "2", "type": "PRICE_ADJUSTMENT", "occurredAt": "2026-11-03T09:00:00Z", "amount": {"amount": "5.00", "currency": "INR"}},
    ]})
    first, second = req.adjustments
    assert (first.adjustment_id, first.type, first.quantity, first.amount) == ("adj-1", "RETURN", D(1), None)
    assert (second.quantity, second.amount, second.substituted_partner_sku) == (None, MoneyInput("5.00", "INR"), None)
    with pytest.raises(HsError, match=r"adjustments\[0\].type is required"):
        OrderAdjustmentRequest.from_api({"adjustments": [{"adjustmentId": "a", "lineId": "1", "occurredAt": "2026-11-03T09:00:00Z"}]})
    with pytest.raises(HsError, match="request.adjustments is required"):
        OrderAdjustmentRequest.from_api({})


@pytest.mark.parametrize(
    ("text", "expected"),
    [("89.00", "89.00"), ("-1.5", "-1.5"), ("+2", "2"), ("5.", "5"), (".25", "0.25"), ("79228162514264337593543950335", "79228162514264337593543950335")],
)
def test_currency_try_parse_accepts_sign_digits_and_one_point(text: str, expected: str) -> None:
    value = _try_parse_currency(text)
    assert value is not None and str(value) == expected


@pytest.mark.parametrize("text", [None, "", ".", "-", "1e3", "1,000", " 1.00", "1.00 ", "1.2.3", "\u0661\u0662", "\u00b2", "79228162514264337593543950336", "NaN"])
def test_currency_try_parse_rejects_what_decimal_try_parse_rejects(text: str | None) -> None:
    assert _try_parse_currency(text) is None


# ---------------------------------------------------------------------------------------------------------------------
# OrderService validation
# ---------------------------------------------------------------------------------------------------------------------


def test_priced_lines_parse_the_amounts() -> None:
    first, second = OrderService.priced_lines(OrderCreateRequest.from_api(_order_request()))
    assert (first.price, first.line_discount, first.order_discount, first.tax, first.net) == (D("89.00"), 0, D("8.00"), None, D("170.00"))
    assert (second.price, second.order_discount, second.net) == (D("40.00"), 0, D("40.00"))
    assert str(first.net) == "170.00"


def test_priced_lines_collects_duplicate_ids_and_foreign_currencies_in_line_order() -> None:
    req = _order_request(lines=[
        {"lineId": "1", "partnerSku": "A", "quantity": 1, "unitPrice": {"amount": "1.00", "currency": "USD"}, "netAmount": {"amount": "1.00", "currency": "INR"},
         "taxAmount": {"amount": "0.10", "currency": "EUR"}},
        {"lineId": "1", "partnerSku": "B", "quantity": 1, "unitPrice": {"amount": "x", "currency": "INR"}, "netAmount": {"amount": "1.00", "currency": "USD"},
         "lineDiscount": {"amount": "0.50", "currency": "USD"}},
    ])
    with pytest.raises(HsError) as e:
        OrderService.priced_lines(OrderCreateRequest.from_api(req))
    assert _status(e.value) == (400, "HS-REQ-INVALID") and e.value.detail == "Order validation failed"
    assert e.value.errors == [
        FieldError("/lines/0/unitPrice/currency", "Must be INR"), FieldError("/lines/0/taxAmount/currency", "Must be INR"),
        FieldError("/lines/1/lineId", "Duplicate lineId"), FieldError("/lines/1/lineDiscount/currency", "Must be INR"),
        FieldError("/lines/1/netAmount/currency", "Must be INR"),
    ]


def test_priced_lines_treats_unparseable_amounts_as_zero() -> None:
    req = _order_request(lines=[{"lineId": "1", "partnerSku": "A", "quantity": 1, "unitPrice": {"amount": "1e2", "currency": "INR"},
                                 "netAmount": {"amount": " 5.00", "currency": "INR"}}])
    (line,) = OrderService.priced_lines(OrderCreateRequest.from_api(req))
    assert (line.price, line.net) == (0, 0)


@pytest.mark.parametrize(
    ("current", "target"),
    [("PLACED", "CONFIRMED"), ("PLACED", "DELIVERED"), ("PLACED", "CANCELLED"), ("CONFIRMED", "PARTIALLY_FULFILLED"),
     ("PARTIALLY_FULFILLED", "PARTIALLY_FULFILLED"), ("PARTIALLY_FULFILLED", "DELIVERED"), ("PARTIALLY_FULFILLED", "CANCELLED")],
)
def test_allowed_order_transitions(current: str, target: str) -> None:
    OrderService.check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [("PLACED", "PLACED"), ("CONFIRMED", "CONFIRMED"), ("CONFIRMED", "PLACED"), ("DELIVERED", "CANCELLED"), ("DELIVERED", "DELIVERED"),
     ("CANCELLED", "DELIVERED"), ("PLACED", "SHIPPED")],
)
def test_disallowed_order_transitions(current: str, target: str) -> None:
    with pytest.raises(HsError) as e:
        OrderService.check_transition(current, target)
    assert _status(e.value) == (409, "HS-ORDER-STATE") and e.value.detail == f"Cannot change order status from {current} to {target}"


def _line_row(qty: str = "2.000", delivered: str | None = None, cancelled: str | None = None, net: str = "170.00", line_id: str = "1") -> LineRow:
    return LineRow(line_id, ALT_SKU, None, D(qty), D("89.00"), D("0.00"), D("8.00"), None, None, D(net), SID,
                   None if delivered is None else D(delivered), None if cancelled is None else D(cancelled))


def test_line_status_checks() -> None:
    lines = [_line_row(line_id="1"), _line_row(qty="1.000", line_id="2")]
    assert OrderService.check_line_status(lines, LineStatus("2", D(1), D(0))) is lines[1]
    assert OrderService.check_line_status(lines, LineStatus("1", D(1), D(1))) is lines[0]
    with pytest.raises(HsError, match="Unknown lineId 9"):
        OrderService.check_line_status(lines, LineStatus("9"))
    with pytest.raises(HsError) as e:
        OrderService.check_line_status(lines, LineStatus("1", D(2), D("0.5")))
    assert _status(e.value) == (400, "HS-REQ-INVALID") and e.value.detail == "Line 1: delivered + cancelled exceeds the ordered quantity"


# ---------------------------------------------------------------------------------------------------------------------
# FP-1 evidence chain
# ---------------------------------------------------------------------------------------------------------------------


def _alternative(sku: str = ALT_SKU, rank: int = 1) -> AlternativeSnapshot:
    return AlternativeSnapshot(rank, sku, uuid.UUID(int=7), 1, D("89.00"), D("256"), "g", D("12.5"), False)


def _snapshot(**overrides: Any) -> SuggestionSnapshot:
    values: dict[str, Any] = {
        "suggestion_id": SID, "cart_ref": "cart-1", "market": "IN", "from_partner_sku": FROM_SKU, "from_qty": D(2), "from_unit_price": D("56.00"),
        "from_net_quantity": D("280"), "from_net_unit": "g", "currency": "INR", "alternatives": (_alternative("SKU-OTHER", 1), _alternative(ALT_SKU, 2)),
        "rules_version": "R-1.3", "graph_version": "IN-g1", "created_at": T0, "expires_at": T0 + timedelta(minutes=30),
        "attribution_expires_at": T0 + timedelta(days=7),
    }
    values.update(overrides)
    return SuggestionSnapshot(**values)


def _order_line(sku: str = ALT_SKU, sid: uuid.UUID | None = SID, qty: str = "2", line_id: str = "1") -> OrderLine:
    return OrderLine(line_id, sku, D(qty), MoneyInput("89.00", "INR"), MoneyInput("170.00", "INR"), suggestion_id=sid)


def _event(kind: str, seconds: float, sku: str | None = None, sid: uuid.UUID = SID) -> EventRow:
    return EventRow(sid, kind, sku, T0 + timedelta(seconds=seconds))


GOOD_EVENTS = [_event("IMPRESSION", 1), _event("ACCEPT", 10, ALT_SKU)]
PLACED = T0 + timedelta(minutes=5)


def _evidence(s: SuggestionSnapshot | None, events: list[EventRow], placed_at: datetime = PLACED, line: OrderLine | None = None,
              claimed: set[uuid.UUID] | None = None) -> tuple[str, str | None, AlternativeSnapshot | None]:
    return AttributionEngine.evidence(s, line or _order_line(), events, placed_at, set() if claimed is None else claimed)


def test_evidence_pending_claims_the_suggestion() -> None:
    claimed: set[uuid.UUID] = set()
    state, reason, alt = _evidence(_snapshot(), GOOD_EVENTS, claimed=claimed)
    assert (state, reason, alt) == ("PENDING", None, _alternative(ALT_SKU, 2)) and claimed == {SID}
    # A second line of the same order pointing at the same suggestion is a DUPLICATE.
    assert _evidence(_snapshot(), GOOD_EVENTS, line=_order_line(line_id="2"), claimed=claimed)[:2] == ("REJECTED", "DUPLICATE")


def test_evidence_rejection_reasons_in_csharp_order() -> None:
    late = T0 + timedelta(days=8)
    expired_not_a_swap = _snapshot(from_unit_price=None)
    # Each case also breaks every later check: the first failing check wins.
    assert _evidence(None, [], late) == ("REJECTED", "NOT_SERVED_BY_HEALTHSWAP", None)
    assert _evidence(_snapshot(alternatives=(_alternative("SKU-OTHER"),), from_unit_price=None), [], late) == ("REJECTED", "PRODUCT_MISMATCH", None)
    assert _evidence(expired_not_a_swap, [], late) == ("REJECTED", "NO_ACCEPT", _alternative(ALT_SKU, 2))
    assert _evidence(expired_not_a_swap, [_event("ACCEPT", 10, ALT_SKU), _event("IMPRESSION", 15.001)], late)[:2] == ("REJECTED", "NO_IMPRESSION")
    assert _evidence(expired_not_a_swap, GOOD_EVENTS, late)[:2] == ("REJECTED", "EXPIRED")
    assert _evidence(expired_not_a_swap, GOOD_EVENTS)[:2] == ("REJECTED", "NOT_A_SWAP")
    assert _evidence(_snapshot(), GOOD_EVENTS, claimed={SID})[:2] == ("REJECTED", "DUPLICATE")


def test_evidence_accept_and_impression_rules() -> None:
    s = _snapshot()
    # An ACCEPT for another alternative does not count; one without an alternative SKU does.
    assert _evidence(s, [_event("IMPRESSION", 1), _event("ACCEPT", 10, "SKU-OTHER")])[:2] == ("REJECTED", "NO_ACCEPT")
    assert _evidence(s, [_event("IMPRESSION", 1), _event("ACCEPT", 10)])[0] == "PENDING"
    # Events of other suggestions are ignored.
    assert _evidence(s, [_event("IMPRESSION", 1, sid=OTHER_SID), _event("ACCEPT", 10, ALT_SKU)])[:2] == ("REJECTED", "NO_IMPRESSION")
    # The impression may trail the (last) ACCEPT by up to 5 seconds.
    assert _evidence(s, [_event("ACCEPT", 10, ALT_SKU), _event("IMPRESSION", 15)])[0] == "PENDING"
    assert _evidence(s, [_event("ACCEPT", 0, ALT_SKU), _event("IMPRESSION", 20), _event("ACCEPT", 30, ALT_SKU)])[0] == "PENDING"
    assert _evidence(s, [_event("ACCEPT", 30, ALT_SKU), _event("IMPRESSION", 40), _event("ACCEPT", 0, ALT_SKU)])[0] == "REJECTED"


def test_evidence_attribution_window() -> None:
    s = _snapshot()
    assert _evidence(s, GOOD_EVENTS, s.attribution_expires_at)[0] == "PENDING"
    assert _evidence(s, GOOD_EVENTS, s.attribution_expires_at + timedelta(microseconds=1))[:2] == ("REJECTED", "EXPIRED")
    assert _evidence(s, GOOD_EVENTS, T0 - timedelta(minutes=5))[0] == "PENDING"  # clock skew tolerance
    assert _evidence(s, GOOD_EVENTS, T0 - timedelta(minutes=5, microseconds=1))[:2] == ("REJECTED", "EXPIRED")


def test_evidence_requires_a_suggestion_id() -> None:
    with pytest.raises(ValueError):
        _evidence(_snapshot(), GOOD_EVENTS, line=_order_line(sid=None))


def test_fair_premium_of_the_in1_line_is_5_80() -> None:
    cfg: Any = SimpleNamespace(fee=SimpleNamespace(fee_basis="TAX_INCLUSIVE"))
    s, line = _snapshot(), _order_line()
    fp = FairPremiumRule.compute(AttributionEngine.fair_premium_input(s, _alternative(), line, D("170.00"), [line], "INR", cfg), D("0.10"))
    # p_alt = 170 / 2 = 85; dpack = 85 - 56 = 29 (dsize = 85 - 56 x 256/280 = 33.8 is larger); 10% of 29 x 2.
    assert (fp.replaced_quantity, fp.pack_delta, fp.fair_premium, fp.fee) == (2, D("29"), D("58"), D("5.80"))
    # Keeping one unit of the original in the same order lowers the replaced quantity.
    kept = OrderLine("2", FROM_SKU, D(1), MoneyInput("56.00", "INR"), MoneyInput("56.00", "INR"))
    fp = FairPremiumRule.compute(AttributionEngine.fair_premium_input(s, _alternative(), line, D("170.00"), [line, kept], "INR", cfg), D("0.10"))
    assert (fp.replaced_quantity, fp.fee) == (1, D("2.90"))
    with pytest.raises(ValueError):
        AttributionEngine.fair_premium_input(_snapshot(from_unit_price=None), _alternative(), line, D("170.00"), [line], "INR", cfg)


# ---------------------------------------------------------------------------------------------------------------------
# Accrual, returns, price adjustments
# ---------------------------------------------------------------------------------------------------------------------


def _attr_row(**overrides: Any) -> AttrRow:
    values: dict[str, Any] = {
        "attribution_id": uuid.UUID(int=1), "order_id": "IN-1", "line_id": "1", "suggestion_id": SID, "state": "PENDING", "rejection_reason": None,
        "currency": "INR", "replaced_qty": D("2.000"), "orig_unit_price_snapshot": D("56.0000"), "alt_net_unit_price": D("85.0000"),
        "pack_delta": D("29.0000"), "size_normalized_delta": None, "fair_premium": D("58.00"), "fee_rate": D("0.1000"), "fee_amount": D("5.80"),
        "reversed_amount": D("0.00"), "rule_version": "FP-1",
    }
    values.update(overrides)
    return AttrRow(**values)


def test_accrual_on_delivery() -> None:
    a = _attr_row()
    assert AttributionEngine.accrual(a, _line_row(), True) == ("ACCRUED", D("5.80"), D("2.000"))
    assert AttributionEngine.accrual(a, _line_row(), False) is None  # partial fulfilment, no delivered quantity for the line
    assert AttributionEngine.accrual(a, _line_row(delivered="1"), False) == ("ACCRUED", D("2.90"), D(1))
    assert AttributionEngine.accrual(a, _line_row(cancelled="1"), True) == ("ACCRUED", D("2.90"), D(1))
    assert AttributionEngine.accrual(a, _line_row(delivered="0"), False) == ("VOID", 0, 0)
    assert AttributionEngine.accrual(a, _line_row(cancelled="2"), True) == ("VOID", 0, 0)
    assert AttributionEngine.accrual(a, _line_row(qty="3.000", delivered="3"), True) == ("ACCRUED", D("5.80"), D("2.000"))  # capped at replaced
    assert AttributionEngine.accrual(_attr_row(replaced_qty=D(3)), _line_row(qty="3.000", delivered="1"), True)[1] == D("1.93")
    assert str(AttributionEngine.accrual(_attr_row(fee_amount=D("0.05")), _line_row(delivered="1"), True)[1]) == "0.02"  # half-even


def test_return_quantity() -> None:
    assert AttributionEngine.return_quantity(_line_row(), D(1), D("85.00")) == 1  # the quantity wins
    assert AttributionEngine.return_quantity(_line_row(), None, D("85.00")) == 1
    assert str(AttributionEngine.return_quantity(_line_row(qty="1.000", net="30.00"), None, D("10.00"))) == "0.333"
    assert AttributionEngine.return_quantity(_line_row(qty="3.000", net="120.00"), None, D("50.00")) == D("1.25")
    assert AttributionEngine.return_quantity(_line_row(), None, None) == D("2.000")
    assert AttributionEngine.return_quantity(_line_row(net="0.00"), None, D("5.00")) == D("2.000")


def test_pending_return_reduces_fee_and_quantity() -> None:
    assert AttributionEngine.pending_return(_attr_row(), D(1)) == (D("2.90"), D(1))
    assert AttributionEngine.pending_return(_attr_row(), D(5)) == (0, 0)
    assert AttributionEngine.pending_return(_attr_row(replaced_qty=D(0), fee_amount=D(0)), D(1)) == (0, 0)


def test_reversal_is_proportional_and_never_exceeds_the_remaining_fee() -> None:
    assert FairPremiumRule.reversal(D("5.80"), D("0.00"), D(2), D(1), "INR") == D("2.90")
    assert FairPremiumRule.reversal(D("5.80"), D("2.90"), D(2), D(2), "INR") == D("2.90")
    assert FairPremiumRule.reversal(D("5.80"), D("0.00"), D(2), D(5), "INR") == D("5.80")
    assert FairPremiumRule.reversal(D("5.80"), D("0.00"), D(0), D(1), "INR") == 0
    assert FairPremiumRule.reversal(D("1.00"), D("0.00"), D(3), D(1), "INR") == D("0.33")


def test_price_adjustment_recomputes_a_lower_fee() -> None:
    a = _attr_row()
    # The shopper got 20.00 back on 2 units: 75 a unit, premium 75 - 56 = 19, fee 10% x 19 x 2.
    assert AttributionEngine.price_adjustment(a, D(2), D("20.00"), D("0.10")) == (D(75), D("3.80"))
    assert AttributionEngine.price_adjustment(a, D(2), D("200.00"), D("0.10"))[1] == 0  # no premium left
    assert AttributionEngine.price_adjustment(a, D(2), D("1.00"), D("0.50"))[1] == D("5.80")  # never above the current fee
    # A size-normalised delta caps the premium after the credit: 20 - (85 - 75) = 10 a unit.
    assert AttributionEngine.price_adjustment(_attr_row(size_normalized_delta=D(20)), D(2), D("20.00"), D("0.10")) == (D(75), D("2.00"))
    assert AttributionEngine.price_adjustment(_attr_row(replaced_qty=D(0)), D(2), D("20.00"), D("0.10")) == (D("85.0000"), 0)


def test_summary_totals_count_billable_states_only() -> None:
    rows = [
        _attr_row(line_id="1", state="ACCRUED", reversed_amount=D("2.90"), finalizes_at=T0 + timedelta(days=3), original_partner_sku=FROM_SKU,
                  alternative_partner_sku=ALT_SKU),
        _attr_row(line_id="2", state="REJECTED", rejection_reason="NO_IMPRESSION", fee_amount=D("0.00"), fair_premium=D("0.00")),
        _attr_row(line_id="3", state="VOID", rejection_reason="NOT_DELIVERED"),
        _attr_row(line_id="4", state="CREDITED", fee_amount=D("1.00"), fair_premium=D("10.00"), size_normalized_delta=D("3.5")),
        _attr_row(line_id="5", state="REVERSED", reversed_amount=D("5.80")),
    ]
    s = AttributionEngine.summary_of("IN-1", "INR", rows)
    assert s["totalFairPremium"] == {"amount": "68.00", "currency": "INR"} and s["totalFee"] == {"amount": "3.90", "currency": "INR"}
    first = s["lines"][0]
    assert first == {
        "lineId": "1", "suggestionId": str(SID), "originalPartnerSku": FROM_SKU, "alternativePartnerSku": ALT_SKU, "replacedQuantity": D("2.000"),
        "originalUnitPriceSnapshot": {"amount": "56.00", "currency": "INR"}, "alternativeNetUnitPrice": {"amount": "85.00", "currency": "INR"},
        "packDelta": {"amount": "29.00", "currency": "INR"}, "fairPremium": {"amount": "58.00", "currency": "INR"}, "feeRate": D("0.1000"),
        "fee": {"amount": "2.90", "currency": "INR"}, "state": "ACCRUED", "finalizesAt": "2026-11-05T10:00:00.000000Z", "ruleVersion": "FP-1",
    }
    assert s["lines"][1]["rejectionReason"] == "NO_IMPRESSION" and "sizeNormalizedDelta" not in first
    assert s["lines"][3]["sizeNormalizedDelta"] == {"amount": "3.50", "currency": "INR"}
    assert [x["lineId"] for x in s["lines"]] == ["1", "2", "3", "4", "5"]


# ---------------------------------------------------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------------------------------------------------


def test_invoice_number_format() -> None:
    assert BillingService.invoice_number("IN", date(2026, 11, 1), 42) == "HS-IN-2026-11-000042"
    assert BillingService.invoice_number("US", date(2027, 1, 1), 1) == "HS-US-2027-01-000001"
    assert BillingService.invoice_number("IN", date(2026, 3, 1), 1234567) == "HS-IN-2026-03-1234567"


def _billed(fee: str, reversed_: str = "0.00", order_id: str = "IN-1", line_id: str = "1") -> dict[str, Any]:
    return {"attribution_id": uuid.uuid4(), "order_id": order_id, "line_id": line_id, "suggestion_id": SID, "fair_premium": D("58.00"),
            "fee_amount": D(fee), "reversed_amount": D(reversed_), "delivered_at": T0}


def _charge(amount: str = "1.00") -> dict[str, Any]:
    return {"charge_id": uuid.uuid4(), "amount": D(amount), "credited_amount": D("0.00")}


def test_statement_lines_put_each_reversal_after_its_fee() -> None:
    a1, a2, a3 = _billed("5.80", "2.90"), _billed("3.00", line_id="2"), _billed("1.50", "1.50", order_id="IN-2")
    lines = BillingService.statement_lines([a1, a2, a3])
    assert [(no, kind, a["attribution_id"], amount) for no, kind, a, amount in lines] == [
        (1, "FEE", a1["attribution_id"], D("5.80")), (2, "REVERSAL", a1["attribution_id"], D("-2.90")), (3, "FEE", a2["attribution_id"], D("3.00")),
        (4, "FEE", a3["attribution_id"], D("1.50")), (5, "REVERSAL", a3["attribution_id"], D("-1.50")),
    ]
    assert BillingService.statement_lines([]) == []


def test_statement_totals_of_the_in1_month() -> None:
    t = BillingService.statement_totals("INR", [_billed("5.80", "2.90")], [_charge(), _charge()], D("0.18"))
    assert (t.premium_fee, t.reco_fee, t.reco_charge_count, t.reversals, t.credits) == (D("5.80"), D("2.00"), 2, D("2.90"), 0)
    assert (t.gross_fee, t.net_fee, t.tax, t.total_due) == (D("7.80"), D("4.90"), D("0.88"), D("5.78"))
    assert str(t.tax) == "0.88" and str(t.total_due) == "5.78"
    only_charges = BillingService.statement_totals("USD", [], [_charge("0.25")], D(0))
    assert (only_charges.premium_fee, only_charges.gross_fee, only_charges.tax, only_charges.total_due) == (0, D("0.25"), 0, D("0.25"))


def test_dispute_window() -> None:
    issued = datetime(2026, 12, 1, 0, 5, tzinfo=UTC)
    assert not BillingService.dispute_window_closed(issued, issued + timedelta(days=30), 30)
    assert BillingService.dispute_window_closed(issued, issued + timedelta(days=30, microseconds=1), 30)
    assert not BillingService.dispute_window_closed(issued, issued, 0)
    assert not BillingService.dispute_window_closed(None, issued + timedelta(days=999), 30)


def test_credit_note_amounts() -> None:
    assert BillingService.credit_note_tax(D(0), D("1.00"), D("0.18"), "INR") == D("0.18")
    assert str(BillingService.credit_note_tax(D("2.90"), D("1.00"), D("0.18"), "INR")) == "0.70"
    assert BillingService.credit_note_tax(D("0.25"), D(0), D("0.18"), "INR") == D("0.04")  # 0.045 -> 0.04 (half-even)
    assert BillingService.credit_note_tax(D(100), D(0), D("0.10"), "JPY") == D(10)
    st = {"credits": D("0.00"), "tax_amount": D("0.88"), "gross_fee": D("7.80"), "reversals": D("2.90")}
    assert BillingService.after_credit_note(st, D(0), D("1.00"), D("0.18")) == (D("1.00"), D("0.70"), D("3.90"), D("4.60"))


def test_trial_balance_check_is_per_currency() -> None:
    rows = [
        TrialBalanceRow("1100-AR", "INR", D("5.78"), D(0), D("5.78")), TrialBalanceRow("4000-REV", "INR", D(0), D("5.78"), D("-5.78")),
        TrialBalanceRow("1100-AR", "USD", D("1.00"), D(0), D("1.00")), TrialBalanceRow("4000-REV", "EUR", D(0), D("1.00"), D("-1.00")),
    ]
    assert BillingService.balanced(rows[:2]) and BillingService.balanced([])
    assert not BillingService.balanced(rows)


def test_dispute_requests_from_api() -> None:
    charge = uuid.uuid4()
    req = DisputeRequest.from_api({"statementId": str(SID), "reason": "NOT_ADDED_TO_CART", "recommendationChargeIds": [str(charge)],
                                   "lines": [{"orderId": "IN-1", "orderLineId": "1"}], "comment": "c"})
    assert req == DisputeRequest(SID, "NOT_ADDED_TO_CART", (DisputedLine("IN-1", "1"),), (charge,), "c")
    assert DisputeRequest.from_api({"statementId": str(SID), "reason": "X"}).lines is None
    for bad, detail in [({"reason": "X"}, "request.statementId is required"), ({"statementId": "nope", "reason": "X"}, "request.statementId must be a UUID"),
                        ({"statementId": str(SID)}, "request.reason is required"),
                        ({"statementId": str(SID), "reason": "X", "recommendationChargeIds": ["x"]}, "recommendationChargeIds[0] must be a UUID"),
                        ({"statementId": str(SID), "reason": "X", "lines": [{"orderId": "IN-1"}]}, "lines[0].orderLineId is required")]:
        with pytest.raises(HsError) as e:
            DisputeRequest.from_api(bad)
        assert _status(e.value) == (400, "HS-REQ-INVALID") and e.value.detail == detail
    res = DisputeResolution.from_api({"decision": "PARTIALLY_UPHELD", "upheldChargeIds": [str(charge)], "resolution": "ok"})
    assert res == DisputeResolution("PARTIALLY_UPHELD", None, (charge,), "ok")
    with pytest.raises(HsError, match="request.decision is required"):
        DisputeResolution.from_api({})


def test_statement_and_dispute_mapping() -> None:
    row = {
        "statement_id": SID, "partner_id": uuid.UUID(int=2), "period_start": date(2026, 11, 1), "currency": "INR", "status": "ISSUED",
        "premium_fee": D("5.80"), "reco_fee": D("2.00"), "reco_charge_count": 2, "gross_fee": D("7.80"), "reversals": D("2.90"), "credits": D("0.00"),
        "net_fee": D("4.90"), "tax_rate": D("0.1800"), "tax_amount": D("0.88"), "total_due": D("5.78"), "paid_amount": D("0.00"), "line_count": 2,
        "invoice_number": "HS-IN-2026-11-000001", "issued_at": T0, "due_at": None,
    }
    st = BillingService.to_api(row)
    assert list(st) == ["statementId", "period", "currency", "status", "totals", "lineCount", "invoiceNumber", "issuedAt"]
    assert (st["period"], st["totals"]["totalDue"], st["totals"]["recommendationChargeCount"]) == ("2026-11", {"amount": "5.78", "currency": "INR"}, 2)
    assert list(st["totals"]) == ["premiumFee", "recommendationFee", "recommendationChargeCount", "grossFee", "reversals", "credits", "netFee", "tax", "totalDue"]

    d = {
        "dispute_id": OTHER_SID, "partner_id": uuid.UUID(int=2), "statement_id": SID, "reason": "NOT_ADDED_TO_CART",
        "disputed_lines": '[{"orderId": "IN-1", "orderLineId": "1"}]', "disputed_charge_ids": [], "comment": None, "status": "UPHELD",
        "credit_note_amount": D("1.18"), "credit_note_tax": D("0.18"), "opened_at": T0, "resolved_at": T0 + timedelta(hours=1), "resolution": None,
    }
    api = BillingService.dispute_to_api(d, "INR")
    assert api == {
        "disputeId": str(OTHER_SID), "statementId": str(SID), "status": "UPHELD", "reason": "NOT_ADDED_TO_CART", "lines": [{"orderId": "IN-1", "orderLineId": "1"}],
        "creditNoteAmount": {"amount": "1.18", "currency": "INR"}, "openedAt": "2026-11-02T10:00:00.000000Z", "resolvedAt": "2026-11-02T11:00:00.000000Z",
    }
    assert BillingService.dispute_to_api({**d, "disputed_lines": "[]", "disputed_charge_ids": [SID]}, "INR")["recommendationChargeIds"] == [str(SID)]
    assert BillingService._disputed_lines(None) == [] and BillingService._disputed_lines("[]") == []


# ---------------------------------------------------------------------------------------------------------------------
# ML data
# ---------------------------------------------------------------------------------------------------------------------


def _profile(**overrides: Any) -> HealthProfileData:
    values: dict[str, Any] = {
        "product_id": uuid.uuid4(), "market": "IN", "name": "Atta Noodles 280 g", "category_code": "noodles.instant", "profile_version": "R-1.3",
        "physical_state": "SOLID", "diet_class": "VEG", "net_quantity": NetQuantity(D(280), "g"), "form": "NOODLE_BLOCK", "use_occasion": "MEAL",
    }
    values.update(overrides)
    return HealthProfileData(**values)


def test_gold_substitutable_label() -> None:
    a = _profile()
    assert MlData.gold_substitutable(a, _profile(), 0)
    assert not MlData.gold_substitutable(a, _profile(), 3)
    assert not MlData.gold_substitutable(a, _profile(physical_state="LIQUID"), 1)
    assert not MlData.gold_substitutable(a, _profile(diet_class="NON_VEG"), 1)
    assert MlData.gold_substitutable(a, _profile(diet_class="VEGAN"), 1)
    assert MlData.gold_substitutable(_profile(diet_class="NON_VEG"), _profile(diet_class="UNKNOWN"), 1)
    assert MlData.gold_substitutable(a, _profile(net_quantity=NetQuantity(D(560), "g")), 1)  # exactly 2x
    assert not MlData.gold_substitutable(a, _profile(net_quantity=NetQuantity(D(561), "g")), 1)
    assert not MlData.gold_substitutable(a, _profile(net_quantity=NetQuantity(D(139), "g")), 1)
    assert MlData.gold_substitutable(a, _profile(net_quantity=NetQuantity(D(1000), "ml")), 1)  # not comparable: no size check
    assert MlData.gold_substitutable(a, _profile(net_quantity=None), 1)
    assert not MlData.gold_substitutable(a, _profile(use_occasion="SNACK"), 1)
    assert MlData.gold_substitutable(a, _profile(use_occasion=None), 1)
    assert not MlData.gold_substitutable(a, _profile(form="CUP"), 1)
    assert MlData.gold_substitutable(a, _profile(form="CUP"), 2)  # an approved cross-category map ignores the form
    assert MlData.gold_substitutable(a, _profile(form=None), 1)


class _FixedCase(random.Random):
    def __init__(self, case: int) -> None:
        super().__init__(0)
        self.case = case

    def randrange(self, *args: Any, **kwargs: Any) -> int:
        assert args == (5,)
        return self.case


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (0, "WHOLE WHEAT ATTA PACK 500 G"),
        (1, "Whole Wheat Atta Pk 500 g"),  # words never contain the space of "Whole Wheat" (as in C#)
        (2, "Whole Atta Wheat Pack 500 g"),
        (3, "Whole Wheat Atta Pack g 500"),
        (4, "Whole Wheat Atta Pack 500g (Pack of 1)"),
    ],
)
def test_noisy_titles(case: int, expected: str) -> None:
    assert MlData.noisy("Whole Wheat Atta Pack 500 g", _FixedCase(case)) == expected


def test_noisy_edge_cases() -> None:
    assert MlData.noisy("Cola  750 ml", _FixedCase(2)) == "Cola 750 ml"  # empty entries removed; no swap with three words
    assert MlData.noisy("Cola 750 ml", _FixedCase(3)) == "Cola ml 750"
    assert MlData.noisy("Cola 750 ml", _FixedCase(4)) == "Cola 750ml (Pack of 1)"
    assert MlData.noisy("Chips", _FixedCase(3)) == "Chips "
    seeded = [MlData.noisy("Whole Wheat Atta Pack 500 g", random.Random(42)) for _ in range(3)]
    assert seeded == [MlData.noisy("Whole Wheat Atta Pack 500 g", random.Random(42)) for _ in range(3)]


def test_acceptance_logit() -> None:
    f = [0.0] * 16
    f[0], f[1], f[2], f[12], f[15] = 10.0, 0.5, 1.5, 1.0, 1.0
    assert MlData.acceptance_logit(f, 2, "CHECKOUT") == pytest.approx(-1.2 + 1.1 + 0.35 - 0.8 + 0.4 - 0.55 - 0.2 + 0.3)
    assert MlData.acceptance_logit(f, 1, "CART") == pytest.approx(-1.2 + 1.1 + 0.35 - 0.8 + 0.4 + 0.3)
    f[2] = 0.5  # a cheaper alternative carries no price penalty
    assert MlData.acceptance_logit(f, 1, "CART") == pytest.approx(-1.2 + 1.1 + 0.35 + 0.4 + 0.3)


def test_distinct_by_product_keeps_first_occurrences_in_order() -> None:
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    a, b, c, d = _profile(product_id=p1, market="IN"), _profile(product_id=p2), _profile(product_id=p1, market="US"), _profile(product_id=p2, market="US")
    assert MlData._distinct_by_product([a, b, c, d]) == [a, b]
    assert MlData._distinct_by_product([d, a, b]) == [d, a]


def test_jsonl_lines_write_float32_features_like_system_text_json() -> None:
    features = _features([0.1, 1 / 3, 123456789.0, 1e9, 1e-5, 0.0001, -0.0, 0.5])
    line = netjson.serialize_hs({"suggestionId": str(SID), "rank": 1, "placement": "CART", "features": features, "label": 0, "propensity": 1.0,
                                 "heuristic": D("0.8125"), "brand": None})
    assert line == (f'{{"suggestionId":"{SID}","rank":1,"placement":"CART","features":[0.1,0.33333334,123456790,1E+09,1E-05,0.0001,-0,0.5],'
                    '"label":0,"propensity":1,"heuristic":0.8125}')


@pytest.mark.parametrize(
    ("value", "text"),
    [(0.1, "0.1"), (1 / 3, "0.33333334"), (16777216.0, "16777216"), (123456789.0, "123456790"), (1e9, "1E+09"), (0.0001, "0.0001"),
     (1e-5, "1E-05"), (-2.5, "-2.5"), (0.0, "0"), (-0.0, "-0"), (3.4028234663852886e38, "3.4028235E+38"), (1.401298464324817e-45, "1E-45")],
)
def test_single_text(value: float, text: str) -> None:
    assert netjson.single_text(value) == text


def test_single_text_rejects_non_finite_values() -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            netjson.single_text(value)


def test_eval_report_prints_camel_case() -> None:
    report = EvalReport(2, 1, 0.5, {"numeric_fidelity": 1}, 12.5, 100, 40, "mock-t5")
    assert jsonutil.loads(jsonutil.dumps(report)) == {
        "examples": 2, "accepted": 1, "acceptanceRate": D("0.5"), "failedChecks": {"numeric_fidelity": 1}, "meanLatencyMs": D("12.5"), "tokensIn": 100,
        "tokensOut": 40, "modelRef": "mock-t5",
    }
