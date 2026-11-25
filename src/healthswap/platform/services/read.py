"""Services/ReadServices.cs ported: product health profiles and lookups (ProductService, hot cache only), methodology
transparency and service status (TransparencyService), and the hot-cache warmer (CacheWarmer, a BackgroundWorker that re-warms
whenever PostgreSQL changed outside this process)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from healthswap.domain.primitives import Clock, as_utc, gtin_try_normalize, iso, utc_now
from healthswap.domain.scoring.health_profiles import HealthProfileData
from healthswap.platform import jsonutil
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.hot_cache import CachedExplanation, HotCache, InventoryItem, ShopperPrefsDoc, SkuMapEntry, SwapEdge
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory
from healthswap.platform.workers import BackgroundWorker

log = logging.getLogger(__name__)


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class ProductService:
    """Product health profiles and lookups (hot cache only)."""

    def __init__(self, cache: HotCache, partners: PartnerDirectory) -> None:
        self.cache = cache
        self.partners = partners

    def get(self, ctx: PartnerContext, product_id: uuid.UUID, market: str, locale: str | None = None) -> dict[str, Any]:
        """GET /products/{productId}: the public HealthProfile."""
        self._ensure_market(ctx, market)
        pid = _uuid(product_id)
        hp = self.cache.get_profile(market, pid)
        if hp is None:
            raise HsError.not_found(f"Product {pid}")
        return ApiMapper.profile(hp, locale)

    def lookup(self, ctx: PartnerContext, market: str, gtin: str | None, partner_sku: str | None, locale: str | None = None) -> dict[str, Any]:
        """GET /products/lookup: by the partner's SKU (catalog map) first, then by GTIN."""
        self._ensure_market(ctx, market)
        product_id: uuid.UUID | None = None
        if partner_sku is not None:
            entry = self.cache.get_sku_maps(ctx.partner_id, [partner_sku])[0]
            product_id = None if entry is None else entry.product_id
        if product_id is None and gtin is not None:
            g14 = gtin_try_normalize(gtin)
            if g14 is None:
                raise HsError.invalid("Invalid GTIN", [FieldError("/gtin", "Invalid check digit or length", "HS-INVALID-GTIN")])
            product_id = self.cache.get_product_by_gtin(g14)
        if gtin is None and partner_sku is None:
            raise HsError.invalid("Provide gtin or partnerSku")
        hp = None if product_id is None else self.cache.get_profile(market, product_id)
        if hp is None:
            raise HsError.not_found("Product")
        return ApiMapper.profile(hp, locale)

    def _ensure_market(self, ctx: PartnerContext, market: str) -> None:
        partner = self.partners.get(ctx.partner_id)
        if market not in partner.markets:
            raise HsError.unprocessable("HS-UNSUPPORTED-MARKET", f"Market {market} is not enabled for this partner")
        if ctx.is_sdk_session and ctx.sdk_market is not None and ctx.sdk_market != market:
            raise HsError.forbidden("The SDK session is bound to another market")


