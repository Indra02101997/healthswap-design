"""Jurisdiction packs (MarketPack) and the denormalized per-market health profile (Scoring/HealthProfiles.cs).

`HealthProfileBuilder.build` turns a canonical ProductData into the hot-cache document hp:{market}:{productId}: all
points-model scores, the HSI and its grade, front-of-pack bands, flags and badges, additive tiers and the US FDA "healthy"
check. Lists are tuples and C# dictionaries are dicts (insertion order kept, like the C# Dictionary enumeration).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from healthswap.domain.knowledge.parsing import to_upper_invariant
from healthswap.domain.knowledge.products import AllergenPresence, Nutrition, ProductData, decimal_div
from healthswap.domain.primitives import UTC, NetQuantity, as_utc
from healthswap.domain.scoring.models import (
    AdditiveRegulation,
    AdditiveTiers,
    FopBand,
    FopBands,
    FopThreshold,
    Hsi,
    HsiParams,
    ModelScore,
    PointsModel,
    PointsModelParams,
    _json_child,
    _json_decimal,
    _json_decimal_or_none,
    _json_int,
    _json_list,
    _json_map,
    _json_object,
    _json_props,
    _json_str,
    _json_str_or_none,
    _Reader,
)

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_MIN_UTC = datetime.min.replace(tzinfo=UTC)


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


# ---------------------------------------------------------------------------------------------------------------------
# Jurisdiction pack
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RulesParams:
    """Versioned eligibility-rule parameters per market (doc 03 section 3 defaults)."""

    version: str = "R-1.3"
    tau_sub: Decimal = Decimal("0.70")
    delta_min: Decimal = Decimal(8)
    limited_nutrient_tolerance: Decimal = Decimal("0.10")
    dismiss_cooldown_days: int = 7
    min_alternative_confidence: Decimal = Decimal("0.80")
    verified_allergen_confidence: Decimal = Decimal("0.98")
    target_ceiling_percentile: int = 60
    max_edges_per_product: int = 50
    retrieval_candidates: int = 200

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> RulesParams:
        """A pack's "rules" object (also part of the MARKET_PACK params)."""
        return RulesParams(**_json_props(_json_object(data, path, "RulesParams"), path, _RULES_PARAMS))


_RULES_PARAMS: tuple[tuple[str, str, _Reader], ...] = (
    ("version", "version", _json_str),
    ("tauSub", "tau_sub", _json_decimal),
    ("deltaMin", "delta_min", _json_decimal),
    ("limitedNutrientTolerance", "limited_nutrient_tolerance", _json_decimal),
    ("dismissCooldownDays", "dismiss_cooldown_days", _json_int),
    ("minAlternativeConfidence", "min_alternative_confidence", _json_decimal),
    ("verifiedAllergenConfidence", "verified_allergen_confidence", _json_decimal),
    ("targetCeilingPercentile", "target_ceiling_percentile", _json_int),
    ("maxEdgesPerProduct", "max_edges_per_product", _json_int),
    ("retrievalCandidates", "retrieval_candidates", _json_int),
)


@dataclass(frozen=True, slots=True)
class ClaimThreshold:
    claim_code: str
    nutrient: str
    physical_state: str
    basis: str
    max_value: Decimal | None
    min_value: Decimal | None
    min_relative_difference: Decimal | None

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> ClaimThreshold:
        """One entry of a pack's "claims" array."""
        v = _json_object(data, path, "ClaimThreshold")

        def text(name: str) -> str:
            return _json_str(v.get(name.lower()), _json_child(path, name))

        def number(name: str) -> Decimal | None:
            return _json_decimal_or_none(v.get(name.lower()), _json_child(path, name))

        return ClaimThreshold(
            text("claimCode"), text("nutrient"), text("physicalState"), text("basis"),
            number("maxValue"), number("minValue"), number("minRelativeDifference"),
        )


