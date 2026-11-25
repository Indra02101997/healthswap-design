"""Services/OrderServices.cs ported: orders, lifecycle updates and adjustments (OrderService, HLD 9.4-9.5) and the FP-1
attribution engine (AttributionEngine, HLD 9.2-9.5): evidence chain, fair premium with caps, PENDING at order, ACCRUED at
delivery (FEE_ACCRUAL), cancellations, proportional reversals for returns and FINAL after the return window.

Requests are the camelCase dicts of api/openapi.yaml (OrderCreateRequest, OrderStatusUpdate, OrderAdjustmentRequest) or the
parsed dataclasses below; results are Order and AttributionSummary dicts in the C# property order: ids and timestamps are
strings, amounts Money dicts, quantities and rates Decimals with their column scale, None members omitted.

Port notes:
    * SQL, role switches (hs_app for the order, hs_ledger_writer for the postings), rejection reasons and their order,
      caps, ledger postings, outbox payloads and error messages are those of the C#. C# decimal multiplication, division
      and Math.Round go through healthswap.domain.knowledge.products (decimal_mul / decimal_div / round_decimal).
    * The decisions are factored into pure static helpers (AttributionEngine.evidence, accrual, return_quantity,
      pending_return, price_adjustment; OrderService.check_transition, priced_lines) that are unit-tested without a
      database; the SQL methods only load rows, call them and write the outcome.
    * from_api answers 400 for the members the contract requires (placedAt, occurredAt, quantity, ...) where the C# records
      fell back to default values (DateTime.MinValue, 0) after the contract validation.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from healthswap.domain.commerce.fees import FairPremiumInput, FairPremiumResult, FairPremiumRule
from healthswap.domain.commerce.ledger import Postings
from healthswap.domain.commerce.partner_config import PartnerConfig
from healthswap.domain.knowledge.products import decimal_div, decimal_max, decimal_min, decimal_mul, round_decimal
from healthswap.domain.primitives import UTC, Clock, NetQuantity, as_utc, iso, minor_units, new_id, parse_iso, to_decimal, try_parse_amount
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.hot_cache import AlternativeSnapshot, HotCache, SuggestionSnapshot
from healthswap.platform.services.evaluation import MoneyInput
from healthswap.platform.services.events import SuggestionStore
from healthswap.platform.services.infrastructure import LedgerService
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory

__all__ = [
    "AttrRow", "AttributionEngine", "EventRow", "HeaderRow", "LineRow", "LineStatus", "OrderAdjustment", "OrderAdjustmentRequest", "OrderCreateRequest",
    "OrderLine", "OrderService", "OrderStatusUpdate", "PricedLine",
]

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE = Decimal(1)
_DECIMAL_MAX = Decimal("79228162514264337593543950335")  # decimal.MaxValue
_IMPRESSION_TOLERANCE = timedelta(seconds=5)
_EARLY_ORDER_TOLERANCE = timedelta(minutes=5)
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PLACED": ("CONFIRMED", "PARTIALLY_FULFILLED", "DELIVERED", "CANCELLED"),
    "CONFIRMED": ("PARTIALLY_FULFILLED", "DELIVERED", "CANCELLED"),
    "PARTIALLY_FULFILLED": ("PARTIALLY_FULFILLED", "DELIVERED", "CANCELLED"),
}
_BILLABLE_STATES = ("PENDING", "ACCRUED", "FINAL", "CREDITED")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drops None members (JsonIgnoreCondition.WhenWritingNull)."""
    return {k: v for k, v in d.items() if v is not None}


def _currency_round(amount: Decimal, currency: str) -> Decimal:
    """C# Currency.Round: Math.Round(amount, minor units, ToEven), which never adds trailing zeros."""
    return round_decimal(amount, minor_units(currency))


def _try_parse_currency(text: str | None) -> Decimal | None:
    """C# Currency.TryParse (AllowLeadingSign | AllowDecimalPoint, invariant culture): ASCII digits with an optional sign and
    one decimal point, no white space; None when malformed or outside the C# decimal range."""
    if text is None or not text.isascii() or text != text.strip():
        return None
    value = try_parse_amount(text)
    return None if value is None or value.copy_abs() > _DECIMAL_MAX else value


def _month_start(at: datetime) -> datetime:
    at = as_utc(at)
    return datetime(at.year, at.month, 1, tzinfo=UTC)


def _add_month(start: datetime) -> datetime:
    return datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------------------------------
# Request parsing (HealthSwap.Contracts records read from the API dicts)
# ---------------------------------------------------------------------------------------------------------------------


