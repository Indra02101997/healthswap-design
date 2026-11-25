"""Offline knowledge jobs (C# Knowledge/Jobs.cs): scoring (HLD 6.3), swap-graph builds (HLD 6.4, doc 03 sections 3-6) and
explanation pre-generation (doc 03 sections 8-9).

Port notes:
    * SQL, thresholds, version strings and the blue/green cache swap are those of the C# jobs. LLM calls (T4 judge, T5
      polish, T7 embeddings) and their audit rows never run inside an open transaction; the C# jobs already kept them
      outside, so no restructuring was needed.
    * Stored internal documents follow the Python hot-cache convention (HotCache.to_json: snake_case keys, decimals as
      strings): rec.health_profile.profile_doc and rec.swap_edge.features ({"from_version", "to_version", "to_category",
      "introduced_tier_c"}). Texts that are hashed, cached or compared keep the exact C# System.Text.Json form: the
      product_score inputs hash (the nutrition as HotCache.Json writes it, computed properties included), the T4 payload
      (default options, PascalCase "A"/"B" envelope), the T5 payload and rec.explanation.details (HotCache.Json:
      camelCase, nulls omitted, relaxed escaping, decimals with their scale), the validation report and score components.
    * Ordering follows the C# LINQ semantics: stable sorts, Guid ordering (= uuid.UUID ordering), Dictionary/GroupBy in
      first-insertion order, HashSet as insertion-ordered dict keys where the order becomes visible (diagnostics).
"""

from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import reduce
from operator import add
from typing import Any

from healthswap.domain.knowledge.products import Nutrition, decimal_div, decimal_mul, round_decimal
from healthswap.domain.primitives import Clock
from healthswap.domain.recommendation.explanations import ExplanationTemplates, ExplanationValidator, Fact, FactSheets, RenderedExplanation
from healthswap.domain.recommendation.ranking import Substitutability
from healthswap.domain.recommendation.rules import StaticRules
from healthswap.domain.scoring.health_profiles import HealthProfileBuilder, HealthProfileData, MarketPack
from healthswap.domain.scoring.models import ModelScore
from healthswap.domain.text import HashingEmbedder, TextSimilarity, _utf8, double_to_decimal
from healthswap.platform import netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole
from healthswap.platform.errors import HsError
from healthswap.platform.hot_cache import CachedExplanation, HotCache, SwapEdge
from healthswap.platform.knowledge.kb_reader import KbReader
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository
from healthswap.platform.llm.gateway import LlmGateway, LlmResult, LlmTask, PromptRegistry, _array, _conversion_error, _get
from healthswap.platform.ml.registry import ModelRegistry
from healthswap.platform.settings import GraphOptions

log = logging.getLogger(__name__)

_HEALTH_PROFILES_SQL = "select profile_doc::text from rec.health_profile where market = @market"


def _first_column(rows: list[dict[str, Any]]) -> list[Any]:
    """Dapper QueryAsync<T> of a single-column result."""
    return [next(iter(r.values())) for r in rows]


def _profiles(docs: Sequence[str]) -> list[HealthProfileData]:
    return [HotCache.from_json(HealthProfileData, d) for d in docs]


def _by_product(profiles: Sequence[HealthProfileData]) -> dict[uuid.UUID, HealthProfileData]:
    """profiles.ToDictionary(p => p.ProductId)."""
    result: dict[uuid.UUID, HealthProfileData] = {}
    for p in profiles:
        if p.product_id in result:
            raise ValueError(f"An item with the same key has already been added. Key: {p.product_id}")
        result[p.product_id] = p
    return result


def _nutrition_json(n: Nutrition) -> str:
    """JsonSerializer.Serialize(nutrition, HotCache.Json): camelCase in declaration order, nulls omitted, and the computed
    properties (IsLiquid, Sodium, Salt, EnergyKj, AvailableCarbohydrate, LimitedSugars) that System.Text.Json also writes."""
    return netjson.serialize_hs({
        "basis": n.basis, "energyKcal": n.energy_kcal, "proteinG": n.protein_g, "carbohydrateG": n.carbohydrate_g, "carbDefinition": n.carb_definition,
        "totalSugarsG": n.total_sugars_g, "addedSugarsG": n.added_sugars_g, "totalFatG": n.total_fat_g, "saturatedFatG": n.saturated_fat_g,
        "transFatG": n.trans_fat_g, "cholesterolMg": n.cholesterol_mg, "sodiumMg": n.sodium_mg, "saltG": n.salt_g, "fibreG": n.fibre_g,
        "fvlnPct": n.fvln_pct, "fvlnEstimated": n.fvln_estimated, "isLiquid": n.is_liquid, "sodium": n.sodium, "salt": n.salt, "energyKj": n.energy_kj,
        "availableCarbohydrate": n.available_carbohydrate, "limitedSugars": n.limited_sugars,
    })


