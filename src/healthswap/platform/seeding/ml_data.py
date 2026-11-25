"""Seeding/MlData.cs ported: training-data exports for ml/ (substitutability, entity resolution, ranker, LLM fine-tuning) and
the evaluation harness for fine-tuned LLM deployments. Features come from the same domain code used at serving time, so
there is no train/serve skew.

JSONL lines are written like the C# HotCache.Json lines: camelCase members in the C# order, nulls omitted, relaxed
escaping, decimals with their scale and the float32 feature vectors as System.Text.Json writes C# float values (the field
names and the feature order are those of Substitutability / EntityResolution / RankerFeatures FEATURE_NAMES). Files use
the platform newline (StreamWriter.WriteLine).

Port notes:
    * Random draws follow the C# call order, but Python's generators differ from System.Random: the seeded streams
      (entity-resolution variants with Random(42), `simulate --seed`) are deterministic and repeatable, not identical to
      the .NET files. The far-negative sampling (C# Random.Shared) uses an unseeded generator unless `rng` is passed.
    * The category-map query aliases its two `code` columns (rows are dicts here; Dapper read the tuple by position).
    * simulate raises ValueError where the C# threw InvalidOperationException (no in-stock catalog items).
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from healthswap.domain.knowledge.parsing import equals_ignore_case, to_upper_invariant
from healthswap.domain.knowledge.products import decimal_div
from healthswap.domain.knowledge.quality import EntityResolution
from healthswap.domain.recommendation.explanations import ExplanationTemplates, ExplanationValidator, FactSheets
from healthswap.domain.recommendation.ranking import Substitutability
from healthswap.domain.scoring.health_profiles import HealthProfileData
from healthswap.domain.text import round_double, to_float32
from healthswap.platform import jsonutil, netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.db import Db, DbRole
from healthswap.platform.hot_cache import HotCache
from healthswap.platform.knowledge.jobs import ExplanationPregen, _fact_json, _json_string
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository
from healthswap.platform.llm.gateway import LlmGateway, _array
from healthswap.platform.llm.mock import _facts
from healthswap.platform.services.evaluation import CartEvaluationRequest, CartLine, EvaluationService, MoneyInput
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory

__all__ = ["EvalReport", "MlData"]

log = logging.getLogger(__name__)

_HALF = Decimal("0.5")
_TWO = Decimal(2)
_INT_MAX = 2**31 - 1  # Random.Next() returns [0, int.MaxValue)
_PLACEMENTS = ("CART", "CHECKOUT", "CART", "AGENT")


def _features(values: Sequence[float]) -> list[netjson.Single]:
    """A C# float[] for the JSON lines (float32 text)."""
    return [netjson.Single(to_float32(v)) for v in values]


def _write_line(w: TextIO, value: Mapping[str, Any]) -> None:
    """StreamWriter.WriteLine(JsonSerializer.Serialize(value, HotCache.Json)): compact, nulls omitted."""
    w.write(netjson.serialize_hs(value) + "\n")


@dataclass(frozen=True, slots=True)
class EvalReport:
    """MlData.EvalReport: T5 acceptance through the production validator chain."""

    examples: int
    accepted: int
    acceptance_rate: float
    failed_checks: dict[str, int]
    mean_latency_ms: float
    tokens_in: int
    tokens_out: int
    model_ref: str


