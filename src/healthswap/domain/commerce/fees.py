"""FP-1 fair-premium fee, RA-1 recommendation-add fee and attribution state machine (HLD 9.2, docs/06; Commerce/Fees.cs).

Money is Decimal throughout. Currency rounding uses primitives.round_money (half-even to the minor unit); the 4-place
rounding of the FP-1 breakdown is C# Math.Round(decimal, 4), which never adds trailing zeros.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from healthswap.domain.commerce.partner_config import FeeCaps, RecommendationAddFeeConfig
from healthswap.domain.primitives import UTC, NetQuantity, as_utc, round_money, to_decimal

_ZERO = Decimal(0)
_ONE = Decimal(1)
_MIN_UTC = datetime.min.replace(tzinfo=UTC)
RA1_VERSION = "RA-1"


def _round(value: Decimal, places: int) -> Decimal:
    """C# Math.Round(decimal, places): banker's rounding, applied only when the scale exceeds `places`."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= -places:
        return value
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def _max(a: Decimal, b: Decimal) -> Decimal:
    """C# Math.Max(decimal, decimal) (returns `a` on ties)."""
    return a if a >= b else b


def _min(a: Decimal, b: Decimal) -> Decimal:
    """C# Math.Min(decimal, decimal) (returns `b` on ties)."""
    return a if a < b else b


def _to_upper_invariant(s: str) -> str:
    """C# ToUpperInvariant (simple case mapping; U+0131 stays, "ß" does not become "SS")."""
    if s.isascii():
        return s.upper()
    out: list[str] = []
    for ch in s:
        upper = ch.upper()
        if ch == "\u0131":
            upper = ch
        elif len(upper) != 1:
            title = ch.title()
            upper = title if len(title) == 1 else ch
        out.append(upper)
    return "".join(out)


# ---------------------------------------------------------------------------------------------------------------------
# FP-1
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FairPremiumInput:
    # p_orig: unit price of the original item at suggestion time (snapshot).
    original_unit_price: Decimal
    # q_orig: original line quantity when the suggestion was served (snapshot).
    original_quantity: Decimal
    original_size: NetQuantity | None = None
    original_tax_rate: Decimal | None = None
    # q_alt: quantity of the alternative on the order line.
    alternative_quantity: Decimal
    # Net amount actually paid for the alternative line (after line and pro-rated order discounts).
    alternative_net_amount: Decimal
    alternative_size: NetQuantity | None = None
    alternative_tax_rate: Decimal | None = None
    # Quantity of the original product that is still present in the same order.
    original_quantity_in_order: Decimal = _ZERO
    currency: str
    convert_to_ex_tax: bool = False


@dataclass(frozen=True, slots=True)
class FairPremiumResult:
    replaced_quantity: Decimal
    original_unit_price: Decimal
    alternative_net_unit_price: Decimal
    pack_delta: Decimal
    size_normalized_delta: Decimal | None
    premium_per_unit: Decimal
    fair_premium: Decimal
    fee: Decimal


