"""Canonical product reads and recall ingestion (C# Knowledge/KbReader.cs).

`KbReader.load` returns the current version of every active or quarantined product (optionally for one market and/or a
set of ids) as ProductData, with ingredients re-parsed against the taxonomy, diet class, allergen confidence and the
selected sources. `RecallIngestor.ingest` records recall notices (FR-14), matches them to products by GTIN and, on a
match, publishes a KB change event (cache invalidation) and opens a P1 curation task.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from healthswap.domain.knowledge.parsing import DietClassifier, IngredientParser, net_trim
from healthswap.domain.knowledge.products import AllergenPresence, Basis, Nutrition, ProductData
from healthswap.domain.primitives import NetQuantity
from healthswap.platform import jsonutil, netjson
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.knowledge.connectors import RecallRecord
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository

log = logging.getLogger(__name__)

_PRODUCTS_SQL = """
select p.product_id, p.primary_gtin, p.name, b.name as brand, c.code as category_code, c.category_id, c.name as category_name,
       c.scoring_category, coalesce(p.physical_state, c.physical_state, 'SOLID') as physical_state, p.net_quantity, p.net_quantity_unit,
       p.pack_count, p.markets, p.is_private_label, p.status, p.attributes::text as attributes,
       v.version_id, v.serving_size, v.serving_unit, v.carb_definition, v.ingredients_text, v.allergen_statement, v.nova_group,
       v.nova_confidence, v.confidence, v.data_sufficiency, v.verification
from kb.product p
join kb.product_version v on v.version_id = p.current_version_id
left join kb.brand b on b.brand_id = p.brand_id
left join kb.category c on c.category_id = p.category_id
where p.status in ('ACTIVE', 'QUARANTINED') and c.code is not null
  and (@market::text is null or @market = any(p.markets))
  and (@ids::uuid[] is null or p.product_id = any(@ids::uuid[]))