def _obj(m: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(m, Mapping):
        raise HsError.invalid(f"{what} must be an object")
    return m


def _str(m: Mapping[str, Any], key: str, what: str, required: bool = False) -> str | None:
    value = m.get(key)
    if value is None:
        if required:
            raise HsError.invalid(f"{what}.{key} is required")
        return None
    if not isinstance(value, str):
        raise HsError.invalid(f"{what}.{key} must be a string")
    return value


def _number(m: Mapping[str, Any], key: str, what: str, required: bool = False) -> Decimal | None:
    value = m.get(key)
    if value is None:
        if required:
            raise HsError.invalid(f"{what}.{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise HsError.invalid(f"{what}.{key} must be a number")
    return to_decimal(value)


def _timestamp(m: Mapping[str, Any], key: str, what: str) -> datetime:
    value = m.get(key)
    if value is None:
        raise HsError.invalid(f"{what}.{key} is required")
    if isinstance(value, datetime):
        return as_utc(value)
    try:
        if not isinstance(value, str):
            raise ValueError(value)
        return parse_iso(value)
    except ValueError:
        raise HsError.invalid(f"{what}.{key} must be an RFC 3339 timestamp") from None


def _uuid(m: Mapping[str, Any], key: str, what: str) -> uuid.UUID | None:
    value = m.get(key)
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        if not isinstance(value, str):
            raise ValueError(value)
        return uuid.UUID(value.strip())
    except ValueError:
        raise HsError.invalid(f"{what}.{key} must be a UUID") from None


def _money(m: Mapping[str, Any], key: str, what: str) -> MoneyInput | None:
    value = m.get(key)
    if value is None or isinstance(value, MoneyInput):
        return value
    return MoneyInput.from_api(value, f"{what}.{key}")


def _required_money(m: Mapping[str, Any], key: str, what: str) -> MoneyInput:
    value = _money(m, key, what)
    if value is None:
        raise HsError.invalid(f"{what}.{key} is required")
    return value


def _list(m: Mapping[str, Any], key: str, what: str, required: bool = False) -> list[Any] | None:
    value = m.get(key)
    if value is None:
        if required:
            raise HsError.invalid(f"{what}.{key} is required")
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HsError.invalid(f"{what}.{key} must be an array")
    return list(value)


@dataclass(frozen=True, slots=True)
class OrderLine:
    """Contracts.OrderLine (request)."""

    line_id: str
    partner_sku: str
    quantity: Decimal
    unit_price: MoneyInput
    net_amount: MoneyInput
    gtin: str | None = None
    line_discount: MoneyInput | None = None
    allocated_order_discount: MoneyInput | None = None
    tax_amount: MoneyInput | None = None
    tax_rate: Decimal | None = None
    suggestion_id: uuid.UUID | None = None

    @staticmethod
    def from_api(m: Any, what: str = "line") -> OrderLine:
        m = _obj(m, what)
        quantity = _number(m, "quantity", what, True)
        return OrderLine(
            line_id=_str(m, "lineId", what, True) or "", partner_sku=_str(m, "partnerSku", what, True) or "",
            quantity=_ZERO if quantity is None else quantity, unit_price=_required_money(m, "unitPrice", what),
            net_amount=_required_money(m, "netAmount", what), gtin=_str(m, "gtin", what), line_discount=_money(m, "lineDiscount", what),
            allocated_order_discount=_money(m, "allocatedOrderDiscount", what), tax_amount=_money(m, "taxAmount", what),
            tax_rate=_number(m, "taxRate", what), suggestion_id=_uuid(m, "suggestionId", what),
        )


@dataclass(frozen=True, slots=True)
class OrderCreateRequest:
    """Contracts.OrderCreateRequest."""

    order_id: str
    market: str
    currency: str
    placed_at: datetime
    lines: tuple[OrderLine, ...]
    cart_ref: str | None = None
    user_ref: str | None = None
    store_id: str | None = None
    fulfillment_type: str | None = None
    experiment_arm: str | None = None
    order_discount: MoneyInput | None = None

    @staticmethod
    def from_api(req: Any) -> OrderCreateRequest:
        if isinstance(req, OrderCreateRequest):
            return req
        what = "request"
        req = _obj(req, what)
        lines = _list(req, "lines", what, True) or []
        return OrderCreateRequest(
            order_id=_str(req, "orderId", what, True) or "", market=_str(req, "market", what, True) or "",
            currency=_str(req, "currency", what, True) or "", placed_at=_timestamp(req, "placedAt", what),
            lines=tuple(x if isinstance(x, OrderLine) else OrderLine.from_api(x, f"lines[{i}]") for i, x in enumerate(lines)),
            cart_ref=_str(req, "cartRef", what), user_ref=_str(req, "userRef", what), store_id=_str(req, "storeId", what),
            fulfillment_type=_str(req, "fulfillmentType", what), experiment_arm=_str(req, "experimentArm", what),
            order_discount=_money(req, "orderDiscount", what),
        )


@dataclass(frozen=True, slots=True)
class LineStatus:
    """Contracts.LineStatus."""

    line_id: str
    delivered_quantity: Decimal | None = None
    cancelled_quantity: Decimal | None = None

    @staticmethod
    def from_api(m: Any, what: str = "lineStatus") -> LineStatus:
        m = _obj(m, what)
        return LineStatus(_str(m, "lineId", what, True) or "", _number(m, "deliveredQuantity", what), _number(m, "cancelledQuantity", what))


@dataclass(frozen=True, slots=True)
class OrderStatusUpdate:
    """Contracts.OrderStatusUpdate."""

    status: str
    occurred_at: datetime
    line_statuses: tuple[LineStatus, ...] | None = None

    @staticmethod
    def from_api(req: Any) -> OrderStatusUpdate:
        if isinstance(req, OrderStatusUpdate):
            return req
        what = "request"
        req = _obj(req, what)
        statuses = _list(req, "lineStatuses", what)
        return OrderStatusUpdate(
            _str(req, "status", what, True) or "", _timestamp(req, "occurredAt", what),
            None if statuses is None else tuple(x if isinstance(x, LineStatus) else LineStatus.from_api(x, f"lineStatuses[{i}]") for i, x in enumerate(statuses)),
        )


@dataclass(frozen=True, slots=True)
class OrderAdjustment:
    """Contracts.OrderAdjustment."""

    adjustment_id: str
    line_id: str
    type: str
    occurred_at: datetime
    quantity: Decimal | None = None
    amount: MoneyInput | None = None
    substituted_partner_sku: str | None = None

    @staticmethod
    def from_api(m: Any, what: str = "adjustment") -> OrderAdjustment:
        m = _obj(m, what)
        return OrderAdjustment(
            adjustment_id=_str(m, "adjustmentId", what, True) or "", line_id=_str(m, "lineId", what, True) or "", type=_str(m, "type", what, True) or "",
            occurred_at=_timestamp(m, "occurredAt", what), quantity=_number(m, "quantity", what), amount=_money(m, "amount", what),
            substituted_partner_sku=_str(m, "substitutedPartnerSku", what),
        )


@dataclass(frozen=True, slots=True)
class OrderAdjustmentRequest:
    """Contracts.OrderAdjustmentRequest."""

    adjustments: tuple[OrderAdjustment, ...]

    @staticmethod
    def from_api(req: Any) -> OrderAdjustmentRequest:
        if isinstance(req, OrderAdjustmentRequest):
            return req
        items = _list(_obj(req, "request"), "adjustments", "request", True) or []
        return OrderAdjustmentRequest(tuple(x if isinstance(x, OrderAdjustment) else OrderAdjustment.from_api(x, f"adjustments[{i}]") for i, x in enumerate(items)))


# ---------------------------------------------------------------------------------------------------------------------
# Row records
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeaderRow:
    """OrderService.HeaderRow (txn.order_header)."""

    order_id: str
    placed_at: datetime
    cart_ref: str | None
    market: str
    currency: str
    status: str
    fulfillment_type: str | None
    return_window_days: int
    delivered_at: datetime | None

    @staticmethod
    def of(r: Mapping[str, Any]) -> HeaderRow:
        return HeaderRow(
            r["order_id"], r["placed_at"], r["cart_ref"], r["market"], r["currency"], r["status"], r["fulfillment_type"], int(r["return_window_days"]),
            r["delivered_at"],
        )


@dataclass(frozen=True, slots=True)
class LineRow:
    """OrderService.LineRow (txn.order_line)."""

    line_id: str
    partner_sku: str
    product_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    line_discount: Decimal
    allocated_order_discount: Decimal
    tax_amount: Decimal | None
    tax_rate: Decimal | None
    net_amount: Decimal
    suggestion_id: uuid.UUID | None
    delivered_qty: Decimal | None = None
    cancelled_qty: Decimal | None = None

    @staticmethod
    def of(r: Mapping[str, Any]) -> LineRow:
        return LineRow(
            r["line_id"], r["partner_sku"], r["product_id"], r["quantity"], r["unit_price"], r["line_discount"], r["allocated_order_discount"], r["tax_amount"],
            r["tax_rate"], r["net_amount"], r["suggestion_id"], r["delivered_qty"], r["cancelled_qty"],
        )


@dataclass(slots=True)
class AttrRow:
    """AttributionEngine.AttrRow (txn.attribution); reversed_amount is updated in place by reversals."""

    attribution_id: uuid.UUID
    order_id: str
    line_id: str
    suggestion_id: uuid.UUID
    state: str
    rejection_reason: str | None
    currency: str
    replaced_qty: Decimal
    orig_unit_price_snapshot: Decimal
    alt_net_unit_price: Decimal
    pack_delta: Decimal
    size_normalized_delta: Decimal | None
    fair_premium: Decimal
    fee_rate: Decimal
    fee_amount: Decimal
    reversed_amount: Decimal
    rule_version: str
    finalizes_at: datetime | None = None
    original_partner_sku: str | None = None
    alternative_partner_sku: str | None = None

    @staticmethod
    def of(r: Mapping[str, Any]) -> AttrRow:
        return AttrRow(**{name: r[name] for name in AttrRow.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class EventRow:
    """AttributionEngine.EventRow (txn.suggestion_event evidence)."""

    suggestion_id: uuid.UUID
    event_type: str
    alternative_partner_sku: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class PricedLine:
    """An order line with its parsed amounts (C# tuple (Line, Price, LineDiscount, OrderDiscount, Tax, Net))."""

    line: OrderLine
    price: Decimal
    line_discount: Decimal
    order_discount: Decimal
    tax: Decimal | None
    net: Decimal


# ---------------------------------------------------------------------------------------------------------------------
# OrderService
# ---------------------------------------------------------------------------------------------------------------------


class OrderService:
    """Orders, lifecycle updates and adjustments (HLD 9.4-9.5). Attribution and fees are delegated to AttributionEngine."""

    HeaderRow = HeaderRow
    LineRow = LineRow

    def __init__(self, db: Db, cache: HotCache, partners: PartnerDirectory, suggestions: SuggestionStore, attribution: AttributionEngine,
                 clock: Clock) -> None:
        self.db = db
        self.cache = cache
        self.partners = partners
        self.suggestions = suggestions
        self.attribution = attribution
        self.clock = clock

    @staticmethod
    def priced_lines(req: OrderCreateRequest) -> list[PricedLine]:
        """Parses the line amounts; 400 "Order validation failed" with one FieldError per duplicate lineId or foreign currency."""
        errors: list[FieldError] = []
        lines: list[PricedLine] = []
        for i, line in enumerate(req.lines):
            if any(x.line_id == line.line_id for x in req.lines[:i]):
                errors.append(FieldError(f"/lines/{i}/lineId", "Duplicate lineId"))

            def amount(m: MoneyInput | None, field: str, i: int = i) -> Decimal:
                if m is None:
                    return _ZERO
                if m.currency != req.currency:
                    errors.append(FieldError(f"/lines/{i}/{field}/currency", f"Must be {req.currency}"))
                value = _try_parse_currency(m.amount)
                return _ZERO if value is None else value

            price = amount(line.unit_price, "unitPrice")
            line_discount = amount(line.line_discount, "lineDiscount")
            order_discount = amount(line.allocated_order_discount, "allocatedOrderDiscount")
            tax = None if line.tax_amount is None else amount(line.tax_amount, "taxAmount")
            lines.append(PricedLine(line, price, line_discount, order_discount, tax, amount(line.net_amount, "netAmount")))
        if errors:
            raise HsError.invalid("Order validation failed", errors)
        return lines

    def create(self, ctx: PartnerContext, request: Mapping[str, Any] | OrderCreateRequest) -> dict[str, Any]:
        """OrderCreateRequest -> Order (the attribution summary included). 409 HS-ORDER-EXISTS when the orderId was reported."""
        req = OrderCreateRequest.from_api(request)
        partner = self.partners.get(ctx.partner_id)
        cfg = partner.config
        if req.market not in partner.markets:
            raise HsError.unprocessable("HS-UNSUPPORTED-MARKET", f"Market {req.market} is not enabled for this partner")
        if req.currency != cfg.currency:
            raise HsError.unprocessable("HS-CURRENCY-MISMATCH", f"Order currency must be {cfg.currency}")
        lines = self.priced_lines(req)

        maps = self.cache.get_sku_maps(ctx.partner_id, [x.partner_sku for x in req.lines])
        snapshots: dict[uuid.UUID, SuggestionSnapshot | None] = {}
        for sid in dict.fromkeys(x.suggestion_id for x in req.lines if x.suggestion_id is not None):
            snapshots[sid] = self.suggestions.get(ctx.partner_id, sid)

        now = self.clock.utc_now()

        def work(u: Uow) -> None:
            inserted = u.execute(
                "insert into txn.order_key (partner_id, order_id, placed_at) values (@p, @o, @at) on conflict do nothing",
                {"p": ctx.partner_id, "o": req.order_id, "at": req.placed_at},
            )
            if inserted == 0:
                raise HsError.conflict("HS-ORDER-EXISTS", f"Order {req.order_id} was already reported")
            order_discount = _try_parse_currency(req.order_discount.amount) if req.order_discount is not None else None
            u.execute(
                """
                insert into txn.order_header (partner_id, order_id, placed_at, cart_ref, user_ref, store_id, market, currency, status, fulfillment_type, experiment_arm,
                    order_discount, return_window_days, updated_at)
                values (@p, @o, @at, @cartRef, @userRef, @storeId, @market, @currency, 'PLACED', @ft, @arm, @discount, @window, @now)
                """,
                {
                    "p": ctx.partner_id, "o": req.order_id, "at": req.placed_at, "cartRef": req.cart_ref, "userRef": req.user_ref, "storeId": req.store_id,
                    "market": req.market, "currency": req.currency, "ft": req.fulfillment_type, "arm": req.experiment_arm,
                    "discount": _ZERO if order_discount is None else order_discount, "window": cfg.return_window_for(req.fulfillment_type), "now": now,
                },
            )
            for i, x in enumerate(lines):
                u.execute(
                    """
                    insert into txn.order_line (partner_id, order_id, placed_at, line_id, partner_sku, product_id, quantity, unit_price, line_discount, allocated_order_discount,
                        tax_amount, tax_rate, net_amount, suggestion_id)
                    values (@p, @o, @at, @lineId, @sku, @product, @qty, @price, @ld, @od, @tax, @taxRate, @net, @sid)
                    """,
                    {
                        "p": ctx.partner_id, "o": req.order_id, "at": req.placed_at, "lineId": x.line.line_id, "sku": x.line.partner_sku,
                        "product": None if maps[i] is None else maps[i].product_id, "qty": x.line.quantity, "price": x.price, "ld": x.line_discount,
                        "od": x.order_discount, "tax": x.tax, "taxRate": x.line.tax_rate, "net": x.net, "sid": x.line.suggestion_id,
                    },
                )
            self.attribution.attribute_order(u, ctx.partner_id, cfg, req, [(x.line, x.net) for x in lines], snapshots)
            Outbox.enqueue(u, ctx.partner_id, Topics.ORDERS, req.order_id, {
                "orderId": req.order_id, "market": req.market, "placedAt": req.placed_at, "experimentArm": req.experiment_arm, "cartRef": req.cart_ref,
                "lines": len(req.lines), "attributedLines": sum(1 for x in req.lines if x.suggestion_id is not None), "gross": sum((x.net for x in lines), _ZERO),
            })

        self.db.in_tx(DbRole.APP, ctx.partner_id, work)
        log.info("Order %s recorded for partner %s", req.order_id, ctx.partner_id)
        return self.get(ctx.partner_id, req.order_id)

    def get(self, partner_id: uuid.UUID, order_id: str) -> dict[str, Any]:
        """Order (404 when unknown to this partner)."""

        def work(u: Uow) -> dict[str, Any]:
            h, lines = self.load(u, order_id)
            return self.to_api(h, lines, AttributionEngine.summary(u, order_id, h.currency.strip()))

        return self.db.in_tx(DbRole.APP, partner_id, work)

    def attribution_summary(self, partner_id: uuid.UUID, order_id: str) -> dict[str, Any]:
        """AttributionSummary of an order (C# AttributionAsync)."""

        def work(u: Uow) -> dict[str, Any]:
            h, _ = self.load(u, order_id)
            return AttributionEngine.summary(u, order_id, h.currency.strip())

        return self.db.in_tx(DbRole.APP, partner_id, work)

    @staticmethod
    def check_transition(current: str, target: str) -> None:
        """409 HS-ORDER-STATE unless `current` -> `target` is an allowed order status change."""
        if target not in _ALLOWED_TRANSITIONS.get(current, ()):
            raise HsError.conflict("HS-ORDER-STATE", f"Cannot change order status from {current} to {target}")

    @staticmethod
    def check_line_status(lines: Sequence[LineRow], ls: LineStatus) -> LineRow:
        """The order line a LineStatus refers to; 400 for an unknown lineId or delivered + cancelled above the ordered quantity."""
        line = next((x for x in lines if x.line_id == ls.line_id), None)
        if line is None:
            raise HsError.invalid(f"Unknown lineId {ls.line_id}")
        delivered = _ZERO if ls.delivered_quantity is None else ls.delivered_quantity
        cancelled = _ZERO if ls.cancelled_quantity is None else ls.cancelled_quantity
        if delivered + cancelled > line.quantity:
            raise HsError.invalid(f"Line {ls.line_id}: delivered + cancelled exceeds the ordered quantity")
        return line

    def update_status(self, partner_id: uuid.UUID, order_id: str, request: Mapping[str, Any] | OrderStatusUpdate) -> dict[str, Any]:
        """OrderStatusUpdate -> Order. Delivery accrues the PENDING fees (FEE_ACCRUAL); cancellation voids or reverses them."""
        update = OrderStatusUpdate.from_api(request)

        def work(u: Uow) -> None:
            h, lines = self.load(u, order_id)
            self.check_transition(h.status, update.status)
            for ls in update.line_statuses or ():
                self.check_line_status(lines, ls)
                u.execute(
                    "update txn.order_line set delivered_qty = coalesce(@d, delivered_qty), cancelled_qty = coalesce(@c, cancelled_qty) where order_id = @o and placed_at = @at and line_id = @l",
                    {"d": ls.delivered_quantity, "c": ls.cancelled_quantity, "o": order_id, "at": h.placed_at, "l": ls.line_id},
                )
            delivered = update.status in ("DELIVERED", "PARTIALLY_FULFILLED")
            u.execute(
                "update txn.order_header set status = @s, delivered_at = case when @delivered then coalesce(delivered_at, @at) else delivered_at end, updated_at = now() where order_id = @o and placed_at = @placed",
                {"s": update.status, "delivered": delivered, "at": update.occurred_at, "o": order_id, "placed": h.placed_at},
            )
            _, updated_lines = self.load(u, order_id)
            if delivered:
                self.attribution.accrue(u, partner_id, order_id, update.occurred_at + timedelta(days=h.return_window_days), updated_lines,
                                        update.status == "DELIVERED")
            if update.status == "CANCELLED":
                self.attribution.cancel(u, partner_id, order_id, "ORDER_CANCELLED", None)
            Outbox.enqueue(u, partner_id, Topics.ORDERS, order_id, {"orderId": order_id, "status": update.status, "occurredAt": update.occurred_at})

        self.db.in_tx(DbRole.APP, partner_id, work)
        return self.get(partner_id, order_id)

    def adjust(self, partner_id: uuid.UUID, order_id: str, request: Mapping[str, Any] | OrderAdjustmentRequest) -> dict[str, Any]:
        """OrderAdjustmentRequest -> AttributionSummary. Replayed adjustmentIds are skipped."""
        req = OrderAdjustmentRequest.from_api(request)
        partner = self.partners.get(partner_id)

        def work(u: Uow) -> dict[str, Any]:
            h, lines = self.load(u, order_id)
            currency = h.currency.strip()
            for a in req.adjustments:
                line = next((x for x in lines if x.line_id == a.line_id), None)
                if line is None:
                    raise HsError.invalid(f"Unknown lineId {a.line_id}")
                if a.amount is not None and a.amount.currency != currency:
                    raise HsError.invalid(f"Adjustment {a.adjustment_id}: currency must be {currency}")
                if a.quantity is not None and a.quantity > line.quantity:
                    raise HsError.invalid(f"Adjustment {a.adjustment_id}: quantity exceeds the line quantity")
                amount = _try_parse_currency(a.amount.amount) if a.amount is not None else None
                inserted = u.execute(
                    """
                    insert into txn.order_adjustment (partner_id, adjustment_id, order_id, line_id, adjustment_type, quantity, amount, substituted_partner_sku, occurred_at)
                    values (@p, @id, @o, @l, @type, @qty, @amount, @sub, @at) on conflict (partner_id, adjustment_id) do nothing
                    """,
                    {
                        "p": partner_id, "id": a.adjustment_id, "o": order_id, "l": a.line_id, "type": a.type, "qty": a.quantity, "amount": amount,
                        "sub": a.substituted_partner_sku, "at": a.occurred_at,
                    },
                )
                if inserted == 0:
                    continue  # replayed adjustment
                self.attribution.adjust(u, partner_id, partner.config, order_id, line, a, amount)
            Outbox.enqueue(u, partner_id, Topics.ORDERS, order_id, {"orderId": order_id, "adjustments": len(req.adjustments)})
            return AttributionEngine.summary(u, order_id, currency)

        return self.db.in_tx(DbRole.APP, partner_id, work)

    @staticmethod
    def load(u: Uow, order_id: str) -> tuple[HeaderRow, list[LineRow]]:
        """Header and lines (ordered by line_id) of an order visible to the unit of work's partner; 404 otherwise."""
        placed_at = u.scalar("select placed_at from txn.order_key where order_id = @orderId", {"orderId": order_id})
        if placed_at is None:
            raise HsError.not_found("Order " + order_id)
        h = HeaderRow.of(u.single(
            "select order_id, placed_at, cart_ref, market, currency, status, fulfillment_type, return_window_days, delivered_at from txn.order_header where order_id = @orderId and placed_at = @placedAt",
            {"orderId": order_id, "placedAt": placed_at},
        ))
        lines = [LineRow.of(r) for r in u.query(
            """
            select line_id, partner_sku, product_id, quantity, unit_price, line_discount, allocated_order_discount, tax_amount, tax_rate, net_amount, suggestion_id,
                   delivered_qty, cancelled_qty
            from txn.order_line where order_id = @orderId and placed_at = @placedAt order by line_id
            """,
            {"orderId": order_id, "placedAt": placed_at},
        )]
        return h, lines

    @staticmethod
    def to_api(h: HeaderRow, lines: Sequence[LineRow], summary: dict[str, Any]) -> dict[str, Any]:
        """Order."""
        c = h.currency.strip()
        return _compact({
            "orderId": h.order_id, "status": h.status, "market": h.market, "currency": c, "placedAt": iso(h.placed_at), "deliveredAt": iso(h.delivered_at),
            "lines": [_compact({
                "lineId": x.line_id, "partnerSku": x.partner_sku, "quantity": x.quantity, "unitPrice": ApiMapper.money(x.unit_price, c),
                "lineDiscount": ApiMapper.money(x.line_discount, c) if x.line_discount != 0 else None,
                "allocatedOrderDiscount": ApiMapper.money(x.allocated_order_discount, c) if x.allocated_order_discount != 0 else None,
                "taxAmount": None if x.tax_amount is None else ApiMapper.money(x.tax_amount, c), "taxRate": x.tax_rate,
                "netAmount": ApiMapper.money(x.net_amount, c), "suggestionId": None if x.suggestion_id is None else str(x.suggestion_id),
            }) for x in lines],
            "attribution": summary,
        })


# ---------------------------------------------------------------------------------------------------------------------
# AttributionEngine
# ---------------------------------------------------------------------------------------------------------------------


class AttributionEngine:
    """
    FP-1 attribution (HLD 9.2-9.5): evidence chain (served, impression, accept, product match, window), fair premium with caps,
    PENDING at order, ACCRUED at delivery (FEE_ACCRUAL), proportional reversals for returns, FINAL after the return window.
    """

    COLUMNS = """
        attribution_id, order_id, line_id, suggestion_id, state, rejection_reason, currency, replaced_qty, orig_unit_price_snapshot, alt_net_unit_price, pack_delta,
        size_normalized_delta, fair_premium, fee_rate, fee_amount, reversed_amount, rule_version, finalizes_at, original_partner_sku, alternative_partner_sku
        """

    def __init__(self, db: Db, ledger: LedgerService, clock: Clock) -> None:
        self.db = db
        self.ledger = ledger
        self.clock = clock

    # ---- pure decisions ---------------------------------------------------------------------------------------------

    @staticmethod
    def evidence(s: SuggestionSnapshot | None, line: OrderLine, events: Sequence[EventRow], placed_at: datetime,
                 claimed: set[uuid.UUID]) -> tuple[str, str | None, AlternativeSnapshot | None]:
        """The FP-1 evidence chain of one order line with a suggestionId: (state, rejection reason, matched alternative).

        Checks in order: NOT_SERVED_BY_HEALTHSWAP, PRODUCT_MISMATCH, NO_ACCEPT (the last ACCEPT for any or this SKU),
        NO_IMPRESSION (none up to 5 s after that ACCEPT), EXPIRED (placed after the attribution window or more than 5 minutes
        before the suggestion), NOT_A_SWAP (no original price) and DUPLICATE (a line of the same order already claimed the
        suggestion). A line that passes claims its suggestion in `claimed` and is PENDING.
        """
        sid = line.suggestion_id
        if sid is None:
            raise ValueError("The order line has no suggestionId")
        evs = [e for e in events if e.suggestion_id == sid]
        accepts = [e for e in evs if e.event_type == "ACCEPT" and (e.alternative_partner_sku is None or e.alternative_partner_sku == line.partner_sku)]
        accept = sorted(accepts, key=lambda e: e.occurred_at)[-1] if accepts else None  # OrderBy(...).LastOrDefault()
        if s is None:
            return "REJECTED", "NOT_SERVED_BY_HEALTHSWAP", None
        alt = next((a for a in s.alternatives if a.partner_sku == line.partner_sku), None)
        if alt is None:
            return "REJECTED", "PRODUCT_MISMATCH", None
        if accept is None:
            return "REJECTED", "NO_ACCEPT", alt
        if not any(e.event_type == "IMPRESSION" and e.occurred_at <= accept.occurred_at + _IMPRESSION_TOLERANCE for e in evs):
            return "REJECTED", "NO_IMPRESSION", alt
        if placed_at > s.attribution_expires_at or placed_at < s.created_at - _EARLY_ORDER_TOLERANCE:
            return "REJECTED", "EXPIRED", alt
        if s.from_unit_price is None:
            return "REJECTED", "NOT_A_SWAP", alt
        if sid in claimed:
            return "REJECTED", "DUPLICATE", alt
        claimed.add(sid)
        return "PENDING", None, alt

    @staticmethod
    def fair_premium_input(s: SuggestionSnapshot, alt: AlternativeSnapshot, line: OrderLine, net: Decimal, order_lines: Sequence[OrderLine],
                           currency: str, cfg: PartnerConfig) -> FairPremiumInput:
        """FP-1 inputs of an evidenced line: the suggestion snapshot (p_orig, q_orig, sizes) and the order line (q_alt, net)."""
        if s.from_unit_price is None:
            raise ValueError("The suggestion has no original unit price (NOT_A_SWAP)")
        return FairPremiumInput(
            original_unit_price=s.from_unit_price, original_quantity=s.from_qty, original_size=NetQuantity.normalize(s.from_net_quantity, s.from_net_unit),
            original_tax_rate=s.from_tax_rate, alternative_quantity=line.quantity, alternative_net_amount=net,
            alternative_size=NetQuantity.normalize(alt.net_quantity, alt.net_unit), alternative_tax_rate=line.tax_rate,
            original_quantity_in_order=sum((x.quantity for x in order_lines if x.partner_sku == s.from_partner_sku), _ZERO), currency=currency,
            convert_to_ex_tax=cfg.fee.fee_basis == "TAX_EXCLUSIVE",
        )

    @staticmethod
    def accrual(a: AttrRow, line: LineRow, complete: bool) -> tuple[str, Decimal, Decimal] | None:
        """Delivery outcome of a PENDING attribution: None (partial fulfilment without a delivered quantity for the line),
        ("VOID", 0, 0) when nothing was delivered, else ("ACCRUED", fee on the delivered share, fee-bearing quantity)."""
        if not complete and line.delivered_qty is None:
            return None
        delivered = line.delivered_qty if line.delivered_qty is not None else line.quantity - (_ZERO if line.cancelled_qty is None else line.cancelled_qty)
        if delivered <= 0:
            return "VOID", _ZERO, _ZERO
        fee_bearing = decimal_min(a.replaced_qty, delivered)
        fee = a.fee_amount
        if a.replaced_qty > 0 and fee_bearing < a.replaced_qty:
            fee = _currency_round(decimal_div(decimal_mul(a.fee_amount, fee_bearing), a.replaced_qty), a.currency.strip())
        return "ACCRUED", fee, fee_bearing

    @staticmethod
    def return_quantity(line: LineRow, adj_quantity: Decimal | None, amount: Decimal | None) -> Decimal:
        """Returned quantity: the adjustment's quantity, else the refunded share of the line's net amount (3 places), else the line."""
        if adj_quantity is not None:
            return adj_quantity
        if amount is not None and line.net_amount > 0:
            return round_decimal(decimal_div(decimal_mul(line.quantity, amount), line.net_amount), 3)
        return line.quantity

    @staticmethod
    def pending_return(a: AttrRow, qty: Decimal) -> tuple[Decimal, Decimal]:
        """A return before delivery: (reduced fee, remaining fee-bearing quantity)."""
        remaining = decimal_max(_ZERO, a.replaced_qty - qty)
        fee = _currency_round(decimal_div(decimal_mul(a.fee_amount, remaining), a.replaced_qty), a.currency.strip()) if a.replaced_qty > 0 else _ZERO
        return fee, remaining

    @staticmethod
    def price_adjustment(a: AttrRow, line_quantity: Decimal, credit: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
        """The shopper paid `credit` less: (net unit price after the credit, fee recomputed on the reduced premium, never higher)."""
        net_unit = a.alt_net_unit_price - decimal_div(credit, decimal_max(_ONE, line_quantity)) if a.replaced_qty > 0 else a.alt_net_unit_price
        size_cap = _DECIMAL_MAX if a.size_normalized_delta is None else a.size_normalized_delta - (a.alt_net_unit_price - net_unit)
        premium_per_unit = decimal_max(_ZERO, decimal_min(net_unit - a.orig_unit_price_snapshot, size_cap))
        new_fee = decimal_min(a.fee_amount, _currency_round(decimal_mul(decimal_mul(rate, premium_per_unit), a.replaced_qty), a.currency.strip()))
        return net_unit, new_fee

    # ---- order lifecycle --------------------------------------------------------------------------------------------

    def attribute_order(self, u: Uow, partner_id: uuid.UUID, cfg: PartnerConfig, order: OrderCreateRequest, lines: Sequence[tuple[OrderLine, Decimal]],
                        snapshots: Mapping[uuid.UUID, SuggestionSnapshot | None]) -> None:
        """Inserts one txn.attribution row per order line with a suggestionId (PENDING with its capped FP-1 fee, or REJECTED)."""
        suggestion_ids = list(dict.fromkeys(x.suggestion_id for x, _ in lines if x.suggestion_id is not None))
        if not suggestion_ids:
            return
        events = [EventRow(**r) for r in u.query(
            "select suggestion_id, event_type, alternative_partner_sku, occurred_at from txn.suggestion_event where suggestion_id = any(@ids) and event_type in ('IMPRESSION', 'ACCEPT', 'UNDO')",
            {"ids": suggestion_ids},
        )]
        month_start = _month_start(order.placed_at)
        month_fee = to_decimal(u.scalar(
            "select coalesce(sum(fee_amount - reversed_amount), 0) from txn.attribution where state in ('PENDING', 'ACCRUED', 'FINAL') and computed_at >= @from and computed_at < @to",
            {"from": month_start, "to": _add_month(month_start)},
        ) or _ZERO)
        order_fee = _ZERO
        claimed: set[uuid.UUID] = set()
        order_lines = [x for x, _ in lines]
        for line, net in lines:
            if line.suggestion_id is None:
                continue
            sid = line.suggestion_id
            s = snapshots.get(sid)
            state, reason, alt = self.evidence(s, line, events, order.placed_at, claimed)
            fp: FairPremiumResult | None = None
            fee = uncapped = _ZERO
            if state == "PENDING" and s is not None and alt is not None:
                fp = FairPremiumRule.compute(self.fair_premium_input(s, alt, line, net, order_lines, order.currency, cfg), cfg.fee.rate)
                uncapped = fp.fee
                fee = FairPremiumRule.apply_caps(fp.fee, cfg.fee.caps, order_fee, month_fee)
                order_fee += fee
                month_fee += fee
            if fp is not None:
                porig, palt = fp.original_unit_price, fp.alternative_net_unit_price
            else:
                porig = s.from_unit_price if s is not None and s.from_unit_price is not None else _ZERO
                palt = round_decimal(decimal_div(net, line.quantity), 4) if line.quantity > 0 else _ZERO
            u.execute(
                """
                insert into txn.attribution (attribution_id, partner_id, order_id, line_id, suggestion_id, state, rejection_reason, currency, replaced_qty,
                    orig_unit_price_snapshot, alt_net_unit_price, pack_delta, size_normalized_delta, fair_premium, fee_rate, fee_amount, rule_version,
                    original_partner_sku, alternative_partner_sku, uncapped_fee_amount, computed_at)
                values (@id, @p, @o, @l, @sid, @state, @reason, @currency, @qrep, @porig, @palt, @dpack, @dsize, @premium, @rate, @fee, @rule, @origSku, @altSku, @uncapped, @now)
                """,
                {
                    "id": new_id(), "p": partner_id, "o": order.order_id, "l": line.line_id, "sid": sid, "state": state, "reason": reason,
                    "currency": order.currency, "qrep": fp.replaced_quantity if fp is not None else _ZERO, "porig": porig, "palt": palt,
                    "dpack": fp.pack_delta if fp is not None else _ZERO, "dsize": fp.size_normalized_delta if fp is not None else None,
                    "premium": fp.fair_premium if fp is not None else _ZERO, "rate": cfg.fee.rate, "fee": fee, "rule": FairPremiumRule.VERSION,
                    "origSku": s.from_partner_sku if s is not None else None, "altSku": line.partner_sku, "uncapped": uncapped, "now": self.clock.utc_now(),
                },
            )
        Outbox.enqueue(u, partner_id, Topics.ATTRIBUTION, order.order_id, {"orderId": order.order_id, "stage": "PENDING", "orderFee": order_fee})

    def accrue(self, u: Uow, partner_id: uuid.UUID, order_id: str, finalizes_at: datetime, lines: Sequence[LineRow], complete: bool) -> None:
        """Delivery: PENDING -> ACCRUED with the fee on the delivered share; posts FEE_ACCRUAL (hs_ledger_writer)."""
        pending = [AttrRow.of(r) for r in u.query(f"select {self.COLUMNS} from txn.attribution where order_id = @orderId and state = 'PENDING'", {"orderId": order_id})]
        if not pending:
            return
        u.set_role(DbRole.LEDGER_WRITER)
        for a in pending:
            line = next((x for x in lines if x.line_id == a.line_id), None)
            if line is None:
                raise LookupError("Sequence contains no matching element")
            outcome = self.accrual(a, line, complete)
            if outcome is None:
                continue  # partial fulfillment: only lines with a reported delivered quantity
            state, fee, fee_bearing = outcome
            if state == "VOID":
                u.execute(
                    "update txn.attribution set state = 'VOID', rejection_reason = 'NOT_DELIVERED', computed_at = now() where attribution_id = @id",
                    {"id": a.attribution_id},
                )
                continue
            u.execute(
                "update txn.attribution set state = 'ACCRUED', fee_amount = @fee, replaced_qty = @q, accrued_at = @now, finalizes_at = @fin where attribution_id = @id and state = 'PENDING'",
                {"fee": fee, "q": fee_bearing, "now": self.clock.utc_now(), "fin": finalizes_at, "id": a.attribution_id},
            )
            if fee > 0:
                self.ledger.post(u, partner_id, a.currency.strip(), Postings.fee_accrual(a.attribution_id, fee), "attribution-engine")
        u.set_role(DbRole.APP)
        Outbox.enqueue(u, partner_id, Topics.ATTRIBUTION, order_id, {"orderId": order_id, "stage": "ACCRUED", "finalizesAt": finalizes_at})

    def cancel(self, u: Uow, partner_id: uuid.UUID, order_id: str, reason: str, line_id: str | None) -> None:
        """Order or line cancellation: PENDING -> VOID; ACCRUED -> full reversal (FEE_REVERSAL)."""
        rows = [AttrRow.of(r) for r in u.query(
            f"select {self.COLUMNS} from txn.attribution where order_id = @orderId and (@lineId::text is null or line_id = @lineId) and state in ('PENDING', 'ACCRUED')",
            {"orderId": order_id, "lineId": line_id},
        )]
        if not rows:
            return
        u.set_role(DbRole.LEDGER_WRITER)
        for a in rows:
            if a.state == "PENDING":
                u.execute("update txn.attribution set state = 'VOID', rejection_reason = @reason where attribution_id = @id", {"reason": reason, "id": a.attribution_id})
            else:
                self._reverse(u, partner_id, a, a.fee_amount - a.reversed_amount, reason + ":" + (line_id if line_id is not None else "order"))
        u.set_role(DbRole.APP)

    def adjust(self, u: Uow, partner_id: uuid.UUID, cfg: PartnerConfig, order_id: str, line: LineRow, adj: OrderAdjustment, amount: Decimal | None) -> None:
        """Applies one new adjustment to the line's PENDING or ACCRUED attribution (FINAL fees change only through disputes)."""
        r = u.one(f"select {self.COLUMNS} from txn.attribution where order_id = @orderId and line_id = @lineId", {"orderId": order_id, "lineId": line.line_id})
        a = None if r is None else AttrRow.of(r)
        if a is None or a.state not in ("PENDING", "ACCRUED"):
            return
        if adj.type in ("CANCELLATION", "PICKER_SUBSTITUTION"):
            self.cancel(u, partner_id, order_id, adj.type, line.line_id)
        elif adj.type in ("RETURN", "REFUND"):
            qty = self.return_quantity(line, adj.quantity, amount)
            if a.state == "PENDING":
                fee, remaining = self.pending_return(a, qty)
                u.execute("update txn.attribution set fee_amount = @fee, replaced_qty = @q where attribution_id = @id", {"fee": fee, "q": remaining, "id": a.attribution_id})
                return
            reversal = FairPremiumRule.reversal(a.fee_amount, a.reversed_amount, a.replaced_qty, qty, a.currency.strip())
            u.set_role(DbRole.LEDGER_WRITER)
            self._reverse(u, partner_id, a, reversal, adj.adjustment_id)
            u.set_role(DbRole.APP)
        elif adj.type == "PRICE_ADJUSTMENT" and amount is not None:
            # The shopper paid less: recompute the premium on the reduced net amount and reverse the difference.
            net_unit, new_fee = self.price_adjustment(a, line.quantity, amount, cfg.fee.rate)
            if a.state == "PENDING":
                u.execute(
                    "update txn.attribution set fee_amount = @fee, alt_net_unit_price = @unit where attribution_id = @id",
                    {"fee": new_fee, "unit": round_decimal(net_unit, 4), "id": a.attribution_id},
                )
                return
            u.set_role(DbRole.LEDGER_WRITER)
            self._reverse(u, partner_id, a, decimal_max(_ZERO, a.fee_amount - a.reversed_amount - new_fee), adj.adjustment_id)
            u.set_role(DbRole.APP)

    def _reverse(self, u: Uow, partner_id: uuid.UUID, a: AttrRow, amount: Decimal, adjustment_key: str) -> None:
        amount = decimal_min(amount, a.fee_amount - a.reversed_amount)
        if amount <= 0:
            return
        reversed_amount = a.reversed_amount + amount
        u.execute(
            "update txn.attribution set reversed_amount = @reversed, state = case when @reversed >= fee_amount then 'REVERSED' else state end where attribution_id = @id",
            {"reversed": reversed_amount, "id": a.attribution_id},
        )
        self.ledger.post(u, partner_id, a.currency.strip(), Postings.fee_reversal(a.attribution_id, adjustment_key, amount), "attribution-engine")
        a.reversed_amount = reversed_amount

    # ---- finalizer and summary --------------------------------------------------------------------------------------

    def finalize_due(self) -> int:
        """Return window elapsed: ACCRUED -> FINAL, posts FEE_FINALIZE for the net fee and drops the userRef. Returns the count."""
        now = self.clock.utc_now()
        partner_ids = [r["partner_id"] for r in self.db.in_tx(DbRole.OPS, None, lambda u: u.query(
            "select distinct partner_id from txn.attribution where state = 'ACCRUED' and finalizes_at <= @now", {"now": now}))]
        total = 0
        for partner_id in partner_ids:
            total += self.db.in_tx(DbRole.LEDGER_WRITER, partner_id, lambda u, pid=partner_id: self._finalize_partner(u, pid, now))
        if total > 0:
            log.info("FP-1 finalizer: %d attributions FINAL", total)
        return total

    def _finalize_partner(self, u: Uow, partner_id: uuid.UUID, now: datetime) -> int:
        due = [AttrRow.of(r) for r in u.query(
            f"select {self.COLUMNS} from txn.attribution where state = 'ACCRUED' and finalizes_at <= @now for update skip locked", {"now": now})]
        for a in due:
            net = a.fee_amount - a.reversed_amount
            u.execute("update txn.attribution set state = 'FINAL', finalized_at = @now where attribution_id = @id and state = 'ACCRUED'", {"now": now, "id": a.attribution_id})
            if net > 0:
                self.ledger.post(u, partner_id, a.currency.strip(), Postings.fee_finalize(a.attribution_id, net), "attribution-finalizer", now)
            Outbox.enqueue(u, partner_id, Topics.ATTRIBUTION, a.order_id, {"orderId": a.order_id, "lineId": a.line_id, "stage": "FINAL", "fee": net})
        if due:
            u.set_role(DbRole.APP)
            u.execute(
                "update txn.order_header set user_ref = null where order_id = any(@orders) and user_ref is not null",
                {"orders": list(dict.fromkeys(a.order_id for a in due))},
            )
        return len(due)

    @staticmethod
    def summary(u: Uow, order_id: str, currency: str) -> dict[str, Any]:
        """AttributionSummary of an order (rows ordered by line_id; totals over PENDING, ACCRUED, FINAL and CREDITED)."""
        rows = [AttrRow.of(r) for r in u.query(f"select {AttributionEngine.COLUMNS} from txn.attribution where order_id = @orderId order by line_id",
                                                {"orderId": order_id})]
        return AttributionEngine.summary_of(order_id, currency, rows)

    @staticmethod
    def summary_of(order_id: str, currency: str, rows: Sequence[AttrRow]) -> dict[str, Any]:
        billable = [r for r in rows if r.state in _BILLABLE_STATES]
        return {
            "orderId": order_id, "currency": currency,
            "totalFairPremium": ApiMapper.money(sum((r.fair_premium for r in billable), _ZERO), currency),
            "totalFee": ApiMapper.money(sum((r.fee_amount - r.reversed_amount for r in billable), _ZERO), currency),
            "lines": [_compact({
                "lineId": r.line_id, "suggestionId": str(r.suggestion_id), "originalPartnerSku": r.original_partner_sku,
                "alternativePartnerSku": r.alternative_partner_sku, "replacedQuantity": r.replaced_qty,
                "originalUnitPriceSnapshot": ApiMapper.money(r.orig_unit_price_snapshot, currency),
                "alternativeNetUnitPrice": ApiMapper.money(r.alt_net_unit_price, currency), "packDelta": ApiMapper.money(r.pack_delta, currency),
                "sizeNormalizedDelta": None if r.size_normalized_delta is None else ApiMapper.money(r.size_normalized_delta, currency),
                "fairPremium": ApiMapper.money(r.fair_premium, currency), "feeRate": r.fee_rate,
                "fee": ApiMapper.money(r.fee_amount - r.reversed_amount, currency), "state": r.state, "rejectionReason": r.rejection_reason,
                "finalizesAt": iso(r.finalizes_at), "ruleVersion": r.rule_version,
            }) for r in rows],
        }
