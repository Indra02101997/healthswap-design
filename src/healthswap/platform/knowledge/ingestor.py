"""Knowledge-base ingestion (C# Knowledge/Ingestor.cs, doc 02 sections 6-7, 10, 11): bronze copy with hash and licence,
deterministic parsing, QA rules, entity resolution, field-level survivorship with provenance, SCD-2 product versions and
CDC events.

Port notes:
    * The SQL is the C# SQL. Every JSON text the C# code hashed, compared or stored is reproduced exactly
      (netjson.to_json_string = JsonSerializer.Serialize with default options): field_provenance.value_json (the
      survivorship texts), the label canonical form hashed into product_version.label_hash, curation-task payloads and
      product attributes. Allergen presences in the provenance JSON are the C# enum names ("Contains", "MayContain",
      "FreeFrom"), as AllergenPresence.ToString() printed them.
    * The T3 category classifier (LLM) runs before the unit of work, never inside a transaction (as in C#).
    * Messages format numbers like the C# custom formats: {conf:0.00} on a double (15 significant digits, then half away
      from zero), {score:0.000} / {score:0.00} on a decimal (half away from zero).
    * Only HsError rejects a candidate (C# catches HsException); any other exception aborts the run, as in C#.
"""

from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from healthswap.domain.knowledge.parsing import (
    AllergenDeriver,
    DietClassifier,
    IngredientParser,
    IngredientTaxonomy,
    NovaClassifier,
    is_null_or_white_space,
)
from healthswap.domain.knowledge.products import AllergenPresence, Nutrition, decimal_div, decimal_max, round_decimal
from healthswap.domain.knowledge.quality import DataSufficiencyRules, EntityResolution, QaAction, QaInput, QaRules, Survivorship
from healthswap.domain.primitives import Clock, NetQuantity, gtin_is_restricted_circulation, gtin_try_normalize, new_id
from healthswap.domain.scoring.models import AdditiveTiers
from healthswap.domain.text import TextSimilarity, _utf8
from healthswap.platform import netjson
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole
from healthswap.platform.errors import HsError
from healthswap.platform.knowledge.connectors import ProductCandidate
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository
from healthswap.platform.llm.gateway import LlmGateway, LlmTask, PromptRegistry, _conversion_error, _get, parse_json
from healthswap.platform.ml.registry import ModelRegistry
from healthswap.platform.settings import LakeOptions

log = logging.getLogger(__name__)

# (nutrient code, getter) in the C# NutrientFields order; NA reads the derived sodium (declared sodium or salt x 400).
_NUTRIENT_FIELDS: tuple[tuple[str, Callable[[Nutrition], Decimal | None]], ...] = (
    ("ENERC_KCAL", lambda n: n.energy_kcal), ("PROCNT", lambda n: n.protein_g), ("CHOCDF", lambda n: n.carbohydrate_g),
    ("SUGAR", lambda n: n.total_sugars_g), ("SUGAD", lambda n: n.added_sugars_g), ("FAT", lambda n: n.total_fat_g),
    ("FASAT", lambda n: n.saturated_fat_g), ("FATRN", lambda n: n.trans_fat_g), ("CHOLE", lambda n: n.cholesterol_mg),
    ("NA", lambda n: n.sodium), ("FIBTG", lambda n: n.fibre_g),
)

# AllergenPresence.ToString() in C# (the enum member names) and the kb.product_allergen.presence codes.
_PRESENCE_NAMES = {AllergenPresence.CONTAINS: "Contains", AllergenPresence.MAY_CONTAIN: "MayContain", AllergenPresence.FREE_FROM: "FreeFrom"}
_PRESENCE_CODES = {AllergenPresence.CONTAINS: "CONTAINS", AllergenPresence.MAY_CONTAIN: "MAY_CONTAIN"}

_VERIFIED = ("BRAND_VERIFIED", "CURATOR_VERIFIED")
_ID_TYPES = {"OFF": "OFF_CODE", "USDA_FDC_BRANDED": "FDC_ID"}
_TIER_CONFIDENCE = {1: Decimal("1.0"), 2: Decimal("0.95"), 3: Decimal("0.9"), 4: Decimal("0.85")}


@dataclass(frozen=True, slots=True)
class IngestReport:
    received: int
    created: int
    new_versions: int
    unchanged: int
    rejected: int
    curation_tasks: int
    messages: list[str]


@dataclass(frozen=True, slots=True)
class _OneResult:
    created: bool
    new_version: bool
    tasks: int
    messages: list[str]