def _by_category(profiles: Sequence[HealthProfileData]) -> dict[uuid.UUID, list[HealthProfileData]]:
    """profiles.Where(CategoryId is not null).GroupBy(CategoryId): groups in order of first appearance."""
    groups: dict[uuid.UUID, list[HealthProfileData]] = {}
    for p in profiles:
        if p.category_id is not None:
            groups.setdefault(p.category_id, []).append(p)
    return groups


# ---------------------------------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoringReport:
    market: str
    products: int
    eligible: int
    changed: int
    categories: int


class ScoringJob:
    """
    Scoring service (HLD 6.3): jurisdiction models, NOVA, additive tiers and HSI per product and market; writes
    rec.product_score, rec.health_profile (read model) and rec.category_stats (p60 ceilings), then refreshes the hot cache.
    """

    def __init__(self, db: Db, reader: KbReader, refs: ReferenceDataRepository, cache: HotCache, clock: Clock) -> None:
        self.db = db
        self.reader = reader
        self.refs = refs
        self.cache = cache
        self.clock = clock

    def run_all(self) -> list[ScoringReport]:
        return [self.run(market) for market in self.refs.markets()]

    def run(self, market: str) -> ScoringReport:
        self.refs.invalidate()
        pack = self.refs.get_pack(market)
        products = self.reader.load(market, None)
        now = self.clock.utc_now()
        profiles = [HealthProfileBuilder.build(p.data, pack, now, p.category_name, p.sources) for p in products]
        q = decimal_div(Decimal(pack.rules.target_ceiling_percentile), Decimal(100))

        changed = 0
        categories = 0
        with self.db.tx(DbRole.KB_WRITER) as u:
            previous: dict[uuid.UUID, dict[str, Any]] = {}
            for row in u.query(
                "select product_id, profile_version, profile_doc::text as profile_doc from rec.health_profile where market = @market", {"market": market}
            ):
                if row["product_id"] in previous:  # ToDictionary
                    raise ValueError(f"An item with the same key has already been added. Key: {row['product_id']}")
                previous[row["product_id"]] = row
            model_ids: dict[str, uuid.UUID] = {}
            for row in u.query("select model_id, code, version from rec.scoring_model where status = 'ACTIVE'"):
                key = row["code"] + "@" + row["version"]
                if key in model_ids:
                    raise ValueError(f"An item with the same key has already been added. Key: {key}")
                model_ids[key] = row["model_id"]
            for hp in profiles:
                for s in hp.scores:
                    model_id = model_ids.get(s.code + "@" + s.version)
                    if model_id is not None:
                        u.execute(
                            """
                            insert into rec.product_score (version_id, model_id, market, score, grade, components, inputs_hash, computed_at)
                            values (@v, @modelId, @market, @score, @grade, @components::jsonb, @hash, @now)
                            on conflict (version_id, model_id, market) do update set score = excluded.score, grade = excluded.grade,
                                components = excluded.components, inputs_hash = excluded.inputs_hash, computed_at = excluded.computed_at
                            """,
                            {
                                "v": hp.version_id, "modelId": model_id, "market": market, "score": s.value, "grade": s.grade,
                                "components": netjson.to_json_string(dict(s.components)), "hash": ScoringJob._inputs_hash(hp, s), "now": now,
                            },
                        )

                doc = HotCache.to_json(hp)
                u.execute(
                    """
                    insert into rec.health_profile (product_id, market, version_id, hsi, hsi_grade, primary_model_code, primary_score, primary_grade, nova_group,
                        flags, additive_tiers, us_fda_healthy, data_sufficient, eligible, profile_version, computed_at, category_id, profile_doc)
                    values (@ProductId, @market, @VersionId, @Hsi, @HsiGrade, @model, @score, @grade, @nova, @flags, @tiers::jsonb, @UsFdaHealthy,
                        @sufficient, @Eligible, @ProfileVersion, @now, @CategoryId, @doc::jsonb)
                    on conflict (product_id, market) do update set version_id = excluded.version_id, hsi = excluded.hsi, hsi_grade = excluded.hsi_grade,
                        primary_model_code = excluded.primary_model_code, primary_score = excluded.primary_score, primary_grade = excluded.primary_grade,
                        nova_group = excluded.nova_group, flags = excluded.flags, additive_tiers = excluded.additive_tiers, us_fda_healthy = excluded.us_fda_healthy,
                        data_sufficient = excluded.data_sufficient, eligible = excluded.eligible, profile_version = excluded.profile_version,
                        computed_at = excluded.computed_at, category_id = excluded.category_id, profile_doc = excluded.profile_doc
                    """,
                    {
                        "ProductId": hp.product_id, "market": market, "VersionId": hp.version_id, "Hsi": hp.hsi, "HsiGrade": hp.hsi_grade,
                        "model": None if hp.primary is None else hp.primary.code, "score": None if hp.primary is None else hp.primary.value,
                        "grade": None if hp.primary is None else hp.primary.grade, "nova": hp.nova, "flags": list(hp.flags),
                        "tiers": netjson.to_json_string(dict(hp.additive_tier_counts)), "UsFdaHealthy": hp.us_fda_healthy,
                        "sufficient": hp.data_sufficiency == "SUFFICIENT", "Eligible": hp.eligible, "ProfileVersion": hp.profile_version, "now": now,
                        "CategoryId": hp.category_id, "doc": doc,
                    },
                )

                prev = previous.get(hp.product_id)
                prev_doc = None if prev is None else HotCache.from_json(HealthProfileData, prev["profile_doc"])
                changes = ScoringJob.changes(prev_doc, hp)
                if changes:
                    changed += 1
                    Outbox.enqueue(
                        u, None, Topics.KB_PRODUCT_CHANGES, str(hp.product_id),
                        {
                            "kind": "PROFILE_CHANGED", "productId": hp.product_id, "market": market,
                            "previousProfileVersion": None if prev is None else prev["profile_version"], "profileVersion": hp.profile_version, "changes": changes,
                        },
                    )

            for category_id, group in _by_category(profiles).items():
                hsis = sorted(p.hsi for p in group if p.hsi is not None)
                categories += 1
                u.execute(
                    """
                    insert into rec.category_stats (market, category_id, product_count, hsi_p60, hsi_mean, computed_at) values (@market, @cat, @n, @p60, @mean, @now)
                    on conflict (market, category_id) do update set product_count = excluded.product_count, hsi_p60 = excluded.hsi_p60, hsi_mean = excluded.hsi_mean,
                        computed_at = excluded.computed_at
                    """,
                    {
                        "market": market, "cat": category_id, "n": len(group), "p60": ScoringJob.percentile(hsis, q),
                        "mean": None if not hsis else round_decimal(decimal_div(reduce(add, hsis), Decimal(len(hsis))), 2), "now": now,
                    },
                )

        for hp in profiles:
            self.cache.set_profile(hp)
            if hp.gtin is not None:
                self.cache.set_gtin(hp.gtin, hp.product_id)
        for category_id, group in _by_category(profiles).items():
            p60 = ScoringJob.percentile(sorted(p.hsi for p in group if p.hsi is not None), q)
            if p60 is not None:
                self.cache.set_category_ceiling(market, category_id, p60)

        report = ScoringReport(market, len(profiles), sum(1 for p in profiles if p.eligible), changed, categories)
        log.info("Scored %s: %d products, %d eligible, %d changed", market, report.products, report.eligible, report.changed)
        return report

    @staticmethod
    def percentile(sorted_values: Sequence[Decimal], q: Decimal) -> Decimal | None:
        """Linear-interpolated percentile (PERCENTILE_CONT) of ascending values, rounded to 2 places; None for empty input."""
        if len(sorted_values) == 0:
            return None
        if len(sorted_values) == 1:
            return sorted_values[0]
        pos = decimal_mul(q, Decimal(len(sorted_values) - 1))
        lo = int(pos.to_integral_value(rounding=ROUND_FLOOR))
        hi = int(pos.to_integral_value(rounding=ROUND_CEILING))
        return round_decimal(sorted_values[lo] + decimal_mul(sorted_values[hi] - sorted_values[lo], pos - lo), 2)

    @staticmethod
    def changes(prev: HealthProfileData | None, now: HealthProfileData) -> list[str]:
        """Change kinds of a PROFILE_CHANGED event (CREATED for a first profile)."""
        if prev is None:
            return ["CREATED"]
        changes: list[str] = []
        if prev.version_id != now.version_id:
            changes.append("NUTRITION_UPDATED")
        if dict(prev.allergens) != dict(now.allergens):
            changes.append("ALLERGENS_UPDATED")
        if prev.profile_version != now.profile_version and prev.version_id == now.version_id:
            changes.append("SCORE_MODEL_UPDATED")
        if not prev.recalled and now.recalled:
            changes.append("RECALL_FLAGGED")
        if prev.hsi != now.hsi or prev.hsi_grade != now.hsi_grade or tuple(prev.flags) != tuple(now.flags):
            changes.append("SCORE_CHANGED")
        if prev.eligible != now.eligible:
            changes.append("ELIGIBILITY_CHANGED")
        return changes

    @staticmethod
    def _inputs_hash(hp: HealthProfileData, s: ModelScore) -> bytes:
        """SHA-256 of "{code}@{version}|{versionId}|{nutrition JSON}|{additive codes}|{nova}", byte-identical to C#."""
        nova = "" if hp.nova is None else str(hp.nova)
        text = f"{s.code}@{s.version}|{hp.version_id}|{_nutrition_json(hp.nutrition)}|{','.join(a.code for a in hp.additives)}|{nova}"
        return hashlib.sha256(_utf8(text)).digest()


