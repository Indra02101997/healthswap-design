"""Typed facade over the HLD 10.2 key design (C# Cache/HotCache.cs). All hot-path reads go through here (no PostgreSQL,
no LLM). Key formats are identical to the C# implementation; GUIDs in keys use the 32-digit "N" format (`uuid.hex`).

Values are JSON written with `HotCache.to_json` (jsonutil.encode_dataclass: snake_case keys, decimals as strings,
enums by value) and read with `HotCache.from_json(type, text)`; both are also module-level `dumps` / `loads`. They
replace C# `JsonSerializer.Serialize(x, HotCache.Json)` / `JsonSerializer.Deserialize<T>(text, HotCache.Json)`, so use them
for every internal document the C# code wrote with HotCache.Json (profile_doc, swap-edge features, suggestion
alternatives and reco_fee). Fields marked json_ignore (ModelScore.normalized, C# [JsonIgnore]) are not stored: a profile
read back from the cache has normalized = None, as in C#.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from healthswap.domain.commerce.fees import RecoFeeSnapshot
from healthswap.domain.primitives import UTC, as_utc
from healthswap.domain.scoring.health_profiles import HealthProfileData
from healthswap.platform import jsonutil
from healthswap.platform.cache import ICacheStore, Ttl
from healthswap.platform.jsonutil import _decode_value, decode_dataclass, encode_dataclass

_EMPTY = uuid.UUID(int=0)
_ZERO = Decimal(0)
_MIN_UTC = datetime.min.replace(tzinfo=UTC)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------------------------------------------------
# Cached records
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkuMapEntry:
    product_id: uuid.UUID
    confidence: Decimal
    state: str


@dataclass(frozen=True, slots=True)
class SwapEdge:
    """Precomputed swap edge (product level, partner-agnostic) stored as swap:{market}:{graphVersion}:{productId}."""

    to: uuid.UUID
    health_delta: Decimal
    substitutability: Decimal
    reasons: tuple[str, ...]
    from_version: int
    to_version: int
    to_category: str
    introduced_tier_c: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InventoryItem:
    in_stock: bool
    price: Decimal
    mrp: Decimal | None
    observed_at: datetime
    available_quantity: int | None


@dataclass(frozen=True, slots=True)
class AlternativeSnapshot:
    rank: int
    partner_sku: str
    product_id: uuid.UUID
    version_id: int
    unit_price: Decimal | None
    net_quantity: Decimal | None
    net_unit: str | None
    health_delta: Decimal
    sponsored: bool
    # Ranker feature vector (float32 values, RankerFeatures.vector).
    features: list[float] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SuggestionSnapshot:
    """Everything attribution (FP-1) and RA-1 need about a served suggestion (mirrors txn.suggestion)."""

    suggestion_id: uuid.UUID = _EMPTY
    partner_id: uuid.UUID = _EMPTY
    evaluation_id: uuid.UUID | None = None
    cart_ref: str
    user_ref: str | None = None
    market: str
    store_id: str | None = None
    placement: str = "CART"
    line_id: str | None = None
    from_partner_sku: str
    from_product_id: uuid.UUID = _EMPTY
    from_version_id: int = 0
    from_qty: Decimal = _ZERO
    from_unit_price: Decimal | None = None
    from_net_quantity: Decimal | None = None
    from_net_unit: str | None = None
    from_tax_rate: Decimal | None = None
    currency: str
    alternatives: tuple[AlternativeSnapshot, ...] = ()
    reco_fee: RecoFeeSnapshot | None = None
    rules_version: str
    graph_version: str
    created_at: datetime = _MIN_UTC
    expires_at: datetime = _MIN_UTC
    attribution_expires_at: datetime = _MIN_UTC


@dataclass(frozen=True, slots=True)
class CachedExplanation:
    short: str
    bullets: tuple[str, ...]
    generator: str
    ai_generated: bool
    model_ref: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ShopperPrefsDoc:
    diet_type: str = "ANY"
    allergens: tuple[str, ...] = ()
    strict_may_contain: bool = True
    goals: tuple[str, ...] = ()
    avoid_ingredients: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()
    religious_preferences: tuple[str, ...] = ()
    locale: str | None = None
    is_minor: bool | None = None
    health_data_consent: bool = False
    personalization_consent: bool = False
    consent_notice_version: str | None = None
    updated_at: datetime = _MIN_UTC


# ---------------------------------------------------------------------------------------------------------------------
# Helpers (C# Guid "N" format, Guid.TryParse, decimal invariant formatting and NumberStyles.Number parsing)
# ---------------------------------------------------------------------------------------------------------------------


def _s(g: uuid.UUID | str) -> str:
    return (g if isinstance(g, uuid.UUID) else uuid.UUID(str(g))).hex


def _try_uuid(text: str | None) -> uuid.UUID | None:
    if text is None:
        return None
    try:
        return uuid.UUID(text.strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _decimal_text(d: Decimal) -> str:
    """decimal.ToString(CultureInfo.InvariantCulture): plain notation, scale kept, never "-0"."""
    return format(abs(d) if d.is_zero() else d, "f")


_NUMBER_RX = re.compile(r"[ \t\n\v\f\r]*([+-]?)([0-9][0-9,]*)?(?:\.([0-9]*))?([+-]?)[ \t\n\v\f\r]*")


def _try_parse_number(text: str) -> Decimal | None:
    """decimal.TryParse(text, NumberStyles.Number, CultureInfo.InvariantCulture)."""
    m = _NUMBER_RX.fullmatch(text)
    if m is None:
        return None
    lead, whole, frac, trail = m.groups()
    if not (whole or frac) or (lead and trail):
        return None
    sign = "-" if "-" in (lead + trail) else ""
    try:
        return Decimal(sign + (whole or "0").replace(",", "") + ("." + frac if frac else ""))
    except InvalidOperation:
        return None


def _parse_number(text: str) -> Decimal:
    value = _try_parse_number(text)
    if value is None:
        raise ValueError(f"Input string was not in a correct format: {text!r}")
    return value


def _unix_seconds(value: datetime) -> int:
    return (as_utc(value) - _EPOCH) // timedelta(seconds=1)


# ---------------------------------------------------------------------------------------------------------------------
# HotCache
# ---------------------------------------------------------------------------------------------------------------------


class HotCache:
    """Typed facade over the HLD 10.2 key design. All hot-path reads go through here (no PostgreSQL, no LLM)."""

    def __init__(self, store: ICacheStore) -> None:
        self._store = store

    @property
    def store(self) -> ICacheStore:
        return self._store

    # --- JSON codec (C# HotCache.Json) ---------------------------------------------------------------------------------

    @staticmethod
    def to_json(value: Any) -> str:
        """JsonSerializer.Serialize(value, HotCache.Json): dataclasses (also inside lists and dicts) as snake_case JSON."""
        return json.dumps(encode_dataclass(value), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_json(tp: Any, text: str | bytes | None) -> Any:
        """JsonSerializer.Deserialize<T>(text, HotCache.Json). `tp` is a dataclass or a generic such as list[SwapEdge];
        None or JSON null give None."""
        if text is None:
            return None
        value = jsonutil.loads(text)
        if value is None:
            return None
        if isinstance(tp, type) and dataclasses.is_dataclass(tp):
            return decode_dataclass(tp, value)
        return _decode_value(tp, value)

    # --- skumap:{partnerId}:{partnerSku} -------------------------------------------------------------------------------

    @staticmethod
    def sku_map_key(partner: uuid.UUID, sku: str) -> str:
        return f"skumap:{_s(partner)}:{sku}"

    def get_sku_maps(self, partner: uuid.UUID, skus: Sequence[str]) -> list[SkuMapEntry | None]:
        values = self._store.m_get([self.sku_map_key(partner, s) for s in skus])
        return [self.from_json(SkuMapEntry, v) for v in values]

    def set_sku_map(self, partner: uuid.UUID, sku: str, e: SkuMapEntry) -> None:
        self._store.set(self.sku_map_key(partner, sku), self.to_json(e))

    def remove_sku_map(self, partner: uuid.UUID, sku: str) -> None:
        self._store.delete(self.sku_map_key(partner, sku))

    # --- p2sku:{partnerId}:{productId} ---------------------------------------------------------------------------------

    @staticmethod
    def p2_sku_key(partner: uuid.UUID, product: uuid.UUID) -> str:
        return f"p2sku:{_s(partner)}:{_s(product)}"

    def get_partner_skus(self, partner: uuid.UUID, product: uuid.UUID) -> list[str]:
        return self._store.s_members(self.p2_sku_key(partner, product))

    def add_partner_sku(self, partner: uuid.UUID, product: uuid.UUID, sku: str) -> None:
        self._store.s_add(self.p2_sku_key(partner, product), [sku])

    def remove_partner_sku(self, partner: uuid.UUID, product: uuid.UUID, sku: str) -> None:
        self._store.s_rem(self.p2_sku_key(partner, product), [sku])

    # --- gtin:{gtin14} -> productId (GTIN fallback for lines not yet in the synced catalog) ---------------------------

    def get_product_by_gtin(self, gtin14: str) -> uuid.UUID | None:
        return _try_uuid(self._store.get("gtin:" + gtin14))

    def set_gtin(self, gtin14: str, product: uuid.UUID) -> None:
        self._store.set("gtin:" + gtin14, str(product))

    # --- hp:{market}:{productId} ---------------------------------------------------------------------------------------

    @staticmethod
    def profile_key(market: str, product: uuid.UUID) -> str:
        return f"hp:{market}:{_s(product)}"

    def get_profile(self, market: str, product: uuid.UUID) -> HealthProfileData | None:
        return self.from_json(HealthProfileData, self._store.get(self.profile_key(market, product)))

    def get_profiles(self, market: str, products: Iterable[uuid.UUID]) -> dict[uuid.UUID, HealthProfileData]:
        ids = list(dict.fromkeys(products))
        values = self._store.m_get([self.profile_key(market, p) for p in ids])
        result: dict[uuid.UUID, HealthProfileData] = {}
        for product, value in zip(ids, values, strict=True):
            hp = self.from_json(HealthProfileData, value)
            if hp is not None:
                result[product] = hp
        return result

    def set_profile(self, hp: HealthProfileData) -> None:
        self._store.set(self.profile_key(hp.market, hp.product_id), self.to_json(hp))

    # --- swap:active:{market} and swap:{market}:{graphVersion}:{productId} -------------------------------------------

    def get_active_graph(self, market: str) -> str | None:
        return self._store.get(f"swap:active:{market}")

    def set_active_graph(self, market: str, graph_version: str) -> None:
        self._store.set(f"swap:active:{market}", graph_version)

    @staticmethod
    def edges_key(market: str, graph: str, product: uuid.UUID) -> str:
        return f"swap:{market}:{graph}:{_s(product)}"

    def get_edges(self, market: str, graph: str, products: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[SwapEdge]]:
        ids = list(dict.fromkeys(products))
        values = self._store.m_get([self.edges_key(market, graph, p) for p in ids])
        result: dict[uuid.UUID, list[SwapEdge]] = {}
        for product, value in zip(ids, values, strict=True):
            edges = self.from_json(list[SwapEdge], value)
            if edges is not None:
                result[product] = edges
        return result

    def set_edges(self, market: str, graph: str, product: uuid.UUID, edges: Sequence[SwapEdge]) -> None:
        self._store.set(self.edges_key(market, graph, product), self.to_json(list(edges)))

    def delete_graph(self, market: str, graph: str) -> int:
        return self._store.delete_by_prefix(f"swap:{market}:{graph}:")

    # --- inv:{partnerId}:{storeId} -> hash partnerSku -> "inStock|price|mrp|observedUnix|qty" -------------------------

    @staticmethod
    def inventory_key(partner: uuid.UUID, store: str) -> str:
        return f"inv:{_s(partner)}:{store}"

    def get_inventory(self, partner: uuid.UUID, store_id: str, skus: Sequence[str]) -> dict[str, InventoryItem]:
        values = self._store.hm_get(self.inventory_key(partner, store_id), skus)
        result: dict[str, InventoryItem] = {}
        for sku, value in zip(skus, values, strict=True):
            item = None if value is None else self.parse_inventory(value)
            if item is not None:
                result[sku] = item
        return result

    def set_inventory(self, partner: uuid.UUID, store_id: str, entries: Iterable[tuple[str, InventoryItem]]) -> None:
        self._store.h_set(self.inventory_key(partner, store_id), [(sku, self.format_inventory(item)) for sku, item in entries])

    def get_inventory_skus(self, partner: uuid.UUID, store_id: str) -> list[str]:
        return list(self._store.h_get_all(self.inventory_key(partner, store_id)))

    @staticmethod
    def format_inventory(i: InventoryItem) -> str:
        return "|".join((
            "1" if i.in_stock else "0",
            _decimal_text(i.price),
            "" if i.mrp is None else _decimal_text(i.mrp),
            str(_unix_seconds(i.observed_at)),
            "" if i.available_quantity is None else str(i.available_quantity),
        ))

    @staticmethod
    def parse_inventory(v: str) -> InventoryItem | None:
        p = v.split("|")
        if len(p) < 4:
            return None
        return InventoryItem(
            p[0] == "1",
            _parse_number(p[1]),
            _parse_number(p[2]) if len(p[2]) > 0 else None,
            datetime.fromtimestamp(int(p[3]), UTC),
            int(p[4]) if len(p) > 4 and len(p[4]) > 0 else None,
        )

    # --- expl:{market}:{locale}:{from}:{to}:{factsHash} ---------------------------------------------------------------

    @staticmethod
    def explanation_key(market: str, locale: str, from_: uuid.UUID, to: uuid.UUID, facts_hash: str) -> str:
        return f"expl:{market}:{locale}:{_s(from_)}:{_s(to)}:{facts_hash}"

    def get_explanation(self, market: str, locale: str, from_: uuid.UUID, to: uuid.UUID, facts_hash: str) -> CachedExplanation | None:
        return self.from_json(CachedExplanation, self._store.get(self.explanation_key(market, locale, from_, to, facts_hash)))

    def set_explanation(self, market: str, locale: str, from_: uuid.UUID, to: uuid.UUID, facts_hash: str, e: CachedExplanation) -> None:
        self._store.set(self.explanation_key(market, locale, from_, to, facts_hash), self.to_json(e))

    # --- pref:{partnerId}:{userRef} (TTL 30 days; PostgreSQL is the source of truth) ----------------------------------

    PREFS_TTL = timedelta(days=30)

    @staticmethod
    def prefs_key(partner: uuid.UUID, user_ref: str) -> str:
        return f"pref:{_s(partner)}:{user_ref}"

    def get_prefs(self, partner: uuid.UUID, user_ref: str) -> ShopperPrefsDoc | None:
        return self.from_json(ShopperPrefsDoc, self._store.get(self.prefs_key(partner, user_ref)))

    def set_prefs(self, partner: uuid.UUID, user_ref: str, prefs: ShopperPrefsDoc) -> None:
        self._store.set(self.prefs_key(partner, user_ref), self.to_json(prefs), self.PREFS_TTL)

    def delete_prefs(self, partner: uuid.UUID, user_ref: str) -> None:
        self._store.delete(self.prefs_key(partner, user_ref))

    # --- dismiss:{partnerId}:{userRef}:{fromProductId}:{toProductId} - R8 fatigue cool-down ---------------------------

    @staticmethod
    def dismiss_key(partner: uuid.UUID, user_ref: str, from_: uuid.UUID, to: uuid.UUID) -> str:
        return f"dismiss:{_s(partner)}:{user_ref}:{_s(from_)}:{_s(to)}"

    def get_dismissed(self, partner: uuid.UUID, user_ref: str, pairs: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> set[tuple[uuid.UUID, uuid.UUID]]:
        values = self._store.m_get([self.dismiss_key(partner, user_ref, f, t) for f, t in pairs])
        return {(f, t) for (f, t), v in zip(pairs, values, strict=True) if v is not None}

    def set_dismissed(self, partner: uuid.UUID, user_ref: str, from_: uuid.UUID, to: uuid.UUID, ttl: Ttl) -> None:
        self._store.set(self.dismiss_key(partner, user_ref, from_, to), "1", ttl)

    def delete_user_keys(self, partner: uuid.UUID, user_ref: str) -> int:
        return self._store.delete_by_prefix(f"dismiss:{_s(partner)}:{user_ref}:")

    # --- reco:{partnerId}:{cartRef} -> hash partnerSku -> suggestionId (cart lines holding a recommended product) -----

    @staticmethod
    def cart_reco_key(partner: uuid.UUID, cart_ref: str) -> str:
        return f"reco:{_s(partner)}:{cart_ref}"

    def get_cart_reco_labels(self, partner: uuid.UUID, cart_ref: str) -> dict[str, uuid.UUID]:
        labels = ((sku, _try_uuid(v)) for sku, v in self._store.h_get_all(self.cart_reco_key(partner, cart_ref)).items())
        return {sku: sid for sku, sid in labels if sid is not None}

    def add_cart_reco_label(self, partner: uuid.UUID, cart_ref: str, sku: str, suggestion_id: uuid.UUID, ttl: Ttl) -> None:
        self._store.h_set(self.cart_reco_key(partner, cart_ref), [(sku, str(suggestion_id))], ttl)

    def remove_cart_reco_label(self, partner: uuid.UUID, cart_ref: str, sku: str) -> None:
        self._store.h_del(self.cart_reco_key(partner, cart_ref), [sku])

    # --- sug:{suggestionId} - snapshot available to the event collector before the async database write lands --------

    def get_suggestion(self, suggestion_id: uuid.UUID) -> SuggestionSnapshot | None:
        return self.from_json(SuggestionSnapshot, self._store.get("sug:" + _s(suggestion_id)))

    def set_suggestion(self, s: SuggestionSnapshot, ttl: Ttl) -> None:
        self._store.set("sug:" + _s(s.suggestion_id), self.to_json(s), ttl)

    # --- eval:{partnerId}:{evaluationId} - GET /cart-evaluations/{id} until expiresAt ---------------------------------

    def get_evaluation_json(self, partner: uuid.UUID, evaluation_id: uuid.UUID) -> str | None:
        return self._store.get(f"eval:{_s(partner)}:{_s(evaluation_id)}")

    def set_evaluation_json(self, partner: uuid.UUID, evaluation_id: uuid.UUID, json_text: str, ttl: Ttl) -> None:
        self._store.set(f"eval:{_s(partner)}:{_s(evaluation_id)}", json_text, ttl)

    # --- cat:{market}:{categoryId} -> HSI p60 (target-line ceiling); catmap:{market}:{fromCategory} -> approved targets

    def get_category_ceilings(self, market: str, categories: Collection[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        ids = list(dict.fromkeys(categories))
        values = self._store.m_get([f"cat:{market}:{_s(c)}" for c in ids])
        result: dict[uuid.UUID, Decimal] = {}
        for category, value in zip(ids, values, strict=True):
            d = None if value is None else _try_parse_number(value)
            if d is not None:
                result[category] = d
        return result

    def set_category_ceiling(self, market: str, category: uuid.UUID, ceiling: Decimal) -> None:
        self._store.set(f"cat:{market}:{_s(category)}", _decimal_text(ceiling))

    def get_category_map(self, market: str, from_category: str) -> list[str]:
        return self._store.s_members(f"catmap:{market}:{from_category}")

    def set_category_map(self, market: str, from_category: str, to_categories: Sequence[str]) -> None:
        self._store.s_add(f"catmap:{market}:{from_category}", list(to_categories))


# C# HotCache.Json helpers as module functions.
dumps = HotCache.to_json
loads = HotCache.from_json
