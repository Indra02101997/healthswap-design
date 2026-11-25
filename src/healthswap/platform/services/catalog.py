"""Services/CatalogServices.cs ported: partner catalog sync and matching (CatalogService), asynchronous enrichment
(CatalogEnrichment), bulk feeds (FeedService), the store inventory mirror (InventoryService), shopper preferences with
consent (PreferenceService) and erasure (PrivacyService).

Requests and results are camelCase dicts shaped like api/openapi.yaml (the C# Contracts records serialized with HsJson:
camelCase, C# property order, None members omitted); ids are strings, timestamps ISO 8601 strings, numbers Decimal.

Catalog items are read like System.Text.Json read HealthSwap.Contracts.CatalogItem with HsJson.Options (`read_catalog_item`):
case-insensitive property names, unknown properties ignored, strict JSON types, record defaults (NutritionPanel.basis
"PER_100G", NetQuantityDto.value 0), and the required partnerSku and title. The result is a dict with the C# property
order, so `netjson.serialize_hs(item)` is the JSON the C# implementation stored as the partner-feed source record.
Messages of format errors follow System.Text.Json's wording without its line/byte positions.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import uuid
import zlib
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from healthswap.domain.knowledge.parsing import is_null_or_white_space
from healthswap.domain.knowledge.products import Basis, Nutrition, round_decimal
from healthswap.domain.knowledge.quality import EntityResolution
from healthswap.domain.primitives import (
    Clock,
    NetQuantity,
    as_utc,
    gtin_is_restricted_circulation,
    gtin_try_normalize,
    iso,
    new_id,
    parse_iso,
    to_decimal,
    try_parse_amount,
)
from healthswap.domain.text import _utf8
from healthswap.platform import jsonutil, netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.hot_cache import HotCache, InventoryItem, ShopperPrefsDoc, SkuMapEntry, _parse_number
from healthswap.platform.knowledge.connectors import ProductCandidate
from healthswap.platform.knowledge.ingestor import ProductIngestor
from healthswap.platform.knowledge.jobs import ScoringJob
from healthswap.platform.ml.registry import ModelRegistry
from healthswap.platform.services.infrastructure import CryptographicError, DataProtectionProvider, WebhookFanoutConsumer, WebhookTypes
from healthswap.platform.settings import FeedOptions
from healthswap.platform.tenancy import PartnerDirectory

log = logging.getLogger(__name__)

_ZERO = Decimal(0)


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drops None members (JsonIgnoreCondition.WhenWritingNull)."""
    return {k: v for k, v in d.items() if v is not None}


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _try_uuid(value: Any) -> uuid.UUID | None:
    """Guid.TryParse."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _datetime(value: Any) -> datetime:
    """A DateTime from an API value (datetime or RFC 3339 text)."""
    return as_utc(value) if isinstance(value, datetime) else parse_iso(str(value))


# ---------------------------------------------------------------------------------------------------------------------
# CatalogItem (HealthSwap.Contracts) as System.Text.Json reads it with HsJson.Options
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Record:
    clr: str
    fields: tuple[tuple[str, Any], ...]
    defaults: Mapping[str, Any]
    required: tuple[str, ...] = ()


_STR, _DEC, _DEC_N, _INT_N, _BOOL_N, _STRS, _MAP = "str", "dec", "dec?", "int?", "bool?", "str[]", "map"
_CLR = {
    _STR: "System.String", _DEC: "System.Decimal", _DEC_N: "System.Nullable`1[System.Decimal]", _INT_N: "System.Nullable`1[System.Int32]",
    _BOOL_N: "System.Nullable`1[System.Boolean]", _STRS: "System.Collections.Generic.List`1[System.String]",
    _MAP: "System.Collections.Generic.Dictionary`2[System.String,System.String]",
}

_NET_QUANTITY = _Record("HealthSwap.Contracts.NetQuantityDto", (("value", _DEC), ("unit", _STR)), {"value": _ZERO})
_IMAGE = _Record("HealthSwap.Contracts.CatalogImage", (("url", _STR), ("kind", _STR)), {})
_OTHER_NUTRIENT = _Record("HealthSwap.Contracts.OtherNutrient", (("code", _STR), ("amount", _DEC), ("unit", _STR)), {"amount": _ZERO})
_NUTRITION_PANEL = _Record(
    "HealthSwap.Contracts.NutritionPanel",
    (
        ("basis", _STR), ("servingSize", _NET_QUANTITY), ("energyKcal", _DEC_N), ("proteinG", _DEC_N), ("carbohydrateG", _DEC_N),
        ("totalSugarsG", _DEC_N), ("addedSugarsG", _DEC_N), ("totalFatG", _DEC_N), ("saturatedFatG", _DEC_N), ("transFatG", _DEC_N),
        ("cholesterolMg", _DEC_N), ("sodiumMg", _DEC_N), ("saltG", _DEC_N), ("dietaryFibreG", _DEC_N), ("other", [_OTHER_NUTRIENT]),
    ),
    {"basis": "PER_100G"},
)
_CATALOG_ITEM = _Record(
    "HealthSwap.Contracts.CatalogItem",
    (
        ("partnerSku", _STR), ("gtin", _STR), ("title", _STR), ("brand", _STR), ("categoryPath", _STRS), ("netQuantity", _NET_QUANTITY),
        ("packCount", _INT_N), ("physicalState", _STR), ("markets", _STRS), ("images", [_IMAGE]), ("ingredientsText", _STR),
        ("allergenStatement", _STR), ("nutrition", [_NUTRITION_PANEL]), ("dietaryMarks", _STRS), ("fssaiLicenseNumbers", _STRS),
        ("isPrivateLabel", _BOOL_N), ("isListed", _BOOL_N), ("attributes", _MAP),
    ),
    {},
    ("partnerSku", "title"),
)
_INT32 = (-(2**31), 2**31 - 1)


def _conversion_error(clr: str, path: str) -> ValueError:
    return ValueError(f"The JSON value could not be converted to {clr}. Path: {path}.")


def _number(value: Any) -> bool:
    return isinstance(value, (int, Decimal, float)) and not isinstance(value, bool)


def _read(spec: Any, value: Any, path: str) -> Any:
    if isinstance(spec, _Record):
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise _conversion_error(spec.clr, path)
        by_name = {name.lower(): (name, tp) for name, tp in spec.fields}
        found: dict[str, Any] = {}
        for key, item in value.items():
            match = by_name.get(str(key).lower())
            if match is not None:  # unknown properties are skipped; duplicates: the last one wins
                found[match[0]] = _read(match[1], item, f"{path}.{key}")
        missing = [name for name in spec.required if name not in found]
        if missing:
            names = ", ".join(f"'{m}'" for m in missing)
            raise ValueError(f"JSON deserialization for type '{spec.clr}' was missing required properties including: {names}.")
        result: dict[str, Any] = {}
        for name, _ in spec.fields:
            v = found[name] if name in found else spec.defaults.get(name)
            if v is not None:
                result[name] = v
        return result
    if isinstance(spec, list):
        if value is None:
            return None
        if not isinstance(value, list):
            raise _conversion_error(f"System.Collections.Generic.List`1[{spec[0].clr}]", path)
        return [_read(spec[0], v, f"{path}[{i}]") for i, v in enumerate(value)]
    if value is None:
        if spec == _DEC:
            raise _conversion_error(_CLR[spec], path)
        return None
    if spec == _STR:
        if isinstance(value, str):
            return value
    elif spec in (_DEC, _DEC_N):
        if _number(value):
            return to_decimal(value)
    elif spec == _INT_N:
        if isinstance(value, int) and not isinstance(value, bool) and _INT32[0] <= value <= _INT32[1]:
            return value
        if isinstance(value, float) and value.is_integer() and _INT32[0] <= value <= _INT32[1]:
            return int(value)
    elif spec == _BOOL_N:
        if isinstance(value, bool):
            return value
    elif spec == _STRS:
        if isinstance(value, list):
            return [_read(_STR, v, f"{path}[{i}]") for i, v in enumerate(value)]
    elif spec == _MAP:
        if isinstance(value, Mapping):
            return {str(k): _read(_STR, v, f"{path}.{k}") for k, v in value.items()}
    raise _conversion_error(_CLR[spec], path)


def read_catalog_item(value: Any) -> dict[str, Any] | None:
    """A CatalogItem dict (C# property order, defaults applied, None members omitted) from a parsed JSON value; None for JSON
    null. Raises ValueError like System.Text.Json raised JsonException. Idempotent on its own results."""
    item = _read(_CATALOG_ITEM, value, "$")
    if item is not None:
        for name in ("partnerSku", "title"):
            if item.get(name) is None:
                raise ValueError(f"{name} is required")
    return item


def _reject_constant(name: str) -> Any:
    raise ValueError(f"'{name[:1]}' is an invalid start of a value.")


_JSON = json.JSONDecoder(parse_float=Decimal, parse_constant=_reject_constant)


def _api_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """An API request item (already validated against the OpenAPI contract) in canonical form; 400 when malformed."""
    try:
        result = read_catalog_item(item)
    except ValueError as ex:
        raise HsError.invalid(str(ex)) from None
    if result is None:
        raise HsError.invalid("Catalog items must not be null")
    return result


# ---------------------------------------------------------------------------------------------------------------------
# CatalogService
# ---------------------------------------------------------------------------------------------------------------------

_ITEM_SQL = (
    "select partner_sku, product_id, match_method, match_confidence, match_state, is_listed, updated_at from tenant.catalog_item "
    "where partner_id = @partnerId and partner_sku = @sku"
)


def _match(state: str, method: str, confidence: Decimal, product_id: uuid.UUID | None, sufficiency: str) -> dict[str, Any]:
    """MatchResult (State, Method, Confidence, ProductId, DataSufficiency)."""
    return _compact({
        "state": state, "method": method, "confidence": confidence, "productId": None if product_id is None else str(product_id),
        "dataSufficiency": sufficiency,
    })


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    # CatalogBatchResult: {"batchId", "received", "accepted", "rejected", "results"}.
    result: dict[str, Any]
    # Items whose label data must enrich the knowledge base (PENDING_ENRICHMENT): (catalog item, market).
    needs_enrichment: list[tuple[dict[str, Any], str]]


@dataclass(slots=True)
class _CacheOp:
    sku: str
    product: uuid.UUID | None
    confidence: Decimal
    state: str
    listed: bool
    previous: uuid.UUID | None


class CatalogService:
    """Partner catalog sync and matching to canonical products (HLD 6.2: GTIN exact, GTIN normalized, fuzzy, enrichment)."""

    def __init__(self, db: Db, cache: HotCache, partners: PartnerDirectory, models: ModelRegistry, clock: Clock) -> None:
        self.db = db
        self.cache = cache
        self.partners = partners
        self.models = models
        self.clock = clock

    def upsert(self, partner_id: uuid.UUID, items: Sequence[Mapping[str, Any]], feed_id: uuid.UUID | None = None) -> UpsertOutcome:
        partner_id = _uuid(partner_id)
        items = [_api_item(i) for i in items]
        partner = self.partners.get(partner_id)
        default_market = partner.markets[0]
        results: list[dict[str, Any]] = []
        cache_ops: list[_CacheOp] = []
        review: list[dict[str, Any]] = []
        enrichment: list[tuple[dict[str, Any], str]] = []
        now = self.clock.utc_now()

        def work(u: Uow) -> None:
            for item in items:
                sku = item["partnerSku"]
                errors: list[FieldError] = []
                gtin14: str | None = None
                gtin_normalized = False
                raw = item.get("gtin")
                if raw:
                    g = gtin_try_normalize(raw)
                    if g is None:
                        errors.append(FieldError("/gtin", "Invalid GTIN check digit or length", "HS-INVALID-GTIN"))
                    else:
                        gtin14 = None if gtin_is_restricted_circulation(g) else g
                        gtin_normalized = len(raw) in (8, 12)
                markets = item.get("markets") or [default_market]
                if any(x not in partner.markets for x in markets):
                    errors.append(FieldError("/markets", "Market not enabled for this partner", "HS-UNSUPPORTED-MARKET"))
                if errors:
                    results.append({"partnerSku": sku, "status": "REJECTED", "errors": [e.to_api() for e in errors]})
                    continue

                market = markets[0]
                previous = u.one(_ITEM_SQL, {"partnerId": partner_id, "sku": sku})
                product_id: uuid.UUID | None = None
                method = "NONE"
                confidence = _ZERO
                if previous is not None and previous["match_state"] in ("CONFIRMED", "REJECTED"):
                    # Manual decisions are sticky until the partner overrides them again.
                    product_id = previous["product_id"]
                    method = previous["match_method"] or "MANUAL"
                    confidence = Decimal(1) if previous["match_confidence"] is None else previous["match_confidence"]
                    state = previous["match_state"]
                else:
                    if gtin14 is not None:
                        product_id = self.cache.get_product_by_gtin(gtin14) or u.scalar(
                            "select product_id from kb.product where primary_gtin = @gtin14 union select product_id from kb.product_identifier where id_type = 'GTIN' and id_value = @gtin14 limit 1",
                            {"gtin14": gtin14},
                        )
                        if product_id is not None:
                            method = "GTIN_NORMALIZED" if gtin_normalized else "GTIN_EXACT"
                            confidence = Decimal("0.99") if gtin_normalized else Decimal("1.0")
                    if product_id is None:
                        net = item.get("netQuantity")
                        size = NetQuantity.normalize(None if net is None else net.get("value"), None if net is None else net.get("unit"))
                        best_score, best_row = _ZERO, None
                        for f in u.query(
                            """
                            select p.product_id, p.name, b.name as brand, p.net_quantity, p.net_quantity_unit, p.primary_gtin, similarity(p.name, @title) as sim
                            from kb.product p left join kb.brand b on b.brand_id = p.brand_id
                            where p.status = 'ACTIVE' and @market = any(p.markets) and similarity(p.name, @title) > 0.25
                            order by sim desc limit 5
                            """,
                            {"title": item["title"], "market": market},
                        ):
                            other_gtin = None if gtin14 is None or f["primary_gtin"] is None else f["primary_gtin"].strip()
                            features = EntityResolution.features(
                                EntityResolution.Candidate(item["title"], item.get("brand"), size, None, (), gtin14),
                                EntityResolution.Candidate(f["name"], f["brand"], NetQuantity.normalize(f["net_quantity"], f["net_quantity_unit"]), None, (), other_gtin),
                            )
                            score = self.models.score_entity_match(features)
                            if score > best_score:
                                best_score, best_row = score, f
                        if best_row is not None and best_score >= EntityResolution.REVIEW_THRESHOLD:
                            product_id = best_row["product_id"]
                            method = "FUZZY"
                            confidence = round_decimal(best_score, 3)
                    if product_id is None:
                        state = "PENDING_ENRICHMENT" if item.get("nutrition") and item.get("ingredientsText") is not None else "UNMATCHED"
                    else:
                        state = "AUTO_MATCHED" if confidence >= EntityResolution.AUTO_MERGE_THRESHOLD else "NEEDS_REVIEW"

                listed = True if item.get("isListed") is None else item["isListed"]
                net = item.get("netQuantity")
                u.execute(
                    """
                    insert into tenant.catalog_item (partner_id, partner_sku, gtin, title, brand_text, category_path, net_quantity, net_quantity_unit, product_id,
                        match_method, match_confidence, match_state, raw_payload_uri, is_listed, updated_at)
                    values (@partnerId, @sku, @gtin14, @title, @brand, @path::text[], @netValue, @netUnit, @productId, @method, @confidence, @state, @uri, @listed, @now)
                    on conflict (partner_id, partner_sku) do update set gtin = excluded.gtin, title = excluded.title, brand_text = excluded.brand_text,
                        category_path = excluded.category_path, net_quantity = excluded.net_quantity, net_quantity_unit = excluded.net_quantity_unit,
                        product_id = excluded.product_id, match_method = excluded.match_method, match_confidence = excluded.match_confidence,
                        match_state = excluded.match_state, raw_payload_uri = excluded.raw_payload_uri, is_listed = excluded.is_listed, updated_at = excluded.updated_at
                    """,
                    {
                        "partnerId": partner_id, "sku": sku, "gtin14": gtin14, "title": item["title"], "brand": item.get("brand"), "path": item.get("categoryPath"),
                        "netValue": None if net is None else net.get("value"), "netUnit": None if net is None else net.get("unit"), "productId": product_id,
                        "method": method, "confidence": confidence, "state": state, "uri": None if feed_id is None else f"feed://{feed_id}", "listed": listed, "now": now,
                    },
                )

                hp = None if product_id is None else self.cache.get_profile(market, product_id)
                match = _match(state, method, confidence, product_id, "INSUFFICIENT" if hp is None else hp.data_sufficiency)
                results.append({"partnerSku": sku, "status": "ACCEPTED", "match": match})
                cache_ops.append(_CacheOp(sku, product_id, confidence, state, listed, None if previous is None else previous["product_id"]))
                if state in ("NEEDS_REVIEW", "UNMATCHED"):
                    review.append({"partnerSku": sku, "match": match})
                if state == "PENDING_ENRICHMENT":
                    enrichment.append((item, market))
            Outbox.enqueue(u, partner_id, Topics.CATALOG_CHANGES, str(partner_id), {
                "feedId": feed_id, "items": [{"sku": c.sku, "product": c.product, "state": c.state} for c in cache_ops],
            })
            if review:
                WebhookFanoutConsumer.request(u, partner_id, WebhookTypes.CATALOG_REVIEW, {"feedId": feed_id, "items": review})

        self.db.in_tx(DbRole.APP, partner_id, work)

        for c in cache_ops:
            self.apply_cache(partner_id, c.sku, c.product, c.confidence, c.state, c.listed, c.previous)
        accepted = sum(1 for r in results if r["status"] == "ACCEPTED")
        log.info("Catalog upsert for %s: %d/%d accepted, %d need review, %d pending enrichment", partner_id, accepted, len(items), len(review), len(enrichment))
        result = {"batchId": str(new_id()), "received": len(items), "accepted": accepted, "rejected": len(items) - accepted, "results": results}
        return UpsertOutcome(result, enrichment)

    def apply_cache(self, partner_id: uuid.UUID, sku: str, product: uuid.UUID | None, confidence: Decimal, state: str, listed: bool,
                    previous: uuid.UUID | None) -> None:
        """Only confirmed or high-confidence matches enter the hot path (skumap / p2sku)."""
        if previous is not None and previous != product:
            self.cache.remove_partner_sku(partner_id, previous, sku)
        if product is not None and listed and state in ("AUTO_MATCHED", "CONFIRMED"):
            self.cache.set_sku_map(partner_id, sku, SkuMapEntry(product, confidence, state))
            self.cache.add_partner_sku(partner_id, product, sku)
        else:
            self.cache.remove_sku_map(partner_id, sku)
            if product is not None:
                self.cache.remove_partner_sku(partner_id, product, sku)

    def get(self, partner_id: uuid.UUID, sku: str) -> dict[str, Any]:
        """CatalogItemStatus: {"partnerSku", "isListed", "match", "health"?, "updatedAt"}."""
        partner_id = _uuid(partner_id)
        partner = self.partners.get(partner_id)
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(_ITEM_SQL, {"partnerId": partner_id, "sku": sku}))
        if row is None:
            raise HsError.not_found("Catalog item " + sku)
        hp = None if row["product_id"] is None else self.cache.get_profile(partner.markets[0], row["product_id"])
        return _compact({
            "partnerSku": row["partner_sku"], "isListed": row["is_listed"],
            "match": _match(
                row["match_state"], row["match_method"] or "NONE", _ZERO if row["match_confidence"] is None else row["match_confidence"], row["product_id"],
                "INSUFFICIENT" if hp is None else hp.data_sufficiency,
            ),
            "health": None if hp is None else ApiMapper.summary(hp), "updatedAt": iso(row["updated_at"]),
        })

    def delist(self, partner_id: uuid.UUID, sku: str) -> None:
        partner_id = _uuid(partner_id)

        def work(u: Uow) -> uuid.UUID | None:
            row = u.one(
                "select partner_sku, product_id, match_state, is_listed from tenant.catalog_item where partner_id = @partnerId and partner_sku = @sku",
                {"partnerId": partner_id, "sku": sku},
            )
            if row is None:
                raise HsError.not_found("Catalog item " + sku)
            u.execute(
                "update tenant.catalog_item set is_listed = false, updated_at = @now where partner_id = @partnerId and partner_sku = @sku",
                {"partnerId": partner_id, "sku": sku, "now": self.clock.utc_now()},
            )
            return row["product_id"]

        product = self.db.in_tx(DbRole.APP, partner_id, work)
        self.apply_cache(partner_id, sku, product, _ZERO, "DELISTED", False, None)

    def override_match(self, partner_id: uuid.UUID, sku: str, req: Mapping[str, Any]) -> dict[str, Any]:
        """MatchOverrideRequest {"action": CONFIRM|REJECT|ASSIGN, "productId"?, "comment"?} -> CatalogItemStatus."""
        partner_id = _uuid(partner_id)
        action = req.get("action")
        requested = None if req.get("productId") is None else _uuid(req["productId"])
        if action == "ASSIGN" and requested is None:
            raise HsError.unprocessable("HS-MATCH-PRODUCT-REQUIRED", "productId is required when action is ASSIGN")

        def work(u: Uow) -> tuple[uuid.UUID | None, uuid.UUID | None]:
            row = u.one(
                "select partner_sku, product_id, match_state, is_listed from tenant.catalog_item where partner_id = @partnerId and partner_sku = @sku",
                {"partnerId": partner_id, "sku": sku},
            )
            if row is None:
                raise HsError.not_found("Catalog item " + sku)
            product_id: uuid.UUID | None = row["product_id"]
            if action == "CONFIRM":
                if product_id is None:
                    raise HsError.unprocessable("HS-MATCH-NOTHING-TO-CONFIRM", "The item has no candidate product; use ASSIGN")
                state, method, confidence = "CONFIRMED", "MANUAL", Decimal(1)
            elif action == "REJECT":
                state, method, confidence = "REJECTED", "MANUAL", _ZERO
                product_id = None
            else:
                if not u.scalar("select exists (select 1 from kb.product where product_id = @id and status <> 'MERGED')", {"id": requested}):
                    raise HsError.unprocessable("HS-UNKNOWN-PRODUCT", "productId does not exist")
                state, method, confidence, product_id = "CONFIRMED", "MANUAL", Decimal(1), requested
            u.execute(
                "update tenant.catalog_item set product_id = @productId, match_state = @state, match_method = @method, match_confidence = @confidence, updated_at = @now where partner_id = @partnerId and partner_sku = @sku",
                {"productId": product_id, "state": state, "method": method, "confidence": confidence, "now": self.clock.utc_now(), "partnerId": partner_id, "sku": sku},
            )
            u.execute(
                "insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, @action, 'CATALOG_ITEM', @id, @details::jsonb)",
                {
                    "actor": f"partner:{partner_id}", "action": f"MATCH_{action}", "id": sku,
                    # JsonSerializer.Serialize(new { req.ProductId, req.Comment }) with default options.
                    "details": netjson.to_json_string({"ProductId": None if requested is None else str(requested), "Comment": req.get("comment")}),
                },
            )
            return product_id, row["product_id"]

        product, previous = self.db.in_tx(DbRole.APP, partner_id, work)
        status = self.get(partner_id, sku)
        self.apply_cache(partner_id, sku, product, status["match"]["confidence"], status["match"]["state"], status["isListed"], previous)
        return status

    def delist_missing(self, partner_id: uuid.UUID, present: Collection[str]) -> int:
        """Delists SKUs that are missing from a FULL feed."""
        partner_id = _uuid(partner_id)

        def work(u: Uow) -> list[dict[str, Any]]:
            rows = u.query(
                "select partner_sku, product_id from tenant.catalog_item where partner_id = @partnerId and is_listed and not (partner_sku = any(@present::text[]))",
                {"partnerId": partner_id, "present": list(present)},
            )
            u.execute(
                "update tenant.catalog_item set is_listed = false, updated_at = now() where partner_id = @partnerId and partner_sku = any(@skus::text[])",
                {"partnerId": partner_id, "skus": [r["partner_sku"] for r in rows]},
            )
            return rows

        missing = self.db.in_tx(DbRole.APP, partner_id, work)
        for r in missing:
            self.apply_cache(partner_id, r["partner_sku"], r["product_id"], _ZERO, "DELISTED", False, None)
        return len(missing)

    @staticmethod
    def to_candidate(partner_id: uuid.UUID, item: Mapping[str, Any], market: str, now: datetime) -> ProductCandidate:
        """Converts a partner catalog item to a knowledge-base candidate (source PARTNER_FEED, trust tier 3)."""
        partner_id = _uuid(partner_id)
        item = _api_item(item)
        panels = item.get("nutrition")
        panel = None
        if panels:
            panel = next((n for n in panels if n is not None and n.get("basis") in ("PER_100G", "PER_100ML")), None) or panels[0]
        net = item.get("netQuantity")
        serving = None if panel is None else panel.get("servingSize")
        nutrition = None
        if panel is not None:
            nutrition = Nutrition(
                basis=Basis.PER_100G if panel.get("basis") == "PER_SERVING" else panel.get("basis"),
                carb_definition="TOTAL_INCL_FIBRE" if market == "US" else "AVAILABLE",
                energy_kcal=panel.get("energyKcal"), protein_g=panel.get("proteinG"), carbohydrate_g=panel.get("carbohydrateG"),
                total_sugars_g=panel.get("totalSugarsG"), added_sugars_g=panel.get("addedSugarsG"), total_fat_g=panel.get("totalFatG"),
                saturated_fat_g=panel.get("saturatedFatG"), trans_fat_g=panel.get("transFatG"), cholesterol_mg=panel.get("cholesterolMg"),
                sodium_mg=panel.get("sodiumMg"), salt_g=panel.get("saltG"), fibre_g=panel.get("dietaryFibreG"),
            )
        physical_state = item.get("physicalState")
        if physical_state is None:
            physical_state = "LIQUID" if panel is not None and panel.get("basis") == "PER_100ML" else "SOLID"
        return ProductCandidate(
            source_code="PARTNER_FEED", external_id=f"{partner_id.hex}:{item['partnerSku']}", raw_gtin=item.get("gtin"), name=item["title"],
            brand=item.get("brand"), category_code=None, markets=(market,), net_value=None if net is None else net.get("value"),
            net_unit=None if net is None else net.get("unit"), pack_count=1 if item.get("packCount") is None else item["packCount"],
            physical_state=physical_state, serving_value=None if serving is None else serving.get("value"),
            serving_unit=None if serving is None else serving.get("unit"), nutrition=nutrition,
            sodium_declared=panel is not None and panel.get("sodiumMg") is not None, salt_declared=panel is not None and panel.get("saltG") is not None,
            ingredients_text=item.get("ingredientsText"), allergen_statement=item.get("allergenStatement"),
            dietary_marks=tuple(ApiMapper.internal_marks(item.get("dietaryMarks"), market)), attributes=dict(item.get("attributes") or {}),
            is_private_label=bool(item.get("isPrivateLabel") or False), verification="UNVERIFIED", observed_at=now, partner_id=partner_id,
            raw_json=netjson.serialize_hs(item),
        )


# ---------------------------------------------------------------------------------------------------------------------
# CatalogEnrichment
# ---------------------------------------------------------------------------------------------------------------------


class CatalogEnrichment:
    """
    Asynchronous enrichment for PENDING_ENRICHMENT items (HLD 6.2): the partner's label data becomes a tier-3 knowledge-base
    record; the item is then matched (GTIN) or queued for review, and the market is re-scored so the item gets a profile.
    """

    def __init__(self, db: Db, ingestor: ProductIngestor, scoring: ScoringJob, catalog: CatalogService, clock: Clock) -> None:
        self.db = db
        self.ingestor = ingestor
        self.scoring = scoring
        self.catalog = catalog
        self.clock = clock

    def enrich(self, partner_id: uuid.UUID, items: Sequence[tuple[Mapping[str, Any], str]]) -> None:
        if not items:
            return
        partner_id = _uuid(partner_id)
        report = self.ingestor.ingest([CatalogService.to_candidate(partner_id, item, market, self.clock.utc_now()) for item, market in items])
        for market in dict.fromkeys(market for _, market in items):
            self.scoring.run(market)
        for item, _ in items:
            sku = item["partnerSku"]
            product_id: uuid.UUID | None = None
            g = None if item.get("gtin") is None else gtin_try_normalize(item["gtin"])
            if g is not None:
                product_id = self.db.in_tx(DbRole.APP, partner_id, lambda u, g=g: u.scalar("select product_id from kb.product where primary_gtin = @g", {"g": g}))
            state = "NEEDS_REVIEW" if product_id is None else "AUTO_MATCHED"

            def work(u: Uow, sku: str = sku, product_id: uuid.UUID | None = product_id, state: str = state) -> None:
                u.execute(
                    "update tenant.catalog_item set product_id = @productId, match_state = @state, match_method = @method, match_confidence = @conf, updated_at = now() where partner_id = @partnerId and partner_sku = @sku and match_state = 'PENDING_ENRICHMENT'",
                    {
                        "productId": product_id, "state": state, "method": "NONE" if product_id is None else "GTIN_EXACT",
                        "conf": _ZERO if product_id is None else Decimal(1), "partnerId": partner_id, "sku": sku,
                    },
                )
                if product_id is None:
                    WebhookFanoutConsumer.request(u, partner_id, WebhookTypes.CATALOG_REVIEW, {
                        "feedId": None,
                        "items": [{"partnerSku": sku, "match": _match(state, "NONE", _ZERO, None, "PARTIAL")}],
                    })

            self.db.in_tx(DbRole.APP, partner_id, work)
            listed = True if item.get("isListed") is None else item["isListed"]
            self.catalog.apply_cache(partner_id, sku, product_id, _ZERO if product_id is None else Decimal(1), state, listed, None)
        log.info("Enrichment for %s: %d products created, %d versions", partner_id, report.created, report.new_versions)


# ---------------------------------------------------------------------------------------------------------------------
# FeedService
# ---------------------------------------------------------------------------------------------------------------------

_INT_TEXT_WS = " \t\n\v\f\r"


def _parse_int32(text: str) -> int:
    """int.Parse(text, CultureInfo.InvariantCulture) (NumberStyles.Integer); out-of-range values are format errors here."""
    t = text.strip(_INT_TEXT_WS)
    body = t[1:] if t[:1] in ("+", "-") else t
    if not body or not (body.isascii() and body.isdigit()):
        raise ValueError(f"The input string '{text}' was not in a correct format.")
    value = int(t)
    if not _INT32[0] <= value <= _INT32[1]:
        raise ValueError(f"Value was either too large or too small for an Int32: '{text}'.")
    return value


def _parse_bool(text: str) -> bool:
    """bool.Parse: "true"/"false" in any case, surrounding white space and NUL characters ignored."""
    t = text.strip(_INT_TEXT_WS + "\x00").lower()
    if t == "true":
        return True
    if t == "false":
        return False
    raise ValueError(f"String '{text}' was not recognized as a valid Boolean.")


def _chunks(body: bytes | bytearray | memoryview | BinaryIO | Iterable[bytes]) -> Iterator[bytes]:
    if isinstance(body, (bytes, bytearray, memoryview)):
        yield bytes(body)
        return
    read = getattr(body, "read", None)
    if callable(read):
        while True:
            chunk = read(81920)
            if not chunk:
                return
            yield bytes(chunk)
    for chunk in body:  # type: ignore[union-attr]
        if chunk:
            yield bytes(chunk)


def _stats(text: str | None) -> dict[str, int]:
    """FeedStats (all seven counters, zero when absent) from tenant.catalog_feed.stats."""
    d = jsonutil.loads(text) if text else {}
    d = {str(k).lower(): v for k, v in (d.items() if isinstance(d, dict) else ())}
    names = ("received", "accepted", "rejected", "autoMatched", "needsReview", "unmatched", "pendingEnrichment")
    return {n: int(d.get(n.lower()) or 0) for n in names}


class FeedService:
    """Bulk catalog feeds (POST /catalog/feeds): pre-signed upload URL, JSONL/CSV (optionally gzip), FULL or DELTA mode."""

    UPLOAD_PURPOSE = "HealthSwap.FeedUpload.v1"
    _FEED_SQL = (
        "select feed_id, partner_id, format, compression, mode, status, stats::text as stats, upload_expires_at, error_report_uri, created_at, completed_at "
        "from tenant.catalog_feed where feed_id = @feedId"
    )

    def __init__(self, db: Db, catalog: CatalogService, enrichment: CatalogEnrichment, protection: DataProtectionProvider, options: FeedOptions,
                 clock: Clock) -> None:
        self.db = db
        self.catalog = catalog
        self.enrichment = enrichment
        self.options = options
        self.clock = clock
        self._protector = protection.create_time_limited_protector(self.UPLOAD_PURPOSE)

    def _path(self, feed_id: uuid.UUID, suffix: str) -> Path:
        return Path(self.options.storage_root) / f"{feed_id.hex}{suffix}"

    def create(self, partner_id: uuid.UUID, req: Mapping[str, Any], upload_url: Callable[[uuid.UUID, str], str]) -> dict[str, Any]:
        """FeedCreateRequest {"format", "mode", "compression"?, "expectedItems"?} -> Feed with the pre-signed upload URL."""
        partner_id = _uuid(partner_id)
        if req.get("format") == "PARQUET":
            raise HsError.unprocessable("HS-FEED-FORMAT-UNSUPPORTED", "PARQUET feeds are not supported by the reference implementation; use JSONL or CSV")
        feed_id = new_id()
        expires = self.clock.utc_now() + timedelta(minutes=self.options.upload_url_minutes)
        compression = "GZIP" if req.get("compression") is None else req["compression"]
        self.db.in_tx(DbRole.APP, partner_id, lambda u: u.execute(
            """
            insert into tenant.catalog_feed (feed_id, partner_id, format, compression, mode, status, expected_items, upload_expires_at, created_at)
            values (@feedId, @partnerId, @format, @compression, @mode, 'AWAITING_UPLOAD', @expected, @expires, @now)
            """,
            {
                "feedId": feed_id, "partnerId": partner_id, "format": req.get("format"), "compression": compression, "mode": req.get("mode"),
                "expected": req.get("expectedItems"), "expires": expires, "now": self.clock.utc_now(),
            },
        ))
        token = self._protector.protect(f"{partner_id.hex}.{feed_id.hex}", expires)
        return {
            "feedId": str(feed_id), "status": "AWAITING_UPLOAD", "uploadUrl": upload_url(feed_id, token), "uploadUrlExpiresAt": iso(expires),
            "createdAt": iso(self.clock.utc_now()),
        }

    def get(self, partner_id: uuid.UUID, feed_id: uuid.UUID, error_report_url: Callable[[uuid.UUID], str] | None = None) -> dict[str, Any]:
        partner_id, feed_id = _uuid(partner_id), _uuid(feed_id)
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(self._FEED_SQL, {"feedId": feed_id}))
        if row is None:
            raise HsError.not_found(f"Feed {feed_id}")
        status = "EXPIRED" if row["status"] == "AWAITING_UPLOAD" and row["upload_expires_at"] < self.clock.utc_now() else row["status"]
        return _compact({
            "feedId": str(row["feed_id"]), "status": status,
            "uploadUrlExpiresAt": iso(row["upload_expires_at"]) if status == "AWAITING_UPLOAD" else None, "stats": _stats(row["stats"]),
            "errorReportUrl": error_report_url(row["feed_id"]) if row["error_report_uri"] is not None and error_report_url is not None else None,
            "createdAt": iso(row["created_at"]), "completedAt": iso(row["completed_at"]),
        })

    def accept_upload(self, feed_id: uuid.UUID, token: str, body: bytes | BinaryIO | Iterable[bytes]) -> tuple[uuid.UUID, uuid.UUID]:
        """Validates the pre-signed token and stores the upload; processing continues in the background (`process`)."""
        feed_id = _uuid(feed_id)
        try:
            payload, _ = self._protector.unprotect(token)
        except CryptographicError:
            raise HsError(403, "HS-FEED-UPLOAD-DENIED", "Forbidden", "The upload URL is invalid or expired") from None
        parts = payload.split(".")
        token_feed = _try_uuid(parts[1]) if len(parts) == 2 else None
        partner_id = _try_uuid(parts[0]) if len(parts) == 2 else None
        if token_feed is None or token_feed != feed_id or partner_id is None:
            raise HsError(403, "HS-FEED-UPLOAD-DENIED", "Forbidden", "The upload URL does not match this feed")
        Path(self.options.storage_root).mkdir(parents=True, exist_ok=True)
        path = self._path(feed_id, ".upload")
        total = 0
        try:
            with path.open("wb") as file:
                for chunk in _chunks(body):
                    total += len(chunk)
                    if total > self.options.max_upload_bytes:
                        raise HsError.too_large(f"Feed uploads are limited to {self.options.max_upload_bytes} bytes")
                    file.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)  # no partial uploads on disk
            raise
        updated = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.execute(
            "update tenant.catalog_feed set status = 'PROCESSING', storage_uri = @uri where feed_id = @feedId and status = 'AWAITING_UPLOAD'",
            {"feedId": feed_id, "uri": "file://" + str(path)},
        ))
        if updated == 0:
            raise HsError.conflict("HS-FEED-STATE", "The feed is not awaiting an upload")
        return partner_id, feed_id

    def _read_items(self, path: Path, fmt: str, compression: str, errors: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with path.open("rb") as raw:
            stream: BinaryIO = gzip.GzipFile(fileobj=raw, mode="rb") if compression == "GZIP" else raw  # type: ignore[assignment]
            with io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline=None) as reader:
                header: list[str] | None = None
                for line_no, text in enumerate(reader, start=1):
                    line = text[:-1] if text.endswith("\n") else text
                    if is_null_or_white_space(line):
                        continue
                    try:
                        if fmt == "CSV":
                            cells = self.parse_csv_line(line)
                            if header is None:
                                header = cells
                                continue
                            items.append(self._from_csv(header, cells))
                        else:
                            item = read_catalog_item(_JSON.decode(line))
                            if item is None:
                                raise ValueError("empty line")
                            items.append(item)
                    except (ValueError, RecursionError) as ex:
                        errors.append(f"line {line_no}: {ex}")
        return items

    def process(self, partner_id: uuid.UUID, feed_id: uuid.UUID) -> None:
        partner_id, feed_id = _uuid(partner_id), _uuid(feed_id)
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.single(self._FEED_SQL, {"feedId": feed_id}))
        path = self._path(feed_id, ".upload")
        errors: list[str] = []
        stats = _stats(None)
        status = "COMPLETED"
        try:
            items = self._read_items(path, row["format"], row["compression"], errors)
            received = len(items) + len(errors)
            accepted, rejected = 0, len(errors)
            auto = review = unmatched = pending = 0
            present: list[str] = []
            for start in range(0, len(items), 1000):
                chunk = items[start:start + 1000]
                outcome = self.catalog.upsert(partner_id, chunk, feed_id)
                accepted += outcome.result["accepted"]
                rejected += outcome.result["rejected"]
                for r in outcome.result["results"]:
                    if r["status"] == "REJECTED":
                        errors.append(f"{r['partnerSku']}: {'; '.join(e['message'] for e in r.get('errors') or [])}")
                    state = (r.get("match") or {}).get("state")
                    if state in ("AUTO_MATCHED", "CONFIRMED"):
                        auto += 1
                    elif state == "NEEDS_REVIEW":
                        review += 1
                    elif state == "UNMATCHED":
                        unmatched += 1
                    elif state == "PENDING_ENRICHMENT":
                        pending += 1
                present.extend(i["partnerSku"] for i in chunk)
                self.enrichment.enrich(partner_id, outcome.needs_enrichment)
            if row["mode"] == "FULL":
                self.catalog.delist_missing(partner_id, present)
            stats = {
                "received": received, "accepted": accepted, "rejected": rejected, "autoMatched": auto, "needsReview": review, "unmatched": unmatched,
                "pendingEnrichment": pending,
            }
            status = "COMPLETED" if rejected == 0 else "FAILED" if accepted == 0 else "PARTIAL"
        except (OSError, EOFError, zlib.error) as ex:
            errors.append(str(ex))
            status = "FAILED"
        error_uri = None
        if errors:
            error_path = self._path(feed_id, ".errors.txt")
            with error_path.open("w", encoding="utf-8") as f:
                f.writelines(e + "\n" for e in errors)
            error_uri = "file://" + str(error_path)
        self.db.in_tx(DbRole.APP, partner_id, lambda u: u.execute(
            "update tenant.catalog_feed set status = @status, stats = @stats::jsonb, error_report_uri = @errorUri, completed_at = @now where feed_id = @feedId",
            {"status": status, "stats": netjson.serialize_hs(stats), "errorUri": error_uri, "now": self.clock.utc_now(), "feedId": feed_id},
        ))
        log.info("Feed %s %s: %d accepted, %d rejected", feed_id, status, stats["accepted"], stats["rejected"])

    def error_report_path(self, feed_id: uuid.UUID) -> str | None:
        p = self._path(_uuid(feed_id), ".errors.txt")
        return str(p) if p.is_file() else None

    @staticmethod
    def _from_csv(header: Sequence[str], cells: Sequence[str]) -> dict[str, Any]:
        def v(name: str) -> str | None:
            i = header.index(name) if name in header else -1
            return cells[i] if 0 <= i < len(cells) and len(cells[i]) > 0 else None

        partner_sku = v("partnerSku")
        if partner_sku is None:
            raise ValueError("partnerSku is required")
        title = v("title")
        if title is None:
            raise ValueError("title is required")
        net_value = v("netQuantityValue")
        pack_count, private_label, listed, category_path = v("packCount"), v("isPrivateLabel"), v("isListed"), v("categoryPath")
        return _compact({
            "partnerSku": partner_sku, "gtin": v("gtin"), "title": title, "brand": v("brand"),
            "categoryPath": None if category_path is None else category_path.split("|"),
            "netQuantity": None if net_value is None else {"value": _parse_number(net_value), "unit": v("netQuantityUnit") or "g"},
            "packCount": None if pack_count is None else _parse_int32(pack_count),
            "isPrivateLabel": None if private_label is None else _parse_bool(private_label),
            "isListed": None if listed is None else _parse_bool(listed),
        })

    @staticmethod
    def parse_csv_line(line: str) -> list[str]:
        """RFC 4180 line parser (quoted fields, doubled quotes); multi-line fields are not supported."""
        cells: list[str] = []
        sb: list[str] = []
        quoted = False
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if quoted:
                if ch == '"' and i + 1 < n and line[i + 1] == '"':
                    sb.append('"')
                    i += 1
                elif ch == '"':
                    quoted = False
                else:
                    sb.append(ch)
            elif ch == '"':
                quoted = True
            elif ch == ",":
                cells.append("".join(sb))
                sb.clear()
            else:
                sb.append(ch)
            i += 1
        cells.append("".join(sb))
        return cells


# ---------------------------------------------------------------------------------------------------------------------
# InventoryService
# ---------------------------------------------------------------------------------------------------------------------


class InventoryService:
    """Store inventory mirror (MIRROR availability mode): PostgreSQL durable copy + hot cache hash per store."""

    def __init__(self, db: Db, cache: HotCache, partners: PartnerDirectory) -> None:
        self.db = db
        self.cache = cache
        self.partners = partners

    def upsert(self, partner_id: uuid.UUID, store_id: str, req: Mapping[str, Any]) -> dict[str, Any]:
        """InventoryBatchRequest {"snapshotAt", "mode": FULL_SNAPSHOT|DELTA, "entries"} -> InventoryBatchAck."""
        partner_id = _uuid(partner_id)
        partner = self.partners.get(partner_id)
        currency = partner.config.currency
        snapshot_at = _datetime(req["snapshotAt"])
        mode = req["mode"]
        errors: list[dict[str, str]] = []
        accepted: list[tuple[str, InventoryItem]] = []
        for e in req["entries"]:
            sku = e["partnerSku"]
            price = e.get("price") or {}
            if price.get("currency") != currency:
                errors.append({"partnerSku": sku, "code": "HS-CURRENCY-MISMATCH", "message": f"Price currency must be {currency}"})
                continue
            amount = try_parse_amount(price.get("amount"))
            if amount is None or amount < 0:
                errors.append({"partnerSku": sku, "code": "HS-INVALID-PRICE", "message": "Price must be a non-negative decimal"})
                continue
            mrp = None if e.get("mrp") is None else try_parse_amount(e["mrp"].get("amount"))
            accepted.append((sku, InventoryItem(bool(e.get("inStock") or False), amount, mrp, snapshot_at, e.get("availableQuantity"))))
        missing: list[str] = []

        def work(u: Uow) -> None:
            if not u.scalar(
                "select exists (select 1 from tenant.store where partner_id = @partnerId and store_id = @storeId and is_active)",
                {"partnerId": partner_id, "storeId": store_id},
            ):
                raise HsError.unprocessable("HS-UNKNOWN-STORE", f"Store {store_id} is not registered for this partner")
            for sku, item in accepted:
                u.execute(
                    """
                    insert into tenant.store_inventory (partner_id, store_id, partner_sku, in_stock, available_qty, price, mrp, currency, observed_at)
                    values (@partnerId, @storeId, @sku, @inStock, @qty, @price, @mrp, @currency, @observed)
                    on conflict (partner_id, store_id, partner_sku) do update set in_stock = excluded.in_stock, available_qty = excluded.available_qty,
                        price = excluded.price, mrp = excluded.mrp, currency = excluded.currency, observed_at = excluded.observed_at
                    where tenant.store_inventory.observed_at <= excluded.observed_at
                    """,
                    {
                        "partnerId": partner_id, "storeId": store_id, "sku": sku, "inStock": item.in_stock, "qty": item.available_quantity, "price": item.price,
                        "mrp": item.mrp, "currency": currency, "observed": item.observed_at,
                    },
                )
            if mode == "FULL_SNAPSHOT":
                missing.extend(r["partner_sku"] for r in u.query(
                    "update tenant.store_inventory set in_stock = false, observed_at = @observed where partner_id = @partnerId and store_id = @storeId and in_stock and not (partner_sku = any(@skus::text[])) returning partner_sku",
                    {"partnerId": partner_id, "storeId": store_id, "observed": snapshot_at, "skus": [sku for sku, _ in accepted]},
                ))
            Outbox.enqueue(u, partner_id, Topics.INVENTORY_UPDATES, f"{partner_id}:{store_id}", {
                "storeId": store_id, "mode": mode, "accepted": len(accepted), "markedOutOfStock": len(missing), "snapshotAt": snapshot_at,
            })

        self.db.in_tx(DbRole.APP, partner_id, work)

        current = self.cache.get_inventory(partner_id, store_id, [sku for sku, _ in accepted] + missing)
        updates = [(sku, item) for sku, item in accepted if sku not in current or current[sku].observed_at <= item.observed_at]
        for s in missing:
            base = current.get(s) or InventoryItem(False, _ZERO, None, snapshot_at, None)
            updates.append((s, replace(base, in_stock=False, observed_at=snapshot_at)))
        if updates:
            self.cache.set_inventory(partner_id, store_id, updates)
        return _compact({"accepted": len(accepted), "rejected": len(errors), "errors": errors or None})


# ---------------------------------------------------------------------------------------------------------------------
# PreferenceService
# ---------------------------------------------------------------------------------------------------------------------

_PREFS_SQL = """
            select diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands, religious_prefs, locale, is_minor,
                   health_data_consent, personalization_consent, consent_notice_version, updated_at
            from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef
            """


def _flag(value: Any, default: bool) -> bool:
    return default if value is None else bool(value)


def _strings(value: Any) -> tuple[str, ...]:
    return () if value is None else tuple(value)


def _prefs_doc(r: Mapping[str, Any]) -> ShopperPrefsDoc:
    """ShopperPrefsDoc from a tenant.user_preference row."""
    return ShopperPrefsDoc(
        diet_type=r["diet_type"], allergens=_strings(r["allergens"]), strict_may_contain=r["strict_may_contain"], goals=_strings(r["goals"]),
        avoid_ingredients=_strings(r["avoid_ingredients"]), excluded_brands=_strings(r["excluded_brands"]),
        religious_preferences=_strings(r["religious_prefs"]), locale=r["locale"], is_minor=r["is_minor"],
        health_data_consent=r["health_data_consent"], personalization_consent=r["personalization_consent"],
        consent_notice_version=r["consent_notice_version"], updated_at=r["updated_at"],
    )


class PreferenceService:
    """Shopper preferences with consent handling (HLD 11: DPDP / CCPA; health fields need explicit consent)."""

    def __init__(self, db: Db, cache: HotCache, clock: Clock) -> None:
        self.db = db
        self.cache = cache
        self.clock = clock

    @staticmethod
    def validate_user_ref(user_ref: str) -> None:
        if not isinstance(user_ref, str) or not 8 <= len(user_ref) <= 128 or not all((c.isascii() and c.isalnum()) or c in "_-" for c in user_ref):
            raise HsError.invalid("userRef must be a pseudonymous identifier of 8-128 characters [A-Za-z0-9_-]")

    @staticmethod
    def to_api(d: ShopperPrefsDoc) -> dict[str, Any]:
        """UserPreferences (C# property order; locale, isMinor and consentNoticeVersion omitted when None)."""
        return _compact({
            "dietType": d.diet_type, "allergens": list(d.allergens), "strictMayContain": d.strict_may_contain, "goals": list(d.goals),
            "avoidIngredients": list(d.avoid_ingredients), "excludedBrands": list(d.excluded_brands), "religiousPreferences": list(d.religious_preferences),
            "locale": d.locale, "isMinor": d.is_minor, "healthDataConsent": d.health_data_consent, "personalizationConsent": d.personalization_consent,
            "consentNoticeVersion": d.consent_notice_version, "updatedAt": iso(d.updated_at),
        })

    def get(self, partner_id: uuid.UUID, user_ref: str) -> dict[str, Any]:
        self.validate_user_ref(user_ref)
        partner_id = _uuid(partner_id)
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(_PREFS_SQL, {"partnerId": partner_id, "userRef": user_ref}))
        if row is None:
            raise HsError.not_found("Preferences")
        return self.to_api(_prefs_doc(row))

    def put(self, partner_id: uuid.UUID, user_ref: str, p: Mapping[str, Any]) -> dict[str, Any]:
        """Stores UserPreferences; allergens and goals are only kept with health-data consent; consent changes are recorded."""
        self.validate_user_ref(user_ref)
        partner_id = _uuid(partner_id)
        health = _flag(p.get("healthDataConsent"), False)
        now = self.clock.utc_now()
        doc = ShopperPrefsDoc(
            diet_type="ANY" if p.get("dietType") is None else p["dietType"],
            allergens=_strings(p.get("allergens")) if health else (), strict_may_contain=_flag(p.get("strictMayContain"), True),
            goals=_strings(p.get("goals")) if health else (), avoid_ingredients=_strings(p.get("avoidIngredients")),
            excluded_brands=_strings(p.get("excludedBrands")), religious_preferences=_strings(p.get("religiousPreferences")), locale=p.get("locale"),
            is_minor=p.get("isMinor"), health_data_consent=health, personalization_consent=_flag(p.get("personalizationConsent"), False),
            consent_notice_version=p.get("consentNoticeVersion"), updated_at=now,
        )

        def work(u: Uow) -> None:
            previous = u.one(
                "select health_data_consent, personalization_consent from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef",
                {"partnerId": partner_id, "userRef": user_ref},
            )
            u.execute(
                """
                insert into tenant.user_preference (partner_id, user_ref, diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands,
                    religious_prefs, locale, is_minor, health_data_consent, personalization_consent, consent_notice_version, updated_at)
                values (@partnerId, @userRef, @DietType, @allergens::text[], @StrictMayContain, @goals::text[], @avoid::text[], @brands::text[], @religious::text[], @Locale, @IsMinor,
                    @HealthDataConsent, @PersonalizationConsent, @ConsentNoticeVersion, @UpdatedAt)
                on conflict (partner_id, user_ref) do update set diet_type = excluded.diet_type, allergens = excluded.allergens,
                    strict_may_contain = excluded.strict_may_contain, goals = excluded.goals, avoid_ingredients = excluded.avoid_ingredients,
                    excluded_brands = excluded.excluded_brands, religious_prefs = excluded.religious_prefs, locale = excluded.locale, is_minor = excluded.is_minor,
                    health_data_consent = excluded.health_data_consent, personalization_consent = excluded.personalization_consent,
                    consent_notice_version = excluded.consent_notice_version, updated_at = excluded.updated_at
                """,
                {
                    "partnerId": partner_id, "userRef": user_ref, "DietType": doc.diet_type, "allergens": list(doc.allergens),
                    "StrictMayContain": doc.strict_may_contain, "goals": list(doc.goals), "avoid": list(doc.avoid_ingredients),
                    "brands": list(doc.excluded_brands), "religious": list(doc.religious_preferences), "Locale": doc.locale, "IsMinor": doc.is_minor,
                    "HealthDataConsent": doc.health_data_consent, "PersonalizationConsent": doc.personalization_consent,
                    "ConsentNoticeVersion": doc.consent_notice_version, "UpdatedAt": doc.updated_at,
                },
            )
            for purpose, granted, before in (
                ("HEALTH_PREFERENCES", doc.health_data_consent, None if previous is None else previous["health_data_consent"]),
                ("PERSONALIZATION", doc.personalization_consent, None if previous is None else previous["personalization_consent"]),
            ):
                if before != granted:
                    u.execute(
                        "insert into tenant.consent_record (partner_id, user_ref, purpose, granted, notice_version, captured_at, evidence) values (@partnerId, @userRef, @purpose, @granted, @notice, @now, '{\"channel\":\"API\"}'::jsonb)",
                        {
                            "partnerId": partner_id, "userRef": user_ref, "purpose": purpose, "granted": granted,
                            "notice": "unspecified" if doc.consent_notice_version is None else doc.consent_notice_version, "now": now,
                        },
                    )

        self.db.in_tx(DbRole.APP, partner_id, work)
        self.cache.set_prefs(partner_id, user_ref, doc)
        return self.to_api(doc)

    def load_for_evaluation(self, partner_id: uuid.UUID, user_ref: str) -> ShopperPrefsDoc | None:
        """The shopper's preferences for the hot path: hot cache first, then PostgreSQL (re-cached); None when unknown."""
        partner_id = _uuid(partner_id)
        cached = self.cache.get_prefs(partner_id, user_ref)
        if cached is not None:
            return cached
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(_PREFS_SQL, {"partnerId": partner_id, "userRef": user_ref}))
        if row is None:
            return None
        doc = _prefs_doc(row)
        self.cache.set_prefs(partner_id, user_ref, doc)
        return doc