# A curation task: (kind, priority, rule code, payload JSON text).
_Task = tuple[str, str, str, str]
# A selected field value: (value JSON, source record, trust tier, conflict).
_Selected = tuple[str, int, int, bool]


# ---------------------------------------------------------------------------------------------------------------------
# Formatting and JsonNode helpers
# ---------------------------------------------------------------------------------------------------------------------


def _decimal_fixed(value: Decimal, places: int) -> str:
    """C# decimal custom formats "0.00" / "0.000" (invariant culture): half away from zero, fixed fraction digits."""
    q = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(abs(q) if q.is_zero() else q, "f")


def _double_fixed(value: float, places: int) -> str:
    """C# double custom format "0.00": the double is first taken to 15 significant digits, then rounded half away from zero."""
    q = Decimal(format(value, ".15g")).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(abs(q) if q.is_zero() else q, "f")


def _json_string(node: Any) -> str | None:
    """node?.GetValue<string>() on a parsed JsonNode."""
    if node is None or isinstance(node, str):
        return node
    raise _conversion_error(node, "System.String")


def _json_double(node: Any) -> float:
    """node.GetValue<double>() on a parsed JsonNode (a number that is not a finite double cannot be converted)."""
    if isinstance(node, bool) or not isinstance(node, (int, Decimal)):
        raise _conversion_error(node, "System.Double")
    try:
        value = float(node)
    except OverflowError:
        value = math.inf
    if not math.isfinite(value):
        raise _conversion_error(node, "System.Double")
    return value


# ---------------------------------------------------------------------------------------------------------------------
# T3 category classification input (also used by ml/finetune/build_datasets.py)
# ---------------------------------------------------------------------------------------------------------------------


def allowed_categories(codes: Iterable[str]) -> list[str]:
    """The T3 category enum: every kb.category code below the top level except infant foods, in the given order."""
    return [x for x in codes if x.count(".") >= 1 and x != "infant-foods"]


def category_payload(name: str | None, brand: str | None, allowed: Iterable[str]) -> str:
    """The T3 payload (inside <product>): listing title, brand and the allowed codes (default JSON escaping, nulls written)."""
    return netjson.to_json_string({"name": name, "brand": brand, "allowed": list(allowed)})


# ---------------------------------------------------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------------------------------------------------