@dataclass(frozen=True, slots=True)
class UsHealthyGroup:
    group: str
    added_sugars_max_g: Decimal
    sodium_max_mg: Decimal
    sat_fat_max_g: Decimal
    required_flag: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketPack:
    """In-memory jurisdiction pack (reg.* + rec.scoring_model params) for one market."""

    market: str
    primary_model: str
    points_models: Mapping[str, PointsModelParams]
    hsi: HsiParams = field(default_factory=HsiParams)
    fop: tuple[FopThreshold, ...] = ()
    daily_values: Mapping[str, Decimal] = field(default_factory=dict)
    claims: tuple[ClaimThreshold, ...] = ()
    additives: Mapping[str, AdditiveRegulation] = field(default_factory=dict)
    rules: RulesParams = field(default_factory=RulesParams)
    us_healthy_by_category_prefix: Mapping[str, UsHealthyGroup] = field(default_factory=dict)
    methodology_url: str = "https://healthswap.example/methodology"
    pack_version: str = "1.0"

    @property
    def primary(self) -> PointsModelParams:
        return self.points_models[self.primary_model]

    def comparative_threshold(self, nutrient: str) -> Decimal:
        """Minimum relative difference for comparative wording ("less", "reduced", "more"); default Codex 25%."""
        c = next(
            (c for c in self.claims if c.claim_code in ("REDUCED", "INCREASED") and c.basis == "RELATIVE" and (c.nutrient == nutrient or c.nutrient == "ANY")),
            None,
        )
        return c.min_relative_difference if c is not None and c.min_relative_difference is not None else Decimal("0.25")

    def absolute_claim(self, claim: str, nutrient: str, liquid: bool) -> Decimal | None:
        state = "LIQUID" if liquid else "SOLID"
        c = next((c for c in self.claims if c.claim_code == claim and c.nutrient == nutrient and (c.physical_state == "ANY" or c.physical_state == state)), None)
        if c is None:
            return None
        return c.max_value if c.max_value is not None else c.min_value

    @staticmethod
    def from_pack_file(
        pack: Mapping[str, Any], models: Iterable[PointsModelParams], additives: Mapping[str, AdditiveRegulation] | None = None
    ) -> MarketPack:
        """The pack the platform serves for an already-parsed data/packs/XX.json.

        Equivalent to ReferenceDataLoader.ApplyPackAsync followed by ReferenceDataRepository.GetPackAsync: the
        "models" keys ("CODE@VERSION") select from `models` (keys without a model are skipped, like the SQL lookup) and the
        result is keyed by model code; `additives` normally comes from AdditiveRegulation.from_additives_file.
        """
        v = _json_object(pack, "", "PackFile", ("market", "packVersion", "primaryModel"))
        by_key = {f"{m.code}@{m.version}": m for m in models}
        points_models: dict[str, PointsModelParams] = {}
        for key in _json_list(v["models"], "models", _json_str) if "models" in v else ():
            model = by_key.get(key)
            if model is not None:
                points_models[model.code] = model
        us_healthy: dict[str, UsHealthyGroup] = {}
        for i, item in enumerate(_json_list(v["ushealthy"], "usHealthy", lambda x, p: x) if "ushealthy" in v else ()):
            path = f"usHealthy[{i}]"
            h = _json_object(item, path, "UsHealthyFile")
            prefix = _json_str(h.get("categoryprefix"), _json_child(path, "categoryPrefix"))
            if prefix in us_healthy:
                raise ValueError(f"An item with the same key has already been added. Key: {prefix}")

            def number(name: str, h: Mapping[str, Any] = h, path: str = path) -> Decimal:
                return _json_decimal(h[name.lower()], _json_child(path, name)) if name.lower() in h else _ZERO

            us_healthy[prefix] = UsHealthyGroup(
                _json_str(h.get("group"), _json_child(path, "group")), number("addedSugarsMaxG"), number("sodiumMaxMg"), number("satFatMaxG"),
                _json_str_or_none(h.get("requiredflag"), _json_child(path, "requiredFlag")),
            )
        return MarketPack(
            market=_json_str(v["market"], "market"),
            primary_model=_json_str(v["primarymodel"], "primaryModel"),
            points_models=points_models,
            hsi=HsiParams.from_dict(v["hsi"], "hsi") if "hsi" in v else HsiParams(),
            fop=_json_list(v["fop"], "fop", FopThreshold.from_dict) if "fop" in v else (),
            daily_values=_json_map(v["dailyvalues"], "dailyValues", _json_decimal) if "dailyvalues" in v else {},
            claims=_json_list(v["claims"], "claims", ClaimThreshold.from_dict) if "claims" in v else (),
            additives=dict(additives) if additives is not None else {},
            rules=RulesParams.from_dict(v["rules"], "rules") if "rules" in v else RulesParams(),
            us_healthy_by_category_prefix=us_healthy,
            methodology_url=_json_str(v["methodologyurl"], "methodologyUrl") if "methodologyurl" in v else "",
            pack_version=_json_str(v["packversion"], "packVersion"),
        )