class FairPremiumRule:
    """
    FP-1 fair-premium rule (HLD 9.2): premium = max(0, min(d_pack, d_size)), fee = round_half_even(r x premium x q_rep).
    HealthSwap earns only on the health premium, never on extra volume, and never more than the shopper paid extra.
    """

    VERSION = "FP-1"

    @staticmethod
    def compute(i: FairPremiumInput, rate: Decimal) -> FairPremiumResult:
        q_alt = to_decimal(i.alternative_quantity)
        q_rep = _max(_ZERO, _min(q_alt, to_decimal(i.original_quantity) - to_decimal(i.original_quantity_in_order)))
        p_orig = to_decimal(i.original_unit_price)
        p_alt = to_decimal(i.alternative_net_amount) / q_alt if q_alt > 0 else _ZERO
        if i.convert_to_ex_tax:
            orig_tax = i.original_tax_rate if i.original_tax_rate is not None else i.alternative_tax_rate
            alt_tax = i.alternative_tax_rate if i.alternative_tax_rate is not None else i.original_tax_rate
            p_orig /= _ONE + (to_decimal(orig_tax) if orig_tax is not None else _ZERO)
            p_alt /= _ONE + (to_decimal(alt_tax) if alt_tax is not None else _ZERO)
        d_pack = p_alt - p_orig
        d_size: Decimal | None = None
        so, sa = i.original_size, i.alternative_size
        if so is not None and sa is not None and so.is_comparable_to(sa) and so.value > 0:
            d_size = p_alt - p_orig * (to_decimal(sa.value) / to_decimal(so.value))
        premium_per_unit = _max(_ZERO, _min(d_pack, d_size) if d_size is not None else d_pack)
        fair_premium = premium_per_unit * q_rep
        fee = round_money(to_decimal(rate) * fair_premium, i.currency)
        return FairPremiumResult(
            q_rep, _round(p_orig, 4), _round(p_alt, 4), _round(d_pack, 4), None if d_size is None else _round(d_size, 4),
            _round(premium_per_unit, 4), _round(fair_premium, 4), fee,
        )

    @staticmethod
    def apply_caps(fee: Decimal, caps: FeeCaps, order_fee_so_far: Decimal, month_fee_so_far: Decimal) -> Decimal:
        """Applies per-line, per-order and per-month caps in that order (HLD 9.2: caps are applied afterwards)."""
        capped = fee
        if caps.per_line is not None:
            capped = _min(capped, caps.per_line)
        if caps.per_order is not None:
            capped = _min(capped, _max(_ZERO, caps.per_order - order_fee_so_far))
        if caps.per_month is not None:
            capped = _min(capped, _max(_ZERO, caps.per_month - month_fee_so_far))
        return _max(_ZERO, capped)

    @staticmethod
    def reversal(fee: Decimal, already_reversed: Decimal, fee_bearing_quantity: Decimal, returned_quantity: Decimal, currency: str) -> Decimal:
        """Proportional reversal for returns (IN-1: returning 1 of 2 units reverses INR 2.90 of INR 5.80)."""
        if fee_bearing_quantity <= 0:
            return _ZERO
        share = _min(_ONE, to_decimal(returned_quantity) / to_decimal(fee_bearing_quantity))
        amount = round_money(to_decimal(fee) * share, currency)
        return _min(amount, fee - already_reversed)


# ---------------------------------------------------------------------------------------------------------------------
# RA-1
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoFeeSnapshot:
    """RA-1 terms snapshotted on the suggestion when it is served, so later contract changes never re-price it."""

    rule: str = RA1_VERSION
    amount: Decimal = _ZERO
    currency: str
    basis: str = "PER_ADD_EVENT"
    undo_grace_seconds: int = 5
    settle_after_seconds: int = 600
    require_impression: bool = True
    impression_tolerance_seconds: int = 5
    charge_accept: bool = True
    max_chargeable_adds_per_suggestion: int | None = None
    monthly_cap: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoChargeEvidence:
    suggestion_served: bool = False
    sku_recommended: bool = False
    first_impression_at: datetime | None = None
    suggestion_created_at: datetime = _MIN_UTC
    attribution_expires_at: datetime = _MIN_UTC
    # FINAL charges already recorded for the same suggestion (for the per-suggestion cap).
    final_charges_for_suggestion: int = 0
    # FINAL RA-1 amount already recorded for this partner in the calendar month (for the monthly cap).
    month_to_date_amount: Decimal = _ZERO


class RecoChargeReasons:
    NOT_SERVED = "NOT_SERVED_BY_HEALTHSWAP"
    NO_IMPRESSION = "NO_IMPRESSION"
    SKU_NOT_RECOMMENDED = "SKU_NOT_RECOMMENDED"
    OUTSIDE_WINDOW = "OUTSIDE_ATTRIBUTION_WINDOW"
    UNDONE_WITHIN_GRACE = "UNDONE_WITHIN_GRACE"
    CAP_REACHED = "CAP_REACHED"
    NOT_CONTRACTED = "FEE_NOT_CONTRACTED"