class TransparencyService:
    """Public methodology transparency (GET /scoring-models) and service status (GET /status)."""

    DESCRIPTIONS: Mapping[str, str] = {
        "IN_INR_DRAFT": "FSSAI Indian Nutrition Rating (draft) - threshold-points model, ILLUSTRATIVE parameters",
        "NUTRISCORE_2023": "Nutri-Score 2023 algorithm (foods, beverages, water), ILLUSTRATIVE parameters",
        "HSI": "HealthSwap Index: 70% nutrient profile, 15% processing (NOVA), 15% additives of concern",
        "NOVA": "NOVA processing classification derived from ingredient markers",
        "ADDITIVE_TIERS": "Additive concern tiers from regulators and authoritative bodies only",
    }

    def __init__(self, db: Db, refs: ReferenceDataRepository, cache: HotCache, clock: Clock) -> None:
        self.db = db
        self.refs = refs
        self.cache = cache
        self.clock = clock

    def models(self, market: str | None = None) -> dict[str, Any]:
        """ScoringModelList {"items": [ScoringModel]} for one market or every market with an active pack."""
        markets = self.refs.markets() if market is None else [market]
        items: list[dict[str, Any]] = []
        for m in markets:
            try:
                pack = self.refs.get_pack(m)
            except HsError:
                continue
            keys = [f"{p.code}@{p.version}" for p in pack.points_models.values()] + [f"HSI@{pack.hsi.version}", "NOVA@1.0", "ADDITIVE_TIERS@1.0"]
            rows = self.db.in_tx(DbRole.READ_ONLY, None, lambda u, keys=keys: u.query(
                "select code, version, status, methodology_url, published_at from rec.scoring_model where code || '@' || version = any(@keys)", {"keys": keys}))
            for r in rows:
                items.append({
                    k: v for k, v in {
                        "code": r["code"], "version": r["version"], "market": m, "status": r["status"], "description": self.DESCRIPTIONS.get(r["code"]),
                        "documentationUrl": r["methodology_url"] if r["methodology_url"] is not None else pack.methodology_url,
                        "effectiveFrom": None if r["published_at"] is None else as_utc(r["published_at"]).strftime("%Y-%m-%d"),
                    }.items() if v is not None
                })
        items.sort(key=lambda i: (i["market"], i["code"]))
        return {"items": items}

    def status(self) -> dict[str, Any]:
        """ServiceStatus: a market without an active swap graph in the hot cache is DEGRADED."""
        regions = [{"region": m, "status": "DEGRADED" if self.cache.get_active_graph(m) is None else "OPERATIONAL"} for m in self.refs.markets()]
        if not regions or all(r["status"] == "DEGRADED" for r in regions):
            status = "DEGRADED"
        elif any(r["status"] != "OPERATIONAL" for r in regions):
            status = "PARTIAL_OUTAGE"
        else:
            status = "OPERATIONAL"
        return {"status": status, "regions": regions, "updatedAt": iso(self.clock.utc_now())}