# ---------------------------------------------------------------------------------------------------------------------
# Health profile
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdditiveDetail:
    code: str
    e_number: str | None
    name: str
    functional_class: str | None
    # Concern tier "A", "B", "C" or "D".
    tier: str


@dataclass(frozen=True, slots=True)
class BadgeData:
    code: str
    label: str
    tone: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthProfileData:
    """Denormalized health profile per product and market: the hot-cache document hp:{market}:{productId}."""

    product_id: uuid.UUID = uuid.UUID(int=0)
    market: str
    version_id: int = 0
    gtin: str | None = None
    name: str
    brand: str | None = None
    brand_tier: str = "MAINSTREAM"
    category_code: str
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    scoring_category: str = "GENERAL"
    physical_state: str = "SOLID"
    net_quantity: NetQuantity | None = None
    hsi: Decimal | None = None
    hsi_grade: str | None = None
    primary: ModelScore | None = None
    scores: tuple[ModelScore, ...] = ()
    nova: int | None = None
    nova_confidence: Decimal = _ZERO
    flags: tuple[str, ...] = ()
    badges: tuple[BadgeData, ...] = ()
    # Additive count per concern tier: keys "A", "B", "C", "D".
    additive_tier_counts: Mapping[str, int] = field(default_factory=dict)
    additives: tuple[AdditiveDetail, ...] = ()
    us_fda_healthy: bool | None = None
    data_sufficiency: str = "INSUFFICIENT"
    confidence: Decimal = _ZERO
    eligible: bool = False
    recalled: bool = False
    quarantined: bool = False
    nutrition: Nutrition = field(default_factory=Nutrition)
    serving_size: Decimal | None = None
    serving_unit: str | None = None
    fop_bands: Mapping[str, FopBand] = field(default_factory=dict)
    allergens: Mapping[str, AllergenPresence] = field(default_factory=dict)
    allergens_verified: bool = False
    allergen_confidence: Decimal = _ZERO
    diet_class: str = "UNKNOWN"
    jain_friendly_inferred: bool = False
    marks: tuple[str, ...] = ()
    ingredient_flags: tuple[str, ...] = ()
    form: str | None = None
    flavour_family: str | None = None
    use_occasion: str | None = None
    is_private_label: bool = False
    sources: tuple[str, ...] = ()
    profile_version: str
    computed_at: datetime = _MIN_UTC

    def tier_count(self, tier: str) -> int:
        return self.additive_tier_counts.get(tier, 0)

    def has_additive_tier(self, tier: str) -> bool:
        return self.tier_count(tier) > 0


# Ingredient flags copied onto the profile (the request-time rules and badges only need these).
_PROFILE_INGREDIENT_FLAGS = frozenset({"NNS", "ARTIFICIAL_COLOUR", "PALM_OIL", "ADDED_MSG", "CAFFEINE", "WHOLE_GRAIN", "REFINED_FLOUR", "PHO", "ADDED_SUGAR"})