class MlData:
    """
    Training-data exports for ml/ (substitutability, entity resolution, ranker, LLM fine-tuning) and the evaluation harness for
    fine-tuned LLM deployments. Features are produced by the same domain code used at serving time.
    """

    def __init__(self, db: Db, refs: ReferenceDataRepository, cache: HotCache, evaluations: EvaluationService, pregen: ExplanationPregen,
                 llm: LlmGateway, partners: PartnerDirectory) -> None:
        self.db = db
        self.refs = refs
        self.cache = cache
        self.evaluations = evaluations
        self.pregen = pregen
        self.llm = llm
        self.partners = partners

    @property
    def provider(self) -> str:
        """The LLM provider name (C# Provider)."""
        return self.llm.provider_name

    def _profiles(self) -> list[HealthProfileData]:
        rows = self.db.in_tx(DbRole.KB_WRITER, None, lambda u: u.query("select profile_doc::text from rec.health_profile order by market, product_id"))
        return [HotCache.from_json(HealthProfileData, r["profile_doc"]) for r in rows]

    @staticmethod
    def _distinct_by_product(profiles: Sequence[HealthProfileData]) -> list[HealthProfileData]:
        """DistinctBy(p => p.ProductId): the first profile of each product, in order of first appearance."""
        first: dict[uuid.UUID, HealthProfileData] = {}
        for p in profiles:
            first.setdefault(p.product_id, p)
        return list(first.values())

    # ---- export -----------------------------------------------------------------------------------------------------

    def export(self, out_dir: str | Path, rng: random.Random | None = None) -> dict[str, int]:
        """Writes substitutability_pairs, er_pairs, explanation_facts, category_examples and products (.jsonl) to `out_dir`;
        returns the line count per file. `rng` drives the far-negative sampling of the substitutability pairs (C# Random.Shared)."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        shared = rng if rng is not None else random.Random()
        profiles = self._profiles()
        counts: dict[str, int] = {}
        maps = {
            (r["market"], r["from_code"], r["to_code"]) for r in self.db.in_tx(DbRole.KB_WRITER, None, lambda u: u.query(
                "select s.market, f.code as from_code, t.code as to_code from rec.category_substitution s join kb.category f on f.category_id = s.from_category_id "
                "join kb.category t on t.category_id = s.to_category_id"))
        }
        by_market: dict[str, list[HealthProfileData]] = {}
        for p in profiles:
            by_market.setdefault(p.market, []).append(p)

        # 1. Substitutability pairs (doc 03 section 6). Labels: synthetic "gold" from curated attributes (form, use occasion, diet class,
        #    pack size) - replace with curator-labelled pairs and T4 judgments in production.
        pairs = 0
        with open(out / "substitutability_pairs.jsonl", "w", encoding="utf-8") as w:
            for market, group in by_market.items():
                for a in group:
                    for b in group:
                        if b.product_id == a.product_id:
                            continue
                        approved = (market, a.category_code, b.category_code) in maps
                        distance = Substitutability.category_distance(a.category_code, b.category_code, approved)
                        if distance > 2 and shared.random() > 0.15:
                            continue  # keep some far negatives
                        f = Substitutability.features(a, b, distance)
                        _write_line(w, {
                            "market": market, "from": str(a.product_id), "to": str(b.product_id), "fromName": a.name, "toName": b.name,
                            "features": _features(f), "label": 1 if self.gold_substitutable(a, b, distance) else 0, "heuristic": Substitutability.heuristic(f),
                        })
                        pairs += 1
        counts["substitutability_pairs"] = pairs

        # 2. Entity-resolution pairs (doc 02 7.3): noisy listing variants of the same product vs different products in the same category.
        er = 0
        er_rng = random.Random(42)
        distinct = self._distinct_by_product(profiles)
        with open(out / "er_pairs.jsonl", "w", encoding="utf-8") as w:
            for p in distinct:
                original = EntityResolution.Candidate(p.name, p.brand, p.net_quantity, p.nutrition, p.ingredient_flags, p.gtin)
                for _ in range(4):
                    name = self.noisy(p.name, er_rng)
                    brand = None if er_rng.random() < 0.2 else p.brand
                    size = None if er_rng.random() < 0.1 else p.net_quantity
                    nutrition = None if er_rng.random() < 0.5 else p.nutrition
                    variant = EntityResolution.Candidate(name, brand, size, nutrition, p.ingredient_flags, None)
                    _write_line(w, {"a": p.name, "b": variant.name, "features": _features(EntityResolution.features(original, variant)), "label": 1})
                    er += 1
                others = self._distinct_by_product([o for o in profiles if o.product_id != p.product_id and o.category_code == p.category_code])[:4]
                for other in others:
                    candidate = EntityResolution.Candidate(other.name, other.brand, other.net_quantity, other.nutrition, other.ingredient_flags,
                                                           other.gtin if er_rng.random() < 0.3 else None)
                    compared = dataclasses.replace(candidate, gtin=None if candidate.gtin is None else p.gtin)
                    _write_line(w, {"a": p.name, "b": other.name, "features": _features(EntityResolution.features(original, compared)), "label": 0})
                    er += 1
        counts["er_pairs"] = er

        # 3. LLM fine-tuning sources: explanation fact sheets (T5), substitutability attributes (T4), product titles (T3).
        expl = 0
        with open(out / "explanation_facts.jsonl", "w", encoding="utf-8") as w:
            for market, group in by_market.items():
                pack = self.refs.get_pack(market)
                by_id: dict[uuid.UUID, HealthProfileData] = {}
                for p in group:
                    if p.product_id in by_id:
                        raise ValueError(f"An item with the same key has already been added. Key: {p.product_id}")
                    by_id[p.product_id] = p
                edges = self.db.in_tx(DbRole.KB_WRITER, None, lambda u, m=market: u.query(
                    "select e.from_product_id, e.to_product_id from rec.v_active_swap_edge e join rec.swap_graph_version g using (graph_version) where g.market = @m",
                    {"m": m}))
                for e in edges:
                    a, b = by_id.get(e["from_product_id"]), by_id.get(e["to_product_id"])
                    if a is None or b is None:
                        continue
                    sheet = FactSheets.build(a, b, pack)
                    selected = FactSheets.select(sheet)
                    if not selected:
                        continue
                    for locale in ApiMapper.locales_for(market):
                        template = ExplanationTemplates.render(selected, locale)
                        _write_line(w, {
                            "market": market, "locale": locale, "basis": sheet.basis, "facts": [_fact_json(f) for f in selected],
                            "template": {"short": template.short, "bullets": list(template.bullets)},
                        })
                        expl += 1
        counts["explanation_facts"] = expl

        with open(out / "category_examples.jsonl", "w", encoding="utf-8") as w:
            for p in distinct:
                _write_line(w, {"name": p.name, "brand": p.brand, "market": p.market, "category": p.category_code})
        counts["category_examples"] = len(distinct)

        with open(out / "products.jsonl", "w", encoding="utf-8") as w:
            for p in profiles:
                _write_line(w, {
                    "market": p.market, "productId": str(p.product_id), "name": p.name, "brand": p.brand, "categoryCode": p.category_code, "hsi": p.hsi,
                    "hsiGrade": p.hsi_grade, "nova": p.nova, "flags": list(p.flags), "form": p.form, "flavourFamily": p.flavour_family,
                    "useOccasion": p.use_occasion, "dietClass": p.diet_class, "netQuantity": None if p.net_quantity is None else p.net_quantity.value,
                    "netUnit": None if p.net_quantity is None else p.net_quantity.unit, "physicalState": p.physical_state, "brandTier": p.brand_tier,
                })
        counts["products"] = len(profiles)
        log.info("Exported ML data to %s: %s", out, ", ".join(f"{k}={v}" for k, v in counts.items()))
        return counts

    @staticmethod
    def gold_substitutable(a: HealthProfileData, b: HealthProfileData, distance: int) -> bool:
        """Synthetic gold label: same form and use occasion, compatible diet class, pack within 0.5-2x, or an approved cross-category map."""
        if distance > 2 or a.physical_state != b.physical_state:
            return False
        if (a.diet_class in ("VEG", "VEGAN")) != (b.diet_class in ("VEG", "VEGAN")):
            return False
        sa, sb = a.net_quantity, b.net_quantity
        if sa is not None and sb is not None and sa.is_comparable_to(sb) and sa.value > 0:
            ratio = decimal_div(sb.value, sa.value)
            if ratio < _HALF or ratio > _TWO:
                return False
        if a.use_occasion is not None and b.use_occasion is not None and a.use_occasion != b.use_occasion:
            return False
        if distance == 2:
            return True
        return a.form is None or b.form is None or a.form == b.form

    @staticmethod
    def noisy(name: str, rng: random.Random) -> str:
        """A noisy listing title of the same product: upper case, Pk abbreviation, swapped words, size moved to the end or a pack suffix."""
        words = [x for x in name.split(" ") if x]
        case = rng.randrange(5)
        if case == 0:
            return to_upper_invariant(name)
        if case == 1:
            return " ".join("Pk" if equals_ignore_case(x, "Pack") else x.replace("Whole Wheat", "WW") for x in words)
        if case == 2:
            if len(words) > 3:
                words[1], words[2] = words[2], words[1]
            return " ".join(words)
        if case == 3:
            return " ".join(x for x in words if not any(c.isdecimal() for c in x)) + " " + "".join(x for x in words if any(c.isdecimal() for c in x))
        return name.replace(" g", "g").replace(" ml", "ml") + " (Pack of 1)"

    # ---- ranker simulation ------------------------------------------------------------------------------------------

    @staticmethod
    def acceptance_logit(features: Sequence[float], rank: int, placement: str) -> float:
        """Hidden "true" acceptance model: substitutability, health gain, price premium, brand loyalty and position."""
        f = features
        return (-1.2 + 2.2 * f[1] + 0.035 * f[0] - 1.6 * max(0.0, to_float32(f[2] - 1)) + 0.4 * f[15] - 0.55 * (rank - 1)
                + (-0.2 if placement == "CHECKOUT" else 0.0) + 0.3 * f[12])

    def simulate(self, partner_id: uuid.UUID, sessions: int, out_file: str | Path, seed: int) -> int:
        """
        Simulates shopper sessions against the real evaluation pipeline and logs ranker training rows (ranker_events.jsonl) with a
        synthetic acceptance model (impressions are positional; acceptance depends on substitutability, health gain, price and rank).
        """
        partner = self.partners.get(partner_id)
        market = partner.markets[0]
        currency = partner.config.currency
        catalog = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.query(
            "select i.partner_sku, i.store_id, i.price from tenant.store_inventory i join tenant.catalog_item c on c.partner_id = i.partner_id and c.partner_sku = i.partner_sku "
            "where i.in_stock and c.is_listed"))
        if not catalog:
            raise ValueError("Seed the demo partner first (no in-stock catalog items)")
        rng = random.Random(seed)
        ctx = PartnerContext(partner_id=partner_id, client_id="simulator", scopes=frozenset({"carts.evaluate"}))
        path = Path(out_file)
        path.resolve().parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with open(path, "w", encoding="utf-8") as w:
            for s in range(sessions):
                store = catalog[rng.randrange(len(catalog))]["store_id"]
                take = rng.randint(1, 4)
                in_store = [c for c in catalog if c["store_id"] == store]
                keys = [rng.randrange(_INT_MAX) for _ in in_store]
                picks = [c for _, c in sorted(zip(keys, in_store, strict=True), key=lambda t: t[0])][:take]
                placement = _PLACEMENTS[rng.randrange(4)]
                result = self.evaluations.evaluate(ctx, CartEvaluationRequest(
                    cart_ref=f"sim-{seed}-{s}", market=market, currency=currency, store_id=store, placement=placement,
                    lines=tuple(
                        CartLine(line_id=str(i + 1), partner_sku=p["partner_sku"], quantity=Decimal(rng.randint(1, 2)),
                                 unit_price=MoneyInput(**ApiMapper.money(p["price"], currency)))
                        for i, p in enumerate(picks)
                    ),
                ))
                for sug in result.get("suggestions") or []:
                    snapshot = self.cache.get_suggestion(uuid.UUID(sug["suggestionId"]))
                    for alt in sug["alternatives"]:
                        rank = alt["rank"]
                        features = None if snapshot is None else next((a.features for a in snapshot.alternatives if a.rank == rank), None)
                        if features is None:
                            continue
                        p_accept = 1 / (1 + math.exp(-self.acceptance_logit(features, rank, placement)))
                        accepted = 1 if rng.random() < p_accept else 0
                        _write_line(w, {
                            "suggestionId": sug["suggestionId"], "rank": rank, "placement": placement, "features": _features(features), "label": accepted,
                            "propensity": 1.0,
                        })
                        rows += 1
        log.info("Simulated %d sessions, %d ranker rows -> %s", sessions, rows, out_file)
        return rows

    # ---- LLM evaluation ---------------------------------------------------------------------------------------------

    def evaluate_explanations(self, input_file: str | Path, max_examples: int = 200) -> EvalReport:
        """
        Fine-tuned T5 evaluation through the production validator chain (schema, numeric fidelity, banned claims, language,
        mandatory disclosures). Input lines come from explanation_facts.jsonl (or a held-out split of the fine-tuning set).
        """
        n = ok = tokens_in = tokens_out = latency = 0
        failed: dict[str, int] = {}
        model_ref = ""
        with open(input_file, encoding="utf-8-sig") as f:  # File.ReadLines: UTF-8, a byte order mark is skipped
            for raw in itertools.islice(f, max(0, max_examples)):
                root = jsonutil.loads(raw.rstrip("\n"))
                facts = _facts(root["facts"])
                locale = root["locale"]
                basis = root["basis"] if "basis" in root else "PER_100G"
                _text, accepted, result = self.pregen.polish(facts, basis, locale)
                n += 1
                tokens_in += result.tokens_in
                tokens_out += result.tokens_out
                latency += result.latency_ms
                model_ref = result.model_ref
                if accepted:
                    ok += 1
                    continue
                o = result.output
                if isinstance(o, dict):
                    bullets = o.get("bullets")
                    report = ExplanationValidator.validate(
                        _json_string(o.get("short")) or "", [] if bullets is None else [_json_string(x) or "" for x in _array(bullets)], facts, locale,
                    )
                    for check, passed in report.checks.items():
                        if not passed:
                            failed[check] = failed.get(check, 0) + 1
                else:
                    failed["schema"] = failed.get("schema", 0) + 1
        return EvalReport(
            n, ok, 0.0 if n == 0 else round_double(ok / n, 4), failed, 0.0 if n == 0 else round_double(latency / n, 1), tokens_in, tokens_out, model_ref,
        )