order by p.product_id
"""

_PRESENCE = {"CONTAINS": AllergenPresence.CONTAINS, "MAY_CONTAIN": AllergenPresence.MAY_CONTAIN}


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


def _attributes(text: str) -> dict[str, str | None]:
    """JsonSerializer.Deserialize<Dictionary<string, string>>(text) ?? []: string (or null) values only."""
    value = jsonutil.loads(text)
    if value is None:
        return {}
    if not isinstance(value, dict) or any(v is not None and not isinstance(v, str) for v in value.values()):
        raise ValueError(f"The JSON value could not be converted to Dictionary<string, string>: {text[:200]}")
    return value


def _group(rows: Iterable[dict[str, Any]], key: str = "version_id") -> dict[Any, list[dict[str, Any]]]:
    """ToLookup: rows per key in query order."""
    result: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        result.setdefault(r[key], []).append(r)
    return result


@dataclass(frozen=True, slots=True)
class LoadedProduct:
    """A canonical product (current version) with its category name and the selected sources (C# KbReader.LoadedProduct)."""

    data: ProductData
    category_name: str | None
    sources: tuple[str, ...]


class KbReader:
    """Reads canonical product records (current versions) for scoring, graph builds and the admin plane."""

    LoadedProduct = LoadedProduct

    def __init__(self, db: Db, refs: ReferenceDataRepository) -> None:
        self.db = db
        self.refs = refs

    def load(self, market: str | None, ids: Sequence[uuid.UUID] | None = None) -> list[LoadedProduct]:
        """Products in `market` (None = all markets), optionally restricted to `ids`, ordered by product id."""
        taxonomy = self.refs.get_taxonomy()
        products, nutrients, allergens, marks, recalled, sources = self.db.in_tx(
            DbRole.KB_WRITER, None, lambda u: self._query(u, market, None if ids is None else list(ids))
        )
        nutrients_by_version = _group(nutrients)
        allergens_by_version = _group(allergens)
        marks_by_version = _group(marks)
        sources_by_version: dict[int, list[str]] = {}
        for s in sources:
            sources_by_version.setdefault(s["version_id"], []).append(s["code"])
        result: list[LoadedProduct] = []
        for p in products:
            attrs = _attributes(p["attributes"])
            basis = Basis.PER_100ML if p["physical_state"] == "LIQUID" else Basis.PER_100G
            rows = nutrients_by_version.get(p["version_id"], [])
            if rows and all(r["basis"] != basis for r in rows):
                basis = rows[0]["basis"]

            def n(code: str, rows: list[dict[str, Any]] = rows, basis: str = basis) -> Decimal | None:
                return next((r["amount"] for r in rows if r["nutrient_code"] == code and r["basis"] == basis), None)

            nutrition = Nutrition(
                basis=basis, carb_definition=p["carb_definition"] if p["carb_definition"] is not None else "AVAILABLE",
                energy_kcal=n("ENERC_KCAL"), protein_g=n("PROCNT"), carbohydrate_g=n("CHOCDF"), total_sugars_g=n("SUGAR"), added_sugars_g=n("SUGAD"),
                total_fat_g=n("FAT"), saturated_fat_g=n("FASAT"), trans_fat_g=n("FATRN"), cholesterol_mg=n("CHOLE"), sodium_mg=n("NA"),
                fibre_g=n("FIBTG"), fvln_pct=n("FVLN_PCT"),
                fvln_estimated=any(r["nutrient_code"] == "FVLN_PCT" and r["is_estimated"] for r in rows),
            )
            parsed = IngredientParser.parse(p["ingredients_text"], taxonomy)
            version_allergens = allergens_by_version.get(p["version_id"], [])
            allergen_map: dict[str, AllergenPresence] = {}
            for a in version_allergens:
                if a["allergen_code"] in allergen_map:
                    raise ValueError(f"An item with the same key has already been added. Key: {a['allergen_code']}")
                allergen_map[a["allergen_code"]] = _PRESENCE.get(a["presence"], AllergenPresence.FREE_FROM)
            verified = p["verification"] in ("BRAND_VERIFIED", "CURATOR_VERIFIED") or any(a["verified"] for a in version_allergens)
            mark_list = [m["mark_code"] for m in marks_by_version.get(p["version_id"], [])]
            diet = DietClassifier.classify(mark_list, parsed.flags, allergen_map)
            if verified:
                allergen_confidence = Decimal("1.0")
            elif p["allergen_statement"] is not None:
                allergen_confidence = Decimal("0.9")
            else:
                allergen_confidence = Decimal("0.85") if allergen_map else Decimal("0.6")
            brand_tier = attrs.get("brandTier")
            data = ProductData(
                product_id=p["product_id"], version_id=p["version_id"], gtin=None if p["primary_gtin"] is None else net_trim(p["primary_gtin"]),
                name=p["name"], brand=p["brand"],
                brand_tier=brand_tier if brand_tier is not None else "PRIVATE_LABEL" if p["is_private_label"] else "MAINSTREAM",
                category_code=p["category_code"], category_id=p["category_id"], scoring_category=p["scoring_category"],
                physical_state=p["physical_state"], net_quantity=NetQuantity.normalize(p["net_quantity"], p["net_quantity_unit"]),
                pack_count=p["pack_count"], markets=tuple(p["markets"]), is_private_label=p["is_private_label"], nutrition=nutrition,
                serving_size=p["serving_size"], serving_unit=p["serving_unit"], ingredients_text=p["ingredients_text"],
                ingredients=parsed.tokens, ingredient_resolution=parsed.resolution_ratio, additives=parsed.additives,
                ingredient_flags=parsed.flags, allergens=allergen_map, allergens_verified=verified, allergen_confidence=allergen_confidence,
                marks=tuple(mark_list), diet_class=diet.diet_class, jain_friendly_inferred=diet.jain_friendly_inferred or "JAIN_FRIENDLY" in mark_list,
                nova=p["nova_group"], nova_confidence=p["nova_confidence"] if p["nova_confidence"] is not None else Decimal(0),
                data_sufficiency=p["data_sufficiency"], confidence=p["confidence"], verification=p["verification"],
                form=attrs.get("form"), flavour_family=attrs.get("flavourFamily"), use_occasion=attrs.get("useOccasion"), cuisine=attrs.get("cuisine"),
                recalled=p["product_id"] in recalled, quarantined=p["status"] == "QUARANTINED",
            )
            result.append(LoadedProduct(data, p["category_name"], tuple(sorted(sources_by_version.get(p["version_id"], []), key=_ordinal))))
        return result

    @staticmethod
    def _query(u: Uow, market: str | None, ids: list[uuid.UUID] | None) -> tuple[Any, ...]:
        p = u.query(_PRODUCTS_SQL, {"market": market, "ids": ids})
        versions = [x["version_id"] for x in p]
        product_ids = [x["product_id"] for x in p]
        n = u.query(
            "select version_id, nutrient_code, basis, amount, is_estimated from kb.product_nutrient where version_id = any(@versions::bigint[])",
            {"versions": versions},
        )
        a = u.query(
            "select version_id, allergen_code, presence, verified from kb.product_allergen where version_id = any(@versions::bigint[])",
            {"versions": versions},
        )
        m = u.query("select version_id, mark_code from kb.product_mark where version_id = any(@versions::bigint[])", {"versions": versions})
        r = {
            row["product_id"]
            for row in u.query(
                "select distinct product_id from kb.recall where product_id = any(@productIds::uuid[]) and (suppress_until is null or suppress_until > now())",
                {"productIds": product_ids},
            )
        }
        s = u.query(
            """
            select distinct fp.version_id, s.code from kb.field_provenance fp join kb.source_record sr using (source_record_id) join kb.source s using (source_id)
            where fp.version_id = any(@versions::bigint[]) and fp.is_selected
            """,
            {"versions": versions},
        )
        return p, n, a, m, r, s


class RecallIngestor:
    """Recall ingestion (FR-14): match by GTIN, suppress immediately and publish a KB change event for cache invalidation."""

    def __init__(self, db: Db) -> None:
        self.db = db

    def ingest(self, records: Iterable[RecallRecord]) -> int:
        """Records each notice in its own transaction; returns how many matched a product."""
        matched = 0
        for r in records:
            product_id = self.db.in_tx(DbRole.KB_WRITER, None, lambda u, r=r: self._ingest_one(u, r))
            if product_id is not None:
                matched += 1
                log.warning("Recall %s matched product %s; suppressed from recommendations", r.reference, product_id)
        return matched

    @staticmethod
    def _ingest_one(u: Uow, r: RecallRecord) -> uuid.UUID | None:
        pid = None if r.gtin is None else u.scalar(
            "select product_id from kb.product where primary_gtin = @gtin union select product_id from kb.product_identifier where id_type = 'GTIN' and id_value = @gtin limit 1",
            {"gtin": r.gtin},
        )
        u.execute(
            """
            insert into kb.recall (jurisdiction, authority, reference, product_id, gtin, description, classification, published_at)
            values (@jurisdiction, @authority, @reference, @pid, @gtin, @description, @classification, @published)
            on conflict (authority, reference, gtin) do update set product_id = coalesce(excluded.product_id, kb.recall.product_id)
            """,
            {
                "jurisdiction": r.jurisdiction, "authority": r.authority, "reference": r.reference, "pid": pid, "gtin": r.gtin,
                "description": r.description, "classification": r.classification, "published": r.published_at,
            },
        )
        if pid is not None:
            Outbox.enqueue(u, None, Topics.KB_PRODUCT_CHANGES, str(pid), {"productId": pid, "recall": r.reference})
            u.execute(
                "insert into kb.curation_task (kind, priority, product_id, rule_code, payload) values ('RECALL_CONFIRM', 'P1', @pid, @reference, @payload::jsonb)",
                {
                    "pid": pid, "reference": r.reference,
                    "payload": netjson.to_json_string({"Authority": r.authority, "Description": r.description, "Classification": r.classification}),
                },
            )
        return pid