class RecoAddFeeRule:
    """
    RA-1 recommendation-add fee: a fixed fee (default INR 1.00) every time the shopper adds a HealthSwap-recommended,
    labeled product to the cart. Evidence chain: suggestion served by HealthSwap, the label was shown (impression), the
    added SKU is one of the recommended alternatives, and the add happened inside the attribution window.
    """

    VERSION = RA1_VERSION

    @staticmethod
    def snapshot(cfg: RecommendationAddFeeConfig, currency: str) -> RecoFeeSnapshot | None:
        if not cfg.enabled or cfg.amount <= 0 or _to_upper_invariant(cfg.currency) != _to_upper_invariant(currency):
            return None
        return RecoFeeSnapshot(
            rule=cfg.rule, amount=cfg.amount, currency=_to_upper_invariant(cfg.currency), basis=cfg.basis,
            undo_grace_seconds=cfg.undo_grace_seconds, settle_after_seconds=cfg.settle_after_seconds, require_impression=cfg.require_impression,
            impression_tolerance_seconds=cfg.impression_tolerance_seconds, charge_accept=cfg.charge_accept,
            max_chargeable_adds_per_suggestion=cfg.max_chargeable_adds_per_suggestion, monthly_cap=cfg.monthly_cap,
        )

    @staticmethod
    def is_chargeable_event(event_type: str, s: RecoFeeSnapshot) -> bool:
        return event_type == "ADD_TO_CART" or (event_type == "ACCEPT" and s.charge_accept)

    @staticmethod
    def amount(s: RecoFeeSnapshot, quantity: Decimal | None) -> Decimal:
        if s.basis == "PER_UNIT":
            return round_money(s.amount * _max(_ONE, to_decimal(quantity) if quantity is not None else _ONE), s.currency)
        return round_money(s.amount, s.currency)

    @staticmethod
    def finalizes_at(occurred_at: datetime, received_at: datetime, s: RecoFeeSnapshot) -> datetime:
        by_occurrence = as_utc(occurred_at) + timedelta(seconds=s.settle_after_seconds)
        minimum = as_utc(received_at) + timedelta(seconds=max(s.undo_grace_seconds, 1))
        return by_occurrence if by_occurrence > minimum else minimum

    @staticmethod
    def undo_voids(add_occurred_at: datetime, undo_occurred_at: datetime, s: RecoFeeSnapshot) -> bool:
        """An UNDO voids the latest add of the same suggestion when it happens within the grace period."""
        add, undo = as_utc(add_occurred_at), as_utc(undo_occurred_at)
        return undo >= add and (undo - add).total_seconds() <= s.undo_grace_seconds

    @staticmethod
    def decide(occurred_at: datetime, amount: Decimal, s: RecoFeeSnapshot, e: RecoChargeEvidence) -> tuple[str, str | None]:
        """(state, reason): ("FINAL", None) or ("REJECTED", one of RecoChargeReasons)."""
        if not e.suggestion_served:
            return "REJECTED", RecoChargeReasons.NOT_SERVED
        if not e.sku_recommended:
            return "REJECTED", RecoChargeReasons.SKU_NOT_RECOMMENDED
        at = as_utc(occurred_at)
        tolerance = timedelta(seconds=s.impression_tolerance_seconds)
        if at < as_utc(e.suggestion_created_at) - tolerance or at > as_utc(e.attribution_expires_at):
            return "REJECTED", RecoChargeReasons.OUTSIDE_WINDOW
        if s.require_impression and (e.first_impression_at is None or as_utc(e.first_impression_at) > at + tolerance):
            return "REJECTED", RecoChargeReasons.NO_IMPRESSION
        if s.max_chargeable_adds_per_suggestion is not None and e.final_charges_for_suggestion >= s.max_chargeable_adds_per_suggestion:
            return "REJECTED", RecoChargeReasons.CAP_REACHED
        if s.monthly_cap is not None and e.month_to_date_amount + amount > s.monthly_cap:
            return "REJECTED", RecoChargeReasons.CAP_REACHED
        return "FINAL", None


class AttributionStates:
    _ALLOWED: dict[str, tuple[str, ...]] = {
        "PENDING": ("ACCRUED", "VOID", "REJECTED"),
        "ACCRUED": ("ACCRUED", "REVERSED", "FINAL"),
        "FINAL": ("CREDITED",),
        "REVERSED": (),
        "VOID": (),
        "REJECTED": (),
        "CREDITED": (),
    }

    @staticmethod
    def can_transition(from_state: str, to_state: str) -> bool:
        allowed = AttributionStates._ALLOWED.get(from_state)
        return allowed is not None and to_state in allowed
