"""Versioned per-partner configuration document (HLD section 18 + RA-1), stored in tenant.partner.config (Commerce/PartnerConfig.cs).

`from_dict` follows the C# System.Text.Json settings used for this document (HsJson: web defaults, strict numbers):
property names match case-insensitively, unknown properties are ignored, missing properties keep the defaults and a
present property replaces the default (lists and maps are replaced, not merged). Decimals are JSON numbers; the fields
that C# marks [JsonNumberHandling(AllowReadingFromString)] (fee caps, recommendationAdd.amount/monthlyCap,
priceGuards.maxAbsPremiumPerUnit) also accept decimal strings such as "50.00". Anything C# would reject raises ValueError.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

Obj = TypeVar("Obj")

_JSON_NUMBER_RX = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------------------------------------------------
# Configuration records (defaults are the C# initializers)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FeeCaps:
    per_line: Decimal | None = Decimal("50")
    per_order: Decimal | None = Decimal("120")
    per_month: Decimal | None = None
    currency: str = "INR"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecommendationAddFeeConfig:
    """RA-1: fixed fee every time a shopper adds a HealthSwap-recommended (labeled) product to the cart."""

    enabled: bool = True
    rule: str = "RA-1"
    amount: Decimal = Decimal("1.00")
    currency: str = "INR"
    # PER_ADD_EVENT (default: one fee per add action) or PER_UNIT (fee x units added by the action).
    basis: str = "PER_ADD_EVENT"
    # An UNDO within this many seconds of the add voids the charge (matches the 5-second undo in the UX guidelines).
    undo_grace_seconds: int = 5
    # Charges become FINAL after this window so that late, batched events (impressions, undos) are considered.
    settle_after_seconds: int = 600
    # The labeled recommendation must have been shown (IMPRESSION) for an add to be billable.
    require_impression: bool = True
    # Clock-skew tolerance between client-side IMPRESSION and add timestamps.
    impression_tolerance_seconds: int = 5
    charge_accept: bool = True
    max_chargeable_adds_per_suggestion: int | None = None
    monthly_cap: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeeConfig:
    # FP-1 rate: share of the fair premium (0 disables the premium fee).
    rate: Decimal = Decimal("0.10")
    rule: str = "FP-1"
    caps: FeeCaps = field(default_factory=FeeCaps)
    # SAME_AS_PRICE or TAX_EXCLUSIVE (convert tax-inclusive prices with the line taxRate before computing FP-1).
    fee_basis: str = "SAME_AS_PRICE"
    recommendation_add: RecommendationAddFeeConfig = field(default_factory=RecommendationAddFeeConfig)


@dataclass(frozen=True, slots=True, kw_only=True)
class PriceGuards:
    max_normalized_price_ratio: Decimal = Decimal("1.75")
    max_abs_premium_per_unit: Decimal = Decimal("150")


@dataclass(frozen=True, slots=True, kw_only=True)
class PackSizeRatio:
    min: Decimal = Decimal("0.5")
    max: Decimal = Decimal("2.0")


@dataclass(frozen=True, slots=True, kw_only=True)
class SponsoredConfig:
    allowed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentConfig:
    name: str = "default"
    # Share of carts (by stable hash of userRef or cartRef) that get no suggestions (holdout arm).
    holdout_percent: int = 0


def _default_return_window_days() -> dict[str, int]:
    return {"QUICK_COMMERCE": 2, "SCHEDULED_DELIVERY": 7, "PICKUP": 2}


@dataclass(frozen=True, slots=True, kw_only=True)
class PartnerConfig:
    """Versioned per-partner configuration document (HLD section 18 + RA-1). Stored in tenant.partner.config."""

    markets: tuple[str, ...] = ("IN",)
    currency: str = "INR"
    fee: FeeConfig = field(default_factory=FeeConfig)
    # Output tax on the service invoice (for example 18% GST in India; 0 for most US B2B service fees).
    tax_rate: Decimal = Decimal("0.18")
    payment_terms_days: int = 30
    dispute_window_days: int = 15
    return_window_days: dict[str, int] = field(default_factory=_default_return_window_days)
    attribution_window_hours: int = 24
    placements: tuple[str, ...] = ("CART", "CHECKOUT", "PDP", "SEARCH", "AGENT")
    max_suggestions_per_cart: int = 3
    max_alternatives_per_line: int = 2
    price_guards: PriceGuards = field(default_factory=PriceGuards)
    # TAX_INCLUSIVE (India MRP basis) or TAX_EXCLUSIVE; applies to cart, inventory and order prices.
    price_basis: str = "TAX_INCLUSIVE"
    pack_size_ratio: PackSizeRatio = field(default_factory=PackSizeRatio)
    availability_mode: str = "MIRROR"
    inventory_staleness_seconds: int = 600
    scoring_profile: str = "IN_DEFAULT_V1"
    sponsored: SponsoredConfig = field(default_factory=SponsoredConfig)
    blocked_brands: tuple[str, ...] = ()
    excluded_categories: tuple[str, ...] = ("infant-foods", "alcohol", "supplements")
    minors_policy: str = "STRICT"
    locales: tuple[str, ...] = ("en-IN", "hi-IN")
    kill_switch: bool = False
    disabled_placements: tuple[str, ...] = ()
    experiment: ExperimentConfig = field(default_factory=ExperimentConfig)
    exploration_rate: Decimal = Decimal("0.05")
    max_lines: int = 200

    def return_window_for(self, fulfillment_type: str | None) -> int:
        if fulfillment_type is not None and fulfillment_type in self.return_window_days:
            return self.return_window_days[fulfillment_type]
        return max(self.return_window_days.values(), default=7)

    def validate(self) -> list[str]:
        errors: list[str] = []
        ra = self.fee.recommendation_add
        if len(self.markets) == 0:
            errors.append("markets must not be empty")
        if _utf16_len(self.currency) != 3:
            errors.append("currency must be ISO 4217")
        if self.fee.rate < 0 or self.fee.rate > 1:
            errors.append("fee.rate must be between 0 and 1")
        if ra.amount < 0:
            errors.append("fee.recommendationAdd.amount must be >= 0")
        if ra.basis not in ("PER_ADD_EVENT", "PER_UNIT"):
            errors.append("fee.recommendationAdd.basis must be PER_ADD_EVENT or PER_UNIT")
        if _utf16_len(ra.currency) != 3:
            errors.append("fee.recommendationAdd.currency must be ISO 4217")
        if ra.undo_grace_seconds < 0 or ra.undo_grace_seconds > 3600:
            errors.append("fee.recommendationAdd.undoGraceSeconds must be 0-3600")
        if ra.settle_after_seconds < ra.undo_grace_seconds:
            errors.append("fee.recommendationAdd.settleAfterSeconds must be >= undoGraceSeconds")
        if self.tax_rate < 0 or self.tax_rate > 1:
            errors.append("taxRate must be between 0 and 1")
        if self.max_suggestions_per_cart < 0 or self.max_suggestions_per_cart > 10:
            errors.append("maxSuggestionsPerCart must be 0-10")
        if self.max_alternatives_per_line < 1 or self.max_alternatives_per_line > 5:
            errors.append("maxAlternativesPerLine must be 1-5")
        if self.price_guards.max_normalized_price_ratio <= 0:
            errors.append("priceGuards.maxNormalizedPriceRatio must be > 0")
        if self.availability_mode not in ("MIRROR", "PARTNER_FILTER"):
            errors.append("availabilityMode must be MIRROR or PARTNER_FILTER")
        if self.minors_policy not in ("STRICT", "STANDARD"):
            errors.append("minorsPolicy must be STRICT or STANDARD")
        if self.experiment.holdout_percent < 0 or self.experiment.holdout_percent > 100:
            errors.append("experiment.holdoutPercent must be 0-100")
        return errors

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> PartnerConfig:
        """Reads the camelCase JSON document (for example json.loads of tenant.partner.config). Raises ValueError."""
        return _read(PartnerConfig, d, "", _PARTNER_CONFIG)

    def to_dict(self) -> dict[str, Any]:
        """camelCase JSON-compatible document; money-like decimals are strings, other decimals numbers, None is null."""
        return _write(self, _PARTNER_CONFIG)


# ---------------------------------------------------------------------------------------------------------------------
# JSON mapping
# ---------------------------------------------------------------------------------------------------------------------


def _fail(path: str, expected: str, value: Any) -> ValueError:
    return ValueError(f"$.{path}: the JSON value {value!r} could not be converted to {expected}")


def _decimal(value: Any, path: str, allow_string: bool = False, nullable: bool = False) -> Decimal | None:
    if value is None:
        if nullable:
            return None
        raise _fail(path, "a decimal", value)
    if isinstance(value, bool):
        raise _fail(path, "a decimal", value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _fail(path, "a decimal", value)
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail(path, "a decimal", value)
        return Decimal(repr(value))
    if isinstance(value, str) and allow_string and _JSON_NUMBER_RX.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation:
            pass
    raise _fail(path, "a decimal", value)


def _int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not _INT32_MIN <= value <= _INT32_MAX:
        raise _fail(path, "an Int32", value)
    return value


def _int_or_none(value: Any, path: str) -> int | None:
    return None if value is None else _int(value, path)


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, "a Boolean", value)
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise _fail(path, "a String", value)
    return value


def _str_list(value: Any, path: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _fail(path, "a list of strings", value)
    return tuple(_str(item, f"{path}[{i}]") for i, item in enumerate(value))


def _int_map(value: Any, path: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _fail(path, "an object of integers", value)
    return {_str(key, path): _int(item, f"{path}.{key}") for key, item in value.items()}


def _number_out(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() and value.as_tuple().exponent == 0 else float(value)


def _money_out(value: Decimal | None) -> str | None:
    # Like C# decimal formatting, a negative zero is written without its sign.
    return None if value is None else format(abs(value) if value.is_zero() else value, "f")


@dataclass(frozen=True, slots=True)
class _Prop:
    json: str
    attr: str
    read: Callable[[Any, str], Any]
    write: Callable[[Any], Any]


def _read(cls: Callable[..., Obj], data: Any, path: str, props: tuple[_Prop, ...]) -> Obj:
    if not isinstance(data, Mapping):
        raise _fail(path or "(root)", "an object", data)
    # PropertyNameCaseInsensitive: the last of several case variants wins; unknown properties are ignored.
    values = {str(k).lower(): v for k, v in data.items()}
    kwargs: dict[str, Any] = {}
    for prop in props:
        key = prop.json.lower()
        if key in values:
            kwargs[prop.attr] = prop.read(values[key], f"{path}.{prop.json}" if path else prop.json)
    return cls(**kwargs)


def _write(obj: object, props: tuple[_Prop, ...]) -> dict[str, Any]:
    return {prop.json: prop.write(getattr(obj, prop.attr)) for prop in props}


def _number_prop(json: str, attr: str) -> _Prop:
    return _Prop(json, attr, lambda v, p: _decimal(v, p), _number_out)


def _money_prop(json: str, attr: str, nullable: bool = False) -> _Prop:
    return _Prop(json, attr, lambda v, p: _decimal(v, p, allow_string=True, nullable=nullable), _money_out)


def _int_prop(json: str, attr: str, nullable: bool = False) -> _Prop:
    return _Prop(json, attr, _int_or_none if nullable else _int, lambda v: v)


def _bool_prop(json: str, attr: str) -> _Prop:
    return _Prop(json, attr, _bool, lambda v: v)


def _str_prop(json: str, attr: str) -> _Prop:
    return _Prop(json, attr, _str, lambda v: v)


def _list_prop(json: str, attr: str) -> _Prop:
    return _Prop(json, attr, _str_list, list)


def _object_prop(json: str, attr: str, cls: Callable[..., Any], props: tuple[_Prop, ...]) -> _Prop:
    return _Prop(json, attr, lambda v, p: _read(cls, v, p, props), lambda v: _write(v, props))


_FEE_CAPS = (
    _money_prop("perLine", "per_line", nullable=True),
    _money_prop("perOrder", "per_order", nullable=True),
    _money_prop("perMonth", "per_month", nullable=True),
    _str_prop("currency", "currency"),
)
_RECOMMENDATION_ADD = (
    _bool_prop("enabled", "enabled"),
    _str_prop("rule", "rule"),
    _money_prop("amount", "amount"),
    _str_prop("currency", "currency"),
    _str_prop("basis", "basis"),
    _int_prop("undoGraceSeconds", "undo_grace_seconds"),
    _int_prop("settleAfterSeconds", "settle_after_seconds"),
    _bool_prop("requireImpression", "require_impression"),
    _int_prop("impressionToleranceSeconds", "impression_tolerance_seconds"),
    _bool_prop("chargeAccept", "charge_accept"),
    _int_prop("maxChargeableAddsPerSuggestion", "max_chargeable_adds_per_suggestion", nullable=True),
    _money_prop("monthlyCap", "monthly_cap", nullable=True),
)
_FEE = (
    _number_prop("rate", "rate"),
    _str_prop("rule", "rule"),
    _object_prop("caps", "caps", FeeCaps, _FEE_CAPS),
    _str_prop("feeBasis", "fee_basis"),
    _object_prop("recommendationAdd", "recommendation_add", RecommendationAddFeeConfig, _RECOMMENDATION_ADD),
)
_PRICE_GUARDS = (
    _number_prop("maxNormalizedPriceRatio", "max_normalized_price_ratio"),
    _money_prop("maxAbsPremiumPerUnit", "max_abs_premium_per_unit"),
)
_PACK_SIZE_RATIO = (_number_prop("min", "min"), _number_prop("max", "max"))
_SPONSORED = (_bool_prop("allowed", "allowed"),)
_EXPERIMENT = (_str_prop("name", "name"), _int_prop("holdoutPercent", "holdout_percent"))
_PARTNER_CONFIG = (
    _list_prop("markets", "markets"),
    _str_prop("currency", "currency"),
    _object_prop("fee", "fee", FeeConfig, _FEE),
    _number_prop("taxRate", "tax_rate"),
    _int_prop("paymentTermsDays", "payment_terms_days"),
    _int_prop("disputeWindowDays", "dispute_window_days"),
    _Prop("returnWindowDays", "return_window_days", _int_map, dict),
    _int_prop("attributionWindowHours", "attribution_window_hours"),
    _list_prop("placements", "placements"),
    _int_prop("maxSuggestionsPerCart", "max_suggestions_per_cart"),
    _int_prop("maxAlternativesPerLine", "max_alternatives_per_line"),
    _object_prop("priceGuards", "price_guards", PriceGuards, _PRICE_GUARDS),
    _str_prop("priceBasis", "price_basis"),
    _object_prop("packSizeRatio", "pack_size_ratio", PackSizeRatio, _PACK_SIZE_RATIO),
    _str_prop("availabilityMode", "availability_mode"),
    _int_prop("inventoryStalenessSeconds", "inventory_staleness_seconds"),
    _str_prop("scoringProfile", "scoring_profile"),
    _object_prop("sponsored", "sponsored", SponsoredConfig, _SPONSORED),
    _list_prop("blockedBrands", "blocked_brands"),
    _list_prop("excludedCategories", "excluded_categories"),
    _str_prop("minorsPolicy", "minors_policy"),
    _list_prop("locales", "locales"),
    _bool_prop("killSwitch", "kill_switch"),
    _list_prop("disabledPlacements", "disabled_placements"),
    _object_prop("experiment", "experiment", ExperimentConfig, _EXPERIMENT),
    _number_prop("explorationRate", "exploration_rate"),
    _int_prop("maxLines", "max_lines"),
)