# ---------------------------------------------------------------------------------------------------------------------
# Swap graph
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphBuildReport:
    market: str
    graph_version: str
    status: str
    products: int
    edges: int
    validation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SwapDiagnosis:
    to: uuid.UUID
    name: str
    category: str
    hsi: Decimal | None
    health_delta: Decimal
    model_substitutability: Decimal
    judge_score: Decimal | None
    substitutability: Decimal
    category_allowed: bool
    eligible: bool
    failed_rules: tuple[str, ...]
    reason_codes: tuple[str, ...]
    in_active_graph: bool


# One scored edge before validation: (edge, brand tier of the alternative, substitutability features).
_ScoredEdge = tuple[SwapEdge, str, list[float]]


def judge_attributes(name: str, category: str, form: str | None, flavour_family: str | None, use_occasion: str | None, diet_class: str | None,
                     net_quantity: Decimal | int | None) -> dict[str, Any]:
    """One product as the T4 judge sees it (no health or price data). Also used by ml/finetune/build_datasets.py."""
    return {
        "name": name, "category": category, "form": form, "flavourFamily": flavour_family, "useOccasion": use_occasion,
        "dietClass": diet_class, "netQuantity": net_quantity,
    }


def judge_payload(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    """The T4 payload (inside <products>): product A (intended) and B (candidate); default JSON escaping, nulls written."""
    return netjson.to_json_string({"A": a, "B": b})


def _judge_attributes(p: HealthProfileData) -> dict[str, Any]:
    return judge_attributes(
        p.name, p.category_code, p.form, p.flavour_family, p.use_occasion, p.diet_class, None if p.net_quantity is None else p.net_quantity.value,
    )


def _try_double(node: Any) -> float | None:
    """JsonValue.TryGetValue<double>: numbers only, and only when they are finite doubles."""
    if isinstance(node, bool) or not isinstance(node, (int, Decimal)):
        return None
    try:
        value = float(node)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _g6(x: float) -> str:
    """float.ToString("G6", CultureInfo.InvariantCulture): 6 significant digits, .NET exponent style (E-05)."""
    return format(x, ".6g").replace("e", "E")


class SwapGraphBuilder:
    """
    Swap-graph builder (HLD 6.4, doc 03 sections 3-6): blocking by category and approved maps, embedding retrieval, static
    rules R1-R4/R7, substitutability model, top-K with brand-tier diversity, validation gates and blue/green activation.
    """

    SwapDiagnosis = SwapDiagnosis

    def __init__(
        self, db: Db, refs: ReferenceDataRepository, models: ModelRegistry, llm: LlmGateway, cache: HotCache, options: GraphOptions, clock: Clock
    ) -> None:
        self.db = db
        self.refs = refs
        self.models = models
        self.llm = llm
        self.cache = cache
        self.options = options
        self.clock = clock

    def build_all(self) -> list[GraphBuildReport]:
        return [self.build(market) for market in self.refs.markets()]

    def build(self, market: str) -> GraphBuildReport:
        pack = self.refs.get_pack(market)
        rules = pack.rules
        with self.db.tx(DbRole.KB_WRITER) as u:
            docs = _first_column(u.query(_HEALTH_PROFILES_SQL, {"market": market}))
            maps = u.query(
                """
                select f.code as from_code, t.code as to_code from rec.category_substitution s
                join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id where s.market = @market
                """,
                {"market": market},
            )
        profiles = _profiles(docs)
        approved: dict[str, dict[str, None]] = {}
        for m in maps:
            approved.setdefault(m["from_code"], {})[m["to_code"]] = None

        # Embeddings for retrieval (T7). Stored in rec.product_embedding for pgvector HNSW search in production.
        texts = [Substitutability.describe(p) for p in profiles]
        vectors = [] if len(texts) == 0 else self.llm.embed(texts)
        embedding: dict[uuid.UUID, list[float]] = {}
        for i, p in enumerate(profiles):
            if p.product_id in embedding:
                raise ValueError(f"An item with the same key has already been added. Key: {p.product_id}")
            embedding[p.product_id] = vectors[i]

        now = self.clock.utc_now()
        taken = self.db.in_tx(
            DbRole.KB_WRITER, None,
            lambda u: u.scalar(
                "select exists (select 1 from rec.swap_graph_version where graph_version = @graphVersion)",
                {"graphVersion": SwapGraphBuilder._graph_version(market, now, False)},
            ),
        )
        graph_version = SwapGraphBuilder._graph_version(market, now, bool(taken))
        edges: dict[uuid.UUID, list[_ScoredEdge]] = {}
        judged = 0
        for a in profiles:
            if a.hsi is None:
                continue
            targets = approved.get(a.category_code, {})
            scored: list[_ScoredEdge] = []
            for b in SwapGraphBuilder._candidates(a, profiles, targets, embedding, rules.retrieval_candidates):
                edge, diagnosis, features = self._evaluate_pair(a, b, targets, pack, market)
                if diagnosis.judge_score is not None:
                    judged += 1
                if edge is not None:
                    scored.append((edge, b.brand_tier, features))
            if scored:
                edges[a.product_id] = SwapGraphBuilder._diversify(scored, rules.max_edges_per_product)

        # Validation gates (doc 03 section 6): never an edge into an ineligible product, every edge above the minimum delta.
        by_id = _by_product(profiles)
        all_edges = [(from_id, e[0]) for from_id, scored in edges.items() for e in scored]
        into_ineligible = sum(1 for _, e in all_edges if not by_id[e.to].eligible)
        below_delta = sum(1 for _, e in all_edges if e.health_delta < rules.delta_min)
        targets_with_edges = len(edges)
        validation: dict[str, Any] = {
            "edges": len(all_edges), "productsWithEdges": targets_with_edges, "edgesIntoIneligible": into_ineligible, "edgesBelowDeltaMin": below_delta,
            "llmJudged": judged, "substitutabilityModel": self.models.substitutability_version, "rulesVersion": rules.version,
        }
        passed = into_ineligible == 0 and below_delta == 0 and (len(all_edges) > 0 or sum(1 for p in profiles if p.eligible) < 2)
        status = "ACTIVE" if passed else "FAILED"
        t7 = self.llm.options.deployments.get("T7")
        model_ref = HashingEmbedder.MODEL_REF if self.llm.provider_name == "mock" else f"{self.llm.provider_name}/{'' if t7 is None else t7}"

        with self.db.tx(DbRole.KB_WRITER) as u:
            u.execute(
                "insert into rec.swap_graph_version (graph_version, market, rules_version, hsi_model_version, status, edge_count, validation_report) values (@graphVersion, @market, @rv, @hv, 'BUILDING', 0, '{}'::jsonb)",
                {"graphVersion": graph_version, "market": market, "rv": rules.version, "hv": pack.hsi.version},
            )
            for from_id, e in all_edges:
                u.execute(
                    """
                    insert into rec.swap_edge (graph_version, from_product_id, to_product_id, health_delta, substitutability, reason_codes, features)
                    values (@graphVersion, @from, @to, @delta, @sub, @reasons, @features::jsonb)
                    """,
                    {
                        "graphVersion": graph_version, "from": from_id, "to": e.to, "delta": e.health_delta, "sub": e.substitutability, "reasons": list(e.reasons),
                        "features": HotCache.to_json({
                            "from_version": e.from_version, "to_version": e.to_version, "to_category": e.to_category, "introduced_tier_c": list(e.introduced_tier_c),
                        }),
                    },
                )
            u.execute(
                "update rec.swap_graph_version set status = 'VALIDATED', edge_count = @n, validation_report = @report::jsonb where graph_version = @graphVersion",
                {"graphVersion": graph_version, "n": len(all_edges), "report": netjson.to_json_string(validation)},
            )
            if passed:
                u.execute("update rec.swap_graph_version set status = 'RETIRED' where market = @market and status = 'ACTIVE'", {"market": market})
                u.execute("update rec.swap_graph_version set status = 'ACTIVE', activated_at = @now where graph_version = @graphVersion", {"graphVersion": graph_version, "now": now})
            else:
                u.execute("update rec.swap_graph_version set status = 'FAILED' where graph_version = @graphVersion", {"graphVersion": graph_version})
            for product_id, vector in embedding.items():
                u.execute(
                    """
                    insert into rec.product_embedding (product_id, model_ref, embedding) values (@id, @model, @v::vector)
                    on conflict (product_id, model_ref) do update set embedding = excluded.embedding, created_at = now()
                    """,
                    {"id": product_id, "model": model_ref, "v": "[" + ",".join(_g6(x) for x in vector) + "]"},
                )

        if passed:
            # Blue/green: write the new version's keys first, flip the pointer, then delete the old version's keys.
            old = self.cache.get_active_graph(market)
            for from_id, scored in edges.items():
                self.cache.set_edges(market, graph_version, from_id, [x[0] for x in scored])
            self.cache.set_active_graph(market, graph_version)
            if old is not None and old != graph_version:
                self.cache.delete_graph(market, old)
        log.info("Swap graph %s: %s, %d edges for %d products", graph_version, status, len(all_edges), targets_with_edges)
        return GraphBuildReport(market, graph_version, status, targets_with_edges, len(all_edges), validation)

    @staticmethod
    def _graph_version(market: str, now: datetime, taken: bool) -> str:
        """{market}-{yyyy-MM-ddTHH:mm:ss}Z, plus ".fff" (milliseconds) when that version already exists."""
        version = f"{market}-{now.year:04d}-{now.month:02d}-{now.day:02d}T{now.hour:02d}:{now.minute:02d}:{now.second:02d}Z"
        return version + f".{now.microsecond // 1000:03d}" if taken else version

    @staticmethod
    def _candidates(
        a: HealthProfileData, profiles: Sequence[HealthProfileData], targets: Mapping[str, Any], embedding: Mapping[uuid.UUID, Sequence[float]], max_count: int
    ) -> list[HealthProfileData]:
        """Blocking + retrieval: same category or an approved target category, eligible, healthier; nearest by embedding."""
        pool = [
            b for b in profiles
            if b.product_id != a.product_id and b.eligible and b.hsi is not None and a.hsi is not None and b.hsi > a.hsi
            and (b.category_code == a.category_code or b.category_code in targets)
        ]
        ea = embedding[a.product_id]
        pool.sort(key=lambda b: TextSimilarity.cosine(ea, embedding[b.product_id]), reverse=True)  # stable, like OrderByDescending
        return pool[: max(max_count, 0)]

    def _evaluate_pair(
        self, a: HealthProfileData, b: HealthProfileData, targets: Mapping[str, Any], pack: MarketPack, market: str
    ) -> tuple[SwapEdge | None, SwapDiagnosis, list[float]]:
        """One candidate pair: substitutability model, T4 judge for the uncertain band, static rules R1-R4/R7."""
        allowed = b.category_code == a.category_code or b.category_code in targets
        features = Substitutability.features(a, b, Substitutability.category_distance(a.category_code, b.category_code, b.category_code in targets))
        model = self.models.score_substitutability([features])[0]
        sub = model
        judge: Decimal | None = None
        if self.options.llm_judge_uncertain and self.options.uncertain_low <= sub < self.options.uncertain_high:
            judge = self._judge(a, b, market)
            if judge is not None:
                sub = round_decimal(decimal_div(sub + judge, Decimal(2)), 3)
        check = StaticRules.check(a, b, sub, allowed, pack)
        diagnosis = SwapDiagnosis(
            b.product_id, b.name, b.category_code, b.hsi, check.health_delta, model, judge, sub, allowed, check.eligible, tuple(check.failed),
            tuple(check.reason_codes), False,
        )
        if not check.eligible:
            return None, diagnosis, features
        introduced_c = tuple(x.code for x in b.additives if x.tier == "C" and all(y.code != x.code for y in a.additives))
        edge = SwapEdge(
            b.product_id, round_decimal(check.health_delta, 2), round_decimal(sub, 3), tuple(check.reason_codes), a.version_id, b.version_id, b.category_code,
            introduced_c,
        )
        return edge, diagnosis, features

    def diagnose(self, market: str, from_product_id: uuid.UUID) -> dict[str, Any]:
        """Admin diagnostics: why a product does or does not get alternatives (every candidate with its rule results).

        Returns the C# anonymous object as an API dict: {"from": {...}, "rules": {...}, "candidates": [SwapDiagnosis...]}."""
        pack = self.refs.get_pack(market)
        with self.db.tx(DbRole.KB_WRITER) as u:
            docs = _first_column(u.query(_HEALTH_PROFILES_SQL, {"market": market}))
            maps = u.query(
                "select f.code as from_code, t.code as to_code from rec.category_substitution s join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id where s.market = @market",
                {"market": market},
            )
            active = set(_first_column(u.query(
                "select e.to_product_id from rec.v_active_swap_edge e join rec.swap_graph_version g using (graph_version) where g.market = @market and e.from_product_id = @fromProductId",
                {"market": market, "fromProductId": from_product_id},
            )))
        profiles = _profiles(docs)
        a = next((p for p in profiles if p.product_id == from_product_id), None)
        if a is None:
            raise HsError.not_found(f"Product {from_product_id}")
        targets = dict.fromkeys(m["to_code"] for m in maps if m["from_code"] == a.category_code)
        pool = [b for b in profiles if b.product_id != a.product_id and (b.category_code == a.category_code or b.category_code in targets)]
        results: list[SwapDiagnosis] = []
        for b in pool:
            _, d, _ = self._evaluate_pair(a, b, targets, pack, market)
            results.append(replace(d, in_active_graph=b.product_id in active))
        return {
            "from": {
                "productId": a.product_id, "name": a.name, "categoryCode": a.category_code, "hsi": a.hsi, "hsiGrade": a.hsi_grade, "eligible": a.eligible,
                "dataSufficiency": a.data_sufficiency, "approvedTargetCategories": list(targets),
            },
            "rules": {"version": pack.rules.version, "tauSub": pack.rules.tau_sub, "deltaMin": pack.rules.delta_min},
            "candidates": sorted(results, key=lambda r: (not r.eligible, -r.health_delta)),
        }

    @staticmethod
    def _diversify(scored: Sequence[_ScoredEdge], max_count: int) -> list[_ScoredEdge]:
        """Keeps the best edges by health gain x substitutability while ensuring other brand tiers stay represented."""

        def key(e: _ScoredEdge) -> tuple[Decimal, uuid.UUID]:  # OrderByDescending(delta x sub).ThenBy(To)
            return -decimal_mul(e[0].health_delta, e[0].substitutability), e[0].to

        ordered = sorted(scored, key=key)
        top = ordered[: max(max_count, 0)]
        for tier in dict.fromkeys(e[1] for e in ordered):
            if any(e[1] == tier for e in top):
                continue
            best = next(e for e in ordered if e[1] == tier)
            if len(top) >= max_count:
                top.pop()  # List.RemoveAt(Count - 1)
            top.append(best)
        return sorted(top, key=key)

    def _judge(self, a: HealthProfileData, b: HealthProfileData, market: str) -> Decimal | None:
        payload = judge_payload(_judge_attributes(a), _judge_attributes(b))
        spec = PromptRegistry.substitutability_judge(market)
        r = self.llm.run(spec, spec.user_content(payload))
        node = None if r.output is None else _get(r.output, "score")
        d = None if node is None or isinstance(node, (dict, list)) else _try_double(node)
        score = None if d is None else round_decimal(double_to_decimal(d), 3)
        in_range = score is not None and 0 <= score <= 1
        valid = r.ok and in_range
        if not r.cached:
            self.llm.audit(LlmTask.T4, r, payload, {"schema": r.ok, "range": in_range}, valid)
        return score if valid else None


# ---------------------------------------------------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExplanationReport:
    market: str
    pairs: int
    rendered: int
    llm_accepted: int
    llm_rejected: int


def _fact_json(f: Fact) -> dict[str, Any]:
    """A Fact as HotCache.Json writes it (camelCase, declaration order; None members are dropped by serialize_hs)."""
    return {
        "id": f.id, "kind": f.kind, "nutrient": f.nutrient, "from": f.from_, "to": f.to, "unit": f.unit, "basis": f.basis, "relChange": f.rel_change,
        "direction": f.direction, "impact": f.impact, "claimEligible": f.claim_eligible, "mustDisclose": f.must_disclose, "codes": list(f.codes),
    }


def _json_string(node: Any) -> str | None:
    """node?.GetValue<string>() on a parsed JsonNode."""
    if node is None or isinstance(node, str):
        return node
    raise _conversion_error(node, "System.String")


def explanation_payload(facts: Sequence[Fact], basis: str, locale: str) -> str:
    """The T5 payload (inside <facts>): basis, locale and the selected facts as the hot cache writes them (relaxed escaping,
    null members omitted). Also used by ml/finetune/build_datasets.py."""
    return netjson.serialize_hs({"basis": basis, "locale": locale, "facts": [_fact_json(f) for f in facts]})


# (from, to, locale, fact sheet, rendered text, model ref, prompt version)
_ExplanationRow = tuple[uuid.UUID, uuid.UUID, str, Any, RenderedExplanation, str | None, str | None]


class ExplanationPregen:
    """
    Explanation pre-generation (doc 03 sections 8-9): deterministic fact sheet, template rendering, optional LLM polish (T5)
    through the validator chain; failures fall back to the template. Results are stored in rec.explanation and the hot cache.
    """

    def __init__(self, db: Db, refs: ReferenceDataRepository, llm: LlmGateway, cache: HotCache, clock: Clock) -> None:
        self.db = db
        self.refs = refs
        self.llm = llm
        self.cache = cache
        self.clock = clock

    def run_all(self) -> list[ExplanationReport]:
        return [self.run(market) for market in self.refs.markets()]

    def run(self, market: str) -> ExplanationReport:
        pack = self.refs.get_pack(market)
        with self.db.tx(DbRole.KB_WRITER) as u:
            edges = u.query(
                "select from_product_id, to_product_id from rec.v_active_swap_edge e join rec.swap_graph_version g using (graph_version) where g.market = @market",
                {"market": market},
            )
            docs = _first_column(u.query(_HEALTH_PROFILES_SQL, {"market": market}))
        profiles = _by_product(_profiles(docs))
        rendered = accepted = rejected = 0
        rows: list[_ExplanationRow] = []
        for e in edges:
            a = profiles.get(e["from_product_id"])
            b = profiles.get(e["to_product_id"])
            if a is None or b is None:
                continue
            sheet = FactSheets.build(a, b, pack)
            selected = FactSheets.select(sheet)
            if len(selected) == 0:
                continue
            for locale in ApiMapper.locales_for(market):
                text = ExplanationTemplates.render(selected, locale)
                model_ref: str | None = None
                prompt: str | None = None
                if self.llm.options.enable_explanation_polish:
                    polished, ok, r = self.polish(selected, sheet.basis, locale)
                    if ok and polished is not None:
                        text = polished
                        model_ref = r.model_ref
                        prompt = r.prompt_version
                        accepted += 1
                    else:
                        rejected += 1
                rows.append((a.product_id, b.product_id, locale, sheet, text, model_ref, prompt))
                rendered += 1

        now = self.clock.utc_now()
        with self.db.tx(DbRole.KB_WRITER) as u:
            for from_id, to_id, locale, sheet, text, model_ref, prompt in rows:
                u.execute(
                    """
                    insert into rec.explanation (market, locale, from_product_id, to_product_id, facts_hash, short_text, details, generator, model_ref, prompt_version, validated_at)
                    values (@market, @locale, @from, @to, decode(@hash, 'hex'), @short, @details::jsonb, @generator, @modelRef, @prompt, @now)
                    on conflict (market, locale, from_product_id, to_product_id, facts_hash) do update set short_text = excluded.short_text, details = excluded.details,
                        generator = excluded.generator, model_ref = excluded.model_ref, prompt_version = excluded.prompt_version, validated_at = excluded.validated_at
                    """,
                    {
                        "market": market, "locale": locale, "from": from_id, "to": to_id, "hash": sheet.hash, "short": text.short,
                        "details": netjson.serialize_hs({
                            "bullets": list(text.bullets), "factIdsUsed": list(text.fact_ids_used), "facts": [_fact_json(f) for f in sheet.facts],
                        }),
                        "generator": text.generator, "modelRef": model_ref, "prompt": prompt, "now": now,
                    },
                )
        for from_id, to_id, locale, sheet, text, model_ref, _prompt in rows:
            self.cache.set_explanation(
                market, locale, from_id, to_id, sheet.hash, CachedExplanation(text.short, tuple(text.bullets), text.generator, text.ai_generated, model_ref)
            )
        log.info("Explanations %s: %d rendered, LLM accepted %d, rejected %d", market, rendered, accepted, rejected)
        return ExplanationReport(market, len(edges), rendered, accepted, rejected)

    def polish(self, facts: Sequence[Fact], basis: str, locale: str) -> tuple[RenderedExplanation | None, bool, LlmResult]:
        """T5 polish + validator chain. Exposed for the admin plane and the fine-tuning evaluation harness."""
        payload = explanation_payload(facts, basis, locale)
        spec = PromptRegistry.explanation_writer(locale)
        r = self.llm.run(spec, spec.user_content(payload))
        if not r.ok or not isinstance(r.output, dict):
            if not r.cached:
                self.llm.audit(LlmTask.T5, r, payload, {"schema": False}, False)
            return None, False, r
        o = r.output
        short_text = _json_string(o.get("short"))
        bullets_node = o.get("bullets")
        bullets = [] if bullets_node is None else [_json_string(x) or "" for x in _array(bullets_node)]
        report = ExplanationValidator.validate("" if short_text is None else short_text, bullets, facts, locale)
        if not r.cached:
            self.llm.audit(LlmTask.T5, r, payload, report.checks, report.passed)
        if not report.passed:
            return None, False, r
        return RenderedExplanation("" if short_text is None else short_text, tuple(bullets), tuple(f.id for f in facts), "LLM_VALIDATED", True, locale), True, r