class CacheWarmer(BackgroundWorker):
    """
    Rebuilds the hot cache from PostgreSQL (the source of truth): profiles, GTIN map, category ceilings and maps, the active swap
    graph, explanations, partner catalog maps, inventory and preferences. Runs at startup and whenever the knowledge plane changes
    (the background loop compares a stamp of the relevant tables every 15 seconds).
    """

    INTERVAL_SECONDS = 15.0

    _STAMP_SQL = """
        select coalesce((select max(computed_at)::text from rec.health_profile), '') || '|' ||
               coalesce((select string_agg(graph_version, ',' order by graph_version) from rec.swap_graph_version where status = 'ACTIVE'), '') || '|' ||
               coalesce((select max(validated_at)::text from rec.explanation), '') || '|' ||
               coalesce((select max(updated_at)::text from tenant.catalog_item), '') || '|' ||
               coalesce((select count(*)::text from tenant.store_inventory), '')
        """

    def __init__(self, db: Db, cache: HotCache, refs: ReferenceDataRepository, partners: PartnerDirectory) -> None:
        super().__init__()
        self.db = db
        self.cache = cache
        self.refs = refs
        self.partners = partners
        self._knowledge_stamp: str | None = None
        self.last_warm_at: datetime | None = None

    def _stamp(self) -> str | None:
        return self.db.in_tx(DbRole.OPS, None, lambda u: u.scalar(self._STAMP_SQL))

    def warm_all(self) -> dict[str, int]:
        stamp = self._stamp()
        counts = self.warm_knowledge()
        for k, v in self.warm_tenants().items():
            counts[k] = v
        # Pre-load jurisdiction packs and tenant configuration so the first evaluation does not pay for them.
        self.refs.invalidate()
        for market in self.refs.markets():
            self.refs.get_pack(market)
        for p in self.partners.list_all():
            self.partners.invalidate(p.partner_id)
            self.partners.get(p.partner_id)
        self._knowledge_stamp = stamp
        self.last_warm_at = utc_now()
        self.log.info("Hot cache warmed: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
        return counts

    def warm_knowledge(self) -> dict[str, int]:
        def read(u: Uow) -> tuple[list[Any], ...]:
            p = [next(iter(r.values())) for r in u.query("select profile_doc::text from rec.health_profile")]
            c = u.query("select market, category_id, hsi_p60 from rec.category_stats")
            m = u.query(
                "select s.market, f.code as from_code, t.code as to_code from rec.category_substitution s join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id"
            )
            g = u.query("select market, graph_version from rec.swap_graph_version where status = 'ACTIVE'")
            e = u.query("select graph_version, from_product_id, to_product_id, health_delta, substitutability, reason_codes, features::text as features from rec.v_active_swap_edge")
            x = u.query(
                "select market, locale, from_product_id, to_product_id, encode(facts_hash, 'hex') as facts_hash, short_text, details::text as details, generator, model_ref from rec.explanation"
            )
            return p, c, m, g, e, x

        profiles, cats, maps, graphs, edges, expl = self.db.in_tx(DbRole.KB_WRITER, None, read)

        for doc in profiles:
            hp: HealthProfileData = HotCache.from_json(HealthProfileData, doc)
            self.cache.set_profile(hp)
            if hp.gtin is not None:
                self.cache.set_gtin(hp.gtin, hp.product_id)
        for c in cats:
            if c["hsi_p60"] is not None:
                self.cache.set_category_ceiling(c["market"], c["category_id"], c["hsi_p60"])
        category_maps: dict[tuple[str, str], list[str]] = {}
        for row in maps:
            category_maps.setdefault((row["market"], row["from_code"]), []).append(row["to_code"])
        for (market, from_code), targets in category_maps.items():
            self.cache.set_category_map(market, from_code, targets)
        for g in graphs:
            market, graph = g["market"], g["graph_version"]
            by_product: dict[uuid.UUID, list[SwapEdge]] = {}
            for e in edges:
                if e["graph_version"] == graph:
                    by_product.setdefault(e["from_product_id"], []).append(self._edge(e))
            for product_id, group in by_product.items():
                self.cache.set_edges(market, graph, product_id, sorted(group, key=lambda s: s.health_delta * s.substitutability, reverse=True))
            self.cache.set_active_graph(market, graph)
        for x in expl:
            details = jsonutil.loads(x["details"])
            bullets = details.get("bullets") if isinstance(details, dict) else None
            self.cache.set_explanation(
                x["market"], x["locale"], x["from_product_id"], x["to_product_id"], x["facts_hash"],
                CachedExplanation(x["short_text"], tuple("" if b is None else b for b in bullets or ()), x["generator"], x["generator"] == "LLM_VALIDATED", x["model_ref"]),
            )
        return {"profiles": len(profiles), "edges": len(edges), "explanations": len(expl), "graphs": len(graphs)}

    @staticmethod
    def _edge(e: Mapping[str, Any]) -> SwapEdge:
        """A SwapEdge from a rec.v_active_swap_edge row; the features document is the jobs' HotCache JSON (snake_case keys,
        camelCase accepted for databases written by the C# implementation)."""
        f = jsonutil.loads(e["features"]) if e["features"] else {}
        f = f if isinstance(f, dict) else {}

        def feature(name: str, camel: str, default: Any) -> Any:
            value = f.get(name, f.get(camel))
            return default if value is None else value

        return SwapEdge(
            e["to_product_id"], Decimal(e["health_delta"]), Decimal(e["substitutability"]), tuple(e["reason_codes"] or ()),
            int(feature("from_version", "fromVersion", 0)), int(feature("to_version", "toVersion", 0)), str(feature("to_category", "toCategory", "")),
            tuple(feature("introduced_tier_c", "introducedTierC", ())),
        )

    def warm_tenants(self) -> dict[str, int]:
        def read(u: Uow) -> tuple[list[dict[str, Any]], ...]:
            s = u.query(
                "select partner_id, partner_sku, product_id, coalesce(match_confidence, 0) as match_confidence, match_state from tenant.catalog_item where is_listed and product_id is not null and match_state in ('AUTO_MATCHED', 'CONFIRMED')"
            )
            i = u.query("select partner_id, store_id, partner_sku, in_stock, available_qty, price, mrp, observed_at from tenant.store_inventory")
            p = u.query(
                """
                select partner_id, user_ref, diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands, religious_prefs, locale, is_minor,
                       health_data_consent, personalization_consent, consent_notice_version, updated_at from tenant.user_preference
                """
            )
            # Line labels survive restarts: recommended products added to carts within the attribution window and not undone.
            labels = u.query(
                """
                select s.partner_id, s.cart_ref, e.alternative_partner_sku as sku, s.suggestion_id, s.attribution_expires_at as until
                from txn.suggestion_event e join txn.suggestion s on s.suggestion_id = e.suggestion_id
                where e.event_type in ('ACCEPT', 'ADD_TO_CART') and s.attribution_expires_at > now()
                  and not exists (select 1 from txn.suggestion_event x where x.suggestion_id = e.suggestion_id and x.event_type = 'UNDO' and x.occurred_at > e.occurred_at
                                  and (x.alternative_partner_sku is null or x.alternative_partner_sku = e.alternative_partner_sku))
                """
            )
            return s, i, p, labels

        skus, inventory, prefs, labels = self.db.in_tx(DbRole.OPS, None, read)
        for s in skus:
            self.cache.set_sku_map(s["partner_id"], s["partner_sku"], SkuMapEntry(s["product_id"], s["match_confidence"], s["match_state"]))
            self.cache.add_partner_sku(s["partner_id"], s["product_id"], s["partner_sku"])
        stores: dict[tuple[uuid.UUID, str], list[tuple[str, InventoryItem]]] = {}
        for i in inventory:
            stores.setdefault((i["partner_id"], i["store_id"]), []).append(
                (i["partner_sku"], InventoryItem(i["in_stock"], i["price"], i["mrp"], i["observed_at"], i["available_qty"])))
        for (partner_id, store_id), entries in stores.items():
            self.cache.set_inventory(partner_id, store_id, entries)
        for p in prefs:
            self.cache.set_prefs(p["partner_id"], p["user_ref"], ShopperPrefsDoc(
                diet_type=p["diet_type"], allergens=tuple(p["allergens"]), strict_may_contain=p["strict_may_contain"], goals=tuple(p["goals"]),
                avoid_ingredients=tuple(p["avoid_ingredients"]), excluded_brands=tuple(p["excluded_brands"]), religious_preferences=tuple(p["religious_prefs"]),
                locale=p["locale"], is_minor=p["is_minor"], health_data_consent=p["health_data_consent"], personalization_consent=p["personalization_consent"],
                consent_notice_version=p["consent_notice_version"], updated_at=p["updated_at"],
            ))
        for label in labels:
            self.cache.add_cart_reco_label(label["partner_id"], label["cart_ref"], label["sku"], label["suggestion_id"], as_utc(label["until"]) - utc_now())
        return {"skuMaps": len(skus), "inventory": len(inventory), "preferences": len(prefs), "lineLabels": len(labels)}

    def execute(self) -> None:
        while not self.stopping.is_set():
            try:
                if self.last_warm_at is None:
                    self.warm_all()
                elif self._stamp() != self._knowledge_stamp:
                    self.log.info("Database changed outside this process; re-warming the hot cache")
                    self.warm_all()
            except Exception:
                self.log.warning("Cache warm failed; retrying", exc_info=True)
            if not self.sleep(self.INTERVAL_SECONDS):
                break