# ---------------------------------------------------------------------------------------------------------------------
# PrivacyService
# ---------------------------------------------------------------------------------------------------------------------


class PrivacyService:
    """Erasure (DELETE /users/{userRef}): preferences deleted, userRef removed from events, orders and charges."""

    def __init__(self, db: Db, cache: HotCache, clock: Clock) -> None:
        self.db = db
        self.cache = cache
        self.clock = clock

    def erase(self, partner_id: uuid.UUID, user_ref: str) -> dict[str, Any]:
        """ErasureRequest {"requestId", "status": "COMPLETED", "receivedAt"}."""
        PreferenceService.validate_user_ref(user_ref)
        partner_id = _uuid(partner_id)
        request_id = new_id()
        now = self.clock.utc_now()
        user_hash = hashlib.sha256(_utf8(f"{partner_id.hex}:{user_ref}")).digest()

        def work(u: Uow) -> None:
            u.execute(
                "insert into tenant.dsar_request (request_id, partner_id, user_ref_hash, request_type, status, received_at) values (@requestId, @partnerId, @hash, 'ERASURE', 'IN_PROGRESS', @now)",
                {"requestId": request_id, "partnerId": partner_id, "hash": user_hash, "now": now},
            )
            p = {"partnerId": partner_id, "userRef": user_ref}
            u.execute("delete from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef", p)
            u.execute("update tenant.consent_record set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p)
            u.execute("update txn.cart_evaluation set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p)
            u.execute("update txn.suggestion set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p)
            u.execute("update txn.order_header set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p)
            u.execute("update txn.reco_charge set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p)
            u.execute("update tenant.dsar_request set status = 'COMPLETED', completed_at = @now where request_id = @requestId", {"requestId": request_id, "now": now})
            WebhookFanoutConsumer.request(u, partner_id, WebhookTypes.ERASURE_COMPLETED, {"requestId": request_id, "completedAt": now})

        self.db.in_tx(DbRole.APP, partner_id, work)
        self.cache.delete_prefs(partner_id, user_ref)
        removed = self.cache.delete_user_keys(partner_id, user_ref)
        log.info("Erasure %s completed for partner %s (%d cache keys removed)", request_id, partner_id, removed)
        return {"requestId": str(request_id), "status": "COMPLETED", "receivedAt": iso(now)}


__all__ = [
    "CatalogEnrichment", "CatalogService", "FeedService", "InventoryService", "PreferenceService", "PrivacyService", "UpsertOutcome", "read_catalog_item",
]