class ProductIngestor:
    """
    Knowledge-base ingestion (doc 02 sections 6-7, 10, 11): bronze copy with hash and licence, deterministic parsing, QA rules,
    entity resolution, field-level survivorship with provenance, SCD-2 product versions and CDC events.
    """

    NUTRIENT_FIELDS = _NUTRIENT_FIELDS

    def __init__(self, db: Db, refs: ReferenceDataRepository, models: ModelRegistry, llm: LlmGateway, clock: Clock, lake: LakeOptions) -> None:
        self.db = db
        self.refs = refs
        self.models = models
        self.llm = llm
        self.clock = clock
        self.lake = lake

    def ingest(self, candidates: Iterable[ProductCandidate]) -> IngestReport:
        taxonomy = self.refs.get_taxonomy()
        categories = [r["code"] for r in self.db.in_tx(DbRole.KB_WRITER, None, lambda u: u.query("select code from kb.category"))]
        received = created = versions = unchanged = rejected = tasks = 0
        messages: list[str] = []
        for c in candidates:
            received += 1
            try:
                r = self._ingest_one(c, taxonomy, categories)
                if r.created:
                    created += 1
                if r.new_version:
                    versions += 1
                else:
                    unchanged += 1
                tasks += r.tasks
                messages.extend(r.messages)
            except HsError as ex:
                rejected += 1
                messages.append(f"{c.source_code}:{c.external_id} rejected: {'' if ex.detail is None else ex.detail}")
        log.info("Ingest finished: %d received, %d created, %d new versions, %d rejected", received, created, versions, rejected)
        return IngestReport(received, created, versions, unchanged, rejected, tasks, messages)

    def _ingest_one(self, c: ProductCandidate, tax: IngredientTaxonomy, categories: list[str]) -> _OneResult:
        now = self.clock.utc_now()
        messages: list[str] = []
        task_list: list[_Task] = []

        # GTIN (QA-13) and restricted-circulation numbers (keyed by partner SKU instead).
        gtin: str | None = None
        if not is_null_or_white_space(c.raw_gtin):
            g = gtin_try_normalize(c.raw_gtin)
            if g is None:
                raise HsError.invalid(f"QA-13 invalid GTIN {c.raw_gtin}")
            gtin = None if gtin_is_restricted_circulation(g) else g

        # Category: from the source, else LLM classification (T3, offline) with an allowed-ID enum and a confidence threshold.
        category = c.category_code if c.category_code is not None and c.category_code in categories else None
        if category is None:
            allowed = allowed_categories(categories)
            payload = category_payload(c.name, c.brand, allowed)
            spec = PromptRegistry.category_classifier(allowed)
            r = self.llm.run(spec, spec.user_content(payload))
            code = None if r.output is None else _json_string(_get(r.output, "categoryCode"))
            conf_node = None if r.output is None else _get(r.output, "confidence")
            conf = 0.0 if conf_node is None else _json_double(conf_node)
            accepted = r.ok and code is not None and code in allowed and conf >= 0.8
            self.llm.audit(
                LlmTask.T3, r, payload, {"schema": r.ok, "allowedId": code is not None and code in allowed, "confidence": conf >= 0.8}, accepted
            )
            if accepted:
                category = code
                messages.append(f"{c.external_id}: category {code} assigned by T3 ({_double_fixed(conf, 2)})")
            else:
                task_list.append((
                    "QA_REVIEW", "P2", "CATEGORY",
                    netjson.to_json_string({"reason": "Category could not be classified", "suggestion": code, "confidence": conf}),
                ))

        # Deterministic parsing.
        parsed = IngredientParser.parse(c.ingredients_text, tax)
        statement = AllergenDeriver.from_statement(c.allergen_statement)
        for code, presence in c.allergen_tags.items():
            if statement.get(code) != AllergenPresence.CONTAINS:
                statement[code] = presence
        allergens, _undeclared = AllergenDeriver.reconcile(statement, parsed.allergen_hints)
        diet = DietClassifier.classify(c.dietary_marks, parsed.flags, allergens)
        nova, nova_confidence = NovaClassifier.classify(parsed, tax)

        # QA rules as code.
        tier_a: list[str] = []
        for market in c.markets:
            pack = self.refs.get_pack(market)
            tier_a.extend(a for a in parsed.additives if AdditiveTiers.resolve(pack.additives.get(a), market) == "A")
        nutrition = c.nutrition if c.nutrition is not None else Nutrition()
        if parsed.fvln_pct_estimate is not None and nutrition.fvln_pct is None:
            nutrition = replace(nutrition, fvln_pct=parsed.fvln_pct_estimate, fvln_estimated=not parsed.fvln_from_quid)
        findings = QaRules.run(QaInput(
            nutrition=nutrition, per_serving_energy_kcal=c.per_serving_energy_kcal, serving_size=c.serving_value, raw_gtin=c.raw_gtin,
            declared_allergens=statement, ingredient_allergens=list(parsed.allergen_hints), veg_mark_contradicted=diet.veg_mark_contradicted,
            tier_a_additives=list(dict.fromkeys(tier_a)), last_verified_at=c.last_verified_at, now=now, salt_declared=c.salt_declared,
            sodium_declared=c.sodium_declared,
        ))
        for f in findings:
            if f.action != QaAction.REJECT_FIELD:
                continue
            if f.field == "nutrition.totalSugarsG":
                nutrition = replace(nutrition, total_sugars_g=None)
            elif f.field == "nutrition.addedSugarsG":
                nutrition = replace(nutrition, added_sugars_g=None)
            elif f.field == "nutrition.saturatedFatG":
                nutrition = replace(nutrition, saturated_fat_g=None, trans_fat_g=None)
        for f in findings:
            if f.action in (QaAction.REVIEW, QaAction.QUARANTINE):
                task_list.append((
                    "COMPLIANCE" if f.code == "QA-14" else "QA_REVIEW", "P1" if f.code in ("QA-14", "QA-11") else "P2", f.code,
                    netjson.to_json_string({"Field": f.field, "Message": f.message}),
                ))
        messages.extend(f"{c.external_id}: {f.code} {f.action.value} - {f.message}" for f in findings)

        net = NetQuantity.normalize(c.net_value, c.net_unit)
        serving = NetQuantity.normalize(c.serving_value, c.serving_unit)
        quarantined = len(tier_a) > 0

        with self.db.tx(DbRole.KB_WRITER) as u:
            source = u.single("select source_id, trust_tier, license_code from kb.source where code = @code", {"code": c.source_code})
            source_tier: int = source["trust_tier"]
            payload_bytes = _utf8(c.raw_json)
            sha = hashlib.sha256(payload_bytes).digest()
            uri = self._write_bronze(c, payload_bytes, sha)
            source_record_id = u.scalar(
                """
                insert into kb.source_record (source_id, external_id, partner_id, fetched_at, payload_uri, payload_sha256)
                values (@sid, @ext, @partner, @at, @uri, @sha) on conflict (source_id, external_id, payload_sha256) do nothing returning source_record_id
                """,
                {"sid": source["source_id"], "ext": c.external_id, "partner": c.partner_id, "at": c.observed_at, "uri": uri, "sha": sha},
            )
            if source_record_id is None:
                source_record_id = u.scalar(
                    "select source_record_id from kb.source_record where source_id = @sid and external_id = @ext and payload_sha256 = @sha",
                    {"sid": source["source_id"], "ext": c.external_id, "sha": sha},
                )

            # Entity resolution: GTIN exact (never merge different GTINs), then trigram blocking + pairwise model.
            existing: dict[str, Any] | None = None
            if gtin is not None:
                existing = u.one(
                    """
                    select p.product_id, p.current_version_id, p.name, b.name as brand, p.markets, p.primary_gtin, p.net_quantity, p.net_quantity_unit,
                           encode(v.label_hash, 'hex') as label_hash_hex
                    from kb.product p left join kb.brand b on b.brand_id = p.brand_id left join kb.product_version v on v.version_id = p.current_version_id
                    where p.primary_gtin = @gtin or p.product_id = (select product_id from kb.product_identifier where id_type = 'GTIN' and id_value = @gtin)
                    limit 1
                    """,
                    {"gtin": gtin},
                )
                if (
                    existing is not None and existing["brand"] is not None and c.brand is not None
                    and TextSimilarity.trigram_jaccard(existing["brand"], c.brand) < 0.2 and TextSimilarity.trigram_jaccard(existing["name"], c.name) < 0.2
                ):
                    task_list.append(("ER_REVIEW", "P1", "GTIN_CONFLICT", netjson.to_json_string({"gtin": gtin, "existing": existing["name"], "incoming": c.name})))
            else:
                fuzzy = u.query(
                    """
                    select p.product_id, p.name, b.name as brand, p.primary_gtin, p.net_quantity, p.net_quantity_unit, similarity(p.name, @name) as sim
                    from kb.product p left join kb.brand b on b.brand_id = p.brand_id
                    where p.status <> 'MERGED' and similarity(p.name, @name) > 0.3 order by sim desc limit 5
                    """,
                    {"name": c.name},
                )
                for f in fuzzy:
                    features = EntityResolution.features(
                        EntityResolution.Candidate(c.name, c.brand, net, c.nutrition, [t.canonical_name if t.canonical_name is not None else t.raw_text for t in parsed.tokens], None),
                        EntityResolution.Candidate(f["name"], f["brand"], NetQuantity.normalize(f["net_quantity"], f["net_quantity_unit"]), None, [], f["primary_gtin"]),
                    )
                    score = self.models.score_entity_match(features)
                    decision = EntityResolution.decide(score)
                    if decision == "AUTO_MERGE":
                        existing = u.one(
                            "select p.product_id, p.current_version_id, p.name, null as brand, p.markets, p.primary_gtin, p.net_quantity, p.net_quantity_unit, encode(v.label_hash, 'hex') as label_hash_hex from kb.product p left join kb.product_version v on v.version_id = p.current_version_id where p.product_id = @id",
                            {"id": f["product_id"]},
                        )
                        messages.append(f"{c.external_id}: ER auto-merge into {f['product_id']} ({_decimal_fixed(score, 3)})")
                        break
                    if decision == "REVIEW":
                        task_list.append((
                            "ER_REVIEW", "P2", f"ER-{_decimal_fixed(score, 2)}",
                            netjson.to_json_string({"incoming": c.name, "candidate": f["name"], "candidateId": str(f["product_id"]), "score": score}),
                        ))

            brand_id = None if c.brand is None else u.single(
                """
                insert into kb.brand (name, normalized_name, owner_company) values (@name, @norm, @owner)
                on conflict (normalized_name, owner_company) do update set name = excluded.name returning brand_id
                """,
                {"name": c.brand, "norm": IngredientTaxonomy.normalize(c.brand), "owner": c.brand_owner},
            )["brand_id"]
            category_id = None if category is None else u.scalar("select category_id from kb.category where code = @category", {"category": category})
            attributes = netjson.to_json_string(dict(c.attributes) if len(c.attributes) > 0 else {"brandTier": c.brand_tier})

            created_product = existing is None
            product_id: uuid.UUID = new_id() if existing is None else existing["product_id"]
            if created_product:
                u.execute(
                    """
                    insert into kb.product (product_id, primary_gtin, brand_id, category_id, name, net_quantity, net_quantity_unit, pack_count, physical_state,
                                            markets, is_private_label, status, attributes)
                    values (@productId, @gtin, @brandId, @categoryId, @name, @netValue, @netUnit, @pack, @state, @markets, @pl, @status, @attributes::jsonb)
                    """,
                    {
                        "productId": product_id, "gtin": gtin, "brandId": brand_id, "categoryId": category_id, "name": c.name,
                        "netValue": None if net is None else net.value, "netUnit": None if net is None else net.unit, "pack": max(1, c.pack_count),
                        "state": c.physical_state, "markets": list(c.markets), "pl": c.is_private_label, "status": "QUARANTINED" if quarantined else "ACTIVE",
                        "attributes": attributes,
                    },
                )
                if gtin is not None:
                    u.execute(
                        "insert into kb.product_identifier (id_type, id_value, product_id) values ('GTIN', @gtin, @productId) on conflict do nothing",
                        {"gtin": gtin, "productId": product_id},
                    )
            else:
                # Higher-trust sources may refine identity attributes; markets are unioned. Lower tiers never overwrite.
                u.execute(
                    """
                    update kb.product set markets = (select array(select distinct unnest(markets || @markets))),
                        category_id = coalesce(category_id, @categoryId), brand_id = coalesce(brand_id, @brandId),
                        attributes = case when @tier <= 2 then @attributes::jsonb else attributes end,
                        status = case when @quarantined then 'QUARANTINED' else status end, updated_at = now()
                    where product_id = @productId
                    """,
                    {
                        "markets": list(c.markets), "categoryId": category_id, "brandId": brand_id, "attributes": attributes, "tier": int(source_tier),
                        "quarantined": quarantined, "productId": product_id,
                    },
                )
            id_type = _ID_TYPES.get(c.source_code)
            if id_type is not None:
                u.execute(
                    "insert into kb.product_identifier (id_type, id_value, product_id) values (@idType, @ext, @productId) on conflict do nothing",
                    {"idType": id_type, "ext": c.external_id, "productId": product_id},
                )

            # Field-level survivorship against the current version's selected values.
            current: dict[str, dict[str, Any]] = {}
            if existing is not None and existing["current_version_id"] is not None:
                for row in u.query(
                    """
                    select fp.field_path, fp.value_json::text as value_json, fp.trust_tier, fp.source_record_id, sr.fetched_at
                    from kb.field_provenance fp join kb.source_record sr on sr.source_record_id = fp.source_record_id
                    where fp.version_id = @cv and fp.is_selected
                    """,
                    {"cv": existing["current_version_id"]},
                ):
                    if row["field_path"] in current:  # ToDictionary
                        raise ValueError(f"An item with the same key has already been added. Key: {row['field_path']}")
                    current[row["field_path"]] = row
            incoming: dict[str, tuple[str, Decimal | None]] = {}
            for code, get in _NUTRIENT_FIELDS:
                v = get(nutrition)
                if v is not None:
                    incoming[f"nutrient.{code}.{nutrition.basis}"] = (netjson.to_json_string(v), v)
            if c.ingredients_text is not None:
                incoming["ingredients_text"] = (netjson.to_json_string(c.ingredients_text), None)
            if c.allergen_statement is not None or len(c.allergen_tags) > 0:
                incoming["allergens"] = (ProductIngestor._allergens_json(allergens), None)
            if serving is not None:
                incoming["serving_size"] = (netjson.to_json_string(serving.value), serving.value)

            selected: dict[str, _Selected] = {}
            for field_path, prov in current.items():
                selected[field_path] = (prov["value_json"], prov["source_record_id"], prov["trust_tier"], False)
            for field_path, (value_json, number) in incoming.items():
                existing_value = selected.get(field_path)
                if existing_value is None:
                    selected[field_path] = (value_json, source_record_id, source_tier, False)
                    continue
                prov = current[field_path]
                decision = Survivorship.select(field_path, [
                    Survivorship.Candidate(
                        field_path, ProductIngestor._num(prov["value_json"]), prov["value_json"], prov["trust_tier"], prov["fetched_at"], prov["source_record_id"], True
                    ),
                    Survivorship.Candidate(field_path, number, value_json, source_tier, c.observed_at, source_record_id, True),
                ])
                assert decision.selected is not None  # both candidates carry a value
                incoming_won = decision.selected.source_record_id == source_record_id
                if incoming_won:
                    selected[field_path] = (value_json, source_record_id, source_tier, decision.conflict)
                elif decision.conflict:
                    selected[field_path] = (existing_value[0], existing_value[1], existing_value[2], True)
                if decision.conflict:
                    task_list.append((
                        "FIELD_CONFLICT", "P2", field_path,
                        netjson.to_json_string({
                            "field": field_path, "selected": decision.selected.text, "challenger": prov["value_json"] if incoming_won else value_json,
                            "Reason": decision.reason,
                        }),
                    ))

            # SCD-2 versioning by normalized label hash.
            merged = ProductIngestor._merge_nutrition(selected, nutrition.basis, nutrition.carb_definition, nutrition)
            ingredients_text = ProductIngestor._json_to_string(selected["ingredients_text"][0]) if "ingredients_text" in selected else c.ingredients_text
            label_canonical = ProductIngestor._label_canonical(
                selected, ingredients_text, c.dietary_marks, None if net is None else net.value, None if serving is None else serving.value
            )
            label_hash = hashlib.sha256(_utf8(label_canonical)).digest()
            is_new_version = existing is None or existing["label_hash_hex"] != label_hash.hex()
            if is_new_version:
                if existing is not None and existing["current_version_id"] is not None:
                    u.execute("update kb.product_version set valid_to = @now where version_id = @prev", {"now": now, "prev": existing["current_version_id"]})
                # Re-parse the surviving ingredient text (it may come from a higher-tier source than this candidate).
                final_parsed = parsed if ingredients_text == c.ingredients_text else IngredientParser.parse(ingredients_text, tax)
                if c.verification in _VERIFIED:
                    allergen_confidence = Decimal("1.0")
                elif c.allergen_statement is not None:
                    allergen_confidence = Decimal("0.9")
                else:
                    allergen_confidence = Decimal("0.85") if len(c.allergen_tags) > 0 else Decimal("0.6")
                tier_confidence = _TIER_CONFIDENCE.get(source_tier, Decimal("0.7"))
                confidence = decimal_max(Decimal("0.5"), tier_confidence - Decimal("0.05") * sum(1 for f in findings if f.action == QaAction.REVIEW))
                sufficiency = DataSufficiencyRules.evaluate(
                    merged, final_parsed.resolution_ratio, ingredients_text is not None, allergen_confidence, c.verification in _VERIFIED,
                    category_id is not None or existing is not None, net is not None or (existing is not None and existing["net_quantity"] is not None),
                )
                completeness = round_decimal(
                    decimal_div(Decimal(sum(1 for _, get in _NUTRIENT_FIELDS if get(merged) is not None)), Decimal(len(_NUTRIENT_FIELDS))), 3
                )
                version_id: int = u.single(
                    """
                    insert into kb.product_version (product_id, valid_from, label_hash, serving_size, serving_unit, carb_definition, ingredients_text,
                        ingredients_lang, allergen_statement, nova_group, nova_confidence, completeness, confidence, data_sufficiency, verification, last_verified_at)
                    values (@productId, @now, @labelHash, @servingSize, @servingUnit, @carb, @ingredientsText, @lang, @statement, @nova, @novaConfidence,
                        @completeness, @confidence, @sufficiency, @verification, @lastVerified)
                    returning version_id
                    """,
                    {
                        "productId": product_id, "now": now, "labelHash": label_hash, "servingSize": None if serving is None else serving.value,
                        "servingUnit": serving.unit if serving is not None and serving.unit in ("g", "ml") else None, "carb": merged.carb_definition,
                        "ingredientsText": ingredients_text,
                        "lang": "hi" if ingredients_text is not None and any("\u0900" <= ch <= "\u097f" for ch in ingredients_text) else "en",
                        "statement": c.allergen_statement, "nova": nova, "novaConfidence": nova_confidence, "completeness": completeness,
                        "confidence": confidence, "sufficiency": sufficiency, "verification": c.verification, "lastVerified": c.last_verified_at,
                    },
                )["version_id"]
                u.execute("update kb.product set current_version_id = @versionId, updated_at = now() where product_id = @productId", {"versionId": version_id, "productId": product_id})

                for code, get in _NUTRIENT_FIELDS:
                    amount = get(merged)
                    if amount is None:
                        continue
                    sel = selected.get(f"nutrient.{code}.{merged.basis}")
                    src = source_record_id if sel is None else sel[1]
                    declared = not (code == "NA" and src == source_record_id and c.salt_declared and not c.sodium_declared)
                    u.execute(
                        "insert into kb.product_nutrient (version_id, nutrient_code, basis, amount, is_declared, source_record_id, confidence) values (@versionId, @code, @basis, @amount, @declared, @src, @conf)",
                        {"versionId": version_id, "code": code, "basis": merged.basis, "amount": amount, "declared": declared, "src": src, "conf": confidence},
                    )
                if merged.fvln_pct is not None:
                    u.execute(
                        "insert into kb.product_nutrient (version_id, nutrient_code, basis, amount, is_declared, is_estimated, source_record_id, confidence) values (@versionId, 'FVLN_PCT', @basis, @fvlPct, false, @est, @src, 0.8)",
                        {"versionId": version_id, "basis": merged.basis, "fvlPct": merged.fvln_pct, "est": merged.fvln_estimated, "src": source_record_id},
                    )
                for t in final_parsed.tokens:
                    if t.raw_text.startswith("class:"):
                        continue
                    u.execute(
                        """
                        insert into kb.product_ingredient (version_id, position, parent_position, ingredient_id, raw_text, percent)
                        values (@versionId, @pos, @parent, (select ingredient_id from kb.ingredient where canonical_name = @canonical), @raw, @pct)
                        """,
                        {
                            "versionId": version_id, "pos": t.position, "parent": t.parent_position, "canonical": t.canonical_name,
                            "raw": t.raw_text if len(t.raw_text) > 0 else "?", "pct": None if t.percent is not None and t.percent > 100 else t.percent,
                        },
                    )
                for code in final_parsed.additives:
                    u.execute(
                        "insert into kb.product_additive (version_id, additive_id, detected_from) select @versionId, additive_id, 'INGREDIENTS' from kb.additive where ins_number = @code on conflict do nothing",
                        {"versionId": version_id, "code": code},
                    )
                for code, presence in allergens.items():
                    u.execute(
                        "insert into kb.product_allergen (version_id, allergen_code, presence, derived_from, verified) values (@versionId, @code, @presence, @from, @verified) on conflict do nothing",
                        {
                            "versionId": version_id, "code": code, "presence": _PRESENCE_CODES.get(presence, "FREE_FROM"),
                            "from": ("BRAND" if c.verification == "BRAND_VERIFIED" else "STATEMENT") if code in statement else "INGREDIENTS",
                            "verified": c.verification in _VERIFIED,
                        },
                    )
                marks = list(c.dietary_marks)
                if diet.jain_friendly_inferred:
                    marks.append("JAIN_FRIENDLY")
                for mark in dict.fromkeys(marks):
                    u.execute(
                        "insert into kb.product_mark (version_id, mark_code, is_inferred, verified) select @versionId, mark_code, @inferred, @verified from kb.mark where mark_code = @mark on conflict do nothing",
                        {
                            "versionId": version_id, "mark": mark, "inferred": mark == "JAIN_FRIENDLY" or c.marks_inferred,
                            "verified": c.verification == "BRAND_VERIFIED" and mark != "JAIN_FRIENDLY",
                        },
                    )
                for field_path, (value_json, src, tier, conflict) in selected.items():
                    u.execute(
                        """
                        insert into kb.field_provenance (version_id, field_path, source_record_id, value_json, trust_tier, license_code, is_selected, conflict)
                        select @versionId, @field, @src, @value::jsonb, @tier, s.license_code, true, @conflict
                        from kb.source_record r join kb.source s using (source_id) where r.source_record_id = @src
                        on conflict do nothing
                        """,
                        {"versionId": version_id, "field": field_path, "src": src, "value": value_json, "tier": tier, "conflict": conflict},
                    )
                Outbox.enqueue(
                    u, None, Topics.KB_PRODUCT_CHANGES, str(product_id),
                    {"productId": product_id, "versionId": version_id, "gtin": gtin, "source": c.source_code, "created": created_product},
                )
            else:
                version_id = existing["current_version_id"]

            # Keep every candidate value (also non-selected ones) for audits and dispute resolution.
            for field_path, (value_json, _number) in incoming.items():
                u.execute(
                    """
                    insert into kb.field_provenance (version_id, field_path, source_record_id, value_json, trust_tier, license_code, is_selected, conflict)
                    values (@versionId, @field, @src, @value::jsonb, @tier, @license, false, false) on conflict do nothing
                    """,
                    {"versionId": version_id, "field": field_path, "src": source_record_id, "value": value_json, "tier": source_tier, "license": source["license_code"]},
                )

            for kind, priority, rule, task_payload in task_list:
                u.execute(
                    "insert into kb.curation_task (kind, priority, product_id, version_id, rule_code, payload) values (@kind, @priority, @productId, @versionId, @rule, @payload::jsonb)",
                    {"kind": kind, "priority": priority, "productId": product_id, "versionId": version_id, "rule": rule, "payload": task_payload},
                )
        return _OneResult(created_product, is_new_version, len(task_list), messages)

    # --- helpers ---------------------------------------------------------------------------------------------------------

    @staticmethod
    def _num(json_text: str) -> Decimal | None:
        """JsonNode.Parse(json) is JsonValue v && v.TryGetValue<decimal>(out d) ? d : null (numbers keep their scale)."""
        node = parse_json(json_text)
        if isinstance(node, bool) or not isinstance(node, (int, Decimal)):
            return None
        d = Decimal(node) if isinstance(node, int) else node
        return d if d.copy_abs() <= Decimal("79228162514264337593543950335") else None  # copy_abs: abs() rounds to 28 digits

    @staticmethod
    def _json_to_string(json_text: str) -> str | None:
        """JsonSerializer.Deserialize<string>(json): a JSON string or null."""
        node = parse_json(json_text)
        if node is not None and not isinstance(node, str):
            raise ValueError(f"The JSON value could not be converted to System.String: {json_text}")
        return node

    @staticmethod
    def _allergens_json(allergens: Mapping[str, AllergenPresence]) -> str:
        """JsonSerializer.Serialize(allergens.OrderBy(Key).ToDictionary(Key, Value.ToString())) - C# enum names as values."""
        return netjson.to_json_string({code: _PRESENCE_NAMES[presence] for code, presence in sorted(allergens.items(), key=lambda kv: kv[0])})

    @staticmethod
    def _label_canonical(
        selected: Mapping[str, _Selected], ingredients_text: str | None, dietary_marks: Iterable[str], net: Decimal | None, serving: Decimal | None
    ) -> str:
        """The normalized label (C# anonymous type {n, ingredientsText, allergens, DietaryMarks, net, serving}) hashed into label_hash."""
        nutrients = sorted((k, v[0]) for k, v in selected.items() if k.startswith("nutrient."))
        allergens = selected.get("allergens")
        return netjson.to_json_string({
            "n": [k + "=" + v for k, v in nutrients], "ingredientsText": ingredients_text, "allergens": None if allergens is None else allergens[0],
            "DietaryMarks": list(dietary_marks), "net": net, "serving": serving,
        })

    @staticmethod
    def _merge_nutrition(selected: Mapping[str, _Selected], basis: str, carb_definition: str, fallback: Nutrition) -> Nutrition:
        def get(code: str) -> Decimal | None:
            v = selected.get(f"nutrient.{code}.{basis}")
            return None if v is None else ProductIngestor._num(v[0])

        return Nutrition(
            basis=basis, carb_definition=carb_definition, energy_kcal=get("ENERC_KCAL"), protein_g=get("PROCNT"), carbohydrate_g=get("CHOCDF"),
            total_sugars_g=get("SUGAR"), added_sugars_g=get("SUGAD"), total_fat_g=get("FAT"), saturated_fat_g=get("FASAT"), trans_fat_g=get("FATRN"),
            cholesterol_mg=get("CHOLE"), sodium_mg=get("NA"), fibre_g=get("FIBTG"), fvln_pct=fallback.fvln_pct, fvln_estimated=fallback.fvln_estimated,
        )

    @staticmethod
    def _bronze_path(source_code: str, now: datetime, sha: bytes) -> str:
        """bronze/sources/{source}/{yyyy-MM-dd}/{first 16 hex digits of the SHA-256}.json (forward slashes)."""
        return "/".join(("bronze", "sources", source_code, f"{now.year:04d}-{now.month:02d}-{now.day:02d}", sha.hex()[:16] + ".json"))

    def _write_bronze(self, c: ProductCandidate, payload: bytes, sha: bytes) -> str:
        rel = ProductIngestor._bronze_path(c.source_code, self.clock.utc_now(), sha)
        if self.lake.enabled:
            full = Path(self.lake.root) / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            if not full.exists():
                full.write_bytes(payload)
        return "lake://" + rel