_BADGES: dict[str, tuple[str, str]] = {
    "HIGH_SUGAR": ("High in sugar", "CAUTION"),
    "HIGH_SODIUM": ("High in sodium", "CAUTION"),
    "HIGH_SAT_FAT": ("High in saturated fat", "CAUTION"),
    "CONTAINS_TRANS_FAT": ("Contains trans fat", "CAUTION"),
    "ULTRA_PROCESSED": ("Ultra-processed", "NEUTRAL"),
    "ARTIFICIAL_COLOURS": ("Artificial colours", "NEUTRAL"),
    "NON_SUGAR_SWEETENERS": ("Contains sweeteners", "NEUTRAL"),
    "WHOLE_GRAIN": ("Whole grain", "POSITIVE"),
    "HIGH_FIBRE": ("High in fibre", "POSITIVE"),
    "HIGH_PROTEIN": ("High in protein", "POSITIVE"),
    "NO_ADDED_SUGAR": ("No added sugar", "POSITIVE"),
    "RECALLED": ("Recalled", "CAUTION"),
}


def _or(value: Decimal | None, default: int) -> Decimal | int:
    return value if value is not None else default


class HealthProfileBuilder:
    @staticmethod
    def build(
        p: ProductData, pack: MarketPack, now: datetime, category_name: str | None = None, sources: Sequence[str] | None = None
    ) -> HealthProfileData:
        scores = [PointsModel.score(m, p) for m in pack.points_models.values()]
        primary = next((s for s in scores if s.code == pack.primary_model), None)
        if primary is None:
            raise ValueError("Sequence contains no matching element")

        additive_details: list[AdditiveDetail] = []
        tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
        for code in dict.fromkeys(p.additives):
            reg = pack.additives.get(code)
            tier = AdditiveTiers.resolve(reg, pack.market)
            tier_counts[tier] += 1
            additive_details.append(
                AdditiveDetail(
                    code,
                    None if reg is None else "E" + to_upper_invariant(code),
                    reg.name if reg is not None and reg.name is not None else "INS " + code,
                    None if reg is None else next(iter(reg.functional_classes), None),
                    tier,
                )
            )

        tier_a = tier_counts["A"] > 0
        hsi = None if tier_a else Hsi.compute(primary.normalized, p.nova, tier_counts["B"], tier_counts["C"], pack.hsi)
        bands = FopBands.compute(p, pack.fop, pack.daily_values)
        flags = HealthProfileBuilder.compute_flags(p, pack, bands)
        if tier_a and "PROHIBITED_ADDITIVE" not in flags:
            flags = sorted([*flags, "PROHIBITED_ADDITIVE"], key=_ordinal)

        us_healthy: bool | None = None
        if pack.market == "US":
            group = next((g for prefix, g in pack.us_healthy_by_category_prefix.items() if p.category_code.startswith(prefix)), None)
            racc = p.serving_grams
            if group is not None and racc is not None:
                f = decimal_div(racc, _HUNDRED)
                n = p.nutrition
                us_healthy = (
                    _or(n.limited_sugars, 99) * f <= group.added_sugars_max_g
                    and _or(n.sodium, 9999) * f <= group.sodium_max_mg
                    and _or(n.saturated_fat_g, 99) * f <= group.sat_fat_max_g
                    and (group.required_flag is None or p.has(group.required_flag))
                )

        return HealthProfileData(
            product_id=p.product_id, market=pack.market, version_id=p.version_id, gtin=p.gtin, name=p.name, brand=p.brand,
            brand_tier=p.brand_tier, category_code=p.category_code, category_id=p.category_id, category_name=category_name,
            scoring_category=p.scoring_category, physical_state=p.physical_state, net_quantity=p.net_quantity,
            hsi=hsi, hsi_grade=Hsi.grade(hsi), primary=primary, scores=tuple(scores), nova=p.nova, nova_confidence=p.nova_confidence,
            flags=tuple(flags), badges=HealthProfileBuilder._badges(flags), additive_tier_counts=tier_counts, additives=tuple(additive_details),
            us_fda_healthy=us_healthy, data_sufficiency=p.data_sufficiency, confidence=p.confidence,
            eligible=not tier_a and not p.recalled and not p.quarantined and p.data_sufficiency != "INSUFFICIENT" and hsi is not None,
            recalled=p.recalled, quarantined=p.quarantined, nutrition=p.nutrition, serving_size=p.serving_size, serving_unit=p.serving_unit,
            fop_bands=bands, allergens=dict(p.allergens), allergens_verified=p.allergens_verified,
            allergen_confidence=p.allergen_confidence, diet_class=p.diet_class, jain_friendly_inferred=p.jain_friendly_inferred,
            marks=tuple(p.marks),
            ingredient_flags=tuple(f for f in p.ingredient_flags if f in _PROFILE_INGREDIENT_FLAGS),
            form=p.form, flavour_family=p.flavour_family, use_occasion=p.use_occasion, is_private_label=p.is_private_label,
            sources=tuple(sources) if sources is not None else (),
            profile_version=f"{pack.market}:{primary.code}@{primary.version}+HSI@{pack.hsi.version}#v{p.version_id}",
            computed_at=as_utc(now),
        )

    @staticmethod
    def compute_flags(p: ProductData, pack: MarketPack, bands: Mapping[str, FopBand]) -> list[str]:
        """Profile flags in ordinal order (the C# SortedSet<string>(StringComparer.Ordinal))."""
        flags: set[str] = set()
        low = FopBand.LOW
        if bands.get("SUGAR", low) == FopBand.HIGH or bands.get("SUGAD", low) == FopBand.HIGH:
            flags.add("HIGH_SUGAR")
        if bands.get("NA", low) == FopBand.HIGH or bands.get("SALT", low) == FopBand.HIGH:
            flags.add("HIGH_SODIUM")
        if bands.get("FASAT", low) == FopBand.HIGH:
            flags.add("HIGH_SAT_FAT")
        n = p.nutrition
        if _or(n.trans_fat_g, 0) > Decimal("0.2") or p.has("PHO"):
            flags.add("CONTAINS_TRANS_FAT")
        if p.nova == 4:
            flags.add("ULTRA_PROCESSED")
        if p.has("ARTIFICIAL_COLOUR"):
            flags.add("ARTIFICIAL_COLOURS")
        if p.has("NNS"):
            flags.add("NON_SUGAR_SWEETENERS")
        top = next((i for i in p.ingredients if i.parent_position is None), None)
        if top is not None and "WHOLE_GRAIN" in top.flags:
            flags.add("WHOLE_GRAIN")
        high_fibre = pack.absolute_claim("HIGH_FIBRE", "FIBTG", n.is_liquid)
        if _or(n.fibre_g, 0) >= (high_fibre if high_fibre is not None else Decimal(6)):
            flags.add("HIGH_FIBRE")
        prot, kcal = n.protein_g, n.energy_kcal
        if prot is not None and kcal is not None and kcal > 0 and decimal_div(prot * 4, kcal) >= Decimal("0.20"):
            flags.add("HIGH_PROTEIN")
        if (
            not p.has("ADDED_SUGAR") and not p.has("NNS") and p.ingredient_resolution >= Decimal("0.9")
            and _or(n.added_sugars_g, 0) == 0 and len(p.ingredients) > 0
        ):
            flags.add("NO_ADDED_SUGAR")
        if p.recalled:
            flags.add("RECALLED")
        return sorted(flags, key=_ordinal)

    @staticmethod
    def _badges(flags: Iterable[str]) -> tuple[BadgeData, ...]:
        return tuple(BadgeData(f, *_BADGES[f]) for f in flags if f in _BADGES)
